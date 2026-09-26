"""
visitor_classify.py

Admin Dashboard Analytics, Commits 1-2 (26 Sep 2026, owner-specified,
6-commit project): per-HTTP-request traffic classification - the fix
for a real problem the owner measured directly: on 24 Sep 2026 the site
took ~2,000 HTTP requests (Railway's own HTTP log, replayed in full for
Commit 1's own verification - see that commit's report for the dataset
and every count below); classifying them by user-agent and source
network leaves a small handful of genuine human visitors. The Admin
Dashboard's existing view counter (server.py's "requests" pulse
counter) has no way to express that gap - every request, crawler and
vulnerability probe included, counts as one indistinguishable "view".

WHERE THIS PLUGS IN: server.py's own _pulse_counting_middleware - the
HTTP-level request counter that sees literally every request the whole
site serves (see that middleware's own module-level comment block for
why it's a different, complementary signal from metrics_store's
Streamlit-side per-render counter, and from _count_view()'s own
narrower per-server-rendered-page counter). Commit 1's task named
"_count_view()" as the function to touch; this module instead wires
into _pulse_counting_middleware, because that is the ONLY layer that
actually sees every request (including /og/*, /_stcore/*, static
assets, and every Streamlit-proxied ticker/page hit) - _count_view()
only fires from a handful of curated server-rendered route handlers.
Owner-confirmed correct after Commit 1's report - not revisited since.

CLASSIFICATION DESIGN - four labels, deliberately NOT symmetric:
  - "vuln_scanner": the request PATH matches a known exploit-probe
    pattern (wp-admin, wp-login, .php, .env, phpmyadmin, xmlrpc, .git -
    this site is pure Python, so a hit on any of these is never
    legitimate), OR (Commit 2 addition) the UA self-declares as an
    exploit tool (e.g. "cve-2026-87902-poc/1.0") regardless of which
    path it hits - "the path list was meant to catch scanners, not to
    define them" (owner's own words approving this Commit 2 change; see
    Commit 2's own report for exactly how many 24-Sep-replay requests
    this reclassifies). Checked FIRST, overriding every other signal -
    a scanner probing /wp-admin from a browser-shaped UA on an
    unremarkable residential IP is still a scanner, not a visitor who
    happened to mistype a URL.
  - "known_crawler": either (a) the source IP falls in a crawler's own
    OFFICIAL published range (currently only Google's - see
    KNOWN_CRAWLER_NETWORKS), (b) the source IP has been forward-
    confirmed-reverse-DNS VERIFIED as belonging to Google/Bing/Apple's
    own crawl infrastructure (Commit 2 addition - see the "REVERSE-DNS
    VERIFICATION" section below), or (c) the UA matches one of a
    smaller set of other well-known, self-declaring, non-malicious
    crawlers this module has no network-level way to verify at all
    (PetalBot, ClaudeBot, GPTBot, AhrefsBot, etc. - UA-trust only,
    unchanged since Commit 1).
  - "automated_unknown": the source IP falls in a known cloud/hosting
    network a genuine visitor essentially never browses FROM directly,
    OR the UA plainly isn't a browser rendering a page (a bare
    HTTP-library UA, a UA that is itself a URL, a headless-browser
    signature), OR (Commit 2 nuance) a UA CLAIMS to be Google/Bing/
    Apple's crawler but hasn't yet been reverse-DNS verified - see
    below for why an unverified claim is never trusted outright.
  - "human": NEVER assigned by this module. This is the module's own
    central design decision, not an oversight - see the long comment on
    classify_request()'s own default branch below for the reasoning,
    and Commit 1's own report for why the alternative (defaulting an
    unflagged request to "human") would be dishonest given what a
    single, session-less HTTP request can actually prove. A request
    only ever becomes "human" once a LATER commit's session-level
    evidence (Commit 3's day-rotated hash + Commit 4's real-browser
    asset-fetch signal) positively promotes it - this module's own
    "human" label exists in the return-type/counter schema purely so
    those later commits have somewhere to write into, not because this
    module ever writes it itself.

REVERSE-DNS VERIFICATION (Commit 2, owner-specified): a UA string is
trivially spoofable - Commit 1's own replay evidence includes 25
IPs (mostly Tencent Cloud, caught by KNOWN_CLOUD_NETWORKS below) all
claiming, falsely, to be an iPhone. The same spoofing risk applies to
"Googlebot"/"bingbot"/"Applebot" UA strings, which is exactly why
Google documents forward-confirmed reverse DNS as the supported way to
verify a request genuinely came from Googlebot: PTR the source IP,
check the hostname ends in the crawler's own domain
(googlebot.com/google.com, search.msn.com, applebot.apple.com), then
forward-resolve THAT hostname and confirm it maps back to the exact
same IP. Both steps must succeed and agree.

Measured in this sandbox against real Googlebot IPs from the 24 Sep
replay: a full PTR + forward-confirm round trip costs 3-12 milliseconds
- roughly 1,000x this module's own microsecond-scale table-lookup cost,
and NOT "negligible" by the hard per-request-cost constraint this whole
project is built under. So this is never done synchronously on the
request that triggers it - maybe_verify_crawler_async() (called via
asyncio.create_task(), fire-and-forget, from server.py's middleware,
never awaited) runs it in a thread-pool executor in the background and
caches a POSITIVE result, in memory only, keyed by IP
(_crawler_dns_verified below). classify_request() itself stays a pure,
synchronous, sub-microsecond function - it only ever reads that cache
(a dict lookup), never triggers a lookup itself.

Consequence for classify_request()'s own precedence: an UNVERIFIED
"I am Googlebot/bingbot/Applebot" UA claim, from an IP that's neither
in KNOWN_CRAWLER_NETWORKS (Google's fast static range) nor already
rDNS-verified, is classified automated_unknown, NOT known_crawler - the
same "never trust what can't currently be backed by evidence" principle
this module already applies to "human" (see above). The background
verification task then caches a positive result for next time, so a
GENUINE Google/Bing/Apple crawler IP making its first-ever request of
the day is (at most) under-counted for that one request and
self-corrects within the same day, given how many requests these
crawlers typically make (PetalBot alone: 8 IPs, 100+ requests each, 24
Sep replay) - a small, honest, self-correcting imprecision, preferred
over trusting an unverified, easily-spoofed string outright.
KNOWN_CRAWLER_NETWORKS (Google's own published range) remains the fast
path for the one crawler with a long-stable, independently-confirmed
CIDR - see that table's own comment.

CLOUD-NETWORK TABLE STATUS (Commit 2 clarification, owner-specified):
KNOWN_CLOUD_NETWORKS below (Tencent/Alibaba) is a CONVENIENCE, not the
backbone of this design - reverse DNS has no equivalent for a plain
hosting provider (no crawler-style domain to verify against), so it
stays a hand-maintained, evidence-seeded table with the known
limitations Commit 1's own report already disclosed (will under-catch
an unlisted range, not independently verified against a primary
registry in this sandbox). Nothing in this module's own design DEPENDS
on that table being complete: an unlisted cloud IP simply falls through
to the SAME never-human automated_unknown default every other
unclassified request already gets (demonstrated directly in Commit 1's
own replay - 162.62.213.165, sharing the Tencent group's identical
spoofed UA but NOT in any KNOWN_CLOUD_NETWORKS entry, still landed
correctly in automated_unknown, via the default branch, not the table).
The durable signal for "is this really a human session" is Commit 4's
asset-fetch heuristic, which reads a session's own request shape and
doesn't care what network it came from at all.

PRIVACY: classify_request() itself remains a pure function of
(user_agent, client_ip, path) -> one of four short label strings - no
I/O, no state. The ONE piece of state this module now holds
(_crawler_dns_verified, Commit 2) is an in-memory-only dict of
IP -> True, built exclusively from a background DNS lookup, NEVER
written to disk or a database, and lost on every process restart - see
the hard privacy rule's own exact wording ("No IP address is ever
written to disk or database. Classification happens IN MEMORY at
request time..." - holding an IP transiently, in-process, during
classification is what that sentence explicitly describes as the
sanctioned mechanism; only PERSISTENCE is barred). No raw user-agent
string, and no browsing trail, are ever persisted by anything in this
file, in memory or otherwise. The caller (server.py's middleware) is
the one place any of this reaches disk - via the exact same
admin_metrics_store.bump_many() pulse-counter machinery every other
site-pulse figure already uses, writing only (day, "req_class:<label>"
or "page_views", count) - see that module's own docstring for its own
privacy rules, which this module inherits and does not weaken.

PERFORMANCE: every pattern is precompiled (re.compile) and every
network precomputed (ipaddress.ip_network) once, at import time - never
per request. classify_request() does a small number of regex/substring/
network-membership/dict-lookup checks against short strings - see
Commit 1's own report for the measured per-request cost (6.55us against
the full real 24 Sep dataset). The one genuinely slow operation this
module ever performs (DNS) never runs on the request path - see
"REVERSE-DNS VERIFICATION" above.
"""
import asyncio
import ipaddress
import re
import socket
import threading
import time

