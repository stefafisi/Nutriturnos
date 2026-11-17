const CACHE_NAME = 'nutriturnos-static-v2';
const OFFLINE_URL = '/static/offline.html';

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll([
    OFFLINE_URL,
    '/static/manifest.webmanifest',
    '/static/icon.svg'
  ])));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(
      keys.filter(key => key.startsWith('nutriturnos-static-') && key !== CACHE_NAME)
        .map(key => caches.delete(key))
    ))
  );
  self.clients.claim();
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (url.pathname.startsWith('/admin') || url.pathname.includes('/api/')
      || url.pathname.startsWith('/turno/')) return;
  if (request.mode !== 'navigate') return;

  event.respondWith(fetch(request).catch(async () =>
    (await caches.match(OFFLINE_URL)) || Response.error()
  ));
});
