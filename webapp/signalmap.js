/* 訊號圖：我們有哪些資料、哪些真的進了模型、哪些沒有。
 *
 * 中心是稽查優先序模型的現行配置。往內一圈是由資料算出來的訊號，往外一圈是
 * 資料來源本身。
 *
 * **「沒連到中心」是這張圖要講的話，不是缺漏。**
 * 資料來源一律連到它餵的訊號——那些資料我們都有。但訊號到中心那一段會斷：
 *
 *   實線   已在 PRIORITY_FEATURES 裡，真的影響分數
 *   虛線   分層後量到效果，但還沒納入計分
 *   不連線 量過了，分層後不具預測力（或筆數太少無法量測），灰掉
 *
 * 一張只畫有效訊號的圖，會讓人以為我們什麼都用上了。單文件法遵檢核在非營利園
 * 內的提升只有 0.42（低於基準），把它畫成一條連到中心的線就是錯的。
 *
 * 數字全部來自 /api/signal-map，由 `python run.py signal-map` 現算，不寫死。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";

  /* 全站字級底線是 15px（中年稽查員 + 會場投影，見 test_frontend_smoke）。
     15px 的標籤需要 176px 寬的框，13 個訊號排一圈會互相重疊，所以相鄰的
     交錯放在兩個半徑上——徑向錯開 78px，剛好比框高 50px 多一點。 */
  /* 全站字級底線是 15px（中年稽查員 + 會場投影）。SVG 若用固定 viewBox 再縮到
     容器寬度，15px 會被縮成 6px——那等於繞過底線。所以這裡**依容器實際尺寸
     1:1 排版**，字是多大就多大。

     版型不是放射狀而是「來源 → 訊號 → 中心」三欄：13 個訊號的標籤在 15px 下
     各需要約 280px 寬，排成一圈需要的周長是可用空間的兩倍以上，一定互相重疊。
     三欄同樣講得出「都連進來，但不一定連到中心」，而且每一條線的方向就是
     資料流的方向。 */
  const state = { data: null, sel: null, loaded: false };

  const el = (tag, attrs, parent) => {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v !== null && v !== undefined) n.setAttribute(k, v);
    }
    if (parent) parent.appendChild(n);
    return n;
  };

  const esc = (s) => String(s === null || s === undefined ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

  /* 四種判定，各自一個顏色。**不是紅綠燈** ——這裡講的是「這個訊號有沒有
     進模型」，不是「這間園有沒有問題」。所以刻意不用 --seal。 */
  const VERDICT = {
    scored: { zh: "已計分", link: "solid", cls: "sm-scored" },
    candidate: { zh: "候選", link: "dash", cls: "sm-cand" },
    context: { zh: "只作參考", link: "none", cls: "sm-ctx" },
    disproven: { zh: "不具預測力", link: "none", cls: "sm-off" },
    unmeasured: { zh: "無法量測", link: "none", cls: "sm-off" },
  };

  const pct = (v) => (v === null || v === undefined ? "—" : (v * 100).toFixed(1) + "%");
  const num = (v) => (v === null || v === undefined ? "—" : v.toFixed(2) + "×");

  // 訊號來自哪一個來源。id 對得上 build_signal_map.py 的 sources()。
  const FEEDS = {
    prior_penalty: ["penalties"], prior_penalty_3plus: ["penalties"],
    prior_severe: ["penalties"], eval_partial: ["evaluation"],
    has_mention: ["mentions"], comp_fail: ["pagewise"],
    cross_fail: ["pagewise", "fees", "registry"],
    new_school: ["registry"], has_shuttle: ["vehicles"],
  };
  const feedsOf = (id) => FEEDS[id] || (id.startsWith("x:")
    ? ["pagewise", "fees", "registry"] : ["registry"]);

  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

  function draw() {
    const host = $("sm-canvas");
    if (!host || !state.data) return;
    host.textContent = "";

    const W = Math.max(780, Math.floor(host.clientWidth) - 6);
    const rows = Math.max(state.data.signals.length, state.data.sources.length);
    const H = Math.max(560, Math.floor(host.clientHeight) - 6, rows * 46 + 40);

    const SRC_X = 16;                                  // 來源圓點
    const SRC_TEXT = SRC_X + 16;
    const SRC_OUT = Math.min(226, Math.round(W * 0.27));   // 連線從這裡出發
    const PILL_W = Math.min(306, Math.round(W * 0.37));
    const PILL_X = SRC_OUT + 34;
    const CORE_R = Math.min(86, Math.round(H / 7));
    const CORE_X = W - CORE_R - 14;
    const PILL_H = 38;

    const svg = el("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}`,
      class: "smsvg", role: "img", "aria-label": "訊號圖" }, host);
    const links = el("g", {}, svg);
    const nodes = el("g", {}, svg);

    const lay = (items, h) => {
      const gap = (H - 28) / items.length;
      return items.map((it, i) => ({ ...it, y: 14 + gap * (i + 0.5), h }));
    };
    const sigs = lay(state.data.signals, PILL_H);
    const srcs = lay(state.data.sources, 0);
    const byId = Object.fromEntries(srcs.map((s) => [s.id, s]));

    // 來源 → 訊號。這一段一律畫：那些資料我們都有。
    sigs.forEach((s) => {
      feedsOf(s.id).forEach((sid) => {
        const src = byId[sid];
        if (!src) return;
        const mx = (SRC_OUT + PILL_X) / 2;
        el("path", { d: `M${SRC_OUT} ${src.y} C${mx} ${src.y} ${mx} ${s.y} `
          + `${PILL_X} ${s.y}`, class: "sm-feed", fill: "none" }, links);
      });
    });

    // 訊號 → 中心。斷在這裡的，就是沒有影響分數的。
    sigs.forEach((s) => {
      const v = VERDICT[s.verdict] || VERDICT.context;
      if (v.link === "none") return;
      const x0 = PILL_X + PILL_W;
      const x1 = CORE_X - CORE_R;
      const mx = (x0 + x1) / 2;
      el("path", { d: `M${x0} ${s.y} C${mx} ${s.y} ${mx} ${H / 2} ${x1} ${H / 2}`,
        fill: "none",
        class: "sm-link " + (v.link === "dash" ? "sm-link-dash" : "sm-link-solid") },
      links);
    });

    // 中心：模型的現行配置
    const m = state.data.model;
    const cy = H / 2;
    el("circle", { cx: CORE_X, cy, r: CORE_R, class: "sm-core" }, nodes);
    const put = (dy, cls, txt) => {
      const t = el("text", { x: CORE_X, y: cy + dy, class: cls,
        "text-anchor": "middle" }, nodes);
      t.textContent = txt;
    };
    put(-34, "sm-core-eyebrow", "最佳配置");
    put(-8, "sm-core-n", m.features.length + " 特徵");
    put(18, "sm-core-k", "AUC " + m.auc);
    put(42, "sm-core-k", "前 100 " + m.p_at_100 + "×");

    // 來源
    srcs.forEach((s) => {
      const g = el("g", { class: "sm-node sm-src", "data-kind": "source",
        "data-id": s.id, tabindex: "0", role: "button" }, nodes);
      el("circle", { cx: SRC_X, cy: s.y, r: 6, class: "sm-dot" }, g);
      const a = el("text", { x: SRC_TEXT, y: s.y - 3, class: "sm-src-name" }, g);
      a.textContent = clip(s.name, 13);
      const b = el("text", { x: SRC_TEXT, y: s.y + 15, class: "sm-src-n" }, g);
      b.textContent = (s.count || 0).toLocaleString("en-US") + " " + (s.unit || "");
    });

    // 訊號
    sigs.forEach((s) => {
      const v = VERDICT[s.verdict] || VERDICT.context;
      const st = s.stratified || s.overall || {};
      const g = el("g", { class: "sm-node sm-sig " + v.cls, "data-kind": "signal",
        "data-id": s.id, tabindex: "0", role: "button" }, nodes);
      el("rect", { x: PILL_X, y: s.y - PILL_H / 2, width: PILL_W, height: PILL_H,
        rx: 8, class: "sm-box" }, g);
      const a = el("text", { x: PILL_X + 12, y: s.y + 6, class: "sm-sig-name" }, g);
      a.textContent = clip(s.label, 15);
      const b = el("text", { x: PILL_X + PILL_W - 12, y: s.y + 6,
        class: "sm-sig-n", "text-anchor": "end" }, g);
      b.textContent = st.lift ? num(st.lift) : v.zh;
    });

    host.addEventListener("click", onPick);
    host.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") onPick(e);
    });
  }

  function onPick(e) {
    const n = e.target.closest(".sm-node");
    if (!n) return;
    e.preventDefault();
    state.sel = { kind: n.dataset.kind, id: n.dataset.id };
    document.querySelectorAll(".sm-node").forEach((x) =>
      x.classList.toggle("on", x === n));
    detail();
  }

  /* ── 右側說明 ──────────────────────────────────────────────────── */

  function detail() {
    const box = $("sm-detail");
    if (!box || !state.data) return;
    const d = state.data;
    if (!state.sel) { box.innerHTML = summary(); return; }
    if (state.sel.kind === "source") {
      const s = d.sources.find((x) => x.id === state.sel.id);
      if (!s) return;
      box.innerHTML = '<div class="sm-d-eyebrow">資料來源</div>'
        + '<h4 class="sm-d-title">' + esc(s.name) + "</h4>"
        + '<div class="kv"><span>筆數</span><span class="sm-mono">'
        + (s.count || 0).toLocaleString("en-US") + " " + esc(s.unit || "") + "</span>"
        + "<span>類別</span><span>" + esc(s.kind) + "</span>"
        + (s.licence ? "<span>授權</span><span>" + esc(s.licence) + "</span>" : "")
        + "</div>"
        + (s.note ? '<p class="sm-d-note">' + esc(s.note) + "</p>" : "");
      return;
    }
    const s = d.signals.find((x) => x.id === state.sel.id);
    if (!s) return;
    const v = VERDICT[s.verdict] || VERDICT.context;
    const row = (title, r) => {
      if (!r) {
        return '<div class="sm-row"><b>' + title
          + '</b><span class="sm-none">筆數太少，無法量測</span></div>';
      }
      return '<div class="sm-row"><b>' + title + "</b>"
        + '<span class="sm-mono">被點到 ' + r.n + " · 命中 " + r.hits
        + "（" + pct(r.rate) + "）· 對照 " + pct(r.base)
        + " · 提升 <b>" + num(r.lift) + "</b> · p=" + r.p + "</span></div>";
    };
    box.innerHTML = '<div class="sm-d-eyebrow">訊號 · ' + esc(v.zh) + "</div>"
      + '<h4 class="sm-d-title ' + v.cls + '">' + esc(s.label) + "</h4>"
      + '<p class="sm-d-note">' + esc(s.why) + "</p>"
      + row("在 " + esc(s.stratum) + " 母體內（採用這一個）", s.stratified)
      + row("全市未分層（僅供對照）", s.overall)
      + row("改用 " + esc(d.robustness_as_of) + " 觀測（穩健性）", s.robustness)
      + '<p class="sm-d-warn">分層後的數字才算數。任何在公立／非營利／私立間'
      + "有結構性差異的訊號，不分層都會看起來很強——這個專案已經踩過三次。</p>";
  }

  /* 預設面板只回答一件事：目前最佳配置是什麼。
     圖例用圖示不用整句話——這一格的主體是那張圖，說明文字搶了版面就看不到圖。 */
  const ICON = {
    target: '<svg viewBox="0 0 20 20" class="sm-i"><circle cx="10" cy="10" r="7.5"/>'
      + '<circle cx="10" cy="10" r="3.6"/><circle cx="10" cy="10" r="1" class="f"/></svg>',
    gauge: '<svg viewBox="0 0 20 20" class="sm-i"><path d="M3 14a7 7 0 1 1 14 0"/>'
      + '<path d="M10 14 13.6 8.6"/></svg>',
    stack: '<svg viewBox="0 0 20 20" class="sm-i"><path d="M10 3 17 6.6 10 10 3 6.6z"/>'
      + '<path d="M3 10.4 10 14l7-3.6"/></svg>',
  };

  function summary() {
    const d = state.data;
    const m = d.model;
    const n = (v) => d.signals.filter((s) => s.verdict === v).length;
    const off = n("context") + n("disproven") + n("unmeasured");
    return '<div class="sm-d-eyebrow">目前最佳配置</div>'
      + '<div class="sm-stats">'
      + '<div class="sm-stat">' + ICON.stack + "<b>" + m.features.length
      + "</b><span>個特徵</span></div>"
      + '<div class="sm-stat">' + ICON.gauge + "<b>" + m.auc
      + "</b><span>AUC</span></div>"
      + '<div class="sm-stat">' + ICON.target + "<b>" + m.p_at_100
      + "×</b><span>前 100 名</span></div>"
      + "</div>"
      + '<p class="sm-d-note">時序切分實測 · 觀測點 ' + esc(d.as_of)
      + " · 標籤窗 " + d.label_window_days + " 天</p>"
      + '<div class="sm-legend">'
      + '<div><i class="sm-lg sm-lg-solid"></i>已計分<b>' + n("scored") + "</b></div>"
      + '<div><i class="sm-lg sm-lg-dash"></i>候選<b>' + n("candidate") + "</b></div>"
      + '<div><i class="sm-lg sm-lg-off"></i>未連線<b>' + off + "</b></div>"
      + "</div>"
      + '<p class="sm-d-note">點任一個節點看它的數字。</p>';
  }

  /* ── 進場 ────────────────────────────────────────────────────────
     綁在 showPane 而不是分頁列的 click：#tabs 是 hidden，從中庭進來的人
     不會去點它，綁在那裡的結果就是進來一片空白（memos.js 踩過）。 */

  async function open() {
    if (state.loaded) return;
    const box = $("sm-canvas");
    if (box) box.innerHTML = '<div class="soc-load">載入中…</div>';
    try {
      const r = await fetch("/api/signal-map");
      if (!r.ok) {
        let msg = r.status + "";
        try { msg = (await r.json()).detail || msg; } catch (e) { /* 非 JSON */ }
        throw new Error(msg);
      }
      state.data = await r.json();
    } catch (err) {
      if (box) {
        box.innerHTML = '<div class="insuff">訊號圖讀不到：' + esc(err.message)
          + "</div>";
      }
      return;
    }
    state.loaded = true;
    draw();
    detail();
  }

  /* 訊號圖／時間軸回測切換。時間軸那一側要同時開抽屜，不然拖桿在收起來的
     狀態下拖不到（app.js::timelineDock 的既有行為）。 */
  document.addEventListener("click", (e) => {
    const b = e.target.closest("#sm-view button");
    if (!b) return;
    const on = b.dataset.v;
    document.querySelectorAll("#sm-view button").forEach((x) =>
      x.setAttribute("aria-pressed", String(x.dataset.v === on)));
    const pane = $("sm-pane");
    const tl = $("tllist");
    if (pane) pane.hidden = on !== "map";
    if (tl) tl.hidden = on !== "time";
    if (on === "time" && window.SW && window.SW.timelineDock) {
      window.SW.timelineDock(true);
    }
    const src = $("sm-src");
    if (src && state.data) {
      src.textContent = on === "map"
        ? "signal_map · as_of " + state.data.as_of
        : "timeline · 逐年重訓";
    }
  });

  window.SWSignalMap = { open };
})();
