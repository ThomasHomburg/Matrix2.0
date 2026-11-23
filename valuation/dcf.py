"""DCF-Modul: innerer Wert je Aktie mit 3 Szenarien (Bear / Base / Bull).

Nach CLAUDE.md (Build-Schritt 6), aber mit normalisiertem FCF nach SBC
(Durchschnitt der letzten 3 Jahre mit verfuegbaren Daten):

- Basis: normalisierter FCF = Ø FCF-SBC der letzten 3 Jahre (fcf_sbc je Jahr).
- Wachstum Jahre 1-5: Bear 0 %, Base = min(Umsatz-CAGR ueber verfuegbare
  Jahre, base_growth_cap), Bull = min(CAGR x bull_growth_multiplier,
  bull_growth_cap).
- Wachstum Jahre 6-10: linear abnehmend von der Jahr-5-Rate auf
  terminal_growth (fade_to_growth).
- Terminal Value = FCF_10 x (1 + g) / (r - g); Diskontsatz r aus Config
  (discount_rate bzw. discount_rate_cyclicals).
- Innerer Wert = Barwert der FCFs (Jahre 1-10) + Barwert Terminal Value.
- Innerer Wert je Aktie = Innerer Wert / verwässerte Aktien (neuestes FY).
- Margin of Safety (Base) = 1 - Kurs / innerer Wert je Aktie (Base).

Alle Parameter kommen aus config.yaml (Block `dcf`), keine Magic Numbers.
Fehlende Daten -> NaN, nie 0 (CLAUDE.md).
"""

from __future__ import annotations

import datetime as _dt
import math
from dataclasses import dataclass, field

from data.loader import TickerMetrics
from data.metrics import fy_flow_window, tax_rate, tax_rate_ttm, _tax_bounds

# v69 (F2-B): Fenster-Plausibilitaet -- Quartalsabstaende, TTM-Gesamtspanne
# (12 Monate ± Puffer) und FY-Laenge. Konservativ: Verletzung -> Fenster
# unzuverlaessig -> Quelle unbrauchbar (KEINE stille Monatsnaeherung).
_QUARTER_GAP_MIN_DAYS = 60
_QUARTER_GAP_MAX_DAYS = 120
_TTM_SPAN_MIN_DAYS = 340
_TTM_SPAN_MAX_DAYS = 390
_FY_LEN_MIN_DAYS = 280
_FY_LEN_MAX_DAYS = 380

# v69-Fix: globale Ursachenprioritaet fuer fehlgeschlagene FCF-Quellen-
# Konvertierungen (Aggregation ueber ALLE Quellen, kein first-fail-Return):
# fehlende Währung/unparsebares Label -> TTM-Fenster -> FY-Fenster -> Kurs.
_FCF_FAIL_PRIORITY = ("quelle_unbekannt", "fenster_unzuverlaessig",
                     "fy_fenster_unzuverlaessig", "fx_fehlgeschlagen")


@dataclass
class DcfScenario:
    name: str                      # "bear" | "base" | "bull"
    growth_rates: list[float]      # 10 Wachstumsraten (Jahre 1-10)
    fcfs: list[float]              # projizierte FCFs (Jahre 1-10), Berichtswährung
    pv_fcfs: float                 # Barwert der 10 FCFs
    terminal_value: float          # Barwert des Terminal Value (abgezinst)
    enterprise_value: float        # pv_fcfs + terminal_value
    value_per_share: float         # EV / Aktien (NaN wenn keine Aktienanzahl)


@dataclass
class DcfResult:
    fcf_normalized: float          # normalisierter FCF nach SBC (absolut, Berichtswährung)
    fcf_years_used: int            # Anzahl verwendeter Perioden (Jahre bzw. TTM+GJ)
    revenue_cagr: float            # Umsatz-CAGR ueber verfuegbare Jahre (Basis-Wachstum)
    discount_rate: float
    terminal_growth: float
    shares: float                  # verwässerte Aktien, neuestes FY  # aktien_dcf (verwässerte Aktien, newestes FY)
    shares_current: float = float("nan")   # v62: aktien_aktuell = Marktkap. / kurs_boerse
    kurs_boerse: float = float("nan")      # v62: Boersen-Schlusskurs (m.price)
    price_basis: str = "fy_diluted"        # v62: dcf.share_basis (current|fy_diluted)
    scenarios: dict[str, DcfScenario] = field(default_factory=dict)
    margin_of_safety: float = float("nan")   # MoS Base = 1 - Kurs / Wert je Aktie (Base)
    is_buy: bool = False           # Kaufkandidat (Quality/MoS/Bear-Regeln, siehe pipeline)
    fcf_normalized_conv: float = float("nan")  # normalisierter FCF in Marktkap.-Waehrung
    fcf_basis: str = "annual"   # "ttm" (Ø TTM + 2 nicht ueberlappende GJ) oder "annual"
    ttm_quarter_end: str = ""   # Stichtag letztes Quartal (ISO), nur bei fcf_basis="ttm"
    fcf_sources: list = field(default_factory=list)  # verwendete FCF-SBC-Werte [TTM, FY-1, FY-2]
    net_debt: float = float("nan")   # Nettoverschuldung OHNE Kundengelder (Berichtsw., aktuell)
    net_debt_source: str = ""        # "net_debt (gemeldet, exkl. Kundengelder)" | "total_debt - cash" | "n/a"
    customer_deposits: float = float("nan")  # Kundengelder (z. B. PYPL), in Cash enthalten
    fcf_mode: str = "auto"           # Startwert-Modus: "auto" | "avg3" | "latest" | "ttm" | "ttm_only"
    sbc_missing: bool = False        # True wenn kein FCF-SBC verfuegbar -> Roh-FCF ohne SBC verwendet
    fcf_le0: bool = False            # True wenn Start-FCF <= 0 (MoS n/a, Anzeige "Start-FCF <= 0")
    net_debt_mode: str = "reported"  # "reported" (gemeldet) | "incl_sti" (Schulden − (Cash+kurzfr.Anl.))
    net_debt_alt: float = float("nan")  # alternative Nettoverschuldung (anderer Modus, Berichtsw.)
    net_debt_alt_source: str = ""    # Quelle der alternativen Nettoverschuldung
    net_debt_known: bool = False      # v66: True nur bei bekannter UND in Marktkap.-Waehrung konvertierbarer Nettoverschuldung (inkl. bekanntem Wert 0)
    net_debt_status: str = "unbekannt"  # v66: bekannt_null | bekannt_positiv | bekannt_negativ | unbekannt | unvollstaendig | fx_fehlgeschlagen
    mos_base_alt: float = float("nan")   # MoS (Base) mit alternativer Nettoverschuldung
    # --- v69 (F2-B): FX-/Perioden-Transparenz -------------------------------
    fcf_fx_status: str = ""            # ok | fx_fehlgeschlagen | fenster_unzuverlaessig | fy_fenster_unzuverlaessig | quelle_unbekannt (fehlende Quell-/Zielwaehrung ODER unparsebares Label)
    fcf_window_basis: str = ""         # quartalsdaten | unzuverlaessig (nur bei TTM-Quelle)
    # v69-Fix: Ergebnis der Periodenfenster-Pruefung und der daraus folgenden
    # Verwendung der TTM-Quelle. Nie WIDERSPRUCHSARTIG zu fcf_fx_status:
    # unbekannte Waehrung -> "" (Ursache steht allein in fcf_fx_status).
    ttm_window_status: str = ""        # "" | nicht_verfuegbar | quartalsdaten | fenster_unzuverlaessig | fenster_unzuverlaessig_geskippt | nicht_erforderlich_gleiche_waehrung
    net_debt_fx_asof: str = ""         # Stichtag des fuer die ND-Konvertierung verwendeten Kurses (ISO)
    net_debt_conv: float = float("nan")  # ND in Marktkap.-Waehrung (fuer Sensitivitaet, identische Regeln)
    shares_current_status: str = ""    # ok | waehrung_unbekannt | einheit_unklar | fx_fehlgeschlagen
    scenario_growth: dict = field(default_factory=dict)  # {"bear": g5, "base": g5, "bull": g5}
    # --- v70 (FCFF): Basis-Kennzeichnung des Start-FCF ----------------------
    fcf_basis_flag: str = ""          # "" (alt) | fcff | levered_legacy | legacy_nonUS (Boersen-Suffix -> IFRS-Schutz)
    fcff_status: str = ""             # "" | ok | zinsaufwand_fehlend (strikt n/a, kein Fallback)
    fcff_interest_added: float = float("nan")  # Ø-Zinsaufschlag je Quelle (Berichtswaehrung, roh, Information)
    fcff_steuersatz_normiert: bool = False     # True, wenn bei Zinsen > 0 der Normsteuersatz benutzt wurde (tax-Daten fehlen)
    # --- v72 (Zins-Zweitquelle SEC) ------------------------------------------
    fcff_interest_sources: list = field(default_factory=list)  # je Quelle: "<label>:yfinance" | "<label>:sec:<tag>" | "<label>:unwesentlich"
    fcff_zins_schaetzung: float = float("nan")  # Wesentlichkeitsregel: unterstellter nach-Steuer-Zins (konservativ, Max), NaN = nicht angewendet


def _cagr(values: list[float]) -> float:
    """Jaehrliche Wachstumsrate ueber n-1 Jahre (chronologisch)."""
    if len(values) < 2 or values[0] <= 0:
        return float("nan")
    n = len(values) - 1
    return (values[-1] / values[0]) ** (1 / n) - 1


def _growth_path(g5: float, g_terminal: float, cfg: dict) -> list[float]:
    """10 Wachstumsraten: Jahre 1-5 konstant g5, Jahre 6-10 linear auf g_terminal."""
    fade = float(cfg.get("fade_to_growth", g_terminal))
    rates = [g5] * 5
    for i in range(1, 6):  # Jahre 6-10: linear von g5 auf fade
        t = i / 5.0
        rates.append(g5 + (fade - g5) * t)
    return rates


