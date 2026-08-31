"""
broker_import_engine.py

Services batch, Part 3: broker CSV import for My Portfolio. Pure parsing/
mapping module (no Streamlit, no network) - takes CSV text from CommSec,
SelfWealth, Stake or a generic (ticker, units, avg cost, date) layout and
produces one aggregated row per ticker, ready for the "Add a holding"
flow to look up (portfolio_health_engine.fetch_snapshot, exactly like a
manual add) and confirm.

FORMAT DETECTION is best-effort and cosmetic only (drives the preview's
"Detected: CommSec" label and which export hint is shown) - the actual
column mapping underneath uses flexible synonym matching across EVERY
format, including generic, so a broker's export drifting slightly from
what's assumed here still has a real chance of parsing correctly rather
than hard-failing. CommSec's column set (Date/Reference/Type/Code/
Description/Quantity/Unit Price/Brokerage/Net Amount/GST) is verified
against public documentation; SelfWealth's and Stake's are this module's
best understanding of commonly-referenced export shapes and have NOT
been verified against a live export from either broker in this build (no
outbound network in the sandbox this was built in) - the generic layout
is the reliable fallback for either, and is called out as such in the
import UI and in this batch's own final report.

TRANSACTION-LOG vs SNAPSHOT rows: CommSec/SelfWealth/Stake exports are
transaction histories - one row per trade, a ticker can appear many
times (buy AND sell). The generic (ticker, units, avg cost, date) layout
is already an aggregated snapshot - one row per holding. Both shapes are
handled by the SAME aggregation pass below (a snapshot file just has one
"buy" row per ticker, which reduces to using that row's own numbers
directly): net quantity = buys minus sells; average cost = the
quantity-weighted average of BUY rows only (a sell realises a gain/loss
against the existing cost base, which this import doesn't attempt to
track); buy date = the earliest BUY row's date, or today (flagged) if no
date parsed at all. A ticker whose net quantity comes out at zero or
negative (fully or over-sold within the file) is excluded and listed
separately - there's nothing left to hold.

TICKER MAPPING reuses screen_import_store.py's exact ASX/US exchange
logic (_map_symbol/_EXCH_SYMBOL_RE) - "the same ticker-format-detection
pattern already used by the TradingView importer" per the Part 3 spec.
CommSec/SelfWealth rows (ASX-only products) get ".AX" appended to a bare
code; Stake/generic rows are left bare (US default) unless the row
already looks like "ASX:CODE". A ticker that still fails yfinance
validation at import-confirm time is reported as "unknown ticker" and
skipped there - never silently guessed further than this.
"""

import csv
import io
import re
from datetime import datetime, date

import screen_import_store

MAX_ROWS_PER_IMPORT = 1000

_FIELD_SYNONYMS = {
    "ticker": ["ticker", "symbol", "code", "market code", "asx code", "stock code"],
    "quantity": ["units", "quantity", "shares", "qty", "volume"],
    "price": ["average price", "avg price", "unit price", "price", "avg cost",
              "average cost", "cost per unit"],
    "date": ["trade date", "date", "settlement date", "transaction date"],
    "side": ["type", "side", "action", "buy/sell", "transaction type"],
}

# Cosmetic-only detection (see module docstring). A format is "detected"
# when its most distinctive header(s) are all present; anything else
# (including a file matching none of these) falls back to "generic".
_FORMAT_SIGNATURES = [
    ("commsec", {"reference", "brokerage", "net amount"}),
    ("selfwealth", {"brokerage", "trade date"}),
    ("stake", {"symbol", "side"}),
]

SAMPLE_CSV = {
    "commsec": (
        "Date,Reference,Type,Code,Description,Quantity,Unit Price,Brokerage,Net Amount,GST\n"
        "15/01/2026,123456,B,BHP,BHP GROUP LTD,100,45.20,29.95,-4549.95,2.72\n"
        "02/03/2026,123789,B,BHP,BHP GROUP LTD,50,47.10,29.95,-2384.95,2.72\n"
    ),
    "selfwealth": (
        "Trade Date,Type,Market Code,Units,Price,Brokerage\n"
        "15/01/2026,Buy,CBA,20,110.50,9.50\n"
        "10/04/2026,Buy,CBA,10,115.00,9.50\n"
    ),
    "stake": (
        "Date,Symbol,Side,Quantity,Average Price\n"
        "2026-01-15,AAPL,Buy,10,182.30\n"
        "2026-04-02,AAPL,Buy,5,195.10\n"
    ),
    "generic": (
        "Ticker,Units,Avg Cost,Date\n"
        "OCL.AX,500,4.10,2026-01-15\n"
        "AAPL,10,182.30,2026-01-15\n"
    ),
}

EXPORT_HINTS = {
    "commsec": "CommSec: Portfolio → Transactions → choose a date range → Export/Download as CSV.",
    "selfwealth": (
        "SelfWealth: Trade History → choose a date range → Export CSV. Column "
        "names can vary by export type - if the preview below looks wrong, "
        "try the generic layout instead."
    ),
    "stake": (
        "Stake: Account → Documents/Reports → Trade Confirmations or "
        "Transaction History → export. Column names can vary by export "
        "type - if the preview below looks wrong, try the generic layout instead."
    ),
    "generic": "Any spreadsheet with a Ticker, Units, Avg Cost and Date column, in any order.",
}


def _norm(h):
    return re.sub(r"\s+", " ", (h or "").strip().lower())


