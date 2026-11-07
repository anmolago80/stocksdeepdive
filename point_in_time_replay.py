"""
point_in_time_replay.py

TASK 38 (Director-directed, 7 Nov 2026, owner-only, display only):
"Replay at date" - validation for "good company, temporary drop" cases
(the owner's key objective: the Top 200 must catch CSL-type drops).
The 12 Nov Quality v2/Moat/exception preview (TASK 32/35/37) uses
TODAY's data, so it can't on its own prove a holding like CSL.AX (A$120,
16 Jul 2026) would have qualified for the Top 200 THEN, under whichever
formula was/would have been active at that time. This module
reconstructs, as far as this codebase's own stored/fetchable data
allows, what a ticker's Top 200 inputs and Value Score(s) would have
read on a PAST date.

READ-ONLY throughout: never writes to portfolio_store (baseline/
holdings), top100_store (pool/presence), or any cache/volume file -
same safety profile as every other owner-only preview tool
(backfill_preview(), simulate_pool_v2(), etc).

WHAT THIS CAN RECONSTRUCT, AND HOW:
  - price / intrinsic value: from the holding's own LOCKED baseline
    (portfolio_health_engine.baseline_snapshot_fields()'s own shape -
    "price"/"intrinsic_value") when `date` matches the baseline's own
    captured date exactly (a baseline is a point-in-time snapshot by
    construction - the ONLY genuinely point-in-time price/IV pair this
    codebase stores). For any OTHER date, price can still be read from
    daily price history (whatever close was on/before that date); IV
    cannot be reconstructed at all (resolve_intrinsic_value() has no
    historical mode - running it "as of" a past date would need that
    date's own growth estimates/discount rate/FCF base, none of which
    are stored anywhere) - reported as "n/a (not stored)", never
    estimated.
  - psychology (fear/greed/fomo): nightly_scan.analyze_ticker_lite()'s
    OWN exact formula (see psychology_as_of()'s own docstring) - this
    one IS fully point-in-time-reconstructable, because it only needs
    a trailing 63-trading-day window of daily closes ending on `date`,
    and daily price history genuinely exists for any past date.
  - Quality v1: get_quality_score() has no historical mode at all - it
    only ever reads TODAY's live `.info` snapshot (point-in-time by
    construction, just never "as of a past date"). The ONLY Quality v1
    figure this module can honestly report for a past date is the
    holding's own locked baseline quality_score, when `date` matches
    the baseline's capture date - otherwise "n/a (quality_score has no
    historical replay)".
  - Quality v2 / Moat: BOTH read multi-year ANNUAL STATEMENTS (income/
    balance/cashflow), which - unlike a point-in-time `.info` scalar -
    are tagged with their own real fiscal-year-end dates and persist in
    today's fetched bundle for several years back. bundle_as_of() below
    drops any fiscal-year column whose own REPORTING date (FY-end +
    STATEMENT_REPORTING_LAG_DAYS - see that constant's own docstring)
    falls on/after `date`, then quality_v2_engine.get_quality_score_v2()/
    moat_engine._compute_moat_from_bundle() run UNMODIFIED against that
    truncated bundle - no second, independently-derived formula.
    CAVEAT: both engines fall back to TODAY's live `.info` for any
    single input with no usable statement history (e.g. ROE when
    equity/net-income data is missing) - that one input is NOT point-
    in-time when it happens; replay_at_date()'s own "quality_v2_meta"/
    "moat" output names which inputs fell back, so this is disclosed,
    never silently absorbed into the headline number.

WHAT THIS CANNOT RECONSTRUCT AT ALL (disclosed in replay_at_date()'s
own "limitations" list, never guessed at):
  - Quality v1 on any date other than a matching baseline capture date.
  - Intrinsic Value on any date other than a matching baseline capture
    date (no historical DCF replay).
  - The exact statement PUBLICATION date for a given fiscal year - this
    module approximates it as FY-end + STATEMENT_REPORTING_LAG_DAYS,
    a single global constant, not a verified per-company filing date.
  - The historical Top 200 cut-off - top100_store only retains
    POOL_SNAPSHOT_RETENTION (10) days of pool snapshots, so there is no
    stored cut-off from months ago; only TODAY's live cut-off
    (top100_engine.pool_value_score_cutoff()) is ever available, and
    the Director's own instruction is to label that comparison "the Top
    200 didn't exist then" rather than imply it's a real historical
    threshold.
  - Whether the 3x-rule TASK 37 exception's OTHER conditions (Share
    Count Flagged / FCF Source / Trading Status / Intrinsic Source)
    would have held on that date - this module only re-checks the
    Moat/Quality thresholds (the two inputs it actually reconstructs);
    the other four conditions are reported as "not reconstructable
    point-in-time", never assumed clean.
  - Discovery (the attention/momentum score) - needs live News/Trends/
    StockTwits data this module has no historical source for; every
    Value Score variant below uses discovery_measured=False (the same
    "lite scan" convention nightly_scan.py/ranking_engine.py already
    use for exactly this situation - Discovery's weight is dropped and
    proportionally redistributed, never scored as zero).
"""
import datetime
import re

