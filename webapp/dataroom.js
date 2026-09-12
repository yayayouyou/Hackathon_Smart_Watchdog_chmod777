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
      return "<tr" + (isGroup ? ' class="grp"' : "") + ">"
        + "<td>" + esc(r.label) + note + pct + "</td>"
        + (isGroup ? '<td class="n" colspan="' + Math.max(vals.length, 1) + '"></td>' : tds)
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

  function renderKinds() {
    const box = $("dr-kinds");
    if (!box || !state.ov) return;
    const secs = state.ov.sections || [];
    box.innerHTML = secs.map((s) =>
      '<button type="button" class="preset drchip" data-sec="' + esc(s.key) + '"'
      + (s.key === state.section ? ' aria-pressed="true"' : "")
      + ' title="' + esc(s.zh) + "：" + s.tables + " 張，涵蓋 " + s.reports
      + ' 份報告">' + esc(s.zh) + "<i>" + s.tables + "</i></button>").join("");
  }

  async function showKind(section) {
    state.section = section;
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
      box.innerHTML = '<div class="drcount">' + res.count + " 張"
        + (res.count > full.length ? "，以下顯示前 " + full.length + " 張" : "")
        + "</div>" + html;
    } catch (e) {
      box.innerHTML = '<div class="insuff">讀不到：' + esc(e.message) + "</div>";
    }
  }

  /* ── 原始資料層 ──────────────────────────────────────────────── */

  function docsHTML() {
    const ov = state.ov;
    const reps = ov.reports || [];
    const pend = reps.filter((r) => r.state === "pending");
    const load = reps.filter((r) => r.state !== "pending");

    const row = (r) => '<button type="button" class="item drdoc" data-id="'
      + esc(r.id) + '">'
      + '<span class="ord">' + esc(r.code) + "</span>"
      + '<span class="nm">' + esc(r.short_name)
      + ' <span class="badge">' + esc(r.academic_year) + " 學年度</span></span>"
      + '<span class="rk">' + (r.state === "pending" ? "—" : r.tables + " 張") + "</span>"
      + '<span class="why">' + (r.state === "pending"
        ? '<span class="tag w">待上傳</span>'
        : r.pages + " 頁已抽取 · " + (r.n_issues || 0) + " 則註記"
          + (r.identity_ok ? ' · <span class="tag p">機構已核對</span>' : ""))
      + "</span></button>";

    const pub = ov.public || [];
    const books = pub.filter((p) => !p.is_cover);

    return (pend.length
      ? '<div class="sec"><h4>待上傳</h4>' + pend.map(row).join("") + "</div>" : "")
      + '<div class="sec"><h4>非營利園財報 <span class="badge">'
      + load.length + " 份 · 一份＝一園一學年度</span></h4>"
      + load.map(row).join("") + "</div>"
      + '<div class="sec"><h4>公校決算書 <span class="badge">'
      + books.length + " 冊 · 年度制</span></h4>"
      + '<div class="insuff">一冊含多個分基金，切割依頁尾 &lt;分基金代號&gt;-&lt;頁碼&gt;；'
      + "另有 " + (pub.length - books.length) + " 份封面附件。"
      + "這些沒有頁級抽取，不出現在「數字」層。</div>"
      + books.map((p) => '<div class="row"><span class="r">' + esc(p.period)
        + '</span><span class="m">' + esc(p.filename) + "</span><span>"
        + p.pages + " 頁</span></div>").join("")
      + "</div>";
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
      + '<span>PDF。抽取結果會登錄進下方清單與「數字」層。</span></div>'
      + '<div class="drup-now">目前：<b>' + (t.reports || 0) + "</b> 份報告 · <b>"
      + (t.tables || 0) + "</b> 張表 · <b>" + (t.cells || 0).toLocaleString("en-US")
      + "</b> 格數字</div>"
      + '<label class="drup-btn">選擇 PDF'
      + '<input type="file" id="dr-file" accept="application/pdf" hidden></label>'
      + (pend.length ? '<div class="drup-hint">尚未入庫：'
        + pend.map((p) => esc(p.short_name) + " " + p.academic_year + " 學年度").join("、")
        + "</div>" : "")
      + '<button type="button" class="drup-reset" id="dr-reset">重設</button>'
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
      await load();
      // 先重畫會被總數影響的區塊，最後才寫結果——順序反了結果就會被洗掉。
      renderUpload();
      renderDocs();
      renderKinds();
      const after = state.ov.totals;
      const diff = (k) => '<span class="drd"><i>' + (before[k] || 0).toLocaleString("en-US")
        + "</i> → <b>" + (after[k] || 0).toLocaleString("en-US") + "</b></span>";
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
        + '<div class="drdiff">報告 ' + diff("reports") + "　表 " + diff("tables")
        + "　數字 " + diff("cells") + "　空白 " + diff("blank") + "</div>"
        + '<div class="drup-sec">新增 ' + res.added.tables + " 張表，分屬 "
        + res.added.sections.length + " 種類型："
        + res.added.sections.map((s) => '<button type="button" class="preset drchip" '
          + 'data-sec="' + esc(s.key) + '">' + esc(s.zh) + "</button>").join("")
        + "</div></div>";
    } catch (e) {
      out.innerHTML = '<div class="insuff">' + esc(e.message) + "</div>";
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
      + '<div class="insuff">裁罰、登記與界線快照在輿情室與地圖室。</div>';
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

  window.SWData = { open, showKind, showLayer };
})();
