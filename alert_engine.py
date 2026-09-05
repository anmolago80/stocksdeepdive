"""
alert_engine.py

Services batch, Part 1: metric alerts - the evaluation + notification half
(alert_store.py is pure storage). No Anthropic API call anywhere in this
module - every alert is a factual threshold/crossing/state check against
numbers the site's own engines already computed.

Called from two places:
  - scheduler_engine.py's _run_nightly(), once per universe (right after
    that universe's scan/snapshot build) and once more at the very end of
    the whole nightly job for the batched send - see that function's own
    comments for the exact call sequence.
  - app.py's Deep Dive "Alert me when..." control and Portfolio "My
    alerts" tab, for the CRUD-adjacent bits alert_store.py exposes
    directly (this module only owns evaluation + notification).

CROSSING-DETECTION TIMING: nightly_scan.run_universe_scan()/
run_imported_scan() each call score_history.record(rows) themselves as
their very last step - by the time this module ever sees a scan's
`payload["rows"]`, TODAY's values are already the ones score_history.
latest() would return. So "previous value" is captured via
snapshot_previous_values() BEFORE any of tonight's scans run (a small,
cheap read - only tickers with an active alert need a prior-value lookup)
and threaded through every check_universe_rows() call for the rest of the
night. This is deliberately the only way alert_engine hooks into the
nightly job without editing nightly_scan.py itself - "engines called,
never modified" (this batch's own ground rule) stays true to the letter:
nightly_scan.py, moat_engine.py, deep_dive_engine.py etc. are untouched.

BATCHING: every fired alert is queued (alert_store.queue_hit) the moment
it's detected, and only actually emailed/pushed once, at the very end of
the whole nightly run (send_batched_notifications) - "batch all hits per
user into ONE email + ONE push per night" per Part 1's spec, regardless of
how many universes or the extra ticker pass contributed hits.
"""

import os

import requests

import alert_store
import email_auth
import i18n

AI_FEATURE = None  # this module never calls the Anthropic API - explicit,
# so a reviewer grepping for ai_gate/ai_client usage across the batch's
# five new modules finds nothing here and can move on immediately.


# --------------------------------------------------------------------
# Condition evaluation - pure functions, no I/O, easy to unit-test
# directly (see the boot-test harness for this part).
# --------------------------------------------------------------------

_METRIC_TO_HISTORY_COL = {
    "value_score": "long_score", "price": "price", "quality": "quality",
    "moat": "moat", "mos_pct": "mos_pct", "intrinsic_value": "intrinsic_value",
    "moat_state": "moat_state", "valuation_label": "valuation_label",
}

METRIC_LABELS = {
    "mos_pct": "MOS", "value_score": "Value Score", "quality": "Quality",
    "moat": "Moat", "price": "Price", "intrinsic_value": "Intrinsic value",
    "moat_state": "Moat state", "valuation_label": "Valuation",
}

_OP_SYMBOL = {">=": "≥", "<=": "≤"}


def _fmt_num(v):
    if v is None:
        return "-"
    try:
        return f"{float(v):,.1f}"
    except (TypeError, ValueError):
        return str(v)


def alert_fires(alert, row, prev):
    """True if `alert` fires against `row` (a nightly_scan-shaped dict for
    this alert's ticker, computed TONIGHT) given `prev` (the score_history
    row for the same ticker as of the last time it was scanned - a dict
    with the score_history column names, or None if this ticker has never
    been recorded before). Never raises on missing/None data - just
    doesn't fire, since an alert with no data to evaluate is the same as
    one that hasn't been met yet."""
    metric = alert["metric"]
    operator = alert["operator"]
    if metric in alert_store.NUMERIC_METRICS:
        key = alert_store.NUMERIC_METRICS[metric]
        current = row.get(key)
        if current is None:
            return False
        try:
            current = float(current)
            threshold = float(alert["threshold"])
        except (TypeError, ValueError):
            return False
        if operator == ">=":
            return current >= threshold
        if operator == "<=":
            return current <= threshold
        if operator in ("crosses_above", "crosses_below"):
            if not prev:
                return False
            prev_val = prev.get(_METRIC_TO_HISTORY_COL[metric])
            if prev_val is None:
                return False
            prev_val = float(prev_val)
            if operator == "crosses_above":
                return prev_val < threshold <= current
            return prev_val > threshold >= current
        return False
    if metric in alert_store.CATEGORICAL_METRICS:
        key = alert_store.CATEGORICAL_METRICS[metric]
        current = row.get(key)
        threshold = alert["threshold"]
        if current != threshold:
            return False
        if operator != "becomes":
            return False
        prev_val = prev.get(_METRIC_TO_HISTORY_COL[metric]) if prev else None
        # Fires on a genuine transition INTO this state; a ticker with no
        # recorded history yet also counts as a transition (so a
        # newly-created alert on an already-eroding stock doesn't have to
        # wait for a second night to catch it).
        return prev_val != current
    return False