import pandas as pd

import moat_engine
import quality_v2_engine
import ranking_engine
import resolver_engine
import top100_engine

# A fiscal year's own period-end date is not when its numbers became
# PUBLICLY KNOWN - large-cap annual reports/10-Ks are typically filed
# 45-90 days after FY end. No per-company filing-date data is stored
# anywhere in this codebase, so this is ONE global, documented
# approximation (not a verified SEC/ASX filing date) - a fiscal year
# is treated as "reported" starting FY-end + this many days.
STATEMENT_REPORTING_LAG_DAYS = 90


def _as_date(d):
    if isinstance(d, datetime.date):
        return d
    return datetime.date.fromisoformat(str(d)[:10])


def _column_date(col):
    """The real calendar date embedded in a statement DataFrame's own
    column label - the exact same regex fundamentals_data/auto_
    compounder_engine._series_with_dates() use, duplicated here (not
    imported - that one is a private helper) rather than depending on
    an internal helper from a module this one doesn't otherwise need."""
    m = re.search(r"((?:19|20)\d{2})-(\d{2})-(\d{2})", str(col))
    if not m:
        return None
    return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))


def bundle_as_of(bundle, cutoff_date, reporting_lag_days=STATEMENT_REPORTING_LAG_DAYS):
    """A COPY of `bundle` (fundamentals_data.get_bundle()'s own shape)
    with every income/balance/cashflow column (fiscal year) whose own
    reporting date (its real period-end date + `reporting_lag_days`)
    falls ON OR AFTER `cutoff_date` dropped - every downstream reader
    that iterates these DataFrames' own columns (quality_v2_engine.
    get_quality_score_v2(), moat_engine._compute_moat_from_bundle())
    then sees exactly the fiscal years genuinely reported by that date,
    no more. A column with no parseable date is dropped too (never
    assumed "old enough" on missing information). `info`/every other
    bundle key is passed through completely unchanged - this function
    touches statement DataFrames only. Never raises; a bundle missing
    one of the three statement keys simply has nothing to truncate for
    that key."""
    cutoff_date = _as_date(cutoff_date)
    out = dict(bundle or {})
    for key in ("income", "balance", "cashflow"):
        df = (bundle or {}).get(key)
        if df is None or getattr(df, "empty", True):
            continue
        keep = [
            c for c in df.columns
            if (_column_date(c) is not None
                and (_column_date(c) + datetime.timedelta(days=reporting_lag_days)) <= cutoff_date)
        ]
        out[key] = df[keep] if keep else df.iloc[:, 0:0]
    return out


