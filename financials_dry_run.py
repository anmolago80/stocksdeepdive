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
import fundamentals_data
import moat_engine
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


def _ni_diagnostics(entry):
    """Addendum 2 item 7 (5 Oct 2026, Director-directed follow-up) -
    "ni_row_label" (the income-statement row label fcf_valuation_
    engine._row_with_label() actually matched against _NET_INCOME_
    LABELS) and "ni_values" (that row's own values, most-recent-first,
    paired with their period-end column labels) for the store entry's
    income table. (None, None) when there's no entry, or its income
    table has no matching row at all - makes a future substring-
    fallback mismatch (see _row_with_label()'s own docstring) visible
    on the dry run itself rather than only inferable from its effect
    on the valuation."""
    if entry is None:
        return None, None
    income_df = entry.get("income")
    if income_df is None or getattr(income_df, "empty", True):
        return None, None
    label, values = fcf_valuation_engine._row_with_label(
        income_df, fcf_valuation_engine._NET_INCOME_LABELS,
    )
    if label is None:
        return None, None
    periods = [str(c) for c in income_df.columns]
    paired = list(zip(periods, values)) if values is not None else []
    return label, paired


def _moat_scores(ticker, bundle):
    """now_moat/shadow_moat (Addendum 2 item 7, 5 Oct 2026, Director-
    directed follow-up) - the Moat score before and after the switch,
    computed from `bundle` ONLY (never fetches its own - callers pass
    fundamentals_data.peek_cached_bundle()'s own cache-only result, so
    this is None/None whenever nothing is cached yet for this ticker,
    never a new network call). Both go through moat_engine.compute_
    moat_dry_run() - the SAME real Moat dispatch (ROE-vs-ROIC
    substitution for an overridden/financials-mode ticker) the live
    "Moat" column uses, under _switch_on() for the shadow read, so
    there is no second, hand-maintained copy of that logic to drift
    from the real one - same reasoning as this module's own DCF
    shadow value. Returns (None, None) when `bundle` itself is None,
    or when compute_moat_dry_run() can't score this ticker at all
    (e.g. a fund, or fewer than 2 usable statement years) - "n/a" is
    for the caller to render, never an estimate here."""
    if bundle is None:
        return None, None
    now_result = moat_engine.compute_moat_dry_run(ticker, force_switch=None, bundle=bundle)
    with _switch_on():
        shadow_result = moat_engine.compute_moat_dry_run(ticker, force_switch=None, bundle=bundle)
    now_moat = now_result.get("score") if now_result else None
    shadow_moat = shadow_result.get("score") if shadow_result else None
    return now_moat, shadow_moat


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
    fewer than two positive net-income years), "ni_path_abandoned"
    (Addendum 2 item 7, 5 Oct 2026: the net-income path produced a
    non-positive base - most often because the latest year's net
    income is negative, though a store entry with no usable OCF
    either can reach it too - see fcf_source "info" below), "fetch_
    failed" (no store entry, and financials_income_store's own Deep
    Dive attempt marker's last recorded try failed - this can only
    ever reflect a Deep Dive on-view attempt, never the nightly pre-
    pass's own per-run failures, which aren't persisted per-ticker;
    see the report's own note on this), or "ok" (standard mode under
    the switch-ON rule, or financials-mode with a usable net-income
    series).

    now_moat/shadow_moat (Addendum 2 item 7) replace the old literal-
    "changes" shadow_quality column: per that follow-up's own finding
    (grep-verified, see its report), Quality and Long Score never read
    financials_classifier at all - only Moat does - so Moat, not
    Quality, is the real before/after this dry run needs to show. See
    _moat_scores()'s own docstring for the zero-new-network-call
    contract; either is None (displayed "n/a") when nothing is cached
    yet for this ticker, never an estimate."""
    entry = financials_income_store.get(ticker)
    shadow_income_df = entry["income"] if entry is not None else None
    # Addendum 2 item 1 (5 Oct 2026, Director-directed): the store
    # entry's OWN currency_converted flag, not an assumption - a Way-3
    # direct fetch with no cached bundle to resolve currency from saves
    # UNCONVERTED (currency_converted=False, see financials_income_
    # store._save_direct_fetch()'s own docstring), so this must be read
    # per-entry, never hardcoded True just because it came from the
    # store.
    _income_df_currency_converted = bool(entry.get("currency_converted")) if entry is not None else False

    with _switch_on():
        shadow_iv, _shadow_growth, shadow_meta = fcf_valuation_engine.dcf_intrinsic_value(
            ticker, info=info, cashflow_df=cashflow_df, currency=currency,
            discount_rate=discount_rate, perpetual_rate=perpetual_rate,
            growth_rate=growth_rate, manual_fcf=manual_fcf, income_df=shadow_income_df,
            income_df_currency_converted=_income_df_currency_converted,
        )

    shadow_mode = bool(shadow_meta.get("is_financials_mode"))
    shadow_fcf_source = shadow_meta.get("fcf_source")
    shadow_mos_pct = (
        round(((shadow_iv - price) / shadow_iv) * 100, 1)
        if shadow_iv and shadow_iv > 0 else None
    )
    shadow_dcf_unreliable = dcf_looks_unreliable(shadow_iv, price)

    # Addendum 2 item 7 (5 Oct 2026, Director-directed follow-up,
    # item 2 - the IVZ row): a financials-mode ticker whose net-income
    # path produced a non-positive base falls through, inside dcf_
    # intrinsic_value() itself, to info["freeCashflow"] - the SAME
    # fallback a standard-mode ticker with no usable OCF/capex data
    # uses, tagged fcf_source "info". Status "ok" is misleading for
    # such a row (its base is not the net-income figure this whole
    # dry run exists to show) - shadow_reason carries why, display
    # only, never fed back into the DCF itself.
    shadow_reason = None
    if not shadow_mode:
        status = "ok"
    elif entry is None:
        attempt = financials_income_store.last_deep_dive_attempt(ticker)
        status = "fetch_failed" if (attempt and attempt.get("success") is False) else "awaiting_income_fetch"
    elif shadow_fcf_source == "ocf_fallback_financials":
        status = "lt2_positive_years"
    elif shadow_fcf_source in ("info", "none"):
        # B1 (5 Oct 2026, Director-directed, instruction_top200_blank_
        # replies_and_financials_gap.md, "your out-of-scope item"):
        # ni_path_abandoned now also covers fcf_source "none" - the net
        # income path was abandoned AND info["freeCashflow"] itself was
        # non-positive/unavailable, so the DCF has no usable base at
        # all (dcf_intrinsic_value() returns 0/None for this ticker).
        status = "ni_path_abandoned"
        if shadow_fcf_source == "none":
            shadow_reason = (
                "net income path never produced a usable base, and "
                "info[\"freeCashflow\"] was also non-positive or "
                "unavailable - DCF abandoned entirely"
            )
        elif shadow_meta.get("fcf_reason") == "negative_normalised_fcf":
            shadow_reason = (
                "net income path abandoned: latest year's net income was "
                "negative; fell through to info[\"freeCashflow\"]"
            )
        else:
            shadow_reason = (
                "net income path never produced a usable base (no positive "
                "net income AND no usable operating cash flow); fell "
                "through to info[\"freeCashflow\"]"
            )
    else:
        status = "ok"

    ni_row_label, ni_values = _ni_diagnostics(entry)
    bundle = fundamentals_data.peek_cached_bundle(ticker)
    now_moat, shadow_moat = _moat_scores(ticker, bundle)

    return {
        "ticker": ticker,
        "company": company_name,
        "price": price,
        # Addendum 2 item 1 (5 Oct 2026, Director-directed): same
        # fin_ccy/listing_ccy derivation fcf_valuation_engine.
        # dcf_intrinsic_value() itself uses, so these columns show
        # exactly what that function would compare.
        "reporting_currency": (info.get("financialCurrency") or currency or "").upper() or None,
        "listing_currency": (currency or info.get("currency") or "").upper() or None,
        # Addendum 2 item 6 (5 Oct 2026, Director-directed): the store
        # entry's own latest period (derived straight from its income
        # table, same helper the staleness check itself now uses - see
        # financials_income_store.latest_statement_period()'s own
        # docstring) and which of the three ways filled it. None/None
        # when there's no entry at all (status "awaiting_income_fetch"
        # or "fetch_failed").
        "store_latest_period": (
            financials_income_store.latest_statement_period(entry.get("income"))
            if entry is not None else None
        ),
        "store_source": entry.get("source") if entry is not None else None,
        # Addendum 2 item 7, item 1 (the ARES row) - see _ni_diagnostics()'s
        # own docstring.
        "ni_row_label": ni_row_label,
        "ni_values": ni_values,
        "now_intrinsic_value": now_intrinsic_value,
        "now_mos_pct": now_mos_pct,
        "now_fcf_source": now_fcf_source,
        "now_dcf_unreliable": bool(now_dcf_unreliable),
        "now_moat_mode": now_moat_mode,
        "now_quality": now_quality,
        # Addendum 2 item 7, item 3 - replaces the old literal-"changes"
        # shadow_quality: Quality/Long Score never read financials_
        # classifier (grep-verified - see the report), Moat is the real
        # thing the override changes. See _moat_scores()'s own
        # docstring for the zero-new-network-call/None-means-n/a
        # contract.
        "now_moat": now_moat,
        "shadow_moat": shadow_moat,
        "shadow_mode": "financials" if shadow_mode else "standard",
        "shadow_intrinsic_value": round(shadow_iv, 2) if shadow_iv and shadow_iv > 0 else None,
        "shadow_mos_pct": shadow_mos_pct,
        "shadow_fcf_source": shadow_fcf_source,
        "shadow_dcf_unreliable": bool(shadow_dcf_unreliable),
        "shadow_moat_mode": "financials" if shadow_mode else "standard",
        "shadow_reason": shadow_reason,
        "status": status,
        # Addendum 2 item 7, item 3 - which rule decided is_financials_
        # shadow()'s answer for this ticker (see financials_classifier.
        # shadow_mode_reason()'s own docstring).
        "shadow_mode_reason": financials_classifier.shadow_mode_reason(info, ticker=ticker),
        # B1 (5 Oct 2026, Director-directed, instruction_top200_blank_
        # replies_and_financials_gap.md): extended beyond the literal
        # "ocf_fallback_financials" string to also cover "info" (IVZ's
        # own live example) and "none" - the three non-net-income FCF
        # sources top100_engine._financials_pool_ineligible() excludes
        # a financials-mode row for. Gated on shadow_mode explicitly
        # (not just the fcf_source string) since "info"/"none" are NOT
        # mode-exclusive - a standard-mode ticker reaching either via
        # the ordinary OCF-unavailable fallback must never read as
        # pool-ineligible here.
        "pool_ineligible_if_switch_on": (
            shadow_mode and shadow_fcf_source in ("ocf_fallback_financials", "info", "none")
        ),
        "in_current_top100": False,
    }
