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

  /* ── 畫面動作分派 ────────────────────────────────────── */

  function switchTab(name) {
    const btn = document.querySelector(`.tabs button[data-t="${name}"]`);
    if (btn) btn.click();
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
    SW.state.map.flyToBounds(bounds, { padding: [40, 40], duration: 1.1 });
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
      const cap = $("cap");
      if (cap) { cap.value = v.capacity; fire(cap, "input"); }
    }
  }

  /* 六種 ui_action。型別由後端的 UI_ACTION_TYPES 限定，這裡只負責接。 */
  function dispatch(a) {
    switch (a.type) {
      case "navigate":
        if (a.tab) switchTab(a.tab);
        if (a.focus_town) flyToDistrict(a.focus_town);
        if (a.institution_id) SW.openDossier(a.institution_id);
        if (Array.isArray(a.ids) && a.ids.length) {
          SW.state.agentIds = new Set(a.ids);
          SW.drawMarkers();
        }
        break;

      case "set_filters": {
        if (a.tab) switchTab(a.tab);
        const f = a.filters || {};
        if (f.type) applyTypeFilter(f.type);
        if (a.focus_town) flyToDistrict(a.focus_town);
        if (a.map) applyMapView(a.map);
        if (Array.isArray(a.ids)) {
          SW.state.agentIds = a.ids.length ? new Set(a.ids) : null;
        }
        SW.drawMarkers();
        break;
      }

      case "open_drawer":
        if (a.institution_id) SW.openDossier(a.institution_id);
        /* 證據：段落 + 檔名頁碼 + 那一頁的截圖。圖片是 lazy 的，
           因為第一次開要現場渲染 PDF，先把文字與出處顯示出來。 */
        if (a.evidence_query) showEvidence(a);
        break;

      case "close_drawer": {
        const d = $("dossier");
        if (d) d.hidden = true;
        break;
      }

      case "highlight":
        if (Array.isArray(a.ids)) {
          SW.state.agentIds = a.ids.length ? new Set(a.ids) : null;
          SW.drawMarkers();
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

  /* 取得（或建立）某一步的區塊。`st` 決定圓點：
     run 進行中、ok 完成、no 被擋下、done 收尾（沒有 tool 的純敘述）。 */
  function stepBlock(id, st) {
    let el = document.getElementById(`step-${id}`);
    if (!el) {
      el = document.createElement("div");
      el.id = `step-${id}`;
      el.className = "astep";
      el.innerHTML = '<i class="dot"></i><div class="say"></div><div class="act"></div>';
      log().appendChild(el);
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
      el.innerHTML = '<span class="dots"><i></i><i></i><i></i></span><span></span>';
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
    if (busy || !text.trim()) return;
    busy = true;
    $("agentsend").disabled = true;
    bubble(esc(text), "me");
    thinking(true, "連線中");

    let res;
    try {
      res = await fetch("/api/agent/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, session_id: sessionId, view: currentView() }),
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
        break;
      case "tool_call":
        thinking(false);       // 圓點自己會顯示進行中，不必再有第二個指示
        stepAct(d.step_id, d.name, "run", "");
        break;
      case "tool_result":
        stepAct(d.step_id, d.name, d.ok ? "ok" : "no", d.summary);
        thinking(true, "整理中");
        break;
      case "ui_action":
        dispatch(d);
        break;
      case "error":
        thinking(false);
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

  const clear = $("agentclear");
  if (clear) {
    clear.addEventListener("click", () => {
      SW.state.agentIds = null;
      SW.drawMarkers();
    });
  }

  window.Agent = { send, dispatch };
})();
