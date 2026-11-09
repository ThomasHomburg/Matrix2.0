# Hypothesen -- Vergleichskoerbe im Entscheidungslog

Erstellt am: 2026-11-09.
**Dieses Dokument ist nach Erstellung unveraenderlich.** Aenderungen oder Ergaenzungen
erfolgen nur in einem neuen, datierten Dokument (z. B. `hypothesen_2027-01-01.md`).

Koerbe (config.yaml `decisions.baskets`, Snapshot: `decisions.py snapshot --korb all
--datum <Datum des 13F-Quartals>`):

- `screener`: alle Ticker mit Status Kandidat / Beobachtung / Beobachtung (nicht normalisiert)
- `divergenz`: Status nein + divergenz_stufe stark + Quality >= 60, max. 15 Ticker
  (sortiert nach Smart-Money-Score, dann Quality)
- `kontrolle`: 15 Ticker per Zufall (fester Seed 42) aus Status nein mit
  Smart-Money-Score 1 und ohne Fondsaktivitaet
- `benchmark`: SPY (Referenz) und RSP (gleichgewichtet)

## Hypothesen

- **H1**: Korb `screener` schlaegt `kontrolle` nach 12 Monaten
  (Median-Ueberrendite gegenueber SPY > 0).
- **H2**: Korb `divergenz` schlaegt `kontrolle` nach 12 Monaten
  (Median-Ueberrendite gegenueber SPY > 0).
- **H3**: Korb `divergenz` schlaegt `screener` **nicht**
  (Median-Ueberrendite `divergenz` vs. `screener` <= 0).

## Horizonte

3, 6, 12 und 24 Monate (config `decisions.horizonte_monate`, 30 Tage/Monat).

Auswertung: `python decisions.py review [--heute JJJJ-MM-TT]` -> Tabelle +
`decisions_review.csv` (je Korb/Horizont: n, Mittelwert, Median, Anteil mit
Ueberrendite vs. SPY, Differenz zu kontrolle, Brier-Score von p; bei n < 30:
"unkalibriert, n=..."). Ereignis E = Rendite nach 12 Monaten >= +10 %
(config `watchlist.ereignis_rendite_pct`).
