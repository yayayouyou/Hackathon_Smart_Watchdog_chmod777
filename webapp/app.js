/* 稽查派工台 — 動態版
 *
 * 與靜態版讀的是同一份 payload 契約，差別只在來源：靜態版把它烤進 HTML
 * （Artifact 的 CSP 擋掉 fetch 與非白名單腳本，所以地圖只能自己畫 SVG），
 * 這裡則從 /api/ 取得，因此可以用真正的圖磚地圖、滾輪縮放與標記群集。
 *
 * 底圖預設 OpenStreetMap：免金鑰、免費、可商用（需標示），今天就能跑。
 * Google 底圖需金鑰，設定後才會啟用——不預先假設有金鑰，也不因為沒有金鑰
 * 就讓地圖不能用。
 */
const TYPE = ["公立", "非營利", "私立"];
const TVAR = ["--pub", "--np", "--prv"];
const NTPC = [25.02, 121.55];

const $ = (id) => document.getElementById(id);
const nf = (n) => (n == null || n === "" ? "—" : Number(n).toLocaleString("en-US"));
/* 引號一定要轉義：新聞標題、Threads 貼文、Google 評論都是第三方內容，
   而它們會進到 href="…" 與 value="…"。只轉 & 與 < 擋不住跳出屬性。 */
const esc = (s) => String(s ?? "")
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const cssv = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

const state = {
  payload: null, points: [], byId: {}, proposal: [], selected: null,
  cap: 20, cluster: true, flaggedOnly: false, types: new Set([0, 1, 2]),
  map: null, layer: null, districtLayer: null, base: null, googleKey: null,
  // agent 這一輪點名的機構 id（Set）。非 null 時地圖只顯示這幾筆，
  // 讓「我把這幾筆標在地圖上了」這句話對得上畫面。清除就設回 null。
  agentIds: null,
  // 標記著色依據："type"＝設立別（中性事實），"penalty"＝歷史裁罰件數（公開事實）。
  // 兩者都不是我們算出來的分數——分數不上地圖，見 aws-architecture.md §6.5。
  pinBy: "type", dnames: true, dnameLayer: null,
  // 新北以外反灰。ntpcRings 是從區界算出來的市界外框，算一次就快取。
  mask: true, maskLayer: null, outlineLayer: null, ntpcRings: null, land: [],
  choro: true, choroLayer: null,
  // 時間軸模式（timeline.js 設定）。非 null 時地圖改畫「當時的排序」與
  // 「後來實際受罰」，而不是今天的派工提案。
  timeline: null,
};

/* ── 資料 ─────────────────────────────────────────────── */
async function api(path, opts) {
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => null);
  if (!r.ok) {
    // 錯誤內容要留住：掃描端點靠 detail 說明被哪一道預算上限擋下，
    // 只丟狀態碼會讓「今日額度用完」變成看不懂的 HTTP 429。
    const err = new Error(`${path} → HTTP ${r.status}`);
    err.status = r.status;
    err.detail = body && body.detail !== undefined ? body.detail : body;
    throw err;
  }
  return body;
}

function post(path, payload) {
  return api(path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

async function boot() {
  const cfg = await api("/api/config").catch(() => ({}));
  state.googleKey = cfg.google_maps_key || null;
  state.payload = await api("/api/payload");
  // 鄰縣市陸地輪廓。缺了不是致命傷：applyMask 會退回舊的「蓋掉整個世界」版本。
  state.land = (await api("/api/land").catch(() => ({}))).land || [];
  state.points = state.payload.points || [];
  state.points.forEach((p) => { state.byId[p.i] = p; });

  $("s-all").textContent = state.points.length;
  $("s-fin").textContent = state.points.filter((p) => p.fin).length;
  [0, 1, 2].forEach((t) => {
    $("l" + t).textContent = state.points.filter((p) => p.t === t).length;
  });
  const rt = state.payload.realtime || {};
  $("s-ch").textContent = `${rt.channels_live || 0}/${rt.channels_total || 0}`;
  // 花費常駐在表頭。只在掃描頁看得到餘額，等於要花錢的人不一定看得到自己花了多少。
  api("/api/scan/budget").then((b) => {
    $("s-budget").textContent =
      `US$${b.month_spent_usd.toFixed(2)}/${b.caps.month.toFixed(2)}`;
  }).catch(() => { $("s-budget").textContent = "—"; });

  initMap();
  syncLayerCount();
  await refresh();

  // 交棒給中庭。app 預設 hidden，走進某一室才顯示——
  // 先畫地圖再顯示中庭，是因為 Leaflet 在 display:none 裡量不到容器尺寸，
  // 必須先在可見狀態初始化過一次，之後 invalidateSize() 才有東西可以量。
  if (window.Lobby) {
    window.Lobby.start(state.payload);
    $("lb-all").textContent = state.points.length.toLocaleString("en-US");
    $("lb-sel").textContent = state.proposal.length;
    const rt2 = state.payload.realtime || {};
    $("lb-ch").textContent = `${rt2.channels_live || 0}/${rt2.channels_total || 0}`;
  }
}

/* ── 地圖 ─────────────────────────────────────────────── */
/* 底圖收斂成兩家：OpenStreetMap（免金鑰、隨時可用）與 Google（要金鑰）。
   少一家就少一條會在會場斷掉的外部相依，而底圖美術不是這個系統的賣點。 */
const BASES = {
  osm: {
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    attr: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  },
};

function initMap() {
  state.map = L.map("map", {
    center: NTPC, zoom: 11, zoomControl: true,
    scrollWheelZoom: true, preferCanvas: true,
  });
  // 反灰層自己一個 pane：疊在圖磚（200）之上、行政區界線與標記（400／600）
  // 之下——蓋掉的是底圖，不是我們畫上去的東西。
  state.map.createPane("swmask");
  Object.assign(state.map.getPane("swmask").style,
    { zIndex: 350, pointerEvents: "none" });
  // 行政區底色在遮罩之下、圖磚之上。它只畫在新北境內，與遮罩不重疊，
  // 但排在下面才不會蓋掉市界那條線。
  state.map.createPane("swchoro");
  state.map.getPane("swchoro").style.zIndex = 340;
  // 行政區名稱排在標記（600）之下：名字是註記，不該蓋住可以點開卷宗的園。
  state.map.createPane("swdname");
  Object.assign(state.map.getPane("swdname").style,
    { zIndex: 450, pointerEvents: "none" });
  setBase("osm");
  state.layer = L.layerGroup().addTo(state.map);
  fitNTPC();
  refitOnLayout();
  applyMask(state.mask);
  drawChoro();
  drawChoroLegend();
  drawPenaltyLegend();
  drawDistrictNames();
  // 放大到街廓尺度時底色要退場，不然它只是蓋住地圖。
  state.map.on("zoomend", fadeChoro);
  // 行政區名字要跟著縮放換字級，否則全市尺度擠成一團、街廓尺度小到看不見。
  state.map.on("zoomend", drawDistrictNames);

  document.querySelectorAll("#basemaps button").forEach((b) => {
    b.addEventListener("click", async () => {
      if (b.disabled) return;
      document.querySelectorAll("#basemaps button")
        .forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      try {
        await setBase(b.dataset.b);
      } catch (e) {
        $("gnote").textContent = `Google 底圖載入失敗：${e.message}`;
        document.querySelector('#basemaps button[data-b="osm"]').click();
      }
    });
  });
  // Google 底圖需金鑰；沒有就明白停用，不要讓人點了沒反應。
  if (!state.googleKey) {
    $("b-google").disabled = true;
    $("gnote").textContent =
      "Google 底圖需 Maps JavaScript API 金鑰。未設定，目前以 OpenStreetMap 呈現，" +
      "功能（滾輪縮放、拖曳、標記群集）完全相同。";
  } else {
    $("gnote").textContent =
      "Google 底圖已可用。金鑰會出現在瀏覽器端（Maps JS API 本來就如此），" +
      "建議在 Console 設 HTTP referrer 限制。";
  }
}

/* Google 圖層走官方 Maps JavaScript API，再由 GoogleMutant 轉成 Leaflet 圖層。
   兩者都在第一次切換時才載入——沒點過 Google 底圖的人不必付出這段下載。 */
function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`)) return resolve();
    const el = document.createElement("script");
    el.src = src;
    el.async = true;
    el.onload = resolve;
    el.onerror = () => reject(new Error(`載入失敗：${src}`));
    document.head.appendChild(el);
  });
}

async function ensureGoogle() {
  if (window.L && L.gridLayer && L.gridLayer.googleMutant && window.google?.maps) {
    return;
  }
  await loadScript(
    `https://maps.googleapis.com/maps/api/js?key=${encodeURIComponent(state.googleKey)}`
    + "&language=zh-TW&region=TW&loading=async");
  await loadScript(
    "https://unpkg.com/leaflet.gridlayer.googlemutant@0.14.1/dist/Leaflet.GoogleMutant.js");
}

