r"""
(v60) live_update.py + Seite 5 -- drei Luecken (Tests mit Mock-Kursen, kein Netz):

1. Watchlist-Umfang: --watch DATEI; Vereinigung aus Ranking (Kandidat/Beobachtung) +
   watchlist.extra_tickers + --watch-Datei + decisions-Koerben (letzter Snapshot),
   Ausgabe Zahl je Quelle (watchlist_scope).
2. Kurshistorie: backfill_prices -- erster Lauf 2 Jahre (period='2y'), danach nur
   neue Tage ('5d'), inkrementell ohne Duplikate; timing_from_db rechnet
   timing_kennzahlen aus der prices-Historie (300 synthetische Handelstage:
   steigend -> positiv, fallend -> negativ, gemischt -> neutral).
3. Smart-Money-Block Seite 5: smart_money_block unterscheidet 'nicht_geladen'
   ("13F-Daten nicht geladen") von 'keine_halter' ("Kein Fonds haelt den Titel
   (13F-Periode ...)") vs. 'halter' (fonds/periode/aktion/anteil/alter_tage).
"""

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app
import decisions
import live_update
from config import load_config

HEUTE = date(2026, 11, 9)


def _cfg():
    cfg = load_config()
    cfg["watchlist"] = dict(cfg.get("watchlist", {}))
    cfg["watchlist"]["statuses"] = ["Kandidat", "Beobachtung"]
    cfg["watchlist"]["extra_tickers"] = ["IT", "FDS"]
    return cfg


def _ranking_rows():
    return [{"ticker": "AAA", "status": "Kandidat"},
            {"ticker": "BBB", "status": "Beobachtung"},
            {"ticker": "CCC", "status": "nein"}]


# --------------------------------------------------------------------------
# (1) Umfang: Vereinigung der Quellen
# --------------------------------------------------------------------------

def test_watchlist_scope_vereinigung(tmp_path):
    db = tmp_path / "d.db"
    con = decisions.init_db(db)
    con.execute("INSERT INTO decisions (ticker, datum, aktion, korb, kurs) "
                "VALUES ('AAA','2026-08-20','auto','screener',100.0)")
    con.execute("INSERT INTO decisions (ticker, datum, aktion, korb, kurs) "
                "VALUES ('SPY','2026-08-20','auto','benchmark',100.0)")
    con.execute("INSERT INTO decisions (ticker, datum, aktion, korb, kurs) "
                "VALUES ('RSP','2026-11-09','auto','benchmark',100.0)")   # letzter Snapshot
    con.commit(); con.close()
    watch = tmp_path / "watch.csv"
    watch.write_text("ticker\nAAA\nXYZ\n", encoding="utf-8")
    tickers, quellen = live_update.watchlist_scope(
        _ranking_rows(), _cfg(), watch_csv=str(watch), db_path=db)
    assert set(tickers) == {"AAA", "BBB", "IT", "FDS", "XYZ", "RSP"}
    assert quellen["ranking"] == 2 and quellen["extra_tickers"] == 2
    assert quellen["watch_datei"] == 2 and quellen["decisions_koerbe"] == 1   # nur letzter Snapshot
    assert quellen["snapshot_datum"] == "2026-11-09"
    # ohne --watch / ohne DB: nur ranking + extra
    t2, q2 = live_update.watchlist_scope(_ranking_rows(), _cfg(), db_path=tmp_path / "fehlt.db")
    assert set(t2) == {"AAA", "BBB", "IT", "FDS"} and q2["watch_datei"] == 0 and q2["decisions_koerbe"] == 0
    # --watch im CLI
    args = live_update._parse_args(["--ranking", "r.csv", "--watch", str(watch)])
    assert args.watch == str(watch)


# --------------------------------------------------------------------------
# (2) Kurshistorie: Backfill 2y, dann inkrementell; Timing aus 300 Tagen
# --------------------------------------------------------------------------

def _series(values, start=date(2026, 1, 5)):
    idx = pd.date_range(start=start, periods=len(values), freq="B")
    return pd.Series(values, index=idx)


def test_backfill_erst_2y_dann_inkrementell(tmp_path):
    db = tmp_path / "p.db"
    con = decisions.init_db(db)
    calls = []

    def mock_hist(ticker, period):
        calls.append((ticker, period))
        if period == "2y":
            return _series([100.0 + i * 0.1 for i in range(500)], start=date(2025, 1, 6))
        # 5d: 5 neue Tage direkt nach dem letzten Datum der 2y-Serie
        last = _series([1.0] * 500, start=date(2025, 1, 6)).index[-1]
        return _series([160.0 + i for i in range(5)], start=last + pd.Timedelta(days=1))

    n1, err1 = live_update.backfill_prices(con, ["AAA"], batch=10, pause=0.0, hist_fetcher=mock_hist)
    assert err1 == [] and n1 == 500
    assert calls[0] == ("AAA", "2y")
    assert con.execute("SELECT COUNT(*) FROM prices WHERE ticker='AAA'").fetchone()[0] == 500
    # zweiter Lauf: nur neue Tage (period '5d'), keine Duplikate
    n2, err2 = live_update.backfill_prices(con, ["AAA"], batch=10, pause=0.0, hist_fetcher=mock_hist)
    assert err2 == [] and n2 == 5
    assert calls[-1] == ("AAA", "5d")
    c = con.execute("SELECT COUNT(*), COUNT(DISTINCT datum) FROM prices WHERE ticker='AAA'").fetchone()
    assert c[0] == 505 and c[1] == 505
    # Fehlerlog: Fetcher wirft -> error, Lauf stoppt nicht
    def bad(ticker, period):
        raise ValueError("boom")
    n3, err3 = live_update.backfill_prices(con, ["BBB"], pause=0.0, hist_fetcher=bad)
    assert n3 == 0 and err3 and err3[0]["ticker"] == "BBB"
    con.close()


