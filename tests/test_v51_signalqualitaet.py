r"""
(v51) smart_money.py -- Signalqualitaet (nur Mocks/Seed-DBs, kein Netz, kein Volllauf):

(1) Aktionen werden ueber CUSIP verglichen: Position in Q1 ohne Ticker-Mapping
    (Schluessel = roher CUSIP), in Q2 gemappt -> NICHT "neu" (Diagnose Berkshire
    DVA/MCO). Fehlt das Vorquartals-Filing (Luecke) oder ist der CUSIP dort nicht
    auffindbar -> "unbekannt", nicht "neu". Parser liest verschachteltes
    <shrsOrPrnAmt><sshPrnamt> (echtes EDGAR-Format) -- sonst shares=NaN -> falsches "neu".
(2) aktion_max_alter_tage (config rules, Default 200): zu alte Aktionen zaehlen nicht
    fuer Score/Urteil, bleiben aber in der CSV (Spalte aktion_alter_tage).
    GDDY-artig (Verkauf mit filing 2025-05-15) -> neutral statt Warnung.
(3) "gemischt": Kaufen UND Verkaufen durch Fonds mit Gewicht > 0 im selben Quartal
    (MCO-artig: 3 Kaeufer, 1 Verkaeufer -> gemischt).
(4) divergenz_stufe (stark|schwach|-) nach rules.divergenz: "stark" nur bei
    Score >= 3 bzw. Score >= 2 + Quality >= 60, UND MoS-Minimum > -100 %.
(5) Neue CSV-Spalte signal_hinweis (z. B. "13F 2026-Q2, 81 Tage alt, 1 Fonds").
"""

import sqlite3
from collections import Counter
from datetime import date

import pytest

from config import load_config
import smart_money

VALID_UA = "Tom Homburg t09homburg@gmail.com"
CIK10 = "0001067983"


def _cfg(**sm_over):
    cfg = load_config()
    cfg["sec"]["enabled"] = False
    sm = dict(cfg.get("smart_money", {}))
    sm["max_requests_per_second"] = 100000
    sm["user_agent"] = VALID_UA
    sm["funds"] = [{"name": "Berkshire Hathaway", "cik": CIK10, "typ": "konzern", "gewicht": 1.0},
                   {"name": "FondsAktuell", "cik": "0000000002", "typ": "konzern", "gewicht": 1.0},
                   {"name": "Starboard Value", "cik": "0009999999", "typ": "aktivist", "gewicht": 0.0}]
    sm["cusip_map"] = {"111111111": "AAA", "222222222": "BBB", "23918K108": "DVA"}
    sm["cusip_map_url"] = ""
    sm.update(sm_over)
    cfg["smart_money"] = sm
    return cfg


def _seed(con, fonds, rows):
    """rows: (periode, report_date, filing_datum, cusip, ticker, aktion, aenderung_pct, anteil)"""
    for periode, rd, fd, cusip, ticker, aktion, aend, anteil in rows:
        con.execute(
            "INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum, cusip,"
            " ticker, name, title_of_class, shares, wert, anteil_portfolio, aktion, aenderung_pct,"
            " positions_gesamt, rotation_pct, geladen_am) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fonds, periode, rd, fd, cusip, ticker, ticker + " Corp", "COMMON STOCK",
             1.0, 100.0, anteil, aktion, aend, 1, None, "t"))
    con.commit()


# --------------------------------------------------------------------------
# (1) CUSIP-Vergleich + "unbekannt" + sshPrnamt-Parser
# --------------------------------------------------------------------------

