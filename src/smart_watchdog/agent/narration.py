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

from ..report.verify import FORBIDDEN, NEGATED

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

    `NEGATED` 裡的說法要先挖掉再比對，理由與 `report/verify.py` 完全一樣：
    那些說法**否定**違法認定，是治理文件要求要講的那一句。少了這一步，模型
    複述每個 tool 都會回傳的那句 CAVEAT（「這是建議查核的優先序，不是違法認
    定」）就會被改成「不是訊號指向的情形認定」——護欄親手把它要保護的句子改
    壞，而且畫面上還會掛一條「已替換認定性用語：違法」，看起來像模型講錯話。

    `verify.py` 的第一版踩過同一個坑（它把自己 143 封建議書全退了），那裡已經
    有現成的表。**一份表，兩處用**，跟 FORBIDDEN 的道理一樣。
    """
    out = strip_markup(text)
    scan = out
    for phrase in NEGATED:
        scan = scan.replace(phrase, "")
    hits = [w for w in FORBIDDEN if w in scan]
    for word in hits:
        # 只替換沒有被否定說法包住的那些。把 NEGATED 的說法暫時換成佔位字元，
        # 替換完再換回來——否則「非違法認定」裡的「違法」還是會被掃到。
        keep = {f"\x00{i}\x00": ph for i, ph in enumerate(NEGATED)}
        for token, phrase in keep.items():
            out = out.replace(phrase, token)
        out = out.replace(word, REPLACEMENT)
        for token, phrase in keep.items():
            out = out.replace(token, phrase)
    return out, hits
