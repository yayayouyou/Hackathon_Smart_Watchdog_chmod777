/* 時間軸：把時鐘倒回去，看當時的排序後來對不對。
 *
 * 這一條拖桿要講的事只有一件：**這套排序不是只在報告裡那一個窗口有效。**
 * 每一格都是一次獨立的前進式驗證——用那一天看得到的資料重新訓練、重新排序，
 * 再把後續兩年實際受罰的園疊上去。拉到最右邊就是今天，沒有前瞻窗，
 * 只有待辦清單；系統實際運作時永遠站在那一格。
 *
 * 三件事刻意不讓畫面含糊：
 *   1. 右設限的格子（前瞻窗還沒走完）用虛線與註記標出來，**不與完整觀察的
 *      格子連成同一條趨勢線**——它的命中率是低估，連起來會讀成「效能下滑」。
 *   2. 即時格不顯示任何命中率。沒發生的事不是 0。
 *   3. 命中率永遠跟當期基準率並列。35% 聽起來普通，除非旁邊寫著隨機是 16.8%。
 */
/* **整支包在 IIFE 裡。** 傳統 <script> 共用一個全域範圍，而頂層的 `function`
 * 宣告是**靜默覆蓋**——不像 `const` 會丟 SyntaxError，所以壞掉時完全沒有線索。
 * scan.js 與 timeline.js 都宣告了 `function render()`，timeline.js 載入在後，
 * 於是 scan.js 裡呼叫的 render 其實是 timeline 的：掃描主控台永遠停在
 * 「載入中…」，Console 一個字都不會印。`boot` 也在 app.js 與 timeline.js
 * 之間重名。包起來就沒有這回事。 */
(function () {
const T = window.SW;

const tl = {
  data: null,      // /api/timeline 的骨架
  idx: 0,          // 目前在第幾格
  topN: 100,       // 「前 N 名」的 N，與派工容量分開（這是回測參數不是派工量）
  cache: {},       // as_of → 逐園排序，避免拖動時重複請求
};

const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(1)}%`);

async function boot() {
  try {
    tl.data = await T.api("/api/timeline");
  } catch (e) {
    T.$("tlbar").innerHTML =
      `<div class="tlnote">時間軸尚未產生：先執行 <code>python run.py timeline</code></div>`;
    return;
  }
  tl.idx = tl.data.points.length - 1;   // 預設停在「今天」
  render();
  T.$("tlrange").addEventListener("input", (e) => {
    tl.idx = Number(e.target.value);
    render();
    apply();
  });
  T.$("tltop").addEventListener("change", (e) => {
    tl.topN = Number(e.target.value);
    render();
    apply();
  });
  T.$("tloff").addEventListener("click", exit);
}

function point() {
  return tl.data.points[tl.idx];
}

function render() {
  const pts = tl.data.points;
  const p = point();
  const r = T.$("tlrange");
  r.max = String(pts.length - 1);
  r.value = String(tl.idx);

  T.$("tlticks").innerHTML = pts.map((q, i) => {
    const live = q.label_end === "";
    const cls = i === tl.idx ? " on" : "";
    const cen = !live && !q.label_complete ? " censored" : "";
    const label = live ? "今天" : q.as_of.slice(0, 4);
    return `<span class="tltick${cls}${cen}" data-i="${i}">${label}</span>`;
  }).join("");
  T.$("tlticks").querySelectorAll(".tltick").forEach((el) =>
    el.addEventListener("click", () => {
      tl.idx = Number(el.dataset.i);
      render();
      apply();
    }));

  const live = p.label_end === "";
  let metrics;
  if (live) {
    metrics = `<b>即時監控</b>
      <span>用 ${p.as_of} 當天看得到的資料排序全市 ${T.nf(p.n_institutions)} 園</span>
      <span class="tlwarn">尚無前瞻窗 — 這一格是待辦清單，不是成績</span>`;
  } else {
    const lift = p.lift_at["100"];
    metrics = `
      <span>當期基準率 <b>${pct(p.base_rate)}</b>
        <i>（全市 ${T.nf(p.n_institutions)} 園中 ${T.nf(p.n_positive)} 家在
        ${p.label_start}–${p.label_end} 受罰）</i></span>
      <span>前 100 名命中 <b>${pct(p.precision_at["100"])}</b></span>
      <span>提升 <b>${lift == null ? "—" : lift.toFixed(2) + "x"}</b></span>
      <span>AUC <b>${p.auc == null ? "—" : p.auc.toFixed(3)}</b></span>
      ${p.label_complete ? ""
        : `<span class="tlwarn">前瞻窗只走了 ${pct(p.observed_fraction)}
           — 命中率為低估，不可與其他年份直接比較</span>`}`;
  }
  T.$("tlmetrics").innerHTML = metrics;
  T.$("tltrain").textContent = live
    ? `模型以 ${p.train_as_of} 的資料訓練（最近一段標籤已觀察完畢的期間）`
    : `模型以 ${p.train_as_of} 的特徵訓練，標籤取 ${p.train_as_of}–${p.as_of}；`
      + `預測只用 ${p.as_of} 當天看得到的資料`;
}

async function apply() {
  const p = point();
  if (!tl.cache[p.as_of]) {
    const r = await T.api(`/api/timeline/${p.as_of}?n=2000`);
    // 用 e.i（與 payload 點位同一把短 key）當索引，不是完整 UUID——
    // 地圖的 state.byId 是以短 id 建的，用錯就整張圖都不會亮。
    const ranks = {};
    r.ranking.forEach((e) => { ranks[e.i] = e; });
    tl.cache[p.as_of] = ranks;
  }
  T.state.timeline = {
    asOf: p.as_of, point: p, ranks: tl.cache[p.as_of], topN: tl.topN,
  };
  T.drawMarkers();
  T.timelineDock(true);          // 收起來的話，回測模式會沒有任何畫面反應
  T.$("tlbar").classList.add("active");
  drawRankList();
}

function drawRankList() {
  const ranks = tl.cache[point().as_of];
  const rows = Object.values(ranks)
    .filter((e) => e.rank <= tl.topN)
    .sort((a, b) => a.rank - b.rank);
  const html = rows.map((e) => {
    const p = T.state.byId[e.i];
    if (!p) return "";
    const mark = e.hit === 1 ? `<i class="tlm hit">後來受罰</i>`
      : e.hit === null ? `<i class="tlm live">待觀察</i>`
        : `<i class="tlm miss">未受罰</i>`;
    return `<div class="tlrow" data-i="${e.i}">
      <span class="tlr">${e.rank}</span>
      <span class="tln">${T.esc(p.n)}</span>
      <span class="tlt">${T.TYPE[p.t]}·${T.esc(p.d)}</span>
      ${mark}</div>`;
  }).join("");
  const box = T.$("tllist");
  box.innerHTML = html || `<div class="tlnote">這一格沒有排序資料</div>`;
  box.querySelectorAll(".tlrow").forEach((el) =>
    el.addEventListener("click", () => T.openDossier(el.dataset.i)));
}

function exit() {
  T.state.timeline = null;
  T.$("tlbar").classList.remove("active");
  T.$("tllist").innerHTML = "";
  T.drawMarkers();
}

/* app.js 的 boot() 是非同步的；等 payload 到位再啟動，否則排序列表
 * 查不到園名（state.byId 還是空的）。 */
(function waitForPayload() {
  if (T.state.points && T.state.points.length) boot();
  else setTimeout(waitForPayload, 120);
})();
})();
