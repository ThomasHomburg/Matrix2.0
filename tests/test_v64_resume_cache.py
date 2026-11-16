"""Tests fuer v64: Resume-Cache mit vollstaendiger TickerMetrics-Persistenz.

Fix des Audit-Punkts 1: Der Resume-Cache speicherte frueher nur
{"status": "ok"} -- beim naechsten Lauf wurden diese Ticker uebersprungen,
ohne dass ihre Daten wiederhergestellt wurden; die Ergebnis-CSV war
unvollstaendig. Jetzt werden erfolgreiche Ticker mit vollem Datenblock
persistiert, aus dem Cache rekonstruiert (kein erneuter Provider-Abruf),
atomar geschrieben und bei ungueltigen Eintraegen neu geladen.
"""

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import load_config
from data.loader import Metric, TickerMetrics, metric_to_dict, metric_from_dict
from data.ttm import TtmResult, ttm_to_dict, ttm_from_dict
import run_ranking


@pytest.fixture
def cfg():
    c = load_config()
    c = dict(c)
    c["sec"] = {"enabled": False}
    return c


def _metrics(ticker: str) -> TickerMetrics:
    m = TickerMetrics(ticker=ticker, name=f"Name {ticker}", sector="Tech",
                      industry="Software", country="US", source="demo")
    m.market_cap = Metric("market_cap", 1e12, "USD", "USD", 2025)
    m.market_cap_date = "2026-11-16"
    m.price = Metric("price", 123.45, "USD", "USD", 2025)
    m.price_date = "2026-11-16"
    m.next_earnings_date = "2026-12-01"
    m.extras["price_to_book"] = Metric("price_to_book", 4.2, "ratio", "", 0)
    m.extras["dividend_yield"] = Metric("dividend_yield", float("nan"), "ratio", "", 0)
    m.fiscal_ends = {2024: "2024-12-31", 2025: "2025-11-30"}
    for fy in (2024, 2025):
        m.years[fy] = {
            "revenue": Metric("revenue", 100e9, "USD", "USD", fy),
            "net_income": Metric("net_income", float("nan"), "USD", "USD", fy),
            "fcf": Metric("fcf", 23e9, "USD", "USD", fy),
        }
    ttm = TtmResult()
    ttm.ttm["fcf"] = Metric("fcf", 24e9, "USD", "USD", 2025)
    ttm.ttm["revenue"] = Metric("revenue", float("inf"), "USD", "USD", 2025)
    ttm.quarter_end = "2026-09-30"
    ttm.base_label = "TTM bis 2026-09"
    ttm.deviation = 0.012
    ttm.checks = ["fcf: 1.2 %"]
    ttm.quarters.append({"quarter_end": "2026-09-30", "operating_cashflow": 6e9,
                         "capex": -1e9, "stock_based_compensation": 0.5e9,
                         "fcf_sbc": 4.5e9, "yoy": float("nan")})
    ttm.quarters_currency = "USD"
    m.ttm = ttm
    m.ebitda_unplausible = True
    m.ttm_plausible = False
    m.ttm_fallback = True
    return m


def _assert_metric_equal(a: Metric, b: Metric, name: str):
    assert a.name == b.name, name
    assert a.unit == b.unit and a.currency == b.currency, name
    assert a.fiscal_year == b.fiscal_year, name
    assert (a.value == b.value) or (math.isnan(a.value) and math.isnan(b.value)), name


# ---------- (1) Round-trip TickerMetrics ----------

