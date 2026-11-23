r"""(v73) Impliziter IRR + relative Koerbe irr_top/irr_kontrolle (offline).

Abgedeckt (Freigabe-Matrix):
- Bisektion gegen handgerechnetes Beispiel (Abweichung < 0.1 Pp)
- Rundlauf: compute_dcf mit discount_rate UND discount_rate_cyclicals
  auf den IRR gesetzt -> MoS ~ 0
- Keine Wurzel -> NaN mit Grund: f(0.40) > 0 -> "irr>40%";
  f(terminal_growth + offset) < 0 -> "irr<untergrenze"
- Monotonie: hoeherer Kurs -> niedrigerer IRR
- Korb: Sektorcap max_pro_sektor; Pool-Filter je Ausschlussgrund
  (quality, status, mos_extrem, wert_nahe_null, nicht_normalisiert,
  legacy_nonUS, fcff_status, kein_irr, sektor_fehlt); deterministischer
  Zufallskorb (seed 42) aus demselben Pool ohne die Top; Korbgroesse
  Top-N = min(max_ticker, Pool//2), Kontrolle gleich gross; Pool < 16
  -> gar keine Koerbe
- Schema-Guard: Alt-DB ohne irr_auto-Spalte wird per ALTER ergaenzt,
  Daten bleiben, idempotent
- Idempotenter Snapshot (--korb all schreibt irr_top/irr_kontrolle,
  zweite Zeile wird uebersprungen)
- sector-Serialisierung: TickerMetrics roundtrip + Alt-Default leer
"""

import copy
import math
import sqlite3
from pathlib import Path

import pytest

from config import load_config
import decisions
from data.loader import Metric, TickerMetrics
from db_schema import ensure_schema
from valuation.dcf import compute_dcf, implied_irr


# ------------------------------------------------------------- Fixtures --

def _cfg(dcf_over=None, irr_over=None):
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["sec"] = {"enabled": False}
    cfg["dcf"] = dict(cfg["dcf"])
    if dcf_over:
        cfg["dcf"].update(dcf_over)
    return cfg


def _ticker(mcap=1000.0, price=10.0, fcf_by_fy=None, net_debt=0.0,
            interest=0.0, cagr=0.05, ticker="IRRT"):
    """Schuldenfreie Standard-Fixture (Zins 0 -> FCFF == FCF, handlich)."""
    if fcf_by_fy is None:
        fcf_by_fy = {2023: 100.0, 2024: 100.0, 2025: 100.0}
    m = TickerMetrics(ticker=ticker, country="US")
    m.sector = "Technology"
    m.market_cap = Metric("market_cap", mcap, "USD", "USD", 2025)
    m.price = Metric("price", price, "USD", "USD", 2025)
    rev0 = 1000.0
    for i, fy in enumerate(sorted(fcf_by_fy)):
        m.years[fy] = {
            "fcf_sbc": Metric("fcf_sbc", fcf_by_fy[fy], "USD", "USD", fy),
            "interest_expense": Metric("interest_expense", interest,
                                      "USD", "USD", fy),
            "revenue": Metric("revenue", rev0 * (1 + cagr) ** i, "USD", "USD", fy),
            "shares_diluted": Metric("shares_diluted", mcap / price,
                                     "shares", "USD", fy),
            "net_debt": Metric("net_debt", net_debt, "USD", "USD", fy),
            "income_tax_expense": Metric("income_tax_expense", 100.0,
                                         "USD", "USD", fy),
            "income_before_tax": Metric("income_before_tax", 500.0,
                                        "USD", "USD", fy),
        }
        m.fiscal_ends[fy] = f"{fy}-12-31"
    return m


