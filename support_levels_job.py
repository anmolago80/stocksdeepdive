"""
support_levels_job.py

TASK 36 (Director-directed, owner-reported, 6 Nov 2026): the nightly
orchestration that builds support_levels_engine.compute_support_
levels()'s own inputs (weekly OHLC bars, pence-repaired for .L) for
every Top 200 pool member + ASX extension member - NOT every scanned
ticker - and writes the result to support_levels_store.py.

Bulk-download pattern mirrors nightly_scan._reprice_download_chunk()/
reprice_universe() exactly (same chunk size, same single-vs-multi-
ticker yf.download() column handling, same "[module] N tickers in M
chunk(s), Xm Ys" log-timing convention) - a NEW, separate function
rather than reusing/modifying that one directly (it is hardcoded to
period="6mo" for its own, different, nightly-reprice purpose; this
job needs period="5y" for pivot-low detection, which needs years of
history to find a real floor, not six months of it).

Pence-repair order (TASK 28/29/29b/29c): fundamentals_data.repair_
mixed_unit_price_dataframe() runs on the DAILY OHLC frame BEFORE
resampling to weekly - the ratio/neighbour-jump heuristics that
function uses need daily-granularity data to work correctly; resampling
first would corrupt the Low/High extremes across a pence/pounds unit
boundary. Uses the Top 200 pool's own already-known, already-correct
"price" field as the reference price (TASK 29's own "widen repair to
every .L ticker regardless of price_quote_unit" rule - applied here to
EVERY ".L" ticker unconditionally, no Yahoo info["currency"] check
needed at all, since repair_mixed_unit_price_dataframe() itself needs
only a DataFrame and a reference price, never a Yahoo info dict).

TASK 39/TASK 42 item 6b (Director-directed, 8 Nov 2026, "build
together", "reuse TASK 39's 3y-peak data"): the drop-from-3y-peak chip
(drop_chip_engine.py/drop_chip_store.py) is computed from the SAME
weekly bars this job already resamples (5y of history, comfortably
more than the 3-year window the drop chip needs) - no second download,
no second resample, same per-ticker try/except this job already has.
"""

import datetime
import time

import pandas as pd
import yfinance as yf

import drop_chip_engine
import drop_chip_store
import fundamentals_data
import support_levels_engine
import support_levels_store
import top100_store

SUPPORT_LEVELS_CHUNK_SIZE = 175  # tickers per yf.download() batch call - same as nightly_scan.REPRICE_CHUNK_SIZE
SUPPORT_LEVELS_HISTORY_PERIOD = "5y"
SUPPORT_LEVELS_MIN_WEEKLY_BARS = support_levels_engine.PIVOT_LEFT_BARS + support_levels_engine.PIVOT_RIGHT_BARS + 1


def _pool_and_extension_tickers_with_currency(log=print):
    """{ticker: {"currency": str|None, "price": float|None}} for every
    Top 200 pool member PLUS ASX extension member - top100_store.
    current_pool()/current_asx_extension(), the exact same two
    functions TASK 35's own moat-blend shadow preview reads, never a
    third independently-resolved ticker list. Deduped by ticker (a
    ticker could in principle appear in both, though current_asx_
    extension() is documented as disjoint from the pool itself)."""
    out = {}
    for row in top100_store.current_pool() + top100_store.current_asx_extension():
        t = row.get("ticker")
        if not t:
            continue
        out[t] = {"currency": row.get("currency"), "price": row.get("price")}
    return out


def _download_chunk(tickers):
    """One yf.download() call for a chunk of tickers (~175 - see
    SUPPORT_LEVELS_CHUNK_SIZE), returning {ticker: DataFrame} - the
    SAME batch-download shape/column-handling as nightly_scan._reprice_
    download_chunk(), just period="5y" instead of "6mo". Never raises -
    a chunk that fails outright just yields no history for any ticker
    in it; the caller treats that exactly like any other per-ticker
    failure (no chip for that ticker tonight, never a crash)."""
    out = {}
    try:
        data = yf.download(
            list(tickers), period=SUPPORT_LEVELS_HISTORY_PERIOD, progress=False,
            group_by="ticker", threads=True, auto_adjust=True,
        )
    except Exception:
        return out
    if data is None or len(data) == 0:
        return out
    single = len(tickers) == 1
    for t in tickers:
        try:
            out[t] = data if single else data[t]
        except Exception:
            pass
    return out


