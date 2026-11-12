r"""
(v61) Plausibilitaet der DCF-Werte (Lauf v55, Diagnose + Sanity-Guard; Tests ohne Netz):

(1) Regression FCF-Yield-Bug: Mio.-Skalierung der Marktkap. betraf NUR fcf_yield
    (run_ranking.py::main, AAPL-Vorher/Nachher-Block); Start-FCF, Wert je Aktie,
    MoS, Zinsdeckung und ND/EBITDA hängen nicht von der Marktkap.-Skalierung ab.
(2) classify_mos_case: Einheitenfehler / wert_nahe_null / datenluecke / ok.
(3) Sanity-Guard: sanity_mos_flags -> mos:extrem (< -10) und wert:nahe_null (< 1 %
    des Kurses); Anzeige "< -1000 %" in Tabellen, Rohwert bleibt in der CSV.
"""

import csv
import math
from copy import deepcopy

import pytest

import app
import run_ranking
from config import load_config
from data.demo import demo_companies
from data.metrics import fcf_yield
from data.sanity import classify_mos_case
from scoring.pipeline import evaluate_all, sanity_mos_flags
from valuation.dcf import compute_dcf

NAN = float("nan")


def _cfg():
    return load_config()


def _qual():
    return next(m for m in demo_companies() if m.ticker == "QUAL")


# --------------------------------------------------------------------------
# (1) Regression: Mio.-Skalierung der Marktkap. betrifft nur fcf_yield
# --------------------------------------------------------------------------

def test_mio_skalierung_betrifft_fcf_yield_und_kurs_nicht_dcf_wert_nd_zins():
    m = _qual()
    cfg = _cfg()
    latest_fy = max(m.years)
    base = compute_dcf(m, cfg)
    m_bug = deepcopy(m)
    m_bug.market_cap.value = m_bug.market_cap.value / 1e6   # Mio.-Skalierung (Bug)
    buggy = compute_dcf(m_bug, cfg)
    # Wert je Aktie (DCF: FCF + ND + Aktien), ND, Aktien: UNVERAENDERT -- DCF nutzt keine Marktkap.
    assert buggy.scenarios["base"].value_per_share == pytest.approx(base.scenarios["base"].value_per_share)
    assert buggy.net_debt == pytest.approx(base.net_debt)
    assert buggy.shares == pytest.approx(base.shares)
    # ND/EBITDA und Zinsdeckung kommen aus den Statements, nicht aus der Marktkap.
    ym, yb = m.years[latest_fy], m_bug.years[latest_fy]
    assert yb["net_debt"].value / yb["ebitda"].value == pytest.approx(
        ym["net_debt"].value / ym["ebitda"].value)
    assert yb["ebit"].value / abs(yb["interest_expense"].value) == pytest.approx(
        ym["ebit"].value / abs(ym["interest_expense"].value))
    # Start-FCF (Statements) unveraendert
    assert buggy.fcf_normalized == pytest.approx(base.fcf_normalized)
    # BETROFFEN: fcf_yield (Nenner Marktkap.) skaliert mit 1e6 -- das war der Bug
    assert fcf_yield(m_bug, latest_fy) == pytest.approx(fcf_yield(m, latest_fy) * 1e6)
    # BETROFFEN: Kurs = Marktkap. / Aktien (pipeline) -> 1e6 zu klein, daher MoS falsch
    assert (m_bug.market_cap.value / buggy.shares) == pytest.approx((m.market_cap.value / base.shares) / 1e6)


# --------------------------------------------------------------------------
# (2) Klassifikation extremer MoS
# --------------------------------------------------------------------------

def test_classify_mos_case():
    # Wert knapp ueber null: iv_base < 1 % des Kurses -> MoS ~ -1e6 %
    r = classify_mos_case(kurs=100.0, iv_base=0.005)
    assert r["verdict"] == "wert_nahe_null"
    # Einheitenfehler: FCF in Mio., ND absolut
    r = classify_mos_case(kurs=100.0, iv_base=50.0, fcf_unit="Mio. USD", nd_unit="USD")
    assert r["verdict"] == "einheitenfehler"
    # Datenluecke
    r = classify_mos_case(kurs=NAN, iv_base=50.0)
    assert r["verdict"] == "datenluecke"
    r = classify_mos_case(kurs=100.0, iv_base=NAN)
    assert r["verdict"] == "datenluecke"
    # plausibel (MoS extrem wegen hohem Kurs, aber Einheiten ok und Wert > 1 %)
    r = classify_mos_case(kurs=1000.0, iv_base=50.0, net_debt=1e9, shares=1e8)
    assert r["verdict"] == "ok"


# --------------------------------------------------------------------------
# (3) Sanity-Guard: Flags, Anzeige, CSV-Rohwert
# --------------------------------------------------------------------------

def test_sanity_mos_flags():
    cfg = _cfg()
    assert sanity_mos_flags(-14.5, 0.005, 100.0, cfg) == ["mos:extrem", "wert:nahe_null"]
    assert sanity_mos_flags(-9.5, 50.0, 100.0, cfg) == []          # -950 % noch nicht extrem
    assert sanity_mos_flags(0.2, 0.005, 100.0, cfg) == ["wert:nahe_null"]  # iv < 1 % kurs, MoS ok
    assert sanity_mos_flags(NAN, NAN, NAN, cfg) == []
    # Grenzen aus config sanity
    cfg2 = {"sanity": {"mos_extrem_min": -5.0, "wert_nahe_null_anteil": 0.02}}
    assert sanity_mos_flags(-6.0, 50.0, 100.0, cfg2) == ["mos:extrem"]


def test_anzeige_und_csv_rohwert(tmp_path):
    row = {"ticker": "LLY", "margin_of_safety": -14517.678, "flags": "mos:extrem"}
    # Tabelle: "< -1000 %"
    assert run_ranking._fmt_mos_cell(row) == "< -1000 %"
    assert app._fmt_mos(-14517.678, extrem=True).replace("\u2212", "-") == "< -1000 %"
    # ohne Flag: normal formatiert
    assert run_ranking._fmt_mos_cell({"margin_of_safety": -14.5, "flags": ""}) == "-1450.0%"
    # CSV: Rohwert bleibt
    out = tmp_path / "r.csv"
    run_ranking._write_ranking_csv([row], [], [], str(out))
    back = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
    assert float(back[0]["margin_of_safety"]) == pytest.approx(-14517.678)
    assert "mos:extrem" in back[0]["flags"]


def test_evaluate_all_setzt_flags_bei_extremem_mos():
    # synthetischer Ticker mit riesiger Nettoverschuldung -> Wert je Aktie nahe null
    from data.demo import _synthetic
    y = {}
    for i, fy in enumerate(range(2021, 2026)):
        rev = 100e9 * (1.05 ** i)
        y[fy] = dict(revenue=rev, gross_profit=0.5 * rev, ebit=0.2 * rev, ebitda=0.25 * rev,
                     net_income=0.1 * rev, interest=0.1e9, tax=0.03 * rev, shares=1e9,
                     equity=50e9, net_debt=1e12, ocf=0.15 * rev, capex=0.03 * rev, sbc=0.0)
    m = _synthetic("EXT", "Extreme ND Corp", "Industrials", "US", 100e9, y)
    cfg = _cfg()
    rows, _excl = evaluate_all([m], cfg)
    r = rows[0]
    assert "mos:extrem" in (r["flags"] or "") or "wert:nahe_null" in (r["flags"] or "")
    if "wert:nahe_null" in r["flags"]:
        assert r["iv_base"] < 0.01 * r["kurs"]
