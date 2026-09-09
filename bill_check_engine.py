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
import logging
import os
import re
import sqlite3
from datetime import datetime, timezone, timedelta

import requests

# Shared with app.py's own _tools_logger (same logger NAME - Python's
# logging.getLogger returns the SAME instance for a given name from
# anywhere in the process) - this module can't import app.py (would be
# circular), so it gets its own handle to the identical channel rather
# than inventing a second one. Confirmed live on Railway (8 Sep 2026):
# a bare logging.warning() call on this logger reaches the deploy logs
# with no extra setup, unlike a plain print() (see app.py's own history
# on this exact point).
_logger = logging.getLogger("sdd.tools")


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

# Fix round 10 #2 (diagnosis): the "name" marker above is deliberately a
# broad substring match (catches "customer_name"/"account_holder_name"
# drift) - but that same substring also matches "plan_name", an
# EXPLICITLY allowed field (a product name, not a person's). The result
# was silent: plan_name passed the whitelist check, then got dropped one
# line later by the PII check, on every single extraction, no exception
# raised. This is the one allowed field the broad substring match was
# always going to catch, so it's the one named exemption rather than
# narrowing the markers (which would weaken the drift-catching they're
# there for).
_PII_MARKER_EXEMPT_FIELDS = {"plan_name"}


def sanitize_extracted_fields(raw):
    """Whitelist-filters an extraction dict down to ALLOWED_EXTRACTED_
    FIELDS, additionally dropping anything whose KEY matches a PII
    marker even if it were somehow also an allowed name (defence in
    depth - belt AND braces, since this is the one rule in this whole
    tool that must never fail) - except _PII_MARKER_EXEMPT_FIELDS, the
    allowed fields already known not to be PII despite matching a
    marker substring. Non-dict input -> {}."""
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
# 8 Sep 2026 fix (owner report: comparison stuck on "temporarily
# unavailable" even after the underlying fetch bug was fixed) - a total
# fetch failure (every retailer down/rejected) used to be cached at the
# SAME 24h TTL as a real result, so one bad run silently blanked the
# comparison for a full day. An empty result now only sticks for 15
# minutes - see _cache_get.
_EMPTY_CACHE_TTL_HOURS = 0.25

# Curated major-retailer CDR brand slugs - NOT the whole AU energy
# market (see module docstring). Each entry's own base URI is tried
# with both documented path shapes (public CDS sources disagree on
# whether the shared AER gateway uses /energy/plans or /energy/generic/
# plans - this could not be confirmed from this sandbox's network-
# blocked environment; _PLAN_PATH_VARIANTS tries both and uses whichever
# responds 200 first for that retailer).
# 8 Sep 2026 fix: "origin-energy" replaced with "origin" - independently
# confirmed (public third-party documentation of a live 2026-07-27
# fetch against this exact gateway: 3,595 Origin plans under the
# "origin" slug specifically) rather than assumed; "origin-energy" was
# never confirmed and risks silently returning zero plans for this
# retailer if wrong. "engie"/"globird-energy" added per the owner's own
# report of testing them live - this sandbox has no outbound network
# access to re-verify that (see module docstring), but a wrong slug
# here costs nothing: _fetch_retailer_plans already fails closed and
# just logs+skips a retailer whose slug doesn't resolve, exactly like
# any other retailer outage.
RETAILER_SLUGS = [
    "agl", "origin", "energyaustralia", "red-energy",
    "alinta-energy", "momentum-energy", "powershop", "dodo",
    "simply-energy", "amber-electric", "tango-energy", "ovo-energy",
    "engie", "globird-energy",
]
_AER_BASE_TEMPLATE = "https://cdr.energymadeeasy.gov.au/{slug}/cds-au/v1"
_PLAN_PATH_VARIANTS = ("/energy/plans", "/energy/generic/plans")
# 8 Sep 2026 fix - the root cause of the "temporarily unavailable" report:
# every request sent x-v: 3 regardless of endpoint, 406-ing on this
# gateway's listing endpoint (independently confirmed live 2026-07-27 by
# a third party against this exact host: listing wants x-v: 1). This
# sandbox still has no outbound network access to confirm the DETAIL
# endpoint's own version live (see module docstring) - rather than
# hardcode a second guessed number, _fetch_json now starts every request
# at x-v: 1 and, on a 406, reads the gateway's OWN error body for its
# min=/max= hint and retries once with THAT exact version. This is
# correct today by construction (the gateway is the authority on its own
# version, not a guess written into this file) and stays correct if the
# CDS Energy standard is bumped again later.
_CDR_HEADERS = {"Accept": "application/json"}
_DEFAULT_XV = "1"
_XV_HINT_RE = re.compile(r"\b(?:min|max)\s*[=:]\s*(\d+)")
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
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    # 8 Sep 2026 fix: see _EMPTY_CACHE_TTL_HOURS above - an empty
    # (total-failure) result is only trusted for 15 minutes, not the
    # full 24h a real result gets. This alone self-heals any entry
    # already poisoned by the old unconditional-24h bug the moment this
    # deploys - no separate cache-clear step needed, since a poisoned
    # entry is by now almost certainly already older than 15 minutes.
    ttl = timedelta(hours=CACHE_TTL_HOURS) if payload else timedelta(hours=_EMPTY_CACHE_TTL_HOURS)
    if age > ttl:
        return None
    return payload


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


