"""PWA：手機可安裝成 app，但**稽查資料永遠不進手機的快取**。

這支測的不是「PWA 裝不裝得起來」（那要真的手機），而是兩件一旦壞掉就不會有
任何錯誤訊息的事：

1. **Service worker 的管轄範圍。** SW 只管得到它自己所在的路徑。放在
   /static/sw.js 它照樣註冊成功、照樣看起來是 PWA，但攔不到首頁——斷線時白
   畫面，而且沒有 console 錯誤。所以要釘住：它從根路徑送出、註冊時的 scope 是 /。
2. **/api/* 絕不快取。** API 回的是真實機構的建議查核名單與未查證的通報。
   被快取進手機的後果是「裝置遺失時上面躺著一份稽查名單」。這條規則一旦被
   「順便快取一下比較快」的修改繞過，功能上完全看不出來。
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
WEBAPP = ROOT / "webapp"
sys.path.insert(0, str(ROOT / "src"))

pytestmark = pytest.mark.skipif(not WEBAPP.exists(), reason="沒有 webapp/")


def _manifest() -> dict:
    return json.loads((WEBAPP / "manifest.webmanifest").read_text(encoding="utf-8"))


def _sw() -> str:
    return (WEBAPP / "sw.js").read_text(encoding="utf-8")


def test_manifest_is_installable_and_every_icon_exists() -> None:
    m = _manifest()
    assert m["start_url"] == "/" and m["scope"] == "/"
    assert m["display"] == "standalone"
    assert m["short_name"], "主畫面上的名字不能是空的"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes, "Chrome 要 192 與 512 才會跳安裝"
    assert any("maskable" in i.get("purpose", "") for i in m["icons"]), \
        "沒有 maskable 圖示，Android 會在圖示外面加一圈白框"
    for icon in m["icons"]:
        assert icon["src"].startswith("/static/"), icon["src"]
        path = WEBAPP / icon["src"].removeprefix("/static/")
        assert path.exists(), f"manifest 指到不存在的圖示 {icon['src']}"


def test_the_service_worker_never_caches_the_api() -> None:
    src = _sw()
    never = re.search(r"const NEVER\s*=\s*\[([^\]]*)\]", src)
    assert never and '"/api/"' in never.group(1), "NEVER 清單裡沒有 /api/"
    handler = src[src.index('addEventListener("fetch"'):]
    # 放行判斷必須在 respondWith 之前——寫在後面等於先快取、再決定要不要快取。
    assert handler.index("NEVER.some(") < handler.index("respondWith("), \
        "/api/ 的放行判斷跑到 respondWith 之後了"
    assert handler.index('req.method !== "GET"') < handler.index("respondWith("), \
        "非 GET（登入、擬稿、答詢）必須在快取邏輯之前放行"


def test_every_precached_shell_file_exists() -> None:
    shell = re.search(r"const SHELL\s*=\s*\[([^\]]*)\]", _sw())
    assert shell
    for url in re.findall(r'"([^"]+)"', shell.group(1)):
        if url in {"/", "/manifest.webmanifest"}:
            continue
        assert url.startswith("/static/"), url
        assert (WEBAPP / url.removeprefix("/static/")).exists(), f"SHELL 裡的 {url} 不存在"
        assert not url.startswith("/api/"), "資料不得出現在預載清單"


def test_the_page_registers_the_worker_at_the_root_scope() -> None:
    html = (WEBAPP / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest" href="/manifest.webmanifest"' in html
    assert 'register("/sw.js", { scope: "/" })' in html
    assert "/static/sw.js" not in html, "從 /static 註冊的 SW 管不到首頁"
    # iOS Safari 不讀 manifest 的圖示，要各自宣告
    assert 'rel="apple-touch-icon"' in html


def test_the_server_sends_both_files_from_the_root() -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from smart_watchdog.api.server import app

    client = TestClient(app)
    sw = client.get("/sw.js")
    assert sw.status_code == 200
    assert "javascript" in sw.headers["content-type"]
    assert sw.headers.get("service-worker-allowed") == "/"
    assert sw.headers.get("cache-control") == "no-cache", \
        "SW 自己被快取住，改了快取規則也推不到已經裝好的手機上"

    man = client.get("/manifest.webmanifest")
    assert man.status_code == 200
    assert "manifest+json" in man.headers["content-type"]
    assert man.json()["scope"] == "/"


#: 在 node 的 vm 沙箱裡真的執行 sw.js，並確認三個事件處理器都掛上了。
_EVAL_SW = r"""
const vm = require("vm");
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const events = {};
const self = {
  addEventListener: (type) => { events[type] = true; },
  location: { origin: "http://127.0.0.1" },
  skipWaiting() {}, clients: { claim() {} },
};
vm.runInNewContext(src, { self, caches: {}, fetch() {}, URL, Response: {}, Promise, console });
const missing = ["install", "activate", "fetch"].filter((t) => !events[t]);
if (missing.length) { console.error("沒有掛上：" + missing.join(",")); process.exit(2); }
"""


def test_the_service_worker_script_actually_evaluates() -> None:
    """語法檢查過，不代表評估得起來。

    第一版的說明寫在區塊註解裡，其中一串粗體的 /api/ 加星號，裡面的「星號＋
    斜線」提早結束了註解，後面的 `api` 變成一行程式：語法合法（`node --check`
    與前端健檢都照過），一執行就 ReferenceError。瀏覽器只回一句「ServiceWorker
    script evaluation failed」，PWA 安靜地註冊不上——在 iPhone 模擬上才抓到。
    所以這裡真的執行一次。
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("沒有 node")
    run = subprocess.run([node, "-e", _EVAL_SW, str(WEBAPP / "sw.js")],
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert run.returncode == 0, run.stderr[-600:]


def test_losing_the_connection_says_so_instead_of_a_blank_page() -> None:
    """斷線打開 app，要看到一句話，不是一片空白。

    sw.js 刻意不快取 /api，所以離線時殼打得開、資料一定沒有。第一版 auth.js 在
    連不上時什麼都不做（沿用靜態版「名單仍然可看」的假設），結果是標題列下面
    一片空白、沒有任何說明——在 iPhone 模擬上把 server 關掉才看到。
    """
    src = (WEBAPP / "auth.js").read_text(encoding="utf-8")
    check = src[src.index("async function check()"):src.index("async function logout()")]
    catch = check[check.index("catch"):]
    assert "showOffline()" in catch, "連不上時沒有顯示任何提示"
    assert 'addEventListener("online"' in src, "恢復連線後要自己重新載入"
    # 說清楚資料不在手機上：那是刻意的設計，不讓人以為是壞掉
    assert "不會存放在這支手機上" in src
