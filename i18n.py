"""Español instruction, Part 1 - the one i18n module the whole site routes
through for short, static UI chrome strings.

Architecture (per the instruction): two dicts, EN and ES, and a single
t(key, lang, **fmt) lookup. EN values are the site's current literal
strings, copied here as-is - every call site that used to hard-code one
of these strings now calls t("some.key", lang) instead, so passing
lang="en" (or any lang when the ES translation is missing) renders
BYTE-IDENTICAL to the untouched site. Only pass **fmt when the string
has {placeholders}; t() calls .format(**fmt) on the resolved string.

Longer, stand-alone documents (the two standing disclaimers, and later
the /es/ static pages from Part 2) are deliberately NOT duplicated into
the EN dict below - duplicating a paragraph-long constant here would
create a second copy that can drift from the real source (blog_render.
DISCLAIMER, app.py's _render_footer() text) without anyone noticing.
Instead this module holds only the ES *translation* of each as its own
named constant, and the call site picks between "its own existing EN
constant, untouched" and "this module's ES sibling" based on lang - see
DISCLAIMER_ES / FOOTER_DISCLAIMER_FACTUAL_ES / FOOTER_DISCLAIMER_GENERAL_ES
below and their call sites in blog_render.py / app.py.

Coverage in this pass (Part 1) - see the deployment report for the full
per-page breakdown: header nav labels, search box, the EN/ES picker
itself, the account-bar Sign In/Sign out/Subscribe labels,
paywall_engine.render_gate()'s fixed chrome (not each call site's own
feature_label/teaser text), and both standing disclaimers. Explicitly
NOT yet covered (falls back to English via t()'s EN default, or is simply
not yet lang-aware at all): the email sign-in popover's internal copy,
Scanner/Comparison/Portfolio page-specific chrome, alert/email-hook/
follow-control detail text, ai_gate quota messages, and per-gate-call-site
feature_label/teaser strings.

Cleanup round: the "toggle.simple"/"toggle.full" keys (for the Simple|
Full view toggle) were removed here when that feature was removed from
the site - see claude_instruction_cleanup_round.md, Part 1.

Cleanup round, Part 3 (finishing the Español work): added the "dd.*"
(verdict line, anchor chips, KPI tile labels, gauge titles), "hook.*"
(the signed-out email-capture box under the verdict line), "gate.*"
additions (the Deep Dive/Research paywall gate's own feature_label/
teaser copy - previously deliberately left untranslated per this
docstring's older gap list), and "aigate.*" (the Ask-AI quota/sign-in
messages) key families. Deliberately still NOT covered in this pass
(documented gaps, same convention as the list above): the engine-
computed status words that fill in these templates (quality_label,
psychology_sentiment, discovery_label, moat_band_label, valuation,
SIGNAL_THRESHOLDS-derived words like STRONG/FAIR/WEAK/UNDERVALUED) -
those are literal string values baked into deep_dive_engine's own
return dict, not UI chrome, and translating them would mean either
touching the scoring engines (explicitly out of scope) or hand-
maintaining a second parallel mapping of engine output strings, which
risks silently drifting out of sync; the "Value Score"/"Long Score"
score NAMES themselves as they appear inside chart titles and axis
labels (contribution charts, score-history chart) - only the top-of-
page KPI tile labels for these are translated; the Scanner/Comparison
page's own render_gate call (page_label is "Scanner"/"Comparison") -
same "not yet lang-aware" bucket as the rest of those two pages.

Español completion, Part 1b: the email sign-in popover's own internal
copy (paywall_engine._render_signin_control) and the plain-string
messages returned by email_auth.send_code()/verify_code() - both
previously flagged as explicit gaps above - are now covered via the
"signin.*"/"email_auth.*" key families. The Scanner/Comparison
render_gate() call's own feature_label/teaser is now covered too, via
"gate.results_full_label"/"gate.results_teaser" (page_label itself
stays untranslated - see that call site's own comment).
"""

