"""
announce_engine.py

The "new research is up" email, sent to followers (follow_store.py) when
the admin rebuilds the Rational Compounder dataset from an updated
workbook. Modelled directly on digest_engine.py: same Mailgun `_cfg()` env
pattern, same table-based inline-style email HTML (Outlook/Hotmail ignore
`<style>` blocks), same per-recipient try/except failure model.

CONFIG (Railway environment variables) - identical to digest_engine.py,
shares the same Mailgun account:
  MAILGUN_API_KEY   - required. Sending is silently skipped without it.
  MAILGUN_DOMAIN    - required. The sending domain configured in Mailgun.
  MAILGUN_FROM      - optional. Default: StocksDeepDive <research@MAILGUN_DOMAIN>
  MAILGUN_BASE_URL  - optional. Default https://api.mailgun.net (use
                      https://api.eu.mailgun.net for EU-region domains).
  SITE_BASE_URL     - optional. Default https://stocksdeepdive.com - used
                      for the Research page links in the email.

WHO GETS EMAILED: followers of follow_store.ALL_TICKERS ("*") get every
rebuild's added/updated companies; followers of one specific ticker only
get an email when THAT ticker is among the added/updated set this time -
never for unrelated companies.

Failure model: per-recipient try/except - one bad address never stops the
rest of the send. Returns a summary dict for logging / the admin panel's
st.success line.
"""

import os
from datetime import datetime, timezone

import requests

import email_auth
import follow_store
import i18n
import push_send


def _cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <research@{domain}>" if domain else ""),
        "base_url": (
            os.environ.get("MAILGUN_BASE_URL", "").strip()
            or os.environ.get("MAILGUN_API_BASE_URL", "").strip().removesuffix("/v3")
            or "https://api.mailgun.net"
        ).rstrip("/"),
        "site": os.environ.get("SITE_BASE_URL", "").strip()
                or "https://stocksdeepdive.com",
    }


def is_configured():
    c = _cfg()
    return bool(c["api_key"] and c["domain"])


def _send(to_email, subject, html_body):
    c = _cfg()
    resp = requests.post(
        f"{c['base_url']}/v3/{c['domain']}/messages",
        auth=("api", c["api_key"]),
        data={"from": c["from"], "to": [to_email],
              "subject": subject, "html": html_body},
        timeout=20,
    )
    resp.raise_for_status()
    return True


# Same "write email HTML like it's 2003" rule as digest_engine.py - nested
# tables, explicit widths/aligns, everything inline-styled.
_TD = "padding:9px 10px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1e293b;border-bottom:1px solid #e2e8f0;"
_TH = "padding:9px 10px;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#64748b;border-bottom:2px solid #cbd5e1;text-transform:uppercase;letter-spacing:0.5px;"


def _row_html(ticker, added_set, site, lang="en"):
    is_added = ticker in added_set
    tag = i18n.t("email.announce.tag_added" if is_added else "email.announce.tag_updated", lang)
    tag_color = "#15803d" if is_added else "#0d9488"
    link = f"{site}/research?ticker={ticker}&src=research-email"
    return (
        "<tr>"
        f"<td align='left' style='{_TD}font-weight:bold;'>"
        f"<a href='{link}' style='color:#0f766e;text-decoration:none;'>{ticker}</a></td>"
        f"<td align='left' style='{_TD}color:{tag_color};font-weight:bold;'>{tag}</td>"
        "</tr>"
    )