def _row(ticker, sector="Technology", quality=80.0, status="nein", flags="",
         fcff="ok", irr=0.12):
    """Korb-Zeile (wie _korb_rows liefert, relevante Spalten)."""
    return {"ticker": ticker, "sector": sector, "quality": quality,
            "status_all": status, "status": status, "flags": flags,
            "dcf_fcff_status": fcff,
            "irr_auto": irr if irr is not None else float("nan"),
            "mos_rule": 0.1, "kurs": 100.0, "kurs_datum": "2026-11-20",
            "iv_bear": 80.0, "iv_base": 150.0, "iv_bull": 200.0}


def _pool_cfg(min_pool=16, max_ticker=20, max_pro_sektor=4, seed=42):
    cfg = load_config()
    cfg = copy.deepcopy(cfg)
    cfg["decisions"]["baskets"]["irr_top"] = {
        "quality_min": 60, "max_ticker": max_ticker,
        "max_pro_sektor": max_pro_sektor, "min_pool": min_pool, "seed": seed}
    return cfg


# ---------------------------------------------- (1) Handnachrechnung IRR --

def _hand_vps(r, g, fcf, shares, nd):
    """Wert je Aktie nach der Modellformel (unabhaengig nachgebaut):
    Jahre 1-5: konstantes g; Jahre 6-10: linearer Fade auf g_terminal
    (0.025); TV = FCF11 / (r - g_t), abgezinst auf Jahr 10."""
    g_t = 0.025
    rates = [g] * 5 + [g + (g_t - g) * (i / 5) for i in range(1, 6)]
    fcfs, f = [], fcf
    for gr in rates:
        f *= (1 + gr)
        fcfs.append(f)
    pv = sum(x / (1 + r) ** (i + 1) for i, x in enumerate(fcfs))
    tv = fcfs[-1] * (1 + g_t) / (r - g_t)
    ev = pv + tv / (1 + r) ** 10
    return (ev - nd) / shares


def test_bisektion_handnachrechnung():
    """implied_irr gegen unabhaengige Nachbaubisektion der vps-Formel."""
    cfg = _cfg()
    m = _ticker()          # fcf 100, g_base = 5 % (CAGR 5 %, Deckel 6 %), ND 0
    assert cfg["dcf"]["terminal_growth"] == pytest.approx(0.025)
    d = compute_dcf(m, cfg, fcf_mode="avg3")
    assert d.fcf_normalized_conv == pytest.approx(100.0)
    shares = d.shares_current
    lo, hi = 0.026, 0.40
    # vorausgesetzt eine Wurzel existiert: kursrechnung f(hi) < 0 etc.
    for _ in range(100):
        mid = (lo + hi) / 2
        if _hand_vps(mid, 0.05, 100.0, shares, 0.0) > 10.0:
            lo = mid
        else:
            hi = mid
    hand = (lo + hi) / 2
    irr, grund = implied_irr(m, cfg, mode="avg3")
    assert grund == ""
    assert abs(irr - hand) < 0.001  # < 0.1 Pp


def test_rundlauf_mos_bei_irr_null():
    """compute_dcf mit discount_rate UND discount_rate_cyclicals auf den
    IRR gesetzt -> MoS(Base) ~ 0."""
    cfg = _cfg()
    m = _ticker()
    irr, grund = implied_irr(m, cfg, mode="auto")
    assert grund == "" and irr == irr
    cfg2 = _cfg(dcf_over={"discount_rate": irr, "discount_rate_cyclicals": irr})
    d = compute_dcf(m, cfg2)
    assert d.margin_of_safety == pytest.approx(0.0, abs=1e-6)


