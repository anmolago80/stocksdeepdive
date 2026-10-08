"""
currency_risk_render.py

Rendering for the 💱 Currency Risk page - a thin app.py page function
calls render_currency_risk_page() here, same "*_engine.py owns the
logic, *_render.py owns markup" split top100_engine.py/top100_render.py
already established. Reads currency_risk_engine.py directly (no
Streamlit dependency in that module). Pure Python, no AI calls, $0
ongoing beyond one cached-daily yfinance history call per FX pair.

Owner-approved mock (25 Sep 2026, "headwind_and_currency_risk_mock.html",
section ②): pick two currencies (default AUD -> USD, since most of the
Top 100 is USD-priced), a range (5y/10y/20y/Max), and see (A) where the
rate sits in its own history, (B) the distribution of rolling 12-month
changes, (C) what a given position size stands to gain/lose from the FX
leg alone under three scenarios.
"""
import html

import plotly.graph_objects as go
import streamlit as st

import compounder_ui
import currency_risk_engine as cre
import currency_view_engine as cve
import i18n


def _t(key, lang, **fmt):
    return i18n.t(f"currency_risk.{key}", lang, **fmt)


def _tile_html(label, value, color=None):
    color_style = f"color:{color};" if color else "color:#e6edf5;"
    return (
        "<div style='background:#0b1526;border:1px solid #1a2b4a;border-radius:10px;"
        "padding:9px 12px;'>"
        f"<div style='color:#8aa0b8;font-size:10px;letter-spacing:.06em;"
        f"text-transform:uppercase;'>{html.escape(label)}</div>"
        "<div style='font-family:ui-monospace,Menlo,SFMono-Regular,monospace;"
        f"font-size:15px;font-weight:800;margin-top:3px;{color_style}'>{value}</div>"
        "</div>"
    )


def _tiles_grid_html(tiles):
    """Responsive tile grid - repeat(auto-fit,minmax(...)) rather than a
    fixed column count, same "wraps naturally on a phone, no separate
    media query" convention as app.py's own .sdd-strip grid."""
    return (
        "<div style='display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));"
        "gap:10px;margin-top:10px;'>" + "".join(tiles) + "</div>"
    )


def _render_selector_bar(lang, default_base=None):
    """Pair selector (two dropdowns, same-currency guarded downstream)
    + range chips - one shared state (st.selectbox/segmented_control's
    own key= persistence), read fresh on every rerun.

    default_base (SECTION B, COMMIT B3 of instruction_top200_
    amendments_and_currency_view.md, 5 Oct 2026): "for a signed-in
    visitor with a home currency, the From picker starts on it."
    Exactly like DEFAULT_BASE itself, this is only ever consulted the
    FIRST time this widget is instantiated in a session - st.selectbox's
    own key= persistence means a value already in st.session_state["cr_
    base"] (the visitor's own later choice, or a currency-risk-note
    jump's one-shot override written directly into session_state by
    app.py's page_currency_risk()) always wins over `index=`, so this
    never fights either of those. None (every other visitor) reproduces
    today's exact default - DEFAULT_BASE."""
    cols = st.columns([1, 1, 2])
    with cols[0]:
        # Passing index= AND having st.session_state["cr_base"] already
        # set (a currency-risk-note jump's one-shot override, written
        # directly into session_state by app.py's page_currency_risk()
        # before this widget is created) is harmless - session_state
        # always wins - but Streamlit logs a policy warning about it on
        # every such rerun. Omitting index= whenever the key is already
        # present avoids that noise without changing which value wins.
        _base_kwargs = {} if "cr_base" in st.session_state else {
            "index": cre.CURRENCIES.index(
                default_base if default_base in cre.CURRENCIES else cre.DEFAULT_BASE
            )
        }
        base = st.selectbox(_t("base_label", lang), cre.CURRENCIES, key="cr_base", **_base_kwargs)
    with cols[1]:
        _quote_kwargs = {} if "cr_quote" in st.session_state else {
            "index": cre.CURRENCIES.index(cre.DEFAULT_QUOTE)
        }
        quote = st.selectbox(_t("quote_label", lang), cre.CURRENCIES, key="cr_quote", **_quote_kwargs)
    with cols[2]:
        labels = {k: _t(f"range_{k}", lang) for k in cre.RANGE_KEYS}
        options = [labels[k] for k in cre.RANGE_KEYS]
        default_label = labels[cre.DEFAULT_RANGE]
        st.caption(_t("range_label", lang))
        choice_label = st.segmented_control(
            _t("range_label", lang), options, default=default_label,
            key="cr_range", label_visibility="collapsed",
        ) or default_label
        label_to_key = {v: k for k, v in labels.items()}
        range_key = label_to_key.get(choice_label, cre.DEFAULT_RANGE)
    return base, quote, range_key