# 8 Sep 2026 fix: this gateway's unit convention (cents vs dollars) for
# rate/charge fields is NOT confirmed live from this sandbox (no
# outbound network access - module docstring), and the module's own
# worst-case failure mode is exactly a silently-100x-wrong dollar
# figure. Rather than assume a unit, every parsed rate/charge is
# sanity-checked against a real-world-plausible AU band before being
# trusted; a value that fits neither as-parsed nor x100 is dropped
# (the plan is skipped, not shown with a fabricated number). Bands are
# deliberately generous (real published AU rates/charges vary a lot by
# distributor/tariff) - they exist to catch a UNIT mixup, not to
# second-guess a genuinely unusual but real plan.
_GENERAL_RATE_PLAUSIBLE_C_KWH = (5.0, 150.0)
_SUPPLY_CHARGE_PLAUSIBLE_C_DAY = (20.0, 400.0)


def _normalize_cents(raw, plausible_band):
    if raw is None:
        return None
    lo, hi = plausible_band
    if lo <= raw <= hi:
        return raw
    if lo <= raw * 100 <= hi:
        return raw * 100
    return None


def _parse_electricity_contract(plan_detail, retailer_slug):
    """plan_detail: a Get Generic Plan Detail response's `data` object.
    Returns a candidate-plan dict with rates in CENTS (c_kwh_general/
    c_kwh_controlled/daily_supply_c) or None if the schema doesn't match
    what's expected - a plan this can't confidently parse is skipped,
    never guessed at (module docstring's fail-closed rule). Only the
    FIRST current tariffPeriod is read (conditional/seasonal
    multi-period plans are a documented simplification - shown as a
    single blended rate).

    8 Sep 2026 fix: this gateway's v3 detail schema returns
    `electricityContract` as a SINGULAR object (independently confirmed
    - real third-party usage of this exact API) - the plural, indexed
    `electricityContracts[0]` this parser originally assumed meant every
    successful fetch was silently parsing to None. The plural form is
    kept as a fallback only (never observed live from this sandbox -
    module docstring), not because it's confirmed to still occur.
    `controlledLoad` has been documented at both the CONTRACT level and
    nested inside tariffPeriod across CDS schema revisions - both are
    checked rather than assuming one."""
    try:
        contract = plan_detail.get("electricityContract")
        if not isinstance(contract, dict):
            contracts = plan_detail.get("electricityContracts") or []
            contract = contracts[0] if contracts else {}
        periods = contract.get("tariffPeriod") or []
        if not periods:
            return None
        period = periods[0]
        daily_supply_raw = None
        try:
            raw = period.get("dailySupplyCharge")
            if raw is None:
                raw = period.get("dailySupplyCharges")  # seen pluralised in some CDS samples
            daily_supply_raw = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            pass
        daily_supply_c = _normalize_cents(daily_supply_raw, _SUPPLY_CHARGE_PLAUSIBLE_C_DAY)
        c_general_raw = None
        rate_type = period.get("rateBlockUType")
        if rate_type == "singleRate":
            rates = (period.get("singleRate") or {}).get("rates") or []
            if rates:
                try:
                    c_general_raw = float(rates[0].get("unitPrice")) if rates[0].get("unitPrice") else None
                except (TypeError, ValueError):
                    pass
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
                c_general_raw = sum(unit_prices) / len(unit_prices)  # simple blended average - flagged as such below
        c_general = _normalize_cents(c_general_raw, _GENERAL_RATE_PLAUSIBLE_C_KWH)
        if c_general is None:
            return None
        c_controlled_raw = None
        cl = contract.get("controlledLoad") or period.get("controlledLoad") or []
        if cl:
            cl_rates = (cl[0].get("rates") or [])
            if cl_rates:
                try:
                    c_controlled_raw = float(cl_rates[0].get("unitPrice"))
                except (TypeError, ValueError):
                    pass
        c_controlled = _normalize_cents(c_controlled_raw, _GENERAL_RATE_PLAUSIBLE_C_KWH)
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


