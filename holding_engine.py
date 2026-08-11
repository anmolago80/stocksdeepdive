def get_holding_period(stock_type):
    """
    Updated to short-term trading horizons (previously these were long-term
    investing periods like "3-5 Years" for a Compounder - the whole holding
    period philosophy has shifted from buy-and-hold to tactical trading).
    """

    if stock_type == "GROWTH":
        return "2-12 Weeks"

    elif stock_type == "TOLL BOOTH":
        return "1-3 Months"

    elif stock_type == "COMPOUNDER":
        return "1-3 Months"

    elif stock_type == "COMMODITY":
        return "Days-8 Weeks"

    elif stock_type == "TURNAROUND":
        return "2-6 Months"

    else:
        return "2-8 Weeks"
