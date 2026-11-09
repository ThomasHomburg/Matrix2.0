r"""
(v52) decisions.py -- Entscheidungslog mit Vergleichkoerben (kein Netz, Fixture-CSVs):

(1) Koerbe aus config decisions.baskets: screener (Kandidat/Beobachtung...),
    divergenz (Status nein + divergenz_stufe stark + Quality >= 60, max 15, sortiert
    nach SM-Score dann Quality; ohne divergenz_stufe Ersatzregel SM-Score >= 3 UND
    Quality >= 60), kontrolle (15 per Zufall, Seed 42, Status nein + SM-Score 1 +
    ohne Fondsaktivitaet, Benchmark-Ticker ausgeschlossen), benchmark (SPY, RSP).
(2) snapshot --korb all --datum: eine Zeile je (ticker, korb, datum), aktion=auto,
    idempotent, geschriebene Zeilen werden nie ueberschrieben.
(4) review: fuellt Kurse nach, Kennzahlen je Korb/Horizont (Mittelwert, Median,
    Anteil Ueberrendite vs. SPY, Differenz zu kontrolle, n, Brier), n < 30 ->
    "unkalibriert, n=...", Ausgabe Tabelle + decisions_review.csv.
"""

import csv
import random
import sqlite3
from argparse import Namespace
from datetime import date, timedelta

import pytest

from config import load_config
import decisions

RANKING_COLS = ["ticker", "status_all", "quality", "mos_rule", "mos_robust",
                "kurs", "kurs_datum", "iv_bear", "iv_base", "iv_bull"]
OVERLAP_COLS = ["ticker", "status_all", "quality", "mos_rule", "smart_money_score",
                "fonds_kaeufer", "fonds_verkaeufer", "smart_money_urteil", "divergenz_stufe"]


def _cfg():
    return load_config()


def _write_ranking(path):
    rows = []
    # screener
    for t, st, q in [("K1", "Kandidat", 70), ("K2", "Beobachtung", 65),
                     ("K3", "Beobachtung (nicht normalisiert)", 80)]:
        rows.append([t, st, q, 0.40, 0.35, 100.0, "2026-08-20", 80.0, 150.0, 200.0])
    # divergenz-Kandidaten (nein, stark, quality >= 60) + Ausschluesse
    rows.append(["D1", "nein", 75, -0.20, -0.25, 100.0, "2026-08-20", 80.0, 150.0, 200.0])
    rows.append(["D2", "nein", 70, -0.10, -0.15, 100.0, "2026-08-20", 80.0, 150.0, 200.0])
    rows.append(["D3", "nein", 65, -0.05, -0.10, 100.0, "2026-08-20", 80.0, 150.0, 200.0])
    rows.append(["D4", "nein", 80, -0.30, -0.35, 100.0, "2026-08-20", 80.0, 150.0, 200.0])  # schwach
    rows.append(["D5", "nein", 50, -0.20, -0.25, 100.0, "2026-08-20", 80.0, 150.0, 200.0])  # quality < 60
    # kontrolle-Kandidaten: 20x nein + SM-Score 1 + ohne Fondsaktivitaet
    for i in range(1, 21):
        rows.append([f"C{i:02d}", "nein", 40, -0.40, -0.45, 100.0, "2026-08-20", 80.0, 150.0, 200.0])
    rows.append(["CX", "nein", 40, -0.40, -0.45, 100.0, "2026-08-20", 80.0, 150.0, 200.0])  # hat Kaeufer
    # benchmark
    rows.append(["SPY", "nein", 55, 0.10, 0.10, 100.0, "2026-08-20", 90.0, 110.0, 130.0])
    rows.append(["RSP", "nein", 55, 0.10, 0.10, 100.0, "2026-08-20", 90.0, 110.0, 130.0])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(RANKING_COLS)
        w.writerows(rows)


