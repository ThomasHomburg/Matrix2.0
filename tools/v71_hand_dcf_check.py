"""v71 Diagnose: Handnachrechnung des DCF ohne Pipeline-Hilfsfunktionen.

Zweck: Den DCF je Ticker NUR aus Rohwerten des Providers nachrechnen
(FCFF je Quelle selbst konstruiert, Quelldurchschnitt, Wachstumspfad,
Terminal Value, Nettoverschuldung, Aktien, Wert je Aktie) und gegen
compute_dcf setzen. Dokumentiert die verwendeten Regeln im Kopf.

Regeln (Nachbau des Modells, produktionsfremd):
- je Quelle: FCFF = FCF-SBC + |Zinsaufwand| * (1 - t); t aus
  Steuern/Vorsteuer, begrenzt 0-35 % (fehlt: 21 % Normsatz, nur bei Zinsen).
- Quellen im Modus auto (Basis "ttm"): [TTM, FY-1, FY-2]
  (Geschaeftsjahre enden strikt vor dem TTM-Fenster), gleichgewichtet;
  ohne TTM: O3 Geschaeftsjahre (avg3).
- g_base = Umsatz-CAGR, gedeckelt auf dcf.base_growth_cap
  (negativer CAGR wirkt direkt, wie im Modell);
- Jahre 1-5: g_base konstant; Jahre 6-10: linearer Fade auf g_terminal;
- r = dcf.discount_rate; TV = FCF11 / (r - g_t), abgezinst auf Jahr 10;
- Equity = EV - ND, ND zum Stichtag der Start-FCF-Basis (TTM-Quartalsende
  bzw. FY-Ende, Bestandsgroesse); Aktien = Marktkap./kurs_boerse.

Aufruf (Netz noetig, laedt Statements via yfinance):
    python tools/v71_hand_dcf_check.py KO MSFT
"""
from __future__ import annotations

import argparse
import logging
import math
import sys

sys.path.insert(0, ".")

from config import load_config        # noqa: E402
from data.providers.yfinance_provider import YfinanceProvider  # noqa: E402
from valuation.dcf import compute_dcf  # noqa: E402

logger = logging.getLogger("value_screener.tools.hand_check")
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

NAN = float("nan")


def _v(metric):
    if metric is None:
        return NAN
    v = metric.value
    return v if (v == v) else NAN


def _cagr(values):
    """Jaehrliche Wachstumsrate ueber n-1 Jahre, chronologisch, Start > 0."""
    if len(values) < 2 or values[0] <= 0:
        return NAN
    n = len(values) - 1
    return (values[-1] / values[0]) ** (1 / n) - 1


