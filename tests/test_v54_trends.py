r"""
(v54) trends.py -- Trend-Layer Teil A (Struktur, Quellenregister, Bewertung; kein Netz):

- connect(): Guard/Migration wie smart_money -- Tabelle mit fehlenden Pflichtspalten
  wird gedroppt und neu aufgebaut; danach PRAGMA-Assert (RuntimeError bei veraltetem
  schema.sql). DB ohne die neuen Tabellen -> werden angelegt. sources.yaml wird geseedet.
- import-trends / import-evidence / import-exposure: atomar, KEIN Eintrag ohne
  quelle_url (Abbruch mit Zeilennummer, nichts importiert).
- compute_cfs: CFS aus neuesten Belegen je Layer, Layer ohne Beleg = n/a,
  Renormalisierung der Layer-Gewichte, Abdeckung wird ausgewiesen.
- konvergenz (mind. 2 Layer mit Score >= 3) und divergenz_warnung (L2 niedrig bei
  hohem L1/L3 oder umgekehrt).
- ampel: rot bei exposure <= -1 + Stufe vertiefen/handeln; gelb bei monitor;
  gruen sonst; n/a ohne Exposure.
- overlay: Watchlist x Trends -> CSV.
"""

import sqlite3
from pathlib import Path

import pytest

from config import load_config
import trends


def _cfg():
    return load_config()


def _db(tmp_path):
    return trends.connect(tmp_path / "t.db", _cfg())


def _import_trends(con, tmp_path):
    p = tmp_path / "trends.csv"
    p.write_text("name,typ,richtung,horizont,status\n"
                 "Trend A,technologie,positiv,5y,aktiv\n"
                 "Trend B,markt,negativ,2y,aktiv\n", encoding="utf-8")
    return trends.import_trends(con, str(p))


def _ev(con, trend_name, layer, score, url="https://example.com/e", datum="2026-10-01"):
    tid = trends._trend_id(con, trend_name)
    con.execute(
        "INSERT INTO trend_evidence (trend_id, layer, score, beleg_text, quelle_url, "
        "beleg_datum, abruf_datum, erfasst_von) VALUES (?,?,?,?,?,?,?,?)",
        (tid, layer, score, f"Beleg {layer}", url, datum, "2026-11-01", "manuell"))
    con.commit()


# --------------------------------------------------------------------------
# connect / Migration / Guard / sources-Seed
# --------------------------------------------------------------------------

def test_connect_legt_tabellen_an_und_seeded_sources(tmp_path):
    con = _db(tmp_path)
    tabs = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"sources", "trends", "trend_evidence", "trend_exposure"} <= tabs
    n = con.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
    assert n == 11                                  # sources.yaml
    layers = {r["layer"] for r in con.execute("SELECT DISTINCT layer FROM sources")}
    assert layers == {"L1", "L2", "L3", "L4"}


def test_migration_veraltete_tabelle_wird_neu_aufgebaut(tmp_path, caplog):
    import logging
    db = tmp_path / "alt.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE trends (id INTEGER PRIMARY KEY, name TEXT)")  # ohne status/...
    raw.execute("INSERT INTO trends VALUES (1, 'alt')")
    raw.commit(); raw.close()
    with caplog.at_level(logging.WARNING, logger="value_screener.trends"):
        con = trends.connect(db, _cfg())
    assert any("Migration trends" in r.getMessage() for r in caplog.records)
    cols = {r[1] for r in con.execute("PRAGMA table_info(trends)")}
    assert {"id", "name", "typ", "richtung", "horizont", "status"} <= cols
    assert con.execute("SELECT COUNT(*) FROM trends").fetchone()[0] == 0   # Altzeilen verworfen


# --------------------------------------------------------------------------
# Imports: atomar, quelle_url Pflicht
# --------------------------------------------------------------------------

def test_import_trends_upsert(tmp_path):
    con = _db(tmp_path)
    assert _import_trends(con, tmp_path) == 2
    p = tmp_path / "t2.csv"
    p.write_text("name,typ,richtung,horizont,status\nTrend A,markt,gemischt,3y,beobachten\n",
                 encoding="utf-8")
    assert trends.import_trends(con, str(p)) == 1
    r = con.execute("SELECT typ, status FROM trends WHERE name='Trend A'").fetchone()
    assert r["typ"] == "markt" and r["status"] == "beobachten"
    assert con.execute("SELECT COUNT(*) AS n FROM trends").fetchone()["n"] == 2


