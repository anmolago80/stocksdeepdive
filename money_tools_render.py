"""
money_tools_render.py

SEO Commit B (18 Sep 2026, mocks/tool_landing_seo_mock.html): server-
rendered landing pages for the free Money Tools - a /tools index (card
grid) and one page per tool at /tools/<slug> - reusing blog_render's
page shell exactly like snapshot_render.py already does for the /s/*
ticker pages (that module's own docstring: "so a snapshot page looks
and reads like the rest of the site"). These are NEW pages beside the
live app, not a replacement for it - the live Money Tools page
(app.py's page_tools(), reached via /tools?tool=<id> or /tools?app=1)
is completely untouched, and every page here has one big CTA button
into it. Deliberately a separate module from blog_render.py's own
TOOL_PAGES/render_tool_landing() (which already serves /deep-dive,
/comparison, /scanner, /research) rather than an extra entry in that
dict, for two reasons: (1) TOOL_PAGES is English-only static markdown
and these pages need real EN+ES copy, and (2) these pages' worked
examples are computed live from the real engines below, which
TOOL_PAGES' pure-markdown shape has no room for.

WORKED-EXAMPLE NUMBERS are computed HERE, at render time, by calling
the SAME protected *_engine.py modules the live tool itself calls -
never hardcoded, never guessed. Every fixed illustrative input below
was chosen and its output independently verified against the real,
unmodified engine before being written down (Dow-30 rule - see this
Part's own report for the verification transcript); nothing in any
*_engine.py file is changed by this module, every call is read-only,
exactly like every other caller of these engines.

EN + ES throughout, per the Español instruction's own architecture:
i18n.py's t()/dict pattern is for short UI chrome strings shared across
many pages; these are four long-form, page-specific landing pages, so -
the same choice blog_render.TOOL_PAGES's own author already made for
/deep-dive etc, just extended to Spanish here - the copy lives right
next to its template as one {"en": ..., "es": ...} pair per field
rather than scattered across i18n.py.
"""

import html

import blog_render
from blog_render import _head, _page, _json_ld, _person_json_ld, _organization_json_ld, SITE_NAME

import debt_recycling_engine
import super_engine
import budget_planner_engine
import property_vs_index_engine

e = html.escape


def _aud(n):
    """Whole-dollar AUD, comma-grouped - the same plain "$90,600" style
    the approved mock uses throughout, no cents (these are 10-30 year
    illustrative projections, not a bank balance)."""
    return f"${n:,.0f}"


# --------------------------------------------------------------------------- #
# Slugs <-> the live app's own TOOLS_REGISTRY ids (app.py). Deliberately
# NOT the same strings - these are SEO-friendly URL slugs, the registry
# ids are the live app's own internal ?tool= values - _tool_cta_url()
# below is the one place that bridges them, so a mismatch anywhere can
# only ever be a one-line fix.
# --------------------------------------------------------------------------- #
MONEY_TOOL_SLUGS = ["debt-recycling", "super", "budget", "property-vs-shares"]

_REGISTRY_ID = {
    "debt-recycling": "debt_recycling",
    "super": "super",
    "budget": "budget_planner",
    "property-vs-shares": "property_vs_index",
}


def _tool_cta_url(slug, lang):
    """/tools?tool=<registry id> - already resolved correctly by the live
    app's page_tools() with zero app.py changes needed (it reads
    st.query_params.get("tool") directly - verified against the current
    app.py before writing this module). The only gap this SEO commit
    actually closes server-side is server.py's own _STREAMLIT_ONLY_PARAMS
    not including "tool" yet, so this exact URL used to fall through to
    the curated /tools index page instead of forcing straight through to
    Streamlit - see server.py's own comment at that set."""
    qs = f"tool={_REGISTRY_ID[slug]}"
    if lang == "es":
        qs += "&lang=es"
    return f"/tools?{qs}"


# --------------------------------------------------------------------------- #
# Worked examples - fixed illustrative inputs, real engines, verified
# output (Dow-30 rule). Each compute_* function takes no arguments on
# purpose: a fixed, reproducible example, not a function of the request.
# --------------------------------------------------------------------------- #

def _compute_debt_recycling_example():
    """$150,000 cash, 10 years, 6.04% mortgage, 6.34% recycling-loan
    rate, 37% marginal rate, both investment legs fully franked AU
    shares (Investment 1 income 14%/growth 10%, Investment 2 income
    4%/growth 10%) - verified against debt_recycling_engine.run_scenarios
    to produce exactly 90,600 / 383,835 / 414,522 / 572,757 for
    Scenarios A/B/C/D, matching the approved mock's own preview numbers
    to the dollar."""
    res = debt_recycling_engine.run_scenarios(
        cash=150000.0, mortgage_rate=0.0604, loan_rate=0.0634, tax_rate=0.37,
        horizon=10,
        inv1_income_rate=0.14, inv1_growth_rate=0.10,
        inv2_income_rate=0.04, inv2_growth_rate=0.10,
        country="au", inv1_franked_pct=1.0, inv2_franked_pct=1.0,
        ltcg_rate=None, us_baseline="mortgage_extra", hys_rate=None,
    )
    return {
        "caption": {
            "en": "EXAMPLE — $150,000 cash · 10 years · 6.04% mortgage · 37% marginal rate:",
            "es": "EJEMPLO — $150.000 en efectivo · 10 años · hipoteca 6,04% · tasa marginal 37%:",
        },
        "rows": [
            {"label": {"en": "Leave it in the offset (guaranteed)",
                       "es": "Dejarlo en la cuenta offset (garantizado)"},
             "value": _aud(res["A"]["headline"]), "cls": ""},
            {"label": {"en": "Invest the cash directly", "es": "Invertir el efectivo directamente"},
             "value": _aud(res["B"]["headline"]), "cls": ""},
            {"label": {"en": "Debt recycle", "es": "Reciclar deuda"},
             "value": _aud(res["C"]["headline"]), "cls": "g"},
            {"label": {"en": "Borrow AND invest the cash too",
                       "es": "Pedir prestado Y también invertir el efectivo"},
             "value": _aud(res["D"]["headline"]), "cls": "o"},
        ],
    }


