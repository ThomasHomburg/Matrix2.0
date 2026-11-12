# Value-Screener

Automatisches Aktien-Screening nach Qualitaet und Bewertung (Buffett-Tracker) inklusive Smart-Money-Live-Schicht (13F-Portfolios). Versionshistorie: CHANGELOG.md.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate   # Linux/Mac
pip install -r requirements.txt                      # streamlit, pandas, altair, requests, pyyaml, yfinance, pytest
# FMP_API_KEY als Umgebungsvariable setzen (optional -- nur fuer US-Ticker via FMP;
# wird nie committed, siehe .gitignore):
#   export FMP_API_KEY="dein_key"     (Linux/Mac)   $env:FMP_API_KEY="dein_key"   (Windows PowerShell)
```

Datenquellen: FMP (US-Ticker, mit Key) bzw. yfinance (alle anderen Suffixe .DE/.AS/...), kein Key noetig. Live-Ticker stehen in `tickers.csv` (Spalte `ticker`).

## Web-App starten (Streamlit)

```bash
# Windows (im Projektordner, venv aktiv):
.\.venv\Scripts\python.exe -m streamlit run app.py
# Linux/Mac:
python3 -m streamlit run app.py
```

Oeffnet automatisch http://localhost:8501. Fuenf Seiten:

1. **Rangliste** -- sortierbare Tabelle aller Ticker mit Quality/Value/MoS je Startwert-Modus/Status/SEC-Abgleich/Flags; Standard-Sortierung Kaufkandidat zuerst, dann MoS; ausgeschlossene Ticker als eigene Tabelle mit Grund; Filter nach Sektor/Land/Mindest-Score; Quelle (FMP/yfinance) je Ticker sichtbar.
2. **Scorecard** -- Detail je Ticker: Block-Scores, Kriterien mit Rohwerten, DCF-Tabelle (3 Szenarien, 4 Startwert-Modi, umschaltbare Nettoverschuldung), letzte Quartale mit Stichtag, SEC-EDGAR-Abgleich, Sensitivitaet, Charts.
3. **Matrix** -- Streudiagramm Quality (x) vs. MoS (y), Farbe nach Status, Ziel-Quadrant hervorgehoben.
4. **Import** -- eigene Ticker-Liste per CSV (Spalte `ticker`) oder manuell.
5. **Watchlist Live** (v53) -- liest `value_tracker.db` (watchlist_status, valuations, prices, decisions, smart_money_*), Anzeige ohne Netz, Cache 15 Min. Kopfzeile Datenstand (letzter Kurs, letztes Valuations-Datum, 13F-Periode je Fonds mit Alter in Tagen; Warnung bei Kurs > 3 Tage / Valuation > 100 Tage / 13F > 140 Tage). Sortierbare Tabelle mit Filter Korb/Status/Alarm (ticker, name, korb, status_screener, status_live, kurs, mos_live_min, mos_live_robust, kaufzone_kurs, distanz_pct, timing_status als [POS]/[NEU]/[NEG], smart_money_score, urteil, divergenz_stufe, alarm, wert_veraltet, naechster_earnings). Detail je Ticker: Szenario-Tabelle Bear/Base/Bull je Startwert-Modus mit Erwartungswert, Kurs-vs-Kaufzone-Chart (1 Jahr, Altair), Smart-Money-Block (Positionen je Fonds mit Periode/Aktion/Anteil, Katalysatoren, Rationale mit Quelle), Entscheidungslog, Platzhalter "Trends (folgt)". Schaltflaeche "Live aktualisieren" ruft `live_update.py` auf (Netz, Fortschritt + Fehlerlog; ohne Netz bleibt der letzte Stand in der DB). Export `watchlist_live_export.csv` (ASCII-Spaltennamen). Leere DB/fehlende Tabellen -> Meldung statt Exception.

## CLI (ohne Browser)

```bash
python run_ranking.py --tickers tickers_sp500.csv [--out ranking_sp500.csv] [--batch 20] [--pause 2.0] [--no-resume]
python run_universe.py sp500|stoxx600                 # Universum laden (Cache, Resume)
python testlauf_run.py                                 # Testlauf (Batches, testlauf_25.csv + testlauf_fehler.txt)
python rohwerte_check.py RDDT STT                      # Rohwert-/Szenario-Diagnose
```

## Smart-Money-Pipeline (smart_money.py, SQLite value_tracker.db)

13F-HR der Fonds aus `config.yaml` (`smart_money.funds`, 10 Fonds inkl. Aktivisten) von SEC EDGAR laden, parsen, mit Vorquartal vergleichen (aktion neu/erhoeht/reduziert/verkauft), CUSIP->Ticker mappen (cusip_map-Tabelle, statisches `cusip_map`, OpenFIGI mit US-Composite-Filter, nameOfIssuer-Fallback), Optionen und 13D-Katalysatoren erfassen.

```bash
python smart_money.py fetch [--quarters 8] [--reset] [--db value_tracker.db]
python smart_money.py score TICKER [TICKER ...] [--db ...] [--force]
python smart_money.py score --ranking ranking.csv [--db ...] [--force]
python smart_money.py overlap --ranking ranking.csv --watch ticker_watch_v40.csv [--out smart_money_overlap.csv] [--db ...] [--force]
python smart_money.py rationale-template [--out rationale_import.csv] [--db ...]
python smart_money.py rationale-worklist [--db ...]   # -> rationale_worklist.csv (v55)
python smart_money.py rationale-import rationale_import.csv [--db ...]
python smart_money.py ciks [--db ...]        # CIK-Aufloesung + Filer-Status
python smart_money.py show TICKER | show --fonds FONDS [--db ...]
```

- **Schema-Guard**: Migration/Reset pruefen per PRAGMA die Pflichtspalten von smart_money_positions UND smart_money_options (periode/accession/report_date bzw. periode); veraltete schema.sql -> sofortiger RuntimeError statt "no such column" je Fonds.
- **Vollstaendigkeitsschutz**: score/overlap brechen ab, wenn die neueste Periode eines Fonds > 140 Tage alt ist oder in den letzten 4 Perioden eine fehlt; `--force` uebersteuert ausschliesslich diesen Abbruch (Warnzeile je blockiertem Fonds im Log; Ergebnis-CSV wird erst am Ende geschrieben und bleibt bei Abbruch unberuehrt).
- **ETF-Bereinigung**: ETF/Index-Positionen (cusip_map.is_etf bzw. `smart_money.etf_ticker`) bleiben in der CSV sichtbar, werden aber nicht gescored (Score 1, Urteil neutral, Spalte `etf=ja`).
- **Rationale fuer Score 4**: `rationale-template` schreibt eine Import-Vorlage mit allen Score-4-Kandidaten; `rationale-import` bricht bei fehlendem text/quelle_url mit Zeilennummer ab (atomar); Duplikate (ticker, fonds, quelle_url) werden ueberschrieben; die Quelle erscheint als Spalte `rationale_quelle` in der overlap-CSV. Aktueller Entwurf liegt in `rationale_import.csv` (jede Zeile mit [ENTWURF] zur Pruefung).
- **Arbeitsliste (v55)**: `rationale-worklist` erzeugt `rationale_worklist.csv` aus `rationale_import.csv` -- angereichert um aktion, periode, filing_datum und edgar_link aus der DB (CIK ohne fuehrende Nullen, Accession ohne Bindestriche) sowie einen suchhinweis je Fonds (config `smart_money.rationale_suchhinweise`; Berkshire = Aktionaersbrief/Hauptversammlung, Pershing Square = Quartals-/Jahresbrief, Starboard/Elliott = 13D "Purpose of Transaction"). text/quelle_url/geprueft bleiben leer (keine erzeugten Begruendungen; `rationale-import` verlangt sie weiterhin). Sortiert nach Naehe zu Score 4.
- **Signalqualitaet (v51)**: Aktionen werden ueber CUSIP verglichen (kein falsches "neu" bei Mapping-Luecke im Vorquartal; fehlendes Vorquartals-Filing -> Aktion "unbekannt"). `rules.aktion_max_alter_tage` (Default 200): zu alte Aktionen zaehlen nicht fuer Score/Urteil, bleiben aber in der CSV (`aktion_alter_tage`). Urteil "gemischt" bei Kauf + Verkauf durch Fonds mit Gewicht > 0 im selben Quartal. Neue Spalten `divergenz_stufe` (stark|schwach|- nach `rules.divergenz`: stark nur bei Score >= 3 bzw. Score >= 2 + Quality >= 60 UND MoS-Min > -100 %) und `signal_hinweis` (z. B. "13F 2026-Q2, 87 Tage alt, 3 Fonds"). Zaehlung der Divergenz-Stufen in der overlap-Ausgabe (Ziel: <= 15 % stark).

## Entscheidungslog mit Vergleichkoerben (decisions.py, SQLite value_tracker.db)

Koerbe (config `decisions.baskets`): `screener` (alle Kandidat/Beobachtung), `divergenz`
(Status nein + `divergenz_stufe` stark + Quality >= 60, max. 15 nach SM-Score/Quality;
**ohne** `divergenz_stufe` Ersatzregel SM-Score >= 3 + Quality >= 60), `kontrolle`
(15 per Zufall, Seed 42, Status nein + SM-Score 1 + ohne Fondsaktivitaet), `benchmark`
(SPY, RSP).

**Nach jedem neuen 13F-Quartal** (`smart_money.py fetch`) Snapshot -- das Snapshot-Datum ist der **Tag des Snapshots** (Default `--datum` = heute), nicht das Quartalsende:

```bash
python decisions.py snapshot --korb all \
    [--datum YYYY-MM-DD] [--ranking ranking.csv] [--overlap smart_money_overlap.csv] [--live watchlist_live.csv]