EN = {
    # Part 5 (site-wide slim nav): shortened for the persistent nav row -
    # these keys are used ONLY in the nav row/popover (_render_app_nav_items,
    # app.py), never elsewhere, so shortening them in place is safe. Full
    # descriptive names live on each page's own header/section instead.
    "nav.deep_dive": "Deep Dive",
    "nav.research": "Research",
    "nav.comparison": "Compare",
    "nav.scanner": "Scanner",
    "nav.calendar": "Calendar",
    "nav.portfolio": "Portfolio",
    "nav.blog": "Blog",
    "nav.more": "More",
    "nav.methodology": "Methodology",
    "nav.about": "About",
    "nav.track_record": "Track record",

    "header.tagline": "Research any stock in seconds.",
    "header.search_placeholder": (
        "Input your stock ticker (e.g. CSL.AX or AAPL, or CSL.AX BHP.AX to compare)"
    ),
    "header.search_button": "Search",
    "header.search_caption": (
        "One ticker = Deep Dive. Two or more (comma or space separated) = "
        "side-by-side Comparison. ASX (e.g. CSL.AX) and US (e.g. AAPL) "
        "tickers can be mixed freely."
    ),

    "lang.en": "EN",
    "lang.es": "ES",

    "account.sign_in": "Sign In",
    "account.sign_out": "Sign out",
    "account.subscribe": "Subscribe",

    "gate.not_configured": (
        "\U0001F512 Subscriptions aren't fully set up yet - {feature_label} "
        "will unlock here once they are. Check back soon."
    ),
    "gate.locked_title": "\U0001F512 Subscribe to unlock {feature_label}",
    "gate.subscribe_cta": "Subscribe to continue browsing",
    "gate.checkout_error": (
        "Couldn't reach the subscription system just now - please try "
        "again in a moment."
    ),
    "gate.signed_in_as": "Signed in as {email}.",
    "gate.google_signin": "Sign in with Google to continue",

    # Cleanup round, Part 3: the Deep Dive/Research render_gate() call
    # sites' own feature_label/teaser copy (previously a documented gap -
    # see render_gate's own docstring).
    "gate.dd_full_breakdown_label": "the full Deep Dive breakdown",
    "gate.dd_full_breakdown_teaser": (
        "Quality, Psychology, Discovery, and Trade Setup scores - the "
        "full factor breakdown behind the {score_word} above."
    ),
    "gate.dd_reverse_dcf_label": "What the price implies - reverse DCF",
    "gate.dd_reverse_dcf_teaser": (
        "The FCF growth rate the market is currently pricing in for this "
        "stock, alongside the growth rate the model itself assumed."
    ),
    "gate.rc_potential_label": "Company Potential - your own research notes",
    "gate.rc_potential_teaser": (
        "The author's Low/Medium/High ratings and full written analysis "
        "for every covered company."
    ),

    # Cleanup round, Part 3: verdict line + anchor chips
    # (_render_dd_verdict_and_chips) and the Deep Dive's top KPI tiles +
    # gauge titles.
    "dd.verdict.base": (
        "Model estimate {iv} {ccy} vs price {price} {ccy} "
        "({mos} margin of safety)"
    ),
    "dd.verdict.growth_suffix": (
        " — the market is pricing in {implied} growth; the model "
        "assumes {model}."
    ),
    "dd.verdict.plain_suffix": ".",
    "dd.verdict.default_note": (
        "Rests on a default/estimated input where a reported figure "
        "wasn't available - see the notes below."
    ),

    "dd.chip.reverse_dcf": "Reverse DCF",
    "dd.chip.moat": "Moat",
    "dd.chip.moat_scored": "Moat {score}",
    "dd.chip.ask_ai": "Ask AI",
    "dd.chip.insider": "Insider filings",
    "dd.chip.dividends": "Dividends",
    "dd.chip.financials": "10-yr financials",
    "dd.chip.peers": "Peers",
    # Deep Dive first-screen instruction, Part 2: shown once, right under
    # the now-clickable chip row, so a visitor knows the chips are jump
    # links to the full analysis below rather than just decoration.
    "dd.chip.scroll_cue": "Full analysis below ↓",

    "dd.kpi.price": "Price",
    "dd.kpi.intrinsic_value": "Intrinsic Value",
    "dd.kpi.mos_label": "Margin of safety (discount to estimated worth)",
    "dd.kpi.value_score": "Value Score",
    "dd.kpi.long_score": "Long Score",
    "dd.kpi.signal": "Signal",

    "dd.gauge.quality": "Quality - {label}",
    "dd.gauge.psychology": "Psychology - {label}",
    "dd.gauge.discovery": "Discovery - {label}",
    "dd.gauge.moat": "Moat - {label}",
    "dd.gauge.mos": "Margin of Safety - {label}",
    "dd.gauge.value_score": "Value Score",
    "dd.gauge.long_score": "Long Score - {label}",
    # "Trade Setup" is kept as-is in both languages - the already-shipped
    # ES methodology page (site_content.py) keeps this exact English term
    # inside its own Spanish paragraphs ("El **Trade Setup** responde..."),
    # so inventing a translated name here would contradict a term the
    # owner has already published.
    "dd.gauge.trade_setup": "Trade Setup - {label}",

    # Cleanup round, Part 3: the signed-out email-capture box directly
    # under the verdict line (_render_conversion_email_hook).
    "hook.next_report": (
        "**{ticker} reports on {date}.** Get the before/after analysis "
        "by email when it does:"
    ),
    "hook.generic": "Get notified when {ticker}'s numbers change:",
    "hook.email_label": "Email address",
    "hook.email_placeholder": "you@example.com",
    "hook.notify_me": "Notify me",
    "hook.invalid_email": "That doesn't look like a valid email address.",
    "hook.enter_code": "Enter the 6-digit code sent to {email}.",
    "hook.code_label": "6-digit code",
    "hook.verify": "Verify",
    "hook.resend": "Resend code",
    "hook.done": (
        "Done — you'll get {ticker}'s report analysis. You're signed in."
    ),

    # Cleanup round, Part 3: Ask-AI quota/sign-in messages
    # (ai_gate.check()'s lang-aware messages - see that module's own
    # docstring for which call sites pass lang through).
    "aigate.sign_in": "Sign in to ask a question.",
    "aigate.temp_unavailable": (
        "AI features are temporarily unavailable - please try again shortly."
    ),
    "aigate.monthly_cap": (
        "AI features have reached this month's usage cap - back next month."
    ),
    "aigate.plus_monthly_limit": (
        "You've used all {limit} questions included this month - resets "
        "on the 1st."
    ),
    "aigate.plus_daily_limit": (
        "You've reached today's limit of {limit} questions - back tomorrow."
    ),
    "aigate.free_daily_limit": (
        "You've used your {limit} free questions today - come back "
        "tomorrow, or subscribe for 300/month."
    ),

    # Cleanup round, Part 3/Español Part 4: the "new research is up"
    # broadcast email (announce_engine.py) - one representative
    # conversion-pass email flow given an ES variant, selected per
    # recipient via email_auth.get_signup_lang(). The date stamp's month
    # abbreviation was left un-localized here (a documented, accepted
    # gap) - closed in Español completion, Part 2 via format_date_dmy()
    # below, a small manual month-abbreviation map rather than a Spanish
    # server locale dependency.
    "email.announce.subject": "New research on StocksDeepDive: {tickers}",
    "email.announce.heading": "New research is up",
    "email.announce.intro": (
        "The Rational Compounder research workbook was rebuilt on "
        "{date}. Click any ticker below for its live research page."
    ),
    "email.announce.th_ticker": "Ticker",
    "email.announce.th_change": "What changed",
    "email.announce.tag_added": "Added",
    "email.announce.tag_updated": "Updated",
    "email.announce.footer": (
        "Factual information only - this email links to data and "
        "calculator outputs computed from stated inputs; it contains no "
        "recommendations to buy, hold or sell any security. You're "
        "receiving this because you asked to be emailed when this "
        "research updates. To stop, reply STOP, or open any research "
        "page above and click \"Following - click to stop\" (or unfollow "
        "while signed in) on the site."
    ),

    # ---------------------------------------------------------------
    # Español completion instruction, Part 1: remaining on-page Deep
    # Dive coverage flagged by the cleanup round's own Part 3 report.
    # Deliberately still NOT covered (documented, same convention as
    # every prior gap list): the reverse-DCF/Compounder-View methodology
    # popover bodies (multi-paragraph, a separate large translation body
    # of their own), data-table column headers (peer table, insider
    # filings table, results-day before/after table - risk of breaking
    # column_config references for low visible payoff), the alert
    # control's threshold-setting widgets, the checklist's tickbox list
    # items, and engine-computed status words (unchanged rule).
    # ---------------------------------------------------------------
    "dd.plain.value_score": (
        "In plain English: one number blending business quality, price "
        "versus estimated value, crowd psychology, and market attention."
    ),
    "dd.plain.quality": (
        "In plain English: how strong the underlying business is - "
        "profitability, balance sheet strength, and growth - judged "
        "from its own financial statements."
    ),
    "dd.plain.psychology": (
        "In plain English: whether the crowd trading this stock right "
        "now looks fearful, calm, or greedy, read from recent price "
        "behaviour."
    ),
    "dd.plain.discovery": (
        "In plain English: how much attention this stock is getting "
        "right now, from search interest, news, and trading volume."
    ),
    "dd.plain.moat": (
        "In plain English: how well this business's profits are "
        "protected from competitors, based on returns on capital and "
        "margin durability."
    ),
    "dd.plain.mos": (
        "In plain English: how much cheaper today's price is than what "
        "the model estimates the business is worth."
    ),

    "dd.chart.driving_score": "What's driving the {score_word} (points contributed by each factor)",
    "dd.chart.points_score": "Points toward {score_word}",
    "dd.chart.driving_quality": "What's driving Quality (weighted terms)",
    "dd.chart.points_quality": "Points toward Quality",
    "dd.chart.driving_psychology": "What's driving Psychology (Fear - Greed - FOMO)",
    "dd.chart.points_psychology": "Points toward Psychology",
    "dd.chart.driving_discovery": "What's driving Discovery (attention & momentum)",
    "dd.chart.points_discovery": "Points toward Discovery",
    "dd.chart.driving_moat": "What's driving Moat (durability of the return)",
    "dd.chart.points_moat": "Points toward Moat",
    "dd.chart.driving_trade_setup": "What's driving the Trade Setup Score",
    "dd.chart.points_trade_setup": "Points toward Setup Score",

    "dd.history.title": "{score_word} over time",
    "dd.history.show_quality": "Show Quality",
    "dd.history.show_moat": "Show Moat",
    "dd.history.mos_title": "MOS over time",
    "dd.history.caption": "Computed nightly; gaps = days the stock wasn't scanned.",

    "dd.price.title": "Last 6 months",
    "dd.price.legend_price": "Price",
    "dd.price.entry_zone": "Entry zone {value}",

    "dd.rdcf.heading": "What the price implies",
    "dd.rdcf.forecast_caption": "A described calculation from stated inputs, not a forecast.",
    "dd.rdcf.implied_growth": "Implied growth",
    "dd.rdcf.model_growth": "Model growth",
    "dd.rdcf.marker_capped": "Marker capped at -10%/30% for readability.",
    "dd.rdcf.default_note": (
        "Rests on a default/estimated free cash flow input - see the "
        "note under Intrinsic Value above."
    ),

    "dd.peer.heading": "Peer context",
    "dd.peer.no_data": "No peer data yet for {ticker} - not in a scanned overnight universe yet.",
    "dd.peer.provenance": "vs last night's overnight scan (attention-lite).",
    "dd.peer.no_rankable": "No rankable scores for this ticker yet.",
    "dd.peer.closest_peers": "Closest peers - {source}",
    "dd.peer.compare_button": "Compare these →",
    "dd.peer.no_peers": "No peers found in {universe} to compare against.",
    "dd.peer.pct_top": "top {pct}%",
    "dd.peer.pct_bottom": "bottom {pct}%",
    "dd.peer.of": "of",

    "dd.heading.dividends": "Dividends",
    "dd.heading.insider": "Insider & capital",

    # Deep Dive first-screen instruction, Part 3: the compact popover
    # triggers in the new Watchlist/Alerts/Checklist action row under the
    # header - fixed, ticker-free labels (the ticker context that used to
    # live in the old expander/button labels now sits as the first line
    # inside each popover instead - see dd.alert.expander/dd.checklist.
    # expander just below, both still used, just no longer as the outer
    # trigger text).
    "dd.actions.watchlist_button": "☆ Watchlist",
    "dd.actions.alerts_button": "\U0001F514 Alerts",
    "dd.actions.checklist_button": "\U0001F4CB Checklist",

    "dd.alert.expander": "\U0001F514 Alert me when {ticker}...",
    "dd.alert.signin_prompt": (
        "Sign in (top left) to get an email or push notification when "
        "{ticker}'s own computed numbers cross a line you choose."
    ),

    "dd.checklist.expander": "\U0001F4CB My checklist for {ticker}",
    "dd.checklist.signin_prompt": (
        "Sign in (top left) to keep a private pre-purchase checklist and "
        "thesis note for {ticker}."
    ),

    "dd.download.popover": "\U0001F4E5 Download data",
    "dd.download.caption": "Everything on this page for {ticker}, as a spreadsheet.",
    "dd.download.xlsx_button": "Compounder View workbook (.xlsx)",
    "dd.download.csv_button": "Valuation + Scores (.csv)",
    "dd.download.xlsx_failed": "Workbook couldn't be built right now.",
    "dd.download.csv_failed": "CSV couldn't be built right now.",
    "dd.download.fair_value_omitted": (
        "Fair Value sheet omitted from the workbook - subscribe to "
        "include it."
    ),

    "dd.copytext.button_label": "Copy as text",

    "gate.results_full_label": "the full {page_label} results",
    "gate.results_teaser": (
        "Valuation (Intrinsic Value, MOS), Quality, Psychology, "
        "Discovery, and Trade Setup detail for every stock above."
    ),

    "dd.legend.quality": "Quality",
    "dd.legend.moat": "Moat",
    "dd.legend.mos": "MOS",

    "dd.results.heading": "Reported on {date} - before/after",
    "dd.results.stale_warning": (
        "The \"after\" figures below may still rest on a statement Yahoo "
        "hasn't fully ingested yet - re-checked automatically a few days "
        "after the report; if these look unchanged from \"before\", check "
        "back in a few days."
    ),
    "dd.results.what_moved": "What moved:",
    "dd.results.footer_caption": (
        "A computed before/after comparison from this site's own scoring - "
        "described calculation, not a recommendation."
    ),

    # Español completion, Part 1b: the email sign-in popover's own internal
    # copy (paywall_engine._render_signin_control - previously a documented
    # gap) and the plain-string messages email_auth.send_code()/
    # verify_code() return (also previously a documented gap). Placeholders
    # ({email}) are pre-formatted values passed through **fmt, same
    # discipline as the rest of this file.
    "signin.google_button": "Continue with Google",
    "signin.or_divider": "or",
    "signin.email_label": "Email address",
    "signin.email_placeholder": "you@example.com",
    "signin.website_honeypot_label": "Website",
    "signin.send_code_button": "Email me a sign-in code",
    "signin.code_label": "6-digit code",
    "signin.code_placeholder": "123456",
    "signin.verify_button": "Verify",
    "signin.resend_button": "Resend code",

    "email_auth.invalid_email": "That doesn't look like a valid email address.",
    "email_auth.not_configured": "Email sign-in isn't available right now - try Google.",
    "email_auth.too_many_ip": (
        "Too many codes requested from this connection today - please try "
        "again tomorrow."
    ),
    "email_auth.too_many_today": (
        "Too many codes requested today - please try again tomorrow."
    ),
    "email_auth.send_failed": "Couldn't send the email right now - please try again.",
    "email_auth.code_sent": "Code sent to {email} - check your inbox (and spam folder).",
    "email_auth.enter_code": "Enter the 6-digit code from the email.",
    "email_auth.no_code": "No code on record - request a new one.",
    "email_auth.too_many_attempts": "Too many wrong attempts - request a new code.",
    "email_auth.code_expired": "That code has expired - request a new one.",
    "email_auth.wrong_code": "Wrong code - check the email and try again.",
    "email_auth.signed_in": "Signed in.",

    # Español completion, Part 1b (found while wiring the sign-in popover):
    # _render_follow_control's own inline email/code flow duplicates that
    # same flow's strings verbatim (shared by page_research and
    # page_deep_dive) - translated here for consistency rather than left
    # as a second, English-only copy sitting right next to the now-
    # translated one. Reuses signin.*/email_auth.invalid_email for the
    # strings that are identical; these are just the ones unique to it.
    "follow.notify_button": "Notify me",
    "follow.caption": "Get an email when {ticker}'s research updates.",
    "follow.enter_code_prompt": "Enter the 6-digit code sent to {email} for {ticker}.",
    "follow.following_label": "🔔 Following — click to stop",
    "follow.notify_label": "🔔 Email me when research updates",
    "follow.save_failed": "Couldn't save right now - please try again.",

    # Español completion, Part 1b (found while covering blog): the blog
    # post's end-of-article subscribe box (blog_render._blog_subscribe_
    # html) had no lang parameter at all - a Spanish blog post showed an
    # English subscribe box. Now driven by the POST's own lang (post_lang
    # in render_post()), a separate concept from the rest of the site's
    # ?lang=es query-param toggle - see blog_render.py's own "Español
    # instruction, Part 3" comment on post_lang.
    "blog.subscribe.already_heading": "You're on the list.",
    "blog.subscribe.already_body": "You'll get the next research note by email.",
    "blog.subscribe.heading": "Get the next research note by email &mdash; free.",
    "blog.subscribe.email_placeholder": "you@example.com",
    "blog.subscribe.subscribe_button": "Subscribe",
    "blog.subscribe.code_placeholder": "123456",
    "blog.subscribe.verify_button": "Verify",
    "blog.subscribe.done": "Done &mdash; you're on the list.",
    "blog.subscribe.js_invalid_email": "That doesn't look like a valid email address.",
    "blog.subscribe.js_sending": "Sending...",
    "blog.subscribe.js_checking": "Checking...",
    "blog.subscribe.js_network_error": "Couldn't reach the server - please try again.",
    "blog.subscribe.js_wrong_code_fallback": "Wrong code - try again.",

    # Español completion, Part 2: the weekly watchlist digest email
    # (digest_engine.py) - subject + body chrome only. The data table
    # itself (ticker/price/IV/MOS/score rows, and its column headers) stays
    # untranslated, same deferred-table-header policy as the in-app peer/
    # insider/results-day tables in Part 1 - it's numbers plus a "Ticker"/
    # "Price"/etc. header row, not prose. Selected per-recipient via
    # email_auth.get_signup_lang(email), never the sender/admin's own
    # session - same pattern announce_engine.py already uses.
    "email.digest.subject_factual": "Your StocksDeepDive watchlist - weekly update",
    "email.digest.subject_signal": "Your StocksDeepDive watchlist - weekly signals",
    "email.digest.heading": "Your watchlist this week",
    "email.digest.intro": (
        "Weekly re-score of the stocks you saved, as of {date}. Click any "
        "ticker for its full live Deep Dive."
    ),
    "email.digest.disclaimer_factual": (
        "Factual information and calculator outputs only - this email "
        "describes data and model outputs computed from stated inputs; it "
        "contains no recommendations to buy, hold or sell any security."
    ),
    "email.digest.disclaimer_general": (
        "General information only - not financial advice; scores and "
        "signals are model outputs and do not consider your personal "
        "circumstances."
    ),
    "email.digest.footer_note": (
        "Scored without news/social attention inputs (the weekly digest "
        "uses the attention-lite model). You're receiving this because "
        "{email} saved a watchlist on StocksDeepDive while signed in. To "
        "stop these, remove all stocks from your watchlist on the site."
    ),

    # Español completion, Part 2: metric alerts ("condition met" email +
    # push template, alert_engine.py). These are a NEW, email-only label
    # lookup ("alert.metric.*") - deliberately separate from alert_engine.
    # METRIC_LABELS (which app.py's Deep Dive "Your alerts for {ticker}:"
    # list also reads directly and which Part 1 explicitly left as a
    # deferred widget-internals gap) so that decision isn't silently
    # reopened here; this dict only feeds the email/push text.
    "alert.metric.mos_pct": "MOS",
    "alert.metric.value_score": "Value Score",
    "alert.metric.quality": "Quality",
    "alert.metric.moat": "Moat",
    "alert.metric.price": "Price",
    "alert.metric.intrinsic_value": "Intrinsic value",
    "alert.metric.moat_state": "Moat state",
    "alert.metric.valuation_label": "Valuation",
    "alert.op.crossed_above": "crossed above",
    "alert.op.crossed_below": "crossed below",
    "alert.condition_numeric": "{label} {op} {threshold} (now {current})",
    "alert.condition_categorical": "{label} became {value}",
    "alert.hit_message": (
        "{ticker} — condition met: {condition}. Described calculation, "
        "not a recommendation. Open the Deep Dive →"
    ),
    "email.alert.subject_one": "{n} alert condition met tonight",
    "email.alert.subject_many": "{n} alert conditions met tonight",
    "email.alert.heading_one": "{n} alert condition met tonight",
    "email.alert.heading_many": "{n} alert conditions met tonight",
    "email.alert.footer": (
        "Each line above describes a calculation you asked StocksDeepDive "
        "to watch, computed from this site's own data - not a "
        "recommendation to buy, hold or sell any security. Manage or "
        "delete your alerts any time from a stock's Deep Dive page or My "
        "Portfolio &rarr; My alerts."
    ),
    "push.alert.body": "Tap to see what triggered on StocksDeepDive.",

    # Español completion, Part 2: results-day notification email/push
    # (results_engine.py). `moved` items are engine-generated text
    # (results_engine's own "what moved" sentences), stay untranslated -
    # same as app.py's before/after card in Part 1.
    "email.results.subject": "{ticker} reported on {date} - before/after",
    "email.results.reported_on": "reported on {date}",
    "email.results.no_moves": "No material change computed.",
    "email.results.vs_line": "Value Score now {vs}.",
    "email.results.footer": (
        "A computed before/after comparison from this site's own scoring "
        "- not a recommendation to buy, hold or sell any security. Full "
        "before/after on the Deep Dive page above."
    ),
    "email.results.push_fallback": "{ticker} reported on {date}.",

    # Español completion, Part 2: portfolio AI watchdog email/push
    # (portfolio_watchdog_engine.py). The AI-written brief text itself
    # (result["text"]) is asked to be written in Spanish directly by the
    # model (see that module's own _SYSTEM_PROMPT addition) rather than
    # translated after the fact - same one AI call, same ai_gate cost, per
    # the instruction's "instruct the model to write ... in Spanish" note
    # for the weekly brief, applied here too for the same reason (an
    # English AI paragraph next to a translated label/heading would be a
    # visible, jarring gap). "email.ai_label" is IDENTICAL EN text to
    # ai_client.ANSWER_LABEL (kept as a separate i18n key rather than
    # changing that shared constant, since ANSWER_LABEL is also used by
    # several admin-only English tools in app.py that are out of scope).
    "email.watchdog.subject": "StocksDeepDive portfolio watchdog: {tickers}",
    "email.watchdog.heading_one": "Portfolio watchdog - {n} holding changed",
    "email.watchdog.heading_many": "Portfolio watchdog - {n} holdings changed",
    "email.watchdog.intro": (
        "Something material moved since your last brief, as of {date}. "
        "Click any ticker for its full live Deep Dive."
    ),
    "email.watchdog.footer": (
        "Factual information only - each summary above describes data and "
        "model outputs computed from stated inputs; it contains no "
        "recommendations to buy, hold or sell any security, and is "
        "written by an AI model from your saved thesis and this site's "
        "own numbers, not by a person. You're receiving this because you "
        "turned on the AI watchdog for a portfolio on StocksDeepDive - "
        "turn it off any time in that portfolio's Portfolio settings."
    ),
    "email.ai_label": "AI-written summary of the site's data - not advice",
    "push.watchdog.title": "Portfolio watchdog: {tickers}",
    "push.watchdog.body": "Tap to see what changed on StocksDeepDive.",

    # Español completion, Part 2: the personalised weekly brief
    # (weekly_brief_engine.py) - subject/heading/intro/section headings/
    # footer chrome. The AI-written intro paragraph(s) are requested
    # directly in Spanish from the model (see _BRIEF_SYSTEM_PROMPT_ES_
    # SUFFIX in that module) rather than translated after the fact - same
    # approach as the watchdog brief above, and the one this instruction
    # explicitly asks for on this email. The watchlist/health data tables
    # (rows AND column headers) stay untranslated, same deferred policy
    # as digest_engine.py's identical table just above.
    "email.brief.subject_ai": "Your StocksDeepDive weekly brief",
    "email.brief.heading": "Your weekly brief",
    "email.brief.intro": "As of {date}. Click any ticker for its full live Deep Dive.",
    "email.brief.reporting_this_week": "Reporting this week:",
    "email.brief.full_calendar_link": "Full results calendar →",
    "email.brief.health_heading": "Portfolio health",
    "email.brief.posts_heading": "New research this week",
    "email.brief.footer_note": (
        "Scored without news/social attention inputs (this brief uses "
        "the attention-lite model). You're receiving this because "
        "{email} saved a watchlist on StocksDeepDive while signed in. To "
        "stop these, remove all stocks from your watchlist on the site."
    ),

    # top5_au_us instruction: the home page "Tonight's top 5" strip,
    # replacing the old single ASX-200-only table with one per country,
    # each drawn from every universe of that country (snapshot_store,
    # deduped by ticker - see snapshot_store.all_public_rows()'s own
    # docstring for why that dedup is free with this table's schema).
    # {country} is itself localized via home.top5.country_au/country_us
    # below, not hardcoded into this template.
    "home.top5.kicker": "TONIGHT'S TOP 5",
    "home.top5.heading": "Tonight's top 5 — {country}",
    "home.top5.country_au": "Australia",
    "home.top5.country_us": "USA",
    "home.top5.col_ticker": "Ticker",
    "home.top5.col_price": "Price",
    "home.top5.col_value_score": "Value Score",
    "home.top5.col_valuation": "Valuation",
    "home.top5.col_universe": "Universe",
    "home.top5.caption": (
        "From the latest overnight scans across all covered universes "
        "(nightly + weekly) — a sort result from described calculations, "
        "not a recommendation. Full tables in the {link}"
    ),
    "home.top5.no_scan": "No overnight scan yet for {country}.",
    "home.top5.as_of": "as of {day}",

    # Next-batch instruction, Part 1: full home-page ES coverage - the
    # hero, toolkit cards, "how it works" steps, results-day/calendar
    # strips, compounder-coverage section, blog section, CTA band, and the
    # natural-language screening teaser (shared with Scanner). Every EN
    # value below is the exact string that rendered before this pass, so
    # lang=="en" stays byte-identical; only the ES dict adds a translation.
    "home.hero.title_factual": "The <em>data and models</em> behind a valuation judgment.",
    "home.hero.sub_factual": (
        "Live intrinsic values, quality calculations, psychology and discovery "
        "readings &mdash; computed for any ASX or US stock, with <b>every input "
        "stated and every estimate flagged</b>. The judgment stays yours."
    ),
    "home.hero.title_signal": "Know what a stock is <em>worth</em> &mdash; and whether now is a sane entry.",
    "home.hero.sub_signal": (
        "One score that combines <b>value, quality, crowd psychology and market "
        "attention</b> &mdash; computed live for any ASX or US stock. No noise, "
        "no hidden assumptions: every estimated number is flagged."
    ),
    "home.hero.search_aria": "Ticker search",
    "home.hero.search_placeholder": "CSL.AX or AAPL  ·  or two tickers to compare: CSL.AX BHP.AX",
    "home.hero.analyze_button": "Analyze",
    "home.hero.search_hint": (
        "One ticker = full Deep Dive &middot; Two or more = side-by-side "
        "Comparison &middot; ASX + US mixed freely"
    ),

    "home.toolkit.kicker": "THE TOOLKIT",
    "home.toolkit.h2": "Four ways in. One consistent model.",
    "home.toolkit.secsub_factual": (
        "Every tool runs the same engine &mdash; the same DCF model, the same "
        "quality calculation, the same psychology read &mdash; so the numbers "
        "always agree with each other."
    ),
    "home.toolkit.secsub_signal": (
        "Every tool runs the same engine &mdash; the same DCF, the same quality "
        "tests, the same psychology read &mdash; so the numbers always agree "
        "with each other."
    ),
    "home.toolkit.card1_title": "Stock Deep Dive",
    "home.toolkit.card1_desc_factual": (
        "The full picture for one ticker: intrinsic value vs today's price, "
        "what drives the Value Score, and psychology and discovery readings "
        "&mdash; every input stated."
    ),
    "home.toolkit.card1_desc_signal": (
        "The full picture for one ticker: intrinsic value vs price, what "
        "drives the Long Score, crowd psychology, and a technical entry zone "
        "with stop &amp; targets."
    ),
    "home.toolkit.card2_title": "Side-by-side Comparison",
    "home.toolkit.card2_desc_factual": (
        "Two or more tickers lined up on identical calculations &mdash; "
        "intrinsic value, quality calculation, psychology &mdash; as "
        "colour-coded data bars."
    ),
    "home.toolkit.card2_desc_signal": (
        "Two or more tickers lined up on identical criteria &mdash; "
        "valuation, quality, sentiment, trend, trade setup &mdash; as "
        "colour-coded bars and verdict pills."
    ),
    "home.toolkit.card3_title": "Stock Scanner",
    "home.toolkit.card3_desc_factual": (
        "A whole index &mdash; ASX 200, S&amp;P 500 and more &mdash; as one "
        "sortable data table, computed nightly, with an optional sector "
        "filter. Sorting is arithmetic."
    ),
    "home.toolkit.card3_desc_signal": (
        "Rank a whole index &mdash; ASX 200, S&amp;P 500 and more &mdash; by "
        "Long Score, with an optional sector filter. Find what to look at, "
        "not just check what you already own."
    ),
    "home.toolkit.card4_title": "Rational Compounder Research",
    "home.toolkit.card4_desc_factual": (
        "Hand-built research on selected compounders &mdash; a decade of "
        "reported earnings, four fair-value models, and documented company "
        "histories."
    ),
    "home.toolkit.card4_desc_signal": (
        "Hand-built, Buffett/Munger-style research on selected compounders "
        "&mdash; a decade of earnings, four fair-value methods, and written "
        "judgment on every business."
    ),
    "home.toolkit.card5_title": "My Portfolio",
    "home.toolkit.card5_desc_factual": (
        "Track what you actually own against the price and fundamentals on "
        "the day you bought &mdash; private to your signed-in account, "
        "sign-in required."
    ),
    "home.toolkit.card5_desc_signal": (
        "Add what you actually own and lock in the day-you-bought baseline "
        "&mdash; private to your signed-in account only, sign-in required."
    ),

    # Part 5 (home tiles, Option C): the 4-tile grid that replaces the old
    # 5-card sdd-cards5 row above (Deep Dive dropped - it's reached from the
    # hero search box instead). Each tile pairs this fixed copy with a
    # LIVE stat line computed from local data at render time (see
    # _home_research_stat / _home_scanner_stat / _home_portfolio_tab_count
    # in app.py) - {count}/{latest}/{universes} are .format() kwargs, never
    # hardcoded here, so a stat can never go stale the way the old "8 tools
    # inside" mock number already had (Portfolio has grown to 9 tabs).
    "home.tiles.free_badge": "FREE",
    "home.tiles.research_title": "Hand-built Research",
    "home.tiles.research_desc": (
        "One company at a time, weeks each &mdash; thesis, valuation and "
        "verdict written out in full."
    ),
    "home.tiles.research_stat": "{count} companies &middot; latest: {latest}",
    "home.tiles.scanner_title": "Stock Scanner",
    "home.tiles.scanner_desc": (
        "Every stock scored nightly on value, quality and moat &mdash; "
        "sortable, filterable, exportable."
    ),
    "home.tiles.scanner_stat": "{count} stocks &middot; {universes} universes",
    "home.tiles.compare_title": "Compare",
    "home.tiles.compare_desc": (
        "Two or more tickers lined up on identical calculations &mdash; "
        "valuation, quality, moat &mdash; as colour-coded data."
    ),
    "home.tiles.portfolio_title": "My Portfolio",
    "home.tiles.portfolio_desc": (
        "Your holdings health-checked nightly &mdash; income, ETFs, stress "
        "test, AI watchdog."
    ),
    "home.tiles.portfolio_stat": "{count} tools inside",

    "home.hiw.kicker": "HOW IT WORKS",
    "home.hiw.h2_factual": "Search. Compute. Inspect.",
    "home.hiw.h2_signal": "Search. Score. Decide.",
    "home.hiw.step1_title": "Type any ticker",
    "home.hiw.step1_desc": (
        "ASX (CSL.AX) or US (AAPL). Live data is pulled on the spot &mdash; "
        "prices, cash flows, news, search trends, social chatter."
    ),
    "home.hiw.step2_title_factual": "Get one transparent calculation",
    "home.hiw.step2_desc_factual": (
        "The Value Score blends the quality calculation, MOS (the gap "
        "between price and intrinsic value), psychology and discovery "
        "&mdash; the same arithmetic every time, with every input shown."
    ),
    "home.hiw.step2_title_signal": "Get one honest score",
    "home.hiw.step2_desc_signal": (
        "The Long Score blends business quality, margin of safety, crowd "
        "psychology and market attention &mdash; the same value-investing "
        "maths every time, with every input shown."
    ),
    "home.hiw.step3_title_factual": "See value AND psychology",
    "home.hiw.step3_desc_factual": (
        "Two separate calculations, never blurred: what the model computes "
        "from the business's own cash flows, and what the crowd has been "
        "doing to the price &mdash; both stated as numbers, side by side."
    ),
    "home.hiw.step3_title_signal": "See value AND timing",
    "home.hiw.step3_desc_signal": (
        "Two separate verdicts, never blurred: is this a good business to "
        "<em>own</em>, and is right now a sane <em>entry</em>? A great "
        "company can still be a bad buy today."
    ),
    "home.hiw.honesty": (
        "<b>The red-flag rule:</b> whenever a number rests on a default or "
        "average because real data wasn't available, it's shown in red. An "
        "estimate is never dressed up as a fact &mdash; you always know "
        "which numbers are computed and which are assumed."
    ),

    "home.results_day.kicker": "RESULTS DAY",
    "home.results_day.h2": "Reported this week",
    "home.results_day.col_ticker": "Ticker",
    "home.results_day.col_reported": "Reported",
    "home.results_day.col_value_now": "Value Score now",
    "home.results_day.col_vs_before": "vs before",
    "home.results_day.caption": (
        "Tickers that reported results in the last 7 days, with a computed "
        "before/after re-analysis - open a ticker's Deep Dive page for the "
        "full comparison and what moved."
    ),

    "home.results_calendar.kicker": "RESULTS CALENDAR",
    "home.results_calendar.h2": "Reporting this week",
    "home.results_calendar.col_ticker": "Ticker",
    "home.results_calendar.col_date": "Date",
    "home.results_calendar.col_status": "Status",
    "home.results_calendar.status_reported": "&#10003; reported",
    "home.results_calendar.status_expected": "~ expected",
    "home.results_calendar.caption": (
        "Dates from the data provider; confirmed dates marked ✓, estimates "
        "marked ~. "
    ),
    "home.results_calendar.caption_mine": "Your own tickers - ",
    "home.results_calendar.caption_link": "[Full Results Calendar →](/results-calendar)",

    "home.compounder.kicker": "RATIONAL COMPOUNDER RESEARCH",
    "home.compounder.h2": "Covered in depth today",
    "home.compounder.secsub": (
        "New companies are added as the research completes &mdash; each one "
        "takes weeks, not minutes."
    ),
    "home.compounder.sections_label": "Research sections",
    "home.compounder.verdict_label": "Written verdict",
    "home.compounder.request_title": "Which stock should be researched next?",
    "home.compounder.request_cta": "Tell us via Feedback &rarr;",

    "home.blog.kicker": "FROM THE BLOG",
    "home.blog.h2": "Latest research notes",
    "home.blog.secsub": "The reasoning behind the numbers, written out in full &mdash; ",
    "home.blog.all_posts": "all posts &rarr;",
    "home.blog.min_read": "min read",

    "home.cta.title": "Everything is free.",
    "home.cta.desc": (
        "Sign in (top left) to build a watchlist across every ticker you "
        "check and get the weekly {digest_word} digest."
    ),
    "home.cta.digest_watchlist": "watchlist",
    "home.cta.digest_signal": "signal",

    "home.what_you_get.tag": "WHAT YOU GET",
    "home.what_you_get.intro": "Every search answers three questions:",
    "home.what_you_get.q1_factual": "What is the intrinsic value?",
    "home.what_you_get.a1_factual": "shown next to today's price with the MOS stated as a percentage",
    "home.what_you_get.q1_signal": "What is it worth?",
    "home.what_you_get.a1_signal": "plus margin of safety vs today's price",
    "home.what_you_get.dcf_lead": "A live DCF with a per-stock discount rate,",
    "home.what_you_get.q2": "Is it a good business?",
    "home.what_you_get.a2": "A 0&ndash;100 Quality Score from profitability and balance-sheet tests.",
    "home.what_you_get.q3_factual": "What is the crowd doing?",
    "home.what_you_get.a3_factual": (
        "Psychology and discovery readings - distance from recent highs, "
        "volume, search and news attention - stated as numbers."
    ),
    "home.what_you_get.q3_signal": "Is now a sane entry?",
    "home.what_you_get.a3_signal": (
        "Crowd psychology and a technical entry zone &mdash; kept separate "
        "from the ownership question."
    ),
    "home.featured.open_deep_dive": "Open the full {ticker} Deep Dive →",
    "home.featured.rule_au": "Today's feature: the highest Value Score in last night's Australian scans.",
    "home.featured.rule_us": "Today's feature: the highest Value Score in last night's US scans.",

    "home.mood.market_mood": "{country} MARKET MOOD",
    "home.mood.live_reading": "live news-tone reading",
    "home.mood.hopeful": "Hopeful",
    "home.mood.neutral": "Neutral",
    "home.mood.anxious": "Anxious",
    "home.mood.today": "today",

    "chips.try_one": "Try one:",
    "chips.did_you_mean": "Did you mean:",

    "nl.title": "\U0001F50E Describe what you're looking for",
    "nl.caption": (
        "Plain English, e.g. “cheap ASX tech stocks” or “US "
        "small caps in healthcare” - translated into the Scanner's own "
        "country/universe/sector filters, which are always shown before "
        "results run so you can see (and change) exactly what was applied."
    ),
    "nl.signin_prompt": "Sign in (top left) to try natural-language screening.",
    "nl.input_aria": "Describe a screen",
    "nl.input_placeholder": "e.g. cheap quality compounders in Australian mining",
    "nl.button": "Screen",
    "nl.warn_empty": "Type what you're looking for first.",
    "nl.reading": "Reading your request...",

    "dd.data_as_of": "Data as of {date} (latest available daily close).",

    # Part 6 (Scanner opener): "+{n} more universes & sector filter" -
    # {n} is 16 minus the 6 pill shortcuts in _SCANNER_PILL_UNIVERSES
    # (app.py), computed at render time so this stays right if a universe
    # is ever added/removed from scanner_engine's own lists.
    "scanner.change_expander": "+{n} more universes & sector filter",
    "scanner.change_instruction": (
        "Tick one or more countries, then pick a single universe to scan - "
        "each universe below is scanned entirely on its own (ASX 200 and "
        "ASX 300 are never blended together, and neither are any of the "
        "USA universes)."
    ),
    "scanner.country_au": "Australia",
    "scanner.country_us": "USA",
    "scanner.universe_label": "Universe",
    "scanner.pick_country_info": "Tick at least one country above to pick a universe to scan.",
    "scanner.run_scan_button": "Run Scan",
    "scanner.resolving_spinner": "Resolving universe...",
    "scanner.no_stocks_warning": "No stocks matched this universe/sector - try a different selection.",
    "scanner.empty_message": "Pick a country, universe, and (optionally) a sector above, then click Run Scan.",
    "scanner.pills_label": "Popular universes",
    "scanner.sort_by_label": "Sort by",
    "scanner.sort_mos": "MOS",
    "scanner.sort_quality": "Quality",
    "scanner.sort_moat": "Moat",
    "scanner.sort_price": "Price",
    "scanner.rank_col": "#",
    "scanner.scan_of": "scan of {when}",

    "comparison.empty_message": "Search two or more tickers above to run a Comparison.",
    # Part 6 (Comparison opener): the new [ticker] VS [ticker] input row +
    # popular-pair chips, and the head-to-head table shown for paying
    # visitors above the existing full comparison table.
    "comparison.add_third": "+ add third",
    "comparison.compare_button": "Compare",
    "comparison.need_two_warning": "Enter at least two tickers to compare.",
    "comparison.popular_pairs_label": "Popular comparisons",
    "comparison.h2h_heading": "Head-to-head",
    "comparison.h2h_metric_col": "Metric",
    "comparison.h2h_price_model": "Price &rarr; Model estimate",
    "comparison.h2h_quality_moat": "Quality / Moat",
    "comparison.h2h_roic_wacc": "ROIC vs WACC",
    "comparison.h2h_caption": (
        "A described comparison of stated calculations, not a "
        "recommendation - the greener figure on each line is simply the "
        "higher number, nothing more."
    ),

    # Mega-batch Part 7: Results Calendar week board.
    "calendar.subtitle": "Every report this week, at a glance.",
    "calendar.methodology": (
        "Dates from the data provider; confirmed dates marked ✓, "
        "estimates marked ~. Descriptions of calculations, not "
        "recommendations."
    ),
    "calendar.filter_all": "All",
    "calendar.filter_my_tickers": "My tickers",
    "calendar.filter_universe": "Universe...",
    "calendar.filter_sign_in": "Sign in (top left) to filter to your own tickers.",
    "calendar.filter_empty_my_tickers": "Nothing saved to your watchlist, portfolio, or alerts yet.",
    "calendar.week_this": "This week",
    "calendar.week_next": "Next week",
    "calendar.today_badge": "Today",
    "calendar.status_reported": "✓ reported",
    "calendar.status_expected": "~ expected",
    "calendar.pending_reanalysis": "re-analysis pending",
    "calendar.pending_before_after": "before/after coming",
    "calendar.later_heading": "Later this month",
    "calendar.empty_week": "Nothing reported or expected this week.",
    "calendar.empty_next_week": "Nothing expected next week yet.",
    "calendar.empty_later": "Nothing further expected this month yet.",
    "calendar.deep_dive_link": "Deep Dive →",
    "calendar.weekend_note": "Also reporting this weekend:",

    "portfolio.title": "My Portfolio",
    "portfolio.signin_prompt": (
        "Sign in (top left) to track your long-term holdings here. This is "
        "private to your account - nobody else, including other signed-in "
        "visitors, can see it."
    ),
    "portfolio.scoring_spinner": "Scoring your holdings...",
    "portfolio.tab_holdings": "\U0001F4BC Holdings",
    "portfolio.tab_income": "\U0001F4B0 Income",
    "portfolio.tab_overview": "\U0001F4CA Overview & P/L",
    "portfolio.tab_health": "\U0001FA7A Health & News",
    "portfolio.tab_progress": "\U0001F4C8 Progress",
    "portfolio.tab_etfs": "\U0001F9FA My ETFs",
    "portfolio.tab_stress": "\U0001F9EA Stress Test",
    "portfolio.tab_ask": "\U0001F4AC Ask",
    "portfolio.tab_alerts": "\U0001F514 My alerts",

    "portfolio.etfs.empty": "No ETFs in this portfolio.",
    "portfolio.etfs.summary_title": "ETF sleeve summary",
    "portfolio.etfs.col_ticker": "Ticker",
    "portfolio.etfs.col_allocation": "Allocation",
    "portfolio.etfs.col_value": "Value",
    "portfolio.etfs.col_mer": "MER",
    "portfolio.etfs.col_return": "Return p.a.",
    "portfolio.etfs.col_yield": "Yield (TTM)",
    "portfolio.etfs.col_corr_sp500": "Corr. vs S&P 500",
    "portfolio.etfs.col_corr_asx200": "Corr. vs ASX 200",
    "portfolio.etfs.col_top_exposure": "Top exposure",
    "portfolio.etfs.totals_label": "Sleeve total",
    "portfolio.etfs.blended_label": "Blended",
    "portfolio.etfs.caption_return": (
        "* Historical total return from price + distributions, net of the "
        "fund's internal fee - a described calculation from past data, "
        "not a projection."
    ),
    "portfolio.etfs.cards_title": "Fund details",
    "portfolio.etfs.sector_chart_title": "Sector weights",
    "portfolio.etfs.top10_title": "Top 10 holdings",
    "portfolio.etfs.full_list_link": "Full list at issuer →",
    "portfolio.etfs.issuer_link_label": "Issuer page ↗",
    "portfolio.etfs.deep_dive_label": "Deep Dive ↗",
    "portfolio.etfs.category_label": "Category",
    "portfolio.etfs.no_sector_data": "Sector weightings unavailable for this fund.",
    "portfolio.etfs.no_top_holdings": "Top holdings unavailable for this fund.",
    "portfolio.etfs.overlap_title": "Overlap with your direct holdings",
    "portfolio.etfs.overlap_match": (
        # Note: "A$" is written "A\\$" (backslash-escaped) rather than
        # plain "A$" - three literal "$" in one st.markdown() string
        # otherwise gets parsed as two paired LaTeX math delimiters by
        # Streamlit's markdown-it + remark-math pipeline (the same bug
        # documented at this file's _escape_for_markdown() call site in
        # app.py - unsafe_allow_html does NOT suppress it, only a
        # backslash escape does).
        "⚠ {underlying} - held directly (A\\${direct}) and via {etf} "
        "({weight}% weight, ≈A\\${indirect}) - combined ≈A\\${combined}"
    ),
    "portfolio.etfs.overlap_none": (
        "No overlap found between your direct holdings and your ETFs' "
        "published top-10s."
    ),
    "portfolio.etfs.top5_title": "Top 5 combined exposures",
    "portfolio.etfs.overlap_caption": (
        "Computed from each fund's published top-10 - approximate; a "
        "described calculation, not a recommendation."
    ),
    "portfolio.etfs.whatif_title": "What-if projector",
    "portfolio.etfs.whatif_horizon_label": "Years",
    "portfolio.etfs.whatif_rate_label": "Assumed annual return %",
    "portfolio.etfs.whatif_result": (
        # Same "\\$" escaping as overlap_match above - two literal "$"
        # in one markdown string otherwise renders as LaTeX math.
        "At {rate}% p.a. for {years} years, your current A\\${value} ETF "
        "sleeve would become approximately A\\${result}."
    ),
    "portfolio.etfs.whatif_no_rate": (
        "Not enough historical return data to suggest a starting rate - "
        "set one yourself."
    ),
    "portfolio.etfs.whatif_caption": "A what-if calculation at the rate you set - not a forecast.",
    "portfolio.etfs.na": "—",

    "portfolio.stress.empty": "Add a holding with a live price to run a stress test.",
    "portfolio.stress.loading_spinner": "Replaying your holdings against historical prices...",
    "portfolio.stress.intro_caption": (
        "Your current holdings and weights applied to real historical prices "
        "+ distributions - facts about the past, not predictions about the "
        "future."
    ),
    "portfolio.stress.max_downside_title": "MAX DOWNSIDE",
    "portfolio.stress.max_upside_title": "MAX UPSIDE",
    "portfolio.stress.downside_line": (
        "{pct}% (A\\${value}) · {peak} to {trough} · {months} months to recover"
    ),
    "portfolio.stress.downside_line_no_recovery": (
        "{pct}% (A\\${value}) · {peak} to {trough} · not yet recovered"
    ),
    "portfolio.stress.upside_line": "{pct}% (A\\${value}) · {start} to {end}",
    "portfolio.stress.no_data": "Not enough shared price history across your holdings to replay this yet.",
    "portfolio.stress.drawdown_chart_title": "Drawdown history",
    "portfolio.stress.runup_chart_title": "Run-up history",
    "portfolio.stress.replayed_return_line": "Replayed {years}y return: {pct}% p.a.",
    "portfolio.stress.crisis_table_title": "Crisis replays",
    "portfolio.stress.rally_table_title": "Rally replays",
    "portfolio.stress.col_window": "Window",
    "portfolio.stress.col_shock": "Shock",
    "portfolio.stress.col_move_pct": "Portfolio move",
    "portfolio.stress.col_move_value": "Portfolio move (AUD)",
    "portfolio.stress.col_best_defender": "Best defender",
    "portfolio.stress.col_worst_hit": "Worst hit",
    "portfolio.stress.col_biggest_engine": "Biggest engine",
    "portfolio.stress.col_drag": "Biggest drag",
    "portfolio.stress.estimated_flag": "◆ estimated (proxied via sector/index × beta)",
    "portfolio.stress.data_fault_note": (
        "⚠ {tickers}: a split-adjustment data fault was detected in this "
        "ticker's price history - it's excluded from every number on this "
        "tab so it can't skew the portfolio figures."
    ),
    "portfolio.stress.data_fault_cell": "⚠ data fault — excluded",
    "portfolio.stress.shock_grid_title": "Shock grid",
    "portfolio.stress.shock_grid_beta_line": "Portfolio beta (vs a blended S&P 500 / ASX 200 index): {beta}",
    "portfolio.stress.shock_grid_caption": (
        "A plain beta-weighted model, not a replay - a low-beta portfolio "
        "cushions crashes and damps rallies by the same mechanism."
    ),
    "portfolio.stress.rebalance_title": "⚖️ Rebalance sandbox",
    "portfolio.stress.rebalance_never_suggests": (
        "This site never suggests weights - no optimiser, no recommendation. "
        "Set your own what-if weights below."
    ),
    "portfolio.stress.rebalance_never_modifies": (
        "Nothing here changes your real portfolio - this is a what-if "
        "calculation only."
    ),
    "portfolio.stress.rebalance_weight_total": "Total: {total}% (must be 100% to compare)",
    "portfolio.stress.rebalance_reset": "Reset to current weights",
    "portfolio.stress.whatif_table_title": "Current vs what-if",
    "portfolio.stress.col_metric": "Metric",
    "portfolio.stress.col_current": "Current",
    "portfolio.stress.col_whatif": "What-if",
    "portfolio.stress.col_delta": "Δ",
    "portfolio.stress.metric_max_downside": "Max downside",
    "portfolio.stress.metric_max_upside": "Max upside",
    "portfolio.stress.metric_covid_replay": "COVID replay",
    "portfolio.stress.metric_beta": "Beta",
    "portfolio.stress.metric_replayed_10y": "Replayed 10y return p.a.",
    # Mega-batch Part 13: 1-yr 5th-95th percentile simulated range,
    # shown as its own visually-distinct row (divider above, italic/
    # dimmed styling) below the historical what-if metrics, so it can
    # never be mistaken for another historical-replay fact - this one is
    # a model, not something that happened.
    "portfolio.stress.metric_mc_range": "1-yr range, simulated (95%) — not a prediction",
    "portfolio.stress.per_holding_table_title": "Per-holding detail",
    "portfolio.stress.col_ticker": "Ticker",
    "portfolio.stress.col_weight": "Weight",
    "portfolio.stress.col_beta": "Beta",
    "portfolio.stress.col_worst_drawdown": "Worst drawdown (15y)",
    "portfolio.stress.col_best_12m": "Best 12 months",
    "portfolio.stress.monte_carlo_title": "Monte Carlo (1-year band)",
    "portfolio.stress.monte_carlo_band_line": (
        "5th-95th percentile: {p5}% to {p95}% (A\\${p5_value} to A\\${p95_value})"
    ),
    "portfolio.stress.monte_carlo_caption": (
        "5,000 simulated paths from historical volatility and correlations. "
        "Based on the past behaving like the future - which it may not. Not "
        "a prediction."
    ),
    "portfolio.stress.footer_caption": (
        "Correlations rise in severe panics; the replays capture this (it "
        "happened in the data), the grid and simulation partly understate it."
    ),
    "portfolio.stress.window.gfc": "GFC",
    "portfolio.stress.window.q4_2018": "2018 Q4",
    "portfolio.stress.window.covid": "COVID crash",
    "portfolio.stress.window.rate_shock_2022": "2022 rate shock",
    "portfolio.stress.window.post_gfc": "Post-GFC rebound",
    "portfolio.stress.window.melt_up_2016_17": "2016-17 melt-up",
    "portfolio.stress.window.covid_recovery": "COVID recovery",
    "portfolio.stress.window.ai_rally_2023_24": "2023-24 AI rally",
    "portfolio.stress.na": "—",

    # Mega-batch Part 17: "Switch Analyzer" tab - opportunity-cost of
    # selling holding A to buy candidate B, once the real toll of selling
    # (CGT + brokerage) is accounted for. See switch_analyzer_engine.py
    # for every formula behind these strings.
    "portfolio.tab_switch": "\U0001F504 Switch Analyzer",
    "portfolio.switch.combined_view_note": (
        "Pick one portfolio above (not \"All portfolios\") to use the Switch Analyzer - "
        "it needs a single portfolio's own tax rate/brokerage settings and cost bases."
    ),
    "portfolio.switch.empty": "Add a holding with a live price to use the Switch Analyzer.",
    "portfolio.switch.intro_caption": (
        "A factual opportunity-cost calculator, never a recommendation: selling a holding "
        "to buy something else costs real money first (capital-gains tax + brokerage on "
        "both trades) - this shows what the new position would need to out-earn every "
        "year just to make up for that cost, based on this site's own fair-value estimates."
    ),
    "portfolio.switch.settings_title": "Switch Analyzer settings",
    "portfolio.switch.settings_caption": (
        "Your marginal tax rate and flat brokerage fee per trade, for this portfolio only. "
        "Used only for the calculations on this tab - never shared, never sent to the API/MCP."
    ),
    "portfolio.switch.settings_tax_rate": "Marginal tax rate (%)",
    "portfolio.switch.settings_brokerage": "Brokerage per trade (A$)",
    "portfolio.switch.settings_save": "Save settings",
    "portfolio.switch.settings_saved": "Settings saved.",
    "portfolio.switch.settings_missing": (
        "Enter and save your tax rate and brokerage above before running an analysis - "
        "this tool never guesses either one."
    ),
    "portfolio.switch.pair_title": "Pick a pair",
    "portfolio.switch.from_label": "Sell (a holding you're considering selling)",
    "portfolio.switch.to_label": "Buy instead (candidate ticker, Yahoo format e.g. CSL.AX)",
    "portfolio.switch.lookup_btn": "Look up",
    "portfolio.switch.lookup_not_found": (
        "Couldn't find price data for {ticker} - double-check the ticker (Yahoo format, "
        "e.g. CSL.AX for ASX, AAPL for Nasdaq)."
    ),
    "portfolio.switch.years_label": "Holding period to analyse (years)",
    "portfolio.switch.iv_unavailable": (
        "No fair-value estimate is available for {ticker} yet, so the expected-return side "
        "of this analysis can't be computed - the toll (bridge) figures below still stand on "
        "their own."
    ),
    "portfolio.switch.source_hand_built": "\U0001F4DA hand-built research",
    "portfolio.switch.source_auto": "\U0001F916 nightly model estimate",
    "portfolio.switch.side_by_side_title": "Side by side",
    "portfolio.switch.col_position": "Position",
    "portfolio.switch.col_price": "Price",
    "portfolio.switch.col_fair_value": "Fair value",
    "portfolio.switch.col_source": "Source",
    "portfolio.switch.col_upside": "Upside to fair value",
    "portfolio.switch.col_expected_return": "Implied return over {years}y",
    "portfolio.switch.bridge_title": "The switching toll",
    "portfolio.switch.bridge_caption": (
        "Selling {ticker} triggers this before a dollar of it can be redeployed into the new "
        "position."
    ),
    "portfolio.switch.bridge_value": "Sale value (V)",
    "portfolio.switch.bridge_cost_base": "Cost base (C)",
    "portfolio.switch.bridge_gain": "Capital gain",
    "portfolio.switch.bridge_discount_applied": "12-month CGT discount applied (50% of gain taxable)",
    "portfolio.switch.bridge_discount_not_applied": "No 12-month CGT discount (held under 12 months, full gain taxable)",
    "portfolio.switch.bridge_taxable_gain": "Taxable gain",
    "portfolio.switch.bridge_tax": "Tax ({rate}%)",
    "portfolio.switch.bridge_brokerage": "Brokerage (sell + buy, {each} each)",
    "portfolio.switch.bridge_proceeds": "Proceeds after toll (P)",
    "portfolio.switch.bridge_toll_pct": "Toll = {pct}% of the position's value",
    "portfolio.switch.bridge_formula_title": "How the annualised toll (Z) is worked out",
    "portfolio.switch.bridge_formula": (
        "Z = (V ÷ P) ^ (1/N) − 1, where N is the {years}-year holding period above. "
        "This is the constant annual return the new position must out-earn the old one by, "
        "over N years, just to close the gap the toll opened up."
    ),
    "portfolio.switch.bridge_z_line": "Annualised toll (Z): {pct}%/yr over {years} years",
    "portfolio.switch.twelve_month_chip": (
        "Selling now forgoes the 12-month CGT discount: tax would be A\\${tax_now} now vs "
        "A\\${tax_later} after {days} more day(s) (assuming the same sale value) - a "
        "difference of A\\${extra}."
    ),
    "portfolio.switch.verdict_title": "Verdict",
    "portfolio.switch.verdict_passes": (
        "Based on this model's own fair-value estimates, {to_ticker} is estimated to out-earn "
        "{from_ticker} by ≈{spread}%/yr over {years} years - clearing the {toll}%/yr "
        "switching toll by ≈{margin}%/yr."
    ),
    "portfolio.switch.verdict_fails": (
        "Based on this model's own fair-value estimates, {to_ticker} is estimated to out-earn "
        "{from_ticker} by ≈{spread}%/yr over {years} years - short of the {toll}%/yr "
        "switching toll by ≈{margin}%/yr."
    ),
    "portfolio.switch.verdict_no_iv": (
        "Not enough fair-value data on one or both tickers to compare expected returns - only "
        "the toll (above) can be shown for this pair."
    ),
    "portfolio.switch.flip_title": "When would this flip?",
    "portfolio.switch.flip_body": (
        "Holding today's estimated ≈{spread}%/yr return edge steady each year, the switch "
        "would break even at N≈{n} years (N* = ln(V÷P) ÷ ln(1+spread))."
    ),
    "portfolio.switch.flip_never": (
        "With today's estimated return edge at or below zero, a longer holding period alone "
        "never makes this switch break even - the edge itself would need to turn positive first."
    ),
    "portfolio.switch.flags_title": "Things worth checking before reading the verdict",
    "portfolio.switch.flag_concentration": (
        "Buying the full position would make {ticker} ≈{pct}% of the portfolio's value - "
        "above the {threshold}% level this tab flags as concentrated."
    ),
    "portfolio.switch.flag_correlation": (
        "{ticker}'s price history is highly correlated ({corr}) with the rest of the "
        "portfolio - it may add less diversification than it appears to."
    ),
    "portfolio.switch.flag_stale_a": "{ticker}'s data may be stale - {detail}.",
    "portfolio.switch.flag_stale_b": "{ticker}'s data may be stale - {detail}.",
    "portfolio.switch.flag_terminated": (
        "Research on {ticker} was terminated{reason} - see the Research page before treating "
        "its fair value as current."
    ),
    "portfolio.switch.trim_title": "Or trim instead of switching fully",
    "portfolio.switch.trim_caption": (
        "Move only part of the {from_ticker} position into {to_ticker} rather than all of it - "
        "brokerage is a flat fee either way, so a small trim pays proportionally more of it."
    ),
    "portfolio.switch.trim_slider": "Fraction of {ticker} to move",
    "portfolio.switch.trim_toll_line": "Toll on this trim: A\\${toll} ({pct}% of the trimmed amount)",
    "portfolio.switch.trim_blended_return": "Blended implied return after the trim: ≈{pct}%/yr",
    "portfolio.switch.trim_sim_title": "Simulated 1-year range after the trim",
    "portfolio.switch.trim_sim_unavailable": "Not enough shared price history to simulate this trim yet.",

    # Next-batch instruction, Part 4: Rational Compounder page - company
    # shelf + verdict-first opener. Only the NEW opener strings this pass
    # introduces are covered here - the seven section names themselves
    # (Fundamentals, Value vs Book, ...) and everything each section
    # renders below were never i18n'd before this pass either and stay
    # out of scope, same as Part 1's own documented boundary.
    "research.kicker": "RATIONAL COMPOUNDER RESEARCH",
    # Mega-batch Part 9: shown instead of the shelf/detail view whenever
    # compounder_data.json hasn't been (re)built yet on this volume - a
    # brand new/empty volume now that the stale repo seed is gone, or the
    # short window right after an admin wipes it. Never a traceback.
    "research.not_ready": (
        "The hand-built research data is being rebuilt - back shortly."
    ),
    "research.shelf_title": "Hand-built research, one company at a time",
    "research.shelf_positioning": (
        "Every company below has a full, hand-written valuation and thesis "
        "behind it - not a screen, not a score: real analysis, updated as "
        "the underlying business changes."
    ),
    "research.housekeeping_snapshot": "Data snapshot: current ({date})",
    "research.housekeeping_prioritise": (
        "Want a stock prioritised? Say so via the Feedback button above."
    ),
    "research.rebuild_history_link": "See the model's rebuild history →",
    "research.card_sections_verdict": "{count}/{total} sections · written verdict ✓",
    "research.card_sections_no_verdict": "{count}/{total} sections · research in progress",
    "research.card_updated": "Updated {date}",
    "research.card_position_held": "Held",
    "research.card_position_none": "No position",
    "research.card_cta": "View research →",
    "research.back_to_shelf": "← All companies",
    "research.state2_caption": "Workbook: {date}",
    "research.deep_dive_xlink": "Open the automated Deep Dive →",
    "research.verdict_label": "The author's verdict",
    "research.no_verdict_text": "Written verdict not yet published for this company.",
    "research.key_number_price": "Price",
    "research.key_number_margin_of_safety": "Margin of safety",
    "research.key_number_roic": "ROIC",
    "research.key_number_roic_vs_wacc": "ROIC vs WACC",
    # Mega-batch Part 12: research status per company (in progress /
    # terminated) - a distinct badge + the owner's own stated reason, so
    # a deliberately-stopped writeup (risks found, not worth finalising)
    # never gets misread as merely "not started yet" via the generic
    # in-progress fallback.
    "research.terminated_badge": "⛔ Research terminated",
    "research.terminated_fallback_reason": (
        "Research stopped — see the sections for what was completed."
    ),
}

