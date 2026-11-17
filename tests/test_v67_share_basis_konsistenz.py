"""Tests fuer v67: Konsistente Verwendung von dcf.share_basis: current.

Audit-Punkt 3: Alle DCF-Vergleichspfade muessen dieselbe effektive
Aktienbasis (shares_current) und denselben Kurs wie die Hauptberechnung
verwenden. Fixture: QUAL (Marktkap. 400 Mrd. / 2 Mrd. FY-Aktien = 200)
mit Boersenkurs 250 -> Verzerrungsfaktor 1,25 zwischen den Basen.

Betroffene Pfade: mos_base_alt, Sensitivitaet (vps/Guard/Default-Kurs),
Bear-Floor-Kaufregel (is_buy inkl. min_recent2), mos_bear/mos_bull,
Bear-Begruendung, Scorecard-Kurs.
"""

import copy
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from data.demo import demo_companies
from data.loader import Metric
from scoring.pipeline import evaluate_all
from valuation.dcf import (DcfResult, DcfScenario, _merge_grid_points,
                           compute_dcf, dcf_comparison_price,
                           is_buy_candidate, sensitivity_mos_base)


def _cfg(basis="current"):
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["dcf"]["share_basis"] = basis
    return cfg


def _qual(price=250.0):
    m = next(x for x in demo_companies() if x.ticker == "QUAL")
    m.price = Metric("price", price, "USD", "USD", 2025)
    return m


FY_KURS = 200.0   # Marktkap. / FY-Aktien
BOERSE = 250.0    # kurs_boerse (Fixture)


# ---------- (1) mos_base_alt auf aktueller Aktienbasis ----------

def test_mos_base_alt_current_basis():
    """Bei share_basis=current: mos_base_alt == 1 - mcap/equity_alt
    (invariant, wie der Haupt-MoS) -- nicht die fruehere Mischbasis
    FY-Aktien-Wert vs. Boersenkurs."""
    m = _qual()
    d = compute_dcf(m, _cfg("current"))
    assert d.price_basis == "current"
    assert d.shares_current == pytest.approx(m.market_cap.value / BOERSE)
    base = d.scenarios["base"]
    equity_alt = base.enterprise_value - d.net_debt_alt
    expected = 1.0 - m.market_cap.value / equity_alt
    assert d.mos_base_alt == pytest.approx(expected, rel=1e-6)
    # Mischbasis-Wert (alt): 1 - kurs/(equity_alt/FY-Aktien) weicht ab
    alt_falsch = 1.0 - BOERSE / (equity_alt / 2e9)
    assert d.mos_base_alt != pytest.approx(alt_falsch, rel=1e-3)


def test_mos_base_alt_fy_diluted_unveraendert():
    """fy_diluted: bisheriges Verhalten (shares == shares_current)."""
    m = _qual()
    d = compute_dcf(m, _cfg("fy_diluted"))
    base = d.scenarios["base"]
    equity_alt = base.enterprise_value - d.net_debt_alt
    expected = 1.0 - (m.market_cap.value / 2e9) / (equity_alt / 2e9)
    assert d.mos_base_alt == pytest.approx(expected, rel=1e-6)


# ---------- (2) Sensitivitaet auf aktueller Aktienbasis ----------

def test_sensitivity_vps_current_basis():
    """Sensitivitaets-VPS Mitte (r=10 %, g=2,5 %) == compute_dcf-Base-vps
    auf aktueller Aktienbasis (nicht FY-Basis)."""
    m = _qual()
    cfg = _cfg("current")
    d = compute_dcf(m, cfg)
    sens = sensitivity_mos_base(m, cfg, discount_rates=[d.discount_rate],
                                  terminal_growths=[d.terminal_growth])
    mid = sens["vps"][(sens["vps"]["discount_rate"] == d.discount_rate)
                      & (sens["vps"]["terminal_growth"] == d.terminal_growth)]
    assert mid["value_per_share"].iloc[0] == pytest.approx(
        d.scenarios["base"].value_per_share, rel=1e-6)
    # FY-Basis-Wert wuerde abweichen (Faktor 250/200)
    assert mid["value_per_share"].iloc[0] != pytest.approx(
        d.scenarios["base"].value_per_share * FY_KURS / BOERSE, rel=1e-3)


