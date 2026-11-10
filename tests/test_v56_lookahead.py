r"""
(v56) decisions.py Look-ahead-Pruefung (kein Netz):

- snapshot --datum: Startkurs = Kurs am Snapshot-Tag (aus prices, datum <= --datum),
  kurs_datum = Kursdatum; start_datum = max(--datum, juengstes filing_datum der
  verwendeten 13F). --datum vor filing_datum -> Abbruch "... (Look-ahead)", nichts
  geschrieben. Default --datum = heute. Idempotenz bleibt.
- review: Startpunkt der Rendite je Zeile = start_datum (max(Snapshot-Datum,
  filing_datum)); Startkurs = Kurs an start_datum; Rendite gegen diesen Start.
- Migration: Alt-DB ohne Spalte start_datum -> ALTER in _migrate_decisions.
"""

import argparse
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

import decisions

HEUTE = date(2026, 11, 9)


def _ranking(tmp_path):
    p = tmp_path / "ranking.csv"
    p.write_text(
        "ticker,status_all,quality,mos_rule,kurs,kurs_datum,iv_bear,iv_base,iv_bull\n"
        "AAA,Kandidat,70,0.4,100.0,2026-11-01,80.0,150.0,200.0\n"
        "SPY,nein,55,0.1,100.0,2026-11-01,90.0,110.0,130.0\n"
        "RSP,nein,55,0.1,100.0,2026-11-01,90.0,110.0,130.0\n", encoding="utf-8")
    return str(p)


def _db(tmp_path, with_sm=True):
    db = tmp_path / "d.db"
    con = decisions.init_db(db)
    # Kurse: AAA am 2026-11-05 = 110, am Snapshot-Tag 2026-11-09 = 120; SPY/RSP immer 100
    for t, d, k in [("AAA", "2026-11-05", 110.0), ("AAA", "2026-11-09", 120.0),
                    ("SPY", "2026-11-09", 100.0), ("RSP", "2026-11-09", 100.0),
                    ("AAA", "2026-08-14", 130.0), ("AAA", "2027-08-09", 156.0)]:
        con.execute("INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am) "
                    "VALUES (?,?,?,'fixture','x')", (t, d, k))
    if with_sm:
        # verwendetes 13F-Signal: Berkshire, filing_datum 2026-08-14 (nach Quartalsende 2026-06-30)
        con.execute(
            "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum,"
            " accession, cusip, ticker, name, title_of_class, shares, wert, anteil_portfolio,"
            " aktion, aenderung_pct, positions_gesamt, geladen_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("Berkshire Hathaway", "2026-Q2", "2026-06-30", "2026-08-14", "0001067983-26-000002",
             "111111111", "AAA", "AAA Corp", "COMMON STOCK", 1.0, 100.0, 0.06, "erhoeht", 0.2, 50, "t"))
    con.commit()
    con.close()
    return db


def _args(db, ranking, datum, korb="all"):
    return argparse.Namespace(db=str(db), ranking=ranking, korb=korb, datum=datum,
                              overlap=None, live=None)


def test_snapshot_startkurs_ist_kurs_am_snapshot_tag(tmp_path):
    db = _db(tmp_path)
    decisions.cmd_snapshot(_args(db, _ranking(tmp_path), "2026-11-09"))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM decisions WHERE ticker='AAA' AND korb='screener'").fetchone()
    assert r["kurs"] == 120.0                      # Kurs am Snapshot-Tag, nicht ranking-Kurs 100
    assert r["kurs_datum"] == "2026-11-09"
    assert r["datum"] == "2026-11-09"
    # filing_datum 2026-08-14 <= datum -> start_datum = datum
    assert r["start_datum"] == "2026-11-09"
    con.close()


def test_snapshot_datum_vor_filing_datum_bricht_ab_look_ahead(tmp_path):
    db = _db(tmp_path)
    with pytest.raises(SystemExit, match="Look-ahead"):
        decisions.cmd_snapshot(_args(db, _ranking(tmp_path), "2026-06-30"))   # Quartalsende < filing 2026-08-14
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0    # nichts geschrieben
    con.close()


def test_snapshot_idempotenz_bleibt(tmp_path):
    db = _db(tmp_path)
    decisions.cmd_snapshot(_args(db, _ranking(tmp_path), "2026-11-09"))
    decisions.cmd_snapshot(_args(db, _ranking(tmp_path), "2026-11-09"))       # gleiches Datum
    con = sqlite3.connect(db)
    n = con.execute("SELECT COUNT(*) FROM decisions WHERE korb<>''").fetchone()[0]
    assert n == 3                                       # AAA(screener) + SPY + RSP(benchmark), nicht doppelt
    r = con.execute("SELECT kurs FROM decisions WHERE ticker='AAA' AND korb='screener'").fetchone()
    assert r[0] == 120.0
    con.close()


def test_review_startdatum_max_datum_filing(tmp_path):
    db = _db(tmp_path, with_sm=True)
    con = decisions.init_db(db)
    # Altzeile (start_datum NULL): Snapshot-Datum 2026-06-30, filing_datum 2026-08-14 ->
    # start_datum = 2026-08-14, Startkurs = Kurs daran (130), nicht der gespeicherte 100
    con.execute(
        "INSERT INTO decisions (ticker, datum, aktion, korb, kurs, status_screener,"
        " gew_bear, gew_base, gew_bull, erwartungswert_pct, horizont_monate, thesis)"
        " VALUES ('AAA', '2026-06-30', 'auto', 'screener', 100.0, 'Kandidat',"
        " 0.25, 0.5, 0.25, 75.0, 12, 'legacy')")
    con.commit()
    con.close()
    # heute nach 12M ab start_datum 2026-08-14 (= 2027-08-09)
    out = tmp_path / "rev.csv"
    decisions.cmd_review(argparse.Namespace(db=str(db), ranking=None, heute="2027-09-01",
                                            ticker=None, out=str(out)))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT kurs_nachher, rendite_pct, ereignis_e FROM decisions WHERE ticker='AAA'").fetchone()
    # kurs_nach am 2027-08-09 = 156; Startkurs = 130 (Kurs am 2026-08-14) -> +20 %, nicht +56 %
    assert r["kurs_nachher"] == pytest.approx(156.0)
    assert r["rendite_pct"] == pytest.approx(20.0)
    assert r["ereignis_e"] == 1
    con.close()


def test_migration_start_datum_bei_alt_db(tmp_path):
    db = tmp_path / "alt.db"
    raw = sqlite3.connect(db)
    raw.execute("""CREATE TABLE decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, datum TEXT NOT NULL,
        aktion TEXT NOT NULL, kurs REAL NOT NULL, status_screener TEXT, iv_base REAL,
        iv_bear REAL, iv_bull REAL, gew_bear REAL NOT NULL DEFAULT 0.25,
        gew_base REAL NOT NULL DEFAULT 0.50, gew_bull REAL NOT NULL DEFAULT 0.25,
        erwartungswert_pct REAL, horizont_monate INTEGER NOT NULL DEFAULT 12,
        thesis TEXT, break_condition TEXT, kurs_nachher REAL, rendite_pct REAL,
        ereignis_e INTEGER, brier REAL, reviewed_at TEXT)""")
    raw.commit(); raw.close()
    con = decisions._connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(decisions)")}
    assert "start_datum" in cols
    con.close()
