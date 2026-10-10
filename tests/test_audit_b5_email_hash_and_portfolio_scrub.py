"""Audit fix B5 (Fable finding S6, 10 Oct 2026, instruction_combined_
10oct.md PART B): "Logs: replace emails with sha256(email)[:8] and
drop portfolio names from error lines (watchdog, digest, weekly
brief, announce, alert, results engines)."

New shared helper: email_auth.log_safe_email(email) - sha256(email)[:8],
reusing this module's own existing _hash() (already used for codes/
sessions). Every log(...) call in the six named engines that used to
interpolate a raw email now goes through this helper instead; every
log(...) call that reports a FAILURE (an except block, or an explicit
"call/gate/send failed" branch) and used to also interpolate the raw
portfolio name alongside it has that name dropped (ticker, which is
public market data rather than personal information, is kept).

Two non-error lines in portfolio_watchdog_engine.py (hitting the per-
night brief cap, the AI gate blocking a user) still show the
portfolio name - deliberately: this task's own instruction scopes the
portfolio-name drop to "error lines" specifically, and these two are
ordinary operational/business-rule outcomes, not failures. Covered
explicitly below so that scope is proven, not assumed.

Verifies by direct source inspection - the same pattern already used
in this session for B2/B3's own structural fixes (see test_dcf_
outlier_guards.py's "confirmed by calling it the same way analyze_
ticker_lite() does" precedent) - since driving each full engine would
need heavy per-module mocking (DB, Mailgun, push, AI client) with no
behavioural change of its own to prove beyond "no raw email/portfolio
reaches log()", which source inspection answers directly and cheaply.

Run: python3 tests/test_audit_b5_email_hash_and_portfolio_scrub.py
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import email_auth
import portfolio_watchdog_engine
import digest_engine
import weekly_brief_engine
import announce_engine
import alert_engine
import results_engine

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK: {label}")
    else:
        failed += 1
        print(f"  FAIL: {label}")


# ======================================================================
# CHECK: email_auth.log_safe_email() itself.
# ======================================================================
_tag_a = email_auth.log_safe_email("alice@example.com")
_tag_a2 = email_auth.log_safe_email("alice@example.com")
_tag_b = email_auth.log_safe_email("bob@example.com")
check("log_safe_email() returns an 8-char tag", len(_tag_a) == 8)
check("log_safe_email() never contains '@' (not the raw address)", "@" not in _tag_a)
check("log_safe_email() never contains the raw local-part either",
      "alice" not in _tag_a and "example" not in _tag_a)
check("log_safe_email() is deterministic for the same address",
      _tag_a == _tag_a2)
check("log_safe_email() differs for a different address",
      _tag_a != _tag_b)
check("log_safe_email() matches sha256(email)[:8] directly",
      _tag_a == email_auth._hash("alice@example.com")[:8])
check("log_safe_email('') is '' (falsy input, never crashes)",
      email_auth.log_safe_email("") == "")
check("log_safe_email(None) is '' (falsy input, never crashes)",
      email_auth.log_safe_email(None) == "")


# ======================================================================
# CHECK: no raw "{email}"/"{owner}" interpolation survives inside any
# log(...) call in the six named engines.
# ======================================================================
_RAW_EMAIL_IN_LOG = re.compile(r'log\(f?"[^"]*\{email\}')
_RAW_OWNER_IN_LOG = re.compile(r'log\(f?"[^"]*\{owner\}')

_ENGINES = {
    "portfolio_watchdog_engine": portfolio_watchdog_engine,
    "digest_engine": digest_engine,
    "weekly_brief_engine": weekly_brief_engine,
    "announce_engine": announce_engine,
    "alert_engine": alert_engine,
    "results_engine": results_engine,
}
for _name, _mod in _ENGINES.items():
    _src = inspect.getsource(_mod)
    check(f"{_name}: no log(...) call interpolates a raw {{email}}",
          not _RAW_EMAIL_IN_LOG.search(_src))
    check(f"{_name}: no log(...) call interpolates a raw {{owner}}",
          not _RAW_OWNER_IN_LOG.search(_src))
    check(f"{_name}: log_safe_email( is actually used at least once",
          "log_safe_email(" in _src)


# ======================================================================
# CHECK: portfolio_watchdog_engine.py - the only one of the six that
# ever logs a portfolio name at all. Error lines must have dropped it;
# the two deliberately-kept non-error lines must still have it.
# ======================================================================
_wd_src = inspect.getsource(portfolio_watchdog_engine)

check('error line "analysis failed" no longer includes {portfolio}',
      'log(f"[watchdog] {_safe_email}/{ticker}: analysis failed: {e}")' in _wd_src)
check('error line "AI call failed" no longer includes {portfolio}',
      "AI call failed" in _wd_src
      and 'log(f"[watchdog] {_safe_email}/{ticker}: AI call failed: "' in _wd_src)
check('the outer per-user except line no longer includes {portfolio}',
      'log(f"[watchdog] {_safe_email}: {e}")' in _wd_src)
check('"email to {_safe_email}" failure line has no portfolio '
      '(it never did)',
      'log(f"[watchdog] email to {_safe_email}: {e}")' in _wd_src)
check('"push to {_safe_email}" failure line has no portfolio '
      '(it never did)',
      'log(f"[watchdog] push to {_safe_email}: {e}")' in _wd_src)

# Deliberately-kept exceptions: non-error lines, portfolio name
# retained - this task's instruction scopes the drop to error lines.
check('non-error "hit the .../night cap" line still shows {portfolio} '
      '(deliberate - not an error line)',
      'log(f"[watchdog] {_safe_email}/{portfolio}: hit the "' in _wd_src)
check('non-error "AI gate blocked" line still shows {portfolio} '
      '(deliberate - a business-rule outcome, not a failure)',
      'log(f"[watchdog] {_safe_email}/{portfolio}: AI gate blocked "' in _wd_src)

# None of the other five engines log a portfolio name at all (watchlist/
# audience/follower-based, not portfolio-based) - confirm that's still
# true post-fix, i.e. this fix didn't accidentally introduce one.
for _name, _mod in _ENGINES.items():
    if _name == "portfolio_watchdog_engine":
        continue
    _src = inspect.getsource(_mod)
    check(f"{_name}: never logs a portfolio name (not portfolio-scoped)",
          "{portfolio}" not in _src)

print()
print(f"PASS={passed} FAIL={failed}")
sys.exit(1 if failed else 0)
