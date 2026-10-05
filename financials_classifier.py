"""
financials_classifier.py

Shared "is this a financial-services company" classifier - the single
source of truth for moat_engine.py (ROE substitutes for ROIC, since a
bank/insurer's balance sheet IS the business) and fcf_valuation_
engine.py (net income substitutes for free cash flow, since operating
cash flow includes float/deposit flows that aren't shareholder cash -
owner decision, 2 Oct 2026, VERSION A). A single shared function, not
two copies, so the two valuation paths can never drift onto different
tickers.

Fable's principle (4 Oct 2026, Commit 3 of instruction_financials_
income_store_and_top200_guard.md): does customer, policyholder or
clearing money flow through operating cash flow? If yes, financials
mode. If the business is an asset-light fee earner, operating cash
flow is close to owner earnings and standard mode is better.

The switch, FINANCIALS_STORE_LIVE (is_financials_store_live() below):
with it OFF (the default - unset, empty, "0" or "off"), is_financials()
is byte-identical to its pre-Commit-3 behaviour regardless of `ticker`.
Only with it ON ("1" or "on") does the override table below apply. The
Director sets it on Railway after Andrew has read the dry run (Commit
5) - this module never reads or writes it anywhere else.

Known drift risk, noticed while wiring this (not fixed - out of this
instruction's stated scope): auto_compounder_engine.py has its OWN
separate copy of this decision, `_ac_is_financials()` ("mirrors
moat_engine._is_financials() exactly" - see that function's own
docstring), never routed through this module at all. The Compounder
View will keep making this decision via ITS OWN unmodified copy and
so will NOT see the override table below even once the switch is ON -
reported for the Director/Andrew to decide whether that gap matters.

Zero project imports here deliberately: moat_engine.py already imports
auto_compounder_engine, which itself imports fcf_valuation_engine - if
fcf_valuation_engine imported moat_engine directly to reuse its
classifier, that would be a circular import. This module has no such
dependency in either direction, so both can import it freely.
"""

import os

# Ticker override OUT (standard mode, switch ON only) - asset-light fee
# earners where operating cash flow is close to owner earnings: payment
# networks/processors and ratings/index/analytics businesses, per
# Fable's principle above.
_OVERRIDE_OUT_TICKERS = {"V", "MA", "PYPL", "SPGI", "MCO", "MSCI", "FICO", "CPU.AX"}

# Ticker override IN (financials mode, switch ON only) - clearing-house
# margin balances swing operating cash flow the same way a bank's
# deposit book does, per Fable's principle above.
_OVERRIDE_IN_TICKERS = {"CME", "ICE", "NDAQ", "CBOE", "ASX.AX"}

# Yahoo industry string that defaults to standard mode (switch ON only)
# unless the ticker is explicitly in the IN table above - exchanges/
# data vendors are asset-light fee earners by Fable's principle, with
# the clearing-house exceptions named explicitly rather than inferred.
_FINANCIAL_DATA_EXCHANGES_INDUSTRY = "Financial Data & Stock Exchanges"


def is_financials_store_live():
    """The switch (used by Commits 3 and 4 of instruction_financials_
    income_store_and_top200_guard.md) - same unset/""/0/off=OFF,
    1/on=ON pattern as scan_store.is_private_universe(). Re-read on
    every call (cheap - one env var lookup) rather than cached, so a
    changed Railway variable takes effect on the next request without
    a redeploy, same reasoning as every other switch in this codebase.
    Claude Code never sets this - the Director does, on Railway, after
    Andrew has read the Commit 5 dry run."""
    raw = (os.environ.get("FINANCIALS_STORE_LIVE") or "").strip().lower()
    return raw in ("1", "on")


def _override_table_verdict(industry_raw, ticker):
    """Commit 3's override table (rules 2-4 of is_financials()'s own
    docstring), factored out so Commit 5's is_financials_shadow() can
    apply the identical table without depending on the LIVE switch -
    "Keep ONE classifier for both - do not split it" extends to the
    shadow computation too, not just moat_engine/fcf_valuation_engine.
    Returns True/False when the table has a verdict, None when it
    doesn't (caller falls through to the existing sector/industry
    rule) - `ticker` falsy always returns None, same as before this
    refactor (is_financials() never read the table without a ticker)."""
    if not ticker:
        return None
    t = ticker.strip().upper()
    if t in _OVERRIDE_OUT_TICKERS:
        return False
    if t in _OVERRIDE_IN_TICKERS:
        return True
    if industry_raw == _FINANCIAL_DATA_EXCHANGES_INDUSTRY:
        return False
    return None