def condition_text(alert, row, lang="en"):
    """The factual, non-advisory fragment used in both the notification
    template and the Deep Dive/My-alerts inline "condition met" caption -
    e.g. "MOS ≥ 40% (now 67.0%)" or "Moat state became eroding".

    lang (Español completion, Part 2): only affects the label/verb words
    around the numbers, via the i18n.py "alert.metric.*"/"alert.op.*"/
    "alert.condition_*" keys - deliberately a SEPARATE lookup from
    METRIC_LABELS (see this module's own "alert.metric.*" comment in
    i18n.py), so app.py's Deep Dive alert list (a documented Part 1 gap)
    is untouched by this. The threshold/current NUMBERS and operator
    symbols (≥/≤) are unaffected."""
    metric = alert["metric"]
    label = i18n.t(f"alert.metric.{metric}", lang) if f"alert.metric.{metric}" in i18n.EN else METRIC_LABELS.get(metric, metric)
    if metric in alert_store.NUMERIC_METRICS:
        key = alert_store.NUMERIC_METRICS[metric]
        current = _fmt_num(row.get(key))
        op = alert["operator"]
        if op in _OP_SYMBOL:
            return i18n.t(
                "alert.condition_numeric", lang, label=label, op=_OP_SYMBOL[op],
                threshold=_fmt_num(alert["threshold"]), current=current,
            )
        verb = i18n.t(
            "alert.op.crossed_above" if op == "crosses_above" else "alert.op.crossed_below",
            lang,
        )
        return i18n.t(
            "alert.condition_numeric", lang, label=label, op=verb,
            threshold=_fmt_num(alert["threshold"]), current=current,
        )
    return i18n.t("alert.condition_categorical", lang, label=label, value=alert["threshold"])


def hit_message(alert, row, lang="en"):
    ticker = alert["ticker"]
    return i18n.t(
        "alert.hit_message", lang, ticker=ticker,
        condition=condition_text(alert, row, lang=lang),
    )


# --------------------------------------------------------------------
# Nightly orchestration
# --------------------------------------------------------------------

def snapshot_previous_values(log=print):
    """{ticker: score_history row or None} for every ticker with at least
    one active alert - read ONCE, before any of tonight's scans run, so
    crossing-detection compares against last night's value, not tonight's
    (which score_history would otherwise already hold by the time this
    module sees a scan's results - see module docstring)."""
    import score_history
    tickers = alert_store.tickers_with_active_alerts()
    prev = {}
    for t in tickers:
        try:
            prev[t] = score_history.latest(t)
        except Exception as e:
            log(f"[alert_engine] prev-value lookup failed for {t}: {e}")
            prev[t] = None
    return prev


def check_universe_rows(rows, prev_map, log=print):
    """Evaluate every active alert whose ticker appears in `rows` (a
    completed universe/imported-scan's row list, or the extra pass's own
    small row list). Fired alerts are queued (not sent) and their
    last_fired_at/cooldown updated. Returns the number of hits queued."""
    fired = 0
    for row in rows:
        ticker = (row.get("Ticker") or "").strip().upper()
        if not ticker:
            continue
        alerts = alert_store.active_alerts_for_ticker(ticker)
        if not alerts:
            continue
        alert_store.mark_evaluated_today(ticker)
        prev = prev_map.get(ticker) if prev_map else None
        for a in alerts:
            try:
                if not alert_fires(a, row, prev):
                    continue
                if not alert_store.cooldown_ok(a):
                    continue
                alert_store.record_fire(a["id"])
                # Español completion, Part 2: the message is baked in at
                # fire time (queue_hit just stores plain text - no lang
                # column on the hit itself), so the recipient's own
                # sign-up language is resolved right here, per-hit, via
                # the same email_auth.get_signup_lang(email) pattern
                # announce_engine.py/digest_engine.py already use.
                _hit_lang = email_auth.get_signup_lang(a["email"])
                alert_store.queue_hit(a["id"], a["email"], ticker, hit_message(a, row, lang=_hit_lang))
                fired += 1
            except Exception as e:
                log(f"[alert_engine] alert #{a.get('id')} ({ticker}) evaluation failed: {e}")
    return fired


def run_extra_ticker_pass(prev_map, cap=200, log=print):
    """Part 1's "small nightly alert-only pass": for tickers that have an
    active alert but weren't part of any universe/imported scan tonight
    (e.g. a small-cap the user set an alert on that isn't in ASX 200/S&P
    500/their TradingView import), compute one lightweight snapshot each
    (reusing nightly_scan.analyze_ticker_lite - the exact same per-ticker
    scoring every other path uses) and evaluate against it, same as a
    regular universe row. Capped so a large alert list can never blow the
    nightly time budget on its own."""
    import nightly_scan
    import score_history

    tickers = alert_store.tickers_needing_extra_pass(cap=cap)
    if not tickers:
        return {"checked": 0, "fired": 0}
    log(f"[alert_engine] extra pass: {len(tickers)} alert-only ticker(s) with no scan tonight")
    rows = []
    for t in tickers:
        try:
            row = nightly_scan.analyze_ticker_lite(t, attention_lite=True)
            if row:
                nightly_scan._attach_moat(row, t, log=log)
                rows.append(row)
        except Exception as e:
            log(f"[alert_engine] extra pass {t}: {e}")
    fired = 0
    if rows:
        fired = check_universe_rows(rows, prev_map, log=log)
        try:
            score_history.record(rows)
        except Exception as e:
            log(f"[alert_engine] extra pass score_history.record failed: {e}")
    return {"checked": len(rows), "fired": fired}


