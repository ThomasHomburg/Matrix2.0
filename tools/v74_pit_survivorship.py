# v74 Diagnose Teil E (Survivorship), read-only, kein Produktionscode.
# Zweck: (1) Wikipedia-Aenderungstabelle abrufen (Seite "Historical components
#        of the S&P 500", Tabelle id="changes") und 15 Ticker extrahieren,
#        die 2015-2024 aus dem S&P 500 entfernt wurden.
#        (2) Test, ob yfinance fuer diese Ticker Kurse liefert.
#        (3) CIK-Aufloesung + SEC companyfacts fuer diese Ticker,
#            Anteil rekonstruierbarer historischer Mitglieder.
# Aufruf: python tools/v74_pit_survivorship.py [--out logs/v74_survivorship.csv]
# Ergebnisse landen in logs/ (nicht committen).

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

WIKI_RAW = ("https://en.wikipedia.org/w/index.php?"
            "title=Historical_components_of_the_S%26P_500&action=raw")
TICKERS_JSON_CANDIDATES = [
    "https://www.sec.gov/files/company_tickers.json",
    "https://www.sec.gov/files/company_tickers_exchange.json",
]
COMPANYFACTS = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json"
BROWSE_EDGAR = ("https://www.sec.gov/cgi-bin/browse-edgar?"
                "action=getcompany&CIK={query}&type=10-K&dateb=&owner=include&count=10")
FTS_SUCHE = ("https://efts.sec.gov/LATEST/search-index?q={q}&forms=10-K")
WINDOW = (dt.date(2015, 1, 1), dt.date(2024, 12, 31))
N_TICKER = 15
INSTANT_FAM = {"equity"}
KENN_FAMILIEN = ["revenue", "ebit", "ocf", "capex", "equity"]
FAMILIEN = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet"],
    "ebit": ["OperatingIncomeLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "equity": ["StockholdersEquity"],
}
# SEC file cache wie in v72 (logs/sec_cache/), hier read-only wiederverwenden
CACHE_DIR = "logs/sec_cache"


def _ua():
    ua = os.environ.get("SEC_USER_AGENT", "")
    if not ua:
        raise RuntimeError("Umgebungsvariable SEC_USER_AGENT fehlt "
                           "(z. B. 'Name name@example.com')")
    return ua


def _get(url, binary=False, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": _ua()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}


def _name(zelle):
    """[[Firmenname]] -> Klartext"""
    zelle = re.sub(r"\[\[([^\]|]*)\|[^\]]*\]\]", r"\1", zelle)
    zelle = re.sub(r"\[\[([^\]]*)\]\]", r"\1", zelle)
    return zelle.strip()


def _ist_zelle(zeile):
    # Zellen beginnen mit '|' (aber nicht mit '|-' oder '|}')
    return zeile.startswith("|") and not zeile.startswith(("|-", "|}"))


def _datum(text):
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text.strip())
    if not m or m.group(1) not in _MONTHS:
        return None
    return dt.date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))


def wiki_entfernungen():
    """Parst die Wikitext-Aenderungstabelle (Zeilen mit '|-', Zellen mit '|'),
    gibt Liste von Dicts (datum, added_ticker, removed_ticker) zurueck."""
    text = _get(WIKI_RAW)
    tab = text[text.index('id="changes"'):]
    out = []
    zellen = []
    for roh in tab.splitlines():
        zeile = roh.strip()
        if zeile.startswith("{|") or zeile.startswith("|}") or not zeile:
            continue
        if zeile == "|-":
            # Zellenordnung: Datum, AddTick, AddName, RemTick, RemName, Reason, Refs
            if len(zellen) >= 4:
                d = _datum(zellen[0])
                if d is not None:
                    out.append({"datum": d,
                                "added": zellen[1].strip(),
                                "removed": zellen[3].strip(),
                                "removed_name": _name(zellen[4]) if
                                len(zellen) > 4 else ""})
            zellen = []
            continue
        if zeile.startswith("!"):
            continue
        if _ist_zelle(zeile):
            # Inline-Zellen mit '||' innerhalb der Zeile
            zellen.extend(c.strip() for c in zeile.lstrip("|").split("||"))
        elif zellen:
            # Fortsetzungszeile einer mehrzeiligen Zelle (Refs/Reason)
            zellen[-1] += " " + zeile
    return out


