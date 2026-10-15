"""
Director PART 2, TASK B (MOS% display rule).

One shared snapshot_store.display_mos_pct() used at every human-facing
MOS% site: snapshot page (stat grid, copy-text, meta description), OG
card, Scanner (overnight + comparison tables), Deep Dive headline,
Top 200/public shelf cards, blog stat card, portfolio "Cheap vs
healthy" chart. Shows the localized "Model unreliable" string instead
of a percentage when the row's dcf_unreliable_symmetric (or, where
that flag isn't threaded through yet, dcf_unreliable) flag is True;
otherwise the number is unchanged. API/export keep the raw number and
gain a new boolean mos_unreliable field.

Display only - no valuation, score or selection change. Run directly:
python3 tests/test_director_next_batch_taskB_mos_unreliable_display.py
"""
import sys

sys.path.insert(0, ".")

_fail = []
_total = [0]


def check(label, condition):
    _total[0] += 1
    status = "OK" if condition else "FAIL"
    if not condition:
        _fail.append(label)
    print(f"  {status}: {label}")


# ======================================================================
# 1. snapshot_store.display_mos_pct() - the shared function itself
# ======================================================================
import snapshot_store

check("reliable, default fmt: '+12.3%'",
      snapshot_store.display_mos_pct(12.3, False) == "+12.3%")
check("reliable, negative value keeps sign: '-5.0%'",
      snapshot_store.display_mos_pct(-5.0, False) == "-5.0%")
check("reliable, custom fmt (unsigned 1dp, top100_render's own convention): '12.3%'",
      snapshot_store.display_mos_pct(12.3, False, fmt="{:.1f}%") == "12.3%")
check("unreliable overrides the number entirely (EN)",
      snapshot_store.display_mos_pct(999.0, True) == "Model unreliable")
check("unreliable overrides the number entirely (ES)",
      snapshot_store.display_mos_pct(999.0, True, lang="es") == "Modelo no confiable")
check("unreliable wins even if mos_pct is itself None",
      snapshot_store.display_mos_pct(None, True) == "Model unreliable")
check("reliable + None mos_pct returns None (caller keeps its own '-'/'—' placeholder)",
      snapshot_store.display_mos_pct(None, False) is None)

import i18n
check("i18n key exists and matches EN", i18n.EN.get("mos.model_unreliable") == "Model unreliable")
check("i18n key exists and matches ES", i18n.ES.get("mos.model_unreliable") == "Modelo no confiable")


# ======================================================================
# 2. deep_dive_engine.analyze()'s new symmetric flag - unit-level via
# resolver_engine.dcf_looks_unreliable() directly (the exact call
# deep_dive_engine.py now makes), plus a source-text check that the
# new key is actually wired into analyze()'s return dict next to the
# existing (non-symmetric) one.
# ======================================================================
import inspect
import resolver_engine
import deep_dive_engine

check("dcf_looks_unreliable(symmetric=True) catches the LOW-side case too "
      "(intrinsic far BELOW price) - the gap the plain flag misses",
      resolver_engine.dcf_looks_unreliable(10.0, 100.0, symmetric=True) is True
      and resolver_engine.dcf_looks_unreliable(10.0, 100.0, symmetric=False) is False)

_dd_src = inspect.getsource(deep_dive_engine.analyze)
check("deep_dive_engine.analyze()'s return dict carries a NEW "
      "dcf_unreliable_symmetric key (additive - the existing "
      "dcf_unreliable key/line is untouched)",
      '"dcf_unreliable": dcf_looks_unreliable(intrinsic_value, current_price),' in _dd_src
      and '"dcf_unreliable_symmetric": dcf_looks_unreliable(' in _dd_src
      and "symmetric=True" in _dd_src)


# ======================================================================
# 3. portfolio_health_engine.compute_health_components()/_v2(): the
# Valuation component gains an "unreliable" key in the model-based
# path (real callable, pure Python, no network).
# ======================================================================
import portfolio_health_engine as phe