ES = {
    # Part 5 (site-wide slim nav): shortened to match the EN nav row - see
    # the matching comment in the EN dict above.
    "nav.deep_dive": "Deep Dive",
    "nav.research": "Investigación",
    "nav.comparison": "Comparar",
    "nav.scanner": "Buscador",
    "nav.calendar": "Calendario",
    "nav.portfolio": "Cartera",
    "nav.blog": "Blog",
    "nav.more": "Más",
    "nav.methodology": "Metodología",
    "nav.about": "Acerca de",
    "nav.track_record": "Historial",

    "header.tagline": "Analiza cualquier acción en segundos.",
    "header.search_placeholder": (
        "Ingresa el ticker de la acción (p. ej., CSL.AX o AAPL, o CSL.AX "
        "BHP.AX para comparar)"
    ),
    "header.search_button": "Buscar",
    "header.search_caption": (
        "Un ticker = Análisis Profundo (Deep Dive). Dos o más "
        "(separados por coma o espacio) = Comparación en paralelo. Los "
        "tickers de la ASX (p. ej., CSL.AX) y de EE. UU. (p. ej., AAPL) se "
        "pueden combinar libremente."
    ),

    "lang.en": "EN",
    "lang.es": "ES",

    "account.sign_in": "Iniciar sesión",
    "account.sign_out": "Cerrar sesión",
    "account.subscribe": "Suscribirse",

    "gate.not_configured": (
        "\U0001F512 Las suscripciones aún no están completamente "
        "configuradas - {feature_label} se desbloqueará aquí en "
        "cuanto lo estén. Vuelve a consultar pronto."
    ),
    "gate.locked_title": "\U0001F512 Suscríbete para desbloquear {feature_label}",
    "gate.subscribe_cta": "Suscríbete para seguir navegando",
    "gate.checkout_error": (
        "No se pudo conectar con el sistema de suscripciones en este "
        "momento - inténtalo de nuevo en unos minutos."
    ),
    "gate.signed_in_as": "Sesión iniciada como {email}.",
    "gate.google_signin": "Inicia sesión con Google para continuar",

    "gate.dd_full_breakdown_label": "el desglose completo del Deep Dive",
    "gate.dd_full_breakdown_teaser": (
        "Los puntajes de Calidad, Psicología, Descubrimiento y Trade "
        "Setup - el desglose completo de factores detrás del {score_word} "
        "de arriba."
    ),
    "gate.dd_reverse_dcf_label": "Lo que implica el precio - DCF inverso",
    "gate.dd_reverse_dcf_teaser": (
        "La tasa de crecimiento del FCF que el mercado está descontando "
        "actualmente para esta acción, junto con la tasa de crecimiento "
        "que asumió el propio modelo."
    ),
    "gate.rc_potential_label": "Potencial de la empresa - tus propias notas de investigación",
    "gate.rc_potential_teaser": (
        "Las calificaciones Bajo/Medio/Alto del autor y el análisis "
        "completo por escrito de cada empresa cubierta."
    ),

    "dd.verdict.base": (
        "Estimación del modelo: {iv} {ccy} frente al precio de {price} "
        "{ccy} ({mos} de margen de seguridad)"
    ),
    "dd.verdict.growth_suffix": (
        " — el mercado está descontando un {implied} de crecimiento; "
        "el modelo supone un {model}."
    ),
    "dd.verdict.plain_suffix": ".",
    "dd.verdict.default_note": (
        "Se basa en un dato predeterminado o estimado porque no había una "
        "cifra reportada disponible - consulta las notas más abajo."
    ),

    "dd.chip.reverse_dcf": "DCF inverso",
    "dd.chip.moat": "Foso",
    "dd.chip.moat_scored": "Foso {score}",
    "dd.chip.ask_ai": "Preguntar a la IA",
    "dd.chip.insider": "Movimientos de insiders",
    "dd.chip.dividends": "Dividendos",
    "dd.chip.financials": "Financieros de 10 años",
    "dd.chip.peers": "Comparables",
    "dd.chip.scroll_cue": "Análisis completo abajo ↓",

    "dd.kpi.price": "Precio",
    "dd.kpi.intrinsic_value": "Valor intrínseco",
    "dd.kpi.mos_label": "Margen de seguridad (descuento sobre el valor estimado)",
    # Matches the established glossary from the already-shipped ES
    # methodology page (site_content.py: "el Puntaje Value/Long") - kept
    # "Value"/"Long" untranslated, only "Score" -> "Puntaje", rather than
    # inventing a second, inconsistent Spanish name for the same score.
    "dd.kpi.value_score": "Puntaje Value",
    "dd.kpi.long_score": "Puntaje Long",
    "dd.kpi.signal": "Señal",

    "dd.gauge.quality": "Calidad - {label}",
    "dd.gauge.psychology": "Psicología - {label}",
    "dd.gauge.discovery": "Descubrimiento - {label}",
    "dd.gauge.moat": "Foso - {label}",
    "dd.gauge.mos": "Margen de Seguridad - {label}",
    "dd.gauge.value_score": "Puntaje Value",
    "dd.gauge.long_score": "Puntaje Long - {label}",
    "dd.gauge.trade_setup": "Trade Setup - {label}",

    "hook.next_report": (
        "**{ticker} reporta el {date}.** Recibe el análisis de antes/"
        "después por correo cuando lo haga:"
    ),
    "hook.generic": "Recibe un aviso cuando cambien los números de {ticker}:",
    "hook.email_label": "Correo electrónico",
    "hook.email_placeholder": "tucorreo@ejemplo.com",
    "hook.notify_me": "Avisarme",
    "hook.invalid_email": "Esa dirección de correo no parece válida.",
    "hook.enter_code": "Ingresa el código de 6 dígitos enviado a {email}.",
    "hook.code_label": "Código de 6 dígitos",
    "hook.verify": "Verificar",
    "hook.resend": "Reenviar código",
    "hook.done": (
        "Listo — recibirás el análisis del reporte de {ticker}. Has "
        "iniciado sesión."
    ),

    "aigate.sign_in": "Inicia sesión para hacer una pregunta.",
    "aigate.temp_unavailable": (
        "Las funciones de IA no están disponibles en este momento - "
        "inténtalo de nuevo en unos minutos."
    ),
    "aigate.monthly_cap": (
        "Las funciones de IA alcanzaron el límite de uso de este mes - "
        "vuelve el próximo mes."
    ),
    "aigate.plus_monthly_limit": (
        "Ya usaste las {limit} preguntas incluidas este mes - se "
        "reinicia el día 1."
    ),
    "aigate.plus_daily_limit": (
        "Alcanzaste el límite de hoy de {limit} preguntas - vuelve mañana."
    ),
    "aigate.free_daily_limit": (
        "Ya usaste tus {limit} preguntas gratis de hoy - vuelve mañana, "
        "o suscríbete para 300 al mes."
    ),

    "email.announce.subject": "Nueva investigación en StocksDeepDive: {tickers}",
    "email.announce.heading": "Hay nueva investigación disponible",
    "email.announce.intro": (
        "El libro de trabajo de investigación Rational Compounder se "
        "actualizó el {date}. Haz clic en cualquier ticker de abajo para "
        "ver su página de investigación en vivo."
    ),
    "email.announce.th_ticker": "Ticker",
    "email.announce.th_change": "Qué cambió",
    "email.announce.tag_added": "Agregado",
    "email.announce.tag_updated": "Actualizado",
    "email.announce.footer": (
        "Solo información objetiva - este correo enlaza a datos y "
        "resultados de la calculadora calculados a partir de los datos "
        "indicados; no contiene recomendaciones para comprar, mantener o "
        "vender ningún valor. Recibes este correo porque solicitaste que "
        "se te avisara cuando esta investigación se actualice. Para "
        "darte de baja, responde STOP, o abre cualquier página de "
        "investigación de arriba y haz clic en \"Siguiendo - clic para "
        "dejar de seguir\" (o deja de seguir mientras tienes sesión "
        "iniciada) en el sitio."
    ),

    "dd.plain.value_score": (
        "En términos simples: un número que combina la calidad del "
        "negocio, el precio frente al valor estimado, la psicología de "
        "la multitud y la atención del mercado."
    ),
    "dd.plain.quality": (
        "En términos simples: qué tan sólido es el negocio subyacente - "
        "rentabilidad, solidez del balance y crecimiento - a partir de "
        "sus propios estados financieros."
    ),
    "dd.plain.psychology": (
        "En términos simples: si la multitud que opera esta acción "
        "ahora mismo se ve temerosa, tranquila o codiciosa, leído a "
        "partir del comportamiento reciente del precio."
    ),
    "dd.plain.discovery": (
        "En términos simples: cuánta atención está recibiendo esta "
        "acción ahora mismo, a partir del interés de búsqueda, las "
        "noticias y el volumen de operaciones."
    ),
    "dd.plain.moat": (
        "En términos simples: qué tan bien protegidas están las "
        "ganancias de este negocio frente a la competencia, según el "
        "retorno sobre el capital y la durabilidad del margen."
    ),
    "dd.plain.mos": (
        "En términos simples: cuánto más barato es el precio de hoy "
        "que lo que el modelo estima que vale el negocio."
    ),

    "dd.chart.driving_score": "Qué impulsa el {score_word} (puntos aportados por cada factor)",
    "dd.chart.points_score": "Puntos hacia el {score_word}",
    "dd.chart.driving_quality": "Qué impulsa la Calidad (términos ponderados)",
    "dd.chart.points_quality": "Puntos hacia Calidad",
    "dd.chart.driving_psychology": "Qué impulsa la Psicología (Miedo - Codicia - FOMO)",
    "dd.chart.points_psychology": "Puntos hacia Psicología",
    "dd.chart.driving_discovery": "Qué impulsa el Descubrimiento (atención y momentum)",
    "dd.chart.points_discovery": "Puntos hacia Descubrimiento",
    "dd.chart.driving_moat": "Qué impulsa el Foso (durabilidad del retorno)",
    "dd.chart.points_moat": "Puntos hacia el Foso",
    "dd.chart.driving_trade_setup": "Qué impulsa el puntaje de Trade Setup",
    "dd.chart.points_trade_setup": "Puntos hacia el puntaje de Trade Setup",

    "dd.history.title": "{score_word} en el tiempo",
    "dd.history.show_quality": "Mostrar Calidad",
    "dd.history.show_moat": "Mostrar Foso",
    "dd.history.mos_title": "Margen de seguridad en el tiempo",
    "dd.history.caption": "Calculado cada noche; los huecos son días en que la acción no fue escaneada.",

    "dd.price.title": "Últimos 6 meses",
    "dd.price.legend_price": "Precio",
    "dd.price.entry_zone": "Zona de entrada {value}",

    "dd.rdcf.heading": "Lo que implica el precio",
    "dd.rdcf.forecast_caption": "Un cálculo descrito a partir de datos indicados, no un pronóstico.",
    "dd.rdcf.implied_growth": "Crecimiento implícito",
    "dd.rdcf.model_growth": "Crecimiento del modelo",
    "dd.rdcf.marker_capped": "Marcador limitado a -10%/30% para mayor claridad.",
    "dd.rdcf.default_note": (
        "Se basa en un flujo de caja libre predeterminado o estimado - "
        "consulta la nota bajo Valor intrínseco más arriba."
    ),

    "dd.peer.heading": "Comparación con pares",
    "dd.peer.no_data": "Aún no hay datos de pares para {ticker} - todavía no está en un universo escaneado durante la noche.",
    "dd.peer.provenance": "frente al escaneo nocturno de anoche (atención simplificada).",
    "dd.peer.no_rankable": "Aún no hay puntajes clasificables para este ticker.",
    "dd.peer.closest_peers": "Pares más cercanos - {source}",
    "dd.peer.compare_button": "Comparar estos →",
    "dd.peer.no_peers": "No se encontraron pares en {universe} para comparar.",
    "dd.peer.pct_top": "entre el {pct}% mejor",
    "dd.peer.pct_bottom": "entre el {pct}% más bajo",
    "dd.peer.of": "de",

    "dd.heading.dividends": "Dividendos",
    "dd.heading.insider": "Movimientos de insiders y capital",

    "dd.actions.watchlist_button": "☆ Seguimiento",
    "dd.actions.alerts_button": "\U0001F514 Alertas",
    "dd.actions.checklist_button": "\U0001F4CB Lista de verificación",

    "dd.alert.expander": "\U0001F514 Avisarme cuando {ticker}...",
    "dd.alert.signin_prompt": (
        "Inicia sesión (arriba a la izquierda) para recibir un correo o "
        "una notificación push cuando los propios números calculados de "
        "{ticker} crucen una línea que elijas."
    ),

    "dd.checklist.expander": "\U0001F4CB Mi lista de verificación para {ticker}",
    "dd.checklist.signin_prompt": (
        "Inicia sesión (arriba a la izquierda) para llevar una lista de "
        "verificación privada previa a la compra y una nota de tesis "
        "para {ticker}."
    ),

    "dd.download.popover": "\U0001F4E5 Descargar datos",
    "dd.download.caption": "Todo lo de esta página para {ticker}, como una hoja de cálculo.",
    "dd.download.xlsx_button": "Libro de Compounder View (.xlsx)",
    "dd.download.csv_button": "Valoración + Puntajes (.csv)",
    "dd.download.xlsx_failed": "No se pudo generar el libro en este momento.",
    "dd.download.csv_failed": "No se pudo generar el CSV en este momento.",
    "dd.download.fair_value_omitted": (
        "Hoja de Fair Value omitida del libro - suscríbete para incluirla."
    ),

    "dd.copytext.button_label": "Copiar como texto",

    "gate.results_full_label": "los resultados completos de {page_label}",
    "gate.results_teaser": (
        "Valoración (Valor intrínseco, MOS), Calidad, Psicología, "
        "Descubrimiento y detalle de Trade Setup para cada acción de "
        "arriba."
    ),

    "dd.legend.quality": "Calidad",
    "dd.legend.moat": "Foso",
    "dd.legend.mos": "MOS",

    "dd.results.heading": "Reportado el {date} - antes/después",
    "dd.results.stale_warning": (
        "Las cifras \"después\" de abajo podrían aún basarse en un informe "
        "que Yahoo no ha terminado de procesar - se revisa automáticamente "
        "unos días después del informe; si estas cifras no cambiaron "
        "respecto a \"antes\", vuelve a comprobar en unos días."
    ),
    "dd.results.what_moved": "Qué cambió:",
    "dd.results.footer_caption": (
        "Una comparación calculada de antes/después a partir del propio "
        "sistema de puntuación del sitio - un cálculo descrito, no una "
        "recomendación."
    ),

    "signin.google_button": "Continuar con Google",
    "signin.or_divider": "o",
    "signin.email_label": "Correo electrónico",
    "signin.email_placeholder": "tucorreo@ejemplo.com",
    "signin.website_honeypot_label": "Sitio web",
    "signin.send_code_button": "Enviarme un código de acceso",
    "signin.code_label": "Código de 6 dígitos",
    "signin.code_placeholder": "123456",
    "signin.verify_button": "Verificar",
    "signin.resend_button": "Reenviar código",

    "email_auth.invalid_email": "Esa dirección de correo no parece válida.",
    "email_auth.not_configured": (
        "El inicio de sesión por correo no está disponible en este momento "
        "- prueba con Google."
    ),
    "email_auth.too_many_ip": (
        "Se solicitaron demasiados códigos desde esta conexión hoy - "
        "vuelve a intentarlo mañana."
    ),
    "email_auth.too_many_today": (
        "Se solicitaron demasiados códigos hoy - vuelve a intentarlo "
        "mañana."
    ),
    "email_auth.send_failed": (
        "No se pudo enviar el correo en este momento - vuelve a intentarlo."
    ),
    "email_auth.code_sent": (
        "Código enviado a {email} - revisa tu bandeja de entrada (y la "
        "carpeta de spam)."
    ),
    "email_auth.enter_code": "Ingresa el código de 6 dígitos del correo.",
    "email_auth.no_code": "No hay ningún código registrado - solicita uno nuevo.",
    "email_auth.too_many_attempts": (
        "Demasiados intentos fallidos - solicita un código nuevo."
    ),
    "email_auth.code_expired": "Ese código expiró - solicita uno nuevo.",
    "email_auth.wrong_code": "Código incorrecto - revisa el correo e inténtalo de nuevo.",
    "email_auth.signed_in": "Sesión iniciada.",

    "follow.notify_button": "Avisarme",
    "follow.caption": "Recibe un correo cuando se actualice el análisis de {ticker}.",
    "follow.enter_code_prompt": "Ingresa el código de 6 dígitos enviado a {email} para {ticker}.",
    "follow.following_label": "🔔 Siguiendo — clic para dejar de seguir",
    "follow.notify_label": "🔔 Avisarme cuando se actualice el análisis",
    "follow.save_failed": "No se pudo guardar en este momento - vuelve a intentarlo.",

    "blog.subscribe.already_heading": "Ya estás en la lista.",
    "blog.subscribe.already_body": "Recibirás la próxima nota de investigación por correo.",
    "blog.subscribe.heading": "Recibe la próxima nota de investigación por correo - gratis.",
    "blog.subscribe.email_placeholder": "tucorreo@ejemplo.com",
    "blog.subscribe.subscribe_button": "Suscribirse",
    "blog.subscribe.code_placeholder": "123456",
    "blog.subscribe.verify_button": "Verificar",
    "blog.subscribe.done": "Listo - ya estás en la lista.",
    "blog.subscribe.js_invalid_email": "Esa dirección de correo no parece válida.",
    "blog.subscribe.js_sending": "Enviando...",
    "blog.subscribe.js_checking": "Verificando...",
    "blog.subscribe.js_network_error": "No se pudo conectar con el servidor - vuelve a intentarlo.",
    "blog.subscribe.js_wrong_code_fallback": "Código incorrecto - inténtalo de nuevo.",

    "email.digest.subject_factual": "Tu lista de seguimiento en StocksDeepDive - actualización semanal",
    "email.digest.subject_signal": "Tu lista de seguimiento en StocksDeepDive - señales semanales",
    "email.digest.heading": "Tu lista de seguimiento esta semana",
    "email.digest.intro": (
        "Nueva puntuación semanal de las acciones que guardaste, al {date}. "
        "Haz clic en cualquier ticker para ver su Deep Dive completo en vivo."
    ),
    "email.digest.disclaimer_factual": (
        "Solo información factual y resultados de la calculadora - este "
        "correo describe datos y resultados de modelos calculados a partir "
        "de datos indicados; no contiene recomendaciones de comprar, "
        "mantener o vender ningún valor."
    ),
    "email.digest.disclaimer_general": (
        "Solo información general - no es asesoría financiera; los "
        "puntajes y señales son resultados de un modelo y no consideran tu "
        "situación personal."
    ),
    "email.digest.footer_note": (
        "Calculado sin datos de atención de noticias/redes sociales (la "
        "actualización semanal usa el modelo attention-lite). Recibes esto "
        "porque {email} guardó una lista de seguimiento en StocksDeepDive "
        "estando conectado. Para dejar de recibirlo, elimina todas las "
        "acciones de tu lista de seguimiento en el sitio."
    ),

    "alert.metric.mos_pct": "MOS",
    "alert.metric.value_score": "Puntaje Value",
    "alert.metric.quality": "Calidad",
    "alert.metric.moat": "Foso",
    "alert.metric.price": "Precio",
    "alert.metric.intrinsic_value": "Valor intrínseco",
    "alert.metric.moat_state": "Estado del foso",
    "alert.metric.valuation_label": "Valoración",
    "alert.op.crossed_above": "cruzó por encima de",
    "alert.op.crossed_below": "cruzó por debajo de",
    "alert.condition_numeric": "{label} {op} {threshold} (ahora {current})",
    "alert.condition_categorical": "{label} pasó a ser {value}",
    "alert.hit_message": (
        "{ticker} — condición cumplida: {condition}. Cálculo descrito, no "
        "una recomendación. Abrir el Deep Dive →"
    ),
    "email.alert.subject_one": "{n} condición de alerta cumplida esta noche",
    "email.alert.subject_many": "{n} condiciones de alerta cumplidas esta noche",
    "email.alert.heading_one": "{n} condición de alerta cumplida esta noche",
    "email.alert.heading_many": "{n} condiciones de alerta cumplidas esta noche",
    "email.alert.footer": (
        "Cada línea de arriba describe un cálculo que le pediste a "
        "StocksDeepDive que vigilara, a partir de los propios datos del "
        "sitio - no es una recomendación de comprar, mantener o vender "
        "ningún valor. Administra o elimina tus alertas en cualquier "
        "momento desde la página Deep Dive de una acción o desde Mi "
        "cartera &rarr; Mis alertas."
    ),
    "push.alert.body": "Toca para ver qué se activó en StocksDeepDive.",

    "email.results.subject": "{ticker} reportó el {date} - antes/después",
    "email.results.reported_on": "reportó el {date}",
    "email.results.no_moves": "No se calculó ningún cambio material.",
    "email.results.vs_line": "Puntaje Value ahora {vs}.",
    "email.results.footer": (
        "Una comparación calculada de antes/después a partir del propio "
        "sistema de puntuación del sitio - no es una recomendación de "
        "comprar, mantener o vender ningún valor. Antes/después completo "
        "en la página Deep Dive de arriba."
    ),
    "email.results.push_fallback": "{ticker} reportó el {date}.",

    "email.watchdog.subject": "Vigilante de cartera de StocksDeepDive: {tickers}",
    "email.watchdog.heading_one": "Vigilante de cartera - {n} posición cambió",
    "email.watchdog.heading_many": "Vigilante de cartera - {n} posiciones cambiaron",
    "email.watchdog.intro": (
        "Algo material se movió desde tu último resumen, al {date}. Haz "
        "clic en cualquier ticker para ver su Deep Dive completo en vivo."
    ),
    "email.watchdog.footer": (
        "Solo información factual - cada resumen de arriba describe datos "
        "y resultados de un modelo calculados a partir de datos "
        "indicados; no contiene recomendaciones de comprar, mantener o "
        "vender ningún valor, y está redactado por un modelo de IA a "
        "partir de tu tesis guardada y los propios datos del sitio, no "
        "por una persona. Recibes esto porque activaste el vigilante de "
        "IA para una cartera en StocksDeepDive - puedes desactivarlo en "
        "cualquier momento en la configuración de esa cartera."
    ),
    "email.ai_label": "Redactado por IA a partir de los datos del sitio - no es un consejo",
    "push.watchdog.title": "Vigilante de cartera: {tickers}",
    "push.watchdog.body": "Toca para ver qué cambió en StocksDeepDive.",

    "email.brief.subject_ai": "Tu resumen semanal de StocksDeepDive",
    "email.brief.heading": "Tu resumen semanal",
    "email.brief.intro": "Al {date}. Haz clic en cualquier ticker para ver su Deep Dive completo en vivo.",
    "email.brief.reporting_this_week": "Reporta esta semana:",
    "email.brief.full_calendar_link": "Calendario completo de resultados →",
    "email.brief.health_heading": "Salud de la cartera",
    "email.brief.posts_heading": "Nueva investigación esta semana",
    "email.brief.footer_note": (
        "Calculado sin datos de atención de noticias/redes sociales (este "
        "resumen usa el modelo attention-lite). Recibes esto porque "
        "{email} guardó una lista de seguimiento en StocksDeepDive "
        "estando conectado. Para dejar de recibirlo, elimina todas las "
        "acciones de tu lista de seguimiento en el sitio."
    ),

    "home.top5.kicker": "TOP 5 DE ESTA NOCHE",
    "home.top5.heading": "El top 5 de esta noche — {country}",
    "home.top5.country_au": "Australia",
    "home.top5.country_us": "EE. UU.",
    "home.top5.col_ticker": "Ticker",
    "home.top5.col_price": "Precio",
    "home.top5.col_value_score": "Value Score",
    "home.top5.col_valuation": "Valoración",
    "home.top5.col_universe": "Universo",
    "home.top5.caption": (
        "De los escaneos nocturnos más recientes en todos los universos "
        "cubiertos (diarios + semanales) — un resultado de ordenamiento a "
        "partir de cálculos descritos, no una recomendación. Tablas "
        "completas en el {link}"
    ),
    # Note: deliberately ends in "todavía." rather than "{country}." -
    # {country} can resolve to "EE. UU.", whose own trailing period would
    # otherwise double up with the sentence's.
    "home.top5.no_scan": "No hay un escaneo nocturno reciente para {country} todavía.",
    "home.top5.as_of": "al {day}",

    # Next-batch instruction, Part 1: ES translations matching the EN keys
    # added above, term for term against the live EN copy.
    "home.hero.title_factual": "Los <em>datos y modelos</em> detrás de un juicio de valoración.",
    "home.hero.sub_factual": (
        "Valores intrínsecos en vivo, cálculos de calidad, y lecturas de "
        "psicología y descubrimiento &mdash; calculados para cualquier "
        "acción de la ASX o EE. UU., con <b>cada dato declarado y cada "
        "estimación señalada</b>. El juicio sigue siendo tuyo."
    ),
    "home.hero.title_signal": "Descubre cuánto <em>vale</em> una acción &mdash; y si ahora es un momento sensato para entrar.",
    "home.hero.sub_signal": (
        "Un puntaje que combina <b>valor, calidad, psicología de la "
        "multitud y atención del mercado</b> &mdash; calculado en vivo "
        "para cualquier acción de la ASX o EE. UU. Sin ruido ni supuestos "
        "ocultos: cada número estimado está señalado."
    ),
    "home.hero.search_aria": "Buscar ticker",
    "home.hero.search_placeholder": "CSL.AX o AAPL  ·  o dos tickers para comparar: CSL.AX BHP.AX",
    "home.hero.analyze_button": "Analizar",
    "home.hero.search_hint": (
        "Un ticker = Deep Dive completo &middot; Dos o más = comparación "
        "en paralelo &middot; ASX + EE. UU. mezclados libremente"
    ),

    "home.toolkit.kicker": "EL KIT DE HERRAMIENTAS",
    "home.toolkit.h2": "Cuatro formas de empezar. Un solo modelo consistente.",
    "home.toolkit.secsub_factual": (
        "Cada herramienta usa el mismo motor &mdash; el mismo modelo DCF, "
        "el mismo cálculo de calidad, la misma lectura de psicología "
        "&mdash; así que los números siempre concuerdan entre sí."
    ),
    "home.toolkit.secsub_signal": (
        "Cada herramienta usa el mismo motor &mdash; el mismo DCF, las "
        "mismas pruebas de calidad, la misma lectura de psicología "
        "&mdash; así que los números siempre concuerdan entre sí."
    ),
    "home.toolkit.card1_title": "Deep Dive de la acción",
    "home.toolkit.card1_desc_factual": (
        "El panorama completo de un ticker: valor intrínseco frente al "
        "precio de hoy, qué impulsa el Value Score, y lecturas de "
        "psicología y descubrimiento &mdash; cada dato declarado."
    ),
    "home.toolkit.card1_desc_signal": (
        "El panorama completo de un ticker: valor intrínseco frente al "
        "precio, qué impulsa el Long Score, la psicología de la multitud "
        "y una zona de entrada técnica con stop y objetivos."
    ),
    "home.toolkit.card2_title": "Comparación en paralelo",
    "home.toolkit.card2_desc_factual": (
        "Dos o más tickers alineados sobre cálculos idénticos &mdash; "
        "valor intrínseco, cálculo de calidad, psicología &mdash; como "
        "barras de datos con código de color."
    ),
    "home.toolkit.card2_desc_signal": (
        "Dos o más tickers alineados sobre criterios idénticos &mdash; "
        "valoración, calidad, sentimiento, tendencia, entrada &mdash; "
        "como barras con código de color y etiquetas de veredicto."
    ),
    "home.toolkit.card3_title": "Escáner de acciones",
    "home.toolkit.card3_desc_factual": (
        "Un índice completo &mdash; ASX 200, S&amp;P 500 y más &mdash; "
        "como una tabla de datos ordenable, calculada cada noche, con un "
        "filtro de sector opcional. Ordenar es puramente aritmético."
    ),
    "home.toolkit.card3_desc_signal": (
        "Clasifica un índice completo &mdash; ASX 200, S&amp;P 500 y más "
        "&mdash; por Long Score, con un filtro de sector opcional. "
        "Encuentra qué mirar, no solo revisa lo que ya tienes."
    ),
    "home.toolkit.card4_title": "Investigación Rational Compounder",
    "home.toolkit.card4_desc_factual": (
        "Investigación hecha a mano sobre compounders seleccionados "
        "&mdash; una década de resultados reportados, cuatro modelos de "
        "valor razonable, e historias de empresas documentadas."
    ),
    "home.toolkit.card4_desc_signal": (
        "Investigación hecha a mano, al estilo Buffett/Munger, sobre "
        "compounders seleccionados &mdash; una década de resultados, "
        "cuatro métodos de valor razonable, y un juicio escrito sobre "
        "cada negocio."
    ),
    "home.toolkit.card5_title": "Mi cartera",
    "home.toolkit.card5_desc_factual": (
        "Sigue lo que realmente posees frente al precio y los "
        "fundamentos del día en que compraste &mdash; privado para tu "
        "cuenta conectada, requiere inicio de sesión."
    ),
    "home.toolkit.card5_desc_signal": (
        "Agrega lo que realmente posees y fija la base del día de compra "
        "&mdash; privado solo para tu cuenta conectada, requiere inicio "
        "de sesión."
    ),

    # Part 5 (home tiles, Option C) - Spanish counterparts, see the EN
    # dict's matching comment for what fills {count}/{latest}/{universes}.
    "home.tiles.free_badge": "GRATIS",
    "home.tiles.research_title": "Investigación hecha a mano",
    "home.tiles.research_desc": (
        "Una empresa a la vez, semanas de trabajo cada una &mdash; tesis, "
        "valoración y veredicto escritos en su totalidad."
    ),
    "home.tiles.research_stat": "{count} empresas &middot; última: {latest}",
    "home.tiles.scanner_title": "Buscador de acciones",
    "home.tiles.scanner_desc": (
        "Cada acción puntuada cada noche en valor, calidad y foso "
        "&mdash; ordenable, filtrable, exportable."
    ),
    "home.tiles.scanner_stat": "{count} acciones &middot; {universes} universos",
    "home.tiles.compare_title": "Comparar",
    "home.tiles.compare_desc": (
        "Dos o más tickers alineados sobre cálculos idénticos &mdash; "
        "valoración, calidad, foso &mdash; como datos con código de color."
    ),
    "home.tiles.portfolio_title": "Mi cartera",
    "home.tiles.portfolio_desc": (
        "Tus posiciones revisadas cada noche &mdash; ingresos, ETFs, "
        "prueba de estrés, vigilante con IA."
    ),
    "home.tiles.portfolio_stat": "{count} herramientas incluidas",

    "home.hiw.kicker": "CÓMO FUNCIONA",
    "home.hiw.h2_factual": "Busca. Calcula. Inspecciona.",
    "home.hiw.h2_signal": "Busca. Puntúa. Decide.",
    "home.hiw.step1_title": "Escribe cualquier ticker",
    "home.hiw.step1_desc": (
        "ASX (CSL.AX) o EE. UU. (AAPL). Los datos en vivo se obtienen al "
        "instante &mdash; precios, flujos de caja, noticias, tendencias "
        "de búsqueda, conversación social."
    ),
    "home.hiw.step2_title_factual": "Obtén un cálculo transparente",
    "home.hiw.step2_desc_factual": (
        "El Value Score combina el cálculo de calidad, el MOS (la "
        "diferencia entre precio y valor intrínseco), psicología y "
        "descubrimiento &mdash; la misma aritmética cada vez, con cada "
        "dato mostrado."
    ),
    "home.hiw.step2_title_signal": "Obtén un puntaje honesto",
    "home.hiw.step2_desc_signal": (
        "El Long Score combina la calidad del negocio, el margen de "
        "seguridad, la psicología de la multitud y la atención del "
        "mercado &mdash; las mismas matemáticas de value investing cada "
        "vez, con cada dato mostrado."
    ),
    "home.hiw.step3_title_factual": "Ve el valor Y la psicología",
    "home.hiw.step3_desc_factual": (
        "Dos cálculos separados, nunca mezclados: lo que el modelo "
        "calcula a partir de los propios flujos de caja del negocio, y lo "
        "que la multitud ha estado haciendo con el precio &mdash; ambos "
        "expresados como números, uno junto al otro."
    ),
    "home.hiw.step3_title_signal": "Ve el valor Y el momento de entrada",
    "home.hiw.step3_desc_signal": (
        "Dos veredictos separados, nunca mezclados: ¿es este un buen "
        "negocio para <em>poseer</em>, y es ahora mismo una <em>entrada</em> "
        "sensata? Una gran empresa todavía puede ser una mala compra hoy."
    ),
    "home.hiw.honesty": (
        "<b>La regla de la bandera roja:</b> cuando un número se apoya en "
        "un valor por defecto o un promedio porque no había datos reales "
        "disponibles, se muestra en rojo. Una estimación nunca se "
        "presenta como un hecho &mdash; siempre sabes qué números están "
        "calculados y cuáles son supuestos."
    ),

    "home.results_day.kicker": "DÍA DE RESULTADOS",
    "home.results_day.h2": "Reportado esta semana",
    "home.results_day.col_ticker": "Ticker",
    "home.results_day.col_reported": "Reportado",
    "home.results_day.col_value_now": "Value Score ahora",
    "home.results_day.col_vs_before": "vs antes",
    "home.results_day.caption": (
        "Tickers que reportaron resultados en los últimos 7 días, con un "
        "reanálisis antes/después calculado - abre la página Deep Dive de "
        "un ticker para la comparación completa y qué cambió."
    ),

    "home.results_calendar.kicker": "CALENDARIO DE RESULTADOS",
    "home.results_calendar.h2": "Reporta esta semana",
    "home.results_calendar.col_ticker": "Ticker",
    "home.results_calendar.col_date": "Fecha",
    "home.results_calendar.col_status": "Estado",
    "home.results_calendar.status_reported": "&#10003; reportado",
    "home.results_calendar.status_expected": "~ esperado",
    "home.results_calendar.caption": (
        "Fechas del proveedor de datos; las fechas confirmadas se marcan "
        "✓, las estimadas ~. "
    ),
    "home.results_calendar.caption_mine": "Tus propios tickers - ",
    "home.results_calendar.caption_link": "[Calendario completo de resultados →](/results-calendar)",

    "home.compounder.kicker": "INVESTIGACIÓN RATIONAL COMPOUNDER",
    "home.compounder.h2": "Cubierto en profundidad hoy",
    "home.compounder.secsub": (
        "Se agregan nuevas empresas a medida que se completa la "
        "investigación &mdash; cada una toma semanas, no minutos."
    ),
    "home.compounder.sections_label": "Secciones de investigación",
    "home.compounder.verdict_label": "Veredicto escrito",
    "home.compounder.request_title": "¿Qué acción debería investigarse a continuación?",
    "home.compounder.request_cta": "Dínoslo con Feedback &rarr;",

    "home.blog.kicker": "DEL BLOG",
    "home.blog.h2": "Últimas notas de investigación",
    "home.blog.secsub": "El razonamiento detrás de los números, explicado en detalle &mdash; ",
    "home.blog.all_posts": "todas las publicaciones &rarr;",
    "home.blog.min_read": "min de lectura",

    "home.cta.title": "Todo es gratis.",
    "home.cta.desc": (
        "Inicia sesión (arriba a la izquierda) para armar una lista de "
        "seguimiento de cada ticker que revises y recibir el resumen "
        "semanal de {digest_word}."
    ),
    "home.cta.digest_watchlist": "lista de seguimiento",
    "home.cta.digest_signal": "señales",

    "home.what_you_get.tag": "LO QUE OBTIENES",
    "home.what_you_get.intro": "Cada búsqueda responde tres preguntas:",
    "home.what_you_get.q1_factual": "¿Cuál es el valor intrínseco?",
    "home.what_you_get.a1_factual": "mostrado junto al precio de hoy con el MOS indicado como porcentaje",
    "home.what_you_get.q1_signal": "¿Cuánto vale?",
    "home.what_you_get.a1_signal": "más el margen de seguridad frente al precio de hoy",
    "home.what_you_get.dcf_lead": "Un DCF en vivo con una tasa de descuento por acción,",
    "home.what_you_get.q2": "¿Es un buen negocio?",
    "home.what_you_get.a2": "Un Quality Score de 0 a 100 a partir de pruebas de rentabilidad y balance.",
    "home.what_you_get.q3_factual": "¿Qué está haciendo la multitud?",
    "home.what_you_get.a3_factual": (
        "Lecturas de psicología y descubrimiento - distancia a máximos "
        "recientes, volumen, atención de búsqueda y noticias - "
        "expresadas como números."
    ),
    "home.what_you_get.q3_signal": "¿Es ahora una entrada sensata?",
    "home.what_you_get.a3_signal": (
        "Psicología de la multitud y una zona de entrada técnica &mdash; "
        "mantenidas separadas de la pregunta de propiedad."
    ),
    "home.featured.open_deep_dive": "Abrir el Deep Dive completo de {ticker} →",
    "home.featured.rule_au": "La selección de hoy: el Puntaje Value más alto en los escaneos australianos de anoche.",
    "home.featured.rule_us": "La selección de hoy: el Puntaje Value más alto en los escaneos estadounidenses de anoche.",

    "home.mood.market_mood": "ÁNIMO DE MERCADO {country}",
    "home.mood.live_reading": "lectura en vivo del tono de las noticias",
    "home.mood.hopeful": "Esperanzado",
    "home.mood.neutral": "Neutral",
    "home.mood.anxious": "Ansioso",
    "home.mood.today": "hoy",

    "chips.try_one": "Prueba uno:",
    "chips.did_you_mean": "¿Quisiste decir:",

    "nl.title": "\U0001F50E Describe lo que buscas",
    "nl.caption": (
        "En lenguaje sencillo, p. ej. “acciones tecnológicas baratas de "
        "la ASX” o “small caps de EE. UU. en salud” - traducido a los "
        "propios filtros de país/universo/sector del Escáner, que "
        "siempre se muestran antes de ejecutar los resultados para que "
        "veas (y cambies) exactamente lo que se aplicó."
    ),
    "nl.signin_prompt": "Inicia sesión (arriba a la izquierda) para probar el filtrado en lenguaje natural.",
    "nl.input_aria": "Describe un filtro",
    "nl.input_placeholder": "p. ej. compounders de calidad baratos en minería australiana",
    "nl.button": "Filtrar",
    "nl.warn_empty": "Escribe primero lo que buscas.",
    "nl.reading": "Leyendo tu solicitud...",

    "dd.data_as_of": "Datos al {date} (último cierre diario disponible).",

    "scanner.change_expander": "+{n} universos más y filtro de sector",
    "scanner.change_instruction": (
        "Marca uno o más países, luego elige un único universo para "
        "escanear - cada universo se escanea de forma totalmente "
        "independiente (ASX 200 y ASX 300 nunca se combinan, y tampoco "
        "ninguno de los universos de EE. UU.)."
    ),
    "scanner.country_au": "Australia",
    "scanner.country_us": "EE. UU.",
    "scanner.universe_label": "Universo",
    "scanner.pick_country_info": "Marca al menos un país arriba para elegir un universo que escanear.",
    "scanner.run_scan_button": "Ejecutar escaneo",
    "scanner.resolving_spinner": "Resolviendo universo...",
    "scanner.no_stocks_warning": "Ninguna acción coincidió con este universo/sector - prueba otra selección.",
    "scanner.empty_message": "Elige un país, universo y (opcionalmente) un sector arriba, luego haz clic en Ejecutar escaneo.",
    "scanner.pills_label": "Universos populares",
    "scanner.sort_by_label": "Ordenar por",
    "scanner.sort_mos": "MOS",
    "scanner.sort_quality": "Calidad",
    "scanner.sort_moat": "Foso",
    "scanner.sort_price": "Precio",
    "scanner.rank_col": "#",
    "scanner.scan_of": "escaneo del {when}",

    "comparison.empty_message": "Busca dos o más tickers arriba para ejecutar una Comparación.",
    "comparison.add_third": "+ añadir un tercero",
    "comparison.compare_button": "Comparar",
    "comparison.need_two_warning": "Ingresa al menos dos tickers para comparar.",
    "comparison.popular_pairs_label": "Comparaciones populares",
    "comparison.h2h_heading": "Cara a cara",
    "comparison.h2h_metric_col": "Métrica",
    "comparison.h2h_price_model": "Precio &rarr; Estimación del modelo",
    "comparison.h2h_quality_moat": "Calidad / Foso",
    "comparison.h2h_roic_wacc": "ROIC vs WACC",
    "comparison.h2h_caption": (
        "Una comparación descrita de cálculos declarados, no una "
        "recomendación - la cifra en verde en cada línea es simplemente "
        "el número más alto, nada más."
    ),

    # Mega-batch Part 7: tablero semanal del Calendario de resultados.
    "calendar.subtitle": "Todos los informes de esta semana, de un vistazo.",
    "calendar.methodology": (
        "Fechas según el proveedor de datos; las fechas confirmadas se "
        "marcan con ✓, las estimadas con ~. Descripciones de cálculos, "
        "no recomendaciones."
    ),
    "calendar.filter_all": "Todos",
    "calendar.filter_my_tickers": "Mis tickers",
    "calendar.filter_universe": "Universo...",
    "calendar.filter_sign_in": "Inicia sesión (arriba a la izquierda) para filtrar por tus propios tickers.",
    "calendar.filter_empty_my_tickers": "Todavía no has guardado nada en tu watchlist, cartera o alertas.",
    "calendar.week_this": "Esta semana",
    "calendar.week_next": "Próxima semana",
    "calendar.today_badge": "Hoy",
    "calendar.status_reported": "✓ reportado",
    "calendar.status_expected": "~ previsto",
    "calendar.pending_reanalysis": "reanálisis pendiente",
    "calendar.pending_before_after": "antes/después próximamente",
    "calendar.later_heading": "Más adelante este mes",
    "calendar.empty_week": "Nada reportado ni previsto esta semana.",
    "calendar.empty_next_week": "Nada previsto para la próxima semana todavía.",
    "calendar.empty_later": "Nada más previsto este mes todavía.",
    "calendar.deep_dive_link": "Análisis a fondo →",
    "calendar.weekend_note": "También reportando este fin de semana:",

    "portfolio.title": "Mi cartera",
    "portfolio.signin_prompt": (
        "Inicia sesión (arriba a la izquierda) para seguir aquí tus "
        "posiciones de largo plazo. Esto es privado para tu cuenta - "
        "nadie más, incluidos otros visitantes conectados, puede verlo."
    ),
    "portfolio.scoring_spinner": "Puntuando tus posiciones...",
    "portfolio.tab_holdings": "\U0001F4BC Posiciones",
    "portfolio.tab_income": "\U0001F4B0 Ingresos",
    "portfolio.tab_overview": "\U0001F4CA Resumen y P/L",
    "portfolio.tab_health": "\U0001FA7A Salud y noticias",
    "portfolio.tab_progress": "\U0001F4C8 Progreso",
    "portfolio.tab_etfs": "\U0001F9FA Mis ETFs",
    "portfolio.tab_stress": "\U0001F9EA Prueba de estrés",
    "portfolio.tab_ask": "\U0001F4AC Preguntar",
    "portfolio.tab_alerts": "\U0001F514 Mis alertas",

    "portfolio.etfs.empty": "No hay ETFs en esta cartera.",
    "portfolio.etfs.summary_title": "Resumen del bloque de ETFs",
    "portfolio.etfs.col_ticker": "Ticker",
    "portfolio.etfs.col_allocation": "Asignación",
    "portfolio.etfs.col_value": "Valor",
    "portfolio.etfs.col_mer": "Comisión (MER)",
    "portfolio.etfs.col_return": "Rentabilidad anual",
    "portfolio.etfs.col_yield": "Rendimiento (TTM)",
    "portfolio.etfs.col_corr_sp500": "Correl. vs S&P 500",
    "portfolio.etfs.col_corr_asx200": "Correl. vs ASX 200",
    "portfolio.etfs.col_top_exposure": "Mayor exposición",
    "portfolio.etfs.totals_label": "Total del bloque",
    "portfolio.etfs.blended_label": "Ponderado",
    "portfolio.etfs.caption_return": (
        "* Rentabilidad total histórica a partir del precio + "
        "distribuciones, neta de la comisión interna del fondo - un "
        "cálculo descrito a partir de datos pasados, no una proyección."
    ),
    "portfolio.etfs.cards_title": "Detalle de los fondos",
    "portfolio.etfs.sector_chart_title": "Ponderación por sector",
    "portfolio.etfs.top10_title": "Las 10 principales posiciones",
    "portfolio.etfs.full_list_link": "Lista completa en el emisor →",
    "portfolio.etfs.issuer_link_label": "Página del emisor ↗",
    "portfolio.etfs.deep_dive_label": "Deep Dive ↗",
    "portfolio.etfs.category_label": "Categoría",
    "portfolio.etfs.no_sector_data": "Ponderación por sector no disponible para este fondo.",
    "portfolio.etfs.no_top_holdings": "Principales posiciones no disponibles para este fondo.",
    "portfolio.etfs.overlap_title": "Solapamiento con tus posiciones directas",
    "portfolio.etfs.overlap_match": (
        # Same "\\$" escaping as the EN template - see its comment.
        "⚠ {underlying} - en posición directa (A\\${direct}) y a través de "
        "{etf} (ponderación {weight}%, ≈A\\${indirect}) - combinado ≈A\\${combined}"
    ),
    "portfolio.etfs.overlap_none": (
        "No se encontró solapamiento entre tus posiciones directas y las "
        "10 principales publicadas de tus ETFs."
    ),
    "portfolio.etfs.top5_title": "Las 5 mayores exposiciones combinadas",
    "portfolio.etfs.overlap_caption": (
        "Calculado a partir de las 10 principales posiciones publicadas "
        "de cada fondo - aproximado; un cálculo descrito, no una "
        "recomendación."
    ),
    "portfolio.etfs.whatif_title": "Proyector \"qué pasaría si\"",
    "portfolio.etfs.whatif_horizon_label": "Años",
    "portfolio.etfs.whatif_rate_label": "Rentabilidad anual asumida %",
    "portfolio.etfs.whatif_result": (
        # Same "\\$" escaping as the EN template - see its comment.
        "Al {rate}% anual durante {years} años, tu bloque de ETFs actual "
        "de A\\${value} pasaría a ser aproximadamente A\\${result}."
    ),
    "portfolio.etfs.whatif_no_rate": (
        "No hay suficientes datos históricos de rentabilidad para sugerir "
        "una tasa de partida - fija una tú mismo."
    ),
    "portfolio.etfs.whatif_caption": "Un cálculo \"qué pasaría si\" a la tasa que fijes - no es una previsión.",
    "portfolio.etfs.na": "—",

    "portfolio.stress.empty": "Añade una posición con precio en vivo para ejecutar una prueba de estrés.",
    "portfolio.stress.loading_spinner": "Reproduciendo tus posiciones sobre precios históricos...",
    "portfolio.stress.intro_caption": (
        "Tus posiciones y ponderaciones actuales aplicadas a precios + "
        "distribuciones históricas reales - hechos sobre el pasado, no "
        "predicciones sobre el futuro."
    ),
    "portfolio.stress.max_downside_title": "MÁXIMA CAÍDA",
    "portfolio.stress.max_upside_title": "MÁXIMA SUBIDA",
    "portfolio.stress.downside_line": (
        "{pct}% (A\\${value}) · {peak} a {trough} · {months} meses para recuperarse"
    ),
    "portfolio.stress.downside_line_no_recovery": (
        "{pct}% (A\\${value}) · {peak} a {trough} · aún sin recuperar"
    ),
    "portfolio.stress.upside_line": "{pct}% (A\\${value}) · {start} a {end}",
    "portfolio.stress.no_data": "No hay suficiente historial de precios compartido entre tus posiciones para reproducir esto todavía.",
    "portfolio.stress.drawdown_chart_title": "Historial de caídas",
    "portfolio.stress.runup_chart_title": "Historial de recuperación",
    "portfolio.stress.replayed_return_line": "Rentabilidad reproducida a {years} años: {pct}% anual",
    "portfolio.stress.crisis_table_title": "Repeticiones de crisis",
    "portfolio.stress.rally_table_title": "Repeticiones de repuntes",
    "portfolio.stress.col_window": "Periodo",
    "portfolio.stress.col_shock": "Choque",
    "portfolio.stress.col_move_pct": "Movimiento de la cartera",
    "portfolio.stress.col_move_value": "Movimiento de la cartera (AUD)",
    "portfolio.stress.col_best_defender": "Mejor defensa",
    "portfolio.stress.col_worst_hit": "Más golpeada",
    "portfolio.stress.col_biggest_engine": "Mayor motor",
    "portfolio.stress.col_drag": "Mayor lastre",
    "portfolio.stress.estimated_flag": "◆ estimado (aproximado vía sector/índice × beta)",
    "portfolio.stress.data_fault_note": (
        "⚠ {tickers}: se detectó un fallo de datos de ajuste por split en "
        "el historial de precios de este valor - se excluye de todos los "
        "números de esta pestaña para que no distorsione las cifras de la "
        "cartera."
    ),
    "portfolio.stress.data_fault_cell": "⚠ fallo de datos — excluido",
    "portfolio.stress.shock_grid_title": "Cuadrícula de choques",
    "portfolio.stress.shock_grid_beta_line": "Beta de la cartera (vs. un índice combinado S&P 500 / ASX 200): {beta}",
    "portfolio.stress.shock_grid_caption": (
        "Un modelo simple ponderado por beta, no una repetición histórica - "
        "una cartera de beta baja amortigua las caídas y también atenúa las "
        "subidas por el mismo mecanismo."
    ),
    "portfolio.stress.rebalance_title": "⚖️ Simulador de reponderación",
    "portfolio.stress.rebalance_never_suggests": (
        "Este sitio nunca sugiere ponderaciones - sin optimizador, sin "
        "recomendación. Fija tú mismo las ponderaciones \"qué pasaría si\" abajo."
    ),
    "portfolio.stress.rebalance_never_modifies": (
        "Nada aquí cambia tu cartera real - esto es solo un cálculo \"qué "
        "pasaría si\"."
    ),
    "portfolio.stress.rebalance_weight_total": "Total: {total}% (debe ser 100% para comparar)",
    "portfolio.stress.rebalance_reset": "Restablecer a las ponderaciones actuales",
    "portfolio.stress.whatif_table_title": "Actual vs. qué pasaría si",
    "portfolio.stress.col_metric": "Métrica",
    "portfolio.stress.col_current": "Actual",
    "portfolio.stress.col_whatif": "Qué pasaría si",
    "portfolio.stress.col_delta": "Δ",
    "portfolio.stress.metric_max_downside": "Máxima caída",
    "portfolio.stress.metric_max_upside": "Máxima subida",
    "portfolio.stress.metric_covid_replay": "Repetición COVID",
    "portfolio.stress.metric_beta": "Beta",
    "portfolio.stress.metric_replayed_10y": "Rentabilidad reproducida a 10 años anual",
    "portfolio.stress.metric_mc_range": "Rango a 1 año, simulado (95%) — no es una predicción",
    "portfolio.stress.per_holding_table_title": "Detalle por posición",
    "portfolio.stress.col_ticker": "Ticker",
    "portfolio.stress.col_weight": "Ponderación",
    "portfolio.stress.col_beta": "Beta",
    "portfolio.stress.col_worst_drawdown": "Peor caída (15 años)",
    "portfolio.stress.col_best_12m": "Mejores 12 meses",
    "portfolio.stress.monte_carlo_title": "Monte Carlo (banda a 1 año)",
    "portfolio.stress.monte_carlo_band_line": (
        "Percentil 5-95: {p5}% a {p95}% (A\\${p5_value} a A\\${p95_value})"
    ),
    "portfolio.stress.monte_carlo_caption": (
        "5.000 trayectorias simuladas a partir de la volatilidad y "
        "correlaciones históricas. Basado en que el pasado se comporte "
        "como el futuro - lo cual puede no ocurrir. No es una predicción."
    ),
    "portfolio.stress.footer_caption": (
        "Las correlaciones aumentan en pánicos severos; las repeticiones "
        "históricas lo capturan (ocurrió en los datos), la cuadrícula y la "
        "simulación lo subestiman en parte."
    ),
    "portfolio.stress.window.gfc": "Crisis financiera de 2008",
    "portfolio.stress.window.q4_2018": "4.º trimestre de 2018",
    "portfolio.stress.window.covid": "Desplome por COVID",
    "portfolio.stress.window.rate_shock_2022": "Shock de tipos de 2022",
    "portfolio.stress.window.post_gfc": "Repunte post-crisis de 2009",
    "portfolio.stress.window.melt_up_2016_17": "Subida de 2016-17",
    "portfolio.stress.window.covid_recovery": "Recuperación post-COVID",
    "portfolio.stress.window.ai_rally_2023_24": "Repunte de la IA 2023-24",
    "portfolio.stress.na": "—",

    "portfolio.tab_switch": "\U0001F504 Analizador de cambio",
    "portfolio.switch.combined_view_note": (
        "Elige una cartera arriba (no \"Todas las carteras\") para usar el Analizador de "
        "cambio - necesita la tasa de impuesto/comisión de una sola cartera y sus bases de coste."
    ),
    "portfolio.switch.empty": "Añade una posición con precio en vivo para usar el Analizador de cambio.",
    "portfolio.switch.intro_caption": (
        "Una calculadora factual de coste de oportunidad, nunca una recomendación: vender una "
        "posición para comprar otra cuesta dinero real primero (impuesto sobre ganancias de "
        "capital + comisión en ambas operaciones) - esto muestra cuánto tendría que rendir de "
        "más la nueva posición cada año solo para compensar ese coste, según las propias "
        "estimaciones de valor razonable de este sitio."
    ),
    "portfolio.switch.settings_title": "Ajustes del Analizador de cambio",
    "portfolio.switch.settings_caption": (
        "Tu tasa de impuesto marginal y comisión fija por operación, solo para esta cartera. "
        "Se usan únicamente en los cálculos de esta pestaña - nunca se comparten ni se envían a la API/MCP."
    ),
    "portfolio.switch.settings_tax_rate": "Tasa de impuesto marginal (%)",
    "portfolio.switch.settings_brokerage": "Comisión por operación (A$)",
    "portfolio.switch.settings_save": "Guardar ajustes",
    "portfolio.switch.settings_saved": "Ajustes guardados.",
    "portfolio.switch.settings_missing": (
        "Introduce y guarda tu tasa de impuesto y comisión arriba antes de ejecutar un análisis "
        "- esta herramienta nunca los adivina."
    ),
    "portfolio.switch.pair_title": "Elige un par",
    "portfolio.switch.from_label": "Vender (una posición que estás considerando vender)",
    "portfolio.switch.to_label": "Comprar en su lugar (ticker candidato, formato Yahoo p. ej. CSL.AX)",
    "portfolio.switch.lookup_btn": "Buscar",
    "portfolio.switch.lookup_not_found": (
        "No se encontraron datos de precio para {ticker} - revisa el ticker (formato Yahoo, "
        "p. ej. CSL.AX para ASX, AAPL para Nasdaq)."
    ),
    "portfolio.switch.years_label": "Periodo a analizar (años)",
    "portfolio.switch.iv_unavailable": (
        "Aún no hay una estimación de valor razonable para {ticker}, así que la parte de "
        "rendimiento esperado de este análisis no se puede calcular - las cifras del peaje "
        "(abajo) siguen siendo válidas por sí solas."
    ),
    "portfolio.switch.source_hand_built": "\U0001F4DA investigación hecha a mano",
    "portfolio.switch.source_auto": "\U0001F916 estimación del modelo nocturno",
    "portfolio.switch.side_by_side_title": "Comparación",
    "portfolio.switch.col_position": "Posición",
    "portfolio.switch.col_price": "Precio",
    "portfolio.switch.col_fair_value": "Valor razonable",
    "portfolio.switch.col_source": "Fuente",
    "portfolio.switch.col_upside": "Potencial hasta el valor razonable",
    "portfolio.switch.col_expected_return": "Rendimiento implícito en {years} años",
    "portfolio.switch.bridge_title": "El peaje del cambio",
    "portfolio.switch.bridge_caption": (
        "Vender {ticker} genera esto antes de que un solo dólar pueda reinvertirse en la nueva posición."
    ),
    "portfolio.switch.bridge_value": "Valor de venta (V)",
    "portfolio.switch.bridge_cost_base": "Base de coste (C)",
    "portfolio.switch.bridge_gain": "Ganancia de capital",
    "portfolio.switch.bridge_discount_applied": "Se aplica el descuento de 12 meses (50% de la ganancia es imponible)",
    "portfolio.switch.bridge_discount_not_applied": "Sin descuento de 12 meses (mantenida menos de 12 meses, ganancia imponible completa)",
    "portfolio.switch.bridge_taxable_gain": "Ganancia imponible",
    "portfolio.switch.bridge_tax": "Impuesto ({rate}%)",
    "portfolio.switch.bridge_brokerage": "Comisión (venta + compra, {each} cada una)",
    "portfolio.switch.bridge_proceeds": "Producto neto tras el peaje (P)",
    "portfolio.switch.bridge_toll_pct": "El peaje es el {pct}% del valor de la posición",
    "portfolio.switch.bridge_formula_title": "Cómo se calcula el peaje anualizado (Z)",
    "portfolio.switch.bridge_formula": (
        "Z = (V ÷ P) ^ (1/N) − 1, donde N es el periodo de {years} años de arriba. "
        "Es el rendimiento anual constante que la nueva posición debe superar a la anterior, "
        "durante N años, solo para cerrar la brecha que abrió el peaje."
    ),
    "portfolio.switch.bridge_z_line": "Peaje anualizado (Z): {pct}%/año durante {years} años",
    "portfolio.switch.twelve_month_chip": (
        "Vender ahora renuncia al descuento de 12 meses: el impuesto sería A\\${tax_now} ahora "
        "frente a A\\${tax_later} dentro de {days} día(s) más (suponiendo el mismo valor de venta) "
        "- una diferencia de A\\${extra}."
    ),
    "portfolio.switch.verdict_title": "Veredicto",
    "portfolio.switch.verdict_passes": (
        "Según las propias estimaciones de valor razonable de este modelo, se estima que "
        "{to_ticker} supera a {from_ticker} en ≈{spread}%/año durante {years} años - superando "
        "el peaje de {toll}%/año por ≈{margin}%/año."
    ),
    "portfolio.switch.verdict_fails": (
        "Según las propias estimaciones de valor razonable de este modelo, se estima que "
        "{to_ticker} supera a {from_ticker} en ≈{spread}%/año durante {years} años - por debajo "
        "del peaje de {toll}%/año por ≈{margin}%/año."
    ),
    "portfolio.switch.verdict_no_iv": (
        "No hay suficientes datos de valor razonable en uno de los dos tickers (o en ambos) "
        "para comparar rendimientos esperados - solo se puede mostrar el peaje (arriba) para este par."
    ),
    "portfolio.switch.flip_title": "¿Cuándo cambiaría esto?",
    "portfolio.switch.flip_body": (
        "Manteniendo constante la ventaja de rendimiento estimada de hoy de ≈{spread}%/año, "
        "el cambio alcanzaría el punto de equilibrio en N≈{n} años (N* = ln(V÷P) ÷ ln(1+ventaja))."
    ),
    "portfolio.switch.flip_never": (
        "Con la ventaja de rendimiento estimada de hoy en cero o por debajo, un periodo de "
        "tenencia más largo por sí solo nunca hace que este cambio alcance el punto de "
        "equilibrio - la ventaja misma tendría que volverse positiva primero."
    ),
    "portfolio.switch.flags_title": "Cosas que vale la pena revisar antes de leer el veredicto",
    "portfolio.switch.flag_concentration": (
        "Comprar toda la posición haría que {ticker} fuera ≈{pct}% del valor de la cartera - "
        "por encima del nivel del {threshold}% que esta pestaña marca como concentrado."
    ),
    "portfolio.switch.flag_correlation": (
        "El historial de precios de {ticker} está muy correlacionado ({corr}) con el resto de "
        "la cartera - puede aportar menos diversificación de lo que parece."
    ),
    "portfolio.switch.flag_stale_a": "Los datos de {ticker} podrían estar desactualizados - {detail}.",
    "portfolio.switch.flag_stale_b": "Los datos de {ticker} podrían estar desactualizados - {detail}.",
    "portfolio.switch.flag_terminated": (
        "La investigación de {ticker} se terminó{reason} - consulta la página de Investigación "
        "antes de tratar su valor razonable como vigente."
    ),
    "portfolio.switch.trim_title": "O recorta en lugar de cambiar del todo",
    "portfolio.switch.trim_caption": (
        "Mueve solo parte de la posición de {from_ticker} hacia {to_ticker} en lugar de toda - "
        "la comisión es una tarifa fija en cualquier caso, así que un recorte pequeño paga "
        "proporcionalmente más de ella."
    ),
    "portfolio.switch.trim_slider": "Fracción de {ticker} a mover",
    "portfolio.switch.trim_toll_line": "Peaje de este recorte: A\\${toll} ({pct}% del monto recortado)",
    "portfolio.switch.trim_blended_return": "Rendimiento implícito combinado tras el recorte: ≈{pct}%/año",
    "portfolio.switch.trim_sim_title": "Rango simulado a 1 año tras el recorte",
    "portfolio.switch.trim_sim_unavailable": "Aún no hay suficiente historial de precios compartido para simular este recorte.",

    "research.kicker": "INVESTIGACIÓN RATIONAL COMPOUNDER",
    "research.not_ready": (
        "Los datos de investigación hecha a mano se están reconstruyendo "
        "- vuelve en breve."
    ),
    "research.shelf_title": "Investigación hecha a mano, una empresa a la vez",
    "research.shelf_positioning": (
        "Cada empresa de abajo tiene una valoración y una tesis completas, "
        "escritas a mano - no es un filtro ni una puntuación: análisis real, "
        "actualizado a medida que cambia el negocio subyacente."
    ),
    "research.housekeeping_snapshot": "Datos de: actual ({date})",
    "research.housekeeping_prioritise": (
        "¿Quieres que priorice una acción? Dilo con el botón de Comentarios "
        "de arriba."
    ),
    "research.rebuild_history_link": "Ver el historial de actualizaciones del modelo →",
    "research.card_sections_verdict": "{count}/{total} secciones · veredicto escrito ✓",
    "research.card_sections_no_verdict": "{count}/{total} secciones · investigación en curso",
    "research.card_updated": "Actualizado el {date}",
    "research.card_position_held": "En cartera",
    "research.card_position_none": "Sin posición",
    "research.card_cta": "Ver investigación →",
    "research.back_to_shelf": "← Todas las empresas",
    "research.state2_caption": "Libro de trabajo: {date}",
    "research.deep_dive_xlink": "Abrir el Deep Dive automático →",
    "research.verdict_label": "El veredicto del autor",
    "research.no_verdict_text": "Todavía no hay un veredicto escrito para esta empresa.",
    "research.key_number_price": "Precio",
    "research.key_number_margin_of_safety": "Margen de seguridad",
    "research.key_number_roic": "ROIC",
    "research.key_number_roic_vs_wacc": "ROIC vs WACC",
    # Mega-batch Part 12
    "research.terminated_badge": "⛔ Investigación terminada",
    "research.terminated_fallback_reason": (
        "Investigación detenida — ver las secciones para lo que se completó."
    ),
}