async function setBase(kind) {
  if (kind === "google") {
    await ensureGoogle();
    if (state.base) state.map.removeLayer(state.base);
    state.base = L.gridLayer.googleMutant({ type: "roadmap", maxZoom: 21 })
      .addTo(state.map);
    return;
  }
  if (state.base) state.map.removeLayer(state.base);
  const cfg = BASES[kind] || BASES.osm;
  state.base = L.tileLayer(cfg.url, { attribution: cfg.attr, maxZoom: 19 })
    .addTo(state.map);
  // 圖磚是唯一無法就地保存的外部相依（數百 MB）。會場擋掉圖磚時，
  // 標記會浮在一片灰色虛空上，地理脈絡整個消失——所以偵測到載不到就
  // 自動打開行政區界線（那份資料已經在 payload 裡）。
  state.base.on("tileerror", onTileError);
}

let tileErrors = 0;
function onTileError() {
  tileErrors += 1;
  if (tileErrors !== 3) return;          // 偶發失敗不算，連續三次才判定
  const box = $("f-districts");
  if (box && !box.checked) {
    box.checked = true;
    toggleDistricts(true);
  }
  const note = $("gnote");
  if (note) {
    note.textContent = "圖磚載不到（網路可能擋住 tile 伺服器）。"
      + "已自動改以行政區界線提供地理脈絡；標記與所有分析功能不受影響。";
    note.style.color = "var(--warn)";
  }
}

/* ── 只顯示新北 ───────────────────────────────────────
 *
 * 界線資料是 29 個行政區各自的多邊形，裡面沒有「新北市外框」這一條。外框可
 * 以從區界推出來：相鄰兩區共用的那條邊在資料裡會出現兩次，**只出現一次的邊
 * 就是市界**。把這些邊接回封閉環，就同時得到外框與台北市那個內孔（台北市被
 * 新北市整個包住，它不是新北，一樣要反灰）。
 *
 * 接完會多出 93 個面積約 1e-6 度² 的碎環——相鄰區界各自簡化後沒對齊留下的縫。
 * 外框 0.209、台北市孔 0.024，與碎環差五個數量級，取最大環的 1% 當門檻就切
 * 得乾淨，不必在前端做多邊形聯集。
 */
function ntpcRings() {
  if (state.ntpcRings) return state.ntpcRings;
  const k = ([x, y]) => x + "," + y;
  const edges = new Map();
  ((state.payload && state.payload.boundary) || []).forEach((f) =>
    (f.poly || []).forEach((ring) => {
      const pts = ring.slice();
      if (pts.length > 1 && k(pts[0]) === k(pts[pts.length - 1])) pts.pop();
      for (let i = 0; i < pts.length; i += 1) {
        const a = pts[i], b = pts[(i + 1) % pts.length];
        const id = k(a) < k(b) ? k(a) + "|" + k(b) : k(b) + "|" + k(a);
        const e = edges.get(id);
        if (e) e.n += 1; else edges.set(id, { n: 1, a, b });
      }
    }));

  const adj = new Map();
  const link = (p, q) => {
    if (!adj.has(k(p))) adj.set(k(p), []);
    adj.get(k(p)).push(q);
  };
  edges.forEach((e) => { if (e.n === 1) { link(e.a, e.b); link(e.b, e.a); } });

  const used = new Set();
  const eid = (p, q) =>
    (k(p) < k(q) ? k(p) + "|" + k(q) : k(q) + "|" + k(p));
  const rings = [];
  adj.forEach((outs, startKey) => outs.forEach((first) => {
    if (used.has(eid(adj0(startKey), first))) return;
    const start = adj0(startKey);
    const ring = [start];
    let cur = start, nxt = first;
    used.add(eid(cur, nxt));
    while (k(nxt) !== k(start)) {
      ring.push(nxt);
      const step = (adj.get(k(nxt)) || []).find((v) => !used.has(eid(nxt, v)));
      if (!step) return;                 // 接不回起點就丟掉（資料破洞時的保險）
      used.add(eid(nxt, step));
      cur = nxt; nxt = step;
    }
    rings.push(ring);
  }));

  const area = (r) => Math.abs(r.reduce((sum, [x1, y1], i) => {
    const [x2, y2] = r[(i + 1) % r.length];
    return sum + x1 * y2 - x2 * y1;
  }, 0) / 2);
  const areas = rings.map(area);
  const max = areas.length ? Math.max(...areas) : 0;
  // payload 的座標是 [lng, lat]，Leaflet 吃 [lat, lng]。
  state.ntpcRings = rings
    .filter((_, i) => areas[i] >= max * 0.01)
    .map((r) => r.map(([x, y]) => [y, x]));
  return state.ntpcRings;
}

/* adj 的 key 是字串，走訪時要拿回原座標；第一條出邊的起點就是它。 */
function adj0(key) {
  return key.split(",").map(Number);
}

/* 全世界的框。緯度用 ±85 而非 ±90——Web Mercator 在極點會投影到無限遠。
   只在沒有陸地輪廓時當退路用。 */
const WORLD = [[-85, -180], [-85, 180], [85, 180], [85, -180]];

/* 反灰：只灰陸地，不灰海。
 *
 * 第一版是「整個世界當外框、新北當洞」的 even-odd 多邊形，一次蓋掉除了新北
 * 以外的所有東西——包含海。海變成一片死灰之後，「新北是個沿海城市」在畫面上
 * 就消失了：淡水河口、北海岸、東北角全部沒入背景。
 *
 * 現在改成把**鄰縣市的陸地面**畫成灰色（`GET /api/land`，來源同新北區界那份
 * 內政部界線），海就留在底圖原本的顏色。台北市被新北整個包住，它也是鄰縣市，
 * 一樣反灰——那反而讓新北的甜甜圈形狀更清楚。
 */
function applyMask(on) {
  [state.maskLayer, state.outlineLayer].forEach((l) => {
    if (l) state.map.removeLayer(l);
  });
  state.maskLayer = null;
  state.outlineLayer = null;
  if (!on) return;
  const rings = ntpcRings();
  if (!rings.length) return;
  const fill = {
    pane: "swmask", interactive: false, fillRule: "evenodd",
    fillColor: cssv("--mask"), fillOpacity: Number(cssv("--mask-op")) || 0.8,
  };
  const land = state.land || [];
  state.maskLayer = land.length
    // 每個縣市的每個環都是獨立的島，不是彼此的洞——所以要包成
    // [[環]] 的多重多邊形形式。直接給 [環, 環] 會讓第二個島變成第一個島的洞。
    ? L.layerGroup(land.map((c) => L.polygon(
      c.poly.map((r) => [r.map(([x, y]) => [y, x])]),
      { ...fill, color: cssv("--mask-line"), weight: 0.8, opacity: 0.5 },
    ))).addTo(state.map)
    // 沒有陸地輪廓（檔案還沒建）就退回舊版：海會一起灰掉，但畫面仍然只凸顯
    // 新北，不會變成完全沒有遮罩。
    : L.polygon([WORLD, ...rings], { ...fill, stroke: false }).addTo(state.map);

  // 市界要比縣市界重。這是整張圖唯一需要一眼認出的邊。
  state.outlineLayer = L.polygon(rings, {
    pane: "swmask", interactive: false, fill: false,
    color: cssv("--edge"), weight: 1.8, opacity: 0.95,
  }).addTo(state.map);
}

