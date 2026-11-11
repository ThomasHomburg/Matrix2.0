"""Zentraler Schema-Guard (db_schema.py) -- ensure_schema(con).

Problem: schema.sql legt Tabellen nur mit CREATE TABLE IF NOT EXISTS an und
migriert nicht -- eine Alt-DB mit einer Tabelle im alten Schema (z. B. decisions
ohne Spalte korb) bricht Abfragen mit "no such column" ab (z. B. app.py Seite 5).

ensure_schema(con) vergleicht je Tabelle in schema.sql die Soll-Spalten mit
PRAGMA table_info und ergaenzt fehlende Spalten per ALTER TABLE ADD COLUMN
(Default aus schema.sql). Vorhandene Daten bleiben erhalten. Reihenfolge ist
sicher in jedem Zustand: erst ALTER der vorhandenen Tabellen, dann executescript
(legt fehlende Tabellen + Indizes an -- erst danach existieren alle Spalten, auf
die Indizes zeigen).

Nicht migrierbar per ALTER (geaenderter Primaerschluessel oder Typaenderung einer
vorhandenen Spalte): RuntimeError mit klarer Meldung + Reset-Befehl -- kein
stiller Datenverlust.

Aufruf: decisions.py, live_update.py, smart_money.connect(), trends.connect(),
app.py Seite 5 (load_live_overview).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path

logger = logging.getLogger("value_screener.db_schema")

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_TABLE_CONSTRAINT = re.compile(
    r"^\s*(PRIMARY\s+KEY|UNIQUE|CHECK|FOREIGN\s+KEY|CONSTRAINT)\b", re.IGNORECASE)
_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"'`]?(\w+)[\"'`]?\s*\(", re.IGNORECASE)


def parse_schema_tables(sql_text: str) -> dict[str, list[tuple[str, str]]]:
    """Parst schema.sql: {tablename: [(spaltenname, decl)]} -- nur CREATE TABLE
    Bloecke; Tabellen-Constraints (PRIMARY KEY (...), UNIQUE (...), ...) werden
    uebersprungen; Zeilenkommentare (-- ...) entfernt."""
    # Zeilenkommentare entfernen (keine -- in Strings in unserem schema.sql)
    text = re.sub(r"--[^\n]*", "", sql_text)
    tables: dict[str, list[tuple[str, str]]] = {}
    for m in _CREATE_RE.finditer(text):
        name = m.group(1)
        start = m.end()          # Position nach der oeffnenden Klammer
        depth = 1
        i = start
        while i < len(text) and depth:
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
            i += 1
        body = text[start:i - 1]
        # Top-Level-Kommas splitten (Klammern in Deklarationen beachten)
        parts, depth, cur = [], 0, []
        for ch in body:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append("".join(cur))
        cols: list[tuple[str, str]] = []
        for part in parts:
            part = part.strip()
            if not part or _TABLE_CONSTRAINT.match(part):
                continue
            toks = part.split(None, 1)
            col = toks[0].strip('"`[]')
            decl = toks[1].strip() if len(toks) > 1 else ""
            cols.append((col, decl))
        tables[name] = cols
    return tables


def _norm_type(decl: str) -> str:
    return (decl.split()[0] if decl.split() else "").upper()


def _decl_pk(decl: str) -> int:
    return 1 if re.search(r"PRIMARY\s+KEY", decl, re.IGNORECASE) else 0


def _alter_decl(decl: str) -> str:
    """Decl fuer ALTER TABLE ADD COLUMN: NOT NULL ohne DEFAULT ist in SQLite nicht
    erlaubt -- typgerechten Default anhaengen (TEXT -> '', numerisch -> 0)."""
    d = decl.upper()
    if "NOT NULL" in d and "DEFAULT" not in d:
        typ = _norm_type(decl)
        if typ in ("INTEGER", "REAL"):
            return decl + " DEFAULT 0"
        return decl + " DEFAULT ''"
    return decl


def ensure_schema(con: sqlite3.Connection, schema_path: Path = SCHEMA_PATH) -> dict:
    """Gleicht die DB mit schema.sql ab. Rueckgabe: Report je Tabelle
    {"created": bool, "added": [spalten]}. Daten bleiben erhalten.

    - Tabelle fehlt -> anlegen (executescript schema.sql, IF NOT EXISTS).
    - Spalte fehlt -> ALTER TABLE ADD COLUMN <col> <decl> (Default aus schema.sql).
    - Spalte vorhanden, aber Typ oder PK abweichend -> RuntimeError mit Meldung
      und Reset-Befehl (kein stiller Datenverlust)."""
    sql = Path(schema_path).read_text(encoding="utf-8")
    tables = parse_schema_tables(sql)
    existing = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    report: dict[str, dict] = {}
    # 1) vorhandene Tabellen: fehlende Spalten per ALTER ergaenzen, Typ/PK pruefen
    for name, cols in tables.items():
        if name not in existing:
            continue
        have = {r[1]: r for r in con.execute(f"PRAGMA table_info({name})")}
        added: list[str] = []
        for col, decl in cols:
            if col not in have:
                con.execute(f"ALTER TABLE {name} ADD COLUMN {col} {_alter_decl(decl)}")
                added.append(col)
                logger.info("ensure_schema: %s.%s per ALTER ergaenzt (%s)", name, col, decl)
            else:
                row = have[col]
                db_type = (row[2] or "").upper()
                want_type = _norm_type(decl)
                if want_type and db_type and db_type != want_type:
                    raise RuntimeError(
                        f"Schema-Guard: Tabelle {name}, Spalte {col}: Typ geaendert "
                        f"(DB: {db_type}, schema.sql: {want_type}) -- nicht per ALTER migrierbar. "
                        f"Reset: DB sichern und Tabelle neu anlegen, z. B. Datei value_tracker.db "
                        f"umbenennen und `python decisions.py init` ausfuehren (Daten der Tabelle "
                        f"gehen dabei verloren -- Backup vorher).")
                # PK-Pruefung nur bei inline PRIMARY KEY in schema.sql (einzelner PK);
                # zusammengesetzte PKs stehen als Tabellen-Constraint und sind in
                # PRAGMA als pk>0 sichtbar -- die duerfen nicht als "geaendert" gelten
                if _decl_pk(decl) and not row[5]:
                    raise RuntimeError(
                        f"Schema-Guard: Tabelle {name}, Spalte {col}: Primaerschluessel geaendert "
                        f"(schema.sql: PRIMARY KEY, DB: pk=0) -- nicht per ALTER migrierbar. "
                        f"Reset wie bei Typaenderung (Backup + `python decisions.py init`).")
        if added:
            report[name] = {"created": False, "added": added}
    # 2) fehlende Tabellen + alle Indizes anlegen (executescript ist IF NOT EXISTS
    #    ueberall; nach Schritt 1 existieren alle Spalten, auf die Indizes zeigen)
    con.executescript(sql)
    for name in tables:
        if name not in existing:
            report[name] = {"created": True, "added": []}
            logger.info("ensure_schema: Tabelle %s angelegt", name)
    con.commit()
    return report
