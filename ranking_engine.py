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

# Scoring clamps (see module docstring).
MOS_CLAMP = 50.0
PSY_CLAMP = 50.0
DISCOVERY_CAP = 100.0


def _clamp(value, lo, hi):
    return max(lo, min(value, hi))


MOAT_BLEND_WEIGHTS = {"quality": 0.25, "moat": 0.15, "mos": 0.25, "psy": 0.20, "disc": 0.15}


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
        (MOAT_IN_VALUE_SCORE env-var switch, OFF by default - see that
        module's docstring). Quality 25% + Moat 15% + MOS(capped) 25%
        + Psychology(capped) 20% + Discovery(capped) 15%. When
        moat_score is None (fund, or too little statement history),
        Moat's 15 points are redistributed proportionally across the
        other four weights, never scored as 0 - the exact "drop and
        reweight, don't default to neutral" convention moat_engine.py
        itself uses for a missing pillar. This mode is purely additive:
        "current" (the default, used everywhere Phase 2 is off) is
        untouched above.
    """

    mos_c = _clamp(margin_of_safety, -MOS_CLAMP, MOS_CLAMP)
    psy_c = _clamp(psychology_score, -PSY_CLAMP, PSY_CLAMP)
    disc_c = _clamp(discovery_score, 0, DISCOVERY_CAP)

    if mode == "institutional":
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
            return round(
                quality_score * w["quality"]
                + mos_c * w["mos"]
                + psy_c * w["psy"]
                + disc_c * w["disc"],
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

    return round(
        (
            quality_score * 0.35
            + mos_c * 0.25
            + psy_c * 0.20
            + disc_c * 0.20
        ),
        2
    )
