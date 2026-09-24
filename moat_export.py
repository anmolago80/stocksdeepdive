"""
moat_export.py

Commit 6 (24 Sep 2026, owner-reported): consolidated, read-only export of
all five Moat-project Admin Dashboard dry-run previews (Operating-income
audit, Pricing power level, Tangible capital, Sliding-scale scoring, plus
universe/quote-snapshot infrastructure stats) into ONE downloadable ZIP
bundle, so the numbers needed to finish Commits 3b/4b/5b don't have to be
copied out of four separate panels by hand.

Reporting only - no scoring path changes, no switch changes, nothing
user-facing. This module calls the SAME engine functions the four
existing dry-run panels already call (moat_engine.compute_moat_dry_run,
auto_compounder_engine.ebit_ttm()/ebit_year_rows(), moat_engine's own
_year_return_series() - reused directly the same way this codebase's own
precedent already reuses private-by-convention functions, e.g. server.py
calling api_v1._resolve_universe()), with the SAME force_*=False/True
before/after pairs those panels already use, on cached bundles only
(fundamentals_data.peek_cached_bundle() - never a live fetch, see
build_export_bundle()'s own docstring). It never reads or writes the 24h
moat_cache, never mutates a Railway env var, and never touches
score_history/scan_store/quote_snapshot_store beyond the read-only
listing calls those modules already expose. See tests/test_moat_export.py
for the inertness assertion this module's own contract requires: running
build_export_bundle() with all three Moat switches unset changes no
cached score and no rendered output anywhere else on the site.

Commit 6-1/6-2 (24 Sep 2026, owner-reported): every section reports its
own coverage - tickers in the universe, tickers usable, tickers excluded
and why (a distribution computed over an availability-biased subset,
e.g. cached data skewing toward large/liquid names, is a silent
calibration risk) - and summary.json carries provenance (universe
name/size, export timestamp, the oldest/newest cached-bundle fetch date
actually used, all three switch states, MOAT_ENGINE_VERSION, and the
git SHA if reachable) so a calibration decision made off this file days
or weeks later can be traced back to exactly what produced it.
"""

import csv
import io
import json
import os
import subprocess
import zipfile
from datetime import datetime, timezone

import auto_compounder_engine
import fundamentals_data
import moat_engine
import quote_snapshot_store
import scan_store
import scanner_engine
import nightly_scan
import scheduler_engine

EXPORT_VERSION = 2


class NightlyScanWindowActive(Exception):
    """Raised by build_export_bundle() when the nightly scan is running
    or within its own scheduled catch-up window - refuses to run rather
    than add load competing for the same cached-bundle reads during the
    window CLAUDE.md's "deploy straight to production" care exists to
    protect."""


def _candidate_universes():
    return (
        list(scanner_engine.AUSTRALIA_UNIVERSES) + list(scanner_engine.USA_UNIVERSES)
        + [nightly_scan.IMPORTED_UNIVERSE]
    )


def saved_universe_sizes():
    """{universe: ticker_count} for every universe with a saved scan on
    disk - the same scan_store.load_scan_raw()/len(rows) pattern the
    Operating-income audit panel already uses. Used both to build
    Section 5 of the export AND (by the Admin Dashboard caller) to pick
    a sensible default universe - the smallest saved one, since the
    sliding-scale section is the expensive part of this export."""
    out = {}
    for uni in sorted(set(_candidate_universes())):
        if not os.path.exists(scan_store._path(uni)):
            continue
        try:
            payload = scan_store.load_scan_raw(uni)
        except Exception:
            continue
        out[uni] = len(payload.get("rows", [])) if payload else 0
    return out


def _last_completed_quote_snapshot_day():
    """(snap_date, count) for the most recent day in quote_snapshot_
    store.rows_per_day() that is strictly before today (UTC) - "last
    COMPLETED scan day", since today's own recording, if any, may still
    be in progress when this export runs. (None, 0) if no prior day has
    any rows on file."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rows = quote_snapshot_store.rows_per_day(days=14)
    except Exception:
        return None, 0
    for row in rows:  # already most-recent-first
        if row.get("snap_date") != today:
            return row.get("snap_date"), row.get("count", 0)
    return None, 0


def _git_sha():
    """The repo's current commit SHA, or None if unreachable (a
    container build that strips .git, no git binary, etc.) - provenance
    only, never blocks the export."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def _bundle_date_range(bundles):
    """(oldest, newest) fetched_at ISO-8601 string across every cached
    bundle actually used this run, read from each bundle's own "meta"
    dict (fundamentals_data.py's own per-ticker cache timestamp) - lets
    a calibration decision made off this export be checked against how
    stale the underlying fundamentals were. (None, None) if no bundle
    carries the field."""
    dates = [
        (b.get("meta") or {}).get("fetched_at")
        for b in bundles.values()
        if (b.get("meta") or {}).get("fetched_at")
    ]
    return (min(dates), max(dates)) if dates else (None, None)


