r"""
(v65) Audit der Scoring-Engine (criteria.py + score.py) -- Tests ohne Netz:

A. ND/EBITDA + Zinsdeckung nur aus dem SELBEN juengsten gemeinsamen Geschaeftsjahr
   (nicht Listenposition); EBITDA <= 0 -> n/a; Zinsaufwand 0/fehlend -> n/a.
B. ev_ebit_vs_hist_ebit: aktuelles EV je historischem EBIT (keine hist. Marktkaps).
D. CAGR/Slope jahrbewusst (Luecken zaehlen); negative/zero Start/End -> NaN.
E. ROIC/ROE years_available = valide Jahre.
G. DCF-Config: fehlend -> Defaults + Warning; ungueltig -> ValueError.
score.py: value_coverage separat + ungerundete Schwellen; Missing-Semantik
   (is_na/None/NaN); Config-Validierung (direction/weight/full-zero/fehlend).
"""

import logging
import math

import pytest

from config import load_config
from data.loader import Metric, TickerMetrics
from scoring.criteria import (
    CriterionResult, _cagr, _cagr_pairs, _slope_pairs,
    calc_net_debt_ebitda, calc_interest_coverage, calc_roic_avg5,
    calc_revenue_cagr_5y, calc_gross_margin_trend, calc_ev_ebit_vs_hist_ebit,
    _dcf_cfg_for_criteria,
)
from scoring.score import interpolate, score_ticker, validate_criteria_cfg

NAN = float("nan")


def _cfg():
    cfg = load_config()
    cfg = dict(cfg)
    cfg["sec"] = {"enabled": False}
    return cfg


def _mk(years: dict) -> TickerMetrics:
    """years: {fy: {name: value}} -> TickerMetrics mit Metric je Feld."""
    m = TickerMetrics(ticker="AUD", country="US")
    m.market_cap = Metric("market_cap", 100e9, "USD", "USD", 2025)
    m.name = "Audit Corp"
    m.sector = "Technology"
    m.industry = "Software"
    for fy in sorted(years):
        m.years[fy] = {k: Metric(k, v, "USD", "USD", fy) for k, v in years[fy].items()}
    return m


# ---------- A: Geschaeftsjahre korrekt zuordnen ------------------------------

def test_nd_ebitda_gleiches_geschaeftsjahr_nicht_listenposition():
    cfg = _cfg()
    # net_debt nur 2023+2025, ebitda nur 2024+2025 -> gemeinsames juengstes Jahr = 2025
    m = _mk({2023: {"net_debt": 1e9}, 2024: {"ebitda": 2e9}, 2025: {"net_debt": 30e9, "ebitda": 10e9}})
    r = calc_net_debt_ebitda(m, {"thin_years": 5}, None)
    assert not r.is_na and r.value == pytest.approx(3.0)   # 30e9/10e9 aus FY2025, nicht 1e9/...
    assert r.years_available == 1                           # nur 2025 gemeinsam


def test_nd_ebitda_negatives_und_zero_ebitda_na():
    cfg = _cfg()
    m = _mk({2024: {"net_debt": 1e9, "ebitda": 5e9}, 2025: {"net_debt": 1e9, "ebitda": -2e9}})
    assert calc_net_debt_ebitda(m, {"thin_years": 5}, None).is_na     # letztes gemeinsames FY: EBITDA < 0
    m2 = _mk({2024: {"net_debt": 1e9, "ebitda": 5e9}, 2025: {"net_debt": 1e9, "ebitda": 0.0}})
    assert calc_net_debt_ebitda(m2, {"thin_years": 5}, None).is_na    # EBITDA = 0 -> n/a


def test_interest_coverage_gleiches_fy_und_zero_zins():
    cfg = _cfg()
    m = _mk({2024: {"ebit": 10e9, "interest_expense": 1e9},
             2025: {"ebit": 20e9}})                                   # 2025 kein Zins -> nur 2024
    r = calc_interest_coverage(m, {"thin_years": 5}, None)
    assert not r.is_na and r.value == pytest.approx(10.0) and r.years_available == 1
    m2 = _mk({2024: {"ebit": 10e9, "interest_expense": 0.0}, 2025: {"ebit": 20e9, "interest_expense": 2e9}})
    r2 = calc_interest_coverage(m2, {"thin_years": 5}, None)
    assert r2.value == pytest.approx(10.0)                             # FY2025: 20/2, nicht Division durch 0 aus 2024
    m3 = _mk({2025: {"ebit": 20e9, "interest_expense": 0.0}})
    assert calc_interest_coverage(m3, {"thin_years": 5}, None).is_na


# ---------- D: CAGR / Slope jahrbewusst ---------------------------------------

def test_cagr_pairs_mit_jahresluecke():
    # 2021->2025 = 4 Jahre Spanne, nicht 2 (Listenindex)
    assert _cagr_pairs([(2021, 100.0), (2025, 200.0)]) == pytest.approx(2.0 ** 0.25 - 1)
    assert _cagr_pairs([(2021, 100.0), (2022, 200.0)]) == pytest.approx(1.0)


