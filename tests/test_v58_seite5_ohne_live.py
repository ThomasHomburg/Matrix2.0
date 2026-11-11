r"""
(v58) app.py: Live-Lauf (get_companies + evaluate_all in main()) startet nicht mehr
auf jeder Seite -- nur noch auf Seite 1-4 via ensure_screener_data(); Seite 5
(Watchlist Live) liest ausschliesslich value_tracker.db. Cache st.cache_data TTL
15 Min, Schluessel = Ticker-Liste -> wiederholte Seitenwechsel starten nicht neu.

Tests (AppTest, kein Netz): Seite 5 ruft die Live-Auswertung nicht auf (Mock zaehlt),
Seite 1-4 rufen sie genau einmal (auch bei wiederholtem Aufruf), main() ohne
evaluate_all.
"""

import inspect
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app
import decisions
from data.providers.base import ProviderError

HEUTE = date(2026, 11, 9)


def _live_db(tmp_path) -> Path:
    db = tmp_path / "value_tracker.db"
    con = decisions.init_db(db)
    con.execute("INSERT INTO watchlist_status (ticker, updated_at, kurs, status_screener, status_live,"
                " mos_live_min, mos_live_robust, kaufzone_kurs, distanz_pct, timing_status,"
                " wert_veraltet, alarm) VALUES ('AAA',?,100.0,'Kandidat','Kandidat',0.33,0.29,70.0,-0.3,'positiv',0,0)",
                (HEUTE.isoformat(),))
    con.execute("INSERT INTO valuations (ticker, calc_date, kurs, status, quality, iv_base, iv_bear,"
                " iv_bull, naechster_earnings) VALUES ('AAA',?,100.0,'Kandidat',70,150.0,80.0,200.0,'2026-11-29')",
                ((HEUTE.replace(day=1)).isoformat(),))
    con.execute("INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am)"
                " VALUES ('AAA',?,120.0,'fixture','x')", (HEUTE.isoformat(),))
    con.commit()
    con.close()
    return db


def _page_script(tmp_path, db, body):
    s = tmp_path / "run_page.py"
    s.write_text("import streamlit as st\nimport app\n"
                 f"st.session_state['db_path'] = {str(db)!r}\n" + body, encoding="utf-8")
    return s


def _counting_mocks(monkeypatch):
    calls = {"evaluate": 0, "load_live": 0}

    def fake_evaluate(companies, cfg):
        calls["evaluate"] += 1
        return [], []

    def fake_load_live(tickers, years):
        calls["load_live"] += 1
        return []          # -> DEMO-Fallback, kein Netz

    monkeypatch.setattr(app, "evaluate_all", fake_evaluate)
    monkeypatch.setattr(app, "load_live", fake_load_live)
    return calls


def test_seite5_ruft_live_auswertung_nicht_auf(tmp_path, monkeypatch):
    calls = _counting_mocks(monkeypatch)
    db = _live_db(tmp_path)
    at = AppTest.from_file(str(_page_script(tmp_path, db, "app.page_watchlist_live()\n")),
                           default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert calls["evaluate"] == 0 and calls["load_live"] == 0   # Seite 5: nur DB
    assert len(at.dataframe) >= 1                              # Tabelle rendert


def test_seiten1_4_starten_live_genau_einmal(tmp_path, monkeypatch):
    calls = _counting_mocks(monkeypatch)
    # AppTest-Skript: Screener-Daten zweimal anfordern (Seitenwechsel-Simulation)
    s = tmp_path / "run_ensure.py"
    s.write_text("import streamlit as st\nimport app\n"
                 "app.ensure_screener_data()\n"
                 "app.ensure_screener_data()\n", encoding="utf-8")
    at = AppTest.from_file(str(s), default_timeout=120)
    at.run()
    assert not at.exception
    assert calls["evaluate"] == 1        # zweiter Aufruf ueber Session/Cache: kein neuer Lauf
    assert calls["load_live"] == 1
    assert "rows" in at.session_state


def test_main_und_seite5_ohne_live_aufruf():
    src_main = inspect.getsource(app.main)
    assert "evaluate_all(" not in src_main and "get_companies()" not in src_main
    src_page5 = inspect.getsource(app.page_watchlist_live)
    assert "ensure_screener_data" not in src_page5
    for fn in (app.page_ranking, app.page_scorecard, app.page_matrix, app.page_import):
        assert "ensure_screener_data()" in inspect.getsource(fn)
    # Split-Regel: neuer Helper liegt im app_part2-Bereich (nach def _formula_block)
    src = inspect.getsource(app)
    assert src.index("def _formula_block") < src.index("def ensure_screener_data")
