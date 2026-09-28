"""
Owner-reported (28 Sep 2026): the "Imported screen (TradingView,
admin-only)" section was showing up on the Compare tab. It originally
shipped on Stock Scanner (commit d5060533, 27 Aug 2026), then moved to
Comparison the same day (commit 62117c87) - not part of Compare Commit 3
(a4afbba/70f86f3, 27 Sep 2026), a full month later and an unrelated
feature, despite the surface-level "Compare" name similarity.

Fix verified here: the section now lives on the Admin Dashboard
(page_admin_dashboard()) as its own section, and neither page_comparison()
nor page_scanner() reference it at all. Static, source-level checks -
app.py is a single Streamlit script; rendering page_admin_dashboard()
end-to-end would require mocking ai_gate/admin_metrics_store/paywall_engine
plus Streamlit's session machinery for no extra confidence over reading
the actual call graph directly, the same tradeoff several prior
app.py-only admin-panel commits in this repo made (e.g. the B1/A6
commits' own "grep-verify X never enters Y" checks).

Run: python3 tests/test_imported_screen_admin_relocation.py
"""
import os
import re

_APP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
with open(_APP_PATH, encoding="utf-8") as f:
    SRC = f.read()


def _function_span(src, name):
    """Source text of def `name`(...) up to (not including) the next
    top-level (column-0) `def `/`class ` - good enough for these
    page_*() functions, which are all top-level and don't nest another
    top-level def inside them."""
    start = re.search(rf"(?m)^def {re.escape(name)}\(", src)
    assert start, f"def {name}( not found in app.py"
    nxt = re.search(r"(?m)^(def |class )", src[start.end():])
    end = start.end() + nxt.start() if nxt else len(src)
    return src[start.start():end]


# ---- Defined exactly once ----
assert SRC.count("def _render_screen_import_admin(") == 1
print("[defined_once] _render_screen_import_admin() has exactly one definition OK")

# ---- page_admin_dashboard() calls it, and is still owner-gated ----
_admin_body = _function_span(SRC, "page_admin_dashboard")
assert "_render_screen_import_admin()" in _admin_body
assert "ai_gate.is_owner(" in _admin_body
print("[admin_dashboard_calls_it] page_admin_dashboard() renders the section and is still "
      "gated by ai_gate.is_owner() OK")

# ---- Comparison shows nothing from it ----
_cmp_body = _function_span(SRC, "page_comparison")
assert "_render_screen_import_admin" not in _cmp_body
print("[comparison_shows_nothing] page_comparison() no longer references "
      "_render_screen_import_admin at all OK")

# ---- Scanner (where it originally shipped) shows nothing from it either ----
_scanner_body = _function_span(SRC, "page_scanner")
assert "_render_screen_import_admin" not in _scanner_body
print("[scanner_shows_nothing] page_scanner() never references _render_screen_import_admin OK")

# ---- Exactly one unguarded call site in the whole file ----
# (a bare `_render_screen_import_admin()` statement on its own line -
# distinguishes an actual call from the several docstring/comment
# mentions of the function by name elsewhere in this file.)
_calls = [ln for ln in SRC.splitlines() if ln.strip() == "_render_screen_import_admin()"]
assert len(_calls) == 1
print("[one_call_site] exactly one call to _render_screen_import_admin() in app.py OK")

# ---- The old _factual() self-gate is gone (would silently blank the ----
# ---- section for the real owner after an explicit "Exit full view") ----
_def_start = re.search(r"(?m)^def _render_screen_import_admin\(", SRC)
_func_head = SRC[_def_start.start():_def_start.start() + 3500]
assert "if _factual():" not in _func_head
print("[no_factual_self_gate] the function no longer self-gates on _factual() - "
      "page_admin_dashboard()'s own ai_gate.is_owner() check is the only gate now OK")

print("\nALL IMPORTED-SCREEN-ADMIN-RELOCATION FIXTURES PASSED")
