"""
site_content.py

The prose of the three content pages - How the scores work, About, and the
Privacy policy - held in one place because two different renderers now
need exactly the same words.

Streamlit renders them through app.py for anyone using the app, and
server.py renders them as real, crawlable HTML at /methodology, /about and
/privacy (Streamlit pages are invisible to search engines - see
server.py's module docstring). Keeping the text here means the indexed
page and the in-app page can never drift apart, which for a privacy policy
is a legal requirement rather than a nicety.

Everything below is Markdown.
"""

import moat_engine

# Shown above the methodology text in the public (factual) presentation.
METHODOLOGY_FACTUAL_NOTE = (
    "**Presentation note.** This site displays data, model outputs and "
    "described calculations from stated inputs. It does not provide "
    "financial product advice or recommendations - descriptions below "
    "of how each calculation works are exactly that: descriptions of "
    "arithmetic, not guidance on what to do."
)

def _methodology_md_en(factual, moat_blended):
    """English 'How the scores work' copy (moat check, 9 Sep 2026 - owner
    decision to stop publishing the internal recipe). Deliberately says
    WHAT each score answers and how the pieces relate to each other, not
    the weights, point allocations, scoring bands or formulas behind them
    - the previous version was detailed enough that a competitor could
    reimplement the model straight from this page.

    In factual mode (the public default - see _factual() in app.py) the
    Long Score becomes the Value Score and drops its verdict labels and
    thresholds, and the Trade Setup section is swapped for a neutral
    psychology/discovery description, so the public page never reads as
    financial advice. Both presentations are built from one template here
    so they can never drift apart, and app.py's page_methodology() calls
    this function directly rather than keeping its own copy."""
    moat_in_list = ", and durability of competitive advantage" if moat_blended else ""
    _score_name = "Value Score" if factual else "Long Score"
    moat_fold_note = (f" It's also folded into the {_score_name} above."
                       if moat_blended else
                       f" It's shown separately from the {_score_name} above.")

    if factual:
        score_section = f"""#### The Value Score (0–100)

One number summarising measures of business quality, valuation against intrinsic value,
market psychology, market attention{moat_in_list}, weighted so no one factor can dominate
the result. The number is shown without signal labels or recommendations - a description
of the underlying calculations, not investment advice. Where no intrinsic value could be
computed, that is stated plainly and the affected values are marked."""
        timing_section = """#### Psychology and discovery readings

Alongside the valuation models, the site reports what the crowd has been doing:
distance below the 3-month high (fear), distance from the 50-day average and greed/
FOMO terms, and a discovery reading built from volume, search interest, news and
social chatter. These are measurements, stated as numbers - the site does not
display entry levels, targets or trade verdicts."""
    else:
        score_section = f"""#### The Long Score (0–100)

One number answering "is this a good business to own at this price?" It blends measures of
business quality, valuation against intrinsic value, market psychology, market attention{moat_in_list}
into a single score, weighted so no one factor can dominate the result.

Above 70 = **STRONG LONG**, above 50 = **LONG**, above 30 = **WATCHLIST**, otherwise
**AVOID**. If no intrinsic value could be computed at all, the signal is capped at
WATCHLIST - a thesis whose value leg can't be verified doesn't get a full
recommendation."""
        timing_section = """#### Value vs timing - two separate verdicts

The **Investment Signal** answers "good business to own?" The **Trade Setup** answers
"is right now a sane entry?" - support/resistance-based entry zone, stop loss and
targets, gated on trend safety and risk/reward. A great company can be a poor entry
today; the site shows both rather than blurring them into one contradictory verdict."""

    return f"""
Every tool on this site runs the same engine. A ticker goes in; live data comes back
(prices and volumes, financial statements and analyst estimates via Yahoo Finance, search
interest via Google Trends, headlines via Yahoo/NewsAPI, chatter via StockTwits); and the
same value-investing framework runs every time.

{score_section}

#### Moat Score (0–100)

A separate read on how likely the business is to stay as good as it looks today, not just
how good it is right now.{moat_fold_note} Above 70 = **Strong moat**, 40–70 = **Moderate
moat**, 40 and below = **Weak/no moat**.

#### Intrinsic value

The primary model is a discounted cash flow built from the company's own reported free cash
flows, benchmarked against today's price to estimate a fair value. Where a full DCF isn't
possible, a simpler estimate is used instead and labelled as such.

A stock trading meaningfully below intrinsic value is labelled **UNDERVALUED**; meaningfully
above, **EXPENSIVE**; between, **FAIR**.

#### What the price implies (reverse DCF)

Alongside the standard DCF above, the Deep Dive page also runs it in reverse: it solves for
the growth rate that would make the model's fair value equal today's price. That figure -
the implied growth rate - describes what the market is currently pricing in, shown alongside
the growth rate the model itself assumed, so the two can be compared directly.

This is a calculation from stated inputs, not a forecast: it says nothing about whether the
implied growth rate is realistic, only what it is. Where the base free cash flow rests on an
estimate, this figure carries the same red-flag note as the Intrinsic Value it's derived
from.

{timing_section}

#### The red-flag rule

Whenever a number rests on a default or average because real data wasn't available, it's
shown in **red**. An estimate is never dressed up as a fact - you always know which
numbers are computed and which are assumed.

#### Rational Compounder Research

The Research section is different: it isn't computed at all. It's the author's own
hand-built workbook analysis of selected quality compounders - a decade of earnings
history, four independent fair-value methods (trailing P/E, forward P/E, DCF, and a
10-year equity method), and written Buffett/Munger-style judgment on management, moat
and risk. Every threshold and colour band on those pages comes from the original
research, not a generic screen.

#### Limitations, honestly

Data is sourced from free public feeds and can be delayed, revised or occasionally
wrong. Intrinsic value is an estimate resting on assumptions - reasonable assumptions,
shown openly, but assumptions. Scores are model outputs, not personal advice, and none
of this considers your circumstances. Use it the way it was built to be used: as the
starting point for your own judgment, not a substitute for it.
"""

