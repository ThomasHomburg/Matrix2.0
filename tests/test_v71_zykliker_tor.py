r"""
(v71) Zykliker-Status-Tor: das Etikett "Beobachtung (nicht normalisiert)"
wird nur vergeben, wenn das Kandidatenregel-MoS (mos_rule) die Beobachtungs-
schwelle dcf.watch_margin_of_safety_min (0.30) erreicht. Unterhalb (oder
MoS n/a) bleibt es beim bisherigen Status (i. d. R. "nein"); is_buy=False
und die Flags "nicht normalisiert" / "zyk:norm<7J" bleiben als Hinweis.
Tests ohne Netz (synthetische TickerMetrics, Helfer wie test_v64).
"""

import copy
import math

from config import load_config
from data.loader import Metric, TickerMetrics
from scoring.pipeline import evaluate_ticker


def _cfg(watch_min=None):
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["sec"] = {"enabled": False}
    if watch_min is not None:
        cfg["dcf"]["watch_margin_of_safety_min"] = watch_min
    return cfg


def _mk(fcf_by_fy: dict, mcap: float, shares: float = 2e9,
        industry: str = "Gold", sector: str = "Basic Materials") -> TickerMetrics:
    """Fixture analog test_v64._mk (4 Jahre -> zyk_unnorm bei Zykliker-Industry)."""
    m = TickerMetrics(ticker="T71", country="US")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    m.name = "T71 Corp"
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


def _flags(row: dict) -> str:
    # flags ist im Row ein leerzeichen-jointer String (keine Liste)
    return str(row.get("flags") or "")


def test_zykliker_mos_na_bleibt_bei_vorherigem_status():
    """BA-aehnlich: Zykliker (4 Jahre), MoS n/a (Start-FCF <= 0 in allen Modi)
    -> KEIN Etikett, Status 'nein'; Flags + is_buy=False bleiben."""
    m = _mk({fy: -1e9 for fy in (2022, 2023, 2024, 2025)}, mcap=1e9,
            industry="Aerospace & Defense", sector="Industrials")
    row = evaluate_ticker(m, _cfg())["row"]
    assert row["zyk_unnorm"] is True
    assert row["mos_rule"] != row["mos_rule"]  # NaN
    assert row["status"] == "nein"
    assert "nicht normalisiert" in _flags(row)
    assert "zyk:norm<7J" in _flags(row)
    assert row["is_buy"] is False


def test_zykliker_mos_stark_negativ_bleibt_beim_bisherigen_status():
    """WMT-aehnlich: Zykliker, mos_rule tief negativ -> 'nein' (v71: kein
    Etikett mehr ohne MoS-Tor), Flags bleiben, nie Kandidat."""
    m = _mk({fy: 1e9 for fy in (2022, 2023, 2024, 2025)}, mcap=500e9,
            industry="Discount Stores", sector="Consumer Defensive")
    row = evaluate_ticker(m, _cfg())["row"]
    assert row["zyk_unnorm"] is True
    assert row["mos_rule"] < -3.0  # deutlich unter -300 %, Realitaet: WMT -322 %
    assert row["status"] == "nein"
    assert "nicht normalisiert" in _flags(row)
    assert "zyk:norm<7J" in _flags(row)
    assert row["is_buy"] is False


def test_zykliker_mos_ueber_schwelle_bekommt_etikett():
    """Zykliker (4 Jahre) mit mos_rule +0.35-kategorie (hier ~0.71) ->
    'Beobachtung (nicht normalisiert)' + Flags + is_buy=False."""
    m = _mk({fy: 1e9 for fy in (2022, 2023, 2024, 2025)}, mcap=1e10)
    row = evaluate_ticker(m, _cfg())["row"]
    assert row["zyk_unnorm"] is True
    assert row["mos_rule"] >= 0.30
    assert row["status"] == "Beobachtung (nicht normalisiert)"
    assert "nicht normalisiert" in _flags(row)
    assert "zyk:norm<7J" in _flags(row)
    assert row["is_buy"] is False
    assert f"MoS min {row['mos_rule']:.1%}" in row["status_detail"]


def test_zykliker_mos_knapp_unter_schwelle_kein_etikett():
    """Grenzfall: mos_rule knapp unter 0.30 -> 'nein' (Tor verfehlt)."""
    m = _mk({fy: 1e9 for fy in (2022, 2023, 2024, 2025)}, mcap=2.466e10)
    row = evaluate_ticker(m, _cfg())["row"]
    assert 0.25 < row["mos_rule"] < 0.30  # knapp unter der Schwelle
    assert row["status"] == "nein"
    assert "nicht normalisiert" in _flags(row)
    assert "zyk:norm<7J" in _flags(row)
    assert row["is_buy"] is False


def test_zykliker_sieben_jahre_unveraendert():
    """7 Wertjahre -> Normalisierung vollstaendig -> kein zyk_unnorm,
    kein 'nicht normalisiert'-Flag, Status aus der normalen Regel."""
    m = _mk({fy: 1e9 for fy in (2019, 2020, 2021, 2022, 2023, 2024, 2025)},
            mcap=500e9)
    row = evaluate_ticker(m, _cfg())["row"]
    assert row["zyk_unnorm"] is False
    assert row["status"] != "Beobachtung (nicht normalisiert)"
    assert "nicht normalisiert" not in _flags(row)
    assert "zyk:norm<7J" not in _flags(row)


def test_nicht_zykliker_unveraendert():
    """Gleiche Daten, Nicht-Zykliker (Software) -> kein Etikett, keine
    Zykliker-Flags, identischer Statuspfad (kein Tor)."""
    m = _mk({fy: 1e9 for fy in (2022, 2023, 2024, 2025)}, mcap=1e10,
            industry="Software", sector="Technology")
    row = evaluate_ticker(m, _cfg())["row"]
    assert row["zyk_unnorm"] is False
    assert row["status"] != "Beobachtung (nicht normalisiert)"
    assert "nicht normalisiert" not in _flags(row)
    assert row["mos_rule"] == row["mos_rule"]  # MoS da (Kontext: gleiche Basis)


def test_schwelle_aus_config_keine_magic_number():
    """watch_margin_of_safety_min = 0.95 im Test-Cfg -> dieselbe Fixture wie
    test_zykliker_mos_ueber_schwelle (~0.71) verfehlt das Tor -> 'nein'.
    Beweist, dass die Schwelle aus der Config gelesen wird (kein 0.30 hart)."""
    m = _mk({fy: 1e9 for fy in (2022, 2023, 2024, 2025)}, mcap=1e10)
    row_95 = evaluate_ticker(m, _cfg(watch_min=0.95))["row"]
    row_30 = evaluate_ticker(m, _cfg())["row"]
    assert row_95["status"] == "nein"
    assert row_30["status"] == "Beobachtung (nicht normalisiert)"
    # Flags in beiden Faellen gesetzt (Hinweis unabhaengig vom Tor)
    assert "nicht normalisiert" in _flags(row_95)
    assert "nicht normalisiert" in _flags(row_30)