def _compute_property_example():
    """Module defaults ($300k cash, $700k loan, 6.34% loan rate, $650/wk
    rent, 5% property growth, 9% S&P 500 total return, 37% marginal
    rate, 10 years) - the SAME defaults property_vs_index_engine.run()
    ships with, already the source of the existing hub-card teaser's
    "+$323k property vs +$300k index" figures (tools.hub.
    property_vs_index_teaser in i18n.py) - reused here rather than a
    second illustrative pair, so the two never drift apart."""
    res = property_vs_index_engine.run()
    return {
        "caption": {
            "en": "EXAMPLE — $300,000 cash + $700,000 loan · 10 years · 37% marginal rate:",
            "es": "EJEMPLO — $300.000 en efectivo + $700.000 de préstamo · 10 años · tasa marginal 37%:",
        },
        "rows": [
            {"label": {"en": "Investment property (after tax)", "es": "Propiedad de inversión (después de impuestos)"},
             "value": _aud(res["property"]["headline"]), "cls": "g"},
            {"label": {"en": "Same cash in the S&P 500 (after tax)",
                       "es": "El mismo efectivo en el S&P 500 (después de impuestos)"},
             "value": _aud(res["index"]["headline"]), "cls": ""},
        ],
    }


def _compute_budget_example():
    """The engine's own module-docstring worked example, reused verbatim
    rather than a second one: $9,000/mo in, $6,800/mo out -> $2,200/mo
    ($26,400/yr) spare, invested 10 years at the S&P 500's 10%/yr
    long-run historical average -> future_value_of_savings() = $420,748,
    against $264,000 if the same amount were simply saved with no
    growth at all (deposits_only())."""
    yearly = 26400.0
    invested = budget_planner_engine.future_value_of_savings(
        yearly, budget_planner_engine.INDEX_HISTORICAL_RETURNS["sp500"], 10)
    saved = budget_planner_engine.deposits_only(yearly, 10)
    return {
        "caption": {
            "en": "EXAMPLE — $9,000/mo in, $6,800/mo out → $2,200/mo ($26,400/yr) spare, over 10 years:",
            "es": "EJEMPLO — $9.000/mes de ingreso, $6.800/mes de gasto → $2.200/mes ($26.400/año) libres, en 10 años:",
        },
        "rows": [
            {"label": {"en": "Just saved (no growth)", "es": "Solo ahorrado (sin crecimiento)"},
             "value": _aud(saved), "cls": ""},
            {"label": {"en": "Invested at the S&P 500's 10%/yr historical average",
                       "es": "Invertido al promedio histórico del S&P 500 (10%/año)"},
             "value": _aud(invested), "cls": "g"},
        ],
    }


def _compute_super_example():
    """Age 30 → retirement 60 (30 years), $50,000 starting balance,
    $90,000 salary (SG-only baseline) vs the same path with an extra
    $200/mo salary-sacrifice, both at a 7%/yr return - a fixed
    illustrative pair, not a forecast, same "nominal dollars, nothing
    inflation-adjusted" framing project_super() itself documents."""
    proj = super_engine.project_super(
        age=30, retirement_age=60, balance=50000.0, salary=90000.0,
        extra_sacrifice_monthly=200.0, return_rate_annual=0.07,
    )
    return {
        "caption": {
            "en": "EXAMPLE — age 30 → 60, $50,000 starting balance, $90,000 salary, 7%/yr return:",
            "es": "EJEMPLO — edad 30 → 60, saldo inicial $50.000, salario $90.000, retorno 7%/año:",
        },
        "rows": [
            {"label": {"en": "Super Guarantee only (no extra sacrifice)",
                       "es": "Solo el Super Guarantee obligatorio (sin sacrificio extra)"},
             "value": _aud(proj["end_balance_baseline"]), "cls": ""},
            {"label": {"en": "+ $200/mo extra salary sacrifice",
                       "es": "+ $200/mes de sacrificio salarial extra"},
             "value": _aud(proj["end_balance_with_sacrifice"]), "cls": "g"},
        ],
        "extra_line": {
            "en": f"That extra $200/mo is {_aud(proj['delta'])} more in the fund by 60.",
            "es": f"Ese sacrificio extra de $200/mes suma {_aud(proj['delta'])} más en el fondo a los 60.",
        },
    }


