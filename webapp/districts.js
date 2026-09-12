/* 行政區派工優先序。
 *
 * 這一頁回答的是一個派工問題，不是一個地理問題：**這一期的人力該先往哪幾個
 * 行政區走。** 稽查員是以行政區為單位移動的，一趟車去一個區，所以把全市前
 * 100 名攤成一張逐園清單，其實沒有回答「先去哪」。
 *
 * ⚠️ **名次是關於「我們這份派工名單」的陳述，不是關於一個地方的陳述。**
 * 29 個行政區是有居民、有園所、有名譽的真實地點，資料完全不支持「某區的孩子
 * 比較不安全」這種斷言。所以這一頁所有的字都寫「建議查核密度」「建議先看的
 * 順序」，不寫「風險排行」「高風險行政區」「問題最多的區」。
 *
 * 密度怎麼算、為什麼要做型態校正、為什麼 exp<1 不給名次——全部在
 * `api/payload.py::district_board` 的註解裡，那裡是唯一定義。這支只負責畫。
 */
(function () {
const T = window.SW;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const st = { data: null, k: 100, open: null, busy: false };

/* 帶別的顏色。刻意**不用紅綠燈**：綠色會被讀成「這一區合格」，而我們量的是
   派工密度不是合規狀態——永和 67 家裡有 32 家有裁罰紀錄，它只是這一期建議
   查核的件數低於同型態組成的預期。 */
const BAND = {
  高於全市: "band-hi",
  與全市相當: "band-mid",
  低於全市: "band-lo",
  資料不足: "band-na",
};

const TYPE = ["公立", "非營利", "私立"];

/* 誤差棒的畫布刻度。上限固定 4.0 而不是跟著最大值跑：跟著跑的話，換一次 k
   整排長度就變一次，看的人會以為資料變了。超過 4 的用箭頭收邊。 */
const SCALE_MAX = 4;
const pos = (v) => Math.max(0, Math.min(100, (v / SCALE_MAX) * 100));

function bar(r) {
  if (r.sir == null) {
    return '<div class="dbbar-wrap"><span class="dbna">—</span></div>';
  }
  const l = pos(r.lo);
  const w = Math.max(1.5, pos(r.hi) - l);
  return `<div class="dbbar-wrap" title="95% 區間 ${r.lo}–${r.hi}">
    <i class="dbbase" style="left:${pos(1)}%"></i>
    <i class="dbci" style="left:${l}%;width:${w}%"></i>
    <i class="dbdot" style="left:${pos(r.sir)}%"></i>
  </div>`;
}

/* 全區組成的迷你堆疊條。這條在的理由是：它就是型態校正在校正的東西，
   讓人一眼看到「這一區私立多」與「這一區全是公立」長得不一樣。 */
function mix(r) {
  const seg = (n, cls) => (n ? `<i class="${cls}" style="flex:${n}"></i>` : "");
  return `<span class="dbmix" title="公立 ${r.pub}／非營利 ${r.npo}／私立 ${r.prv}">`
    + seg(r.pub, "m0") + seg(r.npo, "m1") + seg(r.prv, "m2") + "</span>";
}

function row(r) {
  const na = r.band === "資料不足";
  return `<div class="dbrow ${BAND[r.band] || ""}" data-d="${esc(r.d)}"
      role="button" tabindex="0" aria-expanded="false">
    <span class="dbrank">${na ? "–" : r.rank}</span>
    <span class="dbname">${esc(r.d)}</span>
    <span class="dbobs">${r.obs}<em>／預期 ${r.exp}</em></span>
    ${bar(r)}
    <span class="dbval">${r.sir == null ? "—" : r.sir.toFixed(2) + "×"}</span>
    <span class="dbband">${esc(r.band)}</span>
    <span class="dbscale">${mix(r)}<em>${r.n} 家</em></span>
    <span class="dbflags">${r.hot ? `<i class="dbhot">報導 ${r.hot}</i>` : ""}</span>
  </div><div class="dbdrill" data-for="${esc(r.d)}" hidden></div>`;
}

/* 點開一區。**不打 API**：整份 payload 開站就抓過了（932 KB／1,213 園），
   `/api/institutions?town=` 的實作就是同一句 filter，再打一次是同一份資料
   走第二趟網路。 */
function drill(d) {
  const r = (st.data.rows || []).find((x) => x.d === d);
  if (!r) return "";
  const pts = (T.state.points || []).filter((p) => p.d === d);
  const inList = pts.filter((p) => p.r <= st.k).sort((a, b) => a.r - b.r);
  const heat = T.state.payload && (T.state.payload.realtime || {}).heat || {};

  // 算式攤開，任何人可以當場心算驗證這個名次怎麼來的。
  const parts = (r.exp_parts || []).filter((x) => x.n)
    .map((x) => `${TYPE[x.t]} ${x.n} 家 × ${(x.base * 100).toFixed(2)}% = ${x.exp}`)
    .join("　＋　");

  const head = r.sir == null
    ? `<div class="dbmath"><b>不給名次</b>　這一區 ${r.n} 家機構，依其
       ${esc(parts || "組成")}，全市平均水準下本期預期進榜 <b>${r.exp}</b> 家
       ——不到 1 家。看到 ${r.obs} 家無法區分是真的沒問題還是我們沒看到，
       所以不給名次。<b>案件本身照常列在下面，一件都沒有被藏起來。</b></div>`
    : `<div class="dbmath"><b>${r.obs} ÷ ${r.exp} = ${r.sir}×</b>
       （95% 區間 ${r.lo}–${r.hi}）<br>預期件數 ${r.exp} ＝ ${esc(parts)}</div>`;

  const list = inList.length
    ? inList.map((p) => {
      const h = heat[p.i];
      // ⚠️ why 有 1069/1213 筆的值是字串 "nan"（payload.py 的
      // `str(r.review_reason or "")` 遇到 pandas 的 float nan，而 nan 是
      // truthy）。直接印會在畫面上出現三個英文字母。
      const why = p.why && p.why !== "nan" ? p.why : "";
      return `<div class="dbitem" data-i="${p.i}">
        <span class="dbr">#${p.r}</span>
        <span class="dbn"><i class="tdot t${p.t}"></i>${esc(p.n)}</span>
        <span class="dbtags">
          <span class="tag ${p.tier.indexOf("財報") === 0 ? "s"
            : p.tier.indexOf("近") === 0 ? "w" : "p"}">${esc(p.tier)}</span>
          ${p.np > 0 ? `<span class="tag p">${p.np} 件裁罰史</span>` : ""}
          ${!p.fin ? '<span class="tag p">無公開財報</span>' : ""}
          ${h && h.tier ? `<span class="tag rt${h.tier === 1 ? " rt-t1" : ""}">近期報導 ${h.n} 則</span>` : ""}
        </span>
        <span class="dbwhy">${esc(why)}</span>
      </div>`;
    }).join("")
    : '<div class="insuff">本區這一期沒有機構進入前 ' + st.k + ' 名。'
      + '這代表在這個容量下沒有排進來，<b>不是這一區沒有問題</b>。</div>';

  const other = r.n - inList.length;
  return head
    + `<div class="dbsub">本區建議查核 ${inList.length} 家（依全市名次）</div>`
    + list
    + `<div class="dbfoot">本區另有 ${other} 家未進入前 ${st.k} 名。`
    + `有裁罰紀錄 ${r.pen} 家、有公開財報 ${r.fin} 家`
    + (r.fin === 0 ? "——<b>本區財務軸整片空白，查不到不等於沒問題</b>。" : "。")
    + "</div>";
}

function render() {
  const box = $("db-list");
  if (!box || !st.data) return;
  const rows = st.data.rows || [];
  const ranked = rows.filter((r) => r.band !== "資料不足");
  const na = rows.filter((r) => r.band === "資料不足");

  // ⚠️ 資料不足那一段**不可折疊、不可預設隱藏**。它是全市 41% 的行政區，
  // 收進「顯示更多」等於用介面把不確定性藏起來。
  box.innerHTML =
    '<div class="dbhead"><span>名次</span><span>行政區</span>'
    + '<span>建議查核</span><span>密度（1.0＝全市平均）</span><span class="dbval">倍數</span><span>對照</span>'
    + '<span>全區組成</span><span>輿情</span></div>'
    + ranked.map(row).join("")
    + (na.length
      ? `<div class="dbsec">資料不足，不排名（${na.length} 區）</div>`
        + `<div class="dbsecnote">這些區的型態組成在全市平均水準下，本期預期`
        + `進榜不到 1 家，給名次只會是雜訊。`
        + `其中 ${na.filter((r) => r.pub === r.n).length} 區是 100% 公立、`
        + `${na.filter((r) => !r.fin).length} 區沒有任何公開財報——`
        + `這不是隨機的空白。個案清單照常可點。</div>`
        + na.map(row).join("")
      : "");

  $("db-n").textContent = st.data.ranked;
  const b = st.data.base || {};
  $("db-base").textContent =
    "全市 " + st.data.population + " 園，本期前 " + st.k + " 名；"
    + "型態別基準率 " + Object.keys(b).sort()
      .map((t) => TYPE[+t] + " " + (b[t] * 100).toFixed(2) + "%").join("／");
  $("db-caveat").textContent = st.data.caveat || "";

  box.querySelectorAll(".dbrow").forEach((el) => {
    const go = () => toggle(el.dataset.d);
    el.addEventListener("click", go);
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); }
    });
  });
  if (st.open) paint(st.open);
}

