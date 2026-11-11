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

import drop_chip_engine
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


def _weekly_closes_as_of(daily_closes_with_dates, date):
    """[(date, close), ...] (any order) -> (dates, closes) weekly
    closes (pandas "W", week-ending-Sunday - the SAME resample
    convention support_levels_job.py's own _weekly_bars_from_daily()
    uses), truncated to bars on/before `date`. Local to this module
    (point_in_time_replay.py has no yfinance/download dependency of
    its own and acquires none here either - this only resamples
    whatever daily series the caller already supplied). ([], []) for
    no usable input, never raises."""
    date = _as_date(date)
    pairs = sorted(
        ((_as_date(d), float(c)) for d, c in (daily_closes_with_dates or [])
         if c is not None and c == c and _as_date(d) <= date),
        key=lambda p: p[0],
    )
    if not pairs:
        return [], []
    idx = pd.DatetimeIndex([pd.Timestamp(d) for d, _ in pairs])
    series = pd.Series([c for _, c in pairs], index=idx)
    weekly = series.resample("W").last().dropna()
    if weekly.empty:
        return [], []
    dates = [d.date() if hasattr(d, "date") else d for d in weekly.index.to_pydatetime()]
    return dates, weekly.tolist()


def fear_from_peak_as_of(daily_closes_with_dates, date, window_years=drop_chip_engine.PEAK_WINDOW_YEARS):
    """TASK 42 item 6a (Director-directed, 8 Nov 2026, CSL.AX replay
    case): drop_chip_engine's own "% below the 3-year weekly peak" fear
    variant, replayed as of a past `date` - resamples `daily_closes_
    with_dates` to weekly closes truncated to on/before `date`, then
    runs drop_chip_engine.compute_peak_and_drop()/fear_from_peak()
    UNMODIFIED - the SAME 3-year-peak math the live card chip uses,
    just windowed to a past date instead of today, never a second,
    independently-derived formula.

    Returns {"peak_date","peak_price","fear"} - every value None when
    there are too few weekly bars on/before `date` to compute a peak
    at all (never raises)."""
    weekly_dates, weekly_closes = _weekly_closes_as_of(daily_closes_with_dates, date)
    if not weekly_closes:
        return {"peak_date": None, "peak_price": None, "fear": None}
    result = drop_chip_engine.compute_peak_and_drop(weekly_dates, weekly_closes, window_years=window_years)
    fear = drop_chip_engine.fear_from_peak(result["current_price"], result["peak_price"])
    return {"peak_date": result["peak_date"], "peak_price": result["peak_price"], "fear": fear}