def _email_html(tickers, added, updated, site, lang="en"):
    """lang: "en" (default) or "es" - cleanup round, Part 3/Español Part
    4. Every piece of copy routes through i18n.t() (email.announce.*);
    the date stamp's month abbreviation is the one un-translated piece
    (Python's strftime needs a Spanish locale installed on the server to
    localize it - a documented, cosmetic gap, see i18n.py's own
    docstring)."""
    added_set = set(added)
    body_rows = "".join(_row_html(t, added_set, site, lang) for t in tickers)
    date_label = datetime.now(timezone.utc).strftime("%d %b %Y")
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f7fa;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="580" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border:1px solid #e2e8f0;">
    <tr><td style="padding:24px 28px 6px 28px;font-family:Arial,Helvetica,sans-serif;">
      <span style="font-size:22px;font-weight:bold;color:#0f172a;">Stocks</span><span style="font-size:22px;font-weight:bold;color:#0d9488;">DeepDive</span>
      <div style="font-size:15px;color:#334155;padding-top:6px;font-weight:bold;">{i18n.t("email.announce.heading", lang)}</div>
      <div style="font-size:13px;color:#64748b;padding-top:4px;">{i18n.t("email.announce.intro", lang, date=date_label)}</div>
    </td></tr>
    <tr><td style="padding:14px 28px 4px 28px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <th align="left" style="{_TH}">{i18n.t("email.announce.th_ticker", lang)}</th>
          <th align="left" style="{_TH}">{i18n.t("email.announce.th_change", lang)}</th>
        </tr>
        {body_rows}
      </table>
    </td></tr>
    <tr><td style="padding:16px 28px 24px 28px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;line-height:1.6;">
      {i18n.t("email.announce.footer", lang)}
    </td></tr>
  </table>
</td></tr>
</table>
"""


def announce_rebuild(added_tickers, updated_tickers, log=print):
    """Email every follower whose subscription covers this rebuild:
    followers of "*" (all new research) always get it; followers of one
    specific ticker only get it when that ticker is in `added_tickers` or
    `updated_tickers`. Returns {'sent': n, 'skipped': n, 'errors': n}."""
    summary = {"sent": 0, "skipped": 0, "errors": 0}

    added = sorted({(t or "").strip().upper() for t in (added_tickers or []) if t})
    updated_set = {(t or "").strip().upper() for t in (updated_tickers or []) if t} - set(added)
    updated = sorted(updated_set)
    all_tickers = added + updated
    if not all_tickers:
        log("[announce] no added/updated tickers this rebuild - nothing to send")
        return summary
    if not is_configured():
        log("[announce] Mailgun not configured (MAILGUN_API_KEY / MAILGUN_DOMAIN) - skipping")
        return summary

    site = _cfg()["site"]

    # recipient email -> the tickers THEY should be told about (all of them
    # for a "*" follower, only their own matching ticker(s) for the rest).
    recipients = {}
    try:
        for email in follow_store.followers_of(follow_store.ALL_TICKERS):
            recipients[email] = list(all_tickers)
        for t in all_tickers:
            for email in follow_store.followers_of(t):
                recipients.setdefault(email, [])
                if t not in recipients[email]:
                    recipients[email].append(t)
    except Exception as e:
        log(f"[announce] couldn't read followers: {e}")
        return summary

    push_configured = push_send.is_configured()
    summary["push_sent"] = 0

    for email, tickers in recipients.items():
        try:
            if not tickers:
                summary["skipped"] += 1
                continue
            # Cleanup round, Part 3/Español Part 4: each recipient gets
            # the email in their OWN sign-up language, not the site's
            # current default - see email_auth.get_signup_lang's own
            # docstring for what a missing/pre-existing row falls back to.
            _lang = email_auth.get_signup_lang(email)
            subject = i18n.t(
                "email.announce.subject", _lang, tickers=", ".join(tickers),
            )
            _send(email, subject, _email_html(tickers, added, updated, site, lang=_lang))
            summary["sent"] += 1
            log(f"[announce] sent to {email} ({len(tickers)} tickers)")
        except Exception as e:
            summary["errors"] += 1
            log(f"[announce] {email}: {e}")

        # Best-effort push alongside the email above - same recipient list,
        # same tickers, but its own failure path (a bad/expired push
        # subscription must never affect whether the email above sent, and
        # vice versa; push_send.send_to_email already prunes dead endpoints
        # and never raises).
        if push_configured and tickers:
            try:
                title = "New research: " + ", ".join(tickers)
                link_ticker = tickers[0]
                result = push_send.send_to_email(
                    email, title,
                    "Tap to open the updated research on StocksDeepDive.",
                    url=f"{site}/research?ticker={link_ticker}&src=research-push",
                )
                summary["push_sent"] += result.get("sent", 0)
            except Exception as e:
                log(f"[announce] push to {email}: {e}")

    return summary