def _weekly_bars_from_daily(daily_df, ticker, reference_price, log=print):
    """Pence-repairs (every ".L" ticker, unconditionally - TASK 29's
    own rule) then resamples a daily OHLC DataFrame to weekly bars,
    returning (dates, highs, lows, closes) as parallel oldest-first
    lists - support_levels_engine.compute_support_levels()'s own
    required input shape. None/empty in -> ([], [], [], []) out, never
    raises.

    Weekly resample: pandas' own `W` (week-ending-Sunday) anchor -
    Open=first/High=max/Low=min/Close=last within each calendar week,
    the standard OHLC aggregation every charting package (including
    the owner's own TradingView) uses. Any week with no trading days
    at all (a public holiday week, or the very first/last partial
    week) is dropped (`dropna()`) rather than left as a NaN bar that
    would otherwise corrupt the pivot-low min() comparisons."""
    if daily_df is None or getattr(daily_df, "empty", True) or "Close" not in daily_df.columns:
        return [], [], [], []
    repaired = daily_df
    if isinstance(ticker, str) and ticker.upper().endswith(".L"):
        repaired = fundamentals_data.repair_mixed_unit_price_dataframe(
            daily_df, reference_price, ticker=ticker, log=log,
        )
    try:
        weekly = repaired.resample("W").agg({
            "Open": "first", "High": "max", "Low": "min", "Close": "last",
        }).dropna(subset=["Close"])
    except Exception:
        return [], [], [], []
    if weekly.empty:
        return [], [], [], []
    dates = [d.date() if hasattr(d, "date") else d for d in weekly.index.to_pydatetime()]
    return dates, weekly["High"].tolist(), weekly["Low"].tolist(), weekly["Close"].tolist()