ABOUT_FACTUAL_MD = """
StocksDeepDive is built and run by **Andres Moreno**, a private investor in Australia.

It didn't start as a website. It started as a personal stock scanner and a very long
Excel workbook - tools built to study businesses with a Buffett/Munger-style value
lens: compute what the model says a business's cash flows are worth, test its quality
from reported fundamentals, and read what the price has been doing. Over the years the
scanner grew a DCF engine, quality calculations, psychology and discovery readings, and a
research workbook that documents one company for weeks at a time.

At some point the obvious question arrived: why not open the numbers up? So this site
is that - the same engine, the same data work, made public.

Two principles carried over from the private version, unchanged:

**The numbers must be honest.** Whenever a figure rests on a default or an average
because real data wasn't available, it's shown in red. An estimate is never dressed up
as a fact. I built that rule for myself, because fooling yourself is expensive - it
applies just as much now that you're reading the numbers too.

**Value and psychology are different measurements.** What the model computes from
a business's cash flows and what the crowd has been doing to its price are reported as
separate numbers on every page. Most tools blur them; this site states each one
plainly and lets you draw your own conclusions.

The site is free while it launches. When subscriptions open, founding members keep
launch pricing. If you want a stock added to the Rational Compounder research list, or
anything here doesn't make sense, use the Feedback button on any results page or email
[rationalcompounder@stocksdeepdive.com](mailto:rationalcompounder@stocksdeepdive.com) -
I read everything.

*This site presents factual information and calculator outputs only - it does not
provide financial product advice or recommendations; see the disclaimer in the footer.
I may own stocks analysed here.*
"""

ABOUT_FULL_MD = """
StocksDeepDive is built and run by **Andres Moreno**, a private investor in Australia.

It didn't start as a website. It started as a personal stock scanner and a very long
Excel workbook - the tools I built to manage my own self-managed super fund with a
Buffett/Munger-style value approach: work out what a business is actually worth, check
its quality like an owner would, and only then look at what the crowd is doing. Over
the years the scanner grew a DCF engine, quality tests, a crowd-psychology read, trade
setups, and a research workbook that interrogates one company for weeks at a time.

At some point the obvious question arrived: if I trust these numbers with my own
retirement savings, why not open them up? So this site is that - the same engine,
the same research, made public.

Two principles carried over from the private version, unchanged:

**The numbers must be honest.** Whenever a figure rests on a default or an average
because real data wasn't available, it's shown in red. An estimate is never dressed up
as a fact. I built that rule for myself, because fooling yourself is expensive - it
applies just as much now that you're reading the numbers too.

**Value and timing are different questions.** Whether a business is worth owning and
whether today is a sane day to buy it get separate verdicts on every page. Most tools
blur them; keeping them apart is half the discipline.

The site is free while it launches. When subscriptions open, founding members keep
launch pricing. If you want a stock added to the Rational Compounder research list, or
anything here doesn't make sense, use the Feedback button on any results page or email
[rationalcompounder@stocksdeepdive.com](mailto:rationalcompounder@stocksdeepdive.com) -
I read everything.

*Nothing on this site is financial advice - see the disclaimer in the footer. I may
own stocks analysed here.*
"""

