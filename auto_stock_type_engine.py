import yfinance as yf


def get_stock_type(ticker, info=None):
    """
    Classify a stock from its sector. Returns (stock_type, defaulted) where
    `defaulted` is True when the sector is unknown and we fell back to the
    GENERAL bucket - the app renders that in red so it's clear the type is an
    assumption rather than sourced from data.
    """

    try:

        if info is None:
            info = yf.Ticker(ticker).info or {}

        sector = (info.get("sector", "") or "").upper()

        if sector == "FINANCIAL SERVICES":
            return "TOLL BOOTH", False
        elif sector == "HEALTHCARE":
            return "COMPOUNDER", False
        elif sector == "ENERGY":
            return "COMMODITY", False
        elif sector == "BASIC MATERIALS":
            return "COMMODITY", False
        elif sector == "TECHNOLOGY":
            return "GROWTH", False
        else:
            return "GENERAL", True

    except Exception:

        return "GENERAL", True
