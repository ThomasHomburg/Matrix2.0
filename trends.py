"""Trend-Layer Teil A (trends.py) -- Struktur, Quellenregister, Bewertung.

Multi-Layer-Foresight-Framework (Masterarbeit): vier Layer
  L1 Wissenschaft/Technologie, L2 Markt/Kapital, L3 Regulierung/Politik,
  L4 Nachfrage/Cross-Industry.
Konvergenzprinzip: mind. `trends.konvergenz_min_layer` unabhaengige Layer mit
Score >= `trends.konvergenz_min_score`. Divergenzwarnung: L2 niedrig bei hohem
L1/L3 oder umgekehrt. CFS je Trend = gewichteter Mittelwert der neuesten Belege
je Layer (Layer ohne Beleg = n/a, Gewichte renormalisieren, Abdeckung ausweisen).
Trend-Ampel je Ticker regelbasiert aus exposure x CFS-Stufe (config trends.ampel_regeln).

Kein Datenabruf aus dem Netz in Teil A -- nur Struktur/Import/Bewertung.

Aufrufe:
    python trends.py import-trends trends_start.csv
    python trends.py import-evidence evidence.csv      # quelle_url Pflicht, atomar
    python trends.py import-exposure exposure.csv      # quelle_url Pflicht, atomar
    python trends.py score
    python trends.py overlay --watch ticker_watch_v40.csv --out trend_overlay.csv
    python trends.py show TICKER | --trend NAME

Import-Regel: kein Eintrag ohne quelle_url -- Validierung aller Zeilen vorab,
bei Fehler Abbruch mit Zeilennummer, nichts wird importiert (atomar).
"""

from __future__ import annotations

import argparse
import csv
import logging
import sqlite3
import sys
from datetime import date
from pathlib import Path

import yaml

from config import load_config
from db_schema import ensure_schema

logger = logging.getLogger("value_screener.trends")

DB_PATH = Path("value_tracker.db")
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
SOURCES_YAML = Path(__file__).resolve().parent / "sources.yaml"

LAYERS = ("L1", "L2", "L3", "L4")
ERFASST_VON = ("manuell", "regel", "llm_vorschlag")
RICHTUNGEN = ("positiv", "negativ", "gemischt")
TREND_STATUS = ("aktiv", "beobachten", "archiviert")

# Pflichtspalten je Tabelle -- Guard/Migration wie bei smart_money (DROP bei
# veralteter Form, danach RuntimeError wenn schema.sql nicht passt)
TRENDS_TABLES = {
    "sources": ("id", "name", "layer", "vorlauf_monate", "zugang", "kosten",
                "aktualisierung", "zuverlaessigkeit", "lizenz"),
    "trends": ("id", "name", "typ", "richtung", "horizont", "status"),
    "trend_evidence": ("id", "trend_id", "layer", "score", "beleg_text", "quelle_id",
                       "quelle_url", "beleg_datum", "abruf_datum", "erfasst_von"),
    "trend_exposure": ("id", "ticker", "trend_id", "exposure", "begruendung",
                       "quelle_url", "datum", "erfasst_von", "geprueft"),
}


