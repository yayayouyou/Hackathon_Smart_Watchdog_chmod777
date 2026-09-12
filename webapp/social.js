/* 社群聲音（輿情室上半）
 *
 * 這一頁讀的是**已經收進來的**東西：民眾在 Threads 上 @標註官方帳號的通報、
 * 連同那一串底下的回覆，加上新聞與 PTT 的即時提及。底下的掃描主控台是另一
 * 回事——那是花錢去收集更多。先看手上有什麼，再決定要不要花錢，所以社群聲音
 * 在上。
 *
 * 契約在 docs/api/social-panel.md。有四條規則寫在那份文件裡而不只是寫在後端，
 * 因為最可能違反它們的就是這個檔案：
 *
 * **計數不相加。** `counts` 刻意沒有跨來源 total。一串十則「+1」繼承了主貼文
 * 的歸屬，加起來看起來像十個人向教育局反映，實際上標註官方帳號的只有一個。
 * 所以主貼文數與回覆數永遠分開印，中間放「·」不是「+」。
 *
 * **無訊號不是綠燈。** 沒有社群聲音的園，絕大多數是因為那裡的家長不用 Threads。
 * 用 .insuff（虛線框）那一族樣式，不用任何打勾或綠色——style.css 的 --k0 註解
 * 已經把同一件事講過一次：0 件不等於安全，把它畫成綠色等於發了 730 張合格證。
 *
 * **available 與 has_signal 是兩件事。** 管道關掉了，但庫裡仍有先前同步進來的
 * 通報——把兩者壓成一個布林，那些通報就從畫面上消失了。
 *
 * **不輪詢、不預抓。** /api/social/{id} 每次呼叫會打一次 Google Places 計費
 * 查詢。清單頁只呼叫 /api/social（免費），詳情只在使用者點開某一園時才要。
 */
const S = window.SW;

const social = {
  list: null,          // /api/social 的回應
  unattr: null,        // /api/social/unattributed 的回應
  open: null,          // 目前展開的機構 full_id
  detail: {},          // full_id -> /api/social/{id} 的回應（點過才有，不預抓）
  loading: new Set(),
  booted: false,
};

/* 日期只印到「日」。貼文的時分秒在 permalink 點進去看得到，而清單上多出來的
   六個字只會把園名擠掉。 */
const day = (iso) => String(iso || "").slice(0, 10) || "—";

/* 主貼文與回覆的計數。**不相加**，理由見檔頭。 */
function threadCounts(c) {
  if (!c) return "";
  const bits = [];
  if (c.mentions) bits.push(`@標註 ${c.mentions}`);
  if (c.replies) bits.push(`回覆 ${c.replies}`);
  if (!bits.length && c.threads) bits.push(`${c.threads} 串`);
  return bits.join(" · ");
}

function newsCount(c) {
  if (!c || !c.total) return "";
  return `新聞／PTT ${c.total}`;
}

/* attribution_source 有三個值再加一個 null。
   null 是「這一列早於這個欄位」（見 docs/api/social-panel.md §2.4），不是
   「沒有歸屬」——此時 institution_id 有值就等同 own。寫成
   `src === "own" ? "點名" : "附和"` 會把真正的點名標成附和。 */
function sourceTag(row) {
  const src = row.attribution_source;
  if (src === "own" || (src == null && row.institution_id)) {
    return `<span class="tag p">自身指名</span>`;
  }
  if (src === "inherited") return `<span class="tag p">繼承主貼文</span>`;
  return `<span class="tag p">未歸屬</span>`;
}

/* ── 清單 ──────────────────────────────────────────────── */

function rowHtml(it) {
  const t = threadCounts(it.counts && it.counts.threads);
  const n = newsCount(it.counts && it.counts.mentions);
  const meta = [t, n].filter(Boolean).join("　");
  const latest = it.latest || {};
  const open = social.open === it.full_id;
  return `
    <div class="soc-row${open ? " on" : ""}" data-id="${S.esc(it.full_id)}">
      <div class="soc-head">
        <span class="soc-nm">${S.esc(it.title)}</span>
        <span class="soc-town">${S.esc(String(it.town || "").replace("區", ""))}</span>
        <span class="soc-cnt">${S.esc(meta)}</span>
        <span class="soc-day">${day(it.last_activity)}</span>
      </div>
      ${latest.summary
        ? `<div class="soc-latest">${S.esc(latest.summary)}</div>`
        : ""}
    </div>
    <div class="soc-detail" id="soc-d-${S.esc(it.full_id)}" ${open ? "" : "hidden"}></div>`;
}

