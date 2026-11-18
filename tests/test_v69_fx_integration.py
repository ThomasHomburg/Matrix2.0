"""v69 (F2-B): produktive FX-Integration -- Regressionstests.

Abgedeckt (Matrix aus dem freigegebenen Implementierungsplan):
- convert_stock-Haertung: fehlender Stichtag -> NaN (kein Tageskurs-Ersatz)
- TTM-FX-Fenster aus den TATSAECHLICH verwendeten Quartalen
  (flow_quarters_used): exaktes 12-Monats-Fenster, Vorgaengerquartal,
  Duplikate/unsortiert/Luecken/Abstands-Verstoesse -> unzuverlaessig
- FY-Nichtueberlappung am echten Fenster (FY2025*-Ausschluss, * Beispiel)
- FCF-Quellen einzeln konvertiert (je Quelle eigene Währung/Zeitraum),
  handgerechnet; nicht konvertierbare Quelle -> DCF n/a + Status + kein Kauf
- unzuverlaessige Periodenabgrenzung schliesst TTM auch bei identischer
  Waehrung aus (Rueckfall modusabhaengig, ohne Mischung mit FY)
- ND als Bestandsgroesse: Stichtagskurs zum echten Bilanzstichtag
- shares_current-Gate: bekannte Waehrungen, GBp/GBX-Sperre (fx_unit_state),
  convert_current oder NaN -- kein FY-Rueckfall
- EV/EBIT: EBIT als Flow, ND als Stock (v65-Semantik unveraendert) + DQ-Flags
- FCF-Yield als Flow (Perioden-O statt Jahresendkurs)
- Cache-Kompatibilitaet: alte TTM-Eintraege ohne flow_quarters_used
- Ende-zu-Ende: Pipeline-Statusfelder + Begruendung + Kandidatenschutz
"""

import copy
import datetime as dt
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from data.loader import Metric, TickerMetrics
from data.ttm import TtmResult, ttm_from_dict, ttm_to_dict
from data.fx import FXConverter
from data.metrics import fcf_yield, fcf_yield_sbc
from valuation.dcf import (_ttm_fx_window, compute_dcf, is_buy_candidate,
                            sensitivity_mos_base)


# ---------------------------------------------------------------- Helpers --

def _conv(manual: dict) -> FXConverter:
    """FXConverter nur mit manuellen Kursen (keine API, kein Cache)."""
    c = FXConverter(client=None, cache=None, manual_path="/nonexistent.yaml")
    c.manual = manual
    return c


def _ttm(ends, used, quarter_end=None, cur="EUR", fcf_sbc=60.0):
    t = TtmResult(
        ttm={"fcf_sbc": Metric("fcf_sbc", fcf_sbc, cur, cur, 2026)},
        quarter_end=quarter_end or ends[0], plausible=True)
    t.quarters = [{"quarter_end": e} for e in ends]
    t.flow_quarters_used = {"fcf_sbc": used, "fcf": used}
    return t


# 5 Quartalsenden (neueste zuerst), lueckenlose ~91-Tage-Kadenz
ENDS5 = ["2026-12-31", "2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31"]
USED4 = ENDS5[:4]


def _fx_ticker(ttm=None, fy_fcf=None, mcap=1000.0, price=None,
               fy_cur="EUR", mcap_cur="USD", fiscal_ends=None,
               ebit=None, net_debt_fy=None, fcf_key="fcf_sbc",
               nd_cur=None):
    """Synthetischer Cross-Currency-Ticker: Statements in fy_cur,
    Marktkap. in mcap_cur (andere) -> FX-Konvertierung noetig."""
    fy_fcf = fy_fcf or {2024: 40.0, 2025: 50.0}
    m = TickerMetrics(ticker="FXT")
    m.market_cap = Metric("market_cap", mcap, mcap_cur, mcap_cur, 2025)
    # v69-Fix-Tests: leeres Dict ist absichtlich (z. B. Stichtag-Faelle) --
    # Default NUR bei None (nicht bei {}), sonst waere der Fall testtechnisch
    # unsichtbar ueberschrieben.
    fiscal_ends = {2024: "2024-12-31", 2025: "2025-12-31",
                   2023: "2023-12-31"} if fiscal_ends is None else fiscal_ends
    for fy, v in fy_fcf.items():
        y = {
            fcf_key: Metric(fcf_key, v, fy_cur, fy_cur, fy),
            "fcf": Metric("fcf", v, fy_cur, fy_cur, fy),
            "revenue": Metric("revenue", 1000.0, fy_cur, fy_cur, fy),
            "shares_diluted": Metric("shares_diluted", 10.0, "shares", fy_cur, fy),
        }
        if net_debt_fy is not None:
            y["net_debt"] = Metric("net_debt", net_debt_fy, nd_cur or fy_cur,
                                   nd_cur or fy_cur, fy)
        if ebit is not None:
            y["ebit"] = Metric("ebit", ebit, fy_cur, fy_cur, fy)
        m.years[fy] = y
    m.fiscal_ends = fiscal_ends
    if ttm is not None:
        m.ttm = ttm
        m.ttm_plausible = True
        m.ttm_fallback = False
    if price is not None:
        m.price = Metric("price", price, fy_cur, fy_cur, 2025)
    return m


def _cfg():
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["dcf"]["share_basis"] = "current"
    return cfg


# --- (1) convert_stock-Haertung (kein Tageskurs-Ersatz) ---------------------

def test_convert_stock_ohne_stichtag_nan():
    c = _conv({"EURUSD": {"2026-06-30": 1.25}})
    assert math.isnan(c.convert_stock(100.0, "EUR", "USD", None))
    assert math.isnan(c.convert_stock(100.0, "EUR", "USD", "nonsense"))
    assert c.convert_stock(100.0, "EUR", "EUR", None) == pytest.approx(100.0)
    assert c.convert_stock(100.0, "EUR", "USD", "2026-06-30") == pytest.approx(125.0)


# --- (2) TTM-FX-Fenster: exakt / Vorgaenger / Abstoss-Faelle ----------------

def test_ttm_fx_window_exact_twelve_months():
    t = _ttm(ENDS5, USED4)
    d0, d1, basis = _ttm_fx_window(t, "fcf_sbc")
    assert basis == "quartalsdaten"
    assert (d0.isoformat(), d1.isoformat()) == ("2026-01-01", "2026-12-31")


def test_ttm_fx_window_fehlendes_vorgaengerquartal():
    ends = ["2026-12-31", "2026-09-30", "2026-06-30", "2026-03-31"]
    t = _ttm(ends, ends[:4])
    assert _ttm_fx_window(t, "fcf_sbc")[2] == "unzuverlaessig"


def test_ttm_fx_window_vorgaenger_abstand_unplausibel():
    # Vorgaenger 2026-03-31 liegt 92+x? nein: hier fehlenden Quartalszyklus
    # simulieren -- Vorgaenger Ende 2026-03-31 mit aeltestem Ende 2025-12-31
    ends = ["2026-12-31", "2026-09-30", "2026-06-30", "2025-12-31", "2026-03-31"]
    t = _ttm(ends, ["2026-12-31", "2026-09-30", "2026-06-30", "2025-12-31"])
    assert _ttm_fx_window(t, "fcf_sbc")[2] == "unzuverlaessig"


def test_ttm_fx_window_duplikat_und_unsortiert():
    t = _ttm(ENDS5, ["2026-12-31", "2026-12-31", "2026-06-30", "2026-03-31"])
    assert _ttm_fx_window(t, "fcf_sbc")[2] == "unzuverlaessig"
    # unsortierte Lieferung ist erlaubt (aufsteigend sortiert intern), aber
    # ordnungs Widerspruch (Ende aelter als Vorgaengerfenster) faellt auf
    t2 = _ttm(ENDS5, USED4)
    d0, d1, b = _ttm_fx_window(t2, "fcf_sbc")   # unsorted input identisch
    assert b == "quartalsdaten" and d1.year == 2026


