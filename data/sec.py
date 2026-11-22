"""SEC-EDGAR-Abgleich (kostenlos, https://data.sec.gov/api/xbrl/companyfacts).

Vergleicht die Cashflow-Kennzahlen (OCF, Capex, SBC) aus dem yfinance-Provider
mit den bei der SEC gemeldeten XBRL-Facts (US GAAP, DEI/USD). Abweichung
ueber `sec.tolerance` (Default 5 %) je Jahr -> Flag "Quellen weichen ab".

v72 Zinsaufwand-Zweitquelle: sec_interest_fallback(m, cfg) liefert je
Geschaeftsjahr/TTM den SEC-Zinsaufwand aus GuV-Tags (Hierarchie siehe
_INTEREST_TAGS), solange yfinance NaN liefert (kein Mischen, yfinance
bleibt Vorrang). TTM = FY + YTD(aktuell) - YTD(Vorjahr) (keine
Einzelquartal-Summe), fehlende Komponente -> NaN (kein Notbehelf).

Wichtig (SEC-Policy): User-Agent MIT E-Mail-Adresse -- aus der
Umgebungsvariable SEC_USER_AGENT (Pflicht; einmalig beim Prozessstart
via validate_user_agent() pruefen, siehe run_ranking/quartalslauf/app.py).
Beispiel:
    export SEC_USER_AGENT="Vorname Nachname <mail@example.com>"

Cache: companyfacts werden je CIK gecacht (sec.cache_dir, Default
logs/sec_cache; sec.cache_ttl_hours, Default 24 h; sec.cache_frozen fuer
Tests/Abnahme: nur Cache lesen, nie neu laden). Rate-Limit 5/s.

Nutzung: sec_check(m, cfg) -> SecCheckResult; sec_interest_fallback(m, cfg)
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from data.loader import TickerMetrics

logger = logging.getLogger("value_screener.sec")

# XBRL-Tags (US GAAP) je Kennzahl; erste vorhandene gewinnt.
_XBRL_TAGS = {
    "operating_cashflow": ["NetCashProvidedByUsedInOperatingActivities",
                           "NetCashProvidedByUsedInOperatingActivitiesContinuing"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "stock_based_compensation": ["ShareBasedCompensation",
                                 "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensation"],
}

# interner Kennzahl-Name -> SEC-Tag-Key
_KEY_MAP = {
    "operating_cashflow": "operating_cashflow",
    "capex": "capex",
    "stock_based_compensation": "stock_based_compensation",
}


@dataclass
class SecCheckResult:
    ok: bool = False               # Abgleich durchgefuehrt
    flag: bool = False             # True = Abweichung > tolerance in mind. 1 Jahr
    deviations: list = field(default_factory=list)  # [{fy, key, ours, sec, dev}]
    note: str = ""


def _cik_for_ticker(ticker: str) -> str | None:
    """CIK zur Ticker-Symbol via SEC company_tickers.json (einmalig, cached)."""
    _rate_limit()
    try:
        with urllib.request.urlopen(
                urllib.request.Request(
                    "https://www.sec.gov/files/company_tickers.json",
                    headers={"User-Agent": user_agent()}), timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        for entry in data.values():
            if entry.get("ticker", "").upper() == ticker.upper():
                return str(entry.get("cik_str")).zfill(10)
    except Exception as exc:
        logger.warning("SEC: Ticker->CIK fuer %s fehlgeschlagen: %s", ticker, exc)
    return None


# Rate-Limit -- Zeitstempel des letzten SEC-Requests (max. N pro Sekunde)
_LAST_REQ = {"t": 0.0}
_MAX_RPS = {"v": 5.0}

# v72: User-Agent ausschliesslich aus der Umgebungsvariable (kein Config-Fallback)
_ENV_HINT = ("Beispiel: export SEC_USER_AGENT=\"Vorname Nachname "
             "<mail@example.com>\"")


def user_agent() -> str:
    """SEC-User-Agent aus der Umgebungsvariable SEC_USER_AGENT (Pflicht,
    muss eine E-Mail-Adresse enthalten, SEC-Policy). Klarer Fehler, wenn
    die Variable fehlt oder kein '@' enthaelt."""
    ua = (os.environ.get("SEC_USER_AGENT") or "").strip()
    if not ua:
        raise RuntimeError("Umgebungsvariable SEC_USER_AGENT fehlt -- der "
                           "SEC-Abzug benoetigt einen User-Agent mit "
                           f"E-Mail-Adresse (SEC-Policy). {_ENV_HINT}")
    if "@" not in ua:
        raise RuntimeError("SEC_USER_AGENT enthaelt keine E-Mail-Adresse "
                           f"('@' fehlt, SEC-Policy). {_ENV_HINT}")
    return ua


def validate_user_agent() -> str:
    """Prozessstart-Pruefung (run_ranking/quartalslauf/app.py): wirft einen
    klaren RuntimeError, wenn SEC_USER_AGENT nicht korrekt gesetzt ist --
    VOR dem ersten SEC-Request."""
    return user_agent()


# v72 Cache: companyfacts je CIK (in-memory + Datei in sec.cache_dir)
_FACTS_MEM_CACHE: dict[str, tuple[str, dict]] = {}  # cik -> (fetched_at, facts)


def _cache_settings(cfg: dict) -> tuple[str, float, bool]:
    """(cache_dir, ttl_stunden, frozen) aus cfg['sec']."""
    sec_cfg = cfg.get("sec", {}) or {}
    cache_dir = str(sec_cfg.get("cache_dir", "logs/sec_cache") or "")
    ttl_h = float(sec_cfg.get("cache_ttl_hours", 24))
    frozen = bool(sec_cfg.get("cache_frozen", False))
    return cache_dir, ttl_h, frozen


def _cache_file(cache_dir: str, cik: str) -> str:
    import os as _os
    return _os.path.join(cache_dir, f"CIK{cik}.json")


def _facts_from_file(cache_dir: str, cik: str, ttl_h: float,
                     frozen: bool) -> tuple[dict | None, bool]:
    """(facts, aus_cache_und_frisch) -- Laedt companyfacts aus dem Datei-Cache.
    Nur frische (TTL) Eintraege werden verwendet, ausser im Frozen-Modus
    (dort zaehlt auch ein abgelaufener Eintrag; fehlt er komplett -> None)."""
    import json as _json
    path = _cache_file(cache_dir, cik)
    try:
        with open(path, encoding="utf-8") as f:
            raw = _json.load(f)
    except (OSError, ValueError):
        if frozen:
            logger.warning("SEC-Cache (frozen): kein Cache-Eintrag fuer CIK %s", cik)
        return None, False
    fetched_at = str(raw.get("fetched_at") or "")
    facts = raw.get("facts")
    if facts is None:
        return None, False
    age_h = 0.0
    try:
        age_h = (datetime.now() - datetime.fromisoformat(fetched_at)).total_seconds() / 3600.0
    except ValueError:
        return None, False  # ungueltiger Zeitstempel -> nicht verwenden
    if frozen or age_h <= ttl_h:
        return facts, True
    return None, False


def fetch_companyfacts_cached(cik: str, cfg: dict) -> tuple[dict | None, bool]:
    """companyfacts je CIK mit Cache (Datei + Memory; Rate-Limit beim echten
    Abruf). Rueckgabe (facts | None, aus_cache). aus_cache=True heisst: die
    Antwort kam aus dem (frischen oder frozen) Cache, kein neuer Abruf;
    ein frischer Abruf schreibt den Cache neu."""
    if not cik:
        return None, False
    cache_dir, ttl_h, frozen = _cache_settings(cfg)
    # Memory-Cache
    if cik in _FACTS_MEM_CACHE:
        fetched_at, facts = _FACTS_MEM_CACHE[cik]
        try:
            age_h = (datetime.now() - datetime.fromisoformat(fetched_at)).total_seconds() / 3600.0
        except ValueError:
            age_h = math.inf
        if frozen or age_h <= ttl_h:
            return facts, True
    if cache_dir:
        facts, hit = _facts_from_file(cache_dir, cik, ttl_h, frozen)
        if hit:
            _FACTS_MEM_CACHE[cik] = (datetime.now().isoformat(timespec="seconds"), facts)
            return facts, True
    if frozen:  # frozen ohne Cache-Treffer: kein Abruf
        return None, False
    ua = user_agent()
    facts = _fetch_companyfacts(cik, ua)
    if facts is not None:
        fetched_at = datetime.now().isoformat(timespec="seconds")
        _FACTS_MEM_CACHE[cik] = (fetched_at, facts)
        if cache_dir:
            try:
                import os as _os
                _os.makedirs(cache_dir, exist_ok=True)
                import json as _json
                with open(_cache_file(cache_dir, cik), "w", encoding="utf-8") as f:
                    _json.dump({"fetched_at": fetched_at, "facts": facts}, f)
            except OSError as exc:
                logger.warning("SEC-Cache nicht schreibbar (%s): %s", cache_dir, exc)
    return facts, False


def _rate_limit() -> None:
    """wartet, bis der Mindestabstand 1/max_requests_per_second seit dem
    letzten SEC-Request vergangen ist (default 5/s -> 0,2 s)."""
    import time as _time
    min_gap = 1.0 / max(0.1, _MAX_RPS["v"])
    now = _time.monotonic()
    wait = _LAST_REQ["t"] + min_gap - now
    if wait > 0:
        _time.sleep(wait)
    _LAST_REQ["t"] = _time.monotonic()


def _fetch_companyfacts(cik: str, user_agent: str) -> dict | None:
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    _rate_limit()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": user_agent})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("SEC: companyfacts CIK %s nicht ladbar: %s", cik, exc)
        return None


def _sec_annual_values(facts: dict, key: str) -> dict[int, float]:
    """Jaehrliche SEC-Werte (USD, 10-K/10-K/A, frame=None) je Kennzahl.

    Rueckgabe: {fiscal_year_end_year: value}. Nur abgeschlossene
    Jahresperioden (start + 12 Monate ≈ end, end-Datum im Jahr).
    """
    out = {}
    dei = facts.get("facts", {}).get("dei", {})
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in _XBRL_TAGS[key]:
        fact = usgaap.get(tag)
        if not fact:
            continue
        units = fact.get("units", {}).get("USD", [])
        for u in units:
            if u.get("form") not in ("10-K", "10-K/A"):
                continue
            start, end = u.get("start"), u.get("end")
            if not start or not end:
                continue
            # Jahresperiode: ca. 12 Monate
            try:
                from datetime import date
                d0 = date.fromisoformat(start)
                d1 = date.fromisoformat(end)
            except ValueError:
                continue
            months = (d1.year - d0.year) * 12 + (d1.month - d0.month)
            if not (11 <= months <= 13):
                continue
            fy = d1.year
            val = u.get("val")
            if val is None or fy in out:
                continue
            out[fy] = float(val)
        if out:
            break  # erster vorhandener Tag gewinnt
    return out


def sec_check(m: TickerMetrics, cfg: dict) -> SecCheckResult:
    """Abgleich OCF/Capex/SBC (Jahreswerte) gegen SEC-EDGAR (US-Ticker).

    Rueckgabe: SecCheckResult. ok=False wenn kein Abgleich moeglich
    (kein US-Ticker, kein Netzwerk, CIK nicht gefunden). flag=True wenn
    mind. ein Jahr um mehr als `sec.tolerance` abweicht.
    """
    res = SecCheckResult()
    sec_cfg = cfg.get("sec", {})
    if not sec_cfg.get("enabled", True):
        res.note = "SEC-Abgleich deaktiviert (config sec.enabled)"
        return res
    tol = float(sec_cfg.get("tolerance", 0.05))
    ua = user_agent()  # v72: ausschliesslich Umgebungsvariable
    # Rate-Limit aus config (default 5/s)
    _MAX_RPS["v"] = float(sec_cfg.get("max_requests_per_second", 5.0))

    country = (m.country or "").upper()
    if country not in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"):
        res.note = f"kein US-Ticker ({m.country or 'unbekannt'}) — SEC-Abgleich nicht anwendbar"
        return res

    cik = _cik_for_ticker(m.ticker)
    if not cik:
        res.note = "CIK nicht gefunden"
        return res
    facts = _fetch_companyfacts(cik, ua)
    if not facts:
        res.note = "companyfacts nicht ladbar"
        return res

    res.ok = True
    worst = 0.0
    for key in ("operating_cashflow", "capex", "stock_based_compensation"):
        sec_vals = _sec_annual_values(facts, key)
        if not sec_vals:
            continue
        for fy in sorted(m.years):
            ours = m.years[fy].get(key)
            if ours is None or ours.is_na():
                continue
            sec_v = sec_vals.get(fy)
            if sec_v is None or sec_v == 0:
                continue
            # Capex: yfinance/FMP negativ, SEC positiv -> Vorzeichen angleichen
            ours_v = ours.value
            if key == "capex" and ours_v < 0:
                ours_v = -ours_v
            dev = abs(ours_v - sec_v) / abs(sec_v)
            res.deviations.append({"fy": fy, "key": key, "ours": ours_v,
                                   "sec": sec_v, "dev": dev})
            worst = max(worst, dev)
    res.flag = worst > tol
    if res.flag:
        res.note = (f"Quellen weichen ab: max. Abweichung {worst:.1%} "
                    f"(> {tol:.0%}) in mind. 1 Jahr")
    else:
        res.note = f"Abgleich ok (max. Abweichung {worst:.1%})"
    logger.info("%s: SEC-Abgleich %s", m.ticker, res.note)
    return res


def sec_sbc_fallback(m: TickerMetrics, cfg: dict) -> tuple[float | None, int | None, str]:
    """SBC-Fallback aus SEC-EDGAR (US-Ticker).

    Wenn der Provider (Yahoo/FMP) fuer ein Geschaeftsjahr keine
    Aktienverguetung (SBC) liefert, wird der XBRL-Tag ShareBasedCompensation
    aus data.sec.gov/api/xbrl/companyfacts als Ersatz verwendet.

    Rueckgabe: (sbc_wert, fiscal_year, quelle) -- sbc_wert None, wenn kein
    SEC-Wert verfuegbar. quelle enthaelt die Herkunft ("SEC EDGAR
    ShareBasedCompensation FY2025").
    """
    sec_cfg = cfg.get("sec", {})
    if not sec_cfg.get("enabled", True):
        return None, None, ""
    country = (m.country or "").upper()
    if country not in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"):
        return None, None, ""
    ua = user_agent()  # v72: ausschliesslich Umgebungsvariable
    _MAX_RPS["v"] = float(sec_cfg.get("max_requests_per_second", 5.0))
    cik = _cik_for_ticker(m.ticker)
    if not cik:
        return None, None, ""
    facts = _fetch_companyfacts(cik, ua)
    if not facts:
        return None, None, ""
    sec_vals = _sec_annual_values(facts, "stock_based_compensation")
    if not sec_vals:
        return None, None, ""
    # juengstes Jahr, fuer das Yahoo keine SBC geliefert hat
    for fy in sorted(m.years, reverse=True):
        ours = m.years[fy].get("stock_based_compensation")
        if ours is None or ours.is_na():
            if fy in sec_vals and sec_vals[fy] != 0:
                return float(sec_vals[fy]), fy, f"SEC EDGAR ShareBasedCompensation FY{fy}"
    # Fallback: juengstes verfuegbares SEC-Jahr
    fy = max(sec_vals)
    return float(sec_vals[fy]), fy, f"SEC EDGAR ShareBasedCompensation FY{fy}"


def sec_sbc_fallback_all(m: TickerMetrics, cfg: dict) -> list[tuple[int, float, str]]:
    """SBC-Fallback aus SEC-EDGAR fuer ALLE Jahre ohne Yahoo-SBC.

    companyfacts wird nur EINMAL geladen; fuer jedes Geschaeftsjahr, in dem
    der Provider keine SBC (stock_based_compensation / fcf_sbc) liefert,
    wird der SEC-Wert (ShareBasedCompensation) uebernommen.

    Rueckgabe: [(fiscal_year, sbc_wert, quelle), ...] -- quelle z. B.
    "SEC EDGAR ShareBasedCompensation FY2025". Leer, wenn kein US-Ticker,
    SEC deaktiviert oder keine SEC-Werte vorhanden.
    """
    sec_cfg = cfg.get("sec", {})
    if not sec_cfg.get("enabled", True):
        return []
    country = (m.country or "").upper()
    if country not in ("US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"):
        return []
    ua = user_agent()  # v72: ausschliesslich Umgebungsvariable
    _MAX_RPS["v"] = float(sec_cfg.get("max_requests_per_second", 5.0))
    cik = _cik_for_ticker(m.ticker)
    if not cik:
        return []
    facts = _fetch_companyfacts(cik, ua)
    if not facts:
        return []
    sec_vals = _sec_annual_values(facts, "stock_based_compensation")
    if not sec_vals:
        return []
    out = []
    for fy in sorted(m.years):
        ours = m.years[fy].get("stock_based_compensation")
        fsbc = m.years[fy].get("fcf_sbc")
        if (ours is None or ours.is_na()) and (fsbc is None or fsbc.is_na()):
            if fy in sec_vals and sec_vals[fy] != 0:
                out.append((fy, float(sec_vals[fy]),
                            f"SEC EDGAR ShareBasedCompensation FY{fy}"))
    return out


def format_sec_detail(deviations: list, ocf_by_fy: dict | None = None) -> str:
    """SEC-Abweichungen kompakt als Text: Feld, Jahr, Prozent.

    deviations: [{fy, key, ours, sec, dev}] aus SecCheckResult. Beispiel:
    "OCF FY2024 +6.2%; Capex FY2023 -12.0%". Nur Abweichungen ueber dem
    konfigurierten Toleranzwert (Default 5 %) werden gelistet, aber alle
    uebergebenen Abweichungen formatiert (Aufrufer filtert ggf.).

    ocf_by_fy (v37, optional): {fy: OCF-Betrag} -- dann enthaelt das Capex-Detail
    zusaetzlich Yahoo-Capex, SEC-Capex und Differenz in Mrd. USD und in % des OCF:
    "Capex FY2024: Yahoo 3.2 / SEC 2.0 Mrd., Diff 1.2 Mrd. = 15.6% des OCF".
    """
    key_label = {
        "operating_cashflow": "OCF",
        "capex": "Capex",
        "stock_based_compensation": "SBC",
    }
    parts = []
    for d in sorted(deviations, key=lambda x: (x.get("fy", 0), x.get("key", "")), reverse=True):
        dev = d.get("dev")
        if dev is None or dev != dev:
            continue
        sign = "+" if d.get("ours", 0) >= d.get("sec", 0) else "-"
        label = key_label.get(d.get("key"), d.get("key"))
        fy = d.get("fy")
        if d.get("key") == "capex" and ocf_by_fy and fy in ocf_by_fy and ocf_by_fy[fy]:
            ocf = abs(ocf_by_fy[fy])
            diff = abs(d.get("ours", 0) - d.get("sec", 0))
            parts.append(
                f"{label} FY{fy}: Yahoo {abs(d.get('ours', 0))/1e9:,.2f} / SEC {d.get('sec', 0)/1e9:,.2f} Mrd. USD, "
                f"Diff {diff/1e9:,.2f} Mrd. = {diff/ocf:.1%} des OCF ({sign}{abs(dev):.1%})")
        else:
            parts.append(f"{label} FY{fy} {sign}{abs(dev):.1%}")
    return "; ".join(parts)


# --- v72: Zinsaufwand-Zweitquelle ----------------------------------------
# GuV-Zins-Tags (US GAAP), Hierarchie: der erste Tag, der die benoetigte
# Periode vollstaendig liefert, gewinnt. KEINE Cash-Zahlungs- oder
# Netto-Tags (InterestPaidNet, NetInterestIncome) -- anderes Sachverhalt.
_INTEREST_TAGS = ("InterestExpense", "InterestExpenseNonoperating",
                  "InterestExpenseDebt", "InterestAndDebtExpense")
_INTEREST_TOL_DAYS = 7          # Periodenende-Toleranz (52/53-Wochen-FJ)
_INTEREST_FY_DUR = (340, 380)   # Jahresdauer-Toleranzbereich
_INTEREST_DEV_THRESHOLD = 0.20  # Flag zins_quellen_abweichend ab > 20 %
_INTEREST_FORMS = ("10-K", "10-K/A", "10-Q", "10-Q/A")


def _interest_entries(facts: dict, tag: str) -> list[dict]:
    """Alle USD-Perioden-Fakten eines Tags (start/end/val/filed/form),
    nur gueltige Datumsangaben und Gueltige Form-IDs."""
    usgaap = facts.get("facts", {}).get("us-gaap", {})
    fact = usgaap.get(tag) or {}
    out = []
    for unit, items in fact.get("units", {}).items():
        if unit != "USD":  # Nicht-USD-Filings ignorieren (keine Umrechnung)
            continue
        for u in items or []:
            start, end = u.get("start"), u.get("end")
            if not start or not end or u.get("val") is None:
                continue
            if u.get("form") not in _INTEREST_FORMS:
                continue
            try:
                d0 = date.fromisoformat(str(start)[:10])
                d1 = date.fromisoformat(str(end)[:10])
            except ValueError:
                continue
            out.append({"start": d0, "end": d1, "dur": (d1 - d0).days,
                        "val": float(u["val"]),
                        "filed": str(u.get("filed") or ""),
                        "form": str(u.get("form"))})
    return out


def _pick_interest(entries: list[dict], want_end: date, dur: int | None,
                   dur_tol: int, end_tol: int) -> dict | None:
    """Neuestes Fakt (juengstes filed) mit end ~ want_end (+-end_tol) und
    Dauer ~ dur (+-dur_tol); dur None -> nur Dauerbereich pruefen (FY)."""
    best = None
    for e in entries:
        if abs((e["end"] - want_end).days) > end_tol:
            continue
        if dur is None:
            if not (_INTEREST_FY_DUR[0] <= e["dur"] <= _INTEREST_FY_DUR[1]):
                continue
        elif abs(e["dur"] - dur) > dur_tol:
            continue
        if best is None or e["filed"] > best["filed"]:
            best = e
    return best


def _pick_interest_fy(entries: list[dict], fy_end: date) -> dict | None:
    """FY-Komponente: Dauer ~365 (340-380) und Ende ~ fy_end (+-7 Tage)."""
    return _pick_interest(entries, fy_end, None, 0, _INTEREST_TOL_DAYS)


def _prior_year(d: date) -> date:
    """selbes Kalenderdatum im Vorjahr (29.2. -> 28.2., Toleranz deckt Rest)."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:  # 29. Februar
        return d.replace(year=d.year - 1, day=28)


def _sec_interest_ttm(m: TickerMetrics,
                      by_tag: dict[str, list[dict]]) -> tuple[float, str] | None:
    """TTM-Zins nach der Formel FY + YTD(aktuell) - YTD(Vorjahr).

    - Q4-Sonderfall: endet das TTM-Fenster (nahe) am Geschaeftsjahresende,
      ist TTM = FY direkt (keine YTD-Formel noetig).
    - 'gleiche Tag-Wahl': alle drei Komponenten aus DEMSELBEN Tag; fehlt
      eine -> dieser Tag scheidet aus, naechster der Hierarchie. Kein
      Mischen zwischen Tags, kein Notbehelf.
    """
    ttm = getattr(m, "ttm", None)
    if ttm is None or not getattr(ttm, "quarter_end", ""):
        return None
    try:
        w_end = date.fromisoformat(str(ttm.quarter_end)[:10])
    except ValueError:
        return None
    fe = []
    for v in (m.fiscal_ends or {}).values():
        try:
            fe.append(date.fromisoformat(str(v)[:10]))
        except ValueError:
            continue
    if not fe:
        return None
    fe = sorted(fe)
    tol = _INTEREST_TOL_DAYS
    # Q4-Sonderfall: TTM endet am FY-Ende -> TTM == FY
    q4 = [d for d in fe if abs((d - w_end).days) <= tol]
    for tag, entries in ((t, by_tag[t]) for t in _INTEREST_TAGS if t in by_tag):
        if q4:
            e = _pick_interest_fy(entries, max(q4))
            if e is not None:
                return e["val"], tag
            continue
        # FY_last: juengstes FY-Ende vor dem TTM-Ende (liegt im Fenster)
        e_date = max((d for d in fe if d < w_end), default=None)
        if e_date is None:
            continue
        ytd_dur = (w_end - e_date).days
        if ytd_dur < 40 or ytd_dur > 350:   # kein sinnvolles YTD (Sicherheit)
            continue
        prior_end = _prior_year(w_end)
        f = _pick_interest_fy(entries, e_date)
        c = _pick_interest(entries, w_end, ytd_dur, tol, tol)
        p = _pick_interest(entries, prior_end, ytd_dur, tol, tol)
        if f is not None and c is not None and p is not None:
            return f["val"] + c["val"] - p["val"], tag
    return None


def sec_interest_fallback(m: TickerMetrics, cfg: dict) -> dict:
    """v72 Zinsaufwand-Zweitquelle: SEC-Werte je Quelle, nur fuer Ticker
    OHNE Boersensuffix (US GAAP). yfinance bleibt Vorrang -- der Aufrufer
    verwendet diese Werte nur, wenn yfinance NaN liefert.

    Rueckgabe: {"FY": {fy: (wert, tag)}, "TTM": (wert, tag) | None,
                "abweichungen": [{periode, yfinance, sec, tag, abweichung}],
                "aus_cache": bool, "note": str}
    Nur wenn `aus_cache` wahr ist, ABWEICHUNGS-Daten als flag-wuerdig
    betrachten (Amendment: Abweichungsflag nur bei ohnehin gecachter
    Antwort, kein zusaetzlicher Abruf dafuer).
    """
    out = {"FY": {}, "TTM": None, "abweichungen": [], "aus_cache": False, "note": ""}
    if "." in (m.ticker or ""):
        out["note"] = "Boersensuffix -> kein SEC-Interest (US-GAAP nur ohne Suffix)"
        return out
    sec_cfg = cfg.get("sec", {}) or {}
    if not sec_cfg.get("enabled", True):
        out["note"] = "SEC deaktiviert"
        return out
    _MAX_RPS["v"] = float(sec_cfg.get("max_requests_per_second", 5.0))
    cik = _cik_for_ticker(m.ticker)  # validiert SEC_USER_AGENT beim ersten Request
    if not cik:
        out["note"] = "CIK nicht gefunden"
        return out
    facts, aus_cache = fetch_companyfacts_cached(cik, cfg)
    if not facts:
        out["note"] = "companyfacts nicht ladbar"
        return out
    out["aus_cache"] = aus_cache
    by_tag = {tag: _interest_entries(facts, tag) for tag in _INTEREST_TAGS}
    by_tag = {t: v for t, v in by_tag.items() if v}
    # --- FY je Geschaeftsjahr (exakter Stichtag +-7 Tage) ---
    for fy, fe_str in (m.fiscal_ends or {}).items():
        try:
            fe = date.fromisoformat(str(fe_str)[:10])
        except ValueError:
            continue
        for tag in _INTEREST_TAGS:
            entries = by_tag.get(tag)
            if not entries:
                continue
            e = _pick_interest_fy(entries, fe)
            if e is not None:
                out["FY"][fy] = (e["val"], tag)
                break   # Hierarchie: erster Tag gewinnt
    # --- TTM ---
    t = _sec_interest_ttm(m, by_tag)
    if t is not None:
        out["TTM"] = t
    # --- Abweichungsdaten (nur Info/CSV; Rechenwert bleibt yfinance) ---
    for fy in sorted(m.years):
        ym = m.years[fy].get("interest_expense")
        sv = out["FY"].get(fy)
        if ym is not None and not ym.is_na() and sv is not None:
            yv = abs(float(ym.value))
            dev = abs(yv - sv[0]) / abs(sv[0]) if sv[0] else float("nan")
            out["abweichungen"].append({"periode": f"FY{fy}", "yfinance": yv,
                                        "sec": sv[0], "tag": sv[1],
                                        "abweichung": dev})
    ttm = getattr(m, "ttm", None)
    ttm_ie = None
    if ttm is not None and ttm.ttm.get("interest_expense") is not None \
            and not ttm.ttm["interest_expense"].is_na():
        ttm_ie = abs(float(ttm.ttm["interest_expense"].value))
    if ttm_ie and out["TTM"] is not None:
        sv, tag = out["TTM"]
        dev = abs(ttm_ie - sv) / abs(sv) if sv else float("nan")
        out["abweichungen"].append({"periode": "TTM", "yfinance": ttm_ie,
                                    "sec": sv, "tag": tag, "abweichung": dev})
    return out
