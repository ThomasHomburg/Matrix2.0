# v75: Aufnahme (Ingest) des Point-in-time-Fundamentalspeichers pit.db.
# Nur Aufnahme: companyfacts laden -> Whitelist-Filter -> Aenderungsprotokoll
# -> Roh-JSON verwerfen bzw. loeschen (Standard), KEIN Adapter, KEIN Backtest.
#
# Aufruf:
#   python tools/v75_pit_ingest.py --baue-universe
#       Universe (aktuelle 503 + Wikipedia-Historie ab 2013) aufbauen,
#       CIK-Aufloesung mit Konfidenz, Stichproben-/Klaerungs-CSV.
#   SEC_USER_AGENT="Name name@example.com" \
#       python tools/v75_pit_ingest.py --limit 10
#       Aufnahme der naechsten 10 CIKs (Resume: gelaufene CIKs werden
#       uebersprungen). Optionen: --limit N, --start-after CIK10,
#       --keep-raw (Roh-JSON behalten), --db data/pit.db.
#
# Ergebnisse/Metriken: logs/v75_universe.json, logs/v75_*.csv (nicht
# committen); pit.db in data/ (.gitignore-Eintrag).

import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "tools")

from data.pit_schema import (
    ALLE_TAGS, DURATION_FAMILIEN, INSTANT_FAMILIEN, PIT_DB_PFAD,
    ZUGELASSENE_FORMEN, ensure_schema,
)

TICKERS_CSV = "tickers_sp500.csv"
UA_ENV = "SEC_USER_AGENT"
CACHE_DIR = "logs/pit_cache"
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik10}.json"
COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
TICKERS_JSON = "https://www.sec.gov/files/company_tickers.json"
BROWSE_EDGAR = ("https://www.sec.gov/cgi-bin/browse-edgar?"
                "action=getcompany&CIK={query}&type=10-K&dateb=&owner=include"
                "&count=10")
FTS_SUCHE = "https://efts.sec.gov/LATEST/search-index?q={q}&forms=10-K"
UNIVERSE_JSON = "logs/v75_universe.json"
STICHPROBE_CSV = "logs/v75_zuordnung_stichprobe.csv"
KLAERUNG_CSV = "logs/v75_zuordnung_klaerung.csv"
WIEDER_CSV = "logs/v75_wieder_ticker.csv"
RATE_LIMIT_S = 0.2  # 5 Anfragen/s gegen data.sec.gov


def user_agent() -> str:
    ua = os.environ.get(UA_ENV, "")
    if not ua or "@" not in ua:
        raise RuntimeError(
            "Umgebungsvariable %s fehlt oder ohne E-Mail (Beispiel: "
            "'Name name@example.com')" % UA_ENV)
    return ua


def _get_json(url: str, timeout: int = 120) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Universe: aktuelle S&P-500-Ticker + Wikipedia-Entfernte, CIK-Aufloesung
# ---------------------------------------------------------------------------

_RECHTSFORM = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|"
    r"lp|nv|ag|sa|holdings|holding|group|the)\b", re.I)


def _norm_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^\w\s]", " ", name)
    name = _RECHTSFORM.sub(" ", name)
    return re.sub(r"\s+", " ", name).strip()


