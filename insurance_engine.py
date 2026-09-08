"""
insurance_engine.py

Mega-batch Part 22: pure-math + network/cache engine behind the
"🛡️ Insurance bill check" tool (Tools tool #4) - a sibling of ⚡ Utilities
bill check (bill_check_engine.py, Part 19), reusing that tool's own
machinery wherever it exists rather than rebuilding it: the same
AI-extraction call shape (build_extraction_message is imported straight
from bill_check_engine, unchanged), the same sanitize-then-compare-then-
save flow, the same tools_store.bill_checks table (insurance rows are
just rows with fuel="ins_health"/"ins_car"/"ins_home"/"ins_ctp" - "they
are bills", per the instruction's own words), the same shared invested-
projection panel, and - because tools_store.can_check()/record_check_
usage()/bill_check_usage_status() were already written keyed on email
alone, never on which TOOL is asking - the SAME trial/cap pool as
Utilities with zero changes needed to that gating code at all.

Two clearly separated halves, same convention as bill_check_engine.py/
stress_engine.py:

  PURE MATH (premium annualisation, health-policy ranking, car/home/CTP
  renewal-creep comparison, benchmark gap) - deterministic, no network/
  file I/O, unit-testable in isolation.

  NETWORK + CACHE (the health-insurance government dataset fetch, the AI
  extraction wrapper) - the one external call this feature genuinely
  needs. Fails CLOSED to an empty/None result on ANY error, same
  non-negotiable rule as bill_check_engine.py's own AER integration and
  for the identical reason: a wrong "cheapest comparable policy is
  $X/yr" number built on a mis-parsed government file is worse than
  honestly saying "automatic comparison is temporarily unavailable,
  compare yourself at privatehealth.gov.au" (which is also what this
  tool always links to, unconditionally, per the spec's own "always
  link" rule).

HEALTH-INSURANCE DATA SOURCE - VERIFIED AT BUILD TIME (2026-09-07), per
the instruction's own explicit requirement to verify and report this:
data.gov.au's "PrivateHealth.gov.au" dataset
(data.gov.au/data/dataset/private-health-insurance), published by the
Private Health Insurance Ombudsman (PHIO) under CC BY 3.0 AU. Confirmed
live via data.gov.au's own listing and a researchdata.edu.au record
describing its structure: a MONTHLY zip release (archives running from
before Aug 2020 through at least Jul 2026, confirming it is still being
published as of this build), each containing one Private Health
Information Statement (PHIS) XML file per registered insurer/product
(the same standardised statement every fund is legally required to
publish), an XML schema file, CSV summaries of changes since the
previous release, a CSV of hospital-agreement data, and a Readme. This
is the SAME underlying data privatehealth.gov.au's own "Compare
policies" tool is built from - the government's own comparison, not a
scrape or a private aggregator.

HONESTLY FLAGGED LIMITATION (same shape as bill_check_engine.py's own
AER/CDR note, for the same underlying reason): this sandbox has NO
outbound network access to actually download and unzip a ~60MB monthly
release or inspect a real PHIS XML file's exact tag names (every
attempt from this whole engagement, on every module that has needed
live data, has hit a blocked/proxied connection - see stress_engine.py
and bill_check_engine.py's own docstrings for the same constraint
stated independently). fetch_latest_health_dataset()/_parse_phis_xml()
below are built against PUBLIC DOCUMENTATION of the PHIS standard and
the dataset's own described structure, not against a real downloaded
file - _parse_phis_xml() tries a small set of plausible tag-name
variants per field (mirroring bill_check_engine.py's own
_PLAN_PATH_VARIANTS "public docs disagree, try more than one shape"
pattern) and SILENTLY SKIPS any product it can't confidently parse,
never guesses one. If the whole pipeline can't parse a single usable
policy from a real release (a real schema mismatch, or simply this
sandbox's own network block finally lifting on a build that still can't
parse it), rank_health_policies()'s caller shows the same fail-closed
"temporarily unavailable, compare yourself at privatehealth.gov.au"
state the AU electricity/gas path already uses for the identical
reason - this MUST be spot-checked on Railway (which has real internet
access) once deployed, exactly like the AER integration before it.

CAR / HOME / CTP: no public per-policy pricing exists for these (quotes
are individualised - a real premium depends on the specific person,
address, vehicle, claims history), so this tool never pretends to rank
one against a market the way health/electricity are ranked. Their value
is measured two other ways instead, both entirely computable from the
user's OWN data with no external fetch at all: year-over-year renewal
creep (this rescan vs. the last saved one for the same policy) and a
plain, owner-editable "typical premium" benchmark - see
renewal_creep()/typical_premium_benchmark() below. Both are always
BADGED as a benchmark, never as a switchable saving (Fix round 10-style
"benchmark never counts toward switchable savings" rule, applied here
exactly as it already is for Utilities' water/US paths).

PII: sanitize_extracted_fields() drops every disallowed field before it
ever returns - see ALLOWED_EXTRACTED_FIELDS/_PII_FIELD_MARKERS below,
extended from bill_check_engine.py's own list with insurance-specific
markers (policy number, rego/registration, VIN, member number, and any
health detail beyond the plain tier). The image bytes themselves are
never written to disk by this module, same rule as bill_check_engine.py.

Never touches scoring engines. Every AU dollar figure assumes GST-
inclusive consumer pricing (matching how insurers publish renewal
notices) unless noted otherwise.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import requests

import bill_check_engine  # reused, unchanged: build_extraction_message()


# --------------------------------------------------------------------------- #
# PURE MATH
# --------------------------------------------------------------------------- #

def annualise_premium(amount, period):
    """amount at the stated billing `period` ("year"/"month"/"fortnight"/
    "week") -> its annual equivalent. Unrecognised/missing period or a
    non-positive amount returns None rather than guessing - mirrors
    bill_check_engine.annualise()'s own "never fabricate" contract,
    just keyed by a named period instead of a day-count (insurance
    renewal notices state "per year"/"per month" plainly; they don't
    give a billing_period_days the way a utility bill's statement
    period does)."""
    if amount is None or amount <= 0:
        return None
    multiplier = {"year": 1.0, "month": 12.0, "fortnight": 26.0, "week": 52.0}.get(
        (period or "year").strip().lower()
    )
    if multiplier is None:
        return None
    return amount * multiplier


# Fields an extraction result (or a manual-entry form) is allowed to
# carry forward. Anything else is dropped - see sanitize_extracted_
# fields(). Deliberately does NOT include a free-text "coverage detail"
# field beyond the plain tier/type enums below, per the spec's own
# "never health details beyond the tier" rule - there is simply no
# field here for the model to put them in even if it tried.
ALLOWED_EXTRACTED_FIELDS = {
    "policy_type",  # health | car | home | ctp
    "insurer", "product_name", "state",
    "premium_amount", "premium_period", "excess",
    # health
    "tier",  # gold | silver | bronze | basic
    "coverage",  # single | couple | family | single_parent
    # car
    "cover_type",  # comprehensive | third_party
    "vehicle_value_basis",  # agreed | market
    # home & contents
    "sum_insured_building", "sum_insured_contents",
    "confidence",
}

# Same "defence in depth" shape as bill_check_engine.py's own
# _PII_FIELD_MARKERS - checked by substring so model key-naming drift
# is still caught - extended with the insurance-specific fields the
# spec explicitly names: policy numbers, vehicle rego, and anything
# that would be a health detail beyond the tier (condition, diagnosis,
# member number tying back to a real person).
_PII_FIELD_MARKERS = (
    "name", "address", "street", "suburb_full", "account_number",
    "account_no", "phone", "email", "customer", "reference_number",
    "policy_number", "policy_no", "member_number", "member_no",
    "rego", "registration", "vin", "chassis", "engine_number",
    "dob", "date_of_birth", "medicare", "condition", "diagnosis",
    "claim_history", "drivers_licence", "license_number",
)

# "product_name" would otherwise be caught by the "name" substring
# marker above - same named exemption as bill_check_engine.py's own
# "plan_name" (Fix round 10 #2's own diagnosis, applied here
# proactively rather than waiting to reproduce that bug): a product's
# own marketing name ("Bronze Plus Hospital", "Comprehensive Drive")
# is not personal information.
_PII_MARKER_EXEMPT_FIELDS = {"product_name"}


def sanitize_extracted_fields(raw):
    """Whitelist-filters an extraction dict down to ALLOWED_EXTRACTED_
    FIELDS, additionally dropping anything whose KEY matches a PII
    marker even if it were somehow also an allowed name (belt and
    braces, identical shape to bill_check_engine.sanitize_extracted_
    fields() - this is the one rule in this tool that must never fail).
    Non-dict input -> {}."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        lk = k.strip().lower()
        if lk not in ALLOWED_EXTRACTED_FIELDS:
            continue
        if lk not in _PII_MARKER_EXEMPT_FIELDS and any(marker in lk for marker in _PII_FIELD_MARKERS):
            continue
        out[lk] = v
    return out


