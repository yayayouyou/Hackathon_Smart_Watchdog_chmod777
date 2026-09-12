"""啟動動態版稽查派工台。

    PYTHONPATH=src .venv/bin/python scripts/serve.py
    → http://127.0.0.1:8000

與靜態版（dist/index.html）讀同一份 payload 契約，差別在於資料從 /api/ 取得，
因此可以用真正的圖磚地圖、滾輪縮放、標記群集與自然語言查詢——Artifact 的 CSP
擋掉 fetch 與非白名單腳本，那些在靜態版一律做不到。

先跑過 scripts/build_frontend.py 產生 dist/data/payload.json。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8

use_utf8()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    a = ap.parse_args()

    import uvicorn

    if a.reload:
        # --reload 會重啟行程。進行中的掃描任務會被標為中斷，額度維持佔用
        # （當機的執行照樣花了錢）。開發時知道就好，不要在示範時用。
        print("⚠️ --reload：重啟會中斷進行中的掃描任務，"
              "之後用 scripts/reconcile_scan.py 對帳")
    print(f"稽查派工台 → http://{a.host}:{a.port}")
    print("  GET  /api/payload            完整 payload")
    print("  GET  /api/proposal?n=20      派工提案")
    print("  GET  /api/institutions/{id}  單園卷宗")
    print("  POST /api/chat               自然語言查詢")
    print("  GET  /api/health             就緒狀態")
    print("  GET  /api/scan/options       掃描主控台設定與剩餘額度")
    print("  POST /api/scan/estimate      算錢，不花錢")
    print("  POST /api/scan               發動掃描（背景任務）")
    # workers 固定為 1：花費帳本的併發保護是行程內鎖加檔案鎖，
    # 多行程會讓兩個 worker 各自看到「還有額度」而同時發動計費執行。
    uvicorn.run("smart_watchdog.api.server:app", host=a.host, port=a.port,
                reload=a.reload, workers=1)


if __name__ == "__main__":
    main()
