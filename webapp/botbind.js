/* 綁定 Telegram：身分卡裡的一顆鈕。
 *
 * 綁定的起點刻意放在派工台（已登入）——bot 是公開搜得到的，任何能在聊天室裡自己
 * 說出口的憑據都等於沒有憑據，理由見 src/smart_watchdog/bots/linking.py。
 * 這支只做三件事：要一組一次性碼、給一個「在 Telegram 開啟」的連結、再給一行可以
 * 直接貼進 bot 的 `/start 碼`。
 *
 * 為什麼要那一行退路：已經開過對話的人再點深層連結，有些 Telegram 版本只會打開
 * 對話、不會把碼送出去，bot 收到的是一個空的「開始」——實際發生過（2026-09-13，
 * 雲端 bot 收到兩則、兩則都因為沒帶碼被擋）。
 *
 * 中庭與房間內各有一張身分卡，所以用 data 屬性而不是 id：id 只能有一個，
 * auth.js 的登出鈕踩過同一個坑。
 *
 * 在電腦上點連結會打開 Telegram 桌面版，那也沒關係：私訊對話在同一個 Telegram
 * 帳號的所有裝置上是同一個，桌面版綁好，手機上就能用。
 */
(function () {
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  async function bind(btn) {
    const out = btn.parentElement && btn.parentElement.querySelector("[data-tgbind-out]");
    if (!out) return;
    btn.disabled = true;
    out.hidden = false;
    out.textContent = "產生中…";
    try {
      const r = await fetch("/api/bot/telegram/bind-code", { method: "POST" });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        out.textContent = r.status === 401 ? "請先登入。"
          : (d.detail || `產生失敗（HTTP ${r.status}）`);
        return;
      }
      const cmd = `/start ${d.code}`;
      const minutes = Math.round((d.expires_in || 600) / 60);
      out.innerHTML = (d.deep_link
        ? `<a class="tgbind-link" href="${esc(d.deep_link)}" target="_blank"
             rel="noopener">在 Telegram 開啟並按「開始」</a>` : "")
        + `<div class="tgbind-alt">沒反應的話，把這行送給 bot：</div>`
        + `<code class="tgbind-cmd">${esc(cmd)}</code>`
        + `<button type="button" class="mini tgbind-copy">複製這行</button>`
        + `<div class="tgbind-note">${minutes} 分鐘內有效、只能用一次。`
        + `電腦上綁好，手機上同一個 Telegram 帳號也能用。</div>`;
      const copy = out.querySelector(".tgbind-copy");
      copy.addEventListener("click", (e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(cmd).then(
          () => { copy.textContent = "已複製"; },
          () => { copy.textContent = "請手動選取上面那行"; });
      });
    } catch (e) {
      out.textContent = `產生失敗：${e.message}`;
    } finally {
      btn.disabled = false;
    }
  }

  document.querySelectorAll("[data-tgbind]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      // 身分卡點外面會收起；按鈕在卡片裡，但仍不讓事件往上冒，免得哪一層的
      // 「點別處就關」把剛產生的碼一起收掉。
      e.stopPropagation();
      bind(btn);
    });
  });

  /* 登出時把產生過的碼一起清掉：下一位登入的人不該看到上一位的綁定碼。 */
  document.querySelectorAll("[data-logout]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("[data-tgbind-out]").forEach((out) => {
        out.hidden = true;
        out.innerHTML = "";
      });
    });
  });
})();
