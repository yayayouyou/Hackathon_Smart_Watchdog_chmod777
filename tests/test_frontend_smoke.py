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


def _html() -> str:
    return (WEBAPP / "index.html").read_text(encoding="utf-8")


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


def test_social_panel_sits_above_the_scan_console() -> None:
    """輿情室的順序即是使用順序：先看已經收到的，再決定要不要花錢掃。

    `socialwrap` 讀庫是免費的；`scanwrap` 底下每一次執行都會計費。把花錢的
    那一塊放在上面，等於請人先付錢再看手上有什麼。
    """
    html = _html()
    assert "socialwrap" in html and "scanwrap" in html
    assert html.index('id="socialwrap"') < html.index('id="scanwrap"')


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
