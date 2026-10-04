"""
financials_dry_run.py

Commit 5 of instruction_financials_income_store_and_top200_guard.md
(4 Oct 2026, Director-directed) - the owner's dry run: shadow values,
computed during the scan for every ticker that is financials-mode
under EITHER the current rule or the switch-ON rule, saved to a
SEPARATE file per universe on the volume
(/data/financials_dry_run/<universe slug>.json). This is what Andrew
approves from before the Director sets FINANCIALS_STORE_LIVE=1.

Never writes to a scan row, score_history, a snapshot or the Top 100
pool - nightly_scan.py's analyze_ticker_lite() computes the shadow row
via its own `shadow_out` out-param (same pattern as growth_summary_
out/oneoff_summary_out), kept entirely separate from the row dict it
returns and that scan_store.save_scan() persists.

Zero additional network calls (tested): compute_shadow_row() reads
financials_income_store.get() (a disk read, never a fetch) and makes
exactly one fcf_valuation_engine.dcf_intrinsic_value() call with the
SAME info/cashflow_df/currency/discount_rate/perpetual_rate/
growth_rate/manual_fcf the NOW valuation for this ticker already used
moments earlier in the same analyze_ticker_lite() call - any FX series
that call needs is already disk-cached from the NOW call for this
exact ticker/currency pair (see fundamentals_data._convert_statement_
currency()'s own once-per-day cache), so this adds no new live fetch.

The switch: this module answers "what WOULD the switch-ON code do"
without ever touching FINANCIALS_STORE_LIVE on Railway - _switch_on()
below sets the env var in THIS PROCESS only, for the duration of the
one shadow dcf_intrinsic_value() call, then restores it exactly as it
was. This is not a Railway variable edit (nothing persists, nothing
the Director or Andrew configured is touched) - it is the only way to
run the EXACT switch-ON dispatch code (financials_classifier.
is_financials() -> fcf_valuation_engine.normalized_base_and_series())
for the shadow answer without a second, hand-maintained copy of that
logic that could drift from the real one.
"""

import contextlib
import json
import os
import tempfile

import financials_classifier
import financials_income_store
import fcf_valuation_engine
from resolver_engine import dcf_looks_unreliable

_DRY_RUN_DIR_NAME = "financials_dry_run"


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


def _dry_run_dir():
    path = os.path.join(_data_dir(), _DRY_RUN_DIR_NAME)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def _safe_slug(universe):
    return "".join(c if (c.isalnum() or c in "._-") else "-" for c in (universe or "").strip())


def _path(universe):
    return os.path.join(_dry_run_dir(), f"{_safe_slug(universe)}.json")


def _atomic_write(path, obj):
    """Same atomic-write pattern as financials_income_store._atomic_
    write() - tmp file + os.replace() so a crash mid-write never leaves
    a corrupt dry-run file behind. Best-effort: a write failure just
    means this universe's dry run stays exactly as it was."""
    try:
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
        os.replace(tmp_path, path)
    except OSError:
        pass


def save(universe, rows):
    """Overwrites this universe's dry-run file with `rows` (a plain
    list of compute_shadow_row() dicts) - called once per universe,
    after that universe's scan loop finishes, regardless of whether
    the real scan row save goes on to succeed (this file is purely
    diagnostic, owner-only, never read by any public/scoring/selection
    path, so it's written for every scan attempt)."""
    _atomic_write(_path(universe), {"universe": universe, "rows": rows or []})


def load(universe):
    """Returns {"universe", "rows"} for this universe, or None if no
    dry run has ever been saved for it. Never raises."""
    path = _path(universe)
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def list_universes():
    """Every universe with a saved dry-run file, sorted - for the
    Admin page's own universe filter dropdown. Reads each file's own
    "universe" field (the real name, e.g. "S&P 500") rather than the
    on-disk filename (a sanitised slug, e.g. "S-P-500") - save()/
    load() are keyed by the slug, but callers always see real names."""
    try:
        files = [f for f in os.listdir(_dry_run_dir()) if f.endswith(".json")]
    except OSError:
        files = []
    names = []
    for f in files:
        try:
            with open(os.path.join(_dry_run_dir(), f)) as fh:
                payload = json.load(fh)
            name = payload.get("universe")
            if name:
                names.append(name)
        except Exception:
            continue
    return sorted(names)


@contextlib.contextmanager
def _switch_on():
    """Forces FINANCIALS_STORE_LIVE=1 in THIS PROCESS for the duration
    of the one shadow dcf_intrinsic_value() call below, then restores
    whatever the variable actually was (unset, or any other value) -
    see this module's own docstring for why this is not a Railway
    variable edit. Never leaves the override in place past the `with`
    block, even if the call inside raises."""
    prev = os.environ.get("FINANCIALS_STORE_LIVE")
    os.environ["FINANCIALS_STORE_LIVE"] = "1"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("FINANCIALS_STORE_LIVE", None)
        else:
            os.environ["FINANCIALS_STORE_LIVE"] = prev