/* ── 行政區優先度底色 ─────────────────────────────────
 *
 * 出題端真正要問的不是「哪一家有問題」，是**人力先派到哪一區**。這一層的
 * 單位因此是：若依模型排序抽全市前 100 名，這一區會有幾家進榜。
 *
 * 為什麼固定用前 100 名、而不是跟著派工容量走：容量 20 家分散到 29 個區之後
 * 每區 0～2 家，看不出輪廓；而前 100 名正是回測講命中率的那個 N（2.29 倍於
 * 隨機抽查），底色與頁尾那句話才是同一個口徑。
 *
 * 為什麼放大要淡出：底色是用來決定「先去哪一區」的。到了街廓尺度，要回答的
 * 問題已經變成「這條街上是哪一家」，那時色塊只會蓋住地圖。
 *
 * ⚠️ 用詞界線：這是**建議查核的密度**，不是危險程度。圖例與 tooltip 都不准
 * 出現「高風險區」這種說法。
 */
const CHORO_TOP = 100;
const CHORO_STEPS = [
  { min: 9, hi: null, v: "--c4", label: "9 家以上" },
  { min: 6, hi: 8, v: "--c3", label: "6–8 家" },
  { min: 3, hi: 5, v: "--c2", label: "3–5 家" },
  { min: 1, hi: 2, v: "--c1", label: "1–2 家" },
];

function choroCounts() {
  const by = {};
  state.points.forEach((p) => {
    if (p.r <= CHORO_TOP) by[p.d] = (by[p.d] || 0) + 1;
  });
  return by;
}

/* z<=11 全滿、z>=14 完全消失，中間線性淡出。分界點挑在 11–14 之間：11 是
   看得到整個新北的尺度（底色要講話），14 已經看得到單一街廓（底色只會擋路）。 */
function choroOpacity() {
  const z = state.map.getZoom();
  return Math.max(0, Math.min(1, (14 - z) / 3)) * 0.66;
}

function drawChoro() {
  if (state.choroLayer) {
    state.map.removeLayer(state.choroLayer);
    state.choroLayer = null;
  }
  if (!state.choro) return;
  const counts = choroCounts();
  const total = {};
  state.points.forEach((p) => { total[p.d] = (total[p.d] || 0) + 1; });
  const layers = [];
  (state.payload.boundary || []).forEach((f) => {
    const n = counts[f.d] || 0;
    const step = CHORO_STEPS.find((x) => n >= x.min);
    if (!step) return;                       // 沒有人進榜的區不上色
    const all = total[f.d] || 0;
    // 實心填色，透明度交給整個 pane。用 fillOpacity 的話，相鄰行政區簡化後
    // 重疊的那一小條會疊出更深的顏色——畫面上會多出幾塊不存在的「更嚴重」。
    const poly = L.polygon(
      f.poly.map((r) => [r.map(([x, y]) => [y, x])]),
      { pane: "swchoro", stroke: false, fillColor: cssv(step.v), fillOpacity: 1 },
    );
    poly.bindTooltip(
      `${f.d}　前 ${CHORO_TOP} 名 ${n} 家 ／ 全區 ${all} 家`
      + (all ? `（${((n / all) * 100).toFixed(1)}%）` : ""),
      { sticky: true });
    layers.push(poly);
  });
  state.choroLayer = L.layerGroup(layers).addTo(state.map);
  fadeChoro();
}

function fadeChoro() {
  if (!state.choroLayer) return;
  const op = choroOpacity();
  const on = state.map.hasLayer(state.choroLayer);
  if (op === 0) {
    // 淡出之後整層移除：留著全透明的色塊會繼續吃掉滑鼠事件，游標壓上去跳出來
    // 的會是行政區提示，不是底下那一家園。
    if (on) state.map.removeLayer(state.choroLayer);
    return;
  }
  if (!on) state.choroLayer.addTo(state.map);
  state.map.getPane("swchoro").style.opacity = String(op);
}

function drawChoroLegend() {
  const counts = choroCounts();
  const vals = Object.values(counts);
  const rows = CHORO_STEPS.map((x) => {
    const k = vals.filter((v) => v >= x.min && (x.hi == null || v <= x.hi)).length;
    return `<div><i style="background:${cssv(x.v)}"></i>${x.label}<b>${k} 區</b></div>`;
  });
  const districts = (state.payload.boundary || []).length;
  rows.push(`<div><i style="background:transparent;border:1px dashed var(--ink-4)"></i>`
    + `未進榜<b>${Math.max(0, districts - vals.length)} 區</b></div>`);
  $("choro-legend").innerHTML = rows.join("");
}

/* 裁罰色階的圖例。每一級後面掛實際家數——沒有家數的圖例只是色票，讀的人
   無從判斷深色到底是 3 家還是 300 家。 */
function drawPenaltyLegend() {
  const box = $("pen-legend");
  if (!box) return;
  const pts = state.points || [];
  const rows = PEN_STEPS.map((s, i) => {
    const hi = i === 0 ? Infinity : PEN_STEPS[i - 1].min - 1;
    const k = pts.filter((p) => (p.np || 0) >= s.min && (p.np || 0) <= hi).length;
    return `<div><i style="background:${cssv(s.v)}"></i>${s.label}<b>${k} 家</b></div>`;
  });
  box.innerHTML = rows.join("");
}

/* 置中新北，並把可視範圍收在市界附近。這個系統只處理新北市，地圖漂到南投
   對使用者沒有意義，只會讓人以為資料掉了。 */
function fitNTPC() {
  const rings = ntpcRings();
  const bounds = rings.length
    ? L.latLngBounds(rings.flat())
    : L.latLngBounds(NTPC, NTPC).pad(0.5);
  // 時間軸面板浮在地圖下緣。不把它的高度算進留白，烏來、坪林那一帶就會被它
  // 蓋住——畫面看起來不是置中，而是「南邊不見了」。
  const box = state.map.getContainer().getBoundingClientRect();
  const bar = $("tlbar");
  const barBox = bar && bar.offsetHeight ? bar.getBoundingClientRect() : null;
  const bottom = barBox
    ? Math.min(box.height * 0.4, Math.max(14, box.bottom - barBox.top + 10))
    : 14;
  // animate:false——開場取景不需要動畫，動畫還會讓「一開就看到整個新北」
  // 這件事延後到動畫跑完才成立。
  state.map.fitBounds(bounds, {
    paddingTopLeft: [14, 14], paddingBottomRight: [14, bottom], animate: false,
  });
  // maxBounds 以「取好景之後的可視範圍」放寬，不能直接拿市界：市界比畫面窄
  // 時，Leaflet 會把中心強制拉回市界中心，剛好抵銷掉上面為面板讓出的留白，
  // 取景會被悄悄改掉（這個坑踩過一次，畫面看起來像是設定沒生效）。
  state.map.setMaxBounds(state.map.getBounds().pad(0.35));
  state.map.setMinZoom(Math.max(7, state.map.getZoom() - 1));
}

/* 時間軸面板要等 /api/timeline 回來才長到最終高度，上面算的留白那時就過時了。
   使用者還沒動過地圖的話重新取景一次；碰過之後就不再插手——會自己跳回去的
   地圖比沒對齊的留白更惱人。 */