# A snapshot shaped like an extreme low-side DCF artifact: intrinsic
# far BELOW price (the symmetric-only case the plain/high-side-only
# flag used elsewhere in this codebase would miss).
_snap_bad = {
    "price": 100.0, "intrinsic_value": 10.0, "mos_pct": -900.0,
    "quality_score": 50, "revenue_growth": 5, "earnings_growth": 5,
    "profit_margin": 10, "roe": 10, "debt_to_equity": 1, "fcf_growth": 5,
}
comps_bad = phe.compute_health_components(_snap_bad, "STOCK")
check("compute_health_components(): Valuation component flags unreliable=True "
      "for an extreme low-side DCF artifact",
      comps_bad.get("Valuation", {}).get("unreliable") is True)

_snap_ok = dict(_snap_bad, intrinsic_value=120.0, mos_pct=16.7)
comps_ok = phe.compute_health_components(_snap_ok, "STOCK")
check("compute_health_components(): Valuation component is unreliable=False "
      "for an ordinary DCF value",
      comps_ok.get("Valuation", {}).get("unreliable") is False)

comps_etf = phe.compute_health_components(_snap_bad, "ETF")
check("compute_health_components(): ETF path (no DCF model) never sets "
      "unreliable=True (key simply absent, same as before this change)",
      not comps_etf.get("Valuation", {}).get("unreliable"))

comps_bad_v2 = phe.compute_health_components_v2(_snap_bad, "STOCK")
check("compute_health_components_v2(): same unreliable=True wiring as v1",
      comps_bad_v2.get("Valuation", {}).get("unreliable") is True)


# ======================================================================
# 4. snapshot_render.py - stat grid, _valuation_note, copy-text, meta
# description. All pure functions of a `pub` dict (snapshot_store.
# public_view()'s own output shape) - no DB/network needed.
# ======================================================================
import snapshot_render as sr

_pub_unreliable = {
    "valuation_label": "Overvalued", "mos_pct": -900.0, "price": 100.0,
    "intrinsic_value": 10.0, "dcf_unreliable_symmetric": True,
    "value_score": 40.0, "quality": 50.0, "psychology": 60.0, "discovery": 70.0,
}
_pub_reliable = dict(_pub_unreliable, mos_pct=16.7, intrinsic_value=120.0,
                      dcf_unreliable_symmetric=False)

check("_valuation_note(): shows 'Model unreliable' in place of the number "
      "when flagged",
      "Model unreliable" in sr._valuation_note(_pub_unreliable, lang="en"))
check("_valuation_note(): unchanged (still a real percentage) when not flagged",
      "+16.7%" in sr._valuation_note(_pub_reliable, lang="en"))

_en_cells = {label: value for label, value, _hint in sr._stat_cells(_pub_unreliable, moat=None, lang="en")}
check("_stat_cells() EN: 'Margin of safety' cell shows 'Model unreliable'",
      _en_cells.get("Margin of safety") == "Model unreliable")
_es_cells = {label: value for label, value, _hint in sr._stat_cells(_pub_unreliable, moat=None, lang="es")}
check("_stat_cells() ES: 'Margen de seguridad' cell shows the ES string",
      _es_cells.get("Margen de seguridad") == "Modelo no confiable")
_en_cells_ok = {label: value for label, value, _hint in sr._stat_cells(_pub_reliable, moat=None, lang="en")}
check("_stat_cells() EN: unchanged numeric format when not flagged",
      _en_cells_ok.get("Margin of safety") == "+16.7%")

_copy_text_unreliable = sr._snapshot_copy_text(
    "TST", _pub_unreliable, moat=None, generated="2026-10-14", universe="Test",
    lede="x", base_url="https://stocksdeepdive.com",
)
check("_snapshot_copy_text(): 'Copy as text' payload shows 'Model unreliable'",
      "Margin of safety: Model unreliable" in _copy_text_unreliable)

