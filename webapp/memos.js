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
  /* 只有最後一次請求算數。
   *
   * 進這一室會先載入全部（`open()`），助理接著又篩「三重」（`focus()`），
   * 兩個請求並行——而全部那一份筆數多、回得慢，於是後到、把 12 筆蓋成 130 筆。
   * 畫面顯示的是助理**沒有**在講的那一批。實測到才發現。 */
  let seq = 0;

  async function load(q) {
    const box = $("memolist");
    const mine = ++seq;
    box.innerHTML = '<div class="insuff">載入中…</div>';
    let r;
    try {
      r = await SW.api("/api/memos" + (q ? `?q=${encodeURIComponent(q)}` : ""));
    } catch (e) {
      if (mine !== seq) return;
      box.innerHTML = `<div class="insuff">載入失敗：${esc(e.message)}</div>`;
      return;
    }
    if (mine !== seq) return;          // 已經有更新的請求發出去了，這份作廢
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
  /* 第一次進這一室才載入——144 筆不必在開站時就抓。
   *
   * ⚠️ 原本只綁在那顆**隱藏的分頁鈕**的 click 上。導覽改走房間系統之後
   * （中庭點房間、助理換室，兩條路都是 `showPane()`），那顆鈕不會被點到，
   * 於是進了答詢擬稿室清單永遠是空的——助理說「已列在畫面上」而畫面什麼
   * 都沒有。實測過。
   *
   * 改成對外開一支 `open()`，由 `showPane()` 呼叫，與資料室、輿情室同一個
   * 做法。分頁鈕的綁定留著：它仍是一條到得了的路。 */
  function open() {
    if (loaded) return;
    loaded = true;
    load("");
  }

  /* 讓助理把清單篩到它剛才講的那幾份。
     搜尋框也要一起填：只改清單不改輸入框的話，畫面顯示空的搜尋條件而列出來
     的只有三重——使用者看到的條件與實際套用的不一致，而且他一按搜尋就會把
     助理設的悄悄洗掉。 */
  function focus(q) {
    loaded = true;
    const box = $("memoq");
    if (box) box.value = q || "";
    load(q || "");
  }

  document.querySelectorAll('.tabs button[data-t="memos"]').forEach((b) =>
    b.addEventListener("click", open));

  window.SWMemos = { open, focus };
})();
