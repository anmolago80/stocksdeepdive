"""
Long Score - blends the four investment factors into one 0-100-ish number.

The previous version let a single unbounded metric dominate. Margin of Safety
is (Intrinsic - Price) / Intrinsic x 100, which is roughly bounded at +100 on
the upside but UNBOUNDED on the downside - a stock priced at 3x its intrinsic
value produces MOS = -200, and at 35% weight that alone was -70 points,
swamping a Quality of 100. That's why PME (Quality 100) landed at -40 while
MSB (Quality 100) reached only ~42: MOS, not the business, was driving the
score.

Fix: every component is CLAMPED to a comparable band before weighting, so no
one factor can overpower the others:

    Quality     0 .. 100     (already bounded)
    MOS        -50 .. +50     (clamped - a deep discount or a wild premium
                              can each move the score by at most its weight)
    Psychology -50 .. +50     (clamped - fear helps, greed hurts, but neither
                              runs away with the score)
    Discovery    0 .. 100     (clamped - attention is a positive-only tilt)

Weights (current mode):
    Quality 35% + MOS 25% + Psychology 20% + Discovery 20%

vs. the old 35 / 35 / 15 / 15. MOS is trimmed from 35% to 25% AND capped;
Psychology and Discovery are each lifted to 20% so the "fear = opportunity"
signal and the "market is noticing this" signal actually move the needle,
which they barely did before.

NOTE: the clamps here only affect SCORING. The true, uncapped MOS / Psychology
/ Discovery numbers are still shown in the tables - we cap their INFLUENCE, not
their reported value.
"""
import os

# Scoring clamps (see module docstring).
MOS_CLAMP = 50.0
PSY_CLAMP = 50.0
DISCOVERY_CAP = 100.0


def _clamp(value, lo, hi):
    return max(lo, min(value, hi))


MOAT_BLEND_WEIGHTS = {"quality": 0.25, "moat": 0.15, "mos": 0.30, "psy": 0.15, "disc": 0.15}


def _drop_and_reweight(weights, key):
    """Redistributes weights[key] proportionally across the other
    entries, then drops it - the same "drop and reweight, never score
    the missing thing as 0" convention moat_blend already uses for
    moat_score=None below, now shared with discovery_measured=False."""
    w = dict(weights)
    dropped = w.pop(key)
    remaining = sum(w.values())
    return {k: v + v * (dropped / remaining) for k, v in w.items()}


def calculate_long_score(
    quality_score,
    margin_of_safety,
    psychology_score,
    discovery_score,
    technical_score=0,
    insider_score=0,
    macro_score=0,
    mode="current",
    moat_score=None,
    discovery_measured=True,
):
    """
    mode="current" (default):
        Quality 35% + MOS(capped) 25% + Psychology(capped) 20%
        + Discovery(capped) 20%

    mode="institutional": future extended version -
        Quality 30% + Valuation 20% + Psychology 15% + Discovery 15%
        + Technicals 10% + Insider 5% + Macro 5%
        (technical/insider/macro default to 0 so it runs today.)

    mode="moat_blend": Phase 2 of moat_engine.py's Moat Score
        (MOAT_IN_VALUE_SCORE env-var switch - see that module's
        docstring). Quality 25% + Moat 15% + MOS(capped) 30%
        + Psychology(capped) 15% + Discovery(capped) 15%. When
        moat_score is None (fund, or too little statement history),
        Moat's 15 points are redistributed proportionally across the
        other four weights, never scored as 0 - the exact "drop and
        reweight, don't default to neutral" convention moat_engine.py
        itself uses for a missing pillar. This mode is purely additive:
        "current" (the default, used everywhere Phase 2 is off) is
        untouched above.

    discovery_measured=True (default): Discovery is scored normally, in
    whichever mode is active - every existing caller (Deep Dive, the
    weekly digest via analyze_ticker_lite, anything else that doesn't
    pass this new parameter) is byte-identical to before this parameter
    existed.

    discovery_measured=False: nightly_scan.py's "attention_lite" path
    (large universes - ASX 200/300, S&P 500, the Russells, etc. - see
    that module's own docstring) never actually fetches Google Trends/
    NewsAPI/StockTwits; the raw price/volume-only discovery_score it
    computes instead is a near-zero placeholder, not a genuine "no
    attention" reading. Rather than blending that placeholder in as if
    it were measured (which is what silently deflated every stored
    Value Score in every large universe), Discovery's weight is dropped
    and redistributed proportionally across the OTHER weights in
    whichever mode is active - the exact "drop and reweight, never
    score the missing thing as 0" convention moat_blend already uses
    for moat_score=None just above, now extended to Discovery. The row
    still stores/displays the raw discovery_score exactly as passed in
    (this module's own "we cap influence, not reported value"
    convention) - only the BLEND treats it as unmeasured.

    mode="current", discovery_measured=False:
        Quality 43.75% + MOS(capped) 31.25% + Psychology(capped) 25%
        (Discovery's 20% redistributed proportionally - the OCL.AX
        fixture: quality=80, mos=69.6 (clamped 50), psychology=42.7 ->
        80*.4375 + 50*.3125 + 42.7*.25 = 61.30).
    """

    mos_c = _clamp(margin_of_safety, -MOS_CLAMP, MOS_CLAMP)
    psy_c = _clamp(psychology_score, -PSY_CLAMP, PSY_CLAMP)
    disc_c = _clamp(discovery_score, 0, DISCOVERY_CAP)

    if mode == "institutional":
        if not discovery_measured:
            w = _drop_and_reweight(
                {"quality": 0.30, "mos": 0.20, "psy": 0.15, "disc": 0.15,
                 "technical": 0.10, "insider": 0.05, "macro": 0.05},
                "disc",
            )
            return round(
                quality_score * w["quality"]
                + mos_c * w["mos"]
                + psy_c * w["psy"]
                + technical_score * w["technical"]
                + insider_score * w["insider"]
                + macro_score * w["macro"],
                2
            )
        return round(
            quality_score * 0.30
            + mos_c * 0.20
            + psy_c * 0.15
            + disc_c * 0.15
            + technical_score * 0.10
            + insider_score * 0.05
            + macro_score * 0.05,
            2
        )

    if mode == "moat_blend":
        w = dict(MOAT_BLEND_WEIGHTS)
        if moat_score is None:
            moat_w = w.pop("moat")
            remaining = sum(w.values())  # 0.85
            w = {k: v + v * (moat_w / remaining) for k, v in w.items()}
            if not discovery_measured:
                w = _drop_and_reweight(w, "disc")
                return round(
                    quality_score * w["quality"]
                    + mos_c * w["mos"]
                    + psy_c * w["psy"],
                    2
                )
            return round(
                quality_score * w["quality"]
                + mos_c * w["mos"]
                + psy_c * w["psy"]
                + disc_c * w["disc"],
                2
            )
        if not discovery_measured:
            w = _drop_and_reweight(w, "disc")
            return round(
                quality_score * w["quality"]
                + moat_score * w["moat"]
                + mos_c * w["mos"]
                + psy_c * w["psy"],
                2
            )
        return round(
            quality_score * w["quality"]
            + moat_score * w["moat"]
            + mos_c * w["mos"]
            + psy_c * w["psy"]
            + disc_c * w["disc"],
            2
        )

    if not discovery_measured:
        w = _drop_and_reweight(
            {"quality": 0.35, "mos": 0.25, "psy": 0.20, "disc": 0.20}, "disc"
        )
        return round(
            quality_score * w["quality"]
            + mos_c * w["mos"]
            + psy_c * w["psy"],
            2
        )
    return round(
        (
            quality_score * 0.35
            + mos_c * 0.25
            + psy_c * 0.20
            + disc_c * 0.20
        ),
        2
    )