def t(key, lang="en", **fmt):
    """EN-fallback lookup: an ES translation is used only when lang=="es"
    AND that key actually has one; every other case (lang=="en", or an
    ES-incomplete key) returns the EN value unchanged - the mechanism
    that keeps the English site byte-identical while Spanish coverage
    fills in incrementally. Missing from EN entirely -> the key itself,
    so a typo shows up as visible mojibake instead of a silent blank."""
    _s = None
    if lang == "es":
        _s = ES.get(key)
    if _s is None:
        _s = EN.get(key, key)
    return _s.format(**fmt) if fmt else _s


# -----------------------------------------------------------------
# Español completion, Part 2: month-name localization for the "%d %b %Y"
# date stamps the notification emails build (digest_engine.py,
# weekly_brief_engine.py, portfolio_watchdog_engine.py, announce_engine.py
# - the last of these previously carried this exact gap as a documented,
# accepted limitation, see the "email.announce.*" comment above). A small
# manual map rather than Python's own locale machinery, which would need a
# Spanish locale installed on the Railway server - a real deployment
# dependency the instruction's own "no server locale dependency" note
# rules out.
# -----------------------------------------------------------------

_MONTH_ABBREV_ES = {
    "Jan": "ene", "Feb": "feb", "Mar": "mar", "Apr": "abr",
    "May": "may", "Jun": "jun", "Jul": "jul", "Aug": "ago",
    "Sep": "sep", "Oct": "oct", "Nov": "nov", "Dec": "dic",
}