def _history_chart_text_equivalent(base, quote, stats):
    return (
        f"{base}/{quote} rate history: today {stats['today']:.4f}, period average "
        f"{stats['average']:.4f} ({stats['pct_vs_average']:+.1f}% vs average), range "
        f"{stats['range_min']:.4f} to {stats['range_max']:.4f}, {stats['percentile']:.0f}th "
        f"percentile, annualised volatility {stats['annualised_vol_pct']:.1f}%."
    )


def _render_history_chart(dates, closes, stats, base, quote, lang):
    avg, sigma = stats["average"], stats["sigma"]
    fig = go.Figure()
    # ±1σ band (behind everything else - added first).
    fig.add_trace(go.Scatter(
        x=list(dates) + list(dates[::-1]),
        y=[avg + sigma] * len(dates) + [avg - sigma] * len(dates),
        fill="toself", fillcolor="rgba(45,212,191,0.10)", line=dict(width=0),
        hoverinfo="skip", showlegend=False, name=_t("band_label", lang),
    ))
    # Dashed period-average line - trace name is internal only
    # (showlegend=False, hoverinfo="skip"), never rendered to the
    # visitor, so it doesn't need the templated {average} label the
    # visible annotation below uses.
    fig.add_trace(go.Scatter(
        x=[dates[0], dates[-1]], y=[avg, avg], mode="lines",
        line=dict(color="#fbbf24", width=1.5, dash="dash"),
        hoverinfo="skip", showlegend=False, name="average",
    ))
    fig.add_annotation(
        x=dates[-1], y=avg, text=_t("avg_line_label", lang, average=f"{avg:.4f}"),
        showarrow=False, xanchor="left", xshift=8, font=dict(size=10, color="#fbbf24"),
    )
    # The rate line itself - site convention: 2px, teal.
    fig.add_trace(go.Scatter(
        x=dates, y=closes, mode="lines", line=dict(color="#2dd4bf", width=2),
        name=f"{base}/{quote}",
    ))
    # Current-rate marker + direct end label (site convention, replacing
    # a legend - see app.py's own _render_portfolio_value_chart()).
    fig.add_trace(go.Scatter(
        x=[dates[-1]], y=[closes[-1]], mode="markers",
        marker=dict(size=7, color="#0b1220", line=dict(width=2, color="#2dd4bf")),
        showlegend=False, hoverinfo="skip",
    ))
    fig.add_annotation(
        x=dates[-1], y=closes[-1], text=f"{closes[-1]:.4f}", showarrow=False,
        xanchor="left", xshift=8, yshift=14, font=dict(size=11, color="#2dd4bf"),
    )
    fig.update_layout(
        margin=dict(t=20, b=10, l=10, r=90), height=340,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#c7d2e0"), hovermode="x unified", showlegend=False,
        xaxis=dict(showgrid=False),
        yaxis=dict(showgrid=True, gridcolor="rgba(138,160,184,0.15)"),
    )
    compounder_ui.sdd_plotly_chart(
        fig, text_description=_history_chart_text_equivalent(base, quote, stats),
    )


def _ordinal_suffix_en(n):
    n = int(n)
    if 11 <= (n % 100) <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")


def _percentile_text(percentile, lang):
    n = round(percentile)
    if lang == "es":
        return _t("percentile_value_es", lang, n=n)
    return _t("percentile_value_en", lang, n=n, suffix=_ordinal_suffix_en(n))


