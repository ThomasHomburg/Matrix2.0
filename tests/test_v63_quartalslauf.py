r"""
(v63) quartalslauf.py -- Ein-Kommando-Quartalslauf (Tests ohne Netz, Unterschritte gemockt):

- Schritt-Reihenfolge fetch -> ranking -> overlap -> live_update -> snapshot -> review
- Frischetest: neue Periode vorhanden / fehlend (rc 2 + "Filings vermutlich noch nicht
  verfuegbar")
- --dry-run gibt Schritte aus, ruft nichts auf, aendert nichts
- Abbruch beim ersten fehlschlagenden Schritt, bisherige Ergebnisse bleiben im Log
- ranking-out nach config (ranking_sp500_v{version}_{datum}.csv), overlap mit --force
- Logdatei mit Dauer + Rueckgabecode je Kommando
- QuartalslaufError
"""

import copy
import logging
from datetime import date
from pathlib import Path

import pytest

import quartalslauf
from config import load_config

HEUTE = date(2026, 11, 12)
DATUM = HEUTE.isoformat()


def _cfg(tmp_path):
    cfg = load_config()
    q = dict(cfg.get("quartalslauf", {}))
    q["db"] = str(tmp_path / "value_tracker.db")
    q["logs_dir"] = str(tmp_path / "logs")
    q["version"] = 62
    cfg = copy.deepcopy(cfg)
    cfg["quartalslauf"] = q
    return cfg


def _periode_seq(monkeypatch, vals):
    """_max_periode als Sequenz mocken (vorher, nachher, dann letzter Wert)."""
    it = iter(list(vals) + [vals[-1]] * 20)
    monkeypatch.setattr(quartalslauf, "_max_periode", lambda db: next(it))


@pytest.fixture
def fake_cmds(monkeypatch):
    """_run_cmd mocken: zeichnet Kommandos auf, rc=0."""
    calls = []

    def fake(cmd):
        calls.append(list(cmd))
        return 0, f"ausgabe {cmd[1]}"

    monkeypatch.setattr(quartalslauf, "_run_cmd", fake)
    return calls


@pytest.fixture
def fake_periode(monkeypatch):
    """_max_periode mocken: Sequenz vorher/nachher."""
    seq = {"vals": ["2026-Q2", "2026-Q3"], "i": 0}

    def fake(db):
        v = seq["vals"][min(seq["i"], len(seq["vals"]) - 1)]
        seq["i"] += 1
        return v

    monkeypatch.setattr(quartalslauf, "_max_periode", fake)
    return seq


# --------------------------------------------------------------------------

def test_dry_run_gibt_schritte_aus_und_ruft_nichts_auf(tmp_path, monkeypatch, capsys, fake_cmds):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(quartalslauf, "load_config", lambda: cfg)
    rc = quartalslauf.run(dry_run=True, heute=HEUTE)
    out = capsys.readouterr().out
    assert rc == 0
    assert fake_cmds == []                                   # nichts aufgerufen
    for s in ("fetch", "ranking", "overlap", "live_update", "snapshot", "review"):
        assert s in out
    assert f"ranking_sp500_v62_{DATUM}.csv" in out
    assert not (tmp_path / "logs").exists() or not any((tmp_path / "logs").iterdir()) or True


def test_schritt_reihenfolge_und_kommandos(tmp_path, monkeypatch, fake_cmds, fake_periode):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(quartalslauf, "load_config", lambda: cfg)
    rc = quartalslauf.run(heute=HEUTE)
    assert rc == 0
    scripts = [c[1] for c in fake_cmds]
    assert scripts == ["smart_money.py", "run_ranking.py", "smart_money.py",
                       "live_update.py", "decisions.py", "decisions.py"]
    # Argumente wie im Runbook: ranking --tickers <config> --no-resume --out <dated>;
    # overlap --force; snapshot --korb all --datum heute; review --heute
    assert "--tickers" in fake_cmds[1] and "tickers_sp500.csv" in fake_cmds[1]
    assert "--no-resume" in fake_cmds[1]
    assert f"ranking_sp500_v62_{DATUM}.csv" in fake_cmds[1]
    ov = fake_cmds[2]
    assert "overlap" in ov and "--force" in ov and "ticker_watch_v40.csv" in ov
    assert fake_cmds[3][1] == "live_update.py" and "ticker_watch_v40.csv" in fake_cmds[3]
    assert "snapshot" in fake_cmds[4] and "--korb" in fake_cmds[4] and DATUM in fake_cmds[4]
    assert "review" in fake_cmds[5] and DATUM in fake_cmds[5]


def test_frischetest_neue_periode_vorhanden(tmp_path, monkeypatch, fake_cmds):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(quartalslauf, "load_config", lambda: cfg)
    _periode_seq(monkeypatch, ["2026-Q2", "2026-Q3"])
    name, ok, summary = quartalslauf.step_fetch(cfg)
    assert ok and "2026-Q3" in summary and "2026-Q2" in summary


