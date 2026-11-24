# v75: Point-in-time-Fundamentalspeicher. Historie der S&P-500-Aenderungen
# aus Wikipedia ("Historical components of the S&P 500", Tabelle id="changes").
# Zweck: Universe-Aufbau (je_removed hinzuzufuegende historische Ticker).
#       Uebernahme des Aenderungs-Tabellen-Parsers aus dem v74-Skript
#       tools/v74_pit_survivorship.py (dort ohne Rueckkanal).
# Aufruf: nur als Modul; Test: python tools/v75_wiki_history.py
#         (druckt Aenderungen ab 2013-01-01 bis heute als CSV auf stdout).

import datetime as dt
import re
import urllib.request

WIKI_RAW = ("https://en.wikipedia.org/w/index.php?"
            "title=Historical_components_of_the_S%26P_500&action=raw")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}


def _name(zelle):
    """[[Firmenname]] -> Klartext"""
    zelle = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", zelle)
    zelle = re.sub(r"\[\[([^\]]*)\]\]", r"\1", zelle)
    return zelle.strip()


def _ist_zelle(zeile):
    # Zellen beginnen mit '|' (aber nicht mit '|-' oder '|}')
    return zeile.startswith("|") and not zeile.startswith(("-}", "|-", "}"))


def _datum(text):
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", text.strip())
    if not m or m.group(1) not in _MONTHS:
        return None
    return dt.date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))


def _hole_wikitext():
    req = urllib.request.Request(
        WIKI_RAW, headers={"User-Agent": "Matrix2.0 audit (Research)"})
    return urllib.request.urlopen(req, timeout=60).read().decode(
        "utf-8", "replace")


def wiki_entfernungen(ab=dt.date(2013, 1, 1)):
    """Parst die Aenderungstabelle (Zeilen mit '|-', Zellen mit '|', Inline-
    Zellen mit '||'). Gibt Liste von Dicts zurueck:
    datum, added (Ticker), added_name, removed (Ticker), removed_name.
    Nur Zeilen ab 'ab' (Effective Date)."""
    text = _hole_wikitext()
    tab = text[text.index('id="changes"'):]
    out = []
    zellen = []
    for roh in tab.splitlines():
        zeile = roh.strip()
        if zeile.startswith("{|") or zeile.startswith("|}") or not zeile:
            continue
        if zeile == "|-":
            # Zellenordnung: Datum, AddTick, AddName, RemTick, RemName, Reason, Refs
            if len(zellen) >= 4:
                d = _datum(zellen[0])
                if d is not None and d >= ab:
                    out.append({
                        "datum": d,
                        "added": zellen[1].strip(),
                        "added_name": _name(zellen[2]) if len(zellen) > 2 else "",
                        "removed": zellen[3].strip(),
                        "removed_name": _name(zellen[4]) if len(zellen) > 4 else "",
                    })
            zellen = []
            continue
        if zeile.startswith("!"):
            continue
        if _ist_zelle(zeile):
            zellen.extend(c.strip() for c in zeile.lstrip("|").split("||"))
        elif zellen:
            zellen[-1] += " " + zeile
    return out


def _selbsttest():
    rows = wiki_entfernungen()
    print("datum,added,added_name,removed,removed_name")
    for r in rows:
        print("%s,%s,%s,%s,%s"
              % (r["datum"].isoformat(), r["added"], r["added_name"],
                 r["removed"], r["removed_name"]))


if __name__ == "__main__":
    _selbsttest()