def _tab_cols(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def connect(db_path: Path = DB_PATH, cfg: dict | None = None) -> sqlite3.Connection:
    """DB oeffnen + zentraler Schema-Guard (v57 ensure_schema): fehlende Tabellen
    werden angelegt, fehlende Spalten per ALTER ergaenzt -- vorhandene Daten
    bleiben erhalten (kein DROP mehr). Danach PRAGMA-Assert -- fehlt danach eine
    Spalte, ist schema.sql veraltet -> sofort RuntimeError (kein Teil-Lauf).
    Anschliessend Quellenregister aus sources.yaml seeden, falls Tabelle leer."""
    con = sqlite3.connect(db_path)
    ensure_schema(con, schema_path=SCHEMA_PATH)   # v57: ALTER statt DROP, Daten bleiben erhalten
    for table, cols in TRENDS_TABLES.items():
        missing = set(cols) - _tab_cols(con, table)
        if missing:
            raise RuntimeError(
                f"trends: Tabelle {table} hat nicht das aktuelle Schema "
                f"(fehlende Spalten: {', '.join(sorted(missing))}). schema.sql ist veraltet: "
                f"{SCHEMA_PATH} -- schema.sql aktualisieren und Lauf wiederholen.")
    con.row_factory = sqlite3.Row
    seed_sources(con, cfg or load_config())
    return con


def seed_sources(con: sqlite3.Connection, cfg: dict) -> int:
    """Quellenregister aus sources.yaml in die Tabelle sources seeden (nur wenn
    leer; INSERT OR IGNORE ueber UNIQUE(name) -- idempotent)."""
    n = con.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
    if n:
        return 0
    path = Path(cfg.get("trends", {}).get("sources_yaml") or "sources.yaml")
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.exists():
        logger.warning("trends: sources.yaml nicht gefunden: %s", path)
        return 0
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    n = 0
    for s in data.get("sources", []):
        con.execute(
            """INSERT OR IGNORE INTO sources
               (name, layer, vorlauf_monate, zugang, kosten, aktualisierung,
                zuverlaessigkeit, lizenz) VALUES (?,?,?,?,?,?,?,?)""",
            (str(s.get("name", "")), str(s.get("layer", "")),
             float(s.get("vorlauf_monate") or 0.0), str(s.get("zugang", "")),
             str(s.get("kosten", "")), str(s.get("aktualisierung", "")),
             str(s.get("zuverlaessigkeit", "")), str(s.get("lizenz", ""))))
        n += 1
    con.commit()
    return n


# --------------------------------------------------------------------------
# Imports (atomar, quelle_url Pflicht, Fehler mit Zeilennummer)
# --------------------------------------------------------------------------

def _read_csv(path: str) -> list[tuple[int, dict]]:
    """CSV einlesen: [(Dateizeile inkl. Header, row)]; #-Kommentarzeilen ignoriert."""
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for zeile, row in enumerate(csv.DictReader(f), start=2):
            if (row.get("name") or row.get("trend") or row.get("ticker") or "").startswith("#"):
                continue
            out.append((zeile, row))
    return out


def _trend_id(con: sqlite3.Connection, name: str) -> int | None:
    r = con.execute("SELECT id FROM trends WHERE name=?", (name.strip(),)).fetchone()
    return r["id"] if r else None


def import_trends(con: sqlite3.Connection, csv_path: str) -> int:
    """trends-Import (name, typ, richtung, horizont, status). Upsert by name.
    Atomar: alle Zeilen validieren, bei Fehler Abbruch mit Zeilennummer."""
    fehler: list[str] = []
    rows = []
    for zeile, r in _read_csv(csv_path):
        name = (r.get("name") or "").strip()
        if not name:
            fehler.append(f"Zeile {zeile}: name fehlt")
            continue
        richtung = (r.get("richtung") or "gemischt").strip()
        status = (r.get("status") or "aktiv").strip()
        if richtung not in RICHTUNGEN:
            fehler.append(f"Zeile {zeile} ({name}): richtung '{richtung}' nicht in {RICHTUNGEN}")
        if status not in TREND_STATUS:
            fehler.append(f"Zeile {zeile} ({name}): status '{status}' nicht in {TREND_STATUS}")
        rows.append((name, (r.get("typ") or "").strip(), richtung,
                     (r.get("horizont") or "").strip(), status))
    if fehler:
        raise SystemExit("Abbruch import-trends -- nichts importiert:\n  " + "\n  ".join(fehler))
    for name, typ, richtung, horizont, status in rows:
        con.execute(
            """INSERT INTO trends (name, typ, richtung, horizont, status) VALUES (?,?,?,?,?)
               ON CONFLICT(name) DO UPDATE SET typ=excluded.typ, richtung=excluded.richtung,
                 horizont=excluded.horizont, status=excluded.status""",
            (name, typ, richtung, horizont, status))
    con.commit()
    return len(rows)


def import_evidence(con: sqlite3.Connection, cfg: dict, csv_path: str) -> int:
    """Evidence-Import (trend, layer, score, beleg_text, quelle_id, quelle_url,
    beleg_datum, abruf_datum, erfasst_von). KEIN Eintrag ohne quelle_url.
    Atomar: alle Zeilen validieren, bei Fehler Abbruch mit Zeilennummer."""
    erlaubt = set(cfg.get("trends", {}).get("erfasst_von_erlaubt", ERFASST_VON))
    fehler: list[str] = []
    rows = []
    for zeile, r in _read_csv(csv_path):
        trend = (r.get("trend") or "").strip()
        layer = (r.get("layer") or "").strip().upper()
        quelle_url = (r.get("quelle_url") or "").strip()
        tid = _trend_id(con, trend) if trend else None
        if not trend or tid is None:
            fehler.append(f"Zeile {zeile}: trend '{trend}' unbekannt (zuerst import-trends)")
            continue
        if layer not in LAYERS:
            fehler.append(f"Zeile {zeile} ({trend}): layer '{layer}' nicht in {LAYERS}")
            continue
        try:
            score = int(float((r.get("score") or "").strip()))
        except ValueError:
            fehler.append(f"Zeile {zeile} ({trend}): score fehlt/ungueltig")
            continue
        if not (1 <= score <= 4):
            fehler.append(f"Zeile {zeile} ({trend}): score {score} nicht in 1..4")
            continue
        if not quelle_url:
            fehler.append(f"Zeile {zeile} ({trend}): quelle_url fehlt -- kein Eintrag ohne Quelle")
            continue
        ev = (r.get("erfasst_von") or "manuell").strip()
        if ev not in erlaubt:
            fehler.append(f"Zeile {zeile} ({trend}): erfasst_von '{ev}' nicht in {sorted(erlaubt)}")
            continue
        qid = (r.get("quelle_id") or "").strip()
        qid = int(qid) if qid.isdigit() else None
        rows.append((tid, layer, score, (r.get("beleg_text") or "").strip(), qid, quelle_url,
                     (r.get("beleg_datum") or "").strip(), (r.get("abruf_datum") or "").strip()
                     or date.today().isoformat(), ev))
    if fehler:
        raise SystemExit("Abbruch import-evidence -- nichts importiert (quelle_url ist Pflicht):\n  "
                         + "\n  ".join(fehler))
    for row in rows:
        con.execute(
            """INSERT INTO trend_evidence
               (trend_id, layer, score, beleg_text, quelle_id, quelle_url, beleg_datum,
                abruf_datum, erfasst_von) VALUES (?,?,?,?,?,?,?,?,?)""", row)
    con.commit()
    return len(rows)


def import_exposure(con: sqlite3.Connection, cfg: dict, csv_path: str) -> int:
    """Exposure-Import (ticker, trend, exposure, begruendung, quelle_url, datum,
    erfasst_von, geprueft). KEIN Eintrag ohne quelle_url; exposure -2..+2.
    Upsert by (ticker, trend_id). Atomar mit Zeilennummer."""
    erlaubt = set(cfg.get("trends", {}).get("erfasst_von_erlaubt", ERFASST_VON))
    fehler: list[str] = []
    rows = []
    for zeile, r in _read_csv(csv_path):
        ticker = (r.get("ticker") or "").strip().upper()
        trend = (r.get("trend") or "").strip()
        quelle_url = (r.get("quelle_url") or "").strip()
        tid = _trend_id(con, trend) if trend else None
        if not ticker:
            fehler.append(f"Zeile {zeile}: ticker fehlt")
            continue
        if not trend or tid is None:
            fehler.append(f"Zeile {zeile}: trend '{trend}' unbekannt (zuerst import-trends)")
            continue
        try:
            exp = int(float((r.get("exposure") or "").strip()))
        except ValueError:
            fehler.append(f"Zeile {zeile} ({ticker}/{trend}): exposure fehlt/ungueltig")
            continue
        if not (-2 <= exp <= 2):
            fehler.append(f"Zeile {zeile} ({ticker}/{trend}): exposure {exp} nicht in -2..+2")
            continue
        if not quelle_url:
            fehler.append(f"Zeile {zeile} ({ticker}/{trend}): quelle_url fehlt -- kein Eintrag ohne Quelle")
            continue
        ev = (r.get("erfasst_von") or "manuell").strip()
        if ev not in erlaubt:
            fehler.append(f"Zeile {zeile} ({ticker}/{trend}): erfasst_von '{ev}' nicht in {sorted(erlaubt)}")
            continue
        gepr = (r.get("geprueft") or "nein").strip()
        if gepr not in ("ja", "nein"):
            fehler.append(f"Zeile {zeile} ({ticker}/{trend}): geprueft '{gepr}' nicht in (ja, nein)")
            continue
        rows.append((ticker, tid, exp, (r.get("begruendung") or "").strip(), quelle_url,
                     (r.get("datum") or "").strip() or date.today().isoformat(), ev, gepr))
    if fehler:
        raise SystemExit("Abbruch import-exposure -- nichts importiert (quelle_url ist Pflicht):\n  "
                         + "\n  ".join(fehler))
    for row in rows:
        con.execute(
            """INSERT INTO trend_exposure
               (ticker, trend_id, exposure, begruendung, quelle_url, datum, erfasst_von, geprueft)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(ticker, trend_id) DO UPDATE SET exposure=excluded.exposure,
                 begruendung=excluded.begruendung, quelle_url=excluded.quelle_url,
                 datum=excluded.datum, erfasst_von=excluded.erfasst_von, geprueft=excluded.geprueft""",
            row)
    con.commit()
    return len(rows)


# --------------------------------------------------------------------------
# Bewertung: CFS je Trend, Konvergenz, Divergenz, Ampel
# --------------------------------------------------------------------------

def latest_evidence_per_layer(con: sqlite3.Connection, trend_id: int) -> dict[str, dict]:
    """Neuester Beleg je Layer (Sortierung nach beleg_datum/abruf_datum DESC --
    erster Treffer je Layer ist der neueste). Layer ohne Beleg fehlen (n/a)."""
    rows = con.execute(
        "SELECT * FROM trend_evidence WHERE trend_id=? "
        "ORDER BY COALESCE(beleg_datum, abruf_datum, '') DESC, id DESC", (trend_id,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(d["layer"], d)
    return out


def stufe_fuer_cfs(cfs: float, cfg: dict) -> str:
    """CFS-Stufe aus config trends.cfs_stufen (Bereiche [lo, hi], Reihenfolge wie im YAML)."""
    if cfs != cfs:
        return "n/a"
    for name, bereich in (cfg.get("trends", {}).get("cfs_stufen") or {}).items():
        lo, hi = float(bereich[0]), float(bereich[1])
        if lo <= cfs <= hi:
            return name
    return "n/a"


def compute_cfs(ev: dict[str, dict], cfg: dict) -> dict:
    """CFS je Trend aus den neuesten Belegen je Layer: gewichteter Mittelwert der
    Layer-Scores mit trends.layer_gewichte. Layer ohne Beleg = n/a -> Gewichte
    renormalisieren; Abdeckung (Anteil Layer mit Beleg an 4) wird ausgewiesen."""
    gew = {L: float((cfg.get("trends", {}).get("layer_gewichte") or {}).get(L, 0.0)) for L in LAYERS}
    used = {L: r for L, r in ev.items() if L in LAYERS}
    coverage = len(used) / len(LAYERS)
    if not used or sum(gew[L] for L in used) <= 0:
        return {"cfs": float("nan"), "stufe": "n/a", "coverage": coverage,
                "layers": {L: int(used[L]["score"]) for L in used} if used else {}}
    wsum = sum(gew[L] for L in used)
    cfs = sum(gew[L] * int(used[L]["score"]) for L in used) / wsum
    return {"cfs": cfs, "stufe": stufe_fuer_cfs(cfs, cfg), "coverage": coverage,
            "layers": {L: int(used[L]["score"]) for L in used}}


def konvergenz(ev: dict[str, dict], cfg: dict) -> dict:
    """Konvergenzprinzip: mind. konvergenz_min_layer unabhaengige Layer mit
    Score >= konvergenz_min_score."""
    t = cfg.get("trends", {})
    min_layer = int(t.get("konvergenz_min_layer", 2))
    min_score = int(t.get("konvergenz_min_score", 3))
    strong = [L for L, r in ev.items() if int(r["score"]) >= min_score]
    return {"konvergent": len(strong) >= min_layer, "layer_mit_score": len(strong),
            "layer_strong": sorted(strong), "min_layer": min_layer, "min_score": min_score}


def divergenz_warnung(ev: dict[str, dict], cfg: dict) -> str:
    """Divergenzwarnung: L2 niedrig (<= 2) bei hohem (>= konvergenz_min_score) L1
    oder L3 -- oder umgekehrt (L2 hoch, L1/L3 niedrig). Beide Seiten muessen
    Belege haben. Sonst leerer String."""
    ms = int(cfg.get("trends", {}).get("konvergenz_min_score", 3))

    def s(L):
        return int(ev[L]["score"]) if L in ev else None
    l1, l2, l3 = s("L1"), s("L2"), s("L3")
    if l2 is not None and l2 <= 2 and ((l1 is not None and l1 >= ms) or (l3 is not None and l3 >= ms)):
        return f"Divergenz: L1/L3 hoch (>= {ms}), L2 niedrig ({l2})"
    if l2 is not None and l2 >= ms and ((l1 is not None and l1 <= 2) or (l3 is not None and l3 <= 2)):
        return f"Divergenz: L2 hoch ({l2}), L1/L3 niedrig"
    return ""


def trend_score(con: sqlite3.Connection, cfg: dict, trend_id: int) -> dict:
    """Gesamtbewertung eines Trends: neueste Belege je Layer -> CFS + Stufe +
    Abdeckung + Konvergenz + Divergenzwarnung."""
    ev = latest_evidence_per_layer(con, trend_id)
    c = compute_cfs(ev, cfg)
    k = konvergenz(ev, cfg)
    return {"trend_id": trend_id, "layers": c["layers"], "cfs": c["cfs"], "stufe": c["stufe"],
            "coverage": c["coverage"], "konvergent": k["konvergent"],
            "layer_mit_score": k["layer_mit_score"], "layer_strong": k["layer_strong"],
            "divergenz": divergenz_warnung(ev, cfg), "evidence": ev}


def ampel(exposures: list[tuple[int, str]], cfg: dict) -> str:
    """Trend-Ampel je Ticker aus exposure x CFS-Stufe (config trends.ampel_regeln):
    rot, wenn ein Trend mit exposure <= rot_exposure_max die Stufe rot_stufen
    (vertiefen/handeln) erreicht; gelb bei Stufe gelb_stufen (monitor);
    gruen sonst; n/a ohne Exposure."""
    if not exposures:
        return "n/a"
    ar = cfg.get("trends", {}).get("ampel_regeln", {})
    rot_max = int(ar.get("rot_exposure_max", -1))
    rot_st = set(ar.get("rot_stufen", ["vertiefen", "handeln"]))
    gelb_st = set(ar.get("gelb_stufen", ["monitor"]))
    for exp, stufe in exposures:
        if exp <= rot_max and stufe in rot_st:
            return "rot"
    for _exp, stufe in exposures:
        if stufe in gelb_st:
            return "gelb"
    return "grün"


def ticker_ampel(con: sqlite3.Connection, cfg: dict, ticker: str) -> tuple[str, list[dict]]:
    """Ampel + Exposure-Details je Ticker: [(exposure, stufe, trend_id, trend_name)]."""
    rows = con.execute(
        """SELECT e.trend_id, e.exposure, t.name FROM trend_exposure e
           JOIN trends t ON t.id=e.trend_id WHERE e.ticker=? ORDER BY t.id""",
        (ticker.upper(),)).fetchall()
    exps = []
    details = []
    for r in rows:
        sc = trend_score(con, cfg, r["trend_id"])
        exps.append((int(r["exposure"]), sc["stufe"]))
        details.append({"trend_id": r["trend_id"], "trend": r["name"],
                        "exposure": int(r["exposure"]), "cfs": sc["cfs"], "stufe": sc["stufe"]})
    return ampel(exps, cfg), details


def _load_watch(path: str | None) -> list[str]:
    p = Path(path) if path else Path("ticker_watch_v40.csv")
    if not p.exists():
        return []
    out = []
    with open(p, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = (r.get("ticker") or "").strip().lstrip("\ufeff").upper()
            if t and not t.startswith("#") and t not in out:
                out.append(t)
    return out


def overlay(cfg: dict, watch_csv: str | None, db_path: Path = DB_PATH,
            out_path: str = "trend_overlay.csv") -> list[dict]:
    """Overlay Watchlist x Trends -> CSV (long format): je Ticker mit Exposure eine
    Zeile pro Trend (ticker, trend, exposure, cfs, stufe, ampel); Ticker ohne
    Exposure eine Zeile mit ampel=n/a. Schreibt out_path (UTF-8)."""
    con = connect(db_path, cfg)
    watch = _load_watch(watch_csv)
    out = []
    for t in watch:
        a, details = ticker_ampel(con, cfg, t)
        if not details:
            out.append({"ticker": t, "trend": "", "exposure": "", "cfs": "", "stufe": "", "ampel": a})
        for d in details:
            out.append({"ticker": t, "trend": d["trend"], "exposure": d["exposure"],
                        "cfs": f"{d['cfs']:.2f}" if d["cfs"] == d["cfs"] else "n/a",
                        "stufe": d["stufe"], "ampel": a})
    cols = ["ticker", "trend", "exposure", "cfs", "stufe", "ampel"]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in out:
            w.writerow([r.get(c, "") for c in cols])
    logger.info("trend_overlay gespeichert: %s (%d Zeilen)", out_path, len(out))
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_import_trends(args) -> None:
    con = connect(Path(args.db))
    n = import_trends(con, args.csv_file)
    print(f"{n} Trend(s) importiert/aktualisiert (aus {args.csv_file}).")


def cmd_import_evidence(args) -> None:
    cfg = load_config()
    con = connect(Path(args.db), cfg)
    n = import_evidence(con, cfg, args.csv_file)
    print(f"{n} Evidence-Zeile(n) importiert (aus {args.csv_file}; quelle_url war Pflicht).")


def cmd_import_exposure(args) -> None:
    cfg = load_config()
    con = connect(Path(args.db), cfg)
    n = import_exposure(con, cfg, args.csv_file)
    print(f"{n} Exposure-Zeile(n) importiert/aktualisiert (aus {args.csv_file}; quelle_url war Pflicht).")


def cmd_score(args) -> None:
    cfg = load_config()
    con = connect(Path(args.db), cfg)
    trends = con.execute("SELECT id, name FROM trends ORDER BY id").fetchall()
    print(f"{'id':>3} {'trend':<46} {'CFS':>5} {'stufe':<11} {'deck':>5} {'L1':>3} {'L2':>3} "
          f"{'L3':>3} {'L4':>3}  konv  divergenz")
    for t in trends:
        sc = trend_score(con, cfg, t["id"])
        lay = sc["layers"]
        cfs = f"{sc['cfs']:.2f}" if sc["cfs"] == sc["cfs"] else "n/a"
        print(f"{t['id']:>3} {t['name'][:46]:<46} {cfs:>5} {sc['stufe']:<11} {sc['coverage']:>5.0%} "
              f"{lay.get('L1', '-'):>3} {lay.get('L2', '-'):>3} {lay.get('L3', '-'):>3} {lay.get('L4', '-'):>3}  "
              f"{'ja' if sc['konvergent'] else 'nein':>4}  {sc['divergenz']}")


def cmd_overlay(args) -> None:
    cfg = load_config()
    out = overlay(cfg, args.watch, db_path=Path(args.db),
                  out_path=args.out or cfg.get("trends", {}).get("overlay_csv", "trend_overlay.csv"))
    ampeln = {}
    for r in out:
        ampeln.setdefault(r["ticker"], r["ampel"])
    print(f"\n=== trend_overlay ({len(out)} Zeilen) -> {args.out} ===")
    for t, a in ampeln.items():
        print(f"  {t:<8} ampel={a}")


def cmd_show(args) -> None:
    cfg = load_config()
    con = connect(Path(args.db), cfg)
    if args.trend:
        r = con.execute("SELECT * FROM trends WHERE name=?", (args.trend,)).fetchone()
        if not r:
            raise SystemExit(f"Trend unbekannt: {args.trend}")
        sc = trend_score(con, cfg, r["id"])
        print(f"=== Trend {r['name']} (id={r['id']}, typ={r['typ']}, richtung={r['richtung']}, "
              f"horizont={r['horizont']}, status={r['status']}) ===")
        print(f"CFS={sc['cfs']:.2f} stufe={sc['stufe']} abdeckung={sc['coverage']:.0%} "
              f"konvergent={'ja' if sc['konvergent'] else 'nein'} ({sc['layer_mit_score']} Layer >= "
              f"{sc['layer_strong']}) divergenz={sc['divergenz'] or '-'}")
        for L in LAYERS:
            ev = sc["evidence"].get(L)
            if ev:
                print(f"  {L} score={ev['score']} beleg={ev['beleg_text'][:60]} "
                      f"quelle={ev['quelle_url'][:60]} datum={ev['beleg_datum'] or ev['abruf_datum']}")
            else:
                print(f"  {L} n/a (kein Beleg)")
        n = con.execute("SELECT COUNT(*) AS n FROM trend_exposure WHERE trend_id=?",
                        (r["id"],)).fetchone()["n"]
        print(f"Exposures: {n}")
        return
    if not args.ticker:
        raise SystemExit("show: ticker oder --trend angeben")
    a, details = ticker_ampel(con, cfg, args.ticker)
    print(f"=== Ticker {args.ticker.upper()} ampel={a} ({len(details)} Exposure(s)) ===")
    for d in details:
        print(f"  {d['trend']:<46} exposure={d['exposure']:+d} CFS="
              f"{f'{d['cfs']:.2f}' if d['cfs'] == d['cfs'] else 'n/a'} stufe={d['stufe']}")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Trend-Layer Teil A (Struktur, Quellenregister, Bewertung)")
    p.add_argument("--db", default=str(DB_PATH), help="SQLite-Datei (Default: value_tracker.db)")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("import-trends", "Trends aus CSV importieren (Upsert by name)"),
                           ("import-evidence", "Evidence aus CSV (quelle_url Pflicht, atomar)"),
                           ("import-exposure", "Exposure aus CSV (quelle_url Pflicht, atomar)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("csv_file")
        sp.set_defaults(func={"import-trends": cmd_import_trends,
                              "import-evidence": cmd_import_evidence,
                              "import-exposure": cmd_import_exposure}[name])
    p_s = sub.add_parser("score", help="CFS/Stufe/Konvergenz/Divergenz je Trend")
    p_s.set_defaults(func=cmd_score)
    p_o = sub.add_parser("overlay", help="Watchlist x Trends -> trend_overlay.csv")
    p_o.add_argument("--watch", default="ticker_watch_v40.csv")
    p_o.add_argument("--out", default=None, help="Default: config trends.overlay_csv")
    p_o.set_defaults(func=cmd_overlay)
    p_sh = sub.add_parser("show", help="Ticker (Ampel/Exposure) oder --trend NAME")
    p_sh.add_argument("ticker", nargs="?", default=None)
    p_sh.add_argument("--trend", default=None)
    p_sh.set_defaults(func=cmd_show)
    return p.parse_args(argv)


def main() -> None:
    args = _parse_args(sys.argv[1:])
    args.func(args)


if __name__ == "__main__":
    main()