HEALTH_TIERS = ("gold", "silver", "bronze", "basic")
HEALTH_COVERAGE = ("single", "couple", "family", "single_parent")


def rank_health_policies(user_annual_premium, tier, coverage, state, candidate_policies):
    """candidate_policies: [{insurer, product_name, tier, coverage,
    state, annual_premium}, ...] already filtered to the SAME tier +
    coverage + state as the user's own policy (the caller does this
    filtering - kept here as a plain list-in so this function stays
    trivially unit-testable against a hand-built candidate list, the
    same convention as bill_check_engine.rank_electricity_plans()).

    Deliberately the SAME shape as rank_electricity_plans() - ranked
    list (cheapest-first, each with "rank"), user_rank ("Nth of M",
    M including the user's own policy), cheapest/2nd/3rd, and
    switchable_saving (max(0, ...) - never negative, "no saving
    available" must never render as one). Reusing that exact contract
    rather than inventing a new shape means the render layer can treat
    a health-policy ranking and an electricity-plan ranking almost
    identically."""
    costed = []
    for p in candidate_policies or []:
        cost = p.get("annual_premium")
        if cost is None or cost <= 0:
            continue
        row = dict(p)
        row["annual_cost"] = cost
        costed.append(row)
    costed.sort(key=lambda r: r["annual_cost"])
    for i, row in enumerate(costed):
        row["rank"] = i + 1

    if user_annual_premium is None:
        return {"ranked": costed, "user_rank": None, "total_count": len(costed),
                "cheapest": costed[0] if costed else None, "switchable_saving": None}

    cheaper_count = sum(1 for r in costed if r["annual_cost"] < user_annual_premium)
    user_rank = cheaper_count + 1
    cheapest = costed[0] if costed else None
    saving = max(0.0, user_annual_premium - cheapest["annual_cost"]) if cheapest else 0.0
    return {
        "ranked": costed, "user_rank": user_rank, "total_count": len(costed) + 1,
        "cheapest": cheapest, "switchable_saving": saving,
    }