def _load_sp500() -> list:
    out = []
    with open(TICKERS_CSV, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            t = (row.get("ticker") or "").strip().upper()
            if t:
                out.append({"ticker": t,
                            "name": row.get("name", "") or "",
                            "index_quelle": "sp500_aktuell"})
    return out


def cik_karte() -> dict:
    """ticker -> (name, cik10) aus company_tickers.json (aktive Firmen)."""
    data = _get_json(TICKERS_JSON)
    karte = {}
    for eintrag in data.values():
        karte[eintrag["ticker"].upper()] = (
            eintrag["title"], str(eintrag["cik_str"]).zfill(10))
    return karte


def _browse_edgar_cik(ticker: str):
    for versuch in range(3):
        url = BROWSE_EDGAR.format(query=urllib.parse.quote(ticker))
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": user_agent()})
            with urllib.request.urlopen(req, timeout=60) as r:
                html = r.read().decode("utf-8", "replace")
            m = re.search(r"CIK=(\d{10})", html)
            if m:
                return "browse_edgar", m.group(1)
            return None, None
        except Exception as e:
            print("  browse-edgar %s (Versuch %d): %s"
                  % (ticker, versuch + 1, e))
            time.sleep(1.5 * (versuch + 1))
    return None, None


def _fts_treffer(abfrage: str, limit_hits: int = 10,
                 startdt: str = "", enddt: str = ""):
    """EDGAR Full-Text-Suche; liefert [(display_name, cik10, tickers)].
    Wichtig: hits[0]['_id'] beginnt mit der ACCSSION-Nummer (deren Praefix
    ist die CIK des EINREICHENDEN Agenten, NICHT der Firma!) -- deshalb
    Auswertung von _source.display_names im Format
    'FIRMENNAME  (TICKER, ...)  (CIK 0001234567)'."""
    url = FTS_SUCHE.format(q=urllib.parse.quote(abfrage))
    if startdt and enddt:
        url += "&dateRange=custom&startdt=%s&enddt=%s" % (startdt, enddt)
    fts = None
    for versuch in range(3):
        try:
            fts = _get_json(url, timeout=60)
            break
        except Exception as e:
            if versuch == 2:
                print("  edgar-fts %s: %s" % (abfrage, e))
            time.sleep(1.5 * (versuch + 1))
    if fts is None:
        return []
    out = []
    for h in fts.get("hits", {}).get("hits", [])[:limit_hits]:
        for disp in h.get("_source", {}).get("display_names", []):
            m = re.search(r"CIK\s*(\d{10})", disp)
            if not m:
                continue
            tickers = re.findall(r"\(([A-Z0-9,\- ]+)\)\s*\(CIK", disp)
            ticker_liste = []
            for gruppe in tickers:
                ticker_liste.extend(x.strip().split() for x in [gruppe.replace(",", " ")])
            ticker_liste = sorted({t for gruppe in ticker_liste for t in gruppe})
            name = re.sub(r"\s*\(.*", "", disp).strip()
            out.append((name, m.group(1), ticker_liste))
    time.sleep(0.25)
    return out


def _browse_edgar_firmensuche(firmenname: str):
    """Offizielle EDGAR-Firmensuche (company=...): Liste (name, cik10)."""
    url = ("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&"
           "company=" + urllib.parse.quote(firmenname)
           + "&type=10-K&dateb=&owner=include&count=40")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": user_agent()})
        with urllib.request.urlopen(req, timeout=60) as r:
            html = r.read().decode("utf-8", "replace")
    except Exception as e:
        print("  firmensuche %s: %s" % (firmenname, e))
        return []
    if "No matching" in html:
        return []
    out = []
    for cik, name in re.findall(
            r'CIK=(\d{10})[^>]*>([^<]+)<', html):
        if name.strip() and name.strip() != cik:
            out.append((name.strip(), cik))
    return out


def cik_fuer(ticker: str, firmenname: str, karte: dict, ist_aktuell: bool,
             entfernt_am: str = ""):
    """(konfidenz, quelle, cik10, sec_name). Konfidenz:
    exakt  = aktiver Ticker aus company_tickers ODER FTS-Treffer, bei dem
             Ticker-Symbol im display_names steht und der Name passt,
    name   = eindeutiger Treffer ueber FTS-Namensphrase oder EDGAR-Firmensuche,
    unsicher = Rest (Klaerungsliste, kein Backtest).
    Fuer historische (Wikipedia-)Ticker -- Reihenfolge gegen Merger-Nachfolge-
    CIKs und Ticker-Wiederverwendung:
    (a) FTS exakte Namensphrase, (b) FTS Ticker im Zeitfenster um das
    Entfernungsdatum (+/-), (c) EDGAR-Firmensuche, (d) Rueckgrat
    company_tickers (Nachfolge, konfidenz name)."""
    if ist_aktuell:
        if ticker in karte:
            sec_name, cik = karte[ticker]
            if _norm_name(sec_name) == _norm_name(firmenname) or not firmenname:
                return "exakt", "company_tickers", cik, sec_name
            return "name", "company_tickers_name", cik, sec_name
    else:
        startdt, enddt = "", ""
        if entfernt_am:
            try:
                jahr = int(entfernt_am[:4])
                startdt = "%d-01-01" % (jahr - 4)
                enddt = "%d-12-31" % (jahr + 1)
            except ValueError:
                pass
        # (a) FTS exakte Namensphrase
        if firmenname:
            kandidaten = {}
            for name, cik, tickers in _fts_treffer('"%s"' % firmenname,
                                                   startdt=startdt,
                                                   enddt=enddt):
                if (_norm_name(name) == _norm_name(firmenname)
                        or _norm_name(firmenname) in _norm_name(name)):
                    kandidaten[cik] = name
            if len(kandidaten) == 1:
                cik, name = next(iter(kandidaten.items()))
                return "exakt" if firmenname.lower() in name.lower() \
                    else "name", "edgar_fts_name", cik, name
        # (b) FTS Ticker im Zeitfenster, display-ticker-Match bevorzugt
        treffer = _fts_treffer(ticker, startdt=startdt, enddt=enddt)
        mit_ticker = sorted({(name, cik) for name, cik, tickers in treffer
                             if ticker in tickers})
        if len(mit_ticker) == 1:
            name, cik = mit_ticker[0]
            return "exakt", "edgar_fts_ticker", cik, name
        # (c) EDGAR-Firmensuche (offizielle company-Liste)
        if firmenname:
            beste = {}
            for name, cik in _browse_edgar_firmensuche(firmenname):
                if _norm_name(firmenname) in _norm_name(name) \
                        or _norm_name(name).startswith(_norm_name(firmenname)):
                    beste[cik] = name
            if len(beste) == 1:
                cik, name = next(iter(beste.items()))
                return "name", "edgar_firmensuche", cik, name
        # (d) Rueckgrat company_tickers (Nachfolge-Firma)
        if ticker in karte:
            sec_name, cik = karte[ticker]
            return "name", "company_tickers_name", cik, sec_name
    quelle, cik = _browse_edgar_cik(ticker)
    if cik:
        return "name", quelle, cik, ""
    return "unsicher", "kein_cik", None, ""


def _wiki_zeilen() -> list:
    from v75_wiki_history import wiki_entfernungen
    return wiki_entfernungen()


def baue_universe(stichprobe_n: int = 30) -> dict:
    """Universe aufbauen: aktuelle 503 + Wikipedia-Entfernte ab 2013.
    Schreibt UNIVERSE_JSON, STICHPROBE_CSV, KLAERUNG_CSV. Gibt das
    Universe-Dict zurueck."""
    eintraege = _load_sp500()
    gesehene = {}
    for e in eintraege:
        gesehene.setdefault(e["ticker"], e)
    eingefuegt = {}
    for z in _wiki_zeilen():
        t = (z.get("removed") or "").upper().strip()
        n = z.get("removed_name") or ""
        at = (z.get("added") or "").upper().strip()
        an = z.get("added_name") or ""
        d = z["datum"].isoformat()
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,5}", at or ""):
            if at not in eingefuegt or eingefuegt[at]["von"] > d:
                eingefuegt[at] = {"von": d, "name": an}
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,5}", t or ""):
            if t in gesehene:
                # Ticker ist heute wieder drin: historische Zeile
                gesehene[t]["historisch_removed"] = d
                gesehene[t]["historisch_name"] = n
            else:
                gesehene[t] = {"ticker": t, "name": n,
                               "index_quelle": "wikipedia",
                               "removed_am": d}
    tickers = sorted(gesehene)
    print("Universe-Ticker gesamt: %d" % len(tickers))
    karte = cik_karte()
    ergebnisse = []
    for t in tickers:
        e = gesehene[t]
        konf, quelle, cik, sec_name = cik_fuer(
            t, e.get("name", ""), karte,
            ist_aktuell=(e["index_quelle"] == "sp500_aktuell"),
            entfernt_am=e.get("removed_am", ""))
        cik_aktuell = karte.get(t, ("", ""))[1] if t in karte \
            else None
        ergebnisse.append({"ticker": t, "name": e.get("name", ""),
                           "cik10": cik, "konfidenz": konf,
                           "quelle": quelle, "sec_name": sec_name,
                           "index_quelle": e["index_quelle"],
                           "cik_wiederverwendung": (
                               cik_aktuell if (cik_aktuell and cik
                                               and cik_aktuell != cik)
                               else None)})
    klaerung = [z for z in ergebnisse if z["konfidenz"] == "unsicher"]
    with open(UNIVERSE_JSON, "w", encoding="utf-8") as fh:
        json.dump({"tickers": ergebnisse}, fh, ensure_ascii=True, indent=1)
    felder = ["ticker", "name", "cik10", "sec_name", "konfidenz"]
    with open(STICHPROBE_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=felder)
        w.writeheader()
        random.seed(42)
        for z in random.sample(ergebnisse,
                                min(stichprobe_n, len(ergebnisse))):
            w.writerow({k: z.get(k) or "" for k in felder})
    with open(KLAERUNG_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=felder + ["quelle"])
        w.writeheader()
        for z in klaerung:
            w.writerow({k: z.get(k) or "" for k in felder + ["quelle"]})
    print("Konfidenz: exakt=%d name=%d unsicher=%d"
          % (sum(1 for z in ergebnisse if z["konfidenz"] == "exakt"),
             sum(1 for z in ergebnisse if z["konfidenz"] == "name"),
             sum(1 for z in ergebnisse if z["konfidenz"] == "unsicher")))
    print("Stichprobe: %s | Klaerung: %s (%d Zeilen)"
          % (STICHPROBE_CSV, KLAERUNG_CSV, len(klaerung)))
    # pit_member: Add/Remove-Ereignisse aus Wikipedia + aktuellem Index
    # und Wieder-Ticker-CSV (gleicher Ticker -> 2 CIK, von/bis).
    con = sqlite3.connect(PIT_DB_PFAD)
    ensure_schema(con)
    con.execute("DELETE FROM pit_member")
    by_ticker = {}
    for z in ergebnisse:
        by_ticker.setdefault(z["ticker"], []).append(z)
    for tt, gruppe in sorted(by_ticker.items()):
        ciks = sorted({z["cik10"] for z in gruppe if z.get("cik10")})
        if len(ciks) > 1:
            print("Wieder-Ticker %s: CIKs %s" % (tt, ciks))
        for z in gruppe:
            if not z.get("cik10"):
                continue
            von = eingefuegt.get(tt, {}).get("von", "")
            bis = z.get("removed_am", "")
            con.execute(
                "INSERT OR IGNORE INTO pit_member VALUES (?,?,?,?,?,?)",
                (z["cik10"], tt, von or "2000-01-01", bis or None,
                 z["index_quelle"], z["konfidenz"]))
    con.commit()
    con.close()
    with open(WIEDER_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "cik10_liste", "konfidenz_liste"])
        for tt, gruppe in sorted(by_ticker.items()):
            ciks = sorted({z["cik10"] for z in gruppe if z.get("cik10")})
            wieder = [z.get("cik_wiederverwendung") for z in gruppe
                      if z.get("cik_wiederverwendung")]
            alle = sorted(set(ciks) | set(wieder))
            if len(alle) > 1:
                w.writerow([tt, ";".join(alle),
                           ";".join(z["konfidenz"] for z in gruppe
                                    if z.get("cik10"))])
    return {"tickers": ergebnisse}