def format_date_dmy(dt, lang="en"):
    """"%d %b %Y" (e.g. "05 Sep 2026") with the month abbreviation
    translated when lang=="es" (e.g. "05 sep 2026") - day and year are
    plain digits, unaffected by language. lang="en" (the default, and any
    other/unknown lang) renders EXACTLY as dt.strftime("%d %b %Y") always
    has, so every existing English caller is byte-identical."""
    day = dt.strftime("%d")
    year = dt.strftime("%Y")
    mon_en = dt.strftime("%b")
    mon = _MONTH_ABBREV_ES.get(mon_en, mon_en) if lang == "es" else mon_en
    return f"{day} {mon} {year}"


# Español completion, Part 3: calendar_render.py's day-group labels use
# "%a %d %b" (weekday + month, no year) - a pattern format_date_dmy above
# doesn't cover. Same "small manual map, no server locale dependency"
# approach, kept as a separate map/function rather than widening
# format_date_dmy's own signature, so every existing caller of that
# function (the notification emails) is untouched.
_WEEKDAY_ABBREV_ES = {
    "Mon": "lun", "Tue": "mar", "Wed": "mié", "Thu": "jue",
    "Fri": "vie", "Sat": "sáb", "Sun": "dom",
}


def format_date_a_d_b(dt, lang="en"):
    """"%a %d %b" (e.g. "Mon 05 Sep") with the weekday AND month
    abbreviations translated when lang=="es" (e.g. "lun 05 sep") - the day
    number is a plain digit either way. lang="en" (the default, and any
    other/unknown lang) renders EXACTLY as dt.strftime("%a %d %b") always
    has, so the existing English /calendar page (and the Streamlit results
    calendar, which shares this same helper) stays byte-identical."""
    wd_en = dt.strftime("%a")
    day = dt.strftime("%d")
    mon_en = dt.strftime("%b")
    if lang == "es":
        wd = _WEEKDAY_ABBREV_ES.get(wd_en, wd_en)
        mon = _MONTH_ABBREV_ES.get(mon_en, mon_en)
    else:
        wd, mon = wd_en, mon_en
    return f"{wd} {day} {mon}"


