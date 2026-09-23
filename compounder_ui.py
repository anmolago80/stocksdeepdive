"""
compounder_ui.py

Shared rendering kit for the Rational Compounder Research page's six
COMPUTED sections - Fundamentals, Value vs Book, Retained Earnings,
Earnings Trends, Cost of Capital, Fair Value. Used by BOTH:
    - page_research() in app.py, with hand-built data read from
      compounder_data.json (Andrew's own SMSF research workbook), and
    - the Deep Dive page's "Compounder View (auto)" expander, with data
      computed live by auto_compounder_engine.build_sections() for any
      ticker.

Both callers pass a `sections` dict of the SAME shape (see
build_compounder_data.py / auto_compounder_engine.py) into render_section()
below, so the two views can never visually drift apart - one function, one
look, two data sources.

"Company Potential" (the author's own Low/Medium/High ratings and written
analysis) is deliberately NOT handled here - it is hand-research-only and
stays in app.py's _render_cp_section, which still branches to it directly
before ever calling into this module.

Moved here from app.py (not duplicated - app.py now imports these from
here): _CP_COLOR_FILL, _CP_COLOR_TEXT, _cp_clean_comment, _cp_format,
_cp_band, _cp_note, and every _cp_*_chart() figure-builder. The old plotly
bullet gauge (_cp_gauge) was retired in favour of band_gauge() below (the
approved compact HTML look) and deleted from app.py once nothing referenced
it any more.
"""

import datetime as _dt
import hashlib
import html
import math
import os
import re

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import i18n
import quote_recorder
import quote_snapshot_store
import trading_cost_engine
from simple_view_copy import SECTION_WHY_CAPTIONS, SECTION_WHY_CAPTIONS_ES

# -----------------------------------
# Trading Cost tab (Commit 3) - master switch. Same _truthy()/env-var
# shape as paywall_engine.PAYWALL_ENABLED - the recorder (quote_
# recorder.py, Commit 1) and the estimator (trading_cost_engine.py,
# Commit 2) both run/compute regardless of this flag; it only gates
# whether the TAB ITSELF appears. Neither quote_snapshot_store nor
# trading_cost_engine import anything from this module or app.py, so
# importing both here at module level (rather than deferred inside a
# function, like this file's own paywall_engine import) is safe - no
# circular import risk, and neither does network/heavy work at import
# time (quote_snapshot_store only opens a SQLite connection on demand;
# trading_cost_engine is pure Python with zero I/O of its own).
# -----------------------------------

def _truthy(v):
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


TRADING_COST_ENABLED = _truthy(os.environ.get("ENABLE_TRADING_COST"))

# -----------------------------------
# Colour vocabulary (moved from app.py, verbatim)
# -----------------------------------
_CP_COLOR_FILL = {"red": "#43222e", "amber": "#43371c", "green": "#27584a", "blue": "#1d4356"}
_CP_COLOR_TEXT = {"red": "#fb7185", "amber": "#fbbf24", "green": "#34d399", "blue": "#5ed3f0"}


# -----------------------------------
# Mobile PWA brief Part 2 (amended): shared sdd_plotly_chart() wrapper,
# used for EVERY plotly figure on the site (app.py, this module, the
# portfolio rendering code - all import it from here). Problem it fixes:
# on a touch device, a one-finger drag that starts on top of a plotly
# chart pans/zooms the CHART instead of scrolling the page - the browser
# has no way to know a swipe "meant" the page until plotly's own JS
# decides what to do with it, and plotly's default drag/scroll-zoom
# handlers claim that gesture for themselves.
#
# The owner wants desktop chart behaviour left alone (drag-to-zoom kept)
# - only phones/tablets get the touch-safe treatment - so the decision is
# made per REQUEST, server-side, from the visitor's User-Agent
# (_is_mobile_request() below), not applied site-wide any more. Hover
# (desktop) and tap-to-read (touch) both keep working everywhere either
# way - only DRAG-to-pan and pinch/scroll-to-zoom are conditionally
# disabled, and only for a mobile request. Belt-and-braces CSS
# (touch-action: pan-y pinch-zoom on the plotly DOM nodes, scoped to
# `@media (max-width:640px) and (pointer:coarse)`) lives in app.py's
# site-wide <style> block for the brief window before plotly's own JS
# finishes attaching its handlers - scoped to match this same
# mobile-only behaviour so a desktop visitor never gets it.
# -----------------------------------

_MOBILE_UA = re.compile(r"Mobi|Android|iPhone|iPad|iPod", re.I)


def _is_mobile_request():
    """Server-side per-request device check used by sdd_plotly_chart() to
    decide whether to disable chart drag/zoom - touch devices only,
    desktop keeps plotly's normal drag-to-zoom untouched. Memoized in
    st.session_state per script run (keyed once per browser tab, not
    recomputed for every chart on the page) since the request's
    User-Agent never changes mid-session for a given tab.

    st.context.headers needs Streamlit >= 1.37; this site runs 1.6x, well
    past that, but the lookup is still wrapped defensively - any failure
    here is treated as "not mobile" so the worst case is a phone visitor
    briefly keeping desktop drag/zoom behaviour, never a desktop visitor
    losing it."""
    if "_sdd_is_mobile_request" in st.session_state:
        return st.session_state["_sdd_is_mobile_request"]
    is_mobile = False
    try:
        ua = st.context.headers.get("User-Agent", "")
        is_mobile = bool(_MOBILE_UA.search(ua or ""))
    except Exception:
        is_mobile = False
    st.session_state["_sdd_is_mobile_request"] = is_mobile
    return is_mobile


def _chart_text_equivalent(fig):
    """Best-effort plain-text description of a Plotly figure, generated
    purely from the figure's own title and trace data - AI-readiness
    roadmap Phase 10 ("text equivalents for every chart"). Deliberately
    NOT authored per call site: every chart on the site already funnels
    through sdd_plotly_chart() below, so a generic, data-driven summary
    here covers every one of them uniformly, with zero changes needed at
    any of the ~40 individual chart call sites. Handles the 4 trace
    types actually used anywhere on this site (Indicator/gauge, Bar,
    Scatter/line, Pie) with a generic per-trace fallback for anything
    else, and never raises - a figure shape this doesn't recognise just
    gets a shorter description, never a broken page.

    Every gauge on this site (_dd_gauge in app.py) deliberately draws its
    title as a paper-space annotation rather than fig.layout.title or the
    Indicator's own built-in title (see that function's own docstring for
    why) - so a figure with no layout title falls back to its first
    annotation's text, which is exactly where a gauge's title actually
    lives. Without this fallback every gauge's text equivalent would read
    as a generic "Chart." with no indication of which score it is."""
    try:
        title = None
        if fig.layout.title and fig.layout.title.text:
            title = re.sub(r"<[^>]+>", "", fig.layout.title.text).strip()
        elif fig.layout.annotations:
            _first_ann = fig.layout.annotations[0].text
            if _first_ann:
                title = re.sub(r"<[^>]+>", "", _first_ann).strip()
        lines = [f"Chart: {title}." if title else "Chart."]
        _used_marker_desc = False
        for trace in fig.data:
            ttype = getattr(trace, "type", None)
            tname = (getattr(trace, "name", None) or "").strip()
            if ttype == "indicator":
                val = getattr(trace, "value", None)
                if val is not None:
                    lines.append(f"Gauge value: {val:,.1f}." if isinstance(val, (int, float)) else f"Gauge value: {val}.")
            elif ttype == "bar":
                xs = list(getattr(trace, "x", None) or [])
                ys = list(getattr(trace, "y", None) or [])
                pairs = list(zip(ys, xs)) if getattr(trace, "orientation", None) == "h" else list(zip(xs, ys))
                if pairs:
                    parts = "; ".join(
                        f"{c}: {v:,.2f}" if isinstance(v, (int, float)) else f"{c}: {v}"
                        for c, v in pairs[:25]
                    )
                    prefix = f"{tname} bar values" if tname else "Bar values"
                    lines.append(f"{prefix} - {parts}.")
            elif ttype == "pie":
                labels = list(getattr(trace, "labels", None) or [])
                values = list(getattr(trace, "values", None) or [])
                if labels and values:
                    parts = "; ".join(f"{l}: {v}" for l, v in zip(labels, values))
                    lines.append(f"Pie shares - {parts}.")
            elif ttype == "scatter":
                ys = [v for v in (list(getattr(trace, "y", None) or [])) if isinstance(v, (int, float))]
                mode = getattr(trace, "mode", None) or ""
                # Fix (2026-09-02, live: the reverse-DCF gauge's text
                # description read "starts at 0.00, ends at 0.00 ...
                # over 1 points") - a marker-only trace (no connecting
                # line) whose Y carries no information at all (every
                # point pinned to the same Y - the reverse-DCF gauge and
                # every other horizontal number-line gauge on this site
                # only vary on X, Y is always 0) used to fall straight
                # into the line-series template below and describe the
                # meaningless constant Y instead of the actual reading.
                # Describe the marker(s) by their X position instead
                # whenever Y doesn't vary across the trace - a genuine
                # multi-point time series (e.g. the Portfolio "Buy"
                # markers overlay, where Y is a real dollar value that
                # DOES vary per point) is unaffected and still gets the
                # line-series description below.
                is_marker_only = "markers" in mode and "lines" not in mode
                if is_marker_only and ys and len(set(ys)) <= 1:
                    xs_num = [v for v in (list(getattr(trace, "x", None) or [])) if isinstance(v, (int, float))]
                    if xs_num:
                        prefix = f"{tname} marker" if tname else "Marker"
                        if len(xs_num) == 1:
                            lines.append(f"{prefix} at {xs_num[0]:,.2f}.")
                        else:
                            lines.append(f"{prefix}s at " + ", ".join(f"{v:,.2f}" for v in xs_num) + ".")
                        _used_marker_desc = True
                        continue
                if ys:
                    prefix = f"{tname} line" if tname else "Line"
                    lines.append(
                        f"{prefix}: starts at {ys[0]:,.2f}, ends at {ys[-1]:,.2f}, "
                        f"ranging {min(ys):,.2f} to {max(ys):,.2f} over {len(ys)} points."
                    )
            elif tname:
                lines.append(f"{tname}: see chart.")
        if _used_marker_desc:
            try:
                _xr = fig.layout.xaxis.range
                if _xr and len(_xr) == 2:
                    lines.append(f"Scale: {_xr[0]:,.0f} to {_xr[1]:,.0f}.")
            except Exception:
                pass
        return " ".join(lines)
    except Exception:
        return "Chart (text description unavailable)."


def sdd_plotly_chart(fig, **kwargs):
    """Drop-in replacement for st.plotly_chart - same call signature, so
    every call site on the site was a mechanical rename to this function.

    Mobile request (_is_mobile_request()): forces dragmode off on the
    figure itself and merges displayModeBar/scrollZoom/doubleClick off
    into whatever `config` (if any) the caller already passed, so a
    caller-supplied config still wins on any OTHER key - same touch-safe
    behaviour as before this amendment.

    Desktop request: the figure and `config` are passed straight through
    completely unchanged (dragmode left at the figure's own default -
    no call site on this site sets it explicitly, so that's plotly's
    normal 'zoom' drag-to-zoom behaviour; config exactly what the call
    site passed, or omitted entirely if the call site passed none).

    AI-readiness roadmap Phase 10: after rendering, also renders a
    COLLAPSED text-equivalent of the same chart (see
    _chart_text_equivalent above) - additive only, the chart itself is
    completely unchanged. The expander's key is derived from the
    figure's own title + trace data (not a per-run counter), so it's
    stable across reruns yet still unique between two DIFFERENT charts
    that happen to share a title (e.g. a "Long Score" gauge repeated per
    Scanner row) - each gets its own key because its underlying numbers
    differ.

    text_description (Fix 2026-09-02, spec item 5, popped before the
    figure reaches st.plotly_chart - never a real plotly kwarg): an
    optional caller-supplied sentence used verbatim as the text
    equivalent instead of the generic auto-generated one. For most
    charts the generic description is fine (that's the whole point of
    doing this once here instead of per call site), but a card that
    already has its own plain-English sentence about the SAME figure -
    the reverse-DCF card's "At X, the market is pricing in ..." - reads
    better and stays perfectly in sync with what the card itself says,
    rather than a second, independently-generated description of the
    same numbers."""
    text_description = kwargs.pop("text_description", None)
    config = kwargs.pop("config", None)
    if _is_mobile_request():
        fig.update_layout(dragmode=False)
        merged = {"displayModeBar": False, "scrollZoom": False, "doubleClick": False, "responsive": True}
        merged.update(config or {})
        result = st.plotly_chart(fig, width='stretch', config=merged, **kwargs)
    elif config is not None:
        result = st.plotly_chart(fig, width='stretch', config=config, **kwargs)
    else:
        result = st.plotly_chart(fig, width='stretch', **kwargs)

    try:
        _anns = [getattr(a, "text", None) for a in (fig.layout.annotations or ())]
        _sig = repr(getattr(fig.layout.title, "text", None)) + repr(_anns) + "|".join(
            repr(list(getattr(t, "x", None) or [])) + repr(list(getattr(t, "y", None) or []))
            + repr(getattr(t, "value", None)) + repr(list(getattr(t, "values", None) or []))
            for t in fig.data
        )
        _key = "sdd_chart_txt_" + hashlib.md5(_sig.encode()).hexdigest()[:16]
        with st.expander("Text description of this chart", expanded=False, key=_key):
            st.caption(text_description or _chart_text_equivalent(fig))
    except Exception:
        pass

    return result


