"""Tests fuer v68 (Audit-Punkt 4, F1): Waehrungstrennung im yfinance-Provider.

Berichts- und Handelswaehrung muessen getrennt behandelt werden:
- trading_cur (info['currency']): Boersenkurs + Marktkapitalisierung (Quote).
- report_cur (info['financialCurrency']): alle GuV-/Bilanz-/Cashflow-Statement-
  Werte und die daraus abgeleiteten TTM-Werte (yfinance liefert die Statements
  in der Berichtswaehrung).
Keine Waehrung wird erfunden: Ist die Berichtswaehrung nicht bestimmbar,
bleibt sie unbekannt (""), und nachgelagerte Berechnungen scheitern sicher
(NaN), statt still falsche Mischwertaergebnisse zu erzeugen.
"""

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from data.metrics import _NoFX, fcf_yield
from data.providers.yfinance_provider import YfinanceProvider
from valuation.dcf import compute_dcf

FIN_ROWS = {
    "Total Revenue": [100e9, 90e9],
    "Gross Profit": [40e9, 35e9],
    "Operating Income": [30e9, 25e9],
    "EBITDA": [45e9, 40e9],
    "Net Income": [20e9, 18e9],
    "Interest Expense": [-1e9, -0.8e9],
    "Income Tax Expense": [5e9, 4e9],
    "Pretax Income": [25e9, 21e9],
    "Diluted Average Shares": [10e9, 10e9],
}
BAL_ROWS = {
    "Stockholders Equity": [50e9, 45e9],
    "Total Debt": [10e9, 8e9],
    "Cash And Cash Equivalents": [40e9, 38e9],
}
CF_ROWS = {
    "Operating Cash Flow": [25e9, 22e9],
    "Capital Expenditure": [-5e9, -4e9],
    "Free Cash Flow": [20e9, 18e9],
    "Stock Based Compensation": [2e9, 1.8e9],
}
COLS = [pd.Timestamp("2025-12-31"), pd.Timestamp("2024-12-31")]


def _frames(cols, rows):
    labels = list(rows)
    data = [[rows[label][i] for label in labels] for i in range(len(cols))]
    return pd.DataFrame(data, index=cols, columns=labels).T


def _load_yf(info, with_ttm=False):
    tk = SimpleNamespace(
        info=info,
        financials=_frames(COLS, FIN_ROWS),
        balance_sheet=_frames(COLS, BAL_ROWS),
        cashflow=_frames(COLS, CF_ROWS),
    )
    if with_ttm:
        qcols = [pd.Timestamp("2026-08-31"), pd.Timestamp("2026-05-31"),
                 pd.Timestamp("2026-02-28"), pd.Timestamp("2025-11-30")]
        tk.quarterly_financials = _frames(qcols, {k: [v[0] / 4] * 4 for k, v in FIN_ROWS.items()})
        tk.quarterly_balance_sheet = _frames(qcols, {k: [v[0]] * 4 for k, v in BAL_ROWS.items()})
        tk.quarterly_cashflow = _frames(qcols, {k: [v[0] / 4] * 4 for k, v in CF_ROWS.items()})
    prov = YfinanceProvider()
    prov._yf = SimpleNamespace(Ticker=lambda t: tk)
    return prov.load("TEST.DE")


# ---------- (1) currency != financialCurrency: Statements in Berichtswaehrung ----------

def test_trading_and_report_currency_differ():
    """currency=EUR, financialCurrency=USD: Statements (und TTM) in USD,
    Kurs und Marktkapitalisierung in EUR."""
    m = _load_yf({"currency": "EUR", "financialCurrency": "USD",
                  "marketCap": 200e9, "regularMarketPrice": 20.0})
    y25 = m.years[2025]
    assert y25["revenue"].currency == "USD"
    assert y25["fcf"].currency == "USD"
    assert y25["fcf_sbc"].currency == "USD"
    assert y25["equity"].currency == "USD"
    assert y25["net_debt"].currency == "USD"
    assert y25["ebitda"].currency == "USD"
    assert m.market_cap.currency == "EUR"
    assert m.price.currency == "EUR"


