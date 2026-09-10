"""
Mega-batch Part 15: plain-English notes across the Stress Test and My ETFs
tabs - "every element on these two tabs needs a 'what is this and what is
it telling me' note, in the site's existing plain-caption style (the
Part-4-of-Simple-view caption dict pattern...)" (the owner's own words -
see simple_view_copy.py's SECTION_WHY_CAPTIONS/_ES for that pattern, which
this file mirrors exactly: one EN dict + one ES dict per concern, static
template text, no AI calls, no per-ticker computation).

One shared set of dicts feeds THREE surfaces so the texts can never drift
apart (per the instruction's own "HOW it's shown" rule):
  1. st.column_config(help=...) tooltips - desktop hover, on every
     dataframe-rendered table's column headers.
  2. A "ⓘ What each column means" expander directly UNDER a table - the
     mobile/touch fallback (no hover), same entries as a compact list.
  3. The fuller paragraphs (Beta formula + boundaries, the scenario
     replay method, the Monte Carlo method) live ONCE, in each tab's top
     "ⓘ How this tab works" expander (Stress Test only - My ETFs gets
     column help + section captions but the instruction doesn't ask for
     a top expander there). Column/tooltip help itself stays to one or
     two sentences - the long Beta note below is deliberately NOT used
     as a tooltip body; STRESS_COLUMN_HELP's "beta" entry is the trimmed
     version for that.

Call sites (app.py) look these up via the small lang-aware helper
functions at the bottom of this file, same EN-fallback contract as
app.py's own _section_why()/SECTION_WHY_CAPTIONS_ES.get(...).
"""

# ---------------------------------------------------------------------
# Stress Test tab - top "How this tab works" expander body. Three parts:
# the overview (replay concept, facts vs the one simulated section, the
# ◆ flag), the full Beta note, the scenario-replay method, and the Monte
# Carlo method - the instruction's own list of "fuller paragraphs" that
# belong here once rather than repeated in every column tooltip.
# ---------------------------------------------------------------------

STRESS_OVERVIEW_EN = (
    "This tab replays your ACTUAL current holdings through real historical "
    "price and dividend data - the headline cards, charts, tables and "
    "shock grid below are all facts about what already happened to these "
    "exact holdings (or a sector/index proxy for a younger one), not "
    "predictions. The one exception is the Monte Carlo section at the "
    "bottom, which is a statistical simulation, not a historical fact - "
    "it's kept in its own clearly-labelled box for exactly that reason. A "
    "◆ next to a figure means that holding didn't exist yet during that "
    "window, so its move is estimated from its sector or benchmark index "
    "scaled by its own measured beta, rather than its own real price "
    "history."
)
STRESS_OVERVIEW_ES = (
    "Esta pestaña reproduce tus posiciones ACTUALES sobre datos reales "
    "históricos de precio y dividendos - las tarjetas principales, los "
    "gráficos, las tablas y la cuadrícula de choque de abajo son todos "
    "hechos sobre lo que ya le ocurrió a estas posiciones exactas (o a un "
    "sector/índice sustituto si la posición es más joven), no "
    "predicciones. La única excepción es la sección Monte Carlo al final, "
    "que es una simulación estadística, no un hecho histórico - por eso "
    "tiene su propio recuadro claramente etiquetado. Un ◆ junto a una "
    "cifra significa que esa posición todavía no existía en esa ventana, "
    "así que su movimiento se estima a partir de su sector o índice de "
    "referencia escalado por su propia beta medida, en lugar de su propio "
    "historial real de precios."
)

BETA_FULL_NOTE_EN = (
    "How much of the market's move this holding historically follows. "
    "Formula: β = correlation with the market × (holding's volatility ÷ "
    "market's volatility) — equivalently cov(holding, market) ÷ "
    "var(market), from monthly returns. Guide: 0 = ignores the market · "
    "0–0.7 defensive · ≈1.0 = matches the market · 1–2 amplified · >2 "
    "rare (usually leveraged or a data artefact) · negative = moves "
    "against the market (rare; gold sometimes). Values below −0.5 or "
    "above 3 should be distrusted — usually short history or bad data."
)
BETA_FULL_NOTE_ES = (
    "Cuánto sigue esta posición, históricamente, los movimientos del "
    "mercado. Fórmula: β = correlación con el mercado × (volatilidad de "
    "la posición ÷ volatilidad del mercado) — equivalente a cov(posición, "
    "mercado) ÷ var(mercado), con rentabilidades mensuales. Guía: 0 = "
    "ignora el mercado · 0–0,7 defensiva · ≈1,0 = igual que el mercado · "
    "1–2 amplificada · >2 rara (normalmente apalancada o un error de "
    "datos) · negativa = se mueve en contra del mercado (rara; a veces el "
    "oro). Los valores por debajo de −0,5 o por encima de 3 deberían "
    "desconfiarse — normalmente por poco historial o datos erróneos."
)

