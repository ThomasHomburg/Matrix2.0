"""Live-Bewertung der Watchlist (live_update.py --ranking <csv>).

Screener-Logik bleibt unveraendert -- dieses Modul liest die ranking-CSV
(kurs, calc_date, iv_*, naechster_earnings, ...) und
bewertet die Watchlist-Titel mit dem AKTUELLEN Tagesschlusskurs.

Aufruf:
    python live_update.py --ranking ranking_test_v39.csv [--out watchlist_live.csv]
        [--heute YYYY-MM-DD] [--batch 20] [--pause 2.0]

Watchlist = Status Kandidat/Beobachtung/Beobachtung (nicht normalisiert)
(config watchlist.statuses) plus config watchlist.extra_tickers (Default IT, FDS).

Tagesschlusskurse per yfinance im Batch, Fehlerlog je Ticker (ein Fehler
stoppt den Lauf nicht).

Je Ticker:
  mos_live je Modus = 1 - kurs / iv_<modus> (iv_avg3/iv_latest/iv_ttm/iv_ttm_only,
  iv_base = aktiver Modus); mos_live_min = Minimum ueber die Modi (Regel min_all,
  wie dcf.mos_min_mode=min); mos_live_robust = 1 - kurs / iv_base_10pct.
  Bear-Pruefung + Kandidatenregel: bestehende Regelfunktion
  valuation/dcf.py::is_buy_candidate (KEINE Doppelimplementierung) --
  status_live = Kandidat, wenn is_buy_candidate(quality, dcf_live, kurs, cfg,
  mos_rule=mos_live_min) True, sonst Beobachtung/nein analog der zweistufigen Regel.
  kaufzone_kurs = hoechster Kurs, bei dem die Kandidatenregel erfuellt ist:
      kurs <= min(iv_mode) * (1 - buy_margin_of_safety_min)  (MoS-Bedingung)
      kurs <= iv_bear / buy_bear_floor                        (Bear-Bedingung)
  distanz_pct = kaufzone_kurs / kurs - 1.

Flags:
  wert_veraltet: calc_date (oder kurs_datum) aelter als watchlist.wert_veraltet_tage
      (Default 100 Tage) ODER naechster_earnings liegt in der Vergangenheit.
  alarm: Kurs <= -(watchlist.alarm_kurs_fall_pct) seit calc_date (Default -10 %)
      ODER Tagesbewegung >= +/- watchlist.alarm_tagesbewegung_pct (Default 8 %).

Ausgabe: watchlist_live.csv + Konsolentabelle.
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from config import load_config
from valuation.dcf import DcfResult, DcfScenario, is_buy_candidate

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s",
                    stream=sys.stderr)
logger = logging.getLogger("value_screener.live_update")

MODI = ("avg3", "latest", "ttm", "ttm_only")

# Spalten der Ausgabe (watchlist_live.csv + Konsole)
OUT_COLS = ["ticker", "kurs", "status_screener", "status_live",
            "mos_live_min", "mos_live_robust", "kaufzone_kurs", "distanz_pct",
            "timing_status", "mom_12_1", "abstand_sma200", "revision_90d",
            "wert_veraltet", "alarm", "naechster_earnings",
            # Smart Money
            "smart_money_score", "fonds_kaeufer", "fonds_verkaeufer",
            "letzte_aktion", "filing_datum", "rationale_vorhanden"]


# --------------------------------------------------------------------------
# Watchlist aus ranking-CSV
# --------------------------------------------------------------------------

def watchlist_tickers(rows: list[dict], cfg: dict) -> list[str]:
    """Status in watchlist.statuses + watchlist.extra_tickers (dedup, Reihenfolge)."""
    statuses = set(cfg.get("watchlist", {}).get(
        "statuses", ["Kandidat", "Beobachtung", "Beobachtung (nicht normalisiert)"]))
    extra = [str(t).upper() for t in cfg.get("watchlist", {}).get("extra_tickers", ["IT", "FDS"])]
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        st = (r.get("status") or "").strip()
        t = (r.get("ticker") or "").strip().upper()
        if t and t not in seen and st in statuses:
            seen.add(t)
            out.append(t)
    for t in extra:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def load_watch_file(path: str) -> list[str]:
    """--watch DATEI: Spalte ticker (wie ticker_watch_v40.csv), dedup, upper."""
    out: list[str] = []
    seen: set[str] = set()
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = (r.get("ticker") or "").strip().lstrip("\ufeff").upper()
            if t and not t.startswith("#") and t not in seen:
                seen.add(t)
                out.append(t)
    return out


def korb_tickers(db_path: Path) -> tuple[list[str], str]:
    """Alle Ticker der Koerbe des letzten decisions-Snapshots (neuestes datum mit
    korb<>''). Rueckgabe: (ticker, snapshot_datum); ohne DB/leer -> ([], '')."""
    if not db_path.exists():
        return [], ""
    con = sqlite3.connect(db_path)
    try:
        tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "decisions" not in tabs:
            return [], ""
        cols = {r[1] for r in con.execute("PRAGMA table_info(decisions)")}
        if "korb" not in cols:
            return [], ""
        r = con.execute("SELECT MAX(datum) FROM decisions WHERE korb<>''").fetchone()
        snap = (r[0] or "") if r else ""
        if not snap:
            return [], ""
        out = []
        seen = set()
        for row in con.execute("SELECT DISTINCT ticker FROM decisions WHERE korb<>'' AND datum=?",
                               (snap,)):
            t = (row[0] or "").strip().upper()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out, snap
    finally:
        con.close()


def watchlist_scope(rows: list[dict], cfg: dict, watch_csv: str | None = None,
                    db_path: Path = Path("value_tracker.db")) -> tuple[list[str], dict]:
    """v60: Live-Umfang = Vereinigung aus (1) Kandidat/Beobachtung des --ranking,
    (2) watchlist.extra_tickers, (3) --watch-Datei, (4) allen Tickern der Koerbe des
    letzten decisions-Snapshots. Rueckgabe: (ticker_liste, {quelle: anzahl})."""
    wcfg = cfg.get("watchlist", {})
    statuses = set(wcfg.get("statuses", ["Kandidat", "Beobachtung", "Beobachtung (nicht normalisiert)"]))
    ranking = [(r.get("ticker") or "").strip().upper() for r in rows
               if (r.get("status") or "").strip() in statuses and (r.get("ticker") or "").strip()]
    extra = [str(t).upper() for t in wcfg.get("extra_tickers", [])]
    watch = load_watch_file(watch_csv) if watch_csv else []
    koerbe, snap = korb_tickers(db_path)
    union: list[str] = []
    seen: set[str] = set()
    for t in ranking + extra + watch + koerbe:
        if t and t not in seen:
            seen.add(t)
            union.append(t)
    quellen = {"ranking": len(ranking), "extra_tickers": len(extra),
               "watch_datei": len(watch), "decisions_koerbe": len(koerbe)}
    quellen["snapshot_datum"] = snap
    return union, quellen


def load_ranking_rows(ranking_csv: str) -> list[dict]:
    """Ranking-CSV laden (v41 Export-Spalten als Strings; numerische Felder via _f)."""
    with open(ranking_csv, newline="", encoding="utf-8") as f:
        rows = [r for r in csv.DictReader(f) if r.get("ticker") and not r["ticker"].startswith("#")]
    return rows


def _f(row: dict, *keys: str) -> float:
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return float("nan")


# --------------------------------------------------------------------------
# Kurse: yfinance im Batch (injizierbar fuer Tests), Fehlerlog je Ticker
# --------------------------------------------------------------------------

def fetch_prices(tickers: list[str], batch: int = 20, pause: float = 2.0,
                 fetcher=None) -> tuple[dict[str, dict], list[dict]]:
    """Tagesschlusskurse per yfinance im Batch. Rueckgabe: (prices, errors).

    prices: {ticker: {"kurs": float, "datum": ISO, "vorheriger": float|nan}}
    fetcher: optionale Funktion ticker -> {"kurs":..., "datum":..., "vorheriger":...}
    (Default: yfinance). Fehler je Ticker stoppen den Lauf nicht (errors-Liste).
    """
    if fetcher is None:
        def fetcher(t: str) -> dict:
            import yfinance as yf
            hist = yf.Ticker(t).history(period="5d")
            if hist is None or hist.empty:
                raise ValueError("keine Kursdaten")
            closes = hist["Close"].dropna()
            return {
                "kurs": float(closes.iloc[-1]),
                "datum": closes.index[-1].date().isoformat(),
                "vorheriger": float(closes.iloc[-2]) if len(closes) >= 2 else float("nan"),
            }
    prices: dict[str, dict] = {}
    errors: list[dict] = []
    for start in range(0, len(tickers), max(1, batch)):
        chunk = tickers[start:start + max(1, batch)]
        logger.info("Kurs-Batch %d-%d von %d", start + 1, start + len(chunk), len(tickers))
        for t in chunk:
            try:
                prices[t] = fetcher(t)
            except Exception as exc:  # Fehler loggen, Lauf stoppt nicht
                logger.error("Fehler bei %s: %s", t, exc)
                errors.append({"ticker": t, "error": str(exc)[:200]})
        if start + batch < len(tickers):
            time.sleep(pause)
    if errors:
        logger.warning("Fehlerlog (%d): %s", len(errors), ", ".join(e["ticker"] for e in errors))
    return prices, errors


# --------------------------------------------------------------------------
# Live-Bewertung je Ticker (reine Funktion aus Row + Kurs -- testbar ohne Netz)
# --------------------------------------------------------------------------

def _dcf_live(row: dict, iv_bear: float, iv_base: float, iv_bull: float,
              mos_live_min: float) -> DcfResult:
    """Leichtes DcfResult fuer die bestehende Regelfunktion is_buy_candidate
    (valuation/dcf.py) -- keine Doppelimplementierung der Kandidatenregel."""
    nan = float("nan")
    return DcfResult(
        fcf_normalized=nan, fcf_years_used=0, revenue_cagr=nan,
        discount_rate=nan, terminal_growth=nan, shares=nan,
        scenarios={
            "bear": DcfScenario("bear", [], [], nan, nan, nan, iv_bear),
            "base": DcfScenario("base", [], [], nan, nan, nan, iv_base),
            "bull": DcfScenario("bull", [], [], nan, nan, nan, iv_bull),
        },
        margin_of_safety=mos_live_min,
        fcf_mode="live",
    )


def live_row(row: dict, kurs: float, cfg: dict, heute: date | None = None,
             vorheriger_kurs: float = float("nan")) -> dict:
    """Live-Bewertung einer ranking-Row mit aktuellem Kurs.

    Nutzt fuer die Kandidatenregel die bestehende Funktion
    valuation/dcf.py::is_buy_candidate (mos_rule=mos_live_min, Regel min_all).
    """
    heute = heute or date.today()
    dc = cfg["dcf"]
    wcfg = cfg.get("watchlist", {})
    quality = _f(row, "quality")
    iv = {m: _f(row, f"iv_{m}") for m in MODI}
    iv_base = _f(row, "iv_base")
    iv_bear = _f(row, "iv_bear")
    iv_bull = _f(row, "iv_bull")
    iv_robust = _f(row, "iv_base_10pct")

    # mos_live je Modus = 1 - kurs/iv; mos_live_min = Minimum (Regel min_all)
    mos_live = {m: (1.0 - kurs / v if v == v and v > 0 else float("nan")) for m, v in iv.items()}
    mos_live["base"] = 1.0 - kurs / iv_base if iv_base == iv_base and iv_base > 0 else float("nan")
    vals = [v for v in mos_live.values() if v == v]
    mos_live_min = min(vals) if vals else float("nan")
    mos_live_robust = 1.0 - kurs / iv_robust if iv_robust == iv_robust and iv_robust > 0 else float("nan")

    # status_live ueber die bestehende Regelfunktion (keine Doppelimplementierung)
    dcf_live = _dcf_live(row, iv_bear, iv_base, iv_bull, mos_live_min)
    is_buy = is_buy_candidate(quality, dcf_live, kurs, cfg, mos_rule=mos_live_min)
    quality_min = float(dc.get("buy_quality_min", 60))
    mos_min = float(dc.get("buy_margin_of_safety_min", 0.30))
    watch_min = float(dc.get("watch_margin_of_safety_min", 0.30))
    quality_ok = quality == quality and quality >= quality_min
    if is_buy:
        status_live = "Kandidat"
    elif quality_ok and vals and max(vals) >= watch_min:
        status_live = "Beobachtung"
    else:
        status_live = "nein"

    # kaufzone_kurs: hoechster Kurs, bei dem die Kandidatenregel erfuellt ist
    # MoS-Bedingung: kurs <= min(iv Modi incl. base) * (1 - mos_min)
    # Bear-Bedingung: iv_bear >= floor * kurs -> kurs <= iv_bear / floor
    floor = float(dc.get("buy_bear_floor", 0.80))
    iv_vals = [v for v in list(iv.values()) + [iv_base] if v == v and v > 0]
    kaufzone = float("nan")
    if iv_vals:
        kaufzone = min(iv_vals) * (1.0 - mos_min)
    if iv_bear == iv_bear and iv_bear > 0 and floor > 0:
        kaufzone = min(kaufzone, iv_bear / floor) if kaufzone == kaufzone else iv_bear / floor
    distanz_pct = kaufzone / kurs - 1.0 if kaufzone == kaufzone and kurs > 0 else float("nan")

    # Flags
    calc_date_s = (row.get("calc_date") or row.get("kurs_datum") or "").strip()
    wert_veraltet = False
    if calc_date_s:
        try:
            calc_d = date.fromisoformat(calc_date_s[:10])
            if (heute - calc_d).days > int(wcfg.get("wert_veraltet_tage", 100)):
                wert_veraltet = True
        except ValueError:
            pass
    ne = (row.get("naechster_earnings") or "").strip()
    if ne:
        try:
            if date.fromisoformat(ne[:10]) < heute:
                wert_veraltet = True  # naechster_earnings liegt in der Vergangenheit
        except ValueError:
            pass
    kurs_calc = _f(row, "kurs")
    alarm = False
    if kurs_calc == kurs_calc and kurs_calc > 0:
        if kurs <= kurs_calc * (1.0 - float(wcfg.get("alarm_kurs_fall_pct", 0.10))):
            alarm = True  # Kurs <= -10 % seit calc_date
    if vorheriger_kurs == vorheriger_kurs and vorheriger_kurs > 0:
        if abs(kurs / vorheriger_kurs - 1.0) >= float(wcfg.get("alarm_tagesbewegung_pct", 0.08)):
            alarm = True  # Tagesbewegung >= +/-8 %

    return {
        "ticker": (row.get("ticker") or "").strip().upper(),
        "kurs": kurs,
        "status_screener": (row.get("status") or "").strip(),
        "status_live": status_live,
        "mos_live_min": mos_live_min,
        "mos_live_robust": mos_live_robust,
        "kaufzone_kurs": kaufzone,
        "distanz_pct": distanz_pct,
        "timing_status": "",       # wird von timing.py ergaenzt (--with-timing)
        "mom_12_1": float("nan"),
        "abstand_sma200": float("nan"),
        "revision_90d": float("nan"),
        "wert_veraltet": int(wert_veraltet),
        "alarm": int(alarm),
        "naechster_earnings": ne,
        # Smart-Money-Spalten (DB optional -- ohne DB n/a)
        "smart_money_score": "n/a",
        "fonds_kaeufer": "",
        "fonds_verkaeufer": "",
        "letzte_aktion": "",
        "filing_datum": "",
        "rationale_vorhanden": "",
    }


def attach_smart_money(out_rows: list[dict], cfg: dict, db_path: Path = Path("value_tracker.db")) -> None:
    """fuellt die smart_money-Spalten je Watchlist-Ticker aus der DB
    (smart_money_score). Ohne DB bleiben die Spalten 'n/a'."""
    if not db_path.exists():
        return
    try:
        import sqlite3 as _sq
        con = _sq.connect(db_path)
        tab = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        if "smart_money_positions" not in tab:
            return  # DB ohne smart_money-Tabellen -> Spalten bleiben n/a
        from smart_money import smart_money_score
        for r in out_rows:
            s = smart_money_score(r["ticker"], cfg, db_path=db_path)
            r["smart_money_score"] = s["score"]
            r["fonds_kaeufer"] = ",".join(s["fonds_kaeufer"])
            r["fonds_verkaeufer"] = ",".join(s["fonds_verkaeufer"])
            r["letzte_aktion"] = s["letzte_aktion"]
            r["filing_datum"] = s["filing_datum"]
            r["rationale_vorhanden"] = "ja" if s["rationale_vorhanden"] else "nein"
    except Exception as exc:
        logger.warning("smart_money-Spalten nicht verfuegbar: %s", exc)


def _fmt(v: float, spec: str) -> str:
    return "n/a" if v != v else format(v, spec)


def _fmt_flag(v) -> str:
    return "JA" if v else ""


def write_watchlist_csv(rows: list[dict], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(OUT_COLS)
        for r in rows:
            w.writerow([("" if r.get(c) is None else r.get(c)) for c in OUT_COLS])
    logger.info("watchlist_live gespeichert: %s (%d Ticker)", out_path, len(rows))


def _yf_history(ticker: str, period: str) -> "pd.Series":
    """Default-Historie-Fetcher: yfinance Tageskurse (Close) als Series."""
    import yfinance as yf
    hist = yf.Ticker(ticker).history(period=period)
    if hist is None or hist.empty:
        raise ValueError("keine Kursdaten")
    return hist["Close"].dropna()


def backfill_prices(con: sqlite3.Connection, tickers: list[str], batch: int = 20,
                    pause: float = 2.0, hist_fetcher=None) -> tuple[int, list[dict]]:
    """v60: Kurshistorie in prices laden. Erster Lauf je Ticker (kein Datum in
    prices) -> 2 Jahre Tagesschlusskurse (period='2y'); danach nur neue Tage seit
    dem letzten gespeicherten Datum (period='5d'). Batch + Fortschritt + Fehlerlog
    je Ticker (ein Fehler stoppt den Lauf nicht). hist_fetcher: (ticker, period) ->
    Series -- injizierbar fuer Tests ohne Netz. Rueckgabe: (eingefuegte Zeilen, errors)."""
    import pandas as pd  # lazy
    if hist_fetcher is None:
        hist_fetcher = _yf_history
    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    errors: list[dict] = []
    for start in range(0, len(tickers), max(1, batch)):
        chunk = tickers[start:start + max(1, batch)]
        logger.info("Historie-Batch %d-%d von %d", start + 1, start + len(chunk), len(tickers))
        for t in chunk:
            try:
                r = con.execute("SELECT MAX(datum) FROM prices WHERE ticker=?", (t,)).fetchone()
                last = r[0] if r else None
                period = "2y" if not last else "5d"   # erst 2 Jahre, dann nur neue Tage
                closes = hist_fetcher(t, period)
                if closes is None or len(closes) == 0:
                    raise ValueError("keine Kursdaten")
                if not isinstance(closes, pd.Series):
                    closes = pd.Series(closes)
                n = 0
                for d, k in closes.items():
                    ds = d.date().isoformat() if hasattr(d, "date") else str(d)[:10]
                    if last and ds <= last:
                        continue
                    if k is None or k != k:
                        continue
                    con.execute("INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am)"
                                " VALUES (?,?,?,'yfinance',?)", (t, ds, float(k), now))
                    n += 1
                inserted += n
                logger.info("%s: %d Kurse (%s, letzter Stand %s -> %s)", t, n, period, last or "neu",
                            closes.index[-1].date().isoformat() if hasattr(closes.index[-1], "date") else closes.index[-1])
            except Exception as exc:
                logger.error("Historie-Fehler bei %s: %s", t, exc)
                errors.append({"ticker": t, "error": str(exc)[:200]})
        if start + batch < len(tickers):
            time.sleep(pause)
    con.commit()
    return inserted, errors


def timing_from_db(con: sqlite3.Connection, tickers: list[str]) -> dict[str, dict]:
    """v60: timing_kennzahlen (timing.py) je Ticker aus der prices-Historie in der DB
    (2y Tagesschlusskurse). Rueckgabe: {ticker: kennzahlen-dict}; Ticker mit < 260
    Tagen bleiben aussen (n/a)."""
    import pandas as pd
    import timing as timing_mod
    out: dict[str, dict] = {}
    for t in tickers:
        rows = con.execute("SELECT datum, kurs FROM prices WHERE ticker=? ORDER BY datum",
                           (t,)).fetchall()
        if len(rows) < 260:
            continue
        s = pd.Series([float(r[1]) for r in rows],
                      index=pd.to_datetime([r[0] for r in rows]))
        out[t] = timing_mod.timing_kennzahlen(s)
    return out


def konsistenz_warnung(ticker: str, mos_live_min: float, mos_rule: float,
                       max_pp: float = 0.02) -> str | None:
    """v62 Konsistenz: am Tag des Rankings muessen mos_live_min und mos_rule (Screener)
    uebereinstimmen; Abweichung > max_pp (2 Prozentpunkte) -> Warnung."""
    if mos_live_min != mos_live_min or mos_rule != mos_rule:
        return None
    diff = abs(mos_live_min - mos_rule)
    if diff > max_pp:
        return (f"Konsistenz {ticker}: |mos_live_min - mos_rule| = {diff * 100:.2f} Pp "
                f"> {max_pp * 100:.0f} Pp (mos_live_min={mos_live_min:.4f}, mos_rule={mos_rule:.4f})")
    return None


def migrate_share_basis(con: sqlite3.Connection, cfg: dict, rows_by_t: dict) -> int:
    """v62 Altbestand: valuations/decisions ohne aktien_aktuell werden umgerechnet:
    iv_aktuell = iv_base * kurs_boerse / kurs_marktkap_basis (aus der ranking-Zeile),
    aktien_aktuell uebernommen, Flag 'umgerechnet' gesetzt. decisions-Zeilen mit datum =
    config decisions.share_basis_fix.datum bekommen hinweis = share_basis_fix.hinweis.
    Keine Zeile wird geloescht oder ueberschrieben (nur ergaenzende Spalten)."""
    fix = cfg.get("decisions", {}).get("share_basis_fix", {}) or {}
    fix_datum = str(fix.get("datum", "2026-11-11"))
    hinweis = str(fix.get("hinweis",
                          "Aktienbasis-Fix: p, EW und Kaufzone ungueltig, Rendite-Start unveraendert"))
    n = 0
    for tick, r in rows_by_t.items():
        kb = _f(r, "kurs_boerse")
        kmb = _f(r, "kurs_marktkap_basis", "kurs")
        ak = _f(r, "aktien_aktuell")
        if kb != kb or kmb != kmb or kb <= 0 or kmb <= 0:
            continue
        for table in ("valuations", "decisions"):
            cols = {x[1] for x in con.execute(f"PRAGMA table_info({table})")}
            if not {"aktien_aktuell", "iv_base"} <= cols:
                continue
            rows = con.execute(
                f"SELECT rowid, iv_base FROM {table} WHERE ticker=? AND aktien_aktuell IS NULL "
                f"AND iv_base IS NOT NULL", (tick,)).fetchall()
            for rowid, iv_base in rows:
                iv_akt = iv_base * kb / kmb
                con.execute(
                    f"UPDATE {table} SET aktien_aktuell=?, iv_aktuell=?, "
                    f"flags=CASE WHEN flags LIKE '%umgerechnet%' THEN flags "
                    f"ELSE trim(coalesce(flags,'') || ' umgerechnet') END WHERE rowid=?",
                    (ak if ak == ak else None, iv_akt, rowid))
                n += 1
            if table == "decisions" and "hinweis" in cols:
                con.execute("UPDATE decisions SET hinweis=? WHERE ticker=? AND datum=? "
                            "AND (hinweis IS NULL OR hinweis='')",
                            (hinweis, tick, fix_datum))
    con.commit()
    return n


def store_live_status(con: sqlite3.Connection, out_rows: list[dict],
                      prices: dict[str, dict]) -> None:
    """v53: Live-Stand in value_tracker.db persistieren (Seite 5 Watchlist Live
    liest von dort): Kurse -> prices, Live-Status -> watchlist_status."""
    now = datetime.now().isoformat(timespec="seconds")
    for t, p in prices.items():
        con.execute(
            "INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am) "
            "VALUES (?,?,?,?,?)",
            (t, p["datum"], p["kurs"], "yfinance", now))
    for r in out_rows:
        con.execute(
            """INSERT OR REPLACE INTO watchlist_status
               (ticker, updated_at, kurs, status_screener, status_live, mos_live_min,
                mos_live_robust, kaufzone_kurs, distanz_pct, timing_status,
                wert_veraltet, alarm)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (r["ticker"], now, r["kurs"], r["status_screener"], r["status_live"],
             r["mos_live_min"], r["mos_live_robust"], r["kaufzone_kurs"], r["distanz_pct"],
             r["timing_status"], r["wert_veraltet"], r["alarm"]))
    con.commit()
    logger.info("watchlist_status + prices aktualisiert (%d Ticker)", len(out_rows))