def _pillar_points(result, name):
    for c in (result or {}).get("components") or []:
        if c.get("pillar") == name:
            return c.get("points")
    return None


def _country(ticker):
    return "AU" if ticker.upper().endswith(".AX") else "US"


def _coverage(total, rows, excluded):
    """Shared coverage summary: how much of the universe actually fed
    this section's numbers, and why the rest didn't - Commit 6-1's own
    requirement, since a distribution computed over an availability-
    biased subset (cached-data coverage tracks size/liquidity, not
    randomness) is a silent calibration risk."""
    reason_counts = {}
    for e in excluded:
        reason_counts[e["reason"]] = reason_counts.get(e["reason"], 0) + 1
    return {
        "tickers_in_universe": total,
        "tickers_with_usable_data": len(rows),
        "tickers_excluded": len(excluded),
        "exclusion_reasons": reason_counts,
    }


def _by_country(unique_tickers, rows, excluded):
    return {
        "included": {
            "AU": sum(1 for r in rows if r["country"] == "AU"),
            "US": sum(1 for r in rows if r["country"] == "US"),
        },
        "excluded": {
            "AU": sum(1 for e in excluded if _country(e["ticker"]) == "AU"),
            "US": sum(1 for e in excluded if _country(e["ticker"]) == "US"),
        },
    }


# ---------------------------------------------------------------
# Section 1 - Operating-income audit (Commit 2's status classification,
# reused verbatim from the existing Admin Dashboard panel).
# ---------------------------------------------------------------

def _section_ebit_audit(unique_tickers, bundles, ticker_universes, log):
    log("Section 1/5: Operating-income audit")
    rows, excluded = [], []
    status_counts = {"corrected": 0, "unverified": 0, "unchanged": 0}
    for tk in unique_tickers:
        bundle = bundles.get(tk)
        if bundle is None:
            excluded.append({"ticker": tk, "section": "section1_ebit_audit", "reason": "no_cached_bundle"})
            continue
        info = bundle.get("info") or {}
        if moat_engine._is_fund(info):
            excluded.append({"ticker": tk, "section": "section1_ebit_audit", "reason": "fund_or_etf"})
            continue
        if moat_engine._is_financials(info):
            excluded.append({"ticker": tk, "section": "section1_ebit_audit", "reason": "financials"})
            continue
        live = moat_engine.compute_moat_dry_run(tk, force_switch=None, bundle=bundle)
        if not live or live.get("mode") == "na":
            excluded.append({"ticker": tk, "section": "section1_ebit_audit", "reason": "insufficient_statement_data"})
            continue
        try:
            q_rows = auto_compounder_engine.ebit_year_rows(bundle, False, force_switch=True)
            ticker_corrected = bool(q_rows) and next(iter(q_rows.values()))["ticker_corrected"]
            unverified_years = [y for y, r in q_rows.items() if r.get("year_status") == "unverified"]
        except Exception:
            ticker_corrected = False
            unverified_years = []
        status = "corrected" if ticker_corrected else ("unverified" if unverified_years else "unchanged")
        status_counts[status] += 1
        rows.append({
            "ticker": tk,
            "country": _country(tk),
            "universe": ", ".join(sorted(ticker_universes.get(tk, []))),
            "roic": live.get("ttm_return"),
            "moat": live.get("score"),
            "status": status,
        })
    coverage = _coverage(len(unique_tickers), rows, excluded)
    return rows, status_counts, coverage, excluded


# ---------------------------------------------------------------
# Section 2 - Pricing power level (Commit 3's own before/after pair).
# ---------------------------------------------------------------

def _percentiles(values):
    if not values:
        return {}
    s = sorted(values)
    n = len(s)

    def q(p):
        idx = p * (n - 1)
        lo, hi = int(idx), min(int(idx) + 1, n - 1)
        frac = idx - lo
        return s[lo] + (s[hi] - s[lo]) * frac

    return {"p10": q(0.10), "p25": q(0.25), "median": q(0.50), "p75": q(0.75), "p90": q(0.90), "n": n}