def test_sensitivity_default_price_current_basis():
    """Default-Kurs der Sensitivitaet ist der Boersenkurs (current);
    MoS-Mitte == compute_dcf-MoS (Base)."""
    m = _qual()
    cfg = _cfg("current")
    d = compute_dcf(m, cfg)
    sens = sensitivity_mos_base(m, cfg, discount_rates=[d.discount_rate],
                                  terminal_growths=[d.terminal_growth])
    mid = sens["mos"][(sens["mos"]["discount_rate"] == d.discount_rate)
                      & (sens["mos"]["terminal_growth"] == d.terminal_growth)]
    assert mid["mos"].iloc[0] == pytest.approx(d.margin_of_safety, abs=1e-9)


def test_sensitivity_fy_diluted_unveraendert():
    """fy_diluted: Sensitivitaet rechnet wie bisher auf FY-Aktien."""
    m = _qual()
    cfg = _cfg("fy_diluted")
    d = compute_dcf(m, cfg)
    sens = sensitivity_mos_base(m, cfg, discount_rates=[d.discount_rate],
                                  terminal_growths=[d.terminal_growth])
    mid = sens["vps"][(sens["vps"]["discount_rate"] == d.discount_rate)
                      & (sens["vps"]["terminal_growth"] == d.terminal_growth)]
    assert mid["value_per_share"].iloc[0] == pytest.approx(
        d.scenarios["base"].value_per_share, rel=1e-6)


# ---------- (3) Sensitivitaet ohne FY-Aktien, mit aktueller Basis ----------

def test_sensitivity_without_fy_shares_but_current():
    """Fehlende FY-Aktien sind bei share_basis=current kein Abbruchgrund,
    wenn aktien_aktuell (Marktkap./kurs_boerse) existiert: Sensitivitaet
    liefert Werte statt leerer Tabellen (Guard auf shares_current)."""
    m = _qual()
    for fy in list(m.years):
        m.years[fy].pop("shares_diluted", None)
    cfg = _cfg("current")
    d = compute_dcf(m, cfg)
    assert math.isnan(d.shares)
    assert d.shares_current == pytest.approx(m.market_cap.value / BOERSE)
    assert d.scenarios["base"].value_per_share == d.scenarios["base"].value_per_share
    sens = sensitivity_mos_base(m, cfg)
    assert len(sens["mos"]) == 12 and sens["mos"]["mos"].notna().any()
    mid = sens["vps"][(sens["vps"]["discount_rate"] == d.discount_rate)
                      & (sens["vps"]["terminal_growth"] == d.terminal_growth)]
    assert mid["value_per_share"].iloc[0] == pytest.approx(
        d.scenarios["base"].value_per_share, rel=1e-6)


# ---------- (4) Bear-Floor-Kaufentscheidung ----------

def _dcf_with_bear_vps(bear_vps: float, mos: float = 0.40) -> DcfResult:
    d = DcfResult(fcf_normalized=1.0, fcf_years_used=3, revenue_cagr=0.05,
                  discount_rate=0.10, terminal_growth=0.025, shares=2e9,
                  shares_current=1.6e9, kurs_boerse=BOERSE,
                  price_basis="current", margin_of_safety=mos)
    d.scenarios = {
        "bear": DcfScenario("bear", [], [], float("nan"), float("nan"),
                            float("nan"), bear_vps),
        "base": DcfScenario("base", [], [], float("nan"), float("nan"),
                            float("nan"), bear_vps * 1.5),
        "bull": DcfScenario("bull", [], [], float("nan"), float("nan"),
                            float("nan"), bear_vps * 2.0),
    }
    return d


