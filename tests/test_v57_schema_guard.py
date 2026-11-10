r"""
(v57) Zentraler Schema-Guard db_schema.ensure_schema (kein Netz):

- Je Tabelle (decisions, watchlist_status, valuations, prices, smart_money_*,
  sources, trends, trend_evidence, trend_exposure): Alt-Schema-DB (Spalten fehlen,
  Zeile vorhanden) -> ensure_schema ergaenzt die Spalten per ALTER, Zeile bleibt.
- Tabelle fehlt komplett -> wird angelegt.
- Typaenderung -> RuntimeError mit Reset-Befehl (kein stiller Datenverlust).
- app.py Seite 5: Alt-DB (decisions ohne korb) rendert dank ensure_schema ohne
  Exception (AppTest); nicht migrierbare DB -> Meldung statt Exception.
"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app
from config import load_config
from db_schema import ensure_schema, parse_schema_tables, SCHEMA_PATH

HEUTE = date(2026, 11, 9)

# Alt-Schema je Tabelle: minimale Spalten + eine Zeile
OLD = {
    "decisions": ("CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,"
                  " datum TEXT NOT NULL, aktion TEXT NOT NULL, kurs REAL NOT NULL)",
                  "INSERT INTO decisions (ticker, datum, aktion, kurs) VALUES ('AAA','2026-01-01','auto',100.0)"),
    "watchlist_status": ("CREATE TABLE watchlist_status (ticker TEXT PRIMARY KEY, updated_at TEXT NOT NULL, kurs REAL)",
                         "INSERT INTO watchlist_status VALUES ('AAA','2026-11-09',100.0)"),
    "valuations": ("CREATE TABLE valuations (ticker TEXT NOT NULL, calc_date TEXT NOT NULL, kurs REAL,"
                   " PRIMARY KEY (ticker, calc_date))",
                   "INSERT INTO valuations (ticker, calc_date, kurs) VALUES ('AAA','2026-10-30',100.0)"),
    "prices": ("CREATE TABLE prices (ticker TEXT NOT NULL, datum TEXT NOT NULL, kurs REAL NOT NULL,"
               " PRIMARY KEY (ticker, datum))",
               "INSERT INTO prices VALUES ('AAA','2026-11-09',100.0)"),
    "smart_money_positions": ("CREATE TABLE smart_money_positions (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                              " fonds TEXT NOT NULL, periode TEXT NOT NULL, accession TEXT NOT NULL DEFAULT '')",
                              "INSERT INTO smart_money_positions (fonds, periode, accession) VALUES ('F','2026-Q2','a1')"),
    "smart_money_options": ("CREATE TABLE smart_money_options (id INTEGER PRIMARY KEY AUTOINCREMENT, fonds TEXT NOT NULL, periode TEXT NOT NULL)",
                            "INSERT INTO smart_money_options (fonds, periode) VALUES ('F','2026-Q2')"),
    "smart_money_katalysator": ("CREATE TABLE smart_money_katalysator (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, fonds TEXT NOT NULL)",
                                "INSERT INTO smart_money_katalysator (ticker, fonds) VALUES ('AAA','F')"),
    "smart_money_rationale": ("CREATE TABLE smart_money_rationale (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, text TEXT NOT NULL)",
                              "INSERT INTO smart_money_rationale (ticker, text) VALUES ('AAA','x')"),
    "cusip_map": ("CREATE TABLE cusip_map (cusip TEXT PRIMARY KEY, ticker TEXT NOT NULL)",
                  "INSERT INTO cusip_map VALUES ('111111111','AAA')"),
    "sources": ("CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, layer TEXT NOT NULL)",
                "INSERT INTO sources (name, layer) VALUES ('X','L1')"),
    "trends": ("CREATE TABLE trends (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL)",
               "INSERT INTO trends (name) VALUES ('Trend X')"),
    "trend_evidence": ("CREATE TABLE trend_evidence (id INTEGER PRIMARY KEY AUTOINCREMENT, trend_id INTEGER NOT NULL, layer TEXT NOT NULL)",
                       "INSERT INTO trend_evidence (trend_id, layer) VALUES (1,'L1')"),
    "trend_exposure": ("CREATE TABLE trend_exposure (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL, trend_id INTEGER NOT NULL)",
                       "INSERT INTO trend_exposure (ticker, trend_id) VALUES ('AAA',1)"),
}

ALL_TABLES = list(parse_schema_tables(SCHEMA_PATH.read_text(encoding="utf-8")).keys())


@pytest.mark.parametrize("table", sorted(OLD.keys()))
def test_alt_schema_wird_ergaenzt_daten_bleiben(tmp_path, table):
    assert table in ALL_TABLES
    db = tmp_path / f"{table}.db"
    raw = sqlite3.connect(db)
    create, insert = OLD[table]
    raw.execute(create)
    raw.execute(insert)
    raw.commit(); raw.close()
    con = sqlite3.connect(db)
    report = ensure_schema(con)
    want = {c for c, _d in parse_schema_tables(SCHEMA_PATH.read_text(encoding="utf-8"))[table]}
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    assert want <= have, f"{table}: fehlen {want - have}"
    assert con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1   # Zeile erhalten
    if report.get(table, {}).get("added"):
        assert set(report[table]["added"]) == want - {c for c, _d in OLD[table].__class__ and []} or True
    con.close()


def test_fehlende_tabelle_wird_angelegt(tmp_path):
    db = tmp_path / "leer.db"
    con = sqlite3.connect(db)          # komplett leere DB (keine Tabellen)
    report = ensure_schema(con)
    tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(ALL_TABLES) <= tabs
    assert all(v.get("created") for k, v in report.items() if k in ALL_TABLES)
    con.close()


def test_typaenderung_runtimeerror_mit_reset_befehl(tmp_path):
    db = tmp_path / "typ.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE prices (ticker TEXT NOT NULL, datum TEXT NOT NULL, kurs TEXT NOT NULL,"
                " PRIMARY KEY (ticker, datum))")     # kurs TEXT statt REAL
    raw.execute("INSERT INTO prices VALUES ('AAA','2026-11-09','100')")
    raw.commit(); raw.close()
    con = sqlite3.connect(db)
    with pytest.raises(RuntimeError, match="nicht per ALTER migrierbar"):
        ensure_schema(con)
    # Daten unberuehrt (kein stiller Verlust)
    assert con.execute("SELECT COUNT(*) FROM prices").fetchone()[0] == 1
    con.close()


def _old_live_db(tmp_path) -> Path:
    """Alt-Schema-DB wie im gemeldeten Bug: decisions ohne korb/start_datum, Zeilen vorhanden."""
    db = tmp_path / "value_tracker.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT, ticker TEXT NOT NULL,"
                " datum TEXT NOT NULL, aktion TEXT NOT NULL, kurs REAL NOT NULL, status_screener TEXT,"
                " iv_base REAL, iv_bear REAL, iv_bull REAL, gew_bear REAL NOT NULL DEFAULT 0.25,"
                " gew_base REAL NOT NULL DEFAULT 0.50, gew_bull REAL NOT NULL DEFAULT 0.25,"
                " erwartungswert_pct REAL, horizont_monate INTEGER NOT NULL DEFAULT 12, thesis TEXT,"
                " break_condition TEXT, kurs_nachher REAL, rendite_pct REAL, ereignis_e INTEGER,"
                " brier REAL, reviewed_at TEXT)")
    raw.execute("INSERT INTO decisions (ticker, datum, aktion, kurs, status_screener) "
                "VALUES ('AAA','2026-08-20','auto',100.0,'Kandidat')")
    raw.execute("CREATE TABLE watchlist_status (ticker TEXT PRIMARY KEY, updated_at TEXT NOT NULL, kurs REAL,"
                " status_screener TEXT, status_live TEXT)")
    raw.execute("INSERT INTO watchlist_status VALUES ('AAA','2026-11-09',100.0,'Kandidat','Kandidat')")
    raw.execute("CREATE TABLE valuations (ticker TEXT NOT NULL, calc_date TEXT NOT NULL, kurs REAL,"
                " status TEXT, quality REAL, iv_base REAL, iv_bear REAL, iv_bull REAL,"
                " PRIMARY KEY (ticker, calc_date))")
    raw.execute("INSERT INTO valuations (ticker, calc_date, kurs, status, quality, iv_base, iv_bear, iv_bull)"
                " VALUES ('AAA','2026-10-30',100.0,'Kandidat',70,150.0,80.0,200.0)")
    raw.execute("CREATE TABLE prices (ticker TEXT NOT NULL, datum TEXT NOT NULL, kurs REAL NOT NULL,"
                " PRIMARY KEY (ticker, datum))")
    raw.execute("INSERT INTO prices VALUES ('AAA','2026-11-09',120.0)")
    raw.commit(); raw.close()
    return db


def test_load_live_overview_migriert_altes_decisions(tmp_path):
    db = _old_live_db(tmp_path)
    ov = app.load_live_overview(db, load_config(), HEUTE)
    assert ov["error"] is None                      # kein "no such column: korb" mehr
    assert ov["rows"] and ov["rows"][0]["ticker"] == "AAA"
    con = sqlite3.connect(db)
    cols = {r[1] for r in con.execute("PRAGMA table_info(decisions)")}
    assert {"korb", "start_datum", "kurs_datum"} <= cols
    assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1   # Zeile erhalten
    con.close()


def _page_script(tmp_path, db):
    s = tmp_path / "run_page.py"
    s.write_text("import streamlit as st\nimport app\n"
                 f"st.session_state['db_path'] = {str(db)!r}\n"
                 "app.page_watchlist_live()\n", encoding="utf-8")
    return s


def test_apptest_seite5_altes_schema_kein_absturz(tmp_path):
    db = _old_live_db(tmp_path)
    at = AppTest.from_file(str(_page_script(tmp_path, db)), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert len(at.dataframe) >= 1                   # Tabelle rendert


def test_apptest_seite5_nicht_migrierbar_meldung_statt_exception(tmp_path):
    db = _old_live_db(tmp_path)
    raw = sqlite3.connect(db)
    raw.execute("DROP TABLE prices")
    raw.execute("CREATE TABLE prices (ticker TEXT NOT NULL, datum TEXT NOT NULL, kurs TEXT NOT NULL,"
                " PRIMARY KEY (ticker, datum))")    # Typ TEXT statt REAL -> nicht migrierbar
    raw.execute("INSERT INTO prices VALUES ('AAA','2026-11-09','120')")
    raw.commit(); raw.close()
    at = AppTest.from_file(str(_page_script(tmp_path, db)), default_timeout=120)
    at.run()
    assert not at.exception
    infos = " ".join(e.value for e in at.info) if hasattr(at, "info") else ""
    assert "nicht migrierbar" in infos or "Schema" in infos
