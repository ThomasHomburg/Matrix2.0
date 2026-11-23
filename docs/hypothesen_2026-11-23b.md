# Hypothesen H4 (v73b, 2026-11-23) -- sektorgematchte Kontrolle + Poolbaseline

DIESES DOKUMENT LOEST **docs/hypothesen_2026-11-23.md** ab (dort
festgelegte H4-Version ist ersetzt; die Datei bleibt unveraendert als
historischer Beleg). Begruendung: Die dort definierte Kontrolle
(irr_kontrolle, reiner Seed-42-Zufallskorb aus dem Pool) war stilfremd
zur Top-Selektion -- im realen Pool vom 2026-11-23 hatte die Kontrolle
einen Technology-Anteil von ca. 40 % gegenueber ca. 20 % im Top-Korb; ein
Mismatch der Sektorverteilung wuerde einen Teil der potentiellen
Ueberrendite dem SektorEffekt statt der IRR-Selektion zuschreiben. Zum
Zeitpunkt der Aenderung existiert noch KEIN Snapshot mit dem alten Korb;
die Uhr fuer H4 startet erst mit dem ersten Snapshot nach diesem
Dokument (Start der Uhr vor dem ersten Messlauf, kein Blick zurueck).

## H4 (neue Formulierung, gueltig ab hier)

**H4: Der Korb irr_top schlaegt den Korb irr_kontrolle (SEKTORGEMATCHT,
siehe unten) nach 12 Monaten (Median-Ueberrendite vs. SPY).**

- **Primaerkennzahl:** Median-Ueberrendite vs. SPY nach 12 Monaten,
  Differenz zu irr_kontrolle.
- **Nebenkennzahlen:** vs. irr_pool (alle Pool-Ticker ohne Top,
  gleichgewichtet -- Basislinie), vs. RSP (benchmark).
- **irr_auto bleibt der implizite Diskontsatz auf EV-Basis** (FCFF,
  Nettoverschuldung abgezogen), KEINE Eigenkapitalrendite.
- n = 20: keine statistische Signifikanz erwartet; Korb = Vorlauf;
  Beweis erst durch Point-in-time-Backtest.

## Eingefrorene Regeln (Aenderungen nur in neuem datierten Dokument)

Unveraendert uebernommen aus docs/hypothesen_2026-11-23.md:

1. Pool-Definition (Quality >= 60; Status nicht Pruefen/Sonderregel;
   keine Flags mos:extrem, wert:nahe_null, nicht normalisiert,
   fcf_basis:legacy_nonUS; dcf_fcff_status == ok; irr_auto gueltig;
   Sektor vorhanden) mit fester Ausschlussprioritaet und gezaehlten
   Gruenden.
2. Auswahl irr_top: Top-N = min(max_ticker 20, Pool//2) nach irr_auto
   absteigend, max 4 je Sektor (yfinance sector), Gleichstand Ticker
   alphabetisch.
3. min_pool 16 (weniger -> gar keine irr-Koerbe), seed 42.
4. Bestehende Koerbe screener/divergenz/kontrolle/benchmark unveraendert;
   der alte kontrolle-Korb (SM-Score 1) bleibt fuer H4 abgeloest.

**Neu (v73b, hiermit eingefroren):**

5. **irr_kontrolle ist sektorgematcht:** je Sektor genau so viele Ticker
   wie irr_top aus diesem Sektor hat; Zufallsauswahl (seed 42,
   random.Random(seed).sample) aus der nach Ticker sortierten
   Pool-Restliste (ohne die irr_top-Ticker), je Sektor eigene aufsteigend
   sortierte Kandidatenliste, Sektoren in alphabetischer Reihenfolge,
   EIN gemeinsamem Zufallsgeneratorueber die Zuege -- volle Deterministik.
   Reicht ein Sektor im Rest nicht aus, wird die Differenz mit
   Zufallstickern aus dem uebrigen Rest gefuellt und pro Sektor im Log
   ausgewiesen ("Sektor X: n von m gematcht").
6. **irr_pool (neuer Korb):** ALLE Pool-Ticker ohne die Top
   (gleichgewichtet), Basislinie im review. Aufnahme in KORB_NAMEN und
   snapshot --korb all.
7. **review-Kennzahl:** je Korb "Differenz Median vs. irr_pool"
   (median_diff_irr_pool_pct); fuer irr_top ist das die Kennzahl
   "Differenz Median irr_top vs. irr_pool". Die Kennzahl
   median_diff_kontrolle_pct bleibt daneben bestehen (vergleicht gegen
   den SEKTORGEMATCHTEN irr_kontrolle).

## Reale Zahlen des Erstsnapshots (S&P 500, Lauf 2026-11-23)

Pool 80 von 503 (Ausschluss gezaehlt: quality 366, nicht_normalisiert 50,
fcff_status 6, mos_extrem 1; Pool-Sektoren Technology 33, Healthcare 17,
Industrials 9, Consumer Defensive 8, Financial Services 7, Consumer
Cyclical 3, Communication Services 3; Pool-IRR min 3.39 %, Median
6.41 %, max 14.38 %). Sektorverteilungen von irr_top (20) und neuem
sektorgematchten irr_kontrolle (20) sind ab dem ersten Snapshot nach
diesem Dokument Bestandteil des Snapshots; der Stand vom Lauf 2026-11-23:

| Sektor | irr_top | irr_kontrolle (sektorgematcht) |
|---|---|---|
| Technology | 4 | 4 |
| Healthcare | 4 | 4 |
| Industrials | 4 | 4 |
| Consumer Defensive | 4 | 4 |
| Consumer Cyclical | 2 | 1 (+1 Zufallsauffuellung) |
| Financial Services | 2 | 2 |

Detail: Consumer Cyclical hat im Pool-Rest nur 1 Ticker (ROL; DECK und RL
sind im Top) -> "Sektor Consumer Cyclical: 1 von 2 gematcht", die Differenz
wurde deterministisch (seed 42) aus dem uebrigen Rest aufgefuellt (landete
in Technology -> dort 5 statt 4, ausgewiesen im Log). irr_pool: n=60
(80 Pool-Ticker minus 20 Top-Ticker).

irr_top (20): DECK 14.4 %, IT 14.0 %, GDDY 12.2 %, ADBE 12.1 %,
INTU 10.4 %, ZTS 9.1 %, FDS 8.9 %, SNA 8.7 %, COR 8.6 %, CAH 8.6 %,
EME 8.2 %, MO 8.2 %, HSY 8.0 %, RMD 8.0 %, RL 7.9 %, CBOE 7.8 %,
CPRT 7.7 %, ALLE 7.5 %, CL 7.2 %, PM 7.1 %.

irr_kontrolle (20, sektorgematcht, seed 42): ROL, KMB, PEP, KO, PG,
MCO, V, VRTX, ABBV, MTD, MCK, CAT, VLTO, VRSK, FIX, ADP, AVGO, BR,
MSFT, TER (irr_auto 3.7 % bis 8.5 %).

Die definitive DB-Besetzung entsteht mit dem ersten
`decisions.py snapshot --korb all` nach diesem Dokument.

## Auswertung

review (decisions.py review) bildet alle Koerbe mit Horizonten; fuer H4
ist die Entscheidungskennzahl: Median-Ueberrendite vs. SPY (irr_top) >
Median-Ueberrendite vs. SPY (irr_kontrolle) UND > 0. Nebenkennzahlen vs.
irr_pool und RSP werden ausgewiesen; n < 30 -> "unkalibriert" (wie
bisher), Kennzahlen werden trotzdem ausgewiesen.
