"""
digest_engine.py

The weekly watchlist digest email, sent through Mailgun.

For every signed-in user with a non-empty watchlist (watchlist_store), each
of their tickers gets an attention-lite re-score (nightly_scan.
analyze_ticker_lite - same maths as the site, no Trends/News/Social), and
the result goes out as one compact HTML email: price, margin of safety,
Long Score and signal per stock, with a link back to each Deep Dive.

CONFIG (Railway environment variables):
  MAILGUN_API_KEY   - required. Sending is silently skipped without it.
  MAILGUN_DOMAIN    - required. The sending domain configured in Mailgun.
  MAILGUN_FROM      - optional. Default: StocksDeepDive <digest@MAILGUN_DOMAIN>
  MAILGUN_BASE_URL  - optional. Default https://api.mailgun.net (use
                      https://api.eu.mailgun.net for EU-region domains).
  SITE_BASE_URL     - optional. Default https://stocksdeepdive.com - used
                      for the Deep Dive links in the email.

Failure model: per-user try/except - one bad ticker or one bounced address
never stops the rest of the send. Returns a summary dict for logging.
"""

import os
from datetime import datetime, timezone

import requests

import watchlist_store

# Mirrors the site's FACTUAL_MODE: when on (the default), the digest shows
# numbers only - no Signal column, no verdicts - matching what the public
# site presents.
FACTUAL_MODE = (os.environ.get("FACTUAL_MODE", "true").strip().lower()
                not in ("false", "0", "no", "off"))


