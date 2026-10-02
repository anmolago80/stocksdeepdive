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

Zero project imports here deliberately: moat_engine.py already imports
auto_compounder_engine, which itself imports fcf_valuation_engine - if
fcf_valuation_engine imported moat_engine directly to reuse its
classifier, that would be a circular import. This module has no such
dependency in either direction, so both can import it freely.
"""


def is_financials(info):
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
    keeps the standard OCF-based DCF and ROIC-based moat path."""
    industry = (info.get("industry") or "").lower()
    if "reit" in industry or "real estate" in industry:
        return False
    sector = (info.get("sector") or "").strip()
    if sector == "Financial Services":
        return True
    return ("bank" in industry) or ("insurance" in industry)