def renewal_creep(prior_extra, current_premium, current_sum_insured=None):
    """Car/home/CTP's one genuinely-computable signal: this rescan's
    premium (and, when both readings have one, sum insured) against the
    PRIOR saved reading for the SAME policy (tools_store carries the
    prior extra_json forward into bill_check_history for exactly this -
    see that module's own docstring). prior_extra: the previous save's
    extra dict (None/{} if this is the first-ever check for this
    policy - the caller then shows "scan last year's notice too" per
    the spec instead of a creep line). Returns None if there's no prior
    reading, or the prior reading had no premium to compare against;
    otherwise {"premium_change_pct", "sum_insured_change_pct" (None if
    either reading lacks a sum-insured figure - never fabricated from a
    missing one)}."""
    if not prior_extra:
        return None
    prior_premium = prior_extra.get("annual_premium")
    if prior_premium is None or prior_premium <= 0 or current_premium is None:
        return None
    out = {"premium_change_pct": (current_premium - prior_premium) / prior_premium * 100.0}
    prior_si = prior_extra.get("sum_insured")
    if prior_si and current_sum_insured:
        out["sum_insured_change_pct"] = (current_sum_insured - prior_si) / prior_si * 100.0
    else:
        out["sum_insured_change_pct"] = None
    return out


def typical_premium_benchmark(user_annual_premium, typical_annual_premium):
    """Owner-editable "typical premium" table lookup result - same
    shape/contract as bill_check_engine.manual_typical_benchmark() (the
    internet/mobile "other" fuel path), reused here conceptually rather
    than imported since the caller looks the figure up from a DIFFERENT
    tools_store table key namespace (see tools_store.py's own note on
    reusing get_typical_deal_rates()/set_typical_deal_rate() with
    "ins_<policy_type>:<state>" keys instead of a new table)."""
    if user_annual_premium is None or typical_annual_premium is None:
        return {"typical_annual_cost": typical_annual_premium, "gap": None}
    return {"typical_annual_cost": typical_annual_premium, "gap": user_annual_premium - typical_annual_premium}


