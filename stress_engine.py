"""
stress_engine.py

Next-batch instruction, Part 3 ("Stress Test" tab): current holdings +
weights replayed against REAL historical prices/distributions, named
crisis/rally windows, a beta-weighted shock grid, and a historical Monte
Carlo band. Pandas/numpy only - no new dependency.

Why this module needs its OWN price-history fetch, separate from the
site's existing one:

  Every other portfolio tab (and etf_insights.py, Part 2) deliberately
  reuses portfolio_health_engine.fetch_snapshot()'s already-fetched 2y
  history to add zero new per-holding network calls. The Stress Test
  tab's crisis windows reach back to the GFC (Oct 2007) - a 2y window
  cannot possibly cover that, so this module fetches its own longer
  history (yfinance's period="max") per ticker, but - matching the same
  "no new per-pageview network fetch" spirit - caches it on the volume
  (the same shared stocksdeepdive.db every other store in this app
  uses) for 24 hours, so it is fetched at most once per ticker per day
  regardless of how many visitors load the tab, not once per pageview.

Two kinds of history flow through this module:
  - a per-holding OWN price history (get_long_history), used for the
    synthetic full replay and for any scenario window the holding
    actually has data for;
  - a per-holding home INDEX history (^AXJO for a .AX ticker, ^GSPC
    otherwise - see etf_insights.benchmark_ticker_for, reused here
    rather than re-implemented) used to measure that holding's beta and
    to proxy a scenario return when the holding itself has no data for
    that window (flagged (diamond) estimated throughout, per the spec).

IMPORTANT - same environment constraint as etf_insights.py (Part 2):
this sandbox and the device bridge both have zero live path to Yahoo
Finance (every yfinance call fails with a proxied CONNECT error,
confirmed by direct curl test), so this module was written against
yfinance's documented/verified-from-source `history()` shape and
unit-tested against hand-built fixtures spanning many years (including
synthetic drawdowns/rallies), never against Andrew's real holdings'
actual multi-decade history. See the Part 3 report for what to
spot-check after this deploys.

Every function here takes already-fetched data (a DataFrame, a dict of
DataFrames, or plain numbers) and returns plain floats/dicts - never
raises, and returns None/"-"-friendly values on insufficient data
rather than crashing a render.

MEGA-BATCH PART 14 (urgent data fix, 2026-09-06): the owner found
IVV.AX showing an impossible worst drawdown of -93.9% and beta 0.43.
Root cause - get_long_history() fetched yfinance's raw, NOT split-
adjusted, Close series (no explicit auto_adjust argument was ever
passed, leaving it to whatever the installed yfinance version's
default happened to be). iShares split IVV.AX 15-for-1 in Dec 2022;
an unadjusted Close series shows that as a fake ~-93% single-day
crash (1 - 1/15), which then poisoned every downstream number built
from it: max drawdown, best-12m, the monthly-return covariance (hence
every beta), the portfolio headline figures, and the Monte Carlo
inputs. CSL.AX's implausible -68% 15y drawdown was the same fault.

Fix, two layers:
  1. get_long_history() now passes auto_adjust=True EXPLICITLY (never
     relying on the library default again). yfinance's auto_adjust
     replaces Close with Yahoo's own split-and-dividend-back-adjusted
     "Adj Close" - so day-over-day pct-change on Close is now already
     a full TOTAL-RETURN figure (price return + reinvested
     distributions baked in via the adjustment ratio), not a bare
     price return. The separate "Dividends" column yfinance returns
     is the RAW per-share amount on its ex-date - untouched by
     auto_adjust - so daily_returns()/_window_return_from_hist() no
     longer add it on top of the price change (that would now double-
     count every distribution); it's kept in the cache purely because
     etf_insights.ttm_distribution_yield still needs the raw per-share
     figure for its (correctly RAW, not total-return) yield calc.
  2. sanity_checked_history() below is the second line of defence:
     Yahoo's own back-adjustment isn't guaranteed complete for every
     ASX-listed instrument's corporate-action history, so after the
     auto_adjust fetch every holding's OWN price history is still
     checked for a leftover split-sized single-day move; if found, a
     direct one-off yf.Ticker(ticker).splits lookup is used to repair
     it by hand, and if that still doesn't clear the anomaly the
     ticker's history is treated as unusable and excluded from every
     replay that would otherwise be built from it - see that
     function's own docstring.

The whole stress_long_history/stress_result_cache tables were also
renamed (both now end in _v2 - see DB_PATH/_conn() below) rather than
patched in place: every previously-cached price series and assembled
result was potentially built from the old, unadjusted convention, and
a plain schema/table-version bump is the simplest way to guarantee
nothing stale is ever read again, without needing a one-off migration
script that has to run exactly once in production.
"""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yfinance as yf

