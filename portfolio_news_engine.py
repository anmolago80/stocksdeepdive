"""
portfolio_news_engine.py

News Intelligence for "My Portfolio" holdings - ported from the desktop
Portfolio Health Monitor app's news_sources.py + news_intel_engine.py +
news_store.py, adapted for a multi-tenant web app:

  - STORAGE: the desktop app kept one JSON file per ticker under
    ./data/news/. News about a ticker is the same regardless of which
    signed-in user holds it, so here it's one shared SQLite table (same
    DB/volume convention as every other store in this app) keyed by
    (ticker, dedup_key) - a global cache, not per-user. Only the
    RELEVANCE check (thesis drivers) is evaluated per holding, at read
    time, from that shared event pool.
  - RATE LIMITING (new - the desktop app was a manually-run script, this
    is a website that re-executes on every click): a holding's feeds are
    only actually re-queried if the last fetch was more than
    REFETCH_STALE_HOURS ago (see news_fetch_log). Every render still
    reclassifies whatever's already stored - fresh classification is
    cheap and lets a thesis-driver edit re-apply instantly - only the
    network calls are throttled.

Same idea as the original: NOT ALL NEWS IS EQUAL. Every headline is
classified on two axes - SEVERITY (noise -> temporary -> material ->
thesis-breaking) and RELEVANCE (does it touch this holding's thesis?) -
and only news that is BOTH relevant AND at least material is allowed to
move the News Risk Score (0-100, starts at 100, pulled down by relevant
bad news, recency-weighted, capped per day so five outlets covering one
event can't quintuple the penalty).

Feeds (each wrapped so a dead/slow one returns [] instead of breaking a
run - same philosophy as the source app):
    GDELT        free, HISTORICAL (back to the purchase date) - the
                 backbone of "news since you bought this".
    ASX          official announcements for .AX tickers.
    Google News  RSS, breadth on recent coverage, no key.
    NewsAPI      optional - same NEWS_API_KEY resolution (st.secrets or
                 env var) this site's own news_engine.py already uses;
                 skipped entirely if no key is configured.
    yfinance     the ticker's own recent news feed, already a site
                 dependency.
"""

import datetime as _dt
import os
import sqlite3
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

import requests
import yfinance as yf