def compute_and_store_support_levels_for_pool(log=print):
    """TASK 36's own nightly entry point - called once per night, AFTER
    tonight's Top 200 selection has already run (scheduler_engine.
    _run_top100() calls this right after top100_engine.run_nightly()
    returns, so top100_store.current_pool()/current_asx_extension()
    are both fresh for tonight). Read-only against the pool itself
    (never writes to top100_pool/top100_store) - only writes to this
    task's own support_levels_store.py. Logs request count/timing in
    the SAME "[module] N tickers in M chunk(s), Xm Ys" convention
    nightly_scan.reprice_universe() uses, so this job's own cost is
    visible in the nightly log the same way every other bulk-download
    pass already is.

    Returns {"tickers": int, "chunks": int, "computed": int, "failed": int,
    "elapsed_seconds": float} - a plain summary dict, for callers (the
    scheduler, or a test) that want the numbers without re-parsing the
    log line."""
    start = time.time()
    by_ticker = _pool_and_extension_tickers_with_currency(log=log)
    tickers = sorted(by_ticker.keys())
    computed = 0
    failed = 0
    n_chunks = 0
    for i in range(0, len(tickers), SUPPORT_LEVELS_CHUNK_SIZE):
        chunk = tickers[i:i + SUPPORT_LEVELS_CHUNK_SIZE]
        n_chunks += 1
        try:
            hist_by_ticker = _download_chunk(chunk)
        except Exception as e:
            log(f"[support_levels] chunk {n_chunks} download failed: {e}")
            hist_by_ticker = {}
        for t in chunk:
            try:
                daily_df = hist_by_ticker.get(t)
                reference_price = by_ticker[t]["price"]
                dates, highs, lows, closes = _weekly_bars_from_daily(
                    daily_df, t, reference_price, log=log,
                )
                if len(closes) < SUPPORT_LEVELS_MIN_WEEKLY_BARS:
                    failed += 1
                    # 7 Nov 2026 follow-up (Director-directed): the
                    # Director's own nightly log review found "1
                    # failed/skipped" with no way to tell WHICH ticker
                    # or why - this path previously had no per-ticker
                    # log line at all, only the counter above. Named
                    # here so a bad night's log always answers "which
                    # one, and how short" directly, no guessing.
                    log(f"[support_levels] {t}: skipped - only {len(closes)} weekly "
                        f"bar(s) available (need >= {SUPPORT_LEVELS_MIN_WEEKLY_BARS}) "
                        f"- likely no/insufficient price history returned for this "
                        f"ticker tonight (new listing, delisted, or a failed download)")
                    continue
                result = support_levels_engine.compute_support_levels(dates, highs, lows, closes)
                support_levels_store.save(t, result, currency=by_ticker[t]["currency"])
                # TASK 39/TASK 42 item 6b (Director-directed, built
                # together per the "reuse TASK 39's 3y-peak data"
                # instruction): the SAME weekly bars already resampled
                # above (5y of history, comfortably more than the
                # 3-year window this needs) feed the drop-from-peak
                # chip too - no second download, no second resample.
                drop_result = drop_chip_engine.compute_peak_and_drop(dates, closes)
                drop_chip_store.save(t, drop_result, currency=by_ticker[t]["currency"])
                computed += 1
            except Exception as e:
                log(f"[support_levels] {t}: {e}")
                failed += 1
        del hist_by_ticker

    elapsed = time.time() - start
    mins, secs = divmod(int(elapsed), 60)
    log(f"[support_levels] {len(tickers)} tickers in {n_chunks} chunk(s) of "
        f"<={SUPPORT_LEVELS_CHUNK_SIZE}, {computed} computed, {failed} failed/skipped, "
        f"in {mins}m {secs}s")

    # 7 Nov 2026 follow-up (Director-directed): one explicit line per
    # run for the Director's own named evidence tickers, so the real
    # support levels can be read straight from the Railway log without
    # needing DB/admin-panel access - same "named tickers, explicit
    # log line" precedent as top100_engine's own "[top100] quality
    # exception: ..." line. Reads back from support_levels_store (just
    # written above, this same run) rather than recomputing - a
    # missing entry (not currently a pool/extension member, or it
    # failed above) logs plainly as "no entry", never fabricated.
    for _named in ("OCL.AX", "CSL.AX", "VAP.AX", "IVV.AX"):
        _entry = support_levels_store.get(_named)
        if not _entry:
            log(f"[support_levels] {_named}: no entry (not a current pool/extension "
                f"member, or tonight's computation failed/skipped for it)")
        elif _entry.get("has_support"):
            log(f"[support_levels] {_named}: support={_entry.get('support_price')} "
                f"({_entry.get('support_date')}) next_support={_entry.get('next_support_price')} "
                f"({_entry.get('next_support_date')}) drop_from_12m_high="
                f"{_entry.get('drop_from_12m_high')}")
        else:
            log(f"[support_levels] {_named}: no support below - lowest_since="
                f"{_entry.get('lowest_since_price')} ({_entry.get('lowest_since_date')}) "
                f"drop_from_12m_high={_entry.get('drop_from_12m_high')}")

    # TASK 39's own test-first named tickers (CSL.AX/OCL.AX/NU/ADBE) -
    # same "named tickers, explicit log line" precedent as the support-
    # levels block just above, reading back from drop_chip_store (just
    # written above, this same run) rather than recomputing.
    for _named in ("CSL.AX", "OCL.AX", "NU", "ADBE"):
        _drop_entry = drop_chip_store.get(_named)
        if not _drop_entry:
            log(f"[drop_chip] {_named}: no entry (not a current pool/extension "
                f"member, or tonight's computation failed/skipped for it)")
        elif _drop_entry.get("chip_shown"):
            log(f"[drop_chip] {_named}: -{abs(_drop_entry.get('drop_pct') or 0):.1f}% "
                f"from {_drop_entry.get('peak_date')} peak "
                f"{_drop_entry.get('peak_price')} (current {_drop_entry.get('current_price')})")
        else:
            log(f"[drop_chip] {_named}: no chip (drop "
                f"{_drop_entry.get('drop_pct')}% is above the -20% threshold) - "
                f"peak {_drop_entry.get('peak_price')} on {_drop_entry.get('peak_date')}, "
                f"current {_drop_entry.get('current_price')}")

    return {
        "tickers": len(tickers), "chunks": n_chunks, "computed": computed,
        "failed": failed, "elapsed_seconds": elapsed,
    }
