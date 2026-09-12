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
})();
