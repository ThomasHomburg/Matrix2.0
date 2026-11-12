"""Sanity-Check: markiert jede Kennzahl ausserhalb eines plausiblen Bereichs im Log.

Grenzen kommen aus config.yaml (Block `sanity`, [min, max]).
Zusaetzlich Quercheck Marktkap./Umsatz (gleiches Jahr, gleiche Einheit) --
erkennt Skalenfehler (z. B. Mio. vs. absolut, Faktor 1e6) zwischen
profile.mktCap und den Statements.

Fehlende Daten (NaN) werden als 'n/a' gefuehrt, nie als Verstoess.
"""

from __future__ import annotations

import logging
import math

from data.loader import TickerMetrics
from data.metrics import fcf_yield, invested_capital, roic, roe

logger = logging.getLogger("value_screener.sanity")


def _check_range(ticker: str, label: str, value: float, bounds: tuple[float, float]) -> bool:
    """True wenn plausibel (oder NaN -> nur Info-Log, kein Alarm)."""
    if math.isnan(value):
        logger.info("%s: %s = n/a (fehlende Daten)", ticker, label)
        return True
    lo, hi = bounds
    if lo <= value <= hi:
        return True
    logger.warning(
        "%s: %s = %.6g liegt ausserhalb des plausiblen Bereichs [%g, %g] -- "
        "Datenqualitaet pruefen (Einheit/Skala/Waehrung)",
        ticker, label, value, lo, hi,
    )
    return False


def check_ticker(m: TickerMetrics, sanity_cfg: dict, fx=None) -> list[str]:
    """Prueft alle Kennzahlen eines Tickers; gibt Liste der Verstoesse zurueck."""
    violations: list[str] = []
    t = m.ticker

    latest_fy = max(m.years) if m.years else None
    if latest_fy is not None:
        y = m.years[latest_fy]
        _check_range(t, "roic", roic(m, latest_fy), sanity_cfg["roic"])
        _check_range(t, "roe", roe(m, latest_fy), sanity_cfg["roe"])
        _check_range(t, "fcf_yield", fcf_yield(m, latest_fy, fx=fx), sanity_cfg["fcf_yield"])

        nd = y.get("net_debt")
        eb = y.get("ebitda")
        if nd is not None and eb is not None and not nd.is_na() and not eb.is_na() and eb.value != 0:
            _check_range(t, "net_debt_ebitda", nd.value / eb.value, sanity_cfg["net_debt_ebitda"])

        ebit = y.get("ebit")
        interest = y.get("interest_expense")
        if ebit is not None and interest is not None and not ebit.is_na() and not interest.is_na():
            if interest.value != 0:
                _check_range(
                    t, "interest_coverage", ebit.value / abs(interest.value),
                    sanity_cfg["interest_coverage"],
                )

        fcf = y.get("fcf")
        rev = y.get("revenue")
        if fcf is not None and rev is not None and not fcf.is_na() and not rev.is_na() and rev.value != 0:
            _check_range(t, "fcf_margin", fcf.value / rev.value, sanity_cfg["fcf_margin"])

        gp = y.get("gross_profit")
        if gp is not None and rev is not None and not gp.is_na() and not rev.is_na() and rev.value != 0:
            _check_range(t, "gross_margin", gp.value / rev.value, sanity_cfg["gross_margin"])

        ebit_v = ebit.value if ebit is not None and not ebit.is_na() else float("nan")
        if (
            m.market_cap is not None and not m.market_cap.is_na()
            and not math.isnan(ebit_v)
        ):
            nd_v = nd.value if nd is not None and not nd.is_na() else 0.0
            if not math.isnan(nd_v) and ebit_v > 0:
                ev = m.market_cap.value + nd_v
                _check_range(t, "ev_ebit", ev / ebit_v, sanity_cfg["ev_ebit"])

        # Quercheck Skalenkonsistenz: Marktkap. / Umsatz (beide absolut, gleiche Waehrung).
        if (
            m.market_cap is not None and not m.market_cap.is_na()
            and rev is not None and not rev.is_na() and rev.value != 0
            and m.market_cap.currency == rev.currency
        ):
            ratio = m.market_cap.value / rev.value
            if not _check_range(t, "market_cap_over_revenue", ratio, sanity_cfg["market_cap_over_revenue"]):
                violations.append(
                    f"market_cap_over_revenue={ratio:.6g} unplausibel "
                    f"(mktCap {m.market_cap.value:.6g} {m.market_cap.currency} vs. "
                    f"Umsatz {rev.value:.6g} {rev.currency}) -- Einheiten/Skala?"
                )

        if rev is not None and not rev.is_na():
            vals = [m.value for m in m.series("revenue") if not m.is_na()]
            if len(vals) >= 2 and vals[0] > 0:
                cagr = (vals[-1] / vals[0]) ** (1 / (len(vals) - 1)) - 1
                _check_range(t, "revenue_cagr", cagr, sanity_cfg["revenue_cagr"])

        ic = invested_capital(m, latest_fy)
        if math.isnan(roic(m, latest_fy)) and not math.isnan(ic):
            logger.info("%s: ROIC = n/a (NOPAT oder investiertes Kapital fehlt", t)

    return violations


def classify_mos_case(kurs, iv_base, net_debt=float("nan"), shares=float("nan"),
                      fcf_unit: str = "USD", nd_unit: str = "USD") -> dict:
    """v61: Einordnung eines extremen MoS (Diagnose Lauf v55). Rueckgabe
    {"verdict", "grund"}:
      - "datenluecke"     : iv_base oder kurs NaN (keine Bewertung berechnet)
      - "einheitenfehler" : FCF- und ND-Einheit nicht beide absolute Waehrung
                            (z. B. Mio. vs. absolut -> Faktor 1e6)
      - "wert_nahe_null"  : 0 < iv_base < 1 % des Kurses (DCF-Wert nahe null,
                            MoS = 1 - kurs/iv -> -unendlich)
      - "ok"              : sonst (MoS extrem, aber Wert/Einheiten plausibel)
    """
    nan = float("nan")
    if iv_base != iv_base or kurs != kurs:
        return {"verdict": "datenluecke", "grund": "iv_base oder kurs = NaN"}
    fu, nu = (fcf_unit or "").upper(), (nd_unit or "").upper()
    # "currency" = reportedCurrency des Statements (Wildcard, nicht "Mio.")
    wildcard = {"", "CURRENCY", "N/A", "NA"}
    mismatch = (fu not in wildcard and nu not in wildcard and fu != nu) or "MIO" in fu or "MIO" in nu
    if mismatch:
        return {"verdict": "einheitenfehler",
                "grund": f"FCF-Einheit '{fcf_unit}' != ND-Einheit '{nd_unit}' (Skalenfehler moeglich)"}
    if 0 < iv_base < 0.01 * kurs:
        return {"verdict": "wert_nahe_null",
                "grund": f"iv_base {iv_base:.6g} < 1 % von kurs {kurs:.6g} (Wert nahe null)"}
    return {"verdict": "ok",
            "grund": f"iv_base {iv_base:.6g}, kurs {kurs:.6g}, ND {net_debt:.6g}, Aktien {shares:.6g} plausibel"}
