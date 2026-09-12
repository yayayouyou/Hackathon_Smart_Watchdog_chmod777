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
    assert "window.SWMemos = { showDraft, showDraftError }" in \
        (WEBAPP / "memos.js").read_text(encoding="utf-8")
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