def _pv(fcf: float, rate: float, year: int) -> float:
    return fcf / (1.0 + rate) ** year


def _dcf_scenario(name: str, g5: float, g_terminal: float, rate: float,
                  fcf_start: float, shares: float, dcfg: dict,
                  net_debt: float = float("nan")) -> DcfScenario:
    """Ein DCF-Szenario: Wachstumspfad, FCFs, Barwerte, Wert je Aktie.

    Wird von compute_dcf (3 Szenarien) und sensitivity_mos_base (Base-Pfad
    je r/g-Kombination) verwendet -- keine duplizierte Logik.

    Unternehmenswert (EV) = Barwert FCFs + Barwert Terminal Value.
    Innerer Wert (Equity) = EV - Nettoverschuldung (ohne Kundengelder).
    Wert je Aktie = Innerer Wert / Aktien.

    v66: Nettoverschuldung unbekannt (NaN) -> Equity/Wert je Aktie NaN.
    Eine unbekannte Nettoverschuldung wird NIEMALS wie null behandelt
    (CLAUDE.md: fehlende Daten als NaN, nie als 0); der DCF ist dann
    nicht belastbar. Ein bekannter Wert von exakt 0 ist dagegen ein
    valider bekannter Wert (Equity = EV).
    """
    nan = float("nan")
    rates = _growth_path(g5, g_terminal, dcfg)
    fcfs, fcf_t = [], fcf_start
    for g in rates:
        fcf_t = fcf_t * (1.0 + g)
        fcfs.append(fcf_t)
    pv_fcfs = sum(_pv(f, rate, i + 1) for i, f in enumerate(fcfs))
    tv = fcfs[-1] * (1.0 + g_terminal) / (rate - g_terminal) if rate > g_terminal else nan
    tv_pv = _pv(tv, rate, 10) if tv == tv else nan
    ev = pv_fcfs + tv_pv if tv_pv == tv_pv else nan
    if net_debt == net_debt:
        equity = ev - net_debt if ev == ev else nan
    else:
        equity = nan  # Nettoverschuldung unbekannt -> DCF nicht belastbar (v66)
    vps = equity / shares if (equity == equity and shares == shares) else nan
    return DcfScenario(
        name=name, growth_rates=rates, fcfs=fcfs, pv_fcfs=pv_fcfs,
        terminal_value=tv_pv, enterprise_value=ev, value_per_share=vps,
    )


def _ttm_fx_window(ttm, key: str) -> "tuple[_dt.date | None, _dt.date | None, str]":
    """v69 (F2-B): TTM-Fenster aus den TATSAECHLICH verwendeten Quartalen.

    Basis ist TtmResult.flow_quarters_used[key] (die vier Quartalsenden, die
    tatsaechlich in die TTM-Summe eingegangen sind -- nicht pauschal
    quarters[0:4]). Belastbar (basis="quartalsdaten") nur, wenn
    1. genau 4 paarweise verschiedene, ISO-parsebare Quartalsenden,
    2. aufsteigend sortierte Abstaende in [60, 120] Tagen (erkennt
       uebersprungene Quartale beim Nachziehen aelterer Werte),
    3. die Gesamtspanne in [340, 390] Tagen (12 Monate ± Puffer),
    4. ein UNMITELBAR davor liegendes Quartalsende aus ttm.quarters
       existiert, dessen Abstand zum aeltesten verwendeten Ende ebenfalls
       in [60, 120] Tagen liegt (Fenster-Anfang = Vorgaenger + 1 Tag).
    Jede Verletzung -> (None, None, "unzuverlaessig"): die Quelle wird bei
    der Periodenabgrenzung als unzuverlaessig behandelt (kein Mix mit FY,
    keine Monats-/Neunmonatsnaeherung).
    """
    used = ((getattr(ttm, "flow_quarters_used", None) or {}).get(key))
    if not used or len(used) != 4:
        return None, None, "unzuverlaessig"
    dates: list[_dt.date] = []
    for u in used:
        try:
            dates.append(_dt.date.fromisoformat(str(u)[:10]))
        except (ValueError, TypeError):
            return None, None, "unzuverlaessig"
    d = sorted(dates)
    if len(set(d)) != 4:
        return None, None, "unzuverlaessig"
    # Abstaende der vier verwendeten Quartale (uebersprungene Quartale beim
    # Nachziehen aelterer Werte erzeugen hier >120-Tage-Spruenge)
    for i in range(3):
        gap = (d[i + 1] - d[i]).days
        if not (_QUARTER_GAP_MIN_DAYS <= gap <= _QUARTER_GAP_MAX_DAYS):
            return None, None, "unzuverlaessig"
    pred = None
    for q in (getattr(ttm, "quarters", None) or []):
        raw = str((q or {}).get("quarter_end", "") or "")[:10]
        try:
            qd = _dt.date.fromisoformat(raw)
        except (ValueError, TypeError):
            continue
        if qd < d[0] and (pred is None or qd > pred):
            pred = qd
    if pred is None or not (_QUARTER_GAP_MIN_DAYS <= (d[0] - pred).days
                            <= _QUARTER_GAP_MAX_DAYS):
        return None, None, "unzuverlaessig"
    # Gesamtspanne des 12-Monats-Fensters: Vorgaenger -> neuestes verwendetes
    # Ende (nicht die verwendeten Ende untereinander -- die spannen nur ~9 Monate).
    span = (d[3] - pred).days
    if not (_TTM_SPAN_MIN_DAYS <= span <= _TTM_SPAN_MAX_DAYS):
        return None, None, "unzuverlaessig"
    return pred + _dt.timedelta(days=1), d[3], "quartalsdaten"


def _shares_current_gate(m: TickerMetrics, mcap, kurs: float, fx
                         ) -> "tuple[float, str]":
    """v69 (F2-B): Waehrungs-/Einheiten-Gate fuer shares_current = mcap/kurs.

    1. Beide Waehrungen muessen BEKANNT sein (leer = unbekannt, v68).
    2. GBP-Familie (GBP/GBp/GBX) nur bei eindeutig normalisierter Einheit
       (TickerMetrics.fx_unit_state == "normalisiert"); "roh_gb_pence"
       (Kurs in Pence, Marktkap. in GBP-Majors, Faktor 100) und
       "unbekannt" werden GESPERRT -> kein scheinbar gueltiger Wert.
       Kein Raten allein aus dem String "GBP" -- nur das Zustandsfeld.
    3. Identische Waehrung -> Division; verschiedene bekannte Waehrungen ->
       Konvertierung des Kurses NUR mit aktuellem Kurs (convert_current,
       Altersgrenze fx.current_max_age_days), sonst NaN. KEIN FY-Rueckfall.
    Rueckgabe (shares, status); status != "ok" -> shares NaN.
    """
    nan = float("nan")
    mc_cur = (mcap.currency or "").upper().strip()
    pr = m.price
    pr_cur = (pr.currency or "").upper().strip() if pr is not None else ""
    if not mc_cur or not pr_cur:
        return nan, "waehrung_unbekannt"
    state = str(getattr(m, "fx_unit_state", "unbekannt") or "unbekannt")
    gbp_family = mc_cur.startswith("GB") or pr_cur.startswith("GB")
    if gbp_family and state != "normalisiert":
        return nan, "einheit_unklar"
    if mc_cur == pr_cur:
        return mcap.value / kurs, "ok"
    converter = fx
    if converter is None:
        from data.metrics import _NoFX
        converter = _NoFX()
    kurs_conv = converter.convert_current(kurs, pr_cur, mc_cur)
    if kurs_conv != kurs_conv or kurs_conv <= 0:
        return nan, "fx_fehlgeschlagen"
    return mcap.value / kurs_conv, "ok"


def _convert_fcf_sources(m: TickerMetrics, start: dict, converter, to_cur: str
                          ) -> "tuple[float, str, str]":
    """v69 (F2-B): konvertiert JEDE FCF-Quelle einzeln (eigene Waehrung,
    eigener Zeitraum: TTM-Fenster aus den verwendeten Quartalen, FY-Fenster
    aus fiscal_ends), DANN erst Mitteln -- Gewichtung und Quellenauswahl
    bleiben unveraendert. Identische Waehrungen benoetigen keinen FX-Kurs
    (die Periodenabgrenzung wurde bereits in _ttm_fcf_start geprueft).
    Eine nicht konvertierbare Quelle macht das Ergebnis NaN (kein stiller
    Ersatz). Rueckgabe (wert, fx_status, window_basis).

    v69-Fix: Ursachenreihenfolge je Quelle UND globale Aggregation --
    1. identische Waehrung -> Kurzschluss (Wert, kein FX noetig)
    2. fehlende Quell-/Zielwaehrung -> quelle_unbekannt (VOR jeder
       Fensterbestimmung -- nie als fx_fehlgeschlagen klassifiziert)
    3. FY-Label parsen (VOR fy_flow_window): unparsebar -> quelle_unbekannt
    4. TTM-/FY-Fenster unzuverlaessig -> fenster_unzuverlaessig /
       fy_fenster_unzuverlaessig
    5. Fenster/Stichtag vorhanden, Konvertierung NaN -> fx_fehlgeschlagen
    Fehlerhafte Quellen werden gesammelt; zurueckgegeben wird der
    hoechstpriorisierte Status ueber ALLE Quellen (_FCF_FAIL_PRIORITY)
    statt der Ursache der zuerst geprueften Quelle. Erfolgreiche
    Konvertierungen und die Mittelwertbildung bleiben unveraendert.
    """
    nan = float("nan")
    sources = start.get("sources") or []
    if not sources:
        return nan, "fx_fehlgeschlagen", ""
    to = (to_cur or "").upper().strip()
    basis = ""
    conv_vals: list[float] = []
    fails: list[tuple[str, str]] = []   # (status, fensterbasis) je Quelle
    for label, value, cur in sources:
        c = (cur or "").upper().strip()
        if c and c == to:
            conv_vals.append(float(value))
            continue
        # 2) fehlende Waehrung -> quelle_unbekannt (vor Fensterbestimmung)
        if not c or not to:
            fails.append(("quelle_unbekannt", ""))
            continue
        lbl = str(label)
        if lbl.startswith("TTM"):
            ttm = getattr(m, "ttm", None)
            d0, d1, w_basis = _ttm_fx_window(ttm, start.get("ttm_key") or "fcf_sbc")
            if d0 is None:
                fails.append(("fenster_unzuverlaessig", w_basis))
                continue
            basis = w_basis
        else:
            # 3) FY-Label parsen (VOR fy_flow_window)
            try:
                fy = int(str(lbl)[2:])
            except ValueError:
                fails.append(("quelle_unbekannt", ""))
                continue
            d0, d1 = fy_flow_window(m, fy)
            if d0 is None:
                fails.append(("fy_fenster_unzuverlaessig", ""))
                continue
        # 5) Fenster vorhanden, aber Konvertierung fehlgeschlagen
        conv = converter.convert_flow(value, c, to, d0, d1)
        if conv != conv:
            fails.append(("fx_fehlgeschlagen", basis if lbl.startswith("TTM") else ""))
            continue
        conv_vals.append(conv)
    if fails:
        # globale Prioritaet statt Reihenfolge der Quellenpruefung
        for status in _FCF_FAIL_PRIORITY:
            for st, b in fails:
                if st == status:
                    return nan, st, b
        return nan, fails[0][0], fails[0][1]
    return sum(conv_vals) / len(conv_vals), "ok", basis


