"""
currency_risk_engine.py

Pure Python (no Streamlit dependency, no AI calls, $0 ongoing) - FX pair
daily-close history (fetch/cache) and the risk statistics for the
Currency Risk page. Same "*_engine.py owns the logic, *_render.py owns
markup" split top100_engine.py/top100_render.py already established.

Owner-approved mock (25 Sep 2026, "headwind_and_currency_risk_mock.html",
section ②): pick any two currencies (default AUD -> USD), a range
(5y/10y/20y/Max), and get (A) where the rate sits in its own history,
(B) the distribution of rolling 12-month changes, (C) what a given
position size stands to gain/lose from the FX leg alone under three
scenarios. Every number here is computed fresh from cached daily closes -
never a live feed per page view, never an AI call.
"""
import datetime
import json
import math
import os
import tempfile

import yfinance as yf

CURRENCIES = ["AUD", "USD", "EUR", "GBP", "JPY", "NZD", "CAD", "SGD"]
DEFAULT_BASE = "AUD"
DEFAULT_QUOTE = "USD"

# Range chips (task's own words) - "max" pulls whatever yfinance's own
# period="max" returns, no fixed cutoff.
RANGE_KEYS = ["5y", "10y", "20y", "max"]
RANGE_YEARS = {"5y": 5, "10y": 10, "20y": 20, "max": None}
DEFAULT_RANGE = "10y"

# Same "cached once per day" cadence as every other daily-price feed on
# this site (fundamentals_data.py's own 24h BUNDLE cache is the closest
# precedent - see that module's own _CACHE_TTL_SECONDS).
CACHE_TTL_SECONDS = 24 * 3600
BUNDLE_VERSION = 1

TRADING_DAYS_PER_YEAR = 252
# ~12 trading months - the "holding-period risk" window (section B).
ROLLING_WINDOW_DAYS = 252


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


def _cache_dir():
    path = os.path.join(_data_dir(), "currency_risk_cache")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _pair_key(base, quote):
    return f"{base.upper()}{quote.upper()}"


def _cache_path(base, quote):
    return os.path.join(_cache_dir(), f"{_pair_key(base, quote)}.json")


def _yahoo_symbol(base, quote):
    """"{BASE}{QUOTE}=X" - the task's own naming, the same Yahoo FX
    convention fcf_valuation_engine.fx_rate() already uses (e.g.
    "AUDUSD=X" -> USD per AUD)."""
    return f"{base.upper()}{quote.upper()}=X"


def _read_cache(base, quote, ignore_ttl=False):
    """Cached history dict if present (and, unless ignore_ttl, younger
    than CACHE_TTL_SECONDS) - None on any miss/parse/IO error, fails
    through to a live fetch. ignore_ttl=True is the stale-fallback peek
    (get_fx_history()'s own fetch-failed path) - same "ignore TTL, still
    check the version, never trigger a fetch" shape as fundamentals_
    data.peek_cached_bundle()."""
    path = _cache_path(base, quote)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            obj = json.load(f)
        if obj.get("bundle_version") != BUNDLE_VERSION:
            return None
        fetched_at = obj.get("fetched_at")
        if not fetched_at:
            return None
        if not ignore_ttl:
            age = (
                datetime.datetime.now(datetime.timezone.utc)
                - datetime.datetime.fromisoformat(fetched_at)
            ).total_seconds()
            if age > CACHE_TTL_SECONDS or age < 0:
                return None
        return obj
    except Exception:
        return None


def _write_cache(base, quote, obj):
    """Atomic write (tmp file + os.replace), best-effort - same pattern
    as fundamentals_data._write_cache()."""
    path = _cache_path(base, quote)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp_path, path)
    except OSError:
        pass


def _fetch_daily_closes(symbol):
    """[(date_iso, close), ...] ascending, or None on any failure/empty
    result - the site's own "any exception during a live feed call
    becomes None, never propagates" convention (see fcf_valuation_
    engine.fx_rate()'s own try/except)."""
    try:
        hist = yf.Ticker(symbol).history(period="max", interval="1d")
        if hist is None or hist.empty:
            return None
        closes = hist["Close"].dropna()
        if closes.empty:
            return None
        return [(d.strftime("%Y-%m-%d"), float(c)) for d, c in closes.items()]
    except Exception:
        return None


