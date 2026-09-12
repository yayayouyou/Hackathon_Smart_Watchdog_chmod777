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
    assert "window.SWSocial = { open }" in \
        (WEBAPP / "social.js").read_text(encoding="utf-8")


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

    top = re.compile(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)", re.M)
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
