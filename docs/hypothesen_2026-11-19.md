# Methodenwechsel v70: FCFF-Basis im DCF (19.11.2026)

## Anlass und Nachweis

Der Start-FCF des DCF ist `OCF - Capex`, also **nach** Zinsen (levered).
`_dcf_scenario` zieht trotzdem die Nettoverschuldung vom Unternehmenswert
ab (`equity = EV - net_debt`). Verschuldung wurde dadurch **doppelt
bestraft**: Die Zinslast senkt den Start-FCF, die Schulden senken zusätzlich
den Equity-Wert. Symptome aus dem v61-Live-Lauf: LLY, TDG, KDP, AMCR mit
`iv_base` 0,10-0,74 USD (Sanity-Flag "wert:nahe_null"), obwohl die
Unternehmen operativ solide sind (keine Einheitenfehler, keine Datenlücken -
v61 diagnostizierte ohne Fix, da der Beleg der methodischen Ursache fehlte).

## Methodenwechsel (v70)

- **FCFF-Basis** (`dcf.fcf_basis: fcff`, Default):
  Start-FCF je Quelle wird um den Zinsaufwand **nach Steuern** angehoben:
  `FCFF = FCF + |Zinsaufwand| × (1 - Steuersatz)`.
  Der Diskontsatz (`dcf.discount_rate`) bleibt als WACC-Näherung, der
  Nettoverschuldungsabzug bleibt - jetzt konsistent unlevered.
- **Quellenweise Adjustierung vor der FX-Konvertierung** (TTM-Quelle: TTM-
  Zinsaufwand und TTM-Steuersatz aus der Summe der letzten 4 Quartale;
  FY-Quelle: Jahreswerte). Gewichtung und Quellenauswahl unveraendert.
- **Steuersatz:** effektiver Satz (bestehende Logik `data.tax_rate_*`,
  geclamped 0-35 %, Normsatz 21 % bei unplausibel/Verlust). Fehlt der
  Steuersatz bei Zinsen > 0, gilt der Normsteuersatz mit Flag
  `steuersatz_normiert` (Entscheidung D1=B).
- **Fehlender oder NaN-Zinsaufwand: strikt n/a** (kein stiller Fallback auf
  levered Werte; CLAUDE.md "fehlende Daten = NaN"). Zinsaufwand exakt **0 ist
  ein gueltiger Wert**.
- **IFRS-Schutz:** Ticker **mit** Börsen-Suffix (`.L`, `.DE`, ...; US-GAAP-
  Ticker haben keins) bleiben auf `levered_legacy`, Flag
  `fcf_basis:legacy_nonUS` (IAS 7 erlaubt Zinszahlungen im Finanzierungs-
  Cashflow; der Aufschlag koennte dort doppelt zaehlen).
- **Fluchtweg:** `dcf.fcf_basis: levered_legacy` reproduziert das alte
  Verhalten exakt (dokumentierte Alt-Methodik fuer Vergleiche).

## Auswirkung auf bestehende Körbe

**Alle Körbe und Entscheidungen vor dem 19.11.2026 basieren auf dem Modell
v62-v69 (levered FCF + ND-Abzug).** Ihre `iv_base`/MoS-Werte sind mit dem
v70-Modell **nicht unmittelbar vergleichbar**:

- Alt-Zeilen in `valuations`/`decisions` werden beim naechsten
  `live_update`-Lauf nur **markiert** (Flag `fcff_vor_v70`), nichts wird
  geloescht oder neu gerechnet.
- Korb-Zeilen am Snapshot-Datum erhalten den konfigurierten Hinweis
  (`decisions.fcff_fix.hinweis`).
- Vergleichs-/Review-Renditen bleiben unveraendert: `rendite_pct` und Start-
  Kurse sind historische Fakten und werden vom Methodenwechsel nicht
  beeinflusst (wie beim v62-Aktienbasis-Fix).
- Bei hoch verschuldeten Positionen (LLY, TDG, KDP, AMCR-typisch) ist künftig
  mit deutlich hoeheren inneren Werten zu rechnen; Kaufzonen/p aus
  Alt-Snapshots sind nicht in das neue Modell uebertragbar.

## Offene nachgelagerte Punkte (je eigene Freigabe)

- FMP-`mktCap`-USD-Live-Verifizierung, GBp/GBX-Normalisierung,
  CSV-Schema-Erweiterung (v69/v70-Row-Felder), `live_update`-Anbindung der
  neuen Felder.
