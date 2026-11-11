r"""
(v59) app.py Seite 5 NaN/None-sicher + Formatierung (AppTest + Fixture-DB, kein Netz):

- decisions.erwartungswert_pct: None/NaN/ungueltig in iv_* oder kurs -> NaN, nie
  Exception (frueher TypeError '>=' not supported between NoneType and float in
  watchlist_detail).
- Ticker ohne iv_* rendert ohne Exception; Detail zeigt "Keine Bewertung gespeichert.
  Bitte run_ranking.py und live_update.py ausfuehren" statt Szenario-Tabelle.
- Formatierung: kurs/kaufzone_kurs 2 Dezimalstellen, mos_live_*/distanz_pct als
  Prozent mit 1 Nachkommastelle, None/NaN -> "n/a"; korb leer -> Hinweis
  "Snapshot noch nicht gelaufen"; Anzeige "n von m Tickern"; Kopfzeile mit Anzahl
  der Ticker ohne Bewertung; Statusfilter default = alle Status.
"""

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import app
import decisions
from config import load_config

HEUTE = date(2026, 11, 9)
NAN = float("nan")


def _db(tmp_path, with_decisions=True, name="value_tracker.db") -> Path:
    db = tmp_path / name
    con = decisions.init_db(db)
    d = HEUTE.isoformat()
    # AAA: komplette Bewertung + Kurs + Korb
    con.execute("INSERT INTO watchlist_status (ticker, updated_at, kurs, status_screener, status_live,"
                " mos_live_min, mos_live_robust, kaufzone_kurs, distanz_pct, timing_status, wert_veraltet, alarm)"
                " VALUES ('AAA',?,120.0,'Kandidat','Kandidat',0.33,0.29,70.0,-0.30,'positiv',0,0)", (d,))
    con.execute("INSERT INTO valuations (ticker, calc_date, kurs, status, quality, iv_bear, iv_base, iv_bull,"
                " naechster_earnings) VALUES ('AAA',?,120.0,'Kandidat',70,80.0,150.0,200.0,'2026-11-29')", (d,))
    con.execute("INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am)"
                " VALUES ('AAA',?,120.0,'fixture','x')", (d,))
    if with_decisions:
        con.execute("INSERT INTO decisions (ticker, datum, aktion, korb, kurs, status_screener)"
                    " VALUES ('AAA',?,'auto','screener',120.0,'Kandidat')", (d,))
    # BBB: Bewertung vorhanden, aber iv_* NULL; Kurs in watchlist_status NULL
    con.execute("INSERT INTO watchlist_status (ticker, updated_at, kurs, status_screener, status_live,"
                " mos_live_min, mos_live_robust, kaufzone_kurs, distanz_pct, timing_status, wert_veraltet, alarm)"
                " VALUES ('BBB',?,NULL,'Beobachtung','Beobachtung',NULL,NULL,NULL,NULL,'neutral',0,1)", (d,))
    con.execute("INSERT INTO valuations (ticker, calc_date, kurs, status, quality, iv_bear, iv_base, iv_bull)"
                " VALUES ('BBB',?,NULL,'Beobachtung',60,NULL,NULL,NULL)", (d,))
    # CCC: nur watchlist_status, keine valuations-Zeile
    con.execute("INSERT INTO watchlist_status (ticker, updated_at, kurs, status_screener, status_live,"
                " mos_live_min, mos_live_robust, kaufzone_kurs, distanz_pct, timing_status, wert_veraltet, alarm)"
                " VALUES ('CCC',?,50.0,'nein','nein',0.1,0.05,40.0,-0.2,'negativ',1,0)", (d,))
    con.commit()
    con.close()
    return db


def _page(tmp_path, db, body="app.page_watchlist_live()\n"):
    s = tmp_path / "run_page.py"
    s.write_text("import streamlit as st\nimport app\n"
                 f"st.session_state['db_path'] = {str(db)!r}\n" + body, encoding="utf-8")
    return s


# --------------------------------------------------------------------------
# erwartungswert_pct: None/NaN -> NaN, nie Exception
# --------------------------------------------------------------------------