SCENARIO_METHOD_EN = (
    "A real calendar window (e.g. COVID crash = Feb–Mar 2020). The value "
    "is this holding's actual price+distribution return over exactly "
    "those dates; the portfolio row is today's weights applied to those "
    "same real returns. ◆ = the holding didn't exist then; proxied by "
    "its sector/index × its beta."
)
SCENARIO_METHOD_ES = (
    "Una ventana real del calendario (p. ej. crisis COVID = feb-mar "
    "2020). El valor es la rentabilidad real (precio + dividendos) de "
    "esta posición exactamente en esas fechas; la fila de la cartera "
    "aplica las ponderaciones de hoy a esas mismas rentabilidades reales. "
    "◆ = la posición no existía entonces; estimado a partir de su "
    "sector/índice × su beta."
)

MONTE_CARLO_METHOD_EN = (
    "From your holdings' real monthly returns we measure average, "
    "volatility and all cross-correlations, then simulate 5,000 possible "
    "next-years (12 monthly draws each, shaped by those statistics), "
    "apply your weights to each, sort the 5,000 outcomes, and report the "
    "5th and 95th percentile. A statistical remix of the past — not a "
    "prediction."
)
MONTE_CARLO_METHOD_ES = (
    "A partir de las rentabilidades mensuales reales de tus posiciones, "
    "medimos el promedio, la volatilidad y todas las correlaciones "
    "cruzadas; luego simulamos 5.000 posibles próximos años (12 sorteos "
    "mensuales cada uno, formados por esas estadísticas), aplicamos tus "
    "ponderaciones a cada uno, ordenamos los 5.000 resultados y "
    "reportamos los percentiles 5 y 95. Una recombinación estadística del "
    "pasado — no una predicción."
)

# ---------------------------------------------------------------------
# Stress Test - one short caption under each section header.
# ---------------------------------------------------------------------

STRESS_SECTION_CAPTIONS_EN = {
    "headline_cards": (
        "The single worst and single best 12-month-or-longer stretch "
        "this exact portfolio has actually lived through, replayed from "
        "real prices."
    ),
    "drawdown_runup": (
        "How today's portfolio value would have tracked, day by day, "
        "through its own worst fall and its own best climb."
    ),
    "crisis_table": (
        "How this portfolio's current holdings would have moved during "
        "each real historical crisis, replayed at today's weights."
    ),
    "rally_table": "The same replay through real historical rallies instead of crises.",
    "shock_grid": (
        "A quick beta-based estimate of how the portfolio moves if the "
        "market itself moves by a given amount - a simplified model, "
        "not a replay."
    ),
    "sandbox": (
        "Change the weights below to see how the SAME historical replay "
        "would have looked with a different mix - nothing here touches "
        "your real portfolio."
    ),
    "per_holding_table": (
        "Every holding's own risk numbers side by side, so you can see "
        "which ones are driving the portfolio-level figures above."
    ),
    "monte_carlo": (
        "A statistical simulation of possible one-year outcomes - the "
        "only section on this tab that isn't a replay of something that "
        "actually happened."
    ),
}
STRESS_SECTION_CAPTIONS_ES = {
    "headline_cards": (
        "El peor y el mejor tramo de 12 meses o más que esta cartera "
        "exacta ha vivido realmente, reproducido a partir de precios "
        "reales."
    ),
    "drawdown_runup": (
        "Cómo habría evolucionado, día a día, el valor de la cartera de "
        "hoy durante su peor caída y su mejor subida."
    ),
    "crisis_table": (
        "Cómo se habrían movido las posiciones actuales de esta cartera "
        "durante cada crisis histórica real, reproducida con las "
        "ponderaciones de hoy."
    ),
    "rally_table": "La misma reproducción, pero con subidas históricas reales en vez de crisis.",
    "shock_grid": (
        "Una estimación rápida basada en beta de cómo se mueve la "
        "cartera si el propio mercado se mueve una cantidad dada - un "
        "modelo simplificado, no una reproducción."
    ),
    "sandbox": (
        "Cambia las ponderaciones de abajo para ver cómo se vería la "
        "MISMA reproducción histórica con una mezcla distinta - nada "
        "aquí toca tu cartera real."
    ),
    "per_holding_table": (
        "Los números de riesgo de cada posición, uno junto a otro, para "
        "ver cuáles están impulsando las cifras de la cartera de arriba."
    ),
    "monte_carlo": (
        "Una simulación estadística de posibles resultados a un año - la "
        "única sección de esta pestaña que no es una reproducción de "
        "algo que realmente ocurrió."
    ),
}

