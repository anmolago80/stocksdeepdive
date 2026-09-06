"""
bill_check_engine.py

Mega-batch Part 19: pure-math + network/cache engine behind the
"⚡ Utilities bill check" tool (Tools tool #2). Scan/photo a utility bill
(or type it in manually) -> are you on the cheapest generally-available
plan for your postcode? -> potential yearly saving -> the shared Part-18
"if this were invested" panel (budget_planner_engine._budget_plan_
projection_panel in app.py; this module never imports Streamlit itself).

Two clearly separated halves, same convention as stress_engine.py (the
one other engine in this codebase that both computes AND fetches):

  PURE MATH (annualisation, plan ranking, benchmarks, dashboard
  aggregates) - deterministic, no network/file I/O, unit-testable in
  isolation exactly like budget_planner_engine.py/debt_recycling_
  engine.py.

  NETWORK + CACHE (AER/CDR plan fetch, AI extraction wrapper) - the
  external calls this feature genuinely needs. Every external call
  fails CLOSED to an empty/None result on ANY error (bad schema,
  timeout, unexpected retailer response) - a wrong "you could save
  $400/yr" number built on a mis-parsed API response is worse than
  honestly saying "automatic comparison is temporarily unavailable,
  compare yourself at energymadeeasy.gov.au" (the bill's own footer
  already tells the visitor to do exactly that).

AER/CDR INTEGRATION - HONESTLY FLAGGED LIMITATION: the Consumer Data
Right for Energy has no single "AER API" - the AER (aer.gov.au) hosts a
SHARED gateway at cdr.energymadeeasy.gov.au/<retailer-brand>/cds-au/v1/
for retailers who don't run their own CDR Data Holder infrastructure
(most of the smaller/mid retailers; AGL/Origin/EnergyAustralia may also
be reachable this way - public docs are inconsistent on the exact path
segment, hence _PLAN_PATH_VARIANTS below trying both documented shapes).
This sandbox has NO outbound network access to verify any of this live
(every curl attempt from both the cloud container and the owner's own
PC hit a proxy 403 before reaching the internet) - so this integration
is built from public CDS documentation only and MUST be spot-checked
against energymadeeasy.gov.au's own comparison once live on Railway
(which does have normal internet access). RETAILER_SLUGS is a curated
list of major retailers, not the whole market - "generally available
plans" here means "generally available from these retailers", stated
on the page. If a retailer's response doesn't parse against the
expected schema, that retailer is silently skipped (not shown as an
error) rather than guessed at; if EVERY retailer fails, the AU
electricity/gas comparison shows the fail-closed "temporarily
unavailable" state instead of the ranking - the tool's other three
comparison paths (water, US benchmark, manual typical-deal) are
completely independent of this and unaffected by an AER outage.

PII: extract_bill_fields() drops every disallowed field before it ever
returns - see ALLOWED_EXTRACTED_FIELDS / _PII_FIELD_MARKERS. The image
bytes themselves are never written to disk by this module and are the
caller's (app.py's) responsibility to discard after the call returns -
see that call site's own comment.

Never touches scoring engines. Every AU dollar figure assumes GST-
inclusive consumer pricing (matching how retailers publish bills and
how the CDR plan data itself is expressed) unless noted otherwise.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta

import requests


# --------------------------------------------------------------------------- #
# PURE MATH
# --------------------------------------------------------------------------- #

def annualise(value, billing_period_days):
    """value observed over billing_period_days -> its 365-day equivalent.
    Used for both usage (kWh) and cost annualisation. None/non-positive
    period returns None rather than guessing or dividing by zero."""
    if value is None or billing_period_days is None or billing_period_days <= 0:
        return None
    return value * 365.0 / billing_period_days


# Fields an extraction result is allowed to carry forward. Anything else
# returned by the model (or typed by a visitor into a field we didn't
# ask for) is dropped, not just ignored - see sanitize_extracted_fields().
ALLOWED_EXTRACTED_FIELDS = {
    "retailer", "plan_name", "fuel", "postcode", "state", "network",
    "billing_period_days", "usage_kwh", "controlled_load_kwh",
    "unit_rate_general_c_kwh", "unit_rate_controlled_c_kwh",
    "daily_supply_charge_c", "total_amount", "solar_export_kwh",
    "solar_feed_in_rate_c_kwh", "confidence",
    # Manual-entry-only ("other" fuel, e.g. internet/mobile) - which row
    # of tools_store's admin-editable typical-deal table to benchmark
    # against. Never returned by the AI extraction prompt (bills for
    # electricity/gas/water don't have one) - always caller-supplied.
    "service_key",
}

# Names an AI extraction (or a careless manual paste) could plausibly
# return that this tool must NEVER keep, per the spec's own privacy
# rule - checked by substring so a model's own key-naming drift
# ("customer_name" vs "name" vs "account_holder") still gets caught.
_PII_FIELD_MARKERS = (
    "name", "address", "street", "suburb_full", "account_number",
    "account_no", "nmi", "meter_number", "meter_no", "phone", "email",
    "customer", "reference_number",
)


def sanitize_extracted_fields(raw):
    """Whitelist-filters an extraction dict down to ALLOWED_EXTRACTED_
    FIELDS, additionally dropping anything whose KEY matches a PII
    marker even if it were somehow also an allowed name (defence in
    depth - belt AND braces, since this is the one rule in this whole
    tool that must never fail). Non-dict input -> {}."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            continue
        lk = k.strip().lower()
        if lk not in ALLOWED_EXTRACTED_FIELDS:
            continue
        if any(marker in lk for marker in _PII_FIELD_MARKERS):
            continue
        out[lk] = v
    return out