def _section_pricing_level(unique_tickers, bundles, ticker_universes, ticker_sector, log):
    log("Section 2/5: Pricing power level")
    rows, excluded = [], []
    medians_by_path = {"gross": [], "operating (fallback)": []}
    for tk in unique_tickers:
        bundle = bundles.get(tk)
        if bundle is None:
            excluded.append({"ticker": tk, "section": "section2_pricing_level", "reason": "no_cached_bundle"})
            continue
        info = bundle.get("info") or {}
        if moat_engine._is_fund(info):
            excluded.append({"ticker": tk, "section": "section2_pricing_level", "reason": "fund_or_etf"})
            continue
        old_detail, new_detail = {}, {}
        old = moat_engine.compute_moat_dry_run(tk, force_switch=None, bundle=bundle, force_pricing_level=False, pricing_detail=old_detail)
        new = moat_engine.compute_moat_dry_run(tk, force_switch=None, bundle=bundle, force_pricing_level=True, pricing_detail=new_detail)
        if not (old and new) or old.get("mode") == "na":
            excluded.append({"ticker": tk, "section": "section2_pricing_level", "reason": "insufficient_statement_data"})
            continue
        pp_old, pp_new = _pillar_points(old, "Pricing power"), _pillar_points(new, "Pricing power")
        if pp_old is None or pp_new is None:
            excluded.append({"ticker": tk, "section": "section2_pricing_level", "reason": "pricing_power_pillar_dropped"})
            continue
        path = "operating (fallback)" if new_detail.get("used_fallback") else "gross"
        delta = round(pp_new - pp_old, 1)
        direction = "up" if delta > 0.05 else ("down" if delta < -0.05 else "flat")
        level_median = new_detail.get("level_median")
        if level_median is not None:
            medians_by_path[path].append(level_median)
        moat_old, moat_new = old.get("score"), new.get("score")
        rows.append({
            "ticker": tk,
            "country": _country(tk),
            "universe": ", ".join(sorted(ticker_universes.get(tk, []))),
            "sector": ticker_sector.get(tk, ""),
            "path": path,
            "pricing_power_old": pp_old,
            "pricing_power_new": pp_new,
            "direction": direction,
            "level_median": level_median,
            "moat_old": moat_old,
            "moat_new": moat_new,
            "moat_delta": (round(moat_new - moat_old, 1) if (moat_old is not None and moat_new is not None) else None),
        })

    distribution_rows = [
        {"path": path, **_percentiles(values)}
        for path, values in medians_by_path.items()
    ]
    up_down_flat = {
        path: {
            d: sum(1 for r in rows if r["path"] == path and r["direction"] == d)
            for d in ("up", "down", "flat")
        }
        for path in medians_by_path
    }
    movable = [r for r in rows if r.get("moat_delta") is not None]
    movers = sorted(movable, key=lambda r: abs(r["moat_delta"]), reverse=True)[:10]

    coverage = _coverage(len(unique_tickers), rows, excluded)
    # Per-path coverage (Commit 6-1): only meaningful for INCLUDED
    # tickers - which path a ticker falls on is only known once the
    # pillar has actually computed, so an excluded ticker can't be
    # attributed to a path.
    coverage["included_by_path"] = {p: sum(1 for r in rows if r["path"] == p) for p in medians_by_path}
    return rows, distribution_rows, up_down_flat, movers, coverage, excluded


# ---------------------------------------------------------------
# Section 3 - Tangible capital (ROIC/ROTC/invested capital, read
# directly off moat_engine._year_return_series()'s own per-year "extra"
# dict - no scoring call needed, these fields are already computed
# unconditionally there regardless of MOAT_TANGIBLE_ROIC).
# ---------------------------------------------------------------