PRIVACY_MD = """
*Last updated: 13 August 2026*

StocksDeepDive ("the site", "we") is operated by Andres Moreno in Australia. This page
explains what information the site handles and what happens to it. Contact for anything
privacy-related: [rationalcompounder@stocksdeepdive.com](mailto:rationalcompounder@stocksdeepdive.com).

#### What we collect

**Nothing, for anonymous browsing.** You can use every analysis tool without an
account. Standard technical logs (IP address, browser type, pages requested) are kept
by our hosting provider (Railway) for security and debugging, as with any website.

**If you sign in with Google:** we receive your name and email address from Google -
nothing else. Sign-in exists so the site can remember your watchlist, attribute your
feedback, and (if you save a watchlist) send you the weekly watchlist digest email. We
never see your Google password.

**If you save a watchlist:** the tickers you save are stored against your email
address on our server.

**If you send feedback:** your message and, if you're signed in, your email address
are stored so we can follow up.

**If subscriptions are active and you subscribe:** payment is handled entirely by
Stripe. We never see or store your card details - we only check with Stripe whether
your email has an active subscription.

#### What we don't do

No advertising, no ad trackers, no third-party analytics or ad tech, and no selling
or sharing of your information with anyone, ever. The only cookies used are the ones
required to keep you signed in.

#### Page analytics

We keep first-party, aggregate page-view counts (which pages get visited, how many
times, per day) so we can see what's useful - no cookies are set for this, no
third-party trackers or ad tech are involved, and no per-visitor identity is stored
alongside a view.

#### Emails

The weekly digest is sent (via Mailgun) only to signed-in users who have saved a
watchlist. To stop it, remove all stocks from your watchlist, or email us and we'll
remove you.

#### Data retention and deletion

Watchlists and feedback are kept while your account is active. Email us from your
sign-in address and we will delete everything we hold about you.

#### Third-party data on the site

Market data shown on the site comes from third-party sources (Yahoo Finance, Google
Trends, StockTwits, NewsAPI, GDELT). Those services receive standard requests from our
server, not information about you.

#### Changes

If this policy changes, the date above will change with it. Material changes will be
noted on the site.
"""


