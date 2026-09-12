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


def test_every_pane_has_a_tab() -> None:
    html = _html()
    tabs = set(re.findall(r'data-t="([a-z]+)"', html))
    panes = set(re.findall(r'id="pane-([a-z]+)"', html))
    orphan = sorted(panes - tabs)
    assert not orphan, f"這些 pane 點不到：{orphan}"


def test_element_ids_used_by_scripts_exist_somewhere() -> None:
    """`$("x")` 取不到東西時多半是靜默失敗（null 就不綁了），所以靜態擋。

    id 可能在 index.html 裡，也可能是 JS 執行期自己產生的（例如卷宗展開後的
    各段、助理的思考中指示），兩種都算數。
    """
    html = _html()
    declared = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', html))
    for path in _our_scripts():
        src = path.read_text(encoding="utf-8")
        made = set(re.findall(r'id="([a-zA-Z0-9_-]+)"', src))
        made |= set(re.findall(r'id\s*=\s*"([a-zA-Z0-9_-]+)"', src))
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


def test_example_buttons_are_scoped_to_a_container() -> None:
    """範例鈕的綁定必須限定在自己的容器內，不能用全域委派。

    全域委派（`document.addEventListener` + `closest(".eg")`）會讓任何面板的
    範例鈕觸發所有監聽者——先前查詢與助理並存時就會兩邊各跑一次。
    現在只剩助理，但規則保留：下一個加範例鈕的面板不該再踩一次。
    """
    for name in ("app.js", "agent.js"):
        src = (WEBAPP / name).read_text(encoding="utf-8")
        if ".eg" not in src:
            continue
        assert 'closest(".eg")' not in src, (
            f"{name} 用了全域委派，會誤觸其他面板的範例鈕"
        )
        assert re.search(r'querySelectorAll\("#[a-zA-Z0-9_-]+\s+\.eg"\)', src), (
            f"{name} 的 .eg 綁定沒有限定容器"
        )


def test_scripts_are_loaded_in_dependency_order() -> None:
    """agent.js 用 window.SW（app.js 匯出）與 window.AgentAuth（auth.js），
    所以載入順序不能反——反了就是 undefined，而且只在執行到那一行才炸。
    """
    html = _html()
    order = re.findall(r'<script src="/static/([a-z]+)\.js"></script>', html)
    for later, earlier in (("agent", "app"), ("agent", "auth"), ("memos", "app")):
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
