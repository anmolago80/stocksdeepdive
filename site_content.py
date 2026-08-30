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

def _methodology_factual_swaps(moat_blended):
    """Built per-call (not a static list) because two of these pairs quote
    the factor count in the non-factual source text, and that count itself
    depends on moat_engine.MOAT_IN_VALUE_SCORE - five factors when Moat is
    blended into the Value Score, four when it isn't (same "N calculations"
    fix as the METHODOLOGY_MD intro line just above). Mirrors the existing
    two-variant Psychology-row pattern below, which already handles the
    weight itself (20% vs 15%) changing the same way."""
    factor_word = "five" if moat_blended else "four"
    return [
        # Verdict bands paragraph -> value-score description
        ("Above 70 = **STRONG LONG**, above 50 = **LONG**, above 30 = **WATCHLIST**, otherwise\n**AVOID**. If no intrinsic value could be computed at all, the signal is capped at\nWATCHLIST - a thesis whose value leg can't be verified doesn't get a full\nrecommendation.",
         f"On this site the number is displayed as the **Value Score** - a weighted\ndescription of the {factor_word} calculations above, shown without signal labels or\nrecommendations. Where no intrinsic value could be computed, that is stated\nplainly and the affected values are marked."),
        ("The Long Score (0\u2013100) and Investment Signal", "The Value Score (0\u2013100)"),
        # Score heading + intro question -> neutral description
        (f"#### The Long Score (0\u2013100)\n\nOne number answering \"is this a good business to own at this price?\" It blends {factor_word}\nfactors, each clamped to a fixed band first so no single factor can run away with the\nresult:",
         f"#### The Value Score (0\u2013100)\n\nOne number summarising {factor_word} calculations, each clamped to a fixed band first so no\nsingle factor can run away with the result:"),
        # Psychology row: drop the advice-flavoured sentence, keep the maths
        ("| Psychology | 20% | Which way is the crowd leaning? Fear minus greed minus FOMO, read from price behaviour. Fear scores positively - the value investor's edge is buying quality when others are anxious. |",
         "| Psychology | 20% | Which way is the crowd leaning? Fear minus greed minus FOMO, read from price behaviour; fear enters the formula with a positive sign. The sign convention is part of the stated arithmetic, not a recommendation. |"),
        ("| Psychology | 15% | Which way is the crowd leaning? Fear minus greed minus FOMO, read from price behaviour. Fear scores positively - the value investor's edge is buying quality when others are anxious. |",
         "| Psychology | 15% | Which way is the crowd leaning? Fear minus greed minus FOMO, read from price behaviour; fear enters the formula with a positive sign. The sign convention is part of the stated arithmetic, not a recommendation. |"),
        # Trade Setup / two-verdicts section -> psychology-readings description
        ("#### Value vs timing - two separate verdicts\n\nThe **Investment Signal** answers \"good business to own?\" The **Trade Setup** answers\n\"is right now a sane entry?\" - support/resistance-based entry zone, stop loss and\ntargets, gated on trend safety and risk/reward. A great company can be a poor entry\ntoday; the site shows both rather than blurring them into one contradictory verdict.",
         "#### Psychology and discovery readings\n\nAlongside the valuation models, the site reports what the crowd has been doing:\ndistance below the 3-month high (fear), distance from the 50-day average and greed/\nFOMO terms, and a discovery reading built from volume, search interest, news and\nsocial chatter. These are measurements, stated as numbers - the site does not\ndisplay entry levels, targets or trade verdicts."),
    ]

# Shown above the methodology text in the public (factual) presentation.
METHODOLOGY_FACTUAL_NOTE = (
    "**Presentation note.** This site displays data, model outputs and "
    "described calculations from stated inputs. It does not provide "
    "financial product advice or recommendations - descriptions below "
    "of how each calculation works are exactly that: descriptions of "
    "arithmetic, not guidance on what to do."
)

