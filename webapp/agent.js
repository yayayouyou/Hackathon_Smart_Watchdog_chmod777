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
  document.querySelectorAll("#agentcol .eg").forEach((b) =>
    b.addEventListener("click", () => send(b.textContent)));

  const clear = $("agentclear");
  if (clear) {
    clear.addEventListener("click", () => {
      SW.state.agentIds = null;
      SW.drawMarkers();
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

  function fold(on) {
    col.classList.toggle("fold", on);
    const btn = $("agentfold");
    btn.textContent = on ? "›" : "‹";
    btn.setAttribute("aria-expanded", String(!on));
    btn.title = on ? "展開助理欄" : "收合助理欄";
    // ⚠️ 不可用 display:none。grip 是 grid 的第 3 格，藏掉之後 aside 會遞補
    // 進那一格（6px），收合狀態就變成一條看不見也點不到的線——實測過。
    // visibility:hidden 保留格位，只是不顯示也不吃事件。
    grip.style.visibility = on ? "hidden" : "";
    document.querySelector("main").style.setProperty(
      "--agentw", on ? "34px" : (restoreWidth() + "px"));
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
    /* 預設收起來。理由是量出來的：視窗 1500 寬時，樓層索引 214 + 派工名單 390
       + 助理 380 佔掉 990，地圖只剩 510 寬卻有 843 高——新北是橫的，塞進直立
       的框裡就會浮出一大片海（實測 fitBounds 後緯度跨距 2.1 度）。
       收起來之後地圖拿到約 856 寬，比例才正常。
       它仍然是常駐的：右緣那條直排寫著「助理」，點一下就展開。 */
    let folded = true;
    try {
      const saved = localStorage.getItem("sw.agentfold");
      if (saved !== null) folded = saved === "1";
    } catch { /* 私密視窗：用預設 */ }
    if (folded) fold(true);
    else document.querySelector("main").style
      .setProperty("--agentw", restoreWidth() + "px");
  }

  window.Agent = { send, dispatch };
})();