def _write_overlap(path, with_stufe=True):
    cols = OVERLAP_COLS if with_stufe else [c for c in OVERLAP_COLS if c != "divergenz_stufe"]
    rows = []
    for t in ("K1", "K2", "K3"):
        rows.append([t, "", 70, 0.4, 2, "Berkshire Hathaway", "", "Bestaetigung", "-"])
    rows.append(["D1", "nein", 75, -0.2, 4, "Berkshire Hathaway", "", "Divergenz", "stark"])
    rows.append(["D2", "nein", 70, -0.1, 3, "Pershing Square", "", "Divergenz", "stark"])
    rows.append(["D3", "nein", 65, -0.05, 3, "FondsAktuell", "", "Divergenz", "stark"])
    rows.append(["D4", "nein", 80, -0.3, 2, "", "", "neutral", "schwach"])
    rows.append(["D5", "nein", 50, -0.2, 4, "Berkshire Hathaway", "", "Divergenz", "stark"])
    for i in range(1, 21):
        rows.append([f"C{i:02d}", "nein", 40, -0.4, 1, "", "", "neutral", "-"])
    rows.append(["CX", "nein", 40, -0.4, 2, "Berkshire Hathaway", "", "Bestaetigung", "-"])
    rows.append(["SPY", "nein", 55, 0.1, 1, "", "", "neutral", "-"])
    rows.append(["RSP", "nein", 55, 0.1, 1, "", "", "neutral", "-"])
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(r if with_stufe else r[:-1])


@pytest.fixture
def fixture_csv(tmp_path):
    ranking = tmp_path / "ranking.csv"
    overlap = tmp_path / "overlap.csv"
    overlap_nostufe = tmp_path / "overlap_ohne_stufe.csv"
    _write_ranking(ranking)
    _write_overlap(overlap, with_stufe=True)
    _write_overlap(overlap_nostufe, with_stufe=False)
    return str(ranking), str(overlap), str(overlap_nostufe)


# --------------------------------------------------------------------------
# (1) Korb-Zusammensetzung
# --------------------------------------------------------------------------

def test_koerbe_fixture_zusammensetzung(fixture_csv, capsys):
    ranking, overlap, _ = fixture_csv
    cfg = _cfg()
    rows = decisions._korb_rows(ranking, overlap)
    k = decisions.koerbe(cfg, rows)
    print("\nKorbzusammensetzung Fixture:")
    for name in decisions.KORB_NAMEN:
        print(f"  {name}: {[r['ticker'] for r in k[name]]}")
    assert [r["ticker"] for r in k["screener"]] == ["K1", "K2", "K3"]
    # divergenz: nur stark + quality >= 60, sortiert nach SM-Score, dann Quality
    assert [r["ticker"] for r in k["divergenz"]] == ["D1", "D2", "D3"]
    # kontrolle: 15 aus 20 Kandidaten (CX hat Kaeufer, SPY/RSP sind Benchmark), Seed 42
    cands = sorted(f"C{i:02d}" for i in range(1, 21))
    expected = random.Random(42).sample(cands, 15)
    assert [r["ticker"] for r in k["kontrolle"]] == expected
    assert len(k["kontrolle"]) == 15
    assert all(decisions._f(r, "smart_money_score") == 1 for r in k["kontrolle"])
    assert all(not r.get("fonds_kaeufer") and not r.get("fonds_verkaeufer") for r in k["kontrolle"])
    assert "SPY" not in {r["ticker"] for r in k["kontrolle"]}
    assert [r["ticker"] for r in k["benchmark"]] == ["SPY", "RSP"]
    out = capsys.readouterr().out
    assert "screener: ['K1', 'K2', 'K3']" in out


def test_koerbe_reproduzierbar_und_cap15(fixture_csv):
    ranking, overlap, _ = fixture_csv
    cfg = _cfg()
    rows = decisions._korb_rows(ranking, overlap)
    # 18 stark-Kandidaten -> Kappe bei 15, Sortierung nach Score/Quality absteigend
    extra = []
    for i in range(18):
        t = f"E{i:02d}"
        extra.append({"ticker": t, "status_all": "nein", "quality": 60 + (i % 20),
                      "smart_money_score": 3 + (1 if i == 0 else 0), "divergenz_stufe": "stark"})
    k1 = decisions.koerbe(cfg, rows + extra)
    k2 = decisions.koerbe(cfg, rows + extra)
    assert [r["ticker"] for r in k1["divergenz"]] == [r["ticker"] for r in k2["divergenz"]]
    assert len(k1["divergenz"]) == 15
    scores = [decisions._f(r, "smart_money_score") for r in k1["divergenz"]]
    assert scores == sorted(scores, reverse=True)
    # kontrolle reproduzierbar
    assert [r["ticker"] for r in k1["kontrolle"]] == [r["ticker"] for r in k2["kontrolle"]]


