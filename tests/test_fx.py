"""v69 (F2 Schritt A): datumsbasierte FX-Infrastruktur (data/fx.py).

Abgedeckt: Paar-Semantik + Inversion, Look-ahead-Schutz, Stichtagskurs,
Periodendurchschnitt mit Coverage-/Gap-Pruefung, Jahresend-Naeherung,
manuelle Kurse (datumsbasiert, avg:YYYY, legacy-Jahr, Vorrang),
Cache (ungueltige Eintraege loeschen, nur valide speichern, API-Ersatz),
Validierung (0/negativ/NaN/inf aus YAML/Cache/API),
Aktualitaetsgrenze fuer "aktuelle" Kurse, build_fx ohne/mit API-Key,
GBP als normalisierte Hauptwaehrung. Alle API-Abrufe gemonckt.
"""

import datetime as dt
import math
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.fx import FXCache, FXConverter, build_fx  # noqa: E402


# ------------------------------------------------------------------ Helpers --

class _MockClient:
    """FMP-Client-Mock: je Paar/Jahr eine konfigurierbare Tagesreihe."""

    def __init__(self, years=None, error_pairs=None):
        self.years = years or {}
        self.error_pairs = error_pairs or set()
        self.calls = []
        self.configured = True
        self.timeout = 10.0

    def _get(self, path, params):
        self.calls.append((path, dict(params or {})))
        pair = str(params.get("symbol", "")).upper()
        if pair in self.error_pairs:
            raise RuntimeError("FMPError-mock")
        year = int(str(params.get("from", "0"))[:4])
        hist = self.years.get((pair, year))
        if hist is None:
            return {}
        return {"historical": [{"date": d, "close": c}
                               for d, c in sorted(hist.items())]}


def _series(rate: float, year: int, skip=None) -> dict:
    """Werktagsreihe eines Jahres (Mo-Fr), optional um 'skip'-Daten reduziert."""
    out = {}
    d = dt.date(year, 1, 1)
    while d.year == year:
        if d.weekday() < 5 and d.isoformat() not in (skip or set()):
            out[d.isoformat()] = rate
        d += dt.timedelta(days=1)
    return out


def _conv(client=None, cache=None, cfg=None):
    return FXConverter(client=client, cache=cache,
                       manual_path="/nonexistent.yaml", cfg=cfg)


def _year(rate, year, skip=None):
    return _MockClient({("EURUSD", year): _series(rate, year, skip)})


# ------------------------------------------------ (1) Paar-Semantik/Inversion --

def test_rate_semantics_quote_per_base():
    """rate('EUR','USD',date) = USD je EUR; convert multipliziert."""
    c = _conv(client=_year(1.10, 2026))
    q = c.quote("EUR", "USD", "2026-06-30")
    assert q.rate == pytest.approx(1.10)
    assert c.convert_stock(100.0, "EUR", "USD", "2026-06-30") == pytest.approx(110.0)


def test_identity_no_api_call():
    """Gleiche bekannte Waehrung: 1.0, keine einzige API-Anfrage."""
    mc = _MockClient()
    c = _conv(client=mc)
    q = c.quote("EUR", "EUR", "2026-06-30")
    fq = c.flow_quote("EUR", "EUR", "2026-01-01", "2026-12-31")
    assert q.rate == 1.0 and q.source == "identity"
    assert fq.rate == 1.0 and fq.source == "identity"
    assert mc.calls == []


def test_inverse_pair_via_reciprocal():
    """Direktes Paar fehlt -> inverses Paar, Kurs = 1/rate (inverse=True)."""
    mc = _MockClient({("USDEUR", 2026): _series(0.90, 2026)})
    c = _conv(client=mc)
    q = c.quote("EUR", "USD", "2026-06-30")
    assert q.rate == pytest.approx(1.0 / 0.90)
    assert q.inverse is True and q.source == "api"