def _cache_pfad(cik10):
    return os.path.join(CACHE_DIR, "CIK%s.json" % cik10)


def cik_aufloesung(ticker, ticker_map, firmenname=""):
    """1) SEC company_tickers-Dateien (nur aktive Firmen),
    2) browse-edgar per Ticker-Symbol, 3) EDGAR Full-Text-Suche per Name."""
    for name, cik in ticker_map.get(ticker, []):
        return name, cik
    for versuch in range(3):
        try:
            html = _get(BROWSE_EDGAR.format(query=urllib.parse.quote(ticker)))
            m = re.search(r"CIK=(\d{10})", html)
            if m:
                return "edgar_browse", m.group(1)
            break
        except Exception as e:
            print("  Hinweis browse-edgar %s (Versuch %d): %s"
                  % (ticker, versuch + 1, e))
            time.sleep(2.0 * (versuch + 1))
    if firmenname:
        try:
            fts = json.loads(_get(FTS_SUCHE.format(
                q=urllib.parse.quote('"%s"' % firmenname))))
            hits = fts.get("hits", {}).get("hits", [])
            if hits:
                cik = hits[0]["_id"].split("-")[0].zfill(10)
                return "edgar_fts_name", cik
        except Exception as e:
            print("  Hinweis edgar-fts %s: %s" % (ticker, e))
    return "kein_cik", None


def _navigate(root):
    # Cache-Wrapper {"fetched_at","facts":{...}} oder rohe API-Antwort
    fakten = root.get("facts", {})
    if "facts" in fakten:
        fakten = fakten["facts"]
    return fakten