def format_weekday_abbrev(dt, lang="en"):
    """Bare "%a" weekday abbreviation (e.g. "Tue"/"mar"), translated for
    lang=="es" via the same _WEEKDAY_ABBREV_ES map format_date_a_d_b uses.
    Added for the home page's per-row "as of {day}" stale-snapshot note
    (top5_au_us instruction), which needs just the weekday, not the full
    "%a %d %b" day-group label. lang="en" (the default) is exactly
    dt.strftime("%a")."""
    wd_en = dt.strftime("%a")
    return _WEEKDAY_ABBREV_ES.get(wd_en, wd_en) if lang == "es" else wd_en


# -----------------------------------------------------------------
# Standing disclaimers - ES translations only (see module docstring for
# why the EN text isn't duplicated up here). Translated carefully, term
# for term, against the live EN copy at blog_render.DISCLAIMER and
# app.py's _render_footer() - not paraphrased - per the instruction's
# explicit "translate the standing disclaimer carefully" note.
# -----------------------------------------------------------------

DISCLAIMER_ES = (
    "<b>Solo información objetiva y comentario general.</b> "
    "StocksDeepDive publica datos, resultados de modelos y cálculos "
    "descritos a partir de los datos ingresados. Nada en este sitio tiene "
    "en cuenta tus objetivos personales, tu situación financiera o tus "
    "necesidades, y nada aquí constituye asesoramiento sobre productos "
    "financieros ni una recomendación para comprar, mantener o vender "
    "ningún valor. Los resultados de los modelos dependen enteramente "
    "de los datos y supuestos ingresados. Considera buscar el "
    "asesoramiento de un asesor con licencia antes de actuar. Datos "
    "proporcionados por Yahoo Finance, Google Trends, StockTwits y "
    "NewsAPI; las cifras pueden estar retrasadas o ser revisadas."
)

