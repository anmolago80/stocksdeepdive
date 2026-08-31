"""
ai_client.py

Thin, fail-safe wrapper around the Anthropic API - the one place the
AI-readiness roadmap's Ask features (Phase 3+) call anthropic.Anthropic()
directly. Mirrors the fail-safe convention this codebase already uses
for its one existing AI call (build_compounder_data.py's optional
grammar/wording check on Company Potential text, which already runs on
ANTHROPIC_API_KEY only if set): no key configured, the package missing,
or any API error all fail SAFE - a None answer plus a plain-English
reason - never an exception the caller has to catch, and never a crash.

Every caller MUST run ai_gate.check() first (see that module's own
docstring - "never let an AI call run without the gate" is a
non-negotiable ground rule of the roadmap). This module does not know
about quotas, tiers or the spend cap at all; it only makes the call and
reports back the real token usage so the caller can log it via
ai_gate.record().
"""

import os

# Haiku 4.5 - the model string already used elsewhere in this codebase
# for the one existing AI call (build_compounder_data.py's grammar
# check) - reused here rather than invented fresh, so there's exactly
# one spelling of the model name across the whole app. Every Phase 3-6
# and Phase 9-10 AI feature uses this model per the owner's locked-in
# decision (Sonnet 5 is reserved for Phase 7's research-note drafting
# and Phase 8's weekly brief only - added here once those phases are
# built, not speculatively now).
MODEL_HAIKU = "claude-haiku-4-5"

# Anthropic's published per-million-token API pricing for this model
# (anthropic.com/claude/haiku, checked 2026-08) - used to estimate each
# call's cost_usd for the spend cap/meter. A hardcoded constant rather
# than a live lookup: list pricing changes rarely enough that an
# occasionally-rechecked constant is simpler and more reliable than an
# extra network call (with its own failure modes) on every single
# question asked.
HAIKU_INPUT_USD_PER_MTOK = 1.00
HAIKU_OUTPUT_USD_PER_MTOK = 5.00

# The one required label (master AI-readiness instruction: "every
# AI-written block visibly labelled") every AI-written block on the
# site shows itself under - defined once here so its wording can never
# drift between Deep Dive's Ask box, My Portfolio's Ask tab, and every
# later phase that shows model output to a visitor.
ANSWER_LABEL = "AI-written summary of the site's data - not advice"


def _api_key():
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None


def available():
    """True if an API key is configured AND the anthropic package
    imports. Callers use this to decide whether to show an Ask box at
    all, rather than showing one that can only ever fail."""
    if not _api_key():
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def estimate_cost_usd(model, input_tokens, output_tokens):
    if model == MODEL_HAIKU:
        return ((input_tokens or 0) / 1_000_000) * HAIKU_INPUT_USD_PER_MTOK + \
               ((output_tokens or 0) / 1_000_000) * HAIKU_OUTPUT_USD_PER_MTOK
    return 0.0


def ask(system_prompt, user_message, model=MODEL_HAIKU, max_tokens=700):
    """Runs one Anthropic call. Returns a dict, always with every key
    below - never raises:

        ok            bool - True only if a non-empty answer came back
        text          the answer, or None if ok is False
        error         a short, user-facing reason, or None if ok is True
        input_tokens / output_tokens - real counts from the API response
                      (0 if the call never reached Anthropic)
        cost_usd      estimate_cost_usd() of the above - 0.0 if ok is False
        model         echoed back, for the caller's logging convenience
    """
    result = {"ok": False, "text": None, "error": None,
              "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
              "model": model}

    api_key = _api_key()
    if not api_key:
        result["error"] = "AI features aren't configured right now."
        return result
    try:
        import anthropic
    except ImportError:
        result["error"] = "AI features aren't available right now."
        return result

    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text = "".join(
            block.text for block in resp.content
            if getattr(block, "type", None) == "text"
        ).strip()
        input_tokens = getattr(resp.usage, "input_tokens", 0) or 0
        output_tokens = getattr(resp.usage, "output_tokens", 0) or 0
        result["input_tokens"] = input_tokens
        result["output_tokens"] = output_tokens
        result["cost_usd"] = estimate_cost_usd(model, input_tokens, output_tokens)
        if text:
            result["ok"] = True
            result["text"] = text
        else:
            result["error"] = "The model returned an empty response - please try again."
        return result
    except Exception as exc:
        result["error"] = f"Couldn't get an answer right now ({exc}) - please try again."
        return result