def lade_universe() -> list:
    if not os.path.exists(UNIVERSE_JSON):
        raise SystemExit("Universe fehlt: erst --baue-universe laufen lassen")
    with open(UNIVERSE_JSON, encoding="utf-8") as fh:
        return json.load(fh)["tickers"]


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _familie_von_tag() -> dict:
    m = {}
    for fam, tags in DURATION_FAMILIEN.items():
        for tag, p in tags:
            m.setdefault(tag, (fam, p, False))
    for fam, tags in INSTANT_FAMILIEN.items():
        for tag, p in tags:
            m.setdefault(tag, (fam, p, True))
    return m


_TAG_FAMILIE = _familie_von_tag()


def _entries(container: dict, namespace: str, tag: str):
    """Fakteintraege eines Tags ( USD oder shares) plus Unit;
    funktioniert mit Cache-Wrapper {'fetched_at','facts': ...} wie mit
    roher API-Antwort {'facts': {ns: ...}}."""
    root = container.get("facts", {})
    inner = root.get("facts", root)
    tag_node = inner.get(namespace, {}).get(tag, {})
    units = tag_node.get("units", {})
    for unit in ("USD", "shares"):
        if unit in units:
            return units[unit], unit
    return [], None


def facts_aus_companyfacts(payload: dict, cik10: str):
    """Extrahiert Tupel (cik10, tag, unit, period_start, period_end,
    filed_date, form, accn, value) gemaess Whititelist + Form-Filter.
    Verworfen-Zaehler nach Grund (kein stiller Verlust)."""
    out = []
    verworfen = {"falsche_form": 0, "kein_wert": 0, "kein_unit": 0}
    for tag in ALLE_TAGS:
        fam, prio, instant = _TAG_FAMILIE[tag]
        namespace = "dei" if fam == "shares_outstanding" else "us-gaap"
        eintraege, unit = _entries(payload, namespace, tag)
        if unit is None:
            verworfen["kein_unit"] += 1
            continue
        for e in eintraege:
            form = e.get("form", "")
            if form not in ZUGELASSENE_FORMEN:
                verworfen["falsche_form"] += 1
                continue
            wert = e.get("val")
            pe = (e.get("end") or "")[:10]
            filed = (e.get("filed") or "")[:10]
            if wert is None or not pe or not filed:
                verworfen["kein_wert"] += 1
                continue
            ps = None if instant else ((e.get("start") or "")[:10] or None)
            out.append((cik10, tag, unit, ps, pe, filed, form,
                        e.get("accn", ""), float(wert)))
    return out, verworfen


