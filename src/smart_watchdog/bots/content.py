"""bot 送出去的文字。平台無關——Telegram 與之後的 LINE 共用。

兩個原則：

1. **跟派工台同一套。** 清單直接呼叫 `server.get_proposal()`，答詢直接呼叫
   `chat.answer()`。bot 自己另寫一套排序或篩選的那天，聊天室與派工台就會對同一個
   問題給出兩個答案，而那是答詢時最難解釋的一種錯。
2. **純文字。** 不用 Markdown／HTML parse mode：園名裡有括號、底線、星號，一個沒
   跳脫到的字元就讓整則訊息送不出去，而且是在最需要它的時候。
"""

from __future__ import annotations

import datetime as dt

LINE_LIMIT = 4000        # Telegram 單則上限 4096，留一點給分段標記

DISCLAIMER = "建議查核優先序，非違法認定；無公開財報代表資料不足，不代表低風險。"


def as_of() -> str:
    """payload 建置日期。圖與清單都要寫出「這是哪一天的名單」。"""
    from ..api import server

    try:
        stamp = server.PAYLOAD_PATH.stat().st_mtime
    except OSError:
        return ""
    return dt.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d")


def proposal(n: int = 20) -> list[dict]:
    from ..api import server

    return server.get_proposal(n)["proposal"]


def proposal_text(rows: list[dict] | None = None, n: int = 20) -> str:
    rows = proposal(n) if rows is None else rows
    head = f"本批建議查核 {len(rows)} 家（依建議查核優先序）"
    stamp = as_of()
    if stamp:
        head += f"｜資料截至 {stamp}"
    lines = [head, ""]
    for i, p in enumerate(rows, 1):
        tags = [str(p.get("tier") or "")]
        if p.get("np"):
            tags.append(f"{p['np']} 件裁罰史")
        if not p.get("fin"):
            tags.append("無公開財報")
        town = str(p.get("d") or "").removesuffix("區")
        lines.append(f"{i:>2}. {p.get('n')}（{town}）{'・'.join(t for t in tags if t)}")
    lines += ["", "地圖上的紅點編號＝清單序號。", DISCLAIMER]
    return "\n".join(lines)


def answer_text(question: str) -> str:
    """一句話答詢。與 `/api/chat` 同一條路，包含模型不通時退回關鍵字規劃器。"""
    from ..api import server
    from ..api.chat import answer, get_planner

    try:
        r = answer(question, server.payload(), planner=server.chat_planner())
    except Exception:  # noqa: BLE001 - 與 /api/chat 同一個降級：換規劃器，不換答案的界線
        r = answer(question, server.payload(), planner=get_planner("keyword"))

    rows = r.get("results") or []
    lines = [str(r.get("summary") or "（沒有摘要）")]
    if rows:
        lines.append("")
        for i, x in enumerate(rows[:8], 1):
            town = str(x.get("town") or "").removesuffix("區")
            lines.append(f"{i}. #{x.get('rank')} {x.get('title')}（{town}）")
        if len(rows) > 8:
            lines.append(f"…另有 {len(rows) - 8} 筆，請到派工台查看完整清單")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


def chunks(text: str, limit: int = LINE_LIMIT) -> list[str]:
    """依行切段，不在一行中間斷開——斷在園名中間的訊息讀起來像兩家不同的園。"""
    out, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > limit and buf:
            out.append(buf.rstrip("\n"))
            buf = ""
        buf += line + "\n"
    if buf.strip():
        out.append(buf.rstrip("\n"))
    return out or [""]