def is_financials(info, ticker=None):
    """True for a bank/insurer (Financial Services sector, or "bank"/
    "insurance" in the industry string) - see this module's own
    docstring for why moat_engine.py and fcf_valuation_engine.py both
    need the exact same answer for the exact same ticker. AUB.AX (an
    insurance broker) must take this path.

    EXCLUSION (2 Oct 2026, owner-directed): a REIT ("reit" or "real
    estate" in the industry string) is NOT financials mode, even
    though Yahoo often files it under the Financial Services sector -
    a REIT owns and operates real property; its free cash flow and
    ROIC are both still meaningful, unlike a loan/deposit book, so it
    keeps the standard OCF-based DCF and ROIC-based moat path. Checked
    FIRST, before the override table below, in both switch states.

    `ticker` (Commit 3, 4 Oct 2026, Director-directed): optional - this
    function took `info` alone before this commit. With the switch OFF
    (the default), `ticker` is never read and the answer is exactly
    what this function returned before this commit, for every fixture.
    With the switch ON, applies the override table (_override_table_
    verdict() above) in this order, before falling through to the
    existing rule unchanged:
      1. REIT/real-estate exclusion (above, unconditional).
      2. `ticker` in the OUT table -> standard mode (False).
      3. `ticker` in the IN table -> financials mode (True).
      4. Yahoo industry "Financial Data & Stock Exchanges" -> standard
         mode (False), unless caught by rule 3 above.
      5. Otherwise, the existing rule below, unchanged: Financial
         Services sector, or "bank"/"insurance" in industry.
    A caller with no `ticker` (None or empty) skips rules 2-4 even with
    the switch ON and falls straight through to rule 5 - the override
    table needs a ticker to match against, same "missing data never
    blocks a decision" philosophy as the rest of this app."""
    industry_raw = info.get("industry") or ""
    industry = industry_raw.lower()
    if "reit" in industry or "real estate" in industry:
        return False

    if is_financials_store_live() and ticker:
        verdict = _override_table_verdict(industry_raw, ticker)
        if verdict is not None:
            return verdict

    sector = (info.get("sector") or "").strip()
    if sector == "Financial Services":
        return True
    return ("bank" in industry) or ("insurance" in industry)


_SHADOW_REASON_VERDICT = {
    "reit_exclusion": False,
    "ticker_table_out": False,
    "ticker_table_in": True,
    "industry_rule": False,
}


def shadow_mode_reason(info, ticker=None):
    """Which rule decides is_financials_shadow()'s answer for this
    ticker, in the exact order that function checks them (Addendum 2
    item 7 of instruction_financials_income_store_and_top200_guard.md,
    5 Oct 2026, Director-directed follow-up: "financials dry run -
    three follow-ups from the first real data") - Andrew needs to see
    which tickers were made standard by the industry rule alone (e.g.
    COIN/FDS in the S&P 500 export) as opposed to the explicit
    override list, since those are two different kinds of judgment
    call with two different confidence levels.

    One of:
      "reit_exclusion"   - the REIT/real-estate exclusion fired.
      "ticker_table_out"  - `ticker` is in _OVERRIDE_OUT_TICKERS.
      "ticker_table_in"   - `ticker` is in _OVERRIDE_IN_TICKERS.
      "industry_rule"     - Yahoo industry "Financial Data & Stock
                             Exchanges", not caught by the IN table.
      "existing_rule"      - none of the above fired; the answer comes
                             from the pre-Commit-3 sector/industry
                             rule, unchanged by the override table."""
    industry_raw = info.get("industry") or ""
    industry = industry_raw.lower()
    if "reit" in industry or "real estate" in industry:
        return "reit_exclusion"
    if ticker:
        t = ticker.strip().upper()
        if t in _OVERRIDE_OUT_TICKERS:
            return "ticker_table_out"
        if t in _OVERRIDE_IN_TICKERS:
            return "ticker_table_in"
        if industry_raw == _FINANCIAL_DATA_EXCHANGES_INDUSTRY:
            return "industry_rule"
    return "existing_rule"


def is_financials_shadow(info, ticker=None):
    """Commit 5 of instruction_financials_income_store_and_top200_
    guard.md (4 Oct 2026, Director-directed): "what is_financials()
    would answer if FINANCIALS_STORE_LIVE were ON" - used ONLY by
    financials_dry_run.py to show, in the owner-only dry run, which
    tickers would change mode under the switch, WITHOUT reading or
    depending on is_financials_store_live() at all (the dry run must
    work regardless of the live switch's actual state, and must never
    need to flip it to compute a hypothetical answer).

    Identical logic to is_financials(), with the switch check removed -
    the override table always applies here, given a ticker. Returns
    the exact same answer as is_financials(info, ticker=ticker) would
    if the switch were ON; with no ticker given, falls through to the
    existing rule exactly like is_financials() does. Thin wrapper
    around shadow_mode_reason() above (Addendum 2 item 7, 5 Oct 2026),
    which also exposes WHICH rule decided - one shared decision, not
    two copies that could drift apart from each other."""
    reason = shadow_mode_reason(info, ticker=ticker)
    if reason in _SHADOW_REASON_VERDICT:
        return _SHADOW_REASON_VERDICT[reason]

    sector = (info.get("sector") or "").strip()
    if sector == "Financial Services":
        return True
    industry = (info.get("industry") or "").lower()
    return ("bank" in industry) or ("insurance" in industry)