def test_erwartungswert_pct_none_nan_kein_typeerror():
    g = {"bear": 0.25, "base": 0.50, "bull": 0.25}
    for args in [(None, 120.0, 150.0, 100.0), (90.0, NAN, 150.0, 100.0),
                 (90.0, 120.0, None, 100.0), (90.0, 120.0, 150.0, None),
                 (90.0, 120.0, 150.0, NAN), ("x", 120.0, 150.0, 100.0)]:
        ew = decisions.erwartungswert_pct(*args, g)
        assert ew != ew, args                      # NaN, keine Exception
    assert decisions.erwartungswert_pct(90.0, 120.0, 150.0, 100.0, g) == pytest.approx(75.0)


# --------------------------------------------------------------------------
# Uebersicht: ohne_bewertung
# --------------------------------------------------------------------------

def test_overview_ohne_bewertung(tmp_path):
    db = _db(tmp_path)
    ov = app.load_live_overview(db, load_config(), HEUTE)
    assert ov["error"] is None
    assert ov["ohne_bewertung"] == 2        # BBB (iv NULL) + CCC (keine valuations-Zeile)
    assert {r["ticker"] for r in ov["rows"]} == {"AAA", "BBB", "CCC"}


# --------------------------------------------------------------------------
# AppTest: Seite 5 rendert; Formatierung; Filter; Detail ohne iv_*
# --------------------------------------------------------------------------

def test_apptest_seite5_nan_kein_absturz_und_formatierung(tmp_path):
    db = _db(tmp_path)
    at = AppTest.from_file(str(_page(tmp_path, db)), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    # Kopfzeile: Anzahl ohne Bewertung
    caps = " ".join(c.value for c in at.caption)
    assert "ohne gespeicherte Bewertung (iv_*): 2" in caps
    # Anzeige "n von m Tickern"
    assert "3 von 3 Tickern" in caps
    # Haupttabelle formatiert (Default-Sortierung ticker absteigend: CCC, BBB, AAA)
    df = at.dataframe[0].value
    by = {r["ticker"]: r for _, r in df.iterrows()}
    assert by["AAA"]["kurs"] == "120.00" and by["AAA"]["kaufzone_kurs"] == "70.00"
    assert by["AAA"]["mos_live_min"] == "33.0 %" and by["AAA"]["distanz_pct"] == "-30.0 %"
    assert by["BBB"]["kurs"] == "n/a" and by["BBB"]["mos_live_min"] == "n/a"
    assert by["CCC"]["kurs"] == "50.00" and by["CCC"]["mos_live_robust"] == "5.0 %"
    assert by["AAA"]["korb"] == "screener"


def test_apptest_detail_ohne_iv_zeigt_meldung_kein_absturz(tmp_path):
    db = _db(tmp_path)
    at = AppTest.from_file(str(_page(tmp_path, db)), default_timeout=120)
    at.run()
    assert not at.exception
    # Detail auf BBB (iv_* NULL) stellen
    sb = next(s for s in at.selectbox if s.label == "Ticker")
    sb.set_value("BBB")
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    infos = " ".join(e.value for e in at.info)
    assert "Keine Bewertung gespeichert" in infos
    assert "run_ranking.py" in infos


def test_apptest_filter_status_und_korb_hinweis(tmp_path):
    db = _db(tmp_path)
    at = AppTest.from_file(str(_page(tmp_path, db)), default_timeout=120)
    at.run()
    # Statusfilter default = alle; jetzt nur Kandidat -> "1 von 3"
    ms = next(m for m in at.multiselect if m.label == "Status live")
    assert set(ms.options) == {"Beobachtung", "Kandidat", "nein"}   # alle inkl. Kandidat
    ms.set_value(["Kandidat"])
    at.run()
    assert not at.exception
    caps = " ".join(c.value for c in at.caption)
    assert "1 von 3 Tickern" in caps
    assert len(at.dataframe[0].value) == 1
    # korb leer (keine decisions-Zeilen) -> Hinweis ueber der Tabelle
    db2 = _db(tmp_path, with_decisions=False, name="ohne_snap.db")
    at2 = AppTest.from_file(str(_page(tmp_path, db2)), default_timeout=120)
    at2.run()
    assert not at2.exception
    infos2 = " ".join(e.value for e in at2.info)
    assert "Snapshot noch nicht gelaufen" in infos2