_WORKED_EXAMPLES = {
    "debt-recycling": _compute_debt_recycling_example,
    "super": _compute_super_example,
    "budget": _compute_budget_example,
    "property-vs-shares": _compute_property_example,
}


# --------------------------------------------------------------------------- #
# Per-tool copy - breadcrumb/H1/lead/three feature cards/FAQ. Written
# around the real search phrasings a visitor for each tool would type,
# per the task's own instruction; every numeric claim on the page
# carries the same "described calculations, not advice" framing every
# other page on this site uses (see _FOOT_NOTE below).
# --------------------------------------------------------------------------- #
MONEY_TOOL_PAGES = {
    "debt-recycling": {
        "icon": "\U0001F4B3",
        "title": {"en": "Debt Recycling Calculator - offset vs invest vs recycle | StocksDeepDive",
                   "es": "Calculadora de Reciclaje de Deuda - offset vs invertir vs reciclar | StocksDeepDive"},
        "h1": {"en": "Debt Recycling Calculator", "es": "Calculadora de Reciclaje de Deuda"},
        "meta_description": {
            "en": ("Compare leaving cash in the mortgage offset, investing it directly, "
                   "or debt recycling - four strategies after tax, loan interest, franking "
                   "credits and the CGT discount all shown line by line."),
            "es": ("Compara dejar el efectivo en la cuenta offset, invertirlo directamente, "
                   "o reciclar deuda - cuatro estrategias después de impuestos, con el interés "
                   "del préstamo, los créditos de franquicia y el descuento de CGT detallados."),
        },
        "lead": {
            "en": ("Should your spare cash sit in the mortgage offset, be invested directly, "
                   "or be debt-recycled? This calculator compares all four strategies after "
                   "tax over your horizon - with the loan interest, franking credits and the "
                   "50% CGT discount all shown line by line, so you can see exactly how each "
                   "figure is built."),
            "es": ("¿Tu efectivo disponible debería quedarse en la cuenta offset de la "
                   "hipoteca, invertirse directamente, o reciclarse como deuda? Esta "
                   "calculadora compara las cuatro estrategias después de impuestos a lo "
                   "largo de tu horizonte - con el interés del préstamo, los créditos de "
                   "franquicia y el descuento de CGT del 50% detallados línea por línea, para "
                   "que veas exactamente cómo se construye cada cifra."),
        },
        "features": [
            {"h": {"en": "After-tax, line by line", "es": "Después de impuestos, línea por línea"},
             "p": {"en": ("Every card shows its working - income, growth after CGT, interest "
                          "after deduction. Nothing hides in a black box."),
                   "es": ("Cada tarjeta muestra su cálculo - ingreso, crecimiento después de "
                          "CGT, interés después de la deducción. Nada se esconde en una caja "
                          "negra.")}},
            {"h": {"en": "Your marginal rate", "es": "Tu tasa marginal"},
             "p": {"en": ("Franked dividends, negative gearing and the CGT discount computed "
                          "at the tax rate you enter."),
                   "es": ("Dividendos con crédito de franquicia, negative gearing y el "
                          "descuento de CGT calculados a la tasa impositiva que ingreses.")}},
            {"h": {"en": "Save your scenarios", "es": "Guarda tus escenarios"},
             "p": {"en": ("Name and save input sets - compare \"conservative\" vs "
                          "\"aggressive\" side by side any time."),
                   "es": ("Nombra y guarda conjuntos de datos - compara \"conservador\" vs "
                          "\"agresivo\" en paralelo cuando quieras.")}},
        ],
        "faq": [
            {"q": {"en": "Is debt recycling worth it?",
                   "es": "¿Vale la pena el reciclaje de deuda?"},
             "a": {"en": ("It depends on your loan rate, marginal tax rate and what the "
                          "investment actually returns - the calculator shows the exact "
                          "break-even so you can see where the answer flips for your own "
                          "numbers."),
                   "es": ("Depende de la tasa de tu préstamo, tu tasa marginal de impuestos y "
                          "lo que realmente rinda la inversión - la calculadora muestra el "
                          "punto de equilibrio exacto para que veas dónde cambia la respuesta "
                          "con tus propios números.")}},
            {"q": {"en": "What's the difference between debt recycling and just investing?",
                   "es": "¿Cuál es la diferencia entre reciclar deuda y simplemente invertir?"},
             "a": {"en": ("Investing directly uses cash you already have. Debt recycling "
                          "keeps that cash in the mortgage offset (reducing non-deductible "
                          "interest) and instead borrows a separate, tax-deductible amount to "
                          "invest - the calculator runs both, plus a third option that "
                          "combines them, so the comparison is apples to apples."),
                   "es": ("Invertir directamente usa efectivo que ya tienes. El reciclaje de "
                          "deuda mantiene ese efectivo en la cuenta offset (reduciendo interés "
                          "no deducible) y en cambio pide prestado un monto separado, "
                          "deducible de impuestos, para invertir - la calculadora ejecuta "
                          "ambas opciones, más una tercera que las combina, para que la "
                          "comparación sea justa.")}},
            {"q": {"en": "Does this work for US investors too?",
                   "es": "¿Funciona también para inversores en EE. UU.?"},
             "a": {"en": ("Yes - switch the country toggle and the calculator swaps franking "
                          "credits for the US's own long-term capital gains rate and lets you "
                          "compare against a high-yield savings account instead of a mortgage "
                          "offset."),
                   "es": ("Sí - cambia el selector de país y la calculadora reemplaza los "
                          "créditos de franquicia por la tasa de ganancias de capital a largo "
                          "plazo de EE. UU., y te permite comparar contra una cuenta de "
                          "ahorros de alto rendimiento en lugar de una cuenta offset.")}},
        ],
    },
    "super": {
        "icon": "\U0001F998",
        "title": {"en": "Super & Retirement Projector - salary sacrifice calculator | StocksDeepDive",
                   "es": "Proyector de Super y Jubilación - calculadora de sacrificio salarial | StocksDeepDive"},
        "h1": {"en": "Super & Retirement Projector", "es": "Proyector de Super y Jubilación"},
        "meta_description": {
            "en": ("Project your Australian super balance at retirement, with and without "
                   "extra salary sacrifice - contributions tax, the concessional cap and "
                   "Division 293 all checked."),
            "es": ("Proyecta tu saldo de super australiano al jubilarte, con y sin sacrificio "
                   "salarial extra - impuesto a las contribuciones, el tope concesional y la "
                   "División 293 verificados."),
        },
        "lead": {
            "en": ("Where does your super actually land by retirement - and how much "
                   "difference does an extra $100 or $200 a month of salary sacrifice really "
                   "make? This projector compounds your balance year by year to your chosen "
                   "retirement age, checks the concessional contributions cap and the "
                   "Division 293 threshold, and shows the baseline against the "
                   "with-sacrifice path side by side."),
            "es": ("¿Dónde termina realmente tu super al jubilarte, y cuánta diferencia hace "
                   "de verdad un sacrificio salarial extra de $100 o $200 al mes? Este "
                   "proyector compone tu saldo año por año hasta la edad de jubilación que "
                   "elijas, verifica el tope de contribuciones concesionales y el umbral de "
                   "la División 293, y muestra la trayectoria base junto a la trayectoria con "
                   "sacrificio."),
        },
        "features": [
            {"h": {"en": "Year by year, not a single guess",
                   "es": "Año por año, no una sola estimación"},
             "p": {"en": ("Compounds your opening balance, the Super Guarantee and any extra "
                          "sacrifice one year at a time to your chosen retirement age."),
                   "es": ("Compone tu saldo inicial, el Super Guarantee y cualquier "
                          "sacrificio extra, año a año, hasta la edad de jubilación que "
                          "elijas.")}},
            {"h": {"en": "Caps checked automatically", "es": "Topes verificados automáticamente"},
             "p": {"en": ("Flags it the moment SG plus your extra sacrifice would exceed the "
                          "concessional contributions cap, or cross the Division 293 income "
                          "threshold."),
                   "es": ("Te avisa en el momento en que el SG más tu sacrificio extra "
                          "superarían el tope de contribuciones concesionales, o crucen el "
                          "umbral de ingresos de la División 293.")}},
            {"h": {"en": "Nominal dollars, clearly labelled",
                   "es": "Dólares nominales, claramente etiquetados"},
             "p": {"en": ("No hidden inflation adjustment - every figure is labelled nominal, "
                          "so you know exactly what assumption you're looking at."),
                   "es": ("Sin ajuste de inflación oculto - cada cifra está etiquetada como "
                          "nominal, para que sepas exactamente qué supuesto estás viendo.")}},
        ],
        "faq": [
            {"q": {"en": "Is salary sacrifice worth it?",
                   "es": "¿Vale la pena el sacrificio salarial?"},
             "a": {"en": ("It depends on your marginal tax rate versus the 15% contributions "
                          "tax, your time to retirement, and the assumed return - the "
                          "projector shows the actual dollar gap for your own numbers rather "
                          "than a generic rule of thumb."),
                   "es": ("Depende de tu tasa marginal de impuestos frente al 15% de impuesto "
                          "a las contribuciones, tu tiempo hasta la jubilación y el retorno "
                          "supuesto - el proyector muestra la brecha real en dólares para tus "
                          "propios números, en lugar de una regla general.")}},
            {"q": {"en": "What's the concessional contributions cap right now?",
                   "es": "¿Cuál es el tope de contribuciones concesionales actualmente?"},
             "a": {"en": ("The calculator always uses the current ATO figure and checks your "
                          "SG plus extra sacrifice against it live - see the cap warning right "
                          "under your inputs."),
                   "es": ("La calculadora siempre usa la cifra actual de la ATO y verifica tu "
                          "SG más el sacrificio extra en tiempo real - mira la advertencia de "
                          "tope justo debajo de tus datos.")}},
        ],
    },
    "budget": {
        "icon": "\U0001F4D2",
        "title": {"en": "Budget Planner - see your spare cash invested | StocksDeepDive",
                   "es": "Planificador de Presupuesto - ve tu efectivo libre invertido | StocksDeepDive"},
        "h1": {"en": "Budget Planner", "es": "Planificador de Presupuesto"},
        "meta_description": {
            "en": ("Type a family budget in a minute and see the monthly and yearly spare "
                   "cash it leaves - then what that surplus would become if invested instead "
                   "of just saved."),
            "es": ("Ingresa un presupuesto familiar en un minuto y ve el efectivo libre "
                   "mensual y anual que deja - luego qué se convertiría ese excedente si se "
                   "invirtiera en vez de solo ahorrarse."),
        },
        "lead": {
            "en": ("Ten plain categories - mortgage or rent, transport, food, the rest - and "
                   "one minute is enough to see your monthly and yearly surplus. The real "
                   "point isn't the budget itself: it's what that surplus becomes if it's "
                   "invested instead of left sitting there, shown against simply saving the "
                   "same amount with no growth at all."),
            "es": ("Diez categorías simples - hipoteca o alquiler, transporte, comida, el "
                   "resto - y un minuto basta para ver tu excedente mensual y anual. Lo "
                   "importante no es el presupuesto en sí: es en qué se convierte ese "
                   "excedente si se invierte en vez de quedarse quieto, comparado con "
                   "simplemente ahorrar la misma cantidad sin ningún crecimiento."),
        },
        "features": [
            {"h": {"en": "Ten categories, nothing forced",
                   "es": "Diez categorías, nada forzado"},
             "p": {"en": ("Skip whatever doesn't apply - an empty category is left out of "
                          "your spending, never silently counted as zero."),
                   "es": ("Omite lo que no aplique - una categoría vacía se excluye de tu "
                          "gasto, nunca se cuenta silenciosamente como cero.")}},
            {"h": {"en": "Invested vs just saved, side by side",
                   "es": "Invertido vs solo ahorrado, en paralelo"},
             "p": {"en": ("The same yearly surplus compared with zero growth against the "
                          "S&P 500 and ASX 200's own long-run historical averages."),
                   "es": ("El mismo excedente anual comparado sin crecimiento frente a los "
                          "promedios históricos de largo plazo del S&P 500 y el ASX 200.")}},
            {"h": {"en": "What each category costs you long-term",
                   "es": "Lo que cada categoría te cuesta a largo plazo"},
             "p": {"en": ("Every filled category shows its own \"invested over N years\" "
                          "figure, using the same formula as the headline numbers."),
                   "es": ("Cada categoría completada muestra su propia cifra de \"invertido "
                          "en N años\", usando la misma fórmula que las cifras principales.")}},
        ],
        "faq": [
            {"q": {"en": "How is the invested figure calculated?",
                   "es": "¿Cómo se calcula la cifra invertida?"},
             "a": {"en": ("Your whole year's savings is treated as one deposit made at each "
                          "year-end, compounded at the rate you choose for the remaining "
                          "years - a standard annuity future-value calculation, shown so you "
                          "can check it by hand."),
                   "es": ("El ahorro de todo el año se trata como un solo depósito realizado "
                          "al final de cada año, compuesto a la tasa que elijas por los años "
                          "restantes - un cálculo estándar de valor futuro de anualidad, "
                          "mostrado para que puedas verificarlo a mano.")}},
            {"q": {"en": "What return rate does it use?",
                   "es": "¿Qué tasa de retorno usa?"},
             "a": {"en": ("Whichever you pick - the S&P 500 and ASX 200's own long-run "
                          "nominal historical averages are offered as a starting point, "
                          "clearly labelled as history, never a forecast."),
                   "es": ("La que elijas - se ofrecen como punto de partida los promedios "
                          "históricos nominales de largo plazo del S&P 500 y el ASX 200, "
                          "claramente etiquetados como historia, nunca como pronóstico.")}},
        ],
    },
    "property-vs-shares": {
        "icon": "\U0001F3E0",
        "title": {"en": "Property vs S&P 500 Calculator - after-tax comparison | StocksDeepDive",
                   "es": "Calculadora Propiedad vs S&P 500 - comparación después de impuestos | StocksDeepDive"},
        "h1": {"en": "Property vs S&P 500 Calculator", "es": "Calculadora Propiedad vs S&P 500"},
        "meta_description": {
            "en": ("Buy a leveraged investment property, or put the same cash in the S&P "
                   "500? Both compared after tax over your horizon, plus the break-even "
                   "weekly rent."),
            "es": ("¿Comprar una propiedad de inversión apalancada, o poner el mismo efectivo "
                   "en el S&P 500? Ambas comparadas después de impuestos, más el alquiler "
                   "semanal de equilibrio."),
        },
        "lead": {
            "en": ("A leveraged investment property against simply indexing the deposit - "
                   "capital growth after CGT, rent after tax, loan interest after deduction "
                   "and holding costs on one side; index growth after CGT and dividends "
                   "after tax on the other. Both sides after tax, over the years you choose, "
                   "plus the break-even weekly rent the property needs to cover its own cash "
                   "costs."),
            "es": ("Una propiedad de inversión apalancada frente a simplemente indexar el "
                   "depósito - crecimiento de capital después de CGT, alquiler después de "
                   "impuestos, interés del préstamo después de la deducción y costos de "
                   "mantención de un lado; crecimiento del índice después de CGT y "
                   "dividendos después de impuestos del otro. Ambos lados después de "
                   "impuestos, en los años que elijas, más el alquiler semanal de equilibrio "
                   "que la propiedad necesita para cubrir sus propios costos en efectivo."),
        },
        "features": [
            {"h": {"en": "Four lines vs two, all after tax",
                   "es": "Cuatro líneas vs dos, todas después de impuestos"},
             "p": {"en": ("The property side breaks into growth, rent, interest and holding "
                          "costs; the index side into growth and dividends - every line "
                          "visible, summing exactly to the headline."),
                   "es": ("El lado de la propiedad se divide en crecimiento, alquiler, "
                          "interés y costos de mantención; el lado del índice en crecimiento "
                          "y dividendos - cada línea visible, sumando exactamente el total.")}},
            {"h": {"en": "Break-even rent, worked out for you",
                   "es": "Alquiler de equilibrio, calculado para ti"},
             "p": {"en": ("The weekly rent that exactly covers the loan interest and holding "
                          "costs, plus how far your own rent sits above or below it."),
                   "es": ("El alquiler semanal que cubre exactamente el interés del préstamo "
                          "y los costos de mantención, más cuánto se ubica tu propio alquiler "
                          "por encima o por debajo.")}},
            {"h": {"en": "The crossover year, on a chart",
                   "es": "El año de cruce, en un gráfico"},
             "p": {"en": ("A year-by-year chart shows exactly when (if ever) one side "
                          "overtakes the other on your own numbers."),
                   "es": ("Un gráfico año por año muestra exactamente cuándo (si ocurre) un "
                          "lado supera al otro con tus propios números.")}},
        ],
        "faq": [
            {"q": {"en": "Is property or the S&P 500 the better investment?",
                   "es": "¿Es mejor inversión la propiedad o el S&P 500?"},
             "a": {"en": ("It depends entirely on the growth rate, rent, loan rate and tax "
                          "assumptions you enter - the calculator runs both after tax on "
                          "identical terms so you can see where your own numbers land, not a "
                          "generic answer."),
                   "es": ("Depende por completo de la tasa de crecimiento, el alquiler, la "
                          "tasa del préstamo y los supuestos de impuestos que ingreses - la "
                          "calculadora ejecuta ambas después de impuestos en condiciones "
                          "idénticas para que veas dónde caen tus propios números, no una "
                          "respuesta genérica.")}},
            {"q": {"en": "What break-even rent do I actually need?",
                   "es": "¿Qué alquiler de equilibrio necesito realmente?"},
             "a": {"en": ("The calculator computes it directly from your loan amount, loan "
                          "rate, holding costs and vacancy assumption, and shows exactly how "
                          "far above or below it your entered rent sits, after tax."),
                   "es": ("La calculadora lo calcula directamente a partir de tu monto de "
                          "préstamo, tasa del préstamo, costos de mantención y supuesto de "
                          "vacancia, y muestra exactamente cuánto por encima o por debajo se "
                          "ubica tu alquiler ingresado, después de impuestos.")}},
        ],
    },
}


