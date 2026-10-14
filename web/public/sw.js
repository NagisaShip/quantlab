// 手机看板的最小 Service Worker：只缓存页面外壳，接口一律走网络（看板上的数字必须是实时的）。
const SHELL = 'quantlab-shell-v1'
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(['/', '/manifest.webmanifest', '/icon.svg'])).then(() => self.skipWaiting()))
})
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== SHELL).map((k) => caches.delete(k)))).then(() => self.clients.claim()))
})
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url)
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return
  // 页面与静态资源：先网络、断网时用缓存
  e.respondWith(
    fetch(e.request).then((r) => {
      if (r.ok && (url.pathname === '/' || url.pathname.startsWith('/assets/'))) {
        const copy = r.clone()
        caches.open(SHELL).then((c) => c.put(e.request, copy))
      }
      return r
    }).catch(() => caches.match(e.request).then((m) => m || caches.match('/')))
  )
})
