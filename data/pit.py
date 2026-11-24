# v75: Abfrage-API fuer den Point-in-time-Fundamentalspeicher pit.db.
# Nur Abfragen, KEINE Scoring-Logik, KEIN Adapter.
#
# As-of-Semantik (Freigabe v75):
# Gueltige Fassung je (cik10, tag, unit, period_start, period_end) ist die
# juengste Zeile mit date(filed_date, '+1 day') <= Stichtag, bestimmt per
# ROW_NUMBER() OVER (PARTITION BY ... ORDER BY filed_date DESC, accn DESC)
# mit rn = 1. accn ist Tiebreaker (10-K vs. 10-K/A am gleichen filed_date).

import sqlite3
from typing import Iterable, Sequence

import pandas as pd

from data.pit_schema import (
    ALLE_FAMILIEN, DURATION_FAMILIEN, PIT_DB_PFAD, ensure_schema,
)

FY_DAUER = (340, 380)  # Tage fuer fy_series (52/53-Wochen-Jahre)


def verbinde(pfad: str = PIT_DB_PFAD) -> sqlite3.Connection:
    con = sqlite3.connect(pfad)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    return con


_AS_OF_SQL = """
SELECT * FROM (
    SELECT cik10, tag, unit, period_start, period_end, filed_date, form,
           accn, value,
           ROW_NUMBER() OVER (
               PARTITION BY cik10, tag, unit, period_start, period_end
               ORDER BY filed_date DESC, accn DESC
           ) AS rn
    FROM pit_facts
    WHERE cik10 = ?
      AND date(filed_date, '+1 day') <= ?
      AND tag IN ({plaetze})
) WHERE rn = 1
"""


def facts_as_of(con: sqlite3.Connection, cik10: str, stichtag: str,
                tags: Iterable[str]) -> pd.DataFrame:
    """Alle as-of gueltigen Fakten zu den Tags. stichtag = 'YYYY-MM-DD'.
    Gibt einen DataFrame mit Spalten
    cik10, tag, unit, period_start, period_end, filed_date, form, accn, value
    zurueck."""
    tags = sorted(set(tags))
    if not tags:
        return pd.DataFrame(columns=[
            "cik10", "tag", "unit", "period_start", "period_end",
            "filed_date", "form", "accn", "value"])
    sql = _AS_OF_SQL.format(plaetze=",".join("?" * len(tags)))
    df = pd.read_sql_query(sql, con, params=[cik10, stichtag, *tags])
    return df


def _familie_tags(familie: str) -> Sequence[str]:
    if familie not in ALLE_FAMILIEN:
        raise ValueError("unbekannte Familie: %s" % familie)
    return [tag for tag, _prio in ALLE_FAMILIEN[familie]]


def _prio(familie: str) -> dict:
    return dict(ALLE_FAMILIEN[familie])


def instant_as_of(con: sqlite3.Connection, cik10: str, stichtag: str,
                   familie: str) -> pd.DataFrame:
    """Instant-Familie (equity, debt, cash, shares_outstanding...) as-of:
    je end-Datum die gueltige Fassung, sortiert nach period_end."""
    tags = _familie_tags(familie)
    df = facts_as_of(con, cik10, stichtag, tags)
    if df.empty:
        return df
    df = df.sort_values(["period_end", "tag"])
    return df.reset_index(drop=True)


def instant_wert(con: sqlite3.Connection, cik10: str, stichtag: str,
                 familie: str) -> dict:
    """Juengsten Instant-Wert der Familie (hoechstes period_end <= stichtag)
    mit Tag-Prioritaet (erste Wahl der Familie gewinnt bei Gleichstand
    des period_end). Rueckgabe {'value', 'tag', 'period_end', 'filed_date'}
    oder {'value': float('nan'), ...} wenn keine Daten."""
    tags = _familie_tags(familie)
    df = facts_as_of(con, cik10, stichtag, tags)
    if df.empty:
        return {"value": float("nan"), "tag": None, "period_end": None,
                "filed_date": None}
    df = df[df["period_end"] <= stichtag]
    if df.empty:
        return {"value": float("nan"), "tag": None, "period_end": None,
                "filed_date": None}
    # juengstes end; bei mehreren Tags am gleichen end gewinnt die Prioritaet
    prio = _prio(familie)
    end_max = df["period_end"].max()
    kand = df[df["period_end"] == end_max].copy()
    kand["_p"] = kand["tag"].map(prio)
    kand = kand.sort_values(["_p"]).iloc[0]
    return {"value": float(kand["value"]), "tag": kand["tag"],
            "period_end": end_max, "filed_date": kand["filed_date"]}


def fy_series(con: sqlite3.Connection, cik10: str, stichtag: str,
              familie: str, n_jahre: int = 10) -> pd.DataFrame:
    """FY-Serie einer Duration-Familie as-of: je Geschaeftsjahr (definiert
    ueber period_end) die gueltige Fassung des Tags mit hoechster Prioritaet,
    Dauer 340-380 Tage (52/53-Wochen-Jahre mit Toleranz sind durch das
    Intervall abgedeckt).
    Rueckgabe-DataFrame: fy (period_end), tag, unit, value, filed_date, form,
    accn. Nur Zeilen mit Wert vorhanden (kein Auffuellen, kein Falsch-0)."""
    if familie not in DURATION_FAMILIEN:
        raise ValueError("keine Duration-Familie: %s" % familie)
    tags = _familie_tags(familie)
    df = facts_as_of(con, cik10, stichtag, tags)
    if df.empty:
        return pd.DataFrame(columns=["fy", "tag", "unit", "value",
                                     "filed_date", "form", "accn"])
    dauer_ok = []
    for _, z in df.iterrows():
        ps, pe = z["period_start"], z["period_end"]
        if not ps:
            continue
        try:
            tage = (pd.Timestamp(pe) - pd.Timestamp(ps)).days
        except Exception:
            continue
        if FY_DAUER[0] <= tage <= FY_DAUER[1]:
            dauer_ok.append(z)
    if not dauer_ok:
        return pd.DataFrame(columns=["fy", "tag", "unit", "value",
                                     "filed_date", "form", "accn"])
    dauer = pd.DataFrame(dauer_ok)
    prio = _prio(familie)
    dauer["_p"] = dauer["tag"].map(prio)
    # je fy (period_end) der Tag mit hoechster Prioritaet
    dauer = dauer.sort_values(["period_end", "_p", "filed_date"])
    serie = dauer.drop_duplicates(subset=["period_end"], keep="first")
    serie = serie.sort_values("period_end", ascending=False).head(n_jahre)
    serie = serie.rename(columns={"period_end": "fy"})
    spalten = ["fy", "tag", "unit", "value", "filed_date", "form", "accn"]
    return serie[spalten].reset_index(drop=True)