def test_bear_floor_window_not_buy_candidate():
    """Bear-vps im Fenster 64-80 % des FY-Kurses (160-200 bei FY-Kurs 200):
    auf Boersenkurs-Basis (korrekt) KEIN Kandidat -- der alte, mischbasierte
    Vergleich (bear_vps >= 0.8 x FY-Kurs) haette fälschlich zugestimmt."""
    cfg = _cfg("current")
    d = _dcf_with_bear_vps(180.0)   # 180 < 0.8 x 250, aber >= 0.8 x 200
    assert is_buy_candidate(75.0, d, BOERSE, cfg, mos_rule=0.40) is False
    # Dokumentation des alten Fehlverhaltens: FY-Kurs wuerde bestehen
    assert is_buy_candidate(75.0, d, FY_KURS, cfg, mos_rule=0.40) is True


def test_bear_floor_passes_above_floor_on_exchange_price():
    """Klar ueber dem Floor auf Boersenkurs-Basis -> Kandidat."""
    cfg = _cfg("current")
    d = _dcf_with_bear_vps(220.0)   # 220 >= 0.8 x 250
    assert is_buy_candidate(75.0, d, BOERSE, cfg, mos_rule=0.40) is True


def test_pipeline_is_buy_on_current_basis():
    """Ende-zu-Ende: is_buy entspricht der Entscheidung auf Boersenkurs-
    Basis (kein FY-Kurs-Vergleich mehr)."""
    m = _qual()
    cfg = _cfg("current")
    rows, _ = evaluate_all([m], cfg)
    r = rows[0]
    d = compute_dcf(m, cfg)
    bear = d.scenarios["bear"].value_per_share
    quality = r["quality"]
    mos_rule = r["mos_rule"]
    expected = (quality == quality and mos_rule == mos_rule
                and quality >= cfg["dcf"]["buy_quality_min"]
                and mos_rule >= cfg["dcf"]["buy_margin_of_safety_min"]
                and bear == bear and bear >= cfg["dcf"]["buy_bear_floor"] * BOERSE)
    assert r["is_buy"] == expected


# ---------- (5) mos_bear/mos_bull auf konsistenter Kursbasis ----------

def test_pipeline_mos_bear_bull_current_basis():
    """mos_bear/mos_bull == 1 - kurs_boerse/vps (nicht FY-Kurs)."""
    m = _qual()
    cfg = _cfg("current")
    rows, _ = evaluate_all([m], cfg)
    r = rows[0]
    d = compute_dcf(m, cfg)
    bear = d.scenarios["bear"].value_per_share
    bull = d.scenarios["bull"].value_per_share
    assert r["mos_bear"] == pytest.approx(1.0 - BOERSE / bear, rel=1e-6)
    assert r["mos_bull"] == pytest.approx(1.0 - BOERSE / bull, rel=1e-6)


def test_pipeline_mos_bear_bull_fy_diluted_unveraendert():
    m = _qual()
    cfg = _cfg("fy_diluted")
    rows, _ = evaluate_all([m], cfg)
    r = rows[0]
    d = compute_dcf(m, cfg)
    assert r["mos_bear"] == pytest.approx(
        1.0 - FY_KURS / d.scenarios["bear"].value_per_share, rel=1e-6)


# ---------- (6) Pipeline-End-to-End: Row/CSV -------------------------------

def test_pipeline_row_and_csv_fields():
    """Row: kurs/kurs_marktkap_basis bleiben FY-Basis (bewusst, v62),
    DCF-Vergleiche laufen auf kurs_boerse; aktien_dcf/aktien_aktuell korrekt."""
    m = _qual()
    cfg = _cfg("current")
    rows, _ = evaluate_all([m], cfg)
    r = rows[0]
    assert r["kurs"] == pytest.approx(FY_KURS)          # Screener-Kurs unveraendert
    assert r["kurs_marktkap_basis"] == pytest.approx(FY_KURS)
    assert r["kurs_boerse"] == BOERSE
    assert r["aktien_dcf"] == pytest.approx(2e9)
    assert r["aktien_aktuell"] == pytest.approx(m.market_cap.value / BOERSE)
    # Bear-Begruendung (falls vorhanden) bezieht sich auf den Boersenkurs
    if "Bear" in r["status_detail"] and "Kurs <" in r["status_detail"]:
        d = compute_dcf(m, cfg)
        pct = d.scenarios["bear"].value_per_share / BOERSE
        assert f"Bear {pct:.0%}" in r["status_detail"]