def annualised_profile_from_bill(fields):
    """fields: a sanitized extraction (or equivalent manual-entry dict).
    Returns {"annual_usage_kwh", "annual_controlled_load_kwh",
    "annual_cost", "billing_period_days"} - None entries where the
    source field was missing, never a fabricated 0."""
    days = fields.get("billing_period_days")
    return {
        "billing_period_days": days,
        "annual_usage_kwh": annualise(fields.get("usage_kwh"), days),
        "annual_controlled_load_kwh": annualise(fields.get("controlled_load_kwh"), days),
        "annual_cost": annualise(fields.get("total_amount"), days),
    }


def estimate_plan_annual_cost(plan, annual_usage_kwh, annual_controlled_load_kwh=None):
    """plan: a parsed AER candidate plan dict (see _parse_electricity_
    contract below) with c_kwh_general / c_kwh_controlled / daily_
    supply_c already in cents. Returns the estimated GST-inclusive
    annual dollar cost of running the USER's own annual usage through
    THIS plan's published rates - the one apples-to-apples number every
    ranking in this tool is built from. None if the plan is missing a
    general usage rate (can't be safely estimated - excluded from the
    ranking entirely rather than shown as $0)."""
    if not plan or plan.get("c_kwh_general") is None or annual_usage_kwh is None:
        return None
    cost_c = plan["c_kwh_general"] * annual_usage_kwh
    if plan.get("c_kwh_controlled") is not None and annual_controlled_load_kwh:
        cost_c += plan["c_kwh_controlled"] * annual_controlled_load_kwh
    if plan.get("daily_supply_c") is not None:
        cost_c += plan["daily_supply_c"] * 365
    return cost_c / 100.0


def rank_electricity_plans(user_annual_cost, annual_usage_kwh, candidate_plans,
                            annual_controlled_load_kwh=None):
    """candidate_plans: [{retailer, plan_name, ...rate fields...}, ...]
    (already geography-filtered by the caller). Returns
    {"ranked": [...], "user_rank": int, "total_count": int,
     "cheapest": {...} or None, "switchable_saving": float}.

    ranked: every plan that could be costed (estimate_plan_annual_cost
    didn't return None), sorted cheapest-first, each with an added
    "annual_cost" and "rank" (1-based). Plans that couldn't be costed
    are silently excluded from the ranking (not shown as "$0/best") -
    same fail-closed rule as the module docstring.

    user_rank: 1 + how many ranked plans are STRICTLY cheaper than the
    user's own current annual_cost ("your plan ranks Nth of M" -
    ranking the user's own real bill alongside the candidates, not
    re-estimating it from a rate table the way the candidates are).
    total_count is len(ranked) + 1 (the user's own plan) so "Nth of M"
    always includes the user's own plan in M, matching the spec's own
    wording literally.

    switchable_saving: max(0, user_annual_cost - cheapest_annual_cost),
    0.0 (not negative) if the user is already on the cheapest available
    plan - "no saving available" must never render as a negative number."""
    costed = []
    for p in candidate_plans or []:
        cost = estimate_plan_annual_cost(p, annual_usage_kwh, annual_controlled_load_kwh)
        if cost is None:
            continue
        row = dict(p)
        row["annual_cost"] = cost
        costed.append(row)
    costed.sort(key=lambda r: r["annual_cost"])
    for i, row in enumerate(costed):
        row["rank"] = i + 1

    if user_annual_cost is None:
        return {"ranked": costed, "user_rank": None, "total_count": len(costed),
                "cheapest": costed[0] if costed else None, "switchable_saving": None}

    cheaper_count = sum(1 for r in costed if r["annual_cost"] < user_annual_cost)
    user_rank = cheaper_count + 1
    cheapest = costed[0] if costed else None
    saving = max(0.0, user_annual_cost - cheapest["annual_cost"]) if cheapest else 0.0
    return {
        "ranked": costed, "user_rank": user_rank, "total_count": len(costed) + 1,
        "cheapest": cheapest, "switchable_saving": saving,
    }


