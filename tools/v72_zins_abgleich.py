"""v72 Diagnose: Zinsaufwand yfinance vs. SEC als CSV (nicht committet).

Zweck: fuer jede Periode (FY/TTM), in der yfinance UND SEC einen
Zinsaufwand liefern, den Abgleich in tools/v72_zins_abweichungen.csv
ausgeben (Datei steht in .gitignore). Abweichung > 20 % wird in der
Pipeline nur als HINWEIS-Flag gefuehrt (zins_quellen_abweichend); der
Rechenwert bleibt immer yfinance (Regel: kein Mischen).

Aufruf (Netz + SEC_USER_AGENT noetig; companyfacts-Cache wird genutzt):
    python tools/v72_zins_abgleich.py                 # 47 Abnahme-Ticker
    python tools/v72_zins_abgleich.py AAPL MSFT       # eigene Ticker
Optionen: --out DATEI (Default tools/v72_zins_abweichungen.csv)
          --years N (Default 8)
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys

sys.path.insert(0, ".")  # Repo-Root fuer Paketimporte

from config import load_config                 # noqa: E402
from data.providers.yfinance_provider import YfinanceProvider  # noqa: E402
from data.sec import sec_interest_fallback     # noqa: E402

ABNAHME_TICKER = ("LLY TDG KDP AMCR AAPL MSFT GOOGL AMZN META NVDA AVGO "
                  "JNJ UNH XOM CVX ABBV MRK PFE INTC AMD QCOM TXN ORCL CRM "
                  "ADBE CSCO IBM ACN CAT DE BA GE MMM HON RTX LMT NOC UPS "
                  "FDX UNP KO PEP WMT COST HD MCD SBUX").split()

logger = logging.getLogger("value_screener.tools.zins_abgleich")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description="Zins-Abgleich yfinance vs. SEC")
    ap.add_argument("ticker", nargs="*", default=None)
    ap.add_argument("--out", default="tools/v72_zins_abweichungen.csv")
    ap.add_argument("--years", type=int, default=8)
    args = ap.parse_args()
    tickers = args.ticker or ABNAHME_TICKER
    cfg = load_config()
    prov = YfinanceProvider()
    rows = []
    for t in tickers:
        t = t.upper()
        try:
            m = prov.load(t, years=args.years)
        except Exception as exc:
            print(f"{t}: Ladefehler: {exc}")
            continue
        out = sec_interest_fallback(m, cfg)
        for a in out.get("abweichungen") or []:
            rows.append({
                "ticker": t, "periode": a["periode"],
                "yfinance": a["yfinance"], "sec": a["sec"], "tag": a["tag"],
                "abweichung_pct": round(a["abweichung"] * 100, 2) if a["abweichung"] == a["abweichung"] else "",
                "ueber_20pct": "ja" if a["abweichung"] == a["abweichung"] and a["abweichung"] > 0.20 else "nein",
            })
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ticker", "periode", "yfinance",
                                          "sec", "tag", "abweichung_pct",
                                          "ueber_20pct"])
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} Vergleiche -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
