"""Score-Berechnung: lineare Interpolation, Block-Scores, Quality/Value-Gruppen.

Eingang: dict[Kriterium, CriterionResult] (value, is_na, years_available, thin).

Regeln:
- Kriterium mit is_na=True wird aus dem Score herausgenommen; die uebrigen
  Gewichte werden renormalisiert (gewichteter Mittelwert der verbleibenden).
- Quality-Score und Value-Score werden getrennt berechnet (Config-Block
  `score_groups`: quality_blocks / value_blocks). Gesamtscore =
  weights.quality * Quality + weights.value * Value (renormalisiert, falls
  eine Gruppe komplett n/a ist).
- `coverage` = Anteil der bewerteten Kriterien-Gewichte am Gesamtgewicht
  ("Datenabdeckung", z. B. 0.80 -> 80 %).
- Block mit > 30 % fehlender Daten: Flag 'duenn'.
- Kriterium mit thin=True (< data.thin_years Jahre) wird als 'Daten duenn'
  markiert.
- Alle Scores werden auf 1 Nachkommastelle gerundet.
"""

from __future__ import annotations

from scoring.criteria import CriterionResult


def interpolate(value: float, direction: str, full: float, zero: float) -> float:
    """Punkte (0-100) linear zwischen Null- und Voll-Punkte-Schwelle.

    direction 'higher': mehr ist besser (Wert >= full -> 100, <= zero -> 0).
    direction 'lower': weniger ist besser (Wert <= full -> 100, >= zero -> 0).
    Ungueltige direction -> ValueError (nie stillschweigend 'lower').
    """
    if direction not in ("higher", "lower"):
        raise ValueError(f"ungueltige direction {direction!r} (erwartet 'higher'|'lower')")
    if value is None or value != value:  # None oder NaN
        return float("nan")
    if direction == "higher":
        if value >= full:
            return 100.0
        if value <= zero:
            return 0.0
        return 100.0 * (value - zero) / (full - zero) if full != zero else float("nan")
    if value <= full:
        return 100.0
    if value >= zero:
        return 0.0
    return 100.0 * (zero - value) / (zero - full) if zero != full else float("nan")


def _r1(x: float) -> float:
    """Auf 1 Nachkommastelle runden; NaN bleibt NaN."""
    return round(x, 1) if x == x else float("nan")


def _weighted_mean(entries: list[tuple[float, float]]) -> float:
    """Gewichteteter Mittelwert; leere Liste -> NaN."""
    den = sum(w for w, _ in entries)
    if not den:
        return float("nan")
    return sum(w * p for w, p in entries) / den


def validate_criteria_cfg(criteria_cfg: dict) -> None:
    """v65: Konfiguration pruefen -- ungueltige Eintraege loesen ValueError aus.

    Pro Kriterium: weight > 0 (negativ/0 -> Fehler), direction in
    {higher, lower}, full/zero numerisch und konsistent (higher: full > zero;
    lower: full < zero). Fehlende Kriterienkonfiguration wird in score_ticker
    als ValueError gemeldet.
    """
    problems = []
    for name, c in (criteria_cfg or {}).items():
        w = c.get("weight")
        if not isinstance(w, (int, float)) or isinstance(w, bool) or w <= 0:
            problems.append(f"criteria.{name}.weight={w!r} (muss > 0 sein)")
        d = c.get("direction")
        if d not in ("higher", "lower"):
            problems.append(f"criteria.{name}.direction={d!r} (muss 'higher'|'lower' sein)")
        f, z = c.get("full"), c.get("zero")
        for k, v in (("full", f), ("zero", z)):
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v != v:
                problems.append(f"criteria.{name}.{k}={v!r} (muss numerisch sein)")
        if d in ("higher", "lower") and isinstance(f, (int, float)) and isinstance(z, (int, float)):
            if d == "higher" and not f > z:
                problems.append(f"criteria.{name}: direction 'higher' braucht full > zero ({f} vs {z})")
            if d == "lower" and not f < z:
                problems.append(f"criteria.{name}: direction 'lower' braucht full < zero ({f} vs {z})")
    if problems:
        raise ValueError("ungueltige Kriterien-Konfiguration: " + "; ".join(problems))


def _is_missing(crit, pts: float) -> bool:
    """v65: eindeutige Missing-Semantik -- is_na ODER None ODER NaN-Punkte.
    Einheitlich fuer Coverage, N/A-Anteil und Renormalisierung verwendet."""
    return bool(crit.is_na) or crit.value is None or pts != pts


