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

  /* 每一室。`pane` 對應既有 app 的分頁，`door` 是中庭那一側的開口中心。 */
  /* `map: true` 才會顯示地圖那一欄。地圖跟輿情、資料、建議書沒有關係，
     擺在那裡只是把畫面切碎——這是使用者明確要求的。
     `src` 是室頭右側的來源膠囊：每一室都要說得出自己的數字哪來的。 */
  /* 名字直接講這一格做什麼，不再是「○○室」。
     五個名字連起來就是一次稽查的動線：先看現況、再看手上有什麼資料、
     接住外面進來的訊號、驗證我們的判斷方法站不站得住、最後產出對外的回覆。
     id 與 pane 不動——那是程式的接縫，改名只改給人看的字。 */
  const ROOMS = [
    { id: "map", no: "01", name: "全市監看", pane: "list", accent: "--room-map", map: true,
      x: 40, y: 30, w: 505, h: 350, door: { x: 545, y: 210 }, side: "L",
      desc: "1,213 園的現況總覽與即時監測",
      src: "registry · 快照 2026-08-10" },
    { id: "data", no: "02", name: "文件控管", pane: "data", accent: "--room-data",
      x: 40, y: 380, w: 505, h: 350, door: { x: 545, y: 550 }, side: "L",
      desc: "我們掌握的書面資料：原件、逐頁抽取、依表單分類",
      src: "data/extracted · 頁級抽取" },
    { id: "voice", no: "03", name: "輿情蒐集", pane: "scan", accent: "--room-voice",
      x: 895, y: 30, w: 505, h: 234, door: { x: 895, y: 150 }, side: "R",
      desc: "Threads、新聞、PTT 與民眾 @標註通報；未查證線索",
      src: "threads · realtime · 讀庫即時" },
    { id: "backtest", no: "04", name: "分析驗證", pane: "timeline", accent: "--room-back",
      x: 895, y: 264, w: 505, h: 233, door: { x: 895, y: 380 }, side: "R",
      desc: "掌握哪些數據、跟什麼比對、方法怎麼驗證",
      src: "timeline · 2021–2024" },
    { id: "letters", no: "05", name: "答詢擬稿", pane: "memos", accent: "--room-letters",
      x: 895, y: 497, w: 505, h: 233, door: { x: 895, y: 613 }, side: "R",
      desc: "把通報與發現整理成回覆初稿與質詢答復",
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
    /* 兩層：牆與中庭（放大時整層收掉）、房間（放大時只留目標那一間）。
       第一版全部塞在同一層，放大後外牆那條 3px 的線被放大成十幾 px 的黑框
       橫過整個畫面——看起來就是「黑框超出房間」。 */
    const rest = el("g", { id: "planbg" }, zoom);
    const roomLayer = el("g", { id: "planrooms" }, zoom);

    el("rect", { x: 40, y: 30, width: 1360, height: 700, rx: 4,
      fill: cssv("--panel"), stroke: cssv("--edge"), "stroke-width": 3 }, rest);
    /* ⚠️ 不可寫成 el("rect", {...ATRIUM})。ATRIUM 的鍵是 w／h，而 SVG <rect>
       要的是 width／height——那樣產出的是 w="350" h="700"、寬高都是 0 的空矩形，
       中庭的地板從第一版起就沒有畫出來過（看起來一直是全白的走廊）。 */
    yardArt(rest);
    el("line", { x1: 545, y1: 30, x2: 545, y2: 730, stroke: cssv("--edge"),
      "stroke-width": 2.2 }, rest);
    el("line", { x1: 895, y1: 30, x2: 895, y2: 730, stroke: cssv("--edge"),
      "stroke-width": 2.2 }, rest);
    const at = el("text", { x: 720, y: 62, "font-size": 11, fill: cssv("--ink-4"),
      "letter-spacing": 3.4, "text-anchor": "middle" }, rest);
    at.textContent = "中庭遊戲場";

    ROOMS.forEach((r) => {
      // 每一室自己一個 g，進房時把它從 rest 提到 zoom 底下單獨留著
      const g = el("g", { class: "room-hit", "data-room": r.id,
        role: "button", tabindex: "0",
        // 進場時五個入口依序亮一下。第一次看到這張平面圖的人不會知道
        // 整格都可以點，而一個只在 hover 才出現的提示要先猜到才找得到。
        style: `--cue-delay:${.5 + ROOMS.indexOf(r) * .16}s`,
        "aria-label": `${r.no} ${r.name}，${r.desc}` }, roomLayer);
      r.g = g;
      el("rect", { x: r.x, y: r.y, width: r.w, height: r.h, class: "room-fill",
        rx: 6, fill: cssv(r.accent), opacity: .15 }, g);
      /* 門楣：房間頂端一條實心色帶。五間室在平面圖上原本只差一層 7% 的淡底，
         遠看幾乎一樣；一條實心帶是最省版面又最分得開的識別。
         放大進房時它正好變成室頭那條線的延伸。 */
      el("rect", { x: r.x, y: r.y, width: r.w, height: 8, rx: 4,
        fill: cssv(r.accent) }, g);
      el("rect", { x: r.x, y: r.y, width: r.w, height: r.h, fill: "none",
        rx: 6, stroke: cssv(r.accent), "stroke-width": 2.2, opacity: .62,
        // 放大時線寬不跟著長。沒有它，2.2px 在 2.8 倍下會變成 6px 的粗黑邊。
        "vector-effect": "non-scaling-stroke" }, g);

      const tx = r.x + 34;
      /* 門牌：教室門口那塊號碼牌。編號從 12px 的小字變成牌面上的主角——
         遠看先數得出有五格，再讀名字。牌子的顏色就是這一格的顏色。 */
      const py = r.y + 28;
      el("rect", { x: tx, y: py, width: 54, height: 54, rx: 15,
        fill: cssv(r.accent) }, g);
      const no = el("text", { x: tx + 27, y: py + 37, "font-size": 27,
        "font-weight": 700, fill: cssv("--panel"), "text-anchor": "middle",
        "font-family": cssv("--mono") }, g);
      no.textContent = r.no;

      const nx = tx + 70;
      const nm = el("text", { x: nx, y: py + 26, "font-size": 25,
        "font-weight": 600, fill: cssv("--ink"), "letter-spacing": 2 }, g);
      nm.textContent = r.name;
      const ds = el("text", { x: nx, y: py + 50, "font-size": 12.5,
        fill: cssv("--ink-3") }, g);
      ds.textContent = r.desc;

      const big = el("text", { x: tx, y: r.y + 152, "font-size": 30,
        "font-weight": 500, fill: cssv(r.accent), "font-family": cssv("--mono") }, g);
      big.textContent = counts[r.id] || "—";
      const unit = el("text", { x: tx + String(counts[r.id] || "—").length * 18 + 8,
        y: r.y + 152, "font-size": 12, fill: cssv("--ink-3") }, g);
      unit.textContent = r.unit || "";

      /* 門：牆上的開口 + 開門弧線。是真的洞，不是按鈕—— */
      const dx = r.door.x, dy = r.door.y;
      const inward = r.side === "L" ? 1 : -1;
      el("rect", { x: dx - 2, y: dy - 26, width: 4, height: 52,
        fill: cssv("--yard") }, g);
      /* 門墊：鋪在中庭那一側，顏色就是這一室的顏色。幼兒園每間教室門口
         都有一塊自己的墊子，這裡它同時是「哪一間在哪裡」的第四個色點。 */
      el("rect", { x: inward > 0 ? dx + 5 : dx - 27, y: dy - 19, width: 22,
        height: 38, rx: 5, class: "room-mat", fill: cssv(r.accent) }, g);
      el("path", { d: `M${dx} ${dy - 26} a26 26 0 0 ${inward > 0 ? 1 : 0} ${26 * inward} 26`,
        fill: "none", stroke: cssv(r.accent), "stroke-width": 1.6,
        "stroke-dasharray": "3 3", class: "room-door" }, g);
      /* 「點這裡進去」原本只有滑過才看得見，等於要先猜到才找得到。
         改成常駐但安靜：平常半透明，滑過才實心。 */
      const en = el("text", { x: dx + 34 * inward, y: dy + 4, "font-size": 12,
        fill: cssv(r.accent), "font-weight": 600, class: "room-enter",
        "text-anchor": inward > 0 ? "start" : "end" }, g);
      en.textContent = inward > 0 ? "進入 ›" : "‹ 進入";

      g.addEventListener("click", () => enter(r.id));
      g.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); enter(r.id); }
      });
    });

    ROOMS.forEach((r) => { roomArt(r.g, r); kennelArt(r.g, r); });

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
      class: "tip-l1", fill: cssv("--ink-2") }, tip);
    const l2 = el("text", { x: 14, y: 32, "font-size": 11.5,
      class: "tip-l2", fill: cssv("--ink-2") }, tip);
    [l1.textContent, l2.textContent] = DOG_LINES[0];
    positionTip();
    startTalking();

    /* 入口提示只播一次：播完就把 cue 拿掉，之後從房間回中庭不會再閃。
       最後一格的延遲 .5 + 4×.16 = 1.14s，兩輪 2.2s，取 3.6s 收尾。 */
    const lb = $("lobby");
    if (lb) {
      lb.classList.add("cue");
      setTimeout(() => lb.classList.remove("cue"), 3600);
    }
  }


  /* 守護犬的台詞。輪播而不是只說一句：中庭是一個會停留的畫面，
     一句固定的操作說明看第三次就變成雜訊。

     ⚠️ 台詞受同一條用詞界線約束：不可宣稱任何機構有問題，也不可把
     「建議查核的優先序」講成「抓到了」。牠可以可愛，不可以下判斷。 */
  const DOG_LINES = [
    ["今天也在巡邏，", "有事叫我一聲。"],
    ["五個分區都開著，", "想先去哪一個？"],
    ["每個數字我都記得", "是從哪一頁來的。"],
    ["我們排的是查核順序，", "不是誰有罪。"],
    ["慢慢看，", "我不會催你。"],
    ["不管進哪一格，", "我都跟著你。"],
    ["查不到的時候，", "我會說資料不足。"],
    ["中庭風有點大，", "我先趴一下。"],
  ];

  let tipIx = 0;
  let tipTimer = null;

  function sayNext() {
    const t = $("dogtip");
    // 進房途中或已經在室內就不要換詞：那時候泡泡正在淡出，換了會閃一下。
    if (!t || state.where !== "lobby" || state.busy) return;
    t.style.opacity = "0";
    setTimeout(() => {
      if (state.where !== "lobby" || state.busy) return;
      tipIx = (tipIx + 1) % DOG_LINES.length;
      const [a, b] = DOG_LINES[tipIx];
      const e1 = t.querySelector(".tip-l1");
      const e2 = t.querySelector(".tip-l2");
      if (e1) e1.textContent = a;
      if (e2) e2.textContent = b;
      t.style.opacity = "";
    }, 220);
  }

  function startTalking() {
    stopTalking();
    tipTimer = setInterval(sayNext, 7000);
  }

  function stopTalking() {
    if (tipTimer) { clearInterval(tipTimer); tipTimer = null; }
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
      const w = el("text", { x: x + 44, y: y + 208, "font-size": 11.5,
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



  /* 中庭遊戲場。
     這一室監理的對象就是幼兒園，所以中庭畫成園裡的中庭是最省力也最貼題的
     幼兒園化——不必重畫五間房，也不必把平面圖換成插畫。

     分寸拿捏：畫的是**場地裡真的會有的東西**（草皮、樹、跳房子、沙坑），
     不是卡通角色或高彩度色塊。交給教育局的工具，幼兒園的辨識度要來自
     「空間裡有什麼」，而不是把介面變成兒童頻道。

     全部畫在 planbg 層：進房放大時整層 visibility:hidden，不參與過渡，
     所以不會影響已經調到 0 丟幀的那段動畫。也都不是 .room-hit，不吃點擊。 */
  function yardArt(parent) {
    const g = el("g", { id: "yard" }, parent);
    const cx = ATRIUM.x + ATRIUM.w / 2;   // 720

    // 地面
    el("rect", { x: ATRIUM.x, y: ATRIUM.y, width: ATRIUM.w, height: ATRIUM.h,
      fill: cssv("--yard") }, g);
    // 草皮：鋪在下半段，守護犬平常待的位置就在上面
    el("rect", { x: ATRIUM.x + 21, y: 430, width: ATRIUM.w - 42, height: 272,
      rx: 30, fill: cssv("--turf") }, g);
    el("rect", { x: ATRIUM.x + 21, y: 430, width: ATRIUM.w - 42, height: 272,
      rx: 30, fill: "none", stroke: cssv("--turf-2"), "stroke-width": 2 }, g);

    // 跳房子：六格，單雙格交錯。地面標線，所以只有描邊沒有填色，
    // 守護犬走過去不會被它蓋住。
    const cell = 40, gap = 2;
    let y = 142;
    [[0], [0], [-1, 1], [0], [-1, 1], [0]].forEach((cols) => {
      cols.forEach((c) => {
        el("rect", { x: cx + c * (cell / 2 + gap) - cell / 2, y,
          width: cell, height: cell, rx: 4, fill: "none",
          stroke: cssv("--yard-line"), "stroke-width": 2.4 }, g);
      });
      y += cell + gap + 2;
    });

    // 兩棵樹：一棵在上、一棵在右下，把空的兩角收起來
    const tree = (tx, ty, sc) => {
      const t = el("g", { transform: `translate(${tx} ${ty}) scale(${sc})` }, g);
      el("rect", { x: -4, y: 10, width: 8, height: 26, rx: 3,
        fill: cssv("--bark") }, t);
      el("circle", { cx: 0, cy: 0, r: 26, fill: cssv("--leaf"), opacity: .82 }, t);
      el("circle", { cx: -17, cy: 9, r: 17, fill: cssv("--leaf"), opacity: .7 }, t);
      el("circle", { cx: 17, cy: 8, r: 15, fill: cssv("--leaf"), opacity: .7 }, t);
      return t;
    };
    tree(ATRIUM.x + 44, 104, 1);
    tree(ATRIUM.x + ATRIUM.w - 46, 636, .8);

    // 沙坑
    const sb = el("g", {}, g);
    el("rect", { x: ATRIUM.x + 26, y: 620, width: 92, height: 74, rx: 10,
      fill: cssv("--yard-line"), opacity: .75 }, sb);
    el("rect", { x: ATRIUM.x + 33, y: 627, width: 78, height: 60, rx: 7,
      fill: cssv("--yard") }, sb);
    [[22, 18], [46, 30], [62, 16], [34, 44], [60, 46]].forEach(([ox, oy]) => {
      el("circle", { cx: ATRIUM.x + 33 + ox, cy: 627 + oy, r: 2,
        fill: cssv("--yard-line") }, sb);
    });
  }

  /* 每一室左下角的狗窩。中庭看得到它，進房後守護犬就躺在對應的位置——
     牠是「跑進那一格」，不是「消失再出現」。 */
  function kennelArt(g, r) {
    // 貼著左下角，並且比第一版小一號：它是這一室的固定家具，不是重點，
    // 站太靠中間就會跟內容搶位置（輿情室那條警語被壓過一次）。
    const kx = r.x + 16, ky = r.y + r.h - 46;
    const k = el("g", { opacity: .45 }, g);
    el("path", { d: `M${kx} ${ky + 28} v-15 l12 -11 12 11 v15 z`,
      fill: cssv("--panel"), stroke: cssv("--ink-4"), "stroke-width": 1.3,
      "stroke-linejoin": "round" }, k);
    el("path", { d: `M${kx + 8} ${ky + 28} v-9 a4 4 0 0 1 8 0 v9 z`,
      fill: cssv("--sunk"), stroke: cssv("--ink-4"), "stroke-width": 1.1 }, k);
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
    stopTalking();

    /* 順序是：**先走進去躺好，房間才放大。**
       第一版是邊走邊放大，兩件事同時動，看起來像房間把牠吸進去。
       先走完再放大，因果才對：是牠進去了，所以我們跟著進去。 */
    // ① 跑到門口
    moveDog(r.door.x + (r.side === "L" ? -30 : 30), r.door.y, true);

    // ② 進門，走到這一室左下角的狗窩
    setTimeout(() => moveDog(r.x + 38, r.y + r.h - 34, true), 430);

    // ③ 躺好了，房間才開始放大。牠淡出——放大後的室內有自己的狗窩。
    setTimeout(() => {
      const d = $("dog");
      if (d) d.style.opacity = "0";
      // 只留這一間：其餘房間與整層牆在放大時收掉，不然外牆那條線會被
      // 放大成橫過畫面的黑框。
      ROOMS.forEach((x) => x.g.classList.toggle("target", x.id === r.id));
      lobby.classList.add("zooming");
      // 先量、再等一幀才套 transform：量測會觸發 layout，跟套用擠在同一幀
      // 就是那一格掉幀。
      const t = zoomTransform(r);
      requestAnimationFrame(() => { $("planstage").style.transform = t; });
    }, 1000);

    // ④ 室內介面
    setTimeout(() => showRoom(r), 1560);
  }

  /* app 不用 hidden：Leaflet 在 display:none 裡量到的是 0×0，一旦這樣初始化
     過，之後 invalidateSize() 也救不回正確的圖磚邊界。所以 app 從頭到尾都活著，
     中庭只是蓋在它上面（position:fixed, z-index 40）。 */
  /* 室頭 + 版面。五間房走同一條路，差別只在 `r.map` 與 `r.pane`——
     每一室各寫一次進場邏輯，第六間房出現時就會有一間忘了同步。 */
  function dressRoom(r) {
    // 室頭掛上這一室的顏色。中庭的門楣、樓層索引的色條、室頭這條線用同一個色，
    // 「我在哪一間」在三個畫面之間才是連續的。
    const head = document.querySelector(".roomhead");
    if (head) head.style.setProperty("--rc", cssv(r.accent));
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
      startTalking();
      const d = $("dog");
      if (d) d.style.opacity = "";
      moveDog(720, 560, false);      // 直接歸位，不要讓牠橫越整張圖
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
      // 每一列掛上該室的顏色，樓層索引與中庭平面圖用同一組色。
      b.style.setProperty("--rc", cssv(r.accent));
      // 統計數字拿掉了：五個不同單位的數字（園數、份數、則數、倍數）排在
      // 同一欄互相沒有可比性，只是把索引變吵。真正的數字在各室的室頭。
      b.innerHTML = `<span class="rail-no">${r.no}</span>${r.name}`;
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
    $("plan").addEventListener("pointermove", onPointerMove);
    const b = $("rail-back");
    if (b) b.addEventListener("click", back);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && state.where === "room" && !state.busy) back();
    });
  }


  window.Lobby = { start, enter, back, paintWho, get where() { return state.where; } };
})();