def test_trading_and_report_currency_differ_ttm():
    """TTM-Werte aus Quartals-Statements tragen die BERICHTSwaehrung (USD),
    nicht die Quote-Waehrung (EUR) der Marktkapitalisierung."""
    m = _load_yf({"currency": "EUR", "financialCurrency": "USD",
                  "marketCap": 200e9, "regularMarketPrice": 20.0}, with_ttm=True)
    assert m.ttm is not None
    assert m.ttm.quarters_currency == "USD"
    for key in ("revenue", "fcf", "fcf_sbc", "net_debt"):
        assert m.ttm.ttm[key].currency == "USD", key


# ---------- (2) Beide Waehrungen identisch: bisheriges Verhalten ----------

def test_same_currency_behaviour_unchanged():
    m = _load_yf({"currency": "EUR", "financialCurrency": "EUR",
                  "marketCap": 200e9, "regularMarketPrice": 20.0})
    assert m.years[2025]["revenue"].currency == "EUR"
    assert m.years[2025]["fcf"].currency == "EUR"
    assert m.market_cap.currency == "EUR"
    assert m.price.currency == "EUR"


def test_report_currency_missing_no_assumption(caplog):
    """financialCurrency fehlt: die Statement-Waehrung ist nicht bestimmbar
    -- KEINE Zuordnung zur Handelswaehrung (nicht beweisbar). Statements
    bleiben "" (unbekannt) und scheiden nachgelagert sicher als NaN aus."""
    m = _load_yf({"currency": "EUR", "marketCap": 200e9})
    assert m.years[2025]["revenue"].currency == ""    # nicht "EUR" unterstellt
    assert m.years[2025]["fcf"].currency == ""
    assert m.market_cap.currency == "EUR"             # Quote-Waehrung bekannt
    assert math.isnan(fcf_yield(m, 2025))             # sicher NaN, kein Mischwert


# ---------- (4) Beide Angaben fehlen: keine erfundene Waehrung ----------

def test_both_currencies_missing_no_invented_currency():
    m = _load_yf({"marketCap": 200e9})
    assert m.years[2025]["revenue"].currency == ""    # unbekannt, nicht "USD"
    assert m.years[2025]["fcf"].currency == ""
    assert m.market_cap.currency == ""


def test_unknown_currency_downstream_nan():
    """Unbekannte Waehrung (beide Felder fehlen): nachgelagerte Kennzahlen
    scheitern sicher (NaN) -- zwei unbekannte Waehrungen gelten NICHT als
    gleich (kein stilles Durchreichen)."""
    m = _load_yf({"marketCap": 200e9})
    assert math.isnan(fcf_yield(m, 2025))
    cfg = load_config()
    d = compute_dcf(m, cfg)
    assert math.isnan(d.margin_of_safety)
    assert d.scenarios == {} or all(
        math.isnan(sc.value_per_share) for sc in d.scenarios.values())


# ---------- (5) Widerspruechliche/unvollstaendige Angaben ----------

def test_currency_missing_financial_present_quote_unknown():
    """currency fehlt, financialCurrency vorhanden: Statements korrekt in
    Berichtswaehrung; die Quote-Waehrung von Kurs/Marktkap. ist unbekannt
    ("") -- kein Rueckschluss, keine falsche Kennzeichnung."""
    m = _load_yf({"financialCurrency": "DKK", "marketCap": 300e9})
    assert m.years[2025]["revenue"].currency == "DKK"
    assert m.market_cap.currency == ""    # unbekannt, nicht DKK unterstellt
    assert m.price is None or m.price.currency == ""


def test_mismatch_no_silent_wrong_yield():
    """Kernregression: bei currency=EUR/financialCurrency=USD darf die
    FCF-Yield NICHT still aus USD-FCF und EUR-Marktkap. gemischt werden
    (alter Fehler: 20/200 = 10 %). Ohne FX-Konverter -> NaN."""
    m = _load_yf({"currency": "EUR", "financialCurrency": "USD",
                  "marketCap": 200e9, "regularMarketPrice": 20.0})
    assert math.isnan(fcf_yield(m, 2025))
    cfg = dict(load_config())
    cfg["dcf"] = dict(cfg["dcf"])
    d = compute_dcf(m, cfg)
    # DCF ohne FX: FCF (USD) nicht in Marktkap.-Waehrung (EUR) konvertierbar
    assert math.isnan(d.margin_of_safety)
    for sc in d.scenarios.values():
        assert math.isnan(sc.value_per_share)


