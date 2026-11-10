-- Datenhaltung des Value-Screeners in SQLite (value_tracker.db).
-- Reine Ablage -- die Screener-Logik bleibt unveraendert (neues Modul).
--
-- value_tracker.db anlegen:
--   sqlite3 value_tracker.db < schema.sql
-- (oder decisions.py legt die Tabellen beim ersten Aufruf automatisch an:
--  init_db() fuehrt dieses Schema aus.)

PRAGMA journal_mode = WAL;

-- (a) prices: Tagesschlusskurse je Ticker (Quelle: yfinance, live_update.py)
CREATE TABLE IF NOT EXISTS prices (
    ticker      TEXT    NOT NULL,
    datum       TEXT    NOT NULL,              -- ISO-Datum (YYYY-MM-DD), Tagesschluss
    kurs        REAL    NOT NULL,              -- Schlusskurs in Quote-Waehrung
    quelle      TEXT    NOT NULL DEFAULT 'yfinance',
    geladen_am  TEXT    NOT NULL,              -- ISO-Zeitpunkt des Abrufs
    PRIMARY KEY (ticker, datum)
);
CREATE INDEX IF NOT EXISTS idx_prices_ticker_datum ON prices (ticker, datum);

-- (b) valuations: Snapshot der Screener-Bewertung je calc_date
--     (eine Zeile pro Ticker und Berechnungstag; kommt aus run_ranking.py /
--      ranking CSV -- Felder siehe Spaltenliste run_ranking._write_ranking_csv)
CREATE TABLE IF NOT EXISTS valuations (
    ticker          TEXT NOT NULL,
    calc_date       TEXT NOT NULL,             -- ISO-Datum der Bewertung
    kurs            REAL,                      -- Kurs bei calc_date (Export )
    kurs_datum      TEXT,                      -- Stichtag des Kurses
    status          TEXT,                      -- Kandidat / Beobachtung / nein / Pruefen ...
    quality         REAL,
    value           REAL,
    mos_base        REAL,                      -- margin_of_safety (aktiver Modus)
    mos_min         REAL,                      -- mos_rule (min_all: Minimum der Modi)
    mos_robust      REAL,                      -- MoS bei 10 % (robust)
    iv_bear         REAL,                      -- Wert je Aktie Bear (aktiver Modus)
    iv_base         REAL,                      -- Wert je Aktie Base (aktiver Modus)
    iv_bull         REAL,                      -- Wert je Aktie Bull (aktiver Modus)
    iv_avg3         REAL,                      -- iv_base je Startwert-Modus
    iv_latest       REAL,
    iv_ttm          REAL,
    iv_ttm_only     REAL,
    iv_base_10pct   REAL,                      -- iv_base bei robust_discount_rate
    naechster_earnings TEXT,                   -- naechster Earnings-Termin (ISO)
    flags           TEXT,                      -- Flags des Laufs (Leerzeichen-getrennt)
    quelle_csv      TEXT,                      -- ranking CSV, aus der der Snapshot stammt
    PRIMARY KEY (ticker, calc_date)
);
CREATE INDEX IF NOT EXISTS idx_valuations_calc ON valuations (calc_date);

-- (c) watchlist_status: letzte Live-Aktualisierung je Watchlist-Ticker
--     (live_update.py --ranking <csv> schreibt watchlist_live.csv UND diese Tabelle)
CREATE TABLE IF NOT EXISTS watchlist_status (
    ticker          TEXT NOT NULL,
    updated_at      TEXT NOT NULL,             -- ISO-Zeitpunkt des Live-Laufs
    kurs            REAL,                      -- aktueller Tagesschlusskurs
    status_screener TEXT,                      -- Status aus dem Screener-Lauf (ranking CSV)
    status_live     TEXT,                      -- Status bei aktuellem Kurs (is_buy_candidate)
    mos_live_min    REAL,                      -- MoS live, Minimum ueber die Modi (min_all)
    mos_live_robust REAL,                      -- MoS live bei iv_base_10pct
    kaufzone_kurs   REAL,                      -- hoechster Kurs, der die Kandidatenregel erfuellt
    distanz_pct     REAL,                      -- kaufzone_kurs / kurs - 1
    timing_status   TEXT,                      -- positiv / neutral / negativ (timing.py)
    wert_veraltet   INTEGER NOT NULL DEFAULT 0,  -- 1 = nach naechster_earnings oder > 100 Tage alt
    alarm           INTEGER NOT NULL DEFAULT 0,  -- 1 = Kurs <= -10 % seit calc_date oder Tagesbewegung >= +/-8 %
    PRIMARY KEY (ticker)
);