def schreibe_aenderungsprotokoll(con: sqlite3.Connection, fakten: list) -> int:
    """Aenderungsprotokoll: Fassungen je (cik10, tag, unit, period_start,
    period_end) nach (filed_date, accn) sortiert; die erste Fassung wird
    immer gespeichert, weitere nur bei exakter Rohwert-Veraenderung
    (keine Toleranz). Identische Refilings werden nicht gespeichert.
    Gibt die Anzahl neu eingefuegter Zeilen zurueck."""
    neu = 0
    inserts = []
    letzte = {}
    for f in sorted(fakten, key=lambda x: (x[1], x[2], x[3] or "",
                                           x[4], x[5], x[7])):
        schluessel = (f[1], f[2], f[3] or "", f[4])
        alt = letzte.get(schluessel)
        if alt is None or alt != f[8]:
            inserts.append(f)
        letzte[schluessel] = f[8]
    for f in inserts:
        cur = con.execute(
            "INSERT OR IGNORE INTO pit_facts VALUES (?,?,?,?,?,?,?,?,?)", f)
        neu += max(cur.rowcount, 0)
    return neu


def verarbeite_cik(con: sqlite3.Connection, cik10: str, quelle: str,
                   keep_raw: bool = False) -> dict:
    """Laedt companyfacts fuer einen CIK (Cache vor Ort), schreibt das
    Aenderungsprotokoll, ergaenzt pit_entity (submissions) und vermerkt den
    Lauf in ingest_runs (Resume). Roh-JSON wird nach Erfolg geloescht,
    ausser bei keep_raw. Fehler werden je CIK abgefangen (kein Abbruch des
    Batches, kein ingest_runs-Eintrag -> Retry beim naechsten Lauf)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    pfad = os.path.join(CACHE_DIR, "CIK%s.json" % cik10)
    bereits = con.execute(
        "SELECT 1 FROM ingest_runs WHERE cik10 = ? AND quelle = ?",
        (cik10, quelle)).fetchone()
    if bereits:
        return {"status": "schon_gelaufen", "neu": 0}
    payload = None
    von_cache = False
    if os.path.exists(pfad):
        with open(pfad, encoding="utf-8") as fh:
            payload = json.load(fh)
        von_cache = True
    if payload is None:
        time.sleep(RATE_LIMIT_S)
        try:
            payload = _get_json(COMPANYFACTS.format(cik10=cik10))
            with open(pfad, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
        except Exception as e:
            return {"status": "fehler:%s" % type(e).__name__, "neu": 0,
                    "grund": str(e)[:120]}
    fakten, verworfen = facts_aus_companyfacts(payload, cik10)
    con.execute("BEGIN")
    neu = schreibe_aenderungsprotokoll(con, fakten)
    # pit_entity aus submissions (SIC, Zeitraeume, aktueller Ticker)
    time.sleep(RATE_LIMIT_S)
    try:
        subs = _get_json(SUBMISSIONS.format(cik10=cik10))
        tic = subs.get("tickers") or [""]
        con.execute(
            "INSERT OR REPLACE INTO pit_entity VALUES (?,?,?,?,?,?,?,?)",
            (cik10, tic[0] if tic else "",
             subs.get("name", ""),
             str(subs.get("sic", "")),
             subs.get("sicDescription", ""),
             subs.get("filings", {}).get("recent", {}).get(
                 "filingDate", [""])[0] if subs.get("filings", {}).get(
                 "recent", {}).get("filingDate") else "",
             subs.get("filings", {}).get("recent", {}).get(
                 "filingDate", [""])[-1] if subs.get("filings", {}).get(
                 "recent", {}).get("filingDate") else "",
             "submissions"))
    except Exception as e:
        print("  submissions %s fehlgeschlagen: %s" % (cik10, e))
    con.execute("INSERT OR REPLACE INTO ingest_runs VALUES (?,?,?,?)",
                (cik10, quelle,
                 dt.datetime.now().replace(microsecond=0).isoformat(), neu))
    con.commit()
    if not keep_raw and not von_cache:
        os.remove(pfad)
    elif not keep_raw and von_cache:
        os.remove(pfad)
    return {"status": "ok", "neu": neu, "verworfen": verworfen,
            "fakten": len(fakten)}


def _rss_max_mb() -> float:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
    except Exception:
        return float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baue-universe", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start-after", default="")
    ap.add_argument("--keep-raw", action="store_true")
    ap.add_argument("--db", default=PIT_DB_PFAD)
    args = ap.parse_args()

    if args.baue_universe:
        baue_universe()
        return 0

    universe = lade_universe()
    mit_cik = [z for z in universe if z.get("cik10")]
    offene = []
    con = sqlite3.connect(args.db)
    ensure_schema(con)
    for z in mit_cik:
        if not con.execute(
                "SELECT 1 FROM ingest_runs WHERE cik10=? AND quelle=?",
                (z["cik10"], z["ticker"])).fetchone():
            offene.append(z)
    if args.start_after:
        ciks = [z["cik10"] for z in offene]
        if args.start_after in ciks:
            offene = offene[ciks.index(args.start_after) + 1:]
    if args.limit > 0:
        offene = offene[:args.limit]
    print("Aufzunehmende CIKs (offen): %d; geschaetzte Laufzeit "
          "bei 5 req/s und ca. 5 s je CIK: ca. %.0f Minuten"
          % (len(offene), len(offene) * 5.0 / 60.0))
    t0 = time.time()
    letzter = ""
    fehler = []
    fertig = 0
    for z in offene:
        letzter = z["cik10"]
        info = verarbeite_cik(con, z["cik10"], z["ticker"],
                              keep_raw=args.keep_raw)
        print("  %s (%s): %s" % (z["cik10"], z["ticker"], info["status"]))
        if info["status"] == "ok":
            fertig += 1
            print("    neu=%d fakten=%s verworfen=%s rss_max=%.0f MB"
                  % (info["neu"], info["fakten"], info["verworfen"],
                     _rss_max_mb()))
        elif info["status"].startswith("fehler"):
            fehler.append((z["ticker"], info.get("grund", "")))
    if fehler:
        print("Fehler je CIK (%d), werden beim naechsten Lauf erneut "
              "versucht:" % len(fehler))
        for t, g in fehler:
            print("  %s: %s" % (t, g))
    dauer = time.time() - t0
    zeilen = con.execute("SELECT COUNT(*) FROM pit_facts").fetchone()[0]
    groesse = os.path.getsize(args.db) / 1e6
    print("Fertig: %d Zeilen, %.1f MB, %.0f s, RSS max %.0f MB, "
          "letzter CIK %s" % (zeilen, groesse, dauer, _rss_max_mb(), letzter))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