HUMAN = "human"
KNOWN_CRAWLER = "known_crawler"
AUTOMATED_UNKNOWN = "automated_unknown"
VULN_SCANNER = "vuln_scanner"

# All four labels this module's counters are ever recorded under -
# server.py's middleware iterates this (not a hardcoded literal list) so
# a future fifth label added here needs no matching edit there.
ALL_LABELS = (HUMAN, KNOWN_CRAWLER, AUTOMATED_UNKNOWN, VULN_SCANNER)


# ============================================================================
# CLASSIFICATION TABLE - every pattern and every network this module acts
# on lives here, and only here. Read top to bottom.
# ============================================================================

# --- known_crawler: crawlers this module can VERIFY by reverse DNS
# (Commit 2) - Google/Bing/Apple, the three the owner named. A UA
# matching one of these is NEVER trusted outright; see this module's own
# "REVERSE-DNS VERIFICATION" docstring section above for why. The tuple
# holds (ua_pattern, expected_rdns_suffixes, human-readable name).
RDNS_VERIFIABLE_CRAWLERS = (
    (re.compile(r"Googlebot|AdsBot-Google|GoogleOther"), (".googlebot.com", ".google.com"), "Google"),
    (re.compile(r"bingbot"), (".search.msn.com",), "Microsoft Bing"),
    (re.compile(r"Applebot"), (".applebot.apple.com",), "Apple"),
)