def test_timing_aus_300_synthetischen_handelstagen(tmp_path):
    db = tmp_path / "t.db"
    con = decisions.init_db(db)

    def seed(ticker, values):
        s = _series(values)
        for d, k in s.items():
            con.execute("INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am)"
                        " VALUES (?,?,?,'fixture','x')", (ticker, d.date().isoformat(), float(k)))
    seed("UP", [100.0 * 1.001 ** i for i in range(300)])                 # steigend
    seed("DOWN", [200.0 * 0.999 ** i for i in range(300)])               # fallend
    base = [100.0 * 1.002 ** i for i in range(280)]                       # 280 Tage hoch ...
    mixed = base + [base[-1] * 0.97 ** i for i in range(1, 21)]           # ... dann 20 Tage -3 %/Tag
    seed("MIX", mixed)
    con.commit()
    tim = live_update.timing_from_db(con, ["UP", "DOWN", "MIX"])
    assert tim["UP"]["timing_status"] == "positiv"      # mom > 0 UND kurs > SMA200
    assert tim["DOWN"]["timing_status"] == "negativ"    # keines von beiden
    assert tim["MIX"]["timing_status"] == "neutral"     # mom > 0, aber kurs < SMA200
    assert tim["UP"]["mom_12_1"] > 0 and tim["UP"]["abstand_sma200"] > 0
    con.close()


# --------------------------------------------------------------------------
# (3) Smart-Money-Block: Meldungen
# --------------------------------------------------------------------------

def _sm_db(tmp_path):
    db = tmp_path / "sm.db"
    con = decisions.init_db(db)
    con.execute("INSERT INTO watchlist_status (ticker, updated_at, kurs, status_screener, status_live,"
                " mos_live_min, mos_live_robust, kaufzone_kurs, distanz_pct, timing_status,"
                " wert_veraltet, alarm) VALUES ('AAA',?,100.0,'Kandidat','Kandidat',0.3,0.2,70.0,-0.3,'positiv',0,0)",
                (HEUTE.isoformat(),))
    con.commit()
    return db, con


def test_smart_money_block_nicht_geladen(tmp_path):
    db, con = _sm_db(tmp_path)
    modus, pos, meldung = app.smart_money_block(con, "AAA", HEUTE)
    assert modus == "nicht_geladen" and pos == []
    assert "13F-Daten nicht geladen" in meldung
    con.close()


def test_smart_money_block_keine_halter_und_halter(tmp_path):
    db, con = _sm_db(tmp_path)
    con.execute("INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum,"
                " accession, cusip, ticker, name, title_of_class, shares, wert, anteil_portfolio,"
                " aktion, aenderung_pct, positions_gesamt, geladen_am)"
                " VALUES ('Berkshire Hathaway','2026-Q2','2026-06-30','2026-08-14','a1','111','ZZZ',"
                "'ZZZ','CS',1.0,100.0,0.06,'erhoeht',0.2,50,'x')")
    con.commit()
    modus, pos, meldung = app.smart_money_block(con, "AAA", HEUTE)
    assert modus == "keine_halter"
    assert "Kein Fonds haelt den Titel (13F-Periode 2026-Q2)" == meldung
    # AAA haelt Berkshire -> Halter mit Alter in Tagen
    con.execute("INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum,"
                " accession, cusip, ticker, name, title_of_class, shares, wert, anteil_portfolio,"
                " aktion, aenderung_pct, positions_gesamt, geladen_am)"
                " VALUES ('Berkshire Hathaway','2026-Q2','2026-06-30','2026-08-14','a2','222','AAA',"
                "'AAA','CS',1.0,100.0,0.06,'erhoeht',0.2,50,'x')")
    con.commit()
    modus, pos, meldung = app.smart_money_block(con, "AAA", HEUTE)
    assert modus == "halter" and meldung == ""
    r = pos[0]
    assert r["fonds"] == "Berkshire Hathaway" and r["periode"] == "2026-Q2"
    assert r["aktion"] == "erhoeht" and r["anteil_portfolio"] == 0.06
    assert r["alter_tage"] == (HEUTE - date(2026, 8, 14)).days
    con.close()


def test_apptest_seite5_smart_money_meldung(tmp_path):
    db, con = _sm_db(tmp_path)
    con.close()
    s = tmp_path / "run_page.py"
    s.write_text("import streamlit as st\nimport app\n"
                 f"st.session_state['db_path'] = {str(db)!r}\n"
                 "app.page_watchlist_live()\n", encoding="utf-8")
    at = AppTest.from_file(str(s), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    caps = " ".join(c.value for c in at.caption)
    assert "13F-Daten nicht geladen" in caps