def _ttm_fcf_start(m: TickerMetrics) -> dict | None:
    """TTM-Start-FCF: Ø aus TTM (letzte 4 Quartale) + bis zu 2 davor
    liegenden, NICHT ueberlappenden Geschaeftsjahren.

    Nicht ueberlappend: das Geschaeftsjahr muss vor dem ersten Quartal des
    TTM-Fensters enden (exaktes FY-Enddatum vs. erstes TTM-Quartal).
    Rueckgabe: None wenn TTM nicht verwendbar (kein TTM, unplausibel,
    Fallback aktiv, oder zu wenig nicht ueberlappende GJ), sonst dict mit
    fcf / years_used / quarter_end / sources / currency.
    """
    ttm = getattr(m, "ttm", None)
    if (ttm is None or not getattr(m, "ttm_plausible", True)
            or getattr(m, "ttm_fallback", False)):
        return None
    ttm_fcf = ttm.ttm.get("fcf_sbc")
    sbc_missing = False
    if ttm_fcf is None or ttm_fcf.is_na():
        # TTM ohne SBC -> Fallback auf Roh-FCF TTM
        ttm_fcf = ttm.ttm.get("fcf")
        sbc_missing = True
    if ttm_fcf is None or ttm_fcf.is_na():
        return None
    ttm_end = ttm.quarter_end  # ISO-Stichtag letztes Quartal
    # v69 (F2-B): Periodenabgrenzung aus dem ECHTEN TTM-Fenster (nachweislich
    # verwendete Quartale) statt der bisherigen "9 Monate zurueck"-Naeherung.
    # Ist das Fenster unzuverlaessig (Luecken/inkonsistente Komponenten-
    # Quartale/kein Vorgaengerquartal), faellt die TTM-Quelle komplett weg
    # (auch bei identischer Waehrung: keine Mittelung mit moeglicherweise
    # ueberlappenden FY-Werten) -- bestehende Rueckfallkette greift (Ø3J).
    ttm_metric_key = "fcf" if sbc_missing else "fcf_sbc"
    window_start, _window_end, window_basis = _ttm_fx_window(ttm, ttm_metric_key)
    if window_start is None or window_basis != "quartalsdaten":
        return None
    # Geschaeftsjahre, deren Enddatum STRICT vor dem TTM-Fenster-Anfang liegt
    prior_fys = []
    for fy in sorted(m.years, reverse=True):
        fe = m.fiscal_ends.get(fy)
        if not fe:
            continue
        try:
            fe_d = _dt.date.fromisoformat(str(fe)[:10])
        except ValueError:
            continue
        if fe_d < window_start:
            prior_fys.append(fy)
    # Vorjahres-GJ ebenfalls auf Roh-FCF fallbacken, wenn deren fcf_sbc fehlt
    fy_vals_fb = []
    for fy in prior_fys[:2]:
        met = m.years[fy].get("fcf_sbc")
        if met is None or met.is_na():
            met = m.years[fy].get("fcf")
            sbc_missing = True
        if met is not None and not met.is_na():
            fy_vals_fb.append((fy, met.value, met.currency))
    if len(fy_vals_fb) < 2:
        return None  # nicht genug nicht-ueberlappende GJ
    vals = [ttm_fcf.value] + [v for _, v, _ in fy_vals_fb]
    return {
        "fcf": sum(vals) / len(vals),
        "years_used": len(vals),
        "quarter_end": ttm_end,
        "sources": ([("TTM", ttm_fcf.value, ttm_fcf.currency)]
                    + [(f"FY{fy}", v, c) for fy, v, c in fy_vals_fb]),
        "currency": ttm_fcf.currency,
        "sbc_missing": sbc_missing or None,
        "ttm_key": ttm_metric_key,   # v69: Quelle des nachgewiesenen Fensters
        "ttm_window_start": window_start.isoformat(),
        "ttm_window_status": "quartalsdaten",   # v69-Fix: Fenster belastbar
    }


def _ttm_only_fcf_start(m: TickerMetrics) -> dict | None:
    """Reiner TTM-Start-FCF nach SBC (ohne Ø ueber mehrere Perioden).

    Wie _ttm_fcf_start, aber nur das TTM-Fenster (Summe der letzten 4
    Quartale) -- keine Hinzunahme davor liegender Geschaeftsjahre.
    Rueckgabe: None wenn TTM nicht verwendbar (kein TTM, unplausibel,
    Fallback aktiv), sonst dict mit fcf / years_used=1 / quarter_end /
    sources / currency.
    """
    ttm = getattr(m, "ttm", None)
    if (ttm is None or not getattr(m, "ttm_plausible", True)
            or getattr(m, "ttm_fallback", False)):
        return None
    ttm_fcf = ttm.ttm.get("fcf_sbc")
    sbc_missing = False
    if ttm_fcf is None or ttm_fcf.is_na():
        # TTM ohne SBC -> Fallback auf Roh-FCF TTM
        ttm_fcf = ttm.ttm.get("fcf")
        sbc_missing = True
    if ttm_fcf is None or ttm_fcf.is_na():
        return None
    # v69-Fix: Drei-Fall-Waehrungsentscheidung (Entscheidungstabelle) --
    # ttm_only mischt NIE mit FY, daher ist bei identischen bekannten
    # Waehrungen KEIN FX-Fenster erforderlich. Bei bekannten, aber
    # unterschiedlichen Waehrungen bleibt das Fenster konservativ zwingend
    # (TTM wird weitergereicht; die Konvertierung scheitert dann kontrolliert
    # in _convert_fcf_sources -> fenster_unzuverlaessig, KEIN Ø3J-Fallback).
    # Bei unbekannter Quell-/Zielwaehrung erhaelt das start-dict KEIN
    # Fensterlabel -- die Ursache ist ausschliesslich quelle_unbekannt.
    ttm_metric_key = "fcf" if sbc_missing else "fcf_sbc"
    _ws, _we, w_basis = _ttm_fx_window(ttm, ttm_metric_key)
    src_cur = (ttm_fcf.currency or "").upper().strip()
    _mcap = getattr(m, "market_cap", None)
    mcap_cur = ((_mcap.currency or "").upper().strip()
                if _mcap is not None else "")
    w_status = ""
    if not (src_cur and mcap_cur):
        w_status = ""            # unbekannte Waehrung -> kein Fensterlabel
    elif w_basis == "quartalsdaten":
        w_status = "quartalsdaten"
    elif src_cur == mcap_cur:
        w_status = "nicht_erforderlich_gleiche_waehrung"
    else:
        w_status = "fenster_unzuverlaessig"
    out = {
        "fcf": ttm_fcf.value,
        "years_used": 1,
        "quarter_end": ttm.quarter_end,
        "sources": [("TTM", ttm_fcf.value, ttm_fcf.currency)],
        "currency": ttm_fcf.currency,
        "ttm_key": ttm_metric_key,
        "ttm_window_start": _ws.isoformat() if _ws else "",
        "ttm_window_status": w_status,
    }
    if sbc_missing:
        out["sbc_missing"] = True
    return out


def _avg_fcf_start(m: TickerMetrics, n: int) -> dict | None:
    """Ø FCF-SBC der letzten n Geschaeftsjahre mit Daten (chronologisch). v33: Fallback auf Roh-FCF ohne SBC."""
    sbc_series = [(fy, m.years[fy]["fcf_sbc"]) for fy in sorted(m.years)
                  if "fcf_sbc" in m.years[fy] and not m.years[fy]["fcf_sbc"].is_na()]
    if not sbc_series:
        # Fallback auf Roh-FCF (ohne SBC), sbc_missing=True
        fcf_series = [(fy, m.years[fy]["fcf"]) for fy in sorted(m.years)
                      if "fcf" in m.years[fy] and not m.years[fy]["fcf"].is_na()]
        if not fcf_series:
            return None
        last = fcf_series[-n:]
        return {
            "fcf": sum(x.value for _, x in last) / len(last),
            "years_used": len(last),
            "sources": [(f"FY{fy}", x.value, x.currency) for fy, x in last],
            "currency": last[-1][1].currency,
            "sbc_missing": True,
        }
    last = sbc_series[-n:]
    return {
        "fcf": sum(x.value for _, x in last) / len(last),
        "years_used": len(last),
        "sources": [(f"FY{fy}", x.value, x.currency) for fy, x in last],
        "currency": last[-1][1].currency,
    }