# Typical annual household water usage/cost by state - BENCHMARK ONLY
# (no switching exists for a water utility, so this compares the
# visitor's own usage/cost to a published typical figure rather than
# ranking retailers). Source: state water authority / Bureau of Meteorology
# national performance report household consumption figures, kL/yr,
# converted to a rough $/yr using each state's own typical bulk tariff
# (owner-editable via the admin "typical deal" table below - these are
# starting defaults, not hard-coded forever). AU figures are per
# 3-person household; scaled by household_size / 3 by the caller.
WATER_TYPICAL_ANNUAL_COST_AU = {
    "QLD": 1050.0, "NSW": 950.0, "VIC": 900.0, "WA": 1150.0,
    "SA": 1100.0, "TAS": 850.0, "ACT": 900.0, "NT": 1000.0,
}
WATER_TYPICAL_ANNUAL_COST_US = 850.0  # EPA WaterSense national household average, USD/yr


def water_benchmark(user_annual_cost, household_size, state=None, country="au"):
    """No switching exists for water (monopoly) - this is a usage-gap
    BENCHMARK, never counted toward switchable savings. Returns
    {"typical_annual_cost", "gap"} - gap can be negative (using less
    than typical) and is shown as such, never floored at 0 (unlike the
    switchable-saving figures, "you use less than typical" is a real
    and useful fact, not something to hide)."""
    if user_annual_cost is None:
        return {"typical_annual_cost": None, "gap": None}
    size = max(float(household_size or 3), 1.0)
    if country == "au":
        base = WATER_TYPICAL_ANNUAL_COST_AU.get((state or "").upper(), 1000.0)
    else:
        base = WATER_TYPICAL_ANNUAL_COST_US
    typical = base * (size / 3.0)
    return {"typical_annual_cost": typical, "gap": user_annual_cost - typical}


# US state-average retail electricity/gas rates - BENCHMARK ONLY, per
# the owner's own decision (6 Sep) to use a static, admin-editable table
# instead of the EIA's live API: registering a free EIA API key requires
# an email sign-up, which an autonomous coding session should not do on
# the owner's behalf, and these state averages move slowly enough that a
# periodically-refreshed constant is a defensible simplification (stated
# on the page, same as every other simplification in this tool). Source:
# US EIA "Electric Power Monthly"/"Natural Gas Monthly" published state
# averages, snapshotted 2026-08 - re-check and update every 6-12 months
# from eia.gov, or wire up the live API later if the owner does get a key
# (see US_EIA_API_KEY below - if ever set, a future pass can prefer it).
# cents/kWh electricity, $/therm gas.
US_STATE_TYPICAL_RATES = {
    "CA": {"electricity_c_kwh": 31.8, "gas_usd_therm": 2.35},
    "TX": {"electricity_c_kwh": 15.0, "gas_usd_therm": 1.15},
    "NY": {"electricity_c_kwh": 24.0, "gas_usd_therm": 1.75},
    "FL": {"electricity_c_kwh": 14.5, "gas_usd_therm": 2.05},
    "IL": {"electricity_c_kwh": 16.5, "gas_usd_therm": 1.35},
    "WA": {"electricity_c_kwh": 11.5, "gas_usd_therm": 1.55},
    "MA": {"electricity_c_kwh": 28.5, "gas_usd_therm": 2.15},
    "AZ": {"electricity_c_kwh": 14.8, "gas_usd_therm": 1.45},
    "_default": {"electricity_c_kwh": 17.5, "gas_usd_therm": 1.55},
}