# ---------- (6) TTM ohne Provider-Angabe: altes Verhalten (compute_ttm direkt) ----------

def test_compute_ttm_currency_fallback_unchanged():
    """Ohne quarterly['currency'] bleibt der alte Rueckfall auf die
    Marktkap.-Waehrung bestehen (Abwaertskompatibilitaet fuer Direktaufrufe)."""
    from data.ttm import compute_ttm
    from data.loader import Metric, TickerMetrics
    m = TickerMetrics(ticker="T")
    m.market_cap = Metric("market_cap", 1000.0, "EUR", "EUR", 2025)
    qcols = [pd.Timestamp("2026-08-31"), pd.Timestamp("2026-05-31"),
             pd.Timestamp("2026-02-28"), pd.Timestamp("2025-11-30")]
    row_map = {"revenue": ["Total Revenue"],
               "operating_cashflow": ["Operating Cash Flow"],
               "capex": ["Capital Expenditure"],
               "fcf": ["Free Cash Flow"],
               "stock_based_compensation": ["Stock Based Compensation"]}
    frames = {"income": _frames(qcols, {"Total Revenue": [25.0] * 4}),
              "balance": _frames(qcols, {"Stockholders Equity": [50.0] * 4}),
              "cashflow": _frames(qcols, {
                  "Operating Cash Flow": [30.0] * 4,
                  "Capital Expenditure": [-10.0] * 4,
                  "Free Cash Flow": [20.0] * 4,
                  "Stock Based Compensation": [5.0] * 4}),
              "row_map": row_map}
    res = compute_ttm(m, frames)
    assert res.quarters_currency == "EUR"   # Rueckfall Marktkap.-Waehrung
    frames2 = dict(frames)
    frames2["currency"] = "USD"
    res2 = compute_ttm(m, frames2)
    assert res2.quarters_currency == "USD"  # explizite Angabe gewinnt
    assert res2.ttm["revenue"].currency == "USD"


# ---------- (7) compute_ttm: Schluessel-Praesenz statt Wahrheitswert ----------
# Ist quarterly['currency'] vorhanden (auch ""), wird der Wert verwendet;
# nur bei fehlendem Schluessel greift der Legacy-Rueckfall; ohne Marktkap.-
# Waehrung bleibt "" -- KEIN USD-Fallback.

def _ttm_frames(currency=None):
    """Quartals-Frames fuer compute_ttm; currency=None -> Schluessel fehlt."""
    from data.loader import Metric, TickerMetrics
    m = TickerMetrics(ticker="T")
    qcols = [pd.Timestamp("2026-08-31"), pd.Timestamp("2026-05-31"),
             pd.Timestamp("2026-02-28"), pd.Timestamp("2025-11-30")]
    frames = {"income": _frames(qcols, {"Total Revenue": [25.0] * 4}),
              "balance": _frames(qcols, {"Stockholders Equity": [50.0] * 4}),
              "cashflow": _frames(qcols, {
                  "Operating Cash Flow": [30.0] * 4,
                  "Capital Expenditure": [-10.0] * 4,
                  "Free Cash Flow": [20.0] * 4,
                  "Stock Based Compensation": [5.0] * 4}),
              "row_map": {"revenue": ["Total Revenue"],
                          "operating_cashflow": ["Operating Cash Flow"],
                          "capex": ["Capital Expenditure"],
                          "fcf": ["Free Cash Flow"],
                          "stock_based_compensation": ["Stock Based Compensation"]}}
    if currency is not None:
        frames["currency"] = currency
    return m, frames


def _mcap(cur):
    from data.loader import Metric, TickerMetrics
    m = TickerMetrics(ticker="T")
    if cur is not None:
        m.market_cap = Metric("market_cap", 1000.0, cur, cur, 2025)
    return m