def test_ttm_fx_window_luecke_beim_nachziehen():
    # verwendete Quartale mit 182-Tage-Sprung (uebersprungenes Quartal)
    used = ["2026-12-31", "2026-09-30", "2026-03-31", "2025-12-31"]
    ends = sorted(set(used + ["2025-09-30"]), reverse=True)
    t = _ttm(ends, used)
    assert _ttm_fx_window(t, "fcf_sbc")[2] == "unzuverlaessig"


def test_ttm_fx_window_flow_quarters_used_none():
    t = _ttm(ENDS5, None)   # inkonsistente Periodenmenge (z. B. OCF/Capex-Mix)
    assert _ttm_fx_window(t, "fcf_sbc")[2] == "unzuverlaessig"


# --- (3) FY-Nichtueberlappung am echten Fenster ------------------------------

def test_fy_ueberlappt_ausgeschlossen():
    """TTM-Fenster 01.10.2025-30.09.2026: FY2025 (Ende 31.12.2025)
    ueberlappt -> nur TTM + FY2024 + FY2023."""
    ends = ["2026-09-30", "2026-06-30", "2026-03-31", "2025-12-31", "2025-09-30"]
    t = _ttm(ends, ends[:4], fcf_sbc=150.0)
    fy_fcf = {2023: 80.0, 2024: 100.0, 2025: 110.0}
    m = _fx_ticker(ttm=t, fy_fcf=fy_fcf, fy_cur="USD", mcap_cur="USD",
                   fiscal_ends={2023: "2023-12-31", 2024: "2024-12-31",
                                2025: "2025-12-31"})
    res = compute_dcf(m, _cfg(), fcf_mode="ttm")
    assert res.fcf_basis == "ttm"
    names = [n for n, _, _ in res.fcf_sources]
    assert names == ["TTM", "FY2024", "FY2023"]   # FY2025 ueberlappt!
    assert res.fcf_normalized == pytest.approx((150.0 + 100.0 + 80.0) / 3)
    assert res.fcf_window_basis == "quartalsdaten"


def test_fy_vollstaendig_vor_ttm_einbezogen():
    """TTM-Fenster ab 2026-01-01: FY2025 endet davor -> einbezogen."""
    fy_fcf = {2025: 50.0, 2024: 40.0, 2023: 30.0}
    m = _fx_ticker(ttm=_ttm(ENDS5, USED4), fy_fcf=fy_fcf, fy_cur="USD",
                   mcap_cur="USD")
    res = compute_dcf(m, _cfg(), fcf_mode="ttm")
    names = [n for n, _, _ in res.fcf_sources]
    assert names == ["TTM", "FY2025", "FY2024"]


# --- (4) FCF quellenweise FX-Konvertierung (handgerechnet) -------------------

def test_fcf_quellenweis_konvertiert():
    """TTM(60 EUR, 2026) + FY2025(50 EUR) + FY2024(40 EUR);
    Perioden-Ø-Kurse je Jahr: 1.30 / 1.20 / 1.10.
    Erwartung: (60*1.30 + 50*1.20 + 40*1.10) / 3 = 60.666..."""
    conv = _conv({"EURUSD": {"avg:2026": 1.30, "avg:2025": 1.20, "avg:2024": 1.10}})
    fy_fcf = {2024: 40.0, 2025: 50.0, 2023: 1.0}
    m = _fx_ticker(ttm=_ttm(ENDS5, USED4, fcf_sbc=60.0), fy_fcf=fy_fcf)
    res = compute_dcf(m, _cfg(), fx=conv, fcf_mode="ttm")
    assert res.fcf_fx_status == "ok"
    assert res.fcf_normalized_conv == pytest.approx(
        (60.0 * 1.30 + 50.0 * 1.20 + 40.0 * 1.10) / 3)
    # Rohdurchschnitt (Berichtswaehrung) bleibt unveraendert:
    assert res.fcf_normalized == pytest.approx((60.0 + 50.0 + 40.0) / 3)


def test_fcf_nicht_konvertierbar_dcf_na_und_kein_kauf():
    """Cross-Currency ohne Kurse: DCF n/a mit Status/Grund, KEIN Kaufkandidat."""
    conv = _conv({})   # keine Kurse
    m = _fx_ticker(ttm=_ttm(ENDS5, USED4), fy_fcf={2024: 40.0, 2025: 50.0},
                   price=100.0)
    res = compute_dcf(m, _cfg(), fx=conv, fcf_mode="ttm")
    assert math.isnan(res.fcf_normalized_conv)
    assert res.fcf_fx_status in ("fx_fehlgeschlagen",)
    for sc in res.scenarios.values():
        assert sc.value_per_share != sc.value_per_share
    assert math.isnan(res.margin_of_safety)
    assert math.isnan(res.shares_current)   # Kurs EUR vs mcap USD -> Gate NaN
    assert res.shares_current_status == "fx_fehlgeschlagen"
    assert not is_buy_candidate(90.0, res, 100.0, _cfg())


def test_fenster_unzuverlaessig_auch_bei_gleicher_waehrung():
    """Unzuverlaessiges TTM-Fenster: KEINE Mittelung TTM+FY (auch USD/USD) --
    Rueckfall Ø3J wie bei unplausiblen Quartalsdaten (bestehende Kette)."""
    ends = ["2026-12-31", "2026-09-30", "2026-06-30", "2026-03-31"]  # kein Vorgaenger
    t = _ttm(ends, ends[:4], cur="USD", fcf_sbc=150.0)
    fy_fcf = {2023: 80.0, 2024: 100.0, 2025: 110.0}
    m = _fx_ticker(ttm=t, fy_fcf=fy_fcf, fy_cur="USD", mcap_cur="USD")
    res = compute_dcf(m, _cfg(), fcf_mode="ttm")
    assert res.fcf_basis == "annual"            # Rueckfall (bestehende Kette)
    assert res.fcf_normalized == pytest.approx((80.0 + 100.0 + 110.0) / 3)
    names = [n for n, _, _ in res.fcf_sources]
    assert "TTM" not in names                    # keine Mischung mit FY


def test_gleiche_waehrung_ohne_fx_unveraendert():
    """Identische Wahrungen benoetigen keinen FX-Kurs: Werte unveraendert
    (auch ohne fx-Converter, _NoFX-Identitaet)."""
    fy_fcf = {2024: 40.0, 2025: 50.0}
    m1 = _fx_ticker(ttm=_ttm(ENDS5, USED4, cur="USD"), fy_fcf=fy_fcf,
                   fy_cur="USD", mcap_cur="USD")
    r1 = compute_dcf(m1, _cfg(), fcf_mode="ttm")
    assert r1.fcf_fx_status == "ok"
    assert r1.fcf_normalized_conv == pytest.approx(r1.fcf_normalized)


# --- (5) ND als Bestandsgroesse: Stichtagskurs ------------------------------

def test_nd_stichtagskurs_statt_periodendurchschnitt():
    """ND wird zum BILANZSTICHTAG konvertiert (Stichtagskurs 1.25 am
    2025-12-31), nicht mit dem Perioden-Ø (1.20) -- und asof dokumentiert."""
    conv = _conv({"EURUSD": {"2025-12-31": 1.25, "avg:2025": 1.20,
                            "avg:2024": 1.10, "avg:2026": 1.30}})
    fy_fcf = {2024: 40.0, 2025: 50.0}
    m = _fx_ticker(ttm=_ttm(ENDS5, USED4, fcf_sbc=60.0), fy_fcf=fy_fcf,
                   net_debt_fy=200.0)
    res = compute_dcf(m, _cfg(), fx=conv, fcf_mode="ttm")
    assert res.net_debt_known is True
    # TTM-Bestand? Nein: FY2025-ND (2025) herkunft: TTM-ND fehlt ->
    # Rueckgriff auf FY2025-Bilanz mit Stichtag 2025-12-31
    assert res.net_debt_fx_asof == "2025-12-31"
    assert res.net_debt_conv == pytest.approx(200.0 * 1.25)   # Stichtag, nicht Ø


