import yfinance as yf


def quality_score_inputs(info):
    """TASK 34 item 1 (5 Nov 2026, BKNG live case, Director-approved
    diagnostic-logging-only follow-up): the raw inputs get_quality_score()
    itself derives from `info`, factored out as a read-only helper so
    deep_dive_engine.py's and nightly_scan.py's own "[quality_diag]" log
    lines can report the EXACT same numbers get_quality_score() actually
    weighted, rather than a second, drifting re-derivation. Pure read, no
    side effects, not called by get_quality_score() itself (which keeps
    its own inline locals unchanged) - this never changes what any
    existing caller of get_quality_score() gets back.

    Returns {"roic", "roe", "profitMargins", "revenueGrowth",
    "earningsGrowth", "fcf", "debtToEquity"} - roic is the SAME derived
    (marketCap/priceToBook -> book equity -> + totalDebt -> invested
    capital -> netIncomeToCommon/invested capital, clamped to +-100%)
    figure get_quality_score() computes internally; the rest are the
    plain info fields it reads (revenueGrowth/earningsGrowth reported
    here BEFORE the +-50% growth clamp get_quality_score() applies
    before weighting, since this is for comparing against the live
    Yahoo figures a reader would otherwise look up directly)."""
    try:
        roe = info.get("returnOnEquity", 0) or 0
        profit_margin = info.get("profitMargins", 0) or 0
        revenue_growth = info.get("revenueGrowth", 0) or 0
        earnings_growth = info.get("earningsGrowth", 0) or 0
        debt_to_equity = info.get("debtToEquity", 0) or 0
        free_cash_flow = info.get("freeCashflow", 0) or 0
        net_income = info.get("netIncomeToCommon", 0) or 0

        roic = 0.0
        market_cap = info.get("marketCap", 0) or 0
        price_to_book = info.get("priceToBook", 0) or 0
        total_debt = info.get("totalDebt", 0) or 0
        if market_cap > 0 and price_to_book > 0:
            book_equity = market_cap / price_to_book
            invested_capital = book_equity + total_debt
            if invested_capital > 0:
                roic = max(-1.0, min(net_income / invested_capital, 1.0))

        return {
            "roic": roic, "roe": roe, "profitMargins": profit_margin,
            "revenueGrowth": revenue_growth, "earningsGrowth": earnings_growth,
            "fcf": free_cash_flow, "debtToEquity": debt_to_equity,
        }
    except Exception:
        return {
            "roic": None, "roe": None, "profitMargins": None,
            "revenueGrowth": None, "earningsGrowth": None,
            "fcf": None, "debtToEquity": None,
        }


def get_quality_score(ticker, info=None):
    """
    Expanded quality score computed from the stock's own fundamentals.

    Contributions (all yfinance .info fields, which are decimal fractions
    where 0.15 = 15%):
        base                50
        ROE            x100 x 0.30   (profitability on equity)
        profit margin  x100 x 0.20   (profitability on sales)
        revenue growth x100 x 0.20   (top-line momentum)
        earnings growth x100 x 0.20  (bottom-line momentum)
        free cash flow      +10 if positive, -5 if negative
        debt/equity    x0.05 penalty (capped so heavy debt can't tank it)

    Two guards were added so speculative, unprofitable names (e.g. a
    cash-burning clinical-stage biotech) can no longer score 100:

      * GROWTH CLAMP - revenue/earnings growth are clamped to +-50% BEFORE
        weighting, so a +200-300% figure off a tiny base can't max the score.
      * PROFITABILITY GATE - if the business is loss-making or cash-burning
        (negative profit margin, net income, or free cash flow), the score is
        capped at PROFIT_GATE_CAP. A company that doesn't make money is not a
        "high quality" business no matter how fast a small base is growing.

    Returns (score, defaulted) where `defaulted` is True when NONE of the
    fundamental inputs were available - i.e. the score is just the base
    average assumption and the app should render it in red.
    """

    PROFIT_GATE_CAP = 55       # ceiling for loss-making / cash-burning names
    GROWTH_CLAMP = 0.50        # clamp growth inputs to +-50% before weighting

    try:

        if info is None:
            info = yf.Ticker(ticker).info or {}

        keys = (
            "returnOnEquity", "profitMargins", "revenueGrowth",
            "earningsGrowth", "debtToEquity", "freeCashflow", "netIncomeToCommon",
            "marketCap", "priceToBook", "totalDebt",
        )
        # Defaulted when yfinance gave us nothing to work with.
        any_data = any(info.get(k) is not None for k in keys)

        roe = info.get("returnOnEquity", 0) or 0
        profit_margin = info.get("profitMargins", 0) or 0
        revenue_growth = info.get("revenueGrowth", 0) or 0
        earnings_growth = info.get("earningsGrowth", 0) or 0
        debt_to_equity = info.get("debtToEquity", 0) or 0
        free_cash_flow = info.get("freeCashflow", 0) or 0
        net_income = info.get("netIncomeToCommon", 0) or 0

        # Clamp growth so off-a-small-base percentages can't dominate.
        revenue_growth = max(-GROWTH_CLAMP, min(revenue_growth, GROWTH_CLAMP))
        earnings_growth = max(-GROWTH_CLAMP, min(earnings_growth, GROWTH_CLAMP))

        # ROIC (return on invested capital) - the compounder metric: how
        # much profit each dollar of TOTAL capital (equity + debt) earns,
        # so a business can't look great on ROE alone by simply levering
        # up. yfinance's info dict doesn't carry ROIC directly, so it's
        # derived: book equity = marketCap / priceToBook, invested capital
        # = book equity + total debt, ROIC ~= net income / invested
        # capital. Skipped (0 contribution) when the inputs are missing,
        # clamped to +-100% against data glitches.
        roic = 0.0
        market_cap = info.get("marketCap", 0) or 0
        price_to_book = info.get("priceToBook", 0) or 0
        total_debt = info.get("totalDebt", 0) or 0
        if market_cap > 0 and price_to_book > 0:
            book_equity = market_cap / price_to_book
            invested_capital = book_equity + total_debt
            if invested_capital > 0:
                roic = max(-1.0, min(net_income / invested_capital, 1.0))

        score = 50

        # Weights re-balanced to make room for ROIC (same 0.90 total as
        # before, so the score scale is unchanged): quality is now led by
        # ROIC + ROE together rather than ROE alone.
        score += roic * 100 * 0.25
        score += roe * 100 * 0.20
        score += profit_margin * 100 * 0.15
        score += revenue_growth * 100 * 0.15
        score += earnings_growth * 100 * 0.15

        if free_cash_flow > 0:
            score += 10
        elif free_cash_flow < 0:
            score -= 5

        # Cap the debt penalty so a single very high debt/equity reading
        # can't drag an otherwise strong business to zero.
        debt_penalty = min(debt_to_equity * 0.05, 15)
        score -= debt_penalty

        score = max(min(round(score), 100), 0)

        # Profitability gate: a loss-making / cash-burning business cannot be
        # rated high quality regardless of its growth percentages.
        loss_making = (
            profit_margin < 0
            or net_income < 0
            or free_cash_flow < 0
        )
        if loss_making:
            score = min(score, PROFIT_GATE_CAP)

        return score, (not any_data)

    except Exception:

        return 50, True