def _cfg():
    domain = os.environ.get("MAILGUN_DOMAIN", "").strip()
    return {
        "api_key": os.environ.get("MAILGUN_API_KEY", "").strip(),
        "domain": domain,
        "from": os.environ.get("MAILGUN_FROM", "").strip()
                or (f"StocksDeepDive <digest@{domain}>" if domain else ""),
        # Same variables the feedback engine already uses on this
        # deployment - MAILGUN_API_BASE_URL may carry a trailing /v3,
        # which is stripped since the send path below adds it.
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


_SIGNAL_STYLE = {
    # Solid, email-safe colours (no 8-digit hex / alpha - Outlook's Word
    # renderer drops those): light background + dark text per verdict.
    "STRONG LONG": ("#e9f6ee", "#15803d"),
    "LONG": ("#e9f6ee", "#15803d"),
    "WATCHLIST": ("#fdf5e0", "#8a5a00"),
    "AVOID": ("#fdecec", "#b91c1c"),
}

# Every cell inline-styled and inside a fixed-width table - Outlook and
# Hotmail ignore <style> blocks and most modern CSS, so email HTML has to
# be written like it's 2003: nested tables, explicit widths and aligns,
# cellpadding, nothing clever.
_TD = "padding:9px 10px;font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#1e293b;border-bottom:1px solid #e2e8f0;"
_TH = "padding:9px 10px;font-family:Arial,Helvetica,sans-serif;font-size:12px;color:#64748b;border-bottom:2px solid #cbd5e1;text-transform:uppercase;letter-spacing:0.5px;"


def _row_html(row, site):
    iv = row.get("Intrinsic Value")
    mos = row.get("MOS %")
    sig = row.get("Signal", "-")
    bg, fg = _SIGNAL_STYLE.get(sig, ("#eef2f6", "#475569"))
    link = f"{site}/deep-dive?ticker={row['Ticker']}"
    iv_cell = f"{iv:,.2f}" if iv else "-"
    mos_cell = f"{mos:+.1f}%" if mos is not None else "-"
    _cells = (
        "<tr>"
        f"<td align='left' style='{_TD}font-weight:bold;'>"
        f"<a href='{link}' style='color:#0f766e;text-decoration:none;'>{row['Ticker']}</a></td>"
        f"<td align='right' style='{_TD}'>{row['Price']:,.2f}</td>"
        f"<td align='right' style='{_TD}'>{iv_cell}</td>"
        f"<td align='right' style='{_TD}'>{mos_cell}</td>"
        f"<td align='right' style='{_TD}'>{row.get('Long Score', '-')}</td>"
    )
    if not FACTUAL_MODE:
        _cells += (
            f"<td align='center' style='{_TD}'>"
            f"<span style='background-color:{bg};color:{fg};padding:3px 10px;"
            f"font-family:Arial,Helvetica,sans-serif;font-size:12px;font-weight:bold;'>"
            f"{sig}</span></td>"
        )
    return _cells + "</tr>"


def _email_html(email, rows, site):
    body_rows = "".join(_row_html(r, site) for r in rows)
    date_label = datetime.now(timezone.utc).strftime("%d %b %Y")
    return f"""\
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#f4f7fa;">
<tr><td align="center" style="padding:24px 12px;">
  <table role="presentation" width="620" cellpadding="0" cellspacing="0" border="0" style="background-color:#ffffff;border:1px solid #e2e8f0;">
    <tr><td style="padding:24px 28px 6px 28px;font-family:Arial,Helvetica,sans-serif;">
      <span style="font-size:22px;font-weight:bold;color:#0f172a;">Stocks</span><span style="font-size:22px;font-weight:bold;color:#0d9488;">DeepDive</span>
      <div style="font-size:15px;color:#334155;padding-top:6px;font-weight:bold;">Your watchlist this week</div>
      <div style="font-size:13px;color:#64748b;padding-top:4px;">Weekly re-score of the stocks you saved, as of {date_label}. Click any ticker for its full live Deep Dive.</div>
    </td></tr>
    <tr><td style="padding:14px 28px 4px 28px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <th align="left" style="{_TH}">Ticker</th>
          <th align="right" style="{_TH}">Price</th>
          <th align="right" style="{_TH}">Intrinsic value</th>
          <th align="right" style="{_TH}">MOS</th>
          <th align="right" style="{_TH}">{'Value Score' if FACTUAL_MODE else 'Long Score'}</th>
          {'' if FACTUAL_MODE else f'<th align="center" style="{_TH}">Signal</th>'}
        </tr>
        {body_rows}
      </table>
    </td></tr>
    <tr><td style="padding:16px 28px 24px 28px;font-family:Arial,Helvetica,sans-serif;font-size:11px;color:#94a3b8;line-height:1.6;">
      {'Factual information and calculator outputs only - this email describes data and model outputs computed from stated inputs; it contains no recommendations to buy, hold or sell any security.'
       if FACTUAL_MODE else
       'General information only - not financial advice; scores and signals are model outputs and do not consider your personal circumstances.'}
      Scored without news/social attention inputs
      (the weekly digest uses the attention-lite model). You're receiving this because
      {email} saved a watchlist on StocksDeepDive while signed in. To stop these,
      remove all stocks from your watchlist on the site.
    </td></tr>
  </table>
</td></tr>
</table>
"""


def run_weekly_digest(log=print):
    """Send the digest to every user with a non-empty watchlist. Returns
    {'sent': n, 'skipped': n, 'errors': n}."""
    summary = {"sent": 0, "skipped": 0, "errors": 0}
    if not is_configured():
        log("[digest] Mailgun not configured (MAILGUN_API_KEY / MAILGUN_DOMAIN) - skipping")
        return summary

    from nightly_scan import analyze_ticker_lite  # deferred: heavy import
    site = _cfg()["site"]

    for email, tickers in watchlist_store.all_users():
        try:
            rows = []
            for t in tickers[:20]:  # sanity cap per user
                try:
                    r = analyze_ticker_lite(t)
                    if r:
                        rows.append(r)
                except Exception:
                    continue
            if not rows:
                summary["skipped"] += 1
                continue
            rows.sort(key=lambda r: r.get("Long Score") or 0, reverse=True)
            _subject = (
                "Your StocksDeepDive watchlist - weekly update" if FACTUAL_MODE
                else "Your StocksDeepDive watchlist - weekly signals"
            )
            _send(email, _subject, _email_html(email, rows, site))
            summary["sent"] += 1
            log(f"[digest] sent to {email} ({len(rows)} stocks)")
        except Exception as e:
            summary["errors"] += 1
            log(f"[digest] {email}: {e}")
    return summary


if __name__ == "__main__":
    run_weekly_digest()