def _md_safe(text):
    """Duplicated from app.py's own _md_safe (a 2-line HTML/LaTeX-escape
    helper also used outside the compounder rendering path, e.g. the
    position-disclosure strip - kept as a small intentional duplicate
    here rather than importing app.py, which would be a circular import
    since app.py imports this module)."""
    return html.escape(str(text)).replace("$", "&#36;").replace("~", "&#126;")


def _cp_clean_comment(text):
    """Strip the Excel 'threaded comment' boilerplate down to just what
    Andrew actually wrote, joining a 'Comment:' + any 'Reply:' follow-ups
    into one readable block."""
    if not text:
        return ""
    parts = []
    for chunk in text.split("Reply:"):
        chunk = chunk.strip()
        if chunk.startswith("[Threaded comment]"):
            idx = chunk.find("Comment:")
            chunk = chunk[idx + len("Comment:"):] if idx != -1 else ""
        if chunk.strip():
            parts.append(chunk.strip())
    return "\n\n".join(parts)


def _cp_format(value, fmt):
    if value is None:
        return "N/A"
    if fmt == "pct":
        return f"{value * 100:,.1f}%"
    if fmt == "x":
        return f"{value:,.2f}x"
    if fmt == "cur":
        return f"${value:,.0f}" if abs(value) >= 1000 else f"${value:,.2f}"
    return f"{value:,.2f}"


def _cp_band(value, thresholds):
    if value is None or not thresholds:
        return None
    for lo, hi, color, band_label in thresholds:
        if (lo is None or value >= lo) and (hi is None or value < hi):
            return color, band_label
    return None


def _cp_note(text, size="14px"):
    """Render workbook/engine free text as escaped plain HTML - markdown is
    never parsed, so a stray $, ~, lone-dash line (setext heading!), #, or
    list marker can't restyle the page. Newlines preserved as line breaks."""
    body = _md_safe(text).replace("\n", "<br>")
    st.markdown(
        f"<div style='color:#8aa0b8;font-size:{size};line-height:1.65;"
        f"margin:2px 0 10px;'>{body}</div>",
        unsafe_allow_html=True,
    )


# -----------------------------------
# Section-specific chart builders (moved from app.py, verbatim)
# -----------------------------------

def _cp_price_chart(ticker, price_history):
    """Share price history (~10y monthly) with the 10y average drawn as a
    flat reference line - "price vs the median" chart."""
    entry = price_history.get(ticker)
    if not entry or not entry.get("dates"):
        return None
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=entry["dates"], y=entry["prices"], mode="lines", name="Price",
        line=dict(color="#2dd4bf", width=2),
    ))
    if entry.get("avg_10y") is not None:
        fig.add_hline(
            y=entry["avg_10y"], line_dash="dash", line_color="#e6edf5",
            annotation_text=f"10y Average ${entry['avg_10y']:,.2f}",
            annotation_position="top left", annotation_font_size=11,
        )
    fig.update_layout(
        title="Share Price vs 10-Year Average", height=320, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="Price",
    )
    return fig


def _cp_share_price_growth_chart(ticker, share_price_growth):
    """Share Price Growth by year, same green/red convention as EPS Growth
    by Year on the Earnings Trends tab. A dashed reference line marks the
    average growth across the plotted years (same add_hline convention as
    the 10y-average price chart and the valuation-methods average)."""
    entry = share_price_growth.get(ticker)
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    colors = ["#34d399" if v >= 0 else "#fb7185" for v in values]
    fig = go.Figure(go.Bar(
        x=years, y=values, marker_color=colors,
        text=[f"{v * 100:+.1f}%" for v in values], textposition="outside",
    ))
    if values:
        _avg = sum(values) / len(values)
        # annotation_position="top right" (not "top left"): the leftmost
        # bar is whichever year comes first once reversed into chronological
        # order, and when its growth is close to the average (as it often
        # is) a left-anchored label sits right on top of that bar's own
        # "+N.N%" outside-text, e.g. "Average +22.2%" printing over
        # "+24.2%". The rightmost years are far more often the ones near
        # zero/negative, so anchoring right keeps the average label clear
        # of the bar labels in practice.
        fig.add_hline(
            y=_avg, line_dash="dash", line_color="#e6edf5", line_width=1.5,
            annotation_text=f"Average {_avg * 100:+.1f}%",
            annotation_position="top right",
            annotation_font=dict(size=12, color="#e6edf5"),
        )
    fig.update_layout(
        title="Share Price Growth by Year", height=320, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="Share Price Growth",
        yaxis_tickformat=".0%", xaxis_type="category",
    )
    return fig


def _vc_horizon_sort_key(h):
    """Span-order for Value-Created horizon keys ("1Y", "2Y", "4Y*", "5Y",
    "10Y", "TTM"...) - numeric span ascending, TTM always last. Shared by
    the chart's bar order and render_section's measured-window caption so
    the two always list horizons identically."""
    if h.startswith("TTM"):
        return (999, h)
    m = re.match(r"(\d+)Y", h)
    return (int(m.group(1)) if m else 500, h)


def _cp_value_created_chart(ticker, value_created):
    """Retained-earnings 'Value Created' test at 2Y/5Y/10Y/TTM horizons -
    for every $ of earnings retained, how much market value did that
    create."""
    entry = value_created.get(ticker)
    if not entry:
        return None
    # Horizon keys are dynamic: hand-built data always carries the four
    # plain workbook horizons (2Y/5Y/10Y/TTM); the auto engine emits the
    # same four when statement depth allows, or the honest shallow set
    # (1Y / 2Y / e.g. "4Y*" max-available, starred) when it doesn't - see
    # _value_created in auto_compounder_engine.py. Sort by span, TTM
    # last; "_years_available" is metadata, not a horizon.
    order = sorted([h for h in entry if not h.startswith("_")], key=_vc_horizon_sort_key)
    if not order:
        return None
    retained = [entry[h]["retained_earnings"] for h in order]
    created = [entry[h]["value_created"] for h in order]
    fig = go.Figure()
    fig.add_trace(go.Bar(x=order, y=retained, name="Retained Earnings Per Share", marker_color="#94a3b8"))
    fig.add_trace(go.Bar(x=order, y=created, name="Market Value Created for every dollar retained", marker_color="#2dd4bf"))
    fig.update_layout(
        barmode="group", title="Value Created per $ Retained, by horizon",
        height=320, margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", y=-0.2),
    )
    return fig


def _cp_iv_bv_series_chart(ticker, iv_bv_series, thresholds):
    """IV/BV for every modelled year (X=year, Y=IV/BV) - colour per year
    uses the same red/amber/green bands as the IV/BV metric's thresholds."""
    entry = iv_bv_series.get(ticker)
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    ratios = list(reversed(entry["ratios"]))
    colors = []
    for r in ratios:
        band = _cp_band(r, thresholds) if thresholds else None
        colors.append(_CP_COLOR_FILL.get(band[0], "#2dd4bf") if band else "#2dd4bf")
    fig = go.Figure(go.Bar(
        x=years, y=ratios, marker_color=colors,
        text=[f"{v:.2f}x" for v in ratios], textposition="outside",
    ))
    avg_ratio = sum(ratios) / len(ratios)
    fig.add_hline(
        y=avg_ratio, line_dash="dash", line_color="#e6edf5",
        annotation_text=f"Average {avg_ratio:.2f}x",
        annotation_position="top left", annotation_font_size=11,
    )
    fig.update_layout(
        title="IV/BV by Year Modelled", height=300, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="IV/BV",
        xaxis_type="category",
    )
    return fig


def _cp_year_bar_chart(ticker, series, key, title, yaxis_title, fmt="num", color="#2dd4bf"):
    """Generic X=year bar chart for a year-series (EPS, PE Ratio, ...) - one
    consistent shape reused per metric."""
    entry = series.get(ticker, {}).get(key)
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    text = [_cp_format(v, fmt) for v in values]
    fig = go.Figure(go.Bar(x=years, y=values, marker_color=color, text=text, textposition="outside"))
    fig.update_layout(
        title=title, height=300, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title=yaxis_title,
        xaxis_type="category",
    )
    return fig


def _cp_eps_growth_chart(ticker, series):
    """EPS Growth by year - diverging red/green bars."""
    entry = series.get(ticker, {}).get("eps_growth")
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    colors = ["#34d399" if v >= 0 else "#fb7185" for v in values]
    fig = go.Figure(go.Bar(
        x=years, y=values, marker_color=colors,
        text=[f"{v * 100:+.1f}%" for v in values], textposition="outside",
    ))
    fig.update_layout(
        title="EPS Growth by Year", height=300, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="EPS Growth", yaxis_tickformat=".0%",
        xaxis_type="category",
    )
    return fig


def _cp_fcf_growth_chart(ticker, fcf_growth):
    """FCF Growth by year, Value vs Book tab - same diverging red/green
    bar convention as the Earnings Trends tab's own "EPS Growth by Year"
    chart, just for Free Cash Flow instead of EPS. Plots as many fiscal
    years as the statements have on file, up to a max of 10 (same cap the
    underlying _fcf_growth_entry() already applies)."""
    entry = fcf_growth.get(ticker)
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    colors = ["#34d399" if v >= 0 else "#fb7185" for v in values]
    fig = go.Figure(go.Bar(
        x=years, y=values, marker_color=colors,
        text=[f"{v * 100:+.1f}%" for v in values], textposition="outside",
    ))
    fig.update_layout(
        title="FCF Growth by Year", height=300, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="FCF Growth", yaxis_tickformat=".0%",
        xaxis_type="category",
    )
    return fig


def _cp_book_value_growth_chart(ticker, book_value_growth):
    """Book Value Growth by year, Value vs Book tab - same diverging
    red/green bar convention as the FCF Growth chart right above it on
    this tab, just for (per-share) Book Value instead of FCF. No TTM bar
    here (unlike FCF/EPS Growth) - see _book_value_growth_entry's own
    docstring for why there isn't a distinct TTM point to add for balance
    sheet data."""
    entry = book_value_growth.get(ticker)
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    colors = ["#34d399" if v >= 0 else "#fb7185" for v in values]
    fig = go.Figure(go.Bar(
        x=years, y=values, marker_color=colors,
        text=[f"{v * 100:+.1f}%" for v in values], textposition="outside",
    ))
    fig.update_layout(
        title="Book Value Growth by Year", height=300, showlegend=False,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="Book Value Growth", yaxis_tickformat=".0%",
        xaxis_type="category",
    )
    return fig


def _cp_pe_ratio_chart(ticker, series, pe_ratio_refs):
    """PE Ratio by Year, plus two reference lines (3y-EPS-average P/E and
    the overall average P/E) drawn full-width with a manual add_annotation
    label (not add_hline's own annotation_*, which silently clips)."""
    entry = series.get(ticker, {}).get("pe_ratio")
    if not entry or not entry.get("years"):
        return None
    years = list(reversed(entry["years"]))
    values = list(reversed(entry["values"]))
    fig = go.Figure(go.Bar(
        x=years, y=values, marker_color="#8aa0b8",
        text=[_cp_format(v, "x") for v in values], textposition="outside",
        name="PE Ratio", showlegend=False,
    ))
    refs = pe_ratio_refs.get(ticker, {})
    avg_3y = refs.get("avg_3y")
    overall_avg = refs.get("overall_avg")
    if avg_3y is not None and overall_avg is not None and avg_3y <= overall_avg:
        avg_3y_yanchor, overall_avg_yanchor = "top", "bottom"
    else:
        avg_3y_yanchor, overall_avg_yanchor = "bottom", "top"

    def _cp_pe_ref_line(value, color, dash, label, yanchor):
        if value is None:
            return
        fig.add_shape(
            type="line", xref="x", x0=-0.5, x1=len(years) - 0.5, yref="y",
            y0=value, y1=value, line=dict(color=color, dash=dash, width=2),
        )
        fig.add_annotation(
            xref="paper", x=1.0, xanchor="left", xshift=10,
            yref="y", y=value, yanchor=yanchor,
            text=f"{label}: {value:.2f}x", showarrow=False,
            font=dict(color=color, size=11), align="left",
        )

    _cp_pe_ref_line(avg_3y, "#fb923c", "dash", "3y EPS avg", avg_3y_yanchor)
    _cp_pe_ref_line(overall_avg, "#60a5fa", "dot", "Overall avg", overall_avg_yanchor)
    fig.update_layout(
        title="PE Ratio by Year", height=340, showlegend=False,
        margin=dict(l=10, r=100, t=40, b=10), yaxis_title="PE Ratio",
        xaxis_type="category",
    )
    return fig


def _cp_wacc_roic_period_order(available_periods):
    """TTM first (if present), then every other period newest-year-first.
    Replaces the old hardcoded ["TTM","2025","2021","2016"] list, which
    only ever matched the hand-built Research page's own four fixed
    periods - the auto Compounder View computes WACC/ROIC at whatever
    years its own statement history actually has (see auto_compounder_
    engine._year_points), so the chart needs to sort those dynamically
    instead of silently dropping every period not on that fixed list."""
    rest = sorted(
        (p for p in available_periods if p != "TTM"),
        key=lambda p: int(p) if p.isdigit() else -1,
        reverse=True,
    )
    return (["TTM"] if "TTM" in available_periods else []) + rest


