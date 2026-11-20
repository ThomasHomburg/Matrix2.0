"""v70 (FCFF): Regressionstests -- Start-FCF um Zinsaufwand nach Steuern
(STD: fcff = FCF + |Zinsaufwand| x (1 - Steuersatz)), konsistent zum
ND-Abzug (keine Doppelbestrafung der Verschuldung).

Abgedeckt (Freigabe-Matrix):
- Invarianz: zwei identische Geschaeftsmodelle, B aus A konstruiert
  (OCF_B = OCF_A - Zinsen x (1 - t), ND_B = Zinsen / Fremdkapitalkosten)
  -> EV unter fcff nahezu gleich; levered_legacy zeigt die alte
  Doppelbestrafung (Regressionsbeleg)
- Monotonie: mehr Schulden bei konstantem EBIT -> EV unter fcff konstant
- Realitaetstest: LLY-aehnliche Fixture -> iv_base >= 1 % des Kurses
- Handnachrechnung eines Beispiels mit Zinsen (Abweichung < 2 %)
- Zinsaufwand NaN -> DCF n/a + Flag (KEIN stiller Fallback); 0 -> normal
- levered_legacy (Schalter) und IFRS-Schutz (Boersen-Suffix -> legacy_nonUS)
- Steuersatz fehlt bei Zinsen > 0 -> Normsteuersatz + Flag
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from data.loader import Metric, TickerMetrics
from valuation.dcf import compute_dcf, is_buy_candidate, sensitivity_mos_base


def _cfg(fcf_basis="fcff", **dcf_over):
    cfg = load_config()
    cfg = dict(cfg)
    cfg["dcf"] = dict(cfg["dcf"])
    cfg["dcf"]["fcf_basis"] = fcf_basis
    cfg["dcf"].update(dcf_over)
    cfg["sec"] = {"enabled": False}
    return cfg


def _ticker(ticker="FCFT", mcap=1000.0, shares=10.0, fcf_sbc_by_fy=None,
            interest=0.0, tax=100.0, pretax=500.0, net_debt=0.0, price=None,
            cagr=0.05, ttm=None):
    """Synthetischer Ticker; Zinsaufwand/Steuern je Jahr identisch (einfache
    Handnachrechnung), ND bekannt, optional TTM (mit TTM-Zinsaufwand)."""
    if fcf_sbc_by_fy is None:
        fcf_sbc_by_fy = {2023: 100.0, 2024: 100.0, 2025: 100.0}
    m = TickerMetrics(ticker=ticker, country="US")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    if price is not None:
        m.price = Metric("price", price, "USD", "USD", 2025)
    rev0 = 1000.0
    for i, fy in enumerate(sorted(fcf_sbc_by_fy)):
        m.years[fy] = {
            "fcf_sbc": Metric("fcf_sbc", fcf_sbc_by_fy[fy], "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", interest,
                                       "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", tax,
                                         "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", pretax,
                                        "USD", "USD", fy),
            "revenue": Metric("revenue", rev0 * (1 + cagr) ** i, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", shares, "shares", "USD", fy),
            "net_debt": Metric("net_debt", net_debt, "USD", "USD", fy),
        }
        m.fiscal_ends[fy] = f"{fy}-12-31"
    if ttm is not None:
        m.ttm = ttm
        m.ttm_plausible = True
        m.ttm_fallback = False
    return m


def _dcf_cfg_fuers_kriterium(cfg):
    """dcf-Block wie criteria._dcf_cfg_for_criteria -- fuer Handnachrechnung."""
    return cfg["dcf"]


# --- (1) Invarianz: B aus A konstruiert --------------------------------------

def test_invarianz_ev_fcff_gleich_legacy_doppelbestraft():
    """Modell A schuldenfrei; B = A mit Schulden (Zinsen im OCF):
    OCF_B = OCF_A - Zinsen x (1 - t), ND_B = Zinsen / Fremdkapitalkosten
    (5 %). Unter fcff MUSS der Unternehmenswert (EV) nahezu identisch
    sein (FCFF_B == FCFF_A exakt); unter levered_legacy bleibt die
    Doppelbestrafung: EV kleiner UND zusaetzlich ND-Abzug."""
    steuersatz = 0.20          # tax/pretax = 100/500
    zinsen = 10.0
    fremdkapitalkosten = 0.05
    a = _ticker(ticker="A", interest=0.0, net_debt=0.0, mcap=1e4)
    b = _ticker(ticker="B",
                fcf_sbc_by_fy={fy: v - zinsen * (1 - steuersatz)
                              for fy, v in {2023: 100.0, 2024: 100.0,
                                            2025: 100.0}.items()},
                interest=zinsen, net_debt=zinsen / fremdkapitalkosten,
                mcap=1e4)
    cfg = _cfg()
    ra = compute_dcf(a, cfg)
    rb = compute_dcf(b, cfg)
    # fcff: EV identisch (Einzigartiger Unterschied von B ist die _Kapital_
    # struktur; der operative FCFF ist identisch)
    ev_a = ra.scenarios["base"].enterprise_value
    ev_b = rb.scenarios["base"].enterprise_value
    assert ev_b == pytest.approx(ev_a, rel=0.02)
    # ... und Equity B = Equity A - ND_B (nur EIN Abzug, nicht zwei)
    eq_a = ev_a - 0.0
    eq_b = ev_b - zinsen / fremdkapitalkosten
    assert rb.scenarios["base"].value_per_share == pytest.approx(
        eq_b / 10.0, rel=0.02)
    assert rb.scenarios["base"].value_per_share < ra.scenarios["base"].value_per_share

    # levered_legacy: Doppelbestrafung -- EV sinkt UND ND wird abgezogen
    cfg_leg = _cfg(fcf_basis="levered_legacy")
    ra_leg = compute_dcf(a, cfg_leg)
    rb_leg = compute_dcf(b, cfg_leg)
    ev_a_leg = ra_leg.scenarios["base"].enterprise_value
    ev_b_leg = rb_leg.scenarios["base"].enterprise_value
    assert ev_b_leg < ev_a_leg          # Zinsen druecken den Start-FCF
    vps_a_leg = ra_leg.scenarios["base"].value_per_share
    vps_b_leg = rb_leg.scenarios["base"].value_per_share
    # Doppelbestrafung: Equity-B-Verlust > ND_B (Verlust durch ZINSLAST
    # im FCF plus ND-Abzug -- fachlich unzulaessig hoch)
    assert (vps_a_leg - vps_b_leg) * 10.0 > zinsen / fremdkapitalkosten
    assert rb.fcf_basis_flag == "fcff"


def test_monotonie_mehr_schulden_ev_konstant():
    """Bei konstantem EBIT/FCFF steigende Verschuldung (Zinsen x ND
    konsistent) aendert den EV unter fcff NICHT (Monotonie-Bruch des
    levered Modells als Negativ-Beleg)."""
    cfg = _cfg()
    evs = []
    for zinsen in (0.0, 5.0, 10.0, 25.0):
        m = _ticker(ticker="M", interest=zinsen,
                    fcf_sbc_by_fy={fy: 100.0 - zinsen * 0.8
                                   for fy in (2023, 2024, 2025)},
                    net_debt=zinsen / 0.05, mcap=1e4)
        r = compute_dcf(m, cfg)
        evs.append(r.scenarios["base"].enterprise_value)
    for ev in evs[1:]:
        assert ev == pytest.approx(evs[0], rel=0.02), evs


def test_realitaet_lly_artig_iv_mindestens_1_prozent_des_kurses():
    """LLY-aehnliche Fixture (kleiner FCF vs. 45 Mrd. Schulden,
    v61: iv_base 0.10-0.74 USD = 'wert:nahe_null'): unter fcff bleibt
    iv_base oberhalb 1 % des Boersenkurses; unter levered_legacy
    kollabiert der Wert (Symptom reproduziert)."""
    kurs = 50.0
    # LLY-artig: FCF ~4000 (absolut), Zinsaufwand 300 (5 % auf 45.000
    # Schulden), Steuerfelder 20 % -> FCFF = 4000 + 240 = 4240 je Quelle
    m = _ticker(ticker="LLY", mcap=80e4, shares=8e3,
                fcf_sbc_by_fy={2023: 4e3, 2024: 4e3, 2025: 4e3},
                interest=300.0, net_debt=45e3, price=kurs)
    r_fcff = compute_dcf(m, _cfg())
    r_leg = compute_dcf(m, _cfg(fcf_basis="levered_legacy"))
    assert r_fcff.scenarios["base"].value_per_share >= 0.01 * kurs
    # Symptom (Regressionsbeleg): levered ist drastisch niedriger
    assert r_leg.scenarios["base"].value_per_share < \
        r_fcff.scenarios["base"].value_per_share


# --- (2) Handnachrechnung (Abweichung < 2 %) ---------------------------------

def _hand_dcf(fcff_start, shares, nd, r, g_terminal, g_base, dcfg):
    """Nachbildung von _growth_path/_pv/_dcf_scenario (Base-Szenario)."""
    rates = [g_base] * 5
    fade = float(dcfg.get("fade_to_growth", g_terminal))
    for i in range(1, 6):
        rates.append(g_base + (fade - g_base) * i / 5.0)
    fcfs, pv = [], 0.0
    f = fcff_start
    for i, g in enumerate(rates):
        f = f * (1.0 + g)
        fcfs.append(f)
        pv += f / (1.0 + r) ** (i + 1)
    tv = fcfs[-1] * (1.0 + g_terminal) / (r - g_terminal)
    ev = pv + tv / (1.0 + r) ** 10
    return (ev - nd) / shares


def test_handnachrechnung_mit_zinsen_abweichung_kleiner_2_prozent():
    """FCFF je FY = 100 + 10 x (1 - 0.2) = 108; Ø3J = 108; Base-CAGR
    5 % (cap 6 %), r = 10 %, g_t = 2.5 % -- Hand-DCF vs. compute_dcf."""
    cfg = _cfg(discount_rate=0.10, terminal_growth=0.025)
    m = _ticker(interest=10.0, tax=100.0, pretax=500.0, net_debt=200.0,
                mcap=1e4)
    d = compute_dcf(m, cfg)
    assert d.fcf_normalized == pytest.approx(108.0)
    hand = _hand_dcf(108.0, 10.0, 200.0, 0.10, 0.025, 0.05, cfg["dcf"])
    abweichung = abs(hand - d.scenarios["base"].value_per_share) / hand
    assert d.scenarios["base"].value_per_share == pytest.approx(hand, rel=0.02)
    assert abweichung < 0.02
    # Aufschlag dokumentiert (Ø über die Quellen, Berichtswaehrung)
    assert d.fcff_interest_added == pytest.approx(8.0)
    assert d.fcff_steuersatz_normiert is False


# --- (3) NaN-Regeln ----------------------------------------------------------

def test_zinsaufwand_nan_strikt_na_mit_flag():
    """Fehlender/NaN-Zinsaufwand -> strikt DCF n/a (KEIN stiller Fallback
    auf levered), fcff_status, Missing-Grund und DQ ueber die Pipeline."""
    from scoring.pipeline import evaluate_ticker
    m = _ticker(interest=float("nan"), net_debt=0.0, mcap=1e9, shares=1e7,
                price=100.0,
                fcf_sbc_by_fy={2022: 100.0, 2023: 100.0, 2024: 100.0,
                               2025: 100.0})   # 4 Wertjahre fuer Filter
    d = compute_dcf(m, _cfg())
    assert d.fcff_status == "zinsaufwand_fehlend"
    assert d.fcf_basis_flag == "fcff"
    assert d.fcf_normalized == d.fcf_normalized     # Rohwert dokumentiert? ->
    # (Rohwert bleibt zur Transparenz gesetzt, aber MoS/Szenarien n/a)
    assert d.margin_of_safety != d.margin_of_safety
    assert d.scenarios == {}
    out = evaluate_ticker(m, _cfg())
    row = out["row"]
    assert row is not None, "Fixture muss die Filter passieren (mcap >= 500 Mio?)"
    assert any("Zinsaufwand fehlt" in x for x in row["data_quality"])
    assert row["is_buy"] is False
    assert not is_buy_candidate(90.0, d, 100.0, _cfg())


def test_zinsaufwand_null_ist_gueltig_wert():
    """Zinsaufwand exakt 0 -> normal berechenbar, FCFF == FCF (kein
    Steuersatz noetig), Erwartungswerte unveraendert (Fixture-Basis)."""
    m = _ticker(interest=0.0, net_debt=0.0, mcap=1e4)
    d = compute_dcf(m, _cfg())
    assert d.fcff_status == "ok"
    assert d.fcf_normalized == pytest.approx(100.0)
    assert d.fcff_interest_added == pytest.approx(0.0)
    assert d.fcff_steuersatz_normiert is False
    assert d.margin_of_safety == d.margin_of_safety


def test_zinsaufwand_fehlt_komplett_strikt_na():
    """interest_expense-Metric FEHLT ganz (wie FMP ohne interestExpense)
    -> identisch strikt n/a (None != 0, CLAUDE.md)."""
    m = _ticker(net_debt=0.0, mcap=1e4)
    for fy in m.years:
        del m.years[fy]["interest_expense"]
    d = compute_dcf(m, _cfg())
    assert d.fcff_status == "zinsaufwand_fehlend"
    assert d.scenarios == {}


# --- (4) levered_legacy + IFRS-Schutz ----------------------------------------

def test_levered_legacy_schalter_reproduziert_altes_verhalten():
    """dcf.fcf_basis=levered_legacy -> exakt das alte Verhalten (kein
    Aufschlag), Flag gesetzt; Werte NIEDRIGER als fcff (Zinslast bleibt)."""
    m = _ticker(interest=10.0, net_debt=200.0, mcap=1e4)
    d_fcff = compute_dcf(m, _cfg())
    d_leg = compute_dcf(m, _cfg(fcf_basis="levered_legacy"))
    assert d_leg.fcf_basis_flag == "levered_legacy"
    assert d_leg.fcf_normalized == pytest.approx(100.0)  # kein Aufschlag
    assert d_fcff.fcf_normalized == pytest.approx(108.0)
    assert d_leg.scenarios["base"].value_per_share < \
        d_fcff.scenarios["base"].value_per_share
    assert d_leg.fcff_interest_added != d_leg.fcff_interest_added  # NaN


def test_ifrs_schutz_boersen_suffix_legacy_nonus():
    """Ticker MIT Boersen-Suffix (z. B. SHEL.L, SAP.DE) bleibt auf
    levered_legacy (Flag legacy_nonUS; IAS 7: Zinszahlungen koennen im
    Finanzierungs-CF liegen) -- auch bei konfiguriertem fcff."""
    m = _ticker(ticker="SHEL.L", interest=10.0, net_debt=200.0, mcap=1e9,
                shares=1e7,
                fcf_sbc_by_fy={2022: 100.0, 2023: 100.0, 2024: 100.0,
                               2025: 100.0})   # 4 Wertjahre fuer Filter
    d = compute_dcf(m, _cfg())          # fcff konfiguriert!
    assert d.fcf_basis_flag == "legacy_nonUS"
    assert d.fcf_normalized == pytest.approx(100.0)   # kein Aufschlag
    assert d.fcff_status == ""              # kein Fehler, nur Schutz
    # Pipeline-Flag fcf_basis:legacy_nonUS
    from scoring.pipeline import evaluate_ticker
    out = evaluate_ticker(m, _cfg())
    assert "fcf_basis:legacy_nonUS" in out["row"]["flags"]


# --- (5) Steuersatz-Normierung (D1=B) ---------------------------------------

def test_steuersatz_nan_bei_zinsen_normsteuersatz_und_flag():
    """Steuern/Vorsteuer fehlen bei Zinsen > 0 -> Normsteuersatz
    (tax_rate_norm; D1=B) + Flag steuersatz_normiert; Ergebnis bleibt
    bewertbar (kein n/a)."""
    import copy
    m = _ticker(interest=10.0, net_debt=0.0, mcap=1e4)
    for fy in m.years:
        del m.years[fy]["income_tax_expense"]
        del m.years[fy]["income_before_tax"]
    cfg = _cfg()
    t_norm = float(cfg.get("data", {}).get("tax_rate_norm", 0.21))
    d = compute_dcf(m, cfg)
    assert d.fcff_steuersatz_normiert is True
    assert d.fcf_normalized == pytest.approx(100.0 + 10.0 * (1.0 - t_norm))
    assert d.fcff_status == "ok"


def test_ttm_modus_nutzt_ttm_zinsaufwand_und_ttm_steuersatz():
    """ttm_only mit TTM: Zinsaufwand UND Steuersatz kommen aus dem
    TtmResult (Summe letzte 4 Quartale), nicht aus m.years."""
    from data.ttm import TtmResult
    ttm = TtmResult(
        ttm={"fcf_sbc": Metric("fcf_sbc", 120.0, "USD", "USD", 2026),
             "interest_expense": Metric("interest_expense", 8.0, "USD", "USD", 2026),
             "income_tax_expense": Metric("income_tax_expense", 25.0, "USD", "USD", 2026),
             "income_before_tax": Metric("income_before_tax", 125.0, "USD", "USD", 2026)},
        quarter_end="2026-06-30", plausible=True)
    import datetime as _dt
    _e0 = _dt.date(2026, 6, 30)
    ends = [(_e0 - _dt.timedelta(days=91 * i)).isoformat() for i in range(4, -1, -1)]
    ttm.quarters = [{"quarter_end": e} for e in ends]
    ttm.flow_quarters_used = {"fcf_sbc": ends[1:], "fcf": ends[1:]}
    m = _ticker(interest=0.0, net_debt=0.0, mcap=1e4, ttm=ttm)
    m.fiscal_ends = {2023: "2023-12-31", 2024: "2024-12-31", 2025: "2025-12-31"}
    d = compute_dcf(m, _cfg(fcf_mode="ttm_only"))
    assert d.fcf_basis == "ttm"
    # TTM-Steuersatz = 25/125 = 0.2 -> FCFF = 120 + 8 x 0.8 = 126.4
    assert d.fcf_normalized == pytest.approx(120.0 + 8.0 * 0.8)
    assert d.fcff_steuersatz_normiert is False


def test_sensitivitaet_erbt_fcff():
    """Sensitivitaet Wiederverwendung von fcf_normalized_conv -> automatisch
    FCFF; Mittelzelle == Base-Szenario des compute_dcf-Ergebnisses."""
    cfg = _cfg(discount_rate=0.10, terminal_growth=0.025)
    m = _ticker(interest=10.0, net_debt=200.0, mcap=1e4)
    d = compute_dcf(m, cfg)
    sens = sensitivity_mos_base(m, cfg, current_price=None,
                                discount_rates=[0.10], terminal_growths=[0.025])
    mid = sens["vps"].iloc[0]["value_per_share"]
    assert mid == pytest.approx(d.scenarios["base"].value_per_share, rel=1e-9)
