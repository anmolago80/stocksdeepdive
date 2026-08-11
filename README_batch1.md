# Website build - progress notes

This folder is a work-in-progress copy of the scanner, adapted for the
public website. This is **not** deployed anywhere yet, just saved here for
you to review and test.

## Batch 5 - Rational Compounder Analysis content (first pass)

- The "coming soon" placeholder is gone. The page now has two dropdowns:
  **Stock** (every ticker your watchlist workbook has real data for - right
  now that's AUB.AX, CSL.AX, RMD.AX) and **Section**, renamed from the raw
  sheet tabs so they read like what they actually contain:
  - Stock Analysis → **Fundamentals**
  - IV2BV → **Value vs Book**
  - Dividend Ratio → **Dividends**
  - Earnings Analysis → **Earnings Trends**
  - Cost of Capital Analysis → **Cost of Capital**
  - Valuation → **Fair Value**
  - Company Potential → **Company Potential** (name unchanged, on request)
- Once both are picked, every metric column YOU put an explanatory comment
  on in Excel (Fundamentals through Fair Value) shows up as either:
  - a small colour-coded gauge, when your own comment spelled out red/
    amber/green (or a 4th "very high" blue) bands - the exact cutoffs and
    band wording come straight from that comment, nothing invented, or
  - a plain metric card, when your comment explains the number but doesn't
    give bands (e.g. WACC, the DCF share-price projection).
  Every card has a collapsed "What this measures" row underneath with your
  full comment text, so the explanation is there without cluttering the
  page by default.
- **Company Potential is a placeholder for now** - it's in the dropdown as
  requested, but selecting it just explains that its content (your Munger/
  Buffett-style Low/Medium/High ratings plus free-text notes like the
  inversion analysis) is a follow-up batch. It's genuinely different from
  the other six sections - qualitative judgment, not chartable numbers -
  so it needs its own layout rather than reusing the gauge grid.
- New file `build_compounder_data.py`: a one-off script (not run by the
  live site) that reads your SMSF research workbook and writes
  `compounder_data.json`, which is what the page actually reads. Re-run it
  whenever you add tickers or update numbers in the workbook:
  `python3 build_compounder_data.py`. It defaults to the file path from
  your last upload; pass a different path as an argument if it moves.
- Only 3 of your ~30 watchlist rows have real (non-error) data right now
  (AUB.AX, CSL.AX, RMD.AX) - everything else in the workbook is still
  blank/formula-error rows, so the dropdown will grow automatically as you
  fill more names in and re-run the build script.

## Batch 5b - Trend/comparison charts + more "main data" on the thin tabs

Follow-up to Batch 5, after you clarified what you actually wanted:

- **Fundamentals** now opens with a real price chart - ~10 years of your
  monthly price history (the Price Calc tab), with your own "10y Average"
  figure (Stock Analysis column CF) drawn as a dashed reference line, so
  you can see at a glance whether a stock is trading above or below its
  own long-run average. This was the "price vs the median I calculated"
  chart you asked for - there's no MEDIAN() formula anywhere in the
  workbook (I checked every sheet), so it uses your "10y Average" column
  instead, per what you pointed me to.
- **Value vs Book** now opens with Intrinsic Value vs Book Value vs
  Current Price as three side-by-side bars, instead of the single IV/BV
  ratio gauge - the value-creation gap is visible directly instead of
  being compressed into one multiple. The multiple itself is still shown
  in the chart's title (e.g. "IV/BV = 3.18x").
- **Dividends** now opens with a "Value Created per $ Retained" chart -
  your own workbook already runs this test at four horizons (2Y/5Y/10Y/
  TTM): for every dollar of earnings you kept in the business, how much
  market value did that create. It never had a comment on it, but it's
  exactly the "value created" chart you asked for, so it's pulled in as
  a grouped bar (retained earnings vs value created, per horizon).
- **Value vs Book, Dividends, and Cost of Capital** (the three tabs that
  only had 1-2 commented columns) now also show their other headline
  numbers as plain cards - Share Price, 52 Week High/Low, Market Cap,
  Enterprise Value, EPS, Dividend, Dividend Yield, WACC's building blocks
  (Long Term Debt, Interest Expense, ROIC, Income Before Tax), etc. Where
  a column repeats across several years (you keep TTM + a few historical
  years side by side), only the TTM one is shown for now - said I'd show
  you a first pass and take instructions from there.
- Fundamentals, Earnings Trends, Fair Value, and Company Potential are
  unchanged from Batch 5 - still scoped to the columns you put a comment
  on, per your call to keep those as-is.

## Batch 5c - Year-series charts, focused Fair Value, real Company Potential

Built overnight from your list, while you were asleep - a few judgment
calls below that I couldn't check with you, flagged so you can correct
anything I got wrong.

- **Value vs Book**: added "IV/BV by Year Modelled" - a bar per year you've
  modelled (TTM back to 2016), coloured with the same red/amber/green
  bands as before. Sits under the existing IV/BV/Price bar comparison.
- **Stock Analysis metrics** (Interest Coverage, Working Capital to Debt,
  EV to Free Cash Flow, Net Income Ratio, Free Cash Flow Yield, Intangibles
  to Total Assets, Price to Equity Ratio, Return on Tangible Assets, ROE,
  Operating Income Ratio, PFCF Ratio, ROIC, Debt to Assets, Quick Ratio,
  Current Ratio, Debt to Equity, Correlation (SP500)) - checked against
  what's already on the Fundamentals tab: every one of these was already
  there from Batch 5. Nothing to add.
- **Earnings Trends**: added three year-series charts - EPS by year, EPS
  Growth by year (green/red bars), and PE Ratio by year - plus every other
  header from that sheet as plain cards (10y/4y Average Earnings, Max/Min
  Earnings, the SD figures, Average 10 Year Growth, 10Y Growth (3Y AVG),
  PE Ratio Average). The existing AVG PE 3Y*PTB Ratio gauge is unchanged.
- **Fair Value**: replaced with ONLY the 4 methods you asked for - PE
  Trailing, PE Forward, DCF, and Equity Method 10y - as one bar chart
  against current price. None of these four had a comment in Excel, so
  I traced the actual formulas to identify them: PE Forward/Trailing are
  columns J/K. For "DCF" I initially assumed "Share Price 10Y" (column DE)
  since its comment spells out a formula, but tracing it further showed
  DE and "Equity Method 10y" (U) are literally the same calculation, just
  before/after a currency conversion - not two different methods. The real
  DCF turned out to be column P ("Buffet Share Price"), which sums 10 years
  of discounted Free Cash Flow (columns "Discount Value Year 1-10") plus a
  terminal value - that's what's plotted as "DCF" now. Flagging this in
  case you had a different column in mind - it's a judgment call I made
  from the formulas, not something you named directly.
- **Company Potential** now has real content instead of the placeholder:
  - Your Low/Medium/High cells, shown directly as coloured pills (14 of
    them - Management Reputation, Debt/Legal/Inflation Exposure, Business
    Understanding, Value/Progress/Wealth Prospect, Market Sentiment, Risk,
    Insights, Stability within Industry, Ability to Change Pricing, Market
    Activity). Colour (green/amber/red) is my judgment on whether High or
    Low is the "good" answer for that specific question - documented in
    build_compounder_data.py's COMPANY_POTENTIAL_HML list if you want to
    check or override any of them. A few cells (Market Sentiment, Insights,
    Market Activity) I couldn't confidently call good/bad either way, so
    those show as plain grey pills instead of guessing.
  - A "Quick checks" row for the short Yes/No-style answers (public
    interest, share buybacks, true earnings, etc.)
  - Your long free-text answers (Inversion Angle, No Brainer Question,
    Munger tendencies, Buffett Tenets, and so on) merged into 6 themed,
    expandable groups - The Business & Its Moat, Risk & Inversion,
    Psychology & Munger Tendencies, The Investment Case, Financial
    Diligence, Management & Context. The grouping and titles are my call;
    see COMPANY_POTENTIAL_TEXT_GROUPS in build_compounder_data.py to
    rename or regroup.
  - **Data quality flag, not a bug in the app**: several of AUB's
    Company Potential free-text answers (Trademark Product, Advantage of
    Scale, Multidisciplinary Approach, and others) are actually written
    about "Nubank," not AUB - looks like notes got pasted into the wrong
    row in the source workbook at some point. Worth a check/cleanup on
    your end; the app is just displaying what's in row 4 of that sheet.
- Fixed a real display bug found while testing CSL (which only has one
  real year of IV/BV data): a bar chart with a single, numeric-looking
  category label (e.g. just "2022") was rendering as a continuous numeric
  axis instead of a category axis, so ticks would show fractional years
  like "2,021.8." All the new year-series charts now force a category
  axis so this can't happen regardless of how many years a stock has data
  for.

## Batch 4c - Clickable logo

- The "StocksDeepDive" title at the top of every page is now a link back
  to the home page - click it anytime to clear the search and start over.

## Batch 4b - Trim the header so results start immediately

- Your test video showed the URL genuinely changing to `/deep-dive` on
  Search (the new-page navigation from Batch 4 IS working), but the
  compact header + a long explanatory paragraph above the results were
  tall enough that you had to scroll to actually see them - which reads
  exactly like "still loading underneath." Fixed:
  - The header on every results page is now much smaller (a small title,
    a narrower search box, a small Rational Compounder Analysis button)
    instead of the same near-full-size box as the home page.
  - The "One ticker = Deep Dive..." instructions caption only shows on
    the home page now - once you've already searched once, you don't
    need it repeated on every results page.
  - The Deep Dive tab's long explanatory paragraph now only shows when
    there's nothing else on the page yet (before you've searched, or if
    a search errored) - once real results are showing, that paragraph is
    gone and the ticker's numbers start right under the search box.

