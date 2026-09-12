"""示範機構：只給 demo 用的假園所，永遠不進真實資料集。

**為什麼需要這一份。** 決賽現場要放幾則家長通報，通報就得指名某一園。先前的
示範貼文指名的是**真實存在的機構**（蘆洲某私立園、新莊某私立園），而那些
「多收教材費」「只拿到手寫收據」的內容是我們編的。編造的投訴掛在真機構名下、
畫在與真通報一模一樣的版面上、又同步進了資料庫——那已經不是示範，是對一家
真實機構的捏造指控。這與 CLAUDE.md「輸出定位」是同一條界線：系統講的是
「建議查核」，連帶地，示範資料也不准替任何真實機構捏造事實。

所以示範內容一律指名這份檔案裡的園。它們的名字一眼看得出是假的
（`示範一號`／`範例二號`／`樣本三號`…），但**結構像真的**：欄位與
`data/processed/institutions_ntpc.csv` 完全相同，`indoor_area_per_child`
等於 `size_in / count_approved`、`size` 等於室內加室外、月費與核定人數落在
該設立別的真實分布內，所以畫面上排版、分層、統計都看得出該有的樣子。

**三條界線。**

1. **示範機構只走社群／歸屬這條路。** `mention_store.attribute_post()` 要有它們
   才認得出示範貼文指名的是誰；`/api/social` 要有它們才印得出園名。除此之外
   哪裡都不去——不寫進 `data/processed/`、不進 `dist/data/payload.json`、
   不進 `risk/priority.py`。那條路上的數字是量測過的（AUC 0.641／P@100 2.17x），
   混進六筆造出來的園就再也不是那個數字了。這支模組因此只被
   `scripts/sync_threads_mentions.py` 與 `api/social.py` 匯入，
   `tests/test_demo_data.py` 把這件事釘住。
2. **旗標是 id，不是名字。** `is_demo()` 比對的是這份檔案裡的 id
   （`demo0001-…`），不是「名字看起來假不假」。名字是給人看的，
   旗標是給程式檢查的——用名字判斷的話，哪天有人把示範貼文的文字改了，
   標示就會靜靜消失。
3. **畫面上一定要標。** API 每一則帶 `is_demo`，前端在園名旁印「示範資料」。
   看得出是假的名字不夠：截圖會被單獨傳出去，而截圖裡沒有人可以問。
"""

from __future__ import annotations

import csv
import functools
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]

#: 示範機構主檔。欄位與 `data/processed/institutions_ntpc.csv` 完全相同，
#: 但**刻意放在 `data/demo/`**：`data/processed/` 底下的每一個檔案都可能被
#: 某支分析腳本 glob 進去，而這六筆一旦進了那些腳本就再也分不出來。
DEMO_CSV = ROOT / "data/demo/institutions_demo.csv"

#: 示範 id 的前綴。`is_demo()` 不靠它判斷（靠的是檔案裡實際有哪些 id），
#: 但它讓人在資料庫、日誌、API 回應裡一眼看得出這一列是示範資料。
ID_PREFIX = "demo"


@functools.lru_cache(maxsize=1)
def _rows() -> tuple[dict, ...]:
    if not DEMO_CSV.exists():
        return ()
    with DEMO_CSV.open(encoding="utf-8") as fh:
        return tuple(row for row in csv.DictReader(fh) if row.get("id"))


def reload() -> None:
    """丟掉快取，下一次呼叫重讀檔案。給測試與改完檔案要立即生效時用。"""
    _rows.cache_clear()


def institutions() -> list[dict]:
    """示範機構清單，欄位與 `run_realtime_sweep.py` 餵給各管道的那份一致。

    只回歸屬與顯示需要的三欄加上 `type`（分層展示用）。多回的欄位會誘使
    呼叫端拿去算東西，而這六筆不是用來算東西的。
    """
    return [{"id": r["id"], "title": r.get("title", ""),
             "town": r.get("town", ""), "type": r.get("type", "")}
            for r in _rows()]


@functools.lru_cache(maxsize=1)
def ids() -> frozenset[str]:
    """完整 id 與它的前 8 碼都放進來。

    兩種寫法在系統裡都真的會出現：`threads_mention.institution_id` 存完整
    UUID，payload 的 `points[].i` 只有前 8 碼。少收一種，`is_demo()` 就會在
    其中一條路上安靜地回 False——而那正是「示範資料」標示消失的方式。
    """
    out: set[str] = set()
    for row in _rows():
        full = str(row["id"])
        out.add(full)
        out.add(full[:8])
    return frozenset(out)


def is_demo(institution_id: str | None) -> bool:
    """這個 id 是不是示範機構。完整 UUID 與 8 碼前綴都認得。"""
    key = str(institution_id or "")
    if not key:
        return False
    return key in ids() or key[:8] in ids()


def merged(real: list[dict] | None = None) -> list[dict]:
    """真實機構 + 示範機構，給歸屬與顯示用的同一份合併清單。

    合併只發生在這裡，呼叫端不各自 append——兩邊各寫一次 append 的下場是
    其中一邊哪天忘了加，於是示範貼文在某一支端點上變成「認不出是哪一園」。
    """
    return list(real or []) + institutions()
