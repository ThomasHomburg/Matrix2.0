r"""v74 Diagnose A (Summary): Abdeckung aus logs/v74_abdeckung.csv

Zweck: Je Kennzahl-Familie die Verteilung (Min/Median/Max) der FY-Anzahl
(union ueber alle Tags der Hierarchie, nur 10-K/10-K/A), Anzahl Ticker
mit >= 8 bzw. >= 10 FY je Kernfamilie (revenue, ebit, ocf, capex, equity)
sowie Ticker mit Tag-Wechsel und Lueckenjahren. Ausgabe auf der Konsole.

Aufruf:
    python tools/v74_pit_summary.py [--csv logs/v74_abdeckung.csv]
"""
from __future__ import annotations

import argparse
import csv
import statistics
from collections import defaultdict

KERN = ("revenue", "ebit", "ocf", "capex", "equity")


def main() -> int:
    ap = argparse.ArgumentParser(description="v74 Pit-Summary")
    ap.add_argument("--csv", default="logs/v74_abdeckung.csv")
    args = ap.parse_args()
    rows = list(csv.DictReader(open(args.csv, encoding="utf-8")))
    # FY-Anzahl je (ticker, familie) = Summe der je Tag gemeldeten FY-Zeilen?
    # Nein: Zeilen je Tag zaehlen Perioden je Tag; die Familien-Abdeckung
    # ist die Zahl der DISTINCT FY-Endejahre (erste..letzte Jahre), die wir
    # aus erster/letzter Periode + fy_n je Tag annaehern. Genauer: wir
    # summieren fy_n nur EINMAL je (ticker, familie) via des Tags mit den
    # meisten FY -- Duplikate ueber Tags (gleiche Jahre unter zwei Tags)
    # wuerden doppelt zaehlen. Wir approximieren: max fy_n je Familie.
    fam_je_ticker = defaultdict(dict)   # ticker -> familie -> (max fy, erste, letzte)
    luecken = defaultdict(set)           # ticker -> {familie}  (Luecke im Fenster)
    fehler = defaultdict(list)
    FENSTER = (2012, 2025)               # v74-Backtest-Fenster
    for r in rows:
        t, fam, tag = r["ticker"], r["familie"], r["tag"]
        fe = r.get("fehler") or ""
        if fe.startswith("luecke_jahre:"):
            jahre = [int(x) for x in fe.split(":", 1)[1].split(",") if x.strip()]
            # Luecke zaehlt nur, wenn sie im Backtest-Fenster liegt UND
            # beide Nachbarjahre belegt sind (erste/letzte = Jahr NACH der Luecke)
            if any(FENSTER[0] <= y <= FENSTER[1] for y in jahre):
                luecken[t].add(fam)
            continue
        if fe:
            fehler[t].append(fe)
            continue
        if not fam or not tag or not r["fy_n"]:
            continue
        n = int(r["fy_n"])
        alt = fam_je_ticker[t].get(fam)
        # FY-Anzahl im Backtest-Fenster approximieren: Jahre zwischen
        # max(erste, 2011-12) und min(letzte, 2025), nur wenn Periode das
        # Fenster ueberlappt
        e, l = r["erste"], r["letzte"]
        if not e or not l:
            continue
        ey, ly = int(e[:4]), int(l[:4])
        im_fenster = max(0, min(ly, FENSTER[1]) - max(ey, FENSTER[0] - 1))
        if im_fenster <= 0:
            continue
        if alt is None or max(n, im_fenster) > alt[0]:
            fam_je_ticker[t][fam] = (max(n, im_fenster), e, l)

    familien = sorted({f for d in fam_je_ticker.values() for f in d})
    print(f"Ticker mit Daten: {len(fam_je_ticker)} | Fehler: {len(fehler)}"
          f" | mit Luecke: {len(luecken)}")
    print(f"\n{'Familie':<22}{'Ticker':>7}{'Min':>5}{'Median':>8}{'Max':>5}"
          f"{'  >=8':>6}{'  >=10':>7}")
    schwellen = {f: [0, 0] for f in familien}
    for fam in familien:
        werte = [d[fam][0] for d in fam_je_ticker.values() if fam in d]
        if not werte:
            continue
        w = sorted(werte)
        print(f"{fam:<22}{len(w):>7}{w[0]:>5}{statistics.median(w):>8.0f}"
              f"{w[-1]:>5}{sum(1 for x in w if x >= 8):>6}"
              f"{sum(1 for x in w if x >= 10):>7}")
        schwellen[fam] = [sum(1 for x in w if x >= 8), sum(1 for x in w if x >= 10)]
    # Kernfamilien: ALLE >= 8 bzw. >= 10
    alle8 = sum(1 for d in fam_je_ticker.values()
                if len({f for f in KERN if f in d and d[f][0] >= 8}) == len(KERN))
    alle10 = sum(1 for d in fam_je_ticker.values()
                 if len({f for f in KERN if f in d and d[f][0] >= 10}) == len(KERN))
    mind = sum(1 for d in fam_je_ticker.values()
               if {f for f in KERN if f in d and d[f][0] >= 8} == set(KERN))
    print(f"\nKernfamilien {KERN}:")
    print(f"  Ticker MIT ALLEN >= 8 FY: {alle8} / {len(fam_je_ticker)}")
    print(f"  Ticker MIT ALLEN >= 10 FY: {alle10} / {len(fam_je_ticker)}")
    # Tag-Wechsel/Luecken
    print(f"\nTicker mit Lueckenjahren (Tag-Wechsel): {len(luecken)}")
    for t in sorted(luecken)[:25]:
        print(f"  {t}: {sorted(luecken[t])}")
    if len(luecken) > 25:
        print(f"  ... +{len(luecken) - 25} weitere (CSV)")
    if fehler:
        for t in sorted(fehler)[:10]:
            print(f"  Fehler {t}: {fehler[t][:2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
