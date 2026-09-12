/* 登入層。
 *
 * 為什麼要有登入：agent 的 record_feedback 記錄的是「**哪一位**稽查員認同這條
 * 建議」，沒有身分那張人在迴圈的資料集就無從回溯；而治理文件要求個別機構分數
 * 不對外公開揭露，有登入才能主張這是內部系統。
 *
 * 這一層擋的是畫面，不是資料。真正的授權在後端每個端點的 get_current_user，
 * 前端只是不要讓未登入的人對著一個叫不動的助理發問。
 */
(function () {
  const $ = (id) => document.getElementById(id);

  let me = null;

  function show() {
    $("authwrap").hidden = false;
    $("email").focus();
  }

  function hide() {
    $("authwrap").hidden = true;
  }

  function paint() {
    const box = $("whoami");
    if (box) box.textContent = me ? `${me.name}（${me.role}）` : "未登入";
    const out = $("logout");
    if (out) out.hidden = !me;
    /* 右上角的身分鈕由 lobby.js 畫。從這裡通知它，而不是讓它自己輪詢——
       `check()` 是非同步的，中庭啟動時 `me` 常常還是 null，只在啟動時畫一次
       的話身分鈕會永遠是空的（實測過）。 */
    if (window.Lobby && window.Lobby.paintWho) window.Lobby.paintWho();
  }

  async function check() {
    try {
      const r = await fetch("/api/auth/me");
      if (r.ok) {
        me = await r.json();
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

  async function login(email, password) {
    const err = $("autherr");
    err.textContent = "";
    let r;
    try {
      r = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
    } catch (e) {
      err.textContent = `連線失敗：${e.message}`;
      return;
    }
    if (!r.ok) {
      // 後端對「查無帳號」與「密碼錯誤」回同一句話，前端照原樣顯示，
      // 不要自己補一個更具體的理由——那會把後端刻意隱藏的資訊洩漏回來。
      let detail = "登入失敗";
      try { detail = (await r.json()).detail || detail; } catch (_) { /* 用預設 */ }
      err.textContent = detail;
      return;
    }
    me = await r.json();
    hide();
    paint();
  }

  async function logout() {
    try { await fetch("/api/auth/logout", { method: "POST" }); } catch (_) { /* 照樣清 */ }
    me = null;
    paint();
    show();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const form = $("authform");
    if (form) {
      form.addEventListener("submit", (e) => {
        e.preventDefault();
        login($("email").value.trim(), $("password").value);
      });
    }
    const out = $("logout");
    if (out) out.addEventListener("click", logout);
    check();
  });

  window.AgentAuth = { show, hide, check, get user() { return me; } };
})();