def score_ticker(criteria_values: dict[str, CriterionResult], criteria_cfg: dict,
                 groups_cfg: dict | None = None) -> dict:
    """Gesamt-/Block-/Gruppen-Scores mit Renormalisierung bei n/a.

    groups_cfg (optional, config.yaml Block `score_groups`):
        quality_blocks: [profitability, stability, cashflow, moat]
        value_blocks: [valuation]
        weights: {quality: 0.75, value: 0.25}
    Ohne groups_cfg entfaellt die Gruppenebene (abwaertskompatibel).
    """
    # v65: Konfiguration validieren (fehlende/ungueltige Eintraege -> ValueError)
    validate_criteria_cfg(criteria_cfg)
    missing_cfg = [n for n in criteria_values if n not in criteria_cfg]
    if missing_cfg:
        raise ValueError(f"fehlende Kriterien-Konfiguration fuer: {', '.join(sorted(missing_cfg))}")

    result = {
        "total": float("nan"), "blocks": {}, "block_flags": {}, "details": {},
        "thin_criteria": [], "quality": float("nan"), "value": float("nan"),
        "coverage": float("nan"),
        "quality_coverage": float("nan"),  # Abdeckung NUR der Quality-Bloecke
        "value_coverage": float("nan"),    # v65: Abdeckung NUR der Value-Bloecke
        # v65: ungerundete Rohwerte fuer Schwellenwertpruefungen (Anzeige gerundet)
        "coverage_raw": float("nan"), "quality_coverage_raw": float("nan"),
        "value_coverage_raw": float("nan"),
    }
    blocks: dict[str, list[tuple[float, float, bool]]] = {}

    total_weight = 0.0
    covered_weight = 0.0
    # Quality-/Value-Abdeckung separat -- nur Kriterien der jeweiligen Bloecke
    q_blocks = set((groups_cfg or {}).get("quality_blocks", []))
    v_blocks = set((groups_cfg or {}).get("value_blocks", []))
    q_total_weight = 0.0
    q_covered_weight = 0.0
    v_total_weight = 0.0
    v_covered_weight = 0.0

    for name, crit in criteria_values.items():
        cfg = criteria_cfg[name]
        block = cfg["block"]
        weight = cfg["weight"]
        total_weight += weight
        pts = float("nan") if crit.is_na else interpolate(crit.value, cfg["direction"], cfg["full"], cfg["zero"])
        missing = _is_missing(crit, pts)   # v65: is_na ODER None ODER NaN
        blocks.setdefault(block, []).append((weight, pts, missing))
        if not missing:
            covered_weight += weight
        if block in q_blocks:
            q_total_weight += weight
            if not missing:
                q_covered_weight += weight
        if block in v_blocks:
            v_total_weight += weight
            if not missing:
                v_covered_weight += weight
        if crit.thin:
            result["thin_criteria"].append(name)
        result["details"][name] = {
            "value": crit.value, "points": pts, "na": crit.is_na, "missing": missing,
            "years_available": crit.years_available, "thin": crit.thin,
        }

    # Datenabdeckung = Anteil bewerteter Gewichte (ROH, ungerundet fuer Schwellen)
    cov_raw = covered_weight / total_weight if total_weight else float("nan")
    q_cov_raw = q_covered_weight / q_total_weight if q_total_weight else float("nan")
    v_cov_raw = v_covered_weight / v_total_weight if v_total_weight else float("nan")
    result["coverage_raw"] = cov_raw
    result["quality_coverage_raw"] = q_cov_raw
    result["value_coverage_raw"] = v_cov_raw
    result["coverage"] = _r1(cov_raw)          # Anzeige gerundet
    result["quality_coverage"] = _r1(q_cov_raw)
    result["value_coverage"] = _r1(v_cov_raw)

    # Block-Scores (renormalisiert auf nicht-fehlende Kriterien je Block);
    # v65: N/A-Anteil gewichtet (Summe der Gewichte), konsistent zur Coverage
    block_scores: dict[str, float] = {}
    block_weights: dict[str, float] = {}
    for block, entries in blocks.items():
        w_all = sum(w for w, _, _ in entries)
        w_na = sum(w for w, _, na in entries if na)
        na_share = w_na / w_all if w_all else 0.0
        scored = [(w, p) for w, p, na in entries if not na and p == p]
        bscore = _weighted_mean(scored)
        block_scores[block] = bscore
        block_weights[block] = w_all
        result["blocks"][block] = _r1(bscore)
        result["block_flags"][block] = "duenn" if na_share > 0.30 else ""

    # Gruppen-Scores: Bloecke gewichtet mit der Summe ihrer Kriteriengewichte
    # (v65; vorher gleichgewichtet mit 1.0 -- Doku sagte 'gewichtete Summe')
    if groups_cfg:
        q_blocks_l = groups_cfg.get("quality_blocks", [])
        v_blocks_l = groups_cfg.get("value_blocks", [])
        gw = groups_cfg.get("weights", {"quality": 0.75, "value": 0.25})
        q_entries = [(block_weights[b], block_scores[b]) for b in q_blocks_l
                     if b in block_scores and block_scores[b] == block_scores[b]]
        v_entries = [(block_weights[b], block_scores[b]) for b in v_blocks_l
                     if b in block_scores and block_scores[b] == block_scores[b]]
        result["quality"] = _r1(_weighted_mean(q_entries))
        result["value"] = _r1(_weighted_mean(v_entries))
        # v65: Value n/a, wenn VALUE-Abdeckung (ungerundet) < min_coverage_value --
        # vorher wurde die GESAMT-Coverage geprueft (Quality konnte Value decken)
        min_cov = float(groups_cfg.get("min_coverage_value", 0.70))
        if v_cov_raw == v_cov_raw and v_cov_raw < min_cov:
            result["value"] = float("nan")
        # Quality n/a nur, wenn QUALITY-Abdeckung (ungerundet) < min_coverage_quality
        min_cov_q = float(groups_cfg.get("min_coverage_quality", 0.70))
        if q_cov_raw == q_cov_raw and q_cov_raw < min_cov_q:
            result["quality"] = float("nan")

        # Gesamtscore aus Gruppen, renormalisiert falls eine Gruppe n/a
        g_entries = []
        if result["quality"] == result["quality"]:
            g_entries.append((gw.get("quality", 0.75), result["quality"]))
        if result["value"] == result["value"]:
            g_entries.append((gw.get("value", 0.25), result["value"]))
        result["total"] = _r1(_weighted_mean(g_entries))
    else:
        # Fallback ohne Gruppen-Config: Gesamtlogik ueber Blockgewichte
        entries = [(block_weights[b], block_scores[b]) for b in block_scores
                   if block_scores[b] == block_scores[b]]
        result["total"] = _r1(_weighted_mean(entries))

    return result