function refitOnLayout() {
  const bar = $("tlbar");
  if (!bar || !window.ResizeObserver) return;
  let touched = false;
  const stop = () => { touched = true; };
  const el = state.map.getContainer();
  el.addEventListener("pointerdown", stop, { once: true });
  el.addEventListener("wheel", stop, { once: true, passive: true });
  let h = bar.offsetHeight;
  new ResizeObserver(() => {
    if (touched || bar.offsetHeight === h) return;
    h = bar.offsetHeight;
    fitNTPC();
  }).observe(bar);
}

/* ── 標記依裁罰件數著色 ───────────────────────────────
 *
 * 這一層畫的是**主管機關已經開罰的紀錄**，不是我們的模型輸出。全國教保資訊網
 * 依法公開這些紀錄，所以它可以上地圖，而個別機構的風險分數不行
 * （aws-architecture.md §6.5）。圖例與 tooltip 因此一律寫「歷史裁罰紀錄」，
 * 不寫「風險」——差別不是修辭，是這張圖能不能對外展示的界線。
 *
 * 分級不用等距。實際分布極度右偏：0 件 730 家、1 件 189、2–3 件 175、
 * 4–6 件 73、7 件以上 46，最多 23 件。用 np/max 做線性色階的話，95% 的園會
 * 擠在最淺的兩格裡，深色只剩個位數的離群值——地圖會變成「幾乎全白＋幾個黑點」，
 * 什麼也看不出來。下面這組界線是照實際分位切的。
 *
 * ⚠️ 件數會隨**園齡與規模**自然累積：開了 30 年、收 200 人的老牌私立園，
 * 件數天生比新設小園多。深色代表「累積紀錄多」，不代表「現在比較糟」。
 * 這句話要出現在 tooltip 裡，不能只寫在文件。
 */
const PEN_STEPS = [
  { min: 7, v: "--k4", label: "7 件以上" },
  { min: 4, v: "--k3", label: "4–6 件" },
  { min: 2, v: "--k2", label: "2–3 件" },
  { min: 1, v: "--k1", label: "1 件" },
  { min: 0, v: "--k0", label: "無紀錄" },
];

function penaltyVar(np) {
  return (PEN_STEPS.find((s) => (np || 0) >= s.min) || PEN_STEPS[PEN_STEPS.length - 1]).v;
}

function pinIcon(p, flagged) {
  // 10px 的點在 1,600px 寬的螢幕上只是雜訊，分不出三類顏色。放大到 12／17，
  // 白色外圈負責跟底圖分離，紅圈負責跳出來。
  const byPen = state.pinBy === "penalty";
  // 裁罰模式下「無紀錄」縮一級。全市 730 家（60%）是 0 件，同樣大小的話
  // 畫面會被基準線淹掉，有紀錄的那 483 家反而看不出來。縮小不是隱藏——
  // 點還在、還能點開卷宗，只是不搶視覺。
  const quiet = byPen && !flagged && !(p.np > 0);
  const size = flagged ? 17 : (quiet ? 8 : 12);
  const v = byPen ? penaltyVar(p.np) : TVAR[p.t];
  return L.divIcon({
    className: "",
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    html: `<div class="pin${flagged ? " flag" : ""}" style="width:${size}px;
      height:${size}px;background:${cssv(v)}"></div>`,
  });
}

/* 時間軸模式的標記。
 *
 * 三種狀態要一眼分得出來，因為整個展示的說服力就在這個對比上：
 *   命中  當時排進前 N 名，後來真的受罰        ← 實心 + 光暈
 *   落空  當時排進前 N 名，後來沒受罰          ← 空心圈
 *   未排入 當時沒排進前 N 名                   ← 淡點
 * `hit === null` 代表即時格，前瞻窗還沒開始，一律當「待觀察」，不畫成落空。 */
function timelinePin(p, entry, topN) {
  const inTop = entry && entry.rank <= topN;
  const hit = entry ? entry.hit : null;
  let cls = "tl-out";
  if (inTop) cls = hit === 1 ? "tl-hit" : hit === null ? "tl-live" : "tl-miss";
  const size = inTop ? 14 : 7;
  return L.divIcon({
    className: "",
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    html: `<div class="tlpin ${cls}" style="width:${size}px;height:${size}px"></div>`,
  });
}

function drawMarkers() {
  state.layer.clearLayers();
  const tl = state.timeline;
  const flagged = new Set(state.proposal.map((o) => o.i));
  let shown = state.points.filter((p) => state.types.has(p.t));
  if (state.flaggedOnly && !tl) shown = shown.filter((p) => flagged.has(p.i));
  if (state.agentIds && !tl) shown = shown.filter((p) => state.agentIds.has(p.i));

  // 時間軸模式不做群集：群集會把「命中／落空」的顏色對比吃掉，
  // 而那個對比正是這個畫面唯一要講的事。
  const group = state.cluster && !tl
    ? L.markerClusterGroup({ maxClusterRadius: 45, disableClusteringAtZoom: 15 })
    : L.layerGroup();

  shown.forEach((p) => {
    const entry = tl ? tl.ranks[p.i] : null;
    const m = L.marker([p.y, p.x], {
      icon: tl ? timelinePin(p, entry, tl.topN) : pinIcon(p, flagged.has(p.i)),
      zIndexOffset: tl && entry && entry.rank <= tl.topN ? 1000 : 0,
    });
    const tip = tl && entry
      ? `${p.n}　當時排第 ${entry.rank} 名`
        + (entry.hit === 1 ? "　→ 後來受罰"
          : entry.hit === null ? "　→ 待觀察" : "　→ 後來未受罰")
      : `${p.n}（${TYPE[p.t]}·${p.d}）`
        // 著色依據是什麼，tooltip 就要說什麼，否則深淺只能用猜的。
        + (state.pinBy === "penalty"
          ? `<br>歷來裁罰紀錄 ${p.np || 0} 件`
            + (p.np ? "（件數隨園齡與規模累積，非現況評價）" : "")
          : "");
    m.bindTooltip(tip, { direction: "top" });
    m.on("click", () => openDossier(p.i));
    group.addLayer(m);
  });
  state.layer.addLayer(group);
  $("mapbadge").textContent = tl
    ? tlBadge(tl)
    : state.agentIds
      ? `助理標記 ${shown.length} 筆（共 ${state.points.length} 園）`
      : `顯示 ${shown.length} / ${state.points.length} 園　紅圈 ${flagged.size} 家為本批提案`;
}

function tlBadge(tl) {
  const top = Object.values(tl.ranks).filter((e) => e.rank <= tl.topN);
  const hits = top.filter((e) => e.hit === 1).length;
  if (tl.point.label_complete === false && tl.point.label_end === "") {
    return `${tl.asOf} 即時排序前 ${tl.topN} 名　尚無前瞻窗，無命中率可言`;
  }
  const pct = top.length ? ((hits / top.length) * 100).toFixed(0) : "—";
  return `${tl.asOf} 當時前 ${tl.topN} 名　其中 ${hits} 家在後續兩年受罰（${pct}%）`
    + (tl.point.label_complete ? "" : "　※前瞻窗未走完，為低估");
}

/* ── 行政區界線與名稱 ─────────────────────────────────
 *
 * 界線畫兩趟：先一條寬的紙色墊底，再一條細的深色壓上去。單畫一條深線在
 * OSM 的路網上會跟主要道路混在一起（同色階、同粗細），在 Google 底圖上又
 * 會被行政區既有的虛線疊成兩條。墊底那一趟把線從底圖裡「挖」出來，兩種底圖
 * 都不必各調一次。
 *
 * 名字獨立成一層而不是綁在多邊形上，因為兩者的顯示條件不同：界線任何縮放
 * 都該在，名字放到很大之後只是擋路（那時要回答的是「這條街上是哪一家」）。
 */