# ---------------------------------------------------------------------
# Stress Test - short (tooltip-length) per-holding-table column help.
# ---------------------------------------------------------------------

STRESS_COLUMN_HELP_EN = {
    "beta": (
        "How much this holding historically moves relative to the "
        "market (1.0 = matches it). Full formula and how to read extreme "
        "values: see ⓘ How this tab works, above."
    ),
    "worst_drawdown": "The deepest fall from a peak this holding has had in the last 15 years.",
    "best_12m": "This holding's best 12-month-or-longer return over the same 15-year lookback.",
}
STRESS_COLUMN_HELP_ES = {
    "beta": (
        "Cuánto se mueve esta posición, históricamente, en relación con "
        "el mercado (1,0 = igual que el mercado). Fórmula completa y "
        "cómo interpretar valores extremos: ver ⓘ Cómo funciona esta "
        "pestaña, arriba."
    ),
    "worst_drawdown": "La caída más profunda desde un máximo que ha tenido esta posición en los últimos 15 años.",
    "best_12m": "La mejor rentabilidad a 12 meses o más de esta posición en el mismo periodo de 15 años.",
}

# ---------------------------------------------------------------------
# Rebalance sandbox's own "Current vs what-if" table (owner-reported: the
# table was missing a COVID recovery row alongside the existing COVID
# replay one, and had no explanation anywhere for what "Replayed 10y
# return p.a." means). Unlike the tables above, this one's ROWS are the
# different metrics (Max downside, Beta, Replayed 10y, ...) rather than
# one metric per COLUMN, so a single column_config(help=...) on "Metric"
# can't carry a different explanation per row the way col_window's help
# does for the crisis/rally tables - this is one combined tooltip body
# instead, shown via the same ⓘ-popover affordance _info_popover_trigger
# already gives the Monte Carlo row just below this table (a plain
# st.popover, tap-to-open, so no separate mobile fallback is needed
# either - unlike column_config's hover-only help).
# ---------------------------------------------------------------------

SANDBOX_METRICS_HELP_EN = (
    "Max downside/upside: the worst peak-to-trough fall and the best "
    "12-month-or-longer run this mix has lived through, replayed from "
    "real prices. COVID replay/recovery: this mix's actual return over "
    "the COVID crash (Feb-Mar 2020) and the rebound that followed "
    "(Apr 2020-Mar 2021). Beta: this mix's overall sensitivity to its "
    "home market index. Replayed 10y return p.a.: the annualised return "
    "of this exact mix over its own last 10 years (or its full history "
    "if shorter) - one compounded number standing in for a decade of "
    "real ups and downs."
)
SANDBOX_METRICS_HELP_ES = (
    "Máxima caída/subida: la mayor caída de máximo a mínimo y la mejor "
    "racha de 12 meses o más que ha vivido esta combinación, reproducida "
    "con precios reales. Repetición/recuperación COVID: la rentabilidad "
    "real de esta combinación durante la caída del COVID (feb-mar 2020) "
    "y el repunte que le siguió (abr 2020-mar 2021). Beta: la "
    "sensibilidad general de esta combinación a su índice de mercado "
    "local. Rentabilidad reproducida a 10 años anual: la rentabilidad "
    "anualizada de esta combinación exacta durante sus últimos 10 años "
    "(o todo su historial si es más corto) - una única cifra compuesta "
    "que resume una década de subidas y bajadas reales."
)

