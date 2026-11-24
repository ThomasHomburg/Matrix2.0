# v75-Tests fuer den Point-in-time-Fundamentalspeicher (offline, synthetische
# companyfacts; kein Netz, keine echten SEC-Dateien).

import importlib.util
import json
import os
import sqlite3
import sys

import pandas as pd
import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))

from data.pit_schema import ensure_schema  # noqa: E402
from data import pit  # noqa: E402


def _lade_ingest():
    spec = importlib.util.spec_from_file_location(
        "v75_pit_ingest", os.path.join(REPO, "tools", "v75_pit_ingest.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _companyfacts(duration_eintraege=None, instant_eintraege=None):
    """Baut eine synthetische companyfacts-Antwort.
    duration_eintraege: {tag: [entry-dicts]}
    instant_eintraege:  {tag: [entry-dicts]} (ohne start)."""
    facts = {"us-gaap": {}, "dei": {}}
    for tag, eintraege in (duration_eintraege or {}).items():
        ns = "dei" if tag == "EntityCommonStockSharesOutstanding" else "us-gaap"
        facts[ns][tag] = {"units": {"USD": eintraege}}
    for tag, eintraege in (instant_eintraege or {}).items():
        unit = "shares" if "Shares" in tag else "USD"
        facts["us-gaap"][tag] = {"units": {unit: eintraege}}
    return {"cik": 1, "entityName": "Test", "facts": facts}


def _entry(end, val, filed, form="10-K", accn="000000",
           start=None, fy=False):
    e = {"end": end, "val": val, "filed": filed, "form": form, "accn": accn}
    if start:
        e["start"] = start
    return e


@pytest.fixture()
def con(tmp_path):
    db = tmp_path / "pit.db"
    c = sqlite3.connect(str(db))
    ensure_schema(c)
    yield c
    c.close()


def _ingest_payload(con, cik10, payload, quelle="TEST"):
    mod = _lade_ingest()
    fakten, verworfen = mod.facts_aus_companyfacts(payload, cik10)
    neu = mod.schreibe_aenderungsprotokoll(con, fakten)
    return neu, fakten, verworfen


# ---------------------------------------------------------------------------
# 1) Restatement as-of: erst ab filed + 1 Tag sichtbar
# ---------------------------------------------------------------------------

def test_as_of_restatement_erst_ab_filed_plus_1(con):
    payload = _companyfacts(duration_eintraege={
        "Revenues": [
            _entry("2019-12-31", 100.0, "2020-02-01", start="2019-01-01"),
            _entry("2019-12-31", 140.0, "2021-02-15", start="2019-01-01"),
        ]})
    _ingest_payload(con, "0000000001", payload)
    # Stichtag 2020-06-30 -> alte Fassung (100)
    df = pit.facts_as_of(con, "0000000001", "2020-06-30", ["Revenues"])
    assert len(df) == 1 and df.iloc[0]["value"] == 100.0
    # Stichtag exakt filed+0 der neuen Fassung -> noch alt
    df = pit.facts_as_of(con, "0000000001", "2021-02-15", ["Revenues"])
    assert df.iloc[0]["value"] == 100.0
    # Stichtag filed+1 -> neue Fassung
    df = pit.facts_as_of(con, "0000000001", "2021-02-16", ["Revenues"])
    assert df.iloc[0]["value"] == 140.0


# ---------------------------------------------------------------------------
# 2) Aenderungsprotokoll: same-filed-Tiebreak (accn) und Wertsprung X -> Y -> X
# ---------------------------------------------------------------------------

def test_aenderungsprotokoll_tiebreak_und_wertsprung(con):
    payload = _companyfacts(duration_eintraege={
        "Revenues": [
            # Fassung 1: 4 Refilings, davon 2 mit Aenderung
            _entry("2019-12-31", 100.0, "2020-02-01", start="2019-01-01",
                   accn="A1"),
            _entry("2019-12-31", 100.0, "2020-03-01", start="2019-01-01",
                   accn="A2"),            # identisches Refiling
            _entry("2019-12-31", 110.0, "2020-12-01", start="2019-01-01",
                   accn="A3"),            # Aenderung
            _entry("2019-12-31", 110.0, "2021-01-05", start="2019-01-01",
                   accn="A4"),            # identisch
            # gleiche filed_date, zwei Fassungen: accn entscheidet
            _entry("2019-12-31", 120.0, "2021-02-10", start="2019-01-01",
                   accn="B1", form="10-K/A"),
            _entry("2019-12-31", 130.0, "2021-02-10", start="2019-01-01",
                   accn="B2", form="10-K/A"),
            # Wertsprung zurueck zu X
            _entry("2019-12-31", 100.0, "2022-02-10", start="2019-01-01",
                   accn="C1", form="10-K"),
        ]})
    neu, fakten, _v = _ingest_payload(con, "0000000001", payload)
    # gespeichert: 100(A1), 110(A3), 120(B1), 130(B2), 100(C1) = 5
    assert neu == 5
    n = con.execute("SELECT COUNT(*) FROM pit_facts").fetchone()[0]
    assert n == 5
    # as-of: 2021-02-10 + 1 -> accn-Tiebreaker: B2 (130) ist juengste Fassung
    df = pit.facts_as_of(con, "0000000001", "2021-02-11", ["Revenues"])
    assert df.iloc[0]["value"] == 130.0
    assert df.iloc[0]["accn"] == "B2"
    # Ruecksprung X: ab 2022-02-11 wieder 100
    df = pit.facts_as_of(con, "0000000001", "2022-02-11", ["Revenues"])
    assert df.iloc[0]["value"] == 100.0
    # zwischendurch (2021-06-30) bleibt 130 gueltig
    df = pit.facts_as_of(con, "0000000001", "2021-06-30", ["Revenues"])
    assert df.iloc[0]["value"] == 130.0


# ---------------------------------------------------------------------------
# 3) Identische Refilings nicht doppelt speichern
# ---------------------------------------------------------------------------

def test_identische_refilings_nur_einmal(con):
    payload = _companyfacts(duration_eintraege={
        "OperatingIncomeLoss": [
            _entry("2020-12-31", 50.0, "2021-02-01", start="2020-01-01",
                   accn="F1"),
            _entry("2020-12-31", 50.0, "2021-05-01", start="2020-01-01",
                   accn="F2"),
            _entry("2020-12-31", 50.0, "2021-08-01", start="2020-01-01",
                   accn="F3"),
        ]})
    neu, _fakten, _v = _ingest_payload(con, "0000000002", payload)
    assert neu == 1
    n = con.execute("SELECT COUNT(*) FROM pit_facts").fetchone()[0]
    assert n == 1


# ---------------------------------------------------------------------------
# 4) Dauerfilter bei fy_series: 90-Tage-Fakt bleibt in der DB, nicht in Serie
# ---------------------------------------------------------------------------

def test_dauerfilter_fy_series(con):
    payload = _companyfacts(duration_eintraege={
        "Revenues": [
            _entry("2020-12-31", 500.0, "2021-02-01", start="2020-01-01"),
            _entry("2020-09-30", 300.0, "2020-10-30", start="2020-01-01",
                   form="10-Q", accn="Q1"),   # 273 Tage
            _entry("2020-06-30", 150.0, "2020-07-30", start="2020-01-01",
                   form="10-Q", accn="Q2"),  # 181 Tage
        ]})
    _ingest_payload(con, "0000000003", payload)
    n = con.execute("SELECT COUNT(*) FROM pit_facts").fetchone()[0]
    assert n == 3  # alle Fakte in der DB (Dauer erst bei Abfrage)
    serie = pit.fy_series(con, "0000000003", "2021-06-30", "revenue")
    assert len(serie) == 1
    assert serie.iloc[0]["value"] == 500.0


# ---------------------------------------------------------------------------
# 5) 52/53-Wochen-Jahre (371 Tage) in fy_series
# ---------------------------------------------------------------------------

def test_fy_series_52_53_wochen(con):
    payload = _companyfacts(duration_eintraege={
        "Revenues": [
            _entry("2021-01-31", 900.0, "2021-03-15", start="2020-01-26",
                   accn="W1"),   # 370 Tage
            _entry("2022-01-31", 950.0, "2022-03-15", start="2021-01-31",
                   accn="W2"),   # 365 Tage
        ]})
    _ingest_payload(con, "0000000004", payload)
    serie = pit.fy_series(con, "0000000004", "2022-06-30", "revenue")
    assert len(serie) == 2
    assert set(serie["value"]) == {900.0, 950.0}


# ---------------------------------------------------------------------------
# 6) Tag-Hierarchie der Familien
# ---------------------------------------------------------------------------

def test_tag_hierarchie_revenue(con):
    payload = _companyfacts(duration_eintraege={
        "Revenues": [
            _entry("2021-12-31", 700.0, "2022-02-01", start="2021-01-01"),
        ],
        "SalesRevenueNet": [
            _entry("2021-12-31", 650.0, "2022-02-01", start="2021-01-01"),
        ],
        "RevenueFromContractWithCustomerExcludingAssessedTax": [
            _entry("2021-12-31", 680.0, "2022-02-01", start="2021-01-01"),
        ]})
    _ingest_payload(con, "0000000005", payload)
    serie = pit.fy_series(con, "0000000005", "2022-06-30", "revenue")
    assert len(serie) == 1
    assert serie.iloc[0]["tag"] == "Revenues"   # Prioritaet 0
    assert serie.iloc[0]["value"] == 700.0


# ---------------------------------------------------------------------------
# 7) Idempotenz: zweiter Lauf schreibt 0 neue Zeilen
# ---------------------------------------------------------------------------

def test_idempotenz_zweiter_lauf(con):
    payload = _companyfacts(duration_eintraege={
        "NetIncomeLoss": [
            _entry("2020-12-31", 10.0, "2021-02-01", start="2020-01-01"),
            _entry("2020-12-31", 12.0, "2022-02-01", start="2020-01-01",
                   accn="N2"),
        ]})
    neu1, _f, _v = _ingest_payload(con, "0000000006", payload)
    assert neu1 == 2
    neu2, _f, _v = _ingest_payload(con, "0000000006", payload)
    assert neu2 == 0
    n = con.execute("SELECT COUNT(*) FROM pit_facts").fetchone()[0]
    assert n == 2


# ---------------------------------------------------------------------------
# 8) GOOGL-Fallback: dei fehlt, us-gaap CommonStockSharesOutstanding instant
# ---------------------------------------------------------------------------

def test_shares_outstanding_fallback_instant(con):
    payload = _companyfacts(instant_eintraege={
        "CommonStockSharesOutstanding": [
            _entry("2022-01-01", 132000000000, "2022-02-02",
                   accn="S1"),
            _entry("2021-01-01", 30000000000, "2021-02-02", accn="S2"),
        ]})
    neu, _f, _v = _ingest_payload(con, "0000000007", payload)
    assert neu == 2
    wert = pit.instant_wert(con, "0000000007", "2022-06-30",
                            "shares_outstanding_fallback")
    assert wert["value"] == 132000000000
    assert wert["tag"] == "CommonStockSharesOutstanding"
    # as-of vor dem neueren Filing
    wert = pit.instant_wert(con, "0000000007", "2022-02-02",
                            "shares_outstanding_fallback")
    assert wert["value"] == 30000000000


# ---------------------------------------------------------------------------
# 9) Speichergrenze: 50 synthetische Dateien je 20 MB, RSS bleibt gering
# ---------------------------------------------------------------------------

def _rss_mb():
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return 0.0


def test_speichergrenze_streaming(tmp_path):
    """Streamt 50 synthetische Ticker-Dateien je ~20 MB, je Datei ein
    CIK; Roh-Objekte werden nach der Aufnahme freigegeben, RSS muss unter
    der Schranke bleiben (Produktivumgebung: 4-GB-Cgroup-Sandbox)."""
    mod = _lade_ingest()
    grenze_mb = 900.0
    n_eintraege = 170000      # ~20-22 MB je Datei
    dateien = []
    try:
        for nr in range(50):
            grosse = {"facts": {"us-gaap": {"Revenues": {"units": {
                "USD": [
                    {"end": "2021-12-31", "start": "2021-01-01",
                     "val": float(nr * 1000 + j % 5), "filed": "2022-02-01",
                     "form": "10-K", "accn": "X" + str(nr) + str(j)}
                    for j in range(n_eintraege)
                ]}}}}}
            pfad = tmp_path / ("gross_%02d.json" % nr)
            with open(pfad, "w", encoding="utf-8") as fh:
                json.dump(grosse, fh)
            assert pfad.stat().st_size >= 19_000_000
            dateien.append(pfad)
        obere = _rss_mb()
        con = sqlite3.connect(str(tmp_path / "stream.db"))
        ensure_schema(con)
        for nr, pfad in enumerate(dateien):
            with open(pfad, encoding="utf-8") as fh:
                geladen = json.load(fh)
            cik10 = str(nr).zfill(10)
            f, _v = mod.facts_aus_companyfacts(geladen, cik10)
            mod.schreibe_aenderungsprotokoll(con, f)
            del geladen, f
        assert _rss_mb() < grenze_mb
        con.close()
    finally:
        for p in dateien:
            try:
                os.remove(p)
            except OSError:
                pass