def test_import_evidence_ohne_quelle_url_abbruch_atomar_mit_zeilennummer(tmp_path):
    con = _db(tmp_path)
    _import_trends(con, tmp_path)
    p = tmp_path / "ev.csv"
    p.write_text("trend,layer,score,beleg_text,quelle_url,beleg_datum,erfasst_vor\n"
                 "Trend A,L1,4,Beleg ok,https://example.com/a,2026-10-01,manuell\n"
                 "Trend A,L2,2,Beleg ohne Quelle,,2026-10-01,manuell\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Zeile 3"):
        trends.import_evidence(con, _cfg(), str(p))
    # atomar: nichts importiert (auch die gueltige Zeile 2 nicht)
    assert con.execute("SELECT COUNT(*) AS n FROM trend_evidence").fetchone()["n"] == 0


def test_import_exposure_pflicht_quelle_und_bereich(tmp_path):
    con = _db(tmp_path)
    _import_trends(con, tmp_path)
    p = tmp_path / "ex.csv"
    p.write_text("ticker,trend,exposure,begruendung,quelle_url,datum,erfasst_von,geprueft\n"
                 "AAA,Trend A,-2,Belastung,https://example.com/x,2026-11-01,manuell,nein\n"
                 "AAA,Trend B,5,zu hoch,,2026-11-01,manuell,nein\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Zeile 3"):
        trends.import_exposure(con, _cfg(), str(p))
    assert con.execute("SELECT COUNT(*) AS n FROM trend_exposure").fetchone()["n"] == 0
    p2 = tmp_path / "ex2.csv"
    p2.write_text("ticker,trend,exposure,begruendung,quelle_url,datum,erfasst_von,geprueft\n"
                  "AAA,Trend A,-2,Belastung,https://example.com/x,2026-11-01,manuell,nein\n",
                  encoding="utf-8")
    assert trends.import_exposure(con, _cfg(), str(p2)) == 1
    r = con.execute("SELECT exposure FROM trend_exposure WHERE ticker='AAA'").fetchone()
    assert r["exposure"] == -2


# --------------------------------------------------------------------------
# CFS, Renormalisierung, Konvergenz, Divergenz
# --------------------------------------------------------------------------

def test_cfs_renormalisierung_und_abdeckung(tmp_path):
    con = _db(tmp_path)
    _import_trends(con, tmp_path)
    # nur L1 (4) und L3 (2) belegt -> Gewichte 0.25 + 0.25 renormalisiert auf 0.5
    _ev(con, "Trend A", "L1", 4)
    _ev(con, "Trend A", "L3", 2)
    sc = trends.trend_score(con, _cfg(), trends._trend_id(con, "Trend A"))
    assert sc["cfs"] == pytest.approx((4 * 0.25 + 2 * 0.25) / 0.5)   # = 3.0
    assert sc["stufe"] == "vertiefen"                               # 3.0-3.4
    assert sc["coverage"] == pytest.approx(0.5)                      # 2 von 4 Layern
    assert sc["layers"] == {"L1": 4, "L3": 2}
    # neuster Beleg je Layer gewinnt: aelterer L1=1 wird von neuerem L1=4 ersetzt
    _ev(con, "Trend A", "L1", 1, datum="2026-01-01")
    sc2 = trends.trend_score(con, _cfg(), trends._trend_id(con, "Trend A"))
    assert sc2["layers"]["L1"] == 4


def test_cfs_ohne_beleg_na(tmp_path):
    con = _db(tmp_path)
    _import_trends(con, tmp_path)
    sc = trends.trend_score(con, _cfg(), trends._trend_id(con, "Trend B"))
    assert sc["cfs"] != sc["cfs"] and sc["stufe"] == "n/a" and sc["coverage"] == 0.0


def test_konvergenz_mindestens_2_layer_mit_score_ge_3(tmp_path):
    con = _db(tmp_path)
    _import_trends(con, tmp_path)
    tid = trends._trend_id(con, "Trend A")
    _ev(con, "Trend A", "L1", 4)
    _ev(con, "Trend A", "L4", 3)
    sc = trends.trend_score(con, _cfg(), tid)
    assert sc["konvergent"] is True and sc["layer_mit_score"] == 2
    _ev(con, "Trend A", "L4", 1, datum="2026-12-01")   # L4 faellt auf 1 -> nur noch L1 stark
    sc2 = trends.trend_score(con, _cfg(), tid)
    assert sc2["konvergent"] is False and sc2["layer_mit_score"] == 1


