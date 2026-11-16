"""Tests fuer v66: DCF bei unbekannter Nettoverschuldung (Audit-Punkt 2).

Fachregel: Eine unbekannte (NaN), nicht vollstaendig berechenbare oder nicht
in die Marktkapitalisierungs-Waehrung konvertierbare Nettoverschuldung wird
NIEMALS wie null behandelt. Der DCF ist dann nicht belastbar: innerer Wert
je Aktie, MoS, Bear/Base/Bull und Sensitivitaet sind NaN; kein Kaufkandidat.
Ein bekannter Wert von exakt 0 ist dagegen ein valider bekannter Wert
(Equity = EV) und ausdruecklich von NaN zu unterscheiden.

Handgerechnet (siehe test_dcf_modes.py): EV = 1569.00 bei FCF 100,
Wachstum 5 %, Diskont 10 %, g 2,5 %, 10 Aktien.
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from data.loader import Metric, TickerMetrics
from scoring.pipeline import evaluate_ticker
from valuation.dcf import compute_dcf, is_buy_candidate, sensitivity_mos_base


DCF_CFG = {"dcf": {
    "discount_rate": 0.10,
    "terminal_growth": 0.025,
    "base_growth_cap": 0.06,
    "bull_growth_multiplier": 1.3,
    "bull_growth_cap": 0.10,
    "fade_to_growth": 0.025,
    "fcf_avg_years": 3,
    "fcf_mode": "auto",
    "buy_quality_min": 60,
    "buy_margin_of_safety_min": 0.30,
    "buy_bear_floor": 0.80,
    "watch_margin_of_safety_min": 0.30,
    "net_debt_mode": "reported",
}}


def _ticker(net_debt=..., total_debt=..., cash=..., mcap_cur="USD",
            nd_cur="USD") -> TickerMetrics:
    """Synthetischer Ticker; Bilanz-Felder nur, wenn explizit gegeben
    (net_debt=... = Feld fehlt komplett -> unbekannt)."""
    m = TickerMetrics(ticker="TEST")
    m.market_cap = Metric("market_cap", 1000.0, mcap_cur, mcap_cur, 2025)
    for i, fy in enumerate((2023, 2024, 2025)):
        y = {
            "fcf_sbc": Metric("fcf_sbc", 100.0, "USD", "USD", fy),
            "revenue": Metric("revenue", 1000.0 * 1.05 ** i, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 10.0, "shares", "USD", fy),
        }
        if net_debt is not ...:
            y["net_debt"] = Metric("net_debt", net_debt, nd_cur, nd_cur, fy)
        if total_debt is not ...:
            y["_total_debt"] = Metric("_total_debt", total_debt, nd_cur, nd_cur, fy)
        if cash is not ...:
            y["_cash"] = Metric("_cash", cash, nd_cur, nd_cur, fy)
        m.years[fy] = y
    return m


# ---------- (1) Bekannte Nettoverschuldung von exakt null ----------

def test_known_zero_net_debt_normal_dcf():
    res = compute_dcf(_ticker(net_debt=0.0), DCF_CFG)
    assert res.net_debt == 0.0
    assert res.net_debt_known is True
    assert res.net_debt_status == "bekannt_null"
    base = res.scenarios["base"]
    assert base.value_per_share == pytest.approx(156.90, abs=1e-2)
    assert res.margin_of_safety == pytest.approx(1.0 - 100.0 / 156.90, abs=1e-3)


# ---------- (2) Bekannte positive Nettoverschuldung ----------

def test_known_positive_net_debt_subtracts():
    res = compute_dcf(_ticker(net_debt=200.0), DCF_CFG)
    assert res.net_debt_known is True
    assert res.net_debt_status == "bekannt_positiv"
    assert res.scenarios["base"].value_per_share == pytest.approx(136.90, abs=1e-2)


# ---------- (3) Bekannte negative Nettoverschuldung (Nettoliquiditaet) ----------

def test_known_negative_net_debt_adds():
    res = compute_dcf(_ticker(net_debt=-300.0), DCF_CFG)
    assert res.net_debt_known is True
    assert res.net_debt_status == "bekannt_negativ"
    assert res.scenarios["base"].value_per_share == pytest.approx(186.90, abs=1e-2)


# ---------- (4) Unbekannte Nettoverschuldung (NaN) ----------

def test_unknown_net_debt_dcf_not_bearable():
    res = compute_dcf(_ticker(), DCF_CFG)
    assert math.isnan(res.net_debt)
    assert res.net_debt_known is False
    assert res.net_debt_status == "unbekannt"
    for name in ("bear", "base", "bull"):
        assert math.isnan(res.scenarios[name].value_per_share)
    assert math.isnan(res.margin_of_safety)
    # Kein Kaufkandidat trotz hoher Quality und guenstigem Kurs
    assert not is_buy_candidate(90.0, res, 50.0, DCF_CFG)


# ---------- (5) Fehlende Bilanzdaten ----------

def test_missing_balance_data_same_as_unknown():
    """Kein net_debt, kein total_debt, kein cash -> Status unbekannt, DCF n/a."""
    res = compute_dcf(_ticker(), DCF_CFG)
    assert res.net_debt_source == "n/a"
    assert res.net_debt_status == "unbekannt"
    assert math.isnan(res.margin_of_safety)


# ---------- (6) Unvollstaendige Bilanzdaten ----------

def test_incomplete_balance_data_no_partial_guess():
    """total_debt bekannt, cash fehlt -> keine Teilberechnung (Schulden ohne
    Cash-Abzug), sondern Status unvollstaendig und DCF n/a. Genau hier
    waere frueher still ND=0 (EV ohne Abzug) als Wert erschienen."""
    res = compute_dcf(_ticker(total_debt=500.0), DCF_CFG)
    assert math.isnan(res.net_debt)
    assert res.net_debt_known is False
    assert res.net_debt_status == "unvollstaendig"
    for name in ("bear", "base", "bull"):
        assert math.isnan(res.scenarios[name].value_per_share)
    assert math.isnan(res.margin_of_safety)


# ---------- (7) FX-Konvertierung nicht moeglich ----------

def test_fx_failure_dcf_not_bearable():
    """Nettoverschuldung in EUR, Marktkapitalisierung in USD, kein FX-
    Konverter (_NoFX) -> Konvertierung schlaegt fehl -> DCF n/a (frueher:
    still EV ohne Schuldenabzug = kuenklich erhoehter Wert)."""
    m = _ticker(net_debt=200.0, mcap_cur="USD", nd_cur="EUR")
    res = compute_dcf(m, DCF_CFG)
    assert res.net_debt == 200.0  # Berichtswaehrung bekannt
    assert res.net_debt_known is False
    assert res.net_debt_status == "fx_fehlgeschlagen"
    for name in ("bear", "base", "bull"):
        assert math.isnan(res.scenarios[name].value_per_share)
    assert math.isnan(res.margin_of_safety)
    assert not is_buy_candidate(90.0, res, 50.0, DCF_CFG)


# ---------- (8) Alternative Nettoverschuldung ----------

def test_alt_net_debt_nan_when_unknown():
    """Aktiv 'reported' mit gemeldeter ND, aber keine Schulden/Cash/Anlagen
    -> alternative ND (incl_sti) unbekannt -> mos_base_alt NaN (unveraendert
    korrektes Verhalten)."""
    res = compute_dcf(_ticker(net_debt=200.0), DCF_CFG)
    assert res.net_debt_known is True
    # incl_sti ohne Schulden/Cash/Anlagen faellt auf die gemeldete ND zurueck
    assert res.net_debt_alt == pytest.approx(200.0)
    assert res.mos_base_alt == pytest.approx(1.0 - 100.0 / 136.90, abs=1e-3)


def test_alt_net_debt_computed_when_known():
    res = compute_dcf(_ticker(net_debt=200.0, total_debt=500.0, cash=100.0), DCF_CFG)
    assert res.net_debt_alt == pytest.approx(400.0)
    assert res.mos_base_alt == res.mos_base_alt  # berechenbar, nicht NaN


# ---------- (9) Sensitivitaetsanalyse ----------

def test_sensitivity_all_nan_when_net_debt_unknown():
    """Bei unbekannter Nettoverschuldung darf die Sensitivitaetstabelle keine
    scheinbar gueltigen Werte anzeigen (frueher: EV-basierte vps/mos)."""
    sens = sensitivity_mos_base(_ticker(), DCF_CFG)
    assert sens["mos"]["mos"].isna().all()
    assert sens["vps"]["value_per_share"].isna().all()


def test_sensitivity_valid_when_net_debt_known():
    m = _ticker(net_debt=200.0)
    sens = sensitivity_mos_base(m, DCF_CFG)
    assert sens["mos"]["mos"].notna().all()
    # Mitte (r=10 %, g=2,5 %) == compute_dcf Base
    res = compute_dcf(m, DCF_CFG)
    mid = sens["mos"][(sens["mos"]["discount_rate"] == 0.10)
                      & (sens["mos"]["terminal_growth"] == 0.025)]
    assert mid["value_per_share"].iloc[0] == pytest.approx(
        res.scenarios["base"].value_per_share, abs=1e-6)


# ---------- (10) Kaufkandidatentscheidung / Pipeline ----------

def test_no_buy_candidate_with_unknown_net_debt(aapl_metrics, cfg):
    """Ende-zu-Ende: AAPL-Demo ohne jegliche Bilanz-Schulden-Daten -> DCF n/a,
    Status 'nein', Begrundung 'Nettoverschuldung unbekannt' (Datenqualitaet,
    kein Qualitaetsurteil), Flag gesetzt, iv_* NaN, kein Kaufkandidat."""
    m = aapl_metrics
    for fy in list(m.years):
        for key in ("net_debt", "_total_debt", "_cash", "_short_term_investments"):
            m.years[fy].pop(key, None)
    res = evaluate_ticker(m, cfg)
    row = res["row"]
    assert row is not None
    assert row["dcf_net_debt_known"] is False
    assert row["dcf_net_debt_status"] == "unbekannt"
    assert "wert:nd_unbekannt" in row["flags"]
    assert math.isnan(row["margin_of_safety"])
    assert math.isnan(row["iv_base"])
    assert math.isnan(row["iv_bear"])
    assert math.isnan(row["iv_bull"])
    assert row["kandidat"] != "Kandidat"
    assert row["is_buy"] is False
    # Begrundung: Datenqualitaet, nicht Qualitaetsmangel
    assert "Nettoverschuldung unbekannt" in row["status_detail"]
    assert "Datenqualit\u00e4t" in row["status_detail"]
    # Hinweis: Quality kann unabhaengig n/a sein (z. B. ROIC benoetigt
    # Nettoverschuldung) -- die ND-Begruendung selbst ist aber als
    # Datenqualitaetsproblem formuliert, nicht als Qualitaetsurteil.


# ---------- (11) Kein Legacy-Modus ----------

def test_no_legacy_ev_mode_config_switch():
    """Es gibt bewusst KEINEN Schalter, um unbekannte Nettoverschuldung wie
    null zu behandeln: selbst ein gesetztes dcf.net_debt_missing='ev' (aus
    aelteren Vorschlaegen) aendert das Fachverhalten nicht."""
    cfg_legacy = {"dcf": dict(DCF_CFG["dcf"], net_debt_missing="ev")}
    res = compute_dcf(_ticker(), cfg_legacy)
    assert res.net_debt_known is False
    for name in ("bear", "base", "bull"):
        assert math.isnan(res.scenarios[name].value_per_share)
    assert math.isnan(res.margin_of_safety)


# ---------- (12) Bekannt 0 vs. NaN unterscheidbar ----------

def test_known_zero_distinct_from_nan():
    r0 = compute_dcf(_ticker(net_debt=0.0), DCF_CFG)
    rna = compute_dcf(_ticker(), DCF_CFG)
    assert r0.net_debt_known is True and r0.net_debt_status == "bekannt_null"
    assert rna.net_debt_known is False and rna.net_debt_status == "unbekannt"
    assert r0.margin_of_safety == r0.margin_of_safety
    assert math.isnan(rna.margin_of_safety)
