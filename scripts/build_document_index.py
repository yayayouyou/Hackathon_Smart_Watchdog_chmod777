"""建立文件索引：回答「今天要找的資訊在哪一份文件的哪一頁」。

    python run.py doc-index                 建索引（用快取的分基金切割）
    python run.py doc-index -- --rescan     重掃 15 冊公校決算書（慢，約數分鐘）
    python run.py doc-index -- --query 業務發展準備金
    python run.py doc-index -- --locate 安溪 --year 113

產出 `data/processed/document_index.sqlite`。設計理由見
`src/smart_watchdog/docindex/__init__.py`——簡短版是：這個語料不能用語意相似度，
因為 132 份財報是純掃描影像、218 頁公校文字層是造字亂碼，而稽查需要的是
可引用的頁碼而不是近似最近鄰。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8

use_utf8()

from smart_watchdog.docindex import build as build_mod
from smart_watchdog.docindex import schema, search


def _print_results(results: list, header: str) -> None:
    print(f"\n── {header}（{len(results)} 筆）")
    for r in results:
        d = r.as_dict() if hasattr(r, "as_dict") else r
        cite = d.get("citation", "")
        text = (d.get("text") or d.get("label") or "").replace("\n", " ")
        print(f"  {cite}")
        print(f"    {d.get('kind', '')} {d.get('label', '')}")
        if text and text != d.get("label"):
            print(f"    {text[:160]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rescan", action="store_true",
                    help="重掃公校決算書分基金切割（不用快取）")
    ap.add_argument("--no-public", action="store_true",
                    help="只建非營利園部分（跳過需要 data/raw 的公校掃描）")
    ap.add_argument("--query", help="建完後試查一個字串")
    ap.add_argument("--locate", help="建完後試查某園的區段位置")
    ap.add_argument("--year", type=int, help="搭配 --locate／--query 的年度")
    ap.add_argument("--no-build", action="store_true", help="不重建，只查詢現有索引")
    a = ap.parse_args()

    path = schema.DEFAULT_PATH
    if not a.no_build:
        raw = pathlib.Path("data/raw")
        with_public = not a.no_public and raw.exists()
        if not with_public and not a.no_public:
            print("data/raw 不存在，只建非營利園部分"
                  "（抽取結果已進版控，公校分基金切割需要原始 PDF）")
        stats = build_mod.build(path, rescan=a.rescan, with_public=with_public)
        print(f"\n寫入 {path}")
        print(f"  文件 {stats['documents']}　區段 {stats['sections']}　"
              f"事實 {stats['facts']}　可檢索段落 {stats['passages']}")
        size = path.stat().st_size / 1024 / 1024
        print(f"  索引大小 {size:.1f} MB")

    conn = schema.connect(path)
    print("\n── 涵蓋範圍")
    for c in search.summarise_coverage(conn):
        print(f"  {c['scope']:20s} {c['covered']}/{c['total']}")
        print(f"    {c['note']}")

    if a.locate:
        _print_results(
            search.locate(conn, institution=a.locate, year=a.year, limit=12),
            f"{a.locate} 的區段位置")
    if a.query:
        _print_results(
            search.find_passages(conn, a.query, year=a.year, limit=8),
            f"全文檢索「{a.query}」")
        facts = search.find_facts(conn, field=a.query, year=a.year, limit=8)
        if facts:
            print(f"\n── 相符欄位（{len(facts)} 筆）")
            for f in facts:
                v = "（空白／未編列）" if f["is_blank"] else f"{f['value']:,.0f} {f['unit']}"
                print(f"  {f['institution']} {f['year']}{f['year_kind']} "
                      f"{f['label']}：{v}")
                print(f"    {f['citation']}")
    conn.close()


if __name__ == "__main__":
    main()