def test_parse_infotable_nested_sshprnamt():
    # echtes EDGAR-Format: shrsOrPrnAmt ist verschachtelt (sshPrnamt), Direkttext leer
    xml = (b'<?xml version="1.0"?><informationTable><infoTable>'
           b'<nameOfIssuer>DAVITA INC</nameOfIssuer><cusip>23918K108</cusip>'
           b'<titleOfClass>COMMON STOCK</titleOfClass>'
           b'<shrsOrPrnAmt>\n  <sshPrnamt>11609000</sshPrnamt>\n  <sshPrnamtType>SH</sshPrnamtType>\n</shrsOrPrnAmt>'
           b'<value>2574804</value></infoTable></informationTable>')
    holdings, options = smart_money._parse_infotable(xml)
    assert holdings[0]["shares"] == 11609000.0          # nicht NaN
    assert holdings[0]["value"] == pytest.approx(2574804000.0)
    assert options == []
    # flaches Fixture-Format weiterhin unterstuetzt
    flat = (b'<?xml version="1.0"?><informationTable><infoTable>'
            b'<nameOfIssuer>AAA Corp</nameOfIssuer><cusip>111111111</cusip>'
            b'<shrsOrPrnAmt>100 sh</shrsOrPrnAmt><value>10</value></infoTable></informationTable>')
    h2, _ = smart_money._parse_infotable(flat)
    assert h2[0]["shares"] == 100.0


def test_compare_quarters_ueber_cusip_nicht_neu_bei_mapping_luecke():
    """User-Test: Position in Q1 ohne Ticker (Schluessel = roher CUSIP), in Q2 gemappt
    (Schluessel = DVA) -> Vergleich ueber CUSIP -> nicht "neu"."""
    prev = {"23918K108": {"cusip": "23918K108", "name": "DAVITA INC",
                         "shares": 35142479.0, "value": 1e9, "title": "COMMON STOCK"}}
    cur = {"DVA": {"cusip": "23918K108", "name": "DAVITA INC",
                   "shares": 33796541.0, "value": 1e9, "title": "COMMON STOCK"}}
    rows = smart_money.compare_quarters(prev, cur)
    assert len(rows) == 1 and rows[0]["aktion"] == "reduziert"      # nicht "neu"
    assert rows[0]["aenderung_pct"] == pytest.approx(33796541.0 / 35142479.0 - 1.0)


def test_compare_quarters_unbekannt_statt_neu():
    cur = {"DVA": {"cusip": "23918K108", "name": "DAVITA INC",
                   "shares": 100.0, "value": 1.0, "title": ""}}
    # Vorquartal nicht geladen (fehlendes Filing) -> "unbekannt", nicht "neu"
    assert smart_money.compare_quarters({}, cur, vorquartal_bekannt=False)[0]["aktion"] == "unbekannt"
    # Vorquartal geladen, CUSIP dort absent -> "neu"
    assert smart_money.compare_quarters({}, cur, vorquartal_bekannt=True)[0]["aktion"] == "neu"
    # CUSIP im Vorquartal vorhanden, aber shares nicht lesbar (NaN) -> "unbekannt"
    prev_nan = {"23918K108": {"cusip": "23918K108", "shares": float("nan"), "value": 1.0}}
    assert smart_money.compare_quarters(prev_nan, cur)[0]["aktion"] == "unbekannt"


def test_compare_quarters_verkauft_ueber_cusip():
    prev = {"BBB": {"cusip": "222222222", "name": "BBB Inc", "shares": 50.0, "value": 1.0, "title": ""}}
    rows = smart_money.compare_quarters(prev, {})
    assert rows[0]["aktion"] == "verkauft" and rows[0]["cusip"] == "222222222"


def _submissions_json(entries, name="BERKSHIRE HATHAWAY INC"):
    import json
    return json.dumps({
        "name": name,
        "filings": {"recent": {
            "accessionNumber": [e[0] for e in entries],
            "filingDate": [e[1] for e in entries],
            "form": [e[2] for e in entries],
            "reportDate": [e[3] for e in entries],
        }}}).encode()