-- (d) decisions: Entscheidungslog (decisions.py add/snapshot/review)
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    datum           TEXT NOT NULL,             -- ISO-Datum der Entscheidung
    aktion          TEXT NOT NULL,             -- watch | kauf | verkauf | nichts | auto
    kurs            REAL NOT NULL,             -- Kurs bei der Entscheidung
    status_screener TEXT,                      -- Screener-Status zum Zeitpunkt
    iv_base         REAL,                      -- innerer Wert Base zum Zeitpunkt
    iv_bear         REAL,
    iv_bull         REAL,
    gew_bear        REAL NOT NULL DEFAULT 0.25,  -- Szenariogewichte (Default 25/50/25)
    gew_base        REAL NOT NULL DEFAULT 0.50,
    gew_bull        REAL NOT NULL DEFAULT 0.25,
    erwartungswert_pct REAL,                   -- p * 100 (p = Summe Gewichte der Szenarien mit iv >= 1,10 x Kurs)
    horizont_monate INTEGER NOT NULL DEFAULT 12, -- 12 oder 24
    thesis          TEXT,                      -- Begruendung (Pflicht bei manuellen Eintraegen)
    break_condition TEXT,                      -- Invalidierung ("break")
    -- review (decisions.py review): wird nach 12/24 Monaten nachgefuellt
    kurs_nachher    REAL,                      -- Kurs nach Ablauf des Horizonts
    rendite_pct     REAL,                      -- Rendite nach Ablauf des Horizonts in %
    ereignis_e      INTEGER,                   -- 1 = Rendite nach 12 M >= +10 %, sonst 0 (NULL bis review)
    brier           REAL,                      -- (p - E)^2 (NULL bis review)
    reviewed_at     TEXT,                      -- ISO-Zeitpunkt des review
    -- v52 Vergleichkoerbe (snapshot --korb); korb <> '' kennzeichnet Korb-Zeilen,
    -- Alt-DBs bekommen die Spalten per ALTER in decisions._migrate_decisions
    korb            TEXT NOT NULL DEFAULT '',
    kurs_datum      TEXT,
    quality         REAL,
    mos_min         REAL,
    mos_robust      REAL,
    kaufzone_kurs   REAL,
    timing_status   TEXT,
    smart_money_score INTEGER,
    urteil          TEXT,                      -- smart_money_urteil aus overlap-CSV
    divergenz_stufe TEXT,                      -- stark|schwach|- (v51)
    fonds_kaeufer   TEXT,
    fonds_verkaeufer TEXT,
    wahrscheinlichkeit_p REAL,                  -- p = erwartungswert_pct/100 (Rendite >= +10 % in 12 M)
    start_datum     TEXT                        -- v56 Look-ahead: Startdatum der Rendite je Zeile =
                                                -- max(Snapshot-Datum, juengstes filing_datum der verwendeten 13F)
);
CREATE INDEX IF NOT EXISTS idx_decisions_ticker ON decisions (ticker);
-- Idempotenz der Korb-Snapshots: (ticker, korb, datum) eindeutig -- geschriebene
-- Zeilen werden nie ueberschrieben (partieller Index, Legacy-Zeilen korb='' unberuehrt)
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_korb_datum ON decisions(ticker, korb, datum) WHERE korb <> '';
CREATE INDEX IF NOT EXISTS idx_decisions_datum ON decisions (datum);

