/* 助理面板：消費 SSE、顯示講解句、把 ui_action 分派到既有的畫面函式。
 *
 * 為什麼不用 EventSource：它只能發 GET，而送出一則訊息要 POST（問句在 body，
 * 且要帶 cookie）。所以自己讀 fetch 的 ReadableStream 並解析 SSE 分幀。
 *
 * 分派表不是安全機制。tool 白名單在後端的 ToolRegistry，這裡只是把六種已知
 * 動作對應到 window.SW 已經有的函式——前端擋不住任何事，也不該假裝擋得住。
 *
 * 事件順序由後端的 step_id 綁定：同一步的 text、tool_call、tool_result、
 * ui_action 共用一個 step_id，所以「先打字、再動畫面」的節奏在這裡才成立。
 */
(function () {
  const SW = window.SW;
  const $ = SW.$;
  const esc = SW.esc;

  let sessionId = null;
  let busy = false;
  let attached = [];   // 「+」附加的檔案：{filename, id?, pages?, pending?, error?}

  /* ── 畫面動作分派 ────────────────────────────────────── */

  /* 換到某一室。`done` 在那一室真的就位之後才呼叫。
   *
   * 為什麼要有回呼：在中庭時進房是一段 880ms 的動畫，而助理常常是「切到地圖
   * 室、然後飛到蘆洲區」連著做。不等就位就飛，飛行會被進房收尾的 fitNTPC()
   * 拉回全市——畫面上看起來是「它說飛過去了，但沒有」。
   *
   * `Lobby.goto()` 接不下時（動畫進行中）才退回去點那顆隱藏的分頁鈕。那條路
   * 只換 pane、不動室頭與樓層索引，是備援不是正解。 */
  function switchTab(name, done) {
    const run = done || (() => {});
    if (window.Lobby && window.Lobby.goto && window.Lobby.goto(name, run)) return;
    const btn = document.querySelector(`.tabs button[data-t="${name}"]`);
    if (btn) btn.click();
    run();
  }

  function applyTypeFilter(typeName) {
    const CODE = { 公立: 0, 非營利: 1, 私立: 2 };
    if (!(typeName in CODE)) return;
    SW.state.types = new Set([CODE[typeName]]);
    // 勾選框要跟著動，否則畫面顯示的條件與實際套用的不一致。
    document.querySelectorAll(".ftype").forEach((el) => {
      el.checked = +el.value === CODE[typeName];
    });
  }

  /* 地圖設定：**設好值再觸發控制項自己的事件**，不直接改 SW.state。
   *
   * 這樣 agent 的動作與人用滑鼠點，走的是完全同一條程式路徑——各寫一份的話，
   * 兩者遲早會不一致（例如 f-choro 的 handler 還做了圖例重畫，自己改 state
   * 就會漏掉）。副作用是勾選框會跟著動，那正是我們要的：畫面上顯示的條件
   * 必須等於實際套用的條件。 */
  const TOGGLES = {
    cluster: "f-cluster", districts: "f-districts", district_names: "f-dnames",
    mask: "f-mask", choropleth: "f-choro", flagged_only: "f-flagged",
  };

  /* 助理改了篩選之後，地圖與清單都要重畫。只畫其中一個，兩邊就會不一致——
     使用者看到的是「它說蘆洲區 20 筆，清單卻列著全市提案」。 */
  function repaint() {
    SW.drawMarkers();
    SW.drawList();
  }

  function fire(el, type) {
    el.dispatchEvent(new Event(type, { bubbles: true }));
  }

  /* 把地圖飛到某個行政區。
   *
   * 「調閱蘆洲區」應該看起來像有人把地圖放大到蘆洲，而不是只有標記變少。
   * 邊界資料是 [lng, lat]，Leaflet 要 [lat, lng]——這裡跟 app.js 的
   * toggleDistricts 用同一個換法，弄反會飛到地球另一邊。
   */
  function flyToDistrict(name) {
    if (!name || !SW.state.payload) return;
    const b = (SW.state.payload.boundary || []).find((f) => f.d === name);
    let ring = b && b.poly ? b.poly.flat() : null;
    if (!ring) {
      const d = (SW.state.payload.districts || []).find((x) => x.d === name);
      ring = d && d.hull ? d.hull : null;
    }
    if (!ring || !ring.length) return;
    const bounds = L.latLngBounds(ring.map(([x, y]) => [y, x]));
    /* maxBounds 是開場取景時以整個新北設的；飛進某一區一定在範圍內，
       但保險起見先放寬，回全市時 app.js 的 fitNTPC 會再設回來。 */
    // 底部留白與 app.js 共用：時間軸面板打開時，南邊不可以被它蓋住。
    const bottom = SW.mapBottomPad ? SW.mapBottomPad(40) : 40;
    SW.state.map.flyToBounds(bounds, {
      paddingTopLeft: [40, 40], paddingBottomRight: [40, bottom], duration: 1.1,
    });
  }

  function applyMapView(v) {
    if (v.focus_town) flyToDistrict(v.focus_town);
    for (const [key, id] of Object.entries(TOGGLES)) {
      if (typeof v[key] !== "boolean") continue;
      const box = $(id);
      if (!box || box.checked === v[key]) continue;   // 已經是這個狀態就不動
      box.checked = v[key];
      fire(box, "change");
    }
    if (v.colour_by) {
      const btn = document.querySelector(`#pinby button[data-p="${v.colour_by}"]`);
      if (btn) btn.click();
    }
    if (typeof v.capacity === "number") {
      /* 走 SW.setCap，不模擬 input 事件。input 的處理是「打字途中不回寫輸入框」
         （echo:false），所以助理送 10 會被夾成 20、數字框卻停在 10——畫面上的
         條件與實際套用的不一致，違反上面那條契約。setCap 會把夾過的值寫回去。 */
      if (SW.setCap) SW.setCap(v.capacity);
      else {
        const cap = $("cap");
        if (cap) { cap.value = v.capacity; fire(cap, "change"); }
      }
    }
  }

  /* 六種 ui_action。型別由後端的 UI_ACTION_TYPES 限定，這裡只負責接。 */
  function dispatch(a) {
    switch (a.type) {
      /* ⚠️ 換室之後的動作一律放進回呼。從中庭進房要 880ms，而且收尾會重算
         地圖視野——不等就位就飛，飛行會被拉回全市。 */
      case "navigate": {
        const then = () => {
          if (a.memo_query !== undefined && window.SWMemos && window.SWMemos.focus) {
            window.SWMemos.focus(a.memo_query);
          }
          if (a.focus_town) flyToDistrict(a.focus_town);
          if (a.institution_id) SW.openDossier(a.institution_id);
          if (Array.isArray(a.ids) && a.ids.length) {
            SW.state.agentIds = new Set(a.ids);
            repaint();
          }
        };
        if (a.tab) switchTab(a.tab, then); else then();
        break;
      }

      case "set_filters": {
        const then = () => {
          const f = a.filters || {};
          if (f.type) applyTypeFilter(f.type);
          if (a.focus_town) flyToDistrict(a.focus_town);
          if (a.map) applyMapView(a.map);
          if (Array.isArray(a.ids)) {
            SW.state.agentIds = a.ids.length ? new Set(a.ids) : null;
          }
          repaint();
        };
        if (a.tab) switchTab(a.tab, then); else then();
        break;
      }

      case "open_drawer":
        if (a.institution_id) SW.openDossier(a.institution_id);
        /* 證據：段落 + 檔名頁碼 + 那一頁的截圖。圖片是 lazy 的，
           因為第一次開要現場渲染 PDF，先把文字與出處顯示出來。 */
        if (a.evidence_query) showEvidence(a);
        break;

      /* 資料室。⚠️ 這個 case 一度不存在：後端白名單放行、資料室那五個 tool
         也在送，但前端整份 webapp 都沒有 `open_table` 這個字。結果是助理
         照樣說「已列在畫面上」，而畫面停在上一室、`#dr-tables` 一個元素都
         沒有——它在講一件沒發生的事。實測過。 */
      case "open_table": {
        const then = () => {
          if (!window.SWData || !window.SWData.focus) return;
          window.SWData.focus({
            institution: a.institution, year: a.year,
            // sections 是一份清單（list_table_types），開第一種就好——
            // 把八種表一次全攤開，等於什麼都沒指出來。
            section: a.section || (Array.isArray(a.sections) ? a.sections[0] : null),
            // prepare_upload：帶到「原始資料」層，把「選擇 PDF」標出來。
            layer: a.layer, upload: !!a.upload,
            // add_to_dataroom：新報告在背景抽取（job）或已直接入庫（refresh）。
            job: a.job, refresh: !!a.refresh, report: a.report,
          });
        };
        switchTab("data", then);
        break;
      }

      /* 輿情蒐集室。社群面板與掃描主控台同在這一室，`showPane("scan")` 會把
         兩塊都叫醒，所以這裡只要再指到某一所就好。 */
      case "open_voice": {
        const then = () => {
          if (window.SWSocial && window.SWSocial.focus) {
            window.SWSocial.focus(a.institution_id);
          }
        };
        switchTab("scan", then);
        break;
      }

      case "close_drawer": {
        const d = $("dossier");
        if (d) d.hidden = true;
        break;
      }

      case "highlight":
        if (Array.isArray(a.ids)) {
          SW.state.agentIds = a.ids.length ? new Set(a.ids) : null;
          repaint();
        }
        break;

      case "download": {
        /* 排程 CSV。BOM 是必要的——Excel 開不帶 BOM 的 UTF-8 中文會是亂碼。 */
        const blob = new Blob(["﻿" + (a.content || "")],
          { type: (a.mime || "text/csv") + ";charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = a.filename || "download.csv";
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
        break;
      }

      default:
        break;
    }
  }

  /* 證據抽屜。文字先出來，截圖 lazy 載入——第一次開要現場把 PDF 那一頁
     渲染成 PNG，不該讓文字等圖。沒有 data/raw 的機器上後端不會給
     image_url，所以這裡只在有值時才放 <img>。 */
  function showEvidence(a) {
    const rows = [];
    (a.facts || []).forEach((f) => {
      const v = f.is_blank ? "（空白＝未編列，不是 0）"
        : `${SW.nf(f.value)} ${esc(f.unit || "")}`;
      rows.push(`<div class="ev">
        <div class="evh">${esc(f.label || f.field || "")} ${v}</div>
        <div class="evc">${esc(f.citation || "")}</div>
        ${f.image_url ? `<img loading="lazy" src="${f.image_url}" alt="財報頁面">` : ""}
      </div>`);
    });
    (a.passages || []).forEach((p) => {
      rows.push(`<div class="ev">
        <div class="evh">${esc(p.label || p.kind || "")}</div>
        <div class="evt">${esc((p.text || "").slice(0, 300))}</div>
        <div class="evc">${esc(p.citation || "")}</div>
        ${p.image_url ? `<img loading="lazy" src="${p.image_url}" alt="財報頁面">` : ""}
      </div>`);
    });
    if (!rows.length) {
      bubble("這份索引查不到，屬資料不足。", "bot");
      return;
    }
    log().insertAdjacentHTML("beforeend",
      `<div class="msg bot evwrap">${rows.join("")}</div>`);
    log().scrollTop = log().scrollHeight;
  }

  /* ── 畫面輸出 ────────────────────────────────────────── */

  const log = () => $("agentlog");

  function bubble(html, cls) {
    log().insertAdjacentHTML("beforeend", `<div class="msg ${cls}">${html}</div>`);
    log().scrollTop = log().scrollHeight;
  }

  /* 每一步一個區塊，用後端的 step_id 綁定。
   *
   * 先前把 tool 的狀態獨立成一條灰底軌道，結果同一件事被說兩遍——軌道寫
   * 「正在調閱區域」，講解句又寫「正在調閱蘆洲區名單」，而灰底方塊比講解句
   * 還搶眼，主從顛倒。
   *
   * 現在**講解句自己就是那一步的標籤**，tool 名稱降成底下的小字。一個點、
   * 一條細線，讀起來是一條流程而不是幾張卡片。
   */
  const TOOL_LABEL = {
    list_institutions: "調閱名單", get_ranking: "排定查核順序",
    open_institution: "開啟卷宗", get_penalties: "調閱裁罰紀錄",
    get_findings: "核對財報法遵", get_staffing: "比對人力配置",
    get_rank_track: "追蹤名次變化", get_realtime: "查看公開提及",
    get_peer_comparison: "與同儕比較", open_memo: "調閱建議書",
    list_memos: "整理建議書清單", set_time_machine: "回到指定時點",
    get_model_card: "調出模型指標", search_documents: "翻查財報原文",
    set_map_view: "調整地圖", export_schedule: "整理稽查排程",
    scan_estimate: "試算掃描費用", record_feedback: "記錄回饋",
    load_skill: "載入作業指引",
  };

  /* 這一輪的步驟區塊，key 是 step_id。
   *
   * ⚠️ 原本是用 DOM id `step-<step_id>` 去找的，而後端的 `step_id` **每一輪都從
   * 1 重新編號**（`loop.py` 的 `for step in range(1, MAX_STEPS + 1)`），只有
   * `turn` 會累加。於是第二輪的第一步會找到第一輪的第一步那個元素，
   * **就地覆寫**它——新的步驟不會出現在對話末端，而是回頭改寫上面的內容。
   *
   * 使用者看到的是「第二句話送出去之後畫面什麼都沒發生」；更糟的是同一個
   * `.say` 上跑著兩個打字動畫，兩邊交錯寫入，句子會被截成殘句
   * （實測到「排程已匯出，檔名是稽查排」）。
   *
   * 改成每輪一張表之後就沒有跨輪的命名空間可以撞。表在 `send()` 開頭清空。 */
  let steps = new Map();

  /* 取得（或建立）某一步的區塊。`st` 決定圓點：
     run 進行中、ok 完成、no 被擋下、done 收尾（沒有 tool 的純敘述）。 */
  function stepBlock(id, st) {
    let el = steps.get(id);
    if (!el) {
      el = document.createElement("div");
      el.className = "astep";
      el.innerHTML = '<i class="dot"></i><div class="say"></div><div class="act"></div>';
      log().appendChild(el);
      steps.set(id, el);
    }
    if (st) el.dataset.st = st;
    log().scrollTop = log().scrollHeight;
    return el;
  }

  function stepSay(id, text, softened) {
    const el = stepBlock(id);
    const box = el.querySelector(".say");
    if (softened && softened.length) {
      const flag = document.createElement("span");
      flag.className = "soft";
      flag.textContent = `（已替換認定性用語：${softened.join("、")}）`;
      el.appendChild(flag);
    }
    type(box, text);
  }

  function stepAct(id, name, st, summary) {
    const el = stepBlock(id, st);
    const label = TOOL_LABEL[name] || name;
    el.querySelector(".act").textContent =
      summary ? `${label}　${summary}` : label;
  }

  /* 助理的頭像就是中庭那隻守護犬。耳朵、眼睛、鼻子、尾巴的路徑是從 lobby.js
   * 的 `dogArt()` 原樣搬來的，所以兩邊是同一隻狗，不是兩個長得像的角色。
   *
   * 為什麼寫成 JS 字串而不是 index.html 裡的一段標記：每一輪對話都要長出一個
   * 自己的頭像（像 Kiro 那樣，頭像跟著訊息走），所以它需要被重複產生。放在
   * HTML 裡就會變成「靜態那份」與「JS 複製的那份」兩份會各自漂移的markup。
   *
   * viewBox 收在狗的實際邊界上（頭 9–35、耳頂 10、下巴 34、尾尖 41），不留
   * 空邊——留空邊的話同樣的 width 會把狗畫小，線寬跟著被壓細，耳朵在小尺寸
   * 下會從花瓣變成兩根天線。 */
  const DOG = `<svg class="agentdog live" viewBox="8.5 9 33 26" width="30" height="24"
       data-mood="idle" aria-hidden="true"><g class="ad-all">
    <path class="ad-head" d="M9 19c0-2.4 1.4-3.8 3.2-2.9L16 18h12l3.8-1.9C33.6 15.2 35 16.6 35 19v9c0 3.3-2.7 6-6 6H15c-3.3 0-6-2.7-6-6z"
      fill="var(--panel)" stroke="var(--edge)" stroke-width="1.6" stroke-linejoin="round"/>
    <path class="ad-ear ad-l" d="M11 17c-1.6-3.2-1.2-6.4.6-6.9 1.7-.5 3.6 1.4 4.4 4.3z"
      fill="var(--paper)" stroke="var(--edge)" stroke-width="1.5"/>
    <path class="ad-ear ad-r" d="M33 17c1.6-3.2 1.2-6.4-.6-6.9-1.7-.5-3.6 1.4-4.4 4.3z"
      fill="var(--paper)" stroke="var(--edge)" stroke-width="1.5"/>
    <g class="ad-eyes" fill="var(--edge)">
      <circle cx="17.5" cy="24" r="1.7"/><circle cx="26.5" cy="24" r="1.7"/></g>
    <path class="ad-mouth" d="M20.4 28.6h3.2" stroke="var(--edge)" stroke-width="1.6"
      stroke-linecap="round" fill="none"/>
    <ellipse class="ad-yap" cx="22" cy="29.4" rx="2.3" ry="1.7" fill="var(--edge)"/>
    <circle class="ad-nose" cx="22" cy="26.6" r="1.5" fill="var(--seal)"/>
    <path class="ad-tail" d="M35 22 q6 -3 5 -9" stroke="var(--edge)" stroke-width="1.6"
      fill="none" stroke-linecap="round"/>
  </g></svg>`;

  /* 「這一段是助理在講」的抬頭：頭像 ＋ 名字，底下接這一輪的步驟。
   *
   * 每一輪自己一個，但**只有最新那一隻會動**（class `live`）。上面幾輪的狗
   * 留在原地不動——五隻狗同時搖尾巴會變成一片雜訊，而且會讓人以為上面那些
   * 步驟也還在跑。 */
  function turnHead() {
    log().querySelectorAll(".agentdog.live").forEach((d) => {
      d.classList.remove("live");
      d.dataset.mood = "idle";
      delete d.dataset.talking;
    });
    log().insertAdjacentHTML("beforeend",
      `<div class="turnhead">${DOG}<b>助理</b></div>`);
  }

  const liveDog = () => log().querySelector(".agentdog.live");

  /* 收合時那條 42px 的直條原本只有一行淡灰的直排「助理」，在同樣淡的底色上
     幾乎看不出那是什麼——使用者回報「收起來的時候不明顯」。
     把狗放進去：牠是這個助理的辨識符號，一顆圖比一行字快得多。
     只在收合時顯示（展開時對話裡每一輪都有牠，再放一隻是重複）。 */
  function mountFoldedAvatar() {
    const head = document.querySelector(".agenthead");
    if (!head || head.querySelector(".agentdog.folded")) return;
    head.insertAdjacentHTML("afterbegin",
      DOG.replace('class="agentdog live"', 'class="agentdog folded"'));
  }

  /* 頭像的狀態分成兩軸，因為它們是**同時**發生的兩件事。
   *
   * `data-mood` 是身體在幹嘛：idle 待命／think 思考／work 動手／blocked 被擋下。
   * `data-talking` 是嘴巴在不在動。
   *
   * ⚠️ 這兩軸一開始是同一個屬性（talk 也是一種 mood），結果 talk 實測**永遠
   * 只存在 0 毫秒**：後端把講解句與 tool 呼叫連著送，`text` 才剛設成 talk，
   * 下一個事件 `tool_call` 就把它蓋成 work，嘴巴一次都沒動過。而句子還在逐字
   * 打——畫面上明明在講話，狗卻閉著嘴。
   *
   * 合併成一軸就是在說「講話與做事互斥」，但它們不互斥：它一邊講「正在調閱
   * 蘆洲區名單」一邊真的在調閱。分兩軸之後，嘴巴跟著打字動畫走，身體跟著
   * 事件走，各自都不會被對方打斷。 */
  /* work 的最短停留。tool 本身跑得極快（實測全部 <6ms，最慢的
     search_documents 5.6ms），一輪 7 秒裡 99% 是模型在想。所以 `work` 若不給
     下限，它每次都只存在 **0 毫秒**——耳朵豎起那個狀態實際上一次都沒被看見，
     等於沒做。600ms 讓它看得見，而且不說謊：tool 確實跑過了。 */
  const MIN_WORK_MS = 600;
  let moodAt = 0;
  let moodTimer = null;

  function mood(name) {
    const el = liveDog();
    if (!el) return;
    clearTimeout(moodTimer);
    const left = MIN_WORK_MS - (Date.now() - moodAt);
    if (el.dataset.mood === "work" && left > 0) {
      moodTimer = setTimeout(() => mood(name), left);
      return;
    }
    el.dataset.mood = name;
    moodAt = Date.now();
  }

  let yapTimer = null;

  /* 嘴巴動 `ms` 毫秒。`type()` 是每 12ms 吐 2 個字，所以一句話大約
     `長度 × 6` 毫秒講得完，尾巴多留一點才不會話還沒說完嘴就閉上。 */
  function yap(ms) {
    const el = liveDog();
    if (!el) return;
    el.dataset.talking = "1";
    clearTimeout(yapTimer);
    yapTimer = setTimeout(() => { delete el.dataset.talking; }, ms);
  }

  /* 收尾：把這一輪還沒有狀態的步驟標成完成，並讓流程線在最後一步停住。
   *
   * 為什麼不能在收到 `text` 時就標：`stepSay()` 不知道後面還會不會接一個
   * tool，而收尾那句是純敘述（沒有 tool），所以它的圓點從頭到尾沒被設過狀態
   * ——永遠是空心的。使用者讀到的是「跑到一半停住了」。串流結束時才確定
   * 「沒有下一步」，所以在這裡標。
   *
   * 只碰**這一輪**的步驟（用 `steps` 這張表），不掃整個對話區：上面幾輪早就
   * 收好了，重掃一遍只會把已經正確的狀態再寫一次。
   *
   * 還停在 `run` 的不動。那代表某個 tool 真的沒有回報，把它改成「完成」是謊；
   * 實務上不會發生（registry 保證每個 tool_call 都補一列 tool_result），
   * 真的發生時讓它留著比較好查。 */
  function settle() {
    const blocks = [...steps.values()];
    blocks.forEach((el) => { if (!el.dataset.st) el.dataset.st = "done"; });
    // 流程線畫在每一步的 ::before，靠 :last-child 收尾。加了每輪抬頭之後最後
    // 一步不再是最後一個子元素（後面接著下一輪的抬頭），線於是一路延伸下去。
    const last = blocks[blocks.length - 1];
    if (last) last.classList.add("tail");
  }

  /* 一輪結束時把嘴也收掉。最後一句通常是整輪最長的，`yap()` 的計時器還在跑，
     不清掉的話串流都結束了狗還在對著空氣說話。 */
  function hush() {
    clearTimeout(yapTimer);
    const el = liveDog();
    if (el) delete el.dataset.talking;
  }

  /* 「思考中」。一則真的 tool 呼叫來回要數秒到數十秒，沒有這個指示，
     畫面與「壞掉了」完全分不出來。每收到一個事件就把它移到最後面，
     所以它永遠停在最新一步的下方，代表「還有下一步」。 */
  function thinking(on, label) {
    let el = $("agentthinking");
    if (!on) {
      if (el) el.remove();
      return;
    }
    if (!el) {
      el = document.createElement("div");
      el.id = "agentthinking";
      el.className = "thinking";
      el.innerHTML = '<span class="spinner"></span><span></span>';
    }
    el.lastElementChild.textContent = label || "思考中";
    log().appendChild(el);              // 重新 append = 移到最後
    log().scrollTop = log().scrollHeight;
  }

  /* 講解句逐字顯示。後端是整句送出的（要先過禁用詞過濾才能送），
   * 所以打字動畫在這裡做，不是串流的副產品。 */
  function type(el, text, done) {
    let i = 0;
    const tick = () => {
      el.textContent = text.slice(0, (i += 2));
      if (i < text.length) setTimeout(tick, 12);
      else if (done) done();
      log().scrollTop = log().scrollHeight;
    };
    tick();
  }

  /* ── 一輪對話 ────────────────────────────────────────── */

  function currentView() {
    const active = document.querySelector('.tabs button[aria-pressed="true"]');
    return {
      tab: active ? active.dataset.t : "list",
      cap: SW.state.cap,
      selected: SW.state.selected,
    };
  }

  async function send(text) {
    const files = attached.filter((a) => a.id);
    if (busy || (!text.trim() && !files.length) || attached.some((a) => a.pending)) return;
    busy = true;
    // 上一輪的步驟區塊留在畫面上，但不要再被這一輪的 step_id 認領——
    // 後端每輪都從 1 重新編號。
    steps = new Map();
    $("agentsend").disabled = true;
    bubble(esc(text) + files.map((f) => ` <span class="chip">PDF ${esc(f.filename)}</span>`).join(""), "me");
    attached = [];
    paintChips();
    /* ⚠️ 順序：先長出這一輪的抬頭，再設表情。
       反過來的話 `mood("think")` 設到的是**上一輪**那隻狗，而 `turnHead()`
       下一行就把牠凍回 idle，新的那隻則停在預設的 idle——於是送出後到第一個
       事件抵達之間（實測 2.3 秒）頭像顯示「待命」，而它其實在思考。 */
    turnHead();
    mood("think");
    thinking(true, "連線中");

    let res;
    try {
      res = await fetch("/api/agent/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, session_id: sessionId, view: currentView(),
          attachments: files.map((f) => f.id) }),
      });
    } catch (e) {
      thinking(false);
      bubble(`連線失敗：${esc(e.message)}`, "bot err");
      busy = false; $("agentsend").disabled = false;
      return;
    }
    if (res.status === 401) {
      thinking(false);
      bubble("尚未登入，請重新登入後再試。", "bot err");
      window.AgentAuth.show();
      busy = false; $("agentsend").disabled = false;
      return;
    }
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* 保留 HTTP 碼 */ }
      thinking(false);
      bubble(`無法處理：${esc(detail)}`, "bot err");
      busy = false; $("agentsend").disabled = false;
      return;
    }

    /* SSE 分幀：以空行分隔，每幀有 event: 與 data: 兩行。
     *
     * ⚠️ sse-starlette 送的是 CRLF（`\r\n\r\n`），而 `\r\n\r\n` 裡**沒有**
     * 相鄰的 `\n\n`——直接找 "\n\n" 會永遠找不到，事件一則都不會被處理，
     * 畫面上看起來就是「送出去之後完全沒反應」。所以每次都先把整個緩衝區
     * 正規化成 LF 再切；整段替換也順便處理掉 `\r` 與 `\n` 被拆在兩個 chunk
     * 的情況。 */
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf = (buf + dec.decode(value, { stream: true })).replace(/\r\n/g, "\n");
      let cut;
      while ((cut = buf.indexOf("\n\n")) >= 0) {
        handleFrame(buf.slice(0, cut));
        buf = buf.slice(cut + 2);
      }
    }
    thinking(false);
    settle();
    mood("idle");
    // 這裡刻意不呼叫 hush()：串流結束時最後一句通常還在逐字打，
    // 立刻閉嘴就會變成「字還在跑、嘴已經閉了」。讓 yap() 自己的計時器收尾。
    busy = false;
    $("agentsend").disabled = false;
  }

  function handleFrame(frame) {
    let name = "message";
    const dataLines = [];
    frame.split("\n").forEach((line) => {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    });
    if (!dataLines.length) return;
    let d;
    try { d = JSON.parse(dataLines.join("\n")); } catch (_) { return; }

    switch (name) {
      case "session":
        sessionId = d.session_id;
        thinking(true, "思考中");
        break;
      case "text":
        stepSay(d.step_id, d.text, d.softened);
        // 打字動畫每 12ms 吐 2 個字，嘴巴就開合到那句講完為止。
        yap(d.text.length * 6 + 400);
        break;
      case "tool_call":
        thinking(false);       // 圓點自己會顯示進行中，不必再有第二個指示
        mood("work");
        stepAct(d.step_id, d.name, "run", "");
        break;
      case "tool_result":
        stepAct(d.step_id, d.name, d.ok ? "ok" : "no", d.summary);
        mood(d.ok ? "think" : "blocked");
        thinking(true, "整理中");
        break;
      case "ui_action":
        dispatch(d);
        break;
      case "error":
        thinking(false);
        hush();
        mood("blocked");
        bubble(`出錯：${esc(d.message)}`, "bot err");
        break;
      case "done":
        thinking(false);
        if (d.stop_reason === "max_steps") {
          bubble("已達單輪步數上限，先停在這裡。", "bot err");
        }
        break;
      default:
        break;
    }
  }

  /* ── 綁定 ────────────────────────────────────────────── */

  /* 開場白也要有抬頭，否則一進站畫面上沒有狗，使用者不會知道有這隻角色，
     也看不出那段話是誰講的。它是 `live` 的，所以在等第一句話的期間就在
     呼吸、偶爾眨眼——第一輪開始時 `turnHead()` 會把它凍住。 */
  mountFoldedAvatar();

  if (log() && !log().querySelector(".turnhead")) {
    log().insertAdjacentHTML("afterbegin",
      `<div class="turnhead">${DOG}<b>助理</b></div>`);
  }

  const form = $("agentform");
  if (form) {
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const input = $("aq");
      const text = input.value;
      input.value = "";
      send(text);
    });
  }
  document.querySelectorAll("#agentcol .eg").forEach((b) =>
    b.addEventListener("click", () => send(b.textContent)));

  /* ── 附加檔案 ──────────────────────────────────────────
   * 「+」是使用者親手按的，瀏覽器才肯打開檔案選擇視窗（助理自己開不了，見
   * dataroom.js 的 cueUpload）。選好就先傳到伺服器暫存，送出時只帶代號——
   * 由助理的 tool 放進文件控管室，結果是伺服器做完回報的。 */
  function paintChips() {
    const box = $("agentchips");
    if (!box) return;
    box.hidden = !attached.length;
    box.innerHTML = attached.map((a, i) => `<span class="chip${a.error ? " err" : ""}">PDF ${esc(a.filename)}`
      + (a.pending ? " · 上傳中…" : a.error ? ` · ${esc(a.error)}` : a.pages ? ` · ${a.pages} 頁` : "")
      + (a.pending ? "" : `<button type="button" class="chipx" data-i="${i}" aria-label="移除">×</button>`)
      + "</span>").join("");
    $("agentsend").disabled = busy || attached.some((a) => a.pending);
  }

  async function attach(file) {
    const slot = { filename: file.name, pending: true };
    attached.push(slot);
    paintChips();
    const fd = new FormData();
    fd.append("file", file);
    try {
      const r = await fetch("/api/agent/attachments", { method: "POST", body: fd });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
      Object.assign(slot, d, { pending: false });
    } catch (e) {
      Object.assign(slot, { pending: false, error: e.message });
    }
    paintChips();
  }

  const attachBtn = $("agentattach"), fileInput = $("agentfile");
  if (attachBtn && fileInput) {
    attachBtn.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      [...fileInput.files].forEach(attach);
      fileInput.value = "";          // 同一個檔案移除後再選一次也要觸發
    });
  }
  const chipBox = $("agentchips");
  if (chipBox) {
    chipBox.addEventListener("click", (e) => {
      const x = e.target.closest(".chipx");
      if (!x) return;
      attached.splice(Number(x.dataset.i), 1);
      paintChips();
    });
  }

  const clear = $("agentclear");
  if (clear) {
    clear.addEventListener("click", () => {
      SW.state.agentIds = null;
      repaint();
    });
  }

  /* ── 助理欄的寬度與收合 ──────────────────────────────────
   *
   * 寬度寫進 `main` 的 `--agentw`，不是寫進 aside 的 inline style：grip 自己
   * 的位置也由同一個 grid 算，改一處兩者一起動，拖到一半不會錯開一格。
   *
   * 上下限不是美感問題。太窄時對話會斷成一字一行、輸入框的「送出」被擠掉；
   * 太寬則地圖只剩一條，而助理講「我把這幾筆標在地圖上了」時，那句話要對得上
   * 看得見的地圖才有意義。所以上限跟著視窗寬度走，不是一個固定 px。 */
  const col = $("agentcol");
  const grip = $("agentgrip");
  const MIN = 280;
  const maxW = () => Math.max(MIN, Math.round(window.innerWidth * 0.45));

  function setWidth(px) {
    const w = Math.round(Math.min(maxW(), Math.max(MIN, px)));
    document.querySelector("main").style.setProperty("--agentw", w + "px");
    try { localStorage.setItem("sw.agentw", String(w)); } catch { /* 私密視窗 */ }
    if (SW.state.map) SW.state.map.invalidateSize();
  }

  /* 收合列要藏哪些東西。`.mini.fold` 是那顆展開鈕本身，留著。 */
  const HIDE_WHEN_FOLDED = ".agentbody, .who, .mini:not(.fold)";

  /* 收合後整條欄位都能點開，不是只有上面那顆 24px 的小圓鈕。
   *
   * 一條 42px 寬、整個視窗高的細欄，滑鼠過去時使用者的預期是「點它會打開」；
   * 把唯一的觸發點藏在最上緣一顆小鈕裡，等於要人先找到那顆鈕。中間那個大
   * 箭頭只是**指示**，真正吃點擊的是整條欄位。
   *
   * 建一次、之後靠 display 切換：每次收合都重建的話，展開動畫進行中會閃一下。
   * 樣式全部 inline——這個瀏覽器對 style.css 的更新沒有反應，理由見 fold()。 */
  let expandStrip = null;

  function ensureExpandStrip() {
    if (expandStrip) return expandStrip;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "agentexpand";
    b.setAttribute("aria-label", "展開助理欄");
    b.title = "展開助理欄";
    b.textContent = "‹";
    Object.assign(b.style, {
      position: "absolute", inset: "0", width: "100%", height: "100%",
      border: "0", background: "transparent", cursor: "pointer",
      display: "flex", alignItems: "center", justifyContent: "center",
      font: "inherit", fontSize: "22px", color: "var(--ink-4)", padding: "0",
    });
    b.addEventListener("mouseenter", () => { b.style.color = "var(--seal)"; });
    b.addEventListener("mouseleave", () => { b.style.color = "var(--ink-4)"; });
    b.addEventListener("click", () => fold(false));
    col.appendChild(b);
    expandStrip = b;
    return b;
  }

  function fold(on) {
    const btn = $("agentfold");
    col.classList.toggle("fold", on);
    /* 收合時整顆收起來：整條欄位已經是可點的展開區（見 ensureExpandStrip），
       再擺一顆 24px 的小鈕等於把同一件事講兩次，而且比整條難點。 */
    if (btn) btn.style.display = on ? "none" : "";
    // 整片點擊區靠 col 定位，所以收合時 col 必須是 positioned。
    col.style.position = on ? "relative" : "";
    const strip = ensureExpandStrip();
    strip.style.display = on ? "flex" : "none";

    /* 箭頭指向**按下去之後會往哪個方向動**：展開狀態按它會把欄位收到右邊，
       所以是 ›；收合狀態下那條展開區按了會往左長出來，所以是 ‹。
       先前兩個都反了，於是「清除標記」旁邊那顆看起來像要把欄位拉得更開。 */
    btn.textContent = on ? "‹" : "›";
    btn.setAttribute("aria-expanded", String(!on));
    btn.title = on ? "展開助理欄" : "收合助理欄";
    // ⚠️ 不可用 display:none。grip 是 grid 的第 3 格，藏掉之後 aside 會遞補
    // 進那一格（6px），收合狀態就變成一條看不見也點不到的線——實測過。
    // visibility:hidden 保留格位，只是不顯示也不吃事件。
    grip.style.visibility = on ? "hidden" : "";
    /* 42px 不是 34px：收合後那條直排「助理」是 15px（全站字級地板，給中年
       稽查員），34px 是 10.5px 時代的寬度，字放大之後會擠到溢出。
       欄寬要跟著字級走，不是反過來。 */
    document.querySelector("main").style.setProperty(
      "--agentw", on ? "42px" : (restoreWidth() + "px"));
    try { localStorage.setItem("sw.agentfold", on ? "1" : "0"); } catch { /* 同上 */ }
    // Leaflet 要被告知容器變了，否則地圖會停在舊尺寸、滑鼠座標整個對不上。
    if (SW.state.map) setTimeout(() => SW.state.map.invalidateSize(), 210);
  }

  function restoreWidth() {
    let w = 380;
    try { w = Number(localStorage.getItem("sw.agentw")) || 380; } catch { /* 同上 */ }
    return Math.min(maxW(), Math.max(MIN, w));
  }

  if (grip) {
    // pointer 事件而不是 mouse：觸控筆與觸控螢幕走同一條路，不必寫兩套。
    grip.addEventListener("pointerdown", (e) => {
      if (col.classList.contains("fold")) return;
      e.preventDefault();
      grip.setPointerCapture(e.pointerId);
      grip.classList.add("drag");
      document.body.classList.add("resizing");
      const move = (ev) => setWidth(window.innerWidth - ev.clientX);
      const up = () => {
        grip.classList.remove("drag");
        document.body.classList.remove("resizing");
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
    // 鍵盤也要能調：grip 有 tabindex，只能滑鼠拖等於鍵盤使用者調不了。
    grip.addEventListener("keydown", (e) => {
      const cur = col.getBoundingClientRect().width;
      if (e.key === "ArrowLeft") { e.preventDefault(); setWidth(cur + 24); }
      if (e.key === "ArrowRight") { e.preventDefault(); setWidth(cur - 24); }
    });
  }

  const foldBtn = $("agentfold");
  if (foldBtn) {
    foldBtn.addEventListener("click", () => fold(!col.classList.contains("fold")));
    /* **預設展開。**
     *
     * 先前預設是收起來的，理由是地圖比例：視窗 1500 寬時樓層索引 214 + 派工
     * 名單 390 + 助理 380 佔掉 990，地圖只剩 510 寬卻有 843 高，新北是橫的，
     * 塞進直立的框裡會浮出一大片海。
     *
     * 但代價太大：收合狀態下輸入框與送出鈕的尺寸是 0×0（實測），使用者打開
     * 網站看到的是一條 34px 的直條，**根本無法與助理對話**，而且沒有任何提示
     * 說要點它。助理是這個系統的主要互動方式，把它藏起來等於把主功能藏起來。
     *
     * 地圖比例的問題改用別的方式處理（縮小助理預設寬度、或使用者自己拖），
     * 不用「藏起來」解。想收起來的人點右上角那顆鈕，選擇會被記住。
     */
    let folded = false;
    try {
      const saved = localStorage.getItem("sw.agentfold");
      if (saved !== null) folded = saved === "1";
    } catch { /* 私密視窗：用預設 */ }
    /* 兩條路都要走 fold()。原本展開時只設欄寬就結束，於是那些「展開該長什麼
       樣」的 inline style（標題列內距、底線、橫排標題）一次都沒被套上——
       一載入就是展開的人看到的是沒有樣式的標題列。fold(false) 會把欄寬一起
       設好，所以那一行併進去。 */
    fold(folded);
    if (!folded) {
      document.querySelector("main").style
        .setProperty("--agentw", restoreWidth() + "px");
    }
  }

  window.Agent = { send, dispatch };
})();