def _render_section_a(dates, closes, stats, base, quote, lang):
    st.markdown(f"### {_t('section_a_heading', lang)}")
    st.caption(_t("section_a_why", lang))
    _render_history_chart(dates, closes, stats, base, quote, lang)
    tiles = [
        _tile_html(_t("tile_today", lang), f"{stats['today']:.4f}"),
        _tile_html(_t("tile_average", lang), f"{stats['average']:.4f}", "#fbbf24"),
        _tile_html(
            _t("tile_vs_average", lang), f"{stats['pct_vs_average']:+.1f}%",
            "#34d399" if stats["pct_vs_average"] >= 0 else "#fb7185",
        ),
        _tile_html(_t("tile_range", lang), f"{stats['range_min']:.4f} – {stats['range_max']:.4f}"),
        _tile_html(_t("tile_percentile", lang), _percentile_text(stats["percentile"], lang)),
        _tile_html(
            _t("tile_vol", lang),
            f"{stats['annualised_vol_pct']:.1f}%" if stats["annualised_vol_pct"] is not None else "—",
        ),
    ]
    st.markdown(_tiles_grid_html(tiles), unsafe_allow_html=True)
    st.caption(_t("section_a_caption", lang))


_BUCKET_ORDER = [k for k, _lo, _hi in cre.ROLLING_BUCKETS]
# Sign convention (verified against the mock's own section C worked
# numbers - see currency_risk_engine.position_impact()'s own docstring):
# a POSITIVE rolling change means the base currency strengthened - a
# drag on a foreign (quote-denominated) holding - so positive-change
# buckets are RED; a negative change (base weakened) is a tailwind, so
# negative-change buckets are GREEN. (The mock's own decorative SVG bar
# colours read the other way round from its own caption text - an
# inconsistency in that illustrative mock, not reproduced here; this
# implementation follows the task's own explicit "red = base currency
# strengthened / drag... green = tailwind" wording, which the verified
# section C math above also confirms.)
_BUCKET_COLOR = {
    "lt_m10": "#34d399", "m10_m5": "#34d399", "m5_0": "#34d399",
    "0_p5": "#fb7185", "p5_p10": "#fb7185", "gt_p10": "#fb7185",
}


def _render_distribution_chart(dist, lang):
    labels = [_t(f"bucket_{k}", lang) for k in _BUCKET_ORDER]
    pct_values = [dist["bucket_pct"][k] for k in _BUCKET_ORDER]
    colors = [_BUCKET_COLOR[k] for k in _BUCKET_ORDER]
    counts = [dist["bucket_counts"][k] for k in _BUCKET_ORDER]
    text = [f"{v:.0f}%" for v in pct_values]
    fig = go.Figure(go.Bar(
        x=labels, y=pct_values, marker_color=colors, text=text, textposition="outside",
        hovertext=[f"{c} of {dist['n_windows']} 12-month windows" for c in counts],
        hovertemplate="%{hovertext}<extra></extra>",
    ))
    fig.update_layout(
        margin=dict(t=10, b=10, l=10, r=10), height=260,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#c7d2e0"),
        xaxis=dict(showgrid=False), yaxis=dict(showgrid=True, gridcolor="rgba(138,160,184,0.15)",
                                                title=_t("bucket_y_axis", lang)),
    )
    compounder_ui.sdd_plotly_chart(
        fig,
        text_description=(
            f"Distribution of rolling 12-month changes across {dist['n_windows']} windows: "
            + ", ".join(f"{_t(f'bucket_{k}', lang)} {dist['bucket_pct'][k]:.0f}%" for k in _BUCKET_ORDER)
        ),
    )


def _render_section_b(dist, lang):
    st.markdown(f"### {_t('section_b_heading', lang)}")
    st.caption(_t("section_b_why", lang))
    _render_distribution_chart(dist, lang)
    tiles = [
        _tile_html(_t("tile_worst", lang), f"{dist['worst']:+.1f}%", "#fb7185"),
        _tile_html(_t("tile_best", lang), f"{dist['best']:+.1f}%", "#34d399"),
        _tile_html(_t("tile_swing", lang), f"±{dist['typical_swing']:.1f}%"),
    ]
    st.markdown(_tiles_grid_html(tiles), unsafe_allow_html=True)
    st.caption(_t("section_b_caption", lang))


