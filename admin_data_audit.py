"""
admin_data_audit.py

Data-correctness audit, item 5 (27 Sep 2026, owner-directed): a single
owner-only, button-triggered "Data audit checks" panel on the Admin
Dashboard (see app.py's page_admin_dashboard()) that answers, against
REAL production data, every A1/A3b/A5/MER question this whole audit
could not verify from the sandboxed session that did the actual code
fixes (no live network access there - confirmed via two separate
WebFetch attempts against finance.yahoo.com and stocksdeepdive.com,
both EGRESS_BLOCKED). The production server CAN reach Yahoo; this is
the one place on the live site built specifically to use that.

Read-only, by design: nothing here writes to any store, invalidates any
cache, or touches scan_store/score_history/the nightly scan in any way.
Every yfinance object built here is a NEW, disposable yf.Ticker(...)
(or the existing cached wrappers below) - never a write path.

Reuses the site's own existing logic wherever it exists, rather than a
second implementation of the same thing (this repo's own precedent -
see CLAUDE.md and share_class_engine.py's own module docstring):
  - capm_engine.get_risk_free_rate() for the A1 rates themselves.
  - fundamentals_data.get_bundle() (its own existing 24h cache - a
    likely-cached hit for any ticker the nightly scan already covers,
    not a fresh fetch of its own) + fcf_valuation_engine.
    dcf_intrinsic_value() for the A1 "now" discount rate/IV, the exact
    function A1 fixed.
  - share_class_engine.whole_company_shares()/_fetch_diluted_shares()
    for A3b - the SAME three-tier resolver A3b wired into the live DCF
    path, not a re-implementation of the 1.3x test.
  - etf_insights.get_fund_facts() for "what the site displays" on MER.
  - auto_compounder_engine._find_row()/_ROW_ALIASES/_statement_col_
    dates() for A5's statement-row lookups - this codebase's own
    precedent (server.py importing api_v1._resolve_universe()) is to
    reuse a private-by-convention helper when the exact logic already
    lives there, rather than re-derive row-matching from scratch.

Guardrails (the owner's own explicit instructions for this exact
panel):
  - NEVER runs automatically - only from an explicit button click in
    page_admin_dashboard(); nothing in this module is imported or
    scheduled anywhere else.
  - Refuses to run while the nightly scan lock is held, or during
    20:00-03:00 UTC - see refusal_reason() below. Both checks are
    read-only reads of scheduler_engine's existing lock-file mechanism;
    NEITHER acquires/releases the lock, and NEITHER derives the time
    window from scheduler_engine's own NIGHTLY_UNIVERSES/cadence
    config - CLAUDE.md is explicit that config must never be touched
    without an instruction to do so, and this panel's own window is a
    separate, deliberately wider safety margin using the exact numbers
    this whole audit session's own "never push between 20:00 and 03:00
    UTC" standing rule already uses.
  - Paced (a short sleep between each per-ticker Yahoo call across
    every check below) and cached for the session at a 30-minute TTL
    (run_all_checks()) - a re-click within that window costs zero new
    Yahoo calls.
  - A "few dozen calls in total" budget, achieved by fetching only what
    each specific check needs (e.g. `.info` alone for A3b's
    sharesOutstanding/impliedSharesOutstanding, never a full 6-7-call
    fundamentals_data bundle for a check that doesn't need one) rather
    than reusing one maximal fetch everywhere.
"""
import time
from datetime import datetime, timezone

import streamlit as st
import yfinance as yf

import auto_compounder_engine as _ace
import capm_engine
import etf_insights
import fcf_valuation_engine
import fundamentals_data
import scan_store
import scheduler_engine
import share_class_engine

# The owner's own exact numbers for this panel's refusal window -
# deliberately NOT read from scheduler_engine's own scan-hour/cadence
# config (see module docstring).
REFUSAL_WINDOW_START_UTC_HOUR = 20
REFUSAL_WINDOW_END_UTC_HOUR = 3
NIGHTLY_LOCK_JOB_NAME = "nightly"

A1_TICKERS = ["AAPL", "MSFT", "KO", "JNJ", "CROX"]  # CROX: "one small US cap"
A3B_TICKERS = ["HEI", "GOOG", "GOOGL", "FOX", "FOXA", "NWS", "NWSA", "BRK-B"]
A5_TICKERS = ["CSL.AX", "BHP.AX", "WES.AX"]
MER_TICKERS = ["IVV", "VAS", "VGS"]

_PACE_SECONDS = 0.4  # "polite to yfinance" - same spirit as _prefetch_scan_data's pacing


