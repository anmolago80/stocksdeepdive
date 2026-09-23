# Working rules for this repo

This is the **live production repo** for stocksdeepdive.com (FastAPI
`server.py` reverse-proxying/serving in front of a Streamlit `app.py`
subprocess, on Railway). Pushes to `main` deploy straight to production.
There is no staging environment and no CI gate in front of that deploy.

## Definition of done

A task is done when its change is **pushed to `main`** and the **remote
head has been confirmed to have moved** (`git fetch origin main && git
rev-parse origin/main` matches the new commit). Passing local checks or
having a local commit is not done - verify the push actually landed
before reporting completion.

## Deploy-straight-to-production care

- Work one task at a time. Verify locally before pushing. Never leave
  `main` in a half-done state.
- Prefer additive changes (new routes/functions/files) over editing
  working code, especially on read paths real visitors hit right now.
- Before editing any file, read it fresh in that session - don't assume
  its contents from earlier context, since another task may have
  changed it.
- Reuse existing public-view / store functions instead of writing new
  queries against raw tables or duplicating a data shape a renderer
  already computes (e.g. `snapshot_store.public_view()`,
  `blog_store.list_posts()`/`get_post()` default to published-only,
  `research_snapshot_render`'s section/gating helpers). If a function is
  private-by-convention (leading underscore) but the exact logic you
  need already lives there, this codebase's own precedent (e.g.
  `server.py` calling `api_v1._resolve_universe()`) is to import and
  reuse it rather than re-derive the logic in a second place.
- One commit per task with a detailed message describing what changed
  and why, plus how it was verified. Push after verifying, not before.

## Streamlit traps to respect

- `unsafe_allow_html=True` **strips `onclick`** - Streamlit sanitizes it
  out of any HTML string passed to `st.markdown`/`st.write`. Never rely
  on `onclick` for interactivity; use a real `st.button`, a plain
  `<a href>` navigation, or a query-param/session-state driven flow
  instead.
- HTML inside `st.markdown(..., unsafe_allow_html=True)` that is
  **indented 4+ spaces renders as a literal code block** (Markdown's own
  indented-code-block rule fires before the HTML is parsed). Keep HTML
  strings flush-left / unindented in the Python source, even when that
  looks inconsistent with surrounding code style.
- Paired `$...$` in a Markdown context triggers **KaTeX** math
  rendering, which mangles a literal dollar amount like `$1,234`. Use
  `_fmt_aud_md()` (the escaping helper) **only** in plain-Markdown
  contexts; use `_fmt_aud()` in raw-HTML contexts, where KaTeX doesn't
  apply and escaping isn't needed.

## Never touch without an explicit task

- `NIGHTLY_UNIVERSES` and other scheduler config (`scheduler_engine.py`'s
  cadence/config) - only change these when a task explicitly says so.
  A silent change here changes what gets scanned overnight, site-wide.

## Architecture notes (context, not rules)

- `server.py` is a FastAPI app that serves real, crawlable server-rendered
  HTML for specific routes (blog, `/s/<ticker>` snapshots,
  `/methodology`/`/about`/`/privacy`, `/api/v1/*`, etc.) and reverse-proxies
  everything else to the Streamlit `app.py` subprocess it launches - see
  `server.py`'s own module docstring for the full route map.
- `/api/v1/*` is a separate FastAPI sub-app (`api_v1.py`, mounted at
  `/api/v1`) with its own CORS policy, rate limiting, and response
  envelope (`data` + `attribution` + `disclaimer` + `as_of` + optional
  `link`). New public read-only endpoints should follow that same
  envelope and rate-limit convention.
- EN/ES: server-rendered pages use `/es/<path>` URL-prefix routing with
  an explicit `lang` param threaded through render functions; the
  Streamlit app uses `?lang=es` + `i18n.py`'s `t()` dict lookup. These
  are two different, deliberately separate mechanisms - see
  `blog_render._lang_toggle_links()`'s docstring for the exact rules.
