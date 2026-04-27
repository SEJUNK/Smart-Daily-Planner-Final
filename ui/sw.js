// Smart Daily Planner — Service Worker
const CACHE = 'sdp-v5';
const STATIC = [
  '/',
  'https://cdn.tailwindcss.com',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js',
  'https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css',
];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(STATIC).catch(() => {})));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

// Network-first for app shell/API/auth; cache-first for external static assets.
self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const isSameOrigin = url.origin === self.location.origin;
  const path = url.pathname || '';
  const isDynamic =
    path.startsWith('/tasks') ||
    path.startsWith('/events') ||
    path.startsWith('/notes') ||
    path.startsWith('/query') ||
    path.startsWith('/analytics') ||
    path.startsWith('/auth') ||
    path.startsWith('/settings') ||
    path.startsWith('/mcp') ||
    path.startsWith('/risk-analysis') ||
    path.startsWith('/focus-plan') ||
    path.startsWith('/insights');

  if (e.request.mode === 'navigate' || url.pathname === '/' || url.pathname.endsWith('/index.html')) {
    e.respondWith(
      fetch(e.request)
        .then(res => {
          const clone = res.clone();
          caches.open(CACHE).then(c => c.put(e.request, clone));
          return res;
        })
        .catch(() => caches.match(e.request).then(r => r || caches.match('/')))
    );
    return;
  }

  if (isSameOrigin && isDynamic) {
    // Dynamic app endpoints: always network-first and never cached.
    e.respondWith(fetch(e.request).catch(() =>
      new Response(JSON.stringify({ error: 'Offline — reconnect to sync' }), {
        headers: { 'Content-Type': 'application/json' }
      })
    ));
    return;
  }
  // Static assets (mostly CDN): cache first
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).then(res => {
      const clone = res.clone();
      caches.open(CACHE).then(c => c.put(e.request, clone));
      return res;
    }))
  );
});

// Push notification handler
self.addEventListener('push', e => {
  const data = e.data?.json() || { title: 'Smart Daily Planner', body: 'You have an update!' };
  e.waitUntil(self.registration.showNotification(data.title, {
    body: data.body,
    icon: '/ui/icon-192.png',
    badge: '/ui/icon-192.png',
    tag: data.tag || 'sdp-notif',
    data: { url: data.url || '/' },
  }));
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  e.waitUntil(clients.openWindow(e.notification.data?.url || '/'));
});
