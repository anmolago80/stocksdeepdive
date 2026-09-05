"""Simple view, Part 4: the plain-English "why this matters" caption pass.

One short line per major Deep Dive section and per Compounder View tab,
shown directly under that section/tab's own header - in BOTH Simple and
Full view, always visible, regardless of subscription. Every caption is
static template text (no AI, no per-ticker computation) written once here
so the owner can review the whole set in one place instead of hunting
through app.py/compounder_ui.py for each call site.

These are deliberately a different, shorter line than any existing
"In plain English: ..." explainer caption already under these headers -
this pass ADDS a one-line "why should I care" hook above that existing,
more detailed explanation; nothing existing is removed or reworded.

Keys match the section/tab label used at each call site exactly:
  - The six Compounder View tabs (compounder_ui.render_section(), shared
    by the Deep Dive's auto Compounder View and the Research page).
  - The Deep Dive's own major sections (Value Score, Quality, Psychology,
    Discovery, Moat, Margin of Safety, Trade Setup, Insider & capital,
    Dividends, Compounder View (auto)).
"""

SECTION_WHY_CAPTIONS = {
    # --- Compounder View tabs (shared: Deep Dive auto view + Research) ---
    "Fundamentals": (
        "The raw scorecard everything else on this page is built from."
    ),
    "Value vs Book": "What each dollar kept on the books turned into",
    "Retained Earnings": (
        "Did the profits the company kept actually make it more valuable?"
    ),
    "Earnings Trends": "Is the underlying profit actually growing, or just the share price?",
    "Cost of Capital": (
        "The return the company must beat for its growth to create value"
    ),
    "Fair Value": "What the business would be worth using four independent valuation methods",

    # --- Deep Dive's own major sections ---
    "Value Score": "One number blending everything below - not a recommendation, a summary",
    "Quality": "How solid the underlying business is, independent of price",
    "Psychology": "Whether the crowd trading this stock right now looks fearful or greedy",
    "Discovery": "Price/volume attention only - not a quality signal",
    "Moat": "How well this business's profits are protected from competitors",
    "Margin of Safety": "How much cheaper today's price is than the model's own estimate",
    "Trade Setup": "A technical entry/stop-loss/target read - not a valuation judgment",
    "Insider & capital": "What the people running the company are doing with their own money",
    "Dividends": "What the business has actually paid out, and how reliably",
    "Compounder View (auto)": (
        "The same research workbook sections used for hand-covered "
        "companies, computed live for this one"
    ),
}

# Español instruction, Part 1: the Spanish translation of the dict above,
# same keys (the section/tab label - itself left in English/as the site's
# own internal section-key, never translated, since it's also used as a
# lookup key elsewhere) mapped to a translated caption. A caller looks up
# SECTION_WHY_CAPTIONS_ES.get(label) when lang=="es", falling back to the
# EN caption above (via SECTION_WHY_CAPTIONS) when a key is missing here -
# same EN-fallback contract as i18n.t().
SECTION_WHY_CAPTIONS_ES = {
    "Fundamentals": (
        "El cuadro de indicadores base sobre el que se construye todo lo "
        "demás en esta página."
    ),
    "Value vs Book": "En qué se convirtió cada dólar retenido en los libros contables",
    "Retained Earnings": (
        "¿Las ganancias que la empresa retuvo realmente la hicieron más valiosa?"
    ),
    "Earnings Trends": (
        "¿La ganancia subyacente realmente está creciendo, o solo lo hace "
        "el precio de la acción?"
    ),
    "Cost of Capital": (
        "El retorno que la empresa debe superar para que su crecimiento genere valor"
    ),
    "Fair Value": (
        "Cuánto valdría el negocio según cuatro métodos de valoración independientes"
    ),

    "Value Score": (
        "Un solo número que combina todo lo de abajo - no es una "
        "recomendación, es un resumen"
    ),
    "Quality": "Qué tan sólido es el negocio subyacente, independientemente del precio",
    "Psychology": (
        "Si la multitud que opera esta acción ahora mismo parece temerosa o codiciosa"
    ),
    "Discovery": "Solo atención de precio/volumen - no es una señal de calidad",
    "Moat": (
        "Qué tan bien están protegidas las ganancias de este negocio frente "
        "a la competencia"
    ),
    "Margin of Safety": (
        "Cuánto más barato es el precio de hoy respecto a la propia "
        "estimación del modelo"
    ),
    "Trade Setup": (
        "Una lectura técnica de entrada/stop-loss/objetivo - no un juicio "
        "de valoración"
    ),
    "Insider & capital": (
        "Qué están haciendo con su propio dinero las personas que dirigen la empresa"
    ),
    "Dividends": "Cuánto ha pagado realmente el negocio, y con qué grado de fiabilidad",
    "Compounder View (auto)": (
        "Las mismas secciones del cuaderno de investigación usadas para "
        "empresas cubiertas manualmente, calculadas en vivo para esta"
    ),
}