def test_ticker_metrics_round_trip():
    m = _metrics("RT")
    d = m.to_dict()
    # JSON-faehig + NaN/inf als null
    text = json.dumps(d, ensure_ascii=False)
    r = TickerMetrics.from_dict(json.loads(text))
    assert r.ticker == m.ticker and r.name == m.name
    assert r.sector == m.sector and r.industry == m.industry
    assert r.country == m.country and r.source == m.source
    assert r.market_cap_date == m.market_cap_date
    assert r.price_date == m.price_date
    assert r.next_earnings_date == m.next_earnings_date
    assert r.ebitda_unplausible and not r.ttm_plausible and r.ttm_fallback
    assert r.fiscal_ends == m.fiscal_ends
    _assert_metric_equal(r.market_cap, m.market_cap, "market_cap")
    _assert_metric_equal(r.price, m.price, "price")
    _assert_metric_equal(r.extras["price_to_book"], m.extras["price_to_book"], "p/b")
    assert math.isnan(r.extras["dividend_yield"].value)
    for fy in m.years:
        for n in m.years[fy]:
            _assert_metric_equal(r.years[fy][n], m.years[fy][n], f"{fy}/{n}")
    assert isinstance(r.ttm, TtmResult)
    _assert_metric_equal(r.ttm.ttm["fcf"], m.ttm.ttm["fcf"], "ttm/fcf")
    assert math.isnan(r.ttm.ttm["revenue"].value)  # inf -> null -> NaN
    assert r.ttm.quarter_end == "2026-09-30" and r.ttm.base_label == "TTM bis 2026-09"
    assert r.ttm.deviation == pytest.approx(0.012)
    assert r.ttm.checks == ["fcf: 1.2 %"] and r.ttm.quarters_currency == "USD"
    assert r.ttm.quarters[0]["fcf_sbc"] == pytest.approx(4.5e9)
    assert math.isnan(r.ttm.quarters[0]["yoy"])


def test_metric_nan_null_round_trip():
    d = metric_to_dict(Metric("x", float("nan"), "u", "c", 1))
    assert d["value"] is None
    assert math.isnan(metric_from_dict(d).value)
    d = metric_to_dict(Metric("x", float("inf"), "u", "c", 1))
    assert d["value"] is None
    assert math.isnan(metric_from_dict(d).value)


def test_ticker_metrics_from_dict_invalid():
    with pytest.raises(ValueError):
        TickerMetrics.from_dict("kein dict")
    with pytest.raises(ValueError):
        TickerMetrics.from_dict({"name": "ohne ticker"})
    with pytest.raises(ValueError):
        TickerMetrics.from_dict({"ticker": "X"})  # years fehlt


# ---------- Hilfsfunktionen fuer _run_batched-Tests ----------

def _fake_load(tickers_err: set):
    calls = []

    def _load(ticker, cfg, providers, years=5):
        calls.append(ticker)
        if ticker in tickers_err:
            raise RuntimeError(f"boom {ticker}")
        return _metrics(ticker)

    return _load, calls


def _run(tickers, cache_file, tickers_err, extra_args=()):
    load, calls = _fake_load(tickers_err)
    run_ranking_load = run_ranking.load_with_provider
    run_ranking.load_with_provider = load
    import time as _t
    orig_sleep = run_ranking.time.sleep
    run_ranking.time.sleep = lambda s: None
    try:
        args = run_ranking._parse_args(["--tickers", "x.csv", "--cache", str(cache_file),
                                        *extra_args])
        companies, errors = run_ranking._run_batched(tickers, _cfg(), {}, args)
    finally:
        run_ranking.load_with_provider = run_ranking_load
        run_ranking.time.sleep = orig_sleep
    return companies, errors, calls, load


_CFG = None


def _cfg():
    global _CFG
    if _CFG is None:
        _CFG = load_config()
        _CFG = dict(_CFG)
        _CFG["sec"] = {"enabled": False}
    return _CFG


def _write_cache(cache_file, entries, version=run_ranking.CACHE_VERSION):
    cache_file.write_text(json.dumps({"version": version, "entries": entries}),
                          encoding="utf-8")


# ---------- (2) Resume-Vollstaendigkeit (Kernregression) ----------

