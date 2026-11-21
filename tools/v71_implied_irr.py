"""v71 Diagnose: implizierter IRR je Ticker (read-only, kein Produktionscode).

Zweck: Diskontsatz, bei dem MoS(Base, aktiver Modus aus dcf.fcf_mode/auto)
genau 0 ist -- Bisektion auf dcf.discount_rate (Untergrenze
terminal_growth + 0.001, da der Barwert der TV sonst nicht definiert ist;
vps(r) ist monoton fallend). Ergebnis je Ticker: implizierter IRR,
Quality, Screener-Status.

Aufruf (Netz noetig, laedt Statements via yfinance):
    python tools/v71_implied_irr.py                 # 47 Abnahme-Ticker
    python tools/v71_implied_irr.py AAPL MSFT KO    # eigene Tickerliste
Optionen: --years N (Default 8).
"""
from __future__ import annotations

import argparse
import copy
import logging
import math
import sys
import statistics

# Pfad fuer Paket-Importe, wenn das Skript aus dem Repo-Root/tools gestartet wird
sys.path.insert(0, ".")

from config import load_config                 # noqa: E402
from data.providers.yfinance_provider import YfinanceProvider  # noqa: E402
from scoring.pipeline import evaluate_ticker   # noqa: E402
from valuation.dcf import compute_dcf          # noqa: E402

# Abnahme-Universum v70/v71 (47 Ticker)
ABNAHME_TICKER = ("LLY TDG KDP AMCR AAPL MSFT GOOGL AMZN META NVDA AVGO "
                  "JNJ UNH XOM CVX ABBV MRK PFE INTC AMD QCOM TXN ORCL CRM "
                  "ADBE CSCO IBM ACN CAT DE BA GE MMM HON RTX LMT NOC UPS "
                  "FDX UNP KO PEP WMT COST HD MCD SBUX").split()

logger = logging.getLogger("value_screener.tools.irr")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")


def implied_irr(m, cfg: dict, max_iter: int = 60) -> float:
    """Bisektion auf dcf.discount_rate bis vps(Base) == Kurs (MoS = 0).

    Rueckgabe NaN, wenn Base oder Kurs nicht berechenbar sind.
    """
    d = compute_dcf(m, cfg)
    b = d.scenarios.get("base")
    price = d.kurs_boerse  # share_basis current -> Boersenkurs
    if (b is None or d.fcf_le0
            or b.value_per_share != b.value_per_share
            or price != price or price <= 0):
        return float("nan")

    def vps_at(r: float) -> float:
        c = copy.deepcopy(cfg)
        c["dcf"]["discount_rate"] = r
        bb = compute_dcf(m, c).scenarios.get("base")
        if bb is None or bb.value_per_share != bb.value_per_share:
            return float("nan")
        return bb.value_per_share

    g_t = float(cfg["dcf"].get("terminal_growth", 0.025))
    lo, hi = g_t + 0.001, 1.5
    f_lo, f_hi = vps_at(lo) - price, vps_at(hi) - price
    if not (f_lo == f_lo and f_hi == f_hi and f_lo > 0 > f_hi):
        return float("nan")
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        fm = vps_at(mid) - price
        if fm != fm:
            fm = -1.0  # jenseits der Nullstelle nicht mehr definiert
        if fm > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def main() -> int:
    ap = argparse.ArgumentParser(description="Implizierter IRR (v71-Diagnose)")
    ap.add_argument("ticker", nargs="*", default=None,
                    help="Ticker (Default: 47 Abnahme-Ticker)")
    ap.add_argument("--years", type=int, default=8)
    args = ap.parse_args()
    tickers = args.ticker or ABNAHME_TICKER
    cfg = load_config()
    prov = YfinanceProvider()
    rows = []
    for t in tickers:
        try:
            m = prov.load(t.upper(), years=args.years)
        except Exception as exc:  # Netz/Provider-Fehler -> n/a, kein Abbruch
            print(f"{t}: Ladefehler: {exc}")
            rows.append((t, float("nan"), float("nan"), "-"))
            continue
        irr = implied_irr(m, cfg)
        row = evaluate_ticker(m, cfg).get("row") or {}
        rows.append((t, irr, row.get("quality", float("nan")),
                    row.get("status", "-")))
    rows.sort(key=lambda r: -(r[1] if r[1] == r[1] else -1))
    print(f"{'Ticker':<7} {'IRR_impl':>9} {'Quality':>8}  Status")
    for t, irr, q, status in rows:
        irr_s = f"{irr*100:.2f}%" if irr == irr else "n/a"
        q_s = f"{q:.1f}" if q == q else "n/a"
        print(f"{t:<7} {irr_s:>9} {q_s:>8}  {status}")
    val = [r[1] for r in rows if r[1] == r[1]]
    if val:
        print(f"\nn={len(val)} | Min {min(val)*100:.2f}% | "
              f"Median {statistics.median(val)*100:.2f}% | Max {max(val)*100:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
