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

  function paint() {
    const box = $("whoami");
    if (box) box.textContent = me ? `${me.name}（${roleLabel(me.role)}）` : "未登入";
    const out = $("logout");
    if (out) out.hidden = !me;
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
        hide();
      } else {
        me = null;
        show();
      }
    } catch (_) {
      // 後端還沒起來：不要卡死畫面，地圖與名單都是靜態 payload，仍然可看。
      me = null;
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
    const out = $("logout");
    if (out) out.addEventListener("click", logout);
    loadOptions();
    check();
  });

  window.AgentAuth = { show, hide, check, roleLabel, get user() { return me; } };
})();