def _cp_wacc_roic_chart(ticker, wacc_roic_series):
    """WACC vs ROIC per period, grouped bar - ROIC > WACC = value creation.
    WACC/ROIC don't always cover the same periods, so both are aligned to a
    fixed period order with gaps (None) rather than assumed to line up."""
    entry = wacc_roic_series.get(ticker)
    if not entry or not entry.get("wacc") or not entry.get("roic"):
        return None
    wacc_by_period = dict(zip(entry["wacc"]["periods"], entry["wacc"]["values"]))
    roic_by_period = dict(zip(entry["roic"]["periods"], entry["roic"]["values"]))
    periods = _cp_wacc_roic_period_order(set(wacc_by_period) | set(roic_by_period))
    if not periods:
        return None
    wacc_vals = [wacc_by_period.get(p) for p in periods]
    roic_vals = [roic_by_period.get(p) for p in periods]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=periods, y=wacc_vals, name="WACC", marker_color="#94a3b8",
        text=[f"{v * 100:.1f}%" if v is not None else "" for v in wacc_vals], textposition="outside",
    ))
    fig.add_trace(go.Bar(
        x=periods, y=roic_vals, name="ROIC", marker_color="#2dd4bf",
        text=[f"{v * 100:.1f}%" if v is not None else "" for v in roic_vals], textposition="outside",
    ))
    fig.update_layout(
        barmode="group", title="WACC vs ROIC by Year", height=320,
        margin=dict(l=10, r=10, t=40, b=10), yaxis_title="Rate", yaxis_tickformat=".0%",
        xaxis_type="category", legend=dict(orientation="h", y=-0.2),
    )
    return fig


def _cp_wacc_buildup_html(ticker, wacc_buildup, base_equity_risk_premium):
    """Workbook schema update (owner, 8 Sep): the "WACC build-up" pill-row
    under the WACC vs ROIC chart, built from Cost of Capital Analysis's
    new AK-AP columns - build_compounder_data.py's own extraction already
    reduced every field to a clean number/text or None (blank cell or
    Excel error string), so this only has to decide, per field, whether
    there's anything to show - never invents a value for a missing one.
    Visual design matches mocks/coc_section_mock.html exactly: a row of
    small pills joined by "+"/"→" glyphs, the Cost of Equity result pill
    picked out in teal, After-tax cost of debt appended plainly at the
    end (it isn't part of the CAPM sum, so no operator before it).

    base_equity_risk_premium is the GLOBAL AO1 assumption (read once,
    same for every company) - the per-row β term reads "β X × equity
    premium Y%" using this global rate, not a per-company one (the
    workbook has no per-company equity-premium column; only AO1 exists).
    Returns None (render nothing) when this ticker has no build-up data
    at all yet."""
    entry = (wacc_buildup or {}).get(ticker)
    if not entry:
        return None

    def _pill(label, value_html, result=False):
        _style = (
            "border-color:#14b8a6;background:#10312d;color:#2dd4bf;"
            if result else "background:#121f36;border-color:#1f3352;color:#c7d2e0;"
        )
        return (
            f"<span style='{_style}border:1px solid;border-radius:8px;"
            f"padding:5px 11px;font-size:12.5px;display:inline-block;'>"
            f"{html.escape(label)} <b style='font-family:ui-monospace,Menlo,monospace;"
            f"color:{'#2dd4bf' if result else '#e6edf5'};'>{value_html}</b></span>"
        )

    _op = "<span style='color:#5b7290;font-weight:700;'>{}</span>"

    pills = []
    if entry.get("market"):
        pills.append(_pill("Market", html.escape(entry["market"])))
    _capm_sum_pills = []
    if entry.get("risk_free_rate") is not None:
        _capm_sum_pills.append(_pill("Risk-free", f"{entry['risk_free_rate'] * 100:.2f}%"))
    if entry.get("country_risk_premium") is not None:
        _capm_sum_pills.append(_pill("Country risk", f"{entry['country_risk_premium'] * 100:.2f}%"))
    if entry.get("beta") is not None:
        _beta_html = f"{entry['beta']:.2f}</b> × equity premium <b style='font-family:ui-monospace,Menlo,monospace;color:#e6edf5;'>"
        if base_equity_risk_premium is not None:
            _capm_sum_pills.append(_pill("β", f"{_beta_html}{base_equity_risk_premium * 100:.1f}%"))
        else:
            _capm_sum_pills.append(_pill("β", f"{entry['beta']:.2f}"))
    for i, p in enumerate(_capm_sum_pills):
        if i > 0:
            pills.append(_op.format("+"))
        pills.append(p)
    if entry.get("cost_of_equity_capm") is not None:
        if _capm_sum_pills:
            pills.append(_op.format("&#8594;"))
        pills.append(_pill("Cost of equity (CAPM)", f"{entry['cost_of_equity_capm'] * 100:.2f}%", result=True))
    if entry.get("after_tax_cost_of_debt") is not None:
        pills.append(_pill("After-tax cost of debt", f"{entry['after_tax_cost_of_debt'] * 100:.2f}%"))

    if not pills:
        return None

    return (
        "<div style='background:#0b1220;border:1px solid #2a3b5c;border-radius:10px;"
        "padding:10px 14px;margin-top:14px;'>"
        "<div style='font-size:12px;color:#2dd4bf;font-weight:700;letter-spacing:.5px;"
        "margin-bottom:7px;'>HOW THE WACC IS BUILT (from the workbook's own inputs)</div>"
        "<div style='display:flex;gap:8px;flex-wrap:wrap;align-items:center;'>"
        + "".join(pills) +
        "</div>"
        "<div style='color:#5b7290;font-size:11.5px;margin-top:10px;line-height:1.6;'>"
        "Blended by the workbook's own weights into the WACC bars above. "
        "Cells that are blank or erroring in the workbook are simply "
        "skipped &mdash; nothing is invented.</div>"
        "</div>"
    )


def _cp_wacc_buildup_assumptions_caption(assumptions):
    """The one caption under the build-up block for the three single-cell
    (not per-company) constants AL1/AO1/AQ1 - each read live from the
    workbook by build_compounder_data.py, never hardcoded here, so this
    function only ever formats whatever came through; per-value skip
    (same rule as the pill-row above) means a still-blank constant simply
    doesn't get its own clause rather than showing a fake number. Matches
    mocks/coc_section_mock.html's own caption wording, including the
    floor's meaning ("the calculated WACC is never applied below this")
    which the instruction specifically asked to carry over."""
    assumptions = assumptions or {}
    clauses = []
    erp = assumptions.get("base_equity_risk_premium")
    if erp is not None:
        clauses.append(f"base equity risk premium <b style='color:#8aa0b8;'>{erp * 100:.1f}%</b>")
    rf = assumptions.get("fallback_risk_free_rate")
    if rf is not None:
        clauses.append(
            f"fallback risk-free <b style='color:#8aa0b8;'>{rf * 100:.1f}%</b> "
            "(country-specific rates used where the General table sets them)"
        )
    floor = assumptions.get("min_discount_rate_floor")
    if floor is not None:
        clauses.append(
            f"discount-rate floor <b style='color:#8aa0b8;'>{floor * 100:.1f}%</b> "
            "&mdash; the calculated WACC is never applied below this"
        )
    if not clauses:
        return None
    return (
        "<div style='color:#5b7290;font-size:11.5px;border-top:1px solid #1f3352;"
        "padding-top:10px;margin-top:14px;line-height:1.6;'>"
        "Assumptions (read live from the workbook): " + " &middot; ".join(clauses) + "."
        "</div>"
    )


# Fair Value bar order/labels/colours - shared between the chart and the
# "inputs under each bar" row so the two line up.
_CP_VALUATION_METHOD_ORDER = [
    ("price", "Current Price", "#2dd4bf"),
    ("pe_forward", "PE Forward", "#94a3b8"),
    ("pe_trailing", "PE Trailing", "#8aa0b8"),
    ("dcf", "DCF (10y FCF)", "#34d399"),
    ("equity_10y", "Rational Compounder Method 10y", "#4cc38a"),
]


def _cp_valuation_methods_chart(ticker, valuation_methods):
    """The 4 intrinsic-value methods vs current price. Returns
    (figure, [(key, label), ...] actually plotted) so the caller can line
    up the "inputs used" row underneath each bar."""
    entry = valuation_methods.get(ticker)
    if not entry:
        return None, []
    labels, values, colors, used = [], [], [], []
    for key, label, color in _CP_VALUATION_METHOD_ORDER:
        if entry.get(key) is not None:
            labels.append(label)
            values.append(entry[key])
            colors.append(color)
            used.append((key, label))
    if not values:
        return None, []
    fig = go.Figure(go.Bar(
        x=labels, y=values, marker_color=colors,
        text=[f"${v:,.2f}" for v in values], textposition="outside",
    ))
    _method_vals = [v for (k, _), v in zip(used, values) if k != "price"]
    if len(_method_vals) >= 2:
        _avg = sum(_method_vals) / len(_method_vals)
        fig.add_hline(
            y=_avg, line_dash="dash", line_color="#e6edf5", line_width=1.5,
            annotation_text=f"Average ${_avg:,.2f}",
            annotation_position="top left",
            annotation_font=dict(size=12, color="#e6edf5"),
        )
    fig.update_layout(
        title="Intrinsic Value by Method vs Current Price", height=340,
        showlegend=False, margin=dict(l=10, r=10, t=40, b=10),
    )
    return fig, used


def _cp_render_valuation_inputs(ticker, used, valuation_inputs, valuation_methods):
    """The key inputs behind each bar, shown directly underneath it (one
    Streamlit column per bar, same left-to-right order as the chart)."""
    entry = valuation_inputs.get(ticker, {})
    method_values = valuation_methods.get(ticker, {})
    cols = st.columns(len(used))
    for col, (key, label) in zip(cols, used):
        with col:
            st.markdown(f"<div style='text-align:center;font-size:12px;font-weight:600;color:#aebfd4;'>{label}</div>", unsafe_allow_html=True)
            lines = [] if key == "price" else [
                (f"Intrinsic Value: {_cp_format(method_values.get(key), 'cur')}", False)
            ]
            for item in entry.get(key, []):
                text = f"{item['label']}: {_cp_format(item['value'], item['format'])}"
                lines.append((text, bool(item.get("flagged"))))
            if lines:
                # Flagged lines (e.g. a growth rate that hit its cap) render
                # red, same "estimates shown in red" convention as the rest
                # of the Compounder View - each line independently, so one
                # capped input doesn't tint the whole block.
                spans = [
                    (f"<span style='color:#fb7185;'>{html.escape(text)}</span>"
                     if flagged else html.escape(text))
                    for text, flagged in lines
                ]
                st.markdown(
                    "<div style='text-align:center;font-size:11.5px;color:#8aa0b8;line-height:1.6;'>"
                    + "<br>".join(spans) + "</div>",
                    unsafe_allow_html=True,
                )


# -----------------------------------
# band_gauge - the approved compact HTML metric card, replacing the old
# plotly bullet gauge (_cp_gauge, formerly in app.py). Renders instantly
# (no plotly figure build/serialize round-trip) and looks identical
# wherever it's called from.
# -----------------------------------

def band_gauge(label, value, fmt, thresholds, flagged=False, comment=None):
    """One metric: name (red * when flagged), a thin colour-banded strip
    with a white marker at the value's position (clamped 2-98% so it's
    always visible even at an extreme reading), the formatted value below
    (red when flagged), the band's verdict word, and a "What this
    measures" expander with the cleaned comment/definition."""
    thresholds = thresholds or []
    breakpoints = sorted({b for t in thresholds for b in (t[0], t[1]) if b is not None})
    lo_bound = breakpoints[0] if breakpoints else 0.0
    hi_bound = breakpoints[-1] if breakpoints else 1.0
    span = (hi_bound - lo_bound) or (abs(hi_bound) or 1.0)
    pad = span * 0.2
    axis_min, axis_max = lo_bound - pad, hi_bound + pad
    if value is not None:
        if value < axis_min:
            axis_min = value - span * 0.1
        if value > axis_max:
            axis_max = value + span * 0.1

    def _pct(v):
        if axis_max == axis_min:
            return 50.0
        return max(0.0, min(100.0, (v - axis_min) / (axis_max - axis_min) * 100))

    segments = []
    for lo, hi, color, _blabel in thresholds:
        seg_lo = _pct(lo if lo is not None else axis_min)
        seg_hi = _pct(hi if hi is not None else axis_max)
        if seg_hi <= seg_lo:
            continue
        segments.append(
            f"<div style='position:absolute;left:{seg_lo:.2f}%;width:{seg_hi - seg_lo:.2f}%;"
            f"top:0;bottom:0;background:{_CP_COLOR_FILL.get(color, '#26334a')};'></div>"
        )

    marker_pct = max(2.0, min(98.0, _pct(value))) if value is not None else 50.0
    band = _cp_band(value, thresholds)
    band_color, band_label = band if band else (None, None)
    value_color = "#fb7185" if flagged else "#e6edf5"
    star = " <span style='color:#fb7185;'>*</span>" if flagged else ""
    verdict_html = (
        f"<span style='font-size:12px;font-weight:600;"
        f"color:{_CP_COLOR_TEXT.get(band_color, '#aebfd4')};'>{html.escape(band_label)}</span>"
        if band_label else "<span></span>"
    )

    st.markdown(
        "<div style='margin-bottom:2px;'>"
        f"<div style='font-size:13px;font-weight:600;color:#aebfd4;'>{html.escape(str(label))}{star}</div>"
        "<div style='position:relative;height:10px;border-radius:5px;overflow:hidden;"
        f"background:#1a2740;margin:6px 0 5px;'>{''.join(segments)}"
        f"<div style='position:absolute;left:{marker_pct:.2f}%;top:-1px;bottom:-1px;"
        "width:3px;margin-left:-1.5px;background:#ffffff;'></div></div>"
        "<div style='display:flex;justify-content:space-between;align-items:baseline;'>"
        "<span style='font-family:ui-monospace,Menlo,SFMono-Regular,monospace;"
        f"font-size:14px;font-weight:700;color:{value_color};'>{_cp_format(value, fmt)}</span>"
        f"{verdict_html}</div></div>",
        unsafe_allow_html=True,
    )
    with st.expander("What this measures", expanded=False):
        _cp_note(_cp_clean_comment(comment or ""), size="12.5px")


