// 小小守護員 PWA service worker——只讓 app 裝得起來、斷線時至少有殼。
//
// ⚠️ 鐵則：/api/ 底下的一切永遠走網路、絕不快取。
//
// 做法照 MaiCoin 那支，但理由比交易 app 更硬：這裡的 API 回的是 1,213 所真實
// 機構的建議查核名單、未查證的民眾通報、以及登入後才看得到的卷宗。快取進手機
// 的後果不是「看到舊行情」，是手機遺失時裝置上躺著一份稽查名單。所以殼
// （HTML／CSS／JS／圖示）可以快取，資料一律穿透，而且這條規則寫在 fetch
// 處理器的最前面，不讓任何「順便快取一下比較快」的修改有機會繞過它。
//
// 其他三條：
// - 非 GET 一律穿透（登入、擬稿、答詢都是 POST）。
// - 跨源一律不插手：OSM／Google 圖磚與 Google Fonts 有它們自己的快取策略，
//   塞進我們的快取只會讓離線時多一堆過期圖磚、線上時多一份重複儲存。
// - 網路優先：線上時永遠拿新檔（index 路由已替每個 /static 檔加版本號），
//   只有斷線才回快取。開發中改了 JS 不會被這支 SW 卡在舊版。
//
// SW 的管轄範圍是它自己所在的路徑，所以這支由 server.py 的 `/sw.js` 路由從根
// 目錄送出——放在 /static/sw.js 的話它只管得到 /static/，裝得起來卻管不到首頁。
//
// ⚠️ 這段說明刻意用行註解而不是區塊註解。第一版寫成區塊註解，裡面有一串
// 「粗體的 /api/ 加星號」，其中的「星號＋斜線」提早結束了註解，後面的 api
// 被當成一行程式：語法合法（node --check 照過），一執行就 ReferenceError。
// 瀏覽器只回一句「ServiceWorker script evaluation failed」，PWA 安靜地註冊
// 不上。tests/test_pwa.py 現在會在 node 沙箱裡真的執行這支檔案。
const CACHE = "sw-shell-v1";
const SHELL = [
  "/",
  "/manifest.webmanifest",
  "/static/style.css",
  "/static/lobby.css",
  "/static/vendor/leaflet.css",
  "/static/vendor/leaflet.js",
  "/static/vendor/MarkerCluster.css",
  "/static/vendor/MarkerCluster.Default.css",
  "/static/vendor/leaflet.markercluster.js",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];
/* 永不快取的路徑前綴。/mcp 是給外部代理用的協定端點，性質同 API。 */
const NEVER = ["/api/", "/mcp"];

self.addEventListener("install", (e) => {
  // addAll 遇到任一資源失敗會整批失敗；逐一放進去，缺一個檔不該讓整個 app 裝不起來。
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => Promise.all(SHELL.map((u) => c.add(u).catch(() => undefined))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (NEVER.some((p) => url.pathname.startsWith(p))) return;   // ★ 資料永不快取

  e.respondWith(
    fetch(req)
      .then((res) => {
        if (res && res.ok && res.type === "basic") {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() =>
        // 首頁送出的資源網址帶 ?v=<mtime>，快取裡是預載時的無版本網址——
        // 比對時忽略查詢字串，否則斷線時每個檔都會找不到。
        caches.match(req, { ignoreSearch: true }).then((hit) => {
          if (hit) return hit;
          if (req.mode === "navigate") return caches.match("/");
          return Response.error();
        })),
  );
});
