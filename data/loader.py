"""Loader: laedt Fundamentaldaten eines Tickers und extrahiert Kennzahlen.

Jede Kennzahl wird mit Wert, Einheit, Waehrung und Berichtsjahr gespeichert.
Fehlende Rohwerte werden als NaN behandelt, nie als 0.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .fmp_client import FMPClient

YEARS = 5


def metric_to_dict(m: Metric) -> dict[str, Any]:
    """Metric als JSON-faehiges Dict; NaN/Infinity -> null (nie 0)."""
    return {
        "name": m.name,
        "value": m.value if math.isfinite(m.value) else None,
        "unit": m.unit,
        "currency": m.currency,
        "fiscal_year": m.fiscal_year,
    }


def metric_from_dict(data: Any) -> Metric:
    """Metric aus metric_to_dict()-Ausgabe; null -> NaN.

    ValueError bei strukturell ungueltigen Dicts (Aufrufer laedt neu).
    """
    if not isinstance(data, dict):
        raise ValueError("Metric: kein Dict")
    name = data.get("name")
    if not isinstance(name, str):
        raise ValueError("Metric: name fehlt")
    raw = data.get("value")
    try:
        value = float("nan") if raw is None else float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    return Metric(name, value, str(data.get("unit") or ""),
                  str(data.get("currency") or ""), int(data.get("fiscal_year") or 0))


@dataclass
class Metric:
    name: str
    value: float  # float('nan') wenn unbekannt
    unit: str
    currency: str
    fiscal_year: int

    def is_na(self) -> bool:
        return math.isnan(self.value)


@dataclass
class TickerMetrics:
    ticker: str
    name: str = ""
    sector: str = ""
    industry: str = ""   # Branchen-Feld (GICS Industry) -- Sektorregeln (Banken/Versicherer/Zykliker/REIT)
    country: str = ""
    source: str = ""  # Datenquelle: fmp | yfinance | demo
    # Zusatzkennzahlen fuer Sektorregeln (Banken/Versicherer: P/B,
    # Dividendenrendite; REITs: FFO-Hinweis). Je Kennzahl eine Metric.
    extras: dict[str, Metric] = field(default_factory=dict)
    market_cap: Metric | None = None
    market_cap_date: str = ""  # Stichtag der Marktkapitalisierung (ISO-Datum, Abrufdatum)
    price: Metric | None = None      # v62: Boersen-Schlusskurs (kurs_boerse), nicht mcap/Aktien
    price_date: str = ""             # Stichtag des Kurses (Abrufdatum)
    # naechster Earnings-Termin (ISO-Datum), falls der Provider ihn liefert
    next_earnings_date: str = ""
    years: dict[str, dict[str, Metric]] = field(default_factory=dict)
    # TTM (Quartalsdaten): Flussgroessen = Summe letzte 4 Quartale,
    # Bestandsgoessen = Stichtag letztes Quartal (nur yfinance-Provider).
    # EBITDA unplausibel (EBITDA < EBIT in mind. einem Jahr) -> Flag + ND/EBITDA n/a
    ebitda_unplausible: bool = False
    ttm: object | None = None            # data.ttm.TtmResult
    ttm_plausible: bool = True           # False -> Flag "Quartalsdaten unplausibel"
    ttm_fallback: bool = False           # True -> Rueckfall auf Geschaeftsjahre
    # Exaktes Geschaeftsjahres-Enddatum je Jahr (ISO) -- fuer die TTM-Basis
    # (nicht ueberlappende Geschaeftsjahre vor dem TTM-Fenster).
    fiscal_ends: dict[int, str] = field(default_factory=dict)
    # v69 (F2-B): Zustand der Kurs-EINHEIT (GBp/GBX-Thematik) des Providers:
    #   "roh_gb_pence"  -- yfinance currency GBp/GBX: Kurs in Pence, Marktkap.
    #                     faktisch GBP (Majors) -> shares_current gesperrt
    #   "unbekannt"     -- GBP-Familie ohne Normalisierungsnachweis -> gesperrt
    #   "normalisiert"  -- Einheit eindeutig (Majors) bzw. zentral normalisiert
    # Der Normalizer selbst ist NICHT Teil von F2-B; das Gate wertet nur
    # das Feld aus (kein String-Raten aus "GBP").
    fx_unit_state: str = "unbekannt"

    # ---------- v64: Serialisierung (Resume-Cache run_ranking.py) ----------

    def to_dict(self) -> dict[str, Any]:
        """Vollstaendige Serialisierung fuer den Resume-Cache (JSON-faehig).

        NaN/Infinity werden als null gespeichert (CLAUDE.md: fehlende Daten nie
        als 0), from_dict() macht daraus wieder NaN.
        """
        from data.ttm import ttm_to_dict
        return {
            "ticker": self.ticker,
            "name": self.name,
            "sector": self.sector,
            "industry": self.industry,
            "country": self.country,
            "source": self.source,
            "extras": {k: metric_to_dict(v) for k, v in self.extras.items()},
            "market_cap": metric_to_dict(self.market_cap) if self.market_cap else None,
            "market_cap_date": self.market_cap_date,
            "price": metric_to_dict(self.price) if self.price else None,
            "price_date": self.price_date,
            "next_earnings_date": self.next_earnings_date,
            "years": {fy: {n: metric_to_dict(m) for n, m in y.items()}
                      for fy, y in self.years.items()},
            "ebitda_unplausible": self.ebitda_unplausible,
            "ttm": ttm_to_dict(self.ttm) if self.ttm is not None else None,
            "ttm_plausible": self.ttm_plausible,
            "fx_unit_state": self.fx_unit_state,
            "ttm_fallback": self.ttm_fallback,
            "fiscal_ends": {int(fy): d for fy, d in self.fiscal_ends.items()},
        }

    @classmethod
    def from_dict(cls, data: Any) -> "TickerMetrics":
        """Rekonstruiert TickerMetrics aus to_dict()-Ausgabe.

        ValueError bei unvollstaendigen/struktur-invaliden Datensaetzen -- der
        Aufrufer (Resume-Cache) laedt solche Ticker dann neu.
        """
        from data.ttm import ttm_from_dict
        if not isinstance(data, dict):
            raise ValueError("Kein Dict")
        ticker = data.get("ticker")
        if not isinstance(ticker, str) or not ticker:
            raise ValueError("ticker fehlt")
        years_raw = data.get("years")
        if not isinstance(years_raw, dict):
            raise ValueError("years fehlt/ungueltig")
        m = cls(ticker=ticker)
        m.name = str(data.get("name") or "")
        m.sector = str(data.get("sector") or "")
        m.industry = str(data.get("industry") or "")
        m.country = str(data.get("country") or "")
        m.source = str(data.get("source") or "")
        extras = data.get("extras")
        if extras:
            if not isinstance(extras, dict):
                raise ValueError("extras ungueltig")
            m.extras = {str(k): metric_from_dict(v) for k, v in extras.items()}
        m.market_cap = (metric_from_dict(data["market_cap"])
                        if data.get("market_cap") else None)
        m.market_cap_date = str(data.get("market_cap_date") or "")
        m.price = metric_from_dict(data["price"]) if data.get("price") else None
        m.price_date = str(data.get("price_date") or "")
        m.next_earnings_date = str(data.get("next_earnings_date") or "")
        for fy, y in years_raw.items():
            if not isinstance(y, dict):
                raise ValueError(f"Jahr {fy} ungueltig")
            m.years[int(fy)] = {str(n): metric_from_dict(v) for n, v in y.items()}
        m.ebitda_unplausible = bool(data.get("ebitda_unplausible") or False)
        ttm_raw = data.get("ttm")
        m.ttm = ttm_from_dict(ttm_raw) if ttm_raw else None
        m.ttm_plausible = bool(data.get("ttm_plausible", True))
        m.ttm_fallback = bool(data.get("ttm_fallback") or False)
        # v69: fehlendes Feld (alte Cache-Eintraege) -> "unbekannt" (konservativ)
        m.fx_unit_state = str(data.get("fx_unit_state") or "unbekannt")
        fiscal = data.get("fiscal_ends")
        if fiscal:
            if not isinstance(fiscal, dict):
                raise ValueError("fiscal_ends ungueltig")
            m.fiscal_ends = {int(fy): str(d) for fy, d in fiscal.items()}
        return m

    def series(self, metric_name: str) -> list[Metric]:
        """Kennzahl chronologisch (aeltestes Jahr zuerst)."""
        out = []
        for fy in sorted(self.years):
            m = self.years[fy].get(metric_name)
            if m is not None:
                out.append(m)
        return out

    def latest(self, metric_name: str) -> Metric | None:
        series = self.series(metric_name)
        return series[-1] if series else None


_COUNTRY_CODES = {
    "US": "US", "USA": "US", "United States": "US", "United States of America": "US",
    "DE": "DE", "Germany": "DE", "Deutschland": "DE",
    "GB": "GB", "United Kingdom": "GB", "UK": "GB", "Great Britain": "GB",
    "FR": "FR", "France": "FR",
    "NL": "NL", "Netherlands": "NL", "Niederlande": "NL",
    "CH": "CH", "Switzerland": "CH", "Schweiz": "CH",
    "SE": "SE", "Sweden": "SE", "Schweden": "SE",
    "DK": "DK", "Denmark": "DK", "Daenemark": "DK",
    "NO": "NO", "Norway": "NO", "Norwegen": "NO",
    "FI": "FI", "Finland": "FI", "Finnland": "FI",
    "JP": "JP", "Japan": "JP",
    "CN": "CN", "China": "CN",
    "CA": "CA", "Canada": "CA", "Kanada": "CA",
    "AU": "AU", "Australia": "AU",
    "IT": "IT", "Italy": "IT", "Italien": "IT",
    "ES": "ES", "Spain": "ES", "Spanien": "ES",
    "BE": "BE", "Belgium": "BE",
    "IE": "IE", "Ireland": "IE", "Irland": "IE",
    "AT": "AT", "Austria": "AT", "Oesterreich": "AT",
    "KR": "KR", "South Korea": "KR", "Korea": "KR",
    "TW": "TW", "Taiwan": "TW",
    "IN": "IN", "India": "IN",
    "BR": "BR", "Brazil": "BR", "Brasilien": "BR",
    "MX": "MX", "Mexico": "MX",
    "ZA": "ZA", "South Africa": "ZA",
}


def country_code(country: str) -> str:
    """Land als 2-Buchstaben-Kuerzel (US, DE, ...); unbekannt -> Original."""
    if not country:
        return "?"
    c = country.strip()
    return _COUNTRY_CODES.get(c, _COUNTRY_CODES.get(c.upper(), c[:2].upper()))


def _metric(name: str, raw: Any, unit: str, currency: str, fy: int) -> Metric:
    """Baut eine Metric; fehlender Rohwert -> NaN."""
    if raw is None:
        return Metric(name, float("nan"), unit, currency, fy)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return Metric(name, float("nan"), unit, currency, fy)
    return Metric(name, value, unit, currency, fy)


def _by_year(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {int(r.get("calendarYear") or r.get("date", "0000")[:4]): r for r in rows}


def extract_metrics(
    ticker: str,
    income: list[dict[str, Any]],
    balance: list[dict[str, Any]],
    cashflow: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
) -> TickerMetrics:
    """Extrahiert die CLAUDE.md-Kennzahlen aus FMP-Rohdaten (5 Jahre, falls verfuegbar).

    FCF = OCF - |Capex|; FMP liefert Capex mit negativem Vorzeichen.
    Fehlende Komponente -> NaN.
    """
    inc_y = _by_year(income)
    bal_y = _by_year(balance)
    cf_y = _by_year(cashflow)
    all_years = sorted(set(inc_y) | set(bal_y) | set(cf_y), reverse=True)[:YEARS]

    result = TickerMetrics(ticker=ticker.upper())
    if profile:
        result.name = profile.get("companyName", "")
        result.sector = profile.get("sector", "") or "Unbekannt"
        # Industry separat (FMP: 'industry'; yfinance: uebergeben als industry)
        result.industry = profile.get("industry", "") or ""
        result.country = profile.get("country", "") or ""
        mcap = profile.get("mktCap", profile.get("marketCap"))
        # Zusatzkennzahlen (P/B, Dividendenrendite, FFO, ...) aus dem Profil
        for key in ("price_to_book", "dividend_yield", "ffo_per_share", "ffo"):
            raw = profile.get(key)
            if raw is not None:
                try:
                    result.extras[key] = Metric(key, float(raw), "ratio", "", 0)
                except (TypeError, ValueError):
                    pass
        if mcap is not None:
            # FMP liefert mktCap in absoluten USD (wie die Statements);
            # KEINE Umrechnung auf Mio. -- sonst Misch-Einheiten (Faktor 1e6)
            # gegenueber FCF/EBIT/Nettogewinn aus den Statements.
            # Hinweis: mktCap ist die AKTUELLE Marktkapitalisierung, nicht
            # der Stand zum jeweiligen Berichtsjahr.
            # v69 (F2-B): Die USD-Semantik ist eine DOKUMENTIERTE, noch zu
            # verifizierende QUELLENANNAHME (FMP-Dokumentation; keine
            # Live-Verifizierung erfolgt). Bei GBP-Familie-Tickern sperrt
            # das shares_current-Gate den Zustand "unbekannt" konservativ.
            # Die Kennzeichnung "USD" ist NICHT als Live-Beweis zu lesen.
            from datetime import date
            result.market_cap = _metric("market_cap", float(mcap), "USD", "USD",
                                        all_years[0] if all_years else 0)
            result.market_cap_date = date.today().isoformat()  # Stichtag: Abrufdatum
        # v62: Boersen-Schlusskurs aus dem Profil (FMP profile.price), falls vorhanden
        pr = profile.get("price") if profile else None
        if pr is not None:
            # v69 (F2-B): fehlendes profile.currency -> unbekannte Preis-
            # waehrung (LEERES Label, KEIN USD-Fallback): eine unterstellte
            # Waehrung wuerde im shares_current-Gate zu scheinbar gueltigen
            # Aktienzahlen fuehren. Nachgelagerte Pruefungen scheiden NaN aus.
            cur = str(profile.get("currency") or "").upper().strip()
            result.price = _metric("price", float(pr), cur, cur, all_years[0] if all_years else 0)
            from datetime import date as _d
            result.price_date = _d.today().isoformat()

    for fy in all_years:
        inc = inc_y.get(fy, {})
        bal = bal_y.get(fy, {})
        cf = cf_y.get(fy, {})
        # Exaktes Geschaeftsjahres-Enddatum (erstes verfuegbares Statement-Datum)
        for row in (inc, bal, cf):
            d = row.get("date")
            if d:
                result.fiscal_ends[fy] = str(d)[:10]
                break
        # FMP-Statements tragen die Waehrung im Feld "reportedCurrency".
        currency = (
            inc.get("reportedCurrency") or inc.get("currency")
            or bal.get("reportedCurrency") or bal.get("currency")
            or cf.get("reportedCurrency") or cf.get("currency")
            or "USD"
        )

        defs = {
            "revenue": (inc.get("revenue"), "currency"),
            "gross_profit": (inc.get("grossProfit"), "currency"),
            "ebit": (inc.get("operatingIncome"), "currency"),
            "ebitda": (inc.get("ebitda") or inc.get("ebitdaLoss"), "currency"),
            "net_income": (inc.get("netIncome"), "currency"),
            "interest_expense": (inc.get("interestExpense"), "currency"),
            "income_tax_expense": (inc.get("incomeTaxExpense"), "currency"),
            "income_before_tax": (inc.get("incomeBeforeTax"), "currency"),
            "shares_diluted": (inc.get("weightedAverageSharesDiluted"), "shares"),
            "equity": (bal.get("totalStockholdersEquity"), "currency"),
            "net_debt": (bal.get("netDebt"), "currency"),
            "_total_debt": (bal.get("totalDebt"), "currency"),
            "_cash": (bal.get("cashAndCashEquivalents"), "currency"),
            "_short_term_investments": (bal.get("shortTermInvestments")
                                        or bal.get("otherShortTermInvestments"), "currency"),
            "customer_deposits": (bal.get("depositLiabilities")
                                  or bal.get("customerDeposits"), "currency"),
            "operating_cashflow": (cf.get("operatingCashFlow"), "currency"),
            "capex": (cf.get("capitalExpenditure"), "currency"),
            "stock_based_compensation": (cf.get("stockBasedCompensation"), "currency"),
            "depreciation_amortization": (cf.get("depreciationAndAmortization")
                                          or cf.get("depreciationAndAmortizationCashFlow"), "currency"),
        }
        year_metrics: dict[str, Metric] = {
            name: _metric(name, raw, unit, currency, fy) for name, (raw, unit) in defs.items()
        }

        # EBITDA = EBIT + Abschreibungen (D&A) aus dem Cashflow-Statement,
        # NICHT das gemeldete Yahoo/FMP-EBITDA-Feld (das weicht teils ab).
        # Fallback nur wenn kein D&A gemeldet: gemeldetes EBITDA.
        ebit_v = year_metrics["ebit"].value
        da_v = year_metrics["depreciation_amortization"].value
        rep_ebitda = year_metrics["ebitda"].value
        if not math.isnan(ebit_v) and not math.isnan(da_v):
            year_metrics["ebitda"] = Metric("ebitda", ebit_v + da_v, currency, currency, fy)
        elif math.isnan(year_metrics["ebitda"].value):
            pass  # gemeldetes EBITDA (ggf. NaN) bleibt
        if (not math.isnan(year_metrics["ebitda"].value) and not math.isnan(ebit_v)
                and year_metrics["ebitda"].value < ebit_v):
            result.ebitda_unplausible = True

        # FCF = OCF - |Capex|; FMP liefert Capex mit negativem Vorzeichen.
        # Fehlende Komponente -> NaN
        ocf = year_metrics["operating_cashflow"].value
        capex = year_metrics["capex"].value
        fcf = ocf + capex if not (math.isnan(ocf) or math.isnan(capex)) else float("nan")
        year_metrics["fcf"] = Metric("fcf", fcf, currency, currency, fy)

        # FCF nach SBC = FCF - Aktienverguetung (SBC ist bei FMP positiv ausgewiesen;
        # fehlende SBC -> NaN, nie 0 -- sonst wuerde der Roh-FCF verwendet)
        sbc = year_metrics["stock_based_compensation"].value
        fcf_sbc = fcf - sbc if not (math.isnan(fcf) or math.isnan(sbc)) else float("nan")
        year_metrics["fcf_sbc"] = Metric("fcf_sbc", fcf_sbc, currency, currency, fy)

        result.years[fy] = year_metrics
    return result


class DataLoader:
    def __init__(self, client: FMPClient, years: int = YEARS) -> None:
        self.client = client
        self.years = years

    def load_ticker(self, ticker: str) -> TickerMetrics:
        """Laedt Rohdaten fuer einen Ticker und extrahiert die Kennzahlen."""
        income = self.client.income_statements(ticker, years=self.years)
        balance = self.client.balance_sheets(ticker, years=self.years)
        cashflow = self.client.cash_flows(ticker, years=self.years)
        profile = self.client.profile(ticker)
        return extract_metrics(ticker, income, balance, cashflow, profile)
