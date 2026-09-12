/* 答詢擬稿室（05）——兩件事共用一個房間，但它們的收件人不同。
 *
 *   待答擬稿  收件人是**民眾**。輸入是一則通報／一串 @標註，輸出是一份
 *             未送出的回覆草稿，走 report/verify.py 的用詞閘門。
 *   首長答詢  收件人是**局長、處長、議員**。輸入是一句問話，輸出是一張
 *             可以直接念的卡片：一句結論、三個數字、一張圖、五筆清單。
 *
 * 兩者刻意分開，因為把它們寫成同一個聊天框的那天，就會有人把「給民眾的
 * 回覆」當成「給長官的說明」貼出去——前者要克制到不能有任何判斷，後者要
 * 講得出判斷依據。
 *
 * 版面規則（三個房間共用）：顏色帶資訊、文字不帶。來源用色點、狀態用色框、
 * 數量用等寬數字；說明文字一律一行以內，長的收進 title 屬性。
 */
(function () {
  const SW = window.SW;
  const $ = SW.$;
  const esc = SW.esc;

  /* 來源色：這三個色是地圖室機構類別用過的同一組（dataviz 驗證過的 CVD
     分離度）。它們不會同時出現在同一個畫面上——那裡是機構類別，這裡是
     訊息來源——所以重用的是「分得開」這個性質，不是語意。 */
  const SRC = {
    apify_threads: { label: "Threads", v: "--src-th" },
    threads: { label: "Threads", v: "--src-th" },
    news_rss: { label: "新聞", v: "--src-news" },
    ptt: { label: "論壇", v: "--src-ptt" },
    vendor_feed: { label: "監測", v: "--src-vendor" },
  };
  const srcOf = (ch) => SRC[ch] || { label: ch || "其他", v: "--ink-4" };

  const state = { items: [], sel: null, panel: null, loaded: false };

  /* ── 分段切換 ────────────────────────────────────────── */
  function show(view) {
    document.querySelectorAll("#dk-view button").forEach((b) =>
      b.setAttribute("aria-pressed", String(b.dataset.v === view)));
    [["reply", "dk-reply"], ["exec", "dk-exec"], ["memo", "dk-memo"]]
      .forEach(([v, id]) => { $(id).hidden = v !== view; });
    $("dk-src").textContent = view === "reply"
      ? "草稿一律未送出，也沒有送出的程式路徑"
      : view === "exec"
        ? "只查手上的資料，不外推"
        : "report · 本批建議書";
    if (view === "reply" && !state.loaded) loadQueue();
  }

  /* ── 待答清單 ───────────────────────────────────────────
   * 收的是**有人 @ 我們官方帳號**的那些，不是「有人在談」的那些。
   * 這條界線決定了這個房間的性質：@標註是一個人把話講給我們聽，他在等回覆；
   * 新聞與論壇的轉述沒有收件人，沒有人在等我們回。把後者放進待答清單，
   * 承辦人會對著一份永遠清不完、而且沒有一則需要回覆的清單工作。
   * 新聞與論壇仍然看得到——在右邊當脈絡，不在左邊當待辦。
   *
   * 排序依**最後活動時間**，不是任何分數。有人在講話的順序不等於風險順序。 */
  async function loadQueue() {
    state.loaded = true;
    const box = $("dklist");
    box.innerHTML = '<div class="dkempty">載入中…</div>';
    let r;
    try {
      r = await SW.api("/api/social?limit=300");
    } catch (e) {
      box.innerHTML = `<div class="dkempty">載入失敗：${esc(e.message)}</div>`;
      return;
    }
    state.items = (r.items || [])
      .filter((x) => (x.counts.threads.threads || 0) > 0)
      .sort((a, b) => String(b.last_activity).localeCompare(String(a.last_activity)));
    $("dk-n").textContent = state.items.length;
    if (!state.items.length) {
      box.innerHTML = '<div class="dkempty">目前沒有 @ 標註我們的通報。<br>'
        + '<span class="dknote">沒有人 @ 我們，不代表沒有事情發生——'
        + '新聞與論壇的轉述沒有收件人，看得到但不會排進待答。</span></div>';
      return;
    }
    box.innerHTML = state.items.map(rowHtml).join("");
    box.querySelectorAll(".dkrow").forEach((el) =>
      el.addEventListener("click", () => select(el.dataset.i)));
  }

  function rowHtml(x) {
    const t = x.counts.threads;
    const m = x.counts.mentions;
    const other = Object.keys(SRC).filter((k) => k !== "apify_threads" && m[k])
      .map((k) => `<i class="dkchip" style="--c:var(${srcOf(k).v})"
        title="${esc(srcOf(k).label)} ${m[k]} 則">${m[k]}</i>`).join("");
    return `<button type="button" class="dkrow" data-i="${esc(x.institution_id)}">
      <span class="dkname">${esc(x.title)}${
      x.is_demo ? '<i class="dkdemo">示範</i>' : ""}</span>
      <span class="dkchips">
        <i class="dkchip mark" title="@標註通報 ${t.mentions} 則">@${t.mentions}</i>
        ${t.replies ? `<i class="dkchip rep" title="串下回覆 ${t.replies} 則"
          >↩${t.replies}</i>` : ""}${other}</span>
      <span class="dkmeta">${esc(x.town)}<b>${esc(day(x.last_activity))}</b></span>
    </button>`;
  }

  /* 列表只給到「哪一天」。時分秒在這裡沒有決策價值，而它會把園名擠掉。 */
  const day = (v) => String(v || "").slice(0, 10);
  const stamp = (v) => String(v || "").replace("T", " ").slice(5, 16);

  /* ── 單筆：手上有什麼 → 擬稿 ─────────────────────────── */
  async function select(id) {
    state.sel = id;
    document.querySelectorAll(".dkrow").forEach((el) =>
      el.classList.toggle("on", el.dataset.i === id));
    const work = $("dkwork");
    work.innerHTML = '<div class="dkempty">載入中…</div>';
    let p;
    try {
      p = await SW.api("/api/social/" + encodeURIComponent(id));
    } catch (e) {
      work.innerHTML = `<div class="dkempty">載入失敗：${esc(e.message)}</div>`;
      return;
    }
    state.panel = p;
    const point = (SW.state.byId || {})[id] || {};
    const voices = (p.mentions.items || []).slice(0, 6);
    const threads = p.threads.items || [];

    work.innerHTML = `
      <div class="dkhead">
        <b>${esc(p.institution.title)}</b>
        <span class="dkfacts">
          <span>${esc(p.institution.town || "")}</span>
          ${point.r ? `<span>排序 #${point.r}</span>` : ""}
          ${point.cf ? `<span class="bad">財報發現 ${point.cf}</span>` : ""}
          ${point.np ? `<span class="bad">裁罰 ${point.np}</span>` : ""}
        </span>
      </div>

      <div class="dksec">
        <h4>@ 標註我們的通報<i>${threads.length} 串</i></h4>
        ${threads.length ? threads.map(threadHtml).join("")
      : '<div class="dkempty sm">這一家沒有 @ 標註我們的通報。</div>'}
      </div>

      ${voices.length ? `<div class="dksec">
        <h4>其他來源<i>${voices.length} 則·無收件人</i></h4>
        ${voices.map(voiceHtml).join("")}
      </div>` : ""}

      <!-- 兩種收件人：回民眾要有那一串才擬得出來（鈕在該串底下）；
           答詢說明稿是對內的，手上有什麼就寫什麼。 -->
      <div class="dkact">
        <button type="button" class="dkgo alt" id="dkbrief">統整答詢說明稿</button>
        <span class="dknote">對內：回局長／處長／議員，不對外發布</span>
      </div>

      <div id="dkdraft"></div>`;

    work.querySelectorAll(".dkgo[data-root]").forEach((b) =>
      b.addEventListener("click", () => draft(id, b.dataset.root, b)));
    $("dkbrief").addEventListener("click", () => brief(id, $("dkbrief")));
  }

  /* 一串＝主貼文加它底下的回覆。回覆要一起看，因為承辦人要回的是那一串
     ——底下那幾則「+1」「我也遇到」是同一件事的一部分，不是另外幾件事。 */
  function threadHtml(t) {
    const root = t.root || {};
    const replies = t.replies || [];
    const post = (x, cls) => `<div class="dkt-post ${cls}">
      <span class="dkt-who">@${esc(x.username || "")}</span>
      <span class="dkt-date">${esc(stamp(x.posted_at))}</span>
      ${x.permalink ? `<a href="${esc(x.permalink)}" target="_blank"
        rel="noopener">原文</a>` : ""}
      <p>${esc(x.text || "")}</p>
    </div>`;
    return `<div class="dkthread">
      ${t.root_missing_reason
      ? `<div class="dkbad">${esc(t.root_missing_reason)}</div>`
      : post(root, "root")}
      ${replies.length ? `<div class="dkt-replies">
        <span class="dkt-n">串下 ${replies.length} 則回覆</span>
        ${replies.map((r) => post(r, "reply")).join("")}</div>` : ""}
      <div class="dkt-act">
        <button type="button" class="dkgo" data-root="${
      esc(t.root_threads_id || root.threads_id || "")}">擬定回覆草稿</button>
        <span class="dknote">對外：回這一串的民眾，送出前須陳核</span>
      </div>
    </div>`;
  }

  function voiceHtml(v) {
    const s = srcOf(v.channel);
    const text = String(v.headline || "").replace(/^\[[a-z]+\]\s*/, "");
    return `<div class="dkvoice" style="--c:var(${s.v})">
      <span class="dkv-src">${esc(s.label)}</span>
      <span class="dkv-date">${esc(v.published || "")}</span>
      <p>${esc(text.slice(0, 110))}${text.length > 110 ? "…" : ""}</p>
      ${v.url ? `<a href="${esc(v.url)}" target="_blank" rel="noopener">原文</a>` : ""}
    </div>`;
  }

  /* 草稿分兩段：承辦人須知不對外，可送出內文才是可能被貼出去的那一段。
     後端已經把可送出的那段單獨給了（sendable），這裡**不自己切字串**。 */
  async function draft(id, root, btn) {
    const box = $("dkdraft");
    btn.disabled = true;
    btn.textContent = "擬稿中…";
    box.innerHTML = '<div class="dkempty sm">擬稿中…</div>';
    try {
      const d = await SW.post(`/api/social/${encodeURIComponent(id)}/draft-reply`,
        { root_threads_id: root });
      box.innerHTML = draftHtml(d);
      const c = $("dkcopy");
      if (c) {
        c.addEventListener("click", () => {
          navigator.clipboard.writeText(d.sendable || "");
          c.textContent = "已複製可送出內文";
        });
      }
    } catch (e) {
      box.innerHTML = `<div class="dkbad">擬稿失敗：${esc(e.detail || e.message)}</div>`;
    }
    btn.disabled = false;
    btn.textContent = "擬定回覆草稿";
  }

  function draftHtml(d) {
    const ok = d.verified;
    return `<div class="dkdraft ${ok ? "" : "bad"}">
      <div class="dkd-head">
        <span class="dkd-state ${ok ? "ok" : "bad"}">${
      ok ? "用詞檢核通過" : "用詞檢核未過"}</span>
        <span class="dkd-by">${esc(d.backend === "bedrock" ? "Bedrock 生成" : "樣板組裝")}</span>
        <span class="dkd-by">引用 ${d.posts_used} 則</span>
        <button type="button" class="dkcopy" id="dkcopy">複製可送出內文</button>
      </div>
      ${ok ? "" : `<div class="dkbad">${esc((d.problems || []).join("；"))}</div>`}
      <pre class="dkd-body">${esc(d.sendable || d.draft || "")}</pre>
      <details class="dkd-more"><summary>承辦人須知（不對外）</summary>
        <pre class="dkd-body sm">${esc(d.draft || "")}</pre></details>
      <div class="dkd-foot">${esc(d.note || "")}</div>
    </div>`;
  }

  /* 答詢說明稿：把手上的全部東西統整成一份**對內**的稿子。
     與回覆草稿是兩份文書：那份不得點名、這份必須點名；那份會被貼到公開平台，
     這份會被念進議場。所以卡片的頭一定要說出自己是哪一種。 */
  async function brief(id, btn) {
    const box = $("dkdraft");
    btn.disabled = true;
    btn.textContent = "統整中…";
    box.innerHTML = '<div class="dkempty sm">統整中……模型要讀完手上所有來源。</div>';
    try {
      const d = await SW.post(
        `/api/social/${encodeURIComponent(id)}/draft-brief`, {});
      box.innerHTML = `<div class="dkdraft ${d.verified ? "" : "bad"} internal">
        <div class="dkd-head">
          <span class="dkd-tag">內部說明稿</span>
          <span class="dkd-state ${d.verified ? "ok" : "bad"}">${
        d.verified ? "用詞檢核通過" : "用詞檢核未過"}</span>
          <span class="dkd-by">${esc(d.backend === "bedrock" ? "Bedrock 生成" : "樣板組裝")}</span>
          <span class="dkd-by">引用 ${d.voices_used} 則外界聲音</span>
          <button type="button" class="dkcopy" id="dkcopy2">複製全文</button>
        </div>
        ${d.verified ? "" : `<div class="dkbad">${esc((d.problems || []).join("；"))}</div>`}
        ${d.fell_back && d.fallback_reason
        ? `<div class="dkbad">已改用樣板重寫：${esc(d.fallback_reason)}</div>` : ""}
        <pre class="dkd-body">${esc(d.draft || "")}</pre>
        <div class="dkd-foot">${esc(d.note || "")}</div>
      </div>`;
      const c = $("dkcopy2");
      if (c) {
        c.addEventListener("click", () => {
          navigator.clipboard.writeText(d.draft || "");
          c.textContent = "已複製";
        });
      }
    } catch (e) {
      box.innerHTML = `<div class="dkbad">統整失敗：${esc(e.detail || e.message)}</div>`;
    }
    btn.disabled = false;
    btn.textContent = "統整答詢說明稿";
  }

  /* ── 首長答詢 ───────────────────────────────────────────
   * 長官問的是「為什麼是這幾家」「這個月為什麼先查三重」，要的是能直接念出來
   * 的一段話加一張圖，不是一份報表。所以答覆長成卡片：**一句結論、三個數字、
   * 一張行政區圖、五筆清單、一行界線**——多的收進「完整清單」。
   */
  async function ask(q) {
    const log = $("exlog");
    log.insertAdjacentHTML("beforeend",
      `<div class="exq">${esc(q)}</div>`);
    const slot = document.createElement("div");
    slot.className = "excard wait";
    slot.textContent = "查詢中…";
    log.appendChild(slot);
    log.scrollTop = log.scrollHeight;
    try {
      const r = await SW.post("/api/chat", { question: q });
      slot.className = "excard";
      slot.innerHTML = cardHtml(r);
      slot.querySelectorAll(".exrow").forEach((el) =>
        el.addEventListener("click", () => SW.openDossier(el.dataset.i)));
      const c = slot.querySelector(".excopy");
      if (c) c.addEventListener("click", () => {
        navigator.clipboard.writeText(plain(r));
        c.textContent = "已複製";
      });
    } catch (e) {
      slot.className = "excard bad";
      slot.textContent = `查詢失敗：${e.detail || e.message}`;
    }
    log.scrollTop = log.scrollHeight;
  }

  function cardHtml(r) {
    const rows = r.results || [];
    const towns = {};
    rows.forEach((x) => { towns[x.town] = (towns[x.town] || 0) + 1; });
    const nf = rows.filter((x) => x.compliance_failed).length;
    const np = rows.filter((x) => x.penalties).length;
    return `<div class="ex-lead">${esc(r.summary || "")}</div>
      <div class="ex-tiles">
        <div><b>${r.matched ?? rows.length}</b><span>符合</span></div>
        <div><b>${Object.keys(towns).length}</b><span>行政區</span></div>
        <div class="warn"><b>${nf}</b><span>財報發現</span></div>
        <div class="warn"><b>${np}</b><span>有裁罰史</span></div>
      </div>
      ${miniMap(towns)}
      <div class="ex-rows">${rows.slice(0, 5).map((x) => `
        <button type="button" class="exrow" data-i="${esc(x.id)}">
          <span class="r">#${x.rank ?? "—"}</span>
          <span class="n">${esc(x.title)}</span>
          <span class="t">${esc(x.town)}</span>
          ${x.compliance_failed ? '<i class="exdot bad" title="財報法遵未通過"></i>' : ""}
          ${x.penalties ? '<i class="exdot warn" title="有裁罰紀錄"></i>' : ""}
        </button>`).join("")}
        ${rows.length > 5 ? `<div class="dknote">另有 ${rows.length - 5} 筆</div>` : ""}
      </div>
      <div class="ex-foot">
        <span title="${esc(r.caveat || "")}">建議查核優先序，非違法認定</span>
        <button type="button" class="excopy">複製給長官</button>
      </div>`;
  }

  /* 迷你行政區圖：命中幾家就上幾分色。這張圖回答的是長官第一個問題——
     「在哪裡」——用一秒，而不是用一段話。 */
  function miniMap(towns) {
    const b = (SW.state.payload || {}).boundary || [];
    if (!b.length) return "";
    let x0 = 180, x1 = -180, y0 = 90, y1 = -90;
    b.forEach((f) => f.poly.forEach((ring) => ring.forEach(([x, y]) => {
      if (x < x0) x0 = x; if (x > x1) x1 = x;
      if (y < y0) y0 = y; if (y > y1) y1 = y;
    })));
    // 長寬比由資料決定，不要用固定的框——固定 300×150 會在新北兩側留下大片
    // 空白，圖跟著縮小到看不出哪一區。經度要乘 cos(緯度) 才不會把新北拉胖。
    const W = 264;
    const k = Math.cos(((y0 + y1) / 2) * Math.PI / 180);
    const sc = W / ((x1 - x0) * k);
    const H = Math.round((y1 - y0) * sc);
    const px = (x) => ((x - x0) * k * sc).toFixed(1);
    const py = (y) => ((y1 - y) * sc).toFixed(1);
    const max = Math.max(1, ...Object.values(towns));
    const paths = b.map((f) => {
      const n = towns[f.d] || 0;
      const d = f.poly.map((r) =>
        "M" + r.map(([x, y]) => `${px(x)},${py(y)}`).join("L") + "Z").join(" ");
      const fill = n
        ? `var(--ex-fill)" fill-opacity="${(0.25 + 0.75 * (n / max)).toFixed(2)}`
        : "var(--sunk)";
      return `<path d="${d}" fill="${fill}" stroke="var(--rule)" stroke-width=".6">
        <title>${esc(f.d)}${n ? ` ${n} 家` : ""}</title></path>`;
    }).join("");
    return `<svg class="exmap" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"
      role="img" aria-label="命中機構的行政區分布">${paths}</svg>`;
  }

  function plain(r) {
    const rows = (r.results || []).slice(0, 5).map((x, i) =>
      `${i + 1}. ${x.title}（${x.town}，排序 #${x.rank}）`).join("\n");
    return `${r.summary}\n\n${rows}\n\n${r.caveat || ""}`;
  }

  /* ── 綁定 ──────────────────────────────────────────────── */
  document.addEventListener("click", (e) => {
    const b = e.target.closest("#dk-view button");
    if (b) show(b.dataset.v);
    const eg = e.target.closest("#dk-exec .eg");
    if (eg) ask(eg.textContent.trim());
  });

  const f = $("exform");
  if (f) {
    f.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = $("exq").value.trim();
      if (!q) return;
      $("exq").value = "";
      ask(q);
    });
  }

  /* 進房才載入：待答清單要打一次 /api/social，開站時不必先付這個成本。
     ⚠️ 綁在 `.tabs button` 的 click 上是錯的——`#tabs` 是 hidden，從中庭進來
     的人不會去點它，綁在那裡的結果就是進來一片空白（memos.js 踩過這個坑，
     app.js::showPane 的註解記著）。由 showPane 呼叫 open()。 */
  function open() {
    if (!state.loaded) loadQueue();
  }

  window.SWDesk = { show, ask, open };
})();