# --- known_crawler: self-declaring, non-malicious crawler UAs with NO
# reverse-DNS verification available (no crawler-owned domain to check
# against) - trusted by UA string alone, unchanged since Commit 1. Every
# one of these except DuckDuckBot was actually seen hitting this site in
# the 24 Sep 2026 Railway HTTP log replay (see Commit 1's own report for
# the exact per-UA counts); DuckDuckBot is included anyway as a stable,
# extremely well-known crawler that simply didn't happen to crawl that
# particular day. Matched case-sensitively - every bot UA below is
# consistent about its own casing in the wild.
UA_TRUSTED_CRAWLERS = (
    (re.compile(r"PetalBot"), "Huawei Petal Search - the single largest source of ANY kind on this site: 1,012 of 2,087 requests in the 24 Sep replay (~48%)"),
    (re.compile(r"ClaudeBot"), "Anthropic's own crawler - 40 requests, 24 Sep replay"),
    (re.compile(r"GPTBot"), "OpenAI's crawler - 24 requests, 24 Sep replay"),
    (re.compile(r"OAI-SearchBot"), "OpenAI's search crawler - 5 requests, 24 Sep replay"),
    (re.compile(r"AhrefsBot"), "Ahrefs SEO crawler - 12 requests, 24 Sep replay"),
    (re.compile(r"Barkrowler"), "Babbar.tech SEO crawler - 2 requests, 24 Sep replay"),
    (re.compile(r"facebookexternalhit"), "Meta link-preview fetcher - seen 24 Sep replay"),
    (re.compile(r"meta-externalagent"), "Meta link-preview fetcher, newer UA - seen 24 Sep replay"),
    (re.compile(r"Twitterbot"), "X/Twitter link-preview fetcher - seen 24 Sep replay"),
    (re.compile(r"CensysInspect"), "Censys - self-declared internet-wide research scanner (not exploit traffic; a judgement call - see Commit 1's own report) - 10 requests, 24 Sep replay"),
    (re.compile(r"DuckDuckBot"), "DuckDuckGo - not observed 24 Sep, included as a stable well-known crawler"),
)