def _methodology_md_es(factual, moat_blended):
    """Espejo en español de _methodology_md_en() - mismo recorte de detalle
    (moat check, 9 de septiembre de 2026): dice QUÉ responde cada puntaje y
    cómo se relacionan entre sí, no los pesos, puntos, bandas ni fórmulas
    detrás de ellos. Traducido con cuidado término a término, no de forma
    mecánica."""
    moat_in_list = ", y la durabilidad de la ventaja competitiva" if moat_blended else ""
    _score_name = "Value Score" if factual else "Long Score"
    moat_fold_note = (f" También se incorpora al {_score_name} de arriba."
                       if moat_blended else
                       f" Se muestra por separado del {_score_name} de arriba.")

    if factual:
        score_section = f"""#### El Value Score (0–100)

Un número que resume medidas de la calidad del negocio, la valoración frente al valor
intrínseco, la psicología del mercado, la atención del mercado{moat_in_list}, ponderadas
para que ningún factor domine el resultado. El número se muestra sin etiquetas de señal
ni recomendaciones - es una descripción de los cálculos subyacentes, no asesoramiento de
inversión. Cuando no se pudo calcular ningún valor intrínseco, eso se indica claramente y
los valores afectados se marcan."""
        timing_section = """#### Lecturas de psicología y descubrimiento

Junto con los modelos de valoración, el sitio informa qué ha estado haciendo la multitud:
distancia por debajo del máximo de 3 meses (miedo), distancia respecto al promedio de 50
días y términos de codicia/FOMO, y una lectura de descubrimiento construida a partir del
volumen, el interés de búsqueda, las noticias y los comentarios en redes sociales. Son
mediciones, presentadas como números - el sitio no muestra niveles de entrada, objetivos
ni veredictos de trading."""
    else:
        score_section = f"""#### El Long Score (0–100)

Un número que responde "¿es esta una buena empresa para poseer a este precio?" Combina medidas
de la calidad del negocio, la valoración frente al valor intrínseco, la psicología del mercado,
la atención del mercado{moat_in_list} en un solo puntaje, ponderadas para que ningún factor
domine el resultado.

Por encima de 70 = **STRONG LONG**, por encima de 50 = **LONG**, por encima de 30 = **WATCHLIST**,
de lo contrario **AVOID**. Si no se pudo calcular ningún valor intrínseco en absoluto, la señal
se limita a WATCHLIST - una tesis cuya pata de valor no puede verificarse no recibe una
recomendación completa."""
        timing_section = """#### Valor frente al momento de entrada - dos veredictos separados

La **Señal de Inversión** responde "¿es una buena empresa para poseer?" El **Trade Setup** responde
"¿es ahora mismo una entrada sensata?" - zona de entrada basada en soporte/resistencia, stop loss y
objetivos, condicionados a la seguridad de la tendencia y la relación riesgo/beneficio. Una gran
empresa puede ser una mala entrada hoy; el sitio muestra ambas cosas en lugar de mezclarlas en un
solo veredicto contradictorio."""

    return f"""
Cada herramienta de este sitio ejecuta el mismo motor. Se introduce un ticker; se obtienen datos
en vivo (precios y volúmenes, estados financieros y estimaciones de analistas vía Yahoo Finance,
interés de búsqueda vía Google Trends, titulares vía Yahoo/NewsAPI, comentarios vía StockTwits); y
se ejecuta el mismo marco de inversión en valor cada vez.

{score_section}

#### El Puntaje de Foso (Moat Score, 0–100)

Una lectura aparte de qué tan probable es que la empresa siga siendo tan buena como se ve hoy,
no solo qué tan buena es ahora mismo.{moat_fold_note} Por encima de 70 = **Foso fuerte**,
40–70 = **Foso moderado**, 40 o menos = **Foso débil/sin foso**.

#### Valor intrínseco

El modelo principal es un flujo de caja descontado (DCF) construido a partir de los flujos de
caja libre reportados por la propia empresa, comparado con el precio de hoy para estimar un
valor razonable. Cuando no es posible un DCF completo, se usa una estimación más simple en su
lugar, etiquetada como tal.

Una acción que cotiza significativamente por debajo del valor intrínseco se etiqueta como
**INFRAVALORADA**; significativamente por encima, **CARA**; entre ambos, **JUSTA**.

#### Qué implica el precio (DCF inverso)

Además del DCF estándar anterior, la página Deep Dive también lo ejecuta a la inversa: resuelve
la tasa de crecimiento que haría que el valor razonable del modelo sea igual al precio de hoy.
Esa cifra - la tasa de crecimiento implícita - describe lo que el mercado está fijando en el
precio actualmente, mostrada junto a la tasa de crecimiento que el propio modelo asumió, para
que ambas puedan compararse directamente.

Esto es un cálculo a partir de datos indicados, no un pronóstico: no dice nada sobre si la tasa
de crecimiento implícita es realista, solo cuál es. Cuando el flujo de caja libre base descansa
en una estimación, esta cifra lleva la misma nota de alerta que el Valor Intrínseco del que se
deriva.

{timing_section}

#### La regla de la marca en rojo

Cada vez que un número descansa en un valor predeterminado o un promedio porque no había datos
reales disponibles, se muestra en **rojo**. Una estimación nunca se presenta como un hecho - siempre
sabes qué números están calculados y cuáles son supuestos.

#### Rational Compounder Research

La sección de Research es diferente: no está calculada en absoluto. Es el análisis del propio
cuaderno de trabajo, hecho a mano por el autor, de compounders de calidad seleccionados - una década
de historial de ganancias, cuatro métodos independientes de valor razonable (P/E histórico, P/E
futuro, DCF y un método de patrimonio a 10 años), y juicio escrito al estilo Buffett/Munger sobre la
gestión, el foso y el riesgo. Cada umbral y banda de color en esas páginas proviene de la
investigación original, no de un filtro genérico.

#### Limitaciones, con honestidad

Los datos provienen de fuentes públicas gratuitas y pueden estar retrasados, revisados u
ocasionalmente equivocados. El valor intrínseco es una estimación que descansa en supuestos -
supuestos razonables, mostrados abiertamente, pero supuestos al fin. Los puntajes son resultados de
modelos, no consejos personales, y nada de esto tiene en cuenta tu situación. Úsalo de la forma en
que fue diseñado: como punto de partida para tu propio juicio, no como sustituto de él.
"""


