"""前端的靜態健檢。**不需要瀏覽器**，但擋得住實際發生過的三類問題。

為什麼要有這一支：這個 repo 的測試全是 Python，而前端出過三次「後端測試全綠、
畫面是壞的」：

1. `agent.js` 的 SSE 分幀找 `"\\n\\n"`，但 sse-starlette 送的是 CRLF
   → 助理送出後完全沒反應，而 curl 加 grep 照樣看得到事件。
2. FastMCP 只 mount 沒接 lifespan → `tools/list` 列得出來，協定層 initialize 回 500。
3. 與 main 合併時，對方的 `chatform` 綁定被我們這邊的刪除**靜默吃掉**
   （一邊刪一邊沒改＝git 直接刪，不產生衝突標記）→ 查詢頁籤的送出鈕是死的。

所以這裡測四件事，每一件都對應上面某一次事故：
- JS 語法（第 3 類的變形：文字手術改壞檔案）
- 每個頁籤都有對應的 pane（切過去不會是空白）
- JS 引用的 DOM id 不是靜態就是自己產生的（抓得到 rename 後的漏改）
- 每個 pane 的送出表單都有人綁（抓得到第 3 類）

`node --check` 需要 node；沒有就跳過那一項，不讓這支測試變成必須裝 node。
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"

pytestmark = pytest.mark.skipif(not WEBAPP.exists(), reason="沒有 webapp/")


def _our_scripts() -> list[pathlib.Path]:
    """只看我們自己寫的，不看 vendor/（那是第三方壓縮檔）。"""
    return sorted(p for p in WEBAPP.glob("*.js"))


def _script_order() -> list[str]:
    """index.html 裡的載入順序，也就是瀏覽器建立全域範圍的順序。"""
    return re.findall(r'<script src="/static/([a-z]+)\.js"></script>', _html())


def _pollutes_global(src: str) -> bool:
    """這支腳本有沒有把頂層宣告丟進共用的全域範圍（IIFE 包起來的就沒有）。"""
    return not re.match(r"\s*(?:/\*.*?\*/\s*)*\(function\s*\(\)\s*\{", src, re.S)


def _html() -> str:
    return (WEBAPP / "index.html").read_text(encoding="utf-8")


def _without_comments(text: str) -> str:
    """去掉 /* ... */ 與整行的 // 註解。

    「這個檔案不得寫 X」問的是程式有沒有寫 X，不是註解有沒有提到 X。整份原始碼
    直接 grep 的話，一支把理由寫清楚的檔案會因為在註解裡寫了「不可以
    `tone || "neutral"`」而被自己的測試判紅——這兩條測試的第一版正是這樣。
    只吃整行的 //：行內的 `https://` 一併吃掉的話，連結會被切一半。
    """
    out = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    kept = [ln for ln in out.splitlines() if not ln.strip().startswith("//")]
    return "\n".join(kept)
def _code(js: str) -> str:
    """拿掉註解。

    這一檔有好幾支測試是在比對「某一行有沒有出現在另一行之前」，而這個 repo
    的註解裡常常引用程式碼原文（那是刻意的，註解要講清楚在講哪一行）。
    不先拿掉註解的話，會比對到註解裡那一份——實際發生過：
    `turnHead()` 的註解裡寫著 `mood("think")`，順序測試於是永遠是紅的。
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def test_every_script_parses() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node，跳過語法檢查")
    for path in _our_scripts():
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        assert r.returncode == 0, f"{path.name} 語法錯誤：\n{r.stderr[:400]}"


def test_index_tags_are_balanced() -> None:
    """手動編輯 HTML 很容易少一個閉合標籤，而瀏覽器會默默容錯到版面歪掉。"""
    html = _html()
    for tag in ("div", "nav", "header", "form", "main", "section"):
        opens = len(re.findall(rf"<{tag}[\s>]", html))
        closes = len(re.findall(rf"</{tag}>", html))
        assert opens == closes, f"<{tag}> 開 {opens} 關 {closes}"


def test_every_tab_has_a_pane() -> None:
    """少一個 pane 的表徵是「點過去整片空白」，而且不會有任何錯誤訊息。"""
    html = _html()
    tabs = re.findall(r'data-t="([a-z]+)"', html)
    panes = set(re.findall(r'id="pane-([a-z]+)"', html))
    assert tabs, "找不到任何頁籤"
    missing = [t for t in tabs if t not in panes]
    assert not missing, f"這些頁籤沒有對應的 pane：{missing}"


def test_every_pane_is_reachable() -> None:
    """每個 pane 都要有辦法點到，否則它就是死的。

    到得了的路有兩條，缺一不可地都算數：
      - 頂部分頁列的 `data-t`（現在隱藏，但 scan.js／timeline.js 仍靠它）
      - 中庭的樓層索引（`lobby.js` 的 `ROOMS[].pane`）——**現在真正的導覽**

    原本只認分頁列，於是資料室那一間（只從樓層索引進得去）被判成孤兒。
    測試要問的是「到得了嗎」，不是「有沒有分頁」。
    """
    html = _html()
    tabs = set(re.findall(r'data-t="([a-z]+)"', html))
    rail = set(re.findall(r'pane:\s*"([a-z]+)"',
                          (WEBAPP / "lobby.js").read_text(encoding="utf-8")))
    panes = set(re.findall(r'id="pane-([a-z]+)"', html))
    orphan = sorted(panes - tabs - rail)
    assert not orphan, f"這些 pane 點不到：{orphan}"


def test_element_ids_used_by_scripts_exist_somewhere() -> None:
    """`$("x")` 取不到東西時多半是靜默失敗（null 就不綁了），所以靜態擋。

    id 可能在 index.html 裡，也可能是 JS 執行期自己產生的（例如卷宗展開後的
    各段、助理的思考中指示），兩種都算數。

    「自己產生」有三種寫法，全部要認得，否則會把正確的程式碼判成紅燈：
      1. 樣板字串裡的 `id="x"`（卷宗、掃描面板）
      2. `el.id = "x"`（助理的思考中指示）
      3. **屬性物件 `{ id: "x" }`**——`createElementNS` 搭配屬性表時的寫法
         （中庭的 SVG 節點）。第三種原本沒被認得，中庭一加就紅了，
         而那些 id 是真的有被建出來的。
    """
    html = _html()
    declared = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    for path in _our_scripts():
        src = path.read_text(encoding="utf-8")
        made = set(re.findall(r'id\s*=\s*"([a-zA-Z0-9_-]+)"', src))
        made |= set(re.findall(r'\bid:\s*"([a-zA-Z0-9_-]+)"', src))
        used = set(re.findall(r'\$\("([a-zA-Z0-9_-]+)"\)', src))
        unknown = sorted(used - declared - made)
        assert not unknown, f"{path.name} 取用了不存在也沒產生的 id：{unknown}"


def test_each_submit_form_is_wired_to_something() -> None:
    """合併時對方的表單綁定被靜默刪掉過一次，症狀是按鈕按下去沒反應。

    每個有 id 的 form 都必須在某支 JS 裡被 `$("<id>")` 取用——至少代表有人接它。
    """
    html = _html()
    forms = re.findall(r'<form[^>]*id="([a-zA-Z0-9_-]+)"', html)
    assert forms, "找不到任何表單"
    wired = set()
    for path in _our_scripts():
        src = path.read_text(encoding="utf-8")
        wired |= set(re.findall(r'\$\("([a-zA-Z0-9_-]+)"\)', src))
    dead = [f for f in forms if f not in wired]
    assert not dead, f"這些表單沒有任何 JS 接它，送出鈕會是死的：{dead}"


def test_example_buttons_are_scoped_to_their_own_container() -> None:
    """查詢與助理各有一組範例鈕。

    用全域委派（`document.addEventListener` + `closest(".eg")`）的話，助理的
    範例鈕會同時觸發查詢的 `ask()`——兩邊各跑一次，畫面會很奇怪。
    所以兩邊都必須把選擇器限定在自己的容器內。

    斷言看的是**有沒有限定容器**，不是容器叫什麼名字。原本寫死檢查 `#pane-`，
    助理從分頁改成獨立欄（`#agentcol`）之後就紅了——但那次改動並沒有違反這條
    規則，紅的是斷言本身把「限定在自己的容器」誤寫成「限定在某個 pane」。
    """
    scoped = re.compile(r'querySelectorAll\(\s*"#[a-zA-Z0-9_-]+\s+\.eg"')
    for name in ("app.js", "agent.js"):
        src = (WEBAPP / name).read_text(encoding="utf-8")
        if ".eg" not in src:
            continue
        assert scoped.search(src), (
            f"{name} 的 .eg 綁定沒有限定容器（應為 querySelectorAll(\"#容器 .eg\")）"
        )
        assert 'closest(".eg")' not in src, (
            f"{name} 用了全域委派，會誤觸另一邊的範例鈕"
        )


def test_scripts_are_loaded_in_dependency_order() -> None:
    """agent.js 用 window.SW（app.js 匯出）與 window.AgentAuth（auth.js），
    所以載入順序不能反——反了就是 undefined，而且只在執行到那一行才炸。
    """
    html = _html()
    order = re.findall(r'<script src="/static/([a-z]+)\.js"></script>', html)
    for later, earlier in (("agent", "app"), ("agent", "auth"), ("memos", "app"),
                           ("social", "app")):
        if later in order and earlier in order:
            assert order.index(earlier) < order.index(later), (
                f"{earlier}.js 必須排在 {later}.js 前面"
            )