function paint(d) {
  const cell = document.querySelector(`.dbdrill[data-for="${CSS.escape(d)}"]`);
  const head = document.querySelector(`.dbrow[data-d="${CSS.escape(d)}"]`);
  if (!cell || !head) return;
  cell.innerHTML = drill(d);
  cell.hidden = false;
  head.setAttribute("aria-expanded", "true");
  head.classList.add("on");
  cell.querySelectorAll(".dbitem").forEach((el) =>
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      // 開卷宗要回 01 室，卷宗抽屜掛在地圖那一欄。
      if (window.Lobby && window.Lobby.go) window.Lobby.go("map");
      else T.showPane("list");
      T.openDossier(el.dataset.i);
    }));
}

function toggle(d) {
  const same = st.open === d;
  document.querySelectorAll(".dbdrill").forEach((c) => {
    c.hidden = true; c.innerHTML = "";
  });
  document.querySelectorAll(".dbrow").forEach((r) => {
    r.classList.remove("on"); r.setAttribute("aria-expanded", "false");
  });
  st.open = same ? null : d;
  if (st.open) paint(st.open);
}

async function load() {
  if (st.busy) return;
  st.busy = true;
  try {
    st.data = await T.api("/api/district-board?k=" + st.k);
    render();
  } catch (e) {
    const box = $("db-list");
    if (box) box.innerHTML = '<div class="insuff">排行榜讀不到：' + esc(e.message) + "</div>";
  } finally {
    st.busy = false;
  }
}

/* 滑桿去抖。k 的敏感度是這張榜最誠實的部分（Spearman 相對 k=100：k=50 是
   0.972、k=150 掉到 0.721），所以要讓人拖得動；但每動一格打一次 API 沒有
   必要，rAF 等停下來再算。 */
let timer = null;
function onK(v) {
  st.k = +v;
  $("db-kv").textContent = st.k;
  clearTimeout(timer);
  timer = setTimeout(load, 160);
}

function open() {
  if (!st.data) load();
}

document.addEventListener("DOMContentLoaded", () => {
  const k = $("db-k");
  if (k) k.addEventListener("input", (e) => onK(e.target.value));
});

window.SWDistricts = { open, load };
})();