def top200_moat_blend_live():
    """TASK 35 item A1 (6 Nov 2026, Director-directed, BKNG live case):
    the switch that puts the Top 200's own selection/cut-off/previews
    onto mode="moat_blend" (via recompute_value_score() below), instead
    of today's plain formula. Re-read on every call (same unset/""/0/
    off=OFF, 1/on=ON pattern as scan_store.is_private_universe() and
    every other live switch in this codebase) rather than cached, so
    a changed Railway variable takes effect on the very next
    selection run without a redeploy - DELIBERATELY NOT the same
    pattern moat_engine.MOAT_IN_VALUE_SCORE itself uses (a module-level
    constant read once at import) - that one needs a redeploy to pick
    up a change; this one shouldn't, since the Director wants to be
    able to flip it live after reviewing the 12 Nov shadow preview.
    Claude Code never sets this - the Director does, on Railway, only
    after that review."""
    raw = (os.environ.get("TOP200_MOAT_BLEND_LIVE") or "").strip().lower()
    return raw in ("1", "on")


def recompute_value_score(row, moat_blend=False):
    """TASK 35 item A1/A2 (6 Nov 2026, Director-directed, BKNG live
    case): the ONE "Top 200 selection formula" - every caller that
    needs "what does the Top 200 actually rank this row on" (top100_
    engine.py's own selection/cut-off/previews/prescoring, and deep_
    dive_engine.py's "Scanner & Top 200 score" line) must go through
    this, never a parallel/second calculate_long_score() call of its
    own. This closes the exact live BKNG mismatch TASK 34 found: Deep
    Dive's own line used to recompute calculate_long_score() a SECOND
    time, with its own independent moat_blend branch gated only on
    moat_engine.MOAT_IN_VALUE_SCORE, while top100_engine._recompute_
    value_score() (what the real Top 200 selection/cut-off actually
    used) never applied moat_blend at all - so Deep Dive showed a
    moat-blended score for a row the Top 200 had ranked on the plain
    one.

    `row` needs the same four/five keys every existing caller's row
    already carries (a live scan_store.py row, or a Deep-Dive-built
    row shaped the same way): "Quality", "MOS %", "Psychology",
    "Discovery (lite)", and - only read when moat_blend=True -
    "Moat". discovery_measured is always False here (the one existing
    rule this function's callers already shared, "Top 100 selection
    freshness fix 2.2" - see top100_engine._recompute_value_score()'s
    own retained docstring for why).

    moat_blend=False (the default): today's plain formula - byte-
    identical to calling calculate_long_score() directly with no mode
    override, used whenever TOP200_MOAT_BLEND_LIVE is off (unset) or
    moat_engine.MOAT_IN_VALUE_SCORE is off.
    moat_blend=True: mode="moat_blend" using row.get("Moat") - a row
    with no stored Moat score (an older scan, or a universe scanned
    before TASK 35) falls through to calculate_long_score()'s own
    existing missing-moat reweighting (moat_score=None), never scored
    as if Moat were zero/penalised for lacking one."""
    if moat_blend:
        return calculate_long_score(
            row.get("Quality"), row.get("MOS %") or 0.0, row.get("Psychology"),
            row.get("Discovery (lite)") or 0, mode="moat_blend",
            moat_score=row.get("Moat"), discovery_measured=False,
        )
    return calculate_long_score(
        row.get("Quality"), row.get("MOS %") or 0.0, row.get("Psychology"),
        row.get("Discovery (lite)") or 0, discovery_measured=False,
    )