def test_nd_ohne_stichtag_fx_nan():
    """Cross-Currency-ND ohne Stichtag (kein fiscal_end) -> NaN + Status
    fx_fehlgeschlagen (v66-Pfad greift)."""
    conv = _conv({"EURUSD": {"avg:2024": 1.10, "avg:2025": 1.20, "avg:2026": 1.30}})
    fy_fcf = {2024: 40.0, 2025: 50.0}
    # FCF und Marktkap. in USD (kein FY-Fenster noetig), Nettoverschuldung
    # aber in EUR ohne Stichtag (fiscal_ends fehlen) -> ND nicht konvertierbar
    m = _fx_ticker(ttm=_ttm(ENDS5, USED4, cur="USD", fcf_sbc=60.0), fy_fcf=fy_fcf,
                   fy_cur="USD", mcap_cur="USD", net_debt_fy=200.0,
                   nd_cur="EUR", fiscal_ends={})
    res = compute_dcf(m, _cfg(), fx=conv, fcf_mode="ttm")
    assert res.net_debt_known is False
    assert res.net_debt_status == "fx_fehlgeschlagen"
    assert math.isnan(res.margin_of_safety)


# --- (6) shares_current-Gate -------------------------------------------------

def _share_ticker(mcap_cur, price_cur, state, mcap=1000.0, kurs=100.0):
    m = _fx_ticker(fy_fcf={2024: 40.0, 2025: 50.0}, fy_cur="USD",
                   mcap_cur=mcap_cur, mcap=mcap, price=kurs)
    m.price = Metric("price", kurs, price_cur, price_cur, 2025)
    m.fx_unit_state = state
    return m


def test_shares_current_gleiche_waehrung_ok():
    m = _share_ticker("USD", "USD", "normalisiert")
    res = compute_dcf(m, _cfg())
    assert res.shares_current_status == "ok"
    assert res.shares_current == pytest.approx(10.0)


def test_shares_current_gbp_unbekannt_gesperrt():
    m = _share_ticker("GBP", "GBP", "unbekannt")
    res = compute_dcf(m, _cfg())
    assert res.shares_current_status == "einheit_unklar"
    assert math.isnan(res.shares_current)
    assert not is_buy_candidate(90.0, res, 100.0, _cfg())


def test_shares_current_gbp_roh_gesperrt():
    """yfinance GBp-Rohdaten (Kurs in Pence, Faktor 100) -> GESPERRT."""
    m = _share_ticker("GBP", "GBP", "roh_gb_pence")
    res = compute_dcf(m, _cfg())
    assert res.shares_current_status == "einheit_unklar"
    assert math.isnan(res.shares_current)   # NICHT mcap/kurs (Faktor 100!)


def test_shares_current_leere_waehrung_gesperrt():
    """FMP ohne profile.currency -> Preis-Waehrung leer -> NaN (kein USD-Raten)."""
    m = _share_ticker("USD", "", "unbekannt")
    res = compute_dcf(m, _cfg())
    assert res.shares_current_status == "waehrung_unbekannt"
    assert math.isnan(res.shares_current)


def test_shares_current_mismatch_convert_current():
    """Kurs EUR, mcap USD: Umrechnung NUR mit aktuellem Kurs (heute 1.25)
    -> shares = 1000 / 125 = 8; ohne aktuellen Kurs NaN."""
    today = dt.date.today().isoformat()
    conv = _conv({"EURUSD": {today: 1.25}})
    m = _share_ticker("USD", "EUR", "normalisiert")
    res = compute_dcf(m, _cfg(), fx=conv)
    assert res.shares_current_status == "ok"
    assert res.shares_current == pytest.approx(1000.0 / (100.0 * 1.25))
    # ohner Kurs (zu alt): NaN + Status
    conv_alt = _conv({"EURUSD": {(dt.date.today() - dt.timedelta(days=30)).isoformat(): 1.25}})
    res2 = compute_dcf(_share_ticker("USD", "EUR", "normalisiert"), _cfg(), fx=conv_alt)
    assert res2.shares_current_status == "fx_fehlgeschlagen"
    assert math.isnan(res2.shares_current)


# --- (7) Flow-/Stock-Konvertierung in Kriterien ------------------------------

def _kcfg():
    return {"thin_years": 3, "min_points_3y": 2, "min_points_5y": 3,
            "years": 5}


def test_fcf_yield_als_flow_konvertiert():
    """fcf_yield: EUR-FCF / USD-mcap mit Perioden-Ø 1.20 (FY2025).
    Erwartung: (50*1.20)/1000 = 0.06."""
    conv = _conv({"EURUSD": {"avg:2024": 1.10, "avg:2025": 1.20}})
    m = _fx_ticker(fy_fcf={2025: 50.0}, fy_cur="EUR", mcap_cur="USD",
                   fiscal_ends={2024: "2024-12-31", 2025: "2025-12-31"},
                   price=None)
    # FY2025-FCF: raw-Metric heisst in years 'fcf_sbc'
    assert fcf_yield(m, 2025, fx=conv) == pytest.approx(50.0 * 1.20 / 1000.0)
    # ohne Kurs: NaN (kein 1:1-Durchreichen)
    assert math.isnan(fcf_yield(m, 2025, fx=_conv({})))


def test_ev_ebit_flow_und_stock_konvertiert():
    """EV/EBIT v65-Semantik, wahrungskonsistent: EBIT je FY als Flow (Ø 1.10/
    1.20), ND als Stock (Stichtag 1.25) -- handgerechnet."""
    from scoring.criteria import calc_ev_ebit_vs_hist_ebit
    conv = _conv({"EURUSD": {"2024-12-31": 1.05, "2025-12-31": 1.25,
                            "avg:2024": 1.10, "avg:2025": 1.20}})
    _fe = {2022: "2022-12-31", 2023: "2023-12-31", 2024: "2024-12-31",
           2025: "2025-12-31"}
    m = _fx_ticker(fy_fcf={2023: 1.0, 2024: 40.0, 2025: 50.0}, fy_cur="EUR",
                   mcap_cur="USD", ebit=100.0, net_debt_fy=20.0,
                   fiscal_ends=_fe)
    res = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=conv)
    # je FY: EV = 1000 + 20*Stichtag ; EBIT_conv = 100*Ø
    r2024 = (1000.0 + 20.0 * 1.05) / (100.0 * 1.10)
    r2025 = (1000.0 + 20.0 * 1.25) / (100.0 * 1.20)
    #Median ueber 3 Jahre (2023 auch mit avg? 2023 ohne Kurs -> Jahr faellt weg,
    # min_points_5y=3 hier nicht erfuellt -> n/a + dq_flags)
    if math.isnan(res.value):
        assert res.is_na
        assert "fx_nd_fehlgeschlagen" in res.dq_flags   # 2023 ohne Stichtag
    # mit 2023-Kurs erfuellt: separater Check unten
    conv3 = _conv({"EURUSD": {"2023-12-31": 0.95, "2024-12-31": 1.05,
                             "2025-12-31": 1.25, "avg:2023": 1.00,
                             "avg:2024": 1.10, "avg:2025": 1.20}})
    m3 = _fx_ticker(fy_fcf={2024: 40.0, 2025: 50.0, 2023: 1.0}, fy_cur="EUR",
                    mcap_cur="USD", ebit=100.0, net_debt_fy=20.0,
                    fiscal_ends=_fe)
    res3 = calc_ev_ebit_vs_hist_ebit(m3, _kcfg(), fx=conv3)
    r23 = (1000.0 + 20.0 * 0.95) / (100.0 * 1.00)
    r24 = (1000.0 + 20.0 * 1.05) / (100.0 * 1.10)
    r25 = (1000.0 + 20.0 * 1.25) / (100.0 * 1.20)
    vals = sorted([r23, r24, r25])
    median = vals[1]
    assert res3.value == pytest.approx(r25 / median)
    assert res3.dq_flags == []