## Batch 4 - Real results pages + colored classification badges

- Search now takes you to a genuinely separate page instead of expanding
  results below the search box on the same page - the URL actually
  changes (e.g. `.../deep-dive`, `.../comparison`, `.../research`) and the
  new page starts fresh at the top, so there's nothing to scroll past to
  see your results. The compact search box stays pinned at the top of
  every results page so you can search again anytime.
- In the Side-by-side comparison table, the Type, Valuation, Sentiment,
  Trend, and Trade Setup columns are now small colored pills instead of
  plain text - same red/amber/green convention as the score bars:
  Valuation (green UNDERVALUED, amber FAIR, red EXPENSIVE), Sentiment
  (green FEARFUL, amber GREEDY, red OVERHEATED, gray CALM/NEUTRAL), Trend
  (green Uptrend, amber Ranging, red Downtrend), Trade Setup (green BUY,
  amber WATCHLIST, red AVOID). Type isn't a verdict (GROWTH/COMPOUNDER/
  etc. are just categories) so it gets a neutral teal pill, except when
  the sector lookup failed and it fell back to a default classification -
  that's flagged red, same as every other estimated/defaulted cell in
  this app.

## Batch 3 - One search box, no nav bar, no Strategy Mode

- The 4-item nav bar (Home / Stock Comparison / Stock Deep Dive / Rational
  Compounder Analysis) is gone. So are the three landing-page buttons.