def hand_dcf(m, cfg: dict) -> dict:
    """Ein DCF komplett aus Rohwerten (siehe Modul-Docstring)."""
    dcfg = cfg["dcf"]
    g_t = float(dcfg.get("terminal_growth", 0.025))
    r = float(dcfg.get("discount_rate", 0.09))
    cap = float(dcfg.get("base_growth_cap", 0.06))
    fade = float(dcfg.get("fade_to_growth", g_t))

    # --- FCFF je Geschaeftsjahr aus Rohwerten ---
    fy_data = {}
    for fy in sorted(m.years):
        y = m.years[fy]
        ocf, capex = _v(y.get("operating_cashflow")), _v(y.get("capex"))
        sbc = _v(y.get("stock_based_compensation"))
        fcf = ocf + capex if (ocf == ocf and capex == capex) else NAN
        fcf_sbc = fcf - sbc if (fcf == fcf and sbc == sbc) else NAN
        ie = _v(y.get("interest_expense"))
        ie = abs(ie) if ie == ie else NAN
        tax, pretax = _v(y.get("income_tax_expense")), _v(y.get("income_before_tax"))
        t = tax / pretax if (tax == tax and pretax == pretax and pretax != 0) else NAN
        t = min(max(t, 0.0), 0.35) if t == t else 0.21 if (ie == ie and ie > 0) else 0.0
        fcff = fcf_sbc + ie * (1 - t) if (fcf_sbc == fcf_sbc and ie == ie) else NAN
        fy_data[fy] = {"fcff": fcff, "nd": _v(y.get("net_debt")),
                       "revenue": _v(y.get("revenue"))}

    # --- TTM-Rohwerte ---
    ttm = m.ttm.ttm if m.ttm is not None else {}
    ttm_fcf = _v(ttm.get("fcf_sbc"))
    ttm_ie = _v(ttm.get("interest_expense"))
    ttm_ie = abs(ttm_ie) if ttm_ie == ttm_ie else NAN
    ttx, ttp = _v(ttm.get("income_tax_expense")), _v(ttm.get("income_before_tax"))
    ttm_t = ttx / ttp if (ttx == ttx and ttp == ttp and ttp != 0) else NAN
    ttm_t = min(max(ttm_t, 0.0), 0.35) if ttm_t == ttm_t else 0.21
    ttm_fcff = (ttm_fcf + ttm_ie * (1 - ttm_t)
                if (ttm_fcf == ttm_fcf and ttm_ie == ttm_ie) else NAN)

    # --- Quellwahl wie Modell: TTM + 2 nicht ueberlappende GJ, sonst O3J ---
    sources = []
    if ttm_fcff == ttm_fcff and m.ttm is not None and getattr(m, "ttm_plausible", True) \
            and not getattr(m, "ttm_fallback", False) and m.ttm.quarters:
        qs = sorted(q.get("quarter_end", "") for q in m.ttm.quarters)
        window_start = qs[-4][:10] if len(qs) >= 4 else None
        if window_start:
            prior = [fy for fy in sorted(m.years, reverse=True)
                     if str(m.fiscal_ends.get(fy, "9999"))[:10] < window_start][:2]
            sources = ([("TTM", ttm_fcff)] + [(f"FY{fy}", fy_data[fy]["fcff"]) for fy in prior]
                       if len(prior) == 2 else [])
    if not sources:  # avg3-Rueckfall
        vals = [(f"FY{fy}", fy_data[fy]["fcff"]) for fy in sorted(fy_data)
                if fy_data[fy]["fcff"] == fy_data[fy]["fcff"]]
        sources = vals[-3:]
    used = [v for _, v in sources if v == v]
    if not used:
        return {"error": "keine verwendbare FCFF-Quelle"}
    start = sum(used) / len(used)

    # --- Wachstum aus Umsatz-CAGR (verfuegbare Jahre), Deckel wie Modell ---
    revs = [fy_data[fy]["revenue"] for fy in sorted(fy_data)
            if fy_data[fy]["revenue"] == fy_data[fy]["revenue"] and fy_data[fy]["revenue"] > 0]
    cg = _cagr(revs) if revs else NAN
    g_base = 0.0 if cg != cg else (cg if cg < 0 else min(cg, cap))

    # --- Pfad, Barwerte, TV (Modellregeln, hier eigenstaendig) ---
    rates = [g_base] * 5 + [g_base + (fade - g_base) * (i / 5) for i in range(1, 6)]
    fcfs, f = [], start
    for g in rates:
        f *= (1 + g)
        fcfs.append(f)
    pv = sum(x / (1 + r) ** (i + 1) for i, x in enumerate(fcfs))
    tv = fcfs[-1] * (1 + g_t) / (r - g_t)
    tv_pv = tv / (1 + r) ** 10
    ev = pv + tv_pv

    # --- ND zum Stichtag der Basis; Aktien = mcap / kurs_boerse ---
    # Vereinfachung gegenueber dem Modell: ND bei TTM-Basis aus der TTM-
    # Bilanz, bei Jahresbasis aus dem juengsten FY (Modell: exakte
    # Stichtags-/Waehrungsregeln; Diagnose-Abweichung wird reported).
    basis_is_ttm = str(sources[0][0]).startswith("TTM")
    if basis_is_ttm:
        nd = _v(ttm.get("net_debt"))
    else:
        nd = fy_data[sorted(fy_data)[-1]]["nd"]
    market_cap = m.market_cap
    kurs = _v(m.price)
    shares = market_cap.value / kurs if (market_cap and kurs == kurs and kurs > 0) else NAN
    equity = ev - nd if (ev == ev and nd == nd) else NAN
    vps = equity / shares if (equity == equity and shares == shares) else NAN
    return {"start": start, "sources": sources, "g_base": g_base,
            "revenue_cagr": cg, "pv10": pv, "tv_pv": tv_pv, "ev": ev,
            "nd": nd, "shares": shares, "vps": vps, "kurs": kurs}


def main() -> int:
    ap = argparse.ArgumentParser(description="Handnachrechnung DCF (v71-Diagnose)")
    ap.add_argument("ticker", nargs="+", help="Ticker, z. B. KO MSFT")
    ap.add_argument("--years", type=int, default=8)
    args = ap.parse_args()
    cfg = load_config()
    prov = YfinanceProvider()
    for t in args.ticker:
        m = prov.load(t.upper(), years=args.years)
        h = hand_dcf(m, cfg)
        d = compute_dcf(m, cfg)
        b = d.scenarios.get("base")
        iv = b.value_per_share if (b and b.value_per_share == b.value_per_share) else NAN
        print(f"=== {t.upper()} ===")
        if "error" in h:
            print(" ", h["error"])
            continue
        print(f" Quellen: {[(l, round(v/1e9, 3)) for l, v in h['sources']]} (G USD)")
        print(f" Start-FCFF = {h['start']/1e9:.3f} G | Umsatz-CAGR {h['revenue_cagr']*100:.2f}%"
              f" -> g_base {h['g_base']*100:.2f}%")
        print(f" PV10 {h['pv10']/1e9:.1f} G + TV_pv {h['tv_pv']/1e9:.1f} G"
              f" = EV {h['ev']/1e9:.1f} G; ND {h['nd']/1e9:.1f} G")
        print(f" Aktien {h['shares']/1e9:.3f} G | Hand VpA {h['vps']:.2f} USD"
              f" | Kurs {h['kurs']:.2f}")
        if iv == iv and h["vps"] == h["vps"]:
            dev = (h["vps"] - iv) / iv * 100
            print(f" Pipeline iv_base {iv:.2f} USD -> Abweichung {dev:+.2f}%"
                  f" ({'OK' if abs(dev) < 2 else 'PRUEFEN'}; Grenze 2%)")
        else:
            print(f" Pipeline iv_base {'n/a' if iv != iv else iv} (Hand: "
                  f"{'n/a' if h['vps'] != h['vps'] else h['vps']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