def test_inverse_invalid_rate_nan():
    """Inverse Abfrage mit invaliden Kursen (-0.5, 0) -> NaN (nie 1/0=inf)."""
    mc = _MockClient({("USDEUR", 2026): {"2026-06-01": -0.5, "2026-06-02": 0.0}})
    c = _conv(client=mc)
    assert c.quote("EUR", "USD", "2026-06-30").ok is False


def test_empty_currency_nan():
    c = _conv(client=_year(1.1, 2026))
    assert c.quote("", "USD", "2026-06-30").missing == "waehrung_unbekannt"
    assert c.quote("EUR", "", "2026-06-30").missing == "waehrung_unbekannt"
    assert c.quote("", "", "2026-06-30").missing == "waehrung_unbekannt"
    assert math.isnan(c.convert_stock(100.0, "", "USD", "2026-06-30"))
    assert math.isnan(c.convert_stock(100.0, "", "", "2026-06-30"))


def test_api_invalid_close_ignored():
    """API-Close 0/negativ/inf wird nie uebernommen (Validierung vor Speicherung)."""
    mc = _MockClient({("EURUSD", 2026): {"2026-06-01": 0.0, "2026-06-02": -1.0,
                                         "2026-06-03": float("inf"),
                                         "2026-06-04": 1.07}})
    c = _conv(client=mc)
    q = c.quote("EUR", "USD", "2026-06-30")
    assert q.rate == pytest.approx(1.07) and q.asof == "2026-06-04"


# ---------------------------------------------------- (2) Look-ahead-Schutz --

def test_quote_never_uses_rate_after_asof():
    """Stichtag 30.09.2026: nur Kurse <= Stichtag, niemals der 31.12.-Kurs."""
    hist = {"2026-03-31": 1.05, "2026-09-30": 1.07, "2026-12-31": 1.10}
    c = _conv(client=_MockClient({("EURUSD", 2026): hist}))
    q = c.quote("EUR", "USD", "2026-09-30")
    assert q.rate == pytest.approx(1.07) and q.asof == "2026-09-30"
    assert c.quote("EUR", "USD", "2026-12-31").rate == pytest.approx(1.10)


def test_manual_never_after_asof():
    """Manuelle Kurse nach dem Stichtag werden nie verwendet."""
    conv = _conv()
    conv.manual = {"EURUSD": {"2026-06-30": 1.08, "2026-12-31": 1.10}}
    q = conv.quote("EUR", "USD", "2026-07-31")
    assert q.rate == pytest.approx(1.08) and q.asof == "2026-06-30"


# --------------------------- (3) Quellen-Prioritaet: manuell > Cache > API --

def test_quote_priority_manual_cache_api(tmp_path):
    cache = FXCache(str(tmp_path / "fx.db"))
    cache.put_daily("EURUSD", {"2026-06-15": 1.30}, "fmp")
    conv = _conv(client=_MockClient({("EURUSD", 2026): {"2026-06-20": 1.40}}),
                 cache=cache)
    conv.manual = {"EURUSD": {"2026-06-10": 1.20}}
    q = conv.quote("EUR", "USD", "2026-06-20")
    assert (q.source, q.rate) == ("manual", pytest.approx(1.20))
    conv.manual = {}
    q2 = conv.quote("EUR", "USD", "2026-06-20")
    assert (q2.source, q2.rate) == ("cache", pytest.approx(1.30))
    cache.delete_daily("EURUSD", "2026-06-15")
    q3 = conv.quote("EUR", "USD", "2026-06-20")
    assert (q3.source, q3.rate) == ("api", pytest.approx(1.40))


def test_quote_year_fallback_previous_december():
    c = _conv(client=_MockClient({("EURUSD", 2026): {"2026-12-31": 1.08}}))
    q = c.quote("EUR", "USD", "2027-01-05")
    assert q.rate == pytest.approx(1.08) and q.asof == "2026-12-31"


# --------------------- (4) Periodendurchschnitt: Coverage-/Gap-Pruefung --

