/* 資料室：原始資料、依表單類型分類的數字、以及一份原件的入庫。
 *
 * 這一室只回答「這份文件上印的是什麼」。判讀（同儕比較、風險分數、法遵結論）
 * 收在右上角的小選單裡，刻意不佔版面——那些屬於卷宗與派工提案。
 *
 * 檔名只能是純小寫字母：tests/test_frontend_smoke.py 的正則是
 * /static/([a-z]+)\.js，`data-room.js` 會被靜默略過，看起來綠但其實沒被保護。
 */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    ov: null,          // /api/dataroom/overview
    section: null,     // 目前選的表單類型
    family: null,      // 目前展開的家族
    report: "",        // 目前限定的報告（空＝全部）
    year: "",
    loaded: false,
  };

  /* ── 數字格式 ────────────────────────────────────────────────────
   * null 一律印「—」，絕不印 0。「業務發展費預算欄空白」＝未編列預算，
   * 是一項稽查發現；印成 0 是對真實機構的錯誤財務陳述。
   * 括號是負數，而且只加括號不上色——`--seal` 在這個 app 專指
   * 「法遵未通過／回測命中」，拿來標負數會把制度性的負餘絀讀成違規。 */
  function nf(v) {
    if (v === null || v === undefined) return "—";
    const n = Number(v);
    if (!isFinite(n)) return String(v);
    const s = Math.abs(n).toLocaleString("en-US");
    return n < 0 ? "(" + s + ")" : s;
  }

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(path, opts) {
    const r = await fetch(path, opts);
    if (!r.ok) {
      let detail = r.status + "";
      try { detail = (await r.json()).detail || detail; } catch (e) { /* 非 JSON */ }
      throw new Error(detail);
    }
    return r.json();
  }

  /* ── 表單渲染器 ──────────────────────────────────────────────────
   * 四種，因為同一套金額渲染器套下去會產生錯誤的財務陳述。 */

  // 勾選欄：會計師查核附表把「是／否／不適用」印成欄。逐欄判定而不是逐表，
  // 因為同一批表裡混著真實金額——用 section 當開關會把 746,745,466 畫成一個勾。
  const TICK_WORDS = ["是否符合", "不適用", "是", "否"];
  function isTickColumn(label) {
    const flat = String(label || "").replace(/\s/g, "");
    if (!flat) return false;
    return TICK_WORDS.some((w) => flat === w || flat.endsWith("－" + w)
      || flat.endsWith("-" + w));
  }

  // 非金額列：與教保費收入同表的「全期核准招生人數」「營運月數」等。
  // 加千分位與「元」會錯。
  const COUNT_WORDS = ["人數", "月數", "班數", "年限", "日期", "比率", "％", "%"];
  function isCountRow(label) {
    const flat = String(label || "").replace(/\s/g, "");
    return COUNT_WORDS.some((w) => flat.includes(w));
  }

  function cellHTML(v, opts) {
    if (opts.tick) {
      // 勾記只接受 null / 1 / -1 三種值。其他一律退回數字並標存疑——
      // 模型自己在 issues 寫過「V 以 -1 表示僅作標記用」，把一個四位數
      // 畫成乾淨的勾，就是「看似合理的錯誤數字無法被發現」。
      if (v === null || v === undefined) return '<td class="n blank">—</td>';
      if (v === 1 || v === -1) return '<td class="n tick">✓</td>';
      return '<td class="n odd" title="本欄應為勾記，卻抽到數值，需人工確認">'
        + esc(nf(v)) + " ⚠</td>";
    }
    if (v === null || v === undefined) {
      return '<td class="n blank" title="空白＝未編列，不是 0">—</td>';
    }
    const neg = Number(v) < 0;
    const txt = opts.plain ? String(v) : nf(v);
    return '<td class="n' + (neg ? " neg" : "") + '">' + esc(txt) + "</td>";
  }

  function tableHTML(t) {
    const labels = t.period_labels || [];
    const ticks = labels.map(isTickColumn);

    // 標題三段：系統給的類別名（灰）· 原件逐字 · 頁碼。逐字部分與系統名
    // 必須看得出差別，否則違反「原件逐字照抄」那條鐵則。
    const verbatim = t.title || t.context_heading || "";
    const head = '<div class="drth">'
      + '<span class="drk">' + esc(t.section_zh) + "</span>"
      + (verbatim ? '<b class="drv">' + esc(verbatim) + "</b>" : "")
      + (t.section_inherited
        ? '<span class="tag w" title="這張表沒有標題，類別由前一頁的表推論而來">類別為推論</span>' : "")
      + '<span class="drpg">p.' + esc(t.printed_page)
      + '<span class="drpg2"> · PDF p.' + esc(t.pdf_page) + "</span></span>"
      + "</div>";

    // 單位徽章：unit 不含「元」字（例如「金額」）視同未標示，不顯示。
    const unit = (t.unit && String(t.unit).includes("元")) ? t.unit : "";
    const meta = '<div class="drtm">'
      + (unit ? '<span class="badge">' + esc(unit) + "</span>" : "")
      + '<span class="drcite">' + esc(t.report) + " · " + esc(t.uid) + "</span>"
      + (t.pdf_page
        ? ' <button type="button" class="drjump" data-report="' + esc(t.report)
          + '" data-page="' + esc(t.pdf_page) + '">看原件這一頁</button>' : "")
      + "</div>";

    if (!t.aligned) {
      return '<div class="sec dr-t">' + head
        + '<div class="drrefuse">欄位對不齊，已拒收，這張表的數字沒有進資料庫。'
        + "抽出來的內容保留在原件頁上，可點右側看原件。</div>" + meta + "</div>";
    }

    const th = labels.map((l) => "<th>" + esc(l) + "</th>").join("");
    const body = (t.rows || []).map((r) => {
      const vals = r.values || [];
      // 整列皆空且沒有占比 → 這是版面的區段標題（例如「流動資產」），
      // 不是「未編列」。印成 —（未編列）會在每張資產負債表憑空生出
      // 四到六個假的稽查發現。
      const allNull = vals.length > 0 && vals.every((v) => v === null);
      const isGroup = allNull && !r.percents;
      const plain = isCountRow(r.label);
      const tds = vals.length
        ? vals.map((v, i) => cellHTML(v, { tick: ticks[i], plain })).join("")
        : '<td class="n blank">—</td>';
      const pct = (r.percents || []).some((p) => p !== null && p !== undefined)
        ? '<div class="drpct">' + (r.percents || [])
          .map((p) => (p === null || p === undefined ? "—" : p + "%")).join(" · ")
          + "</div>" : "";
      const note = r.note_ref
        ? '<sup class="drnote">' + esc(r.note_ref) + "</sup>" : "";
      // 區段標題列逐欄補空 td 而不是 colspan：結構與其他列完全一致，
      // hover 與底色就不會有一格對不上。
      //
      // class 是 drgrp 不是 grp：style.css 早就有一個全域 `.grp{display:flex}`
      // （版面用的），套到 <tr> 上會讓每個儲存格變成 flex item、各自撐成整列寬
      // 再往下堆——實測區段列被排成 978x11 三層。這一室的 class 一律帶 dr 前綴。
      return "<tr" + (isGroup ? ' class="drgrp"' : "") + ">"
        + "<td>" + esc(r.label) + note + pct + "</td>"
        + (isGroup ? vals.map(() => '<td class="n"></td>').join("") : tds)
        + "</tr>";
    }).join("");

    // 淨值變動表的欄是餘絀類別、列才是日期，與其他表相反。加一行說明而不是
    // 自動轉置——轉了就跟原件對不上。
    const axis = t.section === "equity_change"
      ? '<div class="draxis">這張表的欄是餘絀類別、列是日期，與其他報表相反（原件即如此，未轉置）。</div>'
      : "";

    const issues = (t.page_issues || t.issues || []);
    const foot = issues.length
      ? '<div class="drissues"><b>抽取註記</b>'
        + issues.map((x) => "<p>" + esc(x) + "</p>").join("")
        + "</div>" : "";

    return '<div class="sec dr-t">' + head + axis
      + '<div class="ftabwrap"><table class="ftab"><thead><tr><th>項目</th>'
      + th + "</tr></thead><tbody>" + body + "</tbody></table></div>"
      + meta + foot + "</div>";
  }

  /* ── 數字層 ──────────────────────────────────────────────────── */

  /* 選單兩層：六個家族鈕常駐，下面只展開選中那一族。
     31 種攤平成一面牆需要 6 行才放得下，會把表格擠出畫面；而攤平之後也沒有
     結構——要找「支出類」得用眼睛掃過整面。兩層之後同族內換表仍是一次點擊，
     跨族兩次，而且六個家族本身就先回答了「我們抽到哪幾類資料」。

     ⚠️ 家族的顏色是分類不是分級。六個色的明度刻意相近，不可讓任何一族看起來
     比另一族「嚴重」——這一室只做原件轉錄，沒有風險判讀。 */
  function families() {
    const groups = [];
    (state.ov.sections || []).forEach((s) => {
      const last = groups[groups.length - 1];
      if (last && last.family === s.family) last.items.push(s);
      else groups.push({ family: s.family, zh: s.family_zh, items: [s] });
    });
    return groups;
  }

  function renderKinds() {
    const box = $("dr-kinds");
    if (!box || !state.ov) return;
    const groups = families();
    if (!groups.length) { box.innerHTML = ""; return; }
    const cur = groups.find((g) => g.family === state.family) || groups[0];

    const tabs = groups.map((g) => {
      const n = g.items.reduce((a, b) => a + b.tables, 0);
      return '<button type="button" class="drfambtn" data-f="' + esc(g.family) + '"'
        + (g.family === cur.family ? ' aria-pressed="true"' : "")
        + ' title="' + esc(g.zh) + "：" + g.items.length + " 種表單、" + n
        + ' 張">' + esc(g.zh) + "<i>" + n + "</i></button>";
    }).join("");

    const chips = cur.items.map((s) =>
      '<button type="button" class="drchip" data-sec="' + esc(s.key) + '"'
      + (s.key === state.section ? ' aria-pressed="true"' : "")
      + ' title="' + esc(s.zh) + "：" + s.tables + " 張，涵蓋 " + s.reports
      + ' 份報告">' + esc(s.zh) + "<i>" + s.tables + "</i></button>").join("");

    box.innerHTML = '<div class="drfams">' + tabs + "</div>"
      + '<div class="drtypes" data-f="' + esc(cur.family) + '">' + chips + "</div>";
  }

  async function showKind(section) {
    state.section = section;
    const hit = (state.ov.sections || []).find((s) => s.key === section);
    if (hit) state.family = hit.family;
    renderKinds();
    const box = $("dr-tables");
    if (!box) return;
    box.innerHTML = '<div class="soc-load">載入中…</div>';
    const q = new URLSearchParams({ section, limit: "12" });
    if (state.report) q.set("institution", state.report);
    if (state.year) q.set("year", state.year);
    try {
      const res = await api("/api/dataroom/tables?" + q.toString());
      if (!res.count) {
        box.innerHTML = '<div class="insuff">這個條件下沒有這一種表。'
          + "這代表我們沒有抽到，不代表機構沒有編列——資料不足，不是低風險。</div>";
        return;
      }
      const full = await Promise.all(res.tables.slice(0, 8).map((t) =>
        api("/api/dataroom/table?uid=" + encodeURIComponent(t.uid))));
      // 頁級註記對同一頁的每張表都一樣，逐張重印是雜訊：只在該頁第一張印。
      const seen = new Set();
      const html = full.map((t) => {
        const key = t.report + "/" + t.pdf_page;
        const dup = seen.has(key);
        seen.add(key);
        return tableHTML(dup ? Object.assign({}, t, { page_issues: [] }) : t);
      }).join("");
      // 總數用 `total`，不是 `count`——後者是套用 limit 之後剩下幾張。
      // 用 count 的話，符合 25 張、請求 12 張時畫面會寫「12 張」，而助理用
      // 同一份資料講 25 張，兩邊對不起來。
      const total = res.total == null ? res.count : res.total;
      box.innerHTML = '<div class="drcount">' + total + " 張"
        + (total > full.length ? "，以下顯示前 " + full.length + " 張" : "")
        + "</div>" + html;
    } catch (e) {
      box.innerHTML = '<div class="insuff">讀不到：' + esc(e.message) + "</div>";
    }
  }

  /* ── 原始資料層 ──────────────────────────────────────────────── */

  /* 涵蓋矩陣：列是園、欄是學年度。
     換掉原本那條 132 列的平坦清單，是因為平坦清單答不出這一室最該先答的問題
     ——「我們缺哪幾個園、哪幾年」。矩陣一眼就看得到缺口的形狀。 */
  function matrixHTML() {
    const reps = state.ov.reports || [];
    const years = [...new Set(reps.map((r) => r.academic_year))].sort();
    const byCode = new Map();
    reps.forEach((r) => {
      if (!byCode.has(r.code)) {
        byCode.set(r.code, { code: r.code, name: r.short_name, cells: {} });
      }
      byCode.get(r.code).cells[r.academic_year] = r;
    });
    const rows = [...byCode.values()].sort((a, b) => a.code.localeCompare(b.code));
    const have = reps.filter((r) => r.state !== "pending").length;
    const slots = rows.length * years.length;
    // 待上傳的格子有財報、只是還沒入庫，不能算進「沒有財報」那一堆。
    const empty = slots - reps.length;
    const waiting = reps.length - have;

    const head = '<tr><th class="mx-n">園所</th>'
      + years.map((y) => '<th>' + y + "</th>").join("")
      + "<th class=\"mx-t\">合計</th></tr>";

    const body = rows.map((r) => {
      const tds = years.map((y) => {
        const c = r.cells[y];
        if (!c) {
          return '<td class="mx-c mx-none" title="這一學年度沒有公開財報：'
            + '可能尚未成立或未申報。資料不足，不是合規證明。">—</td>';
        }
        if (c.state === "pending") {
          return '<td class="mx-c mx-wait" title="尚未入庫，上傳原件後才會進來">'
            + "待上傳</td>";
        }
        return '<td class="mx-c mx-has"><button type="button" class="drdoc" '
          + 'data-id="' + esc(c.id) + '" title="' + esc(c.id) + "：" + c.pages
          + " 頁、" + c.tables + ' 張表">' + c.tables + "</button></td>";
      }).join("");
      const tot = years.reduce((a, y) => a + ((r.cells[y] || {}).tables || 0), 0);
      return '<tr><th class="mx-n"><span class="mx-code">' + esc(r.code)
        + "</span>" + esc(r.name) + "</th>" + tds
        + '<td class="mx-c mx-t">' + tot + "</td></tr>";
    }).join("");

    const foot = '<tr><th class="mx-n">各年度</th>'
      + years.map((y) => '<td class="mx-c mx-t">'
        + reps.filter((r) => r.academic_year === y && r.state !== "pending").length
        + "</td>").join("")
      + '<td class="mx-c mx-t">' + have + "</td></tr>";

    return '<div class="sec"><h4>抽取涵蓋 <span class="badge">'
      + rows.length + " 園 × " + years.length + " 學年度</span></h4>"
      + '<p class="drlead">' + slots + " 個可能的園－學年度裡，"
      + "<b>" + have + "</b> 個有公開財報並已逐頁抽取"
      + (waiting ? "，<b>" + waiting + "</b> 個待上傳" : "")
      + "。空的 <b>" + empty + "</b> 格沒有財報——資料不足，不是合規證明。</p>"
      + '<div class="mxwrap"><table class="mx"><thead>' + head + "</thead>"
      + "<tbody>" + body + "</tbody><tfoot>" + foot + "</tfoot></table></div></div>";
  }

  function publicHTML() {
    const pub = state.ov.public || [];
    const books = pub.filter((p) => !p.is_cover);
    return '<div class="sec"><h4>公校決算書 <span class="badge">'
      + books.length + " 冊 · 年度制</span></h4>"
      + '<div class="insuff">一冊含多個分基金，切割依頁尾 &lt;分基金代號&gt;-&lt;頁碼&gt;；'
      + "另有 " + (pub.length - books.length) + " 份封面附件。"
      + "這些走座標抽取、沒有頁級表格，不出現在「數字」層。</div>"
      + books.map((p) => '<div class="row"><span class="r">' + esc(p.period)
        + '</span><span class="m">' + esc(p.filename) + "</span><span>"
        + p.pages + " 頁</span></div>").join("")
      + "</div>";
  }

  function docsHTML() {
    return matrixHTML() + publicHTML();
  }

  async function showDoc(id) {
    const box = $("dr-docpages");
    if (!box) return;
    box.hidden = false;
    box.innerHTML = '<div class="soc-load">載入中…</div>';
    try {
      const r = await api("/api/dataroom/report/" + encodeURIComponent(id));
      const kinds = {};
      r.tables.forEach((t) => { kinds[t.section_zh] = (kinds[t.section_zh] || 0) + 1; });
      box.innerHTML = '<div class="sec"><h4>' + esc(r.id)
        + ' <span class="badge">' + r.pages.length + " 頁 · " + r.tables.length
        + " 張表</span></h4>"
        + '<div class="kv"><span>抽取模型</span><span>' + esc((r.models || []).join("、"))
        + "</span><span>解析度</span><span>" + esc((r.dpi || []).join("、"))
        + " dpi</span></div>"
        + '<div class="drchips">' + Object.entries(kinds).map(([k, n]) =>
          '<span class="preset">' + esc(k) + "<i>" + n + "</i></span>").join("")
        + "</div>"
        + r.pages.map((p) => '<button type="button" class="row drpage" data-report="'
          + esc(r.id) + '" data-page="' + p.pdf_page + '">'
          + '<span class="r">p.' + p.pdf_page + "</span>"
          + '<span class="m">' + esc(p.page_kind) + " · " + p.n_tables + " 表"
          + (p.issues.length ? " · " + p.issues.length + " 則註記" : "") + "</span>"
          + "<span>" + (p.identity_ok ? "已核對" : "未核對") + "</span></button>").join("")
        + "</div>";
    } catch (e) {
      box.innerHTML = '<div class="insuff">' + esc(e.message) + "</div>";
    }
  }

  function showPage(report, page) {
    const box = $("dr-evidence");
    if (!box) return;
    box.hidden = false;
    box.innerHTML = '<div class="evh">' + esc(report) + " p." + esc(page)
      + '<button type="button" class="drclose" id="dr-evclose">關閉</button></div>'
      + '<img class="drimg" alt="原件第 ' + esc(page) + ' 頁" src="/api/dataroom/page?report='
      + encodeURIComponent(report) + "&page=" + encodeURIComponent(page) + '">';
    const c = $("dr-evclose");
    if (c) c.addEventListener("click", () => { box.hidden = true; });
  }

  /* ── 上傳 ────────────────────────────────────────────────────── */

  function uploadHTML() {
    const pend = (state.ov.reports || []).filter((r) => r.state === "pending");
    const t = state.ov.totals || {};
    return '<div class="drup">'
      + '<div class="drup-h"><b>上傳財務報告</b>'
      + '<span>PDF，檔名不限。庫裡已有的原件直接入庫；新的報告會自動抽取，約 2–3 分鐘。</span></div>'
      + '<div class="drup-now">目前：<b>' + (t.reports || 0) + "</b> 份報告 · <b>"
      + (t.tables || 0) + "</b> 張表 · <b>" + (t.cells || 0).toLocaleString("en-US")
      + "</b> 格數字</div>"
      + '<label class="drup-btn">選擇 PDF'
      + '<input type="file" id="dr-file" accept="application/pdf" hidden></label>'
      // 重設緊跟在按鈕後面（.drup-reset 的 margin-left 就是為此）。放在提示之後的話
      // 它會落到提示底下自成一行，縮排 10px 對不到任何東西。
      + '<button type="button" class="drup-reset" id="dr-reset">重設</button>'
      + (pend.length ? '<div class="drup-hint">尚未入庫：'
        + pend.map((p) => esc(p.short_name) + " " + p.academic_year + " 學年度").join("、")
        + "</div>" : "")
      + "</div>";
  }

  async function doUpload(file) {
    const out = $("dr-upresult");
    const before = Object.assign({}, state.ov.totals);
    out.innerHTML = '<div class="thinking"><span class="spinner"></span>處理中…</div>';
    const fd = new FormData();
    fd.append("file", file);
    try {
      const res = await api("/api/dataroom/upload", { method: "POST", body: fd });
      if (res.status === "extracting") return watchJob(res.job.id, before);
      await showResult(res, before);
    } catch (e) {
      out.innerHTML = '<div class="insuff">' + esc(e.message) + "</div>";
    }
  }

  /* 入庫結果。直接認出的原件與抽取完成的新報告，畫面長一樣。 */
  async function showResult(res, before) {
    const out = $("dr-upresult");
    if (res.status === "already_loaded") {
      out.innerHTML = '<div class="insuff">' + esc(res.detail) + "</div>";
      return;
    }
      await load();
    // 先重畫會被總數影響的區塊，最後才寫結果——順序反了結果就會被洗掉。
    renderUpload();
    renderDocs();
    renderKinds();
    const after = state.ov.totals;
    // 標籤與數字包在同一個 nowrap 裡：分開的話「空白」會留在上一行、
    // 數字掉到下一行，而這四組數字正是上傳前後最該一眼看完的東西。
    const diff = (label, k) => '<span class="drd">' + label + " <i>"
      + (before[k] || 0).toLocaleString("en-US") + "</i> → <b>"
      + (after[k] || 0).toLocaleString("en-US") + "</b></span>";
    out.innerHTML = '<div class="drup-ok">'
      + "<b>" + esc(res.institution) + " " + esc(res.academic_year)
      + " 學年度</b> 已入庫"
      + '<div class="kv">'
      + "<span>檔案</span><span>" + esc(res.upload.filename) + " · "
      + (res.upload.bytes / 1048576).toFixed(1) + " MB · "
      + esc(res.upload.pdf_pages) + " 頁</span>"
      + "<span>SHA-256</span><span class=\"drsha\">" + esc(res.upload.sha256) + "</span>"
      + "<span>抽取模型</span><span>" + esc((res.provenance.models || []).join("、"))
      + " · " + esc((res.provenance.dpi || []).join("、")) + " dpi</span>"
      + "<span>機構核對</span><span>" + (res.added.identity_ok
        ? "全部頁面的頁尾代號與期望代號相符" : "有頁面未通過，已隔離") + "</span>"
      + "</div>"
      + '<div class="drdiff">' + diff("報告", "reports") + diff("表", "tables")
      + diff("數字", "cells") + diff("空白", "blank") + "</div>"
      + '<div class="drup-sec">新增 ' + res.added.tables + " 張表，分屬 "
      + res.added.sections.length + " 種類型："
      + res.added.sections.map((s) => '<button type="button" class="preset drchip" '
        + 'data-sec="' + esc(s.key) + '">' + esc(s.zh) + "</button>").join("")
      + "</div></div>";
  }

  /* 新報告在伺服器背景抽取（intake.py），這裡每兩秒問一次進度。
   * 同一份工作只追一次：上傳按鈕與助理可能先後叫到同一個 job。 */
  const watching = new Set();
  async function watchJob(id, before) {
    if (watching.has(id)) return;
    watching.add(id);
    before = before || Object.assign({}, state.ov.totals);
    const out = $("dr-upresult");
    try {
      for (;;) {
        const j = await api("/api/dataroom/jobs/" + encodeURIComponent(id));
        if (j.status === "extracting") {
          out.innerHTML = '<div class="thinking"><span class="spinner"></span>'
            + esc(j.filename) + " 抽取中：" + j.pages_done + " / " + j.pages_total + " 頁</div>";
          await new Promise((r) => setTimeout(r, 2000));
          continue;
        }
        if (j.status === "done") await showResult(j.result, before);
        else out.innerHTML = '<div class="insuff">' + esc(j.filename) + "："
          + esc(j.detail || j.status) + "</div>";
        return;
      }
    } catch (e) {
      out.innerHTML = '<div class="insuff">' + esc(e.message) + "</div>";
    } finally {
      watching.delete(id);
    }
  }

  /* ── 版面 ────────────────────────────────────────────────────── */

  function renderUpload() {
    const box = $("dr-upload");
    if (box) box.innerHTML = uploadHTML();
  }

  function renderDocs() {
    const box = $("dr-docs");
    if (box) box.innerHTML = docsHTML();
    const side = $("dr-docpages");
    if (side && side.hidden) {
      side.hidden = false;
      side.innerHTML = '<div class="insuff">點左邊任一份報告，看它逐頁抽到什麼，'
        + "以及每一頁的頁尾代號是否與期望代號相符。</div>";
    }
  }

  function showLayer(name) {
    ["raw", "num"].forEach((n) => {
      const el = $("dr-" + n);
      if (el) el.hidden = n !== name;
    });
    document.querySelectorAll("#dr-layer button").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.l === name));
    });
  }

  function renderMore() {
    const pop = $("drmorepop");
    if (!pop || !state.ov) return;
    const t = state.ov.totals || {};
    pop.innerHTML = "<h4>判讀</h4>"
      + '<div class="insuff">同儕差距、法遵檢核與風險排序都在卷宗與派工提案裡。'
      + "資料室只做原件轉錄，不做判讀。</div>"
      + "<h4>抽取品質</h4>"
      + '<div class="kv"><span>拒收的表</span><span>' + (t.refused || 0)
      + " 張（欄位對不齊，數字未入庫）</span>"
      + "<span>空白格</span><span>" + (t.blank || 0).toLocaleString("en-US")
      + " 格（未編列，不是 0）</span></div>"
      + '<button type="button" class="preset" id="dr-notes">看抽取註記</button>'
      + '<div id="dr-notesbox"></div>'
      + "<h4>外部佐證</h4>"
      + '<div class="insuff">裁罰、登記與界線快照在「輿情蒐集」與「全市監看」。</div>';
  }

  function railTools() {
    const box = $("rail-tools");
    if (!box || !state.ov) return;
    const reps = (state.ov.reports || []).filter((r) => r.state !== "pending");
    const names = [...new Set(reps.map((r) => r.short_name))].sort();
    const years = [...new Set(reps.map((r) => r.academic_year))].sort();
    box.hidden = false;
    box.innerHTML = '<div class="rail-eyebrow">篩選</div>'
      + '<select id="dr-inst"><option value="">全部園所</option>'
      + names.map((n) => '<option' + (n === state.report ? " selected" : "")
        + ">" + esc(n) + "</option>").join("") + "</select>"
      + '<select id="dr-year"><option value="">全部學年度</option>'
      + years.map((y) => '<option value="' + y + '"'
        + (String(y) === String(state.year) ? " selected" : "")
        + ">" + y + " 學年度</option>").join("") + "</select>";
    $("dr-inst").addEventListener("change", (e) => {
      state.report = e.target.value; if (state.section) showKind(state.section);
    });
    $("dr-year").addEventListener("change", (e) => {
      state.year = e.target.value; if (state.section) showKind(state.section);
    });
  }

  async function load() {
    state.ov = await api("/api/dataroom/overview");
    const t = state.ov.totals || {};
    const src = $("dr-src");
    if (src) {
      src.textContent = "data/extracted · " + (t.reports || 0) + " 份報告 · "
        + (t.tables || 0) + " 張表";
    }
    const nr = $("dr-n-raw");
    const nn = $("dr-n-num");
    if (nr) nr.textContent = (state.ov.reports || []).length;
    if (nn) nn.textContent = t.tables || 0;
  }

  async function open() {
    if (state.loaded) { railTools(); return; }
    const box = $("dr-tables");
    if (box) box.innerHTML = '<div class="soc-load">載入中…</div>';
    try {
      await load();
    } catch (e) {
      if (box) {
        box.innerHTML = '<div class="insuff">資料室讀不到資料：' + esc(e.message)
          + "</div>";
      }
      return;
    }
    state.loaded = true;
    renderUpload();
    renderDocs();
    renderKinds();
    renderMore();
    railTools();
    const secs = state.ov.sections || [];
    const prefer = secs.find((s) => s.key === "tuition_income") || secs[0];
    if (prefer) await showKind(prefer.key);
  }

  /* 一次委派，不對每顆按鈕各綁一次——磁磚與表格都會整塊重畫。 */
  document.addEventListener("click", (e) => {
    const fam = e.target.closest(".drfambtn");
    if (fam) {
      const g = families().find((x) => x.family === fam.dataset.f);
      if (g) { state.family = g.family; showKind(g.items[0].key); }
      return;
    }
    const chip = e.target.closest(".drchip");
    if (chip) { showLayer("num"); showKind(chip.dataset.sec); return; }
    const layer = e.target.closest("#dr-layer button");
    if (layer) { showLayer(layer.dataset.l); return; }
    const doc = e.target.closest(".drdoc");
    if (doc) { showDoc(doc.dataset.id); return; }
    const pg = e.target.closest(".drpage, .drjump");
    if (pg) { showPage(pg.dataset.report, pg.dataset.page); return; }
    const more = e.target.closest("#drmore");
    if (more) {
      const pop = $("drmorepop");
      const on = pop.hidden;
      pop.hidden = !on;
      more.setAttribute("aria-expanded", String(on));
      return;
    }
    if (e.target.closest("#dr-notes")) { loadNotes(); return; }
    if (e.target.closest("#dr-reset")) { doReset(); return; }
  });

  document.addEventListener("change", (e) => {
    if (e.target.id === "dr-file" && e.target.files && e.target.files[0]) {
      doUpload(e.target.files[0]);
    }
  });

  async function loadNotes() {
    const box = $("dr-notesbox");
    if (!box) return;
    box.innerHTML = '<div class="soc-load">載入中…</div>';
    try {
      const res = await api("/api/dataroom/notes?limit=12");
      box.innerHTML = '<div class="drnotes">' + res.notes.map((n) =>
        '<p class="' + (n.unresolved ? "un" : "") + '"><b>' + esc(n.report)
        + " p." + n.pdf_page + "</b> " + esc(n.text) + "</p>").join("")
        + '<small>' + esc(res.note) + "</small></div>";
    } catch (e) {
      box.innerHTML = '<div class="insuff">' + esc(e.message) + "</div>";
    }
  }

  async function doReset() {
    try {
      await api("/api/dataroom/reset", { method: "POST" });
      await load();
      const out = $("dr-upresult");
      if (out) out.innerHTML = "";
      renderUpload();
      renderDocs();
      renderKinds();
      if (state.section) showKind(state.section);
    } catch (e) {
      const out = $("dr-upresult");
      if (out) out.innerHTML = '<div class="insuff">' + esc(e.message) + "</div>";
    }
  }

  /* 讓助理把資料室帶到某一處。
   *
   * 為什麼不是直接呼叫 `showKind()`：那支只認 section，而助理講的通常是
   * 「安溪的財產目錄」——園所與學年度也要跟著限定，否則畫面會列出全 132 園
   * 的同一種表，跟它剛才講的那一所對不起來。
   *
   * 篩選下拉也要一起改。只改 `state` 不改下拉的話，畫面顯示「全部園所」
   * 但列出來的只有安溪——使用者看到的條件與實際套用的不一致，而且他一動
   * 別的條件就會把助理設的悄悄洗掉。
   */
  /* 助理帶使用者來上傳時，把「選擇 PDF」標出來。
   *
   * 助理**不能**替使用者按這顆鈕：瀏覽器只允許使用者親手觸發的點擊打開檔案
   * 選擇視窗，而助理的指令是從串流送來的，不是使用者的點擊（一輪通常要十幾秒，
   * 早就過了瀏覽器給的暫時授權）。所以做法是帶到、標出來，讓人自己按。 */
  function cueUpload() {
    const box = $("dr-upload");
    if (box) box.scrollIntoView({ block: "nearest" });
    const btn = document.querySelector("#dr-upload .drup-btn");
    if (!btn) return;
    btn.classList.remove("drup-cue");
    void btn.offsetWidth;            // 強制重排，連續兩次叫也會重播
    btn.classList.add("drup-cue");
    setTimeout(() => btn.classList.remove("drup-cue"), 4000);
  }

  async function focus(opts) {
    await open();
    const o = opts || {};
    if (o.job || o.refresh) {
      // 助理把附件放進來了：切到原始資料層，重畫清單，新報告就追抽取進度。
      showLayer("raw");
      if (o.refresh) { await load(); renderUpload(); renderDocs(); renderKinds(); }
      if (o.report) showDoc(o.report);   // 直接入庫的那份，打開它的逐頁清單
      if (o.job) watchJob(o.job);
      return;
    }
    if (o.upload) {
      // 上傳區只在「原始資料」層；切過去、標出來就好，不去動表單類型與篩選。
      showLayer("raw");
      cueUpload();
      return;
    }
    if (o.institution !== undefined) state.report = o.institution || "";
    if (o.year !== undefined) state.year = o.year == null ? "" : String(o.year);
    const inst = $("dr-inst"), year = $("dr-year");
    if (inst) inst.value = state.report;
    if (year) year.value = state.year;
    if (o.layer) showLayer(o.layer);
    if (o.section) await showKind(o.section);
    else if (state.section) await showKind(state.section);
  }

  window.SWData = { open, showKind, showLayer, focus };
})();