METHODOLOGY_MD = """
Every tool on this site runs the same engine. A ticker goes in; live data comes back
(prices and volumes, financial statements and analyst estimates via Yahoo Finance, search
interest via Google Trends, headlines via Yahoo/NewsAPI, chatter via StockTwits); and the
same value-investing maths runs every time. Nothing on this page is a black box - every
score's inputs are charted right next to it on the site.

#### The Long Score (0–100)

One number answering "is this a good business to own at this price?" It blends @@FACTOR_COUNT@@
factors, each clamped to a fixed band first so no single factor can run away with the
result:

@@LONG_SCORE_TABLE@@

Above 70 = **STRONG LONG**, above 50 = **LONG**, above 30 = **WATCHLIST**, otherwise
**AVOID**. If no intrinsic value could be computed at all, the signal is capped at
WATCHLIST - a thesis whose value leg can't be verified doesn't get a full
recommendation.

#### Moat Score (0–100)

Quality (above) measures how good the business is **right now** - today's margins,
returns and growth. Moat is a different question: how likely is the business to
**stay** that good, over time? @@MOAT_FOLD_NOTE@@ Four pillars, each computed from
the company's own multi-year statement history:

| Pillar | Weight | What it measures |
|---|---|---|
| Excess-return spread | 30 pts | TTM return on capital (ROIC, or ROE for banks/insurers) minus the cost of that capital (WACC, or cost of equity for financials). ≤0% spread scores 0, 0–5% scores 10, 5–15% scores 20, above 15% scores the full 30. |
| Persistence | 25 pts | The share of available fiscal years in which return on capital cleared 12%. Capped at 20/25 while fewer than 8 years of statement history are on file - the full 25-point read needs real multi-year depth. |
| Pricing power | 25 pts | The gross-margin trend (falls back to operating margin, flagged, if no Gross Profit line is reported): held or expanded margin (10 pts), stability - low year-to-year variance (10 pts), and margin holding up while revenue actually grew (5 pts). |
| Reinvestment | 20 pts | Incremental return on newly-deployed capital - the change in after-tax operating profit versus the change in invested capital, oldest available year to newest. A capital-light business that shrinks invested capital while holding or growing profit scores 16 here directly. |

Above 70 = **Strong moat**, 40–70 = **Moderate moat**, 40 and below = **Weak/no
moat** - the same colour bands (green/amber/red) used everywhere else on the site.

**Erosion overlay.** Independently of the score above, if TTM return on capital and
operating margin have both fallen meaningfully below their own recent multi-year
average - return down more than 20% in relative terms, margin down more than 3
points - the read becomes a **moat watch** caption (no score penalty). If that same
weakness holds across the two most recent multi-year windows in a row, the read
becomes **eroding**, and the score itself is capped at 50: a business can't read as
a "strong moat" while its own numbers are actively deteriorating, however good its
history looks. A single weak window is a caption, not a cap - a real business has
uneven years.

**Missing data is dropped, not guessed at.** If a pillar can't be computed from the
data on file, it's left out entirely and the remaining pillars are reweighted to
100 - never defaulted to a neutral or average score. The Deep Dive page's Moat
section always shows how many pillars were actually used.

Funds and ETFs don't get a Moat Score at all (a moat is a property of an operating
business, not a basket of one) - shown as **N/A (fund)**. A ticker with fewer than
two usable years of statement history also shows **N/A**, plainly, rather than a
score built on too little to mean anything.

#### Intrinsic value

The primary model is a discounted cash flow built from the company's own reported free
cash flows. The discount rate is calculated per stock (CAPM - the stock's own beta
against its market), growth comes from analyst consensus where available, then the
company's own historical FCF growth, and the terminal growth rate is set by the stock's
currency. Where a DCF isn't possible, a P/E-blend fallback is used and labelled as such.
Margin of Safety = (intrinsic value − price) ÷ intrinsic value.

A stock trading 25%+ below intrinsic value is labelled **UNDERVALUED**; above intrinsic
value, **EXPENSIVE**; between, **FAIR**.

#### Value vs timing - two separate verdicts

The **Investment Signal** answers "good business to own?" The **Trade Setup** answers
"is right now a sane entry?" - support/resistance-based entry zone, stop loss and
targets, gated on trend safety and risk/reward. A great company can be a poor entry
today; the site shows both rather than blurring them into one contradictory verdict.

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


def methodology_md(factual=True):
    """The methodology text as the given presentation sees it. In factual
    mode the signal/verdict language is swapped for descriptions of the
    same arithmetic - the public site must not read as a recommendation.

    The Long/Value Score weight table and the Moat section's fold-in note
    are resolved here at call time from moat_engine.MOAT_IN_VALUE_SCORE,
    mirroring app.py's page_methodology() so this crawled/static copy of
    the page can never describe a different formula than the one actually
    running."""
    moat_blended = moat_engine.MOAT_IN_VALUE_SCORE
    if moat_blended:
        long_score_table = """| Factor | Weight | What it measures |