def _fetch_json(url, params=None, x_v=_DEFAULT_XV):
    """GETs url at the given x-v version. On a 406 (CDS content-
    negotiation rejection), reads the gateway's OWN error body for a
    min=/max= version hint and retries ONCE with that exact version -
    see _CDR_HEADERS above for why this asks the gateway rather than
    hardcoding a second guessed number. Returns (json_or_None,
    http_status_or_None) - the status is surfaced purely for the
    per-retailer logging in _fetch_retailer_plans, so a failure is
    diagnosable from Railway's logs instead of just "returned fewer
    plans" (the exact gap that made the original x-v bug invisible)."""
    headers = dict(_CDR_HEADERS)
    headers["x-v"] = x_v
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=_REQUEST_TIMEOUT_S)
        if resp.status_code == 406:
            hinted = _XV_HINT_RE.search(resp.text or "")
            if hinted and hinted.group(1) != x_v:
                headers["x-v"] = hinted.group(1)
                resp = requests.get(url, headers=headers, params=params, timeout=_REQUEST_TIMEOUT_S)
        if resp.status_code != 200:
            return None, resp.status_code
        return resp.json(), resp.status_code
    except (requests.RequestException, ValueError):
        return None, None


def _plan_stub_matches_postcode(stub, postcode):
    """8 Sep 2026 addition: the LISTING response's own `geography` block
    (per CDS Energy's documented shape - includedPostcodes/
    excludedPostcodes as string arrays) can filter to the visitor's own
    postcode BEFORE the (expensive, per-plan) detail fetches, instead of
    fetching every plan's detail regardless of whether it's even sold in
    this postcode. Deliberately permissive on missing data: a stub with
    no geography block, or one that doesn't carry these specific keys,
    is treated as available everywhere (the prior default) - this can
    only ever narrow the candidate set, never wrongly exclude a plan
    whose availability data isn't there to check."""
    if not postcode or not isinstance(stub, dict):
        return True
    geo = stub.get("geography") or {}
    included = geo.get("includedPostcodes")
    if included:
        return postcode in included
    excluded = geo.get("excludedPostcodes")
    if excluded and postcode in excluded:
        return False
    return True


def _fetch_retailer_plans(slug, fuel_type, postcode=None):
    """One retailer's generic plan list + detail for each, trying both
    known path shapes. Returns [] on ANY failure for this retailer -
    callers loop every retailer and simply get fewer results, never an
    exception. Every attempt is logged (slug, path tried, HTTP status,
    plans returned) - 8 Sep 2026 fix: this whole class of failure was
    previously invisible, since every error here was silently
    swallowed with nothing recorded anywhere."""
    base = _AER_BASE_TEMPLATE.format(slug=slug)
    listing = None
    plans_path = None
    listing_status = None
    for path in _PLAN_PATH_VARIANTS:
        listing, listing_status = _fetch_json(
            f"{base}{path}",
            params={"fuelType": fuel_type.upper(), "type": "STANDING", "page-size": 50},
        )
        if listing:
            plans_path = path
            break
    if not listing:
        _logger.warning("[bill_check_aer] %s listing failed - last path=%s status=%s",
                        slug, path, listing_status)
        return []
    try:
        plan_stubs = listing.get("data", {}).get("plans") or listing.get("data") or []
        if isinstance(plan_stubs, dict):
            plan_stubs = plan_stubs.get("plans", [])
    except AttributeError:
        _logger.warning("[bill_check_aer] %s listing status=%s but unparseable shape", slug, listing_status)
        return []
    plan_stubs = [s for s in (plan_stubs or []) if _plan_stub_matches_postcode(s, postcode)]
    out = []
    for stub in plan_stubs[:20]:  # bounded - protects latency/spend, not a full-market sweep
        plan_id = stub.get("planId") if isinstance(stub, dict) else None
        if not plan_id:
            continue
        detail, detail_status = _fetch_json(f"{base}{plans_path}/{plan_id}")
        if not detail:
            continue
        parsed = _parse_electricity_contract(detail.get("data", detail), slug)
        if parsed:
            out.append(parsed)
    _logger.warning("[bill_check_aer] %s: path=%s listing_status=%s candidates=%d parsed=%d",
                    slug, plans_path, listing_status, len(plan_stubs), len(out))
    return out


