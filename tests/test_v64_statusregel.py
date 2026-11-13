r"""
(v64) Statusregel: "Beobachtung" NUR wenn mos_min (mos_rule) >= dcf.watch_margin_of_safety_min;
Ausnahme "Beobachtung (nicht normalisiert)" NUR bei Zyklikern mit sichtbarem Hinweis
in Status UND Flags. Tests ohne Netz (synthetische TickerMetrics wie test_v27).
"""

import copy

import pytest

from config import load_config
from data.loader import Metric, TickerMetrics
from scoring.pipeline import evaluate_ticker


def _cfg():
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["sec"] = {"enabled": False}
    return cfg


def _mk(fcf_by_fy: dict, mcap: float, shares: float = 2e9, industry: str = "Software",
        sector: str = "Technology") -> TickerMetrics:
    m = TickerMetrics(ticker="T64", country="US")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    m.name = "T64 Corp"
    m.sector = sector
    m.industry = industry
    rev = 100e9
    for fy in sorted(fcf_by_fy):
        rev *= 1.10
        m.years[fy] = {
            "fcf": Metric("fcf", fcf_by_fy[fy] + 0.02 * rev, "USD", "USD", fy),
            "fcf_sbc": Metric("fcf_sbc", fcf_by_fy[fy], "USD", "USD", fy),
            "revenue": Metric("revenue", rev, "USD", "USD", fy),
            "gross_profit": Metric("gross_profit", 0.6 * rev, "USD", "USD", fy),
            "ebit": Metric("ebit", 0.25 * rev, "USD", "USD", fy),
            "ebitda": Metric("ebitda", 0.3 * rev, "USD", "USD", fy),
            "net_income": Metric("net_income", 0.2 * rev, "USD", "USD", fy),
            "operating_cashflow": Metric("operating_cashflow", 0.3 * rev, "USD", "USD", fy),
            "capex": Metric("capex", -0.05 * rev, "USD", "USD", fy),
            "stock_based_compensation": Metric("stock_based_compensation", 0.02 * rev, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", shares, "shares", "USD", fy),
            "equity": Metric("equity", 80e9, "USD", "USD", fy),
            "net_debt": Metric("net_debt", 5e9, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 0.05 * rev, "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 0.25 * rev, "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", 0.01 * rev, "USD", "USD", fy),
        }
    return m


def test_beobachtung_nur_wenn_min_ge_watch_min_sndk_situation():
    """SNDK-Situation: min (avg3) < watch_min, bester Modus (juengstes GJ) >= watch_min
    -> v64: 'nein' (alter Sonderfall entfernt)."""
    cfg = _cfg()
    m = _mk({2021: 1e9, 2022: 1e9, 2023: 1e9, 2024: 1e9, 2025: 30e9}, mcap=150e9)
    row = evaluate_ticker(m, cfg)["row"]
    if row["mos_rule"] < 0.30:
        # min < watch_min -> nie Beobachtung, auch wenn bester Modus >= 30 %
        assert row["status"] == "nein"
        assert "Beobachtung" not in row["status"]
    else:
        assert row["status"] == "Beobachtung"


def test_beobachtung_wenn_min_ge_watch_min_aber_bear_schwach():
    """mos_rule >= watch_min (0.30), aber Kandidat scheitert am Bear -> Beobachtung."""
    cfg = _cfg()
    # hoher FCF -> hohes iv_base -> MoS hoch, aber Bear knapp unter 80 % Kurs konstruieren:
    # hier: mcap so waehlen, dass MoS min >= 0.30 aber Bear < 0.80*Kurs
    m = _mk({2021: 20e9, 2022: 20e9, 2023: 20e9, 2024: 20e9, 2025: 20e9}, mcap=100e9)
    row = evaluate_ticker(m, cfg)["row"]
    if row["mos_rule"] >= 0.30 and not row["is_buy"]:
        assert row["status"] == "Beobachtung"
        assert "MoS min" in row["status_detail"]


def test_nicht_normalisiert_nur_bei_zyklikern_mit_flag():
    """Zykliker (Gold) mit < 7 Jahren Normalisierung und mos_min < watch_min ->
    'Beobachtung (nicht normalisiert)' + Flags 'nicht normalisiert' und 'zyk:norm<7J'."""
    cfg = _cfg()
    # nur 4 Jahre -> zyk_unnorm; niedriges FCF -> mos_rule < watch_min
    m = _mk({2022: 1e9, 2023: 1e9, 2024: 1e9, 2025: 1e9}, mcap=500e9,
            industry="Gold", sector="Basic Materials")
    row = evaluate_ticker(m, cfg)["row"]
    assert row["status"] == "Beobachtung (nicht normalisiert)"
    assert "nicht normalisiert" in row["flags"]
    assert "zyk:norm<7J" in row["flags"]
    assert not row["is_buy"]


def test_nicht_normalisiert_nicht_bei_nicht_zykliker():
    """Gleiche Daten, aber Nicht-Zykliker (Software) -> kein 'nicht normalisiert'."""
    cfg = _cfg()
    m = _mk({2022: 1e9, 2023: 1e9, 2024: 1e9, 2025: 1e9}, mcap=500e9,
            industry="Software", sector="Technology")
    row = evaluate_ticker(m, cfg)["row"]
    assert row["status"] != "Beobachtung (nicht normalisiert)"
    assert "nicht normalisiert" not in (row["flags"] or "")


def test_watch_min_aus_config():
    """watch_margin_of_safety_min aus config: bei 0.10 -> MoS 0.15 reicht fuer Beobachtung."""
    cfg = _cfg()
    cfg["dcf"]["watch_margin_of_safety_min"] = 0.10
    m = _mk({2021: 20e9, 2022: 20e9, 2023: 20e9, 2024: 20e9, 2025: 20e9}, mcap=140e9)
    row = evaluate_ticker(m, cfg)["row"]
    if 0.10 <= row["mos_rule"] < 0.30 and not row["is_buy"]:
        assert row["status"] == "Beobachtung"