def methodology_md(factual=True, lang="en"):
    """The methodology text as the given presentation sees it - dispatches
    to _methodology_md_en/_es, which read moat_engine.MOAT_IN_VALUE_SCORE
    at call time so this text can never describe a different setup than
    the one actually running. In factual mode (the public default) the
    signal/verdict language is replaced with descriptions of the same
    arithmetic - the public site must not read as a recommendation.

    Moat check, 9 Sep 2026: this used to include the exact factor weights,
    Moat Score pillar points and scoring bands, DCF mechanics and
    reverse-DCF bounds - detailed enough for a competitor to reimplement
    the model from this page alone. Both language variants now describe
    WHAT each score answers, not the formula behind it."""
    moat_blended = moat_engine.MOAT_IN_VALUE_SCORE
    if lang == "es":
        return _methodology_md_es(factual, moat_blended)
    return _methodology_md_en(factual, moat_blended)


ABOUT_FACTUAL_MD_ES = """
StocksDeepDive está creado y administrado por **Andrés Moreno**, un inversor particular en Australia.

No comenzó como un sitio web. Comenzó como un buscador de acciones personal y un libro de Excel muy
extenso - herramientas creadas para estudiar empresas con un enfoque de valor al estilo
Buffett/Munger: calcular cuánto valen los flujos de caja de una empresa según el modelo, evaluar su
calidad a partir de los fundamentos reportados, y observar qué ha estado haciendo el precio. Con los
años, el buscador incorporó un motor de DCF, cálculos de calidad, lecturas de psicología y
descubrimiento, y un cuaderno de investigación que documenta una empresa durante semanas seguidas.

En algún momento surgió la pregunta obvia: ¿por qué no abrir los números al público? Así que este
sitio es eso - el mismo motor, el mismo trabajo con los datos, hecho público.

Dos principios se mantuvieron desde la versión privada, sin cambios:

**Los números deben ser honestos.** Cada vez que una cifra se basa en un valor predeterminado o un
promedio porque no había datos reales disponibles, se muestra en rojo. Una estimación nunca se
presenta como un hecho. Establecí esa regla para mí mismo, porque engañarse a uno mismo sale caro -
se aplica igual ahora que tú también estás leyendo los números.

**El valor y la psicología son mediciones distintas.** Lo que el modelo calcula a partir de los
flujos de caja de una empresa y lo que la multitud ha estado haciendo con su precio se informan como
números separados en cada página. La mayoría de las herramientas los mezclan; este sitio establece
cada uno con claridad y te deja sacar tus propias conclusiones.

El sitio es gratuito mientras se lanza. Cuando se habiliten las suscripciones, los miembros
fundadores conservarán el precio de lanzamiento. Si quieres que se agregue una acción a la lista de
investigación Rational Compounder, o si algo aquí no tiene sentido, usa el botón de Comentarios en
cualquier página de resultados o escribe a
[rationalcompounder@stocksdeepdive.com](mailto:rationalcompounder@stocksdeepdive.com) - leo todo.

*Este sitio presenta solo información objetiva y resultados de la calculadora - no ofrece
asesoramiento sobre productos financieros ni recomendaciones; consulta el aviso legal en el pie de
página. Puede que sea propietario de acciones analizadas aquí.*
"""

ABOUT_FULL_MD_ES = """
StocksDeepDive está creado y administrado por **Andrés Moreno**, un inversor particular en Australia.

No comenzó como un sitio web. Comenzó como un buscador de acciones personal y un libro de Excel muy
extenso - las herramientas que construí para gestionar mi propio fondo de pensión autogestionado con
un enfoque de valor al estilo Buffett/Munger: determinar cuánto vale realmente una empresa, evaluar
su calidad como lo haría un propietario, y solo después mirar qué está haciendo la multitud. Con los
años, el buscador incorporó un motor de DCF, pruebas de calidad, una lectura de psicología de la
multitud, configuraciones de entrada (trade setups) y un cuaderno de investigación que examina una
empresa durante semanas seguidas.

En algún momento surgió la pregunta obvia: si confío estos números con mis propios ahorros de
jubilación, ¿por qué no abrirlos al público? Así que este sitio es eso - el mismo motor, la misma
investigación, hecha pública.

Dos principios se mantuvieron desde la versión privada, sin cambios:

**Los números deben ser honestos.** Cada vez que una cifra se basa en un valor predeterminado o un
promedio porque no había datos reales disponibles, se muestra en rojo. Una estimación nunca se
presenta como un hecho. Establecí esa regla para mí mismo, porque engañarse a uno mismo sale caro -
se aplica igual ahora que tú también estás leyendo los números.

**El valor y el momento de entrada son preguntas distintas.** Si una empresa vale la pena poseerla y
si hoy es un día razonable para comprarla reciben veredictos separados en cada página. La mayoría de
las herramientas los mezclan; mantenerlos separados es la mitad de la disciplina.

El sitio es gratuito mientras se lanza. Cuando se habiliten las suscripciones, los miembros
fundadores conservarán el precio de lanzamiento. Si quieres que se agregue una acción a la lista de
investigación Rational Compounder, o si algo aquí no tiene sentido, usa el botón de Comentarios en
cualquier página de resultados o escribe a
[rationalcompounder@stocksdeepdive.com](mailto:rationalcompounder@stocksdeepdive.com) - leo todo.

*Nada en este sitio constituye asesoramiento financiero - consulta el aviso legal en el pie de
página. Puede que sea propietario de acciones analizadas aquí.*
"""


