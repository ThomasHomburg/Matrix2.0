r"""v74 Diagnose D: Splits/Aktien im Point-in-time-Kontext.

Zweck: Pruefen, ob dei:EntityCommonStockSharesOutstanding je filed-Datum
split-unbereinigt ist (Beweis: Treppen im Zeitverlauf) und wie eine
historische Marktkapitalisierung aus unbereinigtem Kurs x Aktienanteil
rekonstruiert werden koennte (yfinance mit auto_adjust=False liefert
unbereinigte Schlusskurse).

Untersuchte Ticker (bekannte Splits): AAPL (4:1 2020-08), NVDA (10:1
2024-06), GOOGL (20:1 2022-07), AMZN (20:1 2022-06); TSLA wird nur
geprueft, falls im Universum (tickers_sp500.csv).

Aufruf (Netz fuer yfinance noetig; SEC aus dem Datei-Cache):
    export SEC_USER_AGENT="Vorname Nachname <mail@example.com>"
    python tools/v74_pit_splits.py
"""
from __future__ import annotations

import csv
import json
import statistics
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, ".")
os_set = False
import os  # noqa: E402

os.environ.setdefault("SEC_USER_AGENT", "Value-Screener-Diagnose t09homburg@gmail.com")

CACHE_DIR = Path("logs/sec_cache")
CIK_MAP_JSON = Path("logs/v74_cik_map.json")

SPLITS = {
    "AAPL": ("2020-08-31", 4.0),
    "NVDA": ("2024-06-10", 10.0),
    "GOOGL": ("2022-07-18", 20.0),
    "AMZN": ("2022-06-06", 20.0),
}


def _cik_map() -> dict:
    if CIK_MAP_JSON.exists():
        return json.loads(CIK_MAP_JSON.read_text(encoding="utf-8"))
    raise SystemExit("kein CIK-Mapping (erst v74_pit_abdeckung laufen lassen)")


def _shares_je_filing(raw: dict) -> list[tuple[str, float]]:
    root = raw.get("facts", {})
    inner = root.get("facts", root)
    dei = inner.get("dei", {}) if "dei" in inner else root.get("dei", {})
    units = (dei.get("EntityCommonStockSharesOutstanding")
             or root.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
             or {})
    out = []
    for u in (units.get("units") or {}).get("shares", []) or []:
        if u.get("filed") and u.get("val") is not None:
            out.append((str(u["filed"])[:10], float(u["val"])))
    return sorted(out)


def _pruefe_treppen(daten: list[tuple[str, float]]) -> list[str]:
    """Treppen: Quotient aufeinanderfolgender Werte nahe einem Split-Faktor."""
    treppen = []
    for (d0, v0), (d1, v1) in zip(daten, daten[1:]):
        if v0 > 0 and v1 > 0:
            q = v1 / v0
            if abs(q - 1.0) < 0.10:
                continue
            for f in (2.0, 4.0, 10.0, 20.0):
                if abs(q - 1.0 / f) < 0.05 / f:
                    treppen.append(f"{d0}->{d1}: {v0:,.0f} -> {v1:,.0f} "
                                   f"(q ~ 1/{f:.0f})")
                elif abs(q - f) < 0.05 * f:
                    treppen.append(f"{d0}->{d1}: {v0:,.0f} -> {v1:,.0f} "
                                   f"(q ~ {f:.0f}x)")
    return treppen


def main() -> int:
    universum = {r["ticker"].strip().upper()
                 for r in csv.DictReader(open("tickers_sp500.csv", encoding="utf-8"))
                 if (r.get("ticker") or "").strip()}
    cmap = _cik_map()
    auswahl = dict(SPLITS)
    if "TSLA" in universum:
        auswahl["TSLA"] = ("2022-08-25", 3.0)
    for t, (split_datum, faktor) in auswahl.items():
        print(f"=== {t} (Split {faktor:g}:1 am {split_datum}) ===")
        path = CACHE_DIR / f"CIK{cmap.get(t, '')}.json"
        if not path.exists() or not cmap.get(t):
            print("  kein SEC-Cache/CIK -- uebersprungen (TSLA nur falls im "
                  "Universum, ausgeschiedene nicht Teil dieser Diagnose)")
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        daten = _shares_je_filing(raw)
        if not daten:
            print("  dei:EntityCommonStockSharesOutstanding: keine Fakten")
            continue
        n = len(daten)
        # Werte vor/nach dem Split-Datum
        vor = [(d, v) for d, v in daten if d <= split_datum]
        nach = [(d, v) for d, v in daten if d > split_datum]
        print(f"  Fakten: {n} | spannt {daten[0][0]} .. {daten[-1][0]}")
        if vor and nach:
            med_vor = statistics.median(v for _, v in vor)
            med_nach = statistics.median(v for _, v in nach)
            verhaeltnis = med_vor / med_nach
            bereinigt = abs(verhaeltnis - faktor) / faktor < 0.25
            print(f"  Median Aktien vor Split: {med_vor:,.0f} | nach: "
                  f"{med_nach:,.0f} | Verhaeltnis {verhaeltnis:.2f} "
                  f"(Split-Faktor {faktor:g}) -> dei-Werte sind "
                  f"{'SPLIT-BEREINIGT (hinteres Ende passt nicht zu Kursen!)' if bereinigt else 'split-unbereinigt? siehe Treppen:'}")
        treppen = _pruefe_treppen(daten)
        for z in treppen[:6]:
            print("  Treppe:", z)
        if not treppen:
            print("  keine Treppen (Quotient ~ Faktor) gefunden -- Werte "
                  "wachsen organisch ODER sind durchgehend bereinigt")
        # yfinance: unbereinigte Kurse verfuegbar?
        try:
            import yfinance as yf
            tk = yf.Ticker(t)
            hist = tk.history(start=split_datum, end=None, auto_adjust=False,
                             interval="1mo")
            erste = hist.dropna()
            if len(erste):
                print(f"  yfinance auto_adjust=False: {len(erste)} Monatszeilen "
                      f"ab {str(erste.index[0])[:10]} verfuegbar "
                      f"(Close unadjust {float(erste['Close'].iloc[0]):.2f})")
        except Exception as exc:
            print(f"  yfinance nicht verfuegbar: {exc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