def _render_section_c(base, quote, current_rate, average, sigma, lang):
    st.markdown(f"### {_t('section_c_heading', lang)}")
    st.caption(_t("section_c_why", lang, base=base))
    position = st.number_input(
        _t("position_label", lang, base=base), min_value=0.0, value=50000.0, step=1000.0,
        format="%.0f", key="cr_position_size",
    )
    impacts = {imp["key"]: imp for imp in cre.position_impact(position, current_rate, average, sigma)}
    tiles = []
    for key, label_key in (
        ("average", "scenario_average_label"),
        ("plus_1sigma", "scenario_plus_1sigma_label"),
        ("minus_1sigma", "scenario_minus_1sigma_label"),
    ):
        imp = impacts.get(key)
        if not imp:
            continue
        label = _t(label_key, lang, base=base, rate=f"{imp['scenario_rate']:.4f}")
        color = "#34d399" if imp["pct_change"] >= 0 else "#fb7185"
        sign = "+" if imp["amount_change"] >= 0 else "−"
        value = f"{imp['pct_change']:+.1f}% · {sign}{base}${abs(imp['amount_change']):,.0f}"
        tiles.append(_tile_html(label, value, color))
    st.markdown(_tiles_grid_html(tiles), unsafe_allow_html=True)
    st.caption(_t("section_c_caption", lang))


