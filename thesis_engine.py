def generate_thesis(
    ticker,
    stock_type,
    quality_score,
    margin_of_safety,
    psychology_score,
    discovery_score,
    long_score,
    holding_period
):
    """
    Returns a dict with the four sections from the flowchart:
    Why Buy, Why Wait, Risks, and Holding Period. Rendering (bullets,
    headers, etc.) is left to the caller (app.py) so this stays reusable
    outside Streamlit too.
    """

    why_buy = []
    why_wait = []
    risks = []

    if quality_score >= 70:
        why_buy.append(
            f"High quality business (score {quality_score})."
        )
    else:
        why_wait.append(
            f"Quality score is only {quality_score} - fundamentals could be stronger."
        )

    if margin_of_safety >= 20:
        why_buy.append(
            f"Trading at a {round(margin_of_safety, 1)}% discount to estimated intrinsic value."
        )
    elif margin_of_safety <= 0:
        risks.append(
            "Currently trading at or above estimated intrinsic value - little to no margin of safety."
        )
    else:
        why_wait.append(
            f"Margin of safety is thin ({round(margin_of_safety, 1)}%) - consider waiting for a better entry."
        )

    if psychology_score > 20:
        why_buy.append(
            "Market sentiment shows fear without excessive greed - a contrarian setup."
        )
    elif psychology_score < -10:
        risks.append(
            "Psychology score is negative - the stock may be overheated or driven by FOMO buying."
        )

    if discovery_score > 50:
        why_buy.append(
            "Volume, trend and news activity are elevated - the market may be starting to notice this name."
        )

    if long_score >= 80:
        why_buy.append("Overall Long Score qualifies this as a STRONG LONG candidate.")
    elif long_score < 40:
        risks.append("Overall Long Score is weak - treat as AVOID until conditions improve.")

    if not why_buy:
        why_buy.append("No strong bullish signals present right now.")

    if not why_wait:
        why_wait.append("No notable reasons to delay.")

    if not risks:
        risks.append("No material risks flagged by the model.")

    return {
        "ticker": ticker,
        "stock_type": stock_type,
        "why_buy": why_buy,
        "why_wait": why_wait,
        "risks": risks,
        "holding_period": holding_period,
        "long_score": long_score,
    }


def thesis_to_text(thesis):
    """Flatten the thesis dict into a plain-text block (e.g. for CSV export)."""

    lines = [f"{thesis['ticker']} ({thesis['stock_type']})", ""]

    lines.append("Why Buy:")
    lines += [f"- {point}" for point in thesis["why_buy"]]

    lines.append("")
    lines.append("Why Wait:")
    lines += [f"- {point}" for point in thesis["why_wait"]]

    lines.append("")
    lines.append("Risks:")
    lines += [f"- {point}" for point in thesis["risks"]]

    lines.append("")
    lines.append(f"Suggested Holding Period: {thesis['holding_period']}")

    return "\n".join(lines)
