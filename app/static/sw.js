/* Offline app shell. Network first (so updates arrive), cache fallback (so the
   app and the last saved plan still open with no signal). API calls are never
   cached: stock data must not be served stale by accident. */
const CACHE = 'supplyline-shell-v5';
const CORE = [
  '/', '/static/styles.css?v=5', '/static/app.js?v=5',
  '/static/vendor/leaflet.js', '/static/vendor/leaflet.css',
];
const OPTIONAL = [
  '/manifest.webmanifest', '/static/icon.svg',
  '/static/fonts/overpass-latin-wght-normal.woff2',
  '/static/fonts/atkinson-hyperlegible-latin-400-normal.woff2',
  '/static/fonts/atkinson-hyperlegible-latin-700-normal.woff2',
];

self.addEventListener('install', (e) => {
  // Replace the offline app only when its HTML, scripts and styles all load.
  // An unavailable font or icon must not prevent installation.
  e.waitUntil(
    caches.open(CACHE)
      .then(async (c) => {
        await c.addAll(CORE.map((u) => new Request(u, { cache: 'reload' })));
        await Promise.all(OPTIONAL.map((u) => c.add(new Request(u, { cache: 'reload' })).catch(() => {})));
      })
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k.startsWith('supplyline-shell-') && k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== self.location.origin || url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request, { cache: 'reload' })
      .then(async (res) => {
        if (!res.ok && e.request.mode === 'navigate') throw new Error('Page unavailable');
        if (res.ok) {
          const copy = res.clone();
          try { await (await caches.open(CACHE)).put(e.request, copy); } catch (err) { /* quota exceeded */ }
        }
        return res;
      })
      .catch(async () => {
        const c = await caches.open(CACHE);
        return await c.match(e.request) || (e.request.mode === 'navigate' ? await c.match('/') : null) || Response.error();
      })
  );
});