-- Smart Money -- 13F-Positionen; eindeutiger Schluessel (fonds, periode, cusip, accession)
CREATE TABLE IF NOT EXISTS smart_money_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fonds           TEXT NOT NULL,             -- Fondsname (config smart_money.funds)
    periode         TEXT NOT NULL,             -- YYYY-Qn aus reportDate (v48; v47: exklusiv reportDate)
    report_date     TEXT,                      -- reportDate des Filings (Basis der Periode + Migration)
    accession       TEXT NOT NULL DEFAULT '',  -- Accession des 13F (13F-HR/A ersetzt Original derselben Periode)
    filing_datum    TEXT,                      -- ISO-Datum des (neuesten) 13F-Filings
    cusip           TEXT NOT NULL DEFAULT '',  -- CUSIP der Position
    ticker          TEXT,                      -- gemappt; NULL wenn nicht zuordenbar (v48)
    name            TEXT,                      -- nameOfIssuer
    title_of_class  TEXT,                      -- titleOfClass (z. B. COMMON STOCK, BOND)
    shares          REAL,
    wert            REAL,                      -- Marktwert USD (infotable: 1000 USD x 1000)
    anteil_portfolio REAL,                     -- wert / Gesamtwert Aktien des Fonds-Quartals
    aktion          TEXT NOT NULL,             -- neu | erhoeht | reduziert | verkauft | unveraendert | basis | unbekannt
                                               -- (v51: Vergleich ueber CUSIP; "unbekannt" bei fehlendem Vorquartals-Filing
                                               --  oder Mapping-Luecke statt "neu")
    aenderung_pct   REAL,                      -- Veraenderung shares gg. Vorquartal
    positions_gesamt INTEGER,                  -- Anzahl Aktien-Positionen des Fonds im Quartal
    rotation_pct    REAL,                      -- Umgeschichteter Wert / Portfoliowert Vorquartal
    geladen_am      TEXT NOT NULL,
    UNIQUE (fonds, periode, cusip, accession)
);
CREATE INDEX IF NOT EXISTS idx_smp_ticker ON smart_money_positions (ticker);
CREATE INDEX IF NOT EXISTS idx_smp_periode ON smart_money_positions (periode);
CREATE INDEX IF NOT EXISTS idx_smp_fonds_periode ON smart_money_positions (fonds, periode);

-- Optionen (PUT/CALL) -- getrennt gefuehrt, zaehlen NICHT in Score/Anteil
CREATE TABLE IF NOT EXISTS smart_money_options (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fonds       TEXT NOT NULL,
    periode     TEXT NOT NULL,                 -- wie smart_money_positions
    filing_datum TEXT,
    ticker      TEXT,                          -- gemappt (GOOGL->GOOG); NULL wenn nicht zuordenbar (v48)
    put_call    TEXT NOT NULL,                 -- PUT | CALL
    shares      REAL,
    wert        REAL,
    geladen_am  TEXT NOT NULL,
    UNIQUE (fonds, periode, ticker, put_call)
);

-- Katalysatoren (13D/13D-A der Aktivisten, gewicht 0)
CREATE TABLE IF NOT EXISTS smart_money_katalysator (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,                 -- Ziel-Ticker des 13D
    fonds       TEXT NOT NULL,                 -- aktivistischer Filer
    datum       TEXT,                          -- filingDate des 13D/13D-A
    quelle_url  TEXT,                          -- EDGAR-Archiv-URL
    form        TEXT                           -- 13D | 13D-A
);
CREATE INDEX IF NOT EXISTS idx_smcat_ticker ON smart_money_katalysator (ticker);

-- dauerhaftes CUSIP->Ticker-Mapping; wird nicht erneut abgefragt
CREATE TABLE IF NOT EXISTS cusip_map (
    cusip       TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    quelle      TEXT,                      -- openfigi | name_match | manuell
    geladen_am  TEXT NOT NULL,
    is_etf      INTEGER NOT NULL DEFAULT 0 -- 1 = ETF/Index (OpenFIGI securityType/marketSector oder config etf_ticker); alte DBs per ALTER in connect() migriert
);

-- Rationale (nur manuell oder per Import rationale_import.csv, Pflicht quelle_url)
CREATE TABLE IF NOT EXISTS smart_money_rationale (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    fonds       TEXT,
    text        TEXT NOT NULL,
    quelle_url  TEXT NOT NULL,                 -- Pflichtfeld
    datum       TEXT,
    typ         TEXT                           -- Brief | Interview | 13D | Sonstiges
);
CREATE INDEX IF NOT EXISTS idx_smr_ticker ON smart_money_rationale (ticker);
-- Duplikat (ticker, fonds, quelle_url) wird per INSERT OR REPLACE ueberschrieben, nicht doppelt angelegt
CREATE UNIQUE INDEX IF NOT EXISTS idx_smr_unique ON smart_money_rationale (ticker, COALESCE(fonds, ''), quelle_url);

