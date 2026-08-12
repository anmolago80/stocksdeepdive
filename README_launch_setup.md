# StocksDeepDive — Launch update & Railway setup

What changed on 12 Aug 2026, and the exact steps to switch the infrastructure on.

## What's in this update (code, already done)

**New landing page (dark terminal design, site-wide dark theme).** Scrolling market
tape, hero with concrete value proposition, one-click example ticker chips, featured
analysis card (rotates daily through CSL.AX / AAPL / BHP.AX / RMD.AX / MSFT / WES.AX /
GOOGL, cached 6h, attention-lite so it's fast), market-mood strip, four feature cards,
How-it-works + the red-flag rule, Rational Compounder coverage cards, free-during-launch
CTA. All remote data on the landing page is fetched concurrently with a 10-second hard
budget — a slow Yahoo/GDELT day delays those boxes only, never the page.

**Shareable URLs.** `/deep-dive?ticker=CSL.AX` and `/comparison?tickers=CSL.AX,BHP.AX`
now run directly — your blog can deep-link any live analysis. The address bar stays in
sync with what's on screen.

**Deep Dive.** 6-month price chart (with MA50 + entry zone), plain-English Why Buy /
Why Wait / Risks (same thesis engine as Comparison), rewritten intro, Long Score
explainer caption, example chips on the empty state, and an Add-to-watchlist button for
signed-in users.

**Comparison & Scanner results.** Side-by-side table now leads; Trade Setup, Full Stock
Database and DCF Parameters collapsed into expanders; debug output removed; honest
per-scan time estimates; scans over 100 tickers automatically skip Trends/News/Social
(quota + speed protection, labelled as such); preview/paywall text now says Scanner on
the Scanner page. "Sector Rankings" renamed and hidden under 5 stocks.

**Scanner.** Sector heat is now opt-in (it used to block first render for minutes);
overnight scan results (see below) are served instantly when available; delisted NCM.AX
culled from the fallback list; Russell 2000 no longer drops class-share tickers; the
last good Russell list is cached to disk so an iShares URL change degrades to
yesterday's list, not an empty universe.

**Copy.** Everything addressed to you ("your workbook", "my call") rewritten in public
voice; paywall teasers rewritten; footer with the general-advice disclaimer on every page.

**New modules.**
- `watchlist_store.py` — per-user watchlists (SQLite on the volume)
- `scan_store.py` + `nightly_scan.py` — overnight universe scans
- `digest_engine.py` — weekly Mailgun watchlist email
- `scheduler_engine.py` — in-process scheduler that runs the two jobs
- `.streamlit/config.toml` — the dark theme

## Railway setup — step by step

The design needs ONE volume and a few environment variables. No extra services: the
scheduler runs inside the web app (Railway volumes attach to exactly one service, and
the web app needs the volume — so the jobs live in its process, on a daemon thread).

### Step 1 — attach a Volume (unlocks watchlists + overnight scans + compounder persistence)

1. Railway dashboard → your StocksDeepDive service → right-click (or ⌘K) → **Attach Volume**.
2. Mount path: anything (e.g. `/data`). Railway automatically sets
   `RAILWAY_VOLUME_MOUNT_PATH` — all the new code (and your existing compounder admin
   rebuild) picks it up with zero config.
3. Redeploy. First boot seeds `compounder_data.json` onto the volume automatically.

### Step 2 — Mailgun variables (unlocks the weekly digest)

Service → **Variables** → add:

| Variable | Value |
|---|---|
| `MAILGUN_API_KEY` | your Mailgun private API key |
| `MAILGUN_DOMAIN` | your verified sending domain (e.g. `mg.stocksdeepdive.com`) |
| `MAILGUN_FROM` | optional — default `StocksDeepDive <digest@your-domain>` |
| `MAILGUN_BASE_URL` | only if your Mailgun domain is EU-region: `https://api.eu.mailgun.net` |
| `SITE_BASE_URL` | optional — default `https://stocksdeepdive.com` (used in email links) |

Without the two required ones, the digest silently skips — nothing breaks.

### Step 3 — scheduler tuning (optional, defaults are sensible)

| Variable | Default | Meaning |
|---|---|---|
| `SCHEDULER_ENABLED` | `true` | `false` turns both jobs off |
| `NIGHTLY_UNIVERSES` | `ASX 200` | comma-separated, e.g. `ASX 200,S&P 500` |
| `NIGHTLY_SCAN_UTC_HOUR` | `20` | 20:00 UTC = 6am Brisbane |
| `DIGEST_UTC_WEEKDAY` | `6` (Sunday) | with hour 21 ≈ 7am Monday Brisbane |
| `DIGEST_UTC_HOUR` | `21` | |

Start with just ASX 200 (~10–15 min per night, gentle on Yahoo). Add `S&P 500` once
you're happy. You can also run one manually anytime: `python nightly_scan.py "ASX 200"`
(locally it writes next to the code; on Railway use the service shell and it writes to
the volume).

### Step 4 — verify

1. After the next scheduled night (or a manual run), the Scanner page shows
   "Overnight ASX 200 scan — N stocks ranked by Long Score, computed …" instantly.
2. Sign in with Google on the site, open any Deep Dive, click "☆ Add to my watchlist".
   The chip row appears on the home page.
3. The Monday digest arrives at your own email (your sign-in address) if your
   watchlist has stocks in it. To test immediately: `python digest_engine.py` from the
   Railway service shell.

## Still on your plate

- Fix the AUB/Nubank rows in the research workbook, then rebuild via the admin panel.
- Google OAuth + Stripe env vars when you're ready for sign-in/paywall (see
  paywall_engine.py's checklist) — sign-in is worth enabling soon so watchlists and the
  digest have an audience from week one.
- A privacy policy page/link before pushing sign-in hard (Google's OAuth consent review
  asks for its URL).

## Notes

- Watchlists/overnight scans work WITHOUT the volume too — they just reset on each
  redeploy. Attach the volume before promoting them loudly.
- The digest email includes an implicit unsubscribe (empty your watchlist); if the list
  grows, add a proper one-click unsubscribe link (Mailgun can inject one) before
  volumes get serious.
- Local dev: `SCHEDULER_ENABLED=false streamlit run app.py` if you don't want the
  overnight scan firing on your PC at 6am.
