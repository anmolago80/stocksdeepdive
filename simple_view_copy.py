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
