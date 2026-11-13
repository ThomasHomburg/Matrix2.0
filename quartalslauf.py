"""quartalslauf.py -- Ein-Kommando-Quartalslauf (proposal-free, ASCII-only).

Macht die Runbook-Sequenz aus README ("Regelbetrieb") als einen Befehl ausfuehrbar:
    fetch -> run_ranking -> overlap -> live_update -> snapshot -> review

Jedes bestehende Skript wird als subprocess aufgerufen -- keine duplizierte Logik.
Jeder Schritt ist eine eigene Funktion und gibt (name, ok, zusammenfassung) zurueck;
Abbruch beim ersten Fehler, bisherige Ergebnisse bleiben im Log.

Frischetest: fetch muss mindestens eine Periode bringen, die neuer ist als die
juengste Periode in der DB (SELECT MAX(periode) vorher/nachher). Keine neue Periode
-> sauberer Abbruch mit Rueckgabecode 2 und Hinweis "Filings vermutlich noch nicht
verfuegbar".

CLI:
    python quartalslauf.py run [--dry-run] [--quartal YYYY-QN]

--dry-run gibt nur die geplanten Schritte/Kommandos aus und aendert nichts.
Logdatei: logs/quartalslauf_YYYY-MM-DD.log -- jedes Kommando mit Datum-Uhrzeit,
Dauer und Rueckgabecode.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from config import load_config

logger = logging.getLogger("quartalslauf")


class QuartalslaufError(Exception):
    """Abbruch des Quartalslaufs (fehlgeschlagener Schritt)."""


# --------------------------------------------------------------------------
# Helfer
# --------------------------------------------------------------------------

def _qcfg(cfg: dict) -> dict:
    return cfg.get("quartalslauf", {})


def _db_path(cfg: dict) -> Path:
    return Path(_qcfg(cfg).get("db", "value_tracker.db"))


def _max_periode(db_path: Path) -> str:
    """Juengste 13F-Periode in der DB ('' wenn DB/Tabelle fehlt)."""
    if not db_path.exists():
        return ""
    con = sqlite3.connect(db_path)
    try:
        tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "smart_money_positions" not in tabs:
            return ""
        r = con.execute("SELECT MAX(periode) FROM smart_money_positions").fetchone()
        return (r[0] or "") if r else ""
    finally:
        con.close()


def _run_cmd(cmd: list[str]) -> tuple[int, str]:
    """Subprocess ausfuehren, (rc, output) zurueck. Dauer+rc kommen ins Log (run())."""
    cp = subprocess.run(cmd, capture_output=True, text=True)
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


def _tail(text: str, n: int = 400) -> str:
    return text.strip().splitlines()[-1] if text.strip() else ""


# --------------------------------------------------------------------------
# Schritte -- jede Funktion gibt (name, ok, zusammenfassung) zurueck
# --------------------------------------------------------------------------

def step_fetch(cfg: dict, quartal: str | None = None) -> tuple[str, bool, str]:
    """Schritt 1: smart_money.py fetch. Frischetest: neue Periode in der DB?"""
    db = _db_path(cfg)
    before = _max_periode(db)
    cmd = [sys.executable, "smart_money.py", "fetch"]
    logger.info("%s cmd=%s", datetime.now().isoformat(timespec="seconds"), " ".join(cmd))
    t0 = time.monotonic()
    rc, out = _run_cmd(cmd)
    logger.info("cmd=%s dauer=%.1fs rc=%s", " ".join(cmd), time.monotonic() - t0, rc)
    after = _max_periode(db)
    if rc != 0:
        return "fetch", False, f"fetch rc={rc}: {_tail(out, 300)}"
    if not after or after <= before:
        return ("fetch", False,
                f"keine neue Periode (vorher={before or '-'}, nachher={after or '-'}) -- "
                f"Filings vermutlich noch nicht verfuegbar")
    if quartal and after < quartal:
        return ("fetch", False,
                f"neue Periode {after} < --quartal {quartal} -- Filings vermutlich noch nicht "
                f"verfuegbar")
    return "fetch", True, f"neue Periode {after} (vorher {before or '-'})"


def step_ranking(cfg: dict, out_csv: str) -> tuple[str, bool, str]:
    """Schritt 2: run_ranking.py --tickers <config> --no-resume --out <out_csv>."""
    tickers_csv = _qcfg(cfg).get("tickers_csv", "tickers_sp500.csv")
    cmd = [sys.executable, "run_ranking.py", "--tickers", tickers_csv,
           "--no-resume", "--out", out_csv]
    logger.info("%s cmd=%s", datetime.now().isoformat(timespec="seconds"), " ".join(cmd))
    t0 = time.monotonic()
    rc, out = _run_cmd(cmd)
    logger.info("cmd=%s dauer=%.1fs rc=%s", " ".join(cmd), time.monotonic() - t0, rc)
    return "ranking", rc == 0, (f"rc={rc} -> {out_csv}: {_tail(out, 300)}" if rc == 0
                                else f"rc={rc}: {_tail(out, 300)}")


def step_overlap(cfg: dict, ranking_csv: str) -> tuple[str, bool, str]:
    """Schritt 3: smart_money.py overlap --ranking <ranking> --watch <config> --force."""
    watch_csv = _qcfg(cfg).get("watch_csv", "ticker_watch_v40.csv")
    cmd = [sys.executable, "smart_money.py", "overlap", "--ranking", ranking_csv,
           "--watch", watch_csv, "--force"]   # Vollstaendigkeits-Schutz bewusst uebersteuert
    logger.info("%s cmd=%s", datetime.now().isoformat(timespec="seconds"), " ".join(cmd))
    t0 = time.monotonic()
    rc, out = _run_cmd(cmd)
    logger.info("cmd=%s dauer=%.1fs rc=%s", " ".join(cmd), time.monotonic() - t0, rc)
    return "overlap", rc == 0, (f"rc={rc}: {_tail(out, 300)}")


def step_live_update(cfg: dict, ranking_csv: str) -> tuple[str, bool, str]:
    """Schritt 4: live_update.py --ranking <ranking> --watch <config>."""
    watch_csv = _qcfg(cfg).get("watch_csv", "ticker_watch_v40.csv")
    cmd = [sys.executable, "live_update.py", "--ranking", ranking_csv, "--watch", watch_csv]
    logger.info("%s cmd=%s", datetime.now().isoformat(timespec="seconds"), " ".join(cmd))
    t0 = time.monotonic()
    rc, out = _run_cmd(cmd)
    logger.info("cmd=%s dauer=%.1fs rc=%s", " ".join(cmd), time.monotonic() - t0, rc)
    return "live_update", rc == 0, (f"rc={rc}: {_tail(out, 300)}")


def step_snapshot(cfg: dict, datum: str) -> tuple[str, bool, str]:
    """Schritt 5: decisions.py snapshot --korb all --datum <datum>."""
    cmd = [sys.executable, "decisions.py", "snapshot", "--korb", "all", "--datum", datum]
    logger.info("%s cmd=%s", datetime.now().isoformat(timespec="seconds"), " ".join(cmd))
    t0 = time.monotonic()
    rc, out = _run_cmd(cmd)
    logger.info("cmd=%s dauer=%.1fs rc=%s", " ".join(cmd), time.monotonic() - t0, rc)
    return "snapshot", rc == 0, (f"rc={rc}: {_tail(out, 300)}")


def step_review(cfg: dict, datum: str) -> tuple[str, bool, str]:
    """Schritt 6: decisions.py review --heute <datum>."""
    cmd = [sys.executable, "decisions.py", "review", "--heute", datum]
    logger.info("%s cmd=%s", datetime.now().isoformat(timespec="seconds"), " ".join(cmd))
    t0 = time.monotonic()
    rc, out = _run_cmd(cmd)
    logger.info("cmd=%s dauer=%.1fs rc=%s", " ".join(cmd), time.monotonic() - t0, rc)
    return "review", rc == 0, (f"rc={rc}: {_tail(out, 300)}")


# --------------------------------------------------------------------------
# Klartext-Zusammenfassung nach snapshot+review
# --------------------------------------------------------------------------

def zusammenfassung(cfg: dict, datum: str) -> str:
    """Liest die erzeugten Artefakte (review-CSV, overlap-CSV, DB) und gibt eine
    Klartext-Zusammenfassung aus: Top-Signale, neue/verkaufene Positionen,
    offene Warnungen, Score-Verteilung."""
    q = _qcfg(cfg)
    db = _db_path(cfg)
    lines = [f"=== Quartalslauf-Zusammenfassung {datum} ==="]
    # review-Report (Korb-Kennzahlen)
    review_csv = cfg.get("decisions", {}).get("review_csv", "decisions_review.csv")
    if Path(review_csv).exists():
        with open(review_csv, newline="", encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if r.get("korb")]
        lines.append("-- Koerbe (review) --")
        for r in rows[:8]:
            lines.append(f"  {r.get('korb',''):<12} n={r.get('n','?')} median={r.get('median_pct','?')} "
                         f">SPY={r.get('anteil_ueber_spy_pct','?')}")
    # Score-Verteilung + Top-Signale aus overlap-CSV
    overlap = "smart_money_overlap.csv"
    if Path(overlap).exists():
        with open(overlap, newline="", encoding="utf-8") as f:
            orows = [r for r in csv.DictReader(f) if r.get("ticker")]
        from collections import Counter
        dist = Counter((r.get("smart_money_score") or "n/a") for r in orows)
        lines.append("-- Smart-Money-Score-Verteilung --")
        for score in sorted(dist, key=lambda s: -dist[s]):
            lines.append(f"  score {score}: {dist[score]}")
        top = sorted((r for r in orows if r.get("smart_money_score") not in (None, "", "n/a")),
                     key=lambda r: -float(r["smart_money_score"]))[:10]
        lines.append("-- Top-Signale (nach SM-Score) --")
        for r in top:
            lines.append(f"  {r['ticker']:<8} score={r['smart_money_score']} "
                         f"urteil={r.get('smart_money_urteil') or r.get('urteil') or ''} "
                         f"kaeufer={r.get('fonds_kaeufer') or '-'}")
    # neue/verkaufene Positionen + offene Warnungen aus der DB
    if db.exists():
        con = sqlite3.connect(db)
        try:
            tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "smart_money_positions" in tabs:
                newest = _max_periode(db)
                neu = con.execute(
                    "SELECT ticker, fonds, anteil_portfolio FROM smart_money_positions "
                    "WHERE periode=? AND aktion='neu' ORDER BY anteil_portfolio DESC LIMIT 10",
                    (newest,)).fetchall()
                verk = con.execute(
                    "SELECT ticker, fonds FROM smart_money_positions "
                    "WHERE periode=? AND aktion='verkauft' LIMIT 10", (newest,)).fetchall()
                lines.append(f"-- Neue/verkaufene Positionen ({newest}) --")
                for t, f_, a in neu:
                    lines.append(f"  NEU       {t:<8} {f_} anteil={a}")
                for t, f_ in verk:
                    lines.append(f"  VERKAUFT  {t:<8} {f_}")
                # Fonds-Gaps: Fonds ohne Position in neuester Periode
                all_f = {r[0] for r in con.execute("SELECT DISTINCT fonds FROM smart_money_positions")}
                have = {r[0] for r in con.execute(
                    "SELECT DISTINCT fonds FROM smart_money_positions WHERE periode=?", (newest,))}
                gaps = sorted(all_f - have)
                if gaps:
                    lines.append("-- Offene Warnungen: Fonds ohne 13F in neuester Periode --")
                    for g in gaps:
                        lines.append(f"  Luecke: {g}")
                # ungemappte Ticker (cusip_map ohne ticker)
                if "cusip_map" in tabs:
                    unm = con.execute("SELECT COUNT(*) FROM cusip_map WHERE ticker IS NULL OR ticker=''"
                                      ).fetchone()[0]
                    if unm:
                        lines.append(f"-- Offene Warnungen: {unm} CUSIPs ungemappt --")
        finally:
            con.close()
    return "\n".join(lines)


# --------------------------------------------------------------------------
# run()
# --------------------------------------------------------------------------

def run(dry_run: bool = False, quartal: str | None = None, heute: date | None = None) -> int:
    """Quartalslauf ausfuehren. Rueckgabecode: 0 ok, 1 Schrittfehler, 2 keine frischen
    Filings. dry_run gibt nur die geplanten Schritte aus."""
    cfg = load_config()
    q = _qcfg(cfg)
    heute = heute or date.today()
    datum = heute.isoformat()
    version = str(q.get("version", "62"))
    out_csv = str(q.get("ranking_out", "ranking_sp500_v{version}_{datum}.csv")).format(
        version=version, datum=datum)
    # Logdatei im Logs-Verzeichnis
    logs_dir = Path(q.get("logs_dir", "logs"))
    logs_dir.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(logs_dir / f"quartalslauf_{datum}.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(fh)
    logger.setLevel(logging.INFO)

    steps = [
        ("fetch", lambda: step_fetch(cfg, quartal)),
        ("ranking", lambda: step_ranking(cfg, out_csv)),
        ("overlap", lambda: step_overlap(cfg, out_csv)),
        ("live_update", lambda: step_live_update(cfg, out_csv)),
        ("snapshot", lambda: step_snapshot(cfg, datum)),
        ("review", lambda: step_review(cfg, datum)),
    ]
    print(f"Quartalslauf {datum} (version v{version}, ranking-out {out_csv})")
    if dry_run:
        print("--dry-run: geplante Schritte (aendert nichts):")
        for name, _fn in steps:
            print(f"  {name}")
        print(f"  ranking_out={out_csv} tickers={q.get('tickers_csv')} watch={q.get('watch_csv')}")
        return 0

    results: list[tuple[str, bool, str]] = []
    for name, fn in steps:
        nm, ok, summary = fn()
        results.append((nm, ok, summary))
        print(f"[{'OK' if ok else 'FEHLER'}] {nm}: {summary}")
        if not ok:
            logger.error("Abbruch nach Schritt %s: %s", nm, summary)
            if nm == "fetch" and "noch nicht verfuegbar" in summary:
                return 2
            raise QuartalslaufError(f"Schritt {nm} fehlgeschlagen: {summary}")
    print(zusammenfassung(cfg, datum))
    return 0


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Ein-Kommando-Quartalslauf (fetch -> ranking -> "
                                            "overlap -> live_update -> snapshot -> review)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("run", help="Quartalslauf ausfuehren")
    pr.add_argument("--dry-run", action="store_true", help="nur geplante Schritte ausgeben")
    pr.add_argument("--quartal", default=None, help="erwartetes 13F-Quartal YYYY-QN (Frischetest)")
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    if args.cmd == "run":
        try:
            rc = run(dry_run=args.dry_run, quartal=args.quartal)
        except QuartalslaufError as exc:
            print(f"Quartalslauf abgebrochen: {exc}", file=sys.stderr)
            sys.exit(1)
        sys.exit(rc)


if __name__ == "__main__":
    main()