def test_ttm_currency_empty_string_stays_empty():
    """(1) currency='' bei Marktkap. EUR: TTM-Waehrung bleibt ''
    (unbekannt), kein Rueckfall auf EUR; Metriken ebenfalls ''."""
    from data.ttm import compute_ttm
    m, frames = _ttm_frames(currency="")
    m.market_cap = _mcap("EUR").market_cap
    res = compute_ttm(m, frames)
    assert res.quarters_currency == ""
    assert res.ttm["revenue"].currency == ""
    assert res.ttm["fcf_sbc"].currency == ""


def test_ttm_currency_empty_string_without_mcap():
    """(2) currency='' ohne Marktkap.: TTM-Waehrung bleibt '' -- kein USD."""
    from data.ttm import compute_ttm
    m, frames = _ttm_frames(currency="")
    res = compute_ttm(m, frames)
    assert res.quarters_currency == ""
    assert res.ttm["revenue"].currency == ""


def test_ttm_currency_key_missing_legacy_fallback():
    """(3) Schluessel fehlt, Marktkap. EUR -> Legacy-Rueckfall EUR."""
    from data.ttm import compute_ttm
    m, frames = _ttm_frames(currency=None)
    m.market_cap = _mcap("EUR").market_cap
    res = compute_ttm(m, frames)
    assert res.quarters_currency == "EUR"
    assert res.ttm["revenue"].currency == "EUR"


def test_ttm_currency_key_missing_no_mcap_no_usd():
    """(4) Schluessel fehlt und Marktkap. fehlt -> TTM-Waehrung bleibt ''
    (KEIN USD-Fallback)."""
    from data.ttm import compute_ttm
    m, frames = _ttm_frames(currency=None)
    res = compute_ttm(m, frames)
    assert res.quarters_currency == ""
    assert res.ttm["revenue"].currency == ""


def test_ttm_currency_none_becomes_empty():
    """quarterly['currency'] = None (Schluessel vorhanden, Wert None):
    Waehrung wird "" (unbekannt), nicht None weitergereicht und NICHT durch
    die Marktkap.-Waehrung ersetzt."""
    from data.ttm import compute_ttm
    m, frames = _ttm_frames(currency=None)
    frames["currency"] = None          # Schluessel explizit mit None
    m.market_cap = _mcap("EUR").market_cap
    res = compute_ttm(m, frames)
    assert res.quarters_currency == ""           # nicht None, nicht EUR
    assert res.ttm["revenue"].currency == ""
    assert res.ttm["fcf_sbc"].currency == ""


def test_ttm_currency_none_without_mcap():
    """currency=None ohne Marktkap.: "" -- kein USD, kein None."""
    from data.ttm import compute_ttm
    m, frames = _ttm_frames(currency=None)
    frames["currency"] = None
    res = compute_ttm(m, frames)
    assert res.quarters_currency == ""
    assert res.ttm["revenue"].currency == ""


def test_ttm_currency_explicit_usd():
    """(5) Explizites USD -> TTM-Waehrung und Metriken USD."""
    from data.ttm import compute_ttm
    m, frames = _ttm_frames(currency="USD")
    m.market_cap = _mcap("EUR").market_cap
    res = compute_ttm(m, frames)
    assert res.quarters_currency == "USD"
    assert res.ttm["revenue"].currency == "USD"
    assert res.ttm["fcf_sbc"].currency == "USD"


# ---------- (8) _NoFX: leere Waehrung ist keine valide Waehrung ----------

def test_nofx_empty_currency_is_nan():
    conv = _NoFX()
    assert math.isnan(conv.convert(100.0, "", "", 2025))
    assert math.isnan(conv.convert(100.0, "", "USD", 2025))
    assert math.isnan(conv.convert(100.0, "EUR", "", 2025))
    assert conv.convert(100.0, "EUR", "EUR", 2025) == 100.0
    assert conv.convert(100.0, "eur", "EUR", 2025) == 100.0
    assert math.isnan(conv.convert(100.0, "EUR", "USD", 2025))