def quality_on_sale_as_of(date, daily_closes_with_dates, quality_v1, quality_v2, dcf_unreliable,
                           quality_exception_thresholds_met,
                           min_quality=None):
    """TASK 42 item 6c (Director-directed, 8 Nov 2026): would this
    ticker have passed EVERY "Quality on sale" tab rule (top100_
    engine.quality_on_sale_candidates()'s own three rules) as of
    `date`? Report PASS/FAIL PER RULE (never one collapsed bool), so a
    near-miss is visible rather than hidden behind a single "no".

    `min_quality` defaults to top100_engine.QUALITY_ON_SALE_MIN_QUALITY
    - the SAME constant the live tab uses, never a second copy of the
    number - passed as a parameter only so this module never needs a
    hard top100_engine import-time dependency cycle (top100_engine
    itself does not import point_in_time_replay).

    The drop/peak side reuses drop_chip_engine.compute_peak_and_drop()
    over weekly closes truncated to `date` (via _weekly_closes_as_of()
    above) - the SAME peak math the live drop chip and fear_from_peak_
    as_of() both already use. The "clean data" rule reuses the SAME
    `dcf_unreliable`/`quality_exception_thresholds_met` replay_at_
    date() already computes for this date - no second DCF-unreliable
    check here.

    Returns {
      "quality_used": float|None, "quality_used_source": "v2"|"v1"|None,
      "quality_rule_pass": bool,
      "drop_pct": float|None, "peak_date": date|None, "peak_price": float|None,
      "drop_rule_pass": bool,
      "clean_data_rule_pass": bool,
      "all_rules_pass": bool,
    } - never raises; missing inputs simply fail that rule rather than
    aborting the check."""
    if min_quality is None:
        min_quality = top100_engine.QUALITY_ON_SALE_MIN_QUALITY

    if quality_v2 is not None:
        quality_used, quality_used_source = quality_v2, "v2"
    elif quality_v1 is not None:
        quality_used, quality_used_source = quality_v1, "v1"
    else:
        quality_used, quality_used_source = None, None
    quality_rule_pass = quality_used is not None and quality_used >= min_quality

    weekly_dates, weekly_closes = _weekly_closes_as_of(daily_closes_with_dates, date)
    peak_drop = (
        drop_chip_engine.compute_peak_and_drop(weekly_dates, weekly_closes) if weekly_closes else None
    )
    drop_pct = peak_drop.get("drop_pct") if peak_drop else None
    drop_rule_pass = drop_chip_engine.passes_quality_on_sale_drop_rule(drop_pct)

    clean_data_rule_pass = (not bool(dcf_unreliable)) or bool(quality_exception_thresholds_met)

    return {
        "quality_used": quality_used, "quality_used_source": quality_used_source,
        "quality_rule_pass": quality_rule_pass,
        "drop_pct": drop_pct,
        "peak_date": peak_drop.get("peak_date") if peak_drop else None,
        "peak_price": peak_drop.get("peak_price") if peak_drop else None,
        "drop_rule_pass": drop_rule_pass,
        "clean_data_rule_pass": clean_data_rule_pass,
        "all_rules_pass": bool(quality_rule_pass and drop_rule_pass and clean_data_rule_pass),
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


def _fridays_between(start_date, end_date):
    """Every Friday (inclusive) from `start_date` to `end_date` -
    `start_date`/`end_date` may fall on any weekday; the first/last
    Friday returned is always >= start_date and <= end_date."""
    start_date, end_date = _as_date(start_date), _as_date(end_date)
    first_friday = start_date + datetime.timedelta(days=(4 - start_date.weekday()) % 7)
    out = []
    d = first_friday
    while d <= end_date:
        out.append(d)
        d += datetime.timedelta(weeks=1)
    return out


def quality_on_sale_backtest(ticker, daily_closes_with_dates, start_date, end_date,
                              bundle=None, info=None, reporting_lag_days=STATEMENT_REPORTING_LAG_DAYS,
                              log=print):
    """TASK 44 item 6 (Director-directed, 11 Nov 2026): runs the
    "Quality on sale" tab's own three rules (quality_on_sale_as_of())
    for `ticker` on every FRIDAY from `start_date` to `end_date`
    (inclusive), using ONLY data that would have been available as of
    that Friday - the SAME point-in-time discipline every other
    function in this module already follows, never a second,
    independently-derived rule set.

    HONEST LIMITATION (same as every other point-in-time function in
    this module - see its own docstring): Quality v1 and the DCF-
    unreliable flag have NO historical replay mode at all (both are
    only ever known for a date that matches a holding's own locked
    baseline capture date). A backtest spanning many weeks essentially
    never lands on that one date, so this function reports Quality v1
    as unavailable and dcf_unreliable as "unknown, not assumed
    unreliable" for every week - it does NOT silently fall back to
    v1, and does NOT silently assume clean data. Quality v2 IS fully
    replayable (quality_v2_as_of(), bundle truncated to each Friday's
    own reporting-lag cutoff) and is what every week's own quality
    rule actually runs on; a week with no Quality V2 at all (bundle
    too thin, or the ticker not yet reporting) reports quality_used=
    None and fails the quality rule honestly, rather than guessing.

    Returns {
      "ticker": str,
      "weeks": [
        {"date", "price", "peak_date", "peak_price", "drop_pct",
         "quality_v2", "quality_v2_meta", "result" (quality_on_sale_
         as_of()'s own per-rule dict)},
        ...
      ],  # oldest Friday first, one entry per Friday in range
      "first_qualified": date|None, "last_qualified": date|None,
      "total_weeks_qualified": int,
      "gaps": [(date_before_gap, date_after_gap), ...],  # consecutive
          # QUALIFYING weeks with >=1 non-qualifying week between them
      "limitations": [str, ...],
    }
    Never raises - one week's own lookup/compute failure is logged and
    that week's "result" reads None, never aborting the rest of the
    backtest."""
    fridays = _fridays_between(start_date, end_date)
    weeks = []
    for friday in fridays:
        try:
            quality_v2, quality_v2_meta = (None, None)
            if bundle:
                quality_v2, quality_v2_meta = quality_v2_as_of(
                    ticker, bundle, friday, info=info, reporting_lag_days=reporting_lag_days)
            result = quality_on_sale_as_of(
                friday, daily_closes_with_dates, quality_v1=None, quality_v2=quality_v2,
                dcf_unreliable=None, quality_exception_thresholds_met=None,
            )
            price = None
            for d, c in (daily_closes_with_dates or []):
                if c is not None and c == c and _as_date(d) <= friday:
                    price = float(c)
            weeks.append({
                "date": friday, "price": price,
                "peak_date": result.get("peak_date"), "peak_price": result.get("peak_price"),
                "drop_pct": result.get("drop_pct"),
                "quality_v2": quality_v2, "quality_v2_meta": quality_v2_meta,
                "result": result,
            })
        except Exception as e:
            log(f"[quality_on_sale_backtest] {ticker} {friday}: failed, skipping this week: {e}")
            weeks.append({
                "date": friday, "price": None, "peak_date": None, "peak_price": None,
                "drop_pct": None, "quality_v2": None, "quality_v2_meta": None, "result": None,
            })

    qualifying_dates = [w["date"] for w in weeks if w["result"] and w["result"]["all_rules_pass"]]
    gaps = []
    for i in range(1, len(qualifying_dates)):
        if (qualifying_dates[i] - qualifying_dates[i - 1]).days > 7:
            gaps.append((qualifying_dates[i - 1], qualifying_dates[i]))

    return {
        "ticker": ticker,
        "weeks": weeks,
        "first_qualified": qualifying_dates[0] if qualifying_dates else None,
        "last_qualified": qualifying_dates[-1] if qualifying_dates else None,
        "total_weeks_qualified": len(qualifying_dates),
        "gaps": gaps,
        "limitations": [
            "Quality v1 has no historical replay mode - every week's quality rule runs "
            "on Quality v2 alone (None, never a v1 fallback, when v2 is unavailable for "
            "that week).",
            "DCF-unreliable has no historical replay mode either - every week's "
            "clean-data rule treats it as 'unknown, not assumed unreliable' (passes "
            "unless proven otherwise), never as a real per-week check.",
            "Full-universe ranking among that week's other qualifiers cannot be "
            "reconstructed point-in-time (no historical scan-universe snapshot is "
            "stored) - this function reports pass/fail only, never a rank.",
        ],
    }


def _value_score_variants(quality_v1, quality_v2, mos_pct, psychology, moat_score,
                           peak_fear_psychology=None):
    """The Director's own 4 requested variants - each a thin call into
    ranking_engine.recompute_value_score(), the ONE shared Top 200
    formula (never a parallel/independently-derived one). Discovery is
    always 0 with discovery_measured=False forced inside recompute_
    value_score() itself - see this module's own docstring on why
    Discovery can't be replayed. None for a variant whose own Quality
    input is None (nothing to score).

    `peak_fear_psychology` (TASK 42 item 6a, Director-directed, 8 Nov
    2026, CSL.AX case): when supplied, the SAME 4 setups are scored a
    SECOND time with this psychology figure in place of the real one -
    "Report CSL's replay score with it under all four setups", the
    Director's own exact wording. None (the default) adds no extra
    keys at all - every pre-item-6a caller is byte-for-byte unaffected."""
    def _score(quality, moat_blend, psych):
        if quality is None or psych is None:
            return None
        row = {"Quality": quality, "MOS %": mos_pct or 0.0,
               "Psychology": psych, "Discovery (lite)": 0,
               "Moat": moat_score}
        return ranking_engine.recompute_value_score(row, moat_blend=moat_blend)

    variants = {
        "current_formula": _score(quality_v1, False, psychology),
        "moat_blend": _score(quality_v1, True, psychology),
        "quality_v2": _score(quality_v2, False, psychology),
        "quality_v2_moat_blend": _score(quality_v2, True, psychology),
    }
    if peak_fear_psychology is not None:
        variants.update({
            "peak_fear_current_formula": _score(quality_v1, False, peak_fear_psychology),
            "peak_fear_moat_blend": _score(quality_v1, True, peak_fear_psychology),
            "peak_fear_quality_v2": _score(quality_v2, False, peak_fear_psychology),
            "peak_fear_quality_v2_moat_blend": _score(quality_v2, True, peak_fear_psychology),
        })
    return variants


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

    # TASK 42 item 6a (Director-directed, 8 Nov 2026, CSL.AX case):
    # SHADOW ONLY - swaps the fear term alone (3-year-peak fear instead
    # of the real 63-day-window fear) and leaves greed/fomo exactly as
    # psychology_as_of() already computed them, matching the Director's
    # own root-cause finding ("fear uses a 63-day window... while
    # greed/FOMO penalise the bounce above MA50") - this never re-
    # derives greed/fomo, only substitutes the one term under review.
    peak_fear_result = fear_from_peak_as_of(daily_closes_with_dates or [], date)
    peak_fear_psychology = None
    if (peak_fear_result.get("fear") is not None
            and psych.get("greed") is not None and psych.get("fomo") is not None):
        peak_fear_psychology = peak_fear_result["fear"] - psych["greed"] - psych["fomo"]
    else:
        limitations.append(
            "Peak-fear shadow variant (TASK 42 item 6a): not enough weekly history "
            "on/before this date to compute a 3-year peak, or the real fear/greed/fomo "
            "decomposition is unavailable - no peak_fear_* Value Score variants below."
        )

    value_score = _value_score_variants(
        quality_v1, quality_v2, mos_pct, psych.get("psychology"), moat_score,
        peak_fear_psychology=peak_fear_psychology,
    )

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

    # TASK 42 item 6c (Director-directed, 8 Nov 2026): would this
    # ticker have passed the "Quality on sale" tab's rules as of this
    # date - reuses the SAME dcf_unreliable/quality_exception_
    # thresholds_met this function already computed above, never a
    # second derivation.
    quality_on_sale = quality_on_sale_as_of(
        date, daily_closes_with_dates or [], quality_v1, quality_v2,
        dcf_unreliable, quality_exception_thresholds_met,
    )

    return {
        "ticker": ticker, "date": date.isoformat(),
        "price": price, "price_source": price_source,
        "intrinsic_value": iv, "intrinsic_value_source": iv_source,
        "mos_pct": mos_pct,
        "psychology": psych,
        "peak_fear": peak_fear_result, "peak_fear_psychology": peak_fear_psychology,
        "quality_v1": quality_v1, "quality_v1_source": quality_v1_source,
        "quality_v2": quality_v2, "quality_v2_meta": quality_v2_meta,
        "moat": moat_score, "moat_detail": moat_detail,
        "value_score": value_score,
        "dcf_unreliable": dcf_unreliable,
        "quality_exception_thresholds_met": quality_exception_thresholds_met,
        "today_cutoff": today_cutoff,
        "would_be_in_pool": would_be_in_pool,
        "quality_on_sale": quality_on_sale,
        "limitations": limitations,
    }
