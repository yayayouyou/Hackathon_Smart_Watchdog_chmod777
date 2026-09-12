/* 建議書頁籤：本批 144 份可瀏覽、可搜尋，點一筆展開該園卷宗。
 *
 * 做成頁籤而不是塞進卷宗，是因為這份清單本身有獨立價值——稽查員會想看
 * 「這批要發哪些信」。裁罰紀錄與排名軌跡沒有這種形態（它們必定屬於某一所），
 * 所以那兩個放在卷宗裡。
 */
(function () {
  const SW = window.SW;
  const $ = SW.$;
  const esc = SW.esc;
  let loaded = false;

  async function load(q) {
    const box = $("memolist");
    box.innerHTML = '<div class="insuff">載入中…</div>';
    let r;
    try {
      r = await SW.api("/api/memos" + (q ? `?q=${encodeURIComponent(q)}` : ""));
    } catch (e) {
      box.innerHTML = `<div class="insuff">載入失敗：${esc(e.message)}</div>`;
      return;
    }
    $("memocount").textContent = q
      ? `${r.count} 筆符合「${q}」`
      : `本批 ${r.count} 份建議書`;
    if (!r.count) {
      box.innerHTML = '<div class="insuff">查無符合條件的建議書。</div>';
      return;
    }
    box.innerHTML = r.items.map((x) => `
      <button class="row" data-i="${x.id}">
        <span class="r">#${x.priority_rank ?? "—"}</span>
        <span>${esc(x.title)}</span>
        <span class="m">${esc(x.town)}${
          x.findings ? "・發現" + x.findings : ""}</span>
      </button>`).join("")
      + `<p class="note">${esc(r.note)}</p>`;
    box.querySelectorAll(".row").forEach((el) =>
      el.addEventListener("click", () => SW.openDossier(el.dataset.i)));
  }

  const form = $("memoform");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      load($("memoq").value.trim());
    });
  }
  /* 第一次切到這個頁籤才載入——144 筆不必在開站時就抓。 */
  document.querySelectorAll('.tabs button[data-t="memos"]').forEach((b) =>
    b.addEventListener("click", () => {
      if (!loaded) { loaded = true; load(""); }
    }));

  /* ── 從輿情室送過來的回覆草稿 ──────────────────────────────
   *
   * 這一塊與上面那份建議書清單是**兩種不同的東西**，所以它自己一個容器、
   * 自己的樣式，而且一定要說出自己是草稿：
   *
   * 建議書是已經可以發文的公文；這是一份**未送出、也不會自動送出**的草稿，
   * 回的還是一則未經查證的民眾貼文。兩者長得一樣的那天，就是有人把草稿當成
   * 已核定的公文往外貼的那天。
   *
   * 全文分兩段：「承辦人須知」（含園名、原文連結、待確認事項）不對外，
   * 「建議回覆內文」才是可能被貼出去的。後端已經把可送出的那一段單獨給了
   * （`sendable`），所以這裡**不自己切字串**——切錯的後果是把園名連同
   * 「這是未查證的通報」一起貼到公開平台上。
   */
  function draftHtml(d) {
    const bad = !d.verified;
    let h = `<div class="md-head">
        <b>回覆草稿</b>
        <span class="md-src">${esc(d.institution && d.institution.title || "")}</span>
        <span class="tag p">${esc(d.backend === "bedrock" ? "Bedrock 生成" : "樣板組裝")}</span>
        <span class="tag ${bad ? "s" : "p"}">${
          bad ? "未通過用詞檢核" : "已通過用詞檢核"}</span>
        <button type="button" class="md-x" id="memodraft-close">關閉</button>
      </div>`;
    h += `<div class="md-warn">${esc(d.note || "")}</div>`;
    if (d.fell_back && d.fallback_reason) {
      h += `<div class="insuff">已改用樣板重寫：${esc(d.fallback_reason)}</div>`;
    }
    if (bad) {
      h += `<div class="insuff">這份草稿沒有通過檢核，請勿直接使用：${
        esc((d.problems || []).join("；"))}</div>`;
    }
    if (d.permalink) {
      h += `<div class="md-link">原貼文：<a href="${esc(d.permalink)}"
        target="_blank" rel="noopener">${esc(d.permalink)}</a></div>`;
    }
    h += `<pre class="md-body">${esc(d.draft || "")}</pre>`;
    h += `<div class="md-foot">${esc(d.disclaimer || "")}</div>`;
    return h;
  }

  function showDraft(d) {
    const box = $("memodraft");
    if (!box) return;
    box.hidden = false;
    box.innerHTML = draftHtml(d);
    const x = $("memodraft-close");
    if (x) x.addEventListener("click", () => { box.hidden = true; box.innerHTML = ""; });
    box.scrollIntoView({ block: "nearest" });
  }

  function showDraftError(message) {
    const box = $("memodraft");
    if (!box) return;
    box.hidden = false;
    box.innerHTML = `<div class="insuff">擬定回覆失敗：${esc(message)}</div>`;
  }

  window.SWMemos = { showDraft, showDraftError };
})();