def test_ev_ebit_ohne_kurs_dq_flag():
    from scoring.criteria import calc_ev_ebit_vs_hist_ebit
    fe = {2022: "2022-12-31", 2023: "2023-12-31", 2024: "2024-12-31",
          2025: "2025-12-31"}
    m = _fx_ticker(fy_fcf={2023: 1.0, 2024: 40.0, 2025: 50.0}, fy_cur="EUR",
                   mcap_cur="USD", ebit=100.0, net_debt_fy=20.0,
                   fiscal_ends=fe)
    res = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=_conv({}))
    assert math.isnan(res.value)
    assert "fx_nd_fehlgeschlagen" in res.dq_flags
    # fx=None (uester Pfad): weiterhin NaN, aber kein falsches Mischverhaeltnis
    res2 = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=None)
    assert math.isnan(res2.value)


# --- (8) Cache-Kompatibilitaet ----------------------------------------------

def test_cache_ohne_flow_quarters_used_konservativ():
    """Alte serialisierte TTM-Eintraege ohne flow_quarters_used laden ohne
    Fehler; die Quelle gilt danach als periodisch UNZUVERLAESSIG
    (konservativer Rueckfall, keine stille Fenster-Annahme)."""
    t = _ttm(ENDS5, USED4, cur="USD", fcf_sbc=150.0)
    ttm_metrics = t.ttm
    d = ttm_to_dict(t)
    del d["flow_quarters_used"]          # altes Format simulieren
    t2 = ttm_from_dict(d)
    assert t2.flow_quarters_used["fcf"] is None
    assert t2.flow_quarters_used["fcf_sbc"] is None
    fy_fcf = {2023: 80.0, 2024: 100.0, 2025: 110.0}
    m = _fx_ticker(fy_fcf=fy_fcf, fy_cur="USD", mcap_cur="USD")
    m.ttm = t2
    m.ttm_plausible = True
    m.ttm_fallback = False
    res = compute_dcf(m, _cfg(), fcf_mode="ttm")
    assert res.fcf_basis == "annual"     # Rueckfall Ø3J (transparent)
    # Roundtrip MIT Feld bleibt belastbar:
    d2 = ttm_to_dict(t)
    assert d2["flow_quarters_used"]["fcf_sbc"] == USED4
    t3 = ttm_from_dict(d2)
    assert t3.flow_quarters_used["fcf_sbc"] == USED4


# --- (9) Ende-zu-Ende: Pipeline-Statusfelder + Kandidatenschutz ---------------

def test_pipeline_statusfelder_und_kein_kauf_bei_fx_mangel():
    from scoring.pipeline import evaluate_all
    conv = _conv({})   # Cross-Currency ohne Kurse
    fy_fcf = {2022: 30.0, 2023: 35.0, 2024: 40.0, 2025: 50.0}
    m = _fx_ticker(ttm=_ttm(ENDS5, USED4, fcf_sbc=60.0), fy_fcf=fy_fcf,
                   price=100.0, net_debt_fy=0.0, mcap=1e9,
                   fiscal_ends={2021: "2021-12-31", 2022: "2022-12-31",
                                2023: "2023-12-31", 2024: "2024-12-31",
                                2025: "2025-12-31"})
    rows, excluded = evaluate_all([m], _cfg(), fx=conv)
    assert rows  # nicht ausgeschlossen (Filter greifen nicht auf DCF)
    r = rows[0]
    assert r["dcf_fcf_fx_status"] == "fx_fehlgeschlagen"
    assert r["shares_current_status"] == "fx_fehlgeschlagen"
    assert not r["is_buy"]
    assert any("Datenqualit" in d for d in r["data_quality"])
    assert r["is_buy"] is False
    # Begruendung im Kandidat-Text (missing-Grund)
    assert r["kandidat"].startswith("nein")


def test_pipeline_konvertiert_ende_zu_ende():
    from scoring.pipeline import evaluate_all
    conv = _conv({"EURUSD": {"2024-12-31": 1.05, "2025-12-31": 1.25,
                            "avg:2024": 1.10, "avg:2025": 1.20, "avg:2026": 1.30}})
    fy_fcf = {2022: 30.0, 2023: 35.0, 2024: 40.0, 2025: 50.0}
    m = _fx_ticker(ttm=_ttm(ENDS5, USED4, fcf_sbc=60.0), fy_fcf=fy_fcf,
                   price=1000.0, net_debt_fy=0.0, mcap=1e9,
                   fiscal_ends={2021: "2021-12-31", 2022: "2022-12-31",
                                2023: "2023-12-31", 2024: "2024-12-31",
                                2025: "2025-12-31"})
    # EUR-Kurs: convert_current (aktueller Kurs) fehlt -> Shares-Gate NaN
    rows, _ = evaluate_all([m], _cfg(), fx=conv)
    r = rows[0]
    assert r["dcf_fcf_fx_status"] == "ok"      # FCF konvertiert (TTM+FY)
    assert r["dcf_fcf_window_basis"] == "quartalsdaten"


# --- (10) Sensitivitaet: identische FX-Regeln -------------------------------

def test_sensitivitaet_identische_fx_regeln():
    conv = _conv({"EURUSD": {"2025-12-31": 1.25, "avg:2024": 1.10,
                            "avg:2025": 1.20, "avg:2026": 1.30}})
    m = _fx_ticker(ttm=_ttm(ENDS5, USED4, fcf_sbc=60.0),
                   fy_fcf={2024: 40.0, 2025: 50.0}, net_debt_fy=0.0)
    cfg = _cfg()
    d = compute_dcf(m, cfg, fx=conv, fcf_mode="ttm")
    sens = sensitivity_mos_base(m, cfg, current_price=None, fx=conv,
                                discount_rates=[d.discount_rate],
                                terminal_growths=[d.terminal_growth])
    mid = sens["vps"].iloc[0]["value_per_share"]
    assert mid == pytest.approx(d.scenarios["base"].value_per_share, rel=1e-9)


def test_sensitivitaet_fenster_unzuverlaessig_konsistent():
    """Unzuverlaessiges TTM-Fenster: Sensitivitaet und compute_dcf nutzen
    denselben (Rueckfall-)Start -- keine divergierenden Werte."""
    ends = ["2026-12-31", "2026-09-30", "2026-06-30", "2026-03-31"]
    t = _ttm(ends, ends[:4], cur="USD", fcf_sbc=150.0)
    fy_fcf = {2023: 80.0, 2024: 100.0, 2025: 110.0}
    m = _fx_ticker(ttm=t, fy_fcf=fy_fcf, fy_cur="USD", mcap_cur="USD",
                   net_debt_fy=0.0,
                   fiscal_ends={2023: "2023-12-31", 2024: "2024-12-31",
                                2025: "2025-12-31"})
    cfg = _cfg()
    d = compute_dcf(m, cfg, fcf_mode="ttm")
    assert d.fcf_basis == "annual"
    sens = sensitivity_mos_base(m, cfg, current_price=None,
                                discount_rates=[d.discount_rate],
                                terminal_growths=[d.terminal_growth])
    mid = sens["vps"].iloc[0]["value_per_share"]
    assert mid == pytest.approx(d.scenarios["base"].value_per_share, rel=1e-9)