def _render_mos_view_table(base, quote, ticker, lang, is_owner=False, ticker_currency="", mos=None):
    """SECTION B, COMMIT B3 (amended per the Director's 5 Oct 2026,
    6 Oct 2026, PART A/STEP A1, and "one fix to PART A" follow-ups) of
    instruction_top200_amendments_and_currency_view.md / instruction_
    portfolio_scoring_and_currency_table.md: "one extra small table
    under section C, 'Margin of safety, {base} view', with the three
    scenarios, the currency effect and the resulting margin" - only
    ever called when render_currency_risk_page() has an active ticker
    in st.session_state["cr_active_ticker"] (set from a Deep Dive
    currency-risk note's jump, carried in the URL's own ?ticker= query
    param rather than a one-shot session key, and PERSISTED in session
    state from there across every later rerun of this page until the
    visitor closes it with the real Close button below, or a different
    jump overwrites it).

    Fix history, shortest version (full account in this instruction's
    own reports): a live report (owner, home currency AUD, INTU) first
    showed this section rendering NOTHING even though the jump reached
    this page correctly, because this function used to read snap.get(
    "mos_pct")/snap.get("currency") directly off snapshot_store.
    get_snapshot()'s raw "data" dict - a key NEITHER real producer
    ever writes (margin of safety is stored as "MOS %", and NEITHER
    producer writes a currency field at all, under any key). PART A
    STEP A1 first fixed currency by resolving it live via deep_dive_
    engine.analyze() while leaving margin of safety on the stored-row
    read (via snapshot_store.public_view()). The Director's own "one
    fix to PART A" follow-up (part b) then required removing that
    stored-row read entirely: "do not mix a stored-row margin of
    safety with a live currency" - a stored row can disagree with the
    SAME ticker's live analyze() result, which is exactly the kind of
    silent mismatch this instruction exists to prevent. Both `mos` and
    `ticker_currency` now come from the ONE deep_dive_engine.analyze()
    call app.py's _resolve_ticker_mos_currency_live() makes - the SAME
    fields (_dd["mos"]/_dd["currency"]) the Deep Dive currency note
    itself reads. This function itself has no deep_dive_engine or
    snapshot_store coupling at all, and stays dependency-light, by
    design.

    This function ALWAYS renders something when called (a plain,
    visitor-safe line naming why the table can't show, never a bare
    silent gap), plus an owner-only caption with the exact failed
    condition, instead of silently returning nothing. The close/
    dismiss behaviour is unchanged and available in every case, not
    just when the table itself renders.

    cve.mos_view()'s own single-source-of-truth call into currency_
    risk_engine.position_impact() is the SAME function section C's own
    tiles above just used - never a second, independently-computed
    effect."""
    ticker_currency = (ticker_currency or "").upper()

    view = None
    _plain_key = None
    _plain_kwargs = {"ticker": ticker}
    _owner_detail = None
    if mos is None:
        _plain_key = "mos_view_unavailable_no_valuation"
        _owner_detail = (
            "app.py's _resolve_ticker_mos_currency_live() could not confirm a margin of "
            "safety for this ticker right now (deep_dive_engine.analyze() errored, or "
            "returned no intrinsic value to derive one from)"
        )
    elif not ticker_currency:
        _plain_key = "mos_view_unavailable_no_currency"
        _owner_detail = (
            "app.py's _resolve_ticker_mos_currency_live() could not confirm a trading "
            "currency for this ticker right now (deep_dive_engine.analyze() errored, "
            "or returned its own \"no currency on file\" sentinel)"
        )
    elif ticker_currency == base:
        _plain_key = "mos_view_unavailable_same_currency"
        _owner_detail = (
            f"ticker currency ({ticker_currency}) equals the selected home/From "
            f"currency ({base}) - nothing to convert, not a failure"
        )
    elif ticker_currency != quote:
        _plain_key = "mos_view_unavailable_currency_mismatch"
        _plain_kwargs["currency"] = ticker_currency
        _owner_detail = f"ticker currency ({ticker_currency}) != selected To currency ({quote})"
    else:
        view = cve.mos_view(mos, base, quote)
        if not view:
            _plain_key = "mos_view_unavailable_no_pair"
            _plain_kwargs["base"] = base
            _plain_kwargs["quote"] = quote
            _owner_detail = (
                "currency_view_engine.mos_view() returned no view for this "
                "base/quote pair (e.g. no FX history)"
            )

    _head_col, _close_col = st.columns([5, 1])
    with _head_col:
        if view is not None:
            st.markdown(f"#### {_t('mos_view_heading', lang, base=base, ticker=ticker)}")
            # PART 2 STEP 2.2 (Director, 8 Oct 2026, instruction_health_
            # fixes_chart_and_new_markets.md): "lists three results but
            # not the figure they start from." `mos`/`ticker_currency`
            # are already in hand here (the SAME values that put this
            # branch on the `view is not None` path in the first place -
            # one source of truth, never re-fetched). No dollar sign -
            # a margin of safety is a percentage, formatted exactly like
            # the Deep Dive note's own "+12.3%" style, never a currency
            # amount.
            st.caption(_t("mos_view_starting_figure", lang, ticker=ticker,
                          currency=ticker_currency, mos=f"{mos:+.1f}%"))
        else:
            st.caption(_t(_plain_key, lang, **_plain_kwargs))
            if is_owner:
                st.caption(f"[Owner-only diagnostic] {_owner_detail}")
    with _close_col:
        if st.button(_t("mos_view_close_button", lang), key=f"cr_mos_view_close_{ticker}"):
            st.session_state["cr_active_ticker"] = None
            # Fix (Director, 6 Oct 2026): also drop the URL's own
            # ?ticker= - otherwise reloading this exact URL (or a
            # WebSocket reconnect, the same event that made the ticker
            # need to travel via the URL in the first place) would
            # re-apply it and silently reopen the table right after
            # Close.
            st.query_params.pop("ticker", None)
            st.rerun()
    if view is None:
        return
    rows = []
    for key, label_key, view_val in (
        ("average", "scenario_average_label", view["view_at_average"]),
        ("plus_1sigma", "scenario_plus_1sigma_label", None),
        ("minus_1sigma", "scenario_minus_1sigma_label", None),
    ):
        sc = view["effects"]["scenarios"].get(key)
        if not sc:
            continue
        if view_val is None:
            # the two +-1sigma rows: recover each one's OWN view (not
            # just the min/max range) from the same value_factor
            # mos_view() already computed it from - never a second,
            # independently-derived number.
            view_val = (1.0 - (1.0 - mos / 100.0) / sc["value_factor"]) * 100.0
        rows.append({
            _t("mos_view_col_scenario", lang): _t(label_key, lang, base=base,
                                                   rate=f"{sc['scenario_rate']:.4f}"),
            _t("mos_view_col_effect", lang): f"{sc['pct_change']:+.1f}%",
            _t("mos_view_col_margin", lang): f"{view_val:+.1f}%",
        })
    st.dataframe(rows, hide_index=True, width='stretch')
    st.caption(_t("mos_view_caption", lang, ticker=ticker, stock=quote))