- In their place: **one** minimal, Google-style search box shown at the
  top of every page, all the time. Type one ticker and hit Search (or
  Enter) for a Deep Dive; type two or more (comma or space separated) for
  a side-by-side Comparison - same box, no mode-switching, no separate
  "Run Comparison" or "Analyze" buttons anywhere else on the site.
- The "Rational Compounder Analysis" button sits just beneath the search
  box, on every page - it's the one thing that isn't a ticker search
  (it's a fixed watchlist page), so it keeps its own button.
- The "Stocks**DeepDive**" title shrinks and moves up once there are
  results on the page, so the header stays compact instead of pushing
  content down - big and centered when the page is empty, small once
  you've searched. (One small cosmetic quirk: right after you submit a
  2+-ticker Comparison search, the title briefly stays large for that one
  screen - it shrinks back down as soon as you interact with anything
  else on the results page. Not worth the extra complexity to fix given
  it's purely cosmetic.)
- The "Strategy Mode" selector (Long-Term Investment vs Swing Trading) is
  gone entirely, along with the position-sizing inputs that only appeared
  in Swing mode. Everything now runs permanently as Long-Term Investment
  (the original Long Score: value + quality led) - there's no UI to
  change it anymore.

## Batch 2 - Landing page + background feature defaults

- New landing page (shown first, at the site's root): a big "Stocks**Deep
  Dive**" title, a centered Google-style search box ("Input your stock
  ticker"), and three buttons below it - Stock Comparison, Stock Deep Dive,
  Rational Compounder Analysis (the new name for the tab that used to be
  called "Stock Research"). Typing a ticker into the search box and hitting
  Search runs a Deep Dive on it directly and takes you there in one step.
- Every other view now shows a slim nav bar at the top (Home / Stock
  Comparison / Stock Deep Dive / Rational Compounder Analysis) so you can
  jump between tools without going back to the search page each time.
- Site-wide button styling: teal (#0d9488) outline buttons that fill solid
  teal on hover/when active, rounded corners - one consistent look across
  the whole site instead of Streamlit's default red.
- The old top-of-page "Global Stock Scanner" title is gone; the browser
  tab title is now "StocksDeepDive".
- "Enable Trends & News," "Include Market Cap Bucket," and "Enable Social
  Sentiment" are no longer visitor-facing checkboxes - all three now just
  run on, silently, for every scan.
- The NewsAPI key box (and its "remember this key on this computer" file)
  is gone. A key is picked up from a `NEWS_API_KEY` server environment
  variable instead, set once when we deploy - visitors never see or enter
  one. If no key is set, News Score still works off its Yahoo Finance half
  alone, same fallback behaviour as before.
- Streamlit's tab widget (`st.tabs`) is gone entirely, since it can't be
  driven by code (a button elsewhere can't select a specific tab). Views
  are now tracked in `st.session_state["view"]` instead, which is what
  lets the landing page's buttons and the nav bar jump straight to a
  specific tool.
- The "Strategy Mode" (Long-Term vs Swing Trading) selector moved from the
  very top of the page into the Stock Comparison view itself, since it
  only ever meant anything there.

## Batch 1 - Stock Comparison tab

- Removed the country/market selector and the market-mood block - the tab
  is now just the ticker box and the Run Comparison button. Each ticker is
  still resolved independently by its own suffix (`.AX` = ASX, no suffix =
  US), exactly as before; nothing about ticker handling changed.
- Removed the "Top 10 Stocks", "Swing Setup", and "Watchlist" /
  "Unvalued-speculative" tables.
- Rebuilt "Side-by-side comparison" as one row per ticker with exactly:
  Ticker, Type, Price, Quality Score, Intrinsic Value, MOS, Long Score,
  Psychology Score, Discovery Score, Valuation, Sentiment, Trend, Trade
  Setup, Trade Setup Score. The six score columns (Quality, MOS, Long
  Score, Psychology, Discovery, Trade Setup Score) render as a colored bar
  - red/amber/green - using the same cutoffs the app already verdicts on
  elsewhere (e.g. Long Score's WATCHLIST/LONG gates, MOS's 25% undervalued
  cutoff), not new arbitrary numbers.
- "DCF Parameters" and "Full Stock Database" tables are unchanged, except:
  DCF overrides are now session-only. Previously an override was saved to
  a shared file on disk and applied to every visitor, forever. Now it
  lives only in that visitor's own browser session and resets to defaults
  the moment they leave or reload the page - so one visitor's edits can
  never leak into what anyone else sees.
- Removed entirely (required to get Comparison running standalone): the
  Auto-Trading tab and its Alpaca connection, and the universe "Stock
  Scanner" tab. `alpaca-py` and the disk-persisted override store were
  dropped from requirements/code accordingly.

## Untouched so far
- Stock Deep Dive tab - works exactly as it did in the original app.

## Not done yet (future batches / your call)
- Rational Compounder Analysis - Company Potential section content (see
  Batch 5 above - still a "coming soon" placeholder, everything else on
  the page is built).
- A NEWS_API_KEY value actually set anywhere (harmless if left unset -
  News Score just runs on its Yahoo Finance half only).
- Domain (stocksdeepdive.com) connection.
- Actual deployment to Railway.

## How to run this locally to check it yourself
```
pip install -r requirements.txt
streamlit run app.py
```
It opens straight to the new landing page.
