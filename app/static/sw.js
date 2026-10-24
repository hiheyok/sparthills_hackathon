/* Offline app shell. Network first (so updates arrive), cache fallback (so the
   app and the last saved plan still open with no signal). API calls are never
   cached: stock data must not be served stale by accident. */
const CACHE = 'supplyline-shell-v4';
const SHELL = [
  '/', '/manifest.webmanifest', '/static/styles.css', '/static/app.js', '/static/icon.svg',
  '/static/vendor/leaflet.js', '/static/vendor/leaflet.css',
  '/static/fonts/overpass-latin-wght-normal.woff2',
  '/static/fonts/atkinson-hyperlegible-latin-400-normal.woff2',
  '/static/fonts/atkinson-hyperlegible-latin-700-normal.woff2',
];

self.addEventListener('install', (e) => {
  // Cache each shell asset on its own. Cache.addAll is atomic: one missing or
  // non-2xx asset would reject the whole install and leave the app with no
  // offline shell. Caching individually keeps whatever did load.
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => {}))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
        return res;
      })
      .catch(() => caches.match(e.request).then((r) => r || caches.match('/')))
  );
});