# render_snapshot() takes the RAW internal row (it calls public_view()
# on snap["data"] itself) - unlike _valuation_note/_stat_cells/
# _snapshot_copy_text above, which all take pub (public_view()'s own
# OUTPUT) directly, per their own docstrings.
_raw_row_unreliable = {
    "Price": 100.0, "Intrinsic Value": 10.0, "MOS %": -900.0,
    "Valuation": "Overvalued", "Value Score": 40.0, "Quality": 50.0,
    "DCF Unreliable (Symmetric)": True,
}
_html_en = sr.render_snapshot(
    {"ticker": "TST", "universe": "Test", "generated_at": "2026-10-14",
     "data": _raw_row_unreliable, "moat": None},
    base_url="https://stocksdeepdive.com", lang="en",
)
check("meta description (EN) embeds 'Model unreliable' instead of a percentage",
      "Model unreliable" in _html_en)
_html_es = sr.render_snapshot(
    {"ticker": "TST", "universe": "Test", "generated_at": "2026-10-14",
     "data": _raw_row_unreliable, "moat": None},
    base_url="https://stocksdeepdive.com", lang="es",
)
check("meta description (ES) embeds the ES unreliable string",
      "Modelo no confiable" in _html_es)


# ======================================================================
# 5. top100_render.py - leading metric / secondary metrics / shelf row,
# all re-deriving the symmetric flag fresh from intrinsic_value/price
# (same pattern _dcf_unreliable_chip_html() already uses, symmetric=True
# instead of its own symmetric=False).
# ======================================================================
import top100_render as tr

_row_unreliable = {
    "ticker": "BAD", "mos_pct": -900.0, "intrinsic_value": 10.0, "price": 100.0,
    "value_score": 40.0, "composite": 55.0, "dividend_yield_pct": None,
}
_row_reliable = dict(_row_unreliable, intrinsic_value=120.0, price=100.0, mos_pct=16.7)

check("top100_render._mos_unreliable(): True for the low-side artifact row",
      tr._mos_unreliable(_row_unreliable) is True)
check("top100_render._mos_unreliable(): False for an ordinary row",
      tr._mos_unreliable(_row_reliable) is False)

_label, _value = tr._leading_metric(_row_unreliable, tr.SORT_VALUE_TODAY, "en")
check("_leading_metric() (MOS sort mode): shows 'Model unreliable' when flagged",
      _value == "Model unreliable")
_label_ok, _value_ok = tr._leading_metric(_row_reliable, tr.SORT_VALUE_TODAY, "en")
check("_leading_metric(): unchanged '16.7%' format when not flagged",
      _value_ok == "16.7%")

_sec_html = tr._secondary_metrics_html(_row_unreliable, tr.SORT_VALUE_SCORE, "en")
check("_secondary_metrics_html(): 'Model unreliable' appears in place of the MOS %",
      "Model unreliable" in _sec_html and "-900.0%" not in _sec_html)

_shelf_html = tr._shelf_row_html(_row_unreliable, "en")
check("_shelf_row_html(): bottom-shelf (NOT RATED) row also shows 'Model unreliable'",
      "Model unreliable" in _shelf_html)


# ======================================================================
# 6. blog_render.py stat card - same pub-dict shape, pure function.
# ======================================================================
import blog_render as br
import html as _html_mod

_cells_html = br._ticker_snapshot_strip_html.__globals__.get("snapshot_store")
# _ticker_snapshot_strip_html itself reads from the blog/snapshot DB, so
# it isn't a pure function of an in-memory dict - covered instead by a
# direct check that it calls through display_mos_pct() (source-level,
# since a full DB-backed call is out of scope for this unit test).
check("blog_render._ticker_snapshot_strip_html() source calls "
      "snapshot_store.display_mos_pct() for its MOS cell",
      "snapshot_store.display_mos_pct(" in inspect.getsource(br._ticker_snapshot_strip_html))


# ======================================================================
# 7. og_card_render.py - smoke test: renders without raising for both a
# flagged and an unflagged `public` dict, and the source shows the bar
# fill is skipped (not fed a meaningless fill_frac) when flagged.
# ======================================================================
import og_card_render as ogr