-- Trends (v54, Trend-Layer Teil A: Struktur + Quellenregister + Bewertung) --
-- Multi-Layer-Foresight-Framework (Masterarbeit): L1 Wissenschaft/Technologie,
-- L2 Markt/Kapital, L3 Regulierung/Politik, L4 Nachfrage/Cross-Industry.
CREATE TABLE IF NOT EXISTS sources (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,          -- z. B. "SEC EDGAR (10-K/10-Q, Form 4)"
    layer               TEXT NOT NULL,          -- L1 | L2 | L3 | L4
    vorlauf_monate      REAL,                   -- Vorlauf der Quelle in Monaten (0 = sofort)
    zugang              TEXT,                   -- offen/API | API-Key | kostenpflichtig | intern
    kosten              TEXT,                   -- kostenlos | Basis kostenlos | Abo
    aktualisierung      TEXT,                   -- taeglich | woechentlich | quartalsweise | monatlich | 15-min
    zuverlaessigkeit    TEXT,                   -- hoch | mittel | niedrig
    lizenz              TEXT                    -- z. B. public domain, CC0, CC BY, proprietaer
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_name ON sources (name);

CREATE TABLE IF NOT EXISTS trends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,                  -- eindeutiger Trend-Name
    typ         TEXT,                           -- technologie | markt | regulierung | nachfrage | zyklus | makro
    richtung    TEXT,                           -- positiv | negativ | gemischt
    horizont    TEXT,                           -- z. B. 2y | 3y | 5y | 10y
    status      TEXT NOT NULL DEFAULT 'aktiv'   -- aktiv | beobachten | archiviert
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_trends_name ON trends (name);

-- Belege (Evidence) je Trend und Layer; score 1..4 mit beobachtbarem Beleg.
-- quelle_url ist Pflicht (kein Eintrag ohne Quelle).
CREATE TABLE IF NOT EXISTS trend_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trend_id        INTEGER NOT NULL REFERENCES trends(id),
    layer           TEXT NOT NULL,              -- L1 | L2 | L3 | L4
    score           INTEGER NOT NULL,           -- 1..4
    beleg_text      TEXT,                       -- beobachtbarer Beleg (was, wann, wie gross)
    quelle_id       INTEGER REFERENCES sources(id),
    quelle_url      TEXT NOT NULL,              -- Pflichtfeld (atomarer Import prueft das)
    beleg_datum     TEXT,                       -- Datum des Belegs (ISO)
    abruf_datum     TEXT,                       -- Datum des Abrufs (ISO)
    erfasst_von     TEXT NOT NULL DEFAULT 'manuell'  -- manuell | regel | llm_vorschlag
);
CREATE INDEX IF NOT EXISTS idx_te_trend ON trend_evidence (trend_id);
CREATE INDEX IF NOT EXISTS idx_te_trend_layer ON trend_evidence (trend_id, layer);

-- Exposure je Watchlist-Ticker und Trend (-2..+2); quelle_url Pflicht.
CREATE TABLE IF NOT EXISTS trend_exposure (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    trend_id        INTEGER NOT NULL REFERENCES trends(id),
    exposure        INTEGER NOT NULL,           -- -2 .. +2
    begruendung     TEXT,
    quelle_url      TEXT NOT NULL,              -- Pflichtfeld
    datum           TEXT,                       -- ISO-Datum der Einschaetzung
    erfasst_von     TEXT NOT NULL DEFAULT 'manuell',  -- manuell | regel | llm_vorschlag
    geprueft        TEXT NOT NULL DEFAULT 'nein'     -- ja | nein
);
CREATE INDEX IF NOT EXISTS idx_tx_ticker ON trend_exposure (ticker);
CREATE INDEX IF NOT EXISTS idx_tx_trend ON trend_exposure (trend_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tx_unique ON trend_exposure (ticker, trend_id);
