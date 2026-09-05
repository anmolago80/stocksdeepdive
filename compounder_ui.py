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

import hashlib
import html
import re

import plotly.graph_objects as go
import streamlit as st

from simple_view_copy import SECTION_WHY_CAPTIONS

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
        result = st.plotly_chart(fig, use_container_width=True, config=merged, **kwargs)
    elif config is not None:
        result = st.plotly_chart(fig, use_container_width=True, config=config, **kwargs)
    else:
        result = st.plotly_chart(fig, use_container_width=True, **kwargs)

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
        fig.add_hline(
            y=_avg, line_dash="dash", line_color="#e6edf5", line_width=1.5,
            annotation_text=f"Average {_avg * 100:+.1f}%",
            annotation_position="top left",
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

def render_section(sections, ticker, section_label, gate=None):
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
    """
    section = (sections or {}).get(section_label)
    if not section:
        st.warning(f"No data yet for {ticker} in {section_label}.")
        return

    # Simple view, Part 4: one-line "why this matters" caption, shown in
    # BOTH views regardless of subscription - static template text, see
    # simple_view_copy.py's own module docstring for the full rationale.
    _why = SECTION_WHY_CAPTIONS.get(section_label)
    if _why:
        st.caption(_why)

    if gate:
        import paywall_engine
        title, teaser, key_prefix = gate
        if not paywall_engine.render_gate(title, teaser=teaser, key_prefix=key_prefix):
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


def render_tabs(sections, ticker, section_order, key_prefix, gates=None):
    """Renders `st.tabs(section_order)` and calls render_section() in each
    tab - the same navigation mechanism the Research page already uses
    (see page_research()), reused as-is for the Deep Dive auto view so
    both callers share one nav mechanism, not just one section renderer.

    gates: optional {section_label: (title, teaser, key_prefix)} - only
        the sections present here are gated; every other section renders
        openly.
    """
    gates = gates or {}
    tabs = st.tabs(section_order, key=key_prefix)
    for label, tab in zip(section_order, tabs):
        with tab:
            render_section(sections, ticker, label, gate=gates.get(label))
