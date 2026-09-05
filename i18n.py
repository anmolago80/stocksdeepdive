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
    "nav.research": "Rational Compounder Analysis",
    "nav.comparison": "Side-by-side Comparison",
    "nav.scanner": "Stock Scanner",
    "nav.calendar": "Results Calendar",
    "nav.portfolio": "My Portfolio",

    "header.tagline": "Research any stock in seconds.",
    "header.search_placeholder": (
        "Input your stock ticker (e.g. CSL.AX, or CSL.AX BHP.AX to compare)"
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
    # recipient via email_auth.get_signup_lang(). The date stamp itself
    # (day/month-abbrev/year) is NOT localized - Python's strftime month
    # abbreviations need a Spanish locale installed on the server, which
    # this pass doesn't add; a Spanish reader gets e.g. "05 Sep 2026"
    # rather than "05 sep 2026", a cosmetic gap noted in the report.
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
}

ES = {
    "nav.research": "Análisis Rational Compounder",
    "nav.comparison": "Comparación en paralelo",
    "nav.scanner": "Buscador de acciones",
    "nav.calendar": "Calendario de resultados",
    "nav.portfolio": "Mi cartera",

    "header.tagline": "Analiza cualquier acción en segundos.",
    "header.search_placeholder": (
        "Ingresa el ticker de la acción (p. ej., CSL.AX, o CSL.AX "
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
