r"""
(v53) app.py Seite 5 "Watchlist Live" (AppTest + Fixture-DB, kein Netz):

- load_live_overview liest value_tracker.db (watchlist_status/valuations/prices/
  decisions/smart_money_*), Kopfzeile Datenstand + Veraltet-Warnung.
- Tabelle filter/sortierbar (filter_live_rows/sort_live_rows), Timing als
  [POS]/[NEU]/[NEG], Smart Money via smart_money.py (urteil/divergenz_stufe).
- Export live_export_csv: ASCII-Spaltennamen, ASCII-Inhalt (watchlist_live_export.csv).
- Leere DB / fehlende Tabellen -> Meldung statt Exception.
- AppTest-Rendering der Seite mit Fixture-DB (3 Ticker) und mit leerer DB.
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app
import decisions
from config import load_config

HEUTE = date.today()


def _fixture_db(tmp_path, with_rows=True) -> Path:
    db = tmp_path / "value_tracker.db"
    con = decisions.init_db(db)          # schema.sql -> alle Tabellen inkl. smart_money_*
    if with_rows:
        val_date = (HEUTE - timedelta(days=10)).isoformat()
        for t, status, quality, ivb in [("AAA", "Kandidat", 72, 150.0),
                                        ("BBB", "Beobachtung", 65, 110.0),
                                        ("CCC", "nein", 40, 90.0)]:
            con.execute(
                "INSERT INTO valuations (ticker, calc_date, kurs, kurs_datum, status, quality,"
                " mos_min, mos_robust, iv_bear, iv_base, iv_bull, iv_avg3, iv_latest, iv_ttm,"
                " iv_ttm_only, iv_base_10pct, naechster_earnings)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t, val_date, 100.0, val_date, status, quality, 0.40, 0.35,
                 80.0, ivb, 200.0, ivb, ivb, ivb, ivb, 140.0, (HEUTE + timedelta(days=20)).isoformat()))
            con.execute(
                "INSERT INTO watchlist_status (ticker, updated_at, kurs, status_screener,"
                " status_live, mos_live_min, mos_live_robust, kaufzone_kurs, distanz_pct,"
                " timing_status, wert_veraltet, alarm) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (t, HEUTE.isoformat(), 100.0, status,
                 {"AAA": "Kandidat", "BBB": "Beobachtung", "CCC": "nein"}[t],
                 0.33, 0.29, 70.0, -0.30,
                 {"AAA": "positiv", "BBB": "neutral", "CCC": "negativ"}[t],
                 0, 1 if t == "CCC" else 0))
            for i in range(0, 366, 30):     # ~1 Jahr Kursverlauf
                d = (HEUTE - timedelta(days=365 - i)).isoformat()
                con.execute("INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am)"
                            " VALUES (?,?,?,'fixture','x')", (t, d, 90.0 + i * 0.05))
            con.execute("INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am)"
                        " VALUES (?,?,?,'fixture','x')", (t, HEUTE.isoformat(), 100.0))
            con.execute(
                "INSERT INTO decisions (ticker, datum, aktion, korb, kurs, status_screener,"
                " gew_bear, gew_base, gew_bull, erwartungswert_pct, wahrscheinlichkeit_p,"
                " horizont_monate, thesis) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t, (HEUTE - timedelta(days=400)).isoformat(), "auto",
                 "screener" if t != "CCC" else "kontrolle", 100.0, status,
                 0.25, 0.50, 0.25, 75.0, 0.75, 12, "fixture"))
        # Smart Money: Berkshire erhoeht AAA in 2026-Q2; alter Fonds fuer stale-13F
        con.execute(
            "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum,"
            " accession, cusip, ticker, name, title_of_class, shares, wert, anteil_portfolio,"
            " aktion, aenderung_pct, positions_gesamt, geladen_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("Berkshire Hathaway", "2026-Q2", "2026-06-30", "2026-08-14", "acc1",
             "111111111", "AAA", "AAA Corp", "COMMON STOCK", 1000.0, 100000.0, 0.06,
             "erhoeht", 0.2, 50, "x"))
        con.execute(
            "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum,"
            " accession, cusip, ticker, name, title_of_class, shares, wert, anteil_portfolio,"
            " aktion, aenderung_pct, positions_gesamt, geladen_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("Old Fund", "2025-Q1", "2025-03-31", "2025-05-15", "acc2",
             "222222222", "BBB", "BBB Inc", "COMMON STOCK", 10.0, 100.0, 0.01,
             "basis", None, 5, "x"))
        con.execute("INSERT INTO smart_money_rationale (ticker, fonds, text, quelle_url, datum, typ)"
                    " VALUES (?,?,?,?,?,?)",
                    ("AAA", "Berkshire Hathaway", "Buffett erhoeht Position", "https://example.com/b",
                     "2026-08-20", "Brief"))
        con.execute("INSERT INTO smart_money_katalysator (ticker, fonds, datum, quelle_url, form)"
                    " VALUES (?,?,?,?,?)",
                    ("AAA", "Starboard Value", "2026-07-01", "https://example.com/13d", "SC 13D"))
        con.commit()
    con.close()
    return db


def _page_script(tmp_path, db: Path) -> Path:
    script = tmp_path / "run_page.py"
    script.write_text(
        "import streamlit as st\n"
        "import app\n"
        f"st.session_state['db_path'] = {str(db)!r}\n"
        "app.page_watchlist_live()\n", encoding="utf-8")
    return script


# --------------------------------------------------------------------------
# load_live_overview + Kopfzeile
# --------------------------------------------------------------------------

def test_load_live_overview_fixture(tmp_path, capsys):
    db = _fixture_db(tmp_path)
    cfg = load_config()
    ov = app.load_live_overview(db, cfg, HEUTE)
    assert ov["error"] is None
    assert {r["ticker"] for r in ov["rows"]} == {"AAA", "BBB", "CCC"}
    by = {r["ticker"]: r for r in ov["rows"]}
    assert by["AAA"]["korb"] == "screener" and by["CCC"]["korb"] == "kontrolle"
    assert by["AAA"]["timing_status"] == "[POS]" and by["BBB"]["timing_status"] == "[NEU]"
    assert by["CCC"]["timing_status"] == "[NEG]" and by["CCC"]["alarm"] == 1
    assert by["AAA"]["smart_money_score"] == 3          # Berkshire erhoeht, Anteil 6 % >= 5 %
    assert by["AAA"]["urteil"] == "Bestaetigung"         # Kandidat + Kaeufer
    assert by["AAA"]["divergenz_stufe"] == "-"
    # Datenstand
    assert ov["kurs_stand"]["datum"] == HEUTE.isoformat() and ov["kurs_stand"]["alter_tage"] == 0
    assert ov["valuation_stand"]["alter_tage"] == 10
    fonds = {f["fonds"]: f for f in ov["fonds_13f"]}
    assert fonds["Berkshire Hathaway"]["periode"] == "2026-Q2"
    # Old Fund report 2025-03-31 -> > 140 Tage -> 13F veraltet; Kurs/Valuation frisch
    assert ov["stale"]["13f"] is True
    assert ov["stale"]["kurs"] is False and ov["stale"]["valuation"] is False
    # Fixture-Tabelle als Textausgabe
    df = pd.DataFrame(ov["rows"])[app.LIVE_TABLE_COLS]
    print("\n=== Fixture-Tabelle (Seite 5) ===")
    print(df.to_string(index=False))
    out = capsys.readouterr().out
    assert "AAA" in out and "Kandidat" in out and "[POS]" in out


def test_load_live_overview_stale_kurs(tmp_path):
    db = _fixture_db(tmp_path)
    con = sqlite3.connect(db)
    con.execute("DELETE FROM prices WHERE datum > ?", ((HEUTE - timedelta(days=10)).isoformat(),))
    con.commit(); con.close()
    ov = app.load_live_overview(db, load_config(), HEUTE)
    assert ov["stale"]["kurs"] is True      # letzter Kurs 10 Tage alt > 3


def test_load_live_overview_leere_und_fehlende_db(tmp_path):
    cfg = load_config()
    # DB ohne Zeilen
    db = _fixture_db(tmp_path, with_rows=False)
    ov = app.load_live_overview(db, cfg, HEUTE)
    assert ov["error"] is None and ov["rows"] == []
    # DB-Datei fehlt komplett
    ov2 = app.load_live_overview(tmp_path / "fehlt.db", cfg, HEUTE)
    assert ov2["error"] and "Keine Datenbank" in ov2["error"]
    # DB mit fremder Tabelle (fehlende Pflicht-Tabellen)
    db3 = tmp_path / "fremd.db"
    con = sqlite3.connect(db3)
    con.execute("CREATE TABLE foo (x TEXT)")
    con.commit(); con.close()
    ov3 = app.load_live_overview(db3, cfg, HEUTE)
    assert ov3["error"] and "fehlende Tabellen" in ov3["error"]


# --------------------------------------------------------------------------
# Filter/Sortierung + Export (ASCII)
# --------------------------------------------------------------------------

def test_filter_und_sort_live_rows(tmp_path):
    db = _fixture_db(tmp_path)
    ov = app.load_live_overview(db, load_config(), HEUTE)
    rows = ov["rows"]
    assert {r["ticker"] for r in app.filter_live_rows(rows, koerbe=["screener"])} == {"AAA", "BBB"}
    assert {r["ticker"] for r in app.filter_live_rows(rows, statuses=["nein"])} == {"CCC"}
    assert {r["ticker"] for r in app.filter_live_rows(rows, alarm_only=True)} == {"CCC"}
    assert app.filter_live_rows(rows) == rows or len(app.filter_live_rows(rows)) == 3
    s = app.sort_live_rows(rows, "quality", ascending=False)
    assert [r["ticker"] for r in s] == ["AAA", "BBB", "CCC"]   # 72, 65, 40
    s2 = app.sort_live_rows(rows, "ticker", ascending=True)
    assert [r["ticker"] for r in s2] == ["AAA", "BBB", "CCC"]


def test_live_export_csv_ascii(tmp_path):
    db = _fixture_db(tmp_path)
    ov = app.load_live_overview(db, load_config(), HEUTE)
    rows = ov["rows"]
    rows[0]["name"] = "Müller AG"          # Umlaut im Wert
    data = app.live_export_csv(rows)
    assert all(b < 128 for b in data)       # kompletter Export ASCII
    text = data.decode("ascii")
    header = text.splitlines()[0]
    assert header == "TICKER,NAME,KORB,STATUS_SCREENER,STATUS_LIVE,KURS,MOS_LIVE_MIN," \
                     "MOS_LIVE_ROBUST,KAUFZONE_KURS,DISTANZ_PCT,TIMING_STATUS,SM_SCORE," \
                     "URTEIL,DIVERGENZ_STUFE,ALARM,WERT_VERALTET,NAECHSTER_EARNINGS"
    assert "Mueller AG" in text
    assert "Beobachtung" in text


# --------------------------------------------------------------------------
# AppTest: Seite rendert mit Fixture-DB bzw. leerer DB (kein Netz)
# --------------------------------------------------------------------------

def test_apptest_page_renders_fixture_db(tmp_path):
    db = _fixture_db(tmp_path)
    at = AppTest.from_file(str(_page_script(tmp_path, db)), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    # Ueberschrift + Tabelle + Filter + Detail-Selectbox + Buttons
    assert any("Watchlist Live" in h.value for h in at.header)
    assert len(at.dataframe) >= 2                       # Haupttabelle + Detail-Tabellen
    assert any("Live aktualisieren" in b.label for b in at.button)
    assert any("watchlist_live_export.csv" in d.label or d.file_name == "watchlist_live_export.csv"
               for d in at.download_button)
    assert any(sb.label == "Ticker" for sb in at.selectbox)
    assert any("Korb" == ms.label for ms in at.multiselect)
    # Datenstand-Metriken + 13F-Kopfzeile
    assert any(m.label == "Letzter Kurs" for m in at.metric)
    cap_text = " ".join(c.value for c in at.caption)
    assert "Berkshire Hathaway: 2026-Q2" in cap_text
    # Veraltet-Warnung wegen Old Fund (13F > 140 Tage)
    assert any("Veraltete Daten" in w.value for w in at.warning)


def test_apptest_page_leere_db_meldung_statt_exception(tmp_path):
    db = _fixture_db(tmp_path, with_rows=False)
    at = AppTest.from_file(str(_page_script(tmp_path, db)), default_timeout=120)
    at.run()
    assert not at.exception
    infos = " ".join(e.value for e in at.info) if hasattr(at, "info") else ""
    marks = " ".join(m.value for m in at.markdown)
    assert "Noch keine Live-Daten" in infos or "Noch keine Live-Daten" in marks


def test_apptest_page_fehlende_db(tmp_path):
    script = tmp_path / "run_page2.py"
    script.write_text(
        "import streamlit as st\n"
        "import app\n"
        f"st.session_state['db_path'] = {str(tmp_path / 'fehlt.db')!r}\n"
        "app.page_watchlist_live()\n", encoding="utf-8")
    at = AppTest.from_file(str(script), default_timeout=120)
    at.run()
    assert not at.exception
    infos = " ".join(e.value for e in at.info) if hasattr(at, "info") else ""
    assert "Keine Datenbank" in infos
