const CACHE_NAME = "digiform-pwa-v2";
const OFFLINE_URLS = [
  "/",
  "/manifest.json",
  "/static/icons/icon-192.png?v=digiform-2",
  "/static/icons/icon-512.png?v=digiform-2",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(OFFLINE_URLS)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.map((key) => {
          if (key !== CACHE_NAME) {
            return caches.delete(key);
          }
          return Promise.resolve();
        })
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;

  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        return cachedResponse;
      }

      return fetch(event.request)
        .then((networkResponse) => {
          const cloned = networkResponse.clone();
          if (
            networkResponse.ok &&
            (
              event.request.url.includes("/static/") ||
              event.request.headers.get("accept")?.includes("text/html")
            )
          ) {
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, cloned));
          }
          return networkResponse;
        })
        .catch(() => {
          if (event.request.headers.get("accept")?.includes("text/html")) {
            return caches.match("/");
          }
          return new Response("Offline", {
            status: 503,
            statusText: "Offline",
            headers: { "Content-Type": "text/plain" },
          });
        });
    })
  );
});