def test_resume_produces_complete_ranking(tmp_path):
    """Lauf 1 mit Fehlern/Abbruch-Simulation, Lauf 2 (Resume) muss dieselben
    companies liefern wie ein vollstaendiger Lauf ohne Resume."""
    cache_file = tmp_path / "cache.json"
    tickers = ["T1", "T2", "T3", "T4", "T5"]
    # Lauf 1: T3/T4 scheitern, T1/T2/T5 ok
    c1, e1, calls1, _ = _run(tickers, cache_file, {"T3", "T4"})
    assert [c.ticker for c in c1] == ["T1", "T2", "T5"]
    assert {e["ticker"] for e in e1} == {"T3", "T4"}
    # Lauf 2 (Resume): T3/T4 gelingen jetzt
    c2, e2, calls2, _ = _run(tickers, cache_file, set())
    assert calls2 == ["T3", "T4"]  # nur die Fehlenden werden geladen
    assert [c.ticker for c in c2] == tickers  # vollstaendig, in Eingabe-Reihenfolge
    assert e2 == []
    # Vergleichslauf komplett ohne Resume
    fresh = tmp_path / "fresh.json"
    c3, e3, calls3, _ = _run(tickers, fresh, set(), extra_args=["--no-resume"])
    assert [c.ticker for c in c3] == [c.ticker for c in c2]
    assert {c.ticker for c in c3} == set(tickers)


def test_resume_preserves_order_across_batches(tmp_path):
    """Reihenfolge der Eingabeticker bleibt ueber mehrere Batches erhalten,
    auch wenn der Cache die Ticker in anderer Reihenfolge enthaelt."""
    cache_file = tmp_path / "cache.json"
    tickers = ["T1", "T2", "T3", "T4", "T5", "T6"]
    _run(tickers[:4], cache_file, set())  # T1-T4 landen im Cache
    companies, errors, calls, _ = _run(tickers, cache_file, set())
    assert calls == ["T5", "T6"]
    assert [c.ticker for c in companies] == tickers
    assert errors == []


def test_resume_keeps_stored_errors(tmp_path):
    """Gespeicherte Fehler bleiben im errors-Rueckgabewert, wenn der Retry
    erneut scheitert; gelingt der Retry, verschwindet der alte Fehler."""
    cache_file = tmp_path / "cache.json"
    tickers = ["T1", "BAD1"]
    _run(tickers, cache_file, {"BAD1"})
    companies, errors, _, _ = _run(tickers, cache_file, {"BAD1"})
    assert [e["ticker"] for e in errors] == ["BAD1"]
    assert "boom BAD1" in errors[0]["error"]
    companies, errors, calls, _ = _run(tickers, cache_file, set())
    assert calls == ["BAD1"]
    assert errors == []
    assert [c.ticker for c in companies] == tickers


# ---------- (3) Alte/inkompatible Cache-Formate ----------

def test_resume_legacy_cache_without_data_reloads(tmp_path):
    """Alte erfolgreiche Eintraege ohne Datenblock werden neu geladen
    (kein stilles Fehlen in der Rangliste)."""
    cache_file = tmp_path / "cache.json"
    cache_file.write_text(json.dumps({"T1": {"status": "ok"},
                                      "BAD1": {"status": "fehler", "error": "alt"}}),
                          encoding="utf-8")
    companies, errors, calls, _ = _run(["T1", "T2", "BAD1"], cache_file, set())
    assert calls == ["T1", "T2", "BAD1"]
    assert [c.ticker for c in companies] == ["T1", "T2", "BAD1"]
    assert errors == []


def test_resume_unknown_schema_version_ignored(tmp_path):
    cache_file = tmp_path / "cache.json"
    _write_cache(cache_file, {"T1": {"status": "ok", "data": _metrics("T1").to_dict()}},
                 version=99)
    companies, errors, calls, _ = _run(["T1", "T2"], cache_file, set())
    assert calls == ["T1", "T2"]  # kompletter Cache ignoriert
    assert [c.ticker for c in companies] == ["T1", "T2"]


