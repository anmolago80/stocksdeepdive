"""
currency_format.py

Lists & display Commit 5 (3 Oct 2026, Director-directed): ONE shared
formatter for displaying a per-ticker money amount, driven by the
ticker's own trading currency (fcf_valuation_engine.trading_currency_
for()) - replacing independently hand-rolled "$"/2-decimal hardcoding
scattered across compounder_ui.py/research_snapshot_render.py/
auto_compounder_engine.py/og_card_render.py/results_engine.py/app.py
(Japan Commit A's own inventory first flagged these sites; Stage 1a's
own trading_currency_for() docstring explicitly deferred wiring it into
exactly this display layer as "a separate, much larger... change" -
this commit is that follow-up).

USD/AUD: unchanged - this module never changes what either currency
displays as. Every caller keeps passing its own existing USD/AUD symbol
(e.g. "$", or a site that already disambiguates with "A$"/"US$"), via
`default_symbol`, and gets it back byte-identical. GBP/CAD/JPY: shown
with their own symbol (£/C$/¥) instead of a bare, ambiguous "$" that
would otherwise read as USD. JPY additionally never shows decimals
(yen has no everyday subunit) and always carries thousands separators.

The literal £/C$/¥/$ characters here are plain text, not markdown - a
caller rendering through a plain-Markdown Streamlit context (st.markdown
/st.caption without unsafe HTML) must still apply this codebase's own
KaTeX-pair-escaping convention itself wherever a rendered line could
carry two or more amounts (see app.py._fmt_aud_md()'s own docstring for
why, and CLAUDE.md's "Streamlit traps" section) - this module only
picks the symbol/decimals, it never touches markdown escaping.
"""
import fcf_valuation_engine

_EXTRA_SYMBOLS = {"GBP": "£", "CAD": "C$", "JPY": "¥"}


def symbol_for_ticker(ticker, default_symbol="$"):
    """The display symbol for `ticker`'s own trading currency.
    `default_symbol` is returned unchanged for USD/AUD/anything this
    module doesn't special-case - byte-identical to every pre-existing
    hardcoded "$"/"A$"/"US$" site - GBP/CAD/JPY get their own symbol."""
    ccy = fcf_valuation_engine.trading_currency_for(ticker)
    return _EXTRA_SYMBOLS.get(ccy, default_symbol)


def format_money(value, ticker, default_symbol="$", decimals=None):
    """"{symbol}12,345.67"-style money string for `ticker`'s own
    currency, or None when `value` is None (callers decide their own
    "N/A"/"-"/em-dash convention).

    `decimals`: None keeps each call site's own pre-existing magnitude
    rule (0dp at/above 1000, 2dp below - the "cur" convention several
    of these sites already used for USD/AUD); an explicit int fixes the
    decimal count regardless of magnitude. JPY always shows 0 decimals
    with thousands separators, overriding either."""
    if value is None:
        return None
    ccy = fcf_valuation_engine.trading_currency_for(ticker)
    symbol = _EXTRA_SYMBOLS.get(ccy, default_symbol)
    if ccy == "JPY":
        return f"{symbol}{value:,.0f}"
    d = decimals if decimals is not None else (0 if abs(value) >= 1000 else 2)
    return f"{symbol}{value:,.{d}f}"