# -----------------------------------
# render_section - the shared per-section renderer both callers use.
# -----------------------------------

def render_section(sections, ticker, section_label, gate=None, lang="en"):
    """
    Renders one COMPUTED section's full content (section-specific charts,
    then the colour-coded band-gauge grid, then the plain "other metrics"
    grid) for `ticker`.

    sections: the {"Fundamentals": {...}, "Value vs Book": {...}, ...}
        dict - either compounder_data.json's own "sections" (hand-built),
        or auto_compounder_engine.build_sections()'s return value (live) -
        both share the exact same shape, so either works unmodified.
    section_label: one of "Fundamentals", "Value vs Book",
        "Retained Earnings", "Earnings Trends", "Cost of Capital",
        "Fair Value". ("Company Potential" is NOT handled here - see the
        module docstring.)
    gate: optional (title, teaser, key_prefix) - when given, the section is
        wrapped in paywall_engine.render_gate() first (used for Fair Value,
        which is paywalled on the Research page today; passing the same
        gate from the Deep Dive auto view keeps that consistent instead of
        accidentally giving the paywalled section away for free there).
        Returns without rendering anything if the gate isn't unlocked.
    lang: "en"/"es" (Español instruction, Part 1) - selects the "why this
        matters" caption's language (falls back to the EN caption when
        this section_label has no ES entry yet) and is passed straight
        through to render_gate() for its own fixed chrome.
    """
    section = (sections or {}).get(section_label)
    if not section:
        st.warning(f"No data yet for {ticker} in {section_label}.")
        return

    # Simple view, Part 4: one-line "why this matters" caption, shown in
    # BOTH views regardless of subscription - static template text, see
    # simple_view_copy.py's own module docstring for the full rationale.
    _why = (SECTION_WHY_CAPTIONS_ES.get(section_label) if lang == "es" else None) \
        or SECTION_WHY_CAPTIONS.get(section_label)
    if _why:
        st.caption(_why)

    if gate:
        import paywall_engine
        title, teaser, key_prefix = gate
        if not paywall_engine.render_gate(title, teaser=teaser, key_prefix=key_prefix, lang=lang):
            return

    metrics = section.get("metrics", [])
    share_price_growth_fig = None

    if section_label == "Fundamentals":
        fig = _cp_price_chart(ticker, section.get("price_history", {}))
        if fig:
            sdd_plotly_chart(fig, config={"displayModeBar": False})
        share_price_growth_fig = _cp_share_price_growth_chart(ticker, section.get("share_price_growth", {}))
    elif section_label == "Value vs Book":
        ap_metric = next((m for m in metrics if m["key"] == "AP"), None)
        fig2 = _cp_iv_bv_series_chart(
            ticker, section.get("iv_bv_series", {}),
            ap_metric["thresholds"] if ap_metric else None,
        )
        if fig2:
            sdd_plotly_chart(fig2, config={"displayModeBar": False})
            # Same statement-depth honesty as the Retained Earnings chart:
            # the IV/BV series only ever plots years the statements
            # actually cover, so when that's fewer than the workbook's
            # ~10, say so. Hand-built data always carries the full run,
            # so this caption never shows on the Research page.
            _ivbv_entry = section.get("iv_bv_series", {}).get(ticker) or {}
            _ivbv_years = [y for y in (_ivbv_entry.get("years") or []) if y != "TTM"]
            if _ivbv_years and len(_ivbv_years) < 10:
                st.caption(
                    f"IV/BV modelled for the {len(_ivbv_years)} year(s) of "
                    "statement history available (plus TTM) - the full "
                    "10-year series unlocks as deeper statement history "
                    "becomes available."
                )
        fig3 = _cp_fcf_growth_chart(ticker, section.get("fcf_growth", {}))
        if fig3:
            sdd_plotly_chart(fig3, config={"displayModeBar": False})
            _fcfg_entry = section.get("fcf_growth", {}).get(ticker) or {}
            _fcfg_years = _fcfg_entry.get("years") or []
            if _fcfg_years and len(_fcfg_years) < 9:
                st.caption(
                    f"FCF Growth plotted for the {len(_fcfg_years)} year(s) of "
                    "statement history available - the full 9-bar (10-year) "
                    "series unlocks as deeper statement history becomes "
                    "available."
                )
        fig4 = _cp_book_value_growth_chart(ticker, section.get("book_value_growth", {}))
        if fig4:
            sdd_plotly_chart(fig4, config={"displayModeBar": False})
            _bvg_entry = section.get("book_value_growth", {}).get(ticker) or {}
            _bvg_years = _bvg_entry.get("years") or []
            if _bvg_years and len(_bvg_years) < 9:
                st.caption(
                    f"Book Value Growth plotted for the {len(_bvg_years)} year(s) "
                    "of statement history available - the full 9-bar (10-year) "
                    "series unlocks as deeper statement history becomes "
                    "available."
                )
        metrics = [m for m in metrics if m["key"] != "AP"]
    elif section_label == "Retained Earnings":
        vc = section.get("value_created", {})
        fig = _cp_value_created_chart(ticker, vc)
        if fig:
            sdd_plotly_chart(fig, config={"displayModeBar": False})
            # A starred bar is the auto engine's max-available cumulative
            # horizon (statement depth too shallow for the workbook's full
            # 5Y/10Y/TTM windows) - say exactly what it covers. Hand-built
            # data never carries starred keys, so this caption never shows
            # on the Research page.
            _vc_entry = vc.get(ticker) or {}
            # "Measured over" line: each auto-engine horizon carries a
            # "window" string ("Jun 2024 -> Jun 2025", "Jun 2022 -> today")
            # stating exactly which two dates its price change spans.
            # Hand-built data has no window strings (the workbook's own
            # market-cap snapshots come from its data provider), so this
            # never shows on the Research page.
            _vc_windows = [
                f"{h} = {_vc_entry[h]['window']}"
                for h in sorted(
                    [k for k in _vc_entry if not k.startswith("_")],
                    key=_vc_horizon_sort_key,
                )
                if isinstance(_vc_entry.get(h), dict) and _vc_entry[h].get("window")
            ]
            if _vc_windows:
                st.caption("Measured over: " + "   \u00b7   ".join(_vc_windows))
            if any(h.endswith("*") for h in _vc_entry):
                _vc_years = _vc_entry.get("_years_available")
                _vc_span = f"all {_vc_years} year(s)" if _vc_years else "all years"
                st.caption(
                    f"* cumulative over {_vc_span} of statement history "
                    "available (plus the trailing twelve months) - the "
                    "full 5Y / 10Y / TTM horizons unlock as deeper "
                    "statement history becomes available."
                )
    elif section_label == "Earnings Trends":
        series = section.get("series", {})
        fig1 = _cp_year_bar_chart(ticker, series, "eps", "EPS by Year", "EPS", fmt="cur")
        fig2 = _cp_eps_growth_chart(ticker, series)
        fig3 = _cp_pe_ratio_chart(ticker, series, section.get("pe_ratio_refs", {}))
        chart_cols = st.columns(3)
        for col, fig in zip(chart_cols, [fig1, fig2, fig3]):
            with col:
                if fig:
                    sdd_plotly_chart(fig, config={"displayModeBar": False})
    elif section_label == "Cost of Capital":
        fig = _cp_wacc_roic_chart(ticker, section.get("wacc_roic_series", {}))
        if fig:
            sdd_plotly_chart(fig, config={"displayModeBar": False})
        # Workbook schema update (owner, 8 Sep): the new WACC build-up
        # pill-row + assumptions caption, both gracefully no-ops when the
        # underlying data isn't there yet (hand-built workbook only -
        # auto_compounder_engine.build_sections() never sets these keys,
        # so the Deep Dive page's "Compounder View (auto)" simply shows
        # nothing extra here, exactly as before this change).
        _wacc_assumptions = section.get("wacc_buildup_assumptions", {})
        _wacc_buildup_html = _cp_wacc_buildup_html(
            ticker, section.get("wacc_buildup", {}),
            _wacc_assumptions.get("base_equity_risk_premium"),
        )
        if _wacc_buildup_html:
            st.markdown(_wacc_buildup_html, unsafe_allow_html=True)
            _wacc_caption_html = _cp_wacc_buildup_assumptions_caption(_wacc_assumptions)
            if _wacc_caption_html:
                st.markdown(_wacc_caption_html, unsafe_allow_html=True)
    elif section_label == "Fair Value":
        fig, used = _cp_valuation_methods_chart(ticker, section.get("valuation_methods", {}))
        if fig:
            sdd_plotly_chart(fig, config={"displayModeBar": False})
            _cp_render_valuation_inputs(
                ticker, used, section.get("valuation_inputs", {}), section.get("valuation_methods", {})
            )
        else:
            st.warning(f"No valuation data yet for {ticker}.")
        # Fair Value shows ONLY the 4 methods, same as before the refactor -
        # no metrics grid underneath.
        return

    colored = [m for m in metrics if m.get("thresholds") and m["values"].get(ticker) is not None]
    plain = [m for m in metrics if not (m.get("thresholds") and m["values"].get(ticker) is not None)]

    if colored:
        st.markdown("##### Colour-coded (against your own thresholds)")
        if any(m.get("flagged") for m in colored):
            # The "*" matches band_gauge's own flagged-star color (#fb7185)
            # so the legend's asterisk looks like the one it's describing,
            # instead of rendering in the caption's default muted grey.
            st.caption(
                "Red name + red value + <span style='color:#fb7185;'>*</span> "
                "= estimated — rests on a fallback assumption or incomplete "
                "statement data, not a reported figure. See “What this "
                "measures” below for detail.",
                unsafe_allow_html=True,
            )
        cols = st.columns(3)
        for i, m in enumerate(colored):
            value = m["values"][ticker]
            with cols[i % 3]:
                band_gauge(
                    m["label"], value, m["format"], m["thresholds"],
                    flagged=bool(m.get("flagged")), comment=m.get("comment"),
                )

    if plain:
        st.markdown("##### Other metrics")
        cols = st.columns(4)
        for i, m in enumerate(plain):
            value = m["values"].get(ticker)
            # "Other metrics" (no color thresholds -> plain st.metric, not
            # band_gauge) used to drop the engine's flagged/estimated
            # signal entirely - band_gauge shows a red asterisk + red
            # value for a flagged metric, but a metric with no thresholds
            # never reached band_gauge at all, so e.g. Retained Earnings
            # (TTM) and Cost of Capital's ROIC (TTM)/WACC/Total
            # Investments (TTM), which the engine flags as estimates every
            # single time, showed with no indicator whatsoever. Match the
            # same "red asterisk on the label" convention here via
            # st.metric's own limited-markdown label support (no
            # unsafe_allow_html needed), plus a hover tooltip via `help`
            # spelling out why - so the flag actually reaches the user
            # regardless of which of the two metric styles it renders
            # through. Hand-built workbook data never sets flagged=True,
            # so this is a no-op on the Research page.
            flagged = bool(m.get("flagged"))
            label = f"{m['label']} :red[*]" if flagged else m["label"]
            help_text = (
                "Estimated - rests on a fallback assumption or incomplete "
                "statement data. See “What this measures” below."
                if flagged else None
            )
            with cols[i % 4]:
                st.metric(label, _cp_format(value, m["format"]), help=help_text)
                with st.expander("What this measures", expanded=False):
                    _cp_note(_cp_clean_comment(m["comment"]), size="12.5px")

    if not colored and not plain:
        st.warning(f"No data yet for {ticker} in {section_label}.")

    if share_price_growth_fig:
        sdd_plotly_chart(share_price_growth_fig, config={"displayModeBar": False})
        _spg_entry = section.get("share_price_growth", {}).get(ticker) or {}
        _ytd_year = _spg_entry.get("ytd_year")
        if _ytd_year:
            st.caption(
                f"{_ytd_year} is still in progress, so that bar is a plain "
                f"start-of-year vs latest-price return - every other bar is "
                f"that year's average price vs the year before's, the "
                f"workbook's own convention. The two aren't computed the "
                f"same way; a still-open year averaged against a closed one "
                f"can otherwise look far more extreme than the stock's "
                f"actual year-to-date move."
            )