def about_md(factual=True, lang="en"):
    if lang == "es":
        return ABOUT_FACTUAL_MD_ES if factual else ABOUT_FULL_MD_ES
    return ABOUT_FACTUAL_MD if factual else ABOUT_FULL_MD


# AI-readiness roadmap Phase 10: "public 'How this site uses AI' page
# (computed vs AI-written, always labelled)". A single version, not a
# factual/full pair like methodology_md/about_md above - this page
# describes site INFRASTRUCTURE (which features call an AI model, how
# they're gated, what's computed instead) rather than any stock's
# numbers, so there is no factual-vs-full framing to branch on; the same
# words are accurate in both presentations. Same reason PRIVACY_MD above
# has no factual parameter either.
HOW_AI_IS_USED_MD = """
*Last updated: 31 August 2026*

Short version: every SCORE and NUMBER on this site - Intrinsic Value, Quality,
Psychology, Discovery, Moat, the Value/Long Score, Trade Setup - is **computed**, not
AI-written. A handful of specific features additionally use Claude (Anthropic's AI
model) to turn already-computed numbers into a written paragraph. Every one of those
paragraphs is labelled where it appears, every time, with the words "AI-written
summary of the site's data - not advice."

#### What's computed (never AI)

The DCF, the quality calculation, the psychology and discovery readings, the Moat
Score, the Trade Setup gates and targets - all of it is deterministic arithmetic run
against live market data, exactly as [How the scores work](/methodology) describes.
No AI model ever sees this arithmetic or influences what it outputs. This is true
whether or not any AI feature below is even configured on a given deployment.

#### What's AI-written, and where

- **Ask boxes** (Deep Dive, and My Portfolio for signed-in users) - a grounded answer
  to a typed question, built only from that page's own already-computed numbers.
- **Natural-language screening** (Home, Scanner) - a plain-English query translated
  into the Scanner's own real filters (country, universe, sector); the resolved
  filters are always shown back to you before the results run.
- **Portfolio AI watchdog** - a nightly "what changed" brief for holdings you've
  opted a portfolio into, sent only when something material actually happened.
- **Personalised weekly brief** - the weekly watchlist email's written paragraph,
  covering your own watchlist moves, portfolio health changes, and relevant new
  research from that week.
- **"Explain this number"** - the (i) next to a Deep Dive gauge, explaining what that
  specific figure means for that specific stock.
- **Admin research-note drafting** and **comment spam/abuse triage** - admin-only
  tools that draft a first-pass blog post for review, and flag a pending comment for
  the admin's attention. Neither one publishes or moderates anything by itself - a
  human reviews and clicks Save/Publish, or Approve/Reject, every time.

None of it gives investment advice. Every AI feature is instructed to describe what
the site's own numbers show and to cite which section a claim rests on, never to
recommend buying, holding or selling anything - the same rule the computed scores
themselves follow.

#### Models, and why two

Claude Haiku 4.5 is the default model for every AI feature above. Research-note
drafting and the weekly brief use Claude Sonnet 5 instead, because both call sites
draft longer first-pass text meant for review (an admin drafting one note, or one
brief per subscriber per week) rather than a short grounded answer - low-frequency
enough that the higher per-call cost stays immaterial. Nothing about which model runs
changes what data it's allowed to see or what it's allowed to say.

#### Limits, and why

Using an AI feature requires signing in - free accounts get 20 questions a day; a
Plus subscription (where offered) raises that to 300 a month. A site-wide monthly
spend cap stops all AI features except the owner's own once reached, and every call
is logged (which feature, which model, how many tokens, estimated cost) so usage
stays visible and bounded. "Explain this number" additionally caches each explanation
per stock and metric, so the same figure is only ever explained once, no matter how
many visitors read it.

#### Your data

Nothing from **My Portfolio**, your watchlist, or your email address is ever sent
anywhere except into the grounded prompt for a question you yourself asked (e.g. your
own Portfolio Ask, or your own watchdog brief) - never exposed through the public
snapshot pages, the JSON API, or the MCP server, and never used to train any model.
See the [privacy policy](/privacy) for the site's full data-handling practices; this
page only covers what the AI features specifically do and don't do with what they see.
"""


