"""把分類 sidecar 匯出成**不含內文**的版本，這一份才進版控。

## 為什麼要分成兩份

`data/runtime/news_labels.jsonl` 是分類當下的完整紀錄，裡面留著送給模型的那段
標題／貼文節錄——留著是為了之後能判斷「這個標籤是根據哪段字下的」。但新聞與
Threads 貼文都不是開放授權，我們的立場一直是**只保存 provenance，不重製內文**
（`data/external/news/` 同一條規則）。所以 `data/runtime/` 整個 gitignore，
內文永遠只留在跑分類的那台機器上。

匯出的這一份只有：key、channel、url、兩個標籤、分類時間。key 是分類時對
「channel + url + 標題」算的雜湊，換句話說它是**單向的**——拿得到 key 反推不回
標題。所以這份檔案可以進版控，別人 clone 下來即時圖層就是亮的，不必重跑一次
Bedrock（那要花錢，而且不同時間跑結果不保證一樣）。

⚠️ 這份檔案**不是**分類的真相來源。真相是 sidecar；這只是它的公開投影。
`payload._news_labels()` 兩份都讀，本機 sidecar 蓋過匯出檔——本機重跑過分類的
人看到的一定是自己剛跑出來的結果，不是版控裡的舊快照。

Usage
    PYTHONPATH=src .venv/Scripts/python scripts/export_news_labels.py
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "data/runtime/news_labels.jsonl"
OUT = ROOT / "data/external/news_labels_public.jsonl"

#: 白名單而不是黑名單。之後 sidecar 加欄位時，預設是**不**外流。
KEEP = ("key", "channel", "url", "report_kind", "event_category",
        "backend", "classified_at")


def main() -> None:
    if not SRC.exists():
        print(f"沒有 {SRC}——先跑 scripts/classify_news_mentions.py")
        raise SystemExit(1)
    rows = []
    for line in SRC.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not r.get("key"):
            continue
        rows.append({k: r[k] for k in KEEP if k in r})
    rows.sort(key=lambda r: (r.get("classified_at", ""), r["key"]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    kinds: dict[str, int] = {}
    for r in rows:
        k = r.get("report_kind") or "（未分類）"
        kinds[k] = kinds.get(k, 0) + 1
    print(f"寫出 {OUT.relative_to(ROOT)}　{len(rows)} 則（無內文）")
    for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {k}　{n}")


if __name__ == "__main__":
    main()
