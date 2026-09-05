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