def test_flow_avg_full_coverage():
    c = _conv(client=_year(1.10, 2026))
    fq = c.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    assert fq.ok and fq.basis == "periodendurchschnitt"
    assert fq.rate == pytest.approx(1.10)
    assert fq.n_closes > 0 and fq.coverage >= 0.95
    assert c.convert_flow(100.0, "EUR", "USD", "2026-01-01",
                          "2026-12-31") == pytest.approx(110.0)


def test_flow_partial_coverage_naeherung_at_year_end():
    """~25 % Abdeckung: kein Periodendurchschnitt; da das Fenster am
    Jahresende endet, ausdrueckliche Jahresend-Naeherung, nie als Oe."""
    quarter = {d: r for d, r in _series(1.10, 2026).items() if d[5:7] <= "03"}
    c = _conv(client=_MockClient({("EURUSD", 2026): quarter}))
    fq = c.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    assert fq.basis == "jahresendkurs_naeherung"
    assert fq.rate == pytest.approx(1.10)
    assert fq.asof == "2026-03-31"     # letzter Kurs <= Fensterende


def test_flow_gap_rejected_mid_year_nan():
    """Klumpen am Fensteranfang + grosse Luecke, Fensterende weit vom
    Jahresende: NaN -- kein beliebiger Stichtagskurs als Ersatz."""
    half = {d: r for d, r in _series(1.0, 2026).items() if d[5:7] <= "06"}
    c = _conv(client=_MockClient({("EURUSD", 2026): half}))
    fq = c.flow_quote("EUR", "USD", "2026-04-01", "2026-09-30")
    assert fq.ok is False


def test_flow_empty_window_nan():
    assert _conv(client=_MockClient()).flow_quote(
        "EUR", "USD", "2026-01-01", "2026-12-31").ok is False


def test_flow_single_close_mid_year_nan():
    """Einzelner Kurs weitab vom Jahresende: weder Oe noch Naeherung -> NaN."""
    c = _conv(client=_MockClient({("EURUSD", 2026): {"2026-04-20": 1.09}}))
    fq = c.flow_quote("EUR", "USD", "2026-01-01", "2026-06-30")
    assert fq.ok is False


def test_flow_inverse_reciprocal_avg():
    c = _conv(client=_MockClient({("USDEUR", 2026): _series(0.9090909091, 2026)}))
    fq = c.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    assert fq.ok and fq.inverse is True and fq.basis == "periodendurchschnitt"
    assert fq.rate == pytest.approx(1.10, rel=1e-6)


# ------------------------------------------------ (5) Manuelle Kurse (YAML) --

def test_manual_avg_only_if_window_matches_calendar_year():
    conv = _conv()
    conv.manual = {"EURUSD": {"avg:2025": 1.06, "avg:2026": 1.07}}
    fq = conv.flow_quote("EUR", "USD", "2025-01-01", "2025-12-31")
    assert fq.rate == pytest.approx(1.06) and fq.source == "manual"
    assert fq.basis == "periodendurchschnitt" and math.isnan(fq.coverage)
    # fensteruebergreifend: KEIN Rueckfall auf unpassenden Jahresdurchschnitt
    fq2 = conv.flow_quote("EUR", "USD", "2025-11-01", "2026-01-31")
    assert fq2.ok is False


def test_manual_legacy_year_is_year_end_stichtag():
    conv = _conv()
    conv.manual = {"EURUSD": {"2026-12-31": 1.05}}
    q = conv.quote("EUR", "USD", "2026-12-31")
    assert q.rate == pytest.approx(1.05) and q.source == "manual"


def test_manual_flow_naeherung_year_end():
    conv = _conv()
    conv.manual = {"EURUSD": {"2026-12-31": 1.05}}
    fq = conv.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    assert fq.basis == "jahresendkurs_naeherung"
    assert fq.rate == pytest.approx(1.05) and fq.source == "manual"


