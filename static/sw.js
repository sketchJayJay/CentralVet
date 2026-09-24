const CACHE_NAME = 'centralvet-pos58-impressao-exata-v1';
const STATIC_ASSETS = [
  '/static/style.css',
  '/static/centralvet_logo.png',
  '/static/icon-192.png',
  '/static/icon-512.png',
  '/manifest.json'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(STATIC_ASSETS).catch(() => null))
  );
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE_NAME).map(key => caches.delete(key))))
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== location.origin) return;
  if (url.pathname.startsWith('/static/') || url.pathname === '/manifest.json') {
    event.respondWith(caches.match(req).then(cached => cached || fetch(req)));
    return;
  }
  event.respondWith(fetch(req).catch(() => caches.match(req)));
});