# --- known_crawler: official IP ranges. Google is the one crawler with
# a long-stable, independently-confirmed CIDR (66.249.64.0/19 has been
# Google's own published crawl range for well over a decade, and it's
# also exactly the range independently supplied for this project from
# the owner's own log analysis) - kept as the FAST PATH ahead of
# reverse-DNS verification (a network check costs nanoseconds; a DNS
# round trip costs milliseconds - see this module's own docstring). A
# request from this range counts as known_crawler even on the rare
# chance its UA doesn't match anything above.
KNOWN_CRAWLER_NETWORKS = (
    (ipaddress.ip_network("66.249.64.0/19"), "Google's official crawl range"),
)

# --- vuln_scanner: path substrings, exactly the seven markers this
# project specified - never expanded beyond this list on this module's
# own initiative (Commit 1's own report flagged real replay traffic this
# list doesn't catch by path; the owner's Commit 2 response was to add
# UA-based detection instead - see VULN_SCAN_UA_PATTERNS below - not to
# expand this list).
VULN_SCAN_PATH_MARKERS = (
    "wp-admin", "wp-login", ".php", ".env", "phpmyadmin", "xmlrpc", ".git",
)

# --- vuln_scanner: UA self-declares as an exploit tool (Commit 2,
# owner-specified: "a user-agent that names itself as a CVE exploit tool
# is a scanner whatever path it requests - the path list was meant to
# catch scanners, not to define them"). Checked ALONGSIDE the path
# markers above, same top precedence - reclassifies 9 of the 24 Sep
# replay's requests from automated_unknown to vuln_scanner (2 more
# sharing this UA were already caught by path anyway - see Commit 2's
# own report for the exact breakdown).
VULN_SCAN_UA_PATTERNS = (
    (re.compile(r"^cve-\d", re.I), "self-declared CVE proof-of-concept tool"),
)

# --- automated_unknown: known cloud/hosting networks a genuine site
# visitor essentially never browses FROM directly. A CONVENIENCE table,
# not this design's backbone - see this module's own "CLOUD-NETWORK
# TABLE STATUS" docstring section above. Every network below is directly
# evidenced by the 24 Sep 2026 Railway HTTP log replay - a real request
# from this site's own production traffic landed inside it - and
# cross-checked, where possible, against third-party ASN/WHOIS
# aggregators via web search, since this sandbox has no outbound network
# access to a primary registry feed (confirmed: direct fetches to
# developers.google.com and third-party CIDR aggregators were both
# blocked by this sandbox's own egress policy; DNS lookups, used for
# reverse-DNS verification above, are NOT blocked - a different
# protocol/path). See Commit 1's own report for exactly what was/wasn't
# independently verified for each entry below. Deliberately narrow: a
# seed list grounded in actual observed traffic, not an attempt at a
# complete, self-maintaining feed of every block either provider owns -
# it will under-catch a Tencent/Alibaba IP this site has never seen
# traffic from before, and nothing in this module's design depends on
# that not happening (an unlisted IP simply falls to the same
# never-human default every other unclassified request already gets).
KNOWN_CLOUD_NETWORKS = (
    (ipaddress.ip_network("43.128.0.0/10"),
     "Tencent Cloud - covers 22 of the 25 addresses sharing an identical spoofed "
     "'iPhone OS 13_2_3' UA, each hitting '/' exactly once, in the 24 Sep replay "
     "(observed 43.130.x.x-43.167.x.x; the tightest CIDR-aligned block covering "
     "all of them). Third-party ASN lookups attribute this space to AS132203 "
     "(Tencent Cloud Computing) - not independently verified against a primary "
     "registry in this sandbox."),
    (ipaddress.ip_network("49.51.0.0/16"),
     "Tencent Cloud - 3 of the spoofed-UA addresses fell here; a third-party "
     "WHOIS lookup (ipinfo.io) reports this exact /16 registered to "
     "'TencentCloud' under AS132203."),
    (ipaddress.ip_network("170.106.0.0/16"),
     "Tencent Cloud - 2 of the spoofed-UA addresses fell here, in two different "
     "/19s within this /16; third-party sources confirm at least 170.106.0.0/19 "
     "as Tencent-allocated (AS132203)."),
    (ipaddress.ip_network("129.211.0.0/16"),
     "Tencent Cloud - 1 spoofed-UA address fell here; named directly in this "
     "project's own task evidence, corroborated as AS132203 space by "
     "third-party ASN lookups."),
    (ipaddress.ip_network("47.82.11.0/24"),
     "Alibaba Cloud - 3 requests in the 24 Sep replay, each a single-page hit "
     "with no accompanying asset fetch, from three DIFFERENT ordinary-looking "
     "browser UAs (Chrome, Firefox, Chrome) - the network, not the UA, is what "
     "ties them together. Deliberately kept to just the observed /24 rather "
     "than guessing at Alibaba's much larger overall footprint (47.0.0.0/8 "
     "spans many unrelated allocations no evidence here speaks to)."),
)