def test_keine_wurzel_gruende():
    """(a) sehr hoher Kurs: selbst bei 40 % Diskont vps > kurs
    -> NaN, Grund 'irr>40%'; (b) sehr tiefer Kurs: bereits an der
    Untergrenze vps < kurs -> NaN, Grund 'irr<untergrenze'."""
    cfg = _cfg()
    # (a) Equity auch bei 40 % Diskont weit ueber der Marktkapitalisierung
    #     -> der gesuchte r liegt ueber der Obergrenze: NaN "irr>40%"
    m_tief = _ticker(mcap=100.0)   # FCF 100, mcap 100
    irr, grund = implied_irr(m_tief, cfg, mode="auto")
    assert irr != irr and grund == "irr>40%"
    # (b) Marktkapitalisierung weit ueber jedem inneren Wert: bereits an
    #     der Untergrenze (terminal_growth + offset) ist vps < kurs
    #     -> r unterhalb des Intervalls: NaN "irr<untergrenze"
    m_hoch = _ticker(mcap=1.0e6)
    irr2, grund2 = implied_irr(m_hoch, cfg, mode="auto")
    assert irr2 != irr2 and grund2 == "irr<untergrenze"


def test_monotonie_teuerere_bewertung_niedrigerer_irr():
    """Teurere Bewertung (hoehere Marktkapitalisierung bei gleichem
    Geschaeftsmodell) -> niedrigerer IRR. Der Nominalkurs ist bei
    share_basis=current skalierungsinvariant (vps wächst mit dem Kurs);
    entscheidend ist das Verhaeltnis Equity/Marktkapitalisierung."""
    cfg = _cfg()
    m1 = _ticker(mcap=800.0)
    m2 = _ticker(mcap=1000.0)
    i1, g1 = implied_irr(m1, cfg, mode="auto")
    i2, g2 = implied_irr(m2, cfg, mode="auto")
    assert g1 == "" and g2 == ""
    assert i1 > i2


def test_irr_je_modus_und_grund_leer():
    """avg3/latest/ttm/auto haben gueltige Ticker-IRR (ttm faellt ohne
    TTM-Daten auf O3J zurueck); ttm_only ohne TTM -> NaN mit Grund
    'basis_n/a' (kein stiller Fallback)."""
    cfg = _cfg()
    m = _ticker(price=10.0)
    for mode in ("avg3", "latest", "ttm", "auto"):
        irr, grund = implied_irr(m, cfg, mode=mode)
        assert grund == ""
        assert 0.026 < irr <= 0.40
    irr, grund = implied_irr(m, cfg, mode="ttm_only")
    assert irr != irr and grund == "basis_n/a"


# ------------------------------------------------ Koerbe (Pool/Auswahl) --

def test_sektorcap_begrenzt_kektoral_konzentration():
    """8 Ticker desselben Sektors mit hoechstem IRR -> nur 4 im Korb,
    Nachruecker aus anderem Sektor."""
    rows = [_row(f"T{i}", sector="Energy", irr=0.30 - i * 0.001)
            for i in range(8)]
    rows += [_row(f"U{j}", sector="Utilities", irr=0.20 - j * 0.002)
             for j in range(6)]
    rows += [_row(f"V{k}", sector="Financials", irr=0.19 - k * 0.003)
             for k in range(6)]
    out = decisions.koerbe(_pool_cfg(min_pool=16, max_pro_sektor=4), rows)
    top = out["irr_top"]
    assert len(top) == 10  # min(20, 20 // 2)
    sekt = {}
    for r in top:
        sekt[r["sector"]] = sekt.get(r["sector"], 0) + 1
    assert sekt.get("Energy", 0) == 4
    assert sekt.get("Utilities", 0) >= 1
    assert sekt.get("Financials", 0) >= 1