def _net_debt_ex_customer_deposits(m: TickerMetrics, fcf_basis: str,
                                    net_debt_mode: str = "reported"):
    """Nettoverschuldung OHNE Kundengelder (Berichtswährung) + Quelle.

    Kundengelder (z. B. PYPL 'Deposit Liabilities') sind wirtschaftlich
    Fremdmittel, sitzen aber in 'Cash' -- die gemeldete Nettoverschuldung
    (Total Debt - Cash) unterschlaegt die Verschuldung daher um die
    Kundengelder. Sie werden hier hinzuaddiert.

    net_debt_mode (config `dcf.net_debt_mode`):
      "reported"  — gemeldete Nettoverschuldung (+ Kundengelder)
      "incl_sti"  — Finanzschulden − (Cash + kurzfristige Anlagen)
                    (+ Kundengelder); kurzfristige Anlagen gelten als
                    liquid und werden mit abgezogen.

    Prioritaet: TTM-Bestand (Stichtag letztes Quartal) falls fcf_basis="ttm",
    sonst das neueste Geschaeftsjahr mit Bilanzdaten.
    Rueckgabe: (wert, quelle, kundengelder, waehrung, qualitaet, as_of).
    as_of (v69, F2-B): ISO-Stichtag der tatsaechlich verwendeten Bilanz --
    TTM: ttm.quarter_end; FY: fiscal_ends[fy]; None ohne Ergebnis. Die
    FX-Konvertierung (Bestandsgroesse) MUSS diesen Stichtag verwenden
    (convert_stock, Look-ahead-sicher) -- niemals pauschal latest_fy.
    qualitaet (v66): "ok" | "fehlend" (keine Bilanzdaten zu ND/Schulden/Cash)
    | "unvollstaendig" (Bilanzdaten vorhanden, aber nicht kombinierbar,
    z. B. Total Debt ohne Cash). Ein bekannter Wert von 0 ist "ok" und
    von NaN zu unterscheiden.
    """
    nan = float("nan")
    saw_balance = False  # irgendein Bilanz-Feld (net_debt/_total_debt/_cash) vorhanden
    ttm = getattr(m, "ttm", None)
    if fcf_basis == "ttm" and ttm is not None:
        nd_m = ttm.ttm.get("net_debt")
        cd_m = ttm.ttm.get("customer_deposits")
        td_m = ttm.ttm.get("_total_debt")
        cash_m = ttm.ttm.get("_cash")
        sti_m = ttm.ttm.get("_short_term_investments")
        if nd_m is not None or td_m is not None or cash_m is not None:
            saw_balance = True
        cd = cd_m.value if (cd_m is not None and not cd_m.is_na()) else nan
        cur = (nd_m.currency if nd_m is not None and not nd_m.is_na()
               else (td_m.currency if td_m is not None and not td_m.is_na() else ""))
        if net_debt_mode == "incl_sti" and td_m is not None and not td_m.is_na():
            liquid = 0.0
            if cash_m is not None and not cash_m.is_na():
                liquid += cash_m.value
            if sti_m is not None and not sti_m.is_na():
                liquid += sti_m.value
            if liquid > 0 or (cash_m is not None and not cash_m.is_na()):
                adj = td_m.value - liquid + (cd if cd == cd else 0.0)
                src = (f"TTM (Stichtag {ttm.quarter_end}) "
                       f"Total Debt − (Cash + kurzfr. Anlagen)"
                       + (" + Kundengelder" if cd == cd else ""))
                return adj, src, cd, cur, "ok", str(ttm.quarter_end or "")[:10]
        if nd_m is not None and not nd_m.is_na():
            adj = nd_m.value + cd if cd == cd else nd_m.value
            src = (f"TTM (Stichtag {ttm.quarter_end})"
                   + (" + Kundengelder" if cd == cd else ""))
            return adj, src, cd, nd_m.currency, "ok", str(ttm.quarter_end or "")[:10]
    for fy in sorted(m.years, reverse=True):
        y = m.years[fy]
        nd = y.get("net_debt")
        cd = y.get("customer_deposits")
        td = y.get("_total_debt")
        cash = y.get("_cash")
        sti = y.get("_short_term_investments")
        if nd is not None or td is not None or cash is not None:
            saw_balance = True
        cd_v = cd.value if (cd is not None and not cd.is_na()) else nan
        if net_debt_mode == "incl_sti" and td is not None and not td.is_na():
            liquid = 0.0
            if cash is not None and not cash.is_na():
                liquid += cash.value
            if sti is not None and not sti.is_na():
                liquid += sti.value
            if liquid > 0 or (cash is not None and not cash.is_na()):
                adj = td.value - liquid + (cd_v if cd_v == cd_v else 0.0)
                src = (f"FY{fy} Total Debt − (Cash + kurzfr. Anlagen)"
                       + (" + Kundengelder" if cd_v == cd_v else ""))
                return adj, src, cd_v, td.currency, "ok", (m.fiscal_ends.get(fy) or "")[:10]
        if nd is not None and not nd.is_na():
            adj = nd.value + cd_v if cd_v == cd_v else nd.value
            src = f"FY{fy} net_debt" + (" + Kundengelder" if cd_v == cd_v else "")
            return adj, src, cd_v, nd.currency, "ok", (m.fiscal_ends.get(fy) or "")[:10]
        if (td is not None and not td.is_na()
                and cash is not None and not cash.is_na()):
            adj = td.value - cash.value + (cd_v if cd_v == cd_v else 0.0)
            src = f"FY{fy} total_debt - cash" + (" + Kundengelder" if cd_v == cd_v else "")
            return adj, src, cd_v, td.currency, "ok", (m.fiscal_ends.get(fy) or "")[:10]
    quality = "unvollstaendig" if saw_balance else "fehlend"
    return nan, "n/a", nan, "", quality, None


def _ttm_skip_status(m: TickerMetrics) -> str:
    """v69-Fix: Ursache des TTM-Skips (start=None bei TTM-Anforderung).

    "fenster_unzuverlaessig_geskippt", wenn ein TTM-Wert existiert, das
    Periodenfenster aber unzuverlaessig ist (-> transparenter Ø3J-Fallback
    bei auto/ttm; D_a/kein Ø3J bei ttm_only); sonst "nicht_verfuegbar"
    (kein TTM, unplausibel, Fallback aktiv, kein TTM-FCF-Wert oder -- bei
    _ttm_fcf_start -- zu wenige nicht ueberlappende GJ).
    """
    ttm = getattr(m, "ttm", None)
    if (ttm is None or not getattr(m, "ttm_plausible", True)
            or getattr(m, "ttm_fallback", False)):
        return "nicht_verfuegbar"
    ttm_fcf = ttm.ttm.get("fcf_sbc")
    key = "fcf_sbc"
    if ttm_fcf is None or ttm_fcf.is_na():
        ttm_fcf = ttm.ttm.get("fcf")
        key = "fcf"
    if ttm_fcf is None or ttm_fcf.is_na():
        return "nicht_verfuegbar"
    if _ttm_fx_window(ttm, key)[2] != "quartalsdaten":
        return "fenster_unzuverlaessig_geskippt"
    return "nicht_verfuegbar"


def _fcff_source_interest_tax(m: TickerMetrics, label: str
                             ) -> "tuple[float | None, float, str]":
    """v70/v72: Zinsaufwand + Steuersatz EINER FCF-Quelle (Berichtswaehrung).

    TTM-Quelle -> TtmResult (Summe letzter 4 Quartale; interest_expense liegt
    in FLOW_KEYS), Steuersatz via tax_rate_ttm. FY-Quelle -> m.years[fy],
    Steuersatz via tax_rate.

    v72 Zweitquelle: liefert yfinance NaN/nichts, greift NUR dann der
    Rueckgriff auf m.sec_interest (von data.sec.sec_interest_fallback
    gefuellte SEC-Werte; yfinance bleibt Vorrang, kein Mischen).
    Rueckgabe (zinsaufwand_float | None, steuersatz, quelle);
    quelle = "yfinance" | "sec:<tag>"; Zinsaufwand None = nach beiden
    Quellen fehlt. Der Steuersatz wird nur bei Zinsaufwand > 0 benoetigt."""
    lbl = str(label)
    if lbl.startswith("TTM"):
        ttm = getattr(m, "ttm", None)
        tm = getattr(ttm, "ttm", None) or {}
        ie = tm.get("interest_expense")
        if ie is not None and not ie.is_na():
            return abs(float(ie.value)), tax_rate_ttm(ttm), "yfinance"
        sec = (getattr(m, "sec_interest", None) or {}).get("TTM")
        if sec is not None:
            sv, tag = sec
            return abs(float(sv)), tax_rate_ttm(ttm), f"sec:{tag}"
        return None, float("nan"), ""
    try:
        fy = int(lbl[2:])
    except ValueError:
        return None, float("nan"), ""
    y = m.years.get(fy, {})
    ie = y.get("interest_expense")
    if ie is not None and not ie.is_na():
        return abs(float(ie.value)), tax_rate(m, fy), "yfinance"
    sec = ((getattr(m, "sec_interest", None) or {}).get("FY") or {}).get(fy)
    if sec is not None:
        sv, tag = sec
        return abs(float(sv)), tax_rate(m, fy), f"sec:{tag}"
    return None, float("nan"), ""