def test_resume_corrupt_cache_file_ignored(tmp_path):
    cache_file = tmp_path / "cache.json"
    cache_file.write_text("{ nicht json", encoding="utf-8")
    companies, errors, calls, _ = _run(["T1", "T2"], cache_file, set())
    assert calls == ["T1", "T2"]
    assert [c.ticker for c in companies] == ["T1", "T2"]


def test_resume_incomplete_dataset_reloads(tmp_path):
    """Unvollstaendiger TickerMetrics-Datensatz im Datenblock -> Warnung im
    Log + neu laden, kein Abbruch des Laufs."""
    cache_file = tmp_path / "cache.json"
    broken = _metrics("T1").to_dict()
    del broken["years"]
    _write_cache(cache_file, {"T1": {"status": "ok", "data": broken}})
    companies, errors, calls, _ = _run(["T1", "T2"], cache_file, set())
    assert calls == ["T1", "T2"]
    assert [c.ticker for c in companies] == ["T1", "T2"]


def test_resume_corrupt_entry_reloads(tmp_path):
    cache_file = tmp_path / "cache.json"
    _write_cache(cache_file, {"T1": {"status": "ok", "data": "kein dict"},
                              "T2": {"status": "ok", "data": {"ticker": "T2"}}})
    companies, errors, calls, _ = _run(["T1", "T2", "T3"], cache_file, set())
    assert calls == ["T1", "T2", "T3"]
    assert [c.ticker for c in companies] == ["T1", "T2", "T3"]


def test_resume_nonfinite_values_round_trip(tmp_path):
    """Nicht endliche Zahlenwerte im Cache duerfen neither 0 werden noch den
    Lauf brechen; sie kommen als NaN zurueck."""
    cache_file = tmp_path / "cache.json"
    m = _metrics("T1")
    m.years[2025]["revenue"] = Metric("revenue", float("nan"), "USD", "USD", 2025)
    _write_cache(cache_file, {"T1": {"status": "ok", "data": m.to_dict()}})
    companies, errors, calls, _ = _run(["T1", "T2"], cache_file, {"T2"})
    t1 = next(c for c in companies if c.ticker == "T1")
    assert math.isnan(t1.years[2025]["revenue"].value)
    assert calls == ["T2"]


# ---------- (4) Atomares Schreiben ----------

def test_save_cache_atomic_on_write_failure(tmp_path, monkeypatch):
    """Schreibfehler beim Replace: alter Cache bleibt unversehrt, keine
    halbfertige Cache-Datei, Temp-Datei wird aufgeraeumt."""
    cache_file = tmp_path / "cache.json"
    _write_cache(cache_file, {"OLD": {"status": "fehler", "error": "alt"}})
    before = cache_file.read_text(encoding="utf-8")

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(run_ranking.os, "replace", _boom)
    with pytest.raises(OSError):
        run_ranking._save_cache(cache_file, {"NEW": {"status": "ok"}})
    assert cache_file.read_text(encoding="utf-8") == before
    assert not (tmp_path / "cache.json.tmp").exists()


def test_save_cache_atomic_success(tmp_path):
    cache_file = tmp_path / "cache.json"
    run_ranking._save_cache(cache_file, {"T1": {"status": "ok"}})
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["version"] == run_ranking.CACHE_VERSION
    assert raw["entries"]["T1"]["status"] == "ok"
    assert not (tmp_path / "cache.json.tmp").exists()


# ---------- (5) --no-resume unveraendert ----------

def test_no_resume_ignores_cache(tmp_path):
    cache_file = tmp_path / "cache.json"
    _write_cache(cache_file, {"T1": {"status": "ok", "data": _metrics("T1").to_dict()}})
    companies, errors, calls, _ = _run(["T1", "T2"], cache_file, set(),
                                       extra_args=["--no-resume"])
    assert calls == ["T1", "T2"]  # alles frisch geladen
    assert [c.ticker for c in companies] == ["T1", "T2"]
    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["version"] == run_ranking.CACHE_VERSION
    assert set(raw["entries"]) == {"T1", "T2"}