def should_compute(info, ticker):
    """True when this ticker is financials-mode under EITHER the
    CURRENT rule (is_financials(), honouring the live switch as it
    actually is) or the switch-ON rule (is_financials_shadow(), always
    as if the switch were on) - the instruction's own gate for which
    tickers get a shadow row at all. False for an ordinary standard-
    mode ticker under both rules - the overwhelming majority, skipped
    here before any dcf_intrinsic_value() call is even considered."""
    current_mode = financials_classifier.is_financials(info, ticker=ticker)
    shadow_mode = financials_classifier.is_financials_shadow(info, ticker=ticker)
    return current_mode or shadow_mode


def compute_shadow_row(ticker, company_name, price, info, cashflow_df, currency,
                        now_intrinsic_value, now_mos_pct, now_fcf_source,
                        now_dcf_unreliable, now_moat_mode, now_quality,
                        discount_rate=None, perpetual_rate=None, growth_rate=None,
                        manual_fcf=None):
    """The per-ticker shadow row - see this module's own docstring for
    the zero-new-network-call guarantee and the switch-forcing
    mechanism. `now_*` are this ticker's ALREADY-COMPUTED NOW values
    (free - analyze_ticker_lite() has them in scope by the time it
    calls this), carried through unchanged so the dry-run row shows
    NOW and SHADOW side by side without a second NOW computation.

    status: "awaiting_income_fetch" (no store entry yet, financials-
    mode under the switch-ON rule), "lt2_positive_years" (a store
    entry exists but normalized_base_and_series() still fell back -
    fewer than two positive net-income years), "fetch_failed" (no
    store entry, and financials_income_store's own Deep Dive attempt
    marker's last recorded try failed - this can only ever reflect a
    Deep Dive on-view attempt, never the nightly pre-pass's own per-
    run failures, which aren't persisted per-ticker; see the report's
    own note on this), or "ok" (standard mode under the switch-ON
    rule, or financials-mode with a usable net-income series).

    shadow_quality is always the literal string "changes" - per the
    instruction's own "quality if it can be computed without a new
    network call, otherwise mark it 'changes' and say so": a true
    recomputed quality score needs moat_engine's own ROE-vs-ROIC
    bundle-backed run, which this function deliberately does not
    attempt (that would cost a new fetch on a cold moat_engine cache,
    violating the zero-new-network-call guarantee)."""
    entry = financials_income_store.get(ticker)
    shadow_income_df = entry["income"] if entry is not None else None

    with _switch_on():
        shadow_iv, _shadow_growth, shadow_meta = fcf_valuation_engine.dcf_intrinsic_value(
            ticker, info=info, cashflow_df=cashflow_df, currency=currency,
            discount_rate=discount_rate, perpetual_rate=perpetual_rate,
            growth_rate=growth_rate, manual_fcf=manual_fcf, income_df=shadow_income_df,
        )

    shadow_mode = bool(shadow_meta.get("is_financials_mode"))
    shadow_fcf_source = shadow_meta.get("fcf_source")
    shadow_mos_pct = (
        round(((shadow_iv - price) / shadow_iv) * 100, 1)
        if shadow_iv and shadow_iv > 0 else None
    )
    shadow_dcf_unreliable = dcf_looks_unreliable(shadow_iv, price)

    if not shadow_mode:
        status = "ok"
    elif entry is None:
        attempt = financials_income_store.last_deep_dive_attempt(ticker)
        status = "fetch_failed" if (attempt and attempt.get("success") is False) else "awaiting_income_fetch"
    elif shadow_fcf_source == "ocf_fallback_financials":
        status = "lt2_positive_years"
    else:
        status = "ok"

    return {
        "ticker": ticker,
        "company": company_name,
        "price": price,
        "now_intrinsic_value": now_intrinsic_value,
        "now_mos_pct": now_mos_pct,
        "now_fcf_source": now_fcf_source,
        "now_dcf_unreliable": bool(now_dcf_unreliable),
        "now_moat_mode": now_moat_mode,
        "now_quality": now_quality,
        "shadow_mode": "financials" if shadow_mode else "standard",
        "shadow_intrinsic_value": round(shadow_iv, 2) if shadow_iv and shadow_iv > 0 else None,
        "shadow_mos_pct": shadow_mos_pct,
        "shadow_fcf_source": shadow_fcf_source,
        "shadow_dcf_unreliable": bool(shadow_dcf_unreliable),
        "shadow_moat_mode": "financials" if shadow_mode else "standard",
        "shadow_quality": "changes",
        "status": status,
        "pool_ineligible_if_switch_on": shadow_fcf_source == "ocf_fallback_financials",
        "in_current_top100": False,
    }