def render_currency_risk_page(lang="en", default_base=None, jump_ticker=None, is_owner=False,
                               active_ticker_currency="", active_ticker_mos=None):
    """The Currency Risk page's full content - app.py's page_currency_
    risk() calls this after its own _content_page_shell()/_bump_page_
    view() (same split as top100_render.render_top100_page()).

    default_base/jump_ticker: SECTION B, COMMIT B3 (5 Oct 2026) -
    default_base pre-selects the From picker for a signed-in visitor
    with a home currency (None reproduces today's exact default);
    jump_ticker, only ever set when arriving from a Deep Dive currency-
    risk note, seeds the PERSISTED st.session_state["cr_active_ticker"]
    (amended per the Director's 5 Oct 2026 follow-up - the one extra
    table under section C used to depend on jump_ticker being passed
    on THIS exact call, which made it vanish the moment any other
    widget on the page triggered a rerun; it now reads the persisted
    active ticker instead, which survives every rerun until the
    visitor closes it with _render_mos_view_table()'s own Close button
    or a fresh jump_ticker overwrites it with a different ticker).
    is_owner (Director, 6 Oct 2026 follow-up): whether THIS visitor is
    the real owner (ai_gate.is_owner(), never just "the feature is
    visible to them") - passed straight through to _render_mos_view_
    table() so only the owner ever sees its diagnostic caption.
    active_ticker_currency/active_ticker_mos (Director, 6 Oct 2026,
    PART A STEP A1, amended by the "one fix to PART A" follow-up, part
    b): the active ticker's own trading currency AND margin of safety,
    both already resolved LIVE, from the SAME deep_dive_engine.
    analyze() call, by app.py's _resolve_ticker_mos_currency_live()
    (the same function the Deep Dive note itself uses) - "" / None
    when there is no active ticker, or it couldn't be resolved. Passed
    straight through to _render_mos_view_table(); this module has no
    deep_dive_engine or snapshot_store coupling of its own - margin of
    safety is NEVER read from a stored row here any more, specifically
    so it can never disagree with the live currency it's shown beside.
    Neither default_base nor jump_ticker changes anything else about
    this page - a call with both left at their defaults (every pre-
    existing caller before COMMIT B3, and every signed-out/non-owner-
    while-the-switch-is-off visitor after it) renders byte-identical to
    before that commit."""
    if jump_ticker:
        st.session_state["cr_active_ticker"] = jump_ticker
    st.markdown(
        "<div style='color:#8aa0b8;font-size:12.5px;margin-bottom:10px;'>"
        f"{html.escape(_t('subtitle', lang))}</div>",
        unsafe_allow_html=True,
    )
    st.caption(_t("disclaimer", lang))

    base, quote, range_key = _render_selector_bar(lang, default_base=default_base)
    if base == quote:
        st.warning(_t("same_currency_warning", lang))
        return

    history = cre.get_fx_history(base, quote)
    if not history:
        st.info(_t("no_data", lang, base=base, quote=quote))
        return
    if history["stale"]:
        st.caption(_t("stale_warning", lang))

    dates, closes = cre.slice_range(history["dates"], history["closes"], range_key)
    stats = cre.period_stats(closes)
    if not stats:
        st.info(_t("no_data", lang, base=base, quote=quote))
        return

    st.markdown(
        f"<div style='font-size:26px;font-weight:800;margin:6px 0 14px;color:#e6edf5;'>"
        f"{stats['today']:.4f} <span style='font-size:12px;color:#8aa0b8;font-weight:400;'>"
        f"{html.escape(_t('hero_caption', lang, quote=quote, base=base))}</span></div>",
        unsafe_allow_html=True,
    )

    _render_section_a(dates, closes, stats, base, quote, lang)

    dist = cre.rolling_12m_distribution(closes)
    if dist:
        _render_section_b(dist, lang)
    else:
        st.caption(_t("not_enough_data_for_distribution", lang))

    _render_section_c(base, quote, stats["today"], stats["average"], stats["sigma"], lang)

    _active_ticker = st.session_state.get("cr_active_ticker")
    if _active_ticker:
        _render_mos_view_table(base, quote, _active_ticker, lang, is_owner=is_owner,
                                ticker_currency=active_ticker_currency, mos=active_ticker_mos)