def _fcff_adjust_start(m: TickerMetrics, start: dict
                      ) -> "tuple[list | None, float, bool]":
    """v70/v72: FCFF-Basis je Quelle (VOR der FX-Konvertierung, in
    Berichtswaehrung): wert_fcff = fcf + |Zinsaufwand| * (1 - t).

    - Zinsaufwand exakt 0 ist ein GUELTIGER Wert (kein Steuersatz noetig).
    - Steuersatz NaN bei Zinsen > 0 -> Normsteuersatz + Flag
      steuersatz_normiert (Entscheidung D1=B, Freigabe v70).
    - v72: Fehlt der Zinsaufwand EINER Quelle nach yfinance UND SEC, wird
      diese Quelle OFFEN MARKIERT (adj=None) statt sofort n/a; die
      endgueltige Entscheidung (Wesentlichkeitsregel -> Aufschlag 0 oder
      strikt n/a) wird in compute_dcf NACH der ND-Ermittlung nachgeholt
      (die Regel braucht ND/Marktkap.).
    Rueckgabe (plan | None, Mittel-Zinsaufschlag, steuersatz_normiert);
    plan = Liste je Quelle: {label, value, cur, adj | None, quelle}."""
    nan = float("nan")
    sources = list(start.get("sources") or [])
    if not sources:
        return None, nan, False
    t_min, t_max, t_norm = _tax_bounds()
    adj_vals: list[float] = []
    steuer_normiert = False
    plan = []
    for label, value, cur in sources:
        ie, t, quelle = _fcff_source_interest_tax(m, label)
        if ie is None:
            plan.append({"label": str(label), "value": float(value), "cur": cur,
                         "adj": None, "quelle": ""})
            continue
        if ie == 0:
            t = 0.0                      # kein Steuersatz erforderlich
        elif t != t:
            t = t_norm                   # D1=B: Normsteuersatz + Flag
            steuer_normiert = True
        adj = ie * (1.0 - t)
        plan.append({"label": str(label), "value": float(value), "cur": cur,
                     "adj": adj, "quelle": quelle})
        adj_vals.append(adj)
    avg = (sum(adj_vals) / len(adj_vals)) if adj_vals else nan
    return plan, avg, steuer_normiert


def _start_from_plan(start: dict, plan: list) -> dict:
    """Finalisiert den Start-FCF aus dem Quellen-Plan: Quellen mit adj
    werden angehoben, offen markierte Quellen laufen in compute_dcf ueber
    die Wesentlichkeitsentscheidung mit adj=0 hinein."""
    sources = [(p["label"], p["value"] + (p["adj"] or 0.0), p["cur"])
               for p in plan]
    new_start = dict(start)
    new_start["sources"] = sources
    new_start["fcf"] = sum(v for _, v, _ in sources) / len(sources)
    return new_start


def _fcff_quellen_ende(m: TickerMetrics, label: str):
    """Periodenende einer FCF-Quelle als date (TTM -> Quartalsende,
    FY<n> -> fiscal_ends[n]); None wenn unbekannt."""
    import datetime as _dt
    lbl = str(label)
    try:
        if lbl.startswith("TTM"):
            ttm = getattr(m, "ttm", None)
            if ttm is None or not getattr(ttm, "quarter_end", ""):
                return None
            return _dt.date.fromisoformat(str(ttm.quarter_end)[:10])
        fe = (m.fiscal_ends or {}).get(int(lbl[2:]))
        return _dt.date.fromisoformat(str(fe)[:10]) if fe else None
    except ValueError:
        return None


def _zins_unwesentlich(m: TickerMetrics, dcfg: dict, pending: list,
                       nd_conv: float, mcap_value: float) -> "tuple[bool, float]":
    """v72 Wesentlichkeitsregel fuer offen markierte Quellen (kein Zinsaufwand
    aus yfinance UND SEC, z. B. AAPL ab FY2024):

    Greift pro Quelle nur, wenn der LETZTE bekannte Zinsaufwand (yfinance
    oder SEC; Periodenende max. fcff_zins_max_alter_jahre zurueck) nach
    Steuer < fcff_zins_unwesentlich_grenze x Start-FCF der Quelle ist UND
    |ND| / Marktkap. < fcff_zins_nd_mcap_grenze. Dann Aufschlag 0 mit Flag
    zins_unwesentlich; Rueckgabe (True, unterstellter nach-Steuer-Betrag
    konservativ als Maximum). Sonst (False, NaN) -> weiter strikt n/a.
    Strenge '<': genau an der Grenze greift die Regel NICHT."""
    nan = float("nan")
    max_alter = int(dcfg.get("fcff_zins_max_alter_jahre", 4))
    grenze = float(dcfg.get("fcff_zins_unwesentlich_grenze", 0.05))
    nd_grenze = float(dcfg.get("fcff_zins_nd_mcap_grenze", 0.10))
    t_min, t_max, t_norm = _tax_bounds()
    if not (nd_conv == nd_conv and mcap_value > 0
            and abs(nd_conv) / mcap_value < nd_grenze):
        return False, nan
    # Kandidaten "letzter bekannter Zins": yfinance-Jahre + SEC-FY-Werte
    kandidaten = []  # (end_date, wert, steuersatz)
    for fy in sorted(m.years):
        y = m.years[fy]
        ie = y.get("interest_expense")
        fe = (m.fiscal_ends or {}).get(fy)
        if ie is None or ie.is_na() or not fe:
            continue
        try:
            import datetime as _dt
            kandidaten.append((_dt.date.fromisoformat(str(fe)[:10]),
                               abs(float(ie.value)), tax_rate(m, fy)))
        except ValueError:
            continue
    for fy, (sv, _tag) in ((getattr(m, "sec_interest", None) or {}).get("FY") or {}).items():
        fe = (m.fiscal_ends or {}).get(int(fy))
        if not fe:
            continue
        try:
            import datetime as _dt
            kandidaten.append((_dt.date.fromisoformat(str(fe)[:10]),
                               abs(float(sv)), tax_rate(m, int(fy))))
        except ValueError:
            continue
    schaetzung = nan
    for p in pending:
        end = _fcff_quellen_ende(m, p["label"])
        if end is None:
            return False, nan
        treffer = [(d, v, t) for d, v, t in kandidaten
                   if d <= end and (end - d).days <= max_alter * 366]
        if not treffer:
            return False, nan
        d, v, t = max(treffer, key=lambda x: x[0])
        if t != t:
            t = t_norm
        t = min(max(t, t_min), t_max)
        nach_steuer = v * (1.0 - t)
        if not nach_steuer < grenze * p["value"]:
            return False, nan
        if nach_steuer > (schaetzung if schaetzung == schaetzung else float("-inf")):
            schaetzung = nach_steuer
    return True, schaetzung


