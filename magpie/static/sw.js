const CACHE_NAME = "magpie-shell-v3";

// Versioned URLs must match the <script>/<link> tags in index.html,
// otherwise pre-cached copies drift into "two versions of the UI".
const SHELL_ASSETS = [
  "./",
  "index.html",
  "styles.css?v=4",
  "app.js?v=4",
  "manifest.webmanifest?v=2",
  "icon.svg?v=2",
];

// HTML / JS / CSS must always come from the network first so UI fixes land
// without a hard reload; the cache is only the offline fallback.
const NETWORK_FIRST = (url) =>
  url.search.length > 0 ||
  url.pathname.endsWith("/") ||
  url.pathname.endsWith(".html") ||
  url.pathname.endsWith(".js") ||
  url.pathname.endsWith(".css") ||
  url.pathname.endsWith(".webmanifest");

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        Promise.allSettled(SHELL_ASSETS.map((asset) => cache.add(asset)))
      )
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Never intercept the API — data is live and private.
  if (url.pathname.startsWith("/api/")) return;

  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          const copy = response.clone();
          caches
            .open(CACHE_NAME)
            .then((cache) => cache.put("index.html", copy));
          return response;
        })
        .catch(() => caches.match("index.html"))
    );
    return;
  }

  if (NETWORK_FIRST(url)) {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() =>
          caches
            .match(request)
            .then(
              (cached) =>
                cached ||
                caches
                  .match(url.pathname) // unversioned copy from an older shell
                  .then((old) => old || new Response("", { status: 504 }))
            )
        )
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request)
        .then((response) => {
          if (response.ok && response.type === "basic") {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => cached);
    })
  );
});