def _infotable_xml(holdings, period):
    rows = "".join(
        f"<infoTable><nameOfIssuer>{n}</nameOfIssuer><cusip>{c}</cusip>"
        f"<shrsOrPrnAmt><sshPrnamt>{s}</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
        f"<value>{v}</value></infoTable>"
        for c, n, s, v in holdings)
    return (f'<?xml version="1.0"?><informationTable>'
            f'<periodOfReport>{period}</periodOfReport>{rows}</informationTable>').encode()


def test_fetch_fehlendes_vorquartal_filing_unbekannt(tmp_path):
    """Q1 + Q3 geladen (Q2-Filing fehlt): Q3 -> vorhandene CUSIPs vergleichen,
    neue CUSIPs "unbekannt" (nicht "neu"); Q1 bleibt "basis"."""
    entries = [("0001067983-25-000001", "2025-05-15", "13F-HR", "2025-03-31"),
               ("0001067983-25-000003", "2025-11-14", "13F-HR", "2025-09-30")]
    hold = {
        "0001067983-25-000001": [("111111111", "AAA Corp", 100.0, 10.0)],
        "0001067983-25-000003": [("111111111", "AAA Corp", 150.0, 15.0),
                                 ("222222222", "BBB Inc", 20.0, 2.0)],
    }

    def fetcher(url: str) -> bytes:
        import json
        if f"submissions/CIK{CIK10}.json" in url:
            return _submissions_json(entries)
        if url.endswith("/index.json"):
            return json.dumps({"directory": {"item": [{"name": "primary_doc.xml"},
                                                      {"name": "infotable.xml"}]}}).encode()
        if url.endswith("infotable.xml"):
            acc_digits = url.split("/")[-2]
            for acc, _fd, _form, rd in entries:
                if acc.replace("-", "") == acc_digits:
                    return _infotable_xml(hold[acc], rd)
            raise ValueError(url)
        if url.endswith("primary_doc.xml"):
            return b'<?xml version="1.0"?><doc></doc>'
        raise ValueError(url)

    cfg = _cfg(funds=[{"name": "Berkshire Hathaway", "cik": CIK10, "typ": "konzern", "gewicht": 1.0}])
    db = tmp_path / "sm.db"
    errors = smart_money.fetch_all(cfg, quarters=2, db_path=db, fetcher=fetcher,
                                   sleeper=lambda s: None)
    assert not errors, errors
    con = sqlite3.connect(db)
    q1 = con.execute("SELECT ticker, aktion FROM smart_money_positions WHERE periode='2025-Q1'"
                     ).fetchall()
    q3 = dict((t, a) for t, a in con.execute(
        "SELECT ticker, aktion FROM smart_money_positions WHERE periode='2025-Q3'"))
    assert q1 == [("AAA", "basis")]
    assert q3 == {"AAA": "erhoeht", "BBB": "unbekannt"}     # nicht "neu"
    # Score ignoriert "unbekannt": BBB hat nur "unbekannt" im letzten Quartal -> Score 1
    s = smart_money.smart_money_score("BBB", cfg, db_path=db, heute=date(2025, 12, 31))
    assert s["score"] == 1 and s["fonds_kaeufer"] == []


# --------------------------------------------------------------------------
# (2) aktion_max_alter_tage (Default 200 in config)
# --------------------------------------------------------------------------

def test_aktion_max_alter_tage_schwelle_in_config():
    cfg = load_config()
    assert cfg["smart_money"]["rules"]["aktion_max_alter_tage"] == 200