function ringArea(ring) {
  let a = 0;
  for (let i = 0, n = ring.length; i < n; i += 1) {
    const [x1, y1] = ring[i], [x2, y2] = ring[(i + 1) % n];
    a += x1 * y2 - x2 * y1;
  }
  return Math.abs(a) / 2;
}

/* 多邊形的面積加權重心。用外接矩形中心會讓淡水、石碇這類細長或凹形的區把
   名字放到區外（甚至海上）；重心至少保證落在質量中心附近。只取面積最大的
   那一環，離島與飛地不該把名字拉走。 */
function ringCentroid(ring) {
  let a = 0, cx = 0, cy = 0;
  for (let i = 0, n = ring.length; i < n; i += 1) {
    const [x1, y1] = ring[i], [x2, y2] = ring[(i + 1) % n];
    const f = x1 * y2 - x2 * y1;
    a += f; cx += (x1 + x2) * f; cy += (y1 + y2) * f;
  }
  if (!a) {                                   // 退化成一條線時用端點平均
    const m = ring.reduce((s, [x, y]) => [s[0] + x, s[1] + y], [0, 0]);
    return [m[0] / ring.length, m[1] / ring.length];
  }
  a *= 3;
  return [cx / a, cy / a];
}

/* z11 全市尺度 11px（29 個名字要同時擺得下），z14 以上 20px 封頂。 */
function districtLabelScale() {
  const z = state.map.getZoom();
  return Math.round(Math.max(11, Math.min(20, 11 + (z - 11) * 3)));
}

function drawDistrictNames() {
  if (state.dnameLayer) {
    state.map.removeLayer(state.dnameLayer);
    state.dnameLayer = null;
  }
  if (!state.dnames) return;
  const size = districtLabelScale();
  const marks = (state.payload.boundary || []).map((f) => {
    const biggest = f.poly.reduce((b, r) => (ringArea(r) > ringArea(b) ? r : b), f.poly[0]);
    const [x, y] = ringCentroid(biggest);
    return L.marker([y, x], {
      pane: "swdname",
      interactive: false,
      keyboard: false,
      icon: L.divIcon({
        className: "",
        // 寬度給足並置中，Leaflet 才不會把長名字（如「三芝區」以外的四字區）截掉。
        iconSize: [120, size + 4],
        iconAnchor: [60, (size + 4) / 2],
        html: `<div class="dlabel" style="font-size:${size}px;text-align:center">`
          + `${esc(f.d)}</div>`,
      }),
    });
  });
  state.dnameLayer = L.layerGroup(marks).addTo(state.map);
}

async function toggleDistricts(on) {
  if (!on) {
    if (state.districtLayer) state.map.removeLayer(state.districtLayer);
    state.districtLayer = null;
    return;
  }
  const boundary = state.payload.boundary || [];
  // 用 polygon 而不是 polyline：資料裡的環未必首尾相接，polygon 會自動閉合，
  // polyline 則會在每個區留下一道缺口。
  const rings = boundary.flatMap((f) => f.poly.map((r) => r.map(([x, y]) => [y, x])));
  const pass = (o) => rings.map((r) => L.polygon(r, { fill: false, interactive: false, ...o }));
  state.districtLayer = L.layerGroup([
    ...pass({ color: cssv("--paper"), weight: 3.4, opacity: 0.85 }),
    ...pass({ color: cssv("--ink-2"), weight: 1.4, opacity: 0.9, dashArray: "5 3" }),
  ]).addTo(state.map);
}

/* ── 提案 ─────────────────────────────────────────────── */
async function refresh() {
  const r = await api(`/api/proposal?n=${state.cap}`);
  state.proposal = r.proposal || [];
  $("s-sel").textContent = state.proposal.length;
  drawList();
  drawMarkers();
}

function tagClass(tier) {
  if (tier.startsWith("財報")) return "s";
  if (tier.startsWith("近")) return "w";
  return "p";
}

function drawList() {
  $("list").innerHTML = state.proposal.map((p, i) => {
    const tags = [`<span class="tag ${tagClass(p.tier)}">${esc(p.tier)}</span>`];
    if (p.np > 0) tags.push(`<span class="tag p">${p.np} 件裁罰史</span>`);
    if (!p.fin) tags.push(`<span class="tag p">無公開財報</span>`);
    return `<button class="item" data-i="${p.i}">
      <span class="ord">${i + 1}</span>
      <span class="nm"><i class="tdot" style="background:${cssv(TVAR[p.t])}"></i>${esc(p.n)}</span>
      <span class="rk">${esc(p.d.replace("區", ""))} · #${p.r}</span>
      <span class="why">${tags.join("")}</span></button>`;
  }).join("");
  $("list").querySelectorAll(".item").forEach((el) =>
    el.addEventListener("click", () => openDossier(el.dataset.i)));
}

/* ── 同儕財務差異 ──────────────────────────────────────
 * 與「法遵未通過」刻意分開呈現：那是對一份申報文件的陳述，這是對「同年度其他
 * 非營利園長什麼樣」的比較。兩者的顏色、標題、句型都不共用——共用會讓「跟別人
 * 不一樣」被讀成「做錯事」。
 *
 * 只呈現最新學年度作為現況，歷年另畫趨勢。取歷史最高分當現在狀態會讓一所園為
 * 三年前的申報一直被標記。                                              */
function peerBlock(peer) {
  if (!peer) return "";
  const pct = peer.pct == null ? null : Math.round(peer.pct);
  const bar = peer.history.map((p) => {
    const v = p.pct == null ? 0 : p.pct;
    return `<div class="ptrend-col" title="${p.y} 學年度：同年第 ${p.rank}/${p.peers}">
      <div class="ptrend-bar" style="height:${Math.max(2, v * 0.6)}px"></div>
      <div class="ptrend-y">${p.y}</div></div>`;
  }).join("");

  let body = `<dl class="kv">
    <dt>同年度位置</dt><dd>第 ${peer.rank} / ${peer.peers} 所非營利園${
      pct == null ? "" : `（第 ${pct} 百分位）`}</dd>
    <dt>比較基礎</dt><dd>${peer.nfeat} 項比率指標</dd>
  </dl>`;

  if (peer.anomalies.length) {
    body += `<div class="peer-list"><div class="peer-cap">達統計異常門檻的項目</div>` +
      peer.anomalies.map((t) => `<div class="peer-item">${esc(t)}</div>`).join("") +
      `</div>`;
  } else {
    body += `<div class="peer-list"><div class="peer-cap">無單項達統計異常門檻。
      以下為分數的主要構成，僅說明排名由何而來，<b>不是異常發現</b>。</div>` +
      peer.contributions.map((t) => `<div class="peer-item">${esc(t)}</div>`).join("") +
      `</div>`;
  }

  if (peer.history.length > 1) {
    body += `<div class="peer-cap" style="margin-top:10px">歷年同儕位置（百分位）</div>
      <div class="ptrend">${bar}</div>`;
  }

  return `<div class="sec"><h4>同儕財務差異<span class="badge">${peer.y} 學年度</span></h4>
    ${body}
    <p class="note" style="margin-top:8px">與同一學年度的其他非營利園比較，
    全部使用比率與每生指標，不受園所規模影響。<b>差異可能完全合法</b>——
    新設園、小型園、契約結構不同都會造成差異。此欄僅供安排人工查核的先後順序，
    不構成違規認定，也不併入左側的優先序分數。</p></div>`;
}