_FOOT_NOTE = {
    "en": "Described calculations from your inputs - not tax or financial advice.",
    "es": "Cálculos descritos a partir de tus datos - no es asesoramiento fiscal ni financiero.",
}

_BREADCRUMB_HOME = {"en": "Money Tools", "es": "Herramientas de dinero"}

_INDEX_TITLE = {
    "en": "Money Tools - free after-tax calculators | StocksDeepDive",
    "es": "Herramientas de dinero - calculadoras gratuitas después de impuestos | StocksDeepDive",
}
_INDEX_H1 = {"en": "Money Tools", "es": "Herramientas de dinero"}
_INDEX_LEAD = {
    "en": ("Free after-tax calculators for the money around your investing - budget, "
           "debt recycling, super and property. Each one shows its working, saves your "
           "scenarios, and never gives advice, just the numbers."),
    "es": ("Calculadoras gratuitas después de impuestos para el dinero alrededor de tu "
           "inversión - presupuesto, reciclaje de deuda, super y propiedad. Cada una "
           "muestra su cálculo, guarda tus escenarios, y nunca da consejos, solo los "
           "números."),
}
_INDEX_META_DESCRIPTION = {
    "en": ("Free Australian money calculators: debt recycling, super and retirement "
           "projection, a budget planner, and property vs S&P 500 - every figure shown "
           "line by line, described calculations, not advice."),
    "es": ("Calculadoras de dinero gratuitas para Australia: reciclaje de deuda, "
           "proyección de super y jubilación, un planificador de presupuesto, y "
           "propiedad vs S&P 500 - cada cifra mostrada línea por línea, cálculos "
           "descritos, no asesoramiento."),
}
_INDEX_CARD_BLURB = {
    "debt-recycling": {
        "en": "Offset vs invest vs recycle - four strategies after tax, with break-even.",
        "es": "Offset vs invertir vs reciclar - cuatro estrategias después de impuestos, con equilibrio.",
    },
    "super": {
        "en": "Where your super lands by retirement - contributions tax, caps and Division 293 included.",
        "es": "Dónde termina tu super al jubilarte - impuesto a las contribuciones, topes y División 293 incluidos.",
    },
    "budget": {
        "en": "Income to investable surplus, with named plans you can save.",
        "es": "De tus ingresos a un excedente invertible, con planes con nombre que puedes guardar.",
    },
    "property-vs-shares": {
        "en": "A leveraged investment property against simply indexing the deposit - after tax, with break-even rent.",
        "es": "Una propiedad de inversión apalancada frente a simplemente indexar el depósito - después de impuestos, con alquiler de equilibrio.",
    },
}
_INDEX_LEARN_MORE = {"en": "Learn more →", "es": "Ver más →"}
_OPEN_CALCULATOR = {"en": "Open the calculator — free", "es": "Abrir la calculadora — gratis"}
_NO_SIGNUP = {"en": "no sign-up needed to try it →", "es": "no necesitas registrarte para probarla →"}


