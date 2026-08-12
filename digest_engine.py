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


_SIGNAL_COLOR = {
    "STRONG LONG": "#15803d", "LONG": "#15803d",
    "WATCHLIST": "#ca8a04", "AVOID": "#dc2626",
}


def _row_html(row, site):
    iv = row.get("Intrinsic Value")
    mos = row.get("MOS %")
    sig = row.get("Signal", "-")
    color = _SIGNAL_COLOR.get(sig, "#475569")
    link = f"{site}/deep-dive?ticker={row['Ticker']}"
    iv_cell = f"{iv:,.2f}" if iv else "-"
    mos_cell = f"{mos:+.1f}%" if mos is not None else "-"
    return (
        "<tr>"
        f"<td style='padding:8px 10px;font-weight:600;'>"
        f"<a href='{link}' style='color:#0f766e;text-decoration:none;'>{row['Ticker']}</a></td>"
        f"<td style='padding:8px 10px;'>{row['Price']:,.2f}</td>"
        f"<td style='padding:8px 10px;'>{iv_cell}</td>"
        f"<td style='padding:8px 10px;'>{mos_cell}</td>"
        f"<td style='padding:8px 10px;'>{row.get('Long Score', '-')}</td>"
        f"<td style='padding:8px 10px;'>"
        f"<span style='background:{color}22;color:{color};padding:3px 10px;"
        f"border-radius:10px;font-size:12px;font-weight:700;'>{sig}</span></td>"
        "</tr>"
    )


def _email_html(email, rows, site):
    body_rows = "".join(_row_html(r, site) for r in rows)
    date_label = datetime.now(timezone.utc).strftime("%d %b %Y")
    return f"""
<div style="font-family:'Segoe UI',Arial,sans-serif;max-width:640px;margin:0 auto;color:#1e293b;">
  <h2 style="letter-spacing:-.3px;">Stocks<span style="color:#0d9488;">DeepDive</span>
    &mdash; your watchlist this week</h2>
  <p style="color:#475569;font-size:14px;">Weekly re-score of the stocks you saved,
  as of {date_label}. Click any ticker for its full live Deep Dive.</p>
  <table style="width:100%;border-collapse:collapse;font-size:14px;">
    <thead><tr style="text-align:left;color:#64748b;border-bottom:2px solid #e2e8f0;">
      <th style="padding:8px 10px;">Ticker</th><th style="padding:8px 10px;">Price</th>
      <th style="padding:8px 10px;">Intrinsic value</th><th style="padding:8px 10px;">MOS</th>
      <th style="padding:8px 10px;">Long Score</th><th style="padding:8px 10px;">Signal</th>
    </tr></thead>
    <tbody>{body_rows}</tbody>
  </table>
  <p style="color:#94a3b8;font-size:12px;margin-top:22px;line-height:1.6;">
  General information only - not financial advice; scores and signals are model outputs and do
  not consider your personal circumstances. Scored without news/social attention inputs
  (weekly digest uses the attention-lite model). You're receiving this because
  {email} saved a watchlist on StocksDeepDive while signed in. To stop these,
  remove all stocks from your watchlist on the site.</p>
</div>
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
            _send(email, "Your StocksDeepDive watchlist - weekly signals",
                  _email_html(email, rows, site))
            summary["sent"] += 1
            log(f"[digest] sent to {email} ({len(rows)} stocks)")
        except Exception as e:
            summary["errors"] += 1
            log(f"[digest] {email}: {e}")
    return summary


if __name__ == "__main__":
    run_weekly_digest()