/* ── 卷宗 ─────────────────────────────────────────────── */
async function openDossier(id) {
  const d = await api(`/api/institutions/${id}`);
  const p = d.institution;
  const dos = d.dossier;
  const B = d.benchmarks || {};
  const rt = d.realtime || {};
  const fails = (dos?.findings || []).filter((f) => f.st === "fail");

  state.selected = id;
  state.map.setView([p.y, p.x], Math.max(state.map.getZoom(), 15));

  let h = `<div class="dhead"><button id="dclose">關閉</button>
    <h3>${esc(p.full)}</h3>
    <div class="meta">${TYPE[p.t]} · ${esc(p.d)} · 優先序 #${p.r} / ${state.points.length}
      · 分數 ${p.s.toFixed(3)}</div></div><div class="dbody">
    <div class="sec"><h4>基本</h4><dl class="kv">
      <dt>核定人數</dt><dd>${nf(p.cap)} 人</dd>
      <dt>月費</dt><dd>${p.fee ? nf(p.fee) + " 元" : "—"}</dd>
      <dt>裁罰史</dt><dd>${p.np} 件</dd>
      <dt>近一年官方事件</dt><dd>${p.e365} 件${p.evd ? `（最近 ${p.evd}）` : ""}</dd>
    </dl></div>`;

  h += `<div class="sec"><h4>官方評鑑</h4>${p.er
    ? `<div class="finding${p.ep ? "" : " w"}"${p.ep ? "" : ' style="border-left-color:var(--good)"'}>
        <div class="r">${esc(p.erd)}　${esc(p.er)}</div>
        <div class="d">${p.ep
          ? "近兩年評鑑有指標未通過。全語料庫實測：評鑑後一年內受罰率 17.9%，全數通過者 7.4%（OR 2.72，p=0.0009）。"
          : "最近一次評鑑全數指標通過。這是該次評鑑的結論，不代表其後未發生問題。"}</div></div>`
    : `<div class="insuff">查無評鑑紀錄。未受評鑑不等於通過評鑑。</div>`}</div>`;

  if (!p.fin) {
    h += `<div class="sec"><h4>財務法遵檢核</h4><div class="insuff">
      <b>無公開財報：資料不足，非低風險。</b><br>私立園不公告財務報告，
      本園未出現財務發現，是我們查不到，不是它沒有問題。</div></div>`;
  } else {
    if (fails.length) {
      h += `<div class="sec"><h4>法遵未通過 ${fails.length} 項</h4>`;
      fails.forEach((f) => {
        h += `<div class="finding${f.sev === "high" ? "" : " w"}">
          <div class="r">${f.y} 學年度・${esc(f.rule)}
            <span class="badge">${f.sev === "high" ? "高" : "中"}</span></div>
          <div class="d">${esc(f.detail)}</div>
          <div class="quote"><i>依據原文</i>${esc(f.text)}</div></div>`;
      });
      h += `</div>`;
    }
    (dos?.gaps || []).length && (h += `<div class="sec"><h4>跨年度判讀</h4>` +
      dos.gaps.map((g) => `<div class="finding${g.v.includes("擴大") ? "" : " w"}">
        <div class="r">${esc(g.res)}・${g.y0}→${g.y1} 學年度：${esc(g.v)}</div>
        <div class="d">${esc(g.note)}</div></div>`).join("") + `</div>`);

    h += peerBlock(dos?.peer);

    const st = (dos?.staff || []).filter((s) => s.st && s.cost);
    if (st.length) {
      const last = st[st.length - 1];
      const ph = Math.round(last.cost / last.st);
      h += `<div class="sec"><h4>員工與師生比<span class="badge">${last.y} 學年度</span></h4>
        <dl class="kv">
          <dt>員工人數</dt><dd>${nf(last.st)} 人（教保 ${nf(last.ed)}）</dd>
          <dt>每人人事費</dt><dd>${nf(ph)}（全體中位 ${nf(B.ph_med)}）</dd>
          <dt>師生比</dt><dd>${last.ed && last.en ? "1:" + (last.en / last.ed).toFixed(1) : "—"}
            （中位 1:${B.ratio_med}）</dd>
        </dl>
        <p class="note" style="margin-top:8px">非營利園採成本分攤制、薪給結構一致，
        全體 ${B.n_norm} 份非開辦年報告無一落在四分位距外，故此欄為查證對照，非風險訊號。</p></div>`;
    }
  }

  /* 這三段的資料量比較大（裁罰明細逐筆、名次軌跡七個時點、建議書全文），
     所以先放佔位、開完卷宗再各自載入——不要讓卷宗等它們。 */
  h += `<div class="sec" id="penalties-sec"><h4>裁罰紀錄</h4>
    <div class="insuff">載入中…</div></div>`;
  h += `<div class="sec" id="ranktrack-sec"><h4>排名軌跡</h4>
    <div class="insuff">載入中…</div></div>`;
  h += `<div class="sec" id="memo-sec"><h4>稽核建議書</h4>
    <div class="insuff">載入中…</div></div>`;

  h += realtimeBlock(rt);
  h += `<div class="sec" id="reviews-sec"><h4>Google 地圖評論</h4>
    <div class="insuff">載入中…</div></div>`;
  h += `<div class="sec"><h4>本頁定位</h4><div class="insuff">${esc(d.disclaimer)}</div></div>`;
  h += `</div>`;

  const el = $("dossier");
  el.innerHTML = h;
  el.hidden = false;
  $("dclose").onclick = () => { el.hidden = true; state.selected = null; };
  loadReviews(id);
  loadPenalties(id);
  loadRankTrack(id);
  loadMemo(id);
}

/* ── 卷宗的三段補充 ───────────────────────────────────────
 * 都是「agent 本來就叫得到、但人點卷宗看不到」的東西。與 agent 的
 * get_penalties／set_time_machine／open_memo 走同一組端點，所以畫面上的
 * 數字與 agent 講的話保證一致。 */

function sectionFail(id, msg) {
  const el = $(id);
  if (el) el.querySelector(".insuff").textContent = msg;
}

async function loadPenalties(id) {
  let r;
  try { r = await api(`/api/institutions/${id}/penalties`); }
  catch (e) { return sectionFail("penalties-sec", `載入失敗：${e.message}`); }
  const sec = $("penalties-sec");
  if (!sec) return;
  if (!r.count) {
    sec.innerHTML = `<h4>裁罰紀錄</h4>
      <div class="insuff">查無裁罰紀錄。這代表公開資料中沒有，不等於該園無虞。</div>`;
    return;
  }
  /* 時間軸：新到舊。金額為空白代表非金錢處分，**不是罰 0 元**。 */
  const rows = r.items.map((x) => `
    <li class="ptl">
      <span class="pd">${esc(x.date || "—")}</span>
      <span class="pb">
        <b>${x.article ? "第 " + x.article + " 條" : esc(x.sanction_type || "處分")}</b>
        ${x.actor_role ? `<span class="prole">${esc(x.actor_role)}</span>` : ""}
        <span class="pf">${x.fine == null ? "非金錢處分" : "罰鍰 " + nf(x.fine)}</span>
        ${x.law ? `<div class="pl">${esc(x.law)}</div>` : ""}
      </span>
    </li>`).join("");
  sec.innerHTML = `<h4>裁罰紀錄 ${r.count} 件</h4>
    <ul class="ptlist">${rows}</ul>
    <p class="note">${esc(r.note)}</p>`;
}

async function loadRankTrack(id) {
  let r;
  try { r = await api(`/api/institutions/${id}/ranking`); }
  catch (e) { return sectionFail("ranktrack-sec", `載入失敗：${e.message}`); }
  const sec = $("ranktrack-sec");
  if (!sec) return;
  const pts = r.points.filter((p) => p.rank != null);
  if (!pts.length) {
    sec.innerHTML = `<h4>排名軌跡</h4>
      <div class="insuff">這一所沒有進入任何時點的排序。</div>`;
    return;
  }
  /* 名次越小越前面，所以長條用「越前面越長」表示。hit 三態要看得出來：
     命中（後來受罰）、落空、待觀察（觀察期還沒過完，不是沒事）。 */
  const total = pts[0].total || 1213;
  const bars = pts.map((p) => {
    const pct = Math.max(2, (1 - (p.rank - 1) / total) * 100);
    const cls = p.hit === 1 ? "rt-hit" : p.hit === null ? "rt-live" : "rt-miss";
    const label = p.hit === 1 ? "後來受罰" : p.hit === null ? "待觀察" : "後來未受罰";
    return `<li>
      <span class="rty">${esc(p.as_of.slice(0, 7))}</span>
      <span class="rtbar"><i class="${cls}" style="width:${pct.toFixed(1)}%"></i></span>
      <span class="rtn">#${p.rank}</span>
      <span class="rth ${cls}">${label}</span>
    </li>`;
  }).join("");
  sec.innerHTML = `<h4>排名軌跡</h4><ul class="rtlist">${bars}</ul>
    <p class="note">${esc(r.note)}</p>`;
}