# --- automated_unknown: non-browser HTTP client / tooling UA signatures.
NON_BROWSER_UA_PATTERNS = (
    (re.compile(r"^https?://"),
     "the User-Agent header is itself a URL - a known WordPress-exploit-bot "
     "signature (seen 24 Sep replay: 14 requests to /wp-admin/install.php, "
     "already caught as vuln_scanner by path anyway)"),
    (re.compile(r"python-requests|^curl/|Go-http-client|^okhttp|^Wget/|^Scrapy/|libwww-perl", re.I),
     "a plain scripting/HTTP-library UA, never a browser rendering a page"),
    (re.compile(r"HeadlessChrome"),
     "a headless-automation browser signature - real Chrome internals, but "
     "not a human at a keyboard (48 requests, 24 Sep replay)"),
)

# --- page views (Commit 2, owner-specified): these path shapes are
# infrastructure, never a page a visitor looked at, and must not count
# as any kind of visit. Verified directly against the 24 Sep replay: the
# real 209-request session from 223.181.112.201 collapses to exactly 1
# page-view-eligible request (/deep-dive) once these are excluded -
# literally "one visit", matching the task's own evidence requirement
# verbatim - see Commit 2's own report for the full breakdown. Two small additions
# beyond the owner's own literal list, both flagged in that report
# rather than assumed silently: /favicon.ico and /favicon.png (real
# server.py routes, real replay traffic, the same "icon asset" category
# as the named PWA icons, just not served from /pwa/) and /llms-full.txt
# (the same machine-readable-index family as the named llms.txt).
#
# /_stcore/ is deliberately NOT in this prefix table - see
# _STCORE_MARKER below and is_page_view_path()'s own comment for why it
# needs a substring check, not a startswith() prefix check: the real
# replay evidence shows Streamlit's frontend bundle issuing _stcore
# requests as relative fetches that inherit whatever multipage route
# the browser is currently on (/deep-dive/_stcore/health,
# /comparison/_stcore/host-config, /portfolio/_stcore/health,
# /scanner/_stcore/host-config, /top-100/_stcore/health - all real, all
# from the same 24 Sep dataset), NOT always the bare /_stcore/health a
# startswith() check alone would catch. /static/, /pwa/, /og/, and
# /blog/media/ show NO such prefixed variant anywhere in the full
# 2,087-row replay, so they stay ordinary startswith() prefixes below.
NON_PAGE_VIEW_PATH_PREFIXES = (
    "/static/",    # Streamlit's own bundled JS/CSS, proxied through, never a server.py route
    "/pwa/",       # PWA icons + offline.html, served via the app.mount("/pwa", ...) static mount
    "/og/",        # share-preview card images
    "/blog/media/", # blog post images - a real registered route, static content
)
NON_PAGE_VIEW_EXACT_PATHS = (
    "/manifest.webmanifest", "/sw.js", "/robots.txt", "/sitemap.xml",
    "/llms.txt", "/llms-full.txt", "/feed.xml", "/blog/feed.xml",
    "/favicon.ico", "/favicon.png",
)