def _find_col(header_norm, field):
    for i, h in enumerate(header_norm):
        if h in _FIELD_SYNONYMS[field]:
            return i
    return None


def detect_format(header):
    hn = {_norm(h) for h in header}
    for name, sig in _FORMAT_SIGNATURES:
        if sig <= hn:
            return name
    return "generic"


def _parse_date(s):
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _num(s):
    if s is None:
        return None
    try:
        return float(str(s).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def _guess_yahoo_ticker(raw, fmt):
    raw = (raw or "").strip().upper()
    if not raw:
        return None
    m = screen_import_store._EXCH_SYMBOL_RE.match(raw)
    if m:
        return screen_import_store._map_symbol(m.group(1), m.group(2))
    if raw.endswith(".AX") or "-" in raw:
        return raw  # already looks Yahoo-formatted
    if fmt in ("commsec", "selfwealth"):
        return f"{raw}.AX"
    return raw  # stake/generic default: US, bare code passed through as-is


def parse_broker_csv(text, format_hint=None):
    """Returns {"format_detected", "rows", "skipped_zero_or_negative",
    "skipped_unparsed_rows", "raw_row_count", "error"}. Each item in
    "rows" is {"ticker_raw", "yahoo_ticker", "quantity", "avg_price",
    "buy_date" (a date), "date_defaulted" (bool)} - ready for the UI to
    preview, then hand to portfolio_health_engine.fetch_snapshot() per
    ticker to validate/confirm, exactly like the manual Add-a-holding
    flow. `error` set (with "rows" empty) means nothing is importable -
    the file/header couldn't be read at all."""
    empty = {"format_detected": None, "rows": [], "skipped_zero_or_negative": [],
             "skipped_unparsed_rows": 0, "raw_row_count": 0}
    try:
        raw_rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as e:
        return {**empty, "error": f"Couldn't parse this as CSV: {e}"}
    if not raw_rows:
        return {**empty, "error": "The file is empty."}
    header, data_rows = raw_rows[0], [r for r in raw_rows[1:] if any((c or "").strip() for c in r)]
    if not data_rows:
        return {**empty, "error": "No data rows found under the header."}
    if len(data_rows) > MAX_ROWS_PER_IMPORT:
        return {**empty, "error": f"{len(data_rows)} rows exceeds the "
                f"{MAX_ROWS_PER_IMPORT}-row-per-import cap - split this into smaller files."}

    header_norm = [_norm(h) for h in header]
    fmt = format_hint or detect_format(header)

    ticker_col = _find_col(header_norm, "ticker")
    qty_col = _find_col(header_norm, "quantity")
    price_col = _find_col(header_norm, "price")
    date_col = _find_col(header_norm, "date")
    side_col = _find_col(header_norm, "side")

    if ticker_col is None or qty_col is None or price_col is None:
        return {
            **empty, "raw_row_count": len(data_rows), "format_detected": fmt,
            "error": ("Couldn't find a ticker, units/quantity and price/avg cost "
                      "column in this file's header. Try the generic layout - a "
                      "Ticker, Units, Avg Cost and Date column, under any common "
                      "name for each."),
        }

    def cell(row, idx):
        return row[idx].strip() if idx is not None and idx < len(row) else ""

    agg = {}  # yahoo_ticker -> {"ticker_raw", "buy_qty", "buy_cost", "sell_qty", "dates"}
    skipped_unparsed = 0
    for row in data_rows:
        raw_ticker = cell(row, ticker_col)
        qty = _num(cell(row, qty_col))
        price = _num(cell(row, price_col))
        d = _parse_date(cell(row, date_col)) if date_col is not None else None
        if not raw_ticker or qty is None or qty <= 0:
            skipped_unparsed += 1
            continue
        yahoo = _guess_yahoo_ticker(raw_ticker, fmt)
        if not yahoo:
            skipped_unparsed += 1
            continue
        side = cell(row, side_col).strip().lower() if side_col is not None else "buy"
        is_sell = side.startswith("s")  # "sell"/"s"/"sold" all match; anything
        # else (including a blank/unrecognised side, or no side column at all
        # - the generic snapshot layout) is treated as a buy/holding row.

        a = agg.setdefault(yahoo, {"ticker_raw": raw_ticker, "buy_qty": 0.0,
                                    "buy_cost": 0.0, "sell_qty": 0.0, "dates": []})
        if is_sell:
            a["sell_qty"] += qty
        else:
            a["buy_qty"] += qty
            if price is not None:
                a["buy_cost"] += qty * price
            if d:
                a["dates"].append(d)

    rows = []
    skipped_zero = []
    today = date.today()
    for yahoo, a in agg.items():
        net_qty = a["buy_qty"] - a["sell_qty"]
        if net_qty <= 0 or a["buy_qty"] <= 0:
            skipped_zero.append(a["ticker_raw"])
            continue
        avg_price = a["buy_cost"] / a["buy_qty"]
        buy_date = min(a["dates"]) if a["dates"] else None
        rows.append({
            "ticker_raw": a["ticker_raw"], "yahoo_ticker": yahoo,
            "quantity": round(net_qty, 6), "avg_price": round(avg_price, 6),
            "buy_date": buy_date or today, "date_defaulted": buy_date is None,
        })

    rows.sort(key=lambda r: r["yahoo_ticker"])
    return {
        "format_detected": fmt, "rows": rows,
        "skipped_zero_or_negative": skipped_zero,
        "skipped_unparsed_rows": skipped_unparsed,
        "raw_row_count": len(data_rows), "error": None,
    }