def psychology_as_of(daily_closes_with_dates, date):
    """nightly_scan.analyze_ticker_lite()'s OWN fear/greed/fomo/
    psychology formula (see that function's own 3-month-window/50-day-
    MA/weekly-change comment block), replayed against whatever of
    `daily_closes_with_dates` ([(date, close), ...], any order) falls
    ON OR BEFORE `date` - exactly "as if nightly_scan had run on this
    date". Formula, verbatim:
      window = last 63 trading days on/before `date`
      fear   = (window's own high - date's close) / that high * 100
      ma50   = window's own trailing 50-day close average (or today's
               close itself if fewer than 50 bars exist)
      greed  = max((close - ma50) / ma50 * 100, 0)
      weekly = (close - close 6 bars back) / that close * 100, when at
               least 6 bars exist and that bar is non-zero, else 0
      fomo   = max(greed + max(weekly, 0), 0)
      psychology = fear - greed - fomo

    Returns {"price", "fear", "greed", "fomo", "weekly_change_pct",
    "psychology"} - every value None (never raises) when there is no
    usable close on/before `date` at all."""
    date = _as_date(date)
    pairs = sorted(
        (( _as_date(d), float(c)) for d, c in (daily_closes_with_dates or [])
         if c is not None and c == c),
        key=lambda p: p[0],
    )
    pairs = [p for p in pairs if p[0] <= date]
    if not pairs:
        return {"price": None, "fear": None, "greed": None, "fomo": None,
                "weekly_change_pct": None, "psychology": None}

    window = pairs[-63:]
    closes = pd.Series([c for _, c in window])
    current_price = float(closes.iloc[-1])
    high_price = float(closes.max())
    fear = ((high_price - current_price) / high_price) * 100 if high_price else 0.0
    ma50 = closes.rolling(50).mean().iloc[-1]
    if pd.isna(ma50) or ma50 == 0:
        ma50 = current_price
    greed = max(((current_price - ma50) / ma50) * 100, 0)
    if len(closes) >= 6 and closes.iloc[-6] != 0:
        weekly = ((current_price - closes.iloc[-6]) / closes.iloc[-6]) * 100
    else:
        weekly = 0.0
    fomo = max(greed + max(weekly, 0), 0)
    psychology = fear - greed - fomo
    return {
        "price": current_price, "fear": fear, "greed": greed, "fomo": fomo,
        "weekly_change_pct": weekly, "psychology": psychology,
    }


def quality_v2_as_of(ticker, bundle, date, info=None, reporting_lag_days=STATEMENT_REPORTING_LAG_DAYS):
    """quality_v2_engine.get_quality_score_v2(), run UNMODIFIED against
    bundle_as_of(bundle, date, reporting_lag_days) - no fiscal year
    reported on/after `date` is ever visible to it. `info` (optional,
    defaults to bundle["info"]) is TODAY's live info - used only by
    quality_v2_engine's own per-input fallback (see this module's own
    docstring on why that one path is NOT point-in-time). Returns
    (score, meta) exactly as get_quality_score_v2() does."""
    truncated = bundle_as_of(bundle, date, reporting_lag_days)
    return quality_v2_engine.get_quality_score_v2(ticker, truncated, info=info)


def moat_as_of(ticker, bundle, date, info=None, reporting_lag_days=STATEMENT_REPORTING_LAG_DAYS):
    """moat_engine._compute_moat_from_bundle(), run UNMODIFIED against
    bundle_as_of(bundle, date, reporting_lag_days). `info` defaults to
    bundle["info"] (TODAY's live info - moat_engine reads it only for
    the fund/financials-mode classification, which is a property of
    the BUSINESS, not of a point in time, so this is not a point-in-
    time concern the way quality_v2's per-input .info fallback is).
    Returns the same {"score","components","erosion","flags","years",
    "mode",...} shape compute_moat()/compute_moat_dry_run() return."""
    truncated = bundle_as_of(bundle, date, reporting_lag_days)
    info = info if info is not None else (bundle or {}).get("info") or {}
    return moat_engine._compute_moat_from_bundle(ticker, truncated, info)


def _value_score_variants(quality_v1, quality_v2, mos_pct, psychology, moat_score):
    """The Director's own 4 requested variants - each a thin call into
    ranking_engine.recompute_value_score(), the ONE shared Top 200
    formula (never a parallel/independently-derived one). Discovery is
    always 0 with discovery_measured=False forced inside recompute_
    value_score() itself - see this module's own docstring on why
    Discovery can't be replayed. None for a variant whose own Quality
    input is None (nothing to score)."""
    def _score(quality, moat_blend):
        if quality is None:
            return None
        row = {"Quality": quality, "MOS %": mos_pct or 0.0,
               "Psychology": psychology or 0.0, "Discovery (lite)": 0,
               "Moat": moat_score}
        return ranking_engine.recompute_value_score(row, moat_blend=moat_blend)

    return {
        "current_formula": _score(quality_v1, moat_blend=False),
        "moat_blend": _score(quality_v1, moat_blend=True),
        "quality_v2": _score(quality_v2, moat_blend=False),
        "quality_v2_moat_blend": _score(quality_v2, moat_blend=True),
    }