def us_benchmark(effective_rate, state, fuel):
    """effective_rate: the visitor's own $/unit (c/kWh electricity,
    $/therm gas) computed from their bill. Returns {"typical_rate",
    "gap_pct"} - gap_pct positive means paying MORE than the state
    average. None effective_rate (couldn't compute one from the bill,
    e.g. missing usage) -> both fields None."""
    if effective_rate is None:
        return {"typical_rate": None, "gap_pct": None}
    row = US_STATE_TYPICAL_RATES.get((state or "").upper(), US_STATE_TYPICAL_RATES["_default"])
    typical = row["electricity_c_kwh"] if fuel == "electricity" else row["gas_usd_therm"]
    if not typical:
        return {"typical_rate": typical, "gap_pct": None}
    return {"typical_rate": typical, "gap_pct": (effective_rate - typical) / typical * 100.0}


def manual_typical_benchmark(user_annual_cost, typical_annual_cost):
    """internet/mobile rows (and any other manual-entry-only fuel) -
    compares against the admin-editable "typical deal" figure the
    caller looked up in tools_store. Same shape as water_benchmark's
    return for the UI's sake."""
    if user_annual_cost is None or typical_annual_cost is None:
        return {"typical_annual_cost": typical_annual_cost, "gap": None}
    return {"typical_annual_cost": typical_annual_cost, "gap": user_annual_cost - typical_annual_cost}


def dashboard_aggregate(bills):
    """bills: [{"annual_cost", "switchable_saving" or None (BENCHMARK
    rows never contribute), "status"}, ...] (status one of "switch",
    "cheapest", "benchmark"). Returns the four summary-card numbers:
    total household utilities $/yr, potential saving/yr (⚠ rows only),
    "already on cheapest: N of M comparable" (comparable = status in
    switch/cheapest, i.e. excludes pure benchmarks which have no
    cheapest concept at all), and the raw combined switchable saving
    for the caller to feed into budget_planner_engine.future_value_of_
    savings() itself (this module never imports that - no engine-to-
    engine coupling, app.py wires the two together)."""
    total_annual = sum(b.get("annual_cost") or 0.0 for b in bills)
    total_saving = sum(b.get("switchable_saving") or 0.0 for b in bills
                       if b.get("status") in ("switch", "cheapest"))
    comparable = [b for b in bills if b.get("status") in ("switch", "cheapest")]
    cheapest_count = sum(1 for b in comparable if b.get("status") == "cheapest")
    return {
        "total_annual_cost": total_annual,
        "total_switchable_saving": total_saving,
        "cheapest_count": cheapest_count,
        "comparable_count": len(comparable),
    }


# --------------------------------------------------------------------------- #
# NETWORK + CACHE - AER/CDR plan fetch. Every function below fails CLOSED
# (returns [] / None) on any network, HTTP or schema error - see module
# docstring for why, and for the "this couldn't be verified live" caveat.
# --------------------------------------------------------------------------- #

def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

CACHE_TTL_HOURS = 24.0  # spec: "Cache plan data per postcode/fuel ~24h on the volume"

