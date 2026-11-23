r"""(v72) Zinsaufwand-Zweitquelle SEC -- Regressionstests ohne Netz.

Abgedeckt (Freigabe-Matrix + Zusatzpunkte):
- FY-Match: exaktes Periodenende (+-7 Tage, 52/53-Wochen-Jahre), Dauer
  340-380 Tage, Restatement -> juengstes filed gewinnt; falsches Ende ignoriert
- Tag-Prioritaet: InterestExpense > InterestExpenseNonoperating >
  InterestExpenseDebt > InterestAndDebtExpense; InterestPaidNet/Netto-Tags
  werden NIEMALS verwendet; Nicht-USD-Filings ignoriert
- TTM = FY + YTD(aktuell) - YTD(Vorjahr) (keine Einzelquartal-Summe);
  fehlende Komponente -> NaN; Q4-Sonderfall (TTM endet am FY-Ende -> TTM = FY);
  gleiche Tag-Wahl (kein Mischen zwischen Tags)
- yfinance-Vorrang: SEC nur wenn yfinance NaN; Herkunfts-Flag zins:sec:<tag>
- Wesentlichkeitsregel: greift / genau an der 5-%-Grenze nicht / ND-Mcap-
  Grenze / Altersgrenze; Aufschlag 0 + Flag + Schaetzungsfeld
- Abweichungsflag zins_quellen_abweichend (> 20 %, nur Info; Rechenwert
  bleibt yfinance) -- nur wenn die companyfacts aus dem Cache kamen
- SEC_USER_AGENT: fehlende/ungueltige Variable -> RuntimeError vor dem
  ersten Request
- Boersensuffix -> kein SEC-Abruf (US-GAAP-Schutz wie fcff)
- Cache: TTL abgelaufen -> Neuladen; frozen ohne Cache-Eintrag -> kein Abruf
- TickerMetrics.to_dict/from_dict-Rundlauf von sec_interest; Alt-Cache ohne
  Feld -> Default {}
"""

import copy
import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from data import sec as sec_mod
from data.loader import Metric, TickerMetrics
from valuation.dcf import compute_dcf


# ---------------------------------------------------------------- Fixtures --

def _cfg(sec_over=None, dcf_over=None):
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["sec"] = {"enabled": True, "cache_dir": "", "cache_ttl_hours": 24}
    cfg["sec"].update(sec_over or {})
    cfg["dcf"] = dict(cfg["dcf"])
    cfg["dcf"].update(dcf_over or {})
    return cfg


def _facts(entries_by_tag, usd=True):
    """Synthetische companyfacts: {tag: [ {form,start,end,val,filed} ]}."""
    units = "USD" if usd else "EUR"
    return {"facts": {"us-gaap": {
        tag: {"units": {units: l}} for tag, l in entries_by_tag.items()}}}


def _e(start, end, val, filed="2026-02-01", form="10-K"):
    return {"form": form, "start": start, "end": end, "val": val, "filed": filed}


def _ticker(interest_by_fy=None, fcf_by_fy=None, net_debt=0.0, mcap=100000.0,
            price=10.0, ticker="ZST", ttm=None):
    if interest_by_fy is None:
        interest_by_fy = {}
    if fcf_by_fy is None:
        fcf_by_fy = {2023: 100.0, 2024: 100.0, 2025: 100.0}
    m = TickerMetrics(ticker=ticker, country="US")
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    m.price = Metric("price", price, "USD", "USD", 2025)
    rev0 = 1000.0
    for i, fy in enumerate(sorted(fcf_by_fy)):
        m.years[fy] = {
            "fcf_sbc": Metric("fcf_sbc", fcf_by_fy[fy], "USD", "USD", fy),
            "revenue": Metric("revenue", rev0 * 1.05 ** i, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", mcap / price,
                                     "shares", "USD", fy),
            "net_debt": Metric("net_debt", net_debt, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 100.0,
                                         "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 500.0,
                                        "USD", "USD", fy),
        }
        if fy in interest_by_fy:
            m.years[fy]["interest_expense"] = Metric(
                "interest_expense", interest_by_fy[fy], "USD", "USD", fy)
        m.fiscal_ends[fy] = f"{fy}-12-31"
    if ttm is not None:
        m.ttm = ttm
        m.ttm_plausible = True
        m.ttm_fallback = False
    return m