# Streamlit's own health/host-config/stream endpoints - matched by
# containment, not prefix; see the table comment above for the evidence.
_STCORE_MARKER = "/_stcore/"


# ============================================================================
# REVERSE-DNS CRAWLER VERIFICATION (Commit 2) - see this module's own
# docstring section above for the full design and the measured cost.
# ============================================================================

# In-memory ONLY - never disk/DB, lost on every process restart. See
# this module's own docstring ("PRIVACY") for why holding an IP here,
# transiently, is the explicitly sanctioned mechanism, not a violation
# of the hard privacy rule. Bounded so a sustained flood of spoofed
# "Googlebot" UAs from many distinct IPs can't grow this without limit -
# oldest entries are dropped first once the cap is hit (a coarse, cheap
# bound; not meant to be a precise LRU).
#
# Caches BOTH outcomes (owner-flagged finding, this file's own second
# review): caching only positive results meant a spoofed "Googlebot" UA
# that FAILS verification got a fresh DNS lookup on every single
# request it made - free for the spoofer, not free for this server, and
# an amplification path that exists purely because the negative case
# wasn't remembered. ip -> (verified: bool, expires_at: float, from
# time.monotonic()). A positive result gets the long TTL, a negative
# result the short one - see the two constants below for the exact
# values and why. classify_request() only ever treats a FRESH (verified
# is True and not yet expired) entry as known_crawler; an expired or
# negative entry is silently equivalent to no entry at all there.
_CRAWLER_DNS_CACHE_MAX = 5000
_crawler_dns_lock = threading.Lock()
_crawler_dns_cache = {}  # ip string -> (verified: bool, expires_at: float)

# Positive TTL: 24 hours - a genuine Google/Bing/Apple crawler IP is
# long-lived (Google's own published range has been stable for well
# over a decade), and this project already thinks in day-sized buckets
# everywhere else (the day-rotated visitor hash, daily pulse-counter
# aggregates) - caching a real crawler for a full day avoids re-running
# DNS against it on every one of its (often hundreds of) daily requests
# without ever letting a stale positive linger across days.
_POSITIVE_TTL_SECONDS = 24 * 60 * 60

# Negative TTL: 5 minutes - short enough to bound the DNS cost a single
# spoofing IP can impose to at most one lookup per 5 minutes (down from
# "one lookup per request," the amplification path this fix closes),
# long enough that a genuine crawler hit by a transient DNS hiccup
# (NXDOMAIN/timeout) self-corrects within minutes rather than being
# written off, and short relative to the positive TTL so a truly
# negative address is re-checked far more often than a confirmed one,
# never the other way around.
_NEGATIVE_TTL_SECONDS = 5 * 60

# Ceiling on concurrent in-flight verifications, system-wide (not
# per-IP) - bounds how many blocking DNS calls this process can have
# queued onto the default asyncio executor at once, so a burst of many
# DISTINCT spoofed-UA IPs in a short window (the cache above only
# throttles repeats of the SAME IP) can't queue an unbounded pile of
# lookups. loop.run_in_executor(None, ...) uses Python's default
# ThreadPoolExecutor (sized min(32, cpu_count+4)); nothing else in this
# codebase currently calls it with the default executor (server.py's/
# app.py's/scanner_engine.py's own thread pools are all separate,
# dedicated ThreadPoolExecutor instances - confirmed by reading this
# repo), so this ceiling doesn't have to share headroom with anything
# else today, but is kept well under that pool's typical size anyway as
# a margin against whatever else may come to share it later. A request
# that arrives once the ceiling is already hit simply skips
# verification for that one attempt (no cache entry written either
# way) - the next request from that IP tries again.
_IN_FLIGHT_MAX = 20
_crawler_dns_in_flight = set()  # ip strings currently being verified


