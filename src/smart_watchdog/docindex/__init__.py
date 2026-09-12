"""文件索引：回答「今天要找的資訊，在哪一份文件的哪一頁」。

主辦方給的是 162 份 PDF、1.8 GB。要在決賽現場回答稽查員的問題，需要的不是
「把它丟進向量資料庫」——**這個題目用語意相似度會壞掉**，理由有三：

1. **132 份非營利財報是純掃描影像。** `get_text()` 回空字串，沒有文字可以嵌入。
   內容只存在於已抽取的結構化 JSON 裡（含每個區段的頁碼與附註原文）。
2. **公校決算書有 218 頁文字層是 PUA 私有造字亂碼**，而且每頁對應表不同。
   把亂碼嵌入向量空間只會得到有自信的垃圾。
3. **稽查要的是可引用，不是相似。** 稽查員打電話給園所時必須能說「你們
   113 學年度財務報告第 11 頁的附註二(四)寫著……」。近似最近鄰給不出頁碼，
   而**給錯頁碼比答不出來更糟**。

所以這裡建的是**精確定位索引**，分三層，全部離線、確定性、可引用：

    documents   162 份原始 PDF 的目錄（機構、年度、頁數、文字層是否可用）
    sections    某個區段在哪一份文件的哪幾頁（資產負債表 p.5、附註二 p.11-14…）
    facts       某個數字是多少、出自哪一份文件的哪一頁
    passages    可全文檢索的原文（SQLite FTS5，trigram 分詞器處理中文）
    coverage    **索引知道自己不知道什麼**——沒有公開財報的園是涵蓋範圍限制，
                不是合規證明

全部只用標準函式庫的 `sqlite3`。沒有新相依、不需網路、不需要模型呼叫，
因此在會場網路不通時照樣運作。

FTS5 的 `trigram` 分詞器對中文是正確選擇：它做的是字元層級的子字串比對，
不需要斷詞字典，也不會把「業務發展準備金」切成模型自以為的詞。代價是
不做語意擴展——這在稽查場景是**特性不是缺點**。
"""

from __future__ import annotations

from .schema import connect, create_schema
from .search import SearchResult, find_facts, find_passages, locate, summarise_coverage

__all__ = [
    "SearchResult",
    "connect",
    "create_schema",
    "find_facts",
    "find_passages",
    "locate",
    "summarise_coverage",
]