# Owner-requested (12 Sep 2026): one-click optimizer presets, previewed
# and shipped, then redesigned same day into two purely informational
# reference tiles (best 12-month run / shallowest drawdown, any mix of
# these tickers) after discussion of the presets' built-in bias toward
# volatility over sustained quality. This is the info-popover text for
# that row, in the same spot sandbox_metrics_help's own popover sits for
# the table below it.
OPTIMIZER_HELP_EN = (
    "These two numbers are the best and worst extremes found by testing "
    "3,000 different weight combinations for your tickers against their "
    "real price history (the same replay the table below uses) - the "
    "best single 12-month run any mix achieved, and the shallowest "
    "peak-to-trough fall any mix achieved. They're a reference range, "
    "not a suggested split - use them to judge how close (or far) your "
    "own mix below lands from what this set of tickers has been capable "
    "of. You still choose every %. \"Show the mix that reached this\" on "
    "each tile is information only - it doesn't fill in or change "
    "anything below."
)
OPTIMIZER_HELP_ES = (
    "Estas dos cifras son los extremos mejor y peor hallados al probar "
    "3.000 combinaciones de ponderaciones distintas para tus tickers "
    "contra su historial de precios real (la misma repetición que usa la "
    "tabla de abajo) - la mejor racha de 12 meses consecutivos que logró "
    "cualquier combinación, y la caída de máximo a mínimo menos profunda "
    "que logró cualquier combinación. Son un rango de referencia, no una "
    "combinación sugerida - úsalas para juzgar qué tan cerca (o lejos) "
    "queda tu propia combinación de abajo de lo que este conjunto de "
    "tickers ha sido capaz de lograr. Tú sigues eligiendo cada %. "
    "\"Mostrar la combinación que logró esto\" en cada casilla es solo "
    "información - no rellena ni cambia nada de lo de abajo."
)

# ---------------------------------------------------------------------
# Scenario columns (crisis + rally) - shared by the per-holding table
# AND the two scenario tables. Exact date ranges from
# stress_engine.CRISES/RALLIES (kept in sync manually - both are small,
# hand-curated, rarely-changed lists).
# ---------------------------------------------------------------------

SCENARIO_DATE_RANGES = {
    "gfc": "1 Oct 2007 – 31 Mar 2009",
    "q4_2018": "1 Oct 2018 – 31 Dec 2018",
    "covid": "1 Feb 2020 – 31 Mar 2020",
    "rate_shock_2022": "1 Jan 2022 – 31 Oct 2022",
    "post_gfc": "1 Mar 2009 – 31 Dec 2009",
    "melt_up_2016_17": "1 Jan 2016 – 31 Dec 2017",
    "covid_recovery": "1 Apr 2020 – 31 Mar 2021",
    "ai_rally_2023_24": "1 Jan 2023 – 31 Dec 2024",
}

_SCENARIO_COL_TMPL_EN = (
    "{dates}. This holding's actual price+distribution return over "
    "exactly this window; ◆ = didn't exist yet, estimated from its "
    "sector/index × beta."
)
_SCENARIO_COL_TMPL_ES = (
    "{dates}. La rentabilidad real (precio + dividendos) de esta "
    "posición exactamente en esta ventana; ◆ = todavía no existía, "
    "estimado a partir de su sector/índice × beta."
)


def scenario_column_help(key, lang="en"):
    dates = SCENARIO_DATE_RANGES.get(key, "")
    tmpl = _SCENARIO_COL_TMPL_ES if lang == "es" else _SCENARIO_COL_TMPL_EN
    return tmpl.format(dates=dates)


# ---------------------------------------------------------------------
# My ETFs - one short caption under each section header (cards, overlap,
# projector - the instruction's own list; the summary table already has
# its own caption_return disclaimer and isn't in that list).
# ---------------------------------------------------------------------