async function loadMemo(id) {
  let r;
  try { r = await api(`/api/institutions/${id}/memo`); }
  catch (e) { return sectionFail("memo-sec", `載入失敗：${e.message}`); }
  const sec = $("memo-sec");
  if (!sec) return;
  if (!r.exists) {
    sec.innerHTML = `<h4>稽核建議書</h4><div class="insuff">${esc(r.note)}</div>`;
    return;
  }
  sec.innerHTML = `<h4>稽核建議書</h4>
    <div class="memometa">${esc(r.file)}${
      r.backend ? `　產生方式 ${esc(r.backend)}` : ""}${
      r.verified === "True" ? "　已通過驗證" : ""}</div>
    <pre class="memo">${esc(r.content)}</pre>
    <p class="note">${esc(r.note)}</p>`;
}

/* Google 評論在開卷宗時才取，不隨 payload 一起送——評分會變，而且只有被點開
   的機構才需要。量測結果：裁罰 ≥5 件的園評分中位 4.20、無裁罰者 4.60
   （p=0.061 不顯著），個案完全不具鑑別力，所以這一區標明是脈絡不是訊號。 */
async function loadReviews(id) {
  const sec = $("reviews-sec");
  if (!sec) return;
  let r;
  try {
    r = await api(`/api/reviews/${id}`);
  } catch (e) {
    sec.innerHTML = `<h4>Google 地圖評論</h4>
      <div class="insuff">取得失敗：${esc(e.message)}</div>`;
    return;
  }
  if (state.selected !== id) return;          // 使用者已切換到別園
  if (!r.available) {
    sec.innerHTML = `<h4>Google 地圖評論</h4>
      <div class="insuff">${esc(r.reason || "無評論資料")}。
      Google 自 2025-04-30 起移除 6–18 歲教育機構的評論，
      本市約 23% 的園（國中小附設幼兒園）因此沒有可用評論。</div>`;
    return;
  }
  const stars = (n) => "★".repeat(Math.round(n || 0)).padEnd(5, "☆");
  const rows = (r.reviews || []).slice(0, 5).map((rv) => `
    <div class="rtitem">
      <div style="font-size:12.5px;line-height:1.55">${esc(rv.text) || "（無文字）"}</div>
      <div class="rtmeta"><span>${stars(rv.rating)}</span>
        <span>${esc(rv.published)}</span><span>${esc(rv.author)}</span></div>
    </div>`).join("");
  sec.innerHTML = `<h4>Google 地圖評論
      <span class="badge">${r.rating ?? "—"} 星 · ${r.review_count ?? 0} 則</span></h4>
    ${rows || '<div class="insuff">此地點尚無文字評論。</div>'}
    <p class="note" style="margin-top:8px">${esc(r.note)}
      ${r.maps_uri ? `<a href="${esc(r.maps_uri)}" target="_blank"
        rel="noopener noreferrer">在 Google 地圖開啟</a>` : ""}</p>`;
}

function realtimeBlock(rt) {
  const chips = (rt.channels || []).map((c) =>
    `<span class="chip2${c.status === "live" ? " on" : ""}" title="${esc(c.legal_basis)}">
      ${c.status === "live" ? "●" : "○"} ${esc(c.label)}</span>`).join("");
  const head = `<div class="sec"><h4>即時公開聲音
      <span class="badge">${rt.channels_live || 0}/${rt.channels_total || 0} 管道</span></h4>
    <div class="chbar">${chips}</div>`;
  const items = rt.mentions || [];
  if (!items.length) {
    return head + `<div class="insuff">本次掃描未發現指名本園的公開內容。
      <b>這不代表沒有問題</b>——目前僅 ${rt.channels_live || 0} 個管道在看，
      且多數報導刻意匿名（實測 267 則中僅 7% 可歸屬）。
      掃描時間 ${esc(rt.swept_at) || "—"}。</div></div>`;
  }
  return head + `<div>${items.slice(0, 8).map((m) => `
    <div class="rtitem k-${esc(m.k)}">
      <a href="${esc(m.u)}" target="_blank" rel="noopener noreferrer">${esc(m.h)}</a>
      <div class="rtmeta"><span>${esc(m.d)}</span><span>${esc(m.p)}</span>
        <span>${m.k === "complaint" || m.k === "incident" ? "事件類" : "一般"}</span></div>
    </div>`).join("")}</div>
    <p class="note" style="margin-top:8px">共 ${items.length} 則，掃描於
      ${esc(rt.swept_at) || "—"}。<b>未經查證，處置為「待人工研判」</b>；
      公開內容不改寫風險分數、不作違規標籤。</p></div>`;
}