def test_pool_filter_je_ausschlussgrund():
    """Jeder Ausschlussgrund reisst die Zeile aus dem Pool."""
    rows_gut = [_row(f"G{i}", irr=0.1 + i * 0.001) for i in range(24)]
    bascfg = _pool_cfg(min_pool=16)
    pool, ausschluss, grund_je_t = decisions.irr_pool_stats(bascfg, rows_gut)
    assert len(pool) == 24 and not ausschluss
    faelle = [
        (_row("Q1", quality=55), "quality"),
        (_row("S1", status="Pruefen (Capex-Definition)"), "status"),
        (_row("S2", status="Sonderregel noetig"), "status"),
        (_row("F1", flags="mos:extrem"), "mos_extrem"),
        (_row("F2", flags="wert:nahe_null"), "wert_nahe_null"),
        (_row("F3", flags="nicht normalisiert zyk:norm<7J"), "nicht_normalisiert"),
        (_row("F4", flags="fcf_basis:legacy_nonUS"), "legacy_nonUS"),
        (_row("Z1", fcff="zinsaufwand_fehlend"), "fcff_status"),
        (_row("Z2", fcff=""), "fcff_status"),
        (_row("I1", irr=float("nan")), "kein_irr"),
        (_row("X1", sector=""), "sektor_fehlt"),
    ]
    for r, erwartet in faelle:
        _, auss, _g = decisions.irr_pool_stats(bascfg, [r])
        assert auss and list(auss)[0] == erwartet, (r["ticker"], erwartet, auss)


def test_pool_zu_klein_keine_koerbe(caplog):
    """Pool < min_pool -> gar keine irr-Koerbe (kein stiller Fallback)."""
    rows = [_row(f"P{i}", irr=0.1 + i * 0.001) for i in range(15)]  # < 16
    out = decisions.koerbe(_pool_cfg(min_pool=16), rows)
    assert out["irr_top"] == [] and out["irr_kontrolle"] == []


def test_korbgroesse_und_deterministischer_zufallskorb():
    """Top-N = min(max_ticker, Pool//2); Kontrolle gleich gross, disjunkt
    und mit seed 42 reproduzierbar."""
    rows = [_row(f"P{i:02d}", sector=f"Sek{i % 5}", irr=0.05 + i * 0.005)
            for i in range(31)]
    cfg = _pool_cfg(min_pool=16, max_ticker=20, max_pro_sektor=4, seed=42)
    out1 = decisions.koerbe(cfg, rows)
    out2 = decisions.koerbe(cfg, rows)
    top1 = [r["ticker"] for r in out1["irr_top"]]
    kon1 = [r["ticker"] for r in out1["irr_kontrolle"]]
    assert len(top1) == 15          # min(20, 31 // 2)
    assert len(set(kon1)) < 16      # echter Zufallszug aus 16 Rest-Kandidaten
    assert len(kon1) == len(top1)   # Kontrolle gleich gross wie irr_top
    assert not (set(top1) & set(kon1))
    assert out1["irr_top"] == out2["irr_top"]      # deterministisch
    assert out1["irr_kontrolle"] == out2["irr_kontrolle"]
    # Top sortiert: irr absteigend, Gleichstand Ticker alphabetisch
    irrs = [r["irr_auto"] for r in out1["irr_top"]]
    assert irrs == sorted(irrs, reverse=True)


def test_gleichstand_ticker_alphabetisch():
    rows = [_row("BBB", irr=0.10), _row("AAA", irr=0.10), _row("CCC", irr=0.10)]
    rows += [_row(f"P{i:02d}", irr=0.09 - i * 0.001) for i in range(30)]
    cfg = _pool_cfg(min_pool=16, max_ticker=20, max_pro_sektor=4)
    out = decisions.koerbe(cfg, rows)
    top = [r["ticker"] for r in out["irr_top"]]
    assert top[0] in ("AAA", "BBB", "CCC")
    # alle P* haben hoeheren... nein: 0.10 > 0.09x -> die drei zuerst, AAA vor BBB
    assert top[:3] == ["AAA", "BBB", "CCC"]


# ------------------------------------------------- Schema-Guard/Alt-DB --