# --- (11) v69-Fix: TTM-Skip transparent (auto/ttm) --------------------------

ENDS4_OHNE_VORG = ["2026-12-31", "2026-09-30", "2026-06-30", "2026-03-31"]


def _skip_ticker(fy_fcf, cur="USD", mcap_cur="USD", **kw):
    """TTM mit unzuverlaessigem Fenster (kein Vorgaengerquartal)."""
    t = _ttm(ENDS4_OHNE_VORG, ENDS4_OHNE_VORG[:4], cur=cur, fcf_sbc=150.0)
    return _fx_ticker(ttm=t, fy_fcf=fy_fcf, fy_cur=cur, mcap_cur=mcap_cur,
                      net_debt_fy=0.0, **kw)


def test_auto_ttm_geskippt_status_fenster_unzuverlaessig_geskippt():
    """Matrix 1: auto, Ø3J ok -> Status + DQ Migration; kein MoS-Verlust."""
    m = _skip_ticker({2023: 80.0, 2024: 100.0, 2025: 110.0})
    res = compute_dcf(m, _cfg(), fcf_mode="auto")
    assert res.fcf_basis == "annual"                    # Ø3J-Fallback
    assert res.fcf_normalized == pytest.approx((80.0 + 100.0 + 110.0) / 3)
    assert res.ttm_window_status == "fenster_unzuverlaessig_geskippt"
    assert res.fcf_years_used == 3                       # Ø3J, nicht TTM


def test_auto_ttm_geskippt_pipeline_dq_immer():
    """Matrix 1/2 (E2E): DQ immer (auch bei gueltiger Ø3J-MoS), Row-Feld
    gesetzt; kein zusaetzlicher MoS-Missing-Grund bei berechenbarer MoS."""
    from scoring.pipeline import evaluate_all
    # Filter: mcap >= 500 Mio USD, >= 4 Jahre mit Werten (config filters)
    m = _skip_ticker({2022: 30.0, 2023: 35.0, 2024: 40.0, 2025: 50.0},
                     price=100.0, mcap=1e9,
                     fiscal_ends={2021: "2021-12-31", 2022: "2022-12-31",
                                  2023: "2023-12-31", 2024: "2024-12-31",
                                  2025: "2025-12-31"})
    rows, _ = evaluate_all([m], _cfg(), fx=_conv({}))  # USD/USD: kein FX noetig
    r = rows[0]
    assert r["dcf_ttm_window_status"] == "fenster_unzuverlaessig_geskippt"
    # DQ-Text immer (Pipeline-Text enthaelt Umlaute)
    assert any("TTM-Fenster" in d and "3J-Fallback" in d
               and "übersprungen" in d for d in r["data_quality"])
    # Ø3J-MoS berechenbar -> KEIN "TTM-Fenster ... MoS n/a"-Missing-Grund
    assert "TTM-Fenster" not in r["kandidat"] or "MoS n/a" not in r["kandidat"]


def test_auto_ttm_geskippt_missing_grund_wenn_mos_na():
    """Matrix 2: Ø3J-Quellen fehlen -> Missing-Grund 'TTM-Fenster ...
    Ø3J-Fallback, MoS n/a' statt irrefuehrendem 'MoS Base n/a'."""
    from scoring.pipeline import evaluate_ticker
    # TTM da (unzuverlaessiges Fenster), FY-Jahre NUR mit Umsatz/Aktien
    # (Werte-Jahre fuer Filter) -- aber OHNE FCF -> start=None, MoS n/a
    m = _skip_ticker({}, mcap=1e9,
                     fiscal_ends={2021: "2021-12-31", 2022: "2022-12-31",
                                  2023: "2023-12-31", 2024: "2024-12-31",
                                  2025: "2025-12-31"})
    for fy in (2022, 2023, 2024, 2025):
        m.years[fy] = {
            "revenue": Metric("revenue", 1000.0, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 10.0, "shares", "USD", fy),
        }
    out = evaluate_ticker(m, _cfg())
    assert out["row"] is not None
    r = out["row"]
    assert r["dcf_ttm_window_status"] == "fenster_unzuverlaessig_geskippt"
    assert "TTM-Fenster" in r["kandidat"] and "MoS n/a" in r["kandidat"]


def test_auto_ohne_ttm_status_nicht_verfuegbar():
    """Matrix 3: kein TTM-Objekt -> 'nicht_verfuegbar', kein Skip-DQ."""
    from scoring.pipeline import evaluate_all
    m = _fx_ticker(fy_fcf={2022: 30.0, 2023: 35.0, 2024: 40.0, 2025: 50.0},
                   fy_cur="USD", mcap_cur="USD", net_debt_fy=0.0, mcap=1e9,
                   fiscal_ends={2021: "2021-12-31", 2022: "2022-12-31",
                                2023: "2023-12-31", 2024: "2024-12-31",
                                2025: "2025-12-31"})
    rows, _ = evaluate_all([m], _cfg())
    assert rows
    r = rows[0]
    assert r["dcf_ttm_window_status"] == "nicht_verfuegbar"
    assert not any("TTM-Quelle uebersprungen" in d for d in r["data_quality"])


# --- (12) v69-Fix: ttm_only -- Entscheidungstabelle a-f ---------------------

def test_ttm_only_gleiche_waehrung_ohne_fx_fenster():
    """Fall c: identische bekannte Waehrung + unzuverlaessiges Fenster ->
    TTM wird OHNE FX-Fenster verwendet (keine FY-Mischung in ttm_only)."""
    m = _skip_ticker({2023: 80.0, 2024: 100.0, 2025: 110.0})
    res = compute_dcf(m, _cfg(), fcf_mode="ttm_only")
    assert res.fcf_basis == "ttm"
    assert res.fcf_years_used == 1
    assert res.fcf_normalized == pytest.approx(150.0)
    assert res.ttm_window_status == "nicht_erforderlich_gleiche_waehrung"
    assert res.fcf_fx_status == "ok"   # keine FX-Konvertierung noetig
    assert res.margin_of_safety == res.margin_of_safety   # MoS berechenbar


def test_ttm_only_verschiedene_waehrung_fenster_unzuverlaessig():
    """Fall d: bekannte unterschiedliche Waehrungen + unzuverlaessiges
    Fenster -> fenster_unzuverlaessig, DCF n/a, KEIN Ø3J-Ersatz."""
    conv = _conv({})   # bewusst ohne Kurse: scheitert erst recht nicht am
    # Fenster, sondern am Periodennachweis -- Status MUSS fensterseitig sein
    ends = ENDS4_OHNE_VORG
    t = _ttm(ends, ends[:4], cur="EUR", fcf_sbc=150.0)
    m = _fx_ticker(ttm=t, fy_fcf={2023: 80.0, 2024: 100.0, 2025: 110.0},
                   fy_cur="EUR", mcap_cur="USD", net_debt_fy=0.0)
    res = compute_dcf(m, _cfg(), fx=conv, fcf_mode="ttm_only")
    assert res.fcf_basis == "ttm"                     # KEIN Ø3J-Fallback
    assert res.fcf_years_used == 1
    assert res.fcf_normalized == pytest.approx(150.0)  # TTM-Wert unveraendert
    assert res.ttm_window_status == "fenster_unzuverlaessig"
    assert res.fcf_fx_status == "fenster_unzuverlaessig"
    assert math.isnan(res.fcf_normalized_conv)
    assert math.isnan(res.margin_of_safety)
    assert not is_buy_candidate(90.0, res, 100.0, _cfg())


