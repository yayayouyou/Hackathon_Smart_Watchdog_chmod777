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

  function applyMapView(v) {
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
      step('<span class="tag no">證據</span>這份索引查不到，屬資料不足');
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

  function step(html) {
    log().insertAdjacentHTML("beforeend", `<div class="astep">${html}</div>`);
    log().scrollTop = log().scrollHeight;
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

  function say(text, softened) {
    const wrap = document.createElement("div");
    wrap.className = "msg bot";
    const p = document.createElement("p");
    wrap.appendChild(p);
    if (softened && softened.length) {
      const flag = document.createElement("span");
      flag.className = "soft";
      flag.textContent = `（已替換認定性用語：${softened.join("、")}）`;
      wrap.appendChild(flag);
    }
    log().appendChild(wrap);
    type(p, text);
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
        say(d.text, d.softened);
        break;
      case "tool_call":
        thinking(true, `執行 ${d.name}`);
        step(`<span class="tag">呼叫</span>${esc(d.name)}
              <code>${esc(JSON.stringify(d.arguments))}</code>`);
        break;
      case "tool_result":
        thinking(true, "思考中");
        step(`<span class="tag ${d.ok ? "ok" : "no"}">${d.ok ? "結果" : "擋下"}</span>${
          esc(d.summary)}`);
        break;
      case "ui_action":
        step(`<span class="tag ui">畫面</span>${esc(d.type)}`);
        dispatch(d);
        break;
      case "error":
        thinking(false);
        bubble(`出錯：${esc(d.message)}`, "bot err");
        break;
      case "done":
        thinking(false);
        if (d.stop_reason === "max_steps") {
          step('<span class="tag no">停止</span>已達單輪步數上限');
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
  document.querySelectorAll("#pane-agent .eg").forEach((b) =>
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