def test_manual_invalid_entries_ignored():
    """0/negativ/NaN/inf/nonsense-Datum: ignoriert; valide Kurse bleiben."""
    conv = _conv()
    conv.manual = {"EURUSD": {"2026-06-30": 1.08, "2026-07-01": 0.0,
                              "2026-07-02": -1.0, "2026-07-03": float("nan"),
                              "2026-07-04": float("inf"),
                              "nonsense": 1.5}}
    q = conv.quote("EUR", "USD", "2026-07-10")
    assert q.rate == pytest.approx(1.08) and q.asof == "2026-06-30"


# --------------------------------------- (6) Cache: Validierung/Persistenz --

def test_cache_invalid_entry_deleted_and_refetched(tmp_path):
    """Ungueltiger Cache-Eintrag: geloescht, mit Key neu abgerufen; nur
    valide Kurse werden (ueber)speichert."""
    cache = FXCache(str(tmp_path / "fx.db"))
    with sqlite3.connect(str(cache.db_path)) as conn:
        conn.execute("INSERT INTO fx_daily (pair, date, rate, source) "
                     "VALUES ('EURUSD', '2026-06-30', -1.0, 'bad')")
    mc = _MockClient({("EURUSD", 2026): {"2026-06-30": 1.05}})
    c = _conv(client=mc, cache=cache)
    q = c.quote("EUR", "USD", "2026-06-30")
    assert (q.source, q.rate) == ("api", pytest.approx(1.05))
    with sqlite3.connect(str(cache.db_path)) as conn:
        rows = conn.execute("SELECT rate FROM fx_daily "
                             "WHERE pair='EURUSD' AND date='2026-06-30'").fetchall()
    assert rows == [(1.05,)]


def test_cache_invalid_without_key_nan(tmp_path):
    cache = FXCache(str(tmp_path / "fx.db"))
    with sqlite3.connect(str(cache.db_path)) as conn:
        conn.execute("INSERT INTO fx_daily (pair, date, rate, source) "
                     "VALUES ('EURUSD', '2026-06-30', 0.0, 'bad')")
    assert _conv(client=None, cache=cache).quote(
        "EUR", "USD", "2026-06-30").ok is False


def test_cache_stores_only_valid_rates(tmp_path):
    cache = FXCache(str(tmp_path / "fx.db"))
    n = cache.put_daily("EURUSD", {"2026-01-05": 1.05, "2026-01-06": 0.0,
                                   "2026-01-07": -2.0,
                                   "2026-01-08": float("nan"),
                                   "kein-datum": 1.0}, "fmp")
    assert n == 1
    assert cache.daily("EURUSD", dt.date(2026, 1, 1),
                       dt.date(2026, 1, 31)) == {"2026-01-05": 1.05}


def test_cache_serves_flow_without_second_api_call(tmp_path):
    """Erste Flow-Abfrage fuellt den Cache per API; zweite Abfrage antwortet
    aus dem Cache (kein weiterer Abruf)."""
    cache = FXCache(str(tmp_path / "fx.db"))
    mc = _year(1.10, 2026)
    c = _conv(client=mc, cache=cache)
    fq1 = c.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    calls_after_first = len(mc.calls)
    assert fq1.ok and calls_after_first == 1
    fq2 = c.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    assert fq2.ok and len(mc.calls) == calls_after_first
    assert fq2.source == "cache"


# ------------------------------------- (7) Aktualitaet "aktueller" Kurse --

def test_current_rate_within_age_limit():
    conv = _conv()
    conv.manual = {"EURUSD": {
        (dt.date.today() - dt.timedelta(days=3)).isoformat(): 1.08}}
    q = conv.quote_current("EUR", "USD")
    assert q.ok and q.rate == pytest.approx(1.08)
    assert conv.convert_current(100.0, "EUR", "USD") == pytest.approx(108.0)


def test_current_rate_too_old_nan():
    conv = _conv()
    conv.manual = {"EURUSD": {
        (dt.date.today() - dt.timedelta(days=6)).isoformat(): 1.08}}
    q = conv.quote_current("EUR", "USD")
    assert q.ok is False and q.missing == "kurs_zu_alt"
    assert math.isnan(conv.convert_current(100.0, "EUR", "USD"))