# -----------------------------------
# News tab (📰) - added to the Rational Compounder tab row, page-level
# (not workbook data: no compounder_data.json / auto_compounder_engine
# entry exists or is needed for it, unlike every other section this
# module renders). Shared by BOTH callers - render_tabs() below (the
# Deep Dive page's auto Compounder View) auto-inserts it, and the
# hand-covered Research page's own tab loop (app.py's
# _render_research_detail) calls render_news_tab() directly, since that
# page can't route through render_tabs() itself (it also interleaves
# "Company Potential", which has no compounder_ui.py-computed-section
# equivalent at all - see this module's own top docstring). Either way
# it's the exact same function producing the exact same component.
#
# Data: the SAME News Intelligence feeds + severity classifier
# Portfolio Health already reads - portfolio_news_engine.
# analyze_holding_news(), the exact same function app.py's
# _research_note_news_text() already calls for a non-portfolio ticker.
# No new engine, no new provider: every headline here was already being
# fetched/classified by that module; this section only adds a
# presentation-layer mapping from its 5-way SEVERITY read (noise /
# temporary / material / thesis-breaking / positive) onto the 5-level
# TONE vocabulary below, plus the dial/grouped-feed rendering.
# -----------------------------------

NEWS_TAB_LABEL = "\U0001F4F0 News"  # "📰 News" - EN fallback/default only;
# the actual tab text is always news_tab_label(lang) below (EN/ES via
# compounder.news.tab_label), never this bare constant directly.


def news_tab_label(lang="en"):
    """The 📰 News tab's own localized display text (EN/ES) - a function,
    not a constant, since the tab bar itself must show "📰 Noticias" for
    an ES visitor, not the English label with translated content behind
    it. Both render_tabs() and the Research page's own tab loop
    (app.py's _render_research_detail) use this SAME function for both
    building the tab label AND identifying which tab is News inside
    their render loop, so the two can never drift out of sync with each
    other."""
    return i18n.t("compounder.news.tab_label", lang)

_NEWS_WINDOW_DAYS = 30
_NEWS_MAX_HEADLINES = 30

# portfolio_news_engine._classify_severity()'s own 5 categories, mapped
# onto a -2..+2 tone value - reused as-is, never reclassified further
# (see _news_info_text() below for the honest, in-tab statement of this
# rule, and this module's own top-of-section comment for why this isn't
# "a new engine"). thesis-breaking is the classifier's own worst
# category -> -2; material and temporary (two different magnitudes of
# "a real negative issue" in that classifier's own severity
# definitions) both land on -1, the single "leans negative" step - the
# classifier draws no finer line between them than that; noise (no
# polarity either way) -> 0; positive (the classifier's only "good
# news" category) -> +2.
_TONE_VALUE_BY_SEVERITY = {
    "thesis-breaking": -2.0,
    "material": -1.0,
    "temporary": -1.0,
    "noise": 0.0,
    "positive": 2.0,
}

# (background, border, text) - lifted verbatim from the approved
# compounder_news_tab_mock.html (.tneg / .tln / .tn / .tlp / .tp).
_TONE_CHIP_COLORS = {
    "negative": ("#331419", "#7f1d1d", "#fb7185"),
    "leans_negative": ("#2b1c22", "#59303c", "#f5a8b3"),
    "neutral": ("#1a2740", "#2a3b5c", "#8aa0b8"),
    "leans_positive": ("#132b1e", "#1d4436", "#7fd6a8"),
    "positive": ("#10312d", "#14532d", "#34d399"),
}

# Dial zone colors, same mock, left (most negative) -> right (most
# positive).
_DIAL_ZONE_COLORS = {
    "negative": "#7f1d1d", "leans_negative": "#59303c", "neutral": "#2a3b5c",
    "leans_positive": "#1d6a4c", "positive": "#34d399",
}

# The three feed groups the task spec calls for: Positive folds in
# leans-positive, Negative folds in leans-negative, Neutral stands
# alone. Rendered in this order (best news first, same as the mock's
# own "Leaning positive" example group).
_GROUP_BY_LEVEL = {
    "positive": "positive", "leans_positive": "positive",
    "neutral": "neutral",
    "negative": "negative", "leans_negative": "negative",
}
_GROUP_ORDER = ["positive", "neutral", "negative"]


def _news_tone_value(severity):
    return _TONE_VALUE_BY_SEVERITY.get(severity, 0.0)


def _news_tone_level(value):
    """Buckets a -2..+2 tone value onto the 5-level vocabulary -
    boundaries evenly spaced at the halfway points between the 5 fixed
    per-severity values above (-2 / -1 / 0 / +1 / +2), so a single
    headline's own value always lands exactly on its "home" level, and
    the aggregate 30-day average (see render_news_tab() below) lands
    wherever the actual mix of headlines pulls it - which is the only
    way "Leans positive" / "Leans negative" are ever reached, since no
    single headline's own value sits between two of the fixed points."""
    if value <= -1.5:
        return "negative"
    if value <= -0.5:
        return "leans_negative"
    if value < 0.5:
        return "neutral"
    if value < 1.5:
        return "leans_positive"
    return "positive"


def _news_company_name(ticker):
    """Local, no-network company name lookup for relevance-matching in
    the news fetch below - same source _rc_company_name() (app.py) uses
    for the Research shelf/header (the nightly scan's own snapshot
    cache), read directly here instead of importing app.py (which
    imports THIS module - importing back would be circular). Returns
    None if this ticker hasn't been through a nightly scan yet;
    analyze_holding_news() below already degrades gracefully to
    ticker-root-only relevance matching in that case."""
    try:
        import snapshot_store
        snap = snapshot_store.get_snapshot(ticker)
        if not snap:
            return None
        return snapshot_store.public_view(snap.get("data") or {}).get("company_name")
    except Exception:
        return None


@st.cache_data(ttl=1800, show_spinner=False)
def _news_tab_fetch(ticker, name):
    """Cached (30-min TTL, same convention as app.py's get_price_history
    / get_ticker_info / get_cashflow_df) per-ticker news fetch for the
    News tab - so a tab switch or any other widget interaction on this
    page never re-fetches or re-classifies for the same ticker within
    the TTL window; a cache hit costs nothing.

    Calls portfolio_news_engine.analyze_holding_news() - the SAME News
    Intelligence feeds + severity classifier Portfolio Health already
    reads - with no thesis_drivers (a Compounder ticker isn't
    necessarily a held position with a thesis on file) and a `buy_date`
    set to _NEWS_WINDOW_DAYS+3 days ago purely to bound the underlying
    fetch window to roughly what this tab needs (that function's own
    "keep events back to buy_date - 3 days" rule - see its docstring),
    not to claim any purchase date. analyze_holding_news() has its own
    internal REFETCH_STALE_HOURS re-fetch guard UNDER this cache (keyed
    on ticker alone, shared with every other caller of that function,
    portfolio holding or not) - this decorator's job is purely to stop
    even the classification pass / stale-check DB read from repeating
    on every rerun of the same session.

    Returns a list of up to _NEWS_MAX_HEADLINES dicts
    {date (a naive datetime or None), title, publisher, link, severity},
    newest first, restricted to headlines analyze_holding_news() judged
    RELEVANT to this company and dated within the last _NEWS_WINDOW_DAYS
    days. Never raises - any failure (missing dependency, feed error,
    bad data) returns [], which render_news_tab() below renders as the
    fail-soft "No recent headlines found" line rather than breaking the
    tab."""
    try:
        import portfolio_news_engine
    except Exception:
        return []
    now = _dt.datetime.utcnow()
    buy_date = (now - _dt.timedelta(days=_NEWS_WINDOW_DAYS + 3)).strftime("%Y-%m-%d")
    try:
        result = portfolio_news_engine.analyze_holding_news(
            ticker, name=name, buy_date=buy_date, now=now,
        )
    except Exception:
        return []
    events = (result or {}).get("timeline") or []
    cutoff = now - _dt.timedelta(days=_NEWS_WINDOW_DAYS)
    out = []
    for e in events:
        if not e.get("relevant"):
            continue
        d = e.get("date")
        if d is not None and d < cutoff:
            continue
        out.append({
            "date": d, "title": (e.get("title") or "").strip(),
            "publisher": e.get("publisher") or e.get("source") or "",
            "link": e.get("link") or "", "severity": e.get("severity") or "noise",
        })
    out.sort(key=lambda e: e["date"] or _dt.datetime.min, reverse=True)
    return out[:_NEWS_MAX_HEADLINES]


def _news_chip_html(level, label):
    bg, border, text = _TONE_CHIP_COLORS[level]
    return (
        '<span style="display:inline-block;border-radius:999px;padding:2px 10px;'
        'font-size:10.5px;font-weight:700;white-space:nowrap;'
        f'background:{bg};border:1px solid {border};color:{text};">'
        f'{html.escape(label)}</span>'
    )


def _news_item_html(item, chip_html, lang):
    title = html.escape(item["title"])
    publisher = html.escape(item["publisher"])
    date_label = i18n.format_date_dm(item["date"], lang) if item["date"] else ""
    link = item["link"]
    title_html = (
        f'<a href="{html.escape(link, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer" style="color:#e6edf5;text-decoration:none;">'
        f'{title}</a>'
        if link else f'<span style="color:#e6edf5;">{title}</span>'
    )
    return (
        '<div style="display:flex;gap:12px;align-items:flex-start;padding:9px 0;'
        'border-bottom:1px solid #141f36;font-size:13px;">'
        f'<span style="color:#5b7290;font-size:11px;white-space:nowrap;width:56px;">'
        f'{html.escape(date_label)}</span>'
        f'<div style="flex:1;">{title_html} '
        f'<span style="color:#5b7290;font-size:11px;">· {publisher}</span></div>'
        f'{chip_html}</div>'
    )


_GROUP_HEAD_COLOR = {"positive": "#34d399", "neutral": "#8aa0b8", "negative": "#fb7185"}


def _news_dial_svg(value, level_label):
    """Inline SVG 5-zone dial - arc paths lifted verbatim from the
    approved mock (fixed geometry, a semicircle centred on (110,110),
    radius 85, sweeping from 180deg at the left/most-negative end to
    0deg at the right/most-positive end) - with a needle drawn at
    `value` (-2..+2, clipped) and the "NEWS TONE · 30d · <level>" label
    underneath, already localized by the caller. Never renders the word
    "score" anywhere - see this module's news-tab section comment."""
    v = max(-2.0, min(2.0, value))
    theta = math.radians(90 - 45 * v)
    cx, cy, needle_len = 110, 108, 76
    x2 = cx + needle_len * math.cos(theta)
    y2 = cy - needle_len * math.sin(theta)
    return (
        '<svg viewBox="0 0 220 130" width="200" role="img" '
        f'aria-label="{html.escape(level_label)}">'
        '<path d="M 25 110 A 85 85 0 0 1 59 42" fill="none" '
        f'stroke="{_DIAL_ZONE_COLORS["negative"]}" stroke-width="13" stroke-linecap="round"/>'
        '<path d="M 66 36 A 85 85 0 0 1 100 26" fill="none" '
        f'stroke="{_DIAL_ZONE_COLORS["leans_negative"]}" stroke-width="13"/>'
        '<path d="M 108 25 A 85 85 0 0 1 140 32" fill="none" '
        f'stroke="{_DIAL_ZONE_COLORS["neutral"]}" stroke-width="13"/>'
        '<path d="M 148 36 A 85 85 0 0 1 172 55" fill="none" '
        f'stroke="{_DIAL_ZONE_COLORS["leans_positive"]}" stroke-width="13"/>'
        '<path d="M 178 62 A 85 85 0 0 1 195 110" fill="none" '
        f'stroke="{_DIAL_ZONE_COLORS["positive"]}" stroke-width="13" stroke-linecap="round"/>'
        f'<line x1="{cx}" y1="{cy}" x2="{x2:.1f}" y2="{y2:.1f}" '
        'stroke="#e6edf5" stroke-width="3"/>'
        '<text x="110" y="125" fill="#8aa0b8" font-size="10" text-anchor="middle" '
        f'font-family="Segoe UI">{html.escape(level_label)}</text>'
        '</svg>'
    )