def test_example_button_selectors_point_at_containers_that_exist() -> None:
    """改容器 id 時最容易漏掉的就是這一行。

    症狀是「點範例按鈕完全沒反應」，而且沒有任何錯誤——querySelectorAll
    找不到東西時回空集合，forEach 什麼也不做。實際發生過一次：
    面板從 pane-agent 改名成 agentcol，選擇器沒跟著改。
    """
    html = _html()
    ids = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    for path in _our_scripts():
        src = path.read_text(encoding="utf-8")
        for sel in re.findall(r'querySelectorAll\("#([a-zA-Z0-9_-]+)\s', src):
            assert sel in ids, f"{path.name} 的選擇器 #{sel} 指向不存在的元素"


def test_agent_repaints_both_the_map_and_the_list() -> None:
    """助理改篩選後，地圖與清單必須一起重畫。

    只重畫其中一個的表徵是：它說「蘆洲區 20 筆」，地圖確實只剩那 20 筆，
    但清單仍列著全市提案——使用者看到的是「它講的跟畫面上的不一樣」。
    這個 bug 出現過兩次：第一次是 drawList 沒讀 agentIds，
    第二次是 drawList 根本沒被匯出、分派器也從沒呼叫它。
    """
    app = (WEBAPP / "app.js").read_text(encoding="utf-8")
    agent = (WEBAPP / "agent.js").read_text(encoding="utf-8")

    assert re.search(r"window\.SW\s*=\s*\{[^}]*\bdrawList\b", app, re.S), (
        "app.js 沒把 drawList 匯出，助理就無法重畫清單"
    )
    assert "state.agentIds" in app, "drawList/drawMarkers 要依 agentIds 篩選"
    assert "SW.drawList()" in agent, "agent.js 從來沒有重畫清單"
    # 分派器不該再單獨呼叫 drawMarkers——那正是漏掉清單的寫法。
    body = agent[agent.index("function dispatch("):]
    assert "SW.drawMarkers()" not in body, (
        "分派器仍單獨呼叫 drawMarkers；應改用同時重畫兩者的 repaint()"
    )


def test_list_template_only_reads_fields_that_points_carry() -> None:
    """清單樣板讀的每個欄位，**點位表**都必須有。

    清單有兩種來源：預設是提案列（`/api/proposal`），助理點名機構時改成拿 id
    去點位表取列。兩張表的欄位曾經不一樣——`tier` 只有提案列有——於是助理一
    點名，`tagClass(p.tier)` 就在 `undefined.startsWith` 炸掉，
    整句 `innerHTML = rows.map(...)` 從未執行，清單**留著上一次的全市提案**。

    使用者看到的是「助理說蘆洲區 20 筆，清單列的是三峽、中和、板橋」，
    而且畫面上沒有任何錯誤。要靠讀 console 才找得到，所以釘在這裡。

    現在 `server.tier_of()` 會在載入時替每個點位補上 `tier`；這支測試守的是
    「不要再有第二個只存在於提案列的欄位被樣板讀到」。
    """
    # 要看的是**前端實際收到的形狀**，不是磁碟上的檔案：`tier` 是
    # `load_payload()` 在載入時蓋上去的衍生欄位，檔案裡本來就沒有。
    # 直接讀檔會讓這支測試紅在一個不存在的問題上。
    pytest.importorskip("fastapi")
    import sys

    sys.path.insert(0, str(ROOT / "src"))
    from smart_watchdog.api.server import load_payload

    try:
        points = load_payload().get("points", [])
    except FileNotFoundError:
        pytest.skip("還沒 build payload")
    if not points:
        pytest.skip("payload 沒有點位")

    src = (WEBAPP / "app.js").read_text(encoding="utf-8")
    start = src.index("function drawList(")
    body = src[start:src.index("\nfunction ", start + 1)]
    # 樣板裡的 `p.欄位`。`p.d.replace(...)` 只取 `d`，後面的方法名不是欄位。
    used = set(re.findall(r"\bp\.([a-zA-Z_][a-zA-Z0-9_]*)", body))

    # 交集而非聯集：只要有一筆點位缺這個欄位，助理點到它就會炸。
    have = set(points[0])
    for p in points:
        have &= set(p)
    missing = sorted(used - have)
    assert not missing, (
        f"drawList 讀了點位表沒有的欄位 {missing}；"
        "助理點名機構時清單會靜默停在上一次的內容"
    )


def test_step_blocks_are_not_keyed_by_a_document_wide_id() -> None:
    """步驟區塊不可以用 `step-<step_id>` 這種全域 DOM id 去認領。

    後端的 `step_id` **每一輪都從 1 重新編號**（`loop.py` 的
    `for step in range(1, MAX_STEPS + 1)`），只有 `turn` 會累加。用 step_id 當
    全域 id 的話，第二輪的第一步會找到第一輪的第一步並就地覆寫：
    送出第二句話之後，畫面上不會多出任何東西，而上面的內容被改掉；
    同一個 `.say` 還會同時跑兩個打字動畫，把句子截成殘句。

    這個 bug 只能靠「送出第二句話」才會出現，前五分鐘的手動測試全都測不到它。
    """
    src = (WEBAPP / "agent.js").read_text(encoding="utf-8")
    assert not re.search(r"getElementById\(\s*`step-", src), (
        "stepBlock 用全域 DOM id 認領步驟；第二輪起會覆寫第一輪的步驟"
    )
    assert not re.search(r"\bid\s*=\s*`step-", src), (
        "步驟區塊仍帶著 `step-<step_id>` 這個會跨輪相撞的 id"
    )


def test_the_step_table_is_cleared_when_a_turn_starts() -> None:
    """每輪要換一張新表，否則第二輪的 step 1 會拿到第一輪的元素——
    跟用全域 id 是同一個 bug，只是換個容器。"""
    src = (WEBAPP / "agent.js").read_text(encoding="utf-8")
    body = src[src.index("async function send("):]
    body = body[:body.index("\n  function ")] if "\n  function " in body else body
    assert re.search(r"steps\s*=\s*new Map\(\)", body), (
        "send() 沒有把上一輪的步驟表清掉"
    )


def test_every_mood_the_agent_sets_is_one_the_stylesheet_draws() -> None:
    """頭像的表情是 JS 與 CSS 之間的一份契約，而它斷掉時**完全沒有徵兆**。

    `agent.js` 設 `data-mood="工作"`，`style.css` 卻只認得 `work` 的話，屬性
    照樣寫得進去、選擇器只是配不到——狗不會動，也不會有任何錯誤。改名或新增
    狀態時最容易漏掉另一邊。

    `idle` 是基準狀態，沒有自己的選擇器（就是那組預設動畫），所以放行。
    """
    js = (WEBAPP / "agent.js").read_text(encoding="utf-8")
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")

    set_in_js = set(re.findall(r'\bmood\("([a-z]+)"\)', js))
    assert set_in_js, "agent.js 沒有設定任何表情"
    # 凍住舊頭像時也會指定一個表情，那個值同樣必須畫得出來。
    set_in_js |= set(re.findall(r'dataset\.mood\s*=\s*"([a-z]+)"', js))
    drawn = set(re.findall(r'\[data-mood="([a-z]+)"\]', css)) | {"idle"}
    unknown = sorted(set_in_js - drawn)
    assert not unknown, f"agent.js 設了樣式表畫不出來的表情：{unknown}"

    # 預設值也要是畫得出來的，否則一進站頭像就是死的。頭像的標記在 agent.js
    # 裡（每一輪都要長一個，所以是 JS 產生的，不是 index.html 的靜態標記）。
    default = re.search(r'class="agentdog[^"]*"[^>]*data-mood="([a-z]+)"', js, re.S)
    assert default, "agent.js 的頭像樣板沒有預設表情"
    assert default.group(1) in drawn


def test_the_avatar_reacts_to_the_states_that_matter() -> None:
    """使用者最想分辨的是「它在想」與「它在動手」——後者代表畫面等一下會變。

    所以 `tool_call`／`tool_result`／`error` 三個事件一定要換表情。少接一個的
    表徵是狗卡在同一個動作上，看起來像當掉了。
    """
    js = (WEBAPP / "agent.js").read_text(encoding="utf-8")
    body = js[js.index("function handleFrame("):]
    for event, expected in (("tool_call", "work"), ("error", "blocked")):
        seg = body[body.index(f'case "{event}":'):]
        seg = seg[:seg.index("break;")]
        assert f'mood("{expected}")' in seg, f"{event} 事件沒有把表情換成 {expected}"
    # tool_result 成功與失敗要分得出來，不能兩邊都同一個表情。
    seg = body[body.index('case "tool_result":'):]
    seg = seg[:seg.index("break;")]
    assert "mood(" in seg and "?" in seg, "tool_result 沒有區分成功與被擋下"

    # 嘴巴是**另一軸**，不是一種 mood。合併過一次，結果講解句設的值被下一個
    # tool_call 立刻蓋掉，嘴巴一次都沒動過（實測 talk 每次只存在 0 毫秒）。
    seg = body[body.index('case "text":'):]
    seg = seg[:seg.index("break;")]
    assert "yap(" in seg, "講解句沒有讓嘴巴動起來"
    assert 'mood("' not in seg, "講話被寫成一種 mood，會被下一個事件立刻蓋掉"