def test_current_rate_age_configurable():
    conv = _conv(cfg={"fx": {"current_max_age_days": 2}})
    conv.manual = {"EURUSD": {
        (dt.date.today() - dt.timedelta(days=3)).isoformat(): 1.08}}
    assert conv.quote_current("EUR", "USD").ok is False


def test_current_identity_ok():
    assert _conv().quote_current("EUR", "EUR").ok is True


# ---------------------------------------------- (8) build_fx ohne/mit Key --

def test_build_fx_without_key_uses_manual_and_cache(tmp_path):
    """Ohne API-Key: FXConverter (KEIN _NoFX); manuelle Kurse + Cache nutzbar;
    fehlende Kurse -> NaN; client ist None (keine API-Aufrufe moeglich)."""
    manual = tmp_path / "fx_rates.yaml"
    manual.write_text("EURUSD:\n  \"2026-06-30\": 1.05\n", encoding="utf-8")
    cfg = {"fx": {"cache_file": str(tmp_path / "fx.db"),
                  "manual_path": str(manual)}}
    conv = build_fx(cfg=cfg, api_key="")
    assert conv.client is None
    q1 = conv.quote("EUR", "USD", "2026-06-30")
    assert q1.source == "manual" and q1.rate == pytest.approx(1.05)
    conv.cache.put_daily("USDCHF", {"2026-06-30": 0.88}, "fmp")
    q2 = conv.quote("USD", "CHF", "2026-06-30")
    assert q2.source == "cache" and q2.rate == pytest.approx(0.88)
    assert conv.quote("EUR", "JPY", "2026-06-30").ok is False


def test_build_fx_with_key_uses_client(tmp_path):
    cfg = {"fx": {"cache_file": str(tmp_path / "fx.db"),
                  "manual_path": "/nonexistent.yaml",
                  "min_flow_coverage": 0.9, "current_max_age_days": 3}}
    conv = build_fx(cfg=cfg, api_key="KEY")
    assert conv.client is not None and conv.client.configured
    assert conv.cfg["min_flow_coverage"] == 0.9
    assert conv.cfg["current_max_age_days"] == 3


def test_config_values_applied():
    conv = _conv(cfg={"fx": {"min_flow_coverage": 0.5, "max_flow_gap_days": 25,
                            "naeherung_stichtag_tol_days": 30}})
    assert conv.cfg["min_flow_coverage"] == 0.5
    assert conv.cfg["max_flow_gap_days"] == 25
    assert conv.cfg["naeherung_stichtag_tol_days"] == 30


# ------------------------------------------- (9) GBP als Hauptwaehrung --

def test_gbp_pair_works_like_iso_majors():
    """GBP paart normal mit USD; GBP==GBP ohne Abfrage; Absturzfrei ohne Daten."""
    mc = _MockClient({("GBPUSD", 2026): _series(1.27, 2026)})
    c = _conv(client=mc)
    q = c.quote("GBP", "USD", "2026-06-30")
    assert q.rate == pytest.approx(1.27)
    assert c.quote("GBP", "GBP", "2026-06-30").rate == 1.0
    fq = c.flow_quote("GBP", "EUR", "2026-01-01", "2026-12-31")
    assert fq.ok is False          # keine EURGBP-Daten -> NaN, kein Absturz


# ------------------------------------------- (10) Legacy-Kompatibilitaet --

def test_legacy_convert_year_semantics():
    """legacy convert(value, frm, to, year): Jahresendkurs wie convert_stock
    zum 31.12.; gleiche Waehrung + leere Waehrung wie _NoFX (NaN)."""
    c = _conv(client=_MockClient({("EURUSD", 2026): {"2026-12-31": 1.05}}))
    assert c.convert(100.0, "EUR", "USD", 2026) == pytest.approx(105.0)
    assert c.convert(100.0, "USD", "USD", 2026) == pytest.approx(100.0)
    assert math.isnan(c.convert(100.0, "EUR", "JPY", 2026))
    assert math.isnan(c.convert(100.0, "", "USD", 2026))