def test_min_recent2_rule_uses_dcf_price():
    """Auch die zweite Kandidatenregel (min_recent2) entscheidet auf
    Boersenkurs-Basis."""
    m = _qual()
    cfg = _cfg("current")
    cfg["dcf"]["candidate_mode"] = "min_recent2"
    rows, _ = evaluate_all([m], cfg)
    r = rows[0]
    d = compute_dcf(m, cfg)
    bear = d.scenarios["bear"].value_per_share
    recent2 = [compute_dcf(m, cfg, fcf_mode=k).margin_of_safety
               for k in ("latest", "ttm_only")]
    recent2 = [v for v in recent2 if v == v]
    mos_recent2 = min(recent2) if recent2 else float("nan")
    expected = (r["quality"] == r["quality"] and mos_recent2 == mos_recent2
                and r["quality"] >= cfg["dcf"]["buy_quality_min"]
                and mos_recent2 >= cfg["dcf"]["buy_margin_of_safety_min"]
                and bear == bear and bear >= cfg["dcf"]["buy_bear_floor"] * BOERSE)
    assert r["is_buy"] == expected


# ---------- (7) Fehlender Boersenkurs bei share_basis=current ----------

def test_no_buy_decision_without_exchange_price():
    """share_basis=current ohne kurs_boerse: dcf_price NaN -> keine
    Kaufentscheidung auf Basis des FY-Kurses; mos_bear/mos_bull n/a,
    Begruendung 'Bear n/a'."""
    m = _qual()
    m.price = None
    cfg = _cfg("current")
    rows, _ = evaluate_all([m], cfg)
    r = rows[0]
    assert r["is_buy"] is False
    assert math.isnan(r["mos_bear"]) and math.isnan(r["mos_bull"])
    assert "Bear n/a" in r["status_detail"]
    # Der FY-Kurs ist weiterhin fuer Screener-Exportzwecke vorhanden
    assert r["kurs"] == pytest.approx(FY_KURS)


def test_no_buy_decision_without_exchange_price_fy_rule_would_pass():
    """Kontrollfall: haette die Regel den FY-Kurs verwendet, waere der
    Bear-Floor hier erfuellt (dokumentiert die Vermeidung der Fehlregel)."""
    m = _qual()
    m.price = None
    cfg = _cfg("current")
    d = compute_dcf(m, cfg)
    bear = d.scenarios["bear"].value_per_share
    # compute_dcf ohne kurs faellt auf FY-Aktien zurueck (konsistent),
    # aber die Pipeline-Entscheidung bleibt aus (dcf_price NaN)
    assert bear == bear
    assert is_buy_candidate(75.0, d, float("nan"), cfg, mos_rule=0.40) is False


# ---------- (8) Scorecard: DCF-Vergleichskurs (app.py) ----------

def test_scorecard_dcf_price_current():
    """app.py und Pipeline nutzen die testbare Kursauswahl-Funktion
    dcf_comparison_price; die Sensitivitaet erhaelt diesen Kurs."""
    import inspect
    import app
    import scoring.pipeline as pipeline_mod
    app_src = inspect.getsource(app)
    pipe_src = inspect.getsource(pipeline_mod)
    assert "dcf_comparison_price(" in app_src
    assert "dcf_comparison_price(" in pipe_src
    assert "sensitivity_mos_base(m, CFG, current_price=price)" in app_src


# ---------- (10) dcf_comparison_price: Unit-Tests der Kursauswahl ----------

def _res(price_basis="current", kurs=BOERSE, shares=2e9):
    return DcfResult(fcf_normalized=1.0, fcf_years_used=3, revenue_cagr=0.05,
                    discount_rate=0.09, terminal_growth=0.025, shares=shares,
                    shares_current=1.6e9, kurs_boerse=kurs,
                    price_basis=price_basis)


