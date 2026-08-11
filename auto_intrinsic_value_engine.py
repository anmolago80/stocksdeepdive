import yfinance as yf


# Rough fair-P/E baselines by GICS-ish sector. These aren't precise - they're
# sane anchors so a commodity name isn't valued on the same multiple as a
# software name. yfinance's `sector` strings are matched case-insensitively;
# anything unrecognized falls back to DEFAULT_PE (flagged as a default).
SECTOR_PE_BASELINE = {
    "TECHNOLOGY": 25,
    "COMMUNICATION SERVICES": 20,
    "CONSUMER CYCLICAL": 20,
    "CONSUMER DISCRETIONARY": 20,
    "HEALTHCARE": 22,
    "INDUSTRIALS": 17,
    "CONSUMER DEFENSIVE": 18,
    "CONSUMER STAPLES": 18,
    "UTILITIES": 15,
    "REAL ESTATE": 16,
    "FINANCIAL SERVICES": 12,
    "FINANCIALS": 12,
    "BASIC MATERIALS": 13,
    "MATERIALS": 13,
    "ENERGY": 12,
}

DEFAULT_PE = 15


def get_intrinsic_value(ticker, quality_score, info=None):
    """
    P/E-blend fallback used only when the DCF can't value a name (non-positive
    FCF: financials, early growth, etc.).

    intrinsic_value = EPS x fair_pe, where fair_pe blends three views:
        growth_pe  = clamp(earnings_growth %, 10, 30)
        quality_pe = 10 + quality_score / 5
        sector_pe  = SECTOR_PE_BASELINE[sector]  (else DEFAULT_PE)
    fair_pe = average of the three.

    Returns (intrinsic_value, defaulted) where `defaulted` is True when the
    sector was unknown (so the sector anchor is just the DEFAULT_PE average) -
    the app renders that value in red because part of the multiple is assumed.
    Returns (0, False) when there's no positive EPS to value at all (the
    caller then shows N/A, which is not a default - it's an honest "unknown").
    """

    try:

        if info is None:
            info = yf.Ticker(ticker).info or {}

        eps = info.get("trailingEps", 0) or 0

        if eps <= 0:
            return 0, False

        earnings_growth_raw = info.get("earningsGrowth", 0) or 0
        earnings_growth_pct = earnings_growth_raw * 100

        growth_pe = min(max(earnings_growth_pct, 10), 30)
        quality_pe = 10 + (quality_score / 5)

        sector = (info.get("sector", "") or "").upper()
        sector_known = sector in SECTOR_PE_BASELINE
        sector_pe = SECTOR_PE_BASELINE.get(sector, DEFAULT_PE)

        fair_pe = (growth_pe + quality_pe + sector_pe) / 3

        intrinsic_value = eps * fair_pe

        return round(intrinsic_value, 2), (not sector_known)

    except Exception:

        return 0, False