|---|---|---|
| Quality | 25% | Is this a good business? Return on equity, profit margin, revenue and earnings growth, free cash flow, debt - computed from the company's own fundamentals. Loss-making, cash-burning businesses are capped: a company that doesn't make money can't score as "high quality" no matter how fast it grows. |
| Moat | 15% | How likely is the business to **stay** that good, not just how good it is today - the Moat Score described below, folded in directly. If no Moat Score can be computed (funds/ETFs, or too little statement history), this weight is dropped and the other four factors are reweighted proportionally to fill the gap - never defaulted to zero. |
| Margin of Safety | 30% | Is the price below the value? The gap between our intrinsic-value estimate and today's price, clamped to ±50 so a wild discount (or premium) can move the score but never dominate it. |
| Psychology | 15% | Which way is the crowd leaning? Fear minus greed minus FOMO, read from price behaviour. Fear scores positively - the value investor's edge is buying quality when others are anxious. |
| Discovery | 15% | Is the market noticing? Price activity, unusual volume, search trends, news flow and social chatter - attention only, deliberately separate from sentiment. |"""
        moat_fold_note = (
            "It's shown separately on the Deep Dive page, the Side-by-side comparison and "
            "the Scanner, **and it is folded directly into the Value Score above** at a 15% "
            "weight (see the weight table there) - dropped and the remaining factors "
            "reweighted, never defaulted to zero, on any ticker it can't be computed for."
        )
    else:
        long_score_table = """| Factor | Weight | What it measures |
|---|---|---|
| Quality | 35% | Is this a good business? Return on equity, profit margin, revenue and earnings growth, free cash flow, debt - computed from the company's own fundamentals. Loss-making, cash-burning businesses are capped: a company that doesn't make money can't score as "high quality" no matter how fast it grows. |
| Margin of Safety | 25% | Is the price below the value? The gap between our intrinsic-value estimate and today's price, clamped to ±50 so a wild discount (or premium) can move the score but never dominate it. |
| Psychology | 20% | Which way is the crowd leaning? Fear minus greed minus FOMO, read from price behaviour. Fear scores positively - the value investor's edge is buying quality when others are anxious. |
| Discovery | 20% | Is the market noticing? Price activity, unusual volume, search trends, news flow and social chatter - attention only, deliberately separate from sentiment. |"""
        moat_fold_note = (
            "It's shown separately on the Deep Dive page, the Side-by-side comparison and "
            "the Scanner - it is not currently folded into the Value Score above."
        )
    text = METHODOLOGY_MD.replace("@@LONG_SCORE_TABLE@@", long_score_table)
    text = text.replace("@@MOAT_FOLD_NOTE@@", moat_fold_note)
    text = text.replace("@@FACTOR_COUNT@@", "five" if moat_blended else "four")
    if factual:
        for old, new in _methodology_factual_swaps(moat_blended):
            text = text.replace(old, new)
    return text


def about_md(factual=True):
    return ABOUT_FACTUAL_MD if factual else ABOUT_FULL_MD
