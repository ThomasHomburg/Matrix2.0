# Hypothesen H4 (v73, 2026-11-23) -- relater IRR-Korb vs. Kontrolle

Dieses Dokument friert die Definition des relativen Korbs **irr_top** und
seines Kontrollkorbs **irr_kontrolle** ein. Veraenderungen an Pool,
Auswahl, Sektorcap oder Seed erfordern ein NEUES datiertes Dokument
(gleiche Konvention wie docs/hypothesen_2026-11-19.md, das als Vorlage
dient). Die aelteren Hypothesen-Dokumente bleiben unveraendert.

## H4 (Formulierung)

**H4: Der Korb irr_top schlaegt den Korb irr_kontrolle nach 12 Monaten
(Median-Ueberrendite vs. SPY).**

- **Primaerkennzahl:** Median-Ueberrendite vs. SPY nach 12 Monaten.
- **Nebenkennzahlen:** Mittelwert-Ueberrendite vs. SPY, Anteil der Ticker
  mit Rendite > SPY, Differenz Median zu irr_kontrolle, Differenz Median
  zu RSP.
- **irr_auto ist ein impliziter Diskontsatz auf EV-Basis** (FCFF,
  Nettoverschuldung abgezogen): der Satz r, bei dem der Base-Wert je
  Aktie dem Kurs entspricht. Es ist KEINE Eigenkapitalrendite eines
  Anlegers (keine Dividenden/Rueckkaeufe enthalten).

## Ausdrueckliche Einschaetzung (kein Signifikanzversprechen)

- n = 20 (bei kleinem Pool ggf. weniger; aktuell 20). Bei n = 20 ist
  **keine statistische Signifikanz** zu erwarten -- der Korb dient als
  Vorlauf-Beobachtung (Signalrichtung), nicht als Beweis.
- **Beweis erst durch Point-in-time-Backtest** (historische Snapshots
  mit damals gueltigen Daten, keine look-ahead-Information).

## Eingefrorene Regeln (Aenderungen nur in neuem datiertem Dokument)

1. **Pool** (alle Ausschluesse nach fester Prioritaet; ein Ticker faellt
   mit dem ERSTEN zutreffenden Grund raus, gezaehlt wird dieser):
   - Quality >= 60 (dcf/decisions.baskets.irr_top.quality_min)
   - Status NICHT "Pruefen ..."/"Sonderregel noetig" (Grund: Status)
   - KEIN Flag mos:extrem, KEIN Flag wert:nahe_null
   - KEIN Flag "nicht normalisiert" -- Begruendung: 4-Jahres-FCF bei
     Zykliker-Industries kann ein Hochpunkt sein (v71-Diagnose: mos_rule
     -12 % bis -32 %), der IRR waere systematisch ueberschaetzt
   - KEIN Flag fcf_basis:legacy_nonUS -- Begruendung: Boersensuffix-
     Ticker (IFRS, levered_legacy) sind mit US-GAAP-FCFF-Werten nicht
     vergleichbar
   - dcf_fcff_status == ok (Zinsaufwand vollstaendig, FCFF belastbar)
   - irr_auto gueltig (kein NaN; Grundsaetze des IRR: Intervall
     [terminal_growth + 0.001, 0.40], keine Wurzel -> NaN, nie 0)
   - Sektor (yfinance sector) vorhanden -- der Sektorcap braucht das
     Feld; fehlt es, Grund "sektor_fehlt"
2. **Auswahl irr_top:** Top-N = min(max_ticker 20, Pool // 2), Sortierung
   irr_auto absteigend, bei Gleichstand Ticker alphabetisch; **maximal
   4 Ticker je Sektor** (max_pro_sektor; zaehlt ueber yfinance sector,
   NICHT ueber die Sonderregel-Industrieguppen).
3. **irr_kontrolle:** gleich gross wie irr_top; Zufallsauswahl (seed 42,
   random.Random(seed).sample) aus DEMSELBEN Pool OHNE die Top-Ticker;
   deterministisch (sortierte Kandidatenliste vor der Auswahl).
4. **Pool < 16 (min_pool):** gar keine irr-Koerbe, Log "Pool zu klein
   (n=..)", kein stiller Fallback.
5. **Bestehende Koerbe** (screener/divergenz/kontrolle/benchmark)
   laufen unveraendert weiter (Altauswertung reproduzierbar). Der alte
   "kontrolle"-Korb (SM-Score 1) ist fuer H4 **abgeloest** -- als
   Kontrolle fuer H4 gilt ausschliesslich irr_kontrolle (derselbe Pool
   ohne die Top-Selektion).
6. **snapshot --korb all** schreibt irr_top und irr_kontrolle mit;
   review wertet sie aus (n < 30 -> "unkalibriert" wie bisher, aber
   mit Ausweis der Kennzahlen). Look-ahead-Schutz unveraendert.

## Reale Zahlen des Erstsnapshots (S&P 500, Lauf 2026-11-23)

Voller Lauf ueber tickers_sp500.csv (503 Ticker; Resume-Cache,
SEC-companyfacts Datei-Cache). Vorbehalt: Daten sind live (yfinance/SEC
vom Laufstag); die Korb-Inhaltsliste ist ein Ist-Zustand, der Snapshot
entsteht mit dem naechsten `decisions.py snapshot --korb all`.

- **Pool-Groesse: 80** von 503 (15.9 %); Ausschluss gesamt 423.
- **Ausschlussgruende gezaehlt** (Prio-Reihenfolge,
  jeder Ticker nur mit dem ersten zutreffenden Grund):
  quality 366, nicht_normalisiert 50, fcff_status 6, mos_extrem 1.
  (Grunde "status" / "sektor_fehlt" / "kein_irr" / "wert_nahe_null" /
  "legacy_nonUS" traten selbststaendig nicht auf -- betroffene Ticker
  fielen aus Praezedenzgruenden aus: Quality-Filter zuerst.)
- **Sektorverteilung des Pools:** Technology 33, Healthcare 17,
  Industrials 9, Consumer Defensive 8, Financial Services 7,
  Consumer Cyclical 3, Communication Services 3.
- **Pool irr_auto:** min 3.39 %, Median 6.41 %, max 14.38 %.
- **Tatsaechliches Top-N:** 20 (min(20, 80//2) = 20; kein Sektor mit
  mehr als 4: Technology im Korb trotz Poolanteil 33/80 begrenzt auf 4).
- **irr_top (20):** DECK, IT, GDDY, ADBE, INTU, ZTS, FDS, SNA, COR,
  CAH, EME, MO, HSY, RMD, RL, CBOE, CPRT, ALLE, CL, PM
  (irr_auto 14.38 % bis 7.11 %; Quality 61.5 bis 98.0).
- **irr_kontrolle (20, seed 42):** MTD, APH, AAPL, PG, FIX, EW, DXCM,
  AVGO, VLTO, AMAT, NVDA, STX, META, ADSK, MSCI, KLAC, ABBV, VRSK,
  PEP, CTAS (irr_auto 3.88 % bis 6.96 %).

## Auswertung (ab erstem gueltigen Snapshot +12 Monate)

- review (decisions.py review) bildet je Korb Median/Mittelwert/UEr-
  rendite vs. SPY, Anteil > SPY, Differenz zu kontrolle und benchmark.
- Entscheidungkriterium fuer den OPTIK-Report (kein automatisches
  Handeln): Median-Ueberrendite irr_top > 0 UND >
  Median-Ueberrendite irr_kontrolle.
