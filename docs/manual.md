# StocksDeepDive — Complete User Manual

stocksdeepdive.com · Version of 31 August 2026 · supersedes the 30 August 2026 edition

This manual describes every page, every table, every score and every colour on the public
site, exactly as deployed. Nothing here is left to interpretation: each formula is written
out, each threshold is stated, and each colour is defined. It replaces the 13 August 2026
edition, which had fallen behind the site in several places — most importantly, it described
the entry-timing tools (Investment Signal, Trade Setup, buy/stop/target levels) as public
features. They are not: on the live public site those are computed and used internally, but
never shown. What a signed-out or signed-in (non-admin) visitor actually sees is a **Value
Score** and a set of neutral, described calculations — no buy/sell verdicts. This edition
documents the site exactly as a real visitor experiences it, with the owner's own admin-only
tools broken out into a separate appendix (§12) rather than presented as public features.

---

## 1. Site structure and navigation

The site has ten Streamlit pages, plus a statically-served blog that sits outside the
Streamlit app entirely:

| URL | Page | Purpose |
|---|---|---|
| `/` | Home | Landing page: search, featured analysis, market strip, feature overview |
| `/deep-dive` | Stock Deep Dive | Complete analysis of one ticker |
| `/comparison` | Side-by-side Comparison | Two or more tickers on identical criteria |
| `/scanner` | Stock Scanner | Rank a whole index (ASX 200, S&P 500, etc.) |
| `/research` | Rational Compounder Analysis | The author's hand-built workbook research |
| `/portfolio` | My Portfolio | Your own tracked holdings (sign-in required) |
| `/methodology` | How the scores work | Plain-English methodology reference |
| `/model-history` | Model history | A changelog of Rational Compounder research rebuilds |
| `/about` | About | Who runs the site and why |
| `/privacy` | Privacy policy | What data the site handles |
| `/blog`, `/blog/<slug>` | Blog | Articles, served as static HTML — not a Streamlit page, and not part of the header nav (footer link only) |

A tenth Streamlit page, `/blog-admin`, exists to write and manage blog posts — it is
admin-only and excluded from search-engine indexing (`robots.txt`); it is not part of the
public site and isn't covered further in this manual.

**The search box is the primary navigation.** It appears at the top of every page. The rule
is simple and absolute: type one ticker and press Search (or Enter) for a Deep Dive; type two
or more tickers — separated by spaces, commas or new lines — for a Comparison. ASX tickers
use the `.AX` suffix (`CSL.AX`); US tickers use none (`AAPL`); the two can be mixed freely in
one comparison. Duplicate tickers are ignored, case does not matter.

Below the search box sit four buttons, in this order: **Rational Compounder Analysis**,
**Side-by-side Comparison**, **Stock Scanner**, **My Portfolio**. These open the pages
directly — My Portfolio itself handles showing a sign-in prompt if you aren't signed in,
rather than gating the click.

**Example chips.** On the home page and on Deep Dive/Comparison empty states, clickable chips
run an example search with one click.

**Shareable URLs.** Results pages keep the address bar in sync with what's on screen —
`stocksdeepdive.com/deep-dive?ticker=CSL.AX` opens directly into a live CSL analysis, and
`stocksdeepdive.com/comparison?tickers=CSL.AX,BHP.AX` runs that comparison directly. Copying
the address bar at any time captures the current view.

**Clicking the StocksDeepDive logo** returns to the home page from anywhere.

**The "RC view" control**, if you notice it in the top corner near Sign Out, is the site
owner's own admin unlock (an access-key prompt) — it switches that one browser into the full
admin view described in §12. It has no function for anyone without the key, and entering the
wrong one simply shows "Incorrect key."

**On your phone.** The site can be installed as an app: open it in Safari on iPhone, tap
Share, then "Add to Home Screen" — it then opens full-screen from its own icon, with no
address bar. Chrome on Android offers the same via an "Install app" prompt. Once installed,
if you're signed in you can also turn on push notifications for a followed company's research
updates (see §9) — that needs the app to be installed first; a plain browser tab can't receive
them on iPhone.

---

## 2. The home page

From top to bottom:

