# Report: SECTION B of `instruction_top200_amendments_and_currency_view.md` (5 Oct 2026) - home currency and the currency note

For the Director. All four commits (B1-B4) are **built, tested, and
committed locally** on `main`. **Nothing has been pushed** - per the
instruction's own rule, SECTION B is pushed separately, on its own go,
after the Top 200 commits are live and the Director has checked them.

Local commits:
- `80dcbaf` - COMMIT B1 (home currency on the account)
- `ef154fa` - COMMIT B2 (the calculation and the Deep Dive note)
- `3386585` - COMMIT B3 (Currency Risk tool additions)
- `2390211` - COMMIT B4 (Portfolio currency exposure)

---

## 1. STEP B0 - the six answers (read-only, done before any code)

1. **How accounts store preferences today, and how the language choice is stored/applied:** there was no per-account preference store anywhere in this app before this section. The EN/ES language choice is a **browser cookie/session value**, not an account setting - `st.session_state["lang"]` is the single live source of truth (`app.py`'s `_init_lang()`), resolved each session from a long-lived `sdd_lang` cookie, then `?lang=es`, then the browser's `Accept-Language` header, then `"en"`. There is one unrelated, write-once, DB-backed `lang` column on `email_auth.py`'s `signups` table, but it's stored only at first sign-up for picking a broadcast-email template variant - it is never read by `_init_lang()`/`i18n.t()`, so it isn't a live preference store either. Portfolio has no existing base-currency setting (confirmed in answer 5 below). Conclusion acted on: COMMIT B1 builds a genuinely new, small store (`account_currency_store.py`), not an extension of anything that already existed.
2. **How the EN/ES switch is rendered, and room for a second widget:** a real `st.segmented_control` (`app.py`'s `_render_lang_picker()`), mounted as `paywall_engine.render_account_bar()`'s third extra-widget slot (`extra_widget3`), beside Sign out. Yes - a second real widget fits beside it; COMMIT B1 added a fourth slot (`extra_widget4`) the same way.
3. **The Currency Risk tool:** modules `currency_risk_engine.py` (logic) / `currency_risk_render.py` (markup). `CURRENCIES = ["AUD", "USD", "EUR", "GBP", "JPY", "NZD", "CAD", "SGD"]` for both From/To pickers (`currency_risk_engine.py`). History comes from `get_fx_history(base, quote)` - a daily-close Yahoo fetch, disk-cached one JSON file per pair (`currency_risk_cache/{BASE}{QUOTE}.json`, `CACHE_TTL_SECONDS = 24*3600`), falling back to a stale cached copy on a failed live fetch rather than ever showing nothing. Section C's function is `position_impact(position_size, current_rate, average, sigma)` (`currency_risk_engine.py`). The default window is `DEFAULT_RANGE = "10y"` - a selectable chip (`5y`/`10y`/`20y`/`max`), not a hardcoded cutoff; the underlying fetched history is always Yahoo's full `period="max"` series, only the stat/display window defaults to 10y.
4. **Margin of safety on the Deep Dive, and the listing-currency field:** `app.py`'s `_dd_valuation()`, the `st.subheader(f"Margin of Safety: {_mos_val:+.1f}% - ...")` line. The field is the plain `"currency"` key on the Deep Dive's own result dict (sourced from `fcf_valuation_engine.trading_currency_for()`). London pence is already handled upstream of this: `fundamentals_data.py` detects `GBp`/`GBX` by exact code (never by magnitude), divides every price field by 100, and **rewrites `info["currency"]` to `"GBP"`** before anything downstream (including this section's own code) ever reads it - so "London prices quoted in pence are GBP for this purpose" needed no new handling anywhere in SECTION B.
5. **Portfolio:** holdings live in `portfolio_store.py` (`portfolio_holdings`, one row per `(email, portfolio, ticker)`, each carrying its own `currency` column). Totals are always shown in **AUD**, hardcoded (`portfolio_health_engine.to_aud()`/`fx_to_aud()`, and the `income_goal_aud` column name) - **no existing base-currency setting anywhere in Portfolio**, confirmed by a repo-wide search for `home_currency`/`base_currency`/`account_currency`/`preferred_currency` (the only hit, `tests/test_b2_10_results_fcf_base_currency.py`, is about per-period statement-currency conversion for valuation math, unrelated to a display preference). Per the instruction's own "if Portfolio already has a base currency, reuse it" rule: it doesn't, so COMMIT B1's new setting is not a second currency setting next to an existing one. The stress test renders via two `st.dataframe` tables in `app.py` (`_render_stress_scenario_table`, `_render_stress_per_holding_table`).
6. **Owner-only gating pattern:** `ai_gate.is_owner(email)` (`ai_gate.py`), almost always called as `ai_gate.is_owner(paywall_engine.current_user_email())`. Used directly by `currency_view_engine.visible_to(email, is_owner_fn)` throughout this section.

No mismatch between these findings and the instruction surfaced - nothing was stopped on; COMMIT B1 proceeded straight from this inventory.

---

## 2. The commits

### COMMIT B1 - `80dcbaf` - home currency on the account

**What changed:** new `account_currency_store.py` (one row per email, `get_home_currency()`/`set_home_currency()`, no default, rejects anything outside `currency_risk_engine.CURRENCIES`); new `currency_view_engine.py` (the `CURRENCY_VIEW_LIVE` switch, `visible_to()`); `paywall_engine.py` gains a fourth account-bar widget slot (`extra_widget4`), wired into only the three already-signed-in render branches; `app.py` gains `_render_currency_picker()`/`_currency_picker_body()` (a real `st.popover` + `st.selectbox`) and its two mount points (page header, Home page); `i18n.py` gains `account.home_currency_*` (EN/ES).

| Test | Result |
|---|---|
| Store: set, change, read back | pass |
| Store: unset for a new account | pass |
| Store: invalid currency rejected | pass |
| `is_currency_view_live()`/`visible_to()`, every switch/owner combination | pass |
| `render_account_bar()`: `extra_widget4` never invoked for a signed-out visitor, either switch state | pass |
| The real widget: hidden from a non-owner while OFF, visible once ON, options are exactly `CURRENCIES`, pre-selects a saved value, persists a change | pass |

`tests/test_b1_home_currency.py`: **25/25**. Regression at the time: `PASS=106 FAIL=2` (the two standing non-passes, named once in section 6 below and not repeated per commit).

### COMMIT B2 - `ef154fa` - the calculation and the Deep Dive note

**What changed:** `currency_view_engine.py` gains `scenario_effects()`/`mos_view()` - calls `currency_risk_engine`'s own `get_fx_history()` → `slice_range()` → `period_stats()` → `position_impact()` in that order, never a second rate; `app.py` gains `_render_currency_note()` (the five display cases) and splits `_render_currency_picker()` into a reusable body (`_currency_picker_body()`) so the note's "change" link and the account-bar popover share one implementation; `i18n.py` gains `dd.currency_note.*` (EN/ES, currency codes only, no `$`).

| Test | Result |
|---|---|
| The four worked table rows, to one decimal (rows 1-3 exact; row 4 bracketed - see the test file's own note on why the instruction's own row 4 isn't bit-for-bit reproducible) | pass |
| Single source of truth vs. `position_impact()` directly | pass |
| Every not-available/no-note case returns `None` | pass |
| No network call | pass |
| All five display cases render their own exact text | pass |
| `_dd` dict never mutated | pass |
| Non-owner pages render none of this feature's elements while the switch is unset | pass |

`tests/test_b2_currency_view_calc.py`: **21/21**. `tests/test_b2_deepdive_currency_note.py`: **16/16**. Regression: `PASS=108 FAIL=2`.

### COMMIT B3 - `3386585` - Currency Risk tool additions

**What changed:** `app.py`'s `page_currency_risk()` computes the visitor's home currency (gated the same way) and pops the one-shot `currency_risk_jump` session key B2's "See the currency view" button writes; `currency_risk_render.py`'s `_render_selector_bar()` gains `default_base`, and a new `_render_mos_view_table()` reads the jumped ticker's cached MOS (`snapshot_store.get_snapshot()`, no network call, no DCF recompute) and calls the same `mos_view()`; `i18n.py` gains `currency_risk.mos_view_*`.

| Test | Result |
|---|---|
| From picker starts on the home currency when visible | pass |
| Signed-out/non-owner-while-OFF: picker unchanged (still `DEFAULT_BASE`) | pass |
| No table without a ticker | pass |
| No table when signed out, even with a jump present | pass |
| Table appears with a ticker, pair carried over, numbers equal `mos_view()`'s own | pass |
| A ticker whose currency no longer matches the pair gets no table | pass |

`tests/test_b3_currency_risk_tool.py`: **9/9**. Regression: `PASS=109 FAIL=2`.

### COMMIT B4 - `2390211` - Portfolio currency exposure

**What changed:** `app.py`'s new `_render_currency_exposure_table()`, called from `_render_portfolio_stress_tab()` right after the existing per-holding table; groups holdings by their own trading currency, converts through `scenario_effects()`'s own `current_rate` (never `to_aud()`); `i18n.py` gains `portfolio.currency_exposure.*`.

| Test | Result |
|---|---|
| Three-currency fixture: USD row reproduces the task's own -2,418/-17,650/+14,992 (within the same rounding tolerance as COMMIT B2's own worked-example proof) | pass |
| Home-currency row needs no conversion (shows "-", not "not available") | pass |
| A currency with no history shows "not available", excluded from every total/share figure, but still listed | pass |
| Foreign total sums only convertible currencies | pass |
| Home-currency-only portfolio: no spurious total row | pass |
| Switch-off/non-owner: nothing renders | pass |
| No home currency: the B2 prompt in place of the table | pass |

`tests/test_b4_portfolio_currency_exposure.py`: **14/14**. Regression: `PASS=110 FAIL=2`.

---

## 3. Home currencies offered, and where the list came from

Exactly `currency_risk_engine.CURRENCIES` - `AUD, USD, EUR, GBP, JPY, NZD, CAD, SGD`. Per the task's own "do not invent a list" rule, this is the **same list object** the Currency Risk tool's own From/To pickers already use; `account_currency_store.set_home_currency()` raises on anything outside it, so no second list can ever drift from the tool's own.

## 4. Where the control ended up, and why

**Beside the EN/ES language switch**, in the account bar (not the account menu - there is no separate account menu in this app). The header can host a real widget there already (the language picker is one), so the instruction's fallback ("if the header cannot host a real widget, put the control in the account menu") didn't apply. It's a fourth slot (`extra_widget4`) in `paywall_engine._right_widget_columns()`, following the exact pattern the feedback popover/RC-view unlock/language picker already established, wired into only the three signed-in render branches of `render_account_bar()` so "shown only when signed in" is enforced once, at the routing layer, rather than relied on separately at each of the two mount points (page header, Home page).

## 5. The strings, both languages

| Key | EN | ES |
|---|---|---|
| `account.home_currency_set` | Home currency: {currency} | Moneda local: {currency} |
| `account.home_currency_unset` | Home currency: choose | Moneda local: elegir |
| `account.home_currency_caption` | The currency you invest from. Saved to your account and used across the site. | La moneda desde la que inviertes. Se guarda en tu cuenta y se usa en todo el sitio. |
| `account.home_currency_label` | Home currency | Moneda local |
| `account.home_currency_placeholder` | Choose a currency | Elige una moneda |
| `dd.currency_note.signed_out` | Investing from another currency? Sign in to see what currency movement does to this margin of safety. | ¿Inviertes desde otra moneda? Inicia sesión para ver qué le hace el movimiento de la moneda a este margen de seguridad. |
| `dd.currency_note.no_home_set` | Set your home currency to see the currency view. | Elige tu moneda local para ver la vista de moneda. |
| `dd.currency_note.set_home_button` | Set home currency | Elegir moneda local |
| `dd.currency_note.not_available` | The currency view is not available for {stock}/{home} (not enough exchange-rate history for this pair). | La vista de moneda no está disponible para {stock}/{home} (no hay suficiente historial del tipo de cambio para este par). |
| `dd.currency_note.full` | Home currency {home}. This stock is priced in {stock}. If the exchange rate returns to its 10-year average, the margin of safety reads about {avg}%. Within the usual range of the rate it reads between {low}% and {high}%. A described calculation, not a forecast. | Moneda local {home}. Esta acción cotiza en {stock}. Si el tipo de cambio vuelve a su promedio de 10 años, el margen de seguridad sería de aproximadamente {avg}%. Dentro del rango habitual del tipo de cambio, estaría entre {low}% y {high}%. Un cálculo descrito, no una predicción. |
| `dd.currency_note.change_button` | change | cambiar |
| `dd.currency_note.see_tool_button` | See the currency view | Ver la vista de moneda |
| `currency_risk.mos_view_heading` | Margin of safety, {base} view ({ticker}) | Margen de seguridad, vista en {base} ({ticker}) |
| `currency_risk.mos_view_col_scenario` | Scenario | Escenario |
| `currency_risk.mos_view_col_effect` | Currency effect | Efecto de la moneda |
| `currency_risk.mos_view_col_margin` | Resulting margin of safety | Margen de seguridad resultante |
| `currency_risk.mos_view_caption` | {ticker}'s own margin of safety, described in {stock} terms converted through each scenario above - a described calculation, not a forecast. | El margen de seguridad de {ticker}, descrito en términos de {stock} convertido mediante cada escenario anterior - un cálculo descrito, no una predicción. |
| `portfolio.currency_exposure.heading` | Currency exposure | Exposición cambiaria |
| `portfolio.currency_exposure.col_currency` | Currency | Moneda |
| `portfolio.currency_exposure.col_value` | Value | Valor |
| `portfolio.currency_exposure.col_share` | Share of portfolio | Parte de la cartera |
| `portfolio.currency_exposure.col_rate_vs_average` | Rate vs. 10-year average | Tipo vs. promedio de 10 años |
| `portfolio.currency_exposure.col_scenario_average` | If the rate reverts to average | Si el tipo vuelve al promedio |
| `portfolio.currency_exposure.col_scenario_plus` | If the rate reaches +1σ | Si el tipo alcanza +1σ |
| `portfolio.currency_exposure.col_scenario_minus` | If the rate falls to -1σ | Si el tipo cae a -1σ |
| `portfolio.currency_exposure.not_available` | not available | no disponible |
| `portfolio.currency_exposure.total_foreign_label` | Total foreign holdings | Total en monedas extranjeras |
| `portfolio.currency_exposure.caption` | Described calculations, not a forecast - every value is in your home currency ({home}), using the same method as the Currency Risk tool and the Deep Dive currency note. | Cálculos descritos, no una predicción - cada valor está en tu moneda local ({home}), con el mismo método que la herramienta de riesgo cambiario y la nota de moneda del Deep Dive. |

## 6. Confirmation

- No intrinsic value, margin of safety, score, scan row, stored value, or Top 200 output changed by anything in SECTION B - every one of B1-B4's own tests includes a check for this (B2's own "`_dd` dict never mutated" check is the most direct proof at the render layer).
- No new network call on page view - `get_fx_history()` is a disk-cached read; every B1-B4 render path either calls it (a cache read, same as the existing Currency Risk page already does) or reads an already-cached `snapshot_store` row. No code path in this section calls `yfinance`/`requests` directly.
- `CURRENCY_VIEW_LIVE` is **not set** anywhere by this work - it was never touched in any commit's own code, and `os.environ` was only ever read from, never written to, by `is_currency_view_live()`.
- No Railway variable touched, nothing triggered (no scan/batch/Refresh All/recorder run), $0 spent - every commit's own regression run and every test in this section runs fully offline against synthetic/mocked fixtures.
- Full regression sweep as of the last commit (`2390211`): `PASS=110 FAIL=2` - `tests/test_director_addendum2_iv_before_after.py` (the already-known failing scope guard named in the governing instruction itself) and `tests/test_stage1_japan_commit_a.py` (hits the sweep's 90-120s per-file cap; confirmed in the SECTION A report, and unchanged since, that this is a timeout on an unrelated, slow, real-network-retrying test, not a failure). Neither is new, neither is caused by SECTION B.

## 7. Out-of-scope items noticed (list only, not actioned)

- `_render_filter_bar()`'s "Showing N of N companies" caption on the Top 200 page overcounts by including WAITING rows - flagged in the SECTION A report as a pre-existing, owner-approved-mock behavior, unrelated to SECTION B, not touched here either.
- Portfolio's existing AUD-only infrastructure (`to_aud()`/`fx_to_aud()`, the `income_goal_aud` column name, the existing stress-test tables) is untouched and still AUD-only by design - COMMIT B4 is additive beside it, not a replacement, per the instruction's own scope ("Not in this instruction: ... any change to the Top 200 page").
- The Currency Risk tool's own wording that a return to average is "a tendency, not a certainty" and the page's existing disclaimer were left exactly as they were (the task's own instruction to leave them alone) - confirmed by direct diff, no change in `currency_risk_render.py`'s pre-existing strings/copy outside the two additions named above.
- A judgment call worth naming plainly: COMMIT B3's "See the currency view" jump (and B4's inline picker) use the established one-shot-session-key pattern, meaning the extra "margin of safety, {home} view" table on the Currency Risk page disappears the moment the visitor touches any other widget on that page (changing the range chips, for instance) rather than staying pinned for the rest of that visit. This matches the task's own literal wording ("when opened from a Deep Dive note with a ticker") and the codebase's own existing precedent (`research_jump_ticker` behaves the same way), but it's a real, visible UX property the Director may want to weigh in on before this goes live.

---

**After the push** (not yet reached): remote head, Railway deployment id + SUCCESS, the log all-clear line - to be added once Andrew's go is given and the push actually happens.
