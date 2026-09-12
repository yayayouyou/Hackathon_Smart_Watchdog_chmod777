"""Windows 主控台編碼：別讓警告訊息本身把程式弄死。

Windows 中文語系的 ANSI codepage 是 cp950，而本專案的輸出含 `⚠️`、`⬜`、
`✅`、`✗` 等 cp950 編不出來的字元。附加在互動式主控台時 Python 走
`WriteConsoleW`（UTF-16），沒有問題；但**只要 stdout 被導向檔案或管線**
（`python scripts/serve.py > server.log`、在 Git Bash 底下執行、被 CI 捕捉），
Python 就改用 locale 的 cp950，於是印出警告的那一行拋 `UnicodeEncodeError`。

失敗模式很難看：平常測不出來，**只在警告成立的那一次才當機**——也就是最需要
看到那行字的時候。所以這裡把它一次拆掉，而不是靠記得設 `PYTHONIOENCODING`。

POSIX 一律 UTF-8，`use_utf8()` 在那裡是 no-op。
"""

from __future__ import annotations

import sys


def use_utf8() -> None:
    """把 stdout／stderr 轉成 UTF-8（Windows 專用）。

    就地 reconfigure 現有的 TextIOWrapper，所以在此之後才取得 `sys.stdout`
    參考的程式碼（例如 uvicorn 的 log handler）也會跟著是 UTF-8。

    `errors="replace"` 是最後一道保險：萬一某個終端機仍編不出某個字，
    顯示成 `?` 也不該讓行程死掉。
    """
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
