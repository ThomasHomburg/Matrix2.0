r"""v74 Diagnose A/B/C: Point-in-time-Fundamentals aus SEC companyfacts.

Zweck (read-only, kein Produktionscode): Machbarkeitspruefung eines
Point-in-time-Fundamentalspeichers fuer einen Backtest 2012-2025.

Teil A (Abdeckung): je Ticker und Kennzahl-Familie die Anzahl der
FY-Perioden (Dauer 340-380 Tage, Form 10-K/10-K/A) je Tag der Hierarchie
samt fruehester/spaetester Periode; Familien mit Tag-Wechsel und
Lueckenjahren werden markiert.
Teil B (Restatements): Anteil der Fakten mit mehreren filed-Fassungen
fuer dieselbe Periode (gleicher Tag, gleiches start/end) und drei
Beispiele mit Abweichung > 5 % (Look-ahead-Fehler, wenn nur die neueste
Fassung genutzt wird).
Teil C (Filing-Verzug): filed - period_end (Tage) fuer 10-K und 10-Q,
Median / 90. Perzentil.

Aufruf:
    export SEC_USER_AGENT="Vorname Nachname <mail@example.com>"
    python tools/v74_pit_abdeckung.py                     # alle Ticker
    python tools/v74_pit_abdeckung.py --start 0 --end 170  # Slice (Laufzeit!)
    python tools/v74_pit_abdeckung.py --out logs/v74_abdeckung.csv

Roh-JSON wird je Ticker geladen und danach verworfen; RSS wird je Ticker
protokolliert (Spitzenwert darf nicht linear mit n wachsen). Ergebnisse
werden als CSV (Default logs/, in .gitignore) und Konsolensummary
ausgegeben; NICHT committen.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import resource
import statistics
import sys
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, ".")
import os  # noqa: E402

os.environ.setdefault("SEC_USER_AGENT", "Value-Screener-Diagnose t09homburg@gmail.com")

from data.sec import user_agent, _INTEREST_TAGS  # noqa: E402

logger = logging.getLogger("v74.pit")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

TICKERS_CSV = "tickers_sp500.csv"
CACHE_DIR = Path("logs/sec_cache")
CIK_MAP_JSON = Path("logs/v74_cik_map.json")

# Kennzahl-Familien: Hierarchie (erste vorhandene gewinnt je Periode).
FAMILIEN = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                "SalesRevenueNet"],
    "ebit": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuing"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "sbc": ["ShareBasedCompensation",
            "ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensation"],
    "interest": list(_INTEREST_TAGS),
    "tax": ["IncomeTaxExpenseBenefit"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"],
    "equity": ["StockholdersEquity"],
    "debt": ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount",
             "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "shares_outstanding": ["dei:EntityCommonStockSharesOutstanding"],
}

FY_DUR = (340, 380)
# Bestandsgroessen (Instant-Fakten: nur end, kein start) -> je end-Datum
INSTANT_FAM = {"equity", "debt", "cash", "shares_outstanding"}


def _load_tickers() -> list[str]:
    with open(TICKERS_CSV, encoding="utf-8") as f:
        return [r["ticker"].strip().upper() for r in csv.DictReader(f)
                if (r.get("ticker") or "").strip()]


def _cik_map() -> dict[str, str]:
    """Ticker -> CIK (einmaliger Abruf von company_tickers.json, gecacht)."""
    if CIK_MAP_JSON.exists():
        return json.loads(CIK_MAP_JSON.read_text(encoding="utf-8"))
    req = urllib.request.Request(
        "https://www.sec.gov/files/company_tickers.json",
        headers={"User-Agent": user_agent()})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    m = {e["ticker"].upper(): str(e["cik_str"]).zfill(10) for e in data.values()}
    CIK_MAP_JSON.parent.mkdir(exist_ok=True, parents=True)
    CIK_MAP_JSON.write_text(json.dumps(m), encoding="utf-8")
    return m


def _d(v: str):
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _entries(container: dict, tag: str) -> list[dict]:
    """container = geladene Datei (Cache ODER rohe API-Antwort). Die Cache-
    Datei hat {'fetched_at', 'facts': {cik, entityName, 'facts': {us-gaap,
    dei}}}; die API-Antwort direkt {'facts': {us-gaap, dei}, ...}."""
    root = container.get("facts", {})
    inner = root.get("facts", root)        # bei Cache: inner = us-gaap/dei-Mappe
    if tag.startswith("dei:"):
        units = inner.get("dei", {}).get(tag.split(":", 1)[1], {})
    else:
        units = inner.get("us-gaap", {}).get(tag, {})
    out = []
    for unit, items in (units.get("units") or {}).items():
        if unit != "USD" and not tag.startswith("dei:"):
            continue  # Nicht-USD ignorieren (wie v72)
        for u in items or []:
            out.append(u)
    return out


def analysiere_ticker(ticker: str, cik: str, out_rows: list,
                      restatement_stats: Counter, beispiele: list,
                      lag_10k: list, lag_10q: list, rss_spitzen: list):
    path = CACHE_DIR / f"CIK{cik}.json"
    if not path.exists():
        out_rows.append({"ticker": ticker, "familie": "", "tag": "",
                         "fy_n": None, "erste": "", "letzte": "", "fehler":
                         "kein_cache"})
        return
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Cache-Datei und API liefern beide ein Toplevel-Objekt mit Schluessel
    # "facts" (die Cache-Datei hat zusaetzlich "fetched_at" daneben).
    rss_spitzen.append((ticker, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
    for fam, tags in FAMILIEN.items():
        jahre_je_tag = {}
        instant = fam in INSTANT_FAM
        for tag in tags:
            jahre_je_tag[tag] = set()
            erste, letzte = None, None
            n = 0
            for u in _entries(raw, tag):
                if u.get("form") not in ("10-K", "10-K/A"):
                    continue
                end = _d(u.get("end"))
                if not end:
                    continue
                if instant:
                    if u.get("start"):
                        continue          # Instant-Familie: nur end-Fakten
                    jahre_je_tag[tag].add(end.year)
                    n += 1
                else:
                    start = _d(u.get("start"))
                    if not start:
                        continue
                    dur = (end - start).days
                    if not (FY_DUR[0] <= dur <= FY_DUR[1]):
                        continue
                    jahre_je_tag[tag].add(end.year)
                    n += 1
                if erste is None or end < erste:
                    erste = end
                if letzte is None or end > letzte:
                    letzte = end
            if n:
                out_rows.append({
                    "ticker": ticker, "familie": fam, "tag": tag, "fy_n": n,
                    "erste": str(erste), "letzte": str(letzte), "fehler": "",
                })
        # Tag-Wechsel mit Luecke: Familien-Jahre je Tag vs. Vereinigungsmenge
        vereinigt = set()
        for j in jahre_je_tag.values():
            vereinigt |= j
        if not vereinigt:
            continue
        sortiert = sorted(vereinigt)
        luecken = [b - a - 1 for a, b in zip(sortiert, sortiert[1:]) if b - a > 1]
        if luecken:
            def _jahre_diff(menge):
                return [b for a, b in zip(sorted(menge), sorted(menge)[1:]) if b - a > 1]
            lueckj = _jahre_diff({j for j in vereinigt})
            if lueckj:
                out_rows.append({"ticker": ticker, "familie": fam, "tag":
                                 "(luecke)", "fy_n": len(lueckj), "erste":
                                 str(lueckj[0]), "letzte": str(lueckj[-1]),
                                 "fehler": "luecke_jahre:" +
                                 ",".join(str(y) for y in lueckj[:12])})
    # --- B) Restatements (alle Familien-Tags, auch 10-Q) ---
    for fam, tags in FAMILIEN.items():
        for tag in tags:
            perioden = defaultdict(list)
            for u in _entries(raw, tag):
                if not u.get("filed"):
                    continue
                key = (u.get("start"), u.get("end"))
                if u.get("start") and u.get("end"):
                    perioden[key].append((u["filed"], u.get("val"),
                                          u.get("form")))
            for key, fass in perioden.items():
                restatement_stats["perioden"] += 1
                if len({f[0] for f in fass}) > 1:
                    restatement_stats["refiled"] += 1   # Comparative-Refiling (i. d. R. gleich)
                vals = {f[1] for f in fass if f[1] is not None}
                if len(vals) > 1:
                    restatement_stats["restated"] += 1   # Wert geaendert = echter Look-ahead
                    vals = [f[1] for f in fass if f[1] is not None]
                    if len(vals) >= 2:
                        alt, neu = sorted(fass)[0][1], sorted(fass)[-1][1]
                        if (alt is not None and neu is not None and alt != 0
                                and abs(neu - alt) / abs(alt) > 0.05
                                and len(beispiele) < 6):
                            beispiele.append({
                                "ticker": ticker, "familie": fam, "tag": tag,
                                "periode": f"{key[0]}..{key[1]}",
                                "alt": alt, "neu": neu, "abweichung_pct":
                                round(abs(neu - alt) / abs(alt) * 100, 1)})
    # --- C) Filing-Verzug ---
    for fam, tags in FAMILIEN.items():
        seen = set()
        for tag in tags:
            for u in _entries(raw, tag):
                end, filed = _d(u.get("end")), _d(u.get("filed"))
                if not end or not filed:
                    continue
                k = (tag, u.get("start"), u.get("end"), u.get("form"))
                if k in seen:
                    continue
                seen.add(k)
                delta = (filed - end).days
                if u.get("form") in ("10-K", "10-K/A"):
                    lag_10k.append(delta)
                elif u.get("form") in ("10-Q", "10-Q/A"):
                    lag_10q.append(delta)
    del raw


def main() -> int:
    ap = argparse.ArgumentParser(description="v74 Pit-Diagnose A/B/C")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--out", default="logs/v74_abdeckung.csv")
    args = ap.parse_args()
    tickers = _load_tickers()
    end = min(args.end if args.end is not None else len(tickers), len(tickers))
    cmap = _cik_map()
    out_rows = []
    restatement_stats = Counter()
    beispiele = []
    lag_10k, lag_10q = [], []
    rss_spitzen = []
    for t in tickers[args.start:end]:
        cik = cmap.get(t)
        if not cik:
            out_rows.append({"ticker": t, "familie": "", "tag": "", "fy_n":
                             None, "erste": "", "letzte": "", "fehler":
                             "kein_cik"})
            continue
        try:
            analysiere_ticker(t, cik, out_rows, restatement_stats, beispiele,
                              lag_10k, lag_10q, rss_spitzen)
        except Exception as exc:
            out_rows.append({"ticker": t, "familie": "", "tag": "", "fy_n":
                             None, "erste": "", "letzte": "",
                             "fehler": f"fehler: {exc}"[:90]})
    # CSV (anhaengen fuer Slices)
    neu = not Path(args.out).exists()
    with open(args.out, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "familie", "tag", "fy_n",
                                          "erste", "letzte", "fehler"])
        if neu:
            w.writeheader()
        w.writerows(out_rows)
    # Summary je Slice
    print(f"=== Slice {args.start}:{end} ({end - args.start} Ticker) ===")
    rss_mb = sorted(v for _, v in rss_spitzen)
    if rss_mb:
        print(f"RSS Spitzen: min {rss_mb[0]/1024:.0f} MB | median "
              f"{rss_mb[len(rss_mb)//2]/1024:.0f} MB | max {rss_mb[-1]/1024:.0f} MB"
              f" (n={len(rss_mb)}; Spitze darf nicht linear wachsen)")
    per = restatement_stats["perioden"] or 1
    print(f"Perioden gesamt: {restatement_stats['perioden']} | "
          f"Refilings (>1 filed, auch gleich): {restatement_stats['refiled']} "
          f"({restatement_stats['refiled'] / per * 100:.1f} %) | "
          f"Wertaenderungen (echter Look-ahead): {restatement_stats['restated']} "
          f"({restatement_stats['restated'] / per * 100:.1f} %)")
    if lag_10k:
        lag_10k_s = sorted(lag_10k)
        print(f"Verzug 10-K: Median {statistics.median(lag_10k_s):.0f} d | "
              f"P90 {lag_10k_s[int(len(lag_10k_s)*0.9)]:.0f} d | n={len(lag_10k_s)}")
    if lag_10q:
        lag_10q_s = sorted(lag_10q)
        print(f"Verzug 10-Q: Median {statistics.median(lag_10q_s):.0f} d | "
              f"P90 {lag_10q_s[int(len(lag_10q_s)*0.9)]:.0f} d | n={len(lag_10q_s)}")
    for b in beispiele[:6]:
        print("  Beispiel >5 %:", b)
    # Aggregat je Slice als Datei (Ergebnisse nicht committen)
    stats = {
        "ticker_n": end - args.start,
        "perioden": restatement_stats["perioden"],
        "refiled": restatement_stats["refiled"],
        "restated": restatement_stats["restated"],
        "lag_10k": lag_10k,
        "lag_10q": lag_10q,
        "rss_max_mb": round(max((v for _, v in rss_spitzen), default=0) / 1024),
        "beispiele": beispiele,
    }
    sp = Path(f"logs/v74_stats_{args.start}_{end}.json")
    sp.write_text(json.dumps(stats), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
