"""
Trade Filter Engine - a tactical entry/exit layer that answers "is right now a
sane price to actually start building a long-term position", separate from the
Ranking Engine's "is this a good business to own".

Gate change (July 2026): the old Long Score >= 60 requirement was too
restrictive - it rejected genuinely cheap, near-support entries just because
the business score hadn't cleared 60, even though business quality is already
judged independently by the Investment Signal. That gate is REMOVED. In its
place is a lightweight trend safety filter: don't try to start a position while
the stock is in a confirmed downtrend (falling knife). The dip-buying character
of the rest of the filter (buy near support, fear helps) is unchanged.

Trade filter gate (must ALL be true to even consider a trade):
    Trend is NOT "DOWNTREND"      (was: Long Score >= 60)
    AND Psychology > 0
    AND Discovery > 0
    AND Price <= MA50 x 1.05

Entry Zone   = max(Support20, MA50)
Buy only if  Price <= Entry Zone x 1.03      (near support, not chasing)
Stop Loss    = Support20 x 0.97               (3% below support)

Target 1     = Resistance20                   (~1-month swing high)
Target 2     = Resistance60                   (~3-month swing high)
Target 3     = Resistance60 x 1.10, only if Discovery > 50 (breakout scenario)

Risk         = Entry Price - Stop Loss
Reward N     = Target N - Entry Price
RR N         = Reward N / Risk

Minimum requirement: RR1 >= 1.5 (RR2 still reported, no longer a hard gate).

Early exit (monitor after entry):
    Psychology < -30 OR FOMO > 40 OR Greed > 30 OR Price closes below MA50
"""


def calc_support_resistance(closes):
    """
    closes: a pandas Series (or list) of Close prices, most recent last.
    Returns a dict with Support20/Resistance20 (~1 month) and
    Support60/Resistance60 (~3 months). Falls back to whatever history is
    available if there's less than 60 rows.
    """
    n = len(closes)
    window20 = closes[-min(20, n):]
    window60 = closes[-min(60, n):]

    return {
        "support20": float(window20.min()),
        "resistance20": float(window20.max()),
        "support60": float(window60.min()),
        "resistance60": float(window60.max()),
    }


def evaluate_trade(
    current_price,
    ma50,
    support20,
    resistance20,
    support60,
    resistance60,
    long_score,
    psychology_score,
    discovery_score,
    fomo_score,
    greed_score,
    trend="RANGING",
):
    """
    Runs the full trade filter and returns a dict with every intermediate
    value plus a final BUY / WATCHLIST / AVOID call, so the UI can show its
    work rather than just a verdict.

    `long_score` is kept in the signature (callers still pass it and it is
    reported alongside the trade) but it is NO LONGER part of the gate - the
    business-quality judgement lives in the Investment Signal instead. `trend`
    is one of "UPTREND" / "DOWNTREND" / "RANGING" (case-insensitive); a
    confirmed downtrend blocks the setup.
    """

    # Trend safety filter replaces the old Long Score floor: we still won't try
    # to enter a falling knife, but we no longer require a high business score
    # to consider an entry.
    gate_trend = str(trend).upper() != "DOWNTREND"
    gate_psychology = psychology_score > 0
    gate_discovery = discovery_score > 0
    # Entry gate widened from MA50 x 1.03 to x 1.05 so a genuinely cheap,
    # high-quality name near support isn't rejected on a hair.
    gate_price_vs_ma50 = ma50 > 0 and current_price <= ma50 * 1.05
    passes_gate = (
        gate_trend and gate_psychology and gate_discovery and gate_price_vs_ma50
    )

    entry_zone = max(support20, ma50) if ma50 > 0 else support20
    near_entry = entry_zone > 0 and current_price <= entry_zone * 1.03

    stop_loss = support20 * 0.97

    target1 = resistance20
    target2 = resistance60
    target3 = resistance60 * 1.10 if discovery_score > 50 else None

    entry_price = current_price
    risk = entry_price - stop_loss

    def safe_ratio(reward, risk_):
        if risk_ is None or risk_ <= 0:
            return None
        return round(reward / risk_, 2)

    reward1 = target1 - entry_price
    reward2 = target2 - entry_price
    reward3 = (target3 - entry_price) if target3 is not None else None

    rr1 = safe_ratio(reward1, risk)
    rr2 = safe_ratio(reward2, risk)
    rr3 = safe_ratio(reward3, risk) if reward3 is not None else None

    # Relaxed reward bar: require a realistic RR1 >= 1.5 to the first target.
    # The old "RR1 >= 2 AND RR2 >= 3" double requirement rejected almost every
    # setup (a stock near resistance has a small reward-to-T1 vs a support-
    # based stop). RR2 is still reported, just no longer a hard gate.
    meets_rr_minimum = (rr1 is not None and rr1 >= 1.5)

    if passes_gate and near_entry and meets_rr_minimum and risk > 0:
        signal = "BUY"
    elif passes_gate and risk > 0:
        # Setup qualifies on the trend / psychology gate, but either the price
        # isn't near the entry zone yet or the reward doesn't clear the minimum
        # risk/reward bar - worth watching, not buying now.
        signal = "WATCHLIST"
    else:
        signal = "AVOID"

    early_exit_triggered = (
        psychology_score < -30
        or fomo_score > 40
        or greed_score > 30
        or current_price < ma50
    )

    return {
        "signal": signal,
        "trend": str(trend).upper(),
        "gate_trend": gate_trend,
        "entry_zone": round(entry_zone, 2),
        "near_entry_zone": near_entry,
        "entry_price": round(entry_price, 2),
        "stop_loss": round(stop_loss, 2),
        "target1": round(target1, 2),
        "target2": round(target2, 2),
        "target3": round(target3, 2) if target3 is not None else None,
        "risk": round(risk, 2),
        "reward1": round(reward1, 2),
        "reward2": round(reward2, 2),
        "reward3": round(reward3, 2) if reward3 is not None else None,
        "rr1": rr1,
        "rr2": rr2,
        "rr3": rr3,
        "meets_rr_minimum": meets_rr_minimum,
        "passes_gate": passes_gate,
        "early_exit_watch": early_exit_triggered,
    }