/* ── 一園的詳情 ───────────────────────────────────────── */

function postHtml(p, isReply) {
  const other = p.is_this_institution === false && p.institution_title
    ? `<span class="soc-other">→ ${S.esc(p.institution_title)}</span>` : "";
  const text = p.text
    ? S.esc(p.text).replace(/\n/g, "<br>")
    : `<i class="soc-empty">（無文字，貼圖或圖片）</i>`;
  return `
    <div class="soc-post${isReply ? " reply" : ""}">
      <div class="soc-by">
        <span class="soc-user">@${S.esc(p.username)}</span>
        <span class="soc-day">${day(p.posted_at)}</span>
        ${sourceTag(p)}${other}
        ${p.permalink
          ? `<a class="soc-link" href="${S.esc(p.permalink)}"
               target="_blank" rel="noopener">原文</a>` : ""}
      </div>
      <div class="soc-text">${text}</div>
    </div>`;
}

/* available 與 has_signal 分開判斷，不壓成一個布林。 */
function blockNote(b, emptyLabel) {
  if (!b) return "";
  if (b.available === false) {
    return `<div class="insuff">此管道未開通：${S.esc(b.reason || "缺少授權")}</div>`;
  }
  if (b.has_signal === false) {
    return `<div class="insuff">${S.esc(b.reason || emptyLabel)}</div>`;
  }
  return "";
}

function detailHtml(d) {
  let h = "";

  const th = d.threads || {};
  h += `<div class="sec"><h4>Threads @標註通報</h4>`;
  h += blockNote(th, "此管道目前無訊號。無訊號不等於無異常。");
  for (const item of th.items || []) {
    h += `<div class="soc-thread">`;
    h += item.root
      ? postHtml(item.root, false)
      : `<div class="insuff">主貼文不在庫裡：${
          S.esc(item.root_missing_reason || "未同步")}</div>`;
    for (const r of item.replies || []) h += postHtml(r, true);
    h += `</div>`;
  }
  h += `</div>`;

  const mn = d.mentions || {};
  h += `<div class="sec"><h4>新聞與 PTT</h4>`;
  h += blockNote(mn, "查無指名這一園的公開新聞或討論。");
  for (const m of mn.items || []) {
    h += `<div class="soc-post">
      <div class="soc-by"><span class="soc-user">${S.esc(m.publisher || m.channel)}</span>
        <span class="soc-day">${day(m.published)}</span>
        ${m.url ? `<a class="soc-link" href="${S.esc(m.url)}"
          target="_blank" rel="noopener">原文</a>` : ""}</div>
      <div class="soc-text">${S.esc(m.headline)}</div></div>`;
  }
  if ((mn.skipped || []).length) {
    h += `<div class="soc-skip">未執行：` + mn.skipped
      .map((s) => `${S.esc(s.key)}（${S.esc(s.reason)}）`).join("、") + `</div>`;
  }
  h += `</div>`;

  /* Google 評論的 note 原樣印出。它記的是實測結果——評分對裁罰沒有鑑別力
     ——把它省略，星等就會被讀成風險指標。 */
  const rv = d.reviews || {};
  h += `<div class="sec"><h4>Google 地圖評論</h4>`;
  if (rv.available === false) {
    h += `<div class="insuff">${S.esc(rv.reason || "未設定金鑰")}</div>`;
  } else {
    h += `<div class="soc-rv">評分 ${rv.rating ?? "—"}
      （${S.nf(rv.review_count)} 則）</div>`;
    if (rv.note) h += `<div class="insuff">${S.esc(rv.note)}</div>`;
  }
  h += `</div>`;

  if (d.disclaimer) h += `<div class="soc-dis">${S.esc(d.disclaimer)}</div>`;
  return h;
}