# --------------------------------------------------------------------
# Notification send - same Mailgun shape as announce_engine.py/
# portfolio_watchdog_engine.py (no shared notify_engine module exists in
# this codebase, so this follows the established per-module duplication
# convention rather than inventing a new shared one).
# --------------------------------------------------------------------

def _cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <alerts@{domain}>" if domain else ""),
        "base_url": (
            os.environ.get("MAILGUN_BASE_URL", "").strip()
            or os.environ.get("MAILGUN_API_BASE_URL", "").strip().removesuffix("/v3")
            or "https://api.mailgun.net"
        ).rstrip("/"),
        "site": os.environ.get("SITE_BASE_URL", "").strip() or "https://stocksdeepdive.com",
    }


def is_configured():
    c = _cfg()
    return bool(c["api_key"] and c["domain"])


def _send(to_email, subject, html_body):
    c = _cfg()
    resp = requests.post(
        f"{c['base_url']}/v3/{c['domain']}/messages",
        auth=("api", c["api_key"]),
        data={"from": c["from"], "to": [to_email], "subject": subject, "html": html_body},
        timeout=20,
    )
    resp.raise_for_status()
    return True


def _hit_row_html(hit, site):
    ticker = hit["ticker"]
    link = f"{site}/deep-dive?ticker={ticker}"
    return f"""
<tr><td style="padding:10px 28px;font-family:Arial,Helvetica,sans-serif;
    font-size:13.5px;color:#334155;border-top:1px solid #e2e8f0;">
  <a href="{link}" style="color:#0f766e;text-decoration:none;font-weight:bold;">{ticker}</a>
  <div style="padding-top:4px;">{hit['message']}</div>
</td></tr>
"""


def _email_html(hits, site, lang="en"):
    """lang (Español completion, Part 2): the heading/footer chrome only -
    each hit's own `message` is already lang-baked from fire time (see
    check_universe_rows)."""
    n = len(hits)
    rows_html = "".join(_hit_row_html(h, site) for h in hits)
    heading = i18n.t("email.alert.heading_one" if n == 1 else "email.alert.heading_many", lang, n=n)
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f7fa;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="620" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border:1px solid #e2e8f0;">
    <tr><td style="padding:24px 28px 6px 28px;font-family:Arial,Helvetica,sans-serif;">
      <span style="font-size:22px;font-weight:bold;color:#0f172a;">Stocks</span><span style="font-size:22px;font-weight:bold;color:#0d9488;">DeepDive</span>
      <div style="font-size:15px;color:#334155;padding-top:6px;font-weight:bold;">{heading}</div>
    </td></tr>
    {rows_html}
    <tr><td style="padding:16px 28px 24px 28px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;line-height:1.6;">
      {i18n.t("email.alert.footer", lang)}
    </td></tr>
  </table>
</td></tr>
</table>
"""


def send_batched_notifications(log=print):
    """One email + one push per user, covering every hit queued tonight
    (across every universe scan and the extra pass) - then clears the
    queue. Safe to call with nothing queued (no-op)."""
    import push_send

    hits_by_email = alert_store.pending_hits_by_email()
    if not hits_by_email:
        return {"users_notified": 0}

    site = _cfg()["site"]
    email_configured = is_configured()
    push_configured = push_send.is_configured()
    notified = 0
    all_hit_ids = []

    for email, hits in hits_by_email.items():
        all_hit_ids.extend(h["hit_id"] for h in hits)
        sent_anything = False
        n = len(hits)
        # Español completion, Part 2: subject/heading/footer/push body in
        # the recipient's own sign-up language - same get_signup_lang()
        # pattern as the rest of this batch's email senders.
        _lang = email_auth.get_signup_lang(email)
        subject = i18n.t("email.alert.subject_one" if n == 1 else "email.alert.subject_many", _lang, n=n)
        if email_configured:
            try:
                _send(email, subject, _email_html(hits, site, lang=_lang))
                sent_anything = True
            except Exception as e:
                log(f"[alert_engine] email to {email} failed: {e}")
        if push_configured:
            try:
                push_send.send_to_email(
                    email, subject, i18n.t("push.alert.body", _lang),
                    url=f"{site}/portfolio",
                )
                sent_anything = True
            except Exception as e:
                log(f"[alert_engine] push to {email} failed: {e}")
        if sent_anything:
            notified += 1
            log(f"[alert_engine] notified {email} ({n} hit(s))")

    alert_store.clear_pending_hits(all_hit_ids)
    return {"users_notified": notified}