_card_bytes_unreliable = ogr.render_ticker_card(
    "TST", _pub_unreliable, generated_at="2026-10-14T00:00:00Z", moat=None
)
check("og_card_render.render_ticker_card(): renders valid PNG bytes for a "
      "flagged row without raising", isinstance(_card_bytes_unreliable, bytes)
      and len(_card_bytes_unreliable) > 100)
_card_bytes_reliable = ogr.render_ticker_card(
    "TST", _pub_reliable, generated_at="2026-10-14T00:00:00Z", moat=None
)
check("og_card_render.render_ticker_card(): renders valid PNG bytes for an "
      "ordinary row too", isinstance(_card_bytes_reliable, bytes)
      and len(_card_bytes_reliable) > 100)
check("og_card_render source shows the bar/fill math is skipped (not fed a "
      "meaningless fraction) for a flagged row",
      "if mos_unreliable:" in inspect.getsource(ogr.render_ticker_card))


# ======================================================================
# 8. api_v1.py - mos_unreliable field, raw mos_pct unchanged.
# ======================================================================
import api_v1

# _snapshot_payload/_public_scan_row both call snapshot_store.
# public_view() themselves - same RAW-internal-row requirement as
# render_snapshot() above, not the already-public `pub` shape.
_raw_row_reliable = dict(_raw_row_unreliable, **{
    "Intrinsic Value": 120.0, "MOS %": 16.7, "DCF Unreliable (Symmetric)": False,
})

_snap_payload = api_v1._snapshot_payload(
    {"ticker": "TST", "universe": "Test", "generated_at": "2026-10-14",
     "data": _raw_row_unreliable, "moat": None}
)
check("api_v1._snapshot_payload(): raw mos_pct is UNCHANGED (API never swaps the number)",
      _snap_payload.get("mos_pct") == -900.0)
check("api_v1._snapshot_payload(): new boolean mos_unreliable field is True",
      _snap_payload.get("mos_unreliable") is True)

_scan_row_payload = api_v1._public_scan_row(dict(_raw_row_unreliable, Ticker="TST"))
check("api_v1._public_scan_row(): same mos_unreliable field, raw number kept",
      _scan_row_payload.get("mos_pct") == -900.0
      and _scan_row_payload.get("mos_unreliable") is True)

_snap_payload_ok = api_v1._snapshot_payload(
    {"ticker": "TST", "universe": "Test", "generated_at": "2026-10-14",
     "data": _raw_row_reliable, "moat": None}
)
check("api_v1._snapshot_payload(): mos_unreliable is False for an ordinary row",
      _snap_payload_ok.get("mos_unreliable") is False)


# ======================================================================
# 9. data_export_engine.py - Deep Dive export keeps the raw MOS% number
# and gains a boolean row.
# ======================================================================
import data_export_engine as dee

_dd_fixture_unreliable = {
    "price": 100.0, "currency": "USD", "valuation": "Overvalued",
    "intrinsic_value": 10.0, "mos": -900.0, "dcf_unreliable_symmetric": True,
    "value_default": False, "dcf_growth": 5.0, "dcf_discount": 9.0,
    "dcf_perpetual": 2.5, "growth_governor": None,
}
_export_rows = dict(dee._valuation_rows(_dd_fixture_unreliable))
check("data_export_engine._valuation_rows(): raw 'Margin of Safety %' kept as-is",
      _export_rows.get("Margin of Safety %") == -900.0)
check("data_export_engine._valuation_rows(): new boolean 'Margin of Safety "
      "Unreliable' row is True",
      _export_rows.get("Margin of Safety Unreliable") is True)

_dd_fixture_ok = dict(_dd_fixture_unreliable, intrinsic_value=120.0, mos=16.7,
                      dcf_unreliable_symmetric=False)