/* ── 查詢 ─────────────────────────────────────────────── */
async function ask(question) {
  const log = $("chatlog");
  log.insertAdjacentHTML("beforeend", `<div class="msg me">${esc(question)}</div>`);
  log.scrollTop = log.scrollHeight;

  let r;
  try {
    r = await api("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    });
  } catch (e) {
    log.insertAdjacentHTML("beforeend",
      `<div class="msg bot">查詢失敗：${esc(e.message)}</div>`);
    return;
  }

  const rows = r.results.map((x) => `
    <button class="row" data-i="${x.id}">
      <span class="r">#${x.rank}</span>
      <span>${esc(x.title)}</span>
      <span class="m">${x.compliance_failed ? "法遵" + x.compliance_failed + " " : ""}${
        x.penalties ? "罰" + x.penalties : ""}</span>
    </button>`).join("");
  log.insertAdjacentHTML("beforeend", `<div class="msg bot">
    <p class="sum">${esc(r.summary)}</p>
    <div class="rows">${rows}</div>
    <div class="cav">${esc(r.caveat)}</div>
    <div class="plan">planner=${esc(r.planner)}　${esc(JSON.stringify(r.plan.filters))}</div>
  </div>`);
  log.querySelectorAll(".row").forEach((el) =>
    el.addEventListener("click", () => openDossier(el.dataset.i)));
  log.scrollTop = log.scrollHeight;
}

/* ── 地圖控制與花費 ───────────────────────────────────── */
const LAYER_BOXES = ["f-cluster", "f-districts", "f-dnames", "f-mask",
  "f-flagged", "f-choro"];

function syncLayerCount() {
  $("layern").textContent = LAYER_BOXES.filter((id) => $(id).checked).length;
}

/* 時間軸抽屜。timeline.js 進入回測模式時也會叫它，不然拖桿在收起來的狀態下
   被程式碰到，畫面不會有任何反應。 */
function timelineDock(on) {
  $("tlbar").hidden = !on;
  $("tlpill").setAttribute("aria-expanded", String(on));
}

function showLayers(on) {
  $("layerpanel").hidden = !on;
  $("layerbtn").setAttribute("aria-expanded", String(on));
}

// scan.js 也宣告了 usd。傳統腳本共用同一個全域作用域，同名的 const 會讓
// 後載入的那支整個不執行——掃描頁會無聲消失，所以這裡另取名字。
const money = (v, d = 2) => `US$${Number(v || 0).toFixed(d)}`;

function meter(label, spent, cap) {
  const pct = cap ? Math.min(100, (spent / cap) * 100) : 0;
  return `<div class="meter">
    <div class="mlab"><span>${label}</span><b>${money(spent)} / ${money(cap)}</b></div>
    <div class="bar"><i class="${pct < 70 ? "ok" : ""}"
      style="width:${pct.toFixed(1)}%"></i></div>
  </div>`;
}

/* 花費細目。總額只回答「花了多少」，這裡回答「撞到哪一道牆會先停」——
   三道上限（單次／今日／本週期）哪一道先滿，決定的是掃描按不按得下去。 */
function costHTML(b, entries) {
  const caps = b.caps || {};
  const rows = entries
    .filter((e) => e.kind === "settle" || e.kind === "reserve")
    .slice(0, 5)
    .map((e) => `<span><span>${esc(String(e.ts).slice(5, 16))}　${esc(e.meter)}</span>
      <b>${money(e.actual_usd != null ? e.actual_usd : e.usd_max, 3)}</b></span>`).join("");
  return `<h4>掃描花費（本週期起算 ${esc(b.cycle_start || "—")}）</h4>
    ${meter("本週期", b.month_spent_usd, caps.month)}
    ${meter("今日", b.day_spent_usd, caps.day)}
    ${meter("單次掃描上限", caps.run - b.run_remaining_usd, caps.run)}
    <p class="src">Google Places 本週期已用 ${b.places_used_this_month ?? 0} 次${
    b.unsettled ? `　·　未結清 ${b.unsettled} 筆` : ""}</p>
    ${rows ? `<div class="led">${rows}</div>` : ""}
    <p class="src">${esc(b.source || "")}<br>
      撞自己的牆是不給跑；撞供應商的牆是跑到一半被砍、錢照付。</p>`;
}

async function showCost(on) {
  $("costpop").hidden = !on;
  $("costbtn").setAttribute("aria-expanded", String(on));
  if (!on) return;
  $("costpop").textContent = "載入中…";
  try {
    const [b, l] = await Promise.all([
      api("/api/scan/budget"),
      api("/api/scan/ledger?limit=12").catch(() => ({ entries: [] })),
    ]);
    $("s-budget").textContent =
      `${money(b.month_spent_usd)}/${Number(b.caps.month).toFixed(2)}`;
    $("costpop").innerHTML = costHTML(b, l.entries || []);
  } catch (e) {
    $("costpop").innerHTML = `<p class="src">讀不到帳本：${esc(e.message)}</p>`;
  }
}

/* ── 綁定 ─────────────────────────────────────────────── */
$("layerbtn").addEventListener("click", () => {
  showLayers($("layerpanel").hidden);
  showCost(false);
});
$("costbtn").addEventListener("click", () => {
  showCost($("costpop").hidden);
  showLayers(false);
  showAbout(false);
});
/* ⓘ 說明。頁尾拿掉之後，「這是建議查核不是違法認定」與 CC-BY 的來源標註
   都收在這裡——兩者都不是可有可無的裝飾，只是不再常駐佔畫面。 */
function showAbout(on) {
  $("aboutpop").hidden = !on;
  $("aboutbtn").setAttribute("aria-expanded", String(on));
}
$("aboutbtn").addEventListener("click", () => {
  showAbout($("aboutpop").hidden);
  showCost(false);
  showLayers(false);
  showWho(false);
});
/* 身分浮層。內容由 lobby.js::paintWho 填，這裡只管開關。 */
function showWho(on) {
  $("whopop").hidden = !on;
  $("whopod").setAttribute("aria-expanded", String(on));
}
$("whopod").addEventListener("click", () => {
  showWho($("whopop").hidden);
  showCost(false);
  showAbout(false);
  showLayers(false);
});
// 點到別處就收起浮層；Esc 也收。浮層蓋住地圖時要能一鍵回到地圖。
document.addEventListener("click", (e) => {
  if (!e.target.closest(".mapui")) showLayers(false);
  if (!e.target.closest("#costpop") && !e.target.closest("#costbtn")) showCost(false);
  if (!e.target.closest("#aboutpop") && !e.target.closest("#aboutbtn")) showAbout(false);
  if (!e.target.closest("#whopop") && !e.target.closest("#whopod")) showWho(false);
});
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  showLayers(false);
  showCost(false);
  showAbout(false);
  showWho(false);
});
LAYER_BOXES.forEach((id) =>
  $(id).addEventListener("change", syncLayerCount));

$("cap").addEventListener("input", (e) => {
  state.cap = Math.max(1, Math.min(200, +e.target.value || 20));
  $("capr").value = Math.min(120, state.cap);
  refresh();
});
$("capr").addEventListener("input", (e) => {
  state.cap = +e.target.value; $("cap").value = state.cap; refresh();
});
$("f-cluster").addEventListener("change", (e) => {
  state.cluster = e.target.checked; drawMarkers();
});
$("f-flagged").addEventListener("change", (e) => {
  state.flaggedOnly = e.target.checked; drawMarkers();
});
$("f-districts").addEventListener("change", (e) => toggleDistricts(e.target.checked));
$("f-dnames").addEventListener("change", (e) => {
  state.dnames = e.target.checked; drawDistrictNames();
});
$("f-choro").addEventListener("change", (e) => {
  state.choro = e.target.checked; drawChoro();
});
/* 切換標記著色依據。圖例跟著換：留著上一個模式的圖例比沒有圖例更糟——
   看的人會拿機構類別的三色去讀裁罰深淺。 */
document.querySelectorAll("#pinby button").forEach((b) =>
  b.addEventListener("click", () => {
    state.pinBy = b.dataset.p;
    document.querySelectorAll("#pinby button")
      .forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("pen-grp").hidden = state.pinBy !== "penalty";
    drawPenaltyLegend();
    drawMarkers();
  }));
$("tlpill").addEventListener("click", () => timelineDock($("tlbar").hidden));
$("f-mask").addEventListener("change", (e) => {
  state.mask = e.target.checked; applyMask(state.mask);
});
document.querySelectorAll(".ftype").forEach((el) =>
  el.addEventListener("change", () => {
    state.types = new Set([...document.querySelectorAll(".ftype:checked")]
      .map((x) => +x.value));
    drawMarkers();
  }));
/* 切換右欄顯示哪一個 pane。
 *
 * 抽成函式是因為現在有兩個呼叫端：隱藏的分頁列（scan.js／timeline.js 仍靠它
 * 的 data-t）與左側樓層索引（lobby.js::setRail）。兩邊各寫一份 pane 切換的
 * 話，第三個呼叫端出現時就會有一個忘了同步。 */
function showPane(name) {
  document.querySelectorAll(".tabs button")
    .forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.t === name)));
  document.querySelectorAll(".pane").forEach((pane) => {
    pane.hidden = pane.id !== `pane-${name}`;
  });
  if (name === "scan" && window.SWScan) window.SWScan.open();
  if (name === "timeline") timelineDock(true);
}

document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => showPane(b.dataset.t)));

/* 查詢頁籤（main 的 Bedrock planner）。助理頁籤是另一個面板、另一組 id，
   兩者並存：查詢回名單，助理會實際操作畫面。 */
$("chatform").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  $("q").value = "";
  ask(q);
});
/* 只接查詢面板內的範例鈕。助理面板的 .eg 由 agent.js 自己綁——
   用全域委派會讓助理的範例鈕同時觸發這裡的 ask()。 */
document.querySelectorAll("#pane-chat .eg").forEach((el) =>
  el.addEventListener("click", () => ask(el.textContent.trim())));

/* 掃描分頁（scan.js）需要這些；集中匯出一次，不要讓它去翻全域變數。 */
window.SW = { api, post, $, esc, nf, state, openDossier, TYPE, drawMarkers, refresh,
  timelineDock, showPane, fitNTPC };

boot().catch((e) => {
  document.body.insertAdjacentHTML("afterbegin",
    `<div style="padding:1.5rem;color:#B23A2F">啟動失敗：${esc(e.message)}<br>
     請先執行 <code>PYTHONPATH=src .venv/bin/python scripts/build_frontend.py</code></div>`);
});