REFETCH_STALE_HOURS = 6
_UA = {"User-Agent": "StocksDeepDive/1.0 (+https://stocksdeepdive.com)"}
_TIMEOUT = 10


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS news_events (
            ticker TEXT NOT NULL,
            dedup_key TEXT NOT NULL,
            event_date TEXT,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            publisher TEXT NOT NULL DEFAULT '',
            link TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            domain TEXT NOT NULL DEFAULT '',
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (ticker, dedup_key)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS news_fetch_log (
            ticker TEXT PRIMARY KEY,
            last_fetched_at TEXT NOT NULL
        )"""
    )
    return conn


def _now():
    return _dt.datetime.utcnow()


def _iso(dt):
    return dt.isoformat() if isinstance(dt, _dt.datetime) else None


def _dedup_key(title, date):
    t = (title or "").strip().lower()
    t = "".join(ch for ch in t if ch.isalnum() or ch == " ")
    t = " ".join(t.split())[:70]
    day = date.strftime("%Y-%m-%d") if isinstance(date, _dt.datetime) else "undated"
    return f"{day}|{t}"


# --------------------------------------------------------------------------- #
# Persistent event store (shared across every user holding this ticker)
# --------------------------------------------------------------------------- #

def _load_events(ticker):
    with _conn() as conn:
        rows = conn.execute(
            "SELECT event_date, title, description, publisher, link, source, domain "
            "FROM news_events WHERE ticker = ?", (ticker,),
        ).fetchall()
    out = []
    for d, title, desc, pub, link, source, domain in rows:
        date = None
        if d:
            try:
                date = _dt.datetime.fromisoformat(d)
            except Exception:
                date = None
        out.append({"date": date, "title": title, "description": desc,
                     "publisher": pub, "link": link, "source": source, "domain": domain})
    return out


def _merge_and_save(ticker, fresh_events, max_keep=800):
    """Merge freshly-fetched items into the stored history, de-dup by
    (day, normalised title), prefer the richer record (one with a real
    date), prune to the newest `max_keep`, persist, return the merged
    list."""
    existing = _load_events(ticker)
    by_key = {}
    for e in existing + fresh_events:
        k = _dedup_key(e.get("title"), e.get("date"))
        prev = by_key.get(k)
        if prev is None or (not prev.get("date") and e.get("date")):
            by_key[k] = e
    merged = list(by_key.values())
    merged.sort(key=lambda e: e.get("date") or _dt.datetime.min, reverse=True)
    merged = merged[:max_keep]

    now_iso = _iso(_now())
    with _conn() as conn:
        conn.execute("DELETE FROM news_events WHERE ticker = ?", (ticker,))
        conn.executemany(
            "INSERT OR REPLACE INTO news_events "
            "(ticker, dedup_key, event_date, title, description, publisher, "
            " link, source, domain, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (ticker, _dedup_key(e.get("title"), e.get("date")), _iso(e.get("date")),
                 e.get("title") or "", e.get("description") or "", e.get("publisher") or "",
                 e.get("link") or "", e.get("source") or "", e.get("domain") or "", now_iso)
                for e in merged
            ],
        )
    return merged


def _should_refetch(ticker, max_age_hours=REFETCH_STALE_HOURS):
    with _conn() as conn:
        row = conn.execute(
            "SELECT last_fetched_at FROM news_fetch_log WHERE ticker = ?", (ticker,)
        ).fetchone()
    if not row:
        return True
    try:
        last = _dt.datetime.fromisoformat(row[0])
    except Exception:
        return True
    return (_now() - last) > _dt.timedelta(hours=max_age_hours)


def _mark_fetched(ticker):
    now_iso = _iso(_now())
    with _conn() as conn:
        conn.execute(
            "INSERT INTO news_fetch_log (ticker, last_fetched_at) VALUES (?, ?) "
            "ON CONFLICT(ticker) DO UPDATE SET last_fetched_at = excluded.last_fetched_at",
            (ticker, now_iso),
        )


# --------------------------------------------------------------------------- #
# Feed fetchers (ported near-verbatim from the desktop app's news_sources.py)
# --------------------------------------------------------------------------- #

def _fetch_gdelt(query, start_dt, end_dt=None, max_records=120):
    end_dt = end_dt or _now()
    params = {
        "query": f'"{query}"', "mode": "artlist", "format": "json",
        "maxrecords": str(max_records), "sort": "datedesc",
        "startdatetime": start_dt.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end_dt.strftime("%Y%m%d%H%M%S"),
    }
    try:
        r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc",
                          params=params, headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            return []
        out = []
        for a in (r.json() or {}).get("articles", []) or []:
            title = a.get("title") or ""
            if not title:
                continue
            out.append({
                "title": title, "description": "", "publisher": a.get("domain", "") or "GDELT",
                "link": a.get("url", ""), "published": _parse_gdelt_date(a.get("seendate")),
                "source": "gdelt", "domain": a.get("domain", ""),
            })
        return out
    except Exception:
        return []


def _parse_gdelt_date(s):
    if not s:
        return None
    try:
        return _dt.datetime.strptime(s[:15], "%Y%m%dT%H%M%S")
    except Exception:
        try:
            return _dt.datetime.strptime(s[:8], "%Y%m%d")
        except Exception:
            return None


def _fetch_asx_announcements(ticker, count=80):
    if not ticker.upper().endswith(".AX"):
        return []
    code = ticker.split(".")[0].upper()
    try:
        r = requests.get(
            f"https://www.asx.com.au/asx/1/company/{code}/announcements",
            params={"count": count, "market_sensitive": "false"},
            headers=_UA, timeout=_TIMEOUT,
        )
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            return []
        out = []
        for a in (r.json() or {}).get("data", []) or []:
            header = a.get("header") or a.get("title") or ""
            if not header:
                continue
            sens = a.get("market_sensitive")
            title = f"[ASX] {header}" + (" (price-sensitive)" if sens else "")
            out.append({
                "title": title, "description": header, "publisher": f"ASX:{code}",
                "link": a.get("url", "") or a.get("document_url", ""),
                "published": _parse_asx_date(a.get("document_date") or a.get("date")),
                "source": "asx", "domain": "asx.com.au",
            })
        return out
    except Exception:
        return []


def _parse_asx_date(s):
    if not s:
        return None
    s = str(s)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(s[:19], fmt)
        except Exception:
            continue
    return None


def _fetch_google_news(query, region="AU"):
    hl = "en-AU" if region == "AU" else "en-US"
    ceid = "AU:en" if region == "AU" else "US:en"
    try:
        r = requests.get(
            "https://news.google.com/rss/search",
            params={"q": query, "hl": hl, "gl": region, "ceid": ceid},
            headers=_UA, timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            return []
        try:
            root = ET.fromstring(r.text)
        except Exception:
            return []
        out = []
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            if not title:
                continue
            src = item.find("{*}source")
            publisher = (src.text if src is not None else None) or "Google News"
            out.append({
                "title": title, "description": (item.findtext("description") or "")[:400],
                "publisher": publisher, "link": item.findtext("link") or "",
                "published": _parse_rfc822(item.findtext("pubDate")),
                "source": "googlenews", "domain": "",
            })
        return out
    except Exception:
        return []


def _parse_rfc822(s):
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        return dt.replace(tzinfo=None) if dt else None
    except Exception:
        return None


def _newsapi_key():
    try:
        import streamlit as st
        if "NEWS_API_KEY" in st.secrets:
            return st.secrets["NEWS_API_KEY"]
    except Exception:
        pass
    return os.environ.get("NEWS_API_KEY", "")


def _fetch_newsapi(query, from_dt=None, page_size=80):
    api_key = _newsapi_key()
    if not api_key:
        return []
    params = {"q": query, "language": "en", "sortBy": "publishedAt",
              "pageSize": min(page_size, 100), "apiKey": api_key}
    if from_dt:
        earliest = _now() - _dt.timedelta(days=29)  # free tier: ~30d history only
        params["from"] = max(from_dt, earliest).strftime("%Y-%m-%d")
    try:
        r = requests.get("https://newsapi.org/v2/everything", params=params,
                          headers=_UA, timeout=_TIMEOUT)
        if r.status_code != 200:
            return []
        out = []
        for a in (r.json() or {}).get("articles", []) or []:
            title = a.get("title") or ""
            if not title:
                continue
            out.append({
                "title": title, "description": a.get("description") or "",
                "publisher": (a.get("source") or {}).get("name", "NewsAPI"),
                "link": a.get("url", ""), "published": _parse_iso(a.get("publishedAt")),
                "source": "newsapi", "domain": "",
            })
        return out
    except Exception:
        return []


def _parse_iso(s):
    if not s:
        return None
    try:
        return _dt.datetime.fromisoformat(str(s).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _fetch_yf_news(ticker):
    """yfinance's `.news` schema has shifted across releases (a nested
    'content' dict in newer versions, flatter keys in older ones) -
    handled defensively, per item, so one malformed entry never drops the
    rest."""
    out = []
    try:
        raw = yf.Ticker(ticker).news or []
    except Exception:
        return out
    for it in raw:
        try:
            content = it.get("content") if isinstance(it.get("content"), dict) else it
            title = (content.get("title") or it.get("title") or "").strip()
            if not title:
                continue
            link = ""
            ctu = content.get("clickThroughUrl")
            if isinstance(ctu, dict):
                link = ctu.get("url") or ""
            link = link or it.get("link") or ""
            publisher = "Yahoo Finance"
            provider = content.get("provider")
            if isinstance(provider, dict) and provider.get("displayName"):
                publisher = provider["displayName"]
            elif it.get("publisher"):
                publisher = it["publisher"]
            pub_date = _parse_iso(content.get("pubDate") or content.get("displayTime"))
            if pub_date is None and it.get("providerPublishTime"):
                try:
                    pub_date = _dt.datetime.utcfromtimestamp(int(it["providerPublishTime"]))
                except Exception:
                    pub_date = None
            out.append({
                "title": title, "description": content.get("summary", "") or "",
                "publisher": publisher, "link": link, "published": pub_date,
                "source": "yfinance", "domain": "",
            })
        except Exception:
            continue
    return out


def _queries(name, ticker):
    qs = []
    if name:
        qs.append(name)
    root = ticker.split(".")[0]
    if root and root.upper() not in {q.upper() for q in qs}:
        qs.append(root)
    return qs


def _fetch_all_feeds(ticker, name, start_dt, end_dt=None):
    end_dt = end_dt or _now()
    region = "AU" if ticker.upper().endswith(".AX") else "US"
    items = list(_fetch_yf_news(ticker))
    items += _fetch_asx_announcements(ticker)
    for q in _queries(name, ticker):
        items += _fetch_gdelt(q, start_dt, end_dt, max_records=150)
        items += _fetch_google_news(q, region=region)
        items += _fetch_newsapi(q, from_dt=start_dt)
    return items


# --------------------------------------------------------------------------- #
# Classification lexicons (verbatim from the desktop app - transparent,
# extend the lists rather than hiding logic behind a model)
# --------------------------------------------------------------------------- #

THESIS_BREAKING = [
    "fraud", "accounting scandal", "restates earnings", "earnings restatement",
    "material weakness", "sec investigation", "asic investigation", "delisting",
    "bankruptcy", "insolvency", "going concern", "chapter 11",
    "guidance withdrawn", "withdraws guidance", "dividend suspended",
    "suspends dividend", "class action", "ceo resigns", "cfo resigns",
    "ceo steps down", "cfo steps down", "patent expired", "loss of exclusivity",
    "trial failed", "phase 3 failure", "fda rejection", "complete response letter",
    "profit warning", "slashes guidance", "credit rating cut to junk",
    "major recall", "licence revoked", "covenant breach",
]
MATERIAL_RISK = [
    "misses estimates", "misses expectations", "cuts guidance",
    "lowers guidance", "lowered outlook", "downgrade", "downgraded",
    "cut to sell", "margin pressure", "shrinking margins", "impairment",
    "writedown", "write-down", "lawsuit", "sued", "regulatory probe",
    "investigation", "antitrust", "layoffs", "job cuts", "restructuring",
    "slowing growth", "declining sales", "falling revenue", "loses contract",
    "market share loss", "rising costs", "cost overrun", "recall",
    "short seller", "short report", "guidance miss", "weak outlook",
    "dividend cut", "credit downgrade",
]
TEMPORARY_ISSUE = [
    "supply chain", "one-off", "one off", "temporary", "weather", "delay",
    "delayed", "short-term", "short term", "seasonal", "currency headwind",
    "fx headwind", "logistics", "outage", "strike", "shortage",
]
POSITIVE = [
    "beats estimates", "beats expectations", "record revenue", "record profit",
    "record quarter", "raises guidance", "lifts guidance", "upgrade",
    "upgraded", "buy rating", "outperform", "price target raised",
    "fda approval", "approval granted", "new contract", "wins contract",
    "expansion", "buyback", "share buyback", "dividend increase",
    "raises dividend", "strong results", "accretive acquisition",
    "market share gain", "guidance raised", "milestone", "breakthrough",
]
_TYPE_BY_SEVERITY = {
    "positive": "positive", "noise": "neutral", "temporary": "warning",
    "material": "warning", "thesis-breaking": "thesis-threatening",
}
SEVERITY_HIT = {
    "thesis-breaking": 38.0, "material": 13.0, "temporary": 4.0,
    "positive": -6.0, "noise": 0.0,
}
NEWS_IMPACT = 0.55
NEWS_PERDAY_EXTRA = 0.40
SEVERITY_DEFS = [
    ("Noise", "noise", "Generic coverage, round-ups, unrelated peers, price "
     "chatter with no substance.", "No effect."),
    ("Temporary issue", "temporary", "A real but likely short-lived problem "
     "(supply chain, weather, one-off delay).", "Small, decaying effect if "
     "thesis-relevant."),
    ("Material risk", "material", "A substantive negative that affects the "
     "business (guidance cut, downgrade, lawsuit, margin pressure).",
     "Reduces the score when thesis-relevant."),
    ("Thesis-breaking", "thesis-breaking", "Strikes at why you bought "
     "(fraud, going concern, dividend suspended, trial failure, guidance "
     "withdrawn).", "Heavily reduces the score; caps the Thesis component at 30."),
    ("Positive", "positive", "Beats, upgrades, approvals, buybacks, dividend "
     "raises.", "Adds a little back when thesis-relevant."),
]
_NAME_STOPWORDS = {"the", "ltd", "limited", "inc", "corp", "group", "holdings",
                    "company", "co", "plc", "etf", "fund", "index"}


def _classify_severity(text):
    t = (text or "").lower()
    if any(k in t for k in THESIS_BREAKING):
        return "thesis-breaking"
    _has = lambda words: any(w in t for w in words)
    _severe_verbs = ("withdraw", "suspend", "slash", "scrap")
    _neg_verbs = ("cut", "lower", "trim", "reduce", "weak", "miss", "warn",
                  "downgrade", "disappoint", "soft")
    _guidance = ("guidance", "outlook", "forecast", "full-year", "fy26",
                 "fy25", "profit", "earnings")
    if _has(_guidance) and _has(_severe_verbs):
        return "thesis-breaking"
    if _has(_guidance) and _has(_neg_verbs):
        return "material"
    if any(k in t for k in POSITIVE):
        return "positive"
    if any(k in t for k in MATERIAL_RISK):
        return "material"
    if any(k in t for k in TEMPORARY_ISSUE):
        return "temporary"
    return "noise"


def _is_relevant(text, name, ticker, thesis_drivers, source):
    if source == "asx":
        return True
    t = (text or "").lower()
    for word in (name or "").lower().split():
        if len(word) > 2 and word in t and word not in _NAME_STOPWORDS:
            return True
    root = ticker.split(".")[0].lower()
    if len(root) >= 3 and root in t:
        return True
    for driver in (thesis_drivers or []):
        if driver.lower() in t:
            return True
    return False


def _recency_weight(published, buy_dt, now):
    if published is None:
        return 0.75
    span = (now - buy_dt).days or 1
    age = (now - published).days
    frac = 1.0 - max(0.0, min(age / span, 1.0))
    return 0.5 + 0.5 * frac


def _sev_rank(sev):
    return {"thesis-breaking": 3, "material": 2, "temporary": 1,
            "positive": 0, "noise": 0}.get(sev, 0)


def _parse_date(s):
    if not s:
        return None
    try:
        return _dt.datetime.strptime(str(s)[:10], "%Y-%m-%d")
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #

def analyze_holding_news(ticker, name=None, thesis_drivers=None, buy_date=None, now=None):
    """
    Full News Intelligence result for one holding - same shape as the
    desktop app's news_intel_engine.analyse():
        {news_risk_score, material, timeline, today, counts, all_relevant,
         scanned, source_counts, explain}
    Each event: {date, title, publisher, link, source, severity, type,
                 relevant, weight}

    Feeds are only actually re-queried if this ticker hasn't been fetched
    in the last REFETCH_STALE_HOURS - every call still reclassifies
    whatever's already stored, so editing a holding's thesis re-applies
    instantly without waiting on a refetch.
    """
    now = now or _now()
    buy_dt = _parse_date(buy_date) or (now - _dt.timedelta(days=365))
    ticker = ticker.upper()

    if _should_refetch(ticker):
        raw = _fetch_all_feeds(ticker, name, buy_dt, now)
        fresh = []
        for it in raw:
            title = (it.get("title") or "").strip()
            if not title:
                continue
            fresh.append({
                "date": it.get("published"), "title": title,
                "description": it.get("description", "") or "",
                "publisher": it.get("publisher", ""), "link": it.get("link", ""),
                "source": it.get("source", ""), "domain": it.get("domain", ""),
            })
        merged = _merge_and_save(ticker, fresh)
        _mark_fetched(ticker)
    else:
        merged = _load_events(ticker)

    events, source_counts = [], {}
    for it in merged:
        pub = it.get("date")
        if pub is not None and pub < buy_dt - _dt.timedelta(days=3):
            continue
        text = f"{it.get('title', '')} . {it.get('description', '')}"
        severity = _classify_severity(text)
        relevant = _is_relevant(text, name, ticker, thesis_drivers, it.get("source"))
        ttype = _TYPE_BY_SEVERITY[severity]
        if not relevant and severity in ("material", "temporary", "thesis-breaking"):
            ttype = "neutral"
        source_counts[it.get("source", "?")] = source_counts.get(it.get("source", "?"), 0) + 1
        events.append({
            "date": pub, "title": it.get("title", ""), "publisher": it.get("publisher", ""),
            "link": it.get("link", ""), "source": it.get("source", ""), "severity": severity,
            "type": ttype, "relevant": relevant, "weight": _recency_weight(pub, buy_dt, now),
        })

    material = False
    by_day = {}
    for e in events:
        if not e["relevant"] or SEVERITY_HIT.get(e["severity"], 0.0) == 0.0:
            continue
        day = e["date"].strftime("%Y-%m-%d") if e.get("date") else "undated"
        by_day.setdefault(day, []).append(e)

    score = 100.0
    for day, day_events in by_day.items():
        day_events.sort(key=lambda e: abs(SEVERITY_HIT[e["severity"]]) * e["weight"], reverse=True)
        for i, e in enumerate(day_events):
            hit = SEVERITY_HIT[e["severity"]] * e["weight"]
            hit *= 1.0 if i == 0 else NEWS_PERDAY_EXTRA
            score -= hit
            if e["severity"] in ("material", "thesis-breaking"):
                material = True
    score = round(max(0.0, min(score, 100.0)), 1)

    significant = [e for e in events if e["type"] != "neutral" or e["severity"] == "positive"]
    significant.sort(key=lambda e: e["date"] or _dt.datetime.min, reverse=True)

    recent_cut = now - _dt.timedelta(days=3)
    today = [
        e for e in events
        if e["relevant"] and e["severity"] in ("material", "thesis-breaking", "temporary")
        and (e["date"] is None or e["date"] >= recent_cut)
    ]
    today.sort(key=lambda e: (_sev_rank(e["severity"]), e["date"] or _dt.datetime.min), reverse=True)

    counts = {"positive": 0, "neutral": 0, "warning": 0, "thesis-threatening": 0}
    for e in significant:
        counts[e["type"]] = counts.get(e["type"], 0) + 1

    if not material:
        explain = ("No thesis-relevant material news found - news is not dragging "
                   "the health score.")
    elif counts.get("thesis-threatening"):
        explain = ("Thesis-threatening news detected - this is materially reducing "
                   "the health score and warrants immediate review.")
    else:
        explain = ("Relevant material news is weighing on the score; monitor whether "
                   "it proves temporary or structural.")

    return {
        "news_risk_score": score, "material": material, "timeline": significant,
        "today": today[:5], "counts": counts,
        "all_relevant": sum(1 for e in events if e["relevant"]), "scanned": len(events),
        "source_counts": source_counts, "explain": explain,
    }


_SEVERITY_COLOR = {
    "thesis-breaking": "#d03b3b", "material": "#e0912f",
    "temporary": "#c9a227", "positive": "#0ca30c", "noise": "#9aa0a6",
}


def timeline_html(events, limit=20):
    """Plain HTML/CSS event list (no matplotlib-to-PNG, matching the site's
    own component_bars_html() convention) for the Health tab's news
    sections - a left colour bar per event keyed to severity, title linked
    out to the source where available."""
    import html as _html

    if not events:
        return "<div style='color:#8aa0b8;font-size:13px;'>No significant news events found.</div>"
    rows = []
    for e in events[:limit]:
        color = _SEVERITY_COLOR.get(e.get("severity"), "#9aa0a6")
        d = e["date"].strftime("%d %b %Y") if e.get("date") else "Undated"
        title = _html.escape(e.get("title") or "")
        publisher = _html.escape(e.get("publisher") or "")
        link = e.get("link") or ""
        sev = _html.escape((e.get("severity") or "").replace("-", " ").title())
        title_html = (f"<a href='{_html.escape(link)}' target='_blank' rel='noopener' "
                       f"style='color:#c7d2e0;text-decoration:none;'>{title}</a>") if link else title
        rows.append(
            "<div style='display:flex;gap:10px;align-items:flex-start;margin:8px 0;padding-left:10px;"
            f"border-left:3px solid {color};'>"
            f"<div style='min-width:88px;color:#8aa0b8;font-size:12px;'>{d}</div>"
            "<div style='flex:1;'>"
            f"<div style='font-size:13px;'>{title_html}</div>"
            f"<div style='font-size:11px;color:#8aa0b8;'>{publisher}"
            f" · <span style='color:{color};font-weight:700;'>{sev}</span></div>"
            "</div></div>"
        )
    return "<div>" + "".join(rows) + "</div>"


def summarise(analysis, limit=6):
    events = list(analysis.get("timeline", []))
    events.sort(key=lambda e: (e.get("relevant", False), _sev_rank(e["severity"]),
                               e["date"] or _dt.datetime.min), reverse=True)
    out = []
    for e in events[:limit]:
        d = e["date"].strftime("%d %b") if e.get("date") else "recent"
        tag = e["severity"].replace("-", " ")
        out.append(f"{d} — {e['title'].strip()} ({tag})")
    if not out:
        out.append("No significant news found in the window.")
    return out
