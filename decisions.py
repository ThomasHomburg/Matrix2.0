"""Entscheidungslog (decisions.py) -- SQLite value_tracker.db.

Screener-Logik bleibt unveraendert; dieses Modul ist reine Ablage/Protokoll.

Aufrufe:
    python decisions.py init                                  # DB + Schema anlegen
    python decisions.py add TICKER --aktion watch|kauf|verkauf|nichts \\
        --thesis "..." --break "..." [--kurs 123.4] [--horizont 12|24]
    python decisions.py snapshot [--ranking ranking.csv]      # monatlicher Auto-Eintrag
    python decisions.py review [--ranking ranking.csv] [--heute YYYY-MM-DD]
    python decisions.py list [--ticker TICKER]

add: speichert Datum, Kurs, Screener-Status, iv_* (aus valuations-Snapshot zum
     calc_date bzw. --ranking CSV), Szenariogewichte (Default 25/50/25 aus
     config watchlist.szenario_gewichte), Erwartungswert in % (p*100, p = Summe
     der Gewichte der Szenarien mit iv >= 1,10 x Kurs) und Horizont 12/24 Monate.
snapshot: legt monatlich automatisch einen Eintrag (aktion=auto) fuer alle
     Kandidaten und Beobachtungstitel an (Track-Record ohne manuelle Arbeit).
     v52: snapshot --korb all [--datum JJJJ-MM-TT] schreibt je Ticker und Korb
     (screener|divergenz|kontrolle|benchmark, config decisions.baskets) eine Zeile;
     idempotent je (ticker, korb, datum), geschriebene Zeilen werden nie ueberschrieben.
     v56 Look-ahead: --datum default = heute (Tag des Snapshots); Startkurs = Kurs am
     Snapshot-Tag (prices); start_datum = max(--datum, juengstes filing_datum der
     verwendeten 13F); --datum vor filing_datum -> Abbruch "Look-ahead" vor dem Schreiben.
review: fuellt fuer Eintraege aelter als 12/24 Monate den Kurs nach (aus prices
     oder --ranking CSV), berechnet Rendite und Brier-Score. v52: Korb-Review je
     Korb und Horizont (3/6/12/24 Monate) -> Tabelle + decisions_review.csv
     (Mittelwert, Median, Anteil Ueberrendite vs. SPY, Differenz zu kontrolle,
     n, Brier; n < review_min_eintraege -> "unkalibriert, n=...").
     Ereignis E = Rendite nach 12 Monaten >= +10 % (config watchlist.ereignis_rendite_pct).
     Prognosewahrscheinlichkeit p = Summe der Gewichte der Szenarien mit iv >= 1,10 x Kurs.
     Bei n < watchlist.review_min_eintraege (Default 30): "unkalibriert, n=..."
     statt Kennzahl.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from config import load_config
from db_schema import ensure_schema

DB_PATH = Path("value_tracker.db")
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

AKTIONEN = ("watch", "kauf", "verkauf", "nichts")
HORIZONTS = (12, 24)


# v52 Vergleichkoerbe: neue Spalten der decisions-Tabelle; Alt-DBs per ALTER
DECISIONS_KORB_SPALTEN = (
    ("korb", "TEXT NOT NULL DEFAULT ''"),
    ("kurs_datum", "TEXT"),
    ("quality", "REAL"),
    ("mos_min", "REAL"),
    ("mos_robust", "REAL"),
    ("kaufzone_kurs", "REAL"),
    ("timing_status", "TEXT"),
    ("smart_money_score", "INTEGER"),
    ("urteil", "TEXT"),
    ("divergenz_stufe", "TEXT"),
    ("fonds_kaeufer", "TEXT"),
    ("fonds_verkaeufer", "TEXT"),
    ("wahrscheinlichkeit_p", "REAL"),
    ("start_datum", "TEXT"),   # v56 Look-ahead: max(Snapshot-Datum, juengstes filing_datum verwendeter 13F)
)

KORB_NAMEN = ("screener", "divergenz", "kontrolle", "benchmark")


def _migrate_decisions(con: sqlite3.Connection) -> None:
    """v52: Korb-Spalten per ALTER additiv ergaenzen (Alt-DBs) + partieller
    UNIQUE-Index (ticker, korb, datum) fuer Korb-Zeilen -- Idempotenz der Snapshots;
    geschriebene Zeilen werden nie ueberschrieben."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(decisions)")}
    if not cols:
        return
    for col, decl in DECISIONS_KORB_SPALTEN:
        if col not in cols:
            con.execute(f"ALTER TABLE decisions ADD COLUMN {col} {decl}")
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_korb_datum "
                "ON decisions(ticker, korb, datum) WHERE korb <> ''")
    con.commit()