def test_zu_alte_aktion_zaehlt_nicht_gddy_neutral(tmp_path):
    """Verkauf mit filing_datum 2025-05-15: bei heute=2026-11-09 (543 Tage > 200)
    zaehlt er nicht -> Score/Urteil neutral; bei heute=2025-07-01 (47 Tage) -> Warnung."""
    cfg = _cfg()
    db = tmp_path / "g.db"
    con = smart_money.connect(db)
    _seed(con, "FondsAktuell", [
        ("2024-Q4", "2024-12-31", "2025-02-14", "333333333", "GDDY", "basis", None, 0.1),
        ("2025-Q1", "2025-03-31", "2025-05-15", "333333333", "GDDY", "verkauft", -1.0, 0.1),
    ])
    con.close()
    row = {"status_all": "Kandidat", "quality": "70", "mos_rule": "0.4"}
    s_alt = smart_money.smart_money_score("GDDY", cfg, db_path=db, heute=date(2026, 11, 9))
    assert s_alt["fonds_verkaeufer"] == []                    # zu alt -> nicht gezaehlt
    assert s_alt["aktion_alter_tage"] == 543
    assert s_alt["letzte_aktion"] == "FondsAktuell:verkauft"   # bleibt in der Anzeige/CSV
    assert smart_money.urteil(row, s_alt, cfg) == "neutral"
    s_frisch = smart_money.smart_money_score("GDDY", cfg, db_path=db, heute=date(2025, 7, 1))
    assert s_frisch["fonds_verkaeufer"] == ["FondsAktuell"]
    assert smart_money.urteil(row, s_frisch, cfg) == "Warnung"


def test_overlap_csv_spalte_aktion_alter_tage(tmp_path):
    cfg = _cfg()
    db = tmp_path / "o.db"
    con = smart_money.connect(db)
    _seed(con, "FondsAktuell", [
        ("2026-Q1", "2026-03-31", "2026-05-15", "111111111", "AAA", "basis", None, 0.1),
        ("2026-Q2", "2026-06-30", "2026-08-20", "111111111", "AAA", "erhoeht", 0.5, 0.1),
    ])
    con.close()
    ranking = tmp_path / "r.csv"
    ranking.write_text("ticker,status_all,quality,mos_rule\nAAA,Kandidat,70,0.4\n", encoding="utf-8")
    watch = tmp_path / "w.csv"
    watch.write_text("ticker\nAAA\n", encoding="utf-8")
    out = tmp_path / "overlap.csv"
    rows = smart_money.overlap(str(ranking), str(watch), cfg, db_path=db, out_path=str(out),
                               force=True, heute=date(2026, 11, 9))
    assert rows[0]["aktion_alter_tage"] == 81                  # 2026-08-20 -> 2026-11-09
    header = out.read_text(encoding="utf-8").splitlines()[0]
    assert "aktion_alter_tage" in header and "divergenz_stufe" in header and "signal_hinweis" in header


# --------------------------------------------------------------------------
# (3) "gemischt" bei Kauf + Verkauf (Gewicht > 0) im selben Quartal
# --------------------------------------------------------------------------

def test_urteil_gemischt_bei_kauf_und_verkauf_gewicht_positiv():
    cfg = _cfg()
    sm = {"fonds_kaeufer": ["Berkshire Hathaway"], "fonds_verkaeufer": ["FondsAktuell"], "score": 3}
    row = {"status_all": "Kandidat", "quality": "70", "mos_rule": "0.4"}
    assert smart_money.urteil(row, sm, cfg) == "gemischt"
    # Verkaeufer mit Gewicht 0 (Aktivist) -> nicht "gemischt"
    sm0 = {"fonds_kaeufer": ["Berkshire Hathaway"], "fonds_verkaeufer": ["Starboard Value"], "score": 3}
    assert smart_money.urteil(row, sm0, cfg) == "Bestaetigung"
    # ohne cfg (2-Arg) bleibt altes Verhalten: jeder Verkaeufer zaehlt
    assert smart_money.urteil(row, sm0) == "gemischt"


