/* 中庭：稽查中心的平面圖，以及「走進某一室」這個動作。
 *
 * ## 為什麼守護犬走不進房間
 *
 * 牠跟著游標，但目標點被 `clampToAtrium()` 夾在中庭矩形內——滑鼠飄到房間上
 * 只會亮門，牠仍然停在牆這一側。**進房是一個決定，不是滑鼠經過的副作用。**
 * 沒有這條，游標掃過畫面就會一路進房，人會不敢移動滑鼠。
 *
 * ## 過渡的四拍
 *
 * 整段約 1.1 秒，刻意分四拍而不是一次淡出——一次淡出看不出「走進去」，
 * 而空間感正是這個設計唯一要傳達的東西。
 *
 *   0ms   守護犬從現在的位置跑向那一室的門口
 *   260ms 平面圖開始縮放，把那一間放大到填滿舞台；其餘房間淡出
 *   620ms 守護犬從門口走進去，坐到左下角的狗窩
 *   880ms 室內介面淡入
 *
 * `prefers-reduced-motion` 時全部改成直接切換：位移動畫對前庭系統敏感的人
 * 是真的會不舒服，而這個設計的資訊完全可以靠文字與版面傳達。
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const SVGNS = "http://www.w3.org/2000/svg";
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* 平面圖座標系。用 viewBox 單位，不是 px——SVG 自己 preserveAspectRatio
     去適應容器，所以筆電與外接螢幕拿到的是同一份幾何。 */
  const W = 1440, H = 760;
  const ATRIUM = { x: 545, y: 30, w: 350, h: 700 };
  /* 守護犬的活動範圍：中庭再往內縮，免得牠半個身體卡在牆上 */
  const WALK = { x0: 578, x1: 862, y0: 70, y1: 686 };
  const KENNEL = { x: 118, y: 648 };   // 放大到全螢幕後，狗窩在左下角

  /* 每一室。`pane` 對應既有 app 的分頁，`door` 是中庭那一側的開口中心。 */
  /* `map: true` 才會顯示地圖那一欄。地圖跟輿情、資料、建議書沒有關係，
     擺在那裡只是把畫面切碎——這是使用者明確要求的。
     `src` 是室頭右側的來源膠囊：每一室都要說得出自己的數字哪來的。 */
  const ROOMS = [
    { id: "map", no: "01", name: "地圖室", pane: "list", accent: "--seal", map: true,
      x: 40, y: 30, w: 505, h: 350, door: { x: 545, y: 210 }, side: "L",
      desc: "全市 1,213 園 · 點一園看判斷原因與紀錄",
      src: "registry · 快照 2026-08-10" },
    { id: "data", no: "02", name: "資料室", pane: "data", accent: "--pub",
      x: 40, y: 380, w: 505, h: 350, door: { x: 545, y: 550 }, side: "L",
      desc: "PDF 提取與外部蒐集的存放處",
      src: "data/extracted · data/external" },
    { id: "voice", no: "03", name: "輿情室", pane: "scan", accent: "--good",
      x: 895, y: 30, w: 505, h: 234, door: { x: 895, y: 150 }, side: "R",
      desc: "新聞、PTT、評鑑；Threads 待接",
      src: "realtime · 掃描於 2026-09-08" },
    { id: "backtest", no: "04", name: "回測室", pane: "timeline", accent: "--warn",
      x: 895, y: 264, w: 505, h: 233, door: { x: 895, y: 380 }, side: "R",
      desc: "每年重訓一次，看當時的排序後來對不對",
      src: "timeline · 2021–2024" },
    { id: "letters", no: "05", name: "文書室", pane: "memos", accent: "--ink-3",
      x: 895, y: 497, w: 505, h: 233, door: { x: 895, y: 613 }, side: "R",
      desc: "稽核建議書草稿與派工單",
      src: "report · 143 份" },
  ];

  const state = { where: "lobby", busy: false, dogX: 720, dogY: 560, room: null };
  const cssv = (n) => getComputedStyle(document.documentElement)
    .getPropertyValue(n).trim();

  const el = (tag, attrs, parent) => {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  };

  /* ── 畫平面圖 ──────────────────────────────────────── */
  function drawPlan(counts) {
    const svg = $("plan");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
    svg.textContent = "";

    const defs = el("defs", {}, svg);
    const grid = el("pattern", { id: "lgrid", width: 24, height: 24,
      patternUnits: "userSpaceOnUse" }, defs);
    el("path", { d: "M24 0H0V24", fill: "none", stroke: cssv("--rule"),
      "stroke-width": .6, opacity: .45 }, grid);
    el("rect", { width: W, height: H, fill: "url(#lgrid)" }, svg);

    /* 縮放群組：進房時只動它。守護犬不在裡面，牠要能獨立走位。 */
    const zoom = el("g", { id: "planzoom" }, svg);
    const rest = el("g", { id: "planrest" }, zoom);   // 除了目標房間以外的一切

    el("rect", { x: 40, y: 30, width: 1360, height: 700, rx: 4,
      fill: cssv("--panel"), stroke: cssv("--edge"), "stroke-width": 3 }, rest);
    el("rect", { ...ATRIUM, fill: cssv("--sunk"), opacity: .55 }, rest);
    el("line", { x1: 545, y1: 30, x2: 545, y2: 730, stroke: cssv("--edge"),
      "stroke-width": 2.2 }, rest);
    el("line", { x1: 895, y1: 30, x2: 895, y2: 730, stroke: cssv("--edge"),
      "stroke-width": 2.2 }, rest);
    const at = el("text", { x: 720, y: 62, "font-size": 11, fill: cssv("--ink-4"),
      "letter-spacing": 3.4, "text-anchor": "middle" }, rest);
    at.textContent = "中庭";

    ROOMS.forEach((r) => {
      // 每一室自己一個 g，進房時把它從 rest 提到 zoom 底下單獨留著
      const g = el("g", { class: "room-hit", "data-room": r.id,
        role: "button", tabindex: "0",
        "aria-label": `${r.no} ${r.name}，${r.desc}` }, rest);
      r.g = g;
      el("rect", { x: r.x, y: r.y, width: r.w, height: r.h, class: "room-fill",
        fill: cssv(r.accent), opacity: .07 }, g);
      el("rect", { x: r.x, y: r.y, width: r.w, height: r.h, fill: "none",
        stroke: cssv("--edge"), "stroke-width": 2.2 }, g);

      const tx = r.x + 34;
      const no = el("text", { x: tx, y: r.y + 44, "font-size": 11,
        fill: cssv("--ink-4"), "letter-spacing": 2.4 }, g);
      no.textContent = r.no;
      const nm = el("text", { x: tx, y: r.y + 76, "font-size": 24,
        "font-weight": 600, fill: cssv("--ink"), "letter-spacing": 2 }, g);
      nm.textContent = r.name;
      const ds = el("text", { x: tx, y: r.y + 102, "font-size": 12.5,
        fill: cssv("--ink-3") }, g);
      ds.textContent = r.desc;

      const big = el("text", { x: tx, y: r.y + 148, "font-size": 30,
        "font-weight": 500, fill: cssv(r.accent), "font-family": cssv("--mono") }, g);
      big.textContent = counts[r.id] || "—";
      const unit = el("text", { x: tx + String(counts[r.id] || "—").length * 18 + 8,
        y: r.y + 148, "font-size": 12, fill: cssv("--ink-3") }, g);
      unit.textContent = r.unit || "";

      /* 門：牆上的開口 + 開門弧線。是真的洞，不是按鈕—— */
      const dx = r.door.x, dy = r.door.y;
      const inward = r.side === "L" ? 1 : -1;
      el("rect", { x: dx - 2, y: dy - 26, width: 4, height: 52,
        fill: cssv("--paper") }, g);
      el("path", { d: `M${dx} ${dy - 26} a26 26 0 0 ${inward > 0 ? 1 : 0} ${26 * inward} 26`,
        fill: "none", stroke: cssv("--ink-4"), "stroke-width": 1.4,
        "stroke-dasharray": "3 3", class: "room-door" }, g);
      const en = el("text", { x: dx + 34 * inward, y: dy + 4, "font-size": 11,
        fill: cssv("--seal"), class: "room-enter",
        "text-anchor": inward > 0 ? "start" : "end" }, g);
      en.textContent = "進入 ›";

      g.addEventListener("click", () => enter(r.id));
      g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); enter(r.id); }
      });
    });

    ROOMS.forEach((r) => { roomArt(r.g, r); kennelArt(r.g, r); });
    drawBoard(rest);

    /* 守護犬：獨立一層，不受縮放影響 */
    const dog = el("g", { id: "dog" }, svg);
    dogArt(dog);
    moveDog(state.dogX, state.dogY, false);

    /* 提示泡泡跟著守護犬。用 SVG 而不是 HTML，是因為它要跟平面圖用同一套
       座標，否則視窗一縮放兩者就對不上。 */
    const tip = el("g", { id: "dogtip" }, svg);
    el("rect", { x: 0, y: 0, width: 196, height: 40, rx: 9,
      fill: cssv("--panel"), stroke: cssv("--rule"), "stroke-width": 1.2 }, tip);
    const l1 = el("text", { x: 14, y: 18, "font-size": 11.5,
      fill: cssv("--ink-2") }, tip);
    l1.textContent = "滑鼠帶我在中庭走，";
    const l2 = el("text", { x: 14, y: 32, "font-size": 11.5,
      fill: cssv("--ink-2") }, tip);
    l2.textContent = "點一間房我就進去。";
    positionTip();
  }

  function positionTip() {
    const t = $("dogtip");
    if (!t) return;
    // 泡泡固定在守護犬右邊；貼到右牆時翻到左邊，免得被中庭牆切掉
    const flip = state.dogX > 700;
    const x = flip ? state.dogX - 220 : state.dogX + 26;
    t.setAttribute("transform", `translate(${x} ${state.dogY - 52})`);
  }

  /* 每一室的室內景。空房間看起來像沒做完，而這幾筆插圖同時也在回答
     「這間房裡有什麼」——比再寫一行說明文字有效。 */
  function roomArt(g, r) {
    const x = r.x + 34, y = r.y;
    if (r.id === "map") {
      const m = el("g", { transform: `translate(${r.x + 300} ${y + 128}) scale(.86)` }, g);
      el("path", { d: "M44 36 L96 22 L146 44 L188 32 L216 74 L204 134 L150 168 L92 156 L52 110 Z",
        fill: cssv("--c2"), stroke: cssv("--edge"), "stroke-width": 1.5 }, m);
      el("path", { d: "M96 22 L146 44 L144 92 L96 84 Z", fill: cssv("--c3") }, m);
      el("path", { d: "M92 156 L150 168 L146 116 L98 108 Z", fill: cssv("--c4") }, m);
      el("circle", { cx: 112, cy: 112, r: 4.4, fill: cssv("--k4") }, m);
      el("circle", { cx: 126, cy: 130, r: 4.4, fill: cssv("--k4") }, m);
      el("circle", { cx: 112, cy: 112, r: 9, fill: "none", stroke: cssv("--seal"),
        "stroke-width": 1.8 }, m);
      return;
    }
    if (r.id === "data") {
      // 檔案櫃立面 + 三類資料
      const c = el("g", {}, g);
      el("rect", { x, y: y + 176, width: 124, height: 132, fill: cssv("--panel"),
        stroke: cssv("--edge"), "stroke-width": 1.6 }, c);
      [210, 244, 278].forEach((oy) => el("line", { x1: x, y1: y + oy,
        x2: x + 124, y2: y + oy, stroke: cssv("--edge"), "stroke-width": 1.2 }, c));
      const books = [["--np", 4, 184], ["--pub", 3, 218], ["--good", 2, 252]];
      books.forEach(([tok, n, oy]) => {
        for (let i = 0; i < n; i += 1) {
          el("rect", { x: x + 8 + i * 10, y: y + oy, width: 7, height: 20,
            fill: cssv(tok) }, c);
        }
      });
      const rows = [["--np", "非營利財報", "132"], ["--pub", "公校決算書", "30"],
        ["--good", "外部快照", "7"]];
      rows.forEach(([tok, label, n], i) => {
        const oy = y + 192 + i * 34;
        el("circle", { cx: x + 166, cy: oy, r: 4, fill: cssv(tok) }, c);
        const t = el("text", { x: x + 180, y: oy + 5, "font-size": 12.5,
          fill: cssv("--ink-2") }, c);
        t.textContent = label;
        const v = el("text", { x: x + 400, y: oy + 5, "font-size": 12.5,
          fill: cssv("--ink-3"), "text-anchor": "end",
          "font-family": cssv("--mono") }, c);
        v.textContent = n;
      });
      const note = el("text", { x: x + 180, y: y + 296, "font-size": 11,
        fill: cssv("--ink-4") }, c);
      note.textContent = "每個數字可回溯到來源頁碼";
      return;
    }
    if (r.id === "voice") {
      const chips = [["新聞", true], ["PTT", true], ["評鑑", true], ["Threads", false]];
      let cx = x;
      chips.forEach(([label, live]) => {
        const w = label.length > 3 ? 78 : 60;
        el("rect", { x: cx, y: y + 168, width: w, height: 22, rx: 11, fill: "none",
          stroke: cssv(live ? "--good" : "--rule"), "stroke-width": 1.2,
          "stroke-dasharray": live ? "" : "3 3" }, g);
        const t = el("text", { x: cx + w / 2, y: y + 183, "font-size": 11,
          fill: cssv(live ? "--good" : "--ink-4"), "text-anchor": "middle" }, g);
        t.textContent = label;
        cx += w + 8;
      });
      /* 左下角那一塊是狗窩的地盤（每一室都有），內容一律讓開。
         輿情室只有 234 高，這行字原本就壓在狗窩上。 */
      const w = el("text", { x: x + 72, y: y + 210, "font-size": 11,
        fill: cssv("--warn") }, g);
      w.textContent = "⚠ 提前量 0，定位是即時監看不是預測";
      return;
    }
    if (r.id === "backtest") {
      /* 長條放在大數字的**右邊**，不是下面：這一間只有 233 高，
         標題三行加大數字已經吃到 y+148，長條再往下就會壓到數字（第一版如此）。 */
      const bx = x + 292, base = y + 168, cap = 62;
      const bars = [[40, "--c2"], [52, "--c3"], [46, "--c3"], [cap, "--c4"]];
      bars.forEach(([h, tok], i) => {
        el("rect", { x: bx + i * 44, y: base - h, width: 36, height: h,
          fill: cssv(tok) }, g);
        const t = el("text", { x: bx + i * 44 + 18, y: base + 15, "font-size": 10,
          fill: cssv("--ink-4"), "text-anchor": "middle",
          "font-family": cssv("--mono") }, g);
        t.textContent = String(2021 + i);
      });
      return;
    }
    if (r.id === "letters") {
      /* 紙疊也擺右邊並縮小。第一版從 y+176 起、94 高，房間只到 y+233——
         紙疊直接掉到房間外面去了。 */
      const c = el("g", {}, g);
      const px = x + 300, py = y + 116;
      [0, 7, 14].forEach((o) => el("rect", { x: px + o, y: py - o, width: 58,
        height: 74, fill: cssv("--panel"), stroke: cssv("--edge"),
        "stroke-width": 1.3 }, c));
      [0, 11, 22, 38].forEach((o, i) => el("line", { x1: px + 26,
        y1: py + 4 + o, x2: px + 26 + (i === 2 ? 26 : 40), y2: py + 4 + o,
        stroke: cssv("--rule"), "stroke-width": 1.3 }, c));
      el("circle", { cx: px + 60, cy: py + 62, r: 10, fill: "none",
        stroke: cssv("--seal"), "stroke-width": 1.5 }, c);
      const k = el("text", { x: px + 60, y: py + 66, "font-size": 8.5,
        fill: cssv("--seal"), "text-anchor": "middle" }, c);
      k.textContent = "核";
      // 大數字已經寫了 143，這裡只補它的意思，不重複數字
      const n = el("text", { x, y: y + 176, "font-size": 11.5,
        fill: cssv("--ink-3") }, g);
      n.textContent = "全數通過機械驗證；未通過的草稿不會寫出來";
    }
  }

  /* 中庭立牌：回答「今天要做什麼」 */
  function drawBoard(parent) {
    const g = el("g", {}, parent);
    el("rect", { x: 580, y: 150, width: 280, height: 152, rx: 5,
      class: "plan-card", fill: cssv("--panel"), stroke: cssv("--rule"),
      "stroke-width": 1.4 }, g);
    const put = (x, y, s, size, fill, weight) => {
      const t = el("text", { x, y, "font-size": size, fill,
        "font-weight": weight || 400 }, g);
      t.textContent = s;
      return t;
    };
    put(602, 178, "今天要做什麼", 10.5, cssv("--ink-4")).setAttribute("letter-spacing", 1.6);
    put(602, 208, "名單已排好，20 家。", 15, cssv("--ink"), 600);
    put(602, 232, "其中", 13, cssv("--ink-2"));
    put(636, 232, "4 家", 13, cssv("--seal"), 600);
    put(672, 232, "帶高嚴重度財務發現，", 13, cssv("--ink-2"));
    put(602, 252, "建議優先排訪。", 13, cssv("--ink-2"));
    el("line", { x1: 602, y1: 268, x2: 838, y2: 268, stroke: cssv("--rule"),
      "stroke-width": 1 }, g);
    put(602, 288, "前 100 名命中率 2.29× · AUC 0.658", 11, cssv("--ink-3"));
  }

  /* 每一室左下角的狗窩。中庭看得到它，進房後守護犬就躺在對應的位置——
     牠是「跑進那一格」，不是「消失再出現」。 */
  function kennelArt(g, r) {
    const kx = r.x + 22, ky = r.y + r.h - 52;
    const k = el("g", { opacity: .55 }, g);
    el("path", { d: `M${kx} ${ky + 34} v-18 l14 -13 14 13 v18 z`,
      fill: cssv("--panel"), stroke: cssv("--ink-4"), "stroke-width": 1.4,
      "stroke-linejoin": "round" }, k);
    el("path", { d: `M${kx + 9} ${ky + 34} v-11 a5 5 0 0 1 10 0 v11 z`,
      fill: cssv("--sunk"), stroke: cssv("--ink-4"), "stroke-width": 1.2 }, k);
    const t = el("text", { x: kx + 36, y: ky + 32, "font-size": 9.5,
      fill: cssv("--ink-4"), "letter-spacing": 1.2 }, k);
    t.textContent = "狗窩";
  }

  function dogArt(g) {
    const ink = cssv("--edge"), paper = cssv("--paper"), seal = cssv("--seal");
    el("ellipse", { cx: 22, cy: 37, rx: 12.5, ry: 2.8, fill: cssv("--ink"),
      opacity: .15 }, g);
    el("path", { d: "M9 19c0-2.4 1.4-3.8 3.2-2.9L16 18h12l3.8-1.9C33.6 15.2 35 16.6 35 19v9c0 3.3-2.7 6-6 6H15c-3.3 0-6-2.7-6-6z",
      fill: cssv("--panel"), stroke: ink, "stroke-width": 1.6,
      "stroke-linejoin": "round" }, g);
    el("path", { d: "M11 17c-1.6-3.2-1.2-6.4.6-6.9 1.7-.5 3.6 1.4 4.4 4.3z",
      fill: paper, stroke: ink, "stroke-width": 1.5 }, g);
    el("path", { d: "M33 17c1.6-3.2 1.2-6.4-.6-6.9-1.7-.5-3.6 1.4-4.4 4.3z",
      fill: paper, stroke: ink, "stroke-width": 1.5 }, g);
    el("circle", { cx: 17.5, cy: 24, r: 1.7, fill: ink }, g);
    el("circle", { cx: 26.5, cy: 24, r: 1.7, fill: ink }, g);
    el("path", { d: "M20.4 28.6h3.2", stroke: ink, "stroke-width": 1.6,
      "stroke-linecap": "round" }, g);
    el("circle", { cx: 22, cy: 26.6, r: 1.5, fill: seal }, g);
    el("path", { d: "M35 22 q6 -3 5 -9", stroke: ink, "stroke-width": 1.6,
      fill: "none", "stroke-linecap": "round", id: "dogtail" }, g);
  }

  function moveDog(x, y, animate) {
    state.dogX = x; state.dogY = y;
    const d = $("dog");
    if (!d) return;
    d.classList.toggle("walk", !!animate);
    // 22/30 是狗身中心：位移以牠的腳為準，不然走到門口會半個身體在牆裡
    d.setAttribute("transform", `translate(${x - 22} ${y - 30})`);
    positionTip();
  }

  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

  /* ⚠️ 這就是「不管怎樣動都不能進去房間」那一條。
     游標在哪裡都行，守護犬的**目標點**永遠被夾回中庭。 */
  function clampToAtrium(x, y) {
    return [clamp(x, WALK.x0, WALK.x1), clamp(y, WALK.y0, WALK.y1)];
  }

  function onPointerMove(e) {
    if (state.busy || state.where !== "lobby" || reduced) return;
    const svg = $("plan");
    const r = svg.getBoundingClientRect();
    // 螢幕座標 → viewBox 座標。preserveAspectRatio=meet 會留黑邊，要自己還原。
    const scale = Math.min(r.width / W, r.height / H);
    const ox = (r.width - W * scale) / 2, oy = (r.height - H * scale) / 2;
    const vx = (e.clientX - r.left - ox) / scale;
    const vy = (e.clientY - r.top - oy) / scale;
    const [x, y] = clampToAtrium(vx, vy);
    moveDog(x, y, true);
  }

  /* ── 進房 ─────────────────────────────────────────── */
  /* FLIP：量出那一間在螢幕上的實際矩形，算出把它放大到填滿舞台需要的
     位移與倍率，交給 CSS transform。用螢幕像素而不是 viewBox 單位，
     是因為 preserveAspectRatio 會留邊，兩套座標不是等比的。 */
  function zoomTransform(r) {
    const stage = $("planstage");
    const rect = r.g.getBoundingClientRect();
    const box = stage.getBoundingClientRect();
    if (!rect.width || !box.width) return "";
    const rx = rect.left - box.left, ry = rect.top - box.top;
    const sc = Math.max(box.width / rect.width, box.height / rect.height);
    const tx = box.width / 2 - sc * (rx + rect.width / 2);
    const ty = box.height / 2 - sc * (ry + rect.height / 2);
    return `translate(${tx}px, ${ty}px) scale(${sc})`;
  }

  function enter(id) {
    if (state.busy) return;
    const r = ROOMS.find((x) => x.id === id);
    if (!r) return;
    state.busy = true;
    state.room = r;

    if (reduced) { showRoom(r); return; }

    const lobby = $("lobby");
    const tip = $("dogtip");
    if (tip) tip.style.opacity = "0";
    // ① 先跑到門口
    moveDog(r.door.x + (r.side === "L" ? -30 : 30), r.door.y, true);

    // ② 房間放大、其餘淡出
    setTimeout(() => {
      lobby.classList.add("zooming");
      // 先量、再等一幀才套 transform：量測會觸發 layout，跟套用擠在同一幀
      // 就是那一格掉幀。
      const t = zoomTransform(r);
      requestAnimationFrame(() => { $("planstage").style.transform = t; });
    }, 240);

    // ③ 走進去，坐到狗窩
    setTimeout(() => moveDog(KENNEL.x, KENNEL.y, true), 620);

    // ④ 室內介面淡入
    setTimeout(() => showRoom(r), 880);
  }

  /* app 不用 hidden：Leaflet 在 display:none 裡量到的是 0×0，一旦這樣初始化
     過，之後 invalidateSize() 也救不回正確的圖磚邊界。所以 app 從頭到尾都活著，
     中庭只是蓋在它上面（position:fixed, z-index 40）。 */
  /* 室頭 + 版面。五間房走同一條路，差別只在 `r.map` 與 `r.pane`——
     每一室各寫一次進場邏輯，第六間房出現時就會有一間忘了同步。 */
  function dressRoom(r) {
    $("rh-no").textContent = r.no;
    $("rh-name").textContent = r.name;
    $("rh-desc").textContent = r.desc;
    $("rh-src").textContent = r.src || "";
    $("roombody").classList.toggle("no-map", !r.map);
    lieDown();
  }

  /* 守護犬躺進這一室的狗窩。每次進房重播一次入場，
     因為「牠跟著我進來了」是這個設計要傳達的東西。 */
  function lieDown() {
    const box = document.querySelector(".rail-kennel .kennel-dog");
    if (!box) return;
    box.style.animation = "none";
    void box.offsetWidth;          // 強制重排，動畫才會重播
    box.style.animation = "";
  }

  function showRoom(r) {
    setRail(r);
    dressRoom(r);
    if (window.SW && window.SW.showPane) window.SW.showPane(r.pane);
    $("lobby").hidden = true;
    state.where = "room";
    state.busy = false;
    /* 中庭蓋著的期間版面可能變過（收合助理欄、改視窗大小），重新量一次。
       光 invalidateSize() 不夠：它保留原本的中心與縮放，容器變寬就會多露出
       一片海。要連 fitNTPC() 一起跑，畫面才會回到「剛好是新北」。 */
    if (r.map && window.SW && window.SW.state && window.SW.state.map) {
      setTimeout(() => {
        window.SW.state.map.invalidateSize();
        if (window.SW.fitNTPC) window.SW.fitNTPC();
      }, 40);
    }
  }

  function back() {
    if (state.busy || state.where !== "room") return;
    state.busy = true;
    const lobby = $("lobby");
    lobby.hidden = false;
    state.where = "lobby";
    if (reduced) { state.busy = false; return; }
    // 房間縮回原位，守護犬走回中庭中央
    requestAnimationFrame(() => {
      $("planstage").style.transform = "";
      lobby.classList.remove("zooming");
      const tip2 = $("dogtip");
      if (tip2) tip2.style.opacity = "";
      moveDog(720, 560, true);
      setTimeout(() => { state.busy = false; }, 620);
    });
  }

  /* ── 左側樓層索引 ──────────────────────────────────── */
  function setRail(current) {
    const list = $("rail-list");
    if (!list) return;
    list.textContent = "";
    ROOMS.forEach((r) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "rail-item";
      if (r.id === current.id) b.setAttribute("aria-current", "true");
      b.innerHTML = `<span class="rail-no">${r.no}</span>${r.name}`
        + `<span class="rail-n">${counts[r.id] || ""}</span>`;
      b.addEventListener("click", () => {
        if (r.id === current.id) return;
        state.room = r;
        setRail(r);
        dressRoom(r);
        if (window.SW && window.SW.showPane) window.SW.showPane(r.pane);
        if (r.map && window.SW && window.SW.state.map) {
          setTimeout(() => {
            window.SW.state.map.invalidateSize();
            if (window.SW.fitNTPC) window.SW.fitNTPC();
          }, 40);
        }
      });
      list.appendChild(b);
    });
  }

  /* ── 身分 ─────────────────────────────────────────── */
  /* 身分鈕在中庭與室內各有一顆，所以用 class 一次填滿全部。
     用 id 的話得在兩個 header 放同一個 id，那是重複 id。 */
  const esc = (t) => String(t ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function paintWho() {
    const u = (window.AgentAuth && window.AgentAuth.user) || null;
    document.querySelectorAll(".who-pod").forEach((pod) => {
      pod.hidden = !u;
      if (!u) return;
      pod.querySelector(".who-av").textContent = (u.name || "?").slice(0, 1);
      pod.querySelector(".who-name").textContent = u.name || u.email || "";
      pod.querySelector(".who-role").textContent = u.role || "";
    });
    if (!u) return;
    const towns = (u.towns || []).map((t) => `<span>${esc(t)}</span>`).join("");
    const html =
      `<div class="row"><span>帳號</span><b>${esc(u.email) || "—"}</b></div>`
      + `<div class="row"><span>角色</span><b>${esc(u.role) || "—"}</b></div>`
      + `<div class="row"><span>單位</span><b>${esc(u.unit) || "—"}</b></div>`
      + `<div class="row" style="display:block"><span>負責行政區</span>`
      + `<div class="towns">${towns || "<span>未指定</span>"}</div></div>`;
    document.querySelectorAll(".whopop-body").forEach((b) => { b.innerHTML = html; });
  }

  /* 大廳那一顆自己管開關：app 的那一顆由 app.js 接（它要跟花費、說明互斥）。 */
  function bindLobbyWho() {
    const pod = $("lbwho"), pop = $("lbwhopop");
    if (!pod || !pop) return;
    pod.addEventListener("click", (e) => {
      e.stopPropagation();
      pop.hidden = !pop.hidden;
      pod.setAttribute("aria-expanded", String(!pop.hidden));
    });
    document.addEventListener("click", (e) => {
      if (!e.target.closest("#lbwhopop") && !e.target.closest("#lbwho")) {
        pop.hidden = true;
        pod.setAttribute("aria-expanded", "false");
      }
    });
  }

  /* ── 啟動 ─────────────────────────────────────────── */
  let counts = {};
  function start(payload) {
    const pts = (payload && payload.points) || [];
    const rt = (payload && payload.realtime) || {};
    counts = {
      map: pts.length ? pts.length.toLocaleString("en-US") : "—",
      data: "162",
      voice: String(((rt.by_institution && Object.keys(rt.by_institution).length) || 0)
        ? Object.values(rt.by_institution).reduce((n, v) => n + v.length, 0) : 31),
      backtest: "2.29×",
      letters: "143",
    };
    ROOMS[0].unit = "園"; ROOMS[1].unit = "份"; ROOMS[2].unit = "則";
    ROOMS[3].unit = ""; ROOMS[4].unit = "份";
    drawPlan(counts);
    paintWho();
    bindLobbyWho();
    fillDataRoom();
    $("plan").addEventListener("pointermove", onPointerMove);
    const b = $("rail-back");
    if (b) b.addEventListener("click", back);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.where === "room" && !state.busy) back();
    });
  }

  /* 資料室的磁磚。數字全部是實際盤點出來的，不是佔位——
     一間「待設計」的房間如果連現況都說不清楚，接手的人得從頭盤一次。 */
  function fillDataRoom() {
    const grid = $("data-grid");
    if (!grid) return;
    const tiles = [
      ["非營利園財報", "132", "已抽取並進版控"],
      ["公校決算書", "30", "座標抽取，零模型成本"],
      ["原始 PDF", "162", "data/raw，不進版控"],
      ["法遵檢核發現", "31", "其中 8 項高嚴重度"],
      ["外部快照", "7", "含裁罰、登記、界線"],
      ["文件索引", "148", "問題進，檔案與頁碼出"],
    ];
    grid.innerHTML = tiles.map(([k, v, note]) =>
      `<div class="todo-tile"><span class="k">${k}</span>`
      + `<span class="v">${v}</span><span class="s">${note}</span></div>`).join("");
  }

  window.Lobby = { start, enter, back, paintWho, get where() { return state.where; } };
})();
