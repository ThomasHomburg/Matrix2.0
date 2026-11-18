"""FX-Baustein (v69, F2 Schritt A): datumsbasierte historische Wechselkurse.

Semantik (verbindlich):
    rate(base, quote_cur, date) = Einheiten von *quote_cur* je Einheit von
    *base* (z. B. EUR->USD: USD je EUR). convert_* multipliziert den Wert
    mit dem jeweiligen Kurs. Ist das direkte Paar nicht verfuegbar, wird das
    inverse Paar mit reziprokem Kurs verwendet (1/rate), nur bei valider
    Abfrage.

Kursarten (eindeutig unterscheidbar, nie still vertauschbar):
    - Stichtagskurs (Bestandsgroessen):   quote() / convert_stock()
      letzter verfuegbarer Kurs am oder VOR dem Bilanzstichtag
      (Look-ahead-Schutz: Kurse nach dem Stichtag werden nie verwendet).
    - Periodendurchschnitt (Stromgroessen): flow_quote() / convert_flow()
      arithmetischer Durchschnitt der taeglichen Schlusskurse im Fenster
      [start, end] (Werktage-Basis). Zulaessig nur bei ausreichender
      Abdeckung (n_closes/Werktage >= fx.min_flow_coverage UND groesste
      zusammenhaengende Luecke <= fx.max_flow_gap_days). Bei unzureichender
      Abdeckung NUR dann ein ausdruecklich gekennzeichneter Jahresendkurs-
      Naeherungswert (basis="jahresendkurs_naeherung"), wenn das
      Fensterende innerhalb fx.naeherung_stichtag_tol_days um das
      Jahresende liegt -- niemals als Periodendurchschnitt deklariert und
      niemals ein beliebiger Stichtagskurs aus Fenstermitte/-anfang. Sonst
      NaN.
    - Aktueller Kurs (Marktwerte):        quote_current() / convert_current()
      letzter Kurs <= heute mit Alter <= fx.current_max_age_days;
      aeltere Kurse -> NaN (z. B. shares_current unbestimmbar).

Quellen-Prioritaet je Anfrage: manuell (fx_rates.yaml) -> SQLite-Cache
(fx_rates.db, Tabelle fx_daily) -> FMP-API (nur mit API-Key).
Alle Kurse werden zentral validiert (endlich und strikt > 0). Ungueltige
manuelle Kurse werden mit Warnung ignoriert; ungueltige Cache-Eintrage
werden geloescht und (bei vorhandenem Key) neu abgerufen; nur valide
Kurse werden gespeichert. Fehlende Kurse -> NaN; niemals still 1.0 und
niemals stillschweigend ein beliebiger Kurs als Ersatz fuer unzureichende
Abdeckung.

fx_rates.yaml (optional; abwaertskompatibel), Notierung je Paar =
1 base = rate quote:
    EURUSD:
      "2026-06-30": 1.08      # datumsbasiert (Stichtag), Look-ahead-safe
      "2026": 1.05            # legacy: Jahresendkurs (Naeherung)
      "avg:2025": 1.06        # Perioden-O, NUR wenn Anfragefenster
                              # EXAKT das volle Kalenderjahr 2025 ist
                              # (01.01.-31.12.), nie fuer Teilfenster

build_fx(...) ist die zentrale Factory und liefert IMMER einen FXConverter;
ohne FMP-API-Key bleiben manuelle Kurse und Cache nutzbar, nur das
Nachladen neuer Kurse entfaellt. FMP-Endpunkt:
/api/v3/historical-price-full/{BASEQUOTE}?from&to (taegliche Schlusskurse,
Tarif-abhaengig; Fehler/leere Historie -> NaN, kein Absturz).
"""

from __future__ import annotations

import datetime as _dt
import logging
import math
import os
import sqlite3
from dataclasses import dataclass
from typing import Any

from .fmp_client import FMPClient, FMPError

logger = logging.getLogger("value_screener.fx")

FX_DEFAULTS = {
    "cache_file": "fx_rates.db",
    "manual_path": "fx_rates.yaml",
    "api_timeout_s": 10.0,
    "min_flow_coverage": 0.80,
    "max_flow_gap_days": 10,
    "naeherung_stichtag_tol_days": 14,
    "current_max_age_days": 5,
}

_NAN = float("nan")