def test_gemischt_mco_3_kaeufer_1_verkaeufer(tmp_path):
    """MCO-artig: 3 Kaeufer + 1 Verkaeufer (alle Gewicht > 0) im selben Quartal."""
    cfg = _cfg()
    cfg["smart_money"]["funds"] += [
        {"name": "FondsZwei", "cik": "0000000003", "gewicht": 1.0},
        {"name": "FondsDrei", "cik": "0000000004", "gewicht": 1.0}]
    db = tmp_path / "m.db"
    con = smart_money.connect(db)
    _seed(con, "Berkshire Hathaway", [
        ("2026-Q2", "2026-06-30", "2026-08-20", "615369105", "MCO", "erhoeht", 0.2, 0.06)])
    _seed(con, "FondsZwei", [
        ("2026-Q2", "2026-06-30", "2026-08-20", "615369105", "MCO", "neu", None, 0.04)])
    _seed(con, "FondsDrei", [
        ("2026-Q2", "2026-06-30", "2026-08-20", "615369105", "MCO", "neu", None, 0.03)])
    _seed(con, "FondsAktuell", [          # Verkaeufer Gewicht 1,0
        ("2026-Q2", "2026-06-30", "2026-08-20", "615369105", "MCO", "reduziert", -0.4, 0.03)])
    _seed(con, "Starboard Value", [       # Aktivist (Gew 0) verkauft anderen Ticker -> egal
        ("2026-Q2", "2026-06-30", "2026-08-20", "999999999", "XYZ", "verkauft", -1.0, 0.02)])
    con.close()
    s = smart_money.smart_money_score("MCO", cfg, db_path=db, heute=date(2026, 11, 9))
    assert s["fonds_kaeufer"] == ["Berkshire Hathaway", "FondsDrei", "FondsZwei"]
    assert s["fonds_verkaeufer"] == ["FondsAktuell"]
    row = {"status_all": "Kandidat", "quality": "70", "mos_rule": "0.4"}
    assert smart_money.urteil(row, s, cfg) == "gemischt"


# --------------------------------------------------------------------------
# (4) divergenz_stufe stark|schwach|-
# --------------------------------------------------------------------------

def _sm(score, käufer=("Berkshire Hathaway",)):
    return {"score": score, "fonds_kaeufer": list(käufer), "fonds_verkaeufer": []}


def test_divergenz_stufe_regeln():
    cfg = _cfg()
    row_nein = {"status_all": "nein", "quality": "70", "mos_rule": "0.40"}   # kauft + nein -> Divergenz
    # Score 3 + MoS 0.4 > -100 % -> stark
    assert smart_money.divergenz_stufe(row_nein, _sm(3), cfg) == "stark"
    # Score 2 + Quality 70 >= 60 + MoS ok -> stark
    row_q65 = {"status_all": "nein", "quality": "65", "mos_rule": "0.20"}
    assert smart_money.divergenz_stufe(row_q65, _sm(2), cfg) == "stark"
    # Score 2 + Quality 50 < 60 -> schwach
    row_q50 = {"status_all": "nein", "quality": "50", "mos_rule": "0.20"}
    assert smart_money.divergenz_stufe(row_q50, _sm(2), cfg) == "schwach"
    # Score 3, aber MoS-Minimum -150 % <= -100 % -> schwach
    row_mos = {"status_all": "nein", "quality": "70", "mos_rule": "-1.50"}
    assert smart_money.divergenz_stufe(row_mos, _sm(3), cfg) == "schwach"
    # MoS unbekannt (n/a) -> schwach
    row_nan = {"status_all": "nein", "quality": "70", "mos_rule": ""}
    assert smart_money.divergenz_stufe(row_nan, _sm(3), cfg) == "schwach"
    # keine Divergenz (kein Kaeufer) -> "-"
    assert smart_money.divergenz_stufe({"status_all": "Kandidat", "quality": "70", "mos_rule": "0.4"},
                                       _sm(1, käufer=()), cfg) == "-"


