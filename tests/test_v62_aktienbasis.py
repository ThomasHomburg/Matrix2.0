r"""
(v62) Aktienbasis / Live-Konsistenz / Benchmark (Tests ohne Netz):

- share_basis current vs fy_diluted: MoS identisch (Kurs und iv_* auf aktien_aktuell);
  Ranking-Row speichert kurs_boerse, kurs_marktkap_basis, aktien_dcf, aktien_aktuell.
- Altbestand: migrate_share_basis rechnet iv_aktuell = iv_base * kurs_boerse /
  kurs_marktkap_basis, Flag 'umgerechnet', decisions datum share_basis_fix.datum ->
  hinweis; keine Zeile geloescht/ueberschrieben.
- Konsistenz: |mos_live_min - mos_rule| < 0.5 Pp am Ranking-Tag; konsistenz_warnung
  bei > 2 Pp.
- Benchmark: snapshot --korb benchmark ohne Benchmark-Kurs -> Abbruch; mit Kurs -> 2 Zeilen.
"""

import argparse
import copy
import sqlite3
from datetime import date
from pathlib import Path

import pytest

import decisions
import live_update
from config import load_config
from data.demo import demo_companies
from data.loader import Metric
from scoring.pipeline import evaluate_all
from valuation.dcf import compute_dcf

HEUTE = date(2026, 11, 12)


def _cfg():
    return load_config()


def _qual(price=250.0):
    m = next(x for x in demo_companies() if x.ticker == "QUAL")
    m.price = Metric("price", price, "USD", "USD", 2025)
    return m


# --------------------------------------------------------------------------
# (2) share_basis: MoS identisch, Row-Felder
# --------------------------------------------------------------------------

def test_share_basis_current_mos_identisch_zu_fy_diluted():
    m = _qual(price=250.0)          # mcap/shares = 200 -> Boersen-Kurs 250 weicht ab
    cfg_cur = copy.deepcopy(_cfg()); cfg_cur["dcf"]["share_basis"] = "current"
    cfg_fy = copy.deepcopy(_cfg()); cfg_fy["dcf"]["share_basis"] = "fy_diluted"
    d_cur = compute_dcf(m, cfg_cur)
    d_fy = compute_dcf(m, cfg_fy)
    assert d_cur.price_basis == "current" and d_fy.price_basis == "fy_diluted"
    # aktien_aktuell = Marktkap. / kurs_boerse
    assert d_cur.shares_current == pytest.approx(m.market_cap.value / 250.0)
    assert d_cur.kurs_boerse == 250.0
    # MoS identisch (Screener-MoS unberuehrt): 1 - mcap/equity
    assert d_cur.margin_of_safety == pytest.approx(d_fy.margin_of_safety)
    # aber Wert je Aktie unterscheidet sich (andere Aktienzahl)
    assert d_cur.scenarios["base"].value_per_share != pytest.approx(
        d_fy.scenarios["base"].value_per_share)


def test_ranking_row_aktienbasis_felder():
    m = _qual(price=250.0)
    cfg = copy.deepcopy(_cfg()); cfg["dcf"]["share_basis"] = "current"
    rows, _ = evaluate_all([m], cfg)
    r = rows[0]
    assert r["kurs_boerse"] == 250.0
    assert r["kurs_marktkap_basis"] == pytest.approx(m.market_cap.value / r["aktien_dcf"])
    assert r["kurs"] == pytest.approx(r["kurs_marktkap_basis"])   # Screener-Kurs unveraendert
    assert r["aktien_aktuell"] == pytest.approx(m.market_cap.value / 250.0)


# --------------------------------------------------------------------------
# (3) Altbestand: migrate_share_basis
# --------------------------------------------------------------------------