def print_table(rows: list[dict]) -> None:
    print(f"\n=== watchlist_live ({len(rows)} Ticker) ===")
    header = (f"{'ticker':<8} {'kurs':>8} {'status_screener':<26} {'status_live':<12} "
              f"{'mos_min':>8} {'mos_rob':>8} {'kaufzone':>9} {'dist%':>7} "
              f"{'timing':<8} {'mom_12_1':>8} {'sma200%':>7} {'rev90d':>7} "
              f"{'veraltet':<8} {'alarm':<6} {'sm':>3} {'kaeufer':<22} {'verkaeufer':<14} "
              f"naechster_earnings")
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['ticker']:<8} {_fmt(r['kurs'], '>8.2f')} {r['status_screener'][:26]:<26} "
              f"{r['status_live']:<12} {_fmt(r['mos_live_min'], '>8.1%')} "
              f"{_fmt(r['mos_live_robust'], '>8.1%')} {_fmt(r['kaufzone_kurs'], '>9.2f')} "
              f"{_fmt(r['distanz_pct'], '>7.1%')} {(r['timing_status'] or 'n/a'):<8} "
              f"{_fmt(r['mom_12_1'], '>8.1%')} {_fmt(r['abstand_sma200'], '>7.1%')} "
              f"{_fmt(r['revision_90d'], '>7.1%')} "
              f"{_fmt_flag(r['wert_veraltet']):<8} {_fmt_flag(r['alarm']):<6} "
              f"{str(r.get('smart_money_score', 'n/a')):>3} "
              f"{(r.get('fonds_kaeufer') or '')[:22]:<22} {(r.get('fonds_verkaeufer') or '')[:14]:<14} "
              f"{r['naechster_earnings']}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live-Bewertung der Watchlist (--ranking <csv>)")
    p.add_argument("--ranking", required=True, help="ranking CSV aus run_ranking.py (v41 Export-Spalten)")
    p.add_argument("--out", default="watchlist_live.csv", help="Ausgabe-CSV (Default: watchlist_live.csv)")
    p.add_argument("--heute", default=None, help="Referenzdatum YYYY-MM-DD (Default: heute; Tests)")
    p.add_argument("--db", default="value_tracker.db", help="SQLite-DB fuer smart_money-Spalten (v42)")
    p.add_argument("--watch", default=None,
                   help="v60: zusaetzliche Watchlist-Datei (Spalte ticker); Umfang = Vereinigung "
                        "aus Ranking + extra_tickers + --watch + decisions-Koerben (letzter Snapshot)")
    p.add_argument("--batch", type=int, default=20, help="Batch-Groesse fuer Kursabruf")
    p.add_argument("--pause", type=float, default=2.0, help="Pause zwischen Batches in s")
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    cfg = load_config()
    heute = date.fromisoformat(args.heute) if args.heute else date.today()
    if not Path(args.ranking).exists():
        print(f"Fehler: Ranking-Datei nicht gefunden: {args.ranking}", file=sys.stderr)
        sys.exit(1)
    rows_in = load_ranking_rows(args.ranking)
    # v60: Umfang = Vereinigung aus Ranking + extra_tickers + --watch + decisions-Koerben
    tickers, quellen = watchlist_scope(rows_in, cfg, watch_csv=args.watch, db_path=Path(args.db))
    if not tickers:
        print("Fehler: 0 Watchlist-Ticker (watchlist.statuses/extra_tickers/--watch/Koerbe pruefen).",
              file=sys.stderr)
        sys.exit(1)
    print("Umfang je Quelle: ranking={ranking}, extra_tickers={extra_tickers}, "
          "watch_datei={watch_datei}, decisions_koerbe={decisions_koerbe} "
          "(Snapshot {snapshot_datum}) -> {n} eindeutige Ticker".format(
              n=len(tickers), **{k: quellen[k] for k in
                                 ("ranking", "extra_tickers", "watch_datei", "decisions_koerbe",
                                  "snapshot_datum")}))
    logger.info("Watchlist: %d Ticker (%s)", len(tickers), ", ".join(tickers))
    # v62: Benchmark-Ticker (config decisions.baskets.benchmark) kommen in den Kurs-Batch
    # (2y Historie in prices), auch wenn sie nicht in der Watchlist stehen
    bench = [str(x).upper() for x in
             cfg.get("decisions", {}).get("baskets", {}).get("benchmark", ["SPY", "RSP"])]
    batch_tickers = list(tickers) + [b for b in bench if b not in tickers]
    if len(batch_tickers) > len(tickers):
        print(f"Benchmark im Kurs-Batch: {', '.join(b for b in bench if b not in tickers)}")
    prices, errors = fetch_prices(batch_tickers, batch=args.batch, pause=args.pause)
    rows_by_t = {(r.get("ticker") or "").strip().upper(): r for r in rows_in}
    out_rows = []
    for t in tickers:
        if t not in prices:
            continue  # Fehler bereits geloggt
        row = rows_by_t.get(t) or {"ticker": t, "status": ""}
        r2 = live_row(row, prices[t]["kurs"], cfg, heute=heute,
                      vorheriger_kurs=prices[t].get("vorheriger", float("nan")))
        out_rows.append(r2)
        # v62 Konsistenz: live_update warnt bei Abweichung mos_live_min vs mos_rule > 2 Pp
        w = konsistenz_warnung(t, r2["mos_live_min"], _f(row, "mos_rule"))
        if w:
            logger.warning(w)
    attach_smart_money(out_rows, cfg, db_path=Path(args.db))
    # v53/v57/v60: Live-Stand + Kurshistorie + Timing in die DB schreiben
    from decisions import init_db  # lazy import (Schema anlegen, falls DB neu)
    from db_schema import ensure_schema  # v57 zentraler Schema-Guard
    con = init_db(Path(args.db))
    ensure_schema(con)
    # v60: Kurshistorie backfillen (erst 2y, dann nur neue Tage), dann Timing aus prices
    n_hist, hist_errors = backfill_prices(con, batch_tickers, batch=args.batch, pause=args.pause)
    errors = errors + hist_errors
    tim = timing_from_db(con, tickers)
    for r in out_rows:
        tk = r["ticker"]
        if tk in tim:
            r["timing_status"] = tim[tk]["timing_status"] or ""
            r["mom_12_1"] = tim[tk]["mom_12_1"]
            r["abstand_sma200"] = tim[tk]["abstand_sma200"]
    logger.info("Historie: %d Kurse eingefuegt", n_hist)
    store_live_status(con, out_rows, prices)   # inkl. timing_status in watchlist_status
    # v62 Altbestand: valuations/decisions ohne aktien_aktuell umrechnen (Flag umgerechnet)
    n_fix = migrate_share_basis(con, cfg, rows_by_t)
    if n_fix:
        logger.info("Aktienbasis-Fix: %d Alt-Zeilen umgerechnet (iv_aktuell, Flag umgerechnet)", n_fix)
    con.close()
    write_watchlist_csv(out_rows, args.out)
    print_table(out_rows)
    if errors:
        print(f"\nFehlerlog ({len(errors)}):")
        for e in errors:
            print(f"  {e['ticker']}: {e['error'][:120]}")


if __name__ == "__main__":
    main()