def _valid_rate(r: Any) -> bool:
    """Zentrale Kursvalidierung: endlich UND strikt positiv."""
    try:
        v = float(r)
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0.0


def _to_date(d: "int | str | _dt.date | _dt.datetime | None") -> "_dt.date | None":
    """Normalisiert Jahr (legacy: 31.12. = Jahresendkurs), ISO-String, date/datetime."""
    if d is None:
        return None
    if isinstance(d, _dt.datetime):
        return d.date()
    if isinstance(d, _dt.date):
        return d
    if isinstance(d, int):
        return _dt.date(d, 12, 31)
    s = str(d).strip()
    if not s:
        return None
    try:
        return _dt.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _weekdays(start: _dt.date, end: _dt.date) -> int:
    """Werktage (Mo-Fr) im Fenster [start, end] inklusive Raender."""
    if end < start:
        return 0
    days = (end - start).days + 1
    full_weeks, rest = divmod(days, 7)
    wd = full_weeks * 5
    for i in range(rest):
        if (start.weekday() + full_weeks * 7 + i) % 7 < 5:
            wd += 1
    return wd


def _year_end(d: "_dt.date | int") -> _dt.date:
    if isinstance(d, int):
        return _dt.date(d, 12, 31)
    return _dt.date(d.year, 12, 31)


# --------------------------------------------------------------------------- #
# Status-/Quellenobjekte
# --------------------------------------------------------------------------- #

@dataclass
class FxQuote:
    """Stichtagskurs einer Abfrage inkl. Herkunft (rate NaN -> nicht verfuegbar)."""
    rate: float = _NAN
    asof: str = ""            # Kursdatum (ISO); immer <= angefragtes Datum
    source: str = ""          # manual | cache | api | identity
    inverse: bool = False     # True: reziproker Kurs aus inversem Paar
    missing: str = ""         # Ursache bei NaN: waehrung_unbekannt |
                              # kein_kurs_verfuegbar | kurs_zu_alt | ...

    @property
    def ok(self) -> bool:
        return _valid_rate(self.rate)


@dataclass
class FxFlowQuote:
    """Periodendurchschnitt inkl. Abdeckungs-/Basis-/Quellenstatus."""
    rate: float = _NAN
    start: str = ""
    end: str = ""
    source: str = ""          # manual | cache | api | identity
    basis: str = ""           # periodendurchschnitt | jahresendkurs_naeherung
    coverage: float = _NAN    # n_closes/Werktage; NaN bei deklariertem avg
    max_gap_days: int = -1
    n_closes: int = -1
    inverse: bool = False
    asof: str = ""            # Datum des Naeherungskurses (nur bei Naeherung)

    @property
    def ok(self) -> bool:
        return _valid_rate(self.rate)


# --------------------------------------------------------------------------- #
# Manuelle Kurse (fx_rates.yaml)
# --------------------------------------------------------------------------- #

def _load_manual_rates(path: str) -> dict[str, dict[str, float]]:
    """Laedt datumsbasierte/legacy manuelle Kurse; ungueltige ignoriert + Warnung.

    Rueckgabe: {PAIR: {iso-date: rate, "avg:YYYY": rate}} --
    Legacy-Jahres-Eintraege ("2026": r) werden als Stichtag 31.12. abgelegt.
    """
    out: dict[str, dict[str, float]] = {}
    if not path or not os.path.exists(path):
        return out
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as exc:  # noqa: BLE001 -- Dateifehler -> keine manuellen Kurse
        logger.warning("fx_rates.yaml nicht lesbar (%s): manuelle Kurse fehlen", exc)
        return out
    if not isinstance(raw, dict):
        logger.warning("fx_rates.yaml: Wurzel ist kein Mapping (%s): "
                       "manuelle Kurse fehlen", type(raw).__name__)
        return out
    for pair, entries in raw.items():
        if not isinstance(entries, dict):
            continue
        pair_key = str(pair).upper().strip()
        clean: dict[str, float] = {}
        for key, rate in entries.items():
            ks = str(key).strip()
            if ks.lower().startswith("avg:"):
                if _valid_rate(rate):
                    clean[ks] = float(rate)
                else:
                    logger.warning("fx_rates.yaml: ungueltiger Kurs %s/%s=%s ignoriert",
                                   pair_key, ks, rate)
                continue
            # legacy-Jahr ("2026") oder ISO-Datum ("2026-06-30")
            if ks.isdigit() and len(ks) == 4:
                d = _dt.date(int(ks), 12, 31)
            else:
                d = _to_date(ks)
            if d is None:
                logger.warning("fx_rates.yaml: ungueltiges Datum %s/%s ignoriert",
                               pair_key, ks)
                continue
            if not _valid_rate(rate):
                logger.warning("fx_rates.yaml: ungueltiger Kurs %s/%s=%s ignoriert",
                               pair_key, ks, rate)
                continue
            clean[d.isoformat()] = float(rate)
        if clean:
            out[pair_key] = clean
    return out