def refusal_reason(now=None):
    """None if the audit may run right now; otherwise a short, owner-
    facing string naming exactly why it refused. Two independent
    checks, both read-only:

    1. the nightly scan lock is held with a fresh heartbeat - the same
       freshness test scheduler_engine._acquire_job_lock() itself uses
       (_JOB_LOCK_HEARTBEAT_STALE_SECONDS), read here without ever
       acquiring or releasing anything.
    2. current UTC time falls in the 20:00-03:00 window - see module
       docstring for why this is a literal, panel-local constant
       rather than scheduler_engine's own scan-schedule config."""
    now = now or datetime.now(timezone.utc)

    try:
        payload = scheduler_engine._read_lock_payload(
            scheduler_engine._lock_path(NIGHTLY_LOCK_JOB_NAME)
        )
    except Exception:
        payload = None
    if payload is not None:
        age = time.time() - payload.get("heartbeat", 0)
        if age < scheduler_engine._JOB_LOCK_HEARTBEAT_STALE_SECONDS:
            return "A nightly scan lock is currently held (fresh heartbeat) - refusing to run."

    hour = now.hour
    if hour >= REFUSAL_WINDOW_START_UTC_HOUR or hour < REFUSAL_WINDOW_END_UTC_HOUR:
        return (
            f"Current time is {now.strftime('%H:%M')} UTC, inside the 20:00-03:00 UTC "
            "scan window - refusing to run."
        )
    return None


def _find_any_saved_row(ticker):
    """Like scan_store.find_ticker_row(), but WITHOUT its 72h freshness
    cutoff. find_ticker_row() is deliberately built to answer "what
    would the live Scanner show right now" (rejecting anything stale);
    this audit instead wants "the last thing actually saved, however
    old" as the A1 BEFORE baseline - a genuinely different question, so
    a separate (much smaller) local read rather than a modified/reused
    copy of that function's own freshness gate. Local disk read only,
    zero network cost."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return None
    for universe in scan_store.list_saved_universes():
        try:
            payload = scan_store.load_scan_raw(universe)
        except Exception:
            continue
        if not payload:
            continue
        for row in payload.get("rows") or []:
            if (row.get("Ticker") or row.get("ticker") or "").upper() == ticker:
                return {
                    "universe": universe,
                    "generated_at": payload.get("generated_at"),
                    "row": row,
                }
    return None


def check_a1_rates():
    """A1: the raw ^TNX value fetched now, the US rate it produces, the
    AU rate used and whether it's live or defaulted."""
    out = {"usd": {}, "aud": {}}
    usd_ticker = capm_engine._RISK_FREE_TICKERS.get("USD", "^TNX")
    try:
        hist = yf.Ticker(usd_ticker).history(period="5d")
        out["usd"]["raw_ticker"] = usd_ticker
        out["usd"]["raw_close"] = float(hist["Close"].iloc[-1]) if (hist is not None and not hist.empty) else None
    except Exception as e:
        out["usd"]["raw_close"] = None
        out["usd"]["fetch_error"] = str(e)

    try:
        rate, source = capm_engine.get_risk_free_rate("USD")
        out["usd"]["rate"] = rate
        out["usd"]["source"] = source
    except Exception as e:
        out["usd"]["rate"] = None
        out["usd"]["error"] = str(e)

    try:
        rate, source = capm_engine.get_risk_free_rate("AUD")
        out["aud"]["rate"] = rate
        out["aud"]["source"] = source
    except Exception as e:
        out["aud"]["rate"] = None
        out["aud"]["error"] = str(e)

    return out


def check_a1_before_after(tickers=None):
    """A1: before (last saved scan row, any age, local only) vs now
    (live discount rate / intrinsic value / MOS through the exact DCF
    path A1 fixed) for each of `tickers`."""
    tickers = tickers or A1_TICKERS
    out = []
    for t in tickers:
        row = {"ticker": t, "before": _find_any_saved_row(t)}
        try:
            bundle = fundamentals_data.get_bundle(t)
            info = (bundle or {}).get("info") or {}
            cashflow_df = (bundle or {}).get("cashflow")
            currency = info.get("currency")
            iv, _growth, meta = fcf_valuation_engine.dcf_intrinsic_value(
                t, info=info, cashflow_df=cashflow_df, currency=currency,
            )
            price = info.get("currentPrice")
            mos = ((iv - price) / iv * 100.0) if (iv and price) else None
            row["now"] = {
                "discount_rate": meta.get("discount_rate_used"),
                "discount_source": meta.get("discount_source"),
                "intrinsic_value": round(iv, 2) if iv else None,
                "mos_pct": round(mos, 2) if mos is not None else None,
                "price": price,
            }
        except Exception as e:
            row["now"] = {"error": str(e)}
        out.append(row)
        time.sleep(_PACE_SECONDS)
    return out


def check_a3b_shares(tickers=None):
    """A3b: sharesOutstanding, impliedSharesOutstanding, filed diluted
    average shares, and the whole-company count share_class_engine's
    own resolver actually chooses, for each of `tickers`."""
    tickers = tickers or A3B_TICKERS
    out = []
    for t in tickers:
        row = {"ticker": t}
        info = {}
        try:
            info = yf.Ticker(t).info or {}
            row["sharesOutstanding"] = info.get("sharesOutstanding")
            row["impliedSharesOutstanding"] = info.get("impliedSharesOutstanding")
        except Exception as e:
            row["info_error"] = str(e)

        try:
            shares, flagged, source = share_class_engine.whole_company_shares(info, ticker=t)
            row["whole_company_shares"] = shares
            row["whole_company_flagged"] = flagged
            row["whole_company_source"] = source
        except Exception as e:
            row["resolver_error"] = str(e)

        try:
            row["diluted_average_shares_filed"] = share_class_engine._fetch_diluted_shares(t)
        except Exception as e:
            row["diluted_shares_error"] = str(e)

        out.append(row)
        time.sleep(_PACE_SECONDS)
    return out