FOOTER_DISCLAIMER_FACTUAL_ES = (
    "<b>Solo información objetiva y resultados de la "
    "calculadora.</b> StocksDeepDive calcula y muestra datos, resultados "
    "de modelos y cálculos descritos a partir de los datos "
    "ingresados. No ofrece asesoramiento sobre productos financieros, "
    "recomendaciones ni opiniones sobre comprar, mantener o vender "
    "ningún valor, y nada en este sitio debe interpretarse como tal. "
    "Los resultados de los modelos dependen enteramente de los datos y "
    "supuestos ingresados, que puedes revisar &mdash; y en algunos casos "
    "modificar &mdash; tú mismo. Los valores mostrados en rojo se "
    "basan en datos predeterminados o estimados. Datos proporcionados por "
    "Yahoo Finance, Google Trends, StockTwits y NewsAPI; las cifras "
    "pueden estar retrasadas o ser revisadas."
)

FOOTER_DISCLAIMER_GENERAL_ES = (
    "<b>Solo información general.</b> StocksDeepDive ofrece "
    "información objetiva y comentario general generado a partir de "
    "datos disponibles públicamente. No tiene en cuenta tus objetivos "
    "personales, tu situación financiera o tus necesidades, y no "
    "constituye asesoramiento financiero. Los puntajes, señales, "
    "zonas de entrada y precios objetivo son resultados de modelos, no "
    "recomendaciones. Considera buscar el asesoramiento de un asesor con "
    "licencia antes de actuar. Datos proporcionados por Yahoo Finance, "
    "Google Trends, StockTwits y NewsAPI; las cifras pueden estar "
    "retrasadas o ser estimadas &mdash; los valores estimados se "
    "muestran en rojo en todo el sitio."
)