def compute_dcf(m: TickerMetrics, cfg: dict, fx=None, fcf_mode: str | None = None) -> DcfResult:
    """Berechnet den DCF fuer einen Ticker nach Config-Block `cfg['dcf']`.

    fx: optionaler FX-Converter (wie in data/metrics.py) — der normalisierte
    FCF und die Nettoverschuldung werden in die Marktkap.-Waehrung
    konvertiert, damit Kurs und Wert konsistent sind.

    fcf_mode: Startwert des FCF -- "auto" (Config `dcf.fcf_mode`, Default:
    TTM-Basis falls plausibel, sonst Ø letzte 3 GJ), "avg3" (Ø letzte 3 GJ),
    "latest" (juengstes GJ), "ttm" (TTM Ø: Ø aus TTM + 2 nicht
    ueberlappenden GJ, Rueckfall auf Ø3J wenn TTM nicht verwendbar),
    "ttm_only" (v27: reiner TTM-Start-FCF nach SBC, ohne Ø; Rueckfall auf
    Ø3J wenn TTM nicht verwendbar).

    Innerer Wert (Equity) = Unternehmenswert (Barwert FCFs + Barwert
    Terminal Value) - Nettoverschuldung OHNE Kundengelder. Wert je Aktie =
    Innerer Wert / Aktien. Nettoverschuldung unbekannt (NaN) -> DCF nicht
    belastbar: Wert je Aktie/MoS = NaN (v66).
    """
    dcfg = cfg["dcf"]
    nan = float("nan")
    mode = (fcf_mode or dcfg.get("fcf_mode", "auto")).lower()
    if mode not in ("auto", "avg3", "latest", "ttm", "ttm_only"):
        mode = "auto"
    result = DcfResult(
        fcf_normalized=nan, fcf_years_used=0, revenue_cagr=nan,
        discount_rate=float(dcfg.get("discount_rate", 0.09)),
        terminal_growth=float(dcfg.get("terminal_growth", 0.025)),
        shares=nan, fcf_mode=mode,
    )

    # --- normalisierter FCF nach SBC (Startwert je fcf_mode) ----------------
    # v69-Fix: ttm_only faellt NIE auf Ø3J zurueck (Entscheidungstabelle):
    # kein verwendbarer TTM-Start -> DCF n/a. auto/ttm/avg3 behalten den
    # bisherigen Ø3J-Rueckfall, aber transparent via ttm_window_status.
    start = None
    ttm_requested = mode in ("auto", "ttm", "ttm_only")
    ttm_skipped = False
    if mode in ("auto", "ttm"):
        start = _ttm_fcf_start(m)
        if start is None:
            ttm_skipped = True
    if start is None and mode == "ttm_only":
        start = _ttm_only_fcf_start(m)  # reiner TTM-Start (ohne Ø)
        if start is None:
            ttm_skipped = True
    if start is None and mode == "latest":
        start = _avg_fcf_start(m, 1)
    start_from_avg3 = False
    if start is None and mode in ("auto", "ttm", "avg3"):
        start = _avg_fcf_start(m, int(dcfg.get("fcf_avg_years", 3)))
        start_from_avg3 = True
    if start is None:
        # v69-Fix: kein FCF bestimmbar -- bei angeforderter TTM-Basis wird
        # die Skipp-Ursache transparent dokumentiert (D_a/kein Ø3J).
        if ttm_skipped and ttm_requested:
            # ttm_only: start=None kann nur "nicht_verfuegbar" bedeuten
            # (bei vorhandenem TTM-Wert liefert _ttm_only_fcf_start immer
            # ein start-dict); ein "geskippt"-Label waere hier irrefuehrend,
            # da ttm_only niemals auf Ø3J zurueckfaellt.
            result.ttm_window_status = (
                "nicht_verfuegbar" if mode == "ttm_only"
                else _ttm_skip_status(m))
        return result  # kein FCF bestimmbar (weder SBC noch Roh-FCF)
    if start_from_avg3 and mode in ("auto", "ttm"):
        # v69-Fix: TTM-Quelle wurde uebersprungen (Ø3J-Fallback aktiv) --
        # Differenzierung geskippt/nicht_verfuegbar fuer DQ/Missing-Grund
        result.ttm_window_status = _ttm_skip_status(m)
    elif not result.ttm_window_status:
        result.ttm_window_status = str(start.get("ttm_window_status", "") or "")

    # --- v70 (FCFF): Start-FCF um Zinsaufwand nach Steuern anheben -----------
    # Der Start-FCF ist OCF - Capex, also NACH Zinsen (levered), waehrend
    # _dcf_scenario die Nettoverschuldung vom EV abzieht -- Verschuldung
    # wurde dadurch DOPPELT bestraft (v61-Symptom LLY/TDG/KDP/AMCR,
    # iv_base 0.10-0.74 USD). fcff hebt je Quelle um
    # |Zinsaufwand| * (1 - Steuersatz) an; discount_rate bleibt als
    # WACC-Naeherung, der ND-Abzug bleibt (jetzt konsistent unlevered).
    fcf_basis_cfg = str(dcfg.get("fcf_basis", "fcff")).strip().lower()
    if fcf_basis_cfg not in ("fcff", "levered_legacy"):
        fcf_basis_cfg = "fcff"
    # v72: Zins-Plan/offene Quellen (nur im fcff-Zweig belegt); die
    # endgueltige Entscheidung laeuft nach der ND-Ermittlung.
    plan = None
    zins_pending = []
    # IFRS-Schutz: Ticker MIT Boersen-Suffix (".L", ".DE", ...; US-GAAP hat
    # keins) bleiben auf levered_legacy -- IAS 7 erlaubt Zinszahlungen im
    # Finanzierungs-Cashflow, der Aufschlag koennte doppelt zaehlen.
    if fcf_basis_cfg == "levered_legacy":
        result.fcf_basis_flag = "levered_legacy"
    elif "." in (m.ticker or ""):
        result.fcf_basis_flag = "legacy_nonUS"
    else:
        result.fcf_basis_flag = "fcff"
        plan, zins_avg, steuer_normiert = _fcff_adjust_start(m, start)
        zins_pending = [p for p in (plan or []) if p["adj"] is None] if plan else []
        if plan is None:
            # keine Quellen (Sicherheit) -- wie v70: n/a
            result.fcff_status = "zinsaufwand_fehlend"
            result.fcf_le0 = (start["fcf"] <= 0
                              if start["fcf"] == start["fcf"] else False)
            result.fcf_normalized = start["fcf"]
            result.fcf_years_used = start["years_used"]
            result.fcf_sources = start["sources"]
            return result
        if not zins_pending:
            # alle Quellen mit Zinsaufwand: wie v70 finalisieren
            result.fcff_interest_sources = [f"{p['label']}:{p['quelle']}" for p in plan]
            result.fcff_interest_added = zins_avg
            result.fcff_steuersatz_normiert = steuer_normiert
            start = _start_from_plan(start, plan)
            result.fcff_status = "ok"
        else:
            # v72: Quellen offen markiert (Zins fehlt auch beim SEC). Der
            # Rohstart laeuft VORLAEUFIG weiter (FX-Konvertierung/ND werden
            # ohnehin benoetigt); die endgueltige Entscheidung trifft nach
            # der ND-Ermittlung (Wesentlichkeitsregel braucht ND/Marktkap.):
            # Regel greift -> Aufschlag 0 + Flag; sonst strikt n/a wie v70.
            result.fcff_steuersatz_normiert = steuer_normiert
    result.sbc_missing = bool(start.get("sbc_missing", False))
    result.fcf_le0 = (start["fcf"] <= 0) if start["fcf"] == start["fcf"] else False
    result.fcf_normalized = start["fcf"]
    result.fcf_years_used = start["years_used"]
    result.fcf_sources = start["sources"]
    if mode in ("auto", "ttm", "ttm_only") and "quarter_end" in start:
        result.fcf_basis = "ttm"
        result.ttm_quarter_end = start["quarter_end"]
    else:
        result.fcf_basis = "annual"

    # --- Umsatz-CAGR ueber verfuegbare Jahre (Basis-Wachstum) ---------------
    rev_series = [m.years[fy]["revenue"] for fy in sorted(m.years)
                  if "revenue" in m.years[fy] and not m.years[fy]["revenue"].is_na()]
    rev_vals = [x.value for x in rev_series if x.value > 0]
    cagr = _cagr(rev_vals) if len(rev_vals) >= 2 else nan
    result.revenue_cagr = cagr

    # --- Aktienanzahl (verwässert, neuestes FY) -----------------------------
    for fy in sorted(m.years, reverse=True):
        sh = m.years[fy].get("shares_diluted")
        if sh is not None and not sh.is_na() and sh.value > 0:
            result.shares = sh.value
            break

    # --- Szenario-Wachstumsraten ---------------------------------------------
    base_cap = float(dcfg.get("base_growth_cap", 0.06))
    bull_mult = float(dcfg.get("bull_growth_multiplier", 1.3))
    bull_cap = float(dcfg.get("bull_growth_cap", 0.10))
    neg_spread = float(dcfg.get("bear_negative_spread", 0.03))  # 
    g_terminal = result.terminal_growth
    if cagr != cagr:  # NaN -> kein Wachstum bestimmbar
        g_bear, g_base, g_bull = 0.0, 0.0, 0.0
    elif cagr < 0:
        # schrumpfendes Unternehmen -- Base = CAGR (negativ),
        # Bear = CAGR - bear_negative_spread, Bull = 0 % (keine Erholung)
        g_bear = cagr - neg_spread
        g_base = cagr
        g_bull = 0.0
    else:
        g_bear = 0.0
        g_base = min(cagr, base_cap)
        g_bull = min(cagr * bull_mult, bull_cap)

    # --- FX: FCF + Nettoverschuldung in Marktkap.-Waehrung -------------------
    mcap = m.market_cap
    if mcap is None or mcap.is_na() or mcap.value <= 0:
        return result

    # --- v62 Aktienbasis: share_basis = current|fy_diluted --------------------
    share_basis = str(dcfg.get("share_basis", "current")).lower()
    if share_basis not in ("current", "fy_diluted"):
        share_basis = "current"
    result.price_basis = share_basis
    kurs_boerse = float("nan")
    if m.price is not None and not m.price.is_na() and m.price.value > 0:
        kurs_boerse = m.price.value
    result.kurs_boerse = kurs_boerse
    shares_eff = result.shares
    if share_basis == "current" and kurs_boerse == kurs_boerse:
        # v69 (F2-B): Waehrungs-/Einheiten-Gate (bekannte Waehrungen,
        # GBp/GBX-Sperre bis Normalisierung, convert_current bei Mismatch).
        # Gate-Fehler -> NaN, KEIN Rueckfall auf FY-Aktien (Rev.-2-Entscheidung).
        shares_eff, gate_status = _shares_current_gate(m, mcap, kurs_boerse, fx)
        result.shares_current_status = gate_status
        if gate_status != "ok":
            shares_eff = float("nan")
    result.shares_current = shares_eff
    converter = fx
    if converter is None:
        from data.metrics import _NoFX
        converter = _NoFX()
    # v69 (F2-B): JEDE Quelle einzeln mit eigener Waehrung/Zeitraum konvertieren
    # (TTM-Fenster aus den verwendeten Quartalen, FY-Fenster aus fiscal_ends),
    # DANN erst mitteln -- Gewichtung/Quellenauswahl unveraendert.
    fcf_conv, fcf_fx_status, fcf_window_basis = _convert_fcf_sources(
        m, start, converter, mcap.currency)
    result.fcf_fx_status = fcf_fx_status
    result.fcf_window_basis = fcf_window_basis
    if math.isnan(fcf_conv):
        return result
    result.fcf_normalized_conv = fcf_conv

    # Nettoverschuldung OHNE Kundengelder (Berichtsw. -> Marktkap.-Waehrung)
    nd_mode = str(dcfg.get("net_debt_mode", "reported")).lower()
    if nd_mode not in ("reported", "incl_sti"):
        nd_mode = "reported"
    result.net_debt_mode = nd_mode
    alt_mode = "incl_sti" if nd_mode == "reported" else "reported"
    nd_rep, nd_src, cd_val, nd_cur, nd_qual, nd_asof = _net_debt_ex_customer_deposits(
        m, result.fcf_basis, net_debt_mode=nd_mode)
    nd_alt, nd_alt_src, _cd_alt, nd_alt_cur, _nd_alt_qual, nd_alt_asof = _net_debt_ex_customer_deposits(
        m, result.fcf_basis, net_debt_mode=alt_mode)
    result.net_debt = nd_rep
    result.net_debt_source = nd_src
    result.customer_deposits = cd_val
    result.net_debt_alt = nd_alt
    result.net_debt_alt_source = nd_alt_src
    # v69 (F2-B): ND ist eine Bestandsgroesse -> Stichtagskurs zum ECHTEN
    # Bilanzstichtag (TTM-Quartalsende bzw. FY-Ende), niemals pauschal
    # latest_fy. Identische Waehrungen benoetigen keinen Kurs (Identity,
    # Stichtag bleibt fuer die Transparenz dokumentiert); unterschiedliche
    # Waehrungen OHNE Stichtag -> NaN (FX-Haertung convert_stock).
    _mc_cur = (mcap.currency or "").upper().strip()
    nd_conv = nan
    if nd_rep == nd_rep and nd_cur:
        if nd_cur.upper().strip() == _mc_cur and nd_cur:
            nd_conv = nd_rep
            result.net_debt_fx_asof = str(nd_asof or "")[:10]
        elif nd_asof:
            nd_conv = converter.convert_stock(nd_rep, nd_cur, mcap.currency, nd_asof)
            result.net_debt_fx_asof = str(nd_asof)[:10] if nd_conv == nd_conv else ""
    nd_alt_conv = nan
    if nd_alt == nd_alt and nd_alt_cur:
        if nd_alt_cur.upper().strip() == _mc_cur and nd_alt_cur:
            nd_alt_conv = nd_alt
        elif nd_alt_asof:
            nd_alt_conv = converter.convert_stock(nd_alt, nd_alt_cur, mcap.currency,
                                                  nd_alt_asof)
    result.net_debt_conv = nd_conv

    # v72: nachgeholte Zinsentscheidung fuer offen markierte Quellen
    # (ND/Marktkap. stehen jetzt bereit; Reihenfolge ansonsten unveraendert)
    if plan is not None and zins_pending:
        ok_wesentlich, schaetzung = _zins_unwesentlich(m, dcfg, zins_pending,
                                                       nd_conv, mcap.value)
        if not ok_wesentlich:
            # Zinsaufwand auch nach SEC unbedeutend-unbekannt: strikt n/a
            # (KEIN stiller Fallback auf levered). Roh-FCF bleibt zur
            # Transparenz dokumentiert, MoS/Szenarien NaN -- exakt wie v70.
            result.fcff_status = "zinsaufwand_fehlend"
            result.fcf_le0 = (start["fcf"] <= 0
                              if start["fcf"] == start["fcf"] else False)
            result.fcf_normalized = start["fcf"]
            result.fcf_years_used = start["years_used"]
            result.fcf_sources = start["sources"]
            return result
        # Wesentlichkeitsregel greift: Aufschlag 0 fuer die offenen Quellen
        # (Flag zins_unwesentlich; Wert eher unterschaetzt = konservativ)
        for p in plan:
            if p["adj"] is None:
                p["adj"] = 0.0
                p["quelle"] = "unwesentlich"
        adj_vals = [p["adj"] for p in plan]
        result.fcff_interest_sources = [f"{p['label']}:{p['quelle']}" for p in plan]
        result.fcff_interest_added = sum(adj_vals) / len(adj_vals)
        result.fcff_zins_schaetzung = schaetzung
        start = _start_from_plan(start, plan)
        result.fcff_status = "ok"
        result.fcf_normalized = start["fcf"]
        result.fcf_sources = start["sources"]
        result.fcf_le0 = (start["fcf"] <= 0) if start["fcf"] == start["fcf"] else False
        # Quellen wurden veraendert -> FX-Konvertierung mit finalen Werten
        # wiederholen (dieselbe Funktion/Stichtage, nur die Quellwerte sind
        # jetzt FCFF-final)
        fcf_conv, fcf_fx_status, fcf_window_basis = _convert_fcf_sources(
            m, start, converter, mcap.currency)
        result.fcf_fx_status = fcf_fx_status
        result.fcf_window_basis = fcf_window_basis
        if math.isnan(fcf_conv):
            return result
        result.fcf_normalized_conv = fcf_conv

    # v66: Status der Nettoverschuldung -- bekannt (inkl. exakt 0) vs.
    # unbekannt vs. unvollstaendig vs. FX-Konvertierung fehlgeschlagen.
    # Eine unbekannte Nettoverschuldung wird nie wie null behandelt; der
    # DCF ist dann nicht belastbar (Equity/Wert je Aktie/MoS = NaN).
    if nd_conv == nd_conv:
        result.net_debt_known = True
        if nd_conv == 0.0:
            result.net_debt_status = "bekannt_null"
        elif nd_conv > 0:
            result.net_debt_status = "bekannt_positiv"
        else:
            result.net_debt_status = "bekannt_negativ"
    elif nd_rep == nd_rep:
        result.net_debt_status = "fx_fehlgeschlagen"
    elif nd_qual == "unvollstaendig":
        result.net_debt_status = "unvollstaendig"
    else:
        result.net_debt_status = "unbekannt"

    for name, g5 in (("bear", g_bear), ("base", g_base), ("bull", g_bull)):
        if result.fcf_le0:
            # Start-FCF <= 0 -> Wert je Aktie in der BERECHNUNG n/a
            # (nicht nur Anzeige); Szenario bleibt mit NaN gefuellt.
            sc = DcfScenario(name=name, growth_rates=_growth_path(g5, g_terminal, dcfg),
                             fcfs=[], pv_fcfs=nan, terminal_value=nan,
                             enterprise_value=nan, value_per_share=nan)
        else:
            sc = _dcf_scenario(name, g5, g_terminal, result.discount_rate, fcf_conv,
                               shares_eff, dcfg, net_debt=nd_conv)
        result.scenarios[name] = sc
        result.scenario_growth[name] = g5  # fuer Erklaertext

    # --- Margin of Safety (Base) ----------------------------------------------
    base = result.scenarios.get("base")
    price = nan
    if share_basis == "current" and kurs_boerse == kurs_boerse and kurs_boerse > 0:
        price = kurs_boerse                      # v62: Boersen-Kurs
    elif result.shares == result.shares and result.shares > 0:
        price = mcap.value / result.shares       # Marktkap.-Basis
    # Start-FCF <= 0 -> MoS in der Berechnung n/a (Base vps ist NaN)
    if (base is not None and not result.fcf_le0
            and base.value_per_share == base.value_per_share and base.value_per_share > 0):
        if price == price and price > 0:
            result.margin_of_safety = 1.0 - price / base.value_per_share

    # --- MoS (Base) mit alternativer Nettoverschuldung (nebengerechnet) ------
    # bei fcf_le0 bleibt mos_base_alt NaN
    if nd_alt_conv == nd_alt_conv and base is not None and not result.fcf_le0:
        ev = base.enterprise_value
        equity_alt = ev - nd_alt_conv if ev == ev else nan
        # v67: effektive Aktienbasis wie die Hauptberechnung (shares_current;
        # bei fy_diluted identisch mit shares) -- sonst wuerde bei
        # share_basis=current der Wert je Aktie auf FY-Aktien, der Kurs aber
        # auf den Boersenkurs gerechnet (Mischbasis).
        vps_alt = equity_alt / result.shares_current if (equity_alt == equity_alt
                                                         and result.shares_current == result.shares_current) else nan
        if vps_alt == vps_alt and vps_alt > 0 and price == price and price > 0:
            result.mos_base_alt = 1.0 - price / vps_alt
    return result