def init_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Legt value_tracker.db an; ensure_schema gleicht das Schema ab (idempotent)."""
    con = sqlite3.connect(db_path)
    ensure_schema(con, schema_path=SCHEMA_PATH)   # v57 zentraler Schema-Guard
    _migrate_decisions(con)     # Datenmigration: periode aus report_date relabeln
    con.commit()
    return con


def _connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    if not db_path.exists():
        return init_db(db_path)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    ensure_schema(con, schema_path=SCHEMA_PATH)   # v57: Alt-DB wird migriert (korb/start_datum per ALTER)
    _migrate_decisions(con)
    return con


def _heute() -> date:
    return date.today()


def _parse_date(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


# --------------------------------------------------------------------------
# Snapshot-Helfer: iv_*/status je Ticker aus ranking-CSV oder valuations-Tabelle
# --------------------------------------------------------------------------

def _row_aus_ranking(ranking_csv: str | None, ticker: str) -> dict | None:
    """Zeile eines Tickers aus der ranking-CSV (Export-Spalten)."""
    if not ranking_csv or not Path(ranking_csv).exists():
        return None
    with open(ranking_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("ticker") or "").strip().upper() == ticker.upper():
                return row
    return None


def _latest_valuation(con: sqlite3.Connection, ticker: str) -> dict | None:
    """Neuester valuations-Snapshot je Ticker (falls vorhanden)."""
    cur = con.execute(
        "SELECT * FROM valuations WHERE ticker=? ORDER BY calc_date DESC LIMIT 1",
        (ticker.upper(),))
    r = cur.fetchone()
    return dict(r) if r else None


def _vals(row: dict | None) -> dict:
    """iv_*/status/kurs aus einer ranking-CSV-Zeile oder valuations-Zeile."""
    def _f(*keys) -> float:
        for k in keys:
            v = (row or {}).get(k)
            if v not in (None, ""):
                try:
                    x = float(v)
                    return x
                except (TypeError, ValueError):
                    pass
        return float("nan")
    return {
        "status": (row or {}).get("status") or "",
        "kurs": _f("kurs"),
        "iv_base": _f("iv_base"),
        "iv_bear": _f("iv_bear"),
        "iv_bull": _f("iv_bull"),
    }


def erwartungswert_pct(iv_bear: float, iv_base: float, iv_bull: float, kurs: float,
                       gew: dict[str, float]) -> float:
    """Erwartungswert in %: p = Summe der Gewichte der Szenarien mit
    iv >= 1,10 x Kurs; p*100. NaN-Eingaenge zaehlen nicht mit (Gewichtung verfällt)."""
    if kurs != kurs or kurs <= 0:
        return float("nan")
    p = 0.0
    for iv, g in ((iv_bear, gew["bear"]), (iv_base, gew["base"]), (iv_bull, gew["bull"])):
        if iv == iv and iv >= 1.10 * kurs:
            p += g
    return p * 100.0


def _default_gewichte(cfg: dict) -> dict[str, float]:
    w = cfg.get("watchlist", {}).get("szenario_gewichte", {})
    return {
        "bear": float(w.get("bear", 0.25)),
        "base": float(w.get("base", 0.50)),
        "bull": float(w.get("bull", 0.25)),
    }


# --------------------------------------------------------------------------
# v52 Vergleichkoerbe: Zusammensetzung aus ranking + overlap + live CSV
# --------------------------------------------------------------------------

def _load_csv_rows(path: str | None) -> list[dict]:
    if not path or not Path(path).exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f)
                if r.get("ticker") and not str(r["ticker"]).startswith("#")]


def _f(row: dict, *keys: str) -> float:
    for k in keys:
        v = (row or {}).get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return float("nan")


def _i(row: dict, *keys: str) -> int | None:
    x = _f(row, *keys)
    return int(x) if x == x else None


def _status(row: dict) -> str:
    return ((row or {}).get("status_all") or (row or {}).get("status") or "").strip()


def _korb_rows(ranking_csv: str | None, overlap_csv: str | None = None,
               live_csv: str | None = None) -> list[dict]:
    """Je Ticker eine angereicherte Zeile: ranking-CSV (Kurs/IV/MoS/Quality/Status)
    + overlap-CSV (Smart Money, v51 inkl. divergenz_stufe) + live-CSV (kaufzone/
    timing, nur wenn in ranking nicht vorhanden)."""
    rows: dict[str, dict] = {}
    for r in _load_csv_rows(ranking_csv):
        t = (r.get("ticker") or "").strip().upper()
        rows[t] = {**r, "ticker": t}
    for r in _load_csv_rows(overlap_csv):
        t = (r.get("ticker") or "").strip().upper()
        base = rows.setdefault(t, {"ticker": t})
        for k in ("smart_money_score", "fonds_kaeufer", "fonds_verkaeufer", "letzte_aktion",
                  "smart_money_urteil", "divergenz_stufe", "signal_hinweis", "etf",
                  "aktion_alter_tage"):
            if k in r:
                base[k] = r[k]
    for r in _load_csv_rows(live_csv):
        t = (r.get("ticker") or "").strip().upper()
        base = rows.setdefault(t, {"ticker": t})
        for k in ("kaufzone_kurs", "timing_status", "kurs", "kurs_datum", "status_screener"):
            if k in r and k not in base:
                base[k] = r[k]
    return list(rows.values())


def koerbe(cfg: dict, rows: list[dict]) -> dict[str, list[dict]]:
    """Korb-Zusammensetzung nach config decisions.baskets.

    screener: alle Ticker mit Status aus screener_statuses.
    divergenz: Status nein + divergenz_stufe "stark" + Quality >= quality_min,
      max max_ticker, sortiert nach Smart-Money-Score, dann Quality (absteigend).
      Fehlt divergenz_stufe (kein v51) -> Ersatzregel: SM-Score >= fallback_score_min
      UND Quality >= quality_min.
    kontrolle: max_ticker Ticker per Zufall (fester Seed) aus Status nein mit
      SM-Score kontrolle.smart_money_score und ohne Fondsaktivitaet (keine
      Kaeufer/Verkaeufer).
    benchmark: Liste benchmark (SPY, RSP), soweit in den Zeilen vorhanden.
    """
    b = cfg.get("decisions", {}).get("baskets", {})
    out: dict[str, list[dict]] = {}
    st = set(b.get("screener_statuses") or cfg.get("watchlist", {}).get(
        "statuses", ["Kandidat", "Beobachtung", "Beobachtung (nicht normalisiert)"]))
    out["screener"] = [r for r in rows if _status(r) in st]

    d = b.get("divergenz", {})
    d_status = str(d.get("status", "nein"))
    d_stufe = str(d.get("stufe", "stark"))
    q_min = float(d.get("quality_min", 60))
    d_max = int(d.get("max_ticker", 15))
    fb_score = int(d.get("fallback_score_min", 3))
    has_stufe = any("divergenz_stufe" in r for r in rows)   # Spalte im overlap-CSV vorhanden?
    div = []
    for r in rows:
        if _status(r) != d_status:
            continue
        q = _f(r, "quality")
        if q != q or q < q_min:
            continue
        score = _f(r, "smart_money_score")
        if has_stufe:
            stark = (r.get("divergenz_stufe") or "") == d_stufe
        else:
            stark = score == score and score >= fb_score    # Ersatzregel ohne divergenz_stufe
        if stark:
            div.append(r)
    div.sort(key=lambda r: (-_f(r, "smart_money_score"), -_f(r, "quality"), r["ticker"]))
    out["divergenz"] = div[:d_max]

    k = b.get("kontrolle", {})
    k_status = str(k.get("status", "nein"))
    k_score = int(k.get("smart_money_score", 1))
    k_max = int(k.get("max_ticker", 15))
    seed = int(k.get("seed", 42))
    bench = {str(t).upper() for t in (b.get("benchmark") or ["SPY", "RSP"])}
    cands = sorted(r["ticker"] for r in rows
                   if _status(r) == k_status
                   and _f(r, "smart_money_score") == k_score
                   and not str(r.get("fonds_kaeufer") or "").strip()
                   and not str(r.get("fonds_verkaeufer") or "").strip()
                   and r["ticker"] not in bench)   # Benchmark-Ticker nicht in die Kontrolle
    pick = random.Random(seed).sample(cands, min(k_max, len(cands))) if cands else []
    by_t = {r["ticker"]: r for r in rows}
    out["kontrolle"] = [by_t[t] for t in pick]

    out["benchmark"] = [by_t[t] for t in (b.get("benchmark") or ["SPY", "RSP"])
                        if t in by_t]
    return out


# --------------------------------------------------------------------------
# add
# --------------------------------------------------------------------------

def cmd_add(args) -> None:
    cfg = load_config()
    con = _connect(Path(args.db))
    ticker = args.ticker.upper()
    heute = _heute()
    ranking = getattr(args, "ranking", None)
    row = _row_aus_ranking(ranking, ticker) or _latest_valuation(con, ticker) or {}
    v = _vals(row)
    kurs = args.kurs if args.kurs else v["kurs"]
    if kurs != kurs or kurs <= 0:
        print("Fehler: kein Kurs bekannt -- --kurs angeben (oder --ranking mit Export-Spalten).",
              file=sys.stderr)
        sys.exit(1)
    gew = _default_gewichte(cfg)
    if args.gewichte:
        parts = [float(x) for x in args.gewichte.split("/")]
        if len(parts) != 3 or abs(sum(parts) - 1.0) > 1e-6:
            print("Fehler: --gewichte als bear/base/bull mit Summe 1 angeben (z. B. 25/50/25 -> 0.25/0.5/0.25).",
                  file=sys.stderr)
            sys.exit(1)
        gew = {"bear": parts[0], "base": parts[1], "bull": parts[2]}
    ew = erwartungswert_pct(v["iv_bear"], v["iv_base"], v["iv_bull"], kurs, gew)
    con.execute(
        """INSERT INTO decisions
           (ticker, datum, aktion, kurs, status_screener, iv_base, iv_bear, iv_bull,
            gew_bear, gew_base, gew_bull, erwartungswert_pct, horizont_monate,
            thesis, break_condition)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ticker, heute.isoformat(), args.aktion, kurs, v["status"],
         v["iv_base"], v["iv_bear"], v["iv_bull"],
         gew["bear"], gew["base"], gew["bull"], ew, args.horizont,
         args.thesis, args.break_condition))
    con.commit()
    print(f"Gespeichert: {ticker} {heute.isoformat()} aktion={args.aktion} kurs={kurs:.2f} "
          f"status={v['status'] or 'n/a'} iv_base={v['iv_base']:.2f} "
          f"EW={ew:.1f}% (p={ew/100:.2f}) horizont={args.horizont}M")