def excess_vs_premium_note_applicable(policy_type):
    """True for the three BENCHMARK-only policy types the spec's own
    excess-vs-premium factual note applies to (car/home/CTP) - never
    health, which has its own TRUE comparison and no user-set excess
    lever in the same sense."""
    return (policy_type or "").lower() in ("car", "home", "ctp")


def dashboard_aggregate(bills):
    """Delegates to bill_check_engine.dashboard_aggregate() UNCHANGED -
    insurance rows are just rows in the SAME bill_checks table with a
    fuel value that happens to start with "ins_" (see module docstring)
    and the SAME status vocabulary (switch/cheapest/benchmark), so the
    existing aggregate function already produces the right combined
    totals with zero modification. Kept as a thin named re-export here
    purely so callers can `import insurance_engine` and not need to
    remember which sibling module actually owns this function."""
    return bill_check_engine.dashboard_aggregate(bills)


# --------------------------------------------------------------------------- #
# NETWORK + CACHE - the health-insurance government dataset. Fails CLOSED
# (returns [] / None) on ANY network, HTTP, zip or XML-schema error - see
# module docstring for why, and for the "this couldn't be verified live"
# caveat that applies to every function in this section.
# --------------------------------------------------------------------------- #

def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

# The dataset is published monthly (verified - see module docstring) -
# a bounded, SINGLE-ROW cache (one snapshot, not one row per query) is
# refreshed once a fetch is more than this many hours old, generous
# enough to comfortably span a month without hammering data.gov.au on
# every visitor, tight enough to pick up next month's release
# reasonably promptly. Deliberately much longer than bill_check_
# engine's 24h AER cache (a genuinely monthly source doesn't need a
# daily re-check) - see volume_monitor.py's own retention pruning for
# how this stays bounded on the volume even so (STALE_MULTIPLIER x this
# TTL, same pattern as every other cache table in this codebase).
HEALTH_DATASET_CACHE_TTL_HOURS = 24.0 * 14  # ~2 weeks

_CKAN_PACKAGE_URL = "https://data.gov.au/api/3/action/package_show?id=private-health-insurance"
_REQUEST_TIMEOUT_S = 20

# PHIS XML tag names, per field, in the order tried - public
# documentation of the exact per-release tag casing/nesting could not
# be confirmed live from this sandbox (see module docstring), so each
# field lists every plausible variant found across PHIS-standard
# descriptions; the first that resolves to a non-empty value wins, and
# a product missing every variant for a REQUIRED field is skipped
# entirely rather than guessed at (same "fail closed per-item" shape as
# bill_check_engine._parse_electricity_contract's own per-plan skip).
_PHIS_TAG_VARIANTS = {
    "insurer": ("FundName", "InsurerName", "Fund"),
    "product_name": ("ProductName", "ProductTitle"),
    "product_type": ("ProductType", "Type"),  # Hospital | GeneralHealth | Combined
    "tier": ("Tier", "ProductTier"),
    "state": ("State", "StateCode"),
    "coverage": ("CoverType", "PolicyType"),  # Single | Couple | Family | SingleParentFamily
    "premium": ("Premium", "PremiumAmount", "TotalPremium"),
    "excess": ("Excess", "ExcessAmount"),
}


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS health_policy_dataset_cache (
            cache_key TEXT NOT NULL PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    return conn


def _cache_get(cache_key, ttl_hours):
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT payload_json, created_at FROM health_policy_dataset_cache WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row[1])
    except (TypeError, ValueError):
        return None
    if age > timedelta(hours=ttl_hours):
        return None
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return None


def _cache_set(cache_key, payload):
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _conn() as conn:
            conn.execute(
                "INSERT INTO health_policy_dataset_cache (cache_key, payload_json, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "payload_json = excluded.payload_json, created_at = excluded.created_at",
                (cache_key, json.dumps(payload), now),
            )
    except sqlite3.Error:
        pass