def _merge_grid_points(defaults, configured: float) -> list[float]:
    """v67: Stuetzstellen + konfigurierter Wert, toleranzbasiert dedupliziert.

    set() ist fuer Floats unzuverlaessig: rechnerisch identische Werte koennen
    intern minimal abweichen (z. B. 0.09 vs. 0.09000000000000001) und wuerden
    als doppelte Stuetzstelle im Grid auftauchen. Deduplizierung mit
    Toleranz 1e-9 (Raten liegen in [0, 1]); die fachliche Genauigkeit der
    DCF-Berechnung bleibt unveraendert -- es wird nichts gerundet, nur die
    Grid-Auswahl bereinigt.
    """
    out: list[float] = []
    for v in sorted(float(x) for x in list(defaults) + [float(configured)]):
        if not any(abs(v - u) <= 1e-9 for u in out):
            out.append(v)
    return out


def dcf_comparison_price(dcf: DcfResult, mcap_value: float) -> float:
    """v67: DCF-Vergleichskurs passend zur konfigurierten Aktienbasis.

    share_basis=current -> Boersenkurs (wie compute_dcf); fehlt der
    Boersenkurs, bleibt der Kurs NaN (KEIN Rueckfall auf den FY-Kurs,
    damit keine Entscheidung auf unpassender Basis getroffen wird).
    share_basis=fy_diluted -> Marktkapitalisierung / FY-Aktien (bisheriges
    Verhalten). Rueckgabe NaN, wenn keine Basis verfuegbar ist.
    """
    nan = float("nan")
    if dcf.price_basis == "current":
        if dcf.kurs_boerse == dcf.kurs_boerse and dcf.kurs_boerse > 0:
            return dcf.kurs_boerse
        return nan
    if (dcf.shares == dcf.shares and dcf.shares > 0
            and mcap_value == mcap_value and mcap_value > 0):
        return mcap_value / dcf.shares
    return nan