def test_the_avatar_honours_reduced_motion() -> None:
    """整份樣式表其他會動的東西都有這道開關（中庭的狗、房間轉場），
    頭像是一直在跑的無限動畫，漏掉它對前庭系統敏感的人是實際的傷害。"""
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")

    # ⚠️ 用正則抓 `@media{...}` 會錯：這份樣式表裡有規則以 `}}` 收在同一行，
    # 非貪婪的 `(.+?)\n\}` 於是一路吞到一萬字之外，讓這支測試「通過」得毫無
    # 根據（實測過：把 .ad-all 從開關裡拿掉，測試照樣綠）。所以老實數大括號。
    blocks = []
    for m in re.finditer(r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{", css):
        i, depth = m.end(), 1
        while i < len(css) and depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        blocks.append(css[m.end():i - 1])

    assert blocks, "整份樣式表沒有任何 prefers-reduced-motion 區塊"
    assert any(".ad-all" in b for b in blocks), (
        "頭像的動畫沒有被 prefers-reduced-motion 關掉"
    )


def test_the_turn_gets_its_avatar_before_the_mood_is_set() -> None:
    """每一輪自己長一個頭像，而表情要設在**這一輪**那隻身上。

    順序反過來不會報錯，只會安靜地做錯事：`mood("think")` 設到上一輪那隻，
    `turnHead()` 下一行就把牠凍回 idle，新的那隻停在預設值——於是送出之後到
    第一個事件抵達之間（實測 2.3 秒）頭像顯示「待命」，而它其實在思考。
    """
    js = _code((WEBAPP / "agent.js").read_text(encoding="utf-8"))
    body = js[js.index("async function send("):]
    body = body[:body.index("let res;")]
    assert "turnHead()" in body, "send() 沒有替這一輪長出頭像"
    assert body.index("turnHead()") < body.index('mood("think")'), (
        "先設表情再長頭像：表情會設到上一輪那隻，然後被凍住"
    )


def test_only_the_newest_avatar_is_animated() -> None:
    """五隻狗同時搖尾巴是雜訊，而且會讓人以為上面那幾輪的步驟也還在跑。

    兩邊都要守：JS 要把舊的 `live` 拿掉，CSS 要把動畫限定在 `live` 上。
    只做其中一邊都會留下一堆還在動的舊頭像。
    """
    js = (WEBAPP / "agent.js").read_text(encoding="utf-8")
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")
    assert 'classList.remove("live")' in js, "開新一輪時沒有把舊頭像停掉"
    tight = css.replace(" ", "")
    assert ".agentdog:not(.live)*{animation:none" in tight, (
        "樣式表沒有把非最新的頭像的動畫關掉"
    )


def test_the_last_step_of_a_turn_is_marked_finished() -> None:
    """收尾那句沒有 tool，所以它的圓點從頭到尾沒被設過狀態——永遠是空心的。

    使用者讀到的是「跑到一半停住了」（本人回報：「我也會以為沒有結束」）。
    不能在收到 `text` 時就標，因為那時還不知道後面會不會接一個 tool；
    串流結束才確定「沒有下一步」，所以收尾要發生在讀取迴圈之後。
    """
    js = _code((WEBAPP / "agent.js").read_text(encoding="utf-8"))
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")

    assert 'dataset.st = "done"' in js, "沒有任何地方把收尾那一步標成完成"
    assert '.astep[data-st="done"]' in css, "樣式表畫不出 done 這個狀態"

    # 必須是打勾，不是又一顆圓點。圓點在這一欄裡一律代表「一個動作的狀態」
    # （空心＝沒跑、閃爍＝進行中、實心綠＝tool 成功、實心紅＝被擋下），
    # 而收尾那句沒有 tool——用任何一種圓點都在說一件沒發生的事。
    tight = css.replace(" ", "").replace("\n", "")
    assert '.astep[data-st="done"].dot::after{content:"✓"' in tight, (
        "收尾的記號不是打勾"
    )
    assert '.astep[data-st="done"].dot{border-color:transparent;background:none}' in tight, (
        "打勾底下還留著圓點，會變成兩個記號疊在一起"
    )

    # 收尾必須在串流讀完之後。寫在 handleFrame 裡就等於在「還可能有下一步」
    # 的時候宣告結束。
    body = js[js.index("async function send("):]
    assert "settle()" in body, "send() 沒有收尾"
    assert body.index("getReader()") < body.index("settle()"), (
        "收尾寫在讀取串流之前，那時還不知道有沒有下一步"
    )


def test_the_flow_line_stops_at_the_last_step() -> None:
    """步驟之間那條細線靠 `:last-child` 收尾。

    加了每輪抬頭之後最後一步不再是最後一個子元素（後面接著下一輪的抬頭），
    線於是一路延伸到下一輪去——看起來像還有下一步還沒出現。
    這是加抬頭時弄壞的，補一個明確的收尾標記。
    """
    js = _code((WEBAPP / "agent.js").read_text(encoding="utf-8"))
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")
    assert 'classList.add("tail")' in js, "沒有標出哪一步是這一輪的最後一步"
    assert ".astep.tail::before{display:none}" in css.replace(" ", ""), (
        "樣式表沒有讓流程線在最後一步停住"
    )


def test_settling_only_touches_the_current_turn() -> None:
    """上面幾輪早就收好了。重掃整個對話區不只是浪費——它會把已經停在 `run`
    的舊步驟一起重寫，而那種步驟代表某個 tool 真的沒回報，是要留著查的。

    這一輪有哪些步驟，`steps` 那張表已經知道了。
    """
    js = _code((WEBAPP / "agent.js").read_text(encoding="utf-8"))
    body = js[js.index("function settle()"):]
    body = body[:body.index("\n  }") + 4]
    assert "steps.values()" in body, "settle() 沒有用這一輪的步驟表"
    assert "querySelectorAll" not in body, "settle() 掃了整個對話區，會動到舊的輪次"


def test_the_agent_switches_rooms_not_just_panes() -> None:
    """換室要走房間系統，不能只點那顆隱藏的分頁鈕。

    分頁鈕只換中間那塊 `.pane`；室頭、樓層索引、中庭全都不動。實測助理切到
    回測室之後，室頭還寫著「01 地圖室　全市 1,213 園 · 點一園看判斷原因與
    紀錄」——中間是回測室的內容，抬頭是地圖室的說明。

    更嚴重的是使用者還在中庭的時候：中庭是整片覆蓋的，助理做的一切都在它
    底下，完全看不到。走房間系統才會把人帶進去。
    """
    agent = _code((WEBAPP / "agent.js").read_text(encoding="utf-8"))
    lobby = (WEBAPP / "lobby.js").read_text(encoding="utf-8")
    assert "Lobby.goto" in agent, "助理換室沒有走房間系統"
    assert re.search(r"window\.Lobby\s*=\s*\{[^}]*\bgoto\b", lobby, re.S), (
        "lobby.js 沒有對外開放換室的入口"
    )


def test_work_that_follows_a_room_change_waits_for_the_room() -> None:
    """換室之後的動作一律要等那一室就位。

    從中庭進房是 880ms 的動畫，而且收尾會重算地圖視野。不等就做的話，
    助理的「飛到蘆洲區」會被重算拉回全市——實測：清單正確是蘆洲 20 筆，
    地圖卻停在全市，而它還說「地圖已飛過去」。
    """
    agent = _code((WEBAPP / "agent.js").read_text(encoding="utf-8"))
    body = agent[agent.index("function dispatch("):]
    for case in ("navigate", "set_filters"):
        seg = body[body.index(f'case "{case}":'):]
        seg = seg[:seg.index("\n      case ") if "\n      case " in seg else len(seg)]
        assert "switchTab(a.tab, then)" in seg, f"{case} 沒有把後續動作交給回呼"
        assert "const then = () =>" in seg, f"{case} 沒有把後續動作包成回呼"
        # 直接呼叫等於不等就位。
        assert not re.search(r"^\s{8}if \(a\.focus_town\) flyToDistrict", seg, re.M), (
            f"{case} 仍在回呼外直接移動地圖"
        )


def test_switching_between_rooms_does_not_refit_the_map() -> None:
    """室與室之間切換時不可以重算地圖視野。

    重算是為了「中庭蓋著的期間版面可能變過」而存在的。室與室之間版面沒變，
    而 `fitNTPC()` 會把視野拉回全市——助理先切室再飛過去就會被拉回來。

    完成回呼也必須排在重算之後，否則一樣會被蓋掉。
    """
    lobby = (WEBAPP / "lobby.js").read_text(encoding="utf-8")
    assert "function showRoom(r, refit, done)" in lobby, (
        "showRoom 沒有把重算與完成回呼變成可控的"
    )
    assert "showRoom(r, false, done)" in lobby, "室與室之間切換仍會重算地圖"
    assert "showRoom(r, true, done)" in lobby, "從中庭進房沒有重算地圖"

    # 比對先後要看程式碼，不是註解——這一段的註解裡就寫著 fitNTPC()，
    # 不剝掉的話它永遠排在最前面，這條斷言就恆為真。
    fn = _code(lobby)[_code(lobby).index("function showRoom(r, refit, done)"):]
    fn = fn[:fn.index("\n  function ")]
    assert fn.index("fitNTPC") < fn.index("if (done) done();"), (
        "完成回呼排在重算之前，助理接著做的地圖移動會被拉回全市"
    )


def test_social_panel_comes_before_the_scan_console() -> None:
    """輿情室左右分欄，社群聲音在左（也就是原始碼在前）。

    `socialwrap` 讀庫是免費的；`scanwrap` 底下每一次執行都會計費。讀順序是
    先看手上已經收到什麼，再決定要不要花錢再找——把付費那欄擺前面，等於
    請人先付錢再看手上有什麼。窄螢幕會退回上下排，順序同樣由原始碼決定。
    """
    html = _html()
    assert "socialwrap" in html and "scanwrap" in html
    assert html.index('id="socialwrap"') < html.index('id="scanwrap"')


def test_the_remaining_budget_is_visible_without_scrolling() -> None:
    """右欄頂端常駐剩餘額度。

    掃描台每一次執行都會花錢，而金額只印在捲到底的按鈕上時，決定要不要按的
    人不一定看得到自己還剩多少。欄頭那一格是唯一不會被捲走的位置。
    """
    assert 'id="scanfold-budget"' in _html()
    app = (WEBAPP / "app.js").read_text(encoding="utf-8")
    assert "scanfold-budget" in app, "額度欄位沒有人填值"


def test_social_panel_is_opened_when_the_room_is_entered() -> None:
    """`SWSocial.open()` 沒被呼叫時，輿情室上半會永遠停在「載入中…」。

    這是 agent.js 那次事故的同一個形狀：檔案載入了、函式定義了，但沒有人叫它。
    """
    app = (WEBAPP / "app.js").read_text(encoding="utf-8")
    assert "window.SWSocial" in app, "showPane 沒有喚醒社群聲音面板"
    # 問的是「有沒有匯出 open」，不是「只匯出 open」。原本比對字面
    # `= { open }`，助理需要的 `focus` 一加上去就紅了——而那次改動並沒有
    # 違反這條規則，紅的是斷言把「有這一支」寫成了「只有這一支」。
    assert re.search(r"window\.SWSocial\s*=\s*\{[^}]*\bopen\b",
                     (WEBAPP / "social.js").read_text(encoding="utf-8")), (
        "social.js 沒有匯出 open，showPane 叫不動它"
    )


def test_the_voice_room_no_longer_advertises_threads_as_pending() -> None:
    """大廳那句室況是給評審與使用者看的第一行字，接完就不能再說「待接」。"""
    lobby = (WEBAPP / "lobby.js").read_text(encoding="utf-8")
    assert "Threads 待接" not in lobby


def test_no_signal_is_never_rendered_as_a_pass() -> None:
    """無訊號不是合格。

    style.css 的 --k0 註解已經把同一件事講過：0 件不等於安全，把它畫成綠色
    等於發了 730 張合格證。社群面板沿用 .insuff（虛線框、中性色），所以這裡
    釘住「不要哪天有人順手改成綠色或打勾」。

    只擋**視覺上的合格記號**，不擋字詞：面板裡就有一句「這是『此管道無訊號』，
    不是『全市無異常』」，那是在否定那個說法。用字詞黑名單會把否定句判成違規，
    第一版就是這樣紅的——斷言要問的是畫面長什麼樣，不是出現過哪些字。
    """
    src = (WEBAPP / "social.js").read_text(encoding="utf-8")
    assert "insuff" in src, "無訊號的樣式應沿用 .insuff，不要另外發明合格樣式"
    for banned in ("✓", "✔", "☑", "var(--good)"):
        assert banned not in src, f"社群面板不得用「{banned}」把無訊號畫成合格"


def test_scripts_do_not_collide_in_the_shared_global_scope() -> None:
    """傳統 <script> 共用一個全域範圍，兩支各宣告一次同名 const 就是 SyntaxError。

    實際發生過：social.js 跟著 scan.js 寫 `const S = window.SW;` 放在頂層，
    兩個 `const S` 撞在一起，**後載入的 scan.js 整支解析失敗**——
    `Uncaught SyntaxError: Identifier 'S' has already been declared`。
    症狀是掃描主控台永遠停在「載入中…」，而 scan.js 自己一個字都沒改。

    `node --check` 是逐檔跑的，看不到這件事。把所有腳本照 index.html 的載入
    順序接起來再檢查一次，就是瀏覽器實際會遇到的那個範圍。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node，跳過全域衝突檢查")
    order = _script_order()
    joined = "\n".join(
        (WEBAPP / f"{name}.js").read_text(encoding="utf-8")
        for name in order if (WEBAPP / f"{name}.js").exists()
    )
    merged = WEBAPP.parent / "tmp" / "_merged_scripts_check.js"
    merged.parent.mkdir(parents=True, exist_ok=True)
    merged.write_text(joined, encoding="utf-8")
    try:
        r = subprocess.run([node, "--check", str(merged)],
                           capture_output=True, text=True)
        assert r.returncode == 0, (
            "腳本在共用的全域範圍裡衝突（把重複的頂層宣告包進 IIFE）：\n"
            + r.stderr[:600]
        )
    finally:
        merged.unlink(missing_ok=True)


def test_no_two_scripts_declare_the_same_global_name() -> None:
    """頂層 `function` 重名是**靜默覆蓋**，比 const 衝突更難查。

    實際發生過：scan.js 與 timeline.js 都宣告 `function render()`，而
    timeline.js 載入在後——於是 scan.js 裡呼叫的 `render()` 跑的是 timeline
    的那一個。掃描主控台永遠停在「載入中…」，`open()` 回報成功，Console
    一個字都不印，因為沒有任何錯誤發生：只是叫錯了函式。

    `const` 衝突會丟 SyntaxError，上面那條測試抓得到；function 宣告不會，
    所以要另外比對名字。修法一律是把該腳本包進 IIFE，不是改名字——改名字
    只是把同一顆地雷留給下一個檔案。
    """
    seen: dict[str, list[str]] = {}
    for name in _script_order():
        path = WEBAPP / f"{name}.js"
        if not path.exists():
            continue
        src = path.read_text(encoding="utf-8")
        if not _pollutes_global(src):
            continue
        for pattern in (r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)",
                        r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)"):
            for m in re.finditer(pattern, src, re.M):
                seen.setdefault(m.group(1), []).append(name)

    clashes = {k: v for k, v in seen.items() if len(set(v)) > 1}
    detail = "；".join(
        f"{k} ← {'、'.join(dict.fromkeys(v))}" for k, v in sorted(clashes.items()))
    assert not clashes, (
        "這些名字在共用的全域範圍裡被多支腳本宣告，後載入的會靜默覆蓋前面的："
        f"{detail}。修法：把其中一支包進 IIFE。"
    )


def test_no_duplicate_element_ids() -> None:
    """`getElementById` 只回第一個，第二個從此是死的。

    實際發生過：改版面時舊的 `<div id="socialwrap">` 沒刪、新的又加了一個，
    於是畫面上多出一塊永遠停在「載入中…」的空白，而 JS 完全正常——它寫進了
    第一個。標籤數量是平衡的，語法是合法的，只有肉眼看得出不對。
    """
    ids = re.findall(r'id="([a-zA-Z0-9_-]+)"', _html())
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"index.html 有重複的 id（第二個之後都取不到）：{dupes}"


def test_no_font_size_drops_below_fifteen_pixels() -> None:
    """使用者是中年稽查員。全站最小 15px，這條把它釘住。

    `webapp/*.css` 全掃，不只掃新加的那幾條——字級是整站一起調的，一個檔案
    偷偷放回 13px 的結果是那一塊在會場投影上沒有人讀得到。
    """
    small = re.compile(r"font-size:\s*(\d+(?:\.\d+)?)px")
    for path in sorted(WEBAPP.glob("*.css")):
        sizes = [float(v) for v in small.findall(path.read_text(encoding="utf-8"))]
        tiny = sorted({v for v in sizes if v < 15})
        assert not tiny, f"{path.name} 有小於 15px 的字級：{tiny}"


def test_tone_colours_do_not_borrow_the_other_two_scales() -> None:
    """語氣是第三套語意，不得沿用另外兩套的色階。

    `--c1~c4` 是「建議查核密度」（我們算出來的），`--k0~k4` 是「歷史裁罰件數」
    （主管機關已經開罰的公開事實）。style.css 的 --k0 註解已經說明了為什麼這
    兩族在色相上必須分得開；第三套借用它們，同一張畫面上就會有三套顏色互相
    解釋，而讀的人分不出自己在看哪一套。
    """
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")
    block = _without_comments(css[css.index("/* ── 語氣上色"):])
    for banned in ("--c1", "--c2", "--c3", "--c4",
                   "--k0", "--k1", "--k2", "--k3", "--k4"):
        assert banned not in block, f"語氣色不得沿用 {banned}"


def test_every_tone_colour_has_a_text_label_next_to_it() -> None:
    """不得出現沒有說明的色點。

    這個畫面上的紅色最容易被猜成「這園有問題」，而它實際上只代表「這一則貼文
    的語氣是負面的」。所以有顏色的地方都要帶字：貼文用 toneTags() 配一顆帶
    文字的膠囊，機構列用 toneComposition() 印「N 則語氣負面」。

    第一版是數 `"t-neg"` 出現幾次，要求只能有一處。那是拿出現次數當代理指標：
    組成列各段上色後自然變成兩處，測試就紅了——但那次改動並沒有違反這條規則，
    紅的是斷言本身。改成檢查**兩個產生顏色的函式都會輸出文字**。
    """
    src = _without_comments((WEBAPP / "social.js").read_text(encoding="utf-8"))
    assert "function toneTags" in src and "function toneComposition" in src
    assert "tone_label" in src, "語氣膠囊要印後端給的中文標籤"

    # 兩支函式的本體裡，凡是寫出顏色 class 的那一段，同一段也要有文字輸出。
    for fn in ("toneTags", "toneComposition"):
        body = src[src.index(f"function {fn}"):]
        body = body[:body.index("\nfunction ") if "\nfunction " in body else len(body)]
        if "t-neg" in body or "t-warn" in body:
            assert "labels" in body or "tone_label" in body or "則" in body, (
                f"{fn}() 上了色卻沒有輸出文字標籤"
            )


def test_the_panel_never_defaults_an_unclassified_post_to_neutral() -> None:
    """`tone || "neutral"` 那一行會讓一批沒有人看過的貼文一次變成中性。

    後端已經把「跑過但看不出來」與「從來沒跑過」分開放進 tone_bucket，
    前端照著畫；自己用 p.tone 重推一次就是把那個分別丟掉。
    """
    src = _without_comments((WEBAPP / "social.js").read_text(encoding="utf-8"))
    assert "tone_bucket" in src
    for banned in ('tone || "neutral"', "tone || 'neutral'",
                   'p.tone === "neutral"'):
        assert banned not in src, f"未分類不得被當成中性（{banned}）"
    assert '"unclassified"' in src


def test_the_draft_reply_button_hands_off_to_the_letters_room() -> None:
    """草稿要出現在文書室，而且是走換室那條路——只換 pane 的話，室頭與樓層
    索引還停在「03 輿情室」，內容卻已經是文書室的了。
    """
    src = (WEBAPP / "social.js").read_text(encoding="utf-8")
    assert 'Lobby.go("letters")' in src
    assert 'showPane("memos")' in src, "Lobby 不在時要有退路"
    assert "SWMemos" in src
    # 問的是「有沒有匯出這兩支」，不是「只匯出這兩支」。原本比對字面，
    # 助理需要的 open／focus 一加上去就紅了——而那次改動並沒有違反這條規則。
    memos = (WEBAPP / "memos.js").read_text(encoding="utf-8")
    exported = re.search(r"window\.SWMemos\s*=\s*\{([^}]*)\}", memos)
    assert exported, "memos.js 沒有匯出任何東西"
    for fn in ("showDraft", "showDraftError"):
        assert re.search(rf"\b{fn}\b", exported.group(1)), (
            f"memos.js 沒有匯出 {fn}，輿情室的草稿送不過去"
        )
    assert 'go, back' in (WEBAPP / "lobby.js").read_text(encoding="utf-8")


def test_the_draft_is_never_shown_as_something_already_sent() -> None:
    """草稿不是公文。文書室裡它要說自己沒有被送出。"""
    src = _without_comments((WEBAPP / "memos.js").read_text(encoding="utf-8"))
    assert "草稿" in src
    for banned in ("已送出", "已受理", "已回覆", "送出成功"):
        assert banned not in src, f"草稿不得被畫成「{banned}」"


def test_the_news_labels_never_borrow_the_tone_vocabulary() -> None:
    """新聞那一族的膠囊不得寫「語氣負面」。

    「教育局開罰 39 萬」是一件**已經作成的官方行動**被報導出來，
    「多收教材費想問這樣合理嗎」是一句**未經查證的民眾陳述**。兩者都掛
    「語氣負面」的話，稽查員就分不出該先看哪一則——而那個分別正是他判斷
    輕重的依據。顏色可以共用（這個介面只有 --seal 與 --warn 兩個語意色），
    詞彙不行。
    """
    src = _without_comments((WEBAPP / "social.js").read_text(encoding="utf-8"))
    assert "function reportTags" in src and "function reportComposition" in src
    for fn in ("reportTags", "reportComposition", "reportClass"):
        body = src[src.index(f"function {fn}"):]
        body = body[:body.index("\nfunction ") if "\nfunction " in body else len(body)]
        for banned in ("語氣", "負面", "情緒"):
            assert banned not in body, f"{fn}() 不得沿用語氣詞彙（{banned}）"
    # 上了色就要有字，判準與語氣那兩支同一條。
    for fn in ("reportTags", "reportComposition"):
        body = src[src.index(f"function {fn}"):]
        body = body[:body.index("\nfunction ") if "\nfunction " in body else len(body)]
        if "r-event" in body or "r-dispute" in body:
            assert "report_label" in body or "labels" in body or "則" in body, (
                f"{fn}() 上了色卻沒有輸出文字標籤"
            )


def test_the_news_counts_are_never_merged_into_the_tone_counts() -> None:
    """兩個組成分兩行印，各自帶抬頭，永遠不加在一起。

    後端刻意把它們分成 `tone` 與 `news` 兩個欄位（api/social.py）。前端把它們
    併成一串數字，就是把未查證的抱怨與已經起訴的案件數成同一類。
    """
    src = _without_comments((WEBAPP / "social.js").read_text(encoding="utf-8"))
    assert "reportComposition(it.news)" in src, "機構列要讀後端分開回的 news"
    assert "soc-cap" in src, "兩個組成要各自帶抬頭，否則會讀成同一串"
    # 沒有任何一處把兩份 counts 相加或合併。
    for banned in ("Object.assign(it.tone", "...it.tone", "tone.counts +"):
        assert banned not in src, f"兩份組成不得合併（{banned}）"


def test_the_news_colours_never_borrow_the_density_or_penalty_scales() -> None:
    """--c1~c4（建議查核密度）與 --k0~k4（歷史裁罰件數）已經各有語意。

    第三套借用它們的色階，同一張畫面上就會有三套顏色互相解釋。
    """
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")
    block = css[css.index(".soc-rep{"):css.index(".soc-cap{")]
    assert "--seal" in block and "--warn" in block
    assert not re.search(r"var\(--[ck]\d\)", block), "語氣／報導色不得沿用密度或裁罰色階"
    # 字級不得低於 15px。
    for size in re.findall(r"font-size:(\d+)px", block):
        assert int(size) >= 15, f"字級 {size}px 低於 15px"


def test_the_list_can_be_sorted_but_does_not_default_to_attention() -> None:
    """排序控制要在，而預設**不是**「關注程度」。

    這個面板的資料有一個已知偏誤：抱怨的人會把園名寫完整（要讓機關找得到），
    稱讚的人寫得隨意，所以歸屬成功率本身就與語氣相關。把「負面優先」設成永久
    預設，等於讓一個部分由歸屬規則造成的排序每次開啟都排在最前面——那會讓人
    以為輿情比實際更負面。做成選項讓人主動選，跟做成預設，是兩件事。
    """
    src = _without_comments((WEBAPP / "social.js").read_text(encoding="utf-8"))
    assert 'id="socsort"' in src, "缺排序控制"
    for label in ("關注程度", "最新活動", "機構名稱"):
        assert label in src, f"缺排序選項「{label}」"
    assert 'DEFAULT_SORT = "recent"' in src, "預設必須是最新活動"
    assert 'DEFAULT_SORT = "attention"' not in src
    # 排序偏好記在 localStorage，讀寫都要能吞掉例外（Brave 會直接丟）。
    assert "localStorage" in src and src.count("catch") >= 2


def test_the_unclassified_rows_get_their_own_group_in_the_attention_sort() -> None:
    """「未分類」不得沉到底下跟「無負面」混在一起。

    tone_bucket 為 unclassified 代表**沒有人看過**，不是「看過了沒問題」。
    這與 docs/api/social-panel.md §7.7 是同一條原則：我們沒看過的東西，
    畫面上不可以長得像我們看過而且沒事。

    第一版寫死找「無負面」這個組名，分組改名成「未見負面訊號」之後就紅了——
    但那次改動沒有違反這條規則。斷言改成問「未分類那一段有沒有排在
    『查過而且沒事』那一段之前」，組名怎麼寫由版面決定。
    """
    src = _without_comments((WEBAPP / "social.js").read_text(encoding="utf-8"))
    groups = src[src.index("ATTENTION_GROUPS = ["):]
    groups = groups[:groups.index("]")]
    assert "尚未分類" in groups, "關注程度排序要有未分類自己的一段"
    settled = [g for g in ("未見負面訊號", "無負面") if g in groups]
    assert settled, "要有一段代表「查過而且沒有負面訊號」"
    assert groups.index("尚未分類") < groups.index(settled[0]), (
        "未分類要排在「查過而且沒事」那一段之前，不可以混在一起或沉到最後"
    )
    assert "ATTENTION_WHY" in src, "未分類那一段要有一句話說明它不是「沒事」"


def test_sorting_only_reorders_and_never_recomputes_a_count() -> None:
    """排序不得改變任何計數或標籤，也不得原地改動後端給的清單。

    Array.prototype.sort 是原地排序：直接排 social.list.items 會把後端給的
    時間順序改掉，於是「最新活動」再也回不去了。
    """
    src = _without_comments((WEBAPP / "social.js").read_text(encoding="utf-8"))
    body = src[src.index("function sortedItems"):]
    body = body[:body.index("\nfunction ")]
    assert "slice()" in body, "要先複製再排，不可以原地排後端給的清單"
    for banned in ("counts =", "tone =", "labels ="):
        assert banned not in body, f"排序不得改動計數或標籤（{banned}）"
def test_no_two_scripts_declare_the_same_global() -> None:
    """傳統腳本共用同一個全域詞法作用域，同名的頂層 const 會讓後載入的那支
    **整支 SyntaxError 而不執行**。

    這個坑踩過兩次：第一次是 `usd`（app.js:1087 留了註解），第二次是 `S`
    —— social.js 與 scan.js 都宣告了它，結果 window.SWScan 是 undefined、
    掃描主控台整塊是死的，而畫面上完全看不出來，只有主控台一行錯誤。

    修法是把整支包進 IIFE（見 scan.js 檔頭）。這裡守的是「不要再有第三次」。
    """
    import collections

    # ⚠️ `function` 也要算。同名的 `const` 至少會讓後載入的那支整支
    # SyntaxError，吵得看得見；同名的**函式宣告是靜默互相覆蓋**的——
    # social.js 與 timeline.js 都宣告了 `render()`，於是社群面板呼叫的
    # `render()` 其實是 timeline 的那一支：資料抓到了（count=8），畫面卻永遠
    # 停在「載入中…」，沒有任何錯誤。原本這條只認 const/let/var，漏掉了它。
    top = re.compile(
        r"^(?:const|let|var|(?:async\s+)?function)\s+([A-Za-z_$][\w$]*)", re.M)
    owners: dict[str, list[str]] = collections.defaultdict(list)
    for js in sorted(WEBAPP.glob("*.js")):
        src = js.read_text(encoding="utf-8")
        # 包在 IIFE 裡的檔案，頂層宣告已經是私有的，不參與全域命名空間。
        if re.search(r"^\(function\s*\(\)\s*\{", src, re.M):
            continue
        for name in set(top.findall(src)):
            owners[name].append(js.name)

    clashes = {n: f for n, f in owners.items() if len(f) > 1}
    assert not clashes, (
        "這些頂層名稱在多支未包 IIFE 的腳本裡重複宣告，後載入的那支不會執行："
        f"{clashes}"
    )


def test_dataroom_class_names_are_namespaced_or_deliberately_reused() -> None:
    """資料室自己造的 class 一律帶 dr／mx／ftab 前綴，其餘必須是刻意復用的。

    `<tr class="grp">` 撞上了 style.css 早就有的全域 `.grp{display:flex}`：
    套到 <tr> 上會讓每個儲存格變成 flex item、各自撐成整列寬再往下堆（實測
    區段標題列被排成 978x11 三層）。畫面上看起來只是「多了一條細白帶」，
    很難聯想到是 class 撞名。

    這是同一類問題的第三次（前兩次是 JS 全域的 `usd` 與 `S`），所以釘起來。
    """
    js = (WEBAPP / "dataroom.js").read_text(encoding="utf-8")
    emitted = set()
    for m in re.finditer(r'class="([^"]*)"', js):
        for tok in m.group(1).split():
            if re.fullmatch(r"[a-z][a-z0-9-]*", tok):
                emitted.add(tok)
    assert emitted, "抓不到任何 class，正則可能過時了"

    own = re.compile(r"^(dr|mx|ftab)")
    # 刻意復用既有元件的 class。改這份清單前先確認那個 class 在 style.css 裡
    # 的定義套到你要用的標籤上不會出事。
    shared = {
        "sec", "kv", "insuff", "badge", "row", "r", "m", "list", "item",
        "ord", "nm", "rk", "why", "ev", "evh", "tag", "p", "w", "g",
        "soc-load", "thinking", "spinner", "costbtn", "pop", "preset",
        "rh-src", "rail-eyebrow",
        # 只出現在 .ftab 內，由父選擇器限定範圍
        "n", "blank", "neg", "tick", "odd",
    }
    stray = sorted(c for c in emitted if not own.match(c) and c not in shared)
    assert not stray, (
        f"這些 class 既沒有 dr／mx／ftab 前綴，也不在刻意復用的清單裡：{stray}。"
        "全域 CSS 可能已經有同名規則——加前綴，或確認復用是安全的。"
    )


# ── 即時圖層 ──────────────────────────────────────────────────────────

def test_realtime_layer_is_wired_end_to_end() -> None:
    """按鈕、圖例容器、著色分支三者要同時在。

    這一層是靠 `#pinby` 的按鈕切進來的；少任何一塊的症狀都是「按了沒反應」，
    不會有錯誤訊息。
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    app = (WEBAPP / "app.js").read_text(encoding="utf-8")
    assert 'data-p="realtime"' in html
    assert 'id="rt-grp"' in html and 'id="rt-legend"' in html
    assert 'state.pinBy === "realtime"' in app


def test_realtime_quiet_dots_are_smaller_than_the_reported_ones() -> None:
    """0 級一定要比 1 級小。

    第一版是 [10, 20, 27, 34]：0 級只小一半，而 0 級有 1,210 個、每個帶白邊，
    疊起來是一片白，真正有報導的三顆淹在裡面。這條釘的是「安靜的要退下去」，
    不是釘某個特定數字。
    """
    app = (WEBAPP / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const RT_SIZE = \[([^\]]+)\]", app)
    assert m, "RT_SIZE 不見了"
    sizes = [int(x.strip()) for x in m.group(1).split(",")]
    assert len(sizes) == 4, sizes
    assert sizes == sorted(sizes), f"級數越高要越大：{sizes}"
    assert sizes[1] >= sizes[0] * 2.5, (
        f"0 級 {sizes[0]}px 對 1 級 {sizes[1]}px 差距不夠，"
        "全市視角下有報導的點會被沒報導的淹掉"
    )
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")
    assert ".pin.rt-t0{" in css, "0 級沒有自己的樣式，白邊會照畫"


def test_the_legend_thresholds_match_the_code_that_computes_them() -> None:
    """圖例上印的門檻要等於 payload 真的在用的門檻。

    圖例寫「≥2.0 正在發酵」而程式切在 1.5，比沒有圖例更糟——看的人會拿一套
    不存在的規則去解釋畫面。第一版圖例寫的是「近 30 日多則」，那是在描述一個
    程式沒在做的規則（分數是則數 × 新鮮度，一年內六則也會到 0.6）。
    """
    py = (ROOT / "src/smart_watchdog/api/payload.py").read_text(encoding="utf-8")
    m = re.search(r"tier = 3 if score >= ([\d.]+) else 2 if score >= ([\d.]+)", py)
    assert m, "_heat 的分級寫法變了，圖例要跟著改"
    t3, t2 = m.group(1), m.group(2)

    app = (WEBAPP / "app.js").read_text(encoding="utf-8")
    tiers = re.search(r"const tiers = \[(.+?)\];", app, re.S)
    assert tiers, "圖例的 tiers 不見了"
    assert f'"≥{t3}"' in tiers.group(1), f"圖例沒印 ≥{t3}"
    assert f'"≥{t2}"' in tiers.group(1), f"圖例沒印 ≥{t2}"

    # 新鮮度權重同理：說明文字裡的四個數字要來自 _RECENCY。
    rec = re.search(r"_RECENCY = \(\((.+?)\)\)", py)
    assert rec, "_RECENCY 不見了"
    weights = re.findall(r"\d+,\s*([\d.]+)", "((" + rec.group(1) + "))")
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    note = html[html.index('id="rt-grp"'):html.index('id="rt-grp"') + 1400]
    for w in weights:
        assert w in note, f"說明沒印新鮮度權重 {w}"


def test_the_realtime_layer_never_claims_to_be_a_risk_score() -> None:
    """用詞界線。這一層畫的是公開報導，不是我們算出來的分數。

    `aws-architecture.md` §6.5 禁止的正是把個別機構的風險分數畫到圖上，
    所以這一層的說明必須自己講清楚它不是那個東西。
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    blk = html[html.index('id="rt-grp"'):html.index('id="rt-grp"') + 1400]
    assert "不寫入風險分數" in blk
    assert "不等於" in blk, "缺「沒有報導不等於沒有問題」這句"
def test_every_stylesheet_has_balanced_braces() -> None:
    """CSS 壞掉不會有錯誤訊息——瀏覽器從那一行起把剩下的整份丟棄。

    實際發生過：合併時衝突切在 `.roomhead{...}` 規則中間，一側已經收尾、另一側
    是同一條規則的後半段宣告，接起來就多出一個 `}`。症狀是那一行之後的每一條
    規則都失效——助理欄收合變成一團擠在 34px 裡的文字、頂部按鈕掉樣式——而
    Console 一個字都不印，因為這不是錯誤，是「瀏覽器照規範放棄剖析」。

    JS 有 `node --check` 擋這件事，CSS 沒有。數括號是最便宜的等價物：
    多一個或少一個都代表某處被切開過。
    """
    for path in sorted(WEBAPP.glob("*.css")):
        raw = path.read_text(encoding="utf-8")
        # 用等量換行取代註解，報錯的行號才對得上原始檔
        text = re.sub(r"/\*.*?\*/",
                      lambda m: "\n" * m.group(0).count("\n"), raw, flags=re.S)
        depth, line, stray = 0, 1, []
        for ch in text:
            if ch == "\n":
                line += 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth < 0:
                    stray.append(line)
                    depth = 0
        assert not stray, f"{path.name} 第 {stray} 行有多餘的 }}（規則被切開過）"
        assert depth == 0, f"{path.name} 少了 {depth} 個 }}"


def test_no_stylesheet_rule_is_left_unclosed() -> None:
    """規則沒收尾就接下一條——**括號總數仍然平衡**，所以上一條測試抓不到。

    實際發生過，而且是今天最貴的一個 bug。合併時衝突切在 `.tlbar{...}` 中間，
    接起來變成：

        .tlbar{position:static;width:min(72vw,720px);
          max-width:calc(100vw - var(--rightcols, 460px));
        .tlpillsub{color:var(--ink-4);font-size:15px}
        .tlbar{...完整的那份...}

    第一條 `.tlbar` 沒有 `}`。CSS 剖析器在宣告區塊裡遇到 `{` 會開始吞，一路吞到
    能恢復為止——**那一行之後的一大段規則全部消失**，而括號數量是平衡的、
    沒有任何錯誤訊息、`node --check` 也管不到 CSS。

    症狀分散得看不出關聯：時間軸回測的底色不見（背景是被吞掉的宣告之一）、
    助理欄收合後整塊內文擠在細欄裡、按鈕沒有邊框。我一度判定是瀏覽器快取，
    又一度懷疑是合併把規則刪掉了，來回六七輪。真正的原因是這四行。

    只回報**第一個**：之後的全是連鎖誤報（剖析器已經在錯誤的巢狀層裡）。
    """
    for path in sorted(WEBAPP.glob("*.css")):
        raw = path.read_text(encoding="utf-8")
        text = re.sub(r"/\*.*?\*/",
                      lambda m: "\n" * m.group(0).count("\n"), raw, flags=re.S)
        stack: list[str] = []
        buf, line = "", 1
        for ch in text:
            if ch == "\n":
                line += 1
            if ch == "{":
                head = " ".join(buf.split())
                buf = ""
                assert not (stack and stack[-1] == "decl"), (
                    f"{path.name} 第 {line} 行：上一條規則沒有收尾就接了 "
                    f"「{head[:60]}」——括號總數仍然平衡，但那一行之後的規則"
                    f"會被剖析器吞掉"
                )
                stack.append("at" if head.startswith("@") else "decl")
            elif ch == "}":
                if stack:
                    stack.pop()
                buf = ""
            else:
                buf += ch

def test_every_allowed_ui_action_is_handled_by_the_dispatcher() -> None:
    """後端放行的每一種 ui_action，前端都必須真的接。

    漏接**不會報錯**：tool 照樣成功、助理照樣說「已列在畫面上」，而畫面停在
    上一室什麼都沒發生。實測過——`open_table` 在白名單裡、資料室那五個 tool
    也在送，但整份 webapp 都沒有這個字，助理於是在講一件沒發生的事。

    這條界線兩邊分屬不同語言、不同檔案、不同人寫，沒有任何編譯期檢查會抓到
    它，所以釘在這裡。
    """
    registry = (ROOT / "src/smart_watchdog/agent/registry.py").read_text(encoding="utf-8")
    m = re.search(r"UI_ACTION_TYPES\s*=\s*frozenset\((.*?)\)", registry, re.S)
    assert m, "找不到 UI_ACTION_TYPES"
    allowed = set(re.findall(r'"([a-z_]+)"', m.group(1)))
    assert allowed, "白名單是空的，正則可能過時了"

    agent = _code((WEBAPP / "agent.js").read_text(encoding="utf-8"))
    body = agent[agent.index("function dispatch("):]
    handled = set(re.findall(r'case "([a-z_]+)":', body))
    missing = sorted(allowed - handled)
    assert not missing, (
        f"這些 ui_action 後端會送、前端沒接，畫面會靜默不動：{missing}"
    )


def test_the_dataroom_shows_the_true_number_of_tables() -> None:
    """畫面上的張數要是**總數**，不是這次回傳幾張。

    `/api/dataroom/tables` 的 `count` 是套用 limit 之後的筆數。前端拿它當總數
    顯示的話，符合 25 張、請求 12 張時畫面會寫「12 張」——而助理用同一份資料
    講 25 張。兩邊都在講真話，但使用者看到的是系統自相矛盾。
    """
    js = (WEBAPP / "dataroom.js").read_text(encoding="utf-8")
    assert "res.total" in js, "資料室仍把 count 當成總數顯示"
    api = (ROOT / "src/smart_watchdog/api/dataroom.py").read_text(encoding="utf-8")
    assert '"total"' in api, "/api/dataroom/tables 沒有回傳總數"


def test_every_room_module_is_woken_when_its_pane_is_shown() -> None:
    """每一間室的模組都要由 `showPane()` 叫醒。

    這些模組刻意延後載入（答詢擬稿室有 144 筆建議書，不必在開站時就抓），
    所以「進到這一室」必須有人通知它們。漏掉的表徵是**那一室永遠是空的**，
    而且沒有任何錯誤——助理說「已列在畫面上」，畫面上什麼都沒有。

    這個坑踩過三次。前兩次（資料室、輿情室）在 showPane 裡補上了；第三次是
    答詢擬稿室，它原本只綁在那顆**隱藏的分頁鈕**的 click 上，而導覽改走房間
    系統之後那顆鈕根本不會被點到。

    所以規則寫成通則：凡是對外開了 `open()` 的室模組，`showPane()` 都要叫它。
    """
    app = (WEBAPP / "app.js").read_text(encoding="utf-8")
    pane = app[app.index("function showPane("):]
    pane = pane[:pane.index("\n}")]

    providers = []
    for js in sorted(WEBAPP.glob("*.js")):
        if js.name == "app.js":
            continue
        src = js.read_text(encoding="utf-8")
        providers.extend(
            (m.group(1), js.name)
            for m in re.finditer(r"window\.(SW[A-Za-z]*)\s*=\s*\{([^}]*)\}", src)
            if re.search(r"\bopen\b", m.group(2)))
    assert providers, "找不到任何提供 open() 的室模組，正則可能過時了"

    missing = sorted(f"{g}（{f}）" for g, f in providers if g not in pane)
    assert not missing, (
        f"這些室模組沒有被 showPane() 叫醒，進到那一室會是空的：{missing}"
    )


def test_a_late_response_cannot_overwrite_a_newer_one() -> None:
    """建議書清單的取數要擋掉過期的回應。

    進這一室會先載入全部（`showPane` → `open()`），助理接著又篩「三重」
    （`focus()`），兩個請求並行。全部那一份筆數多、回得慢，於是**後到**、
    把 12 筆蓋回 130 筆——畫面列的是助理沒有在講的那一批，而且沒有任何錯誤。

    `focus()` 也必須把搜尋框一起填：只改清單不改輸入框的話，畫面顯示空的
    搜尋條件而列出來的只有三重，使用者一按搜尋就把助理設的洗掉了。
    """
    src = (WEBAPP / "memos.js").read_text(encoding="utf-8")
    body = _code(src)
    assert re.search(r"\bseq\b", body), "memos.js 沒有擋過期回應的機制"
    assert body.count("mine !== seq") >= 2, (
        "成功與失敗兩條路都要檢查，否則慢的那個錯誤訊息照樣會蓋掉新結果"
    )
    assert 'box.value = q' in body, "focus() 沒有把搜尋框一起填"


def test_the_memo_query_reaches_the_screen() -> None:
    """助理篩了哪幾份，畫面就要列哪幾份。

    `list_memos` 只送「切到答詢擬稿室」而不送查詢字串的話，助理講「提到三重的
    那 12 份」，畫面卻列出全部 130 份——它講的跟畫面上的不是同一批。
    """
    tools = (ROOT / "src/smart_watchdog/agent/tools.py").read_text(encoding="utf-8")
    assert '"memo_query"' in tools, "list_memos 沒有把查詢字串送給畫面"
    agent = _code((WEBAPP / "agent.js").read_text(encoding="utf-8"))
    assert "SWMemos.focus" in agent, "分派器沒有把查詢字串套到畫面上"


def _css_files() -> list[pathlib.Path]:
    return sorted(p for p in WEBAPP.glob("*.css"))


def test_no_css_rule_swallows_the_rest_of_the_stylesheet() -> None:
    """一條沒關好的規則會把後面幾百行整個吃掉，而瀏覽器**不會報任何錯**。

    實際發生過兩次，都是合併時新舊兩版交錯：新版開了 `{` 卻少了後半段與 `}`，
    於是它一路吞到某段孤兒的 `}` 才收——中間 248 行規則全部失效，包括登入層的
    `.authwrap{position:fixed;z-index:9000}`。表徵是登入畫面被中庭整片蓋住、
    **完全無法登入**，而且帶著既有 cookie 測不會遇到，所以它躺了好幾個 commit
    沒被發現。

    括號總數是平衡的，所以單純數括號抓不到。這裡看的是「一條規則跨幾行」：
    `@media` 與 `:root` 本來就長，其餘超過 40 行幾乎一定是吞掉了別人。
    """
    for path in _css_files():
        raw = path.read_text(encoding="utf-8")
        clean = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                       raw, flags=re.S)
        lines = raw.splitlines()
        depth, opened = 0, []
        for i, line in enumerate(clean.splitlines(), 1):
            for ch in line:
                if ch == "{":
                    depth += 1
                    opened.append(i)
                elif ch == "}":
                    depth -= 1
                    assert depth >= 0, f"{path.name} 第 {i} 行有多餘的 }}"
                    start = opened.pop()
                    head = lines[start - 1].lstrip()
                    if head.startswith(("@media", "@supports", ":root")):
                        continue
                    assert i - start <= 40, (
                        f"{path.name} 第 {start} 行的規則跨了 {i - start} 行才關："
                        f"{head[:60]} —— 多半是少了 }}，後面全被吞掉"
                    )
        assert depth == 0, f"{path.name} 檔尾還有 {depth} 個未閉合的 {{"


def test_no_orphan_declaration_lines_in_css() -> None:
    """孤兒宣告行：`  padding:…;gap:12px}` 這種沒有選擇器的尾巴。

    合併時舊版的開頭被刪掉、尾巴留下來就會變成這樣。它自己不會報錯，但那個
    `}` 會關掉不屬於它的區塊，於是錯位一路傳下去。兩次事故都伴隨著它。
    """
    for path in _css_files():
        raw = path.read_text(encoding="utf-8")
        clean = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"),
                       raw, flags=re.S)
        depth = 0
        for i, line in enumerate(clean.splitlines(), 1):
            stripped = line.strip()
            # 深度 0 時，一行若含 `:` 又以 `}` 結尾卻沒有 `{`，就是孤兒尾巴
            if (depth == 0 and stripped.endswith("}") and "{" not in stripped
                    and ":" in stripped):
                raise AssertionError(
                    f"{path.name} 第 {i} 行是沒有選擇器的孤兒宣告：{stripped[:70]}")
            depth += line.count("{") - line.count("}")


def test_every_identity_popup_has_a_way_out() -> None:
    """每一張身分卡都要有登出鈕。

    中庭一張、室內一張，而原本只有室內那張有——在中庭打開身分卡只看得到帳號、
    身分、單位，沒有出口，**登不出去**。

    綁定必須用屬性而不是 id：`id` 只能有一個，所以第二張卡的按鈕不可能共用。
    這是同一類問題的又一次（前面是 `.eg` 範例鈕、`SWMemos` 的匯出），
    規則寫成通則：身分卡有幾張都行，登出鈕一律靠 `[data-logout]` 綁。
    """
    html = _html()
    auth = _code((WEBAPP / "auth.js").read_text(encoding="utf-8"))

    popups = re.findall(r'<div class="pop who"[^>]*id="([a-zA-Z0-9_-]+)"(.*?)</div>\s*</div>',
                        html, re.S)
    # 上面的貪婪切法不可靠，改成逐一定位每張卡的區塊
    ids = re.findall(r'<div class="pop who"[^>]*id="([a-zA-Z0-9_-]+)"', html)
    assert len(ids) >= 2, f"預期至少兩張身分卡（中庭與室內），只找到 {ids}"
    del popups

    for pid in ids:
        start = html.index(f'id="{pid}"')
        # 下一張卡（或檔尾）之前的內容就是這一張
        nxt = html.find('<div class="pop who"', start + 1)
        block = html[start:nxt if nxt > 0 else len(html)]
        assert "data-logout" in block, f"身分卡 #{pid} 沒有登出鈕，從那裡登不出去"

    assert '[data-logout]' in auth, "auth.js 沒有用屬性綁登出，多一張卡就會漏掉"
    assert '$("logout")' not in auth, (
        "auth.js 仍用 id 綁登出；id 只能有一個，第二張身分卡的按鈕會是死的"
    )


def test_logging_out_clears_the_identity_popup() -> None:
    """登出後不可以還留著上一位使用者的 Email 與單位。

    `paintWho()` 原本只把身分鈕藏起來，剛才打開的那張卡會留在畫面上——登入視窗
    已經蓋回來了，右上角卻還印著誰剛剛登入過。
    """
    lobby = _code((WEBAPP / "lobby.js").read_text(encoding="utf-8"))
    fn = lobby[lobby.index("function paintWho()"):]
    fn = fn[:fn.index("\n  function ")]
    assert '.pop.who' in fn and "hidden = true" in fn, (
        "paintWho 在沒有使用者時沒有把身分浮層收掉"
    )
    assert 'innerHTML = ""' in fn, "paintWho 沒有清掉浮層裡的帳號與單位"


def test_the_dataroom_halves_share_the_same_inset() -> None:
    """文件控管室的原始資料層是左右並排的兩半，內距必須一樣。

    右半（`.drside`）本來就有 `padding:0 16px 18px`，左半（`.drcol`）沒有——
    於是「上傳財務報告」「園所」那一整欄直接貼在房間左緣，而它上方的工具列
    是內縮 16px 的。兩者垂直相鄰，差 16px 一眼就看得出來沒對齊（使用者回報
    「原始資料顯示會太貼邊」）。

    這裡比的是兩半彼此，不是寫死 16px：日後整室改內距時兩邊要一起改，
    而這條測試守的正是「不要只改一邊」。
    """
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")

    def horizontal_padding(selector: str) -> str:
        m = re.search(rf"(?:^|\}}|\*/)\s*{re.escape(selector)}\s*\{{([^}}]*)\}}",
                      css, re.S)
        assert m, f"找不到 {selector} 的規則"
        pad = re.search(r"padding:\s*([^;}]+)", m.group(1))
        assert pad, f"{selector} 沒有 padding，內容會貼在容器邊緣"
        parts = pad.group(1).split()
        # padding 的 1~4 值寫法裡，水平那一項分別在第 1/2/2/2 個位置
        return parts[0] if len(parts) == 1 else parts[1]

    left = horizontal_padding(".drcol")
    right = horizontal_padding(".drside")
    assert left == right, (
        f"原始資料層左右兩半的水平內距不一致：.drcol={left}、.drside={right}"
    )


# ── 04 分析驗證室：行政區排行 + 心智圖 ────────────────────────────────

def _js_without_comments(name: str) -> str:
    """去掉註解之後的程式碼。（不能叫 `_code`——這個檔已經有一個同名 helper，
    傳統 def 是靜默覆蓋，症狀是別的測試拿到完全錯的引數。）

    ⚠️ 這一節的測試釘的是「不可以再出現某個寫法」，而這個 repo 的註解習慣正是
    **把踩過的坑原樣寫下來**——`signalmap.js` 的註解裡就寫著
    `Math.max(780, host.clientWidth - 6)` 是元凶。直接掃原始碼會掃到那段說明，
    於是測試在抱怨一段正在警告不要這樣做的文字。
    """
    src = (WEBAPP / name).read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def test_the_analysis_room_has_a_board_tab_and_a_signal_tab() -> None:
    """兩個分頁，排行榜在前。

    ⚠️ 原本第二頁是「時間軸回測」，已移除——`#tlbar` 住在 `.mapwrap` 裡，
    而這一室是 no-map（`.roombody.no-map .mapwrap{display:none}`），所以那一頁
    底下根本沒有地圖，只剩一句「拖動地圖下方的時間軸」的佔位字。回測功能本身
    一點都沒少，它完整活在 01 全市監看室的抽屜裡。
    """
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert 'data-v="board"' in html and 'data-v="map"' in html
    assert 'data-v="time"' not in html, "時間軸分頁鈕還在"
    assert 'id="tllist"' not in html, "#tllist 還在 DOM 裡"
    assert 'id="db-pane"' in html and 'id="db-list"' in html
    # 排行榜要排在訊號圖前面——這一室先回答的是「這期先去哪」。
    assert html.index('data-v="board"') < html.index('data-v="map"')


def test_nothing_still_reaches_for_the_removed_timeline_list() -> None:
    """`#tllist` 從 DOM 拿掉之後，不可以再有人 getElementById 它。

    `timeline.js::exit()` 原本會寫 `T.$("tllist").innerHTML = ""`，而 exit 是
    「收起抽屜＝離開回測模式」的唯一出口。元素消失後那行會 TypeError，
    下一行的 `T.drawMarkers()` 就不會執行——地圖停在回測著色、看不出哪裡壞了。
    """
    for name in ("timeline.js", "signalmap.js", "app.js", "districts.js"):
        assert "tllist" not in _js_without_comments(name), f"{name} 還在引用 #tllist"


def test_the_signal_map_has_no_hard_width_floor() -> None:
    """SVG 不可以有寫死的最小寬度下限以外的硬地板。

    `Math.max(780, clientWidth - 6)` 是「心智圖根本看不清楚」的元凶：畫布在
    1600 的視窗下只有 590px，SVG 硬撐 780 就永遠溢出 190px，右邊的 `1.57×`
    被裁成「1.5」、`無法量測` 剩「無法量」。
    現在只保留一個由實測字串寬度推出來的 MIN_W，而且是內容需求不是樣式偏好。
    """
    src = _js_without_comments("signalmap.js")
    assert "Math.max(780" not in src, "780px 的硬地板又回來了"
    assert re.search(r"const MIN_W = \d+", src), "MIN_W 不見了"
    # viewBox 等比縮放會讓 15px 變 6px，等於繞過全站字級底線。
    css = (WEBAPP / "style.css").read_text(encoding="utf-8")
    m = re.search(r"\.smsvg\{([^}]*)\}", css)
    assert m and "width:100%" not in m.group(1), (
        ".smsvg 加了 width:100%，SVG 會依 viewBox 等比縮放，字會跟著變小"
    )


def test_the_signal_map_redraws_when_its_container_changes() -> None:
    """畫完就不再畫的圖，在可調欄寬的版面裡一定會是錯的。

    訊號圖變成第二分頁之後，`open()` 被呼叫時容器還是 hidden，clientWidth 是
    0——不重畫的話它永遠是最窄版。助理欄收合與拖 #railgrip 都不發 window
    resize 事件，只有 ResizeObserver 抓得到。
    """
    src = (WEBAPP / "signalmap.js").read_text(encoding="utf-8")
    assert "ResizeObserver" in src
    assert "sm-pane" in src, "要觀察 #sm-pane 而不是捲動容器 #sm-canvas"
    assert "document.fonts" in src, "字體載入前量到的是備援字體寬度"


def test_the_board_never_hides_the_districts_it_cannot_rank() -> None:
    """資料不足的區要列在表上，不可以折疊或預設隱藏。

    那是全市 41% 的行政區。收進「顯示更多」等於用介面把不確定性藏起來，
    而這個閘門存在的唯一理由就是「我們選擇說不知道」要被看見。
    """
    src = (WEBAPP / "districts.js").read_text(encoding="utf-8")
    assert "資料不足，不排名" in src
    code = _js_without_comments("districts.js")
    assert "<details" not in code and "顯示更多" not in code
    # 案件本身照常可點——不給名次不等於把案件藏起來。
    assert "一件都沒有被藏起來" in src


def test_the_board_does_not_print_the_nan_string() -> None:
    """`why` 有 1069/1213 筆的值是字串 "nan"，直接印會出現在畫面上。

    成因在 payload.py 的 `str(r.review_reason or "")`：pandas 讀回空值是
    float('nan')，而 nan 是 truthy，所以 `nan or ""` 回的是 nan 本身。
    """
    src = (WEBAPP / "districts.js").read_text(encoding="utf-8")
    assert '"nan"' in src, "沒有擋 nan 字串"