def render_news_tab(ticker, lang="en"):
    """Renders the 📰 News tab's full content for `ticker`: a 5-zone
    tone dial at the 30-day balance, the honesty disclaimer directly
    beneath it, then the headline feed grouped under three heads
    (Positive/Neutral/Negative). Fail-soft throughout - a feed error or
    zero headlines renders only the "No recent headlines" line and
    nothing else breaks; this function never raises."""
    _t = lambda key, **fmt: i18n.t(f"compounder.news.{key}", lang, **fmt)

    name = _news_company_name(ticker)
    try:
        items = _news_tab_fetch(ticker, name)
    except Exception:
        items = []

    if not items:
        st.info(_t("no_headlines", ticker=ticker))
        return

    scored = [(it, _news_tone_value(it["severity"])) for it in items]
    balance = sum(v for _, v in scored) / len(scored)
    level = _news_tone_level(balance)
    level_word = _t(f"tone_{level}").lower()

    dial_col, info_col = st.columns([5, 1])
    with dial_col:
        st.markdown(
            '<div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap;">'
            + _news_dial_svg(balance, f'{_t("dial_prefix")} {level_word}')
            + '<div style="font-size:12.5px;color:#8aa0b8;">'
            + html.escape(_t("headline_count", n=len(items)))
            + '</div></div>',
            unsafe_allow_html=True,
        )
    with info_col:
        with st.popover(_t("info_button")):
            st.markdown(_t("info_text"))

    st.markdown(
        f'<div style="color:#8aa0b8;font-size:11.5px;margin-top:8px;">'
        f'{html.escape(_t("disclaimer"))}</div>',
        unsafe_allow_html=True,
    )

    groups = {g: [] for g in _GROUP_ORDER}
    for it, v in scored:
        item_level = _news_tone_level(v)
        groups[_GROUP_BY_LEVEL[item_level]].append((it, item_level))

    feed_html = ['<div style="margin-top:14px;">']
    for group in _GROUP_ORDER:
        group_items = groups[group]
        if not group_items:
            continue
        feed_html.append(
            f'<div style="font-weight:700;font-size:13px;margin:14px 0 4px;'
            f'color:{_GROUP_HEAD_COLOR[group]};">'
            f'{html.escape(_t(f"group_{group}"))} ({len(group_items)})</div>'
        )
        for it, item_level in group_items:
            chip = _news_chip_html(item_level, _t(f"tone_{item_level}"))
            feed_html.append(_news_item_html(it, chip, lang))
    feed_html.append('</div>')
    st.markdown("".join(feed_html), unsafe_allow_html=True)


def with_news_tab(section_order, lang="en"):
    """`section_order` with the News tab's own localized label
    (news_tab_label(lang) above) inserted immediately after "Fair
    Value" (or appended at the end if "Fair Value" isn't present) -
    shared by render_tabs() below and the hand-covered Research page's
    own tab loop (app.py's _render_research_detail), so News always
    lands in the same place relative to the six computed sections on
    both views. `section_order` itself is returned unmodified elsewhere
    (section counts, ?section= deep-linking, etc.) - only the list
    actually used to build tab labels/tabs needs News added to it."""
    out = list(section_order)
    label = news_tab_label(lang)
    if "Fair Value" in out:
        out.insert(out.index("Fair Value") + 1, label)
    else:
        out.append(label)
    return out


def trading_cost_tab_label(lang="en"):
    """The 💱 Trading Cost tab's own localized display text - same
    "function, not a constant" reasoning as news_tab_label() above (both
    render_tabs() and the Research page's own tab loop use this SAME
    function to build the label AND identify the tab in their render
    loop). Only meaningful when TRADING_COST_ENABLED - see with_
    trading_cost_tab() below, the one place that actually decides
    whether the tab exists at all."""
    return i18n.t("compounder.trading_cost.tab_label", lang)


def with_trading_cost_tab(section_order, lang="en"):
    """`section_order` (already including News, via with_news_tab()
    above) with the Trading Cost tab's own label inserted immediately
    after News - or returned UNCHANGED when TRADING_COST_ENABLED is
    off, so the tab bar is byte-identical to today's on every page for
    every visitor until the owner turns the switch on. Shared by
    render_tabs() below and the Research page's own tab loop, same
    "News always lands in the same place on both views" reasoning
    with_news_tab() itself already documents."""
    if not TRADING_COST_ENABLED:
        return section_order
    out = list(section_order)
    news_label = news_tab_label(lang)
    label = trading_cost_tab_label(lang)
    if news_label in out:
        out.insert(out.index(news_label) + 1, label)
    else:
        out.append(label)
    return out


def render_tabs(sections, ticker, section_order, key_prefix, gates=None, lang="en",
                 price_history=None):
    """Renders `st.tabs(section_order)` and calls render_section() in each
    tab - the same navigation mechanism the Research page already uses
    (see page_research()), reused as-is for the Deep Dive auto view so
    both callers share one nav mechanism, not just one section renderer.
    Always appends a 📰 News tab after "Fair Value" (see with_news_tab()
    above) - page-level content, not part of `sections`, rendered via
    render_news_tab() instead of render_section(). A 💱 Trading Cost tab
    follows News (see with_trading_cost_tab() above) whenever
    TRADING_COST_ENABLED is on.

    gates: optional {section_label: (title, teaser, key_prefix)} - only
        the sections present here are gated; every other section renders
        openly. Never applies to the News or Trading Cost tabs - neither
        is gated.
    lang: "en"/"es" (Español instruction, Part 1) - passed straight
        through to each tab's render_section() call, and used to pick
        the News/Trading Cost tabs' own localized labels.
    price_history: Trading Cost tab's own [{"date","high","low","close",
        "volume"}, ...] for `ticker` (the caller's own get_price_history()
        cache, converted - see render_trading_cost_tab()'s own docstring
        for why this module never fetches it itself). None is fine when
        the tab is off or the caller has nothing to pass - render_
        trading_cost_tab() treats it exactly like an empty list.
    """
    gates = gates or {}
    tab_labels = with_trading_cost_tab(with_news_tab(section_order, lang=lang), lang=lang)
    news_label = news_tab_label(lang)
    tc_label = trading_cost_tab_label(lang)
    tabs = st.tabs(tab_labels, key=key_prefix)
    for label, tab in zip(tab_labels, tabs):
        with tab:
            if label == news_label:
                render_news_tab(ticker, lang=lang)
            elif label == tc_label:
                render_trading_cost_tab(ticker, price_history, lang=lang)
            else:
                render_section(sections, ticker, label, gate=gates.get(label), lang=lang)


# -----------------------------------
# 💱 Trading Cost tab (Commit 3) - see TRADING_COST_ENABLED/with_
# trading_cost_tab() above for the on/off switch and tab placement.
# -----------------------------------

def _currency_symbol_for_ticker(ticker):
    """"A$" for an ASX ticker (".AX" suffix), "US$" otherwise - the
    Trading Cost tab serves both AUD (ASX) and USD (US) tickers, and a
    bare "$" would be ambiguous rather than informative (same reasoning
    _fmt_value_traded() below already applies to its own figure). Same
    ticker-suffix convention _market_tz_for_ticker() below already
    uses - never a second, independently-derived market check."""
    return "A$" if ticker.upper().endswith(".AX") else "US$"


def _fmt_money(value, symbol, decimals=2):
    """"{symbol}12,345.67" - "-" for None. Used throughout the
    redesigned tab's cost-to-cross cards and bid/ask rail/tiles, always
    with the caller's own `symbol` from _currency_symbol_for_ticker()
    above, never a hardcoded "$"."""
    return f"{symbol}{value:,.{decimals}f}" if value is not None else "—"


def _fmt_pct1(value):
    """"2.4%" (one decimal, this tab's own display precision) - "-" for
    None. _fmt_pct_diag() below stays a separate, higher-precision
    (3dp) formatter for the owner-only diagnostics expander; this one
    is for the visitor-facing meters/tiles."""
    return f"{value:.1f}%" if value is not None else "—"


def _fmt_signed_pt(value):
    """"+0.3"/"-1.2"/"0.0" - always signed, one decimal - the header
    meter's own "tracking {offset}pt vs measured" line. None -> None
    (caller skips the line entirely rather than showing a meaningless
    "tracking nonept")."""
    return f"{value:+.1f}" if value is not None else None


# The "Bid & ask now" rail/chip's own classification word+colour -
# trading_cost_engine.now_spread_classification()'s tight/noticeable/
# wide (see that function's own docstring for why this is a SECOND,
# deliberately different scale from spread_band()/band_gauge above).
_NOW_CHIP_STYLE = {
    trading_cost_engine.NOW_TIGHT: ("#34d399", "#10312d", "#14532d", "chip_tight"),
    trading_cost_engine.NOW_NOTICEABLE: ("#fbbf24", "#2a2413", "#7f5a14", "chip_noticeable"),
    trading_cost_engine.NOW_WIDE: ("#fb7185", "#2d1420", "#7f1d3a", "chip_wide"),
}


def _now_chip_html(classification, spread_pct, lang):
    """The rail's classification chip - colour AND a written word
    together ("the word always written, never colour alone" - the
    task's own instruction), from now_spread_classification() above."""
    if classification not in _NOW_CHIP_STYLE:
        return ""
    text_color, bg, border, key = _NOW_CHIP_STYLE[classification]
    label = i18n.t(f"compounder.trading_cost.{key}", lang)
    pct_text = f" · {spread_pct:.1f}%" if spread_pct is not None else ""
    return (
        "<span style='display:inline-flex;align-items:center;gap:5px;border-radius:999px;"
        f"padding:2px 10px;font-size:11px;font-weight:700;background:{bg};"
        f"border:1px solid {border};color:{text_color};'>"
        f"{html.escape(label)}{html.escape(pct_text)}</span>"
    )


def _price_rail_html(bid, ask, last_price, currency_symbol, spread_dollar, lang):
    """Option A: bid and ask as two points on a horizontal price line,
    the gap between them hatched (the "dead ground" a crossing order
    pays to cross), last trade marked below - see debea530-ask_bid_
    tab_mock.html's own OPTION A section, the acceptance bar for this
    layout's grammar (its numbers are fake, never copied)."""
    values = [v for v in (bid, ask, last_price) if v is not None]
    lo, hi = min(values), max(values)
    span = (hi - lo) or (abs(hi) * 0.02 or 1.0)
    pad = span * 0.3
    axis_lo, axis_hi = lo - pad, hi + pad

    def _pos(v):
        return max(0.0, min(100.0, (v - axis_lo) / (axis_hi - axis_lo) * 100.0))

    bid_pct, ask_pct = _pos(bid), _pos(ask)
    gap_left, gap_right = min(bid_pct, ask_pct), max(bid_pct, ask_pct)

    _t = lambda key, **fmt: i18n.t(f"compounder.trading_cost.{key}", lang, **fmt)

    last_html = ""
    if last_price is not None:
        last_pct = _pos(last_price)
        last_html = (
            f"<span style='position:absolute;left:{last_pct:.2f}%;top:37px;width:12px;height:12px;"
            "border-radius:50%;background:#e6edf5;border:2px solid #0b1220;transform:translateX(-50%);'></span>"
            f"<span style='position:absolute;left:{last_pct:.2f}%;top:56px;font-size:11px;"
            "transform:translateX(-50%);text-align:center;line-height:1.4;color:#c7d2e0;'>"
            f"<b style='display:block;font-size:14px;'>{html.escape(_fmt_money(last_price, currency_symbol))}</b>"
            f"{html.escape(_t('rail_last_label'))}</span>"
        )

    # Vertical layout: two-line "above" labels (bold price + word label,
    # ~30px tall) sit ABOVE the rail with enough clearance that they
    # never collide with the bid/ask circles sitting ON the rail line -
    # a real layout bug in an earlier pass here, caught by this
    # redesign's own Playwright screenshot check (labels top:-4px vs
    # circles top:22px left only an 18px gap for a ~30px-tall label
    # block). Circles now at top:34px (was 22px), "above" labels at
    # top:-18px (was -4px) - a full label height of clearance either
    # side of the rail line at top:42px (was 30px).
    return (
        "<div style='position:relative;height:76px;margin:30px 8px 6px;'>"
        "<div style='position:absolute;top:42px;left:0;right:0;height:2px;background:#1f3352;'></div>"
        f"<div style='position:absolute;top:38px;height:10px;left:{gap_left:.2f}%;"
        f"width:{max(gap_right - gap_left, 1.0):.2f}%;border-radius:5px;"
        "background:repeating-linear-gradient(45deg,#7f1d3a,#7f1d3a 4px,#3a0f1f 4px,#3a0f1f 8px);'></div>"
        f"<span style='position:absolute;left:{bid_pct:.2f}%;top:34px;width:18px;height:18px;border-radius:50%;"
        "background:#0b1220;border:2px solid #2dd4bf;transform:translateX(-50%);'></span>"
        f"<span style='position:absolute;left:{ask_pct:.2f}%;top:34px;width:18px;height:18px;border-radius:50%;"
        "background:#0b1220;border:2px solid #fb7185;transform:translateX(-50%);'></span>"
        f"<span style='position:absolute;left:{bid_pct:.2f}%;top:-18px;font-size:11px;"
        "transform:translateX(-50%);text-align:center;line-height:1.4;color:#c7d2e0;'>"
        f"<b style='display:block;font-size:14px;color:#2dd4bf;'>{html.escape(_fmt_money(bid, currency_symbol))}</b>"
        f"{html.escape(_t('rail_bid_label'))}</span>"
        f"<span style='position:absolute;left:{ask_pct:.2f}%;top:-18px;font-size:11px;"
        "transform:translateX(-50%);text-align:center;line-height:1.4;color:#c7d2e0;'>"
        f"<b style='display:block;font-size:14px;color:#fb7185;'>{html.escape(_fmt_money(ask, currency_symbol))}</b>"
        f"{html.escape(_t('rail_ask_label'))}</span>"
        + last_html +
        "<span style='position:absolute;left:97%;top:56px;font-size:11px;transform:translateX(-100%);"
        "text-align:right;line-height:1.4;color:#8aa0b8;'>"
        f"{html.escape(_t('rail_spread_label'))}<br>"
        f"<b style='color:#fbbf24;'>{html.escape(_fmt_money(spread_dollar, currency_symbol))}</b></span>"
        "</div>"
    )


