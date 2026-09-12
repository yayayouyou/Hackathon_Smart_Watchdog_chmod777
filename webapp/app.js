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
  await refresh();
}

/* ── 地圖 ─────────────────────────────────────────────── */
const BASES = {
  osm: {
    url: "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    attr: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  },
  carto: {
    url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    attr: '© OpenStreetMap contributors © <a href="https://carto.com/">CARTO</a>',
  },
};

function initMap() {
  state.map = L.map("map", {
    center: NTPC, zoom: 11, zoomControl: true,
    scrollWheelZoom: true, preferCanvas: true,
  });
  setBase("osm");
  state.layer = L.layerGroup().addTo(state.map);

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

function pinIcon(p, flagged) {
  const size = flagged ? 13 : 10;
  return L.divIcon({
    className: "",
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
    html: `<div class="pin${flagged ? " flag" : ""}" style="width:${size}px;
      height:${size}px;background:${cssv(TVAR[p.t])}"></div>`,
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
      : `${p.n}（${TYPE[p.t]}·${p.d}）`;
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

async function toggleDistricts(on) {
  if (!on) {
    if (state.districtLayer) state.map.removeLayer(state.districtLayer);
    state.districtLayer = null;
    return;
  }
  const boundary = state.payload.boundary || [];
  state.districtLayer = L.layerGroup(
    boundary.flatMap((f) =>
      f.poly.map((ring) =>
        L.polygon(ring.map(([x, y]) => [y, x]), {
          color: cssv("--ink-3"), weight: 1, opacity: 0.5,
          fill: false, interactive: false,
        }))),
  ).addTo(state.map);
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

/* ── 綁定 ─────────────────────────────────────────────── */
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
document.querySelectorAll(".ftype").forEach((el) =>
  el.addEventListener("change", () => {
    state.types = new Set([...document.querySelectorAll(".ftype:checked")]
      .map((x) => +x.value));
    drawMarkers();
  }));
document.querySelectorAll(".tabs button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".tabs button")
      .forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    // 依 data-t 找對應的 pane，加分頁不必再回來改這裡。
    document.querySelectorAll(".pane").forEach((pane) => {
      pane.hidden = pane.id !== `pane-${b.dataset.t}`;
    });
    if (b.dataset.t === "scan" && window.SWScan) window.SWScan.open();
  }));

/* 掃描分頁（scan.js）需要這些；集中匯出一次，不要讓它去翻全域變數。 */
window.SW = { api, post, $, esc, nf, state, openDossier, TYPE, drawMarkers, refresh };

boot().catch((e) => {
  document.body.insertAdjacentHTML("afterbegin",
    `<div style="padding:1.5rem;color:#B23A2F">啟動失敗：${esc(e.message)}<br>
     請先執行 <code>PYTHONPATH=src .venv/bin/python scripts/build_frontend.py</code></div>`);
});