def test_legacy_rate_equivalent_quote():
    c = _conv(client=_year(1.10, 2026))
    assert c.rate("EUR", "USD", "2026-06-30") == pytest.approx(1.10)
    assert math.isnan(c.rate("EUR", "JPY", "2026-06-30"))


# ------------------------------- (11) Korrekturen A-D (externe Pruefung) --

def test_flow_inverse_daily_reciprocal_then_average():
    """A: inverse Tageskurse werden EINZELN rezipiziert und DANN gemittelt
    (mean(1/x)); nicht 1/mean(x). Zwei unterschiedliche Kurse an
    aufeinanderfolgenden Werktagen, Gap-Bedingung nicht verletzt."""
    c = _conv(client=_MockClient({("USDEUR", 2026): {
        "2026-06-01": 0.80, "2026-06-02": 1.00}}))   # Mo, Di
    fq = c.flow_quote("EUR", "USD", "2026-06-01", "2026-06-02")
    assert fq.ok and fq.inverse is True
    assert fq.basis == "periodendurchschnitt"
    assert fq.coverage == pytest.approx(1.0) and fq.max_gap_days == 0
    # korrekt: mean(1/0.80, 1/1.00) = 1.125 -- alt (1/mean(0.80,1.00)) = 1.11...
    assert fq.rate == pytest.approx((1.0 / 0.80 + 1.0 / 1.00) / 2.0)
    assert fq.rate != pytest.approx(1.0 / ((0.80 + 1.00) / 2.0))


def test_daily_in_window_cache_gap_filled_by_api(tmp_path):
    """B: unvollstaendiger Cache -- fehlende Tage werden per API ergaenzt;
    vorhandene gueltige Cache-Werte gewinnen (kein Ueberschreiben)."""
    cache = FXCache(str(tmp_path / "fx.db"))
    full_api = _series(1.20, 2026)
    skip = set(list(full_api)[:10] + list(full_api)[20:30])
    cached_part = {d: 1.10 for d in full_api if d not in skip}
    cache.put_daily("EURUSD", cached_part, "test")
    c = _conv(client=_MockClient({("EURUSD", 2026): full_api}), cache=cache)
    merged = c._daily_in_window("EURUSD", dt.date(2026, 1, 1),
                                dt.date(2026, 12, 31))
    d_cache = sorted(cached_part)[0]        # Tag in Cache UND API, Werte unterschiedlich
    d_api = sorted(skip)[0]                 # Tag nur in API (Cache-Luecke)
    assert merged[d_cache] == pytest.approx(1.10)   # Cache gewinnt
    assert merged[d_api] == pytest.approx(1.20)     # Luecke ergaenzt
    assert set(merged) == set(full_api)             # vollstaendig
    fq = c.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    assert fq.ok and fq.basis == "periodendurchschnitt"
    assert fq.coverage == pytest.approx(1.0)   # vollstaendig ergaenzte Reihe


def test_daily_in_window_gap_without_client_stays_incomplete(tmp_path):
    """B ohne API-Client: unvollstaendiger Cache bleibt unvollstaendig
    (kein Erfinden, kein Absturz) -- Coverage-Pruefung macht es sichtbar."""
    cache = FXCache(str(tmp_path / "fx.db"))
    quarter = {d: 1.10 for d in _series(1.10, 2026) if d[5:7] <= "03"}
    cache.put_daily("EURUSD", quarter, "test")
    c = _conv(client=None, cache=cache)
    merged = c._daily_in_window("EURUSD", dt.date(2026, 1, 1),
                                dt.date(2026, 12, 31))
    assert merged == quarter