def test_ttm_only_unbekannte_quellwaehrung_quelle_unbekannt():
    """Fall e: TTM-Quellwaehrung leer + unzuverlaessiges Fenster ->
    quelle_unbekannt (kein fenster_unzuverlaessig), kein Fensterlabel."""
    ends = ENDS4_OHNE_VORG
    t = _ttm(ends, ends[:4], cur="", fcf_sbc=150.0)   # Währung unbekannt
    m = _fx_ticker(ttm=t, fy_fcf={2023: 80.0, 2024: 100.0, 2025: 110.0},
                   fy_cur="EUR", mcap_cur="USD", net_debt_fy=0.0)
    res = compute_dcf(m, _cfg(), fx=_conv({}), fcf_mode="ttm_only")
    assert res.fcf_basis == "ttm"                     # KEIN Ø3J-Fallback
    assert res.fcf_years_used == 1
    assert res.fcf_fx_status == "quelle_unbekannt"
    assert res.ttm_window_status == ""                # kein Fensterlabel
    assert math.isnan(res.fcf_normalized_conv)


def test_ttm_only_unbekannte_zielwaehrung_quelle_unbekannt():
    """Fall e (Variante): Zielwaehrung (mcap) leer -> quelle_unbekannt,
    auch bei zuverlaessigem Fenster -- Fensterprüfung wird nicht erreicht."""
    m = _fx_ticker(ttm=_ttm(ENDS5, USED4, cur="EUR", fcf_sbc=150.0),
                   fy_fcf={2023: 80.0, 2024: 100.0, 2025: 110.0},
                   fy_cur="EUR", mcap_cur="", net_debt_fy=0.0)
    res = compute_dcf(m, _cfg(), fx=_conv({}), fcf_mode="ttm_only")
    assert res.fcf_fx_status == "quelle_unbekannt"
    assert res.ttm_window_status == ""
    assert res.fcf_years_used == 1
    assert math.isnan(res.fcf_normalized_conv)


