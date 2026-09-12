"""agent 講解句的軟過濾。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/agent/narration.py`。
禁用詞表改用本 repo 既有的 `report.verify.FORBIDDEN`（來源專案是
`memo.gate.BANNED`，那支沒搬）——**一份表，兩處用**，否則建議書與講解句會
對「什麼話不能說」各有一套標準。

與建議書共用同一份表，但處置不同：建議書命中就退件重寫（`report/verify.py`），
講解句命中就替換並記錄。理由是講解句是即時的，沒有重試的餘裕，而它不是正式
文件。替換紀錄要留著，因為「模型多常想說認定性的話」本身就是一個該被看見的指標。
"""

from __future__ import annotations

import re

from ..report.verify import FORBIDDEN

REPLACEMENT = "訊號指向的情形"

# 內部代碼對照。模型引用 tool 回傳的原始欄位值是誠實的，但那些字串是給程式看的，
# 稽查員看到 tier 是 financial_high 只會困惑。畫面上其他地方（名單、時光機）
# 都已經有中文對照，講解句是唯一漏掉的地方。
_JARGON = {
    "financial_high": "財務高風險",
    "penalty_risk": "裁罰風險",
    "insufficient_data": "資料不足",
    "priority_rank": "交付順序",
}

# 講解句是一行純文字，不是 markdown。模型會照它的習慣寫成帶 ** 與 ## 的段落，
# 那些符號會原樣漏到畫面上。
_MD_BOLD = re.compile(r"\*{1,3}(.+?)\*{1,3}")
_MD_HEAD = re.compile(r"^#{1,6}\s*", re.MULTILINE)
_MD_CODE = re.compile(r"`([^`]*)`")


def strip_markup(text: str) -> str:
    """拿掉 markdown 標記與內部代碼，回傳可以直接顯示的一行文字。"""
    out = _MD_HEAD.sub("", text)
    out = _MD_BOLD.sub(r"\1", out)
    out = _MD_CODE.sub(r"\1", out)
    for code, zh in _JARGON.items():
        out = out.replace(code, zh)
    return " ".join(out.split())


def soften(text: str) -> tuple[str, list[str]]:
    """回傳（可直接顯示的文字, 命中的禁用詞）。

    先拿掉 markdown 與內部代碼**再**過濾禁用詞——順序反過來的話，被替換進去的
    中文可能又被當成 markdown 內容處理。
    """
    out = strip_markup(text)
    hits = [w for w in FORBIDDEN if w in out]
    for word in hits:
        out = out.replace(word, REPLACEMENT)
    return out, hits