def test_manual_avg_not_for_partial_window_q1():
    """C: avg:YYYY NUR fuer das VOLLE Kalenderjahr (01.01.-31.12.) --
    Q1-Anfrage darf vorhandenes avg:2026 NICHT verwenden."""
    conv = _conv()
    conv.manual = {"EURUSD": {"avg:2026": 1.07}}
    fq = conv.flow_quote("EUR", "USD", "2026-01-01", "2026-03-31")
    assert fq.ok is False


def test_manual_avg_full_calendar_year_still_used():
    """C: Gegenprobe -- volles Kalenderjahr verwendet avg:YYYY."""
    conv = _conv()
    conv.manual = {"EURUSD": {"avg:2026": 1.07}}
    fq = conv.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    assert fq.ok and fq.rate == pytest.approx(1.07) and fq.source == "manual"


def test_quote_invalid_newer_cache_then_older_valid(tmp_path):
    """D: ungueltiger juengerer Cache-Kurs wird geloescht; danach wird der
    aeltere gueltige Kurs <= Stichtag geliefert (ohne API-Key)."""
    cache = FXCache(str(tmp_path / "fx.db"))
    cache.put_daily("EURUSD", {"2026-12-28": 1.05}, "test")
    with sqlite3.connect(str(cache.db_path)) as conn:
        conn.execute("INSERT INTO fx_daily (pair, date, rate, source) "
                     "VALUES ('EURUSD', '2026-12-31', 0.0, 'bad')")
    c = _conv(client=None, cache=cache)
    q = c.quote("EUR", "USD", "2026-12-31")
    assert q.ok and q.source == "cache"
    assert q.rate == pytest.approx(1.05) and q.asof == "2026-12-28"
    with sqlite3.connect(str(cache.db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fx_daily "
                            "WHERE pair='EURUSD' AND date='2026-12-31'"
                            ).fetchone()[0] == 0
    # strikt <= Stichtag: Anfrage VOR dem aeltesten gueltigen Kurs -> NaN
    assert c.quote("EUR", "USD", "2026-12-27").ok is False


def test_ok_properties_use_valid_rate():
    """Haertung: ok ueber _valid_rate (endlich UND > 0), nicht nur NaN-Check."""
    from data.fx import FxQuote, FxFlowQuote  # noqa: E402
    assert FxQuote(rate=1.0).ok is True
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        assert FxQuote(rate=bad).ok is False
        assert FxFlowQuote(rate=bad).ok is False


def test_manual_yaml_non_dict_root_ignored(tmp_path):
    """Härtung: YAML-Wurzel kein Mapping (z. B. Liste) -> Warnung, keine
    manuellen Kurse, kein Absturz, keine erfundenen Kurse."""
    p = tmp_path / "fx_rates.yaml"
    p.write_text("- EURUSD\n", encoding="utf-8")
    conv = FXConverter(client=None, cache=None, manual_path=str(p))
    assert conv.manual == {}
    assert conv.quote("EUR", "USD", "2026-06-30").ok is False


# -------------------- (12) Nachbesserung: persistente Cache-Prioritaet ---

def test_fetch_year_daily_preserves_existing_cache(tmp_path):
    """put_daily_if_absent: nach API-Abruf bleibt ein vorhandener Cache-Wert
    PERSISTENT erhalten (erneutes SQLite-Auslesen), fehlende Tage werden
    ergaenzt."""
    cache = FXCache(str(tmp_path / "fx.db"))
    full_api = _series(1.20, 2026)
    skip = set(list(full_api)[:10] + list(full_api)[20:30])
    cached_part = {d: 1.10 for d in full_api if d not in skip}
    cache.put_daily("EURUSD", cached_part, "alt")
    c = _conv(client=_MockClient({("EURUSD", 2026): full_api}), cache=cache)
    c._daily_in_window("EURUSD", dt.date(2026, 1, 1), dt.date(2026, 12, 31))
    with sqlite3.connect(str(cache.db_path)) as conn:
        rows = dict(conn.execute(
            "SELECT date, rate FROM fx_daily WHERE pair='EURUSD'").fetchall())
        srcs = dict(conn.execute(
            "SELECT date, source FROM fx_daily WHERE pair='EURUSD'").fetchall())
    for d in cached_part:      # urspruengliche Werte unveraendert
        assert rows[d] == pytest.approx(1.10) and srcs[d] == "alt"
    for d in skip:            # Luecken wurden ergaenzt und gespeichert
        assert rows[d] == pytest.approx(1.20) and srcs[d] == "fmp"
    assert set(rows) == set(full_api)


# --------------- (13) Nachbesserung: inverse manuelle Jahresdurchschnitte --

def test_inverse_manual_avg_vs_daily_reciprocal_basis():
    """Reziproker manueller avg:YYYY -> basis='manueller_jahresdurchschnitt';
    Perioden-O aus reziproken Tageskursen -> basis='periodendurchschnitt'.
    Direkter Vergleich beider Faelle."""
    conv_a = _conv()
    conv_a.manual = {"USDEUR": {"avg:2026": 0.90}}
    fq_man = conv_a.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    assert fq_man.ok and fq_man.inverse is True
    assert fq_man.basis == "manueller_jahresdurchschnitt"
    assert fq_man.source == "manual" and fq_man.n_closes == -1
    assert math.isnan(fq_man.coverage) and fq_man.rate == pytest.approx(1.0 / 0.90)

    conv_b = _conv(client=_MockClient({("USDEUR", 2026): _series(0.90, 2026)}))
    fq_daily = conv_b.flow_quote("EUR", "USD", "2026-01-01", "2026-12-31")
    assert fq_daily.ok and fq_daily.inverse is True
    assert fq_daily.basis == "periodendurchschnitt"
    assert fq_daily.n_closes > 0 and fq_daily.coverage >= 0.95
    assert 0 < fq_daily.coverage < float("inf")
    assert fq_daily.rate == pytest.approx(1.0 / 0.90)
    # Kennzeichnung ist eindeutig verschieden
    assert fq_man.basis != fq_daily.basis


# ------------- (14) Schreibsemantik: ungueltig ersetzen, gueltig behalten --

def test_invalid_cache_entry_replaced_by_api_valid_kept(tmp_path):
    """Erwartete Schreibsemantik:
    - ungueltiger bestehender Eintrag -> im Ergebnis UND persistent durch
      den gueltigen API-Kurs ersetzt (Rate 0.0 weg, 1.10 drin),
    - gueltiger bestehender Eintrag (1.05) -> weder im Ergebnis noch in
      SQLite ueberschrieben,
    - fehlender Eintrag -> ergaenzt."""
    cache = FXCache(str(tmp_path / "fx.db"))
    with sqlite3.connect(str(cache.db_path)) as conn:
        conn.execute("INSERT INTO fx_daily (pair, date, rate, source) "
                     "VALUES ('EURUSD', '2026-06-01', 0.0, 'bad')")
        conn.execute("INSERT INTO fx_daily (pair, date, rate, source) "
                     "VALUES ('EURUSD', '2026-06-02', 1.05, 'guet')")
    mocks = _MockClient({("EURUSD", 2026): {"2026-06-01": 1.10,
                                           "2026-06-02": 9.99,
                                           "2026-06-03": 1.20}})
    c = _conv(client=mocks, cache=cache)
    merged = c._daily_in_window("EURUSD", dt.date(2026, 6, 1),
                                dt.date(2026, 6, 3))
    # Ergebnis: ungueltig ersetzt, gueltig behalten, fehlend ergaenzt
    assert merged["2026-06-01"] == pytest.approx(1.10)
    assert merged["2026-06-02"] == pytest.approx(1.05)
    assert merged["2026-06-03"] == pytest.approx(1.20)
    # persistent: SQLite erneut auslesen
    with sqlite3.connect(str(cache.db_path)) as conn:
        rows = dict(conn.execute("SELECT date, rate FROM fx_daily "
                                "WHERE pair='EURUSD'").fetchall())
    assert rows == {"2026-06-01": 1.10, "2026-06-02": 1.05,
                    "2026-06-03": 1.20}