def replay_at_date(ticker, date, baseline=None, baseline_date=None, bundle=None,
                    daily_closes_with_dates=None, info=None,
                    today_cutoff=None, reporting_lag_days=STATEMENT_REPORTING_LAG_DAYS,
                    log=print):
    """The full TASK 38 replay for one ticker/date. Every input is
    ALREADY-FETCHED data the caller supplies (same "no network call of
    its own" contract as moat_engine._compute_moat_from_bundle()/
    quality_v2_engine.get_quality_score_v2() - this function is pure
    and 100% read-only, never fetches or writes anything itself):

      `baseline` / `baseline_date`: the holding's own locked baseline
        dict (portfolio_health_engine.baseline_snapshot_fields()'s
        shape) and its capture date (portfolio_store's own "baseline_
        date" column) - None/None for a ticker with no portfolio
        holding at all, which simply means price/IV/Quality v1 are
        unavailable for this replay (reported as "n/a", not guessed).
      `bundle`: fundamentals_data.get_bundle(ticker)'s own TODAY bundle
        (income/balance/cashflow + info) - truncated internally per
        fiscal year, never re-fetched for this date.
      `daily_closes_with_dates`: [(date, close), ...] - TODAY's full
        daily price history (any date range covering `date`).
      `today_cutoff`: top100_engine.pool_value_score_cutoff()'s current
        value, for the "compared with today's cut-off" line - caller
        supplies it so this module never has to import top100_store.

    Returns {
      "ticker", "date",
      "price": float|None, "price_source": "baseline"|"history"|"n/a",
      "intrinsic_value": float|None, "intrinsic_value_source": "baseline"|"n/a (not stored)",
      "mos_pct": float|None,
      "psychology": psychology_as_of()'s own dict,
      "quality_v1": float|None, "quality_v1_source": "baseline"|"n/a (no historical replay)",
      "quality_v2": float|None, "quality_v2_meta": dict|None,
      "moat": float|None, "moat_detail": dict|None,
      "value_score": _value_score_variants()'s own dict,
      "dcf_unreliable": bool|None,   # resolver_engine.dcf_looks_unreliable(iv, price) - None when iv/price unavailable
      "quality_exception_thresholds_met": bool|None,  # Moat/Quality thresholds only - see module docstring
      "today_cutoff": float|None,
      "would_be_in_pool": {variant: bool|None, ...},  # value_score >= today_cutoff, vs TODAY's cutoff only
      "limitations": [str, ...],      # exactly what could not be reconstructed for THIS call
    }
    Never raises - any single sub-computation's own failure is caught
    and that piece reports None/empty, never aborting the whole replay."""
    date = _as_date(date)
    baseline = baseline or {}
    limitations = []

    date_matches_baseline = bool(baseline_date) and _as_date(baseline_date) == date

    if date_matches_baseline and baseline.get("price") is not None:
        price, price_source = baseline.get("price"), "baseline"
    else:
        hist_price = None
        for d, c in (daily_closes_with_dates or []):
            if c is not None and c == c and _as_date(d) <= date:
                hist_price = float(c)
        price, price_source = hist_price, ("history" if hist_price is not None else "n/a")

    if date_matches_baseline and baseline.get("intrinsic_value") is not None:
        iv, iv_source = baseline.get("intrinsic_value"), "baseline"
    else:
        iv, iv_source = None, "n/a (not stored)"
        limitations.append(
            "Intrinsic Value: no historical DCF replay exists - only available when "
            "`date` matches the holding's own locked baseline capture date."
        )

    mos_pct = ((iv - price) / iv) * 100 if (iv and price and iv > 0) else None

    try:
        psych = psychology_as_of(daily_closes_with_dates or [], date)
    except Exception as e:
        log(f"[replay] {ticker}: psychology_as_of failed: {e}")
        psych = {"price": None, "fear": None, "greed": None, "fomo": None,
                 "weekly_change_pct": None, "psychology": None}

    if date_matches_baseline and baseline.get("quality_score") is not None:
        quality_v1, quality_v1_source = baseline.get("quality_score"), "baseline"
    else:
        quality_v1, quality_v1_source = None, "n/a (no historical replay)"
        limitations.append(
            "Quality v1: get_quality_score() has no historical mode (today's .info "
            "snapshot only) - only available when `date` matches the holding's own "
            "locked baseline capture date."
        )

    quality_v2, quality_v2_meta = None, None
    if bundle:
        try:
            quality_v2, quality_v2_meta = quality_v2_as_of(
                ticker, bundle, date, info=info, reporting_lag_days=reporting_lag_days,
            )
            if quality_v2_meta and quality_v2_meta.get("fallback_fields"):
                limitations.append(
                    "Quality v2: input(s) " + ", ".join(quality_v2_meta["fallback_fields"]) +
                    " had no usable statement history as of this date and fell back to "
                    "TODAY's live .info value - not point-in-time for those inputs only."
                )
        except Exception as e:
            log(f"[replay] {ticker}: quality_v2_as_of failed: {e}")
    else:
        limitations.append("Quality v2: no fundamentals bundle supplied - cannot compute.")

    moat_score, moat_detail = None, None
    if bundle:
        try:
            moat_detail = moat_as_of(ticker, bundle, date, info=info, reporting_lag_days=reporting_lag_days)
            moat_score = moat_detail.get("score")
            if moat_detail.get("flags"):
                limitations.append(
                    f"Moat: {moat_detail['years']} usable statement year(s) as of this "
                    f"date (mode={moat_detail.get('mode')}) - flags: " + "; ".join(moat_detail["flags"])
                )
        except Exception as e:
            log(f"[replay] {ticker}: moat_as_of failed: {e}")
    else:
        limitations.append("Moat: no fundamentals bundle supplied - cannot compute.")

    limitations.append(
        f"Statement reporting lag is approximated as {reporting_lag_days} days after "
        "fiscal year end (a global constant, not a verified per-company filing date)."
    )
    limitations.append(
        "Discovery (attention/momentum) cannot be replayed (needs live News/Trends "
        "data with no historical source) - every Value Score variant below uses "
        "discovery_measured=False, the same convention nightly_scan.py's lite-scan "
        "path already uses."
    )

    value_score = _value_score_variants(quality_v1, quality_v2, mos_pct, psych.get("psychology"), moat_score)

    dcf_unreliable = resolver_engine.dcf_looks_unreliable(iv, price) if (iv and price) else None
    if dcf_unreliable is None:
        limitations.append("3x-rule check: needs both Intrinsic Value and price - unavailable for this date.")

    quality_exception_thresholds_met = None
    if moat_score is not None and quality_v1 is not None:
        quality_exception_thresholds_met = (
            moat_score >= top100_engine.QUALITY_EXCEPTION_MIN_MOAT
            and quality_v1 >= top100_engine.QUALITY_EXCEPTION_MIN_QUALITY
        )
        limitations.append(
            "TASK 37 exception: only the Moat/Quality thresholds are re-checked here - "
            "Share Count Flagged/FCF Source/Trading Status/Intrinsic Source cannot be "
            "reconstructed point-in-time and are NOT assumed clean."
        )
    else:
        limitations.append(
            "TASK 37 exception: Moat and/or Quality v1 unavailable for this date - "
            "cannot evaluate even the threshold-only check."
        )

    if today_cutoff is None:
        limitations.append(
            "Top 200 cut-off: no historical cut-off exists (top100_store retains only "
            "10 days of pool snapshots) - only TODAY's live cut-off is available."
        )
    would_be_in_pool = {
        k: (v >= today_cutoff) if (v is not None and today_cutoff is not None) else None
        for k, v in value_score.items()
    }

    return {
        "ticker": ticker, "date": date.isoformat(),
        "price": price, "price_source": price_source,
        "intrinsic_value": iv, "intrinsic_value_source": iv_source,
        "mos_pct": mos_pct,
        "psychology": psych,
        "quality_v1": quality_v1, "quality_v1_source": quality_v1_source,
        "quality_v2": quality_v2, "quality_v2_meta": quality_v2_meta,
        "moat": moat_score, "moat_detail": moat_detail,
        "value_score": value_score,
        "dcf_unreliable": dcf_unreliable,
        "quality_exception_thresholds_met": quality_exception_thresholds_met,
        "today_cutoff": today_cutoff,
        "would_be_in_pool": would_be_in_pool,
        "limitations": limitations,
    }
