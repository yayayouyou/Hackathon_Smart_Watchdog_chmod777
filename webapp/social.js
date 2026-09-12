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
 *
 * **語氣上色在貼文，不在機構。** 每一則貼文依它自己的 tone 上色；機構那一列
 * 印的是**組成**（「3 則語氣負面 · 1 則中性 · 1 則詢問」），不是一個顏色。
 * 把一整園染紅，等於用一批未查證的貼文對一家真實機構下風險判斷——
 * features/alerts.py 整支模組就是為了不製造這種傷害而存在的，它的模組說明記了
 * 四個真實誤配，其中一則是某園在新聞裡被誤認後公開澄清「衰被誤認虐童」。
 *
 * **未分類不是中性。** tone_bucket 為 "unclassified" 的那些要印「未分類」，
 * 不可以 `tone || "neutral"`——那一行會讓一批沒有人看過的貼文一次變成中性。
 * 這個判斷後端已經做過一次（tone_bucket / tone_label），前端照著畫就好。
 *
 * **語氣的顏色刻意只用 --seal 與 --warn，不碰 --c1~c4 與 --k0~k4。**
 * 那兩族已經各自有語意（建議查核密度、歷史裁罰件數），第三套借用它們的色階，
 * 三套就會在同一個畫面上互相打架。每個顏色旁邊一定有文字標籤：一個沒有說明的
 * 紅點，在這個畫面上最容易被猜成「這園有問題」。
 *
 * **新聞的標籤與 Threads 的語氣標籤不共用，計數不合併。** 這是整個檔案裡最容易
 * 被寫錯的一條，因為兩族長得很像：都染 --seal／--warn、都印一排膠囊。但它們講
 * 的是兩件事——Threads 的「語氣負面」是一句**未查證的民眾陳述**聽起來如何；
 * 新聞的「事件報導」是一件**已經作成的官方行動**（起訴、開罰 39 萬、勒令停招）
 * 被報導出來。後者不是情緒。兩者共用同一個標籤，稽查員就分不出該先看哪一則，
 * 而那個分別正是他判斷輕重的依據。所以：新聞走 reportTags()／reportComposition()
 * 與 .soc-rep／.r-* 這一族，文字一律用新聞自己的詞彙（「事件報導」，
 * **不可以寫「語氣負面」**），機構那一列的兩個組成分兩行印、各自帶抬頭，
 * 永遠不加在一起。後端也是分開回的（tone 與 news 是兩個欄位）。
 *
 * **排序只改順序，不改任何計數或標籤。** 預設是「最新活動」而不是「關注程度」，
 * 理由不是保守：抱怨的人會把園名寫完整（要讓機關找得到），稱讚的人寫得隨意，
 * 所以歸屬成功率本身就與語氣相關。把「負面優先」設成永久預設，等於讓一個
 * **部分由歸屬規則造成的**排序每次開啟都排在最前面，讀的人會以為輿情比實際
 * 更負面。做成選項讓人主動選，跟做成預設，是兩件事。
 *
 * **整支包在 IIFE 裡。** 這些是傳統 <script>，全域範圍是共用的——第一版跟著
 * scan.js 寫 `const S = window.SW;` 放在頂層，於是兩個 `const S` 撞在同一個
 * 範圍，`scan.js` 整支解析失敗（Uncaught SyntaxError: Identifier 'S' has
 * already been declared），掃描主控台連帶死掉。memos.js／agent.js／lobby.js
 * 都是這樣包的，換一個沒被用的字母只是把同一顆地雷留給下一個人。
 */
/* 補一點 main 的說明沒涵蓋的：撞名的**函式宣告是靜默互相覆蓋**的，不像同名
 * 的 `const` 會讓後載入的那支整支 SyntaxError、吵得看得見。這一支與
 * timeline.js 都有 `function render()`，包起來之前 social 叫到的其實是
 * timeline 那一支：資料抓到了，畫面卻停在「載入中…」，沒有任何錯誤訊息。
 */