**Market tape.** A scrolling strip of live quotes (ASX 200, S&P 500, NASDAQ, AUD/USD, plus
sample stocks) with today's percentage move, coloured green for a gain and red for a loss. If quotes can't
be fetched, the tape simply doesn't render.

**Account bar.** Sign In when signed out; your name, a feedback button and Sign out when
signed in. See §9 for what signing in adds and how sign-in actually works.

**Hero and search.** The headline, the search box with an Analyze button, the example chips,
and the usage rule ("One ticker = full Deep Dive · Two or more = side-by-side Comparison").

**Featured analysis card.** A live, attention-lite Deep Dive of one stock, rotating daily
through a fixed list and cached for several hours. It shows the Value Score on a
colour-coded gauge, intrinsic value, margin of safety, Quality Score, and a price sparkline
against the 50-day average. The button beneath opens the full Deep Dive.

**Your watchlist** (signed-in users with saved stocks only): a chip row of your saved
tickers — one click re-runs any of them.

**Market mood strip.** Up to four tiles: AU and US market mood (Hopeful / Neutral / Anxious —
a rolling reading of news tone for that country; informational only, it feeds no score) and
index levels with today's move. A tile that can't be fetched is silently omitted and returns
when the source recovers.

Feature cards, how-it-works, coverage cards and the launch banner complete the page.

---

## 3. The scoring engines (used identically everywhere)

Every tool on the site runs the same engine. Live data comes from Yahoo Finance (prices,
volumes, fundamentals, analyst estimates), Google Trends (search interest), NewsAPI + Yahoo
news (headlines) and StockTwits (social chatter); statement history depth depends on the data
source in use — a handful of years (typically 4–5) from Yahoo by default, or up to about ten
years when the site is additionally configured with an EODHD data key (not currently set on
the production deployment, so live figures today run on the shorter Yahoo-only history).

### 3.1 Quality Score (0–100)

*"Is this a good business?"* — computed from the company's own fundamentals:

```
Quality = 50 + ROIC × 100 × 0.25 + ROE × 100 × 0.20 + Profit Margin × 100 × 0.15
             + Revenue Growth × 100 × 0.15 + Earnings Growth × 100 × 0.15
             + 10 if free cash flow positive (−5 if negative)
             − min(Debt/Equity × 0.05, 15)   [debt penalty, capped]
```

- **ROIC** (return on invested capital) is the lead metric — profit per dollar of *total*
  capital (equity and debt), so a business can't look high-quality on ROE alone by borrowing
  heavily. Revenue and earnings growth are clamped to ±50% before weighting, so a +300%
  figure off a tiny base can't max the score.
- **Profitability gate:** a loss-making or cash-burning business (negative margin, net income
  or free cash flow) is capped at **55** regardless of growth.
- The final score is clamped to 0–100. If no fundamental data was available at all, the score
  defaults to 50 and is rendered in red (see the red-flag rule, §3.7).

### 3.2 Intrinsic value and Margin of Safety

*"What is it worth?"* The valuation ladder, in order:

1. **DCF** — used whenever free cash flow is positive. The base cash flow is the latest
   reported year, adjusted for average capital expenditure so one unusually heavy investment
   year doesn't collapse the estimate; if that figure still deviates by more than 40% from the
   median of the last few years, the median is used instead as a smoothing step.
2. **P/E blend** fallback when a DCF isn't possible — labelled as such in the "Val Method"
   fields.
3. **N/A** when neither works (typically financials, or no positive EPS/FCF).

The **growth rate** feeding the DCF isn't a simple priority list — where both an analyst
5-year consensus estimate and a "clean" multi-year historical FCF growth rate are available,
the *lower* of the two is used (a conservative blend, not a straight pick of one over the
other); otherwise whichever of the two is available is used alone; failing that, the
company's own reported growth figures; failing that, a flagged default. Whatever the source,
growth is capped by the company's own market-cap tier — roughly 8% for mega caps, rising to
20% for small caps — so a small, fast grower isn't artificially capped at a large company's
pace, and a mega-cap outlier estimate can't run away with the model.

The **discount rate** is CAPM-based — the stock's own beta against its market's risk-free
rate and equity risk premium — floored at 7.5% (and the beta feeding it floored at 0.6) and
capped at 15%, since a measured cost of capital or beta below that usually reflects a data
artefact rather than a genuinely low-risk business. Terminal growth is set by the stock's
currency.