def sensitivity_mos_base(m: TickerMetrics, cfg: dict, current_price: float | None = None,
                         fx=None,
                         discount_rates=(0.08, 0.09, 0.10, 0.11),
                         terminal_growths=(0.02, 0.025, 0.03)) -> dict:
    """Sensitivitaet des Base-Szenarios: MoS und innerer Wert je Aktie.

    Eingabe: TickerMetrics, cfg, Kurs (current_price; Default: Kurs aus
    Marktkapitalisierung / Aktien wie in compute_dcf), optional fx.

    Pro Kombination (r, g) aus discount_rates x terminal_growths wird der
    Base-Szenario-Pfad (Wachstum aus der CAGR, identische Logik wie
    compute_dcf via _dcf_scenario) mit r/g neu diskontiert.

    Rueckgabe: {"mos": DataFrame-Rows, "vps": DataFrame-Rows} mit je einer
    Zeile pro (r, g): {discount_rate, terminal_growth, value_per_share, mos}.
    Zwei Tabellen: MoS = 1 - Kurs / Wert je Aktie, und Wert je Aktie.
    """
    import pandas as pd

    dcfg = cfg["dcf"]
    nan = float("nan")
    base_res = compute_dcf(m, cfg, fx=fx)
    if base_res.fcf_normalized != base_res.fcf_normalized:
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}
    mcap = m.market_cap
    if mcap is None or mcap.is_na() or mcap.value <= 0:
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}
    # v67: effektive Aktienbasis wie die Hauptberechnung (shares_current;
    # bei fy_diluted identisch mit shares). Fehlende FY-Aktien sind bei
    # share_basis=current kein Abbruchgrund, wenn aktien_aktuell existiert.
    if (base_res.shares_current != base_res.shares_current
            or base_res.shares_current <= 0):
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}

    # Kurs: Parameter, sonst aus Marktkap./Aktien (wie compute_dcf)
    if current_price is not None and current_price == current_price and current_price > 0:
        price = float(current_price)
    elif (str(dcfg.get("share_basis", "current")).lower() == "current"
          and base_res.kurs_boerse == base_res.kurs_boerse and base_res.kurs_boerse > 0):
        # v67: Default-Kurs konsistent zur konfigurierten Aktienbasis
        price = base_res.kurs_boerse
    else:
        price = mcap.value / base_res.shares_current
    if price != price or price <= 0:
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}

    # v69 (F2-B): Start-FCF + Nettoverschuldung in Marktkap.-Waehrung --
    # WIEDERVERWENDUNG der bereits in compute_dcf konvertierten Werte
    # (fcf_normalized_conv / net_debt_conv). Damit nutzt die Sensitivitaet
    # GARANTIERT dieselben FX- und Periodenregeln (je Quelle eigene
    # Waehrung, TTM-Fenster, ND-Stichtagskurs) -- keine abweichende
    # Parallelkonvertierung (z.B. Einzelkurs/Jahresend) mehr moeglich.
    fcf_conv = base_res.fcf_normalized_conv
    if fcf_conv != fcf_conv:
        return {"mos": pd.DataFrame(), "vps": pd.DataFrame()}
    nd_conv = base_res.net_debt_conv

    # Base-Wachstum (min(CAGR, base_growth_cap)); identisch zu compute_dcf
    cagr = base_res.revenue_cagr
    base_cap = float(dcfg.get("base_growth_cap", 0.06))
    g5 = 0.0 if cagr != cagr else min(cagr, base_cap)

    # v67: Die konfigurierten Parameter (dcf.discount_rate/terminal_growth)
    # sind immer Teil des Grids -- sonst fehlte der konfigurierte Base Case
    # in der Tabelle, sobald er von den Default-Stuetzstellen abweicht.
    r0 = float(dcfg.get("discount_rate", 0.09))
    g0 = float(dcfg.get("terminal_growth", 0.025))
    rates_grid = _merge_grid_points(discount_rates, r0)
    growths_grid = _merge_grid_points(terminal_growths, g0)

    mos_rows, vps_rows = [], []
    for r in rates_grid:
        for g in growths_grid:
            if r <= g:
                mos_rows.append({"discount_rate": r, "terminal_growth": g,
                                 "value_per_share": nan, "mos": nan})
                vps_rows.append({"discount_rate": r, "terminal_growth": g,
                                 "value_per_share": nan})
                continue
            sc = _dcf_scenario("base", g5, g, r, fcf_conv, base_res.shares_current,
                               dcfg, net_debt=nd_conv)
            vps = sc.value_per_share
            mos = 1.0 - price / vps if (vps == vps and vps > 0) else nan
            mos_rows.append({"discount_rate": r, "terminal_growth": g,
                             "value_per_share": vps, "mos": mos})
            vps_rows.append({"discount_rate": r, "terminal_growth": g,
                             "value_per_share": vps})
    return {"mos": pd.DataFrame(mos_rows), "vps": pd.DataFrame(vps_rows)}


def is_buy_candidate(quality: float, dcf: DcfResult, current_price: float, cfg: dict,
                     mos_rule: float | None = None) -> bool:
    """Kaufkandidat: Quality >= buy_quality_min UND MoS (Base) >=
    buy_margin_of_safety_min UND Bear-Szenario >= Kurs x buy_bear_floor.

    Alle Schwellen aus config.yaml (Block `dcf`). Quality statt Gesamtscore
    (v21): die Bewertungsbedingungen (MoS, Bear) tragen den Value-Anteil.

    mos_rule (v26): das fuer die MoS-Bedingung verwendete MoS — Default
    dcf.margin_of_safety (aktiver fcf_mode). Die Pipeline uebergibt hier
    das nach `dcf.mos_min_mode` aggregierte MoS (min/avg3/latest/ttm/auto).
    """
    dcfg = cfg["dcf"]
    mos = dcf.margin_of_safety if mos_rule is None else mos_rule
    if quality != quality or mos != mos:
        return False
    if current_price != current_price or current_price <= 0:
        return False
    if quality < float(dcfg.get("buy_quality_min", 60)):
        return False
    if mos < float(dcfg.get("buy_margin_of_safety_min", 0.30)):
        return False
    bear = dcf.scenarios.get("bear")
    if bear is None or bear.value_per_share != bear.value_per_share:
        return False
    floor = float(dcfg.get("buy_bear_floor", 0.80))
    return bear.value_per_share >= floor * current_price


# --- v73: impliziter IRR (Diskontsatz auf EV-Basis, FCFF, ND abgezogen) ---

def implied_irr(m: TickerMetrics, cfg: dict, mode: str | None = None, fx=None,
               base: DcfResult | None = None) -> "tuple[float, str]":
    """Impliziter IRR = Diskontsatz r, bei dem der Wert je Aktie des
    Base-Szenarios dem Kurs entspricht (vps(r) = Kurs).

    BENENNUNG (v73): Das ist der implizite Diskontsatz auf EV-Basis
    (FCFF, Nettoverschuldung abgezogen) -- KEINE Eigenkapitalrendite
    eines Anlegers (Dividenden/Rueckkaeufe sind nicht enthalten).

    Methodik (keine duplizierte Logik):
    - Basiswerte (Start-FCF je Modus inkl. FCFF-Aufschlag/FX, ND-Abzug,
      Aktienbasis, Base-Wachstum) stammen aus compute_dcf(m, cfg,
      fcf_mode=mode) -- uebernimmt `base`, wenn der Aufrufer das
      Modus-Ergebnis bereits berechnet hat (Pipeline: Wiederverwendung).
    - Bisektion auf f(r) = value_per_share(r) - Kurs (NICHT auf die
      MoS-Formel; Vorzeichenproblem aus der v71-Diagnose vermeiden),
      je Iteration ueber _dcf_scenario mit identischem Wachstumspfad.
    - Intervall [terminal_growth + dcf.irr_untergrenze_offset,
      dcf.irr_obergrenze]. Keine Wurzel: NaN mit Grund, NIE 0:
      f(obergrenze) > 0 -> r liegt ueber der Obergrenze -> "irr>40%";
      f(untergrenze) < 0 -> r liegt unter der Untergrenze ->
      "irr<untergrenze".
    Rueckgabe (irr | NaN, grund: "" wenn gefunden)."""
    nan = float("nan")
    dcfg = cfg["dcf"]
    base_res = base if base is not None else compute_dcf(m, cfg, fx=fx,
                                                         fcf_mode=mode)
    mcap = m.market_cap
    if (base_res is None or mcap is None or mcap.is_na() or mcap.value <= 0
            or base_res.fcf_normalized_conv != base_res.fcf_normalized_conv
            or base_res.net_debt_conv != base_res.net_debt_conv):
        return nan, "basis_n/a"
    shares = base_res.shares_current if (base_res.shares_current ==
                                         base_res.shares_current
                                         and base_res.shares_current > 0) \
        else nan
    if shares != shares:
        return nan, "basis_n/a"
    # Kurs: derselbe Preisbegriff wie compute_dcf/sensitivity (v67)
    if (str(dcfg.get("share_basis", "current")).lower() == "current"
            and base_res.kurs_boerse == base_res.kurs_boerse
            and base_res.kurs_boerse > 0):
        price = base_res.kurs_boerse
    else:
        price = mcap.value / shares
    if price != price or price <= 0:
        return nan, "basis_n/a"
    # Base-Wachstum identisch zu compute_dcf / sensitivity_mos_base
    cagr = base_res.revenue_cagr
    base_cap = float(dcfg.get("base_growth_cap", 0.06))
    g5 = 0.0 if cagr != cagr else min(cagr, base_cap)
    g_t = base_res.terminal_growth
    fcf_conv = base_res.fcf_normalized_conv
    nd_conv = base_res.net_debt_conv
    offset = float(dcfg.get("irr_untergrenze_offset", 0.001))
    oberg = float(dcfg.get("irr_obergrenze", 0.40))
    max_iter = int(dcfg.get("irr_max_iterationen", 60))
    toleranz = float(dcfg.get("irr_toleranz", 1e-6))
    lo, hi = g_t + offset, oberg

    def f(r: float) -> float:
        sc = _dcf_scenario("base", g5, g_t, r, fcf_conv, shares, dcfg,
                           net_debt=nd_conv)
        return (sc.value_per_share - price) if sc.value_per_share == \
            sc.value_per_share else nan

    f_hi = f(hi)
    if f_hi != f_hi:
        return nan, "basis_n/a"
    if f_hi > 0:
        # selbst bei 40 % Diskont ist der innere Wert ueber dem Kurs
        # -> der gesuchte r liegt OBERHALB des Intervalls
        return nan, "irr>40%"
    f_lo = f(lo)
    if f_lo != f_lo:
        return nan, "basis_n/a"
    if f_lo < 0:
        # bereits an der Untergrenze (terminal_growth + offset) ist der
        # innere Wert UNTER dem Kurs -> r liegt unterhalb des Intervalls
        return nan, "irr<untergrenze"
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if fm != fm:
            fm = -1.0   # jenseits der Wurzel nicht definiert -> weiter nach oben
        elif abs(fm) < toleranz * price:
            return mid, ""
        if fm > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0, ""