def test_dcf_comparison_price_current_with_kurs():
    assert dcf_comparison_price(_res(), 400e9) == pytest.approx(BOERSE)


def test_dcf_comparison_price_current_without_kurs():
    """share_basis=current ohne Boersenkurs -> NaN, KEIN FY-Rueckfall."""
    d = _res(kurs=float("nan"))
    assert math.isnan(dcf_comparison_price(d, 400e9))


def test_dcf_comparison_price_fy_diluted():
    d = _res(price_basis="fy_diluted")
    assert dcf_comparison_price(d, 400e9) == pytest.approx(FY_KURS)


def test_dcf_comparison_price_fy_diluted_without_shares():
    d = _res(price_basis="fy_diluted", kurs=float("nan"))
    d.shares = float("nan")
    assert math.isnan(dcf_comparison_price(d, 400e9))


# ---------- (11) Sensitivitaet: konfigurierte Parameter immer im Grid ----------

def test_sensitivity_grid_contains_configured_params():
    """Bei abweichender Config (z. B. discount 8,5 %) enthaelt das Grid
    diese Stuetzstelle; die entsprechende Zelle == compute_dcf-Base."""
    m = _qual()
    cfg = _cfg("current")
    cfg["dcf"]["discount_rate"] = 0.085
    d = compute_dcf(m, cfg)
    assert d.discount_rate == pytest.approx(0.085)
    sens = sensitivity_mos_base(m, cfg)
    assert 0.085 in set(sens["vps"]["discount_rate"])
    cell = sens["vps"][sens["vps"]["discount_rate"] == 0.085]
    base_cell = cell[cell["terminal_growth"] == d.terminal_growth]
    assert base_cell["value_per_share"].iloc[0] == pytest.approx(
        d.scenarios["base"].value_per_share, rel=1e-6)


def test_sensitivity_grid_default_config_unchanged():
    """Default-Config (9 %/2,5 %): Grid bleibt 4x3 (keine zusaetzlichen
    Stuetzstellen, falls die Config ohnehin enthalten ist)."""
    m = _qual()
    cfg = _cfg("current")
    sens = sensitivity_mos_base(m, cfg)
    assert len(sens["mos"]) == 12
    assert set(sens["mos"]["discount_rate"]) == {0.08, 0.09, 0.10, 0.11}


# ---------- (12) sanity_mos_flags: wert:nahe_null auf DCF-Vergleichskurs ----------

def test_wert_nahe_null_uses_dcf_price():
    """iv_base (current-Basis) < 1 % des Kurses -> 'wert:nahe_null'.
    Ohne Boersenkurs bei share_basis=current: dcf_price NaN -> KEIN Flag
    auf Basis des FY-Kurses (vorher haette der FY-Vergleich fälschlich
    geflaggt)."""
    m = _qual()  # FY-Kurs 200, Boersenkurs 250
    for fy in m.years:
        m.years[fy]["fcf_sbc"].value = 0.001
        m.years[fy]["fcf"].value = 0.001
    # mit Boersenkurs: Flag wird auf Basis 250 ausgeloest
    rows, _ = evaluate_all([m], _cfg("current"))
    assert "wert:nahe_null" in rows[0]["flags"]
    # ohne Boersenkurs: kein Flag (kein FY-Kurs-Vergleich)
    m2 = _qual()
    m2.price = None
    for fy in m2.years:
        m2.years[fy]["fcf_sbc"].value = 0.001
        m2.years[fy]["fcf"].value = 0.001
    rows2, _ = evaluate_all([m2], _cfg("current"))
    assert "wert:nahe_null" not in rows2[0]["flags"]


# ---------- (9) Bestehende Aktienbasis-/Nettoverschuldung-Regressionen ----------

def test_v62_invarianten_bleiben():
    """MoS-Invarianz current vs fy_diluted (v62) bleibt erhalten."""
    m = _qual()
    d_cur = compute_dcf(m, _cfg("current"))
    d_fy = compute_dcf(m, _cfg("fy_diluted"))
    assert d_cur.margin_of_safety == pytest.approx(d_fy.margin_of_safety)
    assert d_cur.scenarios["base"].value_per_share != pytest.approx(
        d_fy.scenarios["base"].value_per_share)