def test_divergenz_fallback_ohne_stufe(fixture_csv):
    """Kein v51 (overlap-CSV ohne divergenz_stufe) -> Ersatzregel SM-Score >= 3 UND Quality >= 60."""
    ranking, _, overlap_nostufe = fixture_csv
    cfg = _cfg()
    rows = decisions._korb_rows(ranking, overlap_nostufe)
    assert not any("divergenz_stufe" in r for r in rows)
    k = decisions.koerbe(cfg, rows)
    # D1 (sm 4, q 75), D2/D3 (sm 3, q >= 60); D5 q 50 faellt wegen quality_min raus, D4 sm 2 raus
    assert [r["ticker"] for r in k["divergenz"]] == ["D1", "D2", "D3"]


# --------------------------------------------------------------------------
# (2) snapshot --korb: idempotent, nie ueberschreiben
# --------------------------------------------------------------------------

def test_snapshot_korb_all_idempotent(fixture_csv, tmp_path, capsys):
    ranking, overlap, _ = fixture_csv
    cfg = _cfg()
    db = tmp_path / "d.db"
    args = Namespace(db=str(db), ranking=ranking, korb="all", datum="2026-08-20",
                     overlap=overlap, live=None)
    decisions.cmd_snapshot(args)
    out = capsys.readouterr().out
    assert "Korb screener: 3 geschrieben, 0 bereits vorhanden" in out
    assert "Korb divergenz: 3 geschrieben" in out
    assert "Korb kontrolle: 15 geschrieben" in out
    assert "Korb benchmark: 2 geschrieben" in out

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM decisions WHERE ticker='D1' AND korb='divergenz'").fetchone()
    assert r["aktion"] == "auto" and r["datum"] == "2026-08-20" and r["korb"] == "divergenz"
    assert r["kurs"] == 100.0 and r["kurs_datum"] == "2026-08-20"
    assert r["status_screener"] == "nein" and r["quality"] == 75.0
    assert r["mos_min"] == -0.2 and r["mos_robust"] == -0.25
    assert r["iv_bear"] == 80.0 and r["iv_base"] == 150.0 and r["iv_bull"] == 200.0
    assert r["smart_money_score"] == 4 and r["urteil"] == "Divergenz" and r["divergenz_stufe"] == "stark"
    assert r["fonds_kaeufer"] == "Berkshire Hathaway"
    assert r["gew_bear"] == 0.25 and r["gew_base"] == 0.50 and r["gew_bull"] == 0.25
    # iv_base 150, iv_bull 200 >= 110 -> p = 0.50 + 0.25 = 0.75
    assert r["erwartungswert_pct"] == pytest.approx(75.0)
    assert r["wahrscheinlichkeit_p"] == pytest.approx(0.75)
    n1 = con.execute("SELECT COUNT(*) AS n FROM decisions WHERE korb <> ''").fetchone()["n"]
    assert n1 == 3 + 3 + 15 + 2

    # nochmal gleiches Datum -> nichts geschrieben, nichts ueberschrieben
    decisions.cmd_snapshot(args)
    out = capsys.readouterr().out
    assert "0 Zeilen geschrieben, 23 uebersprungen" in out
    con2 = sqlite3.connect(db)
    n2 = con2.execute("SELECT COUNT(*) AS n FROM decisions WHERE korb <> ''").fetchone()[0]
    assert n2 == n1
    # Kurs in der Quelle aendern -> Zeile bleibt unveraendert (nie ueberschreiben)
    ranking2 = tmp_path / "ranking2.csv"
    _write_ranking(ranking2)
    txt = ranking2.read_text(encoding="utf-8").replace("D1,nein,75,-0.2,-0.25,100.0", "D1,nein,75,-0.2,-0.25,999.0")
    ranking2.write_text(txt, encoding="utf-8")
    decisions.cmd_snapshot(Namespace(db=str(db), ranking=str(ranking2), korb="divergenz",
                                     datum="2026-08-20", overlap=overlap, live=None))
    con3 = sqlite3.connect(db)
    r3 = con3.execute("SELECT kurs FROM decisions WHERE ticker='D1' AND korb='divergenz' "
                      "AND datum='2026-08-20'").fetchone()
    assert r3[0] == 100.0
    # anderes Datum -> neue Zeilen
    decisions.cmd_snapshot(Namespace(db=str(db), ranking=ranking, korb="screener",
                                     datum="2026-11-09", overlap=overlap, live=None))
    con4 = sqlite3.connect(db)
    assert con4.execute("SELECT COUNT(*) AS n FROM decisions WHERE korb='screener' "
                        "AND datum='2026-11-09'").fetchone()[0] == 3