def _styled_day_table(days_rows, lang):
    """The day-by-day table (structure unchanged from the original
    Commit 3 layout) with its Estimated spread % column subtly muted
    (pandas Styler, since st.dataframe's own column_config has no
    per-column text-colour option) so the recorded columns read as
    primary - the redesign's own section 5 instruction."""
    _t = lambda key, **fmt: i18n.t(f"compounder.trading_cost.{key}", lang, **fmt)
    col_espread = _t("table_col_estimated_spread")
    df = pd.DataFrame([{
        _t("table_col_date"): row["date"],
        _t("table_col_close"): row["close"],
        _t("table_col_recorded_bid"): row["recorded_bid"],
        _t("table_col_recorded_ask"): row["recorded_ask"],
        _t("table_col_recorded_spread"): row["recorded_spread_pct"],
        col_espread: row["estimated_spread_pct"],
    } for row in days_rows])
    return df.style.set_properties(subset=[col_espread], **{"color": "#5b7290"})


def _fmt_value_traded(value):
    """A dollar value-traded figure, abbreviated (K/M/B) - "—" for None.
    No currency symbol prefix by design: the ticker's own currency
    varies (AUD for ASX, USD for US - quote_snapshot_store's own
    "currency" column), and this tile has no single figure's currency
    attached to check, so a bare "$" would be ambiguous rather than
    informative."""
    if value is None:
        return "—"
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if abs(value) >= threshold:
            return f"{value / threshold:,.2f}{suffix}"
    return f"{value:,.0f}"


def _fmt_pct_diag(value):
    return f"{value:.3f}%" if value is not None else "n/a"


# "Bid/ask now" caption (task feedback, 23 Sep 2026, owner-reported):
# the tile must never read as live - its caption always names the
# snapshot's own recorded date/time (or, once stale, just its date),
# so nobody mistakes a recorded snapshot for a live quote. Hand-built
# weekday/month abbreviation tables (not strftime's locale machinery,
# and never locale.setlocale() - that mutates process-wide state, which
# would corrupt every OTHER concurrent Streamlit session's own
# formatting on a shared multi-tenant server) so EN/ES both render
# correctly without any shared/global state.
_WEEKDAY_ABBR = {
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
    "es": ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"],
}
_MONTH_ABBR = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
}


def _market_tz_for_ticker(ticker):
    """quote_recorder.py's own _MARKET_TZ, keyed off the same ".AX"
    suffix check this codebase already uses everywhere else to tell AU
    from US tickers (app.py has a dozen call sites doing exactly this -
    grepped rather than inventing a second convention)."""
    market = quote_recorder.MARKET_ASX if ticker.upper().endswith(".AX") else quote_recorder.MARKET_US
    return quote_recorder._MARKET_TZ[market]


def _format_clock_time(dt_local):
    """"1:00pm"/"4:47pm" - 12-hour, no leading zero, lowercase am/pm.
    Deliberately NOT a fixed "1:00pm" literal: quote_recorder.py's own
    sampling window is 13:00-16:00 local (whichever tick inside that
    window actually fires, not always exactly 13:00 - see is_due_now()'s
    own docstring), so the real recorded time can genuinely be later
    than 1pm. Showing the EXACT recorded time rather than a hardcoded
    "1:00pm" is the honest choice - this whole feature exists to stop
    showing a plausible-looking but wrong number."""
    hour12 = dt_local.hour % 12 or 12
    ampm = "am" if dt_local.hour < 12 else "pm"
    return f"{hour12}:{dt_local.minute:02d}{ampm}"


def _format_snapshot_datetime(dt_local, lang):
    """"Tue 23 Sep, 1:00pm AEST" (or the local equivalent) - weekday,
    day, month, recorded clock time, and the market's own timezone
    abbreviation (tzname() on an aware datetime already resolves to the
    correct one - "AEST"/"AEDT" for Sydney, "EST"/"EDT" for New York -
    DST-correct automatically, same zoneinfo objects quote_recorder.py
    itself samples with)."""
    wd = _WEEKDAY_ABBR[lang][dt_local.weekday()]
    mo = _MONTH_ABBR[lang][dt_local.month - 1]
    tz_abbr = dt_local.tzname() or ""
    return f"{wd} {dt_local.day} {mo}, {_format_clock_time(dt_local)} {tz_abbr}".strip()


def _format_date_only(dt_local, lang):
    """"23 Sep 2026" - the "last recorded <date>" wording for a
    snapshot too old to show a clock time for (see _trading_days_since()
    below) - a bare date reads as "this is old", which is the point;
    attaching a specific time to a 2-week-old quote would look more
    current than it is."""
    mo = _MONTH_ABBR[lang][dt_local.month - 1]
    return f"{dt_local.day} {mo} {dt_local.year}"


_STALE_SNAPSHOT_TRADING_DAYS = 3


def _trading_days_since(snap_date_str, market_tz):
    """How many Mon-Fri days have passed between `snap_date_str`
    ("YYYY-MM-DD") and today, in `market_tz` - 0 if snap_date is today.
    Same weekday-only simplification as quote_recorder.is_due_now() -
    there is still no market-holiday calendar anywhere in this codebase
    (Commit 1's own report) - so a long weekend or a public holiday can
    undercount slightly; this is a "is this tile roughly stale" check,
    not a precise trading-calendar computation."""
    snap_date = _dt.datetime.strptime(snap_date_str, "%Y-%m-%d").date()
    today_local = _dt.datetime.now(_dt.timezone.utc).astimezone(market_tz).date()
    if snap_date >= today_local:
        return 0
    count = 0
    d = snap_date
    while d < today_local:
        d += _dt.timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return count


