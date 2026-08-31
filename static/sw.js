/*
 * sw.js - StocksDeepDive service worker.
 *
 * Deliberately minimal (Part 2 of the PWA brief). This site's whole value
 * is live data - a cached stock page is a wrong stock page - so this SW
 * does the least a service worker can do and still make the site
 * installable:
 *
 *   - precache a handful of static assets (icons, manifest, this file's
 *     own offline fallback page)
 *   - network-first for every request; only on a failed NAVIGATION does
 *     it fall back to the offline page
 *   - cache-first ONLY for the precached static assets themselves
 *   - never touch Streamlit's own traffic, the admin flow, or anything
 *     that isn't a plain GET
 *
 * No background sync, no runtime HTML caching. A stale-shell bug here
 * would look exactly like the site being broken, which is worse than no
 * service worker at all.
 */

// Bump this on any change to the file lists/strategy below - activate()
// deletes every cache that doesn't match, so a version bump is how an old
// visitor's cached assets ever get cleared out.
const CACHE_VERSION = "sdd-v1";
const PRECACHE = [
  "/manifest.webmanifest",
  "/pwa/icons/icon-192.png",
  "/pwa/icons/icon-512.png",
  "/pwa/icons/icon-512-maskable.png",
  "/pwa/icons/apple-touch-icon.png",
  "/pwa/icons/favicon-32.png",
  "/pwa/offline.html",
];

// Paths this SW must never intercept, at all - straight to the network,
// no cache read, no cache write, no offline fallback substitution.
//   /_stcore/  - Streamlit's websocket + XHR lifeline; touching this at
//                all (even a network-first passthrough that still awaits
//                the SW) risks adding latency/failure modes to the one
//                connection the whole app depends on.
//   ?admin=    - the admin full-view unlock; must always hit the network.
//   /blog-admin - the blog CMS.
function _bypasses(url) {
  if (url.pathname.startsWith("/_stcore/")) return true;
  if (url.pathname.startsWith("/blog-admin")) return true;
  if (url.searchParams.has("admin")) return true;
  return false;
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(PRECACHE))
    // Take over immediately on first install rather than waiting for
    // every open tab to close - there is no old cached state yet to
    // conflict with.
    .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k))
      ))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;

  // Explicit bypass (brief's own wording): anything that isn't a GET goes
  // straight through untouched - a POST cached or replayed by a SW is a
  // correctness bug (comment submits, auth cookie endpoints, push
  // subscribe/unsubscribe), not a performance win.
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // never touch cross-origin requests
  if (_bypasses(url)) return;

  // Cache-first ONLY for the exact precached static assets - anything not
  // in that list (every ticker page, every API call, every blog post)
  // is network-first, full stop.
  if (PRECACHE.includes(url.pathname)) {
    event.respondWith(
      caches.match(req).then((cached) => cached || fetch(req))
    );
    return;
  }

  event.respondWith(
    fetch(req).catch(() => {
      // Only a failed NAVIGATION gets the offline card - a failed request
      // for a script/image/data call should just fail normally, not be
      // silently swapped for an unrelated HTML page.
      if (req.mode === "navigate") {
        return caches.match("/pwa/offline.html");
      }
      return Response.error();
    })
  );
});

// ---- Web Push (Part 3) ----------------------------------------------
// push_send.py's payload is always `{title, body, url}` JSON - see its
// module docstring. Falls back to plain generic text if a push ever
// arrives with no data (shouldn't happen from this site, but a malformed
// or empty push must never throw and crash the handler).
self.addEventListener("push", (event) => {
  let payload = { title: "StocksDeepDive", body: "New update available.", url: "/" };
  if (event.data) {
    try {
      payload = { ...payload, ...event.data.json() };
    } catch (e) {
      // Not JSON - fall back to the default copy above rather than fail.
    }
  }
  event.waitUntil(
    self.registration.showNotification(payload.title, {
      body: payload.body,
      icon: "/pwa/icons/icon-192.png",
      badge: "/pwa/icons/icon-192.png",
      data: { url: payload.url || "/" },
    })
  );
});

// Focus an already-open tab on this origin if there is one, otherwise
// open a new one - standard "notification click" pattern.
self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const targetUrl = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
      for (const client of list) {
        if (client.url && new URL(client.url).origin === self.location.origin) {
          client.focus();
          if ("navigate" in client) client.navigate(targetUrl);
          return;
        }
      }
      return clients.openWindow(targetUrl);
    })
  );
});