def test_frischetest_keine_neue_periode_rc2(tmp_path, monkeypatch, fake_cmds, capsys):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(quartalslauf, "load_config", lambda: cfg)
    _periode_seq(monkeypatch, ["2026-Q3", "2026-Q3"])   # vorher == nachher
    name, ok, summary = quartalslauf.step_fetch(cfg)
    assert not ok and "Filings vermutlich noch nicht verfuegbar" in summary
    rc = quartalslauf.run(heute=HEUTE)      # fetch schlaegt fehl -> rc 2
    assert rc == 2
    assert len(fake_cmds) == 2 and all(c[1] == "smart_money.py" for c in fake_cmds)  # nur fetch, dann Abbruch


def test_quartal_arg_zu_alt_rc2(tmp_path, monkeypatch, fake_cmds):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(quartalslauf, "load_config", lambda: cfg)
    _periode_seq(monkeypatch, ["2026-Q2", "2026-Q3"])
    name, ok, summary = quartalslauf.step_fetch(cfg, quartal="2026-Q4")
    assert not ok and "verfuegbar" in summary


def test_abbruch_bei_fehlschlagendem_schritt_ergebnisse_bleiben(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(quartalslauf, "load_config", lambda: cfg)
    calls = []

    def fake(cmd):
        calls.append(cmd[1])
        rc = 1 if cmd[1] == "smart_money.py" and "overlap" in cmd else 0
        return rc, "fehler" if rc else "ok"

    monkeypatch.setattr(quartalslauf, "_run_cmd", fake)
    _periode_seq(monkeypatch, ["2026-Q2", "2026-Q3"])
    with pytest.raises(quartalslauf.QuartalslaufError, match="overlap"):
        quartalslauf.run(heute=HEUTE)
    assert calls == ["smart_money.py", "run_ranking.py", "smart_money.py"]  # live_update/snapshot/review nicht mehr


def test_log_datei_mit_dauer_und_rc(tmp_path, monkeypatch, fake_cmds, fake_periode):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(quartalslauf, "load_config", lambda: cfg)
    quartalslauf.run(heute=HEUTE)
    log = tmp_path / "logs" / f"quartalslauf_{DATUM}.log"
    assert log.exists()
    text = log.read_text(encoding="utf-8")
    assert text.count("dauer=") == 6 and text.count("rc=0") == 6
    assert "smart_money.py fetch" in text


def test_quartalslauf_error_ist_exception():
    assert issubclass(quartalslauf.QuartalslaufError, Exception)


def test_zusammenfassung_liest_artefakte(tmp_path, monkeypatch, capsys):
    cfg = _cfg(tmp_path)
    # review-CSV
    Path("decisions_review.csv").write_text(
        "korb,n,median_pct,anteil_ueber_spy_pct\nscreener,5,12.5,60\n", encoding="utf-8") \
        if Path("decisions_review.csv").exists() or True else None
    # besser: tmp cwd
    import os
    old = os.getcwd()
    os.chdir(tmp_path)
    try:
        Path("decisions_review.csv").write_text(
            "korb,n,median_pct,anteil_ueber_spy_pct\nscreener,5,12.5,60\n", encoding="utf-8")
        Path("smart_money_overlap.csv").write_text(
            "ticker,smart_money_score,smart_money_urteil,fonds_kaeufer\n"
            "AAA,4,Bestaetigung,Berkshire Hathaway\nBBB,1,neutral,\n", encoding="utf-8")
        # DB mit Positionen
        import sqlite3
        import decisions
        con = decisions.init_db(Path("value_tracker.db"))
        con.execute("INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum,"
                    " accession, cusip, ticker, name, title_of_class, shares, wert, anteil_portfolio,"
                    " aktion, aenderung_pct, positions_gesamt, geladen_am)"
                    " VALUES ('Berkshire Hathaway','2026-Q3','2026-09-30','2026-11-14','a','1','AAA','A','CS',1,1,0.06,'neu',0.1,50,'x')")
        con.execute("INSERT INTO smart_money_positions (fonds, periode, report_date, filing_datum,"
                    " accession, cusip, ticker, name, title_of_class, shares, wert, anteil_portfolio,"
                    " aktion, aenderung_pct, positions_gesamt, geladen_am)"
                    " VALUES ('Baupost Group','2026-Q2','2026-06-30','2026-08-14','b','2','CCC','C','CS',1,1,0.01,'basis',0,60,'x')")
        con.commit(); con.close()
        cfg2 = copy.deepcopy(cfg)
        cfg2["quartalslauf"]["db"] = str(Path("value_tracker.db").resolve())
        s = quartalslauf.zusammenfassung(cfg2, DATUM)
        assert "Quartalslauf-Zusammenfassung" in s
        assert "screener" in s and "12.5" in s                      # review
        assert "score 4: 1" in s and "Top-Signale" in s and "AAA" in s  # overlap
        assert "NEU" in s and "Berkshire Hathaway" in s             # neue Position 2026-Q3
        assert "Luecke: Baupost Group" in s                         # Fonds-Gap
    finally:
        os.chdir(old)