def test_ttm_only_kein_ttm_dcf_na_pipeline_grund():
    """Fall f (E2E): kein TTM -> DCF n/a, Missing-Grund 'TTM-Start nicht
    verwendbar (ttm_only)', KEIN Ø3J (mos_ttm_only bleibt n/a)."""
    from scoring.pipeline import evaluate_all
    cfg = _cfg()
    cfg["dcf"]["fcf_mode"] = "ttm_only"
    # KEIN TTM und KEINE Jahres-FCF-Werte (nur Umsatz/Aktien als Wertjahre
    # fuer die Filter): ohne ttm_only-Rueckfall waere avg3 greifbar --
    # genau das darf NICHT passieren. MoS in JEDEM Modus n/a -> die
    # Missing-Kette erreicht den ttm_only-Zweig.
    m = _fx_ticker(fy_fcf={}, fy_cur="USD", mcap_cur="USD", price=100.0,
                   net_debt_fy=0.0, mcap=1e9,
                   fiscal_ends={2021: "2021-12-31", 2022: "2022-12-31",
                                2023: "2023-12-31", 2024: "2024-12-31",
                                2025: "2025-12-31"})
    for fy in (2022, 2023, 2024, 2025):
        m.years[fy] = {
            "revenue": Metric("revenue", 1000.0, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", 10.0, "shares", "USD", fy),
        }
    # Referenz: im auto-Modus WAERE der Ø3J-Rueckfall greifbar
    # (identischer Ticker wird von avg3... nicht geliefert, da kein FCF) --
    # hier bewusst ohne FCF, damit die Kette den ttm_only-Grund erreicht.
    rows, _ = evaluate_all([m], cfg)
    assert rows
    r = rows[0]
    assert r["dcf_ttm_window_status"] == "nicht_verfuegbar"
    # KEIN Ø3J-Ersatz: mos in JEDEM Modus n/a, Grund benennt ttm_only
    assert "TTM-Start nicht verwendbar (ttm_only)" in r["kandidat"]
    assert math.isnan(r["mos_ttm_only"])


def test_ttm_only_fenster_zuverlaessig_verhalten_unveraendert():
    """Faelle a/b: zuverlaessiges Fenster -> bisheriges Verhalten
    (identisch: keine Konvertierung noetig; Status 'quartalsdaten')."""
    m1 = _fx_ticker(ttm=_ttm(ENDS5, USED4, cur="USD", fcf_sbc=60.0),
                    fy_fcf={2024: 40.0, 2025: 50.0},
                    fy_cur="USD", mcap_cur="USD", net_debt_fy=0.0)
    r1 = compute_dcf(m1, _cfg(), fcf_mode="ttm_only")
    assert r1.fcf_basis == "ttm"
    assert r1.fcf_years_used == 1
    assert r1.fcf_normalized == pytest.approx(60.0)
    assert r1.ttm_window_status == "quartalsdaten"
    assert r1.fcf_fx_status == "ok"


def test_ttm_modus_unverandert_streng():
    """'ttm' (TTM+FY): unzuverlaessiges Fenster -> Strenges Gate bleibt
    (Ø3J-Fallback + geskippt-Status, unabhaengig von der Waehrung)."""
    m = _skip_ticker({2023: 80.0, 2024: 100.0, 2025: 110.0}, cur="USD")
    res = compute_dcf(m, _cfg(), fcf_mode="ttm")
    assert res.fcf_basis == "annual"
    assert res.ttm_window_status == "fenster_unzuverlaessig_geskippt"
    names = [n for n, _, _ in res.fcf_sources]
    assert "TTM" not in names


# --- (13) v69-Fix: Ursachenreihenfolge + globale Aggregation ----------------

def _agg_ticker(reliable=True):
    m = TickerMetrics(ticker="AGG")
    if reliable:
        m.ttm = _ttm(ENDS5, USED4, cur="EUR", fcf_sbc=60.0)
    else:
        m.ttm = _ttm(ENDS4_OHNE_VORG, ENDS4_OHNE_VORG[:4], cur="EUR",
                     fcf_sbc=60.0)
    m.fiscal_ends = {2024: "2024-12-31", 2025: "2025-12-31"}
    return m


def test_aggregation_A_fx_fehlgeschlagen_und_quelle_unbekannt():
    """Matrix 11/A: zuerst gepruefte Quelle fx_fehlgeschlagen (Fenster ok,
    Kurs fehlt), spaetere Quelle quelle_unbekannt (Waehrung leer) ->
    global priorisiert: quelle_unbekannt."""
    from valuation.dcf import _convert_fcf_sources
    m = _agg_ticker(reliable=True)
    start = {"sources": [("TTM", 60.0, "EUR"), ("FY2025", 50.0, "")],
             "ttm_key": "fcf_sbc"}
    wert, status, _basis = _convert_fcf_sources(m, start, _conv({}), "USD")
    assert math.isnan(wert)
    assert status == "quelle_unbekannt"


def test_aggregation_B_fenster_und_fx_fehlgeschlagen():
    """Matrix 12/B: TTM fenster_unzuverlaessig + FY fx_fehlgeschlagen ->
    fenster_unzuverlaessig (Prioritaet der Ursachenliste)."""
    from valuation.dcf import _convert_fcf_sources
    m = _agg_ticker(reliable=False)
    m.fiscal_ends = {2025: "2025-12-31", 2024: "2024-12-31"}
    start = {"sources": [("TTM", 60.0, "EUR"), ("FY2025", 50.0, "EUR")],
             "ttm_key": "fcf_sbc"}
    wert, status, basis = _convert_fcf_sources(m, start, _conv({}), "USD")
    assert math.isnan(wert)
    assert status == "fenster_unzuverlaessig"
    assert basis == "unzuverlaessig"


def test_aggregation_C_quelle_und_fenster():
    """Matrix 13/C: FY-Label unparsebar (quelle_unbekannt) + TTM
    fenster_unzuverlaessig -> quelle_unbekannt gewinnt."""
    from valuation.dcf import _convert_fcf_sources
    m = _agg_ticker(reliable=False)
    start = {"sources": [("FY-XX", 50.0, "EUR"), ("TTM", 60.0, "EUR")],
             "ttm_key": "fcf_sbc"}
    wert, status, _basis = _convert_fcf_sources(m, start, _conv({}), "USD")
    assert math.isnan(wert)
    assert status == "quelle_unbekannt"


def test_aggregation_D_fx_fehlgeschlagen_und_fy_fenster():
    """Matrix 14/D: TTM fx_fehlgeschlagen (Fenster ok, Kurs fehlt) +
    FY fy_fenster_unzuverlaessig (kein Vorjahresende) -> fy_fenster_
    unzuverlaessig."""
    from valuation.dcf import _convert_fcf_sources
    m = _agg_ticker(reliable=True)
    m.fiscal_ends = {2025: "2025-12-31"}    # FY2025 ohne Vorjahresende
    start = {"sources": [("TTM", 60.0, "EUR"), ("FY2025", 50.0, "EUR")],
             "ttm_key": "fcf_sbc"}
    wert, status, _basis = _convert_fcf_sources(m, start, _conv({}), "USD")
    assert math.isnan(wert)
    assert status == "fy_fenster_unzuverlaessig"


def test_aggregation_E_alle_ok_mittelung_unveraendert():
    """Matrix 15/E: alle Quellen konvertierbar -> quellenweise Konvertierung
    und Mittelung unveraendert (handgerechnet)."""
    from valuation.dcf import _convert_fcf_sources
    m = _agg_ticker(reliable=True)
    conv = _conv({"EURUSD": {"avg:2026": 1.30, "avg:2025": 1.20}})
    start = {"sources": [("TTM", 60.0, "EUR"), ("FY2025", 50.0, "EUR")],
             "ttm_key": "fcf_sbc"}
    wert, status, basis = _convert_fcf_sources(m, start, conv, "USD")
    assert status == "ok"
    assert basis == "quartalsdaten"
    assert wert == pytest.approx((60.0 * 1.30 + 50.0 * 1.20) / 2)


def test_aggregation_FEHLWERT_identisch_vor_allem():
    """Reihenfolge 1: identische bekannte Waehrung -> Kurzschluss, keine
    FX-/Fensterpruefung (unzuverlaessiges Fenster stoert nicht)."""
    from valuation.dcf import _convert_fcf_sources
    m = _agg_ticker(reliable=False)
    start = {"sources": [("TTM", 60.0, "EUR"), ("FY2025", 50.0, "EUR")],
             "ttm_key": "fcf_sbc"}
    wert, status, _basis = _convert_fcf_sources(m, start, _conv({}), "EUR")
    assert status == "ok"
    assert wert == pytest.approx((60.0 + 50.0) / 2)


def test_aggregation_leere_zielwaehrung_vor_fenster():
    """Reihenfolge 2: fehlende Zielwaehrung -> quelle_unbekannt, BEVOR das
    (hier zuverlaessige) TTM-Fenster geprueft wird -- nicht fx_fehlgeschlagen."""
    from valuation.dcf import _convert_fcf_sources
    m = _agg_ticker(reliable=True)
    start = {"sources": [("TTM", 60.0, "EUR"), ("FY2025", 50.0, "EUR")],
             "ttm_key": "fcf_sbc"}
    wert, status, _basis = _convert_fcf_sources(m, start, _conv({}), "")
    assert math.isnan(wert)
    assert status == "quelle_unbekannt"


def test_ttm_key_der_tatsaechlich_verwendete_quelle():
    """Matrix 16: Fensterpruefung nutzt den ttm_key der EFFEKTIV
    ausgewaehlten Quelle (fcf vs. fcf_sbc) -- unterschiedliche
    flow_quarters_used-Saetze schlagen je nach Key unterschiedlich durch."""
    from valuation.dcf import _convert_fcf_sources
    m = TickerMetrics(ticker="KEY")
    m.ttm = _ttm(ENDS5, USED4, cur="EUR", fcf_sbc=60.0)
    m.ttm.flow_quarters_used["fcf_sbc"] = USED4       # fcf_sbc zuverlaessig
    m.ttm.flow_quarters_used["fcf"] = None            # Roh-FCF inkonsistent
    start = {"sources": [("TTM", 60.0, "EUR")], "ttm_key": "fcf"}
    wert, status, _basis = _convert_fcf_sources(m, start, _conv({}), "USD")
    assert status == "fenster_unzuverlaessig"     # Key "fcf" -> unzuverlaessig
    start2 = {"sources": [("TTM", 60.0, "EUR")], "ttm_key": "fcf_sbc"}
    wert2, status2, basis2 = _convert_fcf_sources(m, start2, _conv({}), "USD")
    assert status2 == "fx_fehlgeschlagen"          # Key "fcf_sbc": Fenster ok
    assert basis2 == "quartalsdaten"


# --- (14) v69-Fix: EV/EBIT- und FCF-Yield-DQ Klassifizierung -----------------

def _ev_ebit_ticker(**kw):
    kw.setdefault("fy_cur", "EUR")
    kw.setdefault("mcap_cur", "USD")
    kw.setdefault("ebit", 100.0)
    kw.setdefault("net_debt_fy", 20.0)
    fe = kw.pop("fiscal_ends", None)
    if fe is None:
        fe = {2022: "2022-12-31", 2023: "2023-12-31", 2024: "2024-12-31",
              2025: "2025-12-31"}
    kw["fiscal_ends"] = fe
    return _fx_ticker(fy_fcf={2023: 1.0, 2024: 40.0, 2025: 50.0}, **kw)


def test_ev_ebit_nd_waehrung_unbekannt_flag():
    """Matrix 17: ND-Wert vorhanden, ND-Waehrung leer ->
    fx_nd_waehrung_unbekannt (nicht fx_fehlgeschlagen)."""
    from scoring.criteria import calc_ev_ebit_vs_hist_ebit
    m = _ev_ebit_ticker()
    m.years[2025]["net_debt"].currency = ""
    res = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=_conv({"EURUSD": {
        "avg:2024": 1.10, "avg:2025": 1.20, "2024-12-31": 1.05,
        "2025-12-31": 1.25, "avg:2023": 1.00, "2023-12-31": 0.95}}))
    assert math.isnan(res.value) or res.value != res.value
    assert "fx_nd_waehrung_unbekannt" in res.dq_flags
    assert "fx_nd_fehlgeschlagen" not in res.dq_flags


def test_ev_ebit_nd_stichtag_unbekannt_flag():
    """Matrix 18: bekannte unterschiedliche Waehrungen ohne Bilanzstichtag
    -> fx_nd_stichtag_unbekannt (kein irrefuehrendes fx_fehlgeschlagen)."""
    from scoring.criteria import calc_ev_ebit_vs_hist_ebit
    m = _ev_ebit_ticker(fiscal_ends={})
    res = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=_conv({}))
    assert "fx_nd_stichtag_unbekannt" in res.dq_flags
    assert "fx_nd_fehlgeschlagen" not in res.dq_flags
    assert math.isnan(res.value)


def test_ev_ebit_ebit_waehrung_unbekannt_flag():
    """Matrix 19: EBIT-Waehrung leer -> fx_ebit_waehrung_unbekannt."""
    from scoring.criteria import calc_ev_ebit_vs_hist_ebit
    m = _ev_ebit_ticker(nd_cur="USD")   # ND identisch zur mcap-Waehrung
    m.years[2025]["ebit"].currency = ""
    res = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=_conv({}))
    assert "fx_ebit_waehrung_unbekannt" in res.dq_flags