# --------------------------------------------------------------------------
# snapshot: monatlich automatisch fuer alle Kandidaten/Beobachtungstitel
# --------------------------------------------------------------------------

def _watchlist_aus_ranking(ranking_csv: str | None, cfg: dict) -> list[dict]:
    """Alle Rows mit Status in watchlist.statuses (Default Kandidat/Beobachtung...)."""
    statuses = set(cfg.get("watchlist", {}).get(
        "statuses", ["Kandidat", "Beobachtung", "Beobachtung (nicht normalisiert)"]))
    if not ranking_csv or not Path(ranking_csv).exists():
        return []
    out = []
    with open(ranking_csv, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() in statuses:
                out.append(row)
    return out


def cmd_snapshot(args) -> None:
    cfg = load_config()
    con = _connect(Path(args.db))
    korb_arg = getattr(args, "korb", None)
    if korb_arg:
        _snapshot_koerbe(args, cfg, con)
        return
    heute = _heute()
    # monatlich: nur einmal pro Kalendermonat snapshotten (Legacy-Zeilen korb='')
    cur = con.execute(
        "SELECT COUNT(*) AS n FROM decisions WHERE aktion='auto' AND korb='' "
        "AND strftime('%Y-%m', datum)=?",
        (heute.strftime("%Y-%m"),))
    if cur.fetchone()["n"] > 0:
        print(f"Snapshot fuer {heute.strftime('%Y-%m')} existiert bereits -- nichts zu tun.")
        return
    rows = _watchlist_aus_ranking(getattr(args, "ranking", None), cfg)
    if not rows:
        print("Keine Watchlist-Titel gefunden -- --ranking <csv> angeben.", file=sys.stderr)
        sys.exit(1)
    gew = _default_gewichte(cfg)
    n = 0
    for row in rows:
        ticker = (row.get("ticker") or "").strip().upper()
        v = _vals(row)
        kurs = v["kurs"]
        if kurs != kurs or kurs <= 0:
            continue
        ew = erwartungswert_pct(v["iv_bear"], v["iv_base"], v["iv_bull"], kurs, gew)
        con.execute(
            """INSERT INTO decisions
               (ticker, datum, aktion, kurs, status_screener, iv_base, iv_bear, iv_bull,
                gew_bear, gew_base, gew_bull, erwartungswert_pct, horizont_monate,
                thesis, break_condition)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, heute.isoformat(), "auto", kurs, v["status"],
             v["iv_base"], v["iv_bear"], v["iv_bull"],
             gew["bear"], gew["base"], gew["bull"], ew, 12,
             "auto-Snapshot (Kandidat/Beobachtung)", ""))
        n += 1
    con.commit()
    print(f"Snapshot {heute.isoformat()}: {n} Auto-Eintraege (aktion=auto, Horizont 12M).")


def _max_filing_datum(con: sqlite3.Connection, ticker: str) -> str:
    """v56 Look-ahead: juengstes filing_datum der 13F-Positionen eines Tickers
    (die verwendeten Smart-Money-Signale); '' wenn keine DB/Tabelle/Positionen."""
    tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "smart_money_positions" not in tabs:
        return ""
    r = con.execute(
        "SELECT MAX(filing_datum) FROM smart_money_positions WHERE ticker=?",
        (ticker.upper(),)).fetchone()
    return (r[0] or "")[:10] if r and r[0] else ""


def _kurs_am(con: sqlite3.Connection, ticker: str, wann: date) -> tuple[float | None, str]:
    """v56: Kurs am Snapshot-Tag `wann` aus prices (exakt, sonst naechster frueherer
    Tag) -> (kurs, kurs_datum); (None, "") wenn keine prices-Tabelle/Daten."""
    tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "prices" not in tabs:
        return None, ""
    r = con.execute(
        "SELECT kurs, datum FROM prices WHERE ticker=? AND datum<=? ORDER BY datum DESC LIMIT 1",
        (ticker.upper(), wann.isoformat())).fetchone()
    if r and r[0] is not None:
        return float(r[0]), (r[1] or "")[:10]
    return None, ""


def _start_datum(datum: str, ticker: str, con: sqlite3.Connection) -> str:
    """v56: Startdatum der Rendite je Zeile = max(Snapshot-Datum, juengstes
    filing_datum der verwendeten 13F-Signale des Tickers)."""
    fd = _max_filing_datum(con, ticker)
    return max(datum, fd) if fd else datum


def _snapshot_koerbe(args, cfg: dict, con: sqlite3.Connection) -> None:
    """v52/v56: snapshot --korb all|screener|divergenz|kontrolle|benchmark [--datum ...]
    (Default --datum = heute = Tag des Snapshots). Schreibt je Ticker und Korb eine
    Zeile (aktion=auto). Idempotent je (ticker, korb, datum): vorhandene Zeilen werden
    uebersprungen, nie ueberschrieben.

    v56 Look-ahead: Startkurs = Kurs am Snapshot-Tag (aus prices, Datum <= --datum;
    Fallback ranking-Kurs). Liegt --datum vor dem filing_datum eines verwendeten
    13F-Signals, bricht der Lauf VOR dem Schreiben ab ("Datum liegt vor
    Veroeffentlichung der Signale (Look-ahead)"). start_datum je Zeile =
    max(--datum, juengstes filing_datum)."""
    korb_arg = args.korb
    datum = (_parse_date(args.datum).isoformat() if getattr(args, "datum", None)
             else _heute().isoformat())
    datum_d = _parse_date(datum)
    overlap = getattr(args, "overlap", None) or (
        "smart_money_overlap.csv" if Path("smart_money_overlap.csv").exists() else None)
    live = getattr(args, "live", None) or (
        "watchlist_live.csv" if Path("watchlist_live.csv").exists() else None)
    rows = _korb_rows(getattr(args, "ranking", None), overlap, live)
    if not rows:
        print("Keine Ranking-Zeilen gefunden -- --ranking <csv> angeben.", file=sys.stderr)
        sys.exit(1)
    koerbe_map = koerbe(cfg, rows)
    namen = list(KORB_NAMEN) if korb_arg == "all" else [korb_arg]
    # Look-ahead-Guard: --datum vor filing_datum verwendeter Signale -> Abbruch vor dem Schreiben
    offenders = []
    for name in namen:
        for r in koerbe_map.get(name, []):
            fd = _max_filing_datum(con, r["ticker"])
            if fd and datum_d < _parse_date(fd):
                offenders.append(f"{r['ticker']} (filing_datum {fd} > datum {datum})")
    if offenders:
        raise SystemExit(
            "Datum liegt vor Veröffentlichung der Signale (Look-ahead): "
            + "; ".join(offenders[:10]) + (" ..." if len(offenders) > 10 else "")
            + f" -- --datum muss >= filing_datum der verwendeten 13F liegen (Default: heute={_heute().isoformat()}); "
              "nichts geschrieben.")
    gew = _default_gewichte(cfg)
    total_w = total_s = 0
    for name in namen:
        w = s = 0
        for r in koerbe_map.get(name, []):
            t = r["ticker"]
            # Startkurs = Kurs am Snapshot-Tag (prices), Fallback ranking-Kurs
            kurs, kurs_datum = _kurs_am(con, t, datum_d)
            if kurs is None:
                kurs = _f(r, "kurs")
                kurs_datum = str(r.get("kurs_datum") or "")
            if kurs != kurs or kurs <= 0:
                continue  # kein Kurs -> keine Zeile (kurs ist NOT NULL)
            exists = con.execute(
                "SELECT 1 FROM decisions WHERE ticker=? AND korb=? AND datum=?",
                (t, name, datum)).fetchone()
            if exists:
                s += 1
                continue  # idempotent: geschriebene Zeilen nie ueberschreiben
            start_datum = _start_datum(datum, t, con)
            ew = erwartungswert_pct(_f(r, "iv_bear"), _f(r, "iv_base"), _f(r, "iv_bull"),
                                    kurs, gew)
            con.execute(
                """INSERT INTO decisions
                   (ticker, datum, aktion, korb, kurs, kurs_datum, status_screener, quality,
                    mos_min, mos_robust, iv_base, iv_bear, iv_bull, kaufzone_kurs,
                    timing_status, smart_money_score, urteil, divergenz_stufe,
                    fonds_kaeufer, fonds_verkaeufer,
                    gew_bear, gew_base, gew_bull, erwartungswert_pct, wahrscheinlichkeit_p,
                    start_datum, horizont_monate, thesis, break_condition)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t, datum, "auto", name, kurs, kurs_datum, _status(r),
                 _f(r, "quality"), _f(r, "mos_min", "mos_rule", "margin_of_safety"),
                 _f(r, "mos_robust"), _f(r, "iv_base"), _f(r, "iv_bear"), _f(r, "iv_bull"),
                 _f(r, "kaufzone_kurs"), str(r.get("timing_status") or ""),
                 _i(r, "smart_money_score"),
                 str(r.get("smart_money_urteil") or r.get("urteil") or ""),
                 str(r.get("divergenz_stufe") or ""),
                 str(r.get("fonds_kaeufer") or ""), str(r.get("fonds_verkaeufer") or ""),
                 gew["bear"], gew["base"], gew["bull"], ew,
                 (ew / 100.0 if ew == ew else None),
                 start_datum, 12, f"auto-Snapshot Korb {name}", ""))
            w += 1
        print(f"Korb {name}: {w} geschrieben, {s} bereits vorhanden (datum={datum}).")
        total_w += w
        total_s += s
    con.commit()
    print(f"Snapshot {datum} --korb {korb_arg}: {total_w} Zeilen geschrieben, "
          f"{total_s} uebersprungen (idempotent je ticker/korb/datum). Startkurs = Kurs am "
          f"Snapshot-Tag; start_datum = max(datum, juengstes filing_datum).")


# --------------------------------------------------------------------------
# review: Kurs nachfuellen, Rendite + Brier-Score, Kalibrierung
# --------------------------------------------------------------------------

def _kurs_nach(con: sqlite3.Connection, ticker: str, wann: date) -> float:
    """Kurs zum Datum `wann` aus prices (exakt, sonst naechster frueherer Tag)."""
    cur = con.execute(
        "SELECT kurs FROM prices WHERE ticker=? AND datum<=? ORDER BY datum DESC LIMIT 1",
        (ticker, wann.isoformat()))
    r = cur.fetchone()
    return float(r["kurs"]) if r else float("nan")


# --------------------------------------------------------------------------
# v52 Korb-Review: Kennzahlen je Korb + Horizont, Brier, decisions_review.csv
# --------------------------------------------------------------------------

def _korb_review(con: sqlite3.Connection, cfg: dict, heute: date,
                 out_path: Path) -> list[dict]:
    """v52: Auswertung der Korb-Snapshots (korb <> ''). Pro (korb, snapshot-datum,
    horizont) aus prices: n, Mittelwert, Median, Anteil mit Ueberrendite vs. SPY,
    Median-Ueberrendite vs. SPY, Differenz des Medians zu kontrolle, Brier-Mittel
    (nur 12M, aus ereignis/brier der Zeilen). n < review_min_eintraege ->
    "unkalibriert, n=...". Schreibt decisions_review.csv (UTF-8)."""
    dcfg = cfg.get("decisions", {})
    horizonte = [int(h) for h in dcfg.get("horizonte_monate", [3, 6, 12, 24])]
    min_n = int(cfg.get("watchlist", {}).get("review_min_eintraege", 30))
    rows = [dict(r) for r in con.execute(
        "SELECT * FROM decisions WHERE korb <> '' ORDER BY datum, korb, ticker")]
    if not rows:
        return []
    spy0 = {r["datum"]: r["kurs"] for r in rows
            if r["korb"] == "benchmark" and r["ticker"] == "SPY"}

    def _spy_rend(datum: str, h: int) -> float:
        k0 = spy0.get(datum)
        if not k0 or k0 <= 0:
            return float("nan")
        kn = _kurs_nach(con, "SPY", _parse_date(datum) + timedelta(days=30 * h))
        return (kn / k0 - 1.0) if kn == kn and kn > 0 else float("nan")

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        for h in horizonte:
            groups.setdefault((r["korb"], r["datum"], h), []).append(r)

    out = []
    for (korb, datum, h), grp in sorted(groups.items()):
        rends: list[float] = []
        briers: list[float] = []
        for r in grp:
            # v56: Start je Zeile = max(datum, juengstes filing_datum), Startkurs = Kurs daran
            sd = ((r["start_datum"] or "")[:10]) or _start_datum(r["datum"], r["ticker"], con)
            start_d = _parse_date(sd)
            start_kurs = r["kurs"]
            if start_d > _parse_date(r["datum"]):
                k_am, _kd = _kurs_am(con, r["ticker"], start_d)
                if k_am is not None:
                    start_kurs = k_am
            kn = _kurs_nach(con, r["ticker"], start_d + timedelta(days=30 * h))
            if kn == kn and kn > 0 and start_kurs and start_kurs > 0:
                rends.append(kn / start_kurs - 1.0)
            if h == 12 and r["brier"] is not None:
                briers.append(float(r["brier"]))
        n = len(rends)
        if n == 0:
            continue
        srend = _spy_rend(datum, h)
        ueber = [x - srend for x in rends if srend == srend]
        out.append({
            "korb": korb, "datum": datum, "horizont_monate": h, "n": n,
            "mittelwert_pct": (sum(rends) / n) * 100.0,
            "median_pct": statistics.median(rends) * 100.0,
            "anteil_ueber_spy": (sum(1 for u in ueber if u > 0) / len(ueber)) if ueber else float("nan"),
            "median_ueberrendite_spy_pct": (statistics.median(ueber) * 100.0) if ueber else float("nan"),
            "brier_mean": (sum(briers) / len(briers)) if briers else None,
            "kalibrierung": "ja" if n >= min_n else f"unkalibriert, n={n}",
        })
    # Differenz des Medians zu kontrolle (gleiches snapshot-datum + horizont)
    med = {(o["korb"], o["datum"], o["horizont_monate"]): o["median_pct"] for o in out}
    for o in out:
        k_med = med.get(("kontrolle", o["datum"], o["horizont_monate"]))
        o["median_diff_kontrolle_pct"] = (o["median_pct"] - k_med) if k_med is not None else None

    cols = ["korb", "datum", "horizont_monate", "n", "mittelwert_pct", "median_pct",
            "anteil_ueber_spy", "median_ueberrendite_spy_pct", "median_diff_kontrolle_pct",
            "brier_mean", "kalibrierung"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for o in out:
            w.writerow([("" if o.get(c) is None else
                         (f"{o[c]:.4f}" if isinstance(o.get(c), float) else o.get(c)))
                        for c in cols])

    print(f"\n=== Korb-Review {heute.isoformat()} (Horizonte {horizonte}) -> {out_path} ===")
    print(f"{'korb':<11} {'datum':<10} {'hor':>4} {'n':>4} {'mittel%':>8} {'median%':>8} "
          f"{'>SPY':>6} {'medUeSPY%':>9} {'dKontrolle%':>11} {'brier':>7}  kalibrierung")
    for o in out:
        def _p(x, spec=">8.1f"):
            return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, spec)
        print(f"{o['korb']:<11} {o['datum']:<10} {o['horizont_monate']:>4} {o['n']:>4} "
              f"{_p(o['mittelwert_pct'])} {_p(o['median_pct'])} "
              f"{_p(o['anteil_ueber_spy'], '>6.2f')} {_p(o['median_ueberrendite_spy_pct'], '>9.1f')} "
              f"{_p(o['median_diff_kontrolle_pct'], '>11.1f')} {_p(o['brier_mean'], '>7.4f')}  "
              f"{o['kalibrierung']}")
    return out


def cmd_review(args) -> None:
    cfg = load_config()
    wcfg = cfg.get("watchlist", {})
    con = _connect(Path(args.db))
    heute = _parse_date(args.heute) if args.heute else _heute()
    ereignis_schwelle = float(wcfg.get("ereignis_rendite_pct", 0.10))
    min_n = int(wcfg.get("review_min_eintraege", 30))

    cur = con.execute(
        """SELECT * FROM decisions
           WHERE kurs_nachher IS NULL AND reviewed_at IS NULL
           ORDER BY datum""")
    rows = cur.fetchall()
    n_reviewed = 0
    for r in rows:
        d0 = _parse_date(r["datum"])
        # v56 Look-ahead: Startdatum der Rendite = max(Snapshot-Datum, juengstes
        # filing_datum der verwendeten 13F); Startkurs = Kurs an diesem Tag
        sd = ((r["start_datum"] or "")[:10]) or _start_datum(r["datum"], r["ticker"], con)
        start_d = _parse_date(sd)
        start_kurs = r["kurs"]
        if start_d > d0:
            k_am, _kd = _kurs_am(con, r["ticker"], start_d)
            if k_am is not None:
                start_kurs = k_am
        wann = start_d + timedelta(days=30 * r["horizont_monate"])  # 12/24 Monate
        if wann > heute:
            continue  # Horizont noch nicht abgelaufen
        kurs_nach = _kurs_nach(con, r["ticker"], wann)
        if kurs_nach != kurs_nach or kurs_nach <= 0:
            continue  # kein Kurs verfuegbar -> ueberspringen
        rendite = kurs_nach / start_kurs - 1.0
        # Ereignis E nur fuer 12-Monats-Horizont definiert (Rendite nach 12 M >= +10 %)
        if r["horizont_monate"] == 12:
            e = 1 if rendite >= ereignis_schwelle else 0
            p = (r["erwartungswert_pct"] / 100.0) if r["erwartungswert_pct"] is not None else float("nan")
            brier = (p - e) ** 2 if p == p else None
        else:
            e, brier = None, None
        con.execute(
            """UPDATE decisions SET kurs_nachher=?, rendite_pct=?, ereignis_e=?,
               brier=?, reviewed_at=? WHERE id=?""",
            (kurs_nach, rendite * 100.0, e, brier, heute.isoformat(), r["id"]))
        n_reviewed += 1
    con.commit()

    # Kalibrierung: Brier-Score ueber alle 12M-Eintraege mit Ereignis
    cur = con.execute(
        "SELECT COUNT(*) AS n, AVG(brier) AS brier FROM decisions "
        "WHERE horizont_monate=12 AND ereignis_e IS NOT NULL AND brier IS NOT NULL")
    agg = cur.fetchone()
    n = agg["n"] or 0
    print(f"Review {heute.isoformat()}: {n_reviewed} Eintraege nachgefuellt.")
    if n < min_n:
        print(f"Kalibrierung: unkalibriert, n={n} (< {min_n}) -- keine Kennzahl.")
    else:
        print(f"Kalibrierung: n={n}, mittlerer Brier-Score = {agg['brier']:.4f} "
              f"(0 = perfekt, 0.25 = Referenz Zufall).")
    # v52: Korb-Auswertung (decisions_review.csv)
    out_csv = getattr(args, "out", None) or cfg.get("decisions", {}).get(
        "review_csv", "decisions_review.csv")
    _korb_review(con, cfg, heute, Path(out_csv))
    cmd_list(args)


def cmd_list(args) -> None:
    con = _connect(Path(args.db))
    q = "SELECT * FROM decisions"
    params: tuple = ()
    if args.ticker:
        q += " WHERE ticker=?"
        params = (args.ticker.upper(),)
    q += " ORDER BY datum DESC, id DESC"
    rows = con.execute(q, params).fetchall()
    print(f"\n=== decisions ({len(rows)} Eintraege) ===")
    header = (f"{'id':>4} {'ticker':<8} {'datum':<10} {'aktion':<8} {'kurs':>8} "
              f"{'status':<22} {'iv_base':>8} {'EW%':>6} {'hor':>4} "
              f"{'kurs_nach':>9} {'rend%':>7} {'E':>2} {'brier':>6}  thesis")
    print(header)
    print("-" * len(header))
    for r in rows:
        def _f(x, spec):
            return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else format(x, spec)
        print(f"{r['id']:>4} {r['ticker']:<8} {r['datum']:<10} {r['aktion']:<8} "
              f"{_f(r['kurs'], '>8.2f')} {(r['status_screener'] or '')[:22]:<22} "
              f"{_f(r['iv_base'], '>8.2f')} {_f(r['erwartungswert_pct'], '>6.1f')} "
              f"{r['horizont_monate']:>4} {_f(r['kurs_nachher'], '>9.2f')} "
              f"{_f(r['rendite_pct'], '>7.1f')} "
              f"{('n/a' if r['ereignis_e'] is None else str(r['ereignis_e'])):>2} "
              f"{_f(r['brier'], '>6.3f')}  {(r['thesis'] or '')[:40]}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Entscheidungslog (value_tracker.db)")
    p.add_argument("--db", default=str(DB_PATH), help="SQLite-Datei (Default: value_tracker.db)")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="DB + Schema anlegen")
    p_init.set_defaults(func=cmd_init)

    p_add = sub.add_parser("add", help="Entscheidung protokollieren")
    p_add.add_argument("ticker")
    p_add.add_argument("--aktion", required=True, choices=AKTIONEN)
    p_add.add_argument("--thesis", default="", help="Begruendung (why)")
    p_add.add_argument("--break", dest="break_condition", default="",
                       help="Invalidierungsbedingung (break)")
    p_add.add_argument("--kurs", type=float, default=None, help="Kurs (Default: aus ranking/valuations)")
    p_add.add_argument("--horizont", type=int, default=12, choices=HORIZONTS)
    p_add.add_argument("--gewichte", default=None, help="bear/base/bull, Summe 1 (z. B. 0.25/0.5/0.25)")
    p_add.add_argument("--ranking", default=None, help="ranking CSV (v41 Export-Spalten)")
    p_add.set_defaults(func=cmd_add)

    p_snap = sub.add_parser("snapshot", help="monatlicher Auto-Snapshot (ohne --korb) bzw. "
                                              "Korb-Snapshot (v52: --korb all|screener|...)")
    p_snap.add_argument("--ranking", default=None, help="ranking CSV")
    p_snap.add_argument("--korb", default=None,
                        choices=list(KORB_NAMEN) + ["all"],
                        help="v52: Vergleichkorb-Snapshot (all = alle Koerbe)")
    p_snap.add_argument("--datum", default=None,
                        help="v52: Snapshot-Datum YYYY-MM-DD (Default: heute; z. B. Datum des 13F-Quartals)")
    p_snap.add_argument("--overlap", default=None,
                        help="v52: smart_money_overlap.csv (Default: Datei falls vorhanden)")
    p_snap.add_argument("--live", default=None,
                        help="v52: watchlist_live.csv fuer kaufzone_kurs/timing_status (Default: Datei falls vorhanden)")
    p_snap.set_defaults(func=cmd_snapshot)

    p_rev = sub.add_parser("review", help="Kurs nachfuellen, Rendite + Brier, Kalibrierung, "
                                          "v52: Korb-Review -> decisions_review.csv")
    p_rev.add_argument("--ranking", default=None, help="ranking CSV (Fallback Kurs)")
    p_rev.add_argument("--heute", default=None, help="Referenzdatum YYYY-MM-DD (Default: heute)")
    p_rev.add_argument("--ticker", default=None, help="nur ein Ticker listen (nur list)")
    p_rev.add_argument("--out", default=None,
                       help="v52: Ausgabe-CSV der Korb-Auswertung (Default: config decisions.review_csv)")
    p_rev.set_defaults(func=cmd_review)

    p_list = sub.add_parser("list", help="alle Entscheidungen anzeigen")
    p_list.add_argument("--ticker", default=None)
    p_list.set_defaults(func=cmd_list)
    return p.parse_args(argv)


def cmd_init(args) -> None:
    con = init_db(Path(args.db))
    print(f"DB angelegt: {args.db} (Schema aus schema.sql)")
    con.close()


def main() -> None:
    args = _parse_args(sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()
