"""
blog_store.py

Storage for the StocksDeepDive blog - the site's one genuinely
search-indexable surface.

Why this exists at all: the app itself is Streamlit, which streams its
content to the browser over a websocket. There is no HTML in the server's
response, so Google has nothing to index on any app page. The blog is
therefore served as real, server-rendered HTML by server.py (see that
module's docstring); this file is only the storage layer both sides share -
the Streamlit admin editor writes here, the public HTML renderer reads
from here.

Same SQLite file and volume-resolution rule as positions_store.py /
watchlist_store.py / email_auth.py: everything lives in stocksdeepdive.db
on the Railway Volume (RAILWAY_VOLUME_MOUNT_PATH, mounted at /data), so
posts survive redeploys. Callers never touch SQL directly.

Two tables:

  blog_posts        one row per post, draft or published.
  blog_redirects    old_slug -> new_slug. Renaming a published post's slug
                    would otherwise break a URL Google has already indexed
                    (and any link pointing at it), so every rename leaves a
                    permanent 301 behind instead.
"""

import os
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone


def _data_dir():
    return os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or os.path.dirname(__file__)


DB_PATH = os.path.join(_data_dir(), "stocksdeepdive.db")

# Uploaded hero images live next to the database on the same volume, so
# they survive redeploys too. server.py serves them at /blog/media/<name>.
MEDIA_DIR = os.path.join(_data_dir(), "blog_media")

STATUS_DRAFT = "draft"
STATUS_PUBLISHED = "published"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS blog_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            body_md TEXT NOT NULL DEFAULT '',
            hero_file TEXT,
            hero_alt TEXT,
            tags TEXT NOT NULL DEFAULT '',
            author TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'draft',
            published_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS blog_redirects (
            old_slug TEXT PRIMARY KEY,
            new_slug TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    return conn


def ensure_media_dir():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    return MEDIA_DIR


# -----------------------------------
# SLUGS
#
# The slug IS the URL (/blog/<slug>), so it is the single most permanent
# thing about a post. Kept to lowercase ASCII words joined by hyphens -
# what Google's own URL guidance asks for, and what survives being pasted
# into an email or a tweet without percent-encoding.
# -----------------------------------

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text, max_len=70):
    if not text:
        return ""
    # Decompose accents (é -> e) rather than dropping the letter entirely.
    text = unicodedata.normalize("NFKD", str(text))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = _SLUG_STRIP.sub("-", text).strip("-")
    if len(text) > max_len:
        # Cut on a word boundary so the slug never ends mid-word.
        text = text[:max_len].rsplit("-", 1)[0]
    return text.strip("-")


def unique_slug(desired, exclude_id=None):
    """A slug guaranteed free right now. Collisions get -2, -3, ... rather
    than silently overwriting somebody else's URL."""
    base = slugify(desired) or "post"
    candidate = base
    n = 1
    while True:
        existing = get_post(candidate, include_drafts=True)
        taken_by_other = existing and existing["id"] != exclude_id
        # A slug parked in the redirect table is also taken: reusing it
        # would create a redirect loop.
        if not taken_by_other and not _redirect_target(candidate):
            return candidate
        n += 1
        candidate = f"{base}-{n}"


def _redirect_target(old_slug):
    with _conn() as conn:
        row = conn.execute(
            "SELECT new_slug FROM blog_redirects WHERE old_slug = ?",
            (old_slug,),
        ).fetchone()
    return row[0] if row else None


def resolve_redirect(old_slug):
    """new slug for a renamed post, or None. Followed a couple of hops so a
    post renamed twice still resolves from its oldest URL."""
    seen = set()
    slug = old_slug
    for _ in range(5):
        target = _redirect_target(slug)
        if not target or target in seen:
            break
        seen.add(target)
        slug = target
    return slug if slug != old_slug else None


# -----------------------------------
# READS
# -----------------------------------

def get_post(slug, include_drafts=False):
    if not slug:
        return None
    sql = "SELECT * FROM blog_posts WHERE slug = ?"
    if not include_drafts:
        sql += " AND status = 'published'"
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, (slug.strip().lower(),)).fetchone()
    return dict(row) if row else None


def get_post_by_id(post_id):
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM blog_posts WHERE id = ?", (post_id,)
        ).fetchone()
    return dict(row) if row else None


def list_posts(include_drafts=False, limit=None, tag=None):
    """Newest first. Published posts order by published_at; drafts (which
    have no published_at) sort to the top for the admin, where they are the
    things still needing attention."""
    sql = "SELECT * FROM blog_posts"
    params = []
    if not include_drafts:
        sql += " WHERE status = 'published'"
    sql += " ORDER BY COALESCE(published_at, updated_at) DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    with _conn() as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if tag:
        t = tag.strip().lower()
        rows = [r for r in rows if t in tag_list(r)]
    return rows