def test_cagr_negative_und_zero_guards():
    assert _cagr_pairs([(2021, 0.0), (2025, 100.0)]) != _cagr_pairs([(2021, 0.0), (2025, 100.0)]) or True
    assert math.isnan(_cagr_pairs([(2021, 0.0), (2025, 100.0)]))       # Start 0
    assert math.isnan(_cagr_pairs([(2021, -5.0), (2025, 100.0)]))      # Start < 0
    assert math.isnan(_cagr_pairs([(2021, 100.0), (2025, -10.0)]))     # End < 0 (Vorzeichenwechsel)
    assert math.isnan(_cagr_pairs([(2021, 100.0), (2025, 0.0)]))       # End 0
    assert math.isnan(_cagr([100.0, -50.0]))                           # End <= 0 auch in _cagr
    assert math.isnan(_cagr([0.0, 100.0]))


def test_revenue_cagr_verwendet_tatsaechliche_jahre():
    m = _mk({2021: {"revenue": 100.0}, 2025: {"revenue": 200.0}})      # Luecke 2022-2024
    r = calc_revenue_cagr_5y(m, {"thin_years": 5, "cagr_min_points": 2}, None)
    assert r.value == pytest.approx(2.0 ** 0.25 - 1, rel=1e-9)          # 4 Jahre, nicht 1


def test_gross_margin_trend_mit_jahresluecke():
    # Marge 0.40 (2021) -> 0.50 (2025): Steigung 0.025/Jahr, nicht 0.05 (Index)
    m = _mk({2021: {"gross_profit": 40.0, "revenue": 100.0},
             2025: {"gross_profit": 50.0, "revenue": 100.0}})
    r = calc_gross_margin_trend(m, {"thin_years": 5, "trend_min_points": 2}, None)
    assert r.value == pytest.approx(2.5, rel=1e-9)                      # 2.5 PP/Jahr
    assert _slope_pairs([(2021, 0.4), (2025, 0.5)]) == pytest.approx(0.025)


# ---------- B: EV/EBIT Bezeichnung --------------------------------------------

def test_ev_ebit_hist_ebit_kein_historischer_median():
    m = _mk({2023: {"ebit": 10e9, "net_debt": 0.0},
             2024: {"ebit": 20e9, "net_debt": 0.0},
             2025: {"ebit": 40e9, "net_debt": 0.0}})
    r = calc_ev_ebit_vs_hist_ebit(m, {"thin_years": 5, "min_points_5y": 3}, None)
    # EV konstant (mcap 100e9) -> current/median = (100/40)/(100/20) = 0.5 = median(EBIT)/EBIT_aktuell
    assert r.value == pytest.approx(0.5)
    assert "ev_ebit_vs_hist_ebit" in __import__("scoring.criteria", fromlist=["CRITERIA"]).CRITERIA


# ---------- E: ROIC years_available --------------------------------------------

def test_roic_years_available_valide_jahre():
    years = {}
    for fy in (2021, 2022, 2023, 2024, 2025):
        years[fy] = {"ebit": 10e9, "net_income": 8e9, "income_tax_expense": 2e9,
                     "income_before_tax": 12e9, "equity": 50e9, "net_debt": 5e9}
    m = _mk(years)
    r = calc_roic_avg5(m, {"thin_years": 5, "min_points_5y": 3, "sector_rules": {}}, None)
    assert not r.is_na and r.years_available == 5


# ---------- G: DCF-Config ------------------------------------------------------

def test_dcf_cfg_fehlend_defaults_mit_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="value_screener.criteria"):
        d = _dcf_cfg_for_criteria({"data": {}})      # kein 'dcf'
    assert d["discount_rate"] == 0.09 and any("Defaults" in r.message for r in caplog.records)


def test_dcf_cfg_ungueltig_valueerror():
    with pytest.raises(ValueError, match="discount_rate"):
        _dcf_cfg_for_criteria({"dcf": {"discount_rate": -0.1, "terminal_growth": 0.025}})
    with pytest.raises(ValueError, match="Terminalwert"):
        _dcf_cfg_for_criteria({"dcf": {"discount_rate": 0.05, "terminal_growth": 0.06}})
    with pytest.raises(ValueError, match="nicht numerisch"):
        _dcf_cfg_for_criteria({"dcf": {"discount_rate": "viel"}})


# ---------- score.py: Missing, Coverage, Validierung ---------------------------

def _crit(value=NAN, is_na=False, years=5, thin=False):
    return CriterionResult(value=value, is_na=is_na, years_available=years, thin=thin)


CRIT_CFG = {
    "q1": {"block": "profitability", "weight": 0.5, "direction": "higher", "full": 1.0, "zero": 0.0},
    "q2": {"block": "cashflow", "weight": 0.5, "direction": "higher", "full": 1.0, "zero": 0.0},
    "v1": {"block": "valuation", "weight": 1.0, "direction": "higher", "full": 1.0, "zero": 0.0},
}
GROUPS = {"quality_blocks": ["profitability", "cashflow"], "value_blocks": ["valuation"],
          "weights": {"quality": 0.75, "value": 0.25},
          "min_coverage_value": 0.70, "min_coverage_quality": 0.70}