(function () {
const S = window.SW;

/* ── 排序偏好 ──────────────────────────────────────────────
 *
 * 記在 localStorage，稽查員選過一次不用每次重選（沿用 scan.js 的 sw_reviewer
 * 與 agent.js 的 sw.agentw）。**讀寫都要包 try/catch**：Brave 之類的瀏覽器會
 * 擋 localStorage 並直接丟例外，而那個例外會讓整個面板連載都載不出來。 */
const SORT_KEY = "sw.socialsort";
const SORTS = [
  ["recent", "最新活動"],
  ["attention", "關注程度"],
  ["name", "機構名稱"],
  /* 名稱寫「負面則數」而不是「則數」：它排的是事件報導與語氣負面的多寡，
     不是全部內容的多寡。依序比較而非相加，理由見 sortedItems()。 */
  ["concern", "負面則數（事件報導優先）"],
];
const DEFAULT_SORT = "recent";
const SORT_KEYS = SORTS.map((s) => s[0]);

function readSort() {
  try {
    const v = localStorage.getItem(SORT_KEY);
    if (SORT_KEYS.indexOf(v) >= 0) return v;
  } catch { /* 私密視窗或被擋掉的儲存 */ }
  return DEFAULT_SORT;
}

function writeSort(v) {
  try { localStorage.setItem(SORT_KEY, v); } catch { /* 同上 */ }
}

const social = {
  list: null,          // /api/social 的回應
  unattr: null,        // /api/social/unattributed 的回應
  open: null,          // 目前展開的機構 full_id
  detail: {},          // full_id -> /api/social/{id} 的回應（點過才有，不預抓）
  loading: new Set(),
  booted: false,
  sort: readSort(),    // 只改順序，不改任何計數或標籤
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

/* 示範機構的標記。**判斷靠後端給的 is_demo，不靠名字看起來假不假**——
   名字是給人看的，旗標是給程式檢查的（src/smart_watchdog/realtime/demo_data.py）。
   為什麼一定要印：示範貼文與真通報在這個畫面上長得一模一樣，而畫面會被截圖，
   截圖裡沒有人可以問「這一家是真的嗎」。沿用 .tag 的字級（15px），
   虛線框與 .tag.w（真的警示）分得開。 */
function demoTag(isDemo) {
  return isDemo ? `<span class="tag demo">示範資料</span>` : "";
}

/* ── 語氣（realtime/classify.py）────────────────────────── */

/* 這一則該畫成哪一種。後端給的 tone_bucket 是唯一的判準——它已經把
   「跑過分類但看不出來」（tone="unclear"）與「從來沒跑過」（unclassified）
   分開了，前端不要自己用 `p.tone` 重推一次。 */
function toneClass(p) {
  const b = p.tone_bucket || "unclassified";
  if (b === "unclassified") return "t-none";
  if (b === "negative") return "t-neg";
  /* 說法籠統的那些也走琥珀：它不是指控，是「還不夠派工」。 */
  if (b === "question" || p.specificity === "vague") return "t-warn";
  return "t-flat";
}

/* 每個顏色旁邊都要有字。色塊自己不會說話，而這個畫面上的紅色很容易被讀成
   「這園有問題」——實際上它只代表「這一則貼文的語氣是負面的」。 */
function toneTags(p) {
  const b = p.tone_bucket || "unclassified";
  let h = `<span class="soc-tone ${toneClass(p)}">${
    S.esc(p.tone_label || "未分類")}</span>`;
  if (b === "unclassified") return h;
  if (p.specificity === "vague") {
    h += `<span class="soc-tone t-warn">說法籠統</span>`;
  }
  if (p.event_category && p.event_category !== "unclear") {
    h += `<span class="soc-tone t-flat">${S.esc(p.event_category)}</span>`;
  }
  if (p.contains_minor_identifiers) {
    h += `<span class="soc-tone t-warn">可能含兒少可識別資訊</span>`;
  }
  return h;
}

/* 機構那一列印的是組成，不是一個顏色。順序固定，0 的那些不印——
   「0 則語氣負面」讀起來像一張合格證，而我們沒有資格發那種東西。

   **每一段各自上色，但整列不上色。** 這個分別是刻意的：「3 則語氣負面」染紅
   講的是那三則貼文，把整列或園名染紅講的是這一園——後者是用未查證內容對真實
   機構下判斷，正是 `features/alerts.py` 整支模組存在的理由。染在數字上，負面
   多的一眼掃得到，但沒有任何一園被說成有問題。 */
const TONE_ORDER = ["negative", "question", "neutral", "unclear", "unclassified"];
const TONE_CLASS = {
  negative: "t-neg", question: "t-warn", neutral: "t-flat",
  unclear: "t-flat", unclassified: "t-none",
};
function toneComposition(t) {
  if (!t || !t.counts) return "";
  return TONE_ORDER
    .filter((k) => t.counts[k])
    .map((k) => `<span class="soc-seg ${TONE_CLASS[k]}">${t.counts[k]} 則${
      S.esc((t.labels || {})[k] || k)}</span>`)
    .join('<span class="soc-sep">·</span>');
}

/* 整張卡片的底色。判準只看**已分類貼文**的語氣組成，不看則數多寡——
   一則負面的園跟十則負面的園，這裡給的是同一個顏色，因為我們沒有量測過
   「幾則才算嚴重」，發明一個門檻就是發明一個沒有根據的分級。

   未分類不參與計算，而且全未分類時**不給顏色**、直接說「尚未分類」：
   把沒人看過的東西畫成中性，等於替它作保。 */
/* rank 是「關注程度」排序用的位置，順序即 ATTENTION_GROUPS 的順序。
   **未分類（rank 3）排在無負面（rank 4）之前，而且自成一段。**
   tone_bucket 為 unclassified 代表沒有人看過，不是「看過了沒問題」——讓一批
   沒跑過分類的園跟確認無負面的園長得一樣，就是替它們作保。 */
function rowMood(t) {
  const c = (t && t.counts) || {};
  const classified = (c.negative || 0) + (c.question || 0)
    + (c.neutral || 0) + (c.unclear || 0);
  if (!classified) {
    return c.unclassified
      ? { cls: "m-none", label: "貼文語氣：尚未分類", rank: 3 }
      /* 一則 Threads 貼文都沒有的園。它不是「無負面」，是這個依據在它身上
         不存在——卡片的顏色改由新聞那一族決定（見 newsMood）。 */
      : { cls: "", label: "", rank: 5 };
  }
  const neg = c.negative || 0;
  if (neg && neg * 2 >= classified) {
    return { cls: "m-neg", label: "貼文語氣：多數負面", rank: 0 };
  }
  if (neg) return { cls: "m-warn", label: "貼文語氣：部分負面", rank: 1 };
  if ((c.question || 0) * 2 >= classified) {
    return { cls: "m-warn", label: "貼文語氣：以詢問為主", rank: 2 };
  }
  return { cls: "m-flat", label: "貼文語氣：無負面", rank: 4 };
}

/* 新聞那一族的卡片顏色。判準與語氣那族**平行但不互通**：
   `事件報導`（已有官方行動）比一則未查證的抱怨重，所以它是紅的；
   `爭議未定`（有指控、未見官方行動）是琥珀。

   為什麼要有這一支：只有新聞、沒有 Threads 通報的園（福音、吉尼爾這些）
   在 rowMood 眼裡是「這個依據不存在」，於是整張卡片沒有顏色——但那幾間
   正是畫面上最該被看見的，它們有 7 則事件報導。 */
function newsMood(n) {
  const c = (n && n.counts) || {};
  const classified = (c["事件報導"] || 0) + (c["爭議未定"] || 0)
    + (c["例行報導"] || 0) + (c.unclear || 0);
  if (!classified) {
    return c.unclassified
      ? { cls: "m-none", label: "新聞／PTT：尚未分類" } : { cls: "", label: "" };
  }
  const hard = c["事件報導"] || 0;
  if (hard && hard * 2 >= classified) {
    return { cls: "m-neg", label: "新聞／PTT：多數事件報導" };
  }
  if (hard) return { cls: "m-warn", label: "新聞／PTT：部分事件報導" };
  if ((c["爭議未定"] || 0) * 2 >= classified) {
    return { cls: "m-warn", label: "新聞／PTT：以爭議未定為主" };
  }
  return { cls: "m-flat", label: "新聞／PTT：無事件報導" };
}

/* 卡片只有一個底色，但抬頭要說清楚**那個顏色是哪一族給的**。
   兩族同時有內容時兩行都印，顏色取較重的那一個——這不是把兩份計數加起來
   （計數仍然分兩行、各自帶抬頭），是在回答「這張卡片為什麼是紅的」。
   把來源省略，紅色就變成一個沒有出處的判斷。 */
const MOOD_WEIGHT = { "m-neg": 3, "m-warn": 2, "m-none": 1, "m-flat": 0, "": -1 };
function cardMood(it) {
  const tone = rowMood(it.tone);
  const news = newsMood(it.news);
  const labels = [tone.label, news.label].filter(Boolean);
  const cls = MOOD_WEIGHT[news.cls] > MOOD_WEIGHT[tone.cls] ? news.cls : tone.cls;
  return { cls, labels, rank: tone.rank };
}

/* ── 報導性質（realtime/news_classify.py）───────────────────
 *
 * **與上面那一族是兩套東西。** 顏色共用（這個介面只有 --seal 與 --warn 兩個
 * 可用的語意色），但文字一律用新聞自己的詞彙：「事件報導」講的是一件已經作成
 * 的官方行動被報導出來，不是記者的情緒。在這裡寫「語氣負面」會把「有人抱怨」
 * 與「已經起訴」畫上等號，而稽查員正是靠這個分別決定先看哪一則。
 *
 * 後端給的 report_bucket 是唯一的判準——它已經把「跑過但看不出來」（unclear）
 * 與「從來沒跑過」（unclassified）分開了，前端不要自己用 report_kind 重推。 */
const REPORT_ORDER = ["事件報導", "爭議未定", "例行報導", "unclear", "unclassified"];
const REPORT_CLASS = {
  "事件報導": "r-event", "爭議未定": "r-dispute", "例行報導": "r-flat",
  unclear: "r-none", unclassified: "r-none",
};

/* PTT 的 complaint／question 是既有的看板判定（scrape/ptt.py），與模型那一層
   是兩回事，所以它自己一顆膠囊、不覆蓋 report_bucket。投訴文走琥珀：那是一則
   民眾投訴，不是一件官方行動——所以不會是紅的。 */
const PTT_LABELS = { complaint: "PTT 投訴文", question: "PTT 詢問文" };

function reportClass(m) {
  const b = m.report_bucket || "unclassified";
  if (b === "事件報導") return "r-event";
  if (b === "爭議未定") return "r-dispute";
  if (b === "例行報導") return "r-flat";
  /* 性質不明與未分類都走虛線灰，除非 PTT 自己說了這是一則投訴文。 */
  return m.kind === "complaint" ? "r-dispute" : "r-none";
}

/* 每個顏色旁邊都要有字，而且那個字要是新聞的詞彙。 */
function reportTags(m) {
  const b = m.report_bucket || "unclassified";
  let h = `<span class="soc-rep ${REPORT_CLASS[b] || "r-none"}">${
    S.esc(m.report_label || "未分類")}</span>`;
  if (PTT_LABELS[m.kind]) {
    h += `<span class="soc-rep ${m.kind === "complaint" ? "r-dispute" : "r-flat"}">${
      S.esc(PTT_LABELS[m.kind])}</span>`;
  }
  if (m.event_category && m.event_category !== "unclear") {
    h += `<span class="soc-rep r-flat">${S.esc(m.event_category)}</span>`;
  }
  /* 「今天查到的」與「上次全市掃描查到的」是兩件事。不標的話，一則四個月前
     的新聞在畫面上看起來像剛發生（docs/api/social-panel.md §3.3）。 */
  if (m.source === "snapshot") h += `<span class="soc-rep r-flat">上次掃描</span>`;
  return h;
}

/* 新聞那一族的組成。**與 toneComposition() 分開印、分兩行、各自帶抬頭**，
   永遠不加在一起：那邊數的是未查證的民眾陳述，這邊數的是已經見報的報導，
   加總就是把兩者數成同一類。 */
function reportComposition(n) {
  if (!n || !n.counts) return "";
  return REPORT_ORDER
    .filter((k) => n.counts[k])
    .map((k) => `<span class="soc-seg ${REPORT_CLASS[k]}">${n.counts[k]} 則${
      S.esc((n.labels || {})[k] || k)}</span>`)
    .join('<span class="soc-sep">·</span>');
}

/* ── 清單 ──────────────────────────────────────────────── */

function rowHtml(it) {
  const t = threadCounts(it.counts && it.counts.threads);
  const n = newsCount(it.counts && it.counts.mentions);
  const meta = [t, n].filter(Boolean).join("　");
  const comp = toneComposition(it.tone);
  const news = reportComposition(it.news);
  const mood = cardMood(it);
  const latest = it.latest || {};
  const open = social.open === it.full_id;
  return `
    <div class="soc-row ${mood.cls}${open ? " on" : ""}" data-id="${S.esc(it.full_id)}">
      ${/* 整張卡片的底色由**這些貼文的語氣組成**決定，而旁邊那句話說的就是
            這件事：「貼文語氣：多數負面」。它不是風險等級、不是查核順位、
            也不進分數——`06-plan` §1 明訂社群聲量不併入風險分數。
            顏色用了就要有字，否則紅色會被讀成「這園有問題」。 */
        mood.labels.length
          ? mood.labels.map((l) =>
              `<div class="soc-mood ${mood.cls}">${S.esc(l)}</div>`).join("")
          : ""}
      <div class="soc-head">
        <span class="soc-nm">${S.esc(it.title)}</span>
        ${demoTag(it.is_demo)}
        <span class="soc-town">${S.esc(String(it.town || "").replace("區", ""))}</span>
        <span class="soc-cnt">${S.esc(meta)}</span>
        <span class="soc-day">${day(it.last_activity)}</span>
      </div>
      ${/* comp 是本檔產生的標記（每一段各自上色），裡面的文字已經逐段
            S.esc 過了，所以這裡不能再整段跳脫——再跳一次會把標籤印成文字。

            兩個組成**分兩行、各自帶抬頭**。併成一行（甚至只是中間放一個「·」）
            都會讀成同一串數字，而那正是後端把 tone 與 news 分成兩個欄位要避免
            的事：一則未查證的抱怨與一件已經起訴的案子不是同一種東西。 */
        comp ? `<div class="soc-mix soc-comp">
            <span class="soc-cap">貼文語氣</span>${comp}</div>` : ""}
      ${news ? `<div class="soc-mix soc-comp">
            <span class="soc-cap">新聞／PTT</span>${news}</div>` : ""}
      ${latest.summary
        ? `<div class="soc-latest">${S.esc(latest.summary)}</div>`
        : ""}
    </div>
    <div class="soc-detail" id="soc-d-${S.esc(it.full_id)}" ${open ? "" : "hidden"}></div>`;
}

/* ── 一園的詳情 ───────────────────────────────────────── */

function postHtml(p, isReply, instId) {
  const other = p.is_this_institution === false && p.institution_title
    ? `<span class="soc-other">→ ${S.esc(p.institution_title)}</span>${
        demoTag(p.is_demo)}` : "";
  const text = p.text
    ? S.esc(p.text).replace(/\n/g, "<br>")
    : `<i class="soc-empty">（無文字，貼圖或圖片）</i>`;
  /* 「擬定回覆」只長在 kind === "mention" 的主貼文上。串下的回覆是別人在跟
     原 PO 講話，多半沒有標註機關、很可能根本不知道機關在讀——對那種貼文由
     官方帳號回話，是機關自己插進一段別人的對話。 */
  const draft = (p.kind === "mention" && instId)
    ? `<button type="button" class="soc-draft"
         data-draft-root="${S.esc(p.threads_id)}"
         data-draft-inst="${S.esc(instId)}">擬定回覆</button>` : "";
  return `
    <div class="soc-post${isReply ? " reply" : ""} ${toneClass(p)}">
      <div class="soc-by">
        <span class="soc-user">@${S.esc(p.username)}</span>
        <span class="soc-day">${day(p.posted_at)}</span>
        ${sourceTag(p)}${other}
        ${p.permalink
          ? `<a class="soc-link" href="${S.esc(p.permalink)}"
               target="_blank" rel="noopener">原文</a>` : ""}
        ${draft}
      </div>
      <div class="soc-tones">${toneTags(p)}</div>
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
  const instId = (d.institution && d.institution.full_id) || "";

  /* 示範機構先講清楚，講在所有內容之前。展開之後整片都是貼文，
     而列表上那顆 chip 已經捲出畫面了。 */
  if (d.institution && d.institution.is_demo) {
    h += `<div class="soc-demo">示範資料：${S.esc(d.demo_note
      || "本園為示範用虛構園所，不存在於真實主檔。")}</div>`;
  }

  const th = d.threads || {};
  h += `<div class="sec"><h4>Threads @標註通報</h4>`;
  h += blockNote(th, "此管道目前無訊號。無訊號不等於無異常。");
  /* 這一段的語氣組成印在段頭，不是染在機構那一列上。 */
  const mix = toneComposition(th.tone);
  // toneComposition 回的是本檔產生的標記，裡面已逐段跳脫；再 S.esc 一次會把
  // 標籤原樣印在畫面上。兩個呼叫點都要記得，這是第二個。
  if (mix) h += `<div class="soc-mix soc-comp">${mix}</div>`;
  if (th.tone_note) h += `<div class="soc-why">${S.esc(th.tone_note)}</div>`;
  for (const item of th.items || []) {
    h += `<div class="soc-thread">`;
    h += item.root
      ? postHtml(item.root, false, instId)
      : `<div class="insuff">主貼文不在庫裡：${
          S.esc(item.root_missing_reason || "未同步")}</div>`;
    for (const r of item.replies || []) h += postHtml(r, true, instId);
    h += `</div>`;
  }
  h += `</div>`;

  const mn = d.mentions || {};
  h += `<div class="sec"><h4>新聞與 PTT</h4>`;
  h += blockNote(mn, "查無指名這一園的公開新聞或討論。");
  /* 這一段的組成用新聞自己的詞彙，而且**不與上面那段的語氣組成相加**。
     reportComposition 回的是本檔產生的標記（已逐段跳脫），不能再整段 S.esc。 */
  const nmix = reportComposition(mn.labels);
  if (nmix) {
    h += `<div class="soc-mix soc-comp">
      <span class="soc-cap">報導性質</span>${nmix}</div>`;
  }
  if (mn.label_note) h += `<div class="soc-why">${S.esc(mn.label_note)}</div>`;
  for (const m of mn.items || []) {
    h += `<div class="soc-post ${reportClass(m)}">
      <div class="soc-by"><span class="soc-user">${S.esc(m.publisher || m.channel)}</span>
        <span class="soc-day">${day(m.published)}</span>
        ${m.url ? `<a class="soc-link" href="${S.esc(m.url)}"
          target="_blank" rel="noopener">原文</a>` : ""}</div>
      <div class="soc-tones">${reportTags(m)}</div>
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

/* ── 擬定回覆 → 文書室 ──────────────────────────────────
 *
 * 按下去只做兩件事：跟後端要一份**草稿文字**，然後把畫面帶到文書室。
 * **不送出任何東西。** scrape/threads.py 是唯讀的，整個前後端沒有任何一條
 * 路徑可以發文——那份模組說明寫得很清楚：官方帳號在任何人讀過內容之前回一句
 * 「已收到您的通報」，是一個公開的受理表態。
 *
 * 換室走 Lobby.go()，不直接 showPane()：只換 pane 的話，室頭與左側樓層索引
 * 還停在「03 輿情室」，而內容已經是文書室的了。Lobby 不在（例如某個只有
 * 分頁列的舊版面）才退回 showPane。
 */
function goToLetters() {
  if (window.Lobby && window.Lobby.go && window.Lobby.go("letters")) return;
  if (window.SW && window.SW.showPane) window.SW.showPane("memos");
}

async function draftReply(instId, rootId, btn) {
  if (!instId || !rootId) return;
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "擬稿中…";
  try {
    const d = await S.post(`/api/social/${instId}/draft-reply`,
                           { root_threads_id: rootId });
    if (window.SWMemos) window.SWMemos.showDraft(d);
  } catch (e) {
    /* 失敗也要帶過去並說清楚。留在原地什麼都不說的話，按鈕看起來像沒反應。 */
    if (window.SWMemos) window.SWMemos.showDraftError(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
  goToLetters();
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

/* ── 排序 ──────────────────────────────────────────────────
 *
 * 整份清單已經載進來了（limit=50），所以排序在前端做，不必再打一次後端。
 * **它只改順序，不改任何計數或標籤。**
 *
 * 「關注程度」分段渲染而不是單純排序，為的是那個未分類的段落：
 * tone_bucket 為 unclassified 的園是**沒有人看過**，不是「看過了沒問題」。
 * 混進「無負面」裡（或沉到最底下跟它們排在一起），畫面上就再也分不出
 * 「我們查過、沒事」與「我們根本還沒看」——而那是這個專案最不能接受的混淆。
 * 所以它自成一段，段頭把話講出來。 */
/* 「關注程度」的分組。**依嚴重度分，不依來源分。**
 *
 * 第一版按 Threads 語氣的 rank 分組，最後一組是「無 Threads 通報（僅新聞／
 * PTT）」——於是一間有 7 則事件報導（起訴、開罰 39 萬、勒令停招）的園，
 * 被排在「無負面」下面。那是這份清單上訊號最強的幾間，卻沉到最底下。
 *
 * 來源的分別沒有消失，它在**卡片上**：每張卡片的抬頭會寫「貼文語氣：多數
 * 負面」或「新聞／PTT：多數事件報導」，兩族都有就印兩行，計數也仍然分兩行。
 * 分組講的是「該先看哪些」，卡片講的是「為什麼」——把來源塞進分組名稱，
 * 等於要求使用者先決定他在乎哪個來源，才看得到該先看誰。 */
const ATTENTION_GROUPS = [
  "建議優先查看", "值得注意", "尚未分類", "未見負面訊號", "無可分類內容",
];
const ATTENTION_WHY = {
  0: "這一組的依據寫在每張卡片的抬頭上：可能是多數貼文語氣負面，"
     + "也可能是多數新聞屬於事件報導（已有官方行動）。兩者不合併計數。",
  2: "這些園的內容還沒有跑過分類——是沒有人看過，不是看過了沒問題。",
  4: "這些園有公開內容，但兩族分類都判不出性質。不是「沒事」，是判不出來。",
};

/* 卡片底色的嚴重度就是分組的依據，兩者同一個來源，不會各算一次而對不上。 */
const ATTENTION_RANK = { "m-neg": 0, "m-warn": 1, "m-none": 2, "m-flat": 3, "": 4 };
function attentionRank(it) {
  const r = ATTENTION_RANK[cardMood(it).cls];
  return r === undefined ? 4 : r;
}

function sortedItems(items) {
  /* slice() 之後再排：Array.prototype.sort 是原地排序，直接排會改動
     social.list.items 的順序，於是「最新活動」再也回不去了。
     JS 的 sort 是穩定的，所以同分的那幾筆維持後端給的時間順序。 */
  const out = items.slice();
  if (social.sort === "name") {
    return out.sort((a, b) =>
      String(a.title || "").localeCompare(String(b.title || ""), "zh-Hant"));
  }
  if (social.sort === "concern") {
    /* **依序比較，不是相加。**
     *
     * 把「7 則事件報導」與「7 則語氣負面」加成 14，就是在一個畫面上看不見的
     * 地方（一個排序鍵）把兩族合併了——而那正是這個面板最不該做的事：
     * 一則已起訴、已開罰 39 萬的報導，與一則「想問這樣合理嗎」，在那個和裡
     * 變成同一個量。合併寫在數字上還看得到，合併寫在排序裡看不到。
     *
     * 所以先比事件報導、平手再比語氣負面、再平手才比 @標註則數。事件報導
     * 擺第一是因為它是**已經作成的官方行動**被報導出來，比一則未查證的
     * 民眾陳述重——這個先後是這裡唯一的加權，而且它是明說的。 */
    const hard = (it) => ((it.news || {}).counts || {})["事件報導"] || 0;
    const neg = (it) => ((it.tone || {}).counts || {}).negative || 0;
    const men = (it) => ((it.counts || {}).threads || {}).mentions || 0;
    return out.sort((a, b) =>
      (hard(b) - hard(a)) || (neg(b) - neg(a)) || (men(b) - men(a)));
  }
  return out;                 // recent：後端已經依 last_activity 新到舊排好
}

function sortBarHtml() {
  const opts = SORTS.map(([v, label]) =>
    `<option value="${S.esc(v)}"${social.sort === v ? " selected" : ""}>${
      S.esc(label)}</option>`).join("");
  return `<label class="soc-sortl">排序
    <select id="socsort" class="soc-sort">${opts}</select></label>`;
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
      ${sortBarHtml()}
    </div>`;

  if (!l.count) {
    h += `<div class="insuff">目前沒有任何已收進來的社群內容。
      這是「此管道無訊號」，不是「全市無異常」。</div>`;
  }
  const items = l.items || [];
  if (social.sort === "attention") {
    for (let rank = 0; rank < ATTENTION_GROUPS.length; rank++) {
      const seg = items.filter((it) => attentionRank(it) === rank);
      if (!seg.length) continue;
      h += `<div class="soc-group">${S.esc(ATTENTION_GROUPS[rank])}
        <span class="soc-group-n">${seg.length} 所</span></div>`;
      if (ATTENTION_WHY[rank]) {
        h += `<div class="soc-group-why">${S.esc(ATTENTION_WHY[rank])}</div>`;
      }
      for (const it of seg) h += rowHtml(it);
    }
  } else {
    for (const it of sortedItems(items)) h += rowHtml(it);
  }
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

  const sel = S.$("socsort");
  if (sel) {
    sel.addEventListener("change", (e) => {
      social.sort = e.target.value;
      writeSort(social.sort);
      render();
    });
    /* 排序控制在 .soc-bar 裡、不在任何 .soc-row 之內，所以不會冒泡到展開／
       收合。仍然擋一次，因為版面之後若把它搬進列裡，選一次就會順手把那一園
       收起來——與下面 data-draft-root 那一段是同一顆地雷。 */
    sel.addEventListener("click", (e) => e.stopPropagation());
  }
  wrap.querySelectorAll(".soc-row").forEach((n) =>
    n.addEventListener("click", () => toggle(n.dataset.id)));
  /* 詳情區在 .soc-row 之外，所以不會冒泡到收合；stopPropagation 仍然留著，
     因為版面之後若把詳情搬進列裡，少了它按一次就會順手把那一園收起來。 */
  wrap.querySelectorAll("[data-draft-root]").forEach((n) =>
    n.addEventListener("click", (e) => {
      e.stopPropagation();
      draftReply(n.dataset.draftInst, n.dataset.draftRoot, n);
    }));
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

/* 讓助理指到某一所的社群串。
 *
 * `toggle()` 是「點一下開、再點一下收」，直接拿來用的話助理連講兩次同一所
 * 會把它收起來——使用者看到的是「它說要看這一所，結果畫面把它關掉了」。
 * 所以這裡只負責「打開」，已經開著就維持開著。
 *
 * `full_id` 是完整的 UUID，而助理手上的 `institution_id` 是 8 碼短碼
 * （`/api/social` 兩個都回）。兩個都接受，找不到就安靜不動——那代表這一所
 * 目前沒有社群訊號，面板上本來就沒有那一列。
 */
async function focus(id) {
  await open();
  if (!id) return;
  const hit = ((social.list || {}).items || []).find(
    (it) => it.full_id === id || it.institution_id === id);
  if (!hit) return;
  if (social.open !== hit.full_id) await toggle(hit.full_id);
  const row = document.querySelector(`.soc-row[data-id="${hit.full_id}"]`);
  if (row) row.scrollIntoView({ block: "nearest" });
}

window.SWSocial = { open, focus };
})();