def fetch_candidate_plans(postcode, fuel_type="electricity", distributor=None):
    """The AU AER comparison's one entry point. postcode/distributor are
    accepted for the cache key AND now used as a real geography filter:
    each retailer's plan listing carries a `geography.includedPostcodes`/
    `excludedPostcodes` block per plan stub, and `_fetch_retailer_plans`
    checks the user's postcode against it before spending a detail fetch
    on a plan that was never available at that address (see
    `_plan_stub_matches_postcode`). A plan stub with no geography block
    at all is treated as generally available, matching the spec's own
    wording. Returns [] if every retailer fails (AER outage, schema
    drift, or - most likely from this sandbox's own testing limits - a
    wrong path/base-URL guess that needs the owner's post-deploy
    spot-check per the module docstring); failed fetches are cached only
    briefly (see `_EMPTY_CACHE_TTL_HOURS`) so a transient outage doesn't
    block real comparisons for a full day."""
    if fuel_type not in ("electricity", "gas"):
        return []
    cache_key = f"{fuel_type}:{postcode}:{distributor or ''}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    all_plans = []
    for slug in RETAILER_SLUGS:
        try:
            plans = _fetch_retailer_plans(slug, fuel_type, postcode=postcode)
            all_plans.extend(plans)
        except Exception:
            _logger.warning("[bill_check_aer] %s raised during fetch - skipped", slug, exc_info=True)
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

# Owner report (8 Sep 2026 - "after uploading the bills it's not reading
# the documents, it keeps all the bill data empty ... same issue we had
# before"): Fix round 10 #2 (see extraction_below_minimum()'s own
# docstring below) only ever checked these two fields for None - but a
# bill the model genuinely couldn't read doesn't always come back with
# an honest None for them. Sometimes it comes back with a technically-
# present-but-meaningless total_amount: 0 or billing_period_days: 0
# instead (the extraction prompt's "omit any field you cannot read
# confidently" instruction isn't always followed for a field the prompt
# also describes as required-sounding). None-only checks waved that
# straight through as "extraction worked" - a real bill is never $0 due
# over a 0-day billing period, so both must be POSITIVE to count as a
# real read, not just non-None.
_POSITIVE_REQUIRED_FIELDS = ("billing_period_days", "total_amount")


def _extraction_has_degenerate_required_field(extracted):
    for f in _POSITIVE_REQUIRED_FIELDS:
        v = extracted.get(f)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
            return True
    return False


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
    """True if a required field is missing (or present but degenerate -
    see _extraction_has_degenerate_required_field()) or the model's own
    reported confidence is low - the spec's own retry trigger ("one
    retry with MODEL_SONNET only if required fields are missing/low-
    confidence")."""
    if not extracted:
        return True
    if any(extracted.get(f) is None for f in REQUIRED_EXTRACTION_FIELDS):
        return True
    if _extraction_has_degenerate_required_field(extracted):
        return True
    conf = extracted.get("confidence")
    return conf is not None and conf < 0.6


def extraction_below_minimum(extracted):
    """Fix round 10 #2: the caller used to accept whatever needs_retry()
    triggered A retry for (missing required field OR low confidence) as
    a SUCCESS the moment it got any non-empty dict back, even from the
    retry - the exact bug: extraction could come back missing fuel/
    billing_period_days/total_amount, get retried once, come back STILL
    missing them, and still get treated as "extraction worked", pre-
    filling the review form with blanks/zeros and no warning shown.
    This is the caller's post-retry gate: True only when a REQUIRED
    field is still missing (or degenerate - a $0 total or 0-day period,
    the 8 Sep 2026 follow-up fix) after the retry has already run - the
    caller should say so plainly and fall back to manual entry, not
    show a silently-incomplete/silently-worthless review form.
    Deliberately narrower than needs_retry(): low confidence alone
    (every required field present, positive, just not confidently read)
    is still worth showing for the visitor to eyeball and correct, not
    treated as an outright failure."""
    if not extracted:
        return True
    if any(extracted.get(f) is None for f in REQUIRED_EXTRACTION_FIELDS):
        return True
    return _extraction_has_degenerate_required_field(extracted)


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
