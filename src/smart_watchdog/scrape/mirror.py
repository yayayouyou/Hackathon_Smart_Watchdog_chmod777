"""逐園裁罰檔：補上 `punish_all.json` 缺的那一樣東西——**處分書文號**。

`registry.py` 的 docstring 自己承認過這個缺口：

    without an upstream event identifier, equal fields do not prove that two
    sanctions are the same event

同一張處分書上開了三條，`punish_all.json` 就是三列彼此無法辨識的紀錄；同一天
對同一園開的兩張不同處分書，看起來也是同樣的三列。少了事件層級的識別碼，
「裁罰次數」只能數條款列，而條款列數與**被抓到幾次**是兩回事。

上游另有一份以**園名**為檔名的逐園裁罰（`data/punish/<縣市>/<園名>.json`），
每一列多帶了兩個欄位：**處分書文號**與**處罰法源**。本模組只負責把它讀進來、
正規化、並對上我們的機構主檔。

## 這份資料能回答而 punish_all 回答不了的四件事

1. **裁罰次數 vs 條款列數**。新北 1,474 列 ↔ 1,029 張處分書，487 園中有 187 園
   兩者不同，最大差 11 件（智盛 23 列 → 12 件）。現行 `n_penalties_prior`
   系統性高估「被抓次數多」的園。
2. **重複計入的罰鍰**。同園同文號同法源且處分內容逐字相同的重複條款列共 90 列、
   涉 5,550,000 元、80 園。最典型是「第16條第1項-年齡規定」與「第16條第1項-混齡
   規定」各記一列 60,000 元，實為一張處分書一筆罰鍰。
3. **已退場園的裁罰**。`punish_all.json` 的頂層 key 是人，人一旦從名冊消失，
   他底下的紀錄整批不見。7 家退場園的 22 列只存在於這份逐園檔，含 4 列廢止設立
   許可、3 列停止招生——那是**裁罰的結果標籤**，全市其他地方拿不到。
4. **重罰後改名**。同一個機構 id 出現在新舊兩個檔名下（菲力→幼禾：2021 年被
   停止招生＋減少招收人數後改名）。punish_all 只留現名，這個行為看不見。

## ⚠️ 這份資料不接進模型特徵

`PRIORITY_FEATURES` 對應的 AUC 0.640／P@100 2.17x 屬於**現在這一組特徵值**。
把裁罰次數從條款列改成處分書件，等於換掉三個特徵的定義，已公布的數字就不再
成立。本模組因此只做「讀進來、對上、比對」，不改 `features/build.py`。
要改是另一次明確的決定，且必須重跑 `run.py baseline`。

授權見 `data/external/README.md`：原始資料來源為全國教保資訊網，資料 CC-BY。
"""

from __future__ import annotations

import collections
import json
import pathlib
import re
import urllib.parse
import urllib.request

MIRROR = "https://kiang.github.io/ap.ece.moe.edu.tw"
API_CONTENTS = "https://api.github.com/repos/kiang/ap.ece.moe.edu.tw/contents"
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_DIR = PROJECT_ROOT / "data" / "external" / "mirror"
DEFAULT_PENALTIES = DEFAULT_DIR / "punish_by_school_ntpc.json"

#: 逐園檔一列的欄位順序。第 7 欄不固定出現（1,406 列 6 欄、102 列 7 欄），
#: 內容是原始機構名——改名或換受託單位時與檔名不同。假設固定 6 欄會 IndexError。
FIELDS = ("date", "doc_no", "statute", "law", "actor", "punishment")

#: 文號的上游打字變體。不正規化會把同一張處分書拆成兩件。
#: (a) 「教幼字字第」重複一個「字」，實測 25 列；
#: (b) 尾碼多一位數字，如 1130034721 vs 11300347212。
_DOC_DUP_CHAR = re.compile(r"教幼字字第")
_DOC_DIGITS = re.compile(r"第(\d+)號")


def _get(url: str, timeout: int = 120) -> bytes:
    # GitHub API 回的 download_url 是未百分比編碼的 UTF-8；urllib 直接開會噴
    # UnicodeEncodeError('ascii')，因為 http.client 用 latin-1 編 request line。
    safe = urllib.parse.quote(url, safe=":/?&=%#")
    with urllib.request.urlopen(safe, timeout=timeout) as resp:
        return resp.read()


def normalise_doc_no(value: object) -> str:
    """把處分書文號正規化到可以當鍵。

    只處理**已實測確認**的兩種上游打字變體，不做模糊比對：文號是要拿來合併
    紀錄的鍵，合錯就是把兩張處分書講成一張。
    """
    text = "".join(str(value or "").split())
    text = _DOC_DUP_CHAR.sub("教幼字第", text)
    # 尾碼多一位：把 11 位數字截回 10 位只有在前 10 位與另一筆相同時才成立，
    # 那個判斷要有全體資料才做得了，所以這裡只回傳原樣，由 `event_key` 處理。
    return text