def fy_zaehlen(payload):
    """Anzahl FY-Perioden je Familie aus companyfacts. Duration-Familien:
    Dauer 340-380 Tage, Form 10-K; Instant-Familien (equity): nur end-Datum,
    Form 10-K. Gezaehlt werden EINDEUTIGE Perioden (end-Datum), nicht Fakten
    (ein periodenend taucht in mehreren Filings erneut auf)."""
    fakten = _navigate(payload)
    usgaap = fakten.get("us-gaap", {})
    counts = {}
    for fam, tags in FAMILIEN.items():
        enden = set()
        instant = fam in INSTANT_FAM
        for tag in tags:
            eintraege = usgaap.get(tag, {}).get("units", {}).get("USD", [])
            for e in eintraege:
                if "end" not in e:
                    continue
                if "10-K" not in e.get("form", ""):
                    continue
                if not instant:
                    if "start" not in e:
                        continue
                    try:
                        d0 = dt.date.fromisoformat(e["start"])
                        d1 = dt.date.fromisoformat(e["end"])
                    except Exception:
                        continue
                    if not (340 <= (d1 - d0).days <= 380):
                        continue
                enden.add(e["end"])
        counts[fam] = len(enden)
    return counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="logs/v74_survivorship.csv")
    args = ap.parse_args()

    # Ticker-Maps laden (beide SEC-Dateien, read-only, gecacht in logs/)
    ticker_map = {}
    for url in TICKERS_JSON_CANDIDATES:
        try:
            data = json.loads(_get(url))
        except Exception as e:
            print("Hinweis: %s nicht abrufbar (%s)" % (url, e))
            continue
        if isinstance(data, dict) and all(isinstance(v, dict)
                                          for v in data.values()):
            for eintrag in data.values():  # company_tickers.json
                cik = str(eintrag["cik_str"]).zfill(10)
                ticker_map.setdefault(eintrag["ticker"], []).append(
                    (eintrag["title"], cik))
        else:  # Listenformat (cik,name,ticker,exchange; 0=kein Delisting)
            for row in data:
                if isinstance(row, dict):
                    cik = str(row.get("cik_str", "")).zfill(10)
                    t = row.get("ticker", "")
                    name = row.get("title", "")
                else:
                    if len(row) < 4 or not row[3]:
                        continue
                    cik = str(row[0]).zfill(10)
                    t, name = row[2], row[1]
                if cik and t:
                    ticker_map.setdefault(t, []).append((name, cik))

    aenderungen = [a for a in wiki_entfernungen()
                   if WINDOW[0] <= a["datum"] <= WINDOW[1]]
    entfernt = []
    gesehene = set()
    for a in sorted(aenderungen, key=lambda x: x["datum"], reverse=True):
        t = a["removed"].upper().strip()
        if not t or "ticker" in t.lower() or t in gesehene:
            continue
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,5}", t):
            gesehene.add(t)
            entfernt.append({"ticker": t,
                             "entfernt_am": a["datum"].isoformat(),
                             "removed_name": a.get("removed_name", "")})
    print("Entfernungen 2015-2024 gefunden: %d" % len(entfernt))
    auswahl = entfernt[:N_TICKER]
    print("Auswahl (neueste %d, deterministisch): %s"
          % (N_TICKER, ", ".join(x["ticker"] for x in auswahl)))

    zeilen = []
    import yfinance as yf
    for eintrag in auswahl:
        t = eintrag["ticker"]
        rss0 = None
        try:
            import resource
            rss0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        except Exception:
            pass
        # (2) yfinance-Kurse
        try:
            hist = yf.Ticker(t).history(start="2012-01-01", end=None,
                                        auto_adjust=False, interval="1mo")
            kurse = "ja" if len(hist) >= 12 else ("duenn" if len(hist) else "nein")
            n_kurse = len(hist)
        except Exception as e:
            kurse, n_kurse = "fehler:%s" % type(e).__name__, 0
        # (3) CIK + companyfacts
        quelle, cik = cik_aufloesung(t, ticker_map,
                                    eintrag.get("removed_name", ""))
        fy = {fam: None for fam in KENN_FAMILIEN}
        sec_fehler = ""
        if cik:
            pfad = _cache_pfad(cik)
            payload = None
            if os.path.exists(pfad):
                try:
                    payload = json.load(open(pfad, encoding="utf-8"))["facts"]
                except Exception:
                    payload = None
            if payload is None:
                try:
                    payload = json.loads(_get(COMPANYFACTS.format(cik10=cik)))
                    # Roh-JSON nicht dauerhaft halten (Diagnose read-only:
                    # kein neuer Cache-Eintrag in sec_cache)
                except Exception as e:
                    payload = {"fehler": str(e)}
                    sec_fehler = str(e)[:60]
            if "fehler" not in payload:
                fy = fy_zaehlen(payload)
            time.sleep(0.25)
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0
        except Exception:
            rss = None
        zeilen.append(dict(eintrag, map_quelle=quelle, cik=cik,
                           kurse=kurse, monate=n_kurse,
                           sec_fehler=sec_fehler,
                           **{("fy_" + f): v for f, v in fy.items()},
                           rss_mb_max=rss))
        print("  %-6s entfernt=%s kurse=%-8s cik=%s fy=%s"
              % (t, eintrag["entfernt_am"], kurse, cik, fy))

    # Anteil rekonstruierbar: Kurse UND >=8 FY in allen Kernfamilien
    voll = [z for z in zeilen
            if z["kurse"] == "ja"
            and all(z.get("fy_" + f) and z["fy_" + f] >= 8 for f in KENN_FAMILIEN)]
    teil = [z for z in zeilen if z not in voll and z["kurse"] != "nein"]

    felder = ["ticker", "entfernt_am", "removed_name", "map_quelle", "cik",
              "kurse", "monate", "sec_fehler"] \
        + ["fy_" + f for f in KENN_FAMILIEN] + ["rss_mb_max"]
    with open(args.out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=felder)
        w.writeheader()
        w.writerows(zeilen)

    print("\nZusammenfassung E:")
    print("  Entfernungen im Fenster gesamt: %d" % len(entfernt))
    print("  Auswahl getestet: %d" % len(zeilen))
    if zeilen:
        print("  mit yfinance-Kursen: %d (%.0f%%)"
              % (sum(1 for z in zeilen if z["kurse"] == "ja"),
                 100.0 * sum(1 for z in zeilen if z["kurse"] == "ja") / len(zeilen)))
        print("  mit SEC-CIK: %d" % sum(1 for z in zeilen if z["cik"]))
        print("  voll rekonstruierbar (Kurse + >=8 FY je Kernfamilie): %d/%d"
              % (len(voll), len(zeilen)))
        print("  nur teilweise (Kurse, aber FY-Luecken): %d" % len(teil))
    print("  CSV: %s" % args.out)


if __name__ == "__main__":
    sys.exit(main())