ETF_SECTION_CAPTIONS_EN = {
    "cards": (
        "Fund-level facts for each ETF you hold - fees, sector mix and "
        "top holdings, straight from the fund's own factsheet where "
        "available."
    ),
    "overlap": (
        "Where a stock you hold directly is ALSO held indirectly inside "
        "one of your ETFs, so you can see your true combined exposure to "
        "it."
    ),
    "projector": (
        "A what-if compounding calculation using the rate you choose - "
        "not a forecast of what your ETFs will actually return."
    ),
}
ETF_SECTION_CAPTIONS_ES = {
    "cards": (
        "Datos de cada ETF que tienes - comisiones, mezcla sectorial y "
        "principales posiciones, directamente de la ficha del propio "
        "fondo cuando está disponible."
    ),
    "overlap": (
        "Dónde una acción que tienes de forma directa TAMBIÉN está "
        "dentro de uno de tus ETFs, para ver tu exposición combinada "
        "real a ella."
    ),
    "projector": (
        "Un cálculo de interés compuesto hipotético con la tasa que "
        "elijas - no una previsión de lo que realmente rentarán tus "
        "ETFs."
    ),
}

# ---------------------------------------------------------------------
# My ETFs - short per-column help for the summary table.
# ---------------------------------------------------------------------

ETF_COLUMN_HELP_EN = {
    "mer": "The fund's yearly fee, already inside its returns.",
    "return": "Annualised total return (price + distributions) over the stated lookback.",
    "yield": "Trailing 12-month distributions as a % of price.",
    "corr_sp500": "How in-step its monthly moves are with the S&P 500: 1 = lockstep, 0 = unrelated.",
    "corr_asx200": "How in-step its monthly moves are with the ASX 200: 1 = lockstep, 0 = unrelated.",
}
ETF_COLUMN_HELP_ES = {
    "mer": "La comisión anual del fondo, ya incluida en su rentabilidad.",
    "return": "Rentabilidad total anualizada (precio + reparto) en el periodo indicado.",
    "yield": "Reparto de los últimos 12 meses como % del precio.",
    "corr_sp500": "Cuánto se mueve en sintonía con el S&P 500 cada mes: 1 = al mismo ritmo, 0 = sin relación.",
    "corr_asx200": "Cuánto se mueve en sintonía con el ASX 200 cada mes: 1 = al mismo ritmo, 0 = sin relación.",
}


# ---------------------------------------------------------------------
# Lang-aware lookup helpers - same EN-fallback contract as app.py's own
# _section_why()/SECTION_WHY_CAPTIONS_ES.get(...).
# ---------------------------------------------------------------------

def _pick(en_text, es_text, lang):
    return es_text if (lang == "es" and es_text) else en_text


def stress_overview(lang="en"):
    return _pick(STRESS_OVERVIEW_EN, STRESS_OVERVIEW_ES, lang)


def beta_full_note(lang="en"):
    return _pick(BETA_FULL_NOTE_EN, BETA_FULL_NOTE_ES, lang)


def scenario_method(lang="en"):
    return _pick(SCENARIO_METHOD_EN, SCENARIO_METHOD_ES, lang)


def monte_carlo_method(lang="en"):
    return _pick(MONTE_CARLO_METHOD_EN, MONTE_CARLO_METHOD_ES, lang)


def stress_section_caption(key, lang="en"):
    d = STRESS_SECTION_CAPTIONS_ES if lang == "es" else STRESS_SECTION_CAPTIONS_EN
    return d.get(key) or STRESS_SECTION_CAPTIONS_EN.get(key, "")


def stress_column_help(key, lang="en"):
    d = STRESS_COLUMN_HELP_ES if lang == "es" else STRESS_COLUMN_HELP_EN
    return d.get(key) or STRESS_COLUMN_HELP_EN.get(key, "")


def sandbox_metrics_help(lang="en"):
    return _pick(SANDBOX_METRICS_HELP_EN, SANDBOX_METRICS_HELP_ES, lang)


def optimizer_help(lang="en"):
    return _pick(OPTIMIZER_HELP_EN, OPTIMIZER_HELP_ES, lang)


def etf_section_caption(key, lang="en"):
    d = ETF_SECTION_CAPTIONS_ES if lang == "es" else ETF_SECTION_CAPTIONS_EN
    return d.get(key) or ETF_SECTION_CAPTIONS_EN.get(key, "")


def etf_column_help(key, lang="en"):
    d = ETF_COLUMN_HELP_ES if lang == "es" else ETF_COLUMN_HELP_EN
    return d.get(key) or ETF_COLUMN_HELP_EN.get(key, "")