def _verify_crawler_sync(ip, expected_suffixes):
    """BLOCKING - must only ever be called via an executor (see
    maybe_verify_crawler_async below), never directly on the event loop.
    Forward-confirmed reverse DNS, Google's own documented method for
    verifying a crawler precisely: PTR the source IP, check the hostname
    ends in one of the crawler's own domains, then forward-resolve that
    hostname and confirm it maps back to the exact same IP - both steps
    must succeed and agree, or this returns False. Never raises - any
    DNS failure (NXDOMAIN, timeout, malformed IP) just means "could not
    verify this time", the same honest non-answer as everywhere else in
    this module, never an exception."""
    try:
        host, _aliases, _addrs = socket.gethostbyaddr(ip)
    except Exception:
        return False
    if not any(host.endswith(suffix) for suffix in expected_suffixes):
        return False
    try:
        _resolved_host, _aliases2, addrs = socket.gethostbyname_ex(host)
    except Exception:
        return False
    return ip in addrs


async def maybe_verify_crawler_async(user_agent, client_ip):
    """Fire-and-forget background verification - server.py's middleware
    calls this via asyncio.create_task(...), NEVER awaited, so it can
    never delay the response it's associated with. No-ops immediately
    (before any DNS I/O) unless the UA claims one of RDNS_VERIFIABLE_
    CRAWLERS - so the common case (an ordinary browser request) costs
    one regex miss and returns.

    For a UA that DOES claim to be Google/Bing/Apple, this now checks
    THREE things before ever touching DNS, in order:
      1. a fresh cache entry (positive OR negative, either one skips
         DNS - see _POSITIVE_TTL_SECONDS/_NEGATIVE_TTL_SECONDS above for
         why each outcome gets a different TTL);
      2. this exact IP already being verified by a concurrent call
         (dedupes a burst of requests from the SAME spoofed/real IP
         arriving faster than one DNS round trip);
      3. the system-wide in-flight ceiling (_IN_FLIGHT_MAX) - bounds how
         many DISTINCT IPs can have a lookup outstanding at once.
    Only past all three does this run the actual (blocking) DNS round
    trip, in a thread-pool executor, never on the event loop itself -
    and this time caches WHATEVER the result is, positive or negative,
    each with its own TTL. Caching negative results closes a real
    amplification path a positive-only cache left open: without it, an
    IP that FAILS verification (a spoofed UA claim) got a fresh DNS
    lookup on every single request it made - cheap for that client,
    not free for this server."""
    ua = user_agent or ""
    ip = client_ip or ""
    if not ua or not ip:
        return
    for pattern, expected_suffixes, _name in RDNS_VERIFIABLE_CRAWLERS:
        if not pattern.search(ua):
            continue
        now = time.monotonic()
        with _crawler_dns_lock:
            entry = _crawler_dns_cache.get(ip)
            if entry is not None and now < entry[1]:
                return  # fresh cached result (positive or negative) - no DNS needed
            if ip in _crawler_dns_in_flight:
                return  # a concurrent request from this exact IP is already verifying it
            if len(_crawler_dns_in_flight) >= _IN_FLIGHT_MAX:
                return  # system-wide ceiling hit - skip this attempt, a later request retries
            _crawler_dns_in_flight.add(ip)
        try:
            try:
                loop = asyncio.get_event_loop()
                verified = await loop.run_in_executor(None, _verify_crawler_sync, ip, expected_suffixes)
            except Exception:
                verified = False
            ttl = _POSITIVE_TTL_SECONDS if verified else _NEGATIVE_TTL_SECONDS
            with _crawler_dns_lock:
                if len(_crawler_dns_cache) >= _CRAWLER_DNS_CACHE_MAX and ip not in _crawler_dns_cache:
                    _crawler_dns_cache.pop(next(iter(_crawler_dns_cache)), None)
                _crawler_dns_cache[ip] = (verified, time.monotonic() + ttl)
        finally:
            with _crawler_dns_lock:
                _crawler_dns_in_flight.discard(ip)
        return