import etf_insights

# -----------------------------------------------------------------
# Storage - same volume-resolution rule as every other store in this
# app (see etf_insights.py / explain_cache_store.py).
# -----------------------------------------------------------------

def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

LONG_HISTORY_TTL_HOURS = 24  # see module docstring - "nightly-ish"
RESULT_TTL_HOURS = 24        # the assembled stress-test result itself


_LONG_HISTORY_TABLE = "stress_long_history_v2"
_RESULT_CACHE_TABLE = "stress_result_cache_v2"


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    # Part 14: _v2 table names (was stress_long_history/stress_result_cache)
    # - see module docstring's "two layers" note. The old tables are left
    # in place untouched (harmless, tiny) rather than dropped; they are
    # simply never read again, which is all "invalidate the cache" needs
    # to mean here - every ticker/result recomputes fresh under the new
    # auto_adjust=True + sanity-guard convention on its next request.
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {_LONG_HISTORY_TABLE} (
            ticker TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {_RESULT_CACHE_TABLE} (
            cache_key TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    return conn


def _hist_to_json(hist):
    """A Close+Dividends DataFrame -> a compact JSON-able dict. Only the
    two columns the whole module needs are kept (no OHLV/splits) to keep
    the cached blob small."""
    return {
        "dates": [d.strftime("%Y-%m-%d") for d in hist.index],
        "close": [None if pd.isna(v) else float(v) for v in hist["Close"]],
        "dividends": ([None if pd.isna(v) else float(v) for v in hist["Dividends"]]
                      if "Dividends" in hist.columns else [0.0] * len(hist)),
    }


def _hist_from_json(d):
    idx = pd.to_datetime(d["dates"])
    return pd.DataFrame({"Close": d["close"], "Dividends": d["dividends"]}, index=idx)


def _cache_get_history(ticker):
    with _conn() as conn:
        row = conn.execute(
            f"SELECT data_json, created_at FROM {_LONG_HISTORY_TABLE} WHERE ticker = ?",
            (ticker,),
        ).fetchone()
    if row is None:
        return None
    created_at = datetime.fromisoformat(row[1])
    if datetime.now(timezone.utc) - created_at > timedelta(hours=LONG_HISTORY_TTL_HOURS):
        return None
    try:
        return _hist_from_json(json.loads(row[0]))
    except Exception:
        return None


def _cache_set_history(ticker, hist):
    with _conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {_LONG_HISTORY_TABLE} (ticker, data_json, created_at) VALUES (?, ?, ?)",
            (ticker, json.dumps(_hist_to_json(hist)), datetime.now(timezone.utc).isoformat()),
        )


def get_long_history(ticker, force_refresh=False):
    """Up to ~max available (yfinance period="max") daily Close+
    Dividends for `ticker`, cached 24h on the volume - see module
    docstring for why this is a separate fetch from the site's 2y
    portfolio snapshot. Never raises; an empty DataFrame means no data
    is available (a delisted ticker, or this environment's own no-
    network-access constraint - see module docstring).

    Part 14: auto_adjust=True is now passed EXPLICITLY (previously
    relied on the yfinance default, which is how an unadjusted-for-
    splits Close series slipped through) - see module docstring's
    "two layers" note. The returned "Close" is Yahoo's own split-and-
    dividend-back-adjusted price; "Dividends" stays the raw per-share
    amount, untouched by that adjustment."""
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return pd.DataFrame()
    if not force_refresh:
        cached = _cache_get_history(ticker)
        if cached is not None:
            return cached
    try:
        hist = yf.Ticker(ticker).history(period="max", auto_adjust=True)
        if hist is None or hist.empty:
            hist = pd.DataFrame()
        else:
            hist = hist[["Close", "Dividends"]] if "Dividends" in hist.columns else hist[["Close"]].assign(Dividends=0.0)
    except Exception:
        hist = pd.DataFrame()
    try:
        if not hist.empty:
            _cache_set_history(ticker, hist)
    except Exception:
        pass
    return hist


# -----------------------------------------------------------------
# Named scenario windows - per the instruction's own list.
# -----------------------------------------------------------------

CRISES = [
    ("gfc", "GFC", "2007-10-01", "2009-03-31"),
    ("q4_2018", "2018 Q4", "2018-10-01", "2018-12-31"),
    ("covid", "COVID crash", "2020-02-01", "2020-03-31"),
    ("rate_shock_2022", "2022 rate shock", "2022-01-01", "2022-10-31"),
]

RALLIES = [
    ("post_gfc", "Post-GFC rebound", "2009-03-01", "2009-12-31"),
    ("melt_up_2016_17", "2016-17 melt-up", "2016-01-01", "2017-12-31"),
    ("covid_recovery", "COVID recovery", "2020-04-01", "2021-03-31"),
    ("ai_rally_2023_24", "2023-24 AI rally", "2023-01-01", "2024-12-31"),
]


def home_index_for(ticker):
    """ASX 200 for a .AX ticker, S&P 500 otherwise - reuses Part 2's
    own convention rather than re-deriving it."""
    return etf_insights.benchmark_ticker_for(ticker)


# -----------------------------------------------------------------
# Part 14 sanity guard: a split-sized single-day move that survived
# get_long_history()'s auto_adjust=True fetch (Yahoo's own back-
# adjustment isn't guaranteed complete for every ASX-listed
# instrument's corporate-action history - the IVV.AX finding that
# prompted this whole part). A fund/ETF tracking a diversified index
# essentially never gaps this hard even in a real crash, so it gets a
# tighter bar than a single stock, which can legitimately gap on
# binary news (a takeover, a trial result, a delisting).
# -----------------------------------------------------------------

SPLIT_ANOMALY_THRESHOLD_FUND = 0.40
SPLIT_ANOMALY_THRESHOLD_STOCK = 0.60


def _detect_anomalous_move(hist, threshold):
    """True if `hist`'s Close has any single-day move beyond
    `threshold` in absolute value - the guard's trigger condition."""
    if hist is None or hist.empty or "Close" not in hist.columns or len(hist) < 2:
        return False
    close = hist["Close"].astype(float)
    ret = close.pct_change().dropna()
    if ret.empty:
        return False
    return bool((ret.abs() > threshold).any())


def _attempt_split_repair(ticker, hist):
    """Fetches `ticker`'s own split-event log directly (a small, one-
    off, uncached call - only ever reached once _detect_anomalous_move
    has already found a problem, so this never runs on the healthy
    path) and, for every split it lists, retroactively divides every
    Close price strictly before that split's ex-date by the split
    ratio - exactly what auto_adjust=True is already supposed to have
    done, redone by hand as a second line of defence for whichever
    listings Yahoo's own back-adjustment doesn't fully cover. Returns
    a (possibly) repaired copy of `hist`; never raises - a failed
    lookup or an empty split log just returns `hist` unchanged, and
    the caller's own re-check after this call is what actually decides
    whether the ticker ends up usable."""
    try:
        splits = yf.Ticker(ticker).splits
    except Exception:
        return hist
    if splits is None or len(splits) == 0:
        return hist
    repaired = hist.copy()
    idx = repaired.index
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
        repaired.index = idx
    for split_date, ratio in splits.items():
        try:
            ratio = float(ratio)
        except (TypeError, ValueError):
            continue
        if not ratio or ratio == 1.0:
            continue
        sd = pd.Timestamp(split_date)
        if sd.tzinfo is not None:
            sd = sd.tz_localize(None)
        before = repaired.index < sd
        repaired.loc[before, "Close"] = repaired.loc[before, "Close"] / ratio
    return repaired


def sanity_checked_history(ticker, hist, is_fund):
    """Part 14's sanity guard. `hist` (already fetched with
    auto_adjust=True) is checked for a single-day move beyond
    SPLIT_ANOMALY_THRESHOLD_FUND (funds/ETFs) or _STOCK (everything
    else). A clean series is returned unchanged. An anomalous one goes
    through _attempt_split_repair and is re-checked; if that clears
    it, the repaired series is returned; if it doesn't, this ticker's
    history is treated as unusable - (None, True) is returned - rather
    than ever feeding a corrupted series into a drawdown/beta/Monte-
    Carlo computation. Returns (checked_hist_or_None, was_faulty)."""
    threshold = SPLIT_ANOMALY_THRESHOLD_FUND if is_fund else SPLIT_ANOMALY_THRESHOLD_STOCK
    if not _detect_anomalous_move(hist, threshold):
        return hist, False
    repaired = _attempt_split_repair(ticker, hist)
    if not _detect_anomalous_move(repaired, threshold):
        return repaired, False
    return None, True


# -----------------------------------------------------------------
# Returns, beta, drawdown - all from a Close+Dividends DataFrame.
# -----------------------------------------------------------------

def daily_returns(hist):
    """Simple daily total-return series - plain pct-change on `hist`'s
    Close. None if `hist` has under 2 rows.

    Part 14: `hist`'s Close now comes from get_long_history()'s
    auto_adjust=True fetch, which is ALREADY a total-return series
    (Yahoo's own back-adjustment folds reinvested distributions into
    the price) - so no per-share Dividends are added here any more
    (doing so on top of an adjusted Close would double-count every
    distribution). See module docstring."""
    if hist is None or hist.empty or "Close" not in hist.columns or len(hist) < 2:
        return None
    close = hist["Close"].astype(float)
    ret = close.pct_change()
    return ret.dropna()


def _monthly_returns(hist):
    """Delegates to etf_insights' own implementation (same "resample to
    month-end Close" logic) rather than re-implementing it."""
    return etf_insights._monthly_returns(hist)


def compute_beta(holding_hist, index_hist, min_months=12):
    """OLS slope of holding monthly returns on index monthly returns
    (cov/var) - None with fewer than `min_months` overlapping months. A
    lower bar than etf_insights.correlation_vs's 24-month floor (a beta
    for a young holding is still useful directionally; the caller flags
    any downstream figure computed FROM a proxy that itself used this
    beta, per the spec's own (diamond) estimated marker)."""
    a = _monthly_returns(holding_hist)
    b = _monthly_returns(index_hist)
    if a is None or b is None:
        return None
    aligned = pd.concat([a, b], axis=1, join="inner").dropna()
    if len(aligned) < min_months:
        return None
    x = aligned.iloc[:, 1].values
    y = aligned.iloc[:, 0].values
    var_x = np.var(x, ddof=1)
    if var_x == 0:
        return None
    cov_xy = np.cov(x, y, ddof=1)[0, 1]
    return float(cov_xy / var_x)


def build_combined_series(weights, histories):
    """Portfolio total-return index (starts at 1.0) from `weights`
    ({ticker: weight fraction, weights need not sum to 1 - they are
    renormalized here}) and `histories` ({ticker: Close+Dividends
    DataFrame}).

    Combined on the INNER-joined common date range across every
    weighted holding that has any history at all (holdings with no
    history are simply excluded, with their weight renormalized across
    the rest) - so the actual replay window is naturally capped by
    whichever included holding has the SHORTEST history. This is a
    simplification worth stating plainly: a portfolio whose youngest
    holding was bought last year will only replay ~1y of real history
    even if its other holdings go back to the 1990s. Returns
    (series, tickers_used, tickers_dropped)."""
    weights = {t: w for t, w in (weights or {}).items() if w}
    total_w = sum(weights.values())
    if total_w <= 0:
        return None, [], list(weights.keys())

    rets = {}
    dropped = []
    for t, w in weights.items():
        h = (histories or {}).get(t)
        r = daily_returns(h)
        if r is None or r.empty:
            dropped.append(t)
            continue
        rets[t] = r
    if not rets:
        return None, [], dropped

    used_w_total = sum(weights[t] for t in rets)
    if used_w_total <= 0:
        return None, [], list(weights.keys())
    norm_w = {t: weights[t] / used_w_total for t in rets}

    df = pd.concat(rets, axis=1, join="inner")
    if df.empty:
        return None, [], list(weights.keys())
    port_daily_ret = sum(df[t] * norm_w[t] for t in rets)
    series = (1.0 + port_daily_ret).cumprod()
    return series, list(rets.keys()), dropped


def cap_to_years(hist, years):
    """The last `years` years of `hist` (or all of it if shorter) - used
    to bound the "up to 15y" synthetic full-replay window (drawdown/
    best-12m/replayed-return) to what the instruction actually asks
    for, WITHOUT capping the separate scenario-replay/beta history
    (those need the FULL available history - a 15y cap from "today"
    would exclude the GFC entirely)."""
    if hist is None or hist.empty:
        return hist
    cutoff = hist.index[-1] - pd.Timedelta(days=int(365.25 * years))
    return hist[hist.index >= cutoff]


def drawdown_series(series):
    """(value / running all-time-high - 1) at every point - always <= 0.
    The chart-ready mirror of max_drawdown()'s single worst figure."""
    if series is None or series.empty:
        return None
    return series / series.cummax() - 1.0


def runup_from_trough_series(series):
    """(value / running all-time-low - 1) at every point - always >= 0,
    and the deliberate symmetric counterpart to drawdown_series() above
    (that one tracks distance below the running HIGH; this one tracks
    distance above the running LOW). Reads as "how much the portfolio
    has recovered/grown from the worst point seen so far" - it only
    resets toward 0 when an actual new all-time low is set, not on some
    fixed schedule."""
    if series is None or series.empty:
        return None
    return series / series.cummin() - 1.0


def max_drawdown(series):
    """Deepest peak-to-trough decline in `series` (a total-return index)
    - {"pct", "peak_date", "trough_date", "recovered_date",
    "months_to_recover"} (the last two None if the series never
    recovers back to its pre-drawdown peak by the series' last point).
    None if `series` is None/empty."""
    if series is None or series.empty:
        return None
    running_max = series.cummax()
    drawdown = series / running_max - 1.0
    trough_date = drawdown.idxmin()
    trough_val = float(drawdown.loc[trough_date])
    peak_date = series.loc[:trough_date].idxmax()
    recovery_level = float(series.loc[peak_date])
    after_trough = series.loc[trough_date:]
    recovered = after_trough[after_trough >= recovery_level]
    recovered_date = recovered.index[0] if len(recovered) else None
    months_to_recover = None
    if recovered_date is not None:
        months_to_recover = round((recovered_date - trough_date).days / 30.44, 1)
    return {
        "pct": trough_val * 100.0,
        "peak_date": peak_date, "trough_date": trough_date,
        "recovered_date": recovered_date, "months_to_recover": months_to_recover,
    }


def best_rolling_12m(series):
    """Best rolling ~252-trading-day (12-month) return in `series` -
    {"pct", "start_date", "end_date"}, or None if `series` has under a
    year of data."""
    if series is None or len(series) < 253:
        return None
    window = 252
    rolled = series / series.shift(window) - 1.0
    rolled = rolled.dropna()
    if rolled.empty:
        return None
    end_date = rolled.idxmax()
    pct = float(rolled.loc[end_date])
    start_date = series.index[series.index.get_loc(end_date) - window]
    return {"pct": pct * 100.0, "start_date": start_date, "end_date": end_date}


def replayed_return_pa(series, years=10):
    """Annualised return of `series` over its own last `years` years (or
    its full span if shorter) - None if under a year of data."""
    if series is None or len(series) < 2:
        return None
    end_dt = series.index[-1]
    start_dt = end_dt - pd.Timedelta(days=int(365.25 * years))
    window = series[series.index >= start_dt]
    if len(window) < 2:
        return None
    span_days = (window.index[-1] - window.index[0]).days
    if span_days < 365:
        return None
    total_growth = float(window.iloc[-1]) / float(window.iloc[0])
    if total_growth <= 0:
        return None
    actual_years = span_days / 365.25
    return (total_growth ** (1.0 / actual_years) - 1.0) * 100.0


# -----------------------------------------------------------------
# Scenario replays - crisis/rally windows, with sector/index x beta
# proxying for a holding that lacks data over the window.
# -----------------------------------------------------------------

def _window_return_from_hist(hist, start, end):
    """Actual total return of one holding's OWN history within
    [start, end], or None if it has under 2 data points inside the
    window (i.e. it wasn't trading / has no cached history there - the
    caller proxies in that case).

    Part 14: plain price growth on `hist`'s (auto_adjust=True) Close -
    no per-share Dividends added on top; see daily_returns()'s
    docstring for why that would now double-count distributions."""
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    window = hist[(hist.index >= start) & (hist.index <= end)]
    if len(window) < 2:
        return None
    start_price = float(window["Close"].iloc[0])
    if start_price <= 0:
        return None
    end_price = float(window["Close"].iloc[-1])
    return end_price / start_price - 1.0


def scenario_replay(weights, histories, index_histories, betas, window, total_value_aud):
    """One named crisis/rally window -> per-holding rows (ticker,
    move_pct, estimated bool) + the portfolio's weighted move (% and
    $). A holding with no own-history coverage of the window is proxied
    as `home_index's actual move over the window x that holding's own
    beta` and flagged estimated=True; a holding with neither its own
    coverage NOR a computable beta is simply excluded (weight
    renormalized across the rest, same convention as
    build_combined_series) rather than guessed at with no basis at all.
    """
    _key, label, start, end = window
    rows = []
    weights = {t: w for t, w in (weights or {}).items() if w}
    for t, w in weights.items():
        hist = (histories or {}).get(t)
        actual = _window_return_from_hist(hist, start, end)
        if actual is not None:
            rows.append({"ticker": t, "weight": w, "move_pct": actual * 100.0, "estimated": False})
            continue
        beta = (betas or {}).get(t)
        idx_ticker = home_index_for(t)
        idx_hist = (index_histories or {}).get(idx_ticker)
        idx_move = _window_return_from_hist(idx_hist, start, end)
        if beta is not None and idx_move is not None:
            rows.append({"ticker": t, "weight": w, "move_pct": beta * idx_move * 100.0, "estimated": True})
        # else: no basis at all for this holding in this window - excluded.

    total_w = sum(r["weight"] for r in rows)
    if total_w <= 0:
        return {"key": _key, "label": label, "start": start, "end": end,
                "move_pct": None, "move_value_aud": None, "rows": rows, "best": None, "worst": None}

    move_pct = sum(r["move_pct"] * r["weight"] for r in rows) / total_w
    move_value_aud = (move_pct / 100.0) * total_value_aud if total_value_aud else None
    ranked = sorted(rows, key=lambda r: r["move_pct"])
    worst = ranked[0] if ranked else None
    best = ranked[-1] if ranked else None
    return {
        "key": _key, "label": label, "start": start, "end": end,
        "move_pct": move_pct, "move_value_aud": move_value_aud,
        "rows": rows, "best": best, "worst": worst,
    }


# -----------------------------------------------------------------
# Beta-weighted shock grid.
# -----------------------------------------------------------------

SHOCK_LEVELS = (-40, -20, -10, 10, 20, 40)


def portfolio_beta(weights, betas):
    """Value-weighted average beta across every holding with a
    computable beta (weight renormalized across those) - None if no
    holding has one."""
    weights = {t: w for t, w in (weights or {}).items() if w}
    num, den = 0.0, 0.0
    for t, w in weights.items():
        b = (betas or {}).get(t)
        if b is not None:
            num += b * w
            den += w
    return (num / den) if den else None


def shock_grid(beta, total_value_aud):
    """beta-weighted portfolio move (% and $) at each SHOCK_LEVELS
    index move - a plain linear beta model, not a replay; explicitly
    labelled as such by the caller."""
    if beta is None:
        return []
    out = []
    for shock_pct in SHOCK_LEVELS:
        move_pct = beta * shock_pct
        out.append({
            "shock_pct": shock_pct, "move_pct": move_pct,
            "move_value_aud": (move_pct / 100.0) * total_value_aud if total_value_aud else None,
        })
    return out


# -----------------------------------------------------------------
# Monte Carlo - 5,000 one-year paths from the historical monthly
# mean/vol/correlation matrix (numpy only).
# -----------------------------------------------------------------

def monte_carlo(weights, histories, total_value_aud, n_paths=5000, horizon_months=12, seed=None):
    """5,000 (default) simulated one-year portfolio paths, each drawn
    from a multivariate-normal model of the holdings' own historical
    monthly returns (mean vector + covariance matrix - captures the
    same volatility AND correlation each holding actually showed).
    Returns {"p5_pct","p95_pct","p5_value_aud","p95_value_aud",
    "n_holdings_used"} - None if fewer than 2 holdings have enough
    monthly history (>=12 months) to build a covariance matrix from."""
    weights = {t: w for t, w in (weights or {}).items() if w}
    monthlies = {}
    for t in weights:
        h = (histories or {}).get(t)
        m = _monthly_returns(h)
        if m is not None and len(m) >= 12:
            monthlies[t] = m
    if len(monthlies) < 1:
        return None

    df = pd.concat(monthlies, axis=1, join="inner").dropna()
    if df.empty or len(df) < 6:
        return None
    tickers = list(monthlies.keys())
    used_w_total = sum(weights[t] for t in tickers)
    if used_w_total <= 0:
        return None
    w_vec = np.array([weights[t] / used_w_total for t in tickers])

    mean_vec = df[tickers].mean().values
    cov = df[tickers].cov().values
    # A single-holding portfolio (or a degenerate all-zero-variance
    # covariance) still needs to run - np.random.multivariate_normal
    # tolerates a positive-semidefinite (not just positive-definite)
    # covariance matrix directly, so no special-casing needed here.
    rng = np.random.default_rng(seed)
    try:
        draws = rng.multivariate_normal(mean_vec, cov, size=(n_paths, horizon_months))
    except Exception:
        return None
    # draws shape: (n_paths, horizon_months, n_tickers) of simulated
    # monthly returns per holding; compound each holding's own monthly
    # draws across the horizon, then combine by weight.
    holding_growth = np.prod(1.0 + draws, axis=1)  # (n_paths, n_tickers)
    port_growth = holding_growth @ w_vec            # (n_paths,)
    port_return_pct = (port_growth - 1.0) * 100.0
    # Amendment (6 Sep) to Parts 3/13/15: the always-open Monte Carlo
    # section needs "the median tick" alongside the 5th/95th markers -
    # p50 added here (purely additive - existing p5/p95/value keys are
    # unchanged) so a result cached before this change still has
    # everything the caller needs; only the median tick is new, and the
    # renderer treats it as optional (falls back to p5/p95-only) for any
    # such pre-existing cached blob.
    p5, p50, p95 = np.percentile(port_return_pct, [5, 50, 95])
    # Owner review round fix #4 (6 Sep): the approved montecarlo_mock.html
    # shows a mini histogram of "where the 5,000 simulated years landed"
    # under the band. Purely additive, same pattern as the p50 addition
    # just above - bins the SAME port_return_pct array this function
    # already produced (no new simulation, no new randomness), so a
    # result cached before this change simply lacks "hist_counts" and
    # the renderer falls back to no histogram for that cached blob.
    _hist_counts, _hist_edges = np.histogram(port_return_pct, bins=15)
    return {
        "p5_pct": float(p5), "p95_pct": float(p95), "p50_pct": float(p50),
        "p5_value_aud": (float(p5) / 100.0) * total_value_aud if total_value_aud else None,
        "p95_value_aud": (float(p95) / 100.0) * total_value_aud if total_value_aud else None,
        "p50_value_aud": (float(p50) / 100.0) * total_value_aud if total_value_aud else None,
        "n_holdings_used": len(tickers),
        "hist_counts": [int(c) for c in _hist_counts],
        "hist_bin_edges": [float(e) for e in _hist_edges],
    }


# -----------------------------------------------------------------
# Result cache - keyed by (sorted holdings+weights) so the whole,
# fairly expensive computation above reruns only when holdings/weights
# actually change (or the cache's own TTL lapses, standing in for "or
# data refreshes" - see module docstring / the instruction's own
# wording, since this site has no explicit "nightly data refresh"
# event to hook into).
# -----------------------------------------------------------------

def cache_key(holdings):
    """Stable hash of this portfolio's (ticker, kind, shares, buy_price)
    - not just ticker+weight - so a changed share count or a newly
    added/removed holding invalidates the cache even though this
    function itself doesn't compute weights."""
    basis = sorted(
        (h.get("ticker"), h.get("kind"), round(float(h.get("shares") or 0), 6),
         round(float(h.get("buy_price") or 0), 6))
        for h in (holdings or [])
    )
    raw = json.dumps(basis, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def get_cached_result(key):
    with _conn() as conn:
        row = conn.execute(
            f"SELECT data_json, created_at FROM {_RESULT_CACHE_TABLE} WHERE cache_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    created_at = datetime.fromisoformat(row[1])
    if datetime.now(timezone.utc) - created_at > timedelta(hours=RESULT_TTL_HOURS):
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def set_cached_result(key, data):
    with _conn() as conn:
        conn.execute(
            f"INSERT OR REPLACE INTO {_RESULT_CACHE_TABLE} (cache_key, data_json, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(data), datetime.now(timezone.utc).isoformat()),
        )