python decisions.py review [--heute YYYY-MM-DD]     # -> decisions_review.csv
```

Look-ahead-Schutz (v56): Startkurs je Zeile = Kurs am Snapshot-Tag (aus `prices`); Startdatum der Rendite = `max(Snapshot-Datum, juengstes filing_datum der verwendeten 13F)` (Spalte `start_datum`). Liegt `--datum` vor dem filing_datum eines verwendeten Signals, bricht `snapshot` vor dem Schreiben ab: "Datum liegt vor Veröffentlichung der Signale (Look-ahead)".

Snapshot schreibt je Ticker und Korb eine Zeile (aktion=auto, Kurs, Status, Quality, MoS,
iv_bear/base/bull, kaufzone_kurs, timing_status, Smart-Money-Felder, Szenariogewichte
25/50/25, Erwartungswert %, wahrscheinlichkeit_p) und ist **idempotent je
(ticker, korb, datum)** -- geschriebene Zeilen werden nie ueberschrieben. Review fuellt
Kurse nach (Horizonte 3/6/12/24 Monate aus config `decisions.horizonte_monate`) und
schreibt je Korb/Horizont Mittelwert, Median, Anteil mit Ueberrendite vs. SPY,
Differenz zu kontrolle, n und Brier-Score von p (bei n < 30: "unkalibriert, n=...")
als Tabelle und `decisions_review.csv`. Hypothesen (H1-H3) siehe `docs/hypothesen.md`.

## Trend-Layer Teil A (trends.py, v54 -- Struktur/Quellenregister/Bewertung, kein Netzabruf)

Multi-Layer-Foresight-Framework: L1 Wissenschaft/Technologie, L2 Markt/Kapital, L3 Regulierung/Politik, L4 Nachfrage/Cross-Industry. CFS je Trend = gewichteter Mittelwert der neuesten Belege je Layer (`trends.layer_gewichte`; Layer ohne Beleg = n/a, renormalisiert, Abdeckung ausgewiesen); Konvergenz ab 2 Layern mit Score >= 3; Divergenzwarnung bei niedrigem L2 zu hohem L1/L3 (oder umgekehrt). Ampel je Ticker aus exposure x CFS-Stufe: rot bei exposure <= -1 und Stufe vertiefen/handeln, gelb bei monitor, gruen sonst, n/a ohne Exposure.

```bash
python trends.py import-trends trends_start.csv          # 12 Start-Trends
python trends.py import-evidence evidence.csv            # quelle_url Pflicht, atomar mit Zeilennummer
python trends.py import-exposure exposure_template.csv   # Watchlist x Trends (gefuellt)
python trends.py score                                   # CFS/Stufe/Konvergenz/Divergenz je Trend
python trends.py overlay --watch ticker_watch_v40.csv --out trend_overlay.csv
python trends.py show TICKER | --trend NAME
```

Quellenregister: `sources.yaml` (11 Quellen, seeding in Tabelle `sources`); schemas in `schema.sql` (`sources`, `trends`, `trend_evidence`, `trend_exposure`). Kein Eintrag ohne `quelle_url`.

## Quartals-Routine (Runbook)

Nach jedem neuen 13F-Quartal (Quartalsende), in dieser Reihenfolge:

```bash
# 1) 13F-HR der Fonds laden (neue Periode in value_tracker.db)
python smart_money.py fetch