def _first_june_column(df):
    """(label, date) for the newest column whose own end-date falls in
    June, or (None, None) - reuses auto_compounder_engine's own column-
    date parser rather than re-deriving date parsing here."""
    try:
        cols = _ace._statement_col_dates(df)
    except Exception:
        return None, None
    for label, date in cols:
        if date.month == 6:
            return label, date
    return None, None


def _row_value(df, row_names, col_label):
    if df is None or col_label is None:
        return None
    try:
        name = _ace._find_row(df, row_names)
        if name is None:
            return None
        v = df.loc[name][col_label]
        if v is None or (isinstance(v, float) and v != v):
            return None
        return float(v)
    except Exception:
        return None


def _cumulative_or_per_half(quarterly_value, annual_value):
    """"cumulative" (roughly equal), "per-half" (roughly half), or
    neither - per the owner's own stated test."""
    if quarterly_value is None or annual_value is None or annual_value == 0:
        return "insufficient data"
    ratio = quarterly_value / annual_value
    if abs(ratio - 1.0) <= 0.10:
        return f"cumulative (equal - ratio {ratio:.2f})"
    if abs(ratio - 0.5) <= 0.15:
        return f"per-half (about half - ratio {ratio:.2f})"
    return f"neither cumulative nor per-half (ratio {ratio:.2f})"


def check_a5_half_year(tickers=None):
    """A5 (verify only): the June column of quarterly_income_stmt next
    to the June column of income_stmt, for Net Income and Diluted EPS,
    for each of `tickers`. "Diluted EPS" is looked up by its own exact
    row name (not auto_compounder_engine's "basic_eps" alias, which
    would prefer "Basic EPS" when both rows exist) - the owner asked
    for that specific row."""
    tickers = tickers or A5_TICKERS
    out = []
    for t in tickers:
        row = {"ticker": t}
        try:
            tk = yf.Ticker(t)
            q_df = tk.quarterly_income_stmt
            a_df = tk.income_stmt
            q_label, q_date = _first_june_column(q_df)
            a_label, a_date = _first_june_column(a_df)
            row["quarterly_june_column"] = str(q_date) if q_date else None
            row["annual_june_column"] = str(a_date) if a_date else None

            ni_q = _row_value(q_df, _ace._ROW_ALIASES["net_income"], q_label)
            ni_a = _row_value(a_df, _ace._ROW_ALIASES["net_income"], a_label)
            eps_q = _row_value(q_df, ["Diluted EPS"], q_label)
            eps_a = _row_value(a_df, ["Diluted EPS"], a_label)

            row["net_income"] = {"quarterly_june": ni_q, "annual_june": ni_a}
            row["diluted_eps"] = {"quarterly_june": eps_q, "annual_june": eps_a}
            row["verdict_net_income"] = _cumulative_or_per_half(ni_q, ni_a)
            row["verdict_diluted_eps"] = _cumulative_or_per_half(eps_q, eps_a)
        except Exception as e:
            row["error"] = str(e)
        out.append(row)
        time.sleep(_PACE_SECONDS)
    return out


def check_mer(tickers=None):
    """MER: the raw expense-ratio value yfinance returns (pre any
    normalization) next to what etf_insights.get_fund_facts() - the
    exact function the site's own ETF look-through actually reads -
    displays for the same ticker, for each of `tickers`."""
    tickers = tickers or MER_TICKERS
    out = []
    for t in tickers:
        row = {"ticker": t}
        try:
            ops = yf.Ticker(t).funds_data.fund_operations
            raw = None
            if ops is not None and "Annual Report Expense Ratio" in ops.index and t in ops.columns:
                v = ops.loc["Annual Report Expense Ratio", t]
                if v is not None and not (isinstance(v, float) and v != v):
                    raw = float(v)
            row["raw_expense_ratio"] = raw
        except Exception as e:
            row["raw_error"] = str(e)

        try:
            facts = etf_insights.get_fund_facts(t)
            row["site_displays_mer_pct"] = (facts or {}).get("mer")
        except Exception as e:
            row["site_error"] = str(e)

        out.append(row)
        time.sleep(_PACE_SECONDS)
    return out


@st.cache_data(ttl=1800, show_spinner=False)
def run_all_checks(_cache_bust=0):
    """Runs every check once, paced, and caches the combined result for
    30 minutes so a re-click within that window costs zero new Yahoo
    calls. `_cache_bust`: bump (e.g. from a "force refresh" checkbox)
    to get a fresh run within the TTL - st.cache_data keys on every
    argument, so this is the standard escape hatch without disabling
    caching outright."""
    return {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "a1_rates": check_a1_rates(),
        "a1_before_after": check_a1_before_after(),
        "a3b_shares": check_a3b_shares(),
        "a5_half_year": check_a5_half_year(),
        "mer": check_mer(),
    }