```
Margin of Safety (MOS) = (Intrinsic Value − Price) / Intrinsic Value × 100
```

**Valuation label:** MOS ≥ 25 → **UNDERVALUED** · MOS < 0 → **EXPENSIVE** · between → **FAIR**
· no intrinsic value → **N/A**.

Per-stock DCF overrides (growth / discount / terminal) can be typed into the DCF Parameters
table on Comparison and Scanner (§5, §6) by any visitor. Overrides live only in your own
browser session, reset when you leave, and never affect any other visitor.

### 3.3 Psychology Score

*"Which way is the crowd leaning?"* — computed from 3 months of price behaviour:

```
Fear  = (3-month High − Price) / High × 100        [distance below the recent high]
Greed = max((Price − MA50) / MA50 × 100, 0)         [stretch above the 50-day average]
FOMO  = Greed + max(weekly change, 0)
Psychology = Fear − Greed − FOMO
```

Higher = fearful (contrarian-friendly); lower = greedy/overheated. The Sentiment label reads
directly off it: > 20 **FEARFUL** · > 5 **CALM** · < −20 **OVERHEATED** · < −5 **GREEDY** ·
else **NEUTRAL**.

### 3.4 Discovery Score

*"Is the market noticing this stock?"* — pure attention, deliberately containing no
sentiment:

```
Discovery = Activity + Volume Ratio × 10 + Trend Score + News Score + Social Score
```

where Activity = |weekly change|, Volume Ratio = latest volume ÷ 3-month average volume,
Trend = Google search interest, News = NewsAPI + Yahoo headline scoring, Social = StockTwits
message volume and bull/bear balance. In **attention-lite** contexts (overnight scans, live
scans over 100 tickers, the featured card and the weekly digest) the Trend/News/Social terms
are skipped and Discovery reflects price and volume only — always labelled where it applies.

### 3.5 Moat Score (0–100) — new

Quality (above) measures how good the business is **right now**. Moat is a different
question: how likely is it to **stay** that good, over time? It's shown on the Deep Dive
page and, where a value has already been computed and stored, on the Side-by-side comparison
and Stock Scanner tables — and, with `MOAT_IN_VALUE_SCORE` set (see §13), it is also folded
directly into the Value Score below at a 15% weight. Four pillars, each computed from the
company's own multi-year statement history:

| Pillar | Weight | What it measures |
|---|---|---|
| Excess-return spread | 30 pts | TTM return on capital (ROIC, or ROE for banks/insurers) minus the cost of that capital. ≤0% spread scores 0, 0–5% scores 10, 5–15% scores 20, above 15% scores the full 30. |
| Persistence | 25 pts | The share of available fiscal years in which return on capital cleared 12%. Capped at 20/25 while fewer than 8 years of statement history are on file. |
| Pricing power | 25 pts | The gross-margin trend (falls back to operating margin, flagged, if no Gross Profit line is reported): held/expanded margin (10 pts), stability (10 pts), and margin holding up while revenue actually grew (5 pts). |
| Reinvestment | 20 pts | Incremental return on newly-deployed capital, oldest available year to newest. A capital-light business that shrinks invested capital while holding or growing profit scores 16 here directly. |

Above 70 = **Strong moat**, 40–70 = **Moderate moat**, 40 and below = **Weak/no moat**.

An **erosion overlay** runs independently of the score: if return on capital and operating
margin have both fallen meaningfully below their own recent multi-year average, the read
becomes a "moat watch" caption (no score penalty); if that holds across the two most recent
multi-year windows in a row, the read becomes "eroding" and the score itself is capped at 50.

If a pillar can't be computed from the data on file, it's dropped and the remaining pillars
reweighted to 100 — never defaulted to a neutral score. Funds/ETFs don't get a Moat Score at
all ("N/A (fund)"); a ticker with fewer than two usable statement years also shows "N/A."

### 3.6 The Value Score (0–100)

The headline number on the public site. Every component is clamped to a fixed band first so
no single factor can dominate. Which formula runs depends on the `MOAT_IN_VALUE_SCORE`
environment variable (§13) — currently **set (on)**, so Moat is folded in:

```
With MOAT_IN_VALUE_SCORE set, and a Moat Score available (current production formula):
Value Score = Quality × 0.25 + Moat × 0.15 + MOS clamped to ±50 × 0.30
             + Psychology clamped to ±50 × 0.15 + Discovery clamped to 0–100 × 0.15

With MOAT_IN_VALUE_SCORE set, but no Moat Score available for this ticker
(funds/ETFs, or too little statement history) — Moat's 15% weight is dropped
and the other four are reweighted proportionally (divided by 0.85, i.e. each
factor's blended-formula weight ÷ 0.85), never defaulted to zero:
Value Score = Quality × 0.2941 + MOS clamped to ±50 × 0.3529
             + Psychology clamped to ±50 × 0.1765 + Discovery clamped to 0–100 × 0.1765

Without MOAT_IN_VALUE_SCORE set at all (the formula used before Moat existed;
kept in the code as mode="current" but not the one running in production today):
Value Score = Quality × 0.35 + MOS clamped to ±50 × 0.25
             + Psychology clamped to ±50 × 0.20 + Discovery clamped to 0–100 × 0.20
```

The middle formula is arithmetically distinct from the third — it is not a fallback to the
pre-Moat formula, just the same blended weights with Moat's share folded back into the other
four in proportion to their own size. The clamps affect only the score's arithmetic; tables
always display the true, unclamped MOS/Psychology/Discovery values. This is the same
underlying calculation the site's own code calls the "Long Score" internally (computed there
via a `mode="moat_blend"` argument when the switch is on), and the same number the site's
owner sees paired with an Investment Signal label in the admin view — see §12. On the public
site it is shown purely as a descriptive number ("Value Score: 48.9 — FAIR" and similar),
with no buy/hold/avoid label attached.

### 3.7 The red-flag rule

Wherever a number rests on a default or average because real data was unavailable, it is
rendered in **red** — in every table, on every page. A red Quality of 50 means "no
fundamentals were available; this is the base assumption," not a computed result. An estimate
is never displayed as a fact.

---

## 4. Stock Deep Dive (`/deep-dive`)

Search one ticker to reach it. From top to bottom:

1. **Headline metrics:** Price, Intrinsic Value, MOS, Value Score.
2. **Follow / Watchlist:** a follow toggle lets any reader — signed in or not — leave just an
   email address to be notified when this stock's Rational Compounder research changes (a
   lighter commitment than the full watchlist below); signed-in users additionally get an
   Add/Remove watchlist button for this stock.
3. **Price chart:** the last 6 months of closes, the 50-day moving average, and the 3-month
   high/low context that Psychology is computed from.
4. **The plain-English case:** Why Buy / Why Wait / Risks in three columns, plus a suggested
   holding period.
5. **Price vs Intrinsic Value** bar chart and the **Value Score gauge** with its threshold
   caption.
6. **"What's driving the Value Score"** — each factor's exact point contribution.
7. **Quality** (gauge + driving-terms chart, including the ROIC term), **Psychology** (gauge +
   Fear/Greed/FOMO chart), **Discovery** (gauge + attention-terms chart), and **Moat** (gauge
   + driving-pillars chart, with an erosion caption when it applies — see §3.5).
8. **Compounder View (auto)** — a collapsed research section mirroring the Rational
   Compounder Analysis page's own layout (Fundamentals, Value vs Book, Retained Earnings,
   Earnings Trends, Cost of Capital, Fair Value), but computed live from this ticker's own
   reported statements and price history instead of drawn from the author's hand-built
   workbook. Metrics are coloured against the same threshold bands the workbook research
   uses; anything resting on an estimate or fallback is flagged red, same convention as
   everywhere else on the site. "Fair Value" here uses the same preview gate as the workbook
   version (see §7).

A position-disclosure line (whether the site's author personally holds, has never held, or
has closed a position in this stock) appears on Research-style pages — see §9.

---

## 5. Side-by-side Comparison (`/comparison`)

Search two or more tickers. Every stock is fetched and scored live, with a progress bar (a
warning shows a realistic time estimate whenever more than 25 tickers were requested). All
tables share one visual language:

- **Infill bars** — a value above a coloured bar whose fill width is the value and whose
  colour is its band (see the full colour reference, §10).
- **Verdict pills** — coloured labels: green = UNDERVALUED / FEARFUL / Uptrend; amber = FAIR /
  GREEDY / Ranging; red = EXPENSIVE / OVERHEATED / Downtrend; grey = CALM / NEUTRAL / N/A.
- **Price-relative colouring** — Intrinsic Value is green when above the current price, red
  when below.
- **Red values** — the red-flag rule (§3.7) overrides all other colouring.

The tables, in order:

**5.1 Comparison preview** — Ticker, Price, Value Score for every stock scanned.

**5.2 Side-by-side comparison** — the main table, one row per ticker in the order you entered
them. Columns: Ticker · Type (pill) · Price · Intrinsic Value (green/red vs price) · MOS (bar)
· Value Score (bar) · Quality (bar) · Psychology (bar) · Discovery (bar) · Moat (bar) ·
Valuation (pill) · Sentiment (pill) · Trend (pill). The Moat column shows only a value already
computed and stored by an earlier nightly scan or Deep Dive view of that ticker — it is never
computed live in this table, since a fresh multi-year fundamentals fetch per ticker would be
too slow for a page with ten or more names — and reads "N/A" until one exists, coloured red
whenever the erosion overlay (§3.5) has fired for that stock.

**5.3 Average score by stock type** — shown only when 5+ stocks were scanned (a group average
of 2–3 names says nothing).

**5.4 Highest Value Score in this scan** — a one-line caption naming the top-scoring stock in
the batch, explicitly framed as a sort result, not a recommendation.

**5.5 DCF Parameters** (collapsed expander) — the valuation mechanics per stock: Ticker ·
Price · Intrinsic Value · IV/Price (green above 1.00×, red below) · Upside % · DCF Growth
(red when defaulted) · Growth Governor (which rule set the growth figure) · Discount ·
Perpetual · Value Score (bar). Click **Enable manual override** to type your own
growth/discount/terminal per stock — blank = automatic; overrides are session-only.

A closing line reports how many stocks were analysed and how long the scan took.

---

## 6. Stock Scanner (`/scanner`)

Answers the bigger question: "across a whole index, what should I even look at?"

**Selection.** Tick Australia and/or USA, then pick exactly one universe — each maps to one
real index, never a blend:

| Universe | Constituents sourced from |
|---|---|
| ASX 200 | Wikipedia S&P/ASX 200 (fallback: a curated local list) |
| ASX 300 | asx300list.com (fallback: ASX 200 live, then the local list) |
| S&P 500 | Wikipedia (fallback: a static 10-ticker list) |
| Nasdaq 100 | Wikipedia |
| Russell 2000 | iShares IWM ETF holdings (fallback: the last successfully fetched list, saved to disk) |
| Small Caps (S&P 600) | Wikipedia |

The source actually used is always shown under the pickers, so a scan on fallback data is
never silently presented as live.

**Sector filter with heat dots.** The Sector dropdown decorates each sector with a coloured
dot — hot / medium / cold, ranked by dollar-volume-weighted 12-month return relative to the
other sectors in this universe. Computing it for a universe the first time takes a few
minutes; after that it's cached for the day.

**Overnight scans — the instant path.** Every night the server scans each configured universe
by itself and stores the ranked result, including a stored Moat value and erosion read for
each ticker (see §3.5) computed as part of that same run. When one exists for the selected
universe, an expander appears with the full colour-coded table: Ticker · Type · Price ·
Intrinsic Value · MOS · Value Score · Quality · Psychology · Discovery · Moat · Valuation ·
Trend. Overnight scans are attention-lite (§3.4) and marked with their computation time;
results older than 3 days are treated as expired.

**Live scan.** The Run Scan button scores the selected universe/sector live, with a progress
bar and an honest up-front time estimate. Scans over 100 tickers automatically run
attention-lite. Results render in exactly the layout of §5 — including the Moat column, which
(as on Comparison) shows a stored value only, never computed live in this loop.

---

## 7. Rational Compounder Analysis (`/research`)

The site's hand-built research — not computed, written. Every chart, threshold and colour
band comes from the author's own research workbook.