# Español instruction, Part 2: ES translations of the two remaining
# content pages, which (unlike methodology/about above) had no
# factual/full split to begin with - one ES constant each, same
# EN-fallback-via-lang-param contract as methodology_md()/about_md().
PRIVACY_MD_ES = """
*Última actualización: 13 de agosto de 2026*

StocksDeepDive ("el sitio", "nosotros") es operado por Andrés Moreno en Australia. Esta página
explica qué información maneja el sitio y qué sucede con ella. Contacto para cualquier tema
relacionado con la privacidad:
[rationalcompounder@stocksdeepdive.com](mailto:rationalcompounder@stocksdeepdive.com).

#### Qué recopilamos

**Nada, para la navegación anónima.** Puedes usar todas las herramientas de análisis sin una
cuenta. Nuestro proveedor de alojamiento (Railway) conserva registros técnicos estándar (dirección
IP, tipo de navegador, páginas solicitadas) por motivos de seguridad y depuración, como en
cualquier sitio web.

**Si inicias sesión con Google:** recibimos tu nombre y dirección de correo electrónico de Google -
nada más. El inicio de sesión existe para que el sitio pueda recordar tu lista de seguimiento,
atribuir tus comentarios y (si guardas una lista de seguimiento) enviarte el resumen semanal por
correo electrónico. Nunca vemos tu contraseña de Google.

**Si guardas una lista de seguimiento:** los tickers que guardas se almacenan junto a tu dirección
de correo electrónico en nuestro servidor.

**Si envías comentarios:** tu mensaje y, si has iniciado sesión, tu dirección de correo electrónico
se almacenan para que podamos hacer seguimiento.

**Si las suscripciones están activas y te suscribes:** el pago lo gestiona íntegramente Stripe.
Nunca vemos ni almacenamos los datos de tu tarjeta - solo consultamos con Stripe si tu correo
electrónico tiene una suscripción activa.

#### Qué no hacemos

Sin publicidad, sin rastreadores publicitarios, sin análisis de terceros ni tecnología publicitaria,
y sin vender ni compartir tu información con nadie, nunca. Las únicas cookies utilizadas son las
necesarias para mantener tu sesión iniciada.

#### Análisis de páginas

Mantenemos conteos agregados y propios de visitas por página (qué páginas se visitan, cuántas
veces, por día) para poder ver qué resulta útil - no se establecen cookies para esto, no participan
rastreadores de terceros ni tecnología publicitaria, y no se almacena ninguna identidad por
visitante junto con una visita.

#### Correos electrónicos

El resumen semanal se envía (mediante Mailgun) únicamente a usuarios que han iniciado sesión y que
han guardado una lista de seguimiento. Para detenerlo, elimina todas las acciones de tu lista de
seguimiento, o escríbenos y te daremos de baja.

#### Retención y eliminación de datos

Las listas de seguimiento y los comentarios se conservan mientras tu cuenta esté activa. Escríbenos
desde tu dirección de inicio de sesión y eliminaremos todo lo que tengamos sobre ti.

#### Datos de terceros en el sitio

Los datos de mercado mostrados en el sitio provienen de fuentes de terceros (Yahoo Finance, Google
Trends, StockTwits, NewsAPI, GDELT). Esos servicios reciben solicitudes estándar de nuestro
servidor, no información sobre ti.

#### Cambios

Si esta política cambia, la fecha indicada arriba cambiará con ella. Los cambios importantes se
indicarán en el sitio.
"""