def _fetch_live(base, quote):
    """(rows, source) via the direct "{BASE}{QUOTE}=X" symbol, or - when
    that symbol has no data (some cross pairs, e.g. two minor currencies
    with no direct Yahoo quote) - a synthetic cross rate via USD:
    (BASE/USD) / (QUOTE/USD) joined on shared dates. (None, None) if
    neither resolves. Report any pair that needed this fallback in the
    task's own "note any pair whose Yahoo symbol needed special-casing"
    ask."""
    direct = _fetch_daily_closes(_yahoo_symbol(base, quote))
    if direct:
        return direct, "direct"
    if base == "USD" or quote == "USD":
        return None, None
    base_usd = _fetch_daily_closes(_yahoo_symbol(base, "USD"))
    quote_usd = _fetch_daily_closes(_yahoo_symbol(quote, "USD"))
    if not base_usd or not quote_usd:
        return None, None
    quote_usd_map = dict(quote_usd)
    cross = [(d, c / quote_usd_map[d]) for d, c in base_usd if d in quote_usd_map and quote_usd_map[d]]
    if not cross:
        return None, None
    return cross, "cross_via_usd"


def get_fx_history(base, quote, force_refresh=False):
    """{"dates": [...], "closes": [...], "source": "direct"|
    "cross_via_usd", "fetched_at": iso, "stale": bool} for BASE/QUOTE,
    daily closes ascending, full available history (range-slicing
    happens downstream in period_stats()/rolling_12m_distribution()
    callers, not here) - cached once per day on disk. A failed live
    fetch serves the last cached copy regardless of age, flagged
    stale=True - NEVER an empty chart (the task's own explicit
    requirement). Returns None only when there is no cache at all AND
    the live fetch also failed (first-ever request for a dead/unlisted
    pair)."""
    base, quote = base.upper(), quote.upper()
    if not force_refresh:
        cached = _read_cache(base, quote, ignore_ttl=False)
        if cached:
            return {**cached, "stale": False}

    rows, source = _fetch_live(base, quote)
    if rows:
        obj = {
            "bundle_version": BUNDLE_VERSION,
            "dates": [d for d, _ in rows],
            "closes": [c for _, c in rows],
            "source": source,
            "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        _write_cache(base, quote, obj)
        return {**obj, "stale": False}

    stale_cached = _read_cache(base, quote, ignore_ttl=True)
    if stale_cached:
        return {**stale_cached, "stale": True}
    return None


def slice_range(dates, closes, range_key):
    """(dates, closes) restricted to the trailing RANGE_YEARS[range_key]
    years (or the full series for "max"/an unrecognised key). Assumes
    `dates` ascending "YYYY-MM-DD" strings. A Feb-29 cutoff landing on a
    non-leap target year falls back to Feb 28, the same "nearest valid
    date" handling date.replace() itself can't do across a leap
    boundary."""
    years = RANGE_YEARS.get(range_key)
    if not years or not dates:
        return dates, closes
    latest = datetime.date.fromisoformat(dates[-1])
    try:
        cutoff = latest.replace(year=latest.year - years)
    except ValueError:
        cutoff = latest.replace(month=2, day=28, year=latest.year - years)
    cutoff_iso = cutoff.isoformat()
    idx = len(dates)
    for i, d in enumerate(dates):
        if d >= cutoff_iso:
            idx = i
            break
    return dates[idx:], closes[idx:]


def period_stats(closes):
    """{"today","average","sigma","pct_vs_average","range_min",
    "range_max","percentile","annualised_vol_pct"} over the given
    (already range-sliced) closes list.

    `sigma`: the LEVEL standard deviation (sample stdev, ddof=1, of the
    daily CLOSE values themselves) - the same number the chart's own
    ±1σ band (section A) and the position-impact scenarios (section C)
    both reuse, so "the three scenarios: average, +1σ, -1σ" are always
    exactly the three numbers already shown on the chart.

    `annualised_vol_pct`: a DIFFERENT, standard measure - stdev of daily
    LOG RETURNS (ddof=1) x sqrt(TRADING_DAYS_PER_YEAR) x 100, its own
    stat tile, not used anywhere else.

    `percentile`: today's close's percentile rank within this window's
    own distribution (fraction of days at or below today's close).

    None if fewer than 2 closes (can't compute a stdev)."""
    n = len(closes)
    if n < 2:
        return None
    today = closes[-1]
    average = sum(closes) / n
    variance = sum((c - average) ** 2 for c in closes) / (n - 1)
    sigma = math.sqrt(variance)
    pct_vs_average = (today - average) / average * 100 if average else 0.0
    rank = sum(1 for c in closes if c <= today)
    percentile = rank / n * 100

    log_returns = [
        math.log(closes[i] / closes[i - 1])
        for i in range(1, n)
        if closes[i] > 0 and closes[i - 1] > 0
    ]
    if len(log_returns) >= 2:
        mean_lr = sum(log_returns) / len(log_returns)
        var_lr = sum((r - mean_lr) ** 2 for r in log_returns) / (len(log_returns) - 1)
        annualised_vol_pct = math.sqrt(var_lr) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100
    else:
        annualised_vol_pct = None

    return {
        "today": today, "average": average, "sigma": sigma,
        "pct_vs_average": pct_vs_average,
        "range_min": min(closes), "range_max": max(closes),
        "percentile": percentile,
        "annualised_vol_pct": annualised_vol_pct,
    }


# Six buckets per the mock, half-open [lo, hi) except the two open ends -
# a change of exactly -10.0 or +10.0 lands in the middle/outer bucket
# respectively, a defensible, consistent tie-break rather than an
# undefined gap between buckets.
ROLLING_BUCKETS = [
    ("lt_m10", None, -10.0),
    ("m10_m5", -10.0, -5.0),
    ("m5_0", -5.0, 0.0),
    ("0_p5", 0.0, 5.0),
    ("p5_p10", 5.0, 10.0),
    ("gt_p10", 10.0, None),
]


def rolling_12m_distribution(closes, window=ROLLING_WINDOW_DAYS):
    """{"changes": [...], "bucket_counts": {key: n}, "bucket_pct":
    {key: pct}, "worst", "best", "typical_swing", "n_windows"} - the
    distribution of rolling WINDOW-trading-day (~12 month) % changes
    across the (already range-sliced) closes list. `typical_swing` is
    the sample stdev (ddof=1) of those rolling changes - the "±1σ
    typical swing" stat tile, a DIFFERENT sigma from period_stats()'s
    own level sigma (this one describes how much a 12-MONTH CHANGE
    typically varies, not the level itself). None if the window has
    fewer than `window`+1 closes (can't form even one rolling window)."""
    n = len(closes)
    if n <= window:
        return None
    changes = []
    for i in range(window, n):
        prev = closes[i - window]
        if prev:
            changes.append((closes[i] / prev - 1) * 100)
    if not changes:
        return None

    bucket_counts = {key: 0 for key, _, _ in ROLLING_BUCKETS}
    for chg in changes:
        for key, lo, hi in ROLLING_BUCKETS:
            if (lo is None or chg >= lo) and (hi is None or chg < hi):
                bucket_counts[key] += 1
                break
    total = len(changes)
    bucket_pct = {k: v / total * 100 for k, v in bucket_counts.items()}

    mean_chg = sum(changes) / total
    if total >= 2:
        var_chg = sum((c - mean_chg) ** 2 for c in changes) / (total - 1)
        typical_swing = math.sqrt(var_chg)
    else:
        typical_swing = 0.0

    return {
        "changes": changes, "bucket_counts": bucket_counts, "bucket_pct": bucket_pct,
        "worst": min(changes), "best": max(changes), "typical_swing": typical_swing,
        "n_windows": total,
    }


POSITION_SCENARIO_KEYS = ["average", "plus_1sigma", "minus_1sigma"]


def position_impact(position_size, current_rate, average, sigma):
    """[{"key","scenario_rate","pct_change","amount_change"}, ...] for
    POSITION_SCENARIO_KEYS - what a `position_size` (in BASE currency
    today) worth of a foreign, QUOTE-currency-denominated holding is
    worth under three FX-leg-only scenarios, price movement in the
    underlying asset itself held constant.

    value_factor = current_rate / scenario_rate (the task's own exact
    formula): scenario_rate > current_rate (the base currency
    strengthens toward/through that level) SHRINKS the foreign
    holding's base-currency value - a drag; scenario_rate < current_rate
    (base weakens) grows it - a tailwind. Verified against the mock's
    own worked numbers (current 0.665, average 0.699 -> -4.9%; +1σ
    0.740 -> -10.1%; -1σ 0.630 -> +5.6%) - exact match.

    A non-positive scenario rate (sigma >= average, a degenerate/
    near-zero-rate pair) is skipped rather than dividing by a
    meaningless negative rate."""
    scenarios = [
        ("average", average),
        ("plus_1sigma", average + sigma),
        ("minus_1sigma", average - sigma),
    ]
    out = []
    for key, scenario_rate in scenarios:
        if scenario_rate is None or scenario_rate <= 0 or current_rate is None:
            continue
        value_factor = current_rate / scenario_rate
        pct_change = (value_factor - 1) * 100
        amount_change = position_size * (value_factor - 1)
        out.append({
            "key": key, "scenario_rate": scenario_rate,
            "pct_change": pct_change, "amount_change": amount_change,
        })
    return out