def test_value_coverage_separat_und_ungerundet():
    # q1,q2 voll (quality_coverage 1.0), v1 fehlt -> value_coverage 0 -> value n/a,
    # obwohl Gesamt-Coverage 2/3 = 0.667 (alt: wurde gegen 0.70 geprueft -> auch n/a,
    # hier testen wir den Grenzfall: value_coverage knapp unter Schwelle)
    vals = {"q1": _crit(0.5), "q2": _crit(0.5), "v1": _crit(is_na=True)}
    r = score_ticker(vals, CRIT_CFG, GROUPS)
    assert r["quality_coverage_raw"] == 1.0
    assert r["value_coverage_raw"] == 0.0
    assert math.isnan(r["value"]) and r["quality"] == pytest.approx(50.0)


def test_coverage_schwelle_ungerundet():
    # value_coverage_raw = 0.6999 -> unter 0.70 -> n/a, obwohl gerundet 0.7
    cfg = {"v1": {"block": "valuation", "weight": 0.6999, "direction": "higher", "full": 1.0, "zero": 0.0},
           "v2": {"block": "valuation", "weight": 0.3001, "direction": "higher", "full": 1.0, "zero": 0.0},
           "q1": {"block": "profitability", "weight": 1.0, "direction": "higher", "full": 1.0, "zero": 0.0}}
    groups = dict(GROUPS)
    vals = {"v1": _crit(0.5), "v2": _crit(is_na=True), "q1": _crit(0.5)}
    r = score_ticker(vals, cfg, groups)
    assert r["value_coverage_raw"] == pytest.approx(0.6999)
    assert round(r["value_coverage_raw"], 1) == 0.7          # Anzeige gerundet
    assert math.isnan(r["value"])                              # Schwelle ungerundet -> n/a
    vals2 = {"v1": _crit(0.5), "v2": _crit(0.5), "q1": _crit(0.5)}
    r2 = score_ticker(vals2, cfg, groups)
    assert r2["value"] == pytest.approx(50.0)                  # knapp drueber -> gueltig


def test_missing_semantik_na_none_nan():
    vals = {"q1": _crit(value=NAN, is_na=False),   # NaN-Wert, is_na False -> missing
            "q2": _crit(value=None),               # None -> missing
            "v1": _crit(is_na=True)}               # is_na -> missing
    r = score_ticker(vals, CRIT_CFG, GROUPS)
    assert r["coverage_raw"] == 0.0
    assert all(d["missing"] for d in r["details"].values())
    assert math.isnan(r["total"])


def test_interpolate_ungueltige_direction_raise():
    with pytest.raises(ValueError, match="direction"):
        interpolate(0.5, "sideways", 1.0, 0.0)
    assert interpolate(NAN, "higher", 1.0, 0.0) != interpolate(NAN, "higher", 1.0, 0.0)  # NaN bleibt NaN


def test_validate_criteria_cfg_fehler():
    with pytest.raises(ValueError, match="direction"):
        validate_criteria_cfg({"x": {"block": "b", "weight": 1.0, "direction": "up", "full": 1.0, "zero": 0.0}})
    with pytest.raises(ValueError, match="weight"):
        validate_criteria_cfg({"x": {"block": "b", "weight": -0.5, "direction": "higher", "full": 1.0, "zero": 0.0}})
    with pytest.raises(ValueError, match="weight"):
        validate_criteria_cfg({"x": {"block": "b", "weight": 0, "direction": "higher", "full": 1.0, "zero": 0.0}})
    with pytest.raises(ValueError, match="full > zero"):
        validate_criteria_cfg({"x": {"block": "b", "weight": 1.0, "direction": "higher", "full": 0.0, "zero": 0.0}})
    with pytest.raises(ValueError, match="fehlende Kriterien-Konfiguration"):
        score_ticker({"unbekannt": _crit(0.5)}, CRIT_CFG, GROUPS)


def test_block_gewichtet_mit_kriteriengewichten():
    # Block A: 2 Kriterien je 0.1; Block B: 1 Kriterium 0.8 -> B dominiert (nicht 50/50)
    cfg = {"a1": {"block": "A", "weight": 0.1, "direction": "higher", "full": 1.0, "zero": 0.0},
           "a2": {"block": "A", "weight": 0.1, "direction": "higher", "full": 1.0, "zero": 0.0},
           "b1": {"block": "B", "weight": 0.8, "direction": "higher", "full": 1.0, "zero": 0.0}}
    groups = {"quality_blocks": ["A", "B"], "value_blocks": [], "weights": {"quality": 1.0},
              "min_coverage_value": 0.0, "min_coverage_quality": 0.0}
    vals = {"a1": _crit(1.0), "a2": _crit(1.0), "b1": _crit(0.0)}   # A=100, B=0
    r = score_ticker(vals, cfg, groups)
    # gewichtet: (0.2*100 + 0.8*0)/1.0 = 20 (alt gleichgewichtet: 50)
    assert r["quality"] == pytest.approx(20.0)