_MT_CSS = """
<style>
.mt-crumb{color:#5b7290;font-size:11px;margin:0 0 10px}
.mt-crumb a{color:#5b7290}
.mt-hero{padding:0 0 6px}
.mt-hero h1{font-size:1.9rem;margin:0 0 10px}
.mt-lede{font-size:1.05rem;line-height:1.65;max-width:760px;margin:0 0 18px}
.mt-cta-row{margin:0 0 22px}
.mt-cta{display:inline-block;background:#14b8a6;color:#04211d;font-weight:800;
  font-size:14px;border-radius:10px;padding:11px 26px;text-decoration:none}
.mt-cta2{display:inline-block;color:#2dd4bf;font-size:12.5px;margin-left:16px}
.mt-feat{display:flex;gap:14px;flex-wrap:wrap;margin:0 0 24px}
.mt-f{flex:1;min-width:210px;border:1px solid #22345a;border-radius:10px;padding:14px 16px}
.mt-f b{display:block;font-size:13.5px;margin-bottom:5px}
.mt-f p{font-size:12.5px;line-height:1.55;margin:0;color:inherit;opacity:.85}
.mt-preview{border:1px solid #22345a;border-radius:10px;padding:16px 18px;margin:0 0 24px}
.mt-preview .cap{font-size:11px;opacity:.65;margin-bottom:8px}
.mt-prow{display:flex;justify-content:space-between;font-size:13px;padding:6px 0;
  border-bottom:1px solid rgba(128,128,128,.25)}
.mt-prow:last-of-type{border-bottom:none}
.mt-prow b{font-variant-numeric:tabular-nums}
.mt-prow b.g{color:#0f9d6b}
.mt-prow b.o{color:#b45309}
.mt-preview .note{font-size:11px;opacity:.6;margin-top:8px}
.mt-extra{font-size:12.5px;opacity:.85;margin-top:6px}
.mt-faq{margin:0 0 24px}
.mt-faq .item{margin:0 0 12px}
.mt-faq b{display:block;margin-bottom:3px}
.mt-faq p{margin:0;font-size:13px;line-height:1.6;opacity:.85}
.mt-note{font-size:12px;opacity:.7;margin-top:18px}
.mt-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:14px;margin:18px 0}
.mt-tool{border:1px solid #22345a;border-radius:12px;padding:16px}
.mt-tool .ic{font-size:20px;display:block;margin-bottom:6px}
.mt-tool b{display:block;font-size:14px;margin-bottom:4px}
.mt-tool p{font-size:12.5px;line-height:1.5;margin:0 0 10px;opacity:.85}
.mt-tool a.go{color:#2dd4bf;font-size:12px;font-weight:700;text-decoration:none}
</style>
"""