def _section_tangible_capital(unique_tickers, bundles, ticker_universes, log):
    log("Section 3/5: Tangible capital")
    rows, excluded = [], []
    rotc_unavailable = []  # included in `rows` (valid ROIC), but ROTC itself couldn't be formed
    for tk in unique_tickers:
        bundle = bundles.get(tk)
        if bundle is None:
            excluded.append({"ticker": tk, "section": "section3_tangible_capital", "reason": "no_cached_bundle"})
            continue
        info = bundle.get("info") or {}
        if moat_engine._is_fund(info):
            excluded.append({"ticker": tk, "section": "section3_tangible_capital", "reason": "fund_or_etf"})
            continue
        if moat_engine._is_financials(info):
            excluded.append({"ticker": tk, "section": "section3_tangible_capital", "reason": "financials"})
            continue
        try:
            return_series = moat_engine._year_return_series(bundle, info, False)
        except Exception:
            return_series = []
        if not return_series or return_series[0][1] is None:
            excluded.append({"ticker": tk, "section": "section3_tangible_capital", "reason": "insufficient_statement_data"})
            continue
        roic, extra = return_series[0][1], return_series[0][2]
        nopat, total_ic, rotc = extra.get("nopat"), extra.get("invested_capital"), extra.get("rotc")
        tangible_ic = (nopat / rotc) if (nopat is not None and rotc) else None
        ratio = (tangible_ic / total_ic) if (tangible_ic is not None and total_ic) else None
        exceeds_5pt = rotc is not None and (rotc - roic) >= 0.05
        if rotc is None:
            rotc_unavailable.append(tk)
        rows.append({
            "ticker": tk,
            "country": _country(tk),
            "universe": ", ".join(sorted(ticker_universes.get(tk, []))),
            "roic": roic,
            "rotc": rotc,
            "tangible_invested_capital": tangible_ic,
            "total_invested_capital": total_ic,
            "tangible_to_total_ratio": ratio,
            "exceeds_5pt": exceeds_5pt,
        })
    exceed_rows = [r for r in rows if r["exceeds_5pt"]]
    exceed_counts = {
        "AU": sum(1 for r in exceed_rows if r["country"] == "AU"),
        "US": sum(1 for r in exceed_rows if r["country"] == "US"),
    }
    coverage = _coverage(len(unique_tickers), rows, excluded)
    coverage["by_country"] = _by_country(unique_tickers, rows, excluded)
    # ROTC-specific availability (Commit 6-1's "missing goodwill row"
    # example): these tickers ARE in 03_tangible_capital.csv (their ROIC
    # is valid), rotc is just None for that row - NOT counted in
    # tickers_excluded above, tracked separately here so it's visible
    # without silently degrading the "usable data" count for a metric
    # (ROIC) that these tickers genuinely do have.
    coverage["tickers_with_usable_rotc"] = len(rows) - len(rotc_unavailable)
    coverage["rotc_unavailable_reasons"] = (
        {"goodwill_or_intangibles_row_missing_or_non_positive_tangible_base": len(rotc_unavailable)}
        if rotc_unavailable else {}
    )
    return rows, exceed_counts, coverage, excluded, rotc_unavailable


# ---------------------------------------------------------------
# Section 4 - Sliding-scale scoring (Commit 5's own before/after pair).
# ---------------------------------------------------------------

