/* 掃描主控台
 *
 * 這一頁會花真的錢，所以整個流程是「先看金額 → 再授權 → 才執行」：
 * 每次改動任何設定都重新向伺服器要一次估算，按鈕上永遠印著即將付出的金額。
 *
 * 前端不算錢。所有金額、公式、剩餘額度都由 /api/scan/estimate 回傳——
 * 價目表只能有一份，複製一份到瀏覽器就等於埋一個遲早會對不上的第二答案。
 */
/* **整支包在 IIFE 裡。** 傳統 <script> 共用一個全域範圍，而頂層的 `function`
 * 宣告是**靜默覆蓋**——不像 `const` 會丟 SyntaxError，所以壞掉時完全沒有線索。
 * scan.js 與 timeline.js 都宣告了 `function render()`，timeline.js 載入在後，
 * 於是 scan.js 裡呼叫的 render 其實是 timeline 的：掃描主控台永遠停在
 * 「載入中…」，Console 一個字都不會印。`boot` 也在 app.js 與 timeline.js
 * 之間重名。包起來就沒有這回事。 */
(function () {
const S = window.SW;
const scan = {
  opts: null, plan: null, job: null, poll: null,
  sel: { scope: "city", channels: new Set(["news_rss", "ptt"]),
         keywords: ["幼兒園"], maxPosts: 50, district: "", topN: 50 },
  picked: new Set(),
  // 輪詢每 1.5 秒重畫一次 #joblog。勾選狀態存在這裡，重畫後還原——
  // 否則稽查員讀到一半勾好的採用項目會被自己的進度更新清光。
  checked: new Set(),
  adopted: new Set(),
};

const usd = (v) => (v == null ? "—" : `US$${Number(v).toFixed(3)}`);

/* 估算要節流，也要防亂序。
 * 節流：關鍵字欄每打一個字就重估一次，等於一句話打完打了十幾次 API。
 * 亂序：估算是非同步的，先送出的請求可能後回來——那會讓畫面上停在
 *      舊設定的金額，而使用者按下去授權的就是那個錯的數字。
 *      每次請求帶一個序號，只有最新那次的回應可以動畫面。 */
let seq = 0;
let timer = null;
function estimate() {
  clearTimeout(timer);
  const btn = S.$("runbtn");
  if (btn) { btn.disabled = true; btn.textContent = "估算中…"; }
  timer = setTimeout(doEstimate, 250);
}

/* ── 版面 ──────────────────────────────────────────────── */
/* ⚠️ `if (scan.opts) return` 擋不住**併發**進入：第一次的 fetch 還在路上時，
   `scan.opts` 仍是 undefined，第二次呼叫照樣往下走。兩個 render() 會互相蓋掉
   scanwrap 的 DOM，於是先排定的 doEstimate 醒來時 #costbox 已經是別人的了
   （症狀：Cannot set properties of null）。用一個 in-flight 的 promise 擋住。 */
let opening = null;
async function open_() {
  if (scan.opts) return;
  if (opening) return opening;
  opening = (async () => {
    try {
      scan.opts = await S.api("/api/scan/options");
    } catch (e) {
      S.$("scanwrap").innerHTML = `<p class="scanerr">載入失敗：${S.esc(e.message)}</p>`;
      return;
    }
    render();
    estimate();
  })().finally(() => { opening = null; });
  return opening;
}

function render() {
  const o = scan.opts;
  const live = o.channels.filter((c) => c.live);
  const pending = o.channels.filter((c) => !c.live);

  S.$("scanwrap").innerHTML = `
    <div class="scanform">
      <section>
        <h3>掃描哪些管道</h3>
        ${live.map(chRow).join("")}
        ${pending.length ? `<details class="pendch">
          <summary>${pending.length} 個管道尚未啟用</summary>
          ${pending.map((c) => `<div class="chline off">
            <span class="chname">${S.esc(c.label)}</span>
            <span class="chnote">${S.esc(c.how_to_enable || c.status)}</span>
          </div>`).join("")}
        </details>` : ""}
        <p class="note">${S.esc(o.apify_per_institution_blocked)}</p>
      </section>

      <section id="kwsec" hidden>
        <h3>Threads 關鍵字</h3>
        <div class="presets">
          ${Object.entries(o.keyword_presets).map(([k, v]) =>
            `<button class="preset" data-k="${k}" title="${S.esc(v.note)}"
              >${S.esc(v.keywords.join("、"))}</button>`).join("")}
        </div>
        <textarea id="kw" rows="2" spellcheck="false"
          placeholder="一行一組關鍵字">${scan.sel.keywords.join("\n")}</textarea>
        <label class="capbox">抓取上限
          <input id="mp" type="number" min="1" max="100" value="${scan.sel.maxPosts}">篇</label>
        <p class="note">關鍵字共用同一次執行，組數不影響費用；
          <b>篇數才影響</b>（每篇 US$0.0025）。</p>
      </section>

      <section>
        <h3>掃描範圍</h3>
        <div class="scopes">
          ${o.scopes.map((s) => `<label class="opt">
            <input type="radio" name="scope" value="${s.key}"
              ${s.key === scan.sel.scope ? "checked" : ""}>
            ${S.esc(s.label)}${s.count ? ` <span class="n">${s.count}</span>` : ""}
            <span class="scnote">${S.esc(s.note)}</span>
          </label>`).join("")}
        </div>
        <select id="dist" hidden>
          <option value="">選擇行政區…</option>
          ${o.districts.map((d) => `<option>${S.esc(d)}</option>`).join("")}
        </select>
        <p class="note"><b>範圍對兩種管道意義不同。</b>Threads 是關鍵字掃描，
          一次執行覆蓋全市，範圍只影響結果怎麼排；Google 評論是逐園查詢，
          範圍直接決定費用，上限 ${o.caps.per_institution} 家。</p>
      </section>

      <section class="costbox" id="costbox">估算中…</section>

      <section>
        <label class="capbox">操作人
          <input id="rev" placeholder="留下誰發動了這次掃描"
            value="${S.esc(localStorage.getItem("sw_reviewer") || "")}"></label>
        <button class="runbtn" id="runbtn" disabled>估算中…</button>
        <p class="note">結果為<b>待人工研判</b>的候選，不進入分數、不成為違規認定。
          要出現在卷宗必須逐則採用。</p>
      </section>

      <section id="joblog"></section>
    </div>`;

  bind();
}

function chRow(c) {
  const on = scan.sel.channels.has(c.key);
  return `<label class="chline">
    <input type="checkbox" class="chbox" value="${c.key}" ${on ? "checked" : ""}>
    <span class="chname">${S.esc(c.label)}</span>
    <span class="chnote">${S.esc(c.cost_note)}</span>
  </label>`;
}

/* ── 綁定 ──────────────────────────────────────────────── */
function bind() {
  document.querySelectorAll(".chbox").forEach((el) =>
    el.addEventListener("change", () => {
      el.checked ? scan.sel.channels.add(el.value) : scan.sel.channels.delete(el.value);
      S.$("kwsec").hidden = !scan.sel.channels.has("apify_threads");
      estimate();
    }));
  document.querySelectorAll('input[name="scope"]').forEach((el) =>
    el.addEventListener("change", () => {
      scan.sel.scope = el.value;
      S.$("dist").hidden = el.value !== "district";
      estimate();
    }));
  S.$("dist").addEventListener("change", (e) => {
    scan.sel.district = e.target.value; estimate();
  });
  S.$("kw").addEventListener("input", (e) => {
    scan.sel.keywords = e.target.value.split("\n").map((x) => x.trim()).filter(Boolean);
    estimate();
  });
  S.$("mp").addEventListener("input", (e) => {
    scan.sel.maxPosts = Math.max(1, Math.min(100, +e.target.value || 50));
    estimate();
  });
  document.querySelectorAll(".preset").forEach((b) =>
    b.addEventListener("click", () => {
      scan.sel.keywords = scan.opts.keyword_presets[b.dataset.k].keywords;
      S.$("kw").value = scan.sel.keywords.join("\n");
      estimate();
    }));
  S.$("rev").addEventListener("change", (e) =>
    localStorage.setItem("sw_reviewer", e.target.value.trim()));
  S.$("runbtn").addEventListener("click", run);
  S.$("kwsec").hidden = !scan.sel.channels.has("apify_threads");
}

function request() {
  return {
    scope: scan.sel.scope, district: scan.sel.district, top_n: scan.sel.topN,
    ids: [...scan.picked], channels: [...scan.sel.channels],
    keywords: scan.sel.keywords, max_posts: scan.sel.maxPosts,
    reviewer: (S.$("rev") || {}).value || "",
  };
}

/* ── 估算 ──────────────────────────────────────────────── */
async function doEstimate() {
  const mine = ++seq;
  const btn = S.$("runbtn");
  let plan;
  try {
    plan = await S.post("/api/scan/estimate", request());
  } catch (e) {
    if (mine !== seq) return;
    const box0 = S.$("costbox");
    if (box0) box0.innerHTML = `<p class="scanerr">估算失敗：${S.esc(e.message)}</p>`;
    if (btn) btn.textContent = "估算失敗";
    return;
  }
  if (mine !== seq) return;      // 已有更新的估算在路上，這份是舊的
  // 估算是 250ms 後才醒的非同步工作；期間畫面可能已被重繪或切走。
  if (!S.$("costbox")) return;
  scan.plan = plan;
  const busy = scan.job && ["queued", "running"].includes(scan.job.state);
  const p = scan.plan;
  const b = p.gate.budget;
  const free = p.usd_max === 0;

  S.$("costbox").innerHTML = `
    <div class="costhead">
      <span class="costnum ${free ? "free" : ""}">${free ? "免費" : usd(p.usd_max)}</span>
      <span class="costlbl">${free ? "本次掃描不產生費用" : "本次掃描費用上限"}</span>
    </div>
    <table class="costtab"><tbody>
      ${p.lines.map((ln) => `<tr class="${ln.blocker ? "blocked" : ""}">
        <td>${S.esc(ln.label)}</td>
        <td class="mode">${ln.mode === "sweep" ? "廣掃" : `逐園 ${ln.targets}`}</td>
        <td class="amt">${ln.blocker ? "—" : (ln.meter.usd_max ? usd(ln.meter.usd_max) : "US$0")}</td>
        <td class="fx">${S.esc(ln.blocker || ln.meter.formula)}</td>
      </tr>`).join("")}
    </tbody></table>
    ${p.expected_leads ? `<p class="leads">預期產出 ${S.esc(p.expected_leads)}。
      計費按<b>回傳筆數</b>，歸屬失敗的貼文一樣付錢——這個比例難看，但它是真的。</p>` : ""}
    <div class="budgetbar" title="本掃描週期 ${S.esc(b.source)}">
      <div class="bfill" style="width:${Math.min(100,
        100 * b.month_spent_usd / scan.opts.caps.month)}%"></div>
      <span>本期 ${usd(b.month_spent_usd)} / ${usd(scan.opts.caps.month)}
        　今日剩 ${usd(b.day_remaining_usd)}
        　Google 免費額度 ${b.places_used_this_month}/1000</span>
    </div>
    ${b.unsettled ? `<p class="note">${b.unsettled} 筆執行尚未結算，暫以上界佔用額度。
      執行 <code>scripts/reconcile_scan.py</code> 可向供應商查回實付金額。</p>` : ""}
    ${p.blockers.length ? p.blockers.map((x) =>
      `<p class="scanerr">${S.esc(x)}</p>`).join("") : ""}`;

  const gate = p.gate;
  if (busy) {
    // 執行中改設定會重估，但不能因此重新開放按鈕——第二次掃描的結果
    // 會蓋掉第一個（已經付過錢的）任務。
    btn.disabled = true;
    btn.textContent = "有掃描正在執行中";
  } else if (!gate.ok) {
    btn.disabled = true;
    btn.textContent = `無法執行：${gate.reason}`;
  } else if (p.blockers.length) {
    btn.disabled = true;
    btn.textContent = "請先調整範圍";
  } else {
    btn.disabled = false;
    btn.textContent = free
      ? `開始掃描（不產生費用）`
      : `開始掃描　最多支付 ${usd(p.usd_max)}`;
  }
}

/* ── 執行 ──────────────────────────────────────────────── */
async function run() {
  const btn = S.$("runbtn");
  const ceiling = scan.plan.confirm_ceiling_usd;
  if (ceiling > 0 && !confirm(
      `這次掃描最多支付 ${usd(ceiling)}。\n\n`
      + `範圍：${scan.plan.scope_label}\n`
      + `管道：${scan.plan.lines.map((l) => l.label).join("、")}\n\n`
      + `實付依供應商回報的實際用量計算，必然不超過此金額。`)) return;

  btn.disabled = true;
  btn.textContent = "發動中…";
  try {
    // 把畫面上那個金額原樣回押。伺服器持鎖重驗，不符就回 409，
    // 使用者授權的永遠是他看到的數字。
    scan.job = await S.post("/api/scan", { ...request(), confirm_ceiling_usd: ceiling });
  } catch (e) {
    const d = e.detail || {};
    if (d.error === "PLAN_STALE") {
      S.$("costbox").insertAdjacentHTML("afterbegin",
        `<p class="scanerr">預算或範圍在確認期間變動了（原 ${usd(d.confirmed)}
         → 現 ${usd(d.now)}）。已重新估算，請再確認一次。</p>`);
      estimate();
      return;
    }
    btn.textContent = `發動失敗：${d.reason || e.message}`;
    setTimeout(estimate, 2500);
    return;
  }
  if (scan.job.deduplicated) {
    S.$("joblog").insertAdjacentHTML("afterbegin",
      `<p class="note">相同條件的掃描正在執行中，已接回同一個任務，未重複計費。</p>`);
  }
  watch();
}

function watch() {
  clearInterval(scan.poll);
  scan.checked.clear();
  drawJob();
  let misses = 0;
  scan.poll = setInterval(async () => {
    try {
      scan.job = await S.api(`/api/scan/jobs/${scan.job.job_id}`);
      misses = 0;
    } catch {
      // 無聲重試到天荒地老，等於讓人盯著一個永遠不會更新的畫面。
      if (++misses < 5) return;
      clearInterval(scan.poll);
      S.$("joblog").insertAdjacentHTML("beforeend",
        `<p class="scanerr">連續 5 次查不到任務狀態，已停止更新。
         任務可能仍在背景執行——重新整理後可在歷史紀錄查看
         ${S.esc(scan.job.job_id)}。</p>`);
      return;
    }
    drawJob();
    if (!["queued", "running"].includes(scan.job.state)) {
      clearInterval(scan.poll);
      estimate();                       // 花完錢要立刻反映在剩餘額度上
    }
  }, 1500);
}

const STATE_TEXT = {
  queued: "排隊中", running: "執行中", done: "完成", failed: "失敗",
  cancelled: "已取消", interrupted: "因重啟中斷",
};
const OUTCOME_TEXT = {
  ok: "有結果", empty: "無可歸屬內容", partial: "部分失敗",
  failed: "管道失敗", skipped: "未啟用", blocked: "被上限擋下",
};
const COMPLETE_TEXT = {
  complete: "", partial: "資料不足：部分管道未能取得結果",
  unusable: "資料不足：所有管道均未取得結果",
};

function drawJob() {
  const j = scan.job;
  const running = ["queued", "running"].includes(j.state);
  const warn = COMPLETE_TEXT[j.data_completeness];

  S.$("joblog").innerHTML = `
    <div class="job ${j.state}">
      <div class="jobhead">
        <b>${STATE_TEXT[j.state] || j.state}</b>
        <span>${S.esc(j.scope_label || "")}</span>
        <span class="amt">${j.usd_actual != null
          ? `實付 ${usd(j.usd_actual)}` : running ? `上限 ${usd(j.usd_max)}`
          : j.usd_max ? `上限 ${usd(j.usd_max)}` : "免費"}</span>
      </div>
      ${running ? '<div class="bar"><i></i></div>' : ""}
      ${warn ? `<p class="insuff">${S.esc(warn)}</p>` : ""}
      ${j.error ? `<p class="scanerr">${S.esc(j.error)}</p>` : ""}
      <table class="octab"><tbody>${(j.outcomes || []).map((o) => `
        <tr class="oc-${o.outcome}">
          <td>${S.esc(o.label)}</td>
          <td class="st">${OUTCOME_TEXT[o.outcome] || o.outcome}</td>
          <td class="amt">${o.mentions} 則${o.raw_items ? `／${o.raw_items} 筆原始` : ""}</td>
        </tr>${o.reason ? `<tr class="ocwhy"><td colspan="3">${S.esc(o.reason)}</td></tr>` : ""}
      `).join("")}</tbody></table>
      ${j.mentions && j.mentions.length ? mentionList(j.mentions) : running ? ""
        : '<p class="note">本次掃描沒有可歸屬到特定機構的內容。</p>'}
    </div>`;

  document.querySelectorAll(".mgo").forEach((el) =>
    el.addEventListener("click", (ev) => {
      // .mgo 在 <label> 裡面：不擋掉的話點機構名會順手把採用勾選框打勾。
      ev.preventDefault();
      ev.stopPropagation();
      S.openDossier(el.dataset.i);
    }));
  document.querySelectorAll(".mck").forEach((el) => {
    el.checked = scan.checked.has(el.value);
    el.addEventListener("change", () =>
      el.checked ? scan.checked.add(el.value) : scan.checked.delete(el.value));
  });
  const ad = S.$("adopt");
  if (ad) ad.addEventListener("click", adopt);
}

function mentionList(ms) {
  const out = ms.filter((m) => m.in_scope === false).length;
  return `<div class="mentions">
    <h4>${ms.length} 則可歸屬內容　<span class="pend">待人工研判</span></h4>
    ${out ? `<p class="note">其中 ${out} 則在你選的範圍之外——廣掃是全市查詢，
      範圍只影響排序。範圍外的命中往往才是最值得看的。</p>` : ""}
    ${ms.map((m) => `<label class="mrow${m.in_scope === false ? " oos" : ""}">
      <input type="checkbox" class="mck" value="${S.esc(m.url)}">
      <div>
        <div class="mhead">
          <b class="mgo" data-i="${S.esc(m.institution_id)}"
            >${S.esc(m.institution_title || m.institution_id)}</b>
          <span>${S.esc(m.published || "")}　${S.esc(m.publisher || "")}</span>
        </div>
        <p>${S.esc(m.headline)}</p>
        <div class="mbasis">歸屬依據：${S.esc(m.attribution_basis || "—")}
          ${m.url ? `　<a href="${S.esc(m.url)}" target="_blank" rel="noopener">原文</a>` : ""}</div>
      </div>
    </label>`).join("")}
    <button class="adopt" id="adopt">採用勾選項目為待查線索</button>
    <p class="note">採用的意思是「這條值得看」，不是「這條成立」。
      採用後狀態仍為待人工研判。</p>
  </div>`;
}

async function adopt() {
  const urls = [...document.querySelectorAll(".mck:checked")].map((x) => x.value);
  const btn = S.$("adopt");
  if (!urls.length) {
    btn.textContent = "請先勾選要採用的項目";
    setTimeout(() => { btn.textContent = "採用勾選項目為待查線索"; }, 1800);
    return;
  }
  btn.disabled = true;
  try {
    const r = await S.post(`/api/scan/jobs/${scan.job.job_id}/adopt`,
      { urls, reviewer: (S.$("rev") || {}).value || "" });
    urls.forEach((u) => scan.adopted.add(u));
    btn.textContent = `已採用 ${r.adopted} 則`;
  } catch (e) {
    // 靜默失敗最糟：人會以為記錄好了，然後那條線索就消失了。
    btn.disabled = false;
    btn.textContent = "採用失敗，請再試一次";
    S.$("joblog").insertAdjacentHTML("beforeend",
      `<p class="scanerr">採用未成功：${S.esc(e.message)}。這些項目尚未記錄。</p>`);
  }
}

window.SWScan = { open: open_ };
})();