HOW_AI_IS_USED_MD_ES = """
*Última actualización: 31 de agosto de 2026*

En resumen: todos los PUNTAJES y NÚMEROS de este sitio - Valor Intrínseco, Calidad, Psicología,
Descubrimiento, Foso, el Puntaje Value/Long, Trade Setup - son **calculados**, no escritos por IA.
Un puñado de funciones específicas además usan Claude (el modelo de IA de Anthropic) para convertir
números ya calculados en un párrafo escrito. Cada uno de esos párrafos está etiquetado donde
aparece, siempre, con las palabras "Resumen escrito por IA a partir de los datos del sitio - no es
un consejo."

#### Qué es calculado (nunca IA)

El DCF, el cálculo de calidad, las lecturas de psicología y descubrimiento, el Puntaje de Foso, las
condiciones y objetivos del Trade Setup - todo es aritmética determinista ejecutada sobre datos de
mercado en vivo, exactamente como describe [Cómo funcionan los puntajes](/es/methodology). Ningún
modelo de IA ve jamás esta aritmética ni influye en lo que produce. Esto es así tanto si alguna
función de IA a continuación está configurada en un despliegue dado como si no.

#### Qué está escrito por IA, y dónde

- **Cuadros de preguntas (Ask boxes)** (Deep Dive, y Mi Cartera para usuarios con sesión iniciada) -
  una respuesta fundamentada a una pregunta escrita, construida solo a partir de los números ya
  calculados de esa página.
- **Filtrado en lenguaje natural** (Inicio, Buscador) - una consulta en lenguaje sencillo traducida
  a los filtros reales del Buscador (país, universo, sector); los filtros resueltos siempre se
  muestran antes de ejecutar los resultados.
- **Vigilancia de IA de la cartera** - un resumen nocturno de "qué cambió" para las posiciones que
  has incluido en una cartera, enviado solo cuando ocurrió algo realmente relevante.
- **Resumen semanal personalizado** - el párrafo escrito del correo semanal de la lista de
  seguimiento, que cubre los movimientos de tu propia lista, los cambios en la salud de tu cartera y
  la investigación nueva relevante de esa semana.
- **"Explica este número"** - la (i) junto a un indicador de Deep Dive, que explica qué significa
  esa cifra específica para esa acción específica.
- **Redacción de notas de investigación (admin)** y **triaje de comentarios spam/abusivos** -
  herramientas solo para administradores que redactan un primer borrador de una entrada de blog
  para revisión, y marcan un comentario pendiente para la atención del administrador. Ninguna de las
  dos publica ni modera nada por sí sola - una persona revisa y hace clic en Guardar/Publicar, o
  Aprobar/Rechazar, en cada caso.

Nada de esto ofrece asesoramiento de inversión. A cada función de IA se le indica que describa lo
que muestran los propios números del sitio y que cite en qué sección se basa una afirmación, nunca
que recomiende comprar, mantener o vender nada - la misma regla que siguen los propios puntajes
calculados.

#### Modelos, y por qué dos

Claude Haiku 4.5 es el modelo predeterminado para cada función de IA mencionada arriba. La
redacción de notas de investigación y el resumen semanal usan Claude Sonnet 5 en su lugar, porque
ambos casos redactan texto de primer borrador más largo, pensado para revisión (un administrador
redactando una nota, o un resumen por suscriptor por semana) en lugar de una respuesta breve y
fundamentada - con una frecuencia lo bastante baja como para que el mayor costo por llamada resulte
insignificante. Qué modelo se ejecuta no cambia en nada qué datos puede ver ni qué puede decir.

#### Límites, y por qué

Usar una función de IA requiere iniciar sesión - las cuentas gratuitas obtienen 20 preguntas al
día; una suscripción Plus (donde esté disponible) eleva eso a 300 al mes. Un límite de gasto
mensual para todo el sitio detiene todas las funciones de IA excepto las del propio administrador
una vez alcanzado, y cada llamada queda registrada (qué función, qué modelo, cuántos tokens, costo
estimado) para que el uso se mantenga visible y acotado. "Explica este número" además almacena en
caché cada explicación por acción y métrica, de modo que la misma cifra solo se explica una vez, sin
importar cuántos visitantes la lean.

#### Tus datos

Nada de **Mi Cartera**, tu lista de seguimiento o tu dirección de correo electrónico se envía jamás
a ningún lado excepto al mensaje fundamentado de una pregunta que tú mismo hiciste (por ejemplo, tu
propia pregunta en Cartera o tu propio resumen de vigilancia) - nunca se expone a través de las
páginas públicas de instantáneas, la API JSON o el servidor MCP, y nunca se usa para entrenar ningún
modelo. Consulta la [política de privacidad](/es/privacy) para conocer las prácticas completas de
manejo de datos del sitio; esta página solo cubre lo que las funciones de IA específicamente hacen y
no hacen con lo que ven.
"""


def privacy_md(lang="en"):
    return PRIVACY_MD_ES if lang == "es" else PRIVACY_MD


def how_ai_is_used_md(lang="en"):
    return HOW_AI_IS_USED_MD_ES if lang == "es" else HOW_AI_IS_USED_MD