def tag_list(post):
    raw = (post or {}).get("tags") or ""
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def all_tags():
    counts = {}
    for p in list_posts():
        for t in tag_list(p):
            counts[t] = counts.get(t, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def count_posts(include_drafts=False):
    sql = "SELECT COUNT(*) FROM blog_posts"
    if not include_drafts:
        sql += " WHERE status = 'published'"
    with _conn() as conn:
        return conn.execute(sql).fetchone()[0]


def last_modified(include_drafts=False):
    """Most recent updated_at across posts - the sitemap's own timestamp."""
    sql = "SELECT MAX(updated_at) FROM blog_posts"
    if not include_drafts:
        sql += " WHERE status = 'published'"
    with _conn() as conn:
        return conn.execute(sql).fetchone()[0]


# -----------------------------------
# WRITES
# -----------------------------------

def create_post(title, slug=None, summary="", body_md="", tags="", author="",
                hero_file=None, hero_alt="", status=STATUS_DRAFT,
                published_at=None):
    slug = unique_slug(slug or title)
    now = _now()
    if status == STATUS_PUBLISHED and not published_at:
        published_at = now
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO blog_posts
                 (slug, title, summary, body_md, hero_file, hero_alt, tags,
                  author, status, published_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (slug, title.strip(), summary.strip(), body_md, hero_file,
             (hero_alt or "").strip(), _clean_tags(tags), author.strip(),
             status, published_at, now, now),
        )
        return cur.lastrowid


def update_post(post_id, title=None, slug=None, summary=None, body_md=None,
                tags=None, author=None, hero_file=..., hero_alt=None,
                status=None, published_at=...):
    """Partial update - only the fields passed are touched. hero_file and
    published_at use Ellipsis rather than None as their "not supplied"
    marker, because None is a meaningful value for both (remove the image /
    unpublish)."""
    existing = get_post_by_id(post_id)
    if not existing:
        return None

    fields, params = {}, []

    if slug is not None:
        new_slug = unique_slug(slug, exclude_id=post_id)
        if new_slug != existing["slug"]:
            fields["slug"] = new_slug
            # Only a slug that has actually been public is worth a
            # redirect; a draft's URL was never indexed or shared.
            if existing["status"] == STATUS_PUBLISHED:
                _add_redirect(existing["slug"], new_slug)

    if title is not None:
        fields["title"] = title.strip()
    if summary is not None:
        fields["summary"] = summary.strip()
    if body_md is not None:
        fields["body_md"] = body_md
    if tags is not None:
        fields["tags"] = _clean_tags(tags)
    if author is not None:
        fields["author"] = author.strip()
    if hero_file is not Ellipsis:
        fields["hero_file"] = hero_file
    if hero_alt is not None:
        fields["hero_alt"] = hero_alt.strip()
    if status is not None:
        fields["status"] = status
        # First publish stamps the date; later edits leave it alone, so an
        # old post that gets a typo fixed doesn't jump to the top of the
        # index (or claim a false publication date in its schema markup).
        if (status == STATUS_PUBLISHED and not existing["published_at"]
                and published_at is Ellipsis):
            fields["published_at"] = _now()
    if published_at is not Ellipsis:
        fields["published_at"] = published_at

    if not fields:
        return existing["slug"]

    fields["updated_at"] = _now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    params = list(fields.values()) + [post_id]
    with _conn() as conn:
        conn.execute(f"UPDATE blog_posts SET {sets} WHERE id = ?", params)
    return fields.get("slug", existing["slug"])


def delete_post(post_id):
    post = get_post_by_id(post_id)
    if not post:
        return
    with _conn() as conn:
        conn.execute("DELETE FROM blog_posts WHERE id = ?", (post_id,))
        # Redirects pointing AT a deleted post are dead weight - drop them
        # so the URL returns an honest 404 rather than redirecting to one.
        conn.execute("DELETE FROM blog_redirects WHERE new_slug = ?",
                     (post["slug"],))


def _add_redirect(old_slug, new_slug):
    with _conn() as conn:
        conn.execute(
            """INSERT INTO blog_redirects (old_slug, new_slug, created_at)
               VALUES (?, ?, ?)
               ON CONFLICT(old_slug) DO UPDATE SET
                 new_slug = excluded.new_slug,
                 created_at = excluded.created_at""",
            (old_slug, new_slug, _now()),
        )
        # Anything that used to point at old_slug should now point at the
        # new one directly, so a chain never grows past one hop.
        conn.execute(
            "UPDATE blog_redirects SET new_slug = ? WHERE new_slug = ?",
            (new_slug, old_slug),
        )


def _clean_tags(tags):
    if not tags:
        return ""
    if isinstance(tags, (list, tuple)):
        parts = list(tags)
    else:
        parts = str(tags).split(",")
    out, seen = [], set()
    for p in parts:
        t = p.strip().lower()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return ", ".join(out)


# -----------------------------------
# MEDIA
# -----------------------------------

_SAFE_MEDIA = re.compile(r"^[A-Za-z0-9._-]+$")


def save_media(filename, data):
    """Writes an uploaded image to the volume, returns the stored filename.
    The name is rebuilt from a slug of the original rather than trusted, so
    an upload can never escape MEDIA_DIR via '../' or land a filename the
    URL router can't express."""
    ensure_media_dir()
    stem, ext = os.path.splitext(filename or "")
    ext = (ext or ".png").lower()
    if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"):
        ext = ".png"
    base = slugify(stem) or "image"
    name = f"{base}{ext}"
    n = 1
    while os.path.exists(os.path.join(MEDIA_DIR, name)):
        n += 1
        name = f"{base}-{n}{ext}"
    with open(os.path.join(MEDIA_DIR, name), "wb") as fh:
        fh.write(data)
    return name


def media_path(name):
    """Absolute path for a stored media file, or None if the name is
    unsafe or missing."""
    # ".." passes the character whitelist (dots are allowed) but names the
    # parent directory - reject anything that isn't a plain filename, and
    # only ever answer with a real file, never a directory.
    if not name or not _SAFE_MEDIA.match(name) or name.strip(".") == "":
        return None
    path = os.path.join(MEDIA_DIR, name)
    return path if os.path.isfile(path) else None


def list_media():
    if not os.path.isdir(MEDIA_DIR):
        return []
    return sorted(
        f for f in os.listdir(MEDIA_DIR)
        if _SAFE_MEDIA.match(f) and os.path.isfile(os.path.join(MEDIA_DIR, f))
    )
