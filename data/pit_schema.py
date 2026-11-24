# v75: Point-in-time-Fundamentalspeicher pit.db -- Schema und Tag-Whitelist.
# Nur Aufnahme und Abfrage (data/pit.py); KEIN Adapter, KEIN Backtest.
#
# Privat-Herleitungen sind aus der v74-Diagnose uebernommen (tools/v74_*.py):
# alle Facts = USD bzw. shares, Formen 10-K/10-K/A/10-Q/10-Q/A,
# Periodendauer erst bei der Abfrage gefiltert.

import sqlite3

PIT_DB_PFAD = "data/pit.db"

# DDL
DDL_PIT_FACTS = """
CREATE TABLE IF NOT EXISTS pit_facts (
    cik10        TEXT NOT NULL,
    tag          TEXT NOT NULL,
    unit         TEXT NOT NULL,
    period_start TEXT,
    period_end   TEXT NOT NULL,
    filed_date   TEXT NOT NULL,
    form         TEXT NOT NULL,
    accn         TEXT NOT NULL,
    value        REAL NOT NULL,
    PRIMARY KEY (cik10, tag, unit, period_start, period_end, filed_date,
                 form, accn)
)
"""

DDL_PIT_ENTITY = """
CREATE TABLE IF NOT EXISTS pit_entity (
    cik10            TEXT PRIMARY KEY,
    ticker_aktuell   TEXT,
    name             TEXT,
    sic              TEXT,
    sic_beschreibung TEXT,
    erste_filing     TEXT,
    letzte_filing    TEXT,
    quelle           TEXT
)
"""

DDL_PIT_MEMBER = """
CREATE TABLE IF NOT EXISTS pit_member (
    cik10                 TEXT NOT NULL,
    ticker_zum_zeitpunkt   TEXT NOT NULL,
    von                   TEXT NOT NULL,
    bis                   TEXT,
    quelle                TEXT NOT NULL,
    konfidenz             TEXT NOT NULL,
    PRIMARY KEY (cik10, ticker_zum_zeitpunkt, von)
)
"""

# Verarbeitungszustaende der Aufnahme fuer Resume (einmal je Ticker/CIK)
DDL_INGEST_RUNS = """
CREATE TABLE IF NOT EXISTS ingest_runs (
    cik10       TEXT NOT NULL,
    quelle      TEXT NOT NULL,
    timestamp   TEXT NOT NULL,
    neues_zeilen INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cik10, quelle)
)
"""

_INDEXE = [
    "CREATE INDEX IF NOT EXISTS ix_facts_cik_tag_end_filed "
    "ON pit_facts (cik10, tag, period_end, filed_date)",
    "CREATE INDEX IF NOT EXISTS ix_facts_cik_filed "
    "ON pit_facts (cik10, filed_date)",
]


def ensure_schema(con: sqlite3.Connection) -> None:
    """Erzeugt fehlende Tabellen/Indizes (idempotent)."""
    con.execute(DDL_PIT_FACTS)
    con.execute(DDL_PIT_ENTITY)
    con.execute(DDL_PIT_MEMBER)
    con.execute(DDL_INGEST_RUNS)
    for idx in _INDEXE:
        con.execute(idx)
    con.commit()


# Tag-Whitelist aus der v74-Diagnose.
# Duration-Familien: (tag, prioritaet) -- kleinere Prioritaet = erste Wahl.
# Instant-Familien sorgen fuer period_start = NULL.
DURATION_FAMILIEN = {
    "revenue": [
        ("Revenues", 0),
        ("RevenueFromContractWithCustomerExcludingAssessedTax", 1),
        ("SalesRevenueNet", 2),
    ],
    "ebit": [("OperatingIncomeLoss", 0)],
    "net_income": [("NetIncomeLoss", 0)],
    "ocf": [
        ("NetCashProvidedByUsedInOperatingActivities", 0),
        ("NetCashProvidedByUsedInOperatingActivitiesContinuingOperations", 1),
    ],
    "capex": [
        ("PaymentsToAcquirePropertyPlantAndEquipment", 0),
        ("PaymentsToAcquireProductiveAssets", 1),
    ],
    "sbc": [
        ("ShareBasedCompensation", 0),
        ("ShareBasedCompensationArrangementByShareBasedPaymentAwardCompensation", 1),
    ],
    "interest": [
        ("InterestExpense", 0),
        ("InterestExpenseNonoperating", 1),
        ("InterestExpenseDebt", 2),
        ("InterestAndDebtExpense", 3),
    ],
    "tax": [("IncomeTaxExpenseBenefit", 0)],
    "pretax": [(
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinary"
        "ItemsNoncontrollingInterest", 0)],
    "shares_diluted": [
        ("WeightedAverageNumberOfDilutedSharesOutstanding", 0),
    ],
}

# Instant-Familien: nur period_end, period_start = NULL
INSTANT_FAMILIEN = {
    "equity": [("StockholdersEquity", 0),
               ("StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest", 1)],
    "debt": [
        ("LongTermDebt", 0),
        ("DebtLongtermAndShorttermCombinedAmount", 1),
        ("LongTermDebtNoncurrent", 2),
        ("LongTermDebtAndCapitalLeaseObligations", 3),
    ],
    "cash": [
        ("CashAndCashEquivalentsAtCarryingValue", 0),
        ("CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", 1),
    ],
    "shares_outstanding": [("EntityCommonStockSharesOutstanding", 0)],
    "shares_outstanding_fallback": [("CommonStockSharesOutstanding", 0)],
}

# us-gaap fuer alle Familien ausser shares_outstanding (dei-Namespace)
FAMILIEN_NAMESPACE = {"shares_outstanding": "dei"}

_ALLE_FAMILIEN = {}
_ALLE_FAMILIEN.update({fam: tags for fam, tags in DURATION_FAMILIEN.items()
                       if tags})
_ALLE_FAMILIEN.update(INSTANT_FAMILIEN)
ALLE_FAMILIEN = _ALLE_FAMILIEN

ALLE_TAGS = sorted({tag for tags in _ALLE_FAMILIEN.values() for tag, _ in tags})

ZUGELASSENE_FORMEN = ("10-K", "10-K/A", "10-Q", "10-Q/A")