def _mean_median(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None, None
    s = sorted(vals)
    n = len(s)
    mean = sum(s) / n
    median = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return mean, median


def _band(score):
    if score is None:
        return "N/A"
    b = min(int(score // 10) * 10, 90)
    return f"{b}-{b + 9}"


def _section_sliding(unique_tickers, bundles, ticker_universes, log):
    log("Section 4/5: Sliding-scale scoring (the expensive one - per-year cost of capital)")
    rows, excluded = [], []
    for tk in unique_tickers:
        bundle = bundles.get(tk)
        if bundle is None:
            excluded.append({"ticker": tk, "section": "section4_sliding", "reason": "no_cached_bundle"})
            continue
        info = bundle.get("info") or {}
        if moat_engine._is_fund(info):
            excluded.append({"ticker": tk, "section": "section4_sliding", "reason": "fund_or_etf"})
            continue
        old = moat_engine.compute_moat_dry_run(tk, force_switch=None, bundle=bundle, force_sliding=False)
        new = moat_engine.compute_moat_dry_run(tk, force_switch=None, bundle=bundle, force_sliding=True)
        if not (old and new) or old.get("mode") == "na":
            excluded.append({"ticker": tk, "section": "section4_sliding", "reason": "insufficient_statement_data"})
            continue
        moat_old, moat_new = old.get("score"), new.get("score")
        spread_old, spread_new = _pillar_points(old, "Excess-return spread"), _pillar_points(new, "Excess-return spread")
        rows.append({
            "ticker": tk,
            "country": _country(tk),
            "universe": ", ".join(sorted(ticker_universes.get(tk, []))),
            "moat_old": moat_old, "moat_new": moat_new,
            "band_old": _band(moat_old), "band_new": _band(moat_new),
            "spread_old": spread_old, "spread_new": spread_new,
        })

    band_order = [f"{b}-{b + 9}" for b in range(0, 100, 10)] + ["N/A"]
    band_before = {b: sum(1 for r in rows if r["band_old"] == b) for b in band_order}
    band_after = {b: sum(1 for r in rows if r["band_new"] == b) for b in band_order}
    moat_mean_old, moat_median_old = _mean_median([r["moat_old"] for r in rows])
    moat_mean_new, moat_median_new = _mean_median([r["moat_new"] for r in rows])
    spread_mean_old, spread_median_old = _mean_median([r["spread_old"] for r in rows])
    spread_mean_new, spread_median_new = _mean_median([r["spread_new"] for r in rows])
    summary = {
        "moat_score": {
            "mean_before": moat_mean_old, "mean_after": moat_mean_new,
            "median_before": moat_median_old, "median_after": moat_median_new,
        },
        "spread_pillar_points": {
            "mean_before": spread_mean_old, "mean_after": spread_mean_new,
            "median_before": spread_median_old, "median_after": spread_median_new,
        },
    }
    coverage = _coverage(len(unique_tickers), rows, excluded)
    coverage["by_country"] = _by_country(unique_tickers, rows, excluded)
    return rows, band_before, band_after, summary, coverage, excluded


# ---------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------

def _write_csv(zf, arcname, rows):
    buf = io.StringIO()
    if rows:
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    zf.writestr(arcname, buf.getvalue())


def build_export_bundle(tickers_by_universe, ticker_sector=None, log=None):
    """tickers_by_universe: {universe_name: [ticker, ...]} - the
    universe(s) selected in the Admin Dashboard picker, same shape the
    other dry-run panels already build via scan_store.load_scan_raw().
    `ticker_sector`: optional {ticker: sector} map, read from the SAME
    saved-scan rows (no new data source) - Section 2's mover table
    reports it as a column only, no scoring logic reads it.
    `log`: optional callable(str) for progress messages ("Section N/5:
    ...") - Section 4 (sliding-scale) is the expensive one, callers
    should surface these to the owner.

    Raises NightlyScanWindowActive if the nightly scan is currently
    running or within its own scheduled catch-up window - refuses to
    run rather than add load during that window.

    Returns {"zip_bytes": bytes, "filename": str, "summary": dict} -
    the caller (the Admin Dashboard) hands zip_bytes straight to
    st.download_button; "summary" is the same dict written into the
    bundle's own summary.json, for an inline on-page preview.

    Never touches the 24h moat_cache, never mutates a Railway env var,
    never makes a live yfinance/EODHD call - every bundle is read via
    fundamentals_data.peek_cached_bundle() only, same contract every
    dry-run panel before it already follows. Purely additive/read-only:
    no scoring path is touched, and no switch is ever forced beyond a
    single in-memory call (see moat_engine.compute_moat_dry_run()'s own
    docstring for why a forced computation never goes near the live
    switch or moat_cache).

    Every section's own coverage (tickers in the universe, tickers with
    usable data, tickers excluded and why) is reported in summary.json
    (Commit 6-1) - a distribution computed over an availability-biased
    subset (cached-data coverage tracks size/liquidity, not randomness)
    is a silent calibration risk, so this is never left implicit. Every
    excluded ticker, across every section, is also listed with its
    reason in excluded_tickers.csv inside the ZIP, so exclusion
    clustering is checkable directly. summary.json also carries
    provenance (Commit 6-2): universe name/size, export timestamp, the
    oldest/newest cached-bundle fetch date actually used this run, all
    three switch states, MOAT_ENGINE_VERSION, and the git SHA (None if
    unreachable) - so a calibration decision made off this file days or
    weeks later can be traced back to exactly what produced it."""
    def _log(msg):
        if log:
            log(msg)

    if scheduler_engine.nightly_scan_window_active():
        raise NightlyScanWindowActive(
            "Refusing to run: the nightly scan is currently running or within its "
            "scheduled catch-up window. Try again outside that window."
        )

    ticker_sector = ticker_sector or {}
    ticker_universes = {}
    for uni, tix in tickers_by_universe.items():
        for tk in tix:
            ticker_universes.setdefault(tk, set()).add(uni)

    unique_tickers = sorted(ticker_universes.keys())
    _log(f"Fetching {len(unique_tickers)} cached bundle(s) (cache-only, never live)...")
    bundles, skipped_no_cache = {}, []
    for tk in unique_tickers:
        bundle = fundamentals_data.peek_cached_bundle(tk)
        if bundle is None:
            skipped_no_cache.append(tk)
        else:
            bundles[tk] = bundle

    ebit_rows, ebit_status_counts, ebit_coverage, ebit_excluded = _section_ebit_audit(
        unique_tickers, bundles, ticker_universes, _log)
    (pricing_rows, pricing_distribution, pricing_updownflat, pricing_movers,
     pricing_coverage, pricing_excluded) = _section_pricing_level(
        unique_tickers, bundles, ticker_universes, ticker_sector, _log)
    (tangible_rows, tangible_exceed_counts, tangible_coverage, tangible_excluded,
     tangible_rotc_unavailable) = _section_tangible_capital(unique_tickers, bundles, ticker_universes, _log)
    (sliding_rows, sliding_band_before, sliding_band_after, sliding_summary,
     sliding_coverage, sliding_excluded) = _section_sliding(unique_tickers, bundles, ticker_universes, _log)

    _log("Section 5/5: Universe sizes and quote-snapshot health")
    universe_sizes = saved_universe_sizes()
    last_snapshot_day, last_snapshot_count = _last_completed_quote_snapshot_day()
    universe_size_rows = [{"universe": u, "tickers": n} for u, n in sorted(universe_sizes.items())]

    bundle_oldest, bundle_newest = _bundle_date_range(bundles)
    all_excluded = ebit_excluded + pricing_excluded + tangible_excluded + sliding_excluded

    summary = {
        "export_version": EXPORT_VERSION,
        # --- Commit 6-2: provenance ---
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "universes_selected": {u: len(tix) for u, tix in sorted(tickers_by_universe.items())},
        "tickers_analyzed": len(bundles),
        "tickers_skipped_no_cache": len(skipped_no_cache),
        "cached_bundle_fetched_at_range": {"oldest": bundle_oldest, "newest": bundle_newest},
        "switches": {
            "MOAT_PRICING_LEVEL": moat_engine.MOAT_PRICING_LEVEL,
            "MOAT_TANGIBLE_ROIC": moat_engine.MOAT_TANGIBLE_ROIC,
            "MOAT_SLIDING": moat_engine.MOAT_SLIDING,
        },
        "moat_engine_version": moat_engine.MOAT_ENGINE_VERSION,
        "git_sha": _git_sha(),
        # --- Commit 6-1: per-section coverage ---
        "section1_ebit_audit": {"status_counts": ebit_status_counts, "coverage": ebit_coverage},
        "section2_pricing_level": {
            "distribution_by_path": pricing_distribution,
            "up_down_flat_by_path": pricing_updownflat,
            "coverage": pricing_coverage,
        },
        "section3_tangible_capital": {"exceeds_5pt_counts": tangible_exceed_counts, "coverage": tangible_coverage},
        "section4_sliding": {
            "band_distribution": {"before": sliding_band_before, "after": sliding_band_after},
            "moat_and_spread_mean_median": sliding_summary,
            "coverage": sliding_coverage,
        },
        "section5_infrastructure": {
            "universe_sizes": universe_sizes,
            "last_completed_quote_snapshot_day": last_snapshot_day,
            "last_completed_quote_snapshot_count": last_snapshot_count,
        },
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.json", json.dumps(summary, indent=2, default=str))
        _write_csv(zf, "01_operating_income_audit.csv", ebit_rows)
        _write_csv(zf, "02_pricing_power_level.csv", pricing_rows)
        _write_csv(zf, "02_pricing_power_level_distribution.csv", pricing_distribution)
        _write_csv(zf, "02_pricing_power_level_movers.csv", pricing_movers)
        _write_csv(zf, "03_tangible_capital.csv", tangible_rows)
        _write_csv(zf, "04_sliding_scale.csv", sliding_rows)
        _write_csv(zf, "05_universe_sizes.csv", universe_size_rows)
        if skipped_no_cache:
            _write_csv(zf, "skipped_no_cache.csv", [{"ticker": t} for t in skipped_no_cache])
        if all_excluded:
            _write_csv(zf, "excluded_tickers.csv", all_excluded)

    filename = f"stocksdeepdive_moat_export_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.zip"
    return {"zip_bytes": buf.getvalue(), "filename": filename, "summary": summary}