# --------------------------------------------------------------------------
# (4) review: Kurse nachfuellen, Korb-Kennzahlen, Brier, unkalibriert
# --------------------------------------------------------------------------

def test_review_korb_kennzahlen_brier_unkalibriert(fixture_csv, tmp_path, capsys):
    ranking, overlap, _ = fixture_csv
    cfg = _cfg()
    db = tmp_path / "r.db"
    heute = date.today()
    d0 = heute - timedelta(days=800)          # alle Horizonte (max 24M=720d) abgelaufen
    decisions.cmd_snapshot(Namespace(db=str(db), ranking=ranking, korb="all",
                                     datum=d0.isoformat(), overlap=overlap, live=None))
    con = decisions.init_db(db)
    # Kurse je Ticker an den Horizont-Daten: konstante Rendite je Ticker
    rend = {"K1": 0.50, "K2": 0.40, "K3": 0.30, "D1": 0.20, "D2": 0.10, "D3": 0.05,
            "SPY": 0.10, "RSP": 0.08}
    for i in range(1, 21):
        rend[f"C{i:02d}"] = 0.05
    tickers = [r[0] for r in con.execute(
        "SELECT DISTINCT ticker FROM decisions WHERE korb <> ''")]
    for t in tickers:
        k0 = con.execute("SELECT kurs FROM decisions WHERE ticker=? AND korb<>'' LIMIT 1",
                         (t,)).fetchone()[0]
        for h in (3, 6, 12, 24):
            wann = (d0 + timedelta(days=30 * h)).isoformat()
            kn = k0 * (1.0 + rend.get(t, 0.05))
            con.execute("INSERT OR REPLACE INTO prices (ticker, datum, kurs, quelle, geladen_am) "
                        "VALUES (?,?,?,'fixture','x')", (t, wann, kn))
    con.commit()
    con.close()

    out_csv = tmp_path / "decisions_review.csv"
    decisions.cmd_review(Namespace(db=str(db), ranking=None, heute=heute.isoformat(),
                                   ticker=None, out=str(out_csv)))
    out = capsys.readouterr().out
    assert "Korb-Review" in out and "unkalibriert, n=" in out      # n < 30

    rows = list(csv.DictReader(open(out_csv, newline="", encoding="utf-8")))
    by = {(r["korb"], int(r["horizont_monate"])): r for r in rows}
    assert set(by) >= {("screener", 3), ("screener", 6), ("screener", 12), ("screener", 24),
                       ("divergenz", 12), ("kontrolle", 12), ("benchmark", 12)}
    s12 = by[("screener", 12)]
    assert int(s12["n"]) == 3
    assert float(s12["median_pct"]) == pytest.approx(40.0)         # Median 30/40/50 %
    assert float(s12["mittelwert_pct"]) == pytest.approx(40.0)
    assert float(s12["anteil_ueber_spy"]) == pytest.approx(1.0)     # alle > SPY +10 %
    assert float(s12["median_ueberrendite_spy_pct"]) == pytest.approx(30.0)  # 40 - 10
    assert s12["kalibrierung"] == "unkalibriert, n=3"
    # Brier 12M: p = 0.75 je Zeile; K1 +50 % -> E=1 -> (0.75-1)^2; K2 +40 % -> E=1; K3 +30 % -> E=1
    assert float(s12["brier_mean"]) == pytest.approx((0.75 - 1.0) ** 2)
    # Differenz zu kontrolle: kontrolle-Rendite 5 % -> Median-Diff = 40 - 5 = 35
    assert float(s12["median_diff_kontrolle_pct"]) == pytest.approx(35.0)
    # 12M-Kurs wurde nachgefuellt (K1: 100 -> 150)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT kurs_nachher, rendite_pct, ereignis_e, brier FROM decisions "
                    "WHERE ticker='K1' AND korb='screener'").fetchone()
    assert r["kurs_nachher"] == pytest.approx(150.0)
    assert r["rendite_pct"] == pytest.approx(50.0)
    assert r["ereignis_e"] == 1 and r["brier"] == pytest.approx((0.75 - 1.0) ** 2)