def test_migrate_share_basis_altbestand(tmp_path):
    db = tmp_path / "d.db"
    con = decisions.init_db(db)          # ensure_schema legt aktien_aktuell/iv_aktuell/flags/hinweis an
    con.execute("INSERT INTO valuations (ticker, calc_date, kurs, status, iv_base, iv_bear, iv_bull)"
                " VALUES ('AAA','2026-11-10',120.0,'Kandidat',150.0,80.0,200.0)")
    con.execute("INSERT INTO decisions (ticker, datum, aktion, korb, kurs, iv_base, iv_bear, iv_bull)"
                " VALUES ('AAA','2026-11-11','auto','screener',120.0,150.0,80.0,200.0)")
    con.execute("INSERT INTO decisions (ticker, datum, aktion, korb, kurs, iv_base, iv_bear, iv_bull)"
                " VALUES ('BBB','2026-08-20','auto','screener',50.0,90.0,60.0,120.0)")
    con.commit()
    rows_by_t = {"AAA": {"ticker": "AAA", "kurs_boerse": "250.0", "kurs_marktkap_basis": "200.0",
                         "aktien_aktuell": "1600000000"}}
    n = live_update.migrate_share_basis(con, _cfg(), rows_by_t)
    assert n == 2                          # valuations AAA + decisions AAA (BBB ohne ranking-Zeile)
    con.row_factory = sqlite3.Row
    v = con.execute("SELECT * FROM valuations WHERE ticker='AAA'").fetchone()
    assert v["iv_aktuell"] == pytest.approx(150.0 * 250.0 / 200.0)
    assert v["aktien_aktuell"] == pytest.approx(1.6e9)
    assert "umgerechnet" in (v["flags"] or "")
    d = con.execute("SELECT * FROM decisions WHERE ticker='AAA'").fetchone()
    assert d["iv_aktuell"] == pytest.approx(150.0 * 250.0 / 200.0)
    assert "umgerechnet" in (d["flags"] or "")
    assert d["hinweis"] == "Aktienbasis-Fix: p, EW und Kaufzone ungueltig, Rendite-Start unveraendert"
    d2 = con.execute("SELECT * FROM decisions WHERE ticker='BBB'").fetchone()
    assert d2["hinweis"] is None           # anderes Datum -> kein Hinweis
    assert d2["iv_aktuell"] is None         # keine ranking-Zeile -> nicht umgerechnet
    assert con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2   # nichts geloescht
    con.close()


# --------------------------------------------------------------------------
# (4) Konsistenz am Ranking-Tag
# --------------------------------------------------------------------------

def test_konsistenz_mos_live_vs_mos_rule_am_ranking_tag():
    cfg = _cfg()
    # ranking-Zeile wie mit share_basis=current: iv_base = equity/aktien_aktuell,
    # mos_rule = 1 - kurs_boerse/iv_base
    kurs_boerse = 100.0
    iv_base = 150.0
    row = {"ticker": "AAA", "status": "Kandidat", "quality": 70, "kurs": 120.0,
           "kurs_boerse": kurs_boerse, "kurs_marktkap_basis": 120.0,
           "iv_base": iv_base, "iv_bear": 80.0, "iv_bull": 200.0,
           "iv_avg3": iv_base, "iv_latest": iv_base, "iv_ttm": iv_base, "iv_ttm_only": iv_base,
           "iv_base_10pct": iv_base * 0.9, "mos_rule": 1 - kurs_boerse / iv_base}
    r = live_update.live_row(row, kurs_boerse, cfg, heute=HEUTE)   # Live-Kurs = Boersen-Kurs
    assert abs(r["mos_live_min"] - row["mos_rule"]) < 0.005         # < 0.5 Pp
    assert live_update.konsistenz_warnung("AAA", r["mos_live_min"], row["mos_rule"]) is None
    # Abweichung > 2 Pp -> Warnung
    r2 = live_update.live_row(row, 200.0, cfg, heute=HEUTE)
    w = live_update.konsistenz_warnung("AAA", r2["mos_live_min"], row["mos_rule"])
    assert w and "> 2 Pp" in w


# --------------------------------------------------------------------------
# (5) Benchmark: snapshot ohne/mit Benchmark-Kurs
# --------------------------------------------------------------------------

def _ranking(tmp_path):
    p = tmp_path / "ranking.csv"
    p.write_text("ticker,status_all,quality,mos_rule,kurs,kurs_datum,iv_bear,iv_base,iv_bull\n"
                 "AAA,Kandidat,70,0.33,100.0,2026-11-12,80.0,150.0,200.0\n", encoding="utf-8")
    return str(p)


def _args(db, ranking, datum):
    return argparse.Namespace(db=str(db), ranking=ranking, korb="benchmark", datum=datum,
                              overlap=None, live=None)


def test_snapshot_benchmark_ohne_kurs_bricht_ab(tmp_path):
    db = tmp_path / "d.db"
    decisions.init_db(db).close()
    with pytest.raises(SystemExit, match="kein Benchmark-Kurs"):
        decisions.cmd_snapshot(_args(db, _ranking(tmp_path), "2026-11-12"))
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM decisions WHERE korb='benchmark'").fetchone()[0] == 0
    con.close()


def test_snapshot_benchmark_mit_kurs_2_zeilen(tmp_path):
    db = tmp_path / "d.db"
    con = decisions.init_db(db)
    for t in ("SPY", "RSP"):
        con.execute("INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am)"
                    " VALUES (?,'2026-11-12',500.0,'fixture','x')", (t,))
    con.commit(); con.close()
    decisions.cmd_snapshot(_args(db, _ranking(tmp_path), "2026-11-12"))
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT ticker, kurs, kurs_datum FROM decisions WHERE korb='benchmark'"
                       ).fetchall()
    assert {r["ticker"] for r in rows} == {"SPY", "RSP"}
    assert all(r["kurs"] == 500.0 and r["kurs_datum"] == "2026-11-12" for r in rows)
    con.close()