def test_ev_ebit_ebit_fenster_unzuverlaessig_flag():
    """Matrix 20: bekannte unterschiedliche Waehrungen, FY-Fenster
    unzuverlaessig (Vorjahresende fehlt) -> fx_ebit_fenster_unzuverlaessig."""
    from scoring.criteria import calc_ev_ebit_vs_hist_ebit
    m = _ev_ebit_ticker(nd_cur="USD",
                        fiscal_ends={2025: "2025-12-31"})
    res = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=_conv({}))
    assert "fx_ebit_fenster_unzuverlaessig" in res.dq_flags


def test_ev_ebit_identische_waehrung_kein_fenster_flag():
    """Matrix 20 (Negativ): identische Waehrungen -> kein FX-Flag trotz
    unzuverlaessigem FY-Fenster (Kurzschluss vor Fensterpruefung)."""
    from scoring.criteria import calc_ev_ebit_vs_hist_ebit
    m = _ev_ebit_ticker(fy_cur="USD", mcap_cur="USD",
                        fiscal_ends={2025: "2025-12-31"})
    res = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=_conv({}))
    assert res.dq_flags == []


def test_ev_ebit_fx_fehlgeschlagen_bei_fenster_und_stichtag_ok():
    """Matrix 21: bekannte unterschiedliche Waehrungen, Fenster/Stichtag ok,
    Kurse fehlen -> fx_nd_/fx_ebit_fehlgeschlagen (keine Rueckstufung)."""
    from scoring.criteria import calc_ev_ebit_vs_hist_ebit
    m = _ev_ebit_ticker(nd_cur="USD")   # ND identisch -> nur EBIT scheitert
    res = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=_conv({}))
    assert "fx_ebit_fehlgeschlagen" in res.dq_flags
    assert "fx_nd_waehrung_unbekannt" not in res.dq_flags


def test_ev_ebit_unbrauchbare_mcap_keine_fx_flags():
    """Matrix 22: Marktkapitalisierung <= 0 -> Kriterium n/a, KEIN
    FX-Flag (unbrauchbare mcap ist kein FX-Fehler)."""
    from scoring.criteria import calc_ev_ebit_vs_hist_ebit
    m = _ev_ebit_ticker(mcap=0.0)
    res = calc_ev_ebit_vs_hist_ebit(m, _kcfg(), fx=_conv({}))
    assert math.isnan(res.value)
    assert res.dq_flags == []


def test_fcf_yield_mcap_unbrauchbar_kein_fx_flag():
    """Matrix 23: mcap <= 0 bei vorhandenen fcf_sbc-Werten -> Yield NaN,
    KEIN FX-Flag."""
    from scoring.criteria import calc_fcf_yield_sbc_avg3
    m = _ev_ebit_ticker(mcap=0.0, net_debt_fy=None, ebit=None)
    m.years[2025]["fcf_sbc"] = Metric("fcf_sbc", 50.0, "EUR", "EUR", 2025)
    res = calc_fcf_yield_sbc_avg3(m, _kcfg(), fx=_conv({}))
    assert math.isnan(res.value)
    assert res.dq_flags == []


def test_fcf_yield_waehrung_unbekannt_flag():
    """Matrix 24: mcap-Waehrung leer (FMP ohne profile.currency) ->
    waehrung_unbekannt; auch BEIDE leer wird nicht als 'gleich' gelesen."""
    from scoring.criteria import calc_fcf_yield_sbc_avg3
    m = _ev_ebit_ticker(net_debt_fy=None, ebit=None)
    m.years[2025]["fcf_sbc"] = Metric("fcf_sbc", 50.0, "EUR", "EUR", 2025)
    m.market_cap.currency = ""
    m.market_cap.unit = ""
    res = calc_fcf_yield_sbc_avg3(m, _kcfg(), fx=_conv({}))
    assert "waehrung_unbekannt" in res.dq_flags


def test_fcf_yield_fenster_unzuverlaessig_flag():
    """Matrix 25: bekannte unterschiedliche Waehrungen, FY-Fenster
    unzuverlaessig -> fenster_unzuverlaessig (statt fx_fehlgeschlagen)."""
    from scoring.criteria import calc_fcf_yield_sbc_avg3
    m = _ev_ebit_ticker(net_debt_fy=None, ebit=None,
                        fiscal_ends={2025: "2025-12-31"})
    m.years[2025]["fcf_sbc"] = Metric("fcf_sbc", 50.0, "EUR", "EUR", 2025)
    res = calc_fcf_yield_sbc_avg3(m, _kcfg(), fx=_conv({}))
    assert "fenster_unzuverlaessig" in res.dq_flags


def test_fcf_yield_fx_fehlgeschlagen_bei_fenster_ok():
    """Matrix 26: Waehrungen bekannt/unterschiedlich, FY-Fenster ok,
    Kurse fehlen -> fx_fehlgeschlagen."""
    from scoring.criteria import calc_fcf_yield_sbc_avg3
    m = _ev_ebit_ticker(net_debt_fy=None, ebit=None)
    m.years[2025]["fcf_sbc"] = Metric("fcf_sbc", 50.0, "EUR", "EUR", 2025)
    res = calc_fcf_yield_sbc_avg3(m, _kcfg(), fx=_conv({}))
    assert "fx_fehlgeschlagen" in res.dq_flags
    assert "waehrung_unbekannt" not in res.dq_flags


def test_fcf_yield_dq_duplikatfrei():
    """Matrix 27: 3 Jahre gleiche Ursache -> Flag nur EINMAL in dq_flags
    und Pipeline-DQ-Text nur einmal."""
    from scoring.criteria import calc_fcf_yield_sbc_avg3
    from scoring.pipeline import evaluate_all
    cfg = _cfg()
    m = _ev_ebit_ticker(net_debt_fy=None, ebit=None, mcap=1e9)
    for fy in (2023, 2024, 2025):
        m.years[fy]["fcf_sbc"] = Metric("fcf_sbc", 50.0, "EUR", "EUR", fy)
    # 4. Wertjahr fuer Filter (nur Umsatz/Aktien -- kein FCF)
    m.years[2022] = {"revenue": Metric("revenue", 1000.0, "EUR", "EUR", 2022),
                     "shares_diluted": Metric("shares_diluted", 10.0,
                                              "shares", "EUR", 2022)}
    res = calc_fcf_yield_sbc_avg3(m, _kcfg(), fx=_conv({}))
    assert res.dq_flags.count("fx_fehlgeschlagen") == 1
    rows, _ = evaluate_all([m], cfg, fx=_conv({}))
    texts = [d for d in rows[0]["data_quality"] if "FCF-Yield" in d
             and "FX-konvertierbar" in d]
    assert len(texts) == 1


def test_fcf_yield_identische_waehrung_kein_fx_flag():
    """Matrix 28: identische bekannte Waehrungen -> keine FX-Flags
    (Yield direkt berechenbar, unveraendertes Verhalten)."""
    from scoring.criteria import calc_fcf_yield_sbc_avg3
    m = _ev_ebit_ticker(net_debt_fy=None, ebit=None, fy_cur="USD",
                        mcap_cur="USD")
    # fcf_sbc existiert (Helper) 2023/2024/2025: Ø(50+40+1)/1000
    res = calc_fcf_yield_sbc_avg3(m, _kcfg(), fx=_conv({}))
    assert res.value == pytest.approx((50.0 + 40.0 + 1.0) / 3 / 1000.0)
    assert res.dq_flags == []
