"""
admin_data_audit.py

Data-correctness audit, item 5 (27 Sep 2026, owner-directed): a single
owner-only, button-triggered "Data audit checks" panel on the Admin
Dashboard (see app.py's page_admin_dashboard()) that answers, against
REAL production data, every A1/A3b/A5/A6/MER question this whole audit
could not verify from the sandboxed session that did the actual code
fixes (no live network access there - confirmed via three separate
WebFetch attempts against finance.yahoo.com, stocksdeepdive.com, and
www.rba.gov.au, all EGRESS_BLOCKED). The production server CAN reach
Yahoo (and, pending verification, the RBA); this is the one place on
the live site built specifically to use that.

A6 (27 Sep 2026, owner-directed, FINAL SPEC): the market-cap-discount-
tier design that would replace beta is DESIGN + DRY RUN ONLY - see
capm_engine.py's own "A6" section for the full rationale. Nothing this
module computes for A6 is read by any live valuation; check_a6_
discount_tiers() below exists purely to give the owner the side-by-side
numbers that spec asked for.

Read-only, by design: nothing here writes to any store, invalidates any
cache, or touches scan_store/score_history/the nightly scan in any way.
Every yfinance object built here is a NEW, disposable yf.Ticker(...)
(or the existing cached wrappers below) - never a write path.

ONE deliberate, owner-mandated exception: capture_a1_before_snapshot_
once() (see its own section further down) writes a small local JSON
snapshot of A1_TICKERS' pre-fix scan rows, exactly once per server
process at import time, so the Data audit panel's A1 "before" column
still means something after the nightly scan overwrites scan_store's
live rows with post-fix values. It never touches scan_store,
score_history, or the nightly scan itself - only its own small file.

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
import json
import os
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
# A6 (27 Sep 2026, owner-directed, FINAL SPEC): the owner's own named
# list, deliberately spanning every discount-rate tier and both
# currencies - AAPL/MSFT (mega-cap USD), KO/JNJ (large-cap USD), CPRT
# (mid-cap USD), NVDA (mega-cap USD), CROX (small/mid-cap USD, already
# this audit's "one small US cap"), CSL.AX/BHP.AX/WES.AX (large-cap
# AUD, already A5's tickers), DUG.AX/AR1.AX/REG.AX/HM1.AX (small/micro-
# cap AUD).
A6_TICKERS = [
    "AAPL", "MSFT", "KO", "JNJ", "CPRT", "NVDA", "CROX",
    "CSL.AX", "BHP.AX", "WES.AX", "DUG.AX", "AR1.AX", "REG.AX", "HM1.AX",
]
# Batch B1 (27 Sep 2026, owner-directed): unverified-findings checks -
# read-only, same as every check above; confirms or rules out before
# anything in Batch B3 is touched.
B1_DIVIDEND_CCY_TICKERS = ["BHP.AX", "RIO.AX", "WDS.AX"]
B1_LEASE_TICKERS = ["WES.AX", "WOW.AX", "QAN.AX", "TGT"]
B1_P2B_FX_TICKERS = ["CSL.AX", "RMD.AX"]
_LEASE_ROW_HINTS = ("lease", "repayment", "principal")

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


# =====================================================================
# A1 pre-fix snapshot (27 Sep 2026, owner-directed, MANDATORY): the ONE
# deliberate exception to this module's "read-only, writes nothing"
# design (see the module docstring). The owner won't be awake before
# tonight's 20:00 UTC scan overwrites scan_store's saved rows with
# POST-A1-fix values, which would silently turn the panel's "before"
# column into another "now" column - not a genuine before/after
# comparison. This captures whatever scan_store CURRENTLY holds for
# A1_TICKERS into a small persisted snapshot, ONE TIME, the moment this
# module is first imported after this fix's own deploy (i.e. while
# scan_store still holds the PRE-fix rows written by the last scan that
# ran before this deploy - see capture_a1_before_snapshot_once()'s own
# docstring for why that timing holds). Every later
# check_a1_before_after() call then reads "before" from this frozen
# snapshot instead of scan_store, so it stays meaningful even after
# tonight's scan (and every scan after it) overwrites the live rows.
# =====================================================================

def _snapshot_dir():
    # Same RAILWAY_VOLUME_MOUNT_PATH-or-local-fallback convention every
    # other persisted store in this app uses (see scan_store._data_dir()).
    base = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)
    path = os.path.join(base, "admin_data_audit")
    os.makedirs(path, exist_ok=True)
    return path


def _snapshot_path():
    return os.path.join(_snapshot_dir(), "a1_before_snapshot.json")


def capture_a1_before_snapshot_once(tickers=None):
    """Idempotent: if a snapshot file already exists, does nothing at
    all (never overwrites a real captured snapshot with a later, no-
    longer-pre-fix read). Otherwise captures _find_any_saved_row() for
    each of `tickers` right now and persists it.

    Called once at the bottom of this module, so it runs exactly once
    per server process at import time - i.e. at boot, right after this
    fix's own deploy. At that exact moment scan_store still holds
    whatever the LAST scan before this deploy wrote (deploying new code
    doesn't itself touch scan_store; only an actual scan run does, and
    none has run between this fix landing and the process booting) -
    genuinely pre-fix data, which is the whole point of taking the
    snapshot here rather than lazily on first panel view (a lazy first
    capture could easily happen AFTER tonight's scan had already
    overwritten the rows, if the owner's first click came after 20:00
    UTC).

    Wrapped in try/except at the call site below (never allowed to
    block app boot); a failure here just means check_a1_before_after()
    keeps falling back to a live scan_store read, exactly like before
    this mechanism existed."""
    path = _snapshot_path()
    if os.path.exists(path):
        return
    tickers = tickers or A1_TICKERS
    rows = {}
    for t in tickers:
        found = _find_any_saved_row(t)
        if found:
            rows[t] = found
    payload = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "rows": rows,
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)  # atomic - a concurrent worker's own capture can't corrupt this


def load_a1_before_snapshot():
    """The frozen pre-fix snapshot, or None if it was never captured
    (e.g. a from-scratch deploy where scan_store had nothing yet for
    these tickers) - read-only, no network, no write."""
    path = _snapshot_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
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
    """A1: before (the frozen pre-fix snapshot if one was captured -
    see capture_a1_before_snapshot_once() above - otherwise falling
    back to a live "last saved scan row, any age" read, same as before
    this mechanism existed) vs now (live discount rate / intrinsic
    value / MOS through the exact DCF path A1 fixed) for each of
    `tickers`."""
    tickers = tickers or A1_TICKERS
    snapshot = load_a1_before_snapshot()
    snapshot_rows = (snapshot or {}).get("rows") or {}
    out = []
    for t in tickers:
        before = snapshot_rows.get(t) if snapshot else None
        if before is None:
            before = _find_any_saved_row(t)
        row = {"ticker": t, "before": before, "before_source": "snapshot" if (snapshot and t in snapshot_rows) else "live"}
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


def check_a6_discount_tiers(tickers=None):
    """A6 dry-run (27 Sep 2026, owner-directed, FINAL SPEC): for each of
    `tickers`, the tier/discount rate/intrinsic value/MOS under (a) the
    current live beta-based CAPM model (with the A1 fix already live -
    capm_engine.resolve_discount_rate) and (b) the new market-cap-tier
    model (capm_engine.resolve_discount_rate_by_market_cap), computed
    by calling fcf_valuation_engine.dcf_intrinsic_value() TWICE for the
    same ticker with the SAME fetched bundle/growth/perpetual-rate
    inputs, differing only in the discount_rate= override passed in -
    isolates the one variable this whole audit is actually about,
    rather than risking two runs that also silently differ in FCF base
    or growth source. DESIGN + DRY RUN ONLY - neither call here ever
    writes anywhere; capm_engine.resolve_discount_rate_by_market_cap()
    itself is not called from any live valuation path (see its own
    docstring)."""
    tickers = tickers or A6_TICKERS
    out = []
    for t in tickers:
        row = {"ticker": t}
        try:
            bundle = fundamentals_data.get_bundle(t)
            info = (bundle or {}).get("info") or {}
            cashflow_df = (bundle or {}).get("cashflow")
            currency = info.get("currency")
            price = info.get("currentPrice")

            iv_old, _g_old, meta_old = fcf_valuation_engine.dcf_intrinsic_value(
                t, info=info, cashflow_df=cashflow_df, currency=currency,
            )
            mos_old = ((iv_old - price) / iv_old * 100.0) if (iv_old and price) else None

            tier_rate, tier_meta = capm_engine.resolve_discount_rate_by_market_cap(info, currency)
            iv_new, _g_new, meta_new = fcf_valuation_engine.dcf_intrinsic_value(
                t, info=info, cashflow_df=cashflow_df, currency=currency,
                discount_rate=tier_rate,
            )
            mos_new = ((iv_new - price) / iv_new * 100.0) if (iv_new and price) else None

            row["price"] = price
            row["current_model"] = {
                "discount_rate": meta_old.get("discount_rate_used"),
                "intrinsic_value": round(iv_old, 2) if iv_old else None,
                "mos_pct": round(mos_old, 2) if mos_old is not None else None,
            }
            row["tier_model"] = {
                "tier_label": tier_meta.get("tier_label"),
                "discount_rate": round(tier_rate, 4),
                "rf_source": tier_meta.get("rf_source"),
                "intrinsic_value": round(iv_new, 2) if iv_new else None,
                "mos_pct": round(mos_new, 2) if mos_new is not None else None,
            }
            row["mos_delta_pts"] = (
                round(mos_new - mos_old, 2) if (mos_old is not None and mos_new is not None) else None
            )
        except Exception as e:
            row["error"] = str(e)
        out.append(row)
        time.sleep(_PACE_SECONDS)
    return out


def check_b1_dividend_currency(tickers=None):
    """B1a (27 Sep 2026, owner-directed): the last two dividend payments
    from tk.dividends (a real pandas Series, date-indexed, in whatever
    currency yfinance itself reports cash dividend amounts) next to
    trailingAnnualDividendRate and the listing currency, for each of
    `tickers` - the owner compares these against the AUD amounts on the
    ASX announcements themselves. Confirms or rules out the finding;
    changes nothing."""
    tickers = tickers or B1_DIVIDEND_CCY_TICKERS
    out = []
    for t in tickers:
        row = {"ticker": t}
        try:
            tk = yf.Ticker(t)
            info = tk.info or {}
            row["currency"] = info.get("currency")
            row["trailingAnnualDividendRate"] = info.get("trailingAnnualDividendRate")
            divs = tk.dividends
            if divs is not None and not divs.empty:
                last_two = divs.tail(2)
                row["last_two_payments"] = [
                    {"date": str(idx), "amount": float(v)} for idx, v in last_two.items()
                ]
            else:
                row["last_two_payments"] = []
        except Exception as e:
            row["error"] = str(e)
        out.append(row)
        time.sleep(_PACE_SECONDS)
    return out


def check_b1_lease_rows(tickers=None):
    """B1b (27 Sep 2026, owner-directed): every cash-flow-statement row
    whose name mentions lease/repayment/principal, with values, plus
    Total Debt and any lease-liability row on the balance sheet, for
    each of `tickers`. Tolerant substring match on row labels (same
    "don't know the exact label in advance" reasoning as auto_
    compounder_engine._find_row()'s own substring fallback) rather than
    an exact-name list, since IFRS lease-line naming varies by filer.
    Confirms or rules out the finding; changes nothing."""
    tickers = tickers or B1_LEASE_TICKERS
    out = []
    for t in tickers:
        row = {"ticker": t}
        try:
            tk = yf.Ticker(t)
            cashflow_df = tk.cashflow
            lease_rows = {}
            if cashflow_df is not None and not cashflow_df.empty:
                for label in cashflow_df.index:
                    if any(hint in str(label).lower() for hint in _LEASE_ROW_HINTS):
                        lease_rows[str(label)] = [
                            (float(v) if v == v else None) for v in cashflow_df.loc[label].tolist()
                        ]
            row["cashflow_lease_rows"] = lease_rows
            row["cashflow_years"] = (
                [str(c) for c in cashflow_df.columns] if cashflow_df is not None and not cashflow_df.empty else []
            )

            balance_df = tk.balance_sheet
            total_debt = None
            balance_lease_rows = {}
            if balance_df is not None and not balance_df.empty:
                for label in balance_df.index:
                    label_l = str(label).lower()
                    if label_l == "total debt":
                        v = balance_df.loc[label].iloc[0]
                        total_debt = float(v) if v == v else None
                    if "lease" in label_l:
                        balance_lease_rows[str(label)] = [
                            (float(v) if v == v else None) for v in balance_df.loc[label].tolist()
                        ]
            row["total_debt_latest"] = total_debt
            row["balance_sheet_lease_rows"] = balance_lease_rows
        except Exception as e:
            row["error"] = str(e)
        out.append(row)
        time.sleep(_PACE_SECONDS)
    return out


def check_b1_price_to_book_fx(tickers=None):
    """B1c (27 Sep 2026, owner-directed): priceToBook next to a locally
    computed currentPrice / bookValue, and both the listing currency
    and the financial-statement currency (the same financialCurrency
    vs currency distinction fcf_valuation_engine.dcf_intrinsic_value()
    already has to account for - see its own "Currency conversion"
    comment), for each of `tickers` - shows whether Yahoo's own
    priceToBook is FX-adjusted or mixes a price-currency numerator
    against a statement-currency denominator. Confirms or rules out the
    finding; changes nothing."""
    tickers = tickers or B1_P2B_FX_TICKERS
    out = []
    for t in tickers:
        row = {"ticker": t}
        try:
            info = yf.Ticker(t).info or {}
            price = info.get("currentPrice")
            book_value = info.get("bookValue")
            row["priceToBook_yahoo"] = info.get("priceToBook")
            row["currentPrice"] = price
            row["bookValue"] = book_value
            row["price_over_book_computed"] = (
                round(price / book_value, 4) if (price and book_value) else None
            )
            row["currency"] = info.get("currency")
            row["financialCurrency"] = info.get("financialCurrency")
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
        "a6_discount_tiers": check_a6_discount_tiers(),
        "b1_dividend_currency": check_b1_dividend_currency(),
        "b1_lease_rows": check_b1_lease_rows(),
        "b1_price_to_book_fx": check_b1_price_to_book_fx(),
        "mer": check_mer(),
    }


# Boot-time, one-shot, idempotent (27 Sep 2026, owner-directed, MANDATORY
# - see capture_a1_before_snapshot_once()'s own docstring above for the
# full timing rationale). Runs once per server process, right after this
# fix's own deploy - `import admin_data_audit` only executes a module's
# top-level code once per process, however many times app.py itself is
# re-run per Streamlit session. Never allowed to block app boot: any
# failure here (e.g. no volume mounted yet, a transient disk error) is
# swallowed, and check_a1_before_after() simply keeps its pre-existing
# live-read fallback, exactly as if this mechanism didn't exist.
try:
    capture_a1_before_snapshot_once()
except Exception:
    pass