def position_management_notes():
    """Static text describing what to do as each target is hit - shown
    alongside the trade filter table since it's a monitoring rule set
    rather than a per-scan number."""
    return [
        "Target 1 hit: sell 25%, move stop to break-even.",
        "Target 2 hit: sell 50%, trail stop below MA50.",
        "Target 3 hit: exit remaining position.",
    ]


def early_exit_notes():
    """Static text describing the early-exit monitoring rules."""
    return [
        "Psychology score drops below -30.",
        "FOMO score rises above 40.",
        "Greed score rises above 30.",
        "Price closes below the 50-day moving average.",
    ]


def score_trade_setup(trade_result, psychology_score, discovery_score, ma50, current_price):
    """
    Trade Setup Score (0-100): NOT a re-implemented trade formula - just a
    visual weighting of the SAME gate checks evaluate_trade() already applies
    (see its docstring's gate list), so a chart/column can show what's
    driving the BUY/WATCHLIST/AVOID verdict. evaluate_trade() only returns
    `gate_trend` individually (the rest are folded into `passes_gate`), so
    the psychology/discovery/MA50 checks here mirror that SAME docstring
    gate definition - not a new rule.

    `trade_result` is the dict evaluate_trade() returned for this stock.
    `psychology_score`/`discovery_score`/`ma50`/`current_price` are the same
    inputs already passed into evaluate_trade() for that call.

    Returns (score, contributions) - contributions is an ordered dict of the
    six weighted components (summing to `score`), so a caller can show a
    "what's driving this" breakdown alongside the verdict.
    """
    gate_psychology = psychology_score > 0
    gate_discovery = discovery_score > 0
    gate_price_ma50 = ma50 > 0 and current_price <= ma50 * 1.05

    contributions = {
        "Trend Safety": 20.0 if trade_result["gate_trend"] else 0.0,
        "Near Entry Zone": 20.0 if trade_result["near_entry_zone"] else 0.0,
        "Risk/Reward (RR1 >= 1.5)": 20.0 if trade_result["meets_rr_minimum"] else 0.0,
        "Price vs MA50": 15.0 if gate_price_ma50 else 0.0,
        "Psychology Momentum": 12.5 if gate_psychology else 0.0,
        "Discovery Momentum": 12.5 if gate_discovery else 0.0,
    }
    score = round(sum(contributions.values()), 2)
    return score, contributions