_export_rows_ok = dict(dee._valuation_rows(_dd_fixture_ok))
check("data_export_engine._valuation_rows(): boolean row is False for an "
      "ordinary DCF value",
      _export_rows_ok.get("Margin of Safety Unreliable") is False)


# ======================================================================
# 10. Scanner/Comparison tables (app.py) - source-level checks only
# (app.py requires a live Streamlit session to execute end-to-end;
# these confirm the wiring exists exactly where TASK B asked for it).
# ======================================================================
import app

_overnight_src = inspect.getsource(app._render_overnight_scan_table)
check("app.py overnight scan table: MOS cell checks "
      "'DCF Unreliable (Symmetric)' before falling back to the bar",
      "_orow.get(\"DCF Unreliable (Symmetric)\")" in _overnight_src
      and "mos.model_unreliable" in _overnight_src)

_cmp_src = inspect.getsource(app._render_scan_results)
check("app.py comparison table: MOS cell checks the row's own "
      "'_mos_unreliable' key before falling back to the bar",
      'r.get("_mos_unreliable")' in _cmp_src and "mos.model_unreliable" in _cmp_src)
check("app.py comparison table: BOTH row-building sites "
      "(_comparison_row_from_saved_scan + the live-fetch loop inside "
      "_render_scan_results) set _mos_unreliable - not just one",
      '"_mos_unreliable": bool(row.get("DCF Unreliable (Symmetric)"))'
      in inspect.getsource(app._comparison_row_from_saved_scan)
      and '"_mos_unreliable": dcf_looks_unreliable(intrinsic_value, current_price, symmetric=True)'
      in _cmp_src)

_dd_headline_src = inspect.getsource(app).split("def _dd_valuation()")[1][:1200]
check("app.py Deep Dive headline subheader calls display_mos_pct() "
      "with the new symmetric flag",
      "snapshot_store.display_mos_pct(" in _dd_headline_src
      and "dcf_unreliable_symmetric" in _dd_headline_src
      and 'st.subheader(f"Margin of Safety: {_mos_headline}' in _dd_headline_src)

_chart_src = inspect.getsource(app._render_portfolio_cheap_healthy_map)
check("app.py portfolio 'Cheap vs healthy' chart: hover text uses "
      "customdata (display_mos_pct per point), never the raw %{x} format",
      "customdata=_mos_hover" in _chart_src
      and "hovertemplate=\"%{text}<br>MOS %{customdata}" in _chart_src
      and "%{x:.1f}%" not in _chart_src)
check("app.py portfolio chart: the scatter point's actual x-position "
      "(and the Valuation score/band behind it) is UNCHANGED - only the "
      "hover text differs for a flagged point",
      '"mos": _mos,' in _chart_src and "_xs = [p[\"mos\"] for p in _company_pts]" in _chart_src)


# ======================================================================
# 11. Display only: the selection/scoring engine itself is untouched by
# this task. top100_engine.py was touched by TASK A (committed
# separately, before this task) - confirm TASK B added no further
# change to it (git diff of this task's own commit, checked by the
# caller/CI, not here) and that the two pure-function engines TASK B
# DID touch (deep_dive_engine.analyze, portfolio_health_engine.compute_
# health_components) only ADDED new dict keys, never replaced an
# existing one - every pre-existing key this test exercises is still
# present and still means what it meant before.
# ======================================================================
check("deep_dive_engine.analyze()'s pre-existing (non-symmetric) "
      "dcf_unreliable key/call is still there, byte-for-byte, next to "
      "the new one (additive, not a replacement)",
      '"dcf_unreliable": dcf_looks_unreliable(intrinsic_value, current_price),' in _dd_src)
check("portfolio_health_engine: the Valuation component's pre-existing "
      "score/current/note keys are unaffected by the new 'unreliable' key",
      comps_ok["Valuation"]["score"] is not None and comps_ok["Valuation"]["current"] == 16.7)


print(f"\nPASS={_total[0] - len(_fail)} FAIL={len(_fail)}")
if _fail:
    sys.exit(1)