def _plain_tile(label, value_text, caption=None):
    """A "value + optional caption" tile, no band pill - for the two
    Trading Cost tiles that aren't a tight/moderate/wide metric (Value
    traded, Bid/ask now). Same label/value colour and font choices as
    band_gauge() above for visual consistency, deliberately WITHOUT
    that function's colour-banded strip or verdict pill, which wouldn't
    mean anything for either of these two figures."""
    st.markdown(
        "<div style='margin-bottom:2px;'>"
        f"<div style='font-size:13px;font-weight:600;color:#aebfd4;'>{html.escape(str(label))}</div>"
        "<div style='height:16px;'></div>"
        "<div style='font-family:ui-monospace,Menlo,SFMono-Regular,monospace;"
        f"font-size:14px;font-weight:700;color:#e6edf5;'>{html.escape(str(value_text))}</div>"
        + (f"<div style='font-size:11px;color:#8aa0b8;margin-top:3px;'>{html.escape(str(caption))}</div>"
           if caption else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def _trading_cost_spread_chart(days_rows, recording_start_date, snap_times, ticker, lang="en"):
    """"Spread, % of price" chart, redesigned to read fact vs estimate
    by SHAPE AND TEXTURE, never colour alone - matching 45e2b786-
    recorded_vs_estimated_mock.html's own grammar (the acceptance bar
    for this layout - its numbers are fake, never copied): estimated
    days are muted, diagonally hatched background bars (plotly's own
    marker_pattern, always present for every day); recorded days are
    solid bright dots with a dark ring, always drawn on top (a second
    trace, so z-order is "last trace wins" - already true here since
    it's added after the bars); a soft tinted zone (add_vrect) marks
    the period since real recording began, labelled "RECORDING BEGAN
    <date>"; the wide-spread threshold line's own label gets a dark
    backing (annotation_bgcolor) so it can't blend into a bar or dot
    it happens to sit near. ONE y-axis - both traces share it.

    `snap_times`: {"YYYY-MM-DD": snap_at_utc} from quote_snapshot_
    store.snapshot_times_for_ticker() - the recorded sample TIME shown
    in each recorded day's own hover text (a plain trace name/legend
    can't carry per-point custom text like this without either a
    hovertemplate+customdata array or, simpler for two independent
    traces sharing the same per-day text, the hovertext list used
    here)."""
    _t = lambda key, **fmt: i18n.t(f"compounder.trading_cost.{key}", lang, **fmt)
    tz = _market_tz_for_ticker(ticker)
    dates = [r["date"] for r in days_rows]
    estimated = [r["estimated_spread_pct"] for r in days_rows]
    recorded = [r["recorded_spread_pct"] for r in days_rows]

    def _snap_time_str(date_str):
        snap_iso = snap_times.get(date_str)
        if not snap_iso:
            return "—"
        dt_local = _dt.datetime.fromisoformat(snap_iso).astimezone(tz)
        tz_abbr = dt_local.tzname() or ""
        return f"{_format_clock_time(dt_local)} {tz_abbr}".strip()

    def _hover_for(row):
        est, rec = row["estimated_spread_pct"], row["recorded_spread_pct"]
        if rec is not None and est is not None:
            gap = rec - est
            return _t("hover_both", recorded=f"{rec:.1f}", time=_snap_time_str(row["date"]),
                       estimated=f"{est:.1f}", gap=f"{gap:+.1f}")
        if rec is not None:
            return _t("hover_recorded_only", recorded=f"{rec:.1f}", time=_snap_time_str(row["date"]))
        if est is not None:
            return _t("hover_estimated_only", estimated=f"{est:.1f}")
        return ""

    hover_text = [_hover_for(r) for r in days_rows]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=dates, y=estimated, name=_t("legend_estimated"),
        marker=dict(
            color="rgba(94,211,240,0.30)",
            pattern=dict(shape="/", fgcolor="rgba(94,211,240,0.65)", size=6, solidity=0.3),
        ),
        hovertext=hover_text, hoverinfo="text",
    ))
    fig.add_trace(go.Scatter(
        x=dates, y=recorded, mode="markers", name=_t("legend_recorded"),
        marker=dict(color=_CP_COLOR_TEXT["green"], size=9, line=dict(color="#0b1220", width=2)),
        hovertext=hover_text, hoverinfo="text",
    ))

    if dates and recording_start_date and recording_start_date <= dates[-1]:
        zone_x0 = max(recording_start_date, dates[0])
        fig.add_vrect(
            x0=zone_x0, x1=dates[-1],
            fillcolor="rgba(52,211,153,0.08)", line_width=0, layer="below",
        )
        fig.add_annotation(
            x=zone_x0, y=1.0, yref="paper", xanchor="left", yanchor="bottom",
            text=_t("recording_began_zone_label", date=recording_start_date),
            showarrow=False, font=dict(size=10, color="#34d399"),
            bgcolor="rgba(11,18,32,0.85)", borderpad=3,
        )

    fig.add_hline(
        y=trading_cost_engine.WIDE_THRESHOLD_PCT, line_dash="dash",
        line_color=_CP_COLOR_TEXT["red"],
        annotation_text=_t("wide_threshold_label"),
        annotation_position="bottom right",
        annotation_font_size=11, annotation_font_color=_CP_COLOR_TEXT["red"],
        annotation_bgcolor="rgba(11,18,32,0.85)",
    )
    fig.update_layout(
        title=_t("chart_spread_title"),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#c7d2e0"),
        margin=dict(l=10, r=10, t=40, b=10), height=300,
        xaxis=dict(gridcolor="rgba(138,160,184,0.15)"),
        yaxis=dict(gridcolor="rgba(138,160,184,0.15)", ticksuffix="%"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        barmode="overlay",
    )
    return fig


def render_trading_cost_tab(ticker, price_history, lang="en"):
    """Renders the 💱 Trading Cost tab's full content for `ticker` - see
    TRADING_COST_ENABLED/with_trading_cost_tab() above for when this
    tab exists at all.

    `price_history`: the caller's own get_price_history(ticker) cache
    (app.py), already converted to trading_cost_engine's own
    [{"date","high","low","close","volume"}, ...] row shape - see that
    module's docstring for why it takes plain data rather than
    fetching anything itself. None/[] is handled gracefully below (an
    info message, never a broken/empty tab) - this is exactly the
    state the tab launches in for a ticker with no cached price
    history yet, and (separately) the state EVERY ticker's snapshot
    history is in on day one, before ENABLE_TRADING_COST has been on
    long enough to have real quote_snapshots rows.

    quote_snapshot_store.snapshots_for_ticker()/latest_snapshot() are
    called directly from here, not passed in - neither is a circular
    import (this module already imports quote_snapshot_store at the
    top of the file), so app.py's own two call sites only need to pass
    `ticker` and `price_history`.

    "Bid/ask now" (the 4th tile) NEVER attempts a live "right now"
    fetch of its own - only ever shows quote_snapshot_store.
    latest_snapshot(ticker), labelled with its own recorded date. A
    live fetch would need to know whether the market is CURRENTLY open
    to avoid exactly the after-hours-junk problem this whole feature
    exists to fix (see quote_recorder.py's own module docstring for the
    OCL.AX example) - and Commit 1's own report already established
    there is no market-hours/holiday-calendar helper anywhere in this
    codebase to answer that reliably. Always showing the latest
    RECORDED (already quote_recorder.py-verified) snapshot instead is
    simpler and can never show Yahoo's raw after-hours quote, by
    construction - which is also exactly what the spec's own "never
    Yahoo's after-hours quote" instruction asks for, just achieved by
    never attempting the live read at all rather than by time-gating
    one."""
    _t = lambda key, **fmt: i18n.t(f"compounder.trading_cost.{key}", lang, **fmt)

    snapshots = quote_snapshot_store.snapshots_for_ticker(ticker)
    series = trading_cost_engine.trading_cost_series(ticker, price_history or [], snapshots, days=30)

    st.markdown(
        f'<div style="color:#8aa0b8;font-size:12.5px;margin-bottom:10px;">'
        f'{html.escape(_t("why_matters"))}</div>',
        unsafe_allow_html=True,
    )

    if not series["days"]:
        st.info(_t("no_price_data", ticker=ticker))
        return

    latest = quote_snapshot_store.latest_snapshot(ticker)
    currency_symbol = _currency_symbol_for_ticker(ticker)
    tz = _market_tz_for_ticker(ticker)

    latest_dt_local = None
    latest_caption = None
    now_spread_pct = None
    if latest and latest.get("snap_at_utc"):
        latest_dt_local = _dt.datetime.fromisoformat(latest["snap_at_utc"]).astimezone(tz)
        days_old = _trading_days_since(latest["snap_date"], tz)
        latest_caption = (
            _t("bid_ask_now_stale_label", date=_format_date_only(latest_dt_local, lang))
            if days_old > _STALE_SNAPSHOT_TRADING_DAYS
            else _t("bid_ask_now_snapshot_label", datetime=_format_snapshot_datetime(latest_dt_local, lang))
        )
    if latest and latest.get("bid") is not None and latest.get("ask") is not None:
        now_spread_pct = trading_cost_engine._recorded_spread_pct(latest["bid"], latest["ask"])

    recorded_leads = series["recorded_days_count"] >= trading_cost_engine.RECORDED_LEADS_MIN_DAYS

    # ---- Section 1: header meters - "recorded" leads once >=
    # RECORDED_LEADS_MIN_DAYS real sessions exist in the window,
    # demoting the estimate to a secondary "for comparison" figure
    # with its own tracking line. Below that count, the estimate stays
    # primary (today's launch-day state, and every ticker's state
    # until enough real snapshots accumulate). ----
    _m1, _m2 = st.columns(2)
    if recorded_leads:
        with _m1:
            _plain_tile(
                _t("meter_recorded_label"),
                _fmt_pct1(series["average_recorded_spread_pct"]),
                caption=_t("meter_recorded_caption", datetime=_format_snapshot_datetime(latest_dt_local, lang))
                if latest_dt_local else None,
            )
            with st.expander("What this measures", expanded=False):
                st.caption(_t("meter_recorded_comment"))
        with _m2:
            _offset = _fmt_signed_pt(series["tracking_offset_pct"])
            _plain_tile(
                _t("meter_estimate_secondary_label"),
                _fmt_pct1(series["average_estimated_spread_pct"]),
                caption=_t("meter_tracking_line", offset=f"{_offset}pt") if _offset else None,
            )
            with st.expander("What this measures", expanded=False):
                st.caption(_t("tile_spread_estimated_comment"))
    else:
        with _m1:
            _plain_tile(_t("tile_spread_estimated"), _fmt_pct1(series["average_estimated_spread_pct"]))
            with st.expander("What this measures", expanded=False):
                st.caption(_t("tile_spread_estimated_comment"))
        with _m2:
            if series["recorded_days_count"] > 0:
                _progress_caption = _t(
                    "meter_recorded_progress",
                    count=series["recorded_days_count"],
                    min_count=trading_cost_engine.RECORDED_LEADS_MIN_DAYS,
                )
            else:
                _progress_caption = _t("bid_ask_now_no_data")
            _plain_tile(
                _t("meter_recorded_label"),
                _fmt_pct1(series["average_recorded_spread_pct"]),
                caption=_progress_caption,
            )
            with st.expander("What this measures", expanded=False):
                st.caption(_t("meter_recorded_comment"))

    # ---- Section 2: "What crossing it costs you" (Option B) -
    # directly under the meters. Prices off the LATEST recorded
    # snapshot's own spread when one exists (a live-crossing-cost
    # question is a "right now" question), else the 30-day estimate -
    # the same `now_spread_pct` also drives section 3's rail/chip
    # below, so both sections always agree on which spread is "now". ----
    st.markdown(f"#### {_t('cost_heading')}")
    _cost_spread = now_spread_pct if now_spread_pct is not None else series["average_estimated_spread_pct"]
    if _cost_spread is None:
        st.info(_t("cost_no_data", ticker=ticker))
    else:
        _cost_rows = trading_cost_engine.crossing_cost(_cost_spread)
        _headline_row = _cost_rows[1]  # the $10,000 card, per the spec's own headline sentence
        # Streamlit trap (CLAUDE.md): a plain st.markdown() with two
        # "$"-prefixed amounts in the same string gets KaTeX-mangled -
        # paired "$...$" is read as LaTeX math, not two dollar
        # amounts. Rendered as raw HTML instead (like every other
        # money figure in this section), never plain markdown.
        _headline_text = _t(
            "cost_headline",
            amount=_fmt_money(_headline_row["cost_each_way"], currency_symbol, decimals=0),
            order=_fmt_money(_headline_row["order_size"], currency_symbol, decimals=0),
            ticker=ticker,
        )
        st.markdown(
            "<div style='font-size:19px;font-weight:800;line-height:1.4;margin:2px 0 12px;color:#e6edf5;'>"
            f"{html.escape(_headline_text)}</div>",
            unsafe_allow_html=True,
        )

        _mid_price = None
        if now_spread_pct is not None:
            _mid_price = (latest["bid"] + latest["ask"]) / 2.0
        elif price_history:
            _last_row = sorted(price_history, key=lambda r: r["date"])[-1]
            _mid_price = _last_row.get("close")

        _cost_cols = st.columns(3)
        for _col, _row in zip(_cost_cols, _cost_rows):
            with _col:
                _order_label = _fmt_money(_row["order_size"], currency_symbol, decimals=0)
                _each_way_label = _fmt_money(_row["cost_each_way"], currency_symbol, decimals=0)
                _detail_label = _t(
                    "cost_card_detail",
                    round_trip=_fmt_money(_row["round_trip"], currency_symbol, decimals=0),
                    multiple=f"{_row['round_trip_vs_brokerage_multiple']:.1f}",
                    brokerage=_fmt_money(_row["brokerage_flat"], currency_symbol, decimals=0),
                )
                st.markdown(
                    "<div style='background:#0b1526;border:1px solid #1a2b4a;border-radius:10px;padding:11px 14px;'>"
                    "<div style='color:#8aa0b8;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;'>"
                    f"{html.escape(_t('cost_card_title', order=_order_label))}</div>"
                    "<div style='font-size:17px;font-weight:800;margin:3px 0 1px;color:#e6edf5;'>"
                    f"{html.escape(_t('cost_card_each_way', amount=_each_way_label))}</div>"
                    f"<div style='font-size:11px;color:#5b7290;line-height:1.5;'>{html.escape(_detail_label)}</div>"
                    "</div>",
                    unsafe_allow_html=True,
                )

        _cost_source = _t("cost_source_recorded") if now_spread_pct is not None else _t("cost_source_estimated", days=30)
        st.markdown(
            "<div style='border:1px dashed #1f3352;border-radius:10px;padding:9px 13px;font-size:12px;"
            "color:#c7d2e0;margin-top:11px;line-height:1.55;'>"
            f"{html.escape(_t('cost_tip', mid=_fmt_money(_mid_price, currency_symbol), source=_cost_source))}</div>",
            unsafe_allow_html=True,
        )
        st.caption(
            (f"{latest_caption} · " if now_spread_pct is not None and latest_caption else "")
            + _t("cost_caption_suffix")
        )

    # ---- Section 3: "Bid & ask now" (Option A rail + Option C tiles) ----
    st.markdown(f"#### {_t('bid_ask_now_title')}")
    if latest and latest.get("bid") is not None and latest.get("ask") is not None:
        _bid, _ask = latest["bid"], latest["ask"]
        _last_price = latest.get("last_price")
        _spread_dollar = _ask - _bid
        _classification = trading_cost_engine.now_spread_classification(now_spread_pct)

        _h1, _h2 = st.columns([3, 2])
        with _h1:
            st.markdown(f"**{ticker}**")
        with _h2:
            st.markdown(_now_chip_html(_classification, now_spread_pct, lang), unsafe_allow_html=True)

        st.markdown(
            _price_rail_html(_bid, _ask, _last_price, currency_symbol, _spread_dollar, lang),
            unsafe_allow_html=True,
        )

        _tl1, _tl2, _tl3, _tl4, _tl5 = st.columns(5)
        with _tl1:
            _plain_tile(_t("tile_bid"), _fmt_money(_bid, currency_symbol))
        with _tl2:
            _plain_tile(_t("tile_ask"), _fmt_money(_ask, currency_symbol))
        with _tl3:
            _plain_tile(_t("tile_mid"), _fmt_money((_bid + _ask) / 2.0, currency_symbol))
        with _tl4:
            _plain_tile(_t("tile_spread_dollar"), _fmt_money(_spread_dollar, currency_symbol))
        with _tl5:
            _plain_tile(_t("tile_spread_pct"), _fmt_pct1(now_spread_pct))

        _exact_dt = _format_snapshot_datetime(latest_dt_local, lang) if latest_dt_local else ""
        st.caption(_t("bid_ask_now_snapshot_label", datetime=_exact_dt) + " · " + _t("rail_not_live_note"))
    else:
        st.markdown("—")
        st.caption(_t("bid_ask_now_no_data"))

    # ---- Section 4: history chart ----
    st.markdown('<div style="margin-top:18px;"></div>', unsafe_allow_html=True)
    snap_times = quote_snapshot_store.snapshot_times_for_ticker(ticker)
    sdd_plotly_chart(
        _trading_cost_spread_chart(series["days"], series["recording_start_date"], snap_times, ticker, lang=lang),
        text_description=_t("chart_description"),
    )

    # ---- Section 5: day-by-day table (structure unchanged, estimated
    # column muted so recorded reads as primary) ----
    st.markdown(f"**{_t('table_title')}**")
    st.dataframe(_styled_day_table(series["days"], lang), width='stretch', hide_index=True)

    st.caption(_t("footnote"))

    # Owner-only diagnostics (task addition, 23 Sep 2026, owner-
    # requested): raw trading_cost_series() output on screen - the
    # exact numbers behind every tile/chart above, for the owner to
    # read real per-ticker figures off a live deploy (no live network
    # access existed in the sandbox this was built in - see Commit 2's
    # own report). Same gating shape as the Deep Dive page's own Moat
    # diagnostics expander (Commit M/T): ai_gate.is_owner(...) AND
    # st.session_state.get("full_view_unlocked") - never rendered, never
    # even checked, for a non-owner visitor or an owner who has exited
    # to normal view. Deferred imports, same reason paywall_engine is
    # already deferred-imported elsewhere in this file: this module has
    # no top-level dependency on either.
    try:
        import ai_gate
        import paywall_engine
        _tc_owner_full_view = bool(
            ai_gate.is_owner(paywall_engine.current_user_email())
            and st.session_state.get("full_view_unlocked")
        )
    except Exception:
        _tc_owner_full_view = False

    if _tc_owner_full_view:
        with st.expander("🔧 Trading Cost diagnostics (owner only)", expanded=False):
            st.caption(
                f"Raw trading_cost_engine.trading_cost_series() output for "
                f"{ticker} - the exact numbers the tiles/charts above are built from."
            )
            st.markdown(
                f"**Median recorded spread:** {_fmt_pct_diag(series['median_recorded_spread_pct'])}  \n"
                f"**Average recorded spread:** {_fmt_pct_diag(series['average_recorded_spread_pct'])}  \n"
                f"**Average estimated spread (30d):** {_fmt_pct_diag(series['average_estimated_spread_pct'])}  \n"
                f"**Tracking offset (est - recorded, overlap days):** {_fmt_pct_diag(series['tracking_offset_pct'])}  \n"
                f"**Recording start date:** {series['recording_start_date'] or 'n/a'}  \n"
                f"**Recorded days count:** {series['recorded_days_count']}  \n"
                f"**Recorded leads header (>= {trading_cost_engine.RECORDED_LEADS_MIN_DAYS} days):** "
                f"{series['recorded_days_count'] >= trading_cost_engine.RECORDED_LEADS_MIN_DAYS}  \n"
                f"**Now spread % (latest snapshot):** {_fmt_pct_diag(now_spread_pct)}  \n"
                f"**Avg daily value traded (30d):** {_fmt_value_traded(series['avg_daily_value_traded_30d'])}"
            )
            st.markdown("**Per-day rows (raw engine output):**")
            st.dataframe(
                [{
                    "date": row["date"], "close": row["close"],
                    "recorded_bid": row["recorded_bid"], "recorded_ask": row["recorded_ask"],
                    "recorded_spread_pct": row["recorded_spread_pct"],
                    "estimated_spread_pct": row["estimated_spread_pct"],
                } for row in series["days"]],
                width='stretch', hide_index=True,
            )