def test_divergenz_stufe_zaehlung_im_overlap(tmp_path):
    cfg = _cfg()
    db = tmp_path / "d.db"
    con = smart_money.connect(db)
    # STARK: 1 Fonds mit Anteil 10 % (>= 5 %, Gew 1,0) kauft, Status nein, MoS 0.4 -> Score 3, stark
    _seed(con, "FondsAktuell", [
        ("2026-Q1", "2026-03-31", "2026-05-15", "111111111", "STARK", "basis", None, 0.1),
        ("2026-Q2", "2026-06-30", "2026-08-20", "111111111", "STARK", "neu", None, 0.10)])
    # SCHWACH: kleiner Anteil 1 % -> Score 2, Quality 50 -> schwach
    _seed(con, "FondsAktuell", [
        ("2026-Q1", "2026-03-31", "2026-05-15", "222222222", "SCHW", "basis", None, 0.1),
        ("2026-Q2", "2026-06-30", "2026-08-20", "222222222", "SCHW", "neu", None, 0.01)])
    con.close()
    ranking = tmp_path / "r.csv"
    ranking.write_text("ticker,status_all,quality,mos_rule\nSTARK,nein,70,0.4\nSCHW,nein,50,0.2\n",
                       encoding="utf-8")
    watch = tmp_path / "w.csv"
    watch.write_text("ticker\nSTARK\nSCHW\n", encoding="utf-8")
    out = tmp_path / "overlap.csv"
    rows = smart_money.overlap(str(ranking), str(watch), cfg, db_path=db, out_path=str(out),
                               force=True, heute=date(2026, 11, 9))
    by = {r["ticker"]: r for r in rows}
    assert by["STARK"]["smart_money_urteil"] == "Divergenz" and by["STARK"]["divergenz_stufe"] == "stark"
    assert by["SCHW"]["smart_money_urteil"] == "Divergenz" and by["SCHW"]["divergenz_stufe"] == "schwach"
    cnt = Counter(r["divergenz_stufe"] for r in rows)
    assert cnt["stark"] == 1 and cnt["schwach"] == 1
    # stark_anteil_max (15 %) ist Portfolio-Ziel, kein Fixture-Assert --
    # Zaehlung/Warnung laeuft ueber overlap-Log + cmd_overlap-Ausgabe


# --------------------------------------------------------------------------
# (5) signal_hinweis
# --------------------------------------------------------------------------

def test_signal_hinweis_format(tmp_path):
    cfg = _cfg()
    db = tmp_path / "s.db"
    con = smart_money.connect(db)
    _seed(con, "Berkshire Hathaway", [
        ("2026-Q1", "2026-03-31", "2026-05-15", "111111111", "AAA", "basis", None, 0.1),
        ("2026-Q2", "2026-06-30", "2026-08-20", "111111111", "AAA", "erhoeht", 0.5, 0.1)])
    _seed(con, "FondsAktuell", [
        ("2026-Q2", "2026-06-30", "2026-08-20", "111111111", "AAA", "neu", None, 0.05)])
    con.close()
    s = smart_money.smart_money_score("AAA", cfg, db_path=db, heute=date(2026, 11, 9))
    # 2026-08-20 -> 2026-11-09 = 81 Tage; 2 Fonds aktiv im letzten Quartal
    assert s["signal_hinweis"] == "13F 2026-Q2, 81 Tage alt, 2 Fonds"
    assert s["letzte_periode"] == "2026-Q2"
    # CSV-Spalte in overlap
    ranking = tmp_path / "r.csv"
    ranking.write_text("ticker,status_all,quality,mos_rule\nAAA,Kandidat,70,0.4\n", encoding="utf-8")
    watch = tmp_path / "w.csv"
    watch.write_text("ticker\nAAA\n", encoding="utf-8")
    out = tmp_path / "overlap.csv"
    rows = smart_money.overlap(str(ranking), str(watch), cfg, db_path=db, out_path=str(out),
                               force=True, heute=date(2026, 11, 9))
    assert rows[0]["signal_hinweis"] == "13F 2026-Q2, 81 Tage alt, 2 Fonds"
    assert "13F 2026-Q2, 81 Tage alt, 2 Fonds" in out.read_text(encoding="utf-8")