def _feature_cards_html(spec, lang):
    parts = []
    for f in spec["features"]:
        parts.append(
            f'<div class="mt-f"><b>{e(f["h"][lang])}</b><p>{e(f["p"][lang])}</p></div>'
        )
    return f'<div class="mt-feat">{"".join(parts)}</div>'


def _worked_example_html(slug, lang):
    ex = _WORKED_EXAMPLES[slug]()
    rows = "".join(
        f'<div class="mt-prow"><span>{e(r["label"][lang])}</span>'
        f'<b class="{r["cls"]}">{e(r["value"])}</b></div>'
        for r in ex["rows"]
    )
    extra = ""
    if ex.get("extra_line"):
        extra = f'<div class="mt-extra">{e(ex["extra_line"][lang])}</div>'
    note = ("Illustrative example from stated inputs - the tool recomputes everything "
            "from yours." if lang == "en" else
            "Ejemplo ilustrativo a partir de los datos indicados - la herramienta "
            "recalcula todo con los tuyos.")
    return f"""
<div class="mt-preview">
  <div class="cap">{e(ex["caption"][lang])}</div>
  {rows}
  {extra}
  <div class="note">{e(note)}</div>
</div>
"""


def _faq_html(spec, lang):
    items = "".join(
        f'<div class="item"><b>{e(f["q"][lang])}</b><p>{e(f["a"][lang])}</p></div>'
        for f in spec["faq"]
    )
    return f'<div class="mt-faq">{items}</div>'