def event_key(school: str, doc_no: object) -> tuple:
    """事件鍵＝(園, 文號)。

    ⚠️ **文號不是全域唯一**：實測 28 個文號跨多個檔名，多數是改名配對，但
    `新北府教幼字第1130236665號` 同時出現在安興非營利與私立智盛兩個不相干的
    園，另有 2 個文號跨不同日期。只用文號當鍵會把兩園的處分書併成一件。
    """
    return (str(school or "").strip(), normalise_doc_no(doc_no))


def parse_rows(school: str, raw: object) -> list:
    """把一個園的原始列轉成 dict，容忍 6 欄與 7 欄兩種長度。"""
    out = []
    for row in raw or []:
        if not isinstance(row, (list, tuple)) or len(row) < len(FIELDS):
            continue
        entry = dict(zip(FIELDS, row[: len(FIELDS)]))
        entry["school"] = str(school).strip()
        # 第 7 欄是上游記的原始機構名；與檔名不同代表改名或換受託單位。
        entry["source_title"] = str(row[6]).strip() if len(row) > 6 else ""
        entry["doc_no_norm"] = normalise_doc_no(entry["doc_no"])
        out.append(entry)
    return out


def load_penalties_by_school(
    path: pathlib.Path | str = DEFAULT_PENALTIES,
) -> dict[str, list[dict]]:
    """讀已釘住的逐園裁罰快照。不打網路，測試用得上。"""
    raw = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    records = raw.get("schools", raw) if isinstance(raw, dict) else raw
    return {school: parse_rows(school, rows) for school, rows in records.items()}


def list_school_files(city: str = "新北市") -> list[dict]:
    """列出某縣市底下的逐園裁罰檔（走 GitHub API，回 name 與 download_url）。"""
    url = f"{API_CONTENTS}/docs/data/punish/{city}"
    data = json.loads(_get(url).decode("utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"預期得到檔案列表，實得：{str(data)[:200]}")
    return [x for x in data if x.get("type") == "file"
            and str(x.get("name", "")).endswith(".json")]


def fetch_penalties_by_school(city: str = "新北市") -> dict[str, list]:
    """抓某縣市全部逐園裁罰。回傳 {園名: 原始列}，不做正規化。

    **不要用 `curl -O`**：URL 最後一段是百分比編碼的中文檔名，展開後超過
    Windows 255 bytes 的檔名上限，497 檔會靜默只成功 433 檔。這裡一律
    在記憶體內組成單一 JSON，不落地成幾百個中文檔名。
    """
    out: dict[str, list] = {}
    for item in list_school_files(city):
        school = str(item["name"])[: -len(".json")]
        out[school] = json.loads(_get(item["download_url"]).decode("utf-8"))
    return out


# ── 與我們現有的裁罰表對照 ──────────────────────────────────────────
def documents(by_school: dict[str, list[dict]]) -> dict[tuple, list[dict]]:
    """依 (園, 文號) 聚合成處分書。"""
    docs: dict[tuple, list[dict]] = collections.defaultdict(list)
    for school, rows in by_school.items():
        for r in rows:
            docs[event_key(school, r["doc_no"])].append(r)
    return dict(docs)


def duplicate_rows(by_school: dict[str, list[dict]]) -> list[dict]:
    """同園＋同文號＋同法源＋處分內容逐字相同的重複條款列。

    ⚠️ **不可只用文號聚合罰鍰**：實測有 34 組同文號同法源但金額不同的真實多筆
    罰鍰（佳學 60,000＋150,000、實栽童心 3,000＋15,000）。只有處分內容字串
    完全相同才判定為重複列——寧可漏抓幾組，不可把真實的兩筆罰鍰併成一筆。
    """
    dupes = []
    for rows in documents(by_school).values():
        seen: dict[tuple, int] = collections.Counter()
        for r in rows:
            k = (r["statute"], r["punishment"])
            seen[k] += 1
            if seen[k] > 1:
                dupes.append(r)
    return dupes


def new_since(by_school: dict[str, list[dict]], as_of: str) -> list[dict]:
    """日期嚴格晚於 `as_of`（YYYY/MM/DD）的裁罰。

    用途是前瞻驗證：這些裁罰發生在我們釘住的快照之後，模型訓練時看不到它們，
    所以拿來檢驗排序不會有洩漏。

    ⚠️ `punish_all.json` **會回溯補登**：實測有一筆 2026/07/08 的裁罰不在
    2026/08/10 抓的快照裡。凍結日不等於資料完整日，做時序切分時 `as_of`
    要再往前退一個月，否則會把「補登的舊案」當成「新發生的案」。
    """
    return sorted(
        (r for rows in by_school.values() for r in rows
         if str(r.get("date") or "") > as_of),
        key=lambda r: (r["date"], r["school"]),
    )