# 2) frischer Screener-Lauf ueber das Universum (ohne Resume)
python run_ranking.py --tickers tickers_sp500.csv --no-resume

# 3) Smart-Money-Overlap gegen das frische Ranking + Watchlist
python smart_money.py overlap --ranking ranking_sp500.csv --watch ticker_watch_v40.csv [--force]

# 4) Live-Update: Seite 5 "Watchlist Live" -> "Live aktualisieren"
#    (oder CLI: python live_update.py --ranking ranking_sp500.csv --watch ticker_watch_v40.csv)
#    -> Umfang = Ranking + extra_tickers + --watch + Koerbe (letzter Snapshot); Kurse + Historie + watchlist_status in die DB
#    -> Kurse + watchlist_status in value_tracker.db

# 5) Vergleichkoerbe snapshotten -- Datum = Tag des Snapshots (heute);
#    Look-ahead-Guard: Abbruch, wenn --datum vor dem filing_datum verwendeter 13F-Signale liegt
python decisions.py snapshot --korb all --datum <heute>

# 6) Review: Kurse nachfuellen, decisions_review.csv (H1-H3, Horizonte 3/6/12/24 Monate)
python decisions.py review [--heute YYYY-MM-DD]
```

## Datenbank (schema.sql)

`value_tracker.db`: `prices`, `valuations`, `watchlist_status`, `decisions` (Screener/Live) sowie `smart_money_positions`, `smart_money_options`, `smart_money_katalysator`, `smart_money_rationale`, `cusip_map` (Smart Money). Wird bei Bedarf automatisch angelegt/migriert.

## Tests

```bash
python -m compileall .
python -m pytest          # kein Netz in Tests (Fetcher-Fixtures/Mocks)
```

## Status (Stand v56)
Der Value-Screener ist vollstaendig umgesetzt: Provider-Schnittstelle (FMP/yfinance), Quality/Value-Scoring mit Renormalisierung, Ausschlussfilter, Sektor-Sonderregeln (Banken/Versicherer/REITs/Versorger/Asset Management), DCF mit 3 Szenarien und 4 Startwert-Modi, SEC-EDGAR-Abgleich mit Capex-Pruefstatus und SBC-Fallback, Robustheits-MoS, Streamlit-App (fuenf Seiten: Rangliste, Scorecard, Matrix, Import, Watchlist Live), Volllauf-CLI mit Batches/Resume, Universumslader S&P 500/STOXX 600. Darauf aufbauend die Live-Schicht: Watchlist-Tracker mit Preis-/Valuations-Historie und Entscheidungslog (decisions.py inkl. Brier-Kalibrierung und Vergleichkoerben), Live-Update mit Kaufzonen und Alarmen, Timing-Modul, Smart-Money-Pipeline (13F-Positionen von 10 Fonds inkl. Aktivisten, CUSIP-Mapping ueber cusip_map/OpenFIGI/Name-Match mit US-Composite-Filter, Options- und Katalysator-Erfassung, Vollstaendigkeitsschutz mit --force, Schema-Guard gegen veraltete DB-Schemata, ETF-Bereinigung, Rationale-Import mit Quellenpflicht fuer Score 4). Ergaenzt: v51 Signalqualitaet (CUSIP-Vergleich, aktion_max_alter_tage, Urteil gemischt, divergenz_stufe, signal_hinweis), v52 Vergleichkoerbe (snapshot --korb, review -> decisions_review.csv, docs/hypothesen.md), v53 Seite 5 Watchlist Live, v54 trends.py Teil A (Quellenregister sources.yaml, CFS/Konvergenz/Divergenz/Ampel, atomare Imports mit quelle_url-Pflicht), v55 rationale-worklist, v56 Look-ahead (Startkurs am Snapshot-Tag, start_datum, Abbruch bei --datum vor filing_datum). Alles getestet (552 passed, 1 skipped, kein Netz in Tests); smarter Overlap live: 536 Ticker, Filings bis 2026-Q2. Offene Punkte: Pershing Square meldet zuletzt 2026-Q1 (Luecke, per --force mit Warnung toleriert); rationale_import.csv mit 45 Vorlage-Zeilen wartet auf manuelle Befuellung mit echten Quellen (Score 4; Arbeitsliste via `rationale-worklist`).
