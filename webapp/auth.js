/* 登入層。
 *
 * inspector 與 admin 現在是兩個固定帳號的身分描述，並不是權限邊界；兩者登入後
 * 共用同一套介面與功能。一般登入會把使用者選的身分送給後端核對，但真正角色
 * 永遠以資料庫 User.role 為準。
 *
 * 快速登入不在前端保存密碼。頁面先問 /api/auth/options，只有後端明確開啟
 * QUICK_LOGIN_ENABLED 且對應 seed 帳號存在時，才顯示一鍵入口。
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const ROLE_LABELS = Object.freeze({ inspector: "稽查人員", admin: "系統管理員" });

  let me = null;

  function roleLabel(role) {
    return ROLE_LABELS[role] || role || "未指定";
  }

  function selectedRole() {
    const selected = document.querySelector('input[name="login-role"]:checked');
    return selected ? selected.value : "inspector";
  }

  function paintRoleChoices() {
    document.querySelectorAll(".authrole").forEach((label) => {
      const input = label.querySelector('input[name="login-role"]');
      label.classList.toggle("selected", Boolean(input && input.checked));
    });
  }

  function show() {
    $("authwrap").hidden = false;
    $("email").focus();
  }

  function hide() {
    $("authwrap").hidden = true;
  }

  /* ── 登入成功 → 主畫面：光圈轉場 ─────────────────────────────
   *
   * 照 MaiCoin frontend-pixel 的傳送光圈（src/game/render.ts::drawIris）：圓外全部
   * 塗滿、圓的邊緣一圈 8px 點陣色環加一道細線。先「收合」到 0，全黑那一格才
   * 真的換畫面，再「張開」——看不到瞬間切換的突兀，也不會有登入層與中庭同時
   * 疊在一起的那一格。
   *
   * 時間照原作：收合 0.26 秒（不擋路），張開用原作開場的 0.55 秒——這是看到
   * 主畫面的第一眼，跟換房不是同一件事。線性，跟原作一樣。
   *
   * ⚠️ 只在「按下登入且成功」時播。開頁時 /api/auth/me 已經登入的那條路
   * （check()）直接 hide()——不然每次重新整理都要看一次開場。
   * prefers-reduced-motion 時直接切換。 */
  const IRIS_OUT = 260;
  const IRIS_IN = 550;

  function irisCanvas() {
    const c = document.createElement("canvas");
    c.id = "irisfx";
    c.setAttribute("aria-hidden", "true");
    // inline：這一層只活不到一秒，而且蓋在登入層（9000）之上。
    Object.assign(c.style, {
      position: "fixed", inset: "0", width: "100vw", height: "100vh",
      zIndex: "9500", pointerEvents: "none",
    });
    document.body.appendChild(c);
    return c;
  }

  /* 點陣色環的 2×2 棋盤圖樣。原作是像素風，逐像素畫在大半徑時每幀要跑幾十萬次，
     pattern 是 O(1)。格子依 devicePixelRatio 放大，高解析螢幕上點才看得到。 */
  function checker(ctx, color, cell) {
    const tile = document.createElement("canvas");
    tile.width = tile.height = cell * 2;
    const g = tile.getContext("2d");
    g.fillStyle = color;
    g.fillRect(0, 0, cell, cell);
    g.fillRect(cell, cell, cell, cell);
    return ctx.createPattern(tile, "repeat");
  }

  function drawIris(ctx, w, h, r, dpr, ink, accent, pattern) {
    const cx = w / 2;
    const cy = h / 2;
    ctx.clearRect(0, 0, w, h);
    // 矩形＋反向圓弧：一條路徑就取到「圓的外部」，比先塗滿再挖洞快。
    ctx.fillStyle = ink;
    ctx.beginPath();
    ctx.rect(0, 0, w, h);
    ctx.arc(cx, cy, Math.max(0, r), 0, Math.PI * 2, true);
    ctx.fill();
    if (r <= 0) return;
    ctx.globalAlpha = 0.85;
    ctx.fillStyle = pattern;
    ctx.beginPath();
    ctx.arc(cx, cy, r + 8 * dpr, 0, Math.PI * 2);
    ctx.arc(cx, cy, r, 0, Math.PI * 2, true);
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.strokeStyle = accent;
    ctx.lineWidth = dpr;
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.stroke();
  }

  function irisSwap(swap) {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      swap();
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      const c = irisCanvas();
      const ctx = c.getContext("2d");
      const root = getComputedStyle(document.documentElement);
      const ink = root.getPropertyValue("--ink").trim() || "#14202A";
      const accent = root.getPropertyValue("--seal").trim() || "#B23A2F";
      let dpr = 0;
      let w = 0;
      let h = 0;
      let maxR = 0;
      let pattern = null;
      /* 位元圖尺寸跟著視窗走。CSS 尺寸是 100vw／100vh，只在開始時量一次的話，
         轉場中途縮放視窗會把固定的位元圖拉成橢圓（審查實測）。每一幀比一次，
         變了才重設——重設會清空畫布狀態，點陣圖樣也要跟著重建。 */
      const fit = () => {
        const d = window.devicePixelRatio || 1;
        const nw = Math.round(window.innerWidth * d);
        const nh = Math.round(window.innerHeight * d);
        if (d === dpr && nw === w && nh === h) return;
        dpr = d;
        w = c.width = nw;
        h = c.height = nh;
        pattern = checker(ctx, accent, Math.max(1, Math.round(dpr)));
        // 光圈全開：蓋滿整個畫面的對角線一半（原作 irisMaxRadius）。
        maxR = Math.hypot(w, h) / 2 + 10 * dpr;
      };
      fit();

      let swapped = false;
      let finished = false;
      const runSwap = () => {
        if (swapped) return;
        swapped = true;
        // 換畫面丟例外的話，光圈照樣要收掉——一片不會消失的黑幕比什麼都糟。
        try { swap(); } catch (err) { console.error(err); }
      };
      const finish = () => {
        if (finished) return;
        finished = true;
        clearTimeout(bail);
        runSwap();
        c.remove();
        resolve();
      };
      // 背景分頁時 requestAnimationFrame 會暫停；保險起見 2.5 秒一定收掉。
      const bail = setTimeout(finish, 2500);

      let phase = "out";
      let t0 = performance.now();
      const frame = (now) => {
        if (finished) return;
        fit();
        const k = Math.min(1, (now - t0) / (phase === "out" ? IRIS_OUT : IRIS_IN));
        if (phase === "out") {
          drawIris(ctx, w, h, maxR * (1 - k), dpr, ink, accent, pattern);
          if (k >= 1) {
            runSwap();             // 全黑的這一格才換畫面
            phase = "in";
            t0 = now;
          }
          requestAnimationFrame(frame);
          return;
        }
        drawIris(ctx, w, h, maxR * k, dpr, ink, accent, pattern);
        if (k < 1) { requestAnimationFrame(frame); return; }
        finish();
      };
      requestAnimationFrame(frame);
    });
  }

  /* 連不上時要講出來，不能留一片空白。
     第一版在 check() 的 catch 裡什麼都不做，理由是「地圖與名單都是靜態 payload，
     仍然可看」——那是 dist/ 靜態版的年代。現在資料一律走 /api，而 sw.js 刻意不
     快取 /api，於是斷線打開 app 的畫面是：標題列在、下面一片空白、沒有任何說明
     （iPhone 模擬實測）。離線殼存在的意義，就是把「現在看不到、為什麼」講出來。
     順便把資料不在手機上這件事說清楚——那是刻意的，不是壞掉。 */
  let offlineBound = false;
  function showOffline() {
    const wrap = $("authwrap");
    if (!wrap) return;
    let box = $("offlinebox");
    if (!box) {
      box = document.createElement("div");
      box.id = "offlinebox";
      box.className = "authbox";
      wrap.appendChild(box);
    }
    const offline = navigator.onLine === false;
    box.innerHTML = `<h2>${offline ? "目前離線" : "連不上伺服器"}</h2>
      <p>稽查名單、通報與卷宗<b>不會存放在這支手機上</b>，所以連線中斷時看不到內容。</p>
      <p>${offline ? "連上網路後會自動重新載入。" : "可能是伺服器未啟動或網路不穩。"}</p>
      <button type="button" id="offlineretry">重新連線</button>`;
    $("offlineretry").addEventListener("click", () => location.reload());
    const form = $("authform");
    if (form) form.hidden = true;
    box.hidden = false;
    wrap.hidden = false;
    if (!offlineBound) {
      offlineBound = true;
      window.addEventListener("online", () => location.reload());
    }
  }

  function clearOffline() {
    const box = $("offlinebox");
    if (box) box.hidden = true;
    const form = $("authform");
    if (form) form.hidden = false;
  }

  function paint() {
    const box = $("whoami");
    if (box) box.textContent = me ? `${me.name}（${roleLabel(me.role)}）` : "未登入";
    /* ⚠️ 用屬性而不是 id：登出鈕有兩顆（中庭一顆、室內一顆），而 id 只能有一個。
       原本只綁室內那顆，於是在中庭**登不出去**——身分卡打開只有資訊，沒有出口。
       之後再多一處身分卡也不必回來改這裡。 */
    document.querySelectorAll("[data-logout]").forEach((b) => { b.hidden = !me; });
    /* 右上角的身分鈕由 lobby.js 畫。從這裡通知它，而不是讓它自己輪詢。 */
    if (window.Lobby && window.Lobby.paintWho) window.Lobby.paintWho();
  }

  function setBusy(busy) {
    document.querySelectorAll("#authform button").forEach((button) => {
      button.disabled = busy;
    });
  }

  async function errorDetail(response, fallback) {
    try { return (await response.json()).detail || fallback; } catch (_) { return fallback; }
  }

  async function authenticate(url, body) {
    const err = $("autherr");
    err.textContent = "";
    setBusy(true);
    try {
      const response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!response.ok) {
        // 帳號、密碼、角色任一不符都照後端的同一句話顯示，避免洩漏帳號資訊。
        err.textContent = await errorDetail(response, "登入失敗");
        return;
      }
      me = await response.json();
      // 光圈收到全黑那一格才換畫面。開頁時已登入（check()）不走這條。
      await irisSwap(() => { hide(); paint(); });
    } catch (e) {
      err.textContent = `連線失敗：${e.message}`;
    } finally {
      setBusy(false);
    }
  }

  async function login(email, password, role) {
    await authenticate("/api/auth/login", { email, password, role });
  }

  async function quickLogin(role) {
    await authenticate("/api/auth/quick-login", { role });
  }

  async function loadOptions() {
    const quick = $("quicklogin");
    if (!quick) return;
    quick.hidden = true;
    try {
      const response = await fetch("/api/auth/options");
      if (!response.ok) return;
      const options = await response.json();
      const available = new Set(options.quick_login_roles || []);
      quick.querySelectorAll("[data-quick-role]").forEach((button) => {
        button.hidden = !available.has(button.dataset.quickRole);
      });
      quick.hidden = !options.quick_login_enabled || available.size === 0;
    } catch (_) {
      // 後端未啟動或正式環境未提供快速登入時，維持隱藏即可。
    }
  }

  async function check() {
    try {
      const response = await fetch("/api/auth/me");
      if (response.ok) {
        me = await response.json();
        clearOffline();
        hide();
      } else {
        me = null;
        clearOffline();
        show();
      }
    } catch (_) {
      // 連不上：離線或伺服器沒起來。資料一律走 /api 且不快取，所以要講出來，
      // 不能像靜態版那樣假設「名單仍然可看」。
      me = null;
      showOffline();
    }
    paint();
  }

  async function logout() {
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch (_) { /* 照樣清 */ }
    me = null;
    paint();
    show();
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll('input[name="login-role"]').forEach((input) => {
      input.addEventListener("change", paintRoleChoices);
    });
    paintRoleChoices();

    const form = $("authform");
    if (form) {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        login($("email").value.trim(), $("password").value, selectedRole());
      });
    }
    document.querySelectorAll("[data-quick-role]").forEach((button) => {
      button.addEventListener("click", () => quickLogin(button.dataset.quickRole));
    });
    document.querySelectorAll("[data-logout]").forEach((b) =>
      b.addEventListener("click", logout));
    loadOptions();
    check();
  });

  window.AgentAuth = { show, hide, check, roleLabel, get user() { return me; } };
})();