def _patch_sec(monkeypatch, facts, captured=None):
    """CIK + companyfacts patchen (kein Netz). captured['fetch'] zaehlt Abrufe.
    Memory-Cache leeren, damit jeder Test mit definiertem Zustand startet."""
    sec_mod._FACTS_MEM_CACHE.clear()
    monkeypatch.setattr(sec_mod, "_cik_for_ticker", lambda t: "0000000001")
    def fake_fetch(cik, ua):
        if captured is not None:
            captured["fetch"] = captured.get("fetch", 0) + 1
        return facts
    monkeypatch.setattr(sec_mod, "_fetch_companyfacts", fake_fetch)


def _fallback(m, cfg):
    return sec_mod.sec_interest_fallback(m, cfg)


# ------------------------------------------------- (1) FY-Match/Restatement --

def test_fy_match_exakt_und_juengstes_filed_gewinnt(monkeypatch):
    """FY2025 endet 2025-12-31; zwei Fakten (Restatement) -> juengstes filed."""
    facts = _facts({"InterestExpense": [
        _e("2025-01-01", "2025-12-31", 100.0, filed="2026-02-01"),
        _e("2025-01-01", "2025-12-31", 120.0, filed="2026-04-30"),  # Restatement
        _e("2024-01-01", "2024-12-31", 90.0, filed="2025-02-01"),
        _e("2025-02-01", "2026-01-31", 111.0, filed="2026-02-01"),  # falsches Ende
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker()
    out = _fallback(m, _cfg())
    assert out["FY"][2025] == (120.0, "InterestExpense")
    assert out["FY"][2024] == (90.0, "InterestExpense")
    assert 2026 not in out["FY"]  # falsches Ende wird nicht gematcht


def test_fy_match_52_53_wochen_toleranz(monkeypatch):
    """52/53-Wochen-FJ (z. B. 2025-09-27 statt 2025-12-31 +-7 Tage): das
    fiscal_end liegt am 29.11.2025, der Fakt endet am 04.12.2025 (5 Tage
    daneben) -> passt in die +-7-Tage-Toleranz."""
    facts = _facts({"InterestExpense": [
        _e("2024-12-01", "2025-12-04", 70.0),  # 5 Tage neben dem fiscal_end
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker()
    m.fiscal_ends[2025] = "2025-11-29"
    out = _fallback(m, _cfg())
    assert out["FY"] != {} and any(abs(v[0] - 70.0) < 1e-9 for v in out["FY"].values())


def test_52_53_wochen_toleranz_ueberschritten(monkeypatch):
    """Endet das SEC-Fakt mehr als 7 Tage daneben -> KEIN Match."""
    facts = _facts({"InterestExpense": [
        _e("2024-11-30", "2025-11-14", 70.0),  # 15 Tage daneben
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker()
    m.fiscal_ends[2025] = "2025-11-29"
    out = _fallback(m, _cfg())
    assert out["FY"] == {}


# ------------------------------------------------------ (2) Tag-Prioritaet --

def test_tag_prioritaet_erster_verfuegbarer_gewinnt(monkeypatch):
    facts = _facts({
        "InterestExpense": [_e("2025-01-01", "2025-12-31", 100.0)],
        "InterestExpenseNonoperating": [_e("2025-01-01", "2025-12-31", 200.0)],
        "InterestExpenseDebt": [_e("2025-01-01", "2025-12-31", 300.0)],
        "InterestAndDebtExpense": [_e("2025-01-01", "2025-12-31", 400.0)],
    })
    _patch_sec(monkeypatch, facts)
    out = _fallback(_ticker(), _cfg())
    assert out["FY"][2025][1] == "InterestExpense"  # hoechste Prioritaet
    # ohne InterestExpense (zweit hoechste gewinnt):
    del facts["facts"]["us-gaap"]["InterestExpense"]
    sec_mod._FACTS_MEM_CACHE.clear()
    out2 = sec_mod.sec_interest_fallback(_ticker(), _cfg())
    assert out2["FY"].get(2025, (0, ""))[1] == "InterestExpenseNonoperating"


def test_nicht_usd_ignoriert(monkeypatch):
    """Non-USD-Filing (EUR) wird ignoriert -- keine stille Umrechnung."""
    facts = _facts({"InterestExpense": [_e("2025-01-01", "2025-12-31", 100.0)]},
                   usd=False)
    _patch_sec(monkeypatch, facts)
    out = _fallback(_ticker(), _cfg())
    assert out["FY"] == {} and out["TTM"] is None


def test_interestpaidnet_wird_nie_verwendet(monkeypatch):
    """Nur InterestPaidNet vorhanden -> NaN (Cash-Zahlung, kein GuV-Aufwand)."""
    facts = _facts({"InterestPaidNet": [_e("2025-01-01", "2025-12-31", 100.0)],
                    "NetInterestIncome": [_e("2025-01-01", "2025-12-31", 50.0)]})
    _patch_sec(monkeypatch, facts)
    out = _fallback(_ticker(), _cfg())
    assert out["FY"] == {} and out["TTM"] is None


# -------------------------------------------------------- (3) TTM-Formel --

def _ttm_result(quarter_end="2026-09-30", fcf=100.0, interest=float("nan")):
    from data.ttm import TtmResult
    met = {"fcf_sbc": Metric("fcf_sbc", fcf, "USD", "USD", 2026),
           "interest_expense": Metric("interest_expense", interest,
                                      "USD", "USD", 2026)}
    quarters = [{"quarter_end": q, "ocf": fcf / 4, "capex": 0.0,
                 "sbc": 0.0, "fcf_sbc": fcf / 4, "yoy": None}
                for q in ("2025-12-31", "2026-03-31", "2026-06-30", quarter_end)]
    return TtmResult(ttm=met, quarter_end=quarter_end, base_label="TTM",
                     quarters=quarters, quarters_currency="USD")


def test_ttm_formel_fy_plus_ytd_minus_ytd_vorjahr(monkeypatch):
    """TTM bis 2026-09-30 = FY2025 + YTD(9M 2026) - YTD(9M 2025) (gleicher Tag).
    Handwerte: 400 + 300 - 260 = 440."""
    facts = _facts({"InterestExpense": [
        # FY im TTM-Fenster
        _e("2025-01-01", "2025-12-31", 400.0, filed="2026-02-01"),
        # YTD aktuell (Jan-Sep 2026), Dauer 273
        _e("2026-01-01", "2026-09-30", 300.0, form="10-Q", filed="2026-10-28"),
        # YTD Vorjahr (Jan-Sep 2025)
        _e("2025-01-01", "2025-09-30", 260.0, form="10-Q", filed="2025-10-29"),
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker(ttm=_ttm_result())
    out = _fallback(m, _cfg())
    assert out["TTM"] is not None
    val, tag = out["TTM"]
    assert abs(val - 440.0) < 1e-9 and tag == "InterestExpense"


def test_ttm_fehlende_komponente_nan_kein_notbehelf(monkeypatch):
    """YTD des Vorjahres fehlt -> TTM = NaN (keine Einzelquartal-Summe,
    kein Rueckgriff auf andere Perioden)."""
    facts = _facts({"InterestExpense": [
        _e("2025-01-01", "2025-12-31", 400.0, filed="2026-02-01"),
        _e("2026-01-01", "2026-09-30", 300.0, form="10-Q", filed="2026-10-28"),
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker(ttm=_ttm_result())
    out = _fallback(m, _cfg())
    assert out["TTM"] is None


def test_ttm_kein_mischen_zwischen_tags(monkeypatch):
    """Tag A liefert FY+YTD-cur, Tag B alle drei -> Tag B gewinnt (gleiche
    Tag-Wahl fuer alle Komponenten)."""
    facts = _facts({
        "InterestExpense": [
            _e("2025-01-01", "2025-12-31", 400.0, filed="2026-02-01"),
            _e("2026-01-01", "2026-09-30", 300.0, form="10-Q", filed="2026-10-28"),
        ],
        "InterestExpenseNonoperating": [
            _e("2025-01-01", "2025-12-31", 410.0, filed="2026-02-01"),
            _e("2026-01-01", "2026-09-30", 310.0, form="10-Q", filed="2026-10-28"),
            _e("2025-01-01", "2025-09-30", 280.0, form="10-Q", filed="2025-10-29"),
        ],
    })
    _patch_sec(monkeypatch, facts)
    m = _ticker(ttm=_ttm_result())
    out = _fallback(m, _cfg())
    assert out["TTM"] == (410.0 + 310.0 - 280.0, "InterestExpenseNonoperating")


def test_ttm_q4_sonderfall_gleich_fy(monkeypatch):
    """TTM endet am FY-Ende -> TTM = FY direkt (keine YTD-Formel)."""
    facts = _facts({"InterestExpense": [
        _e("2025-01-01", "2025-12-31", 400.0, filed="2026-02-01"),
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker(fcf_by_fy={2023: 100.0, 2024: 100.0, 2025: 100.0},
                ttm=_ttm_result(quarter_end="2025-12-31"))
    out = _fallback(m, _cfg())
    assert out["TTM"] == (400.0, "InterestExpense")


# --------------------------------------------- (4) yfinance-Vorrang / DCF --

def test_yfinance_vorrang_sec_nur_bei_nan(monkeypatch):
    """yfinance liefert FY-Zins -> SEC wird nicht verwendet; FY2023 ohne
    yfinance-Zins -> SEC-Wert + Herkunft sec:<tag> in fcff_interest_sources."""
    facts = _facts({"InterestExpense": [
        _e("2023-01-01", "2023-12-31", 10.0),
        _e("2024-01-01", "2024-12-31", 20.0),
        _e("2025-01-01", "2025-12-31", 30.0),
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker(interest_by_fy={2024: 5.0, 2025: 6.0})  # FY2023 fehlt
    m.sec_interest = _fallback(m, _cfg())
    d = compute_dcf(m, _cfg(), fcf_mode="avg3")
    srcs = {s.split(":")[0]: s.split(":", 1)[1] for s in d.fcff_interest_sources}
    assert srcs["FY2023"].startswith("sec:") and "InterestExpense" in srcs["FY2023"]
    assert srcs["FY2024"] == "yfinance" and srcs["FY2025"] == "yfinance"
    assert d.fcff_status == "ok"
    # SEC-Wert fliesst in den Aufschlag (Handrechnung):
    # FY2023: 100 + 10*0.8 = 108, FY2024: 100 + 5*0.8 = 104,
    # FY2025: 100 + 6*0.8 = 104.8 -> O3 = 105.6
    assert d.fcf_normalized == pytest.approx((108.0 + 104.0 + 104.8) / 3.0)


def test_yfinance_vorrang_wert_wirkt_nicht_sec_wert(monkeypatch):
    """Weicht SEC von yfinance ab, bleibt yfinance der Rechenwert
    (kein Mischen: 5 statt 20 als Aufschlag)."""
    facts = _facts({"InterestExpense": [
        _e("2023-01-01", "2023-12-31", 10.0),
        _e("2024-01-01", "2024-12-31", 20.0),
        _e("2025-01-01", "2025-12-31", 30.0),
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker(interest_by_fy={2024: 5.0, 2025: 6.0})
    m.sec_interest = _fallback(m, _cfg())
    m2 = _ticker(interest_by_fy={2024: 5.0, 2025: 6.0})  # ohne sec_interest
    d1 = compute_dcf(m, _cfg(), fcf_mode="latest")
    d2 = compute_dcf(m2, _cfg(), fcf_mode="latest")
    # FY2025 (latest): yfinance 6 vs SEC 20 -> identisch, yfinance entscheidet
    assert d1.scenarios["base"].value_per_share == d2.scenarios["base"].value_per_share


# ------------------------------------------------ (5) Wesentlichkeitsregel --

def test_zins_unwesentlich_greift(monkeypatch):
    """Ohne jede Zinszahl (yfinance UND SEC): letzter bekannter Zins (2022,
    3 Jahre alt) nach Steuer 8 < 5 % x 500 Start-FCF UND ND/Mcap < 10 % ->
    Aufschlag 0 + Flag zins_unwesentlich + Schaetzungsfeld."""
    facts = _facts({"InterestAndDebtExpense": [
        _e("2024-01-01", "2024-12-31", 10.0),
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker(fcf_by_fy={2023: 500.0, 2024: 500.0, 2025: 500.0},
                interest_by_fy={}, net_debt=10.0, mcap=100000.0, price=10.0)
    m.sec_interest = _fallback(m, _cfg())
    d = compute_dcf(m, _cfg(), fcf_mode="latest")
    assert d.fcff_status == "ok"
    assert any(s.endswith(":unwesentlich") for s in d.fcff_interest_sources)
    assert d.fcff_zins_schaetzung == d.fcff_zins_schaetzung
    assert d.fcff_zins_schaetzung == pytest.approx(10.0 * 0.8, abs=1e-9)
    # Aufschlag 0: Start-FCF unveraendert 500
    assert d.fcf_normalized == pytest.approx(500.0, abs=1e-9)


def test_zins_unwesentlich_genau_an_grenze_nicht(monkeypatch):
    """Nach-Steuer-Zins GENAU 5 % des Start-FCF (striktes <) -> greift NICHT
    -> strikt n/a wie v70."""
    facts = _facts({"InterestAndDebtExpense": [
        _e("2024-01-01", "2024-12-31", 31.25),  # nach Steuer 25.0 == 5 % x 500
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker(fcf_by_fy={2023: 500.0, 2024: 500.0, 2025: 500.0},
                interest_by_fy={}, net_debt=10.0, mcap=100000.0, price=10.0)
    m.sec_interest = _fallback(m, _cfg())
    d = compute_dcf(m, _cfg(), fcf_mode="latest")
    assert d.fcff_status == "zinsaufwand_fehlend"
    assert d.fcff_zins_schaetzung != d.fcff_zins_schaetzung  # NaN


def test_zins_unwesentlich_nd_mcap_grenze(monkeypatch):
    """ND/Mcap >= 10 % -> Regel greift nicht -> strikt n/a."""
    facts = _facts({"InterestAndDebtExpense": [
        _e("2024-01-01", "2024-12-31", 10.0),
    ]})
    _patch_sec(monkeypatch, facts)
    m = _ticker(fcf_by_fy={2023: 500.0, 2024: 500.0, 2025: 500.0},
                interest_by_fy={}, net_debt=10000.0, mcap=100000.0,
                price=10.0)  # ND/Mcap = 10 %
    m.sec_interest = _fallback(m, _cfg())
    d = compute_dcf(m, _cfg(), fcf_mode="latest")
    assert d.fcff_status == "zinsaufwand_fehlend"


def test_zins_unwesentlich_altersgrenze(monkeypatch):
    """Letzter bekannter Zins aelter als fcff_zins_max_alter_jahre (4) ->
    Regel greift nicht -> strikt n/a."""
    facts = _facts({"InterestAndDebtExpense": [
        _e("2020-01-01", "2020-12-31", 10.0),  # 5 Jahre vor FY2025
    ]})
    _patch_sec(monkeypatch, facts)
    # fiscal_ends enthaelt 2020 (.Jahre 2020-2025), FAKT also auffindbar,
    # aber aelter als fcff_zins_max_alter_jahre (4) -> kein Treffer
    m = _ticker(fcf_by_fy={2020: 500.0, 2021: 500.0, 2022: 500.0, 2023: 500.0,
                           2024: 500.0, 2025: 500.0},
                interest_by_fy={}, net_debt=10.0, mcap=100000.0, price=10.0)
    m.sec_interest = _fallback(m, _cfg())
    d = compute_dcf(m, _cfg(), fcf_mode="latest")
    assert d.fcff_status == "zinsaufwand_fehlend"


def test_zins_fehlend_ohne_regel_wie_v70(monkeypatch):
    """Alles fehlt (kein SEC-Interest, keine Historie) -> strikt n/a mit
    fcff_status zinsaufwand_fehlend und Roh-FCF dokumentiert (v70-Verhalten)."""
    facts = _facts({})  # keine Zins-Extras
    _patch_sec(monkeypatch, facts)
    m = _ticker(fcf_by_fy={2023: 500.0, 2024: 500.0, 2025: 500.0})
    m.sec_interest = _fallback(m, _cfg())
    d = compute_dcf(m, _cfg(), fcf_mode="latest")
    assert d.fcff_status == "zinsaufwand_fehlend"
    assert d.fcf_normalized == pytest.approx(500.0)

# ------------------------------------------ (6) Abweichungen (Info/Flag) --

def test_abweichung_wird_angelegt_und_flagged(monkeypatch):
    """SEC weicht > 20 % von yfinance ab -> Eintrag in abweichungen;
    Rechenwert bleibt yfinance (Flag zins_quellen_abweichend setzt die
    Pipeline nur bei gecachter Antwort, hier: Auswertung des Dicts)."""
    facts = _facts({"InterestExpense": [
        _e("2023-01-01", "2023-12-31", 10.0),
        _e("2024-01-01", "2024-12-31", 40.0),  # yfinance: 5 -> 700 % Abweichung
        _e("2025-01-01", "2025-12-31", 60.0),
    ]})
    cap = {}
    _patch_sec(monkeypatch, facts, captured=cap)
    m = _ticker(interest_by_fy={2024: 5.0, 2025: 6.0})
    out = _fallback(m, _cfg())
    devs = {a["periode"]: a for a in out["abweichungen"]}
    assert abs(devs["FY2024"]["abweichung"] - abs(5.0 - 40.0) / 40.0) < 1e-9
    assert out["FY"][2024] == (40.0, "InterestExpense")
    assert cap.get("fetch") == 1  # ein Abruf (Cache aus, Memory vorab leer)


# -------------------------------------------------- (7) Umgebungsvariable --

def test_user_agent_fehlend_klarer_fehler(monkeypatch):
    """SEC_USER_AGENT fehlt -> RuntimeError VOR dem ersten Request."""
    capt = {}
    _patch_sec(monkeypatch, _facts({}), captured=capt)
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="SEC_USER_AGENT"):
        sec_mod.sec_interest_fallback(_ticker(), _cfg())
    assert capt.get("fetch", 0) == 0  # kein Request vor dem Fehler


def test_user_agent_ohne_email_klarer_fehler(monkeypatch):
    monkeypatch.setenv("SEC_USER_AGENT", "keine-email")
    with pytest.raises(RuntimeError, match="@"):
        sec_mod.user_agent()


# ---------------------------------------------------- (8) Sonstiges --

def test_boersensuffix_kein_sec_abruf(monkeypatch):
    """Ticker MIT Boersensuffix -> kein SEC-Abruf (US-GAAP-Schutz wie fcff)."""
    capt = {}
    _patch_sec(monkeypatch, _facts({}), captured=capt)
    m = _ticker(ticker="SHEL.L")
    out = _fallback(m, _cfg())
    assert capt.get("fetch", 0) == 0
    assert "Suffix" in out["note"] or out["note"] != ""


def test_cache_ttl_abgelaufen_neuladen(monkeypatch, tmp_path):
    """Cache-Eintrag aelter als TTL (24 h) -> Neuladen; innerhalb TTL ->
    kein zweiter Abruf; frozen ohne Eintrag -> kein Abruf."""
    facts = _facts({"InterestExpense": [_e("2025-01-01", "2025-12-31", 10.0)]})
    capt = {}
    _patch_sec(monkeypatch, facts, captured=capt)
    monkeypatch.setenv("SEC_USER_AGENT", "Value-Screener test@example.com")
    cfg = _cfg({"cache_dir": str(tmp_path)})
    sec_mod._FACTS_MEM_CACHE.clear()
    _, frisch = sec_mod.fetch_companyfacts_cached("0000000001", cfg)
    assert capt.get("fetch") == 1 and not frisch
    _, hit = sec_mod.fetch_companyfacts_cached("0000000001", cfg)
    assert capt.get("fetch") == 1 and hit  # Memory/Festplatten-Cache
    # Datei altern (TTL abgelaufen) -> neuer Abruf
    import datetime as dt
    import json
    path = tmp_path / "CIK0000000001.json"
    raw = json.loads(path.read_text())
    raw["fetched_at"] = (dt.datetime.now() - dt.timedelta(hours=25)).isoformat()
    path.write_text(json.dumps(raw))
    sec_mod._FACTS_MEM_CACHE.clear()
    _, hit2 = sec_mod.fetch_companyfacts_cached("0000000001", cfg)
    assert capt.get("fetch") == 2 and not hit2
    # frozen ohne Cache-Eintrag -> kein Abruf
    cfg_frozen = _cfg({"cache_dir": str(tmp_path / "anders"), "cache_frozen": True})
    res = sec_mod.fetch_companyfacts_cached("0000000009", cfg_frozen)
    assert res[0] is None
    # (frozen mit frischem Eintrag -> Treffer ohne Abruf)
    cfg_frozen2 = _cfg({"cache_dir": str(tmp_path), "cache_frozen": True})
    _, hit3 = sec_mod.fetch_companyfacts_cached("0000000001", cfg_frozen2)
    assert hit3 and capt.get("fetch") == 2


def test_sec_interest_to_dict_from_dict_rundlauf():
    """sec_interest ueberlebt den Resume-Cache; Alt-Cache ohne Feld -> {}."""
    m = _ticker()
    m.sec_interest = {"FY": {2025: (10.0, "InterestExpense")}, "TTM": None,
                      "abweichungen": [], "aus_cache": True, "note": ""}
    d = m.to_dict()
    m2 = TickerMetrics.from_dict(d)
    got = m2.sec_interest["FY"][2025]
    assert (got[0], got[1]) == (10.0, "InterestExpense")
    # Alt-Cache ohne Feld -> Default {}
    alt = {k: v for k, v in d.items() if k != "sec_interest"}
    m3 = TickerMetrics.from_dict(alt)
    assert m3.sec_interest == {}