def test_divergenzwarnung_l2_niedrig_bei_hohem_l1_und_umgekehrt(tmp_path):
    con = _db(tmp_path)
    _import_trends(con, tmp_path)
    tid = trends._trend_id(con, "Trend A")
    _ev(con, "Trend A", "L1", 4)
    _ev(con, "Trend A", "L2", 1)
    sc = trends.trend_score(con, _cfg(), tid)
    assert "Divergenz" in sc["divergenz"] and "L2 niedrig" in sc["divergenz"]
    # umgekehrt: L2 hoch, L1 niedrig
    _ev(con, "Trend A", "L1", 1, datum="2026-12-01")
    _ev(con, "Trend A", "L2", 4, datum="2026-12-02")
    sc2 = trends.trend_score(con, _cfg(), tid)
    assert "Divergenz" in sc2["divergenz"] and "L2 hoch" in sc2["divergenz"]
    # kein Konflikt: L1 hoch, L2 hoch -> keine Warnung
    _ev(con, "Trend A", "L2", 4, datum="2026-12-03")
    _ev(con, "Trend A", "L1", 4, datum="2026-12-04")
    sc3 = trends.trend_score(con, _cfg(), tid)
    assert sc3["divergenz"] == ""


# --------------------------------------------------------------------------
# Ampel + overlay
# --------------------------------------------------------------------------

def test_ampel_regeln(tmp_path):
    cfg = _cfg()
    assert trends.ampel([], cfg) == "n/a"
    assert trends.ampel([(-2, "handeln")], cfg) == "rot"
    assert trends.ampel([(-1, "vertiefen")], cfg) == "rot"      # exposure <= -1 + vertiefen
    assert trends.ampel([(-2, "monitor")], cfg) == "gelb"        # monitor -> gelb
    assert trends.ampel([(+2, "monitor")], cfg) == "gelb"
    assert trends.ampel([(+2, "handeln")], cfg) == "grün"        # positiver Exposure -> nie rot
    assert trends.ampel([(-1, "monitor"), (+1, "beobachten")], cfg) == "gelb"
    assert trends.ampel([(-1, "beobachten")], cfg) == "grün"


def test_overlay_schreibt_csv(tmp_path):
    con = _db(tmp_path)
    _import_trends(con, tmp_path)
    tid_a = trends._trend_id(con, "Trend A")
    _ev(con, "Trend A", "L1", 4)
    _ev(con, "Trend A", "L2", 4)      # CFS = 4.0 -> handeln
    p = tmp_path / "ex.csv"
    p.write_text("ticker,trend,exposure,begruendung,quelle_url,datum,erfasst_von,geprueft\n"
                 "AAA,Trend A,-2,KI frisst Marge,https://example.com/x,2026-11-01,manuell,nein\n"
                 "AAA,Trend B,+1,diversifiziert,https://example.com/y,2026-11-01,manuell,ja\n",
                 encoding="utf-8")
    trends.import_exposure(con, _cfg(), str(p))
    watch = tmp_path / "w.csv"
    watch.write_text("ticker\nAAA\nBBB\n", encoding="utf-8")
    out_csv = tmp_path / "trend_overlay.csv"
    rows = trends.overlay(_cfg(), str(watch), db_path=tmp_path / "t.db", out_path=str(out_csv))
    by = {(r["ticker"], r["trend"]): r for r in rows}
    assert by[("AAA", "Trend A")]["ampel"] == "rot"          # exp -2 + stufe handeln
    assert by[("AAA", "Trend A")]["stufe"] == "handeln"
    assert by[("AAA", "Trend A")]["cfs"] == "4.00"
    assert by[("AAA", "Trend B")]["ampel"] == "rot"          # Ticker-Ampel gilt fuer alle Zeilen
    assert by[("BBB", "")]["ampel"] == "n/a"                 # ohne Exposure
    text = out_csv.read_text(encoding="utf-8")
    assert text.splitlines()[0] == "ticker,trend,exposure,cfs,stufe,ampel"
    assert "AAA,Trend A,-2,4.00,handeln,rot" in text