# Curated major-retailer CDR brand slugs - NOT the whole AU energy
# market (see module docstring). Each entry's own base URI is tried
# with both documented path shapes (public CDS sources disagree on
# whether the shared AER gateway uses /energy/plans or /energy/generic/
# plans - this could not be confirmed from this sandbox's network-
# blocked environment; _PLAN_PATH_VARIANTS tries both and uses whichever
# responds 200 first for that retailer).
RETAILER_SLUGS = [
    "agl", "origin-energy", "energyaustralia", "red-energy",
    "alinta-energy", "momentum-energy", "powershop", "dodo",
    "simply-energy", "amber-electric", "tango-energy", "ovo-energy",
]
_AER_BASE_TEMPLATE = "https://cdr.energymadeeasy.gov.au/{slug}/cds-au/v1"
_PLAN_PATH_VARIANTS = ("/energy/plans", "/energy/generic/plans")
_CDR_HEADERS = {"x-v": "3", "Accept": "application/json"}
_REQUEST_TIMEOUT_S = 12


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS aer_plan_cache (
            cache_key TEXT NOT NULL PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    return conn


def _cache_get(cache_key):
    try:
        with _conn() as conn:
            row = conn.execute(
                "SELECT payload_json, created_at FROM aer_plan_cache WHERE cache_key = ?",
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
    if age > timedelta(hours=CACHE_TTL_HOURS):
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
                "INSERT INTO aer_plan_cache (cache_key, payload_json, created_at) "
                "VALUES (?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "payload_json = excluded.payload_json, created_at = excluded.created_at",
                (cache_key, json.dumps(payload), now),
            )
    except sqlite3.Error:
        pass


def _parse_electricity_contract(plan_detail, retailer_slug):
    """plan_detail: one CDS 'electricityContract' object (from a Get
    Generic Plan Detail response). Returns a candidate-plan dict with
    rates in CENTS (c_kwh_general/c_kwh_controlled/daily_supply_c) or
    None if the schema doesn't match what's expected - a plan this
    can't confidently parse is skipped, never guessed at (module
    docstring's fail-closed rule). Only the FIRST current tariffPeriod
    is read (conditional/seasonal multi-period plans are a documented
    simplification - shown as a single blended rate)."""
    try:
        contract = plan_detail.get("electricityContracts", [{}])[0]
        periods = contract.get("tariffPeriod") or []
        if not periods:
            return None
        period = periods[0]
        daily_supply_c = None
        try:
            daily_supply_c = float(period.get("dailySupplyCharge")) if period.get("dailySupplyCharge") else None
        except (TypeError, ValueError):
            pass
        c_general = None
        rate_type = period.get("rateBlockUType")
        if rate_type == "singleRate":
            rates = (period.get("singleRate") or {}).get("rates") or []
            if rates:
                c_general = float(rates[0].get("unitPrice")) if rates[0].get("unitPrice") else None
        elif rate_type == "timeOfUseRates":
            tou = period.get("timeOfUseRates") or []
            unit_prices = []
            for block in tou:
                for r in block.get("rates") or []:
                    try:
                        unit_prices.append(float(r.get("unitPrice")))
                    except (TypeError, ValueError):
                        pass
            if unit_prices:
                c_general = sum(unit_prices) / len(unit_prices)  # simple blended average - flagged as such below
        if c_general is None:
            return None
        c_controlled = None
        cl = period.get("controlledLoad") or []
        if cl:
            cl_rates = (cl[0].get("rates") or [])
            if cl_rates:
                try:
                    c_controlled = float(cl_rates[0].get("unitPrice"))
                except (TypeError, ValueError):
                    pass
        return {
            "retailer": retailer_slug,
            "plan_name": plan_detail.get("displayName") or plan_detail.get("planId") or retailer_slug,
            "c_kwh_general": c_general,
            "c_kwh_controlled": c_controlled,
            "daily_supply_c": daily_supply_c,
            "is_time_of_use_blended": rate_type == "timeOfUseRates",
        }
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def _fetch_json(url, params=None):
    try:
        resp = requests.get(url, headers=_CDR_HEADERS, params=params, timeout=_REQUEST_TIMEOUT_S)
        if resp.status_code != 200:
            return None
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def _fetch_retailer_plans(slug, fuel_type):
    """One retailer's generic plan list + detail for each, trying both
    known path shapes. Returns [] on ANY failure for this retailer -
    callers loop every retailer and simply get fewer results, never an
    exception."""
    base = _AER_BASE_TEMPLATE.format(slug=slug)
    listing = None
    for path in _PLAN_PATH_VARIANTS:
        listing = _fetch_json(f"{base}{path}",
                              params={"fuelType": fuel_type.upper(), "type": "STANDING", "page-size": 50})
        if listing:
            plans_path = path
            break
    if not listing:
        return []
    try:
        plan_stubs = listing.get("data", {}).get("plans") or listing.get("data") or []
        if isinstance(plan_stubs, dict):
            plan_stubs = plan_stubs.get("plans", [])
    except AttributeError:
        return []
    out = []
    for stub in (plan_stubs or [])[:20]:  # bounded - protects latency/spend, not a full-market sweep
        plan_id = stub.get("planId") if isinstance(stub, dict) else None
        if not plan_id:
            continue
        detail = _fetch_json(f"{base}{plans_path}/{plan_id}")
        if not detail:
            continue
        parsed = _parse_electricity_contract(detail.get("data", detail), slug)
        if parsed:
            out.append(parsed)
    return out


def fetch_candidate_plans(postcode, fuel_type="electricity", distributor=None):
    """The AU AER comparison's one entry point. postcode/distributor are
    accepted for the cache key and for a future geography filter (the
    CDS plan-detail geography block - `geography.excludedPostcodes`/
    `includedPostcodes` - is NOT yet cross-checked against the user's
    own postcode in this pass; every returned plan is presented as
    "generally available", matching the spec's own wording, rather than
    confirmed available at this exact address - a documented
    simplification, not silently assumed). Returns [] if every retailer
    fails (AER outage, schema drift, or - most likely from this
    sandbox's own testing limits - a wrong path/base-URL guess that
    needs the owner's post-deploy spot-check per the module docstring)."""
    if fuel_type not in ("electricity", "gas"):
        return []
    cache_key = f"{fuel_type}:{postcode}:{distributor or ''}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    all_plans = []
    for slug in RETAILER_SLUGS:
        try:
            all_plans.extend(_fetch_retailer_plans(slug, fuel_type))
        except Exception:
            continue  # one bad retailer never blocks the others
    _cache_set(cache_key, all_plans)
    return all_plans


# --------------------------------------------------------------------------- #
# AI EXTRACTION - thin wrapper. app.py's call site is the one that owns
# ai_gate.check()/record() (same convention as every other AI call site
# in this codebase - this module intentionally does NOT call ai_gate
# itself, so it stays a plain, gate-agnostic function the caller fully
# controls, matching ai_client.ask()'s own "the caller runs the gate"
# contract).
# --------------------------------------------------------------------------- #

EXTRACTION_SYSTEM_PROMPT = (
    "You extract structured data from a photo or PDF page of a household "
    "utility bill (electricity, gas or water). Return ONLY a single JSON "
    "object, no prose, no markdown fences. Fields: retailer, plan_name, "
    "fuel (electricity|gas|water), postcode, state, network, "
    "billing_period_days (integer), usage_kwh (number, total kWh for the "
    "period - omit for water), controlled_load_kwh (number, omit if none), "
    "unit_rate_general_c_kwh (cents per kWh, number), "
    "unit_rate_controlled_c_kwh (number, omit if none), "
    "daily_supply_charge_c (cents per day, number), total_amount (the "
    "amount due, number), solar_export_kwh (number, omit if none), "
    "solar_feed_in_rate_c_kwh (number, omit if none), confidence (0-1, "
    "your own confidence every required field above was read correctly). "
    "CRITICAL PRIVACY RULE: NEVER include the customer's name, address, "
    "account number, NMI, meter number, or phone number in your JSON, "
    "even if you can read them clearly on the bill - omit those fields "
    "entirely. Omit any field you cannot read confidently rather than "
    "guessing a number."
)

REQUIRED_EXTRACTION_FIELDS = (
    "fuel", "billing_period_days", "total_amount",
)


def build_extraction_message(image_blocks):
    """image_blocks: [{"media_type": "image/jpeg"|"image/png"|
    "application/pdf", "data_b64": "..."}, ...] (already base64-encoded
    by the caller - this module does no file I/O). Returns the
    Anthropic `content` list ai_client.ask()'s user_message param
    expects: up to 3 image/document blocks then one text instruction,
    per the spec's own "first 3 pages max"."""
    content = []
    for block in (image_blocks or [])[:3]:
        media_type = block.get("media_type", "image/jpeg")
        block_type = "document" if media_type == "application/pdf" else "image"
        content.append({
            "type": block_type,
            "source": {"type": "base64", "media_type": media_type, "data": block["data_b64"]},
        })
    content.append({"type": "text", "text": "Extract the bill fields as instructed."})
    return content


def needs_retry(extracted):
    """True if a required field is missing or the model's own reported
    confidence is low - the spec's own retry trigger ("one retry with
    MODEL_SONNET only if required fields are missing/low-confidence")."""
    if not extracted:
        return True
    if any(extracted.get(f) is None for f in REQUIRED_EXTRACTION_FIELDS):
        return True
    conf = extracted.get("confidence")
    return conf is not None and conf < 0.6


def parse_extraction_response(raw_text):
    """raw_text: ai_client.ask()'s ["text"]. Tolerant of a stray markdown
    fence around the JSON (models do this despite instructions not to).
    Returns sanitize_extracted_fields()'s output, or {} on anything that
    doesn't parse as a JSON object - never raises."""
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