async function toggle(fullId) {
  const box = S.$(`soc-d-${fullId}`);
  if (!box) return;
  if (social.open === fullId) {          // 再點一次收起，不重抓
    social.open = null;
    box.hidden = true;
    document.querySelectorAll(".soc-row.on").forEach((n) => n.classList.remove("on"));
    return;
  }
  social.open = fullId;
  render();

  if (social.detail[fullId]) return;     // 已經抓過就不再花一次 Places 查詢
  if (social.loading.has(fullId)) return;
  social.loading.add(fullId);
  const target = S.$(`soc-d-${fullId}`);
  if (target) target.innerHTML = `<div class="soc-load">載入中…</div>`;
  try {
    social.detail[fullId] = await S.api(`/api/social/${fullId}`);
  } catch (e) {
    social.detail[fullId] = { _error: e.message };
  } finally {
    social.loading.delete(fullId);
    render();
  }
}

/* ── 待人工認園 ───────────────────────────────────────── */

function unattrHtml() {
  const u = social.unattr;
  if (!u || !u.count) return "";
  let h = `<div class="soc-unattr"><h4>待人工認園 ${u.count} 則</h4>`;
  h += `<div class="soc-unattr-note">${S.esc(u.note || "")}</div>`;
  for (const r of u.items || []) {
    h += `<div class="soc-post">
      <div class="soc-by"><span class="soc-user">@${S.esc(r.username)}</span>
        <span class="soc-day">${day(r.posted_at)}</span>
        ${r.permalink ? `<a class="soc-link" href="${S.esc(r.permalink)}"
          target="_blank" rel="noopener">原文</a>` : ""}</div>
      <div class="soc-text">${S.esc(r.text).replace(/\n/g, "<br>")}</div>
      <div class="soc-why">拒配理由：${S.esc(r.attribution_basis || "—")}</div>
    </div>`;
  }
  return h + `</div>`;
}

/* ── 版面 ──────────────────────────────────────────────── */

function render() {
  const wrap = S.$("socialwrap");
  if (!wrap) return;
  const l = social.list;
  if (!l) { wrap.innerHTML = `<div class="soc-load">載入中…</div>`; return; }

  let h = `<div class="soc-bar">
      <span class="soc-title">社群聲音</span>
      <span class="soc-sub">${l.matched} 所園近期有公開內容
        ${l.coverage ? `／全市 ${S.nf(l.coverage.institutions_total)} 所` : ""}</span>
    </div>`;

  if (!l.count) {
    h += `<div class="insuff">目前沒有任何已收進來的社群內容。
      這是「此管道無訊號」，不是「全市無異常」。</div>`;
  }
  for (const it of l.items || []) h += rowHtml(it);
  h += unattrHtml();
  wrap.innerHTML = h;

  // 展開中的那一園，把詳情填進去
  if (social.open) {
    const box = S.$(`soc-d-${social.open}`);
    const d = social.detail[social.open];
    if (box) {
      box.hidden = false;
      box.innerHTML = d
        ? (d._error ? `<div class="insuff">讀取失敗：${S.esc(d._error)}</div>`
                    : detailHtml(d))
        : `<div class="soc-load">載入中…</div>`;
    }
  }

  wrap.querySelectorAll(".soc-row").forEach((n) =>
    n.addEventListener("click", () => toggle(n.dataset.id)));
}

/* 進房時呼叫。清單本身是免費的（只讀資料庫），詳情不預抓。 */
async function open() {
  if (social.booted) { render(); return; }
  social.booted = true;
  render();
  try {
    const [list, unattr] = await Promise.all([
      S.api("/api/social?limit=50"),
      S.api("/api/social/unattributed?limit=20"),
    ]);
    social.list = list;
    social.unattr = unattr;
  } catch (e) {
    social.booted = false;                // 失敗要能重試，不是永久黑掉
    const wrap = S.$("socialwrap");
    if (wrap) wrap.innerHTML = `<div class="insuff">社群聲音讀取失敗：${
      S.esc(e.message)}</div>`;
    return;
  }
  render();
}

window.SWSocial = { open };