def _fetch_json(url, params=None):
    try:
        resp = requests.get(url, params=params, timeout=_REQUEST_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def _latest_dataset_resource_url():
    """CKAN package_show -> the most-recently-created resource's own
    download URL. Discovering the latest release PROGRAMMATICALLY
    (rather than hardcoding a monthly filename that goes stale) is the
    "verify the current data source" requirement applied at RUNTIME,
    not just at this build - the exact same self-updating shape as the
    rest of this codebase's "never hardcode what can be looked up"
    convention. None on any failure (fails closed - the caller then
    shows the "temporarily unavailable" state)."""
    pkg = _fetch_json(_CKAN_PACKAGE_URL)
    if not pkg or not pkg.get("success"):
        return None
    resources = (pkg.get("result") or {}).get("resources") or []
    if not resources:
        return None
    dated = [r for r in resources if r.get("created")]
    if not dated:
        return None
    latest = max(dated, key=lambda r: r["created"])
    return latest.get("url")


def _text_of(el, tag_variants):
    """First non-empty text found under `el` for any tag name in
    `tag_variants` (direct or nested child, case-sensitive per PHIS's
    own documented PascalCase convention) - None if none match, which
    the caller treats as "this field wasn't confidently read" rather
    than a fabricated 0/blank."""
    for tag in tag_variants:
        found = el.find(f".//{tag}")
        if found is not None and found.text and found.text.strip():
            return found.text.strip()
    return None


def _parse_phis_product(product_el):
    """One <Product>-shaped element from a PHIS XML file -> a
    normalized candidate dict, or None if a REQUIRED field (insurer,
    tier, coverage, state, premium) couldn't be read under ANY of its
    tried tag-name variants - never guessed at, per module docstring."""
    try:
        insurer = _text_of(product_el, _PHIS_TAG_VARIANTS["insurer"])
        tier_raw = _text_of(product_el, _PHIS_TAG_VARIANTS["tier"])
        coverage_raw = _text_of(product_el, _PHIS_TAG_VARIANTS["coverage"])
        state = _text_of(product_el, _PHIS_TAG_VARIANTS["state"])
        premium_raw = _text_of(product_el, _PHIS_TAG_VARIANTS["premium"])
        if not (insurer and tier_raw and coverage_raw and state and premium_raw):
            return None
        try:
            premium = float(premium_raw)
        except (TypeError, ValueError):
            return None
        if premium <= 0:
            return None
        tier = tier_raw.strip().lower()
        if tier not in HEALTH_TIERS:
            return None
        coverage = coverage_raw.strip().lower().replace(" ", "_")
        if coverage not in HEALTH_COVERAGE:
            # SingleParentFamily-style CamelCase is a plausible raw
            # value per the schema description - normalise before
            # giving up on this product entirely.
            coverage = coverage.replace("singleparent", "single_parent")
            if coverage not in HEALTH_COVERAGE:
                return None
        return {
            "insurer": insurer,
            "product_name": _text_of(product_el, _PHIS_TAG_VARIANTS["product_name"]) or insurer,
            "tier": tier, "coverage": coverage, "state": state.strip().upper(),
            "annual_premium": premium,
        }
    except Exception:
        return None


def fetch_latest_health_dataset():
    """The health-insurance comparison's one entry point: every
    registered policy this pipeline could confidently parse from the
    latest PrivateHealth.gov.au monthly release, cached (single bounded
    snapshot, ~2-week TTL - see HEALTH_DATASET_CACHE_TTL_HOURS). Returns
    [] on ANY failure along the way (no network in this sandbox always
    included - see module docstring) - the caller's rank_health_
    policies() then simply has nothing to rank against, and the render
    layer shows the same "temporarily unavailable" state as an AER
    outage. This function never raises."""
    cached = _cache_get("latest", HEALTH_DATASET_CACHE_TTL_HOURS)
    if cached is not None:
        return cached
    try:
        import io
        import zipfile
        import xml.etree.ElementTree as ET

        url = _latest_dataset_resource_url()
        if not url:
            return []
        resp = requests.get(url, timeout=_REQUEST_TIMEOUT_S * 3)
        if resp.status_code != 200:
            return []
        out = []
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            for name in zf.namelist():
                if not name.lower().endswith(".xml"):
                    continue
                try:
                    with zf.open(name) as f:
                        tree = ET.parse(f)
                except ET.ParseError:
                    continue
                root = tree.getroot()
                # A PHIS file may itself hold one product or a batch of
                # several under the fund - handle both shapes by
                # scanning for every plausibly-a-product element rather
                # than assuming one fixed root layout (another
                # documented-schema-uncertainty tolerance, same spirit
                # as the tag-variant lookup above).
                candidates = root.findall(".//Product") or [root]
                for prod_el in candidates:
                    parsed = _parse_phis_product(prod_el)
                    if parsed:
                        out.append(parsed)
        _cache_set("latest", out)
        return out
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# AI EXTRACTION - thin wrapper, same "caller owns the gate" contract as
# bill_check_engine.py (this module intentionally does NOT call ai_gate
# itself). build_extraction_message() is reused directly from
# bill_check_engine - it's already fully generic over image/pdf blocks.
# --------------------------------------------------------------------------- #

build_extraction_message = bill_check_engine.build_extraction_message

EXTRACTION_SYSTEM_PROMPT = (
    "You extract structured data from a photo or PDF page of an Australian "
    "insurance renewal or policy notice - health, car, home & contents, or "
    "CTP (compulsory third party). Return ONLY a single JSON object, no "
    "prose, no markdown fences. Fields: policy_type (health|car|home|ctp), "
    "insurer, product_name (the product/plan's own marketing name, e.g. "
    "'Bronze Plus Hospital' - never the customer's name), state (2-3 letter "
    "code), premium_amount (number, the amount due), premium_period "
    "(year|month|fortnight|week - however the notice states its billing "
    "cycle), excess (number, if shown). For a HEALTH notice also include: "
    "tier (gold|silver|bronze|basic), coverage (single|couple|family| "
    "single_parent). For a CAR notice also include: cover_type "
    "(comprehensive|third_party), vehicle_value_basis (agreed|market) - "
    "NEVER the registration/rego number or VIN. For a HOME & CONTENTS "
    "notice also include: sum_insured_building, sum_insured_contents "
    "(numbers, whichever the policy covers). confidence (0-1, your own "
    "confidence every required field above was read correctly). CRITICAL "
    "PRIVACY RULE: NEVER include the customer's name, address, policy "
    "number, member number, vehicle registration/rego, VIN, date of birth, "
    "Medicare number, or ANY health detail beyond the plain tier word "
    "above (no conditions, diagnoses, or claims history), even if you can "
    "read them clearly on the notice - omit those fields entirely. Omit "
    "any field you cannot read confidently rather than guessing a number."
)

REQUIRED_EXTRACTION_FIELDS = ("policy_type", "premium_amount")

# Owner report (8 Sep 2026 - "after uploading the bills it's not reading
# the documents, it keeps all the bill data empty ... same issue we had
# before ... it's the same issue ... same with the insurance"): same gap
# as bill_check_engine.py's own fix of the same date - a None-only check
# waves through a technically-present-but-meaningless premium_amount: 0
# as "extraction worked". A real policy notice is never $0/period.
_POSITIVE_REQUIRED_FIELDS = ("premium_amount",)


def _extraction_has_degenerate_required_field(extracted):
    for f in _POSITIVE_REQUIRED_FIELDS:
        v = extracted.get(f)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            return True
    return False


def needs_retry(extracted):
    """Same trigger shape as bill_check_engine.needs_retry(): a required
    field missing or present-but-degenerate (see _extraction_has_
    degenerate_required_field()), or the model's own reported
    confidence is low."""
    if not extracted:
        return True
    if any(extracted.get(f) is None for f in REQUIRED_EXTRACTION_FIELDS):
        return True
    if _extraction_has_degenerate_required_field(extracted):
        return True
    conf = extracted.get("confidence")
    return conf is not None and conf < 0.6


def extraction_below_minimum(extracted):
    """Same post-retry gate as bill_check_engine.extraction_below_
    minimum() (Fix round 10 #2's own fix, applied here from the start
    rather than needing to reproduce that bug first; extended 8 Sep
    2026 the same way, in step with that module): True when a REQUIRED
    field is STILL missing OR degenerate (a $0 premium) after the retry
    has already run."""
    if not extracted:
        return True
    if any(extracted.get(f) is None for f in REQUIRED_EXTRACTION_FIELDS):
        return True
    return _extraction_has_degenerate_required_field(extracted)


def parse_extraction_response(raw_text):
    """Same tolerant-of-a-stray-markdown-fence parse as bill_check_
    engine.parse_extraction_response(), sanitized through THIS module's
    own (insurance-specific) field whitelist. Never raises."""
    if not raw_text:
        return {}
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return sanitize_extracted_fields(obj)