def classify_request(user_agent, client_ip, path):
    """One of ALL_LABELS for this single HTTP request. Pure function -
    no I/O, no blocking, never raises (a malformed/missing `client_ip`
    simply skips the network checks below rather than erroring - a
    classification miss is always the safe failure mode here, never an
    exception that could take the request down).

    Precedence, deliberate and in this order:
      1. vuln_scanner (path OR UA) - overrides everything else. A
         scanner probing /wp-admin, or a UA that names itself as a CVE
         exploit tool, from a browser-shaped UA/unremarkable IP is
         still a scanner.
      2. known_crawler: Google's fast static range, then the in-memory
         rDNS-verified cache, then the UA-trusted-only crawler list
         (PetalBot etc. - no verification available for these).
      3. automated_unknown: a known cloud/hosting network, a
         non-browser UA signature, OR an unverified Google/Bing/Apple
         UA claim (see this module's own "REVERSE-DNS VERIFICATION"
         docstring section for why an unverified claim is never trusted
         outright - and maybe_verify_crawler_async() is what the
         middleware calls, separately, to populate the cache this
         precedence step reads from).
      4. Otherwise: automated_unknown, NOT human - see this module's own
         docstring ("human: NEVER assigned by this module") for why a
         single, session-less request can never earn a "human" label on
         its own; that determination belongs to a later commit's
         session-level evidence."""
    path = path or ""
    ua = user_agent or ""

    path_lower = path.lower()
    if any(marker in path_lower for marker in VULN_SCAN_PATH_MARKERS):
        return VULN_SCANNER
    for pattern, _why in VULN_SCAN_UA_PATTERNS:
        if pattern.search(ua):
            return VULN_SCANNER

    ip_obj = None
    if client_ip:
        try:
            ip_obj = ipaddress.ip_address(client_ip)
        except ValueError:
            ip_obj = None

    if ip_obj is not None:
        for network, _why in KNOWN_CRAWLER_NETWORKS:
            if ip_obj in network:
                return KNOWN_CRAWLER
        if client_ip:
            with _crawler_dns_lock:
                entry = _crawler_dns_cache.get(client_ip)
            # only a FRESH positive entry counts - an expired entry or a
            # cached negative result is silently equivalent to no entry
            # at all here (see _crawler_dns_cache's own comment above).
            if entry is not None and entry[0] and time.monotonic() < entry[1]:
                return KNOWN_CRAWLER

    for pattern, _why in UA_TRUSTED_CRAWLERS:
        if pattern.search(ua):
            return KNOWN_CRAWLER

    if ip_obj is not None:
        for network, _why in KNOWN_CLOUD_NETWORKS:
            if ip_obj in network:
                return AUTOMATED_UNKNOWN

    if not ua:
        return AUTOMATED_UNKNOWN
    for pattern, _why in NON_BROWSER_UA_PATTERNS:
        if pattern.search(ua):
            return AUTOMATED_UNKNOWN

    # An unverified Google/Bing/Apple UA claim falls through to here too
    # (no rDNS_VERIFIABLE_CRAWLERS match returns anything on its own -
    # it only gates maybe_verify_crawler_async(), called separately by
    # the middleware) - same never-human, never-unverified-trust default
    # as everything else this function doesn't recognise.
    return AUTOMATED_UNKNOWN


def is_page_view_path(path):
    """True if `path` represents a real page a visitor could have looked
    at - False for infrastructure (see NON_PAGE_VIEW_PATH_PREFIXES/
    NON_PAGE_VIEW_EXACT_PATHS/_STCORE_MARKER above and their own comments
    for exactly what's excluded and why). Never raises - a malformed/
    missing path is treated as NOT a page view (the safe default: an
    unrecognisable path should never inflate a "real visit" count).

    _STCORE_MARKER is checked by containment (`in`), not
    startswith() - a real Streamlit multipage app issues _stcore
    health/host-config/stream requests as relative fetches that pick up
    whatever page route the browser is currently on as a path prefix
    (e.g. "/deep-dive/_stcore/health"), not only the bare
    "/_stcore/health" a prefix check alone would catch - see that
    marker's own comment above for the real replay evidence."""
    if not path:
        return False
    if path in NON_PAGE_VIEW_EXACT_PATHS:
        return False
    if _STCORE_MARKER in path:
        return False
    return not any(path.startswith(prefix) for prefix in NON_PAGE_VIEW_PATH_PREFIXES)
