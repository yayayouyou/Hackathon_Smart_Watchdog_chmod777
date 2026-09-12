#!/usr/bin/env python3
"""替新聞／PTT 的提及補上「報導性質」與「事件類別」。一次性補完，有快取。

    PYTHONPATH=src .venv/bin/python scripts/classify_news_mentions.py
    PYTHONPATH=src .venv/bin/python scripts/classify_news_mentions.py --dry-run
    PYTHONPATH=src .venv/bin/python scripts/classify_news_mentions.py --show
    PYTHONPATH=src .venv/bin/python scripts/classify_news_mentions.py --compare

分類邏輯與封閉值在 `realtime/news_classify.py`，那支模組的說明講了為什麼新聞
不沿用 Threads 的 `tone`。這支腳本只做三件事：把快照讀成待分類清單、擋掉已經
分類過的、把結果寫進 sidecar。

**來源是唯讀的。** `data/processed/realtime_mentions_ntpc.csv` 是釘住的外部
產物，`python run.py verify-external` 會驗它的雜湊。這支腳本只讀不寫，結果全
進 `data/runtime/news_labels.jsonl`。刪掉那份 sidecar 再跑一次就重建得回來。

**同一則不重跑。** 鍵是「分類時實際送出的那段文字」的雜湊（見
`news_classify.key_for()`），所以重跑整份快照只會對新出現的那幾則呼叫模型。
要強制重跑用 `--force`，而那會真的重新付一次費。

**不接輪詢。** `realtime/mention_poller.py` 每 5 分鐘一輪是為 Threads 通報
設計的——民眾隨時會發文。新聞快照不是每 5 分鐘變一次，掛上去只會在沒有人按過
任何按鈕的下午重複付費。要補跑就跑這支。

沒有 AWS 憑證時不是失敗，是「這條路還沒開」——照實說「未分類」，回 exit 0。
**未分類不等於沒有問題，也不等於性質例行。**
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8
from smart_watchdog.realtime import news_classify

#: 建置時那次全市掃描的結果。**唯讀**，理由見模組說明。
SNAPSHOT = pathlib.Path("data/processed/realtime_mentions_ntpc.csv")


def _items(path: pathlib.Path) -> list[dict]:
    """把快照 CSV 讀成面板那一層的形狀（`channel` / `url` / `headline`）。

    只取分類需要的那幾欄。多讀的欄位會誘使這支腳本拿去做別的判斷，而它唯一
    被允許做的判斷是「這則報導是什麼性質」——認園是
    `features/alerts.py::attribute()` 的事，那條界線在 `classify.py` 的模組
    說明裡講過：讓同一次呼叫同時選定罪名與被告，就是讓一則被說服的文字兩件事
    一起決定。
    """
    if not path.exists():
        print(f"找不到 {path}——先跑 `python run.py sweep` 產生快照。")
        return []
    with path.open(encoding="utf-8") as fh:
        return [{"channel": row.get("channel", ""),
                 "url": row.get("url", ""),
                 "headline": row.get("headline", ""),
                 "kind": row.get("kind", ""),
                 "institution_title": row.get("institution_title", "")}
                for row in csv.DictReader(fh) if row.get("headline")]


def _line(item: dict, label: dict) -> str:
    kind = item.get("kind") or "—"
    report = label.get("report_kind") or "未分類"
    return (f"  [{kind:<9}] → [{report:<4}] "
            f"{news_classify.body_of(item['headline'])[:58]}")


def _show(items: list[dict]) -> int:
    """sidecar 目前有什麼。**不打模型**，離線可跑。"""
    store = news_classify.load(force=True)
    print(f"sidecar {news_classify.PATH}：{len(store)} 筆已分類")
    done = todo = 0
    for item in items:
        label = news_classify.label_of(item["channel"], item["url"],
                                       item["headline"])
        if label["report_kind"]:
            done += 1
        else:
            todo += 1
        print(_line(item, label))
    print(f"\n快照 {len(items)} 則：已分類 {done}、未分類 {todo}")
    if todo:
        print("未分類不等於沒有問題，也不等於性質例行。")
    return 0


def _compare(items: list[dict]) -> int:
    """關鍵字表（`article_kind()`）與模型判斷的對照表。**不打模型**。

    這張表是這支腳本存在的理由本身：關鍵字表追不上記者用詞，而追不上的地方
    只有並排看才看得見。兩者不一致的那幾則是最該由人看一眼的。
    """
    store = news_classify.load(force=True)
    if not store:
        print("sidecar 是空的，還沒有可比對的結果。先跑一次本腳本。")
        return 0
    rows = []
    for item in items:
        label = news_classify.label_of(item["channel"], item["url"],
                                       item["headline"])
        if label["report_kind"]:
            rows.append((item, label))
    grid: dict[tuple, int] = {}
    for item, label in rows:
        grid[(item.get("kind") or "—", label["report_kind"])] = (
            grid.get((item.get("kind") or "—", label["report_kind"]), 0) + 1)
    print(f"{'關鍵字表 kind':<16}{'模型 report_kind':<18}則數")
    for (old, new), n in sorted(grid.items(), key=lambda kv: -kv[1]):
        print(f"{old:<18}{new:<20}{n}")
    print("\n關鍵字表判 unclear、模型判出性質的那幾則：")
    shown = 0
    for item, label in rows:
        if item.get("kind") == "unclear" and label["report_kind"] != "unclear":
            print(f"  [{label['report_kind']}／{label['event_category']}] "
                  f"{news_classify.body_of(item['headline'])[:60]}")
            shown += 1
    if not shown:
        print("  （沒有）")
    return 0


def main() -> int:
    use_utf8()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", default=str(SNAPSHOT),
                    help="來源快照（唯讀）")
    ap.add_argument("--limit", type=int, default=news_classify.DEFAULT_LIMIT,
                    help="本次最多分類幾則（硬上限，超過的留到下一次）")
    ap.add_argument("--backend", default="auto",
                    choices=("auto", "bedrock", "none"),
                    help="auto＝有憑證才跑；none＝一則也不跑")
    ap.add_argument("--force", action="store_true",
                    help="連已經分類過的也重跑（會重新付費）")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="只列出本次會分類哪幾則，不呼叫模型")
    ap.add_argument("--show", action="store_true",
                    help="列出快照每一則目前的標籤，不呼叫模型")
    ap.add_argument("--compare", action="store_true",
                    help="關鍵字表與模型判斷的對照表，不呼叫模型")
    args = ap.parse_args()

    items = _items(pathlib.Path(args.csv))
    if not items:
        return 0
    if args.show:
        return _show(items)
    if args.compare:
        return _compare(items)

    todo = news_classify.pending(items, force=args.force)
    print(f"快照 {len(items)} 則，待分類 {len(todo)} 則"
          + ("（--force：全部重跑）" if args.force else "（其餘已在 sidecar 裡）"))
    if args.dry_run:
        for item in todo[:args.limit]:
            print(f"  {news_classify.body_of(item['headline'])[:70]}")
        if len(todo) > args.limit:
            print(f"  …另有 {len(todo) - args.limit} 則超過本次上限 {args.limit}")
        return 0
    if not todo:
        print("沒有要跑的。要重跑用 --force。")
        return 0

    backend = news_classify.get_backend(args.backend)
    counts = news_classify.classify_items(todo, backend, limit=args.limit)
    if counts["reason"]:
        print(counts["reason"])
        return 0

    print(f"後端 {counts['backend']}：分類 {counts['classified']} 則、"
          f"失敗 {counts['failed']} 則")
    if counts["deferred"]:
        print(f"另有 {counts['deferred']} 則超過本次上限 {args.limit}，"
              "留在待分類佇列（未被丟掉，再跑一次即可）。")
    if counts["by_kind"]:
        print("  報導性質：" + "、".join(
            f"{k} {v}" for k, v in sorted(counts["by_kind"].items())))
    if counts["by_category"]:
        print("  事件類別：" + "、".join(
            f"{k} {v}" for k, v in sorted(counts["by_category"].items())))
    for err in counts["errors"]:
        print(f"  ✗ {err['url'][:60]}：{err['error']}")
    # `issues` 是模型寫的自由文字，**只印在這裡**——不入 sidecar、不進 API、
    # 不上畫面。它的內容受外部標題影響，印在官方主控台上等於讓陌生人的句子
    # 借用系統的口吻說話。理由見 `news_classify.NewsLabel`。
    for note in counts["issues"]:
        print(f"  ⚠ {note['note']}")
    print(f"\n寫入 {news_classify.PATH}"
          "（sidecar；data/processed/ 的快照沒有被改動）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