**Pick a Stock** — the list is not a fixed set of names; it is read from whatever the current
research data file actually covers, and grows automatically as the author adds and rebuilds
coverage for new companies (a caption states how many companies are covered today). A
**Model history** page (§1) lets you browse every past rebuild of this data alongside the
current live version, as a changelog — not a claim about the accuracy of any past or future
output.

**Pick a Section:**

- **Fundamentals** — years of monthly share price against the author's own multi-year average
  line, colour-banded metric gauges (each with a collapsed "what this measures" explanation
  in the author's own words), other headline metrics as cards, and a share-price-growth-by-
  year chart.
- **Value vs Book** — Intrinsic Value ÷ Book Value by year modelled, colour-banded, with the
  average line.
- **Retained Earnings** — the "value created per $ retained" test at several horizons: for
  every dollar of earnings kept, how much market value was created — plus dividend metrics.
- **Earnings Trends** — EPS by year, EPS growth by year, P/E by year with average reference
  lines, plus the sheet's other figures.
- **Cost of Capital** — WACC vs ROIC by period: the fundamental test of whether the business
  earns more on its capital than that capital costs.
- **Fair Value** *(preview gated — see below)* — four independent valuation methods side by
  side against the current price: trailing P/E, forward P/E, DCF, and a 10-year equity
  method, with the exact inputs behind each.
- **Company Potential** *(preview gated — see below)* — the author's own Buffett/Munger-style
  judgment: Low/Medium/High ratings on management, moat, risk, pricing power and more, quick
  yes/no checks, and the full written analysis grouped by theme.

The Deep Dive page's own "Compounder View (auto)" section (§4) mirrors the first six of these
— Fundamentals through Fair Value — computed live for that one ticker instead of drawn from
the hand-built workbook; it has no live equivalent of Company Potential, since that section is
the author's own judgment rather than a calculation.

A **position-disclosure line** appears here for the selected stock: whether the site's author
personally holds it, has never held it, or has previously held and closed the position. This
is the author's own portfolio disclosure, shown identically to every visitor — it is not a
data signal and doesn't change with any score on the page.

**On the "gated" sections:** the site is currently free (see §9) — Fair Value and Company
Potential are marked as preview content that will move behind a subscription if and when one
opens, but nothing is actually restricted today.

---

## 8. My Portfolio (`/portfolio`) — new since the last edition

Sign in to use this page — it shows a sign-in prompt otherwise. Four tabs track stocks you've
told the site you hold:

- **Holdings** — your tracked positions.
- **Overview & P/L** — profit/loss summary across your holdings.
- **Health & News** — a health read and recent news for what you hold.
- **Progress** — how your tracked portfolio has moved over time.

This is entirely your own data, separate from the site author's own position-disclosure line
described in §7/§9 — the two are unrelated features that happen to share the word "position."

---

## 9. Accounts, watchlist, follow and the weekly digest

**Signing in** takes one of two forms, offered side by side in the same Sign In popover:

- **Google sign-in** — one click, no password to create; the site receives only your name and
  email.
- **Email sign-in code** — type your email, the site emails a 6-digit code (via Mailgun) that
  expires after 15 minutes; five wrong attempts burns the code and a fresh one is needed.
  A signed-in session from either method lasts 90 days in your browser. Code emails are
  rate-limited (a handful per address per day) to stop the sign-in flow being used to spam an
  inbox.

Once signed in, both methods work identically everywhere on the site — there's no feature
difference between a Google session and an email-code session.

Signing in unlocks:

- **The watchlist** — a personal saved-ticker list (an Add button on any Deep Dive); your saved
  stocks appear as one-click chips on the home page, and can be bulk-imported by pasting a
  list of tickers (up to 50 at a time).
- **Feedback** attributed to your email.
- **The weekly digest**, which arrives Monday mornings if your watchlist has stocks in it:
  each saved stock re-scored (attention-lite) with price, intrinsic value, MOS, Value Score,
  each ticker linking to its live Deep Dive. Remove all stocks from your watchlist to stop
  receiving it.

**Follow** is a separate, lighter-weight feature from the watchlist, and doesn't require a
full sign-in: on the Research and Deep Dive pages, a Follow toggle lets you leave just an
email address to be notified when that company's Rational Compounder research is updated (or,
via a follow-all option, when any company's research changes) — it's a notification
subscription for one thing changing, not a saved list you manage. If you're signed in and have
installed the site as an app (§1), an "Also notify on this device" button sits next to the
Follow toggle — it's a second, independent channel (a phone/browser push notification) for the
same research-update event, not a replacement for the email.

**Feedback** — the "Tell us what you think" button on any results page goes straight to the
author's inbox, or email rationalcompounder@stocksdeepdive.com.

**Subscriptions are not active — the site is currently free.** There is no paywall or pricing
page anywhere on the site today; everything described in this manual, including the sections
marked "preview gated" in §7, is available to every visitor right now.

---

## 10. Colour reference (public site)

| Element | Green | Amber | Red | Grey |
|---|---|---|---|---|
| Value Score bar | > 50 | 30–50 | ≤ 30 | — |
| Quality bar | > 80 | 40–80 | ≤ 40 | — |
| MOS bar | > 25% | 0–25% | ≤ 0% | N/A |
| Psychology bar | > 20 | −5 to 20 | ≤ −5 | — |
| Discovery bar | > 50 | 25–50 | ≤ 25 | — |
| Moat bar | > 70 | 40–70 | ≤ 40 | N/A (fund/insufficient data) |
| Valuation pill | UNDERVALUED | FAIR | EXPENSIVE | N/A |
| Sentiment pill | FEARFUL | GREEDY | OVERHEATED | CALM / NEUTRAL |
| Trend pill | Uptrend | Ranging | Downtrend | — |
| Intrinsic value | above current price | — | below current price | N/A |
| IV/Price | > 1.00× | — | < 1.00× | N/A |
| Upside % | positive | — | negative | N/A |
| **Any value in red bold** | — | — | estimated/default input (red-flag rule) | — |

A Moat value in red bold specifically means the erosion overlay (§3.5) has fired for that
stock ("watch" or "eroding"), on top of whatever colour band its score falls in — the same
red-flag convention used for every other estimated/flagged value on the site, not a separate
signal.

---

## 11. Data sources and honest limitations

Prices, fundamentals, and analyst estimates come from Yahoo Finance; search interest from
Google Trends; headlines from NewsAPI and Yahoo; social chatter from StockTwits; country mood
from GDELT. These are free public feeds: data can be delayed, revised, or occasionally
missing — which is exactly what the red-flag rule exists to expose rather than hide.
Statement history depth is typically 4–5 years (Yahoo Finance); the site can be configured
with a deeper data source for up to about ten years, but that is not enabled on the current
production deployment. Intrinsic values are estimates resting on stated assumptions. Nothing
on the site considers any individual's circumstances, and nothing in it is financial advice —
see the disclaimer in the site footer on every page.

---

## 12. Owner's view (appendix — not part of the public site)

Everything in this appendix is visible only to the site's owner, after unlocking "RC view"
(§1) with the admin access key, or via a matching `?admin=` link. A public or signed-in
visitor never sees any of this. It's included here for completeness, and so this manual
doesn't silently omit what the "Value Score" number actually drives internally.

**Investment Signal** (the admin name for the Value Score band): above 70 → **STRONG LONG** ·
above 50 → **LONG** · above 30 → **WATCHLIST** · otherwise **AVOID**. If no intrinsic value
could be computed, the signal is capped at WATCHLIST.

**Trade Setup — the timing axis.** *"Is right now a sane entry?"* — deliberately separate
from the Investment Signal:

```
Entry Zone = max(20-day support, MA50)   — buy zone reached when Price ≤ zone × 1.05
Stop Loss  = support-based
Targets: T1 = 20-day resistance, T2 = 60-day resistance, T3 = T2 × 1.10
Risk = Entry − Stop        RRn = (Target n − Entry) / Risk
```

BUY requires all of: not in a confirmed downtrend, Psychology > 0, Discovery > 0, Price ≤
MA50 × 1.05, price near the Entry Zone, and RR1 ≥ 1.5. Otherwise WATCHLIST or AVOID. The
**Trade Setup Score** (0–100) is a weighted display of the same gates (Trend Safety 20, Near
Entry Zone 20, Risk/Reward 20, Price vs MA50 15, Psychology Momentum 12.5, Discovery Momentum
12.5), coloured green above 65, amber 45–65, red at or below 45.

**Deep Dive (admin view)** additionally shows the full Trade Setup section — gauge, gate-check
chart, entry/stop/targets bar chart, and a caption on whether price is currently inside the
entry zone.

**Comparison and Scanner (admin view)** additionally show: the Investment Signal and Trade
Setup columns/pills throughout; a "Top Investment Candidate" block with the full written
thesis and an "Opportunity Details" expander for the top five results; a collapsed **Trade
Setup** table (Entry Zone, Stop Loss, Targets 1–3, Risk, RR1–3 coloured by rank across the
rows on screen, an Early Exit watch flag) with a "Position management & early-exit rules"
note; and, on Comparison only, an **Import screen CSV (admin)** panel that accepts a
TradingView screener CSV export, scans it in batches of 25 tickers on demand, and shows each
imported batch's results in its own isolated table — deliberately never merged into the main
Scanner results.

---

## 13. Operations reference (site owner)

Configured with Railway environment variables; changing one restarts the service.

| Variable | Set on production today? | Purpose |
|---|---|---|
| `ADMIN_REFRESH_KEY` | yes | The "RC view" / `?admin=` access key (§1, §12), and enables the Research page's workbook-rebuild admin panel |
| `PAYWALL_ENABLED` | set (see §9 — the live site currently shows no paywall/subscribe UI) | Master switch for the Stripe subscription flow; needs the Stripe and Google OAuth variables below |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | yes | Google sign-in (§9) |
| `AUTH_COOKIE_SECRET` | yes | Signs the sign-in session cookie (both Google and email-code sessions) |
| `STRIPE_SECRET_KEY` / `STRIPE_PRICE_ID` | yes | Subscription billing, inert while `PAYWALL_ENABLED` is off |
| `MAILGUN_API_KEY` / `MAILGUN_DOMAIN` | yes | Sends the email sign-in code and the weekly digest |
| `FEEDBACK_SMTP_EMAIL` / `FEEDBACK_SMTP_APP_PASSWORD` / `FEEDBACK_TO_EMAIL` | yes | Feedback-button email delivery |
| `ANTHROPIC_API_KEY` | yes | Optional live grammar/wording check on Company Potential free text |
| `INDEXABLE_PAGES` | yes | Controls which pages are allowed in `robots.txt`/sitemap |
| `NIGHTLY_UNIVERSES` | yes (ASX 200) | Comma-separated universes to pre-scan nightly (names exactly as in §6) |
| `NIGHTLY_SCAN_UTC_HOUR` | yes | Scan hour, UTC. Scans retry up to 3×/day if a run was interrupted |
| `RAILWAY_VOLUME_MOUNT_PATH` | yes (set by Railway) | Where persistent data — watchlists, overnight scans, compounder data, the Moat cache, scheduler state — actually lives, and survives redeploys |
| `EODHD_API_KEY` | **not set** | Optional deeper statement history (§3, §11); without it the site runs on Yahoo Finance's shorter history only |
| `MOAT_IN_VALUE_SCORE` | **set to `1` (on)** | Folds the Moat Score into the Value Score's own weighting — Quality 25% / Moat 15% / MOS 30% / Psychology 15% / Discovery 15%. For any ticker with no Moat Score (funds/ETFs, thin statement history), Moat's 15% is dropped and the other four are reweighted proportionally to fill the gap — never defaulted to zero, and not the same as the pre-Moat formula (see §3.6). Read once at process start (`moat_engine.py`), so changing it restarts the service, same as any other variable here. See §3.5–§3.6 for the full formula. |
| `SCHEDULER_ENABLED` | code default `true` | Set to `false` to disable both the nightly scan and the weekly digest jobs |
| `DIGEST_UTC_WEEKDAY` / `DIGEST_UTC_HOUR` | code default (Sunday 21:00 UTC) | Weekly digest schedule |
| `NEWS_API_KEY` | optional | Enriches the News Score; without it, Yahoo headlines only |

Manual commands from the service shell: `python nightly_scan.py "S&P 500"` (one universe now)
and `python digest_engine.py` (send the digest now).