# --------------------------------------------------------------------------- #
# SQLite-Cache: taegliche Kurse
# --------------------------------------------------------------------------- #

class FXCache:
    """SQLite-Cache fuer taegliche Schlusskurse (fx_rates.db, Tabelle fx_daily).

    Nur valide Kurse werden gespeichert; beim Lesen wird je Kurs validiert;
    ungueltige Eintraege koennen ignoriert/geloescht werden.
    """

    SCHEMA_DAILY = """
    CREATE TABLE IF NOT EXISTS fx_daily (
        pair TEXT NOT NULL,
        date TEXT NOT NULL,
        rate REAL NOT NULL,
        source TEXT NOT NULL,
        PRIMARY KEY (pair, date)
    );"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        with self._connect() as conn:
            conn.execute(self.SCHEMA_DAILY)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def daily(self, pair: str, start: _dt.date, end: _dt.date) -> dict[str, float]:
        """Gueltige Cache-Kurse des Paars im Fenster [start, end] (ISO -> rate)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, rate FROM fx_daily WHERE pair=? AND date>=? AND date<=?",
                (pair.upper(), start.isoformat(), end.isoformat()),
            ).fetchall()
        return {str(d)[:10]: float(r) for d, r in rows if _valid_rate(r)}

    def put_daily(self, pair: str, closes: dict[str, float], source: str) -> int:
        """Speichert nur valide Kurse (Validierung vor dem Schreiben)."""
        n = 0
        with self._connect() as conn:
            for d, r in closes.items():
                dd = str(d)[:10]
                if _to_date(dd) is None or not _valid_rate(r):
                    logger.warning("FX-Cache: ungueltiger Kurs %s/%s=%s nicht gespeichert",
                                   pair, dd, r)
                    continue
                conn.execute(
                    """INSERT INTO fx_daily (pair, date, rate, source)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(pair, date) DO UPDATE SET
                       rate=excluded.rate, source=excluded.source""",
                    (pair.upper(), dd, float(r), source))
                n += 1
        return n

    def put_daily_if_absent(self, pair: str, closes: dict[str, float],
                            source: str) -> int:
        """Ergaenzt nur FEHLENDE Tage: vorhandene Eintraege desselben
        (Paar, Datum) werden NIE ueberschrieben (persistente Cache-Prioritaet).
        Validierung identisch zu put_daily (nur valide Kurse)."""
        n = 0
        with self._connect() as conn:
            for d, r in closes.items():
                dd = str(d)[:10]
                if _to_date(dd) is None or not _valid_rate(r):
                    logger.warning("FX-Cache: ungueltiger Kurs %s/%s=%s nicht gespeichert",
                                   pair, dd, r)
                    continue
                cur = conn.execute(
                    """INSERT INTO fx_daily (pair, date, rate, source)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(pair, date) DO NOTHING""",
                    (pair.upper(), dd, float(r), source))
                n += cur.rowcount
        return n

    def purge_invalid_daily(self, pair: str, start: _dt.date,
                            end: _dt.date) -> int:
        """Loescht ungueltige Cache-Zeilen im Fenster [start, end].

        Ungueltige Eintraege blocksieren sonst put_daily_if_absent()
        (ON CONFLICT DO NOTHING) und verhindern so das persistente Ersetzen
        durch valide API-Kurse. Rueckgabe: Anzahl geloeschter Zeilen.
        """
        bad: list[str] = []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, rate FROM fx_daily "
                "WHERE pair=? AND date>=? AND date<=?",
                (pair.upper(), start.isoformat(), end.isoformat()),
            ).fetchall()
            for d, r in rows:
                if not _valid_rate(r):
                    bad.append(str(d)[:10])
            for b in bad:
                conn.execute("DELETE FROM fx_daily WHERE pair=? AND date=?",
                             (pair.upper(), b))
        for b in bad:
            logger.warning("FX-Cache: ungueltiger Eintrag %s/%s geloescht",
                           pair.upper(), b)
        return len(bad)

    def delete_daily(self, pair: str, d: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM fx_daily WHERE pair=? AND date=?",
                         (pair.upper(), str(d)[:10]))

    def last_daily(self, pair: str, on_or_before: _dt.date) -> "tuple[str, float] | None":
        """Letzter Cache-Kurs <= Datum (unvalidiert -- Aufrufer validiert)."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT date, rate FROM fx_daily WHERE pair=? AND date<=? "
                "ORDER BY date DESC LIMIT 1",
                (pair.upper(), on_or_before.isoformat()),
            ).fetchone()
        return (str(row[0])[:10], float(row[1])) if row else None


# --------------------------------------------------------------------------- #
# FXConverter
# --------------------------------------------------------------------------- #

class FXConverter:
    """Datumsbasierte FX-Konvertierung fuer Strom-, Bestands- und Marktwerte.

    client=None ist zulaessig (ohne API-Key): manuelle Kurse und Cache
    bleiben nutzbar; fehlende Kurse -> NaN.
    """

    def __init__(self, client: "FMPClient | None" = None,
                 cache: "FXCache | None" = None,
                 manual_path: str = FX_DEFAULTS["manual_path"],
                 cfg: dict | None = None) -> None:
        self.client = client
        self.cache = cache
        c = dict(FX_DEFAULTS)
        if isinstance(cfg, dict):
            fx_cfg = cfg.get("fx", cfg) or {}
            for k in FX_DEFAULTS:
                if fx_cfg.get(k) is not None:
                    c[k] = fx_cfg[k]
        self.cfg = c
        if client is not None:
            try:
                client.timeout = float(c["api_timeout_s"])
            except (AttributeError, TypeError, ValueError):
                pass
        self.manual = _load_manual_rates(manual_path or str(c["manual_path"]))

    # ---------------- Paar-/Kursquellen-Hilfen ---------------- #

    @staticmethod
    def _pair(base: str, quote_cur: str) -> str:
        return f"{base}{quote_cur}"

    def _manual_stichtag(self, pair: str, on_or_before: _dt.date
                         ) -> "tuple[_dt.date, float] | None":
        """Letzter manueller Stichtagskurs <= Datum (nie spaeter)."""
        best = None
        for d, r in self.manual.get(pair, {}).items():
            if d.startswith("avg:"):
                continue
            dd = _to_date(d)
            if dd is None or dd > on_or_before or not _valid_rate(r):
                continue     # Look-ahead-Schutz + zentrale Kursvalidierung
            if best is None or dd > best[0]:
                best = (dd, float(r))
        return best

    def _manual_flow_avg(self, pair: str, start: _dt.date, end: _dt.date
                         ) -> "float | None":
        """avg:YYYY NUR fuer das volle Kalenderjahr 01.01.-31.12. YYYY.

        Teilfenster (z. B. Q1) oder fenstergreifende Anfragen duerfen den
        deklarierten Jahresdurchschnitt NICHT verwenden (kein stiller
        Rueckfall auf einen unpassenden Durchschnitt).
        """
        entries = self.manual.get(pair, {})
        if (start.year == end.year
                and start.month == 1 and start.day == 1
                and end.month == 12 and end.day == 31):
            key = f"avg:{start.year}"
            if key in entries and _valid_rate(entries[key]):
                return float(entries[key])
        return None

    def _fetch_year_daily(self, pair: str, year: int) -> dict[str, float]:
        """Ein FMP-Abruf je Paar/Jahr; nur valide Closes; Cache-Speicherung."""
        if self.client is None or not getattr(self.client, "configured", False):
            return {}
        try:
            data = self.client._get(
                "historical-price-full",
                {"from": f"{year}-01-01", "to": f"{year}-12-31", "symbol": pair})
        except Exception:  # noqa: BLE001 -- jede API-Stoerung -> keine Kurse, NaN
            return {}
        if not isinstance(data, dict):
            return {}
        history = data.get("historical") or []
        closes: dict[str, float] = {}
        for h in history:
            d = str(h.get("date", ""))[:10]
            c = h.get("close")
            if d and _valid_rate(c):
                closes[d] = float(c)
        if closes and self.cache is not None:
            # nur fehlende Tage ergaenzen: vorhandene gueltige Cache-Werte
            # haben Vorrang und bleiben auch persistent erhalten.
            self.cache.put_daily_if_absent(pair, closes, "fmp")
        return closes

    def _api_closes_on_or_before(self, pair: str, d: _dt.date) -> dict[str, float]:
        """API-Tageskurse <= d (Jahr d; Fallback Vorjahr, z. B. Anfrage Anfang Januar)."""
        closes = dict(self._fetch_year_daily(pair, d.year))
        if not any(_to_date(k) and _to_date(k) <= d for k in closes):  # type: ignore[operator]
            closes.update(self._fetch_year_daily(pair, d.year - 1))
        return closes

    # ---------------- Stichtagskurs (Bestandsgroessen) ---------------- #

    def quote(self, base: str, quote_cur: str,
              on_or_before: "int | str | _dt.date | None" = None) -> FxQuote:
        """Letzter valider Kurs (quote_cur je base) am oder VOR dem Stichtag.

        Look-ahead-safe; Prioritaet manuell -> Cache -> API; inverses Paar
        per Kehrwert nur bei valider Abfrage. on_or_before=None bedeutet heute.
        """
        base = (base or "").upper().strip()
        quote_cur = (quote_cur or "").upper().strip()
        d = _to_date(on_or_before) if on_or_before is not None else _dt.date.today()
        if not base or not quote_cur or d is None:
            return FxQuote(missing="waehrung_unbekannt")
        if base == quote_cur:
            return FxQuote(rate=1.0, asof=d.isoformat(), source="identity")

        res = self._quote_direct(base, quote_cur, d)
        if res is not None and res.ok:
            return res
        inv = self._quote_direct(quote_cur, base, d)
        if inv is not None and inv.ok and _valid_rate(inv.rate):
            return FxQuote(rate=1.0 / inv.rate, asof=inv.asof, source=inv.source,
                           inverse=True)
        return FxQuote(missing="kein_kurs_verfuegbar")

    def _quote_direct(self, base: str, quote_cur: str,
                      d: _dt.date) -> "FxQuote | None":
        pair = self._pair(base, quote_cur)
        man = self._manual_stichtag(pair, d)
        if man is not None:
            return FxQuote(rate=float(man[1]), asof=man[0].isoformat(), source="manual")
        if self.cache is not None:
            # Schleife bis zum aeltesten Eintrag: ungueltige juengere
            # Eintraege werden geloescht; danach wird WEITER nach einem
            # aelteren gueltigen Kurs <= Stichtag gesucht (nie aufgeben).
            while True:
                cached = self.cache.last_daily(pair, d)
                if cached is None:
                    break
                if _valid_rate(cached[1]):
                    return FxQuote(rate=cached[1], asof=cached[0], source="cache")
                # ungueltiger Cache-Eintrag: loeschen, weitersuchen;
                # nur wenn keine gueltige Cache-Zeile bleibt: API-Rueckfall.
                self.cache.delete_daily(pair, cached[0])
                logger.warning("FX: ungueltiger Cache-Eintrag %s/%s geloescht",
                               pair, cached[0])
        closes = self._api_closes_on_or_before(pair, d)
        if closes:
            prior = [(k, r) for k, r in closes.items()
                     if _to_date(k) is not None and _to_date(k) <= d]  # type: ignore[operator]
            if prior:
                best_d, best_r = max(prior, key=lambda kr: kr[0])
                if _valid_rate(best_r):
                    return FxQuote(rate=best_r, asof=best_d, source="api")
        return None

    def convert_stock(self, value: float, from_cur: str, to_cur: str,
                      as_of: "int | str | _dt.date | None" = None) -> float:
        """Bestandsgroesse: Wert * Stichtagskurs (<= as_of); NaN bei fehlendem Kurs."""
        if value != value:
            return _NAN
        q = self.quote(from_cur, to_cur, as_of)
        return value * q.rate if q.ok else _NAN

    # ---------------- Aktueller Kurs (Marktwerte) ---------------- #

    def quote_current(self, base: str, quote_cur: str) -> FxQuote:
        """Letzter Kurs <= heute; aelterer als current_max_age_days -> NaN + Status.

        Alter des Kurses = heute - Kursdatum in Kalendertagen (Wochenenden
        und Feiertage eingerechnet; FX handelt ~5 Tage/Woche, daher deckt die
        Grenze eine normale Feiertagswoche ab).
        """
        q = self.quote(base, quote_cur, _dt.date.today())
        if not q.ok:
            return q
        asof = _to_date(q.asof)
        if asof is None:
            return FxQuote(missing="kursdatum_unbekannt")
        if (_dt.date.today() - asof).days > int(self.cfg.get("current_max_age_days", 5)):
            return FxQuote(asof=q.asof, source=q.source, missing="kurs_zu_alt")
        return q

    def convert_current(self, value: float, from_cur: str, to_cur: str) -> float:
        """Marktwert: aktuelle Konvertierung (inkl. Altersgrenze); sonst NaN."""
        if value != value:
            return _NAN
        q = self.quote_current(from_cur, to_cur)
        return value * q.rate if q.ok else _NAN

    # ---------------- Periodendurchschnitt (Stromgroessen) ---------------- #

    @staticmethod
    def _max_gap(start: _dt.date, end: _dt.date, dates: "list[_dt.date]") -> int:
        """Groesste zusammenhaengende Luecke (Kalendertage), inkl. Fensterraender."""
        if not dates:
            return (end - start).days + 1
        gaps = [(dates[0] - start).days]
        gaps += [((dates[i] - dates[i - 1]).days - 1) for i in range(1, len(dates))]
        gaps += [(end - dates[-1]).days]
        return max(gaps)

    def _evaluate_flow(self, closes: dict[str, float], d0: _dt.date, d1: _dt.date
                       ) -> "tuple[float, float, int, int] | None":
        """(Ø, coverage, max_gap, n) wenn repraesentativ, sonst None."""
        pairs = sorted((_to_date(k), float(r)) for k, r in closes.items()
                       if _to_date(k) is not None and _valid_rate(r))
        if not pairs:
            return None
        dates = [p[0] for p in pairs]
        rates = [p[1] for p in pairs]
        wd = max(1, _weekdays(d0, d1))
        coverage = len(rates) / wd
        gap = self._max_gap(d0, d1, dates)
        if (coverage >= float(self.cfg.get("min_flow_coverage", 0.8))
                and gap <= int(self.cfg.get("max_flow_gap_days", 10))):
            return sum(rates) / len(rates), coverage, gap, len(rates)
        return None

    def _naeherung(self, base: str, quote_cur: str, d0: _dt.date,
                   d1: _dt.date) -> "FxFlowQuote | None":
        """Jahresendkurs-Naeherung: nur wenn Fensterende ~ Jahresende; nie als Ø.-Basis."""
        tol = int(self.cfg.get("naeherung_stichtag_tol_days", 14))
        if abs((_year_end(d1) - d1).days) > tol:
            return None
        q = self.quote(base, quote_cur, d1)   # Stichtagskurs <= Fensterende
        if not q.ok:
            return None
        return FxFlowQuote(rate=q.rate, start=d0.isoformat(), end=d1.isoformat(),
                           source=q.source, basis="jahresendkurs_naeherung",
                           coverage=_NAN, n_closes=-1, inverse=q.inverse, asof=q.asof)

    def _flow_quote_from_closes(self, closes: dict[str, float], d0: _dt.date,
                                 d1: _dt.date, pair: str,
                                 inverse: bool = False) -> "FxFlowQuote | None":
        """Ø-/Coverage-/Gap-ausgewertete Tagesreihe als Flow-Zitat (kein Rolling)."""
        ev = self._evaluate_flow(closes, d0, d1)
        if ev is None:
            return None
        avg_rate, coverage, gap, n = ev
        src = "cache"
        if self.cache is not None:
            cached = self.cache.daily(pair, d0, d1)
            if not cached or set(cached) != set(closes):
                src = "api" if self.client is not None else "cache"
        elif self.client is not None:
            src = "api"
        return FxFlowQuote(rate=avg_rate, start=d0.isoformat(), end=d1.isoformat(),
                           source=src, basis="periodendurchschnitt",
                           coverage=coverage, max_gap_days=gap, n_closes=n,
                           inverse=inverse)

    def _flow_inverse(self, base: str, quote_cur: str, d0: _dt.date,
                      d1: _dt.date) -> "FxFlowQuote | None":
        """Reziproker Periodendurchschnitt: JEDER Tageskurs des inversen Paars
        wird EINZELN rezipiziert (1/c) und erst danach gemittelt -- mathematisch
        korrekt, denn mean(1/x) != 1/mean(x). Coverage-/Gap-Pruefung läuft auf
        der tatsaechlich verwendeten reziproken Tagesreihe.
        """
        inv_pair = self._pair(quote_cur, base)
        avg = self._manual_flow_avg(inv_pair, d0, d1)
        if avg is not None and _valid_rate(avg):
            # deklarierter manueller Perioden-Ø des inversen Paars: rezipiert
            # (keine Tagesreihe hinterlegt); ausdruecklich als reziproker
            # MANUELLER Jahresdurchschnitt gekennzeichnet -- unterscheidbar vom
            # echten Perioden-O aus reziproken Tageskursen.
            return FxFlowQuote(rate=1.0 / float(avg), start=d0.isoformat(),
                               end=d1.isoformat(), source="manual",
                               basis="manueller_jahresdurchschnitt",
                               coverage=_NAN, n_closes=-1, inverse=True)
        closes = self._daily_in_window(inv_pair, d0, d1)
        if not closes:
            return None
        inv_closes = {d: 1.0 / float(r)
                      for d, r in closes.items() if _valid_rate(r)}
        return self._flow_quote_from_closes(inv_closes, d0, d1, inv_pair,
                                            inverse=True)

    def flow_quote(self, base: str, quote_cur: str,
                   start: "int | str | _dt.date",
                   end: "int | str | _dt.date",
                   _skip_inverse: bool = False) -> FxFlowQuote:
        """Periodendurchschnitt (Stromgroesse) mit Coverage-/Gap-Pruefung.

        Reihenfolge:
        1. base == quote -> 1.0 (identity, keine Abfrage).
        2. manuelles avg:YYYY, wenn das Fenster EXAKT das volle
           Kalenderjahr (01.01.-31.12.) ist
           -> basis="periodendurchschnitt" (deklarierter Ø, keine Zeitreihe).
        3. Tageskurse im Fenster (Cache -> API, Cache ergaenzt Luecken per
           API ohne Ueberschreiben): Ø nur bei Coverage- und
           Gap-Check bestanden -> basis="periodendurchschnitt".
        4. Jahresendkurs-Naeherung (nur wenn Fensterende ~ Jahresende),
           ausdruecklich als Naeherung gekennzeichnet; jeder beliebige Kurs
           aus Fenstermitte/-anfang bleibt unzulaessig.
        5. Inverses Paar analog (markiert inverse=True): echter reziproker
           Perioden-Ø als mean(1/c) der Tageskurse des inversen Paars.
        6. Sonst NaN.
        """
        base = (base or "").upper().strip()
        quote_cur = (quote_cur or "").upper().strip()
        d0, d1 = _to_date(start), _to_date(end)
        fq = FxFlowQuote(start=d0.isoformat() if d0 else "",
                         end=d1.isoformat() if d1 else "")
        if not base or not quote_cur or d0 is None or d1 is None or d1 < d0:
            return fq
        if base == quote_cur:
            fq.rate, fq.source, fq.basis = 1.0, "identity", "periodendurchschnitt"
            fq.n_closes = -1
            return fq

        pair = self._pair(base, quote_cur)
        avg = self._manual_flow_avg(pair, d0, d1)
        if avg is not None:
            fq.rate, fq.source = float(avg), "manual"
            fq.basis, fq.n_closes = "periodendurchschnitt", -1
            return fq

        closes = self._daily_in_window(pair, d0, d1)
        if closes:
            fq_daily = self._flow_quote_from_closes(closes, d0, d1, pair)
            if fq_daily is not None:
                return fq_daily
        inv = None
        if not _skip_inverse:
            # echter reziproker Perioden-O VOR jedweder Naeherung
            inv = self._flow_inverse(base, quote_cur, d0, d1)
            if (inv is not None and inv.ok
                    and inv.basis in ("periodendurchschnitt",
                                      "manueller_jahresdurchschnitt")):
                return inv
        # Naeherung (direkt ODER indirekt): quote() im _naeherung(String) prueft
        # bei Nichtverfuegbarkeit des direkten Paars auch das inverse Paar
        # (Kehrwert, inverse=True) -- deckt die inverse Naeherung ab.
        naeherung = self._naeherung(base, quote_cur, d0, d1)
        if naeherung is not None and naeherung.ok:
            return naeherung
        return fq

    def _daily_in_window(self, pair: str, d0: _dt.date, d1: _dt.date
                         ) -> dict[str, float]:
        """Tageskurse im Fenster [d0, d1]: Cache zuerst; fehlende Tage werden
        ueber die API ergaenzt. Vorhandene gueltige Cache-Werte haben Vorrang
        und werden dabei NIE ueberschrieben. Ohne API-Client bleibt der
        (ggf. unvollstaendige) Cache bestehen -- Unvollstaendigkeit wird
        spaeter ueber die Coverage-/Gap-Pruefung sichtbar, nie still kaschiert.
        """
        closes: dict[str, float] = {}
        if self.cache is not None:
            # ungueltige Zeilen zuerst loeschen: sie waeren im Lesergebnis
            # unsichtbar, wuerden aber das persistente Ergaenzen per
            # put_daily_if_absent (ON CONFLICT DO NOTHING) blockieren.
            self.cache.purge_invalid_daily(pair, d0, d1)
            closes.update(self.cache.daily(pair, d0, d1))
        if len(closes) >= _weekdays(d0, d1) or self.client is None:
            return closes
        api: dict[str, float] = {}
        for year in range(d0.year, d1.year + 1):
            api.update(self._fetch_year_daily(pair, year))
        for d, r in api.items():
            if d not in closes:  # Cache-Vorrang: gueltige Cache-Werte nie ueberschreiben
                closes[d] = r
        return {d: r for d, r in closes.items()
                if d0.isoformat() <= d <= d1.isoformat()}

    def convert_flow(self, value: float, from_cur: str, to_cur: str,
                     start: "int | str | _dt.date",
                     end: "int | str | _dt.date") -> float:
        """Stromgroesse: Wert * Periodendurchschnitt; NaN bei unzureichender Abdeckung."""
        if value != value:
            return _NAN
        fq = self.flow_quote(from_cur, to_cur, start, end)
        return value * fq.rate if fq.ok else _NAN

    # ---------------- Legacy-Kompatibilitaet (Jahresend-Semantik) ---------------- #

    def rate(self, base: str, quote_cur: str,
             as_of: "int | str | _dt.date | None" = None) -> float:
        """Nur der Kurswert des Stichtagskurses (Hilfs-/legacy-API)."""
        return self.quote(base, quote_cur, as_of).rate

    def convert(self, value: float, from_cur: str, to_cur: str, year: int) -> float:
        """Legacy-Kompatibilitaet: Konvertierung zum Jahresendkurs des Jahres."""
        frm, to = (from_cur or "").upper().strip(), (to_cur or "").upper().strip()
        if not frm or not to:
            return _NAN
        if frm == to:
            return value if value == value else _NAN
        return self.convert_stock(value, frm, to, _dt.date(int(year), 12, 31))


# --------------------------------------------------------------------------- #
# Zentrale Factory
# --------------------------------------------------------------------------- #

def build_fx(cfg: "dict | None" = None, api_key: "str | None" = None,
             cache_path: "str | None" = None,
             manual_path: "str | None" = None) -> FXConverter:
    """Zentrale Factory: liefert IMMER einen FXConverter.

    - Ohne FMP_API_KEY: manuelle Kurse (fx_rates.yaml) und SQLite-Cache
      (fx_rates.db) bleiben nutzbar; nur das Nachladen neuer Kurse über die
      FMP-API entfaellt.
    - Mit Key: FMPClient mit konfiguriertem Timeout; Cache in eigener Datei
      (fx.cache_file), nur valide Kurse werden gespeichert.
    """
    fx_cfg: dict = {}
    if isinstance(cfg, dict):
        fx_cfg = dict(cfg.get("fx", {}) or {})
    path_cache = cache_path or str(fx_cfg.get("cache_file", FX_DEFAULTS["cache_file"]))
    path_manual = manual_path or str(fx_cfg.get("manual_path",
                                                FX_DEFAULTS["manual_path"]))
    key = api_key or os.environ.get("FMP_API_KEY", "")
    client: "FMPClient | None" = None
    if key:
        client = FMPClient(
            api_key=key,
            timeout=float(fx_cfg.get("api_timeout_s", FX_DEFAULTS["api_timeout_s"])))
    cache = FXCache(path_cache)
    return FXConverter(client=client, cache=cache, manual_path=path_manual,
                       cfg={"fx": fx_cfg})
