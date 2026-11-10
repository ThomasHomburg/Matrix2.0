r"""
(v55) smart_money.py rationale-worklist (kein Netz, keine erzeugten Begruendungen):

- Arbeitsliste aus rationale_import.csv (ticker, fonds) + DB: aktion, periode,
  filing_datum, edgar_link (CIK ohne fuehrende Nullen + Accession ohne Bindestriche).
- suchhinweis je Fonds aus config (Berkshire = Aktionaersbrief/HV, Pershing = Quartals-
  /Jahresbrief, Starboard/Elliott = 13D "Purpose of Transaction").
- text, quelle_url, geprueft bleiben leer; rationale-import bleibt unveraendert
  (Pflicht text + quelle_url -> Abbruch auf der Arbeitsliste).
- Sortiert nach Naehe zu Score 4 (0 = Score-4-Kandidat, 1 = cond3 letztes Quartal, 2 = sonst).
"""

import argparse
import csv
import sqlite3

import pytest

from config import load_config
import smart_money

VALID_UA = "Tom Homburg t09homburg@gmail.com"


def _cfg():
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["user_agent"] = VALID_UA
    sm["max_requests_per_second"] = 100000
    sm["funds"] = [
        {"name": "Berkshire Hathaway", "cik": "0001067983", "typ": "konzern", "gewicht": 1.0},
        {"name": "Pershing Square Capital Management", "cik": "0001336528", "typ": "konzentriert", "gewicht": 1.0},
        {"name": "Starboard Value", "cik": "0001517137", "typ": "aktivist", "gewicht": 0.0},
    ]
    cfg["smart_money"] = sm
    return cfg


def _seed(con, fonds, rows):
    """rows: (periode, filing_datum, accession, cusip, ticker, aktion, anteil)"""
    quartalsende = {"2026-Q1": "2026-03-31", "2026-Q2": "2026-06-30"}
    for periode, fd, acc, cusip, ticker, aktion, anteil in rows:
        con.execute(
            "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum,"
            " accession, cusip, ticker, name, title_of_class, shares, wert, anteil_portfolio,"
            " aktion, aenderung_pct, positions_gesamt, geladen_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fonds, periode, quartalsende.get(periode, "2026-06-30"), fd, acc, cusip, ticker,
             ticker + " Corp", "COMMON STOCK", 1.0, 100.0, anteil, aktion, 0.1, 10, "t"))
    con.commit()


@pytest.fixture
def fixture(tmp_path):
    cfg = _cfg()
    db = tmp_path / "sm.db"
    con = smart_money.connect(db)
    b = "Berkshire Hathaway"
    # AAA: cond3 in 2 aufeinanderfolgenden Quartalen -> Score-4-Kandidat (rang 0)
    _seed(con, b, [("2026-Q1", "2026-05-15", "0001067983-26-000001", "111111111", "AAA", "erhoeht", 0.10),
                   ("2026-Q2", "2026-08-14", "0001067983-26-000002", "111111111", "AAA", "erhoeht", 0.10)])
    # BBB: nur letztes Quartal cond3 -> rang 1
    _seed(con, b, [("2026-Q2", "2026-08-14", "0001067983-26-000003", "222222222", "BBB", "neu", 0.10)])
    # CCC: Pershing, eine Periode -> rang 2
    _seed(con, "Pershing Square Capital Management",
          [("2026-Q2", "2026-08-14", "0001336528-26-000009", "333333333", "CCC", "neu", 0.05)])
    # DDD: Starboard (Aktivist, Gewicht 0) -> rang 2
    _seed(con, "Starboard Value",
          [("2026-Q2", "2026-08-14", "0001517137-26-000007", "444444444", "DDD", "neu", 0.05)])
    con.close()
    # rationale_import.csv-Fixture (45 Zeilen in echt; hier 4 mit [ENTWURF]-Text)
    imp = tmp_path / "rationale_import.csv"
    imp.write_text(
        "# ENTWURF\n"
        "ticker,fonds,text,quelle_url,datum,typ\n"
        "DDD,Starboard Value,[ENTWURF] pruefen,,2026-08-20,Sonstiges\n"
        "BBB,Berkshire Hathaway,[ENTWURF] pruefen,,2026-08-20,Brief\n"
        "CCC,Pershing Square Capital Management,[ENTWURF] pruefen,,2026-08-20,Sonstiges\n"
        "AAA,Berkshire Hathaway,[ENTWURF] pruefen,,2026-08-20,Brief\n",
        encoding="utf-8")
    return cfg, db, imp


def test_rationale_worklist(fixture, tmp_path):
    cfg, db, imp = fixture
    con = smart_money.connect(db)
    out = tmp_path / "rationale_worklist.csv"
    n = smart_money.write_rationale_worklist(con, cfg, str(imp), str(out))
    assert n == 4
    rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
    assert list(rows[0].keys()) == ["ticker", "fonds", "aktion", "periode", "filing_datum",
                                    "edgar_link", "suchhinweis", "text", "quelle_url", "geprueft"]
    # Sortierung nach Naehe zu Score 4: AAA (rang 0) vor BBB (rang 1) vor CCC/DDD (rang 2)
    assert [r["ticker"] for r in rows] == ["AAA", "BBB", "CCC", "DDD"]
    aaa = rows[0]
    assert aaa["fonds"] == "Berkshire Hathaway"
    assert aaa["aktion"] == "erhoeht" and aaa["periode"] == "2026-Q2" and aaa["filing_datum"] == "2026-08-14"
    # edgar_link: CIK ohne fuehrende Nullen + Accession ohne Bindestriche
    assert aaa["edgar_link"] == "https://www.sec.gov/Archives/edgar/data/1067983/000106798326000002/"
    assert aaa["suchhinweis"] == "Aktionärsbrief und Hauptversammlung"
    # leer: keine erzeugten Begruendungen
    assert aaa["text"] == "" and aaa["quelle_url"] == "" and aaa["geprueft"] == ""
    ccc = rows[2]
    assert ccc["edgar_link"] == "https://www.sec.gov/Archives/edgar/data/1336528/000133652826000009/"
    assert ccc["suchhinweis"] == "Quartals-/Jahresbrief"
    ddd = rows[3]
    assert ddd["suchhinweis"] == "13D \"Purpose of Transaction\""
    con.close()


def test_rationale_worklist_cmd_und_import_unveraendert(fixture, tmp_path):
    cfg, db, imp = fixture
    out = tmp_path / "rationale_worklist.csv"
    args = argparse.Namespace(db=str(db), csv_file=str(imp), out=str(out))
    smart_money.cmd_rationale_worklist(args)
    assert out.exists()
    # rationale-import bleibt unveraendert: Pflicht text + quelle_url -> Abbruch auf der
    # Arbeitsliste (deren text/quelle_url leer sind), mit Zeilennummer
    with pytest.raises(SystemExit, match="quelle_url"):
        smart_money.cmd_rationale_import(argparse.Namespace(db=str(db), csv_file=str(out)))
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM smart_money_rationale").fetchone()[0] == 0
