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
      hide();
      paint();
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