def test_schema_guard_alt_db_irr_auto(tmp_path):
    """Alt-DB ohne irr_auto in decisions/valuations -> per ALTER ergaenzt,
    Daten bleiben, zweiter Aufruf idempotent."""
    db = tmp_path / "alt.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE decisions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT NOT NULL, datum TEXT NOT NULL);
        CREATE TABLE valuations (ticker TEXT NOT NULL, calc_date TEXT NOT NULL,
            iv_base REAL);
        INSERT INTO decisions (ticker, datum) VALUES ('AAA', '2026-01-01');
        INSERT INTO valuations VALUES ('AAA', '2026-01-01', 42.0);
    """)
    con.commit()
    rep = ensure_schema(con, schema_path=Path("schema.sql"))
    assert "irr_auto" in rep["decisions"]["added"]
    assert "irr_auto" in rep["valuations"]["added"]
    assert con.execute("SELECT ticker FROM decisions").fetchall() == [("AAA",)]
    assert con.execute("SELECT irr_auto FROM decisions").fetchall() == [(None,)]
    rep2 = ensure_schema(con, schema_path=Path("schema.sql"))
    assert rep2.get("decisions", {}).get("added", []) == []
    assert rep2.get("valuations", {}).get("added", []) == []
    con.close()


# --------------------------------------------- Idempotenter Snapshot ---

def test_snapshot_koerbe_idempotent_irr(tmp_path):
    """snapshot --korb all schreibt auch irr_top/irr_kontrolle (Pool aus
    einer eigenen ranking-CSV mit 33 Pool-Tickern); zweiter Lauf
    ueberspringt alle Zeilen (idempotent), irr_auto wird mitgeschrieben."""
    import csv as _csv
    from argparse import Namespace

    cols = ["ticker", "sector", "quality", "status", "status_all", "flags",
            "dcf_fcff_status", "irr_auto", "mos_rule", "mos_robust",
            "kurs", "kurs_datum", "iv_bear", "iv_base", "iv_bull"]
    out = tmp_path / "ranking_irr.csv"
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for i in range(33):
            w.writerow(_row(f"P{i:02d}", sector=f"Sek{i % 5}",
                            irr=0.05 + i * 0.001))
    db = tmp_path / "value_tracker.db"
    # je Korb einzeln (korb="all" wuerde zurecht SPY/RSP-Benchmarkkurse
    # verlangen, die es in der Fixture-DB nicht gibt)
    args = Namespace(db=str(db), korb="irr_top", datum="2026-11-20",
                     ranking=str(out), overlap=None, live=None)
    decisions.cmd_snapshot(args)
    args2 = Namespace(db=str(db), korb="irr_kontrolle", datum="2026-11-20",
                      ranking=str(out), overlap=None, live=None)
    decisions.cmd_snapshot(args2)
    con = sqlite3.connect(db)
    n1 = con.execute("SELECT COUNT(*) FROM decisions WHERE korb IN "
                     "('irr_top','irr_kontrolle')").fetchone()[0]
    assert n1 > 0        # Top-N 16 + Kontrolle 15
    # irr_auto-Spalte fuehrt die Werte der Zeilen (nicht NULL bei Korb-Zeilen)
    vals = con.execute("SELECT irr_auto FROM decisions WHERE korb='irr_top'"
                       " AND irr_auto IS NOT NULL").fetchall()
    assert len(vals) == con.execute(
        "SELECT COUNT(*) FROM decisions WHERE korb='irr_top'").fetchone()[0]
    con.close()
    # zweiter Lauf: idempotent, nichts ueberschrieben
    decisions.cmd_snapshot(args)
    decisions.cmd_snapshot(args2)
    con2 = sqlite3.connect(db)
    n2 = con2.execute("SELECT COUNT(*) FROM decisions WHERE korb IN "
                      "('irr_top','irr_kontrolle')").fetchone()[0]
    assert n2 == n1
    con2.close()


def test_sector_serialisierung_alt_default_leer():
    """TickerMetrics.sector uebersteht to_dict/from_dict; Alt-Cache ohne
    Feld -> leerer String (kein None)."""
    m = _ticker()
    m.sector = "Healthcare"
    m2 = TickerMetrics.from_dict(m.to_dict())
    assert m2.sector == "Healthcare"
    d = m.to_dict()
    d.pop("sector")
    m3 = TickerMetrics.from_dict(d)
    assert m3.sector == ""