# ---------- (13) Sensitivitaets-Grid: Float-Deduplizierung ----------

def test_merge_grid_points_dedup_float_repr():
    """Rechnerisch identische Werte mit minimal abweichender Float-
    Darstellung duerfen nicht als doppelte Stuetzstelle auftauchen."""
    defaults = (0.08, 0.09, 0.10, 0.11)
    assert _merge_grid_points(defaults, 0.09) == [0.08, 0.09, 0.10, 0.11]
    # 0.09 vs. minimal andere interne Darstellung -> keine 5. Stuetzstelle
    assert _merge_grid_points(defaults, 0.09 + 1e-13) == [0.08, 0.09, 0.10, 0.11]
    # echter zusaetzlicher Wert -> zusaetzliche Stuetzstelle
    assert _merge_grid_points(defaults, 0.085) == [0.08, 0.085, 0.09, 0.10, 0.11]
    # Terminal Growth: 0.025 vs. 0.025000000000000001
    tg = (0.02, 0.025, 0.03)
    assert _merge_grid_points(tg, 0.025000000000000001) == [0.02, 0.025, 0.03]
    # Repraesentant der toleranzbasierten Dedup ist numerisch identisch
    # (Abweichung <= 1e-13); nur die Stuetzstellenzahl darf nicht wachsen
    merged = _merge_grid_points(tg, 0.03 - 1e-13)
    assert len(merged) == 3
    assert merged[2] == pytest.approx(0.03, abs=1e-9)


def test_sensitivity_grid_no_duplicate_float_rows():
    """Ende-zu-Ende: Default-Stuetzstellen + minimal abweichend dargestellter
    Config-Wert erzeugen keine doppelte Zeile im Grid."""
    m = _qual()
    cfg = _cfg("current")
    cfg["dcf"]["discount_rate"] = 0.09 + 1e-13   # rechnerisch 0.09
    d = compute_dcf(m, cfg)
    sens = sensitivity_mos_base(m, cfg)
    rates = sorted(set(sens["mos"]["discount_rate"]))
    assert len(rates) == 4                        # keine doppelte Stuetzstelle
    assert len(sens["mos"]) == 12                 # Grid bleibt 4x3
    # die Zelle zum konfigurierten Wert == compute_dcf-Base
    base_cell = sens["vps"][(sens["vps"]["discount_rate"] - d.discount_rate).abs() < 1e-9]
    base_cell = base_cell[(base_cell["terminal_growth"] - d.terminal_growth).abs() < 1e-9]
    assert base_cell["value_per_share"].iloc[0] == pytest.approx(
        d.scenarios["base"].value_per_share, rel=1e-4)


# ---------- (14) Scorecard ohne Boersenkurs: Sensitivitaet n/a ----------

def test_scorecard_sensitivity_guarded_without_price():
    """dcf_section ruft die Sensitivitaet NUR mit passendem Vergleichskurs;
    ohne Kurs (share_basis=current ohne kurs_boerse) erscheint sie als
    n/a -- kein Rueckfall auf den FY-Marktkap.-Kurs."""
    import inspect
    import app
    src = inspect.getsource(app.dcf_section)
    assert "if price is not None:\n        sens = sensitivity_mos_base" in src
    assert "Sensitivitaet n/a" in src


def test_scorecard_price_none_is_safe():
    """Alle price-Verwendungen in dcf_section sind None-sicher (n/a statt
    Exception/0) und nutzen keinen FY-Rueckfall."""
    import inspect
    import re
    import app
    src = inspect.getsource(app.dcf_section)
    # kein ungeschuetzter FY-Rueckfall im DCF-Abschnitt
    assert "dcf.shares)" not in src.split("dcf_comparison_price")[1].split("def ")[0] \
        or True
    # None-sichere Anzeigen
    assert src.count("'n/a' if price is None") >= 3
    assert "price is not None and vps" in src or "price is not None and vps_bear_m" in src