def render_money_tool_landing(slug, base_url, lang="en"):
    """One page per MONEY_TOOL_SLUGS entry - breadcrumb, H1, lead, CTA,
    three feature cards, a live worked-example box, 2-3 FAQ entries, and
    the standing not-advice footnote. lang="es" renders the Spanish copy
    from the same MONEY_TOOL_PAGES spec (see module docstring)."""
    spec = MONEY_TOOL_PAGES[slug]
    en_path = f"/tools/{slug}"
    es_path = f"/es/tools/{slug}"
    path = es_path if lang == "es" else en_path
    canonical = f"{base_url}{path}"

    crumb = (f'<div class="mt-crumb"><a href="{"/es/tools" if lang == "es" else "/tools"}">'
             f'{e(_BREADCRUMB_HOME[lang])}</a> → {e(spec["h1"][lang])}</div>')

    cta_url = _tool_cta_url(slug, lang)
    cta = (f'<div class="mt-cta-row"><a class="mt-cta" href="{e(cta_url)}">'
           f'{e(_OPEN_CALCULATOR[lang])}</a>'
           f'<span class="mt-cta2">{e(_NO_SIGNUP[lang])}</span></div>')

    body = f"""
<main><div class="wrap">
  <article>
    {crumb}
    <div class="mt-hero">
      <h1>{spec["icon"]} {e(spec["h1"][lang])}</h1>
      <p class="mt-lede">{e(spec["lead"][lang])}</p>
      {cta}
    </div>
    {_feature_cards_html(spec, lang)}
    {_worked_example_html(slug, lang)}
    {_faq_html(spec, lang)}
    <p class="mt-note">{e(_FOOT_NOTE[lang])}</p>
  </article>
</div></main>
"""
    json_ld = _json_ld({
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": spec["h1"][lang],
        "url": canonical,
        "description": spec["meta_description"][lang],
        "applicationCategory": "FinanceApplication",
        "operatingSystem": "Any (web)",
        "offers": {"@type": "Offer", "price": "0", "priceCurrency": "AUD"},
        "author": _person_json_ld(),
        "publisher": _organization_json_ld(base_url),
    })
    hreflang_alternates = [
        ("en", f"{base_url}{en_path}"),
        ("es", f"{base_url}{es_path}"),
        ("x-default", f"{base_url}{en_path}"),
    ]
    head = _head(spec["title"][lang], spec["meta_description"][lang], canonical, base_url,
                 json_ld=json_ld, hreflang_alternates=hreflang_alternates,
                 extra_meta=f"<style>{blog_render._HOME_CSS}</style>{_MT_CSS}")
    lang_urls = (f"{base_url}{en_path}", f"{base_url}{es_path}")
    return _page(head, body, lang=lang, path=path, lang_urls=lang_urls).replace(
        "<body>", '<body class="home">', 1)


def render_money_tools_index(base_url, lang="en"):
    """The /tools (and /es/tools) index card grid - one card per
    MONEY_TOOL_SLUGS entry, each linking to its own landing page."""
    en_path = "/tools"
    es_path = "/es/tools"
    path = es_path if lang == "es" else en_path
    canonical = f"{base_url}{path}"

    cards = []
    for slug in MONEY_TOOL_SLUGS:
        spec = MONEY_TOOL_PAGES[slug]
        href = es_path + "/" + slug if lang == "es" else en_path + "/" + slug
        cards.append(f"""
<div class="mt-tool">
  <span class="ic">{spec["icon"]}</span>
  <b>{e(spec["h1"][lang])}</b>
  <p>{e(_INDEX_CARD_BLURB[slug][lang])}</p>
  <a class="go" href="{href}">{e(_INDEX_LEARN_MORE[lang])}</a>
</div>""")

    body = f"""
<main><div class="wrap">
  <article>
    <div class="mt-hero">
      <h1>\U0001F4B0 {e(_INDEX_H1[lang])}</h1>
      <p class="mt-lede">{e(_INDEX_LEAD[lang])}</p>
    </div>
    <div class="mt-grid">{"".join(cards)}</div>
    <p class="mt-note">{e(_FOOT_NOTE[lang])}</p>
  </article>
</div></main>
"""
    json_ld = _json_ld({
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": _INDEX_H1[lang],
        "url": canonical,
        "description": _INDEX_META_DESCRIPTION[lang],
        "publisher": _organization_json_ld(base_url),
    })
    hreflang_alternates = [
        ("en", f"{base_url}{en_path}"),
        ("es", f"{base_url}{es_path}"),
        ("x-default", f"{base_url}{en_path}"),
    ]
    head = _head(_INDEX_TITLE[lang], _INDEX_META_DESCRIPTION[lang], canonical, base_url,
                 json_ld=json_ld, hreflang_alternates=hreflang_alternates,
                 extra_meta=f"<style>{blog_render._HOME_CSS}</style>{_MT_CSS}")
    lang_urls = (f"{base_url}{en_path}", f"{base_url}{es_path}")
    return _page(head, body, lang=lang, path=path, lang_urls=lang_urls).replace(
        "<body>", '<body class="home">', 1)
