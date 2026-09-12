"""用 Bedrock 視覺模型實際跑一次財報抽取——`BedrockBackend` 的驅動程式。

`src/smart_watchdog/extract/backends.py` 早就寫好了 `BedrockBackend`，但在這支
檔案出現之前**沒有任何東西呼叫它**：132 份非營利財報的抽取結果是開發階段由
Claude Code subagent 產生後寫進 `data/extracted/nonprofit/` 並進版控的。
於是「交付路徑跑在 Bedrock 上」在程式裡是一句宣告，不是一條跑得起來的路。
這支檔案把那條路接起來。

## 兩個模式

**`--poc`（預設）** 只跑 `data/ground_truth/nonprofit_statements.json` 裡那 5
張人工核對過的頁面，輸出到 `data/interim/poc_extract/`，接著就能直接跑
`scripts/score_extraction.py` 拿到逐格準確率。這是唯一能回答「Bedrock 抽得對
不對」的模式——其餘模式只能回答「抽得出不出來」。基準頁自帶 `file` 與
`page_index`，所以連頁碼都不必推定。

**`--year 113 [--code N01]`** 跑該學年度每份報告的報表頁（資產負債表與本期
收支餘絀表），輸出到 `data/interim/bedrock_extract/`。頁碼由
`ingest/nonprofit_locator.py` 以結構推定，**未經表頭確認**，所以這個模式的產出
要當成待覆核的草稿，不是成品。

## 為什麼不覆寫 data/extracted/nonprofit/

那 132 份是人工作業規範（`docs/EXTRACTION_GUIDE.md`）下產生並經過交叉檢核的
結果，整條下游管線與所有法遵發現都建立在它們之上。決賽前用一次未覆核的模型
輸出換掉它們，風險與收益完全不成比例。因此本腳本**寫不進 `data/extracted/`**
——不是靠約定，是靠 `_assert_safe_out()` 擋下來。要取代正式資料必須是一個
人明確做的決定，不是一個旗標的預設值。

## 成本

Bedrock 沒有 Files API，影像必須 base64 內嵌在每一次請求裡，所以**選頁就是成本
控制**（見 `extract/backends.py` 模組說明）。`--dry-run` 會把要送出的頁數印出來
而不花任何錢，跑之前先看一眼。

用法
    python run.py extract-bedrock                      # 5 張基準頁
    python run.py extract-bedrock -- --dry-run         # 只看要送什麼
    python run.py extract-bedrock -- --year 113 --limit 3
    python run.py score-extraction                     # 對基準頁算準確率
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import pathlib
import re
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8
from smart_watchdog.extract.backends import get_backend
from smart_watchdog.ingest.nonprofit_locator import locate_pages, render_page

use_utf8()

GT_PATH = pathlib.Path("data/ground_truth/nonprofit_statements.json")
REPORT_DIR = pathlib.Path("data/raw/資料集/非營利園財報")
POC_OUT = pathlib.Path("data/interim/poc_extract")
BATCH_OUT = pathlib.Path("data/interim/bedrock_extract")

#: 批次模式送哪些頁。`附註一` 是敘述文字不是表格，STATEMENT_SCHEMA 套不上去，
#: 所以不在這裡——附註的抽取走 `extract/supplement.py` 那條路。
BATCH_PAGES = ("資產負債表", "收支餘絀表_本期")

FILENAME_RE = re.compile(r"^(N\d\d)(.+?)_(\d{3})學年度")

#: 這些字樣代表「再試一次可能就過了」，其餘錯誤重試只是多花錢。
#:
#: 比對的是 `bedrock.explain_error()` 回傳的字串，而它對非特例錯誤產生的是
#: `f"{type(exc).__name__}: {text[:300]}"`——**anthropic SDK 的例外名，不是
#: botocore 的**。限流在這條路上叫 `RateLimitError`（HTTP 429），不叫
#: `ThrottlingException`；只列 botocore 名稱的話限流永遠不會被重試。
#: 兩邊的名稱都留著：走 InvokeModel 時底層 botocore 的字樣仍可能透出來。
RETRYABLE = (
    # anthropic SDK（實際會看到的）
    "RateLimitError", "APITimeoutError", "APIConnectionError",
    "InternalServerError", "429", "529", "overloaded",
    # botocore／Bedrock 服務端
    "Throttling", "TooManyRequests", "ServiceUnavailable",
    "InternalServerException", "ModelTimeout", "timed out",
)


class Job:
    """一頁的抽取工作：要送哪個 PDF 的第幾頁，結果叫什麼名字。"""

    def __init__(self, key: str, pdf: pathlib.Path, page_index: int) -> None:
        self.key = key
        self.pdf = pdf
        self.page_index = page_index

    def __repr__(self) -> str:  # pragma: no cover - 只給 --dry-run 看
        return f"{self.key:28s} {self.pdf.name}  p{self.page_index + 1}"


def _assert_safe_out(out: pathlib.Path) -> None:
    """擋住寫進版控的抽取結果。

    `data/extracted/` 是人工核對過、下游全部依賴的資料。模型輸出要進去必須是
    人明確搬過去的，不能是某支腳本預設行為的副作用。
    """
    resolved = out.resolve()
    forbidden = pathlib.Path("data/extracted").resolve()
    if resolved == forbidden or forbidden in resolved.parents:
        sys.exit(
            f"拒絕寫入 {out}：data/extracted/ 是人工核對過並已進版控的結果，"
            "整條下游管線與所有法遵發現都建立在它上面。\n"
            "模型輸出請寫到 data/interim/，覆核後再由人決定要不要取代。"
        )


def poc_jobs() -> list:
    """基準頁：頁碼直接來自 ground truth，不必推定。"""
    if not GT_PATH.exists():
        sys.exit(f"找不到 {GT_PATH}")
    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    jobs = []
    missing = []
    for key, entry in gt.items():
        if key == "_meta":
            continue
        pdf = pathlib.Path(entry["file"])
        if not pdf.exists():
            missing.append(f"{key}（{pdf}）")
            continue
        jobs.append(Job(key, pdf, int(entry["page_index"])))
    if missing:
        print("⚠ 缺少來源 PDF，這些基準頁跳過："
              + "、".join(missing) + "\n  先跑：python run.py setup-raw <放 zip 的目錄>")
    return jobs


def batch_jobs(year: str, code: str, limit: int) -> list:
    """某學年度的報表頁。頁碼是結構推定，產出須人工確認表頭。"""
    year_dir = REPORT_DIR / f"{year}學年度"
    if not year_dir.exists():
        sys.exit(f"找不到 {year_dir}。先跑：python run.py setup-raw <放 zip 的目錄>")
    jobs = []
    for pdf in sorted(year_dir.glob("*.pdf")):
        m = FILENAME_RE.match(pdf.name)
        if not m:
            continue
        if code and m.group(1) != code:
            continue
        layout = locate_pages(pdf)
        for label in BATCH_PAGES:
            idx = layout.pages.get(label)
            if idx is not None:
                jobs.append(Job(f"{m.group(1)}_{m.group(3)}_{label}", pdf, idx))
        if limit and len({j.pdf for j in jobs}) >= limit:
            break
    return jobs


def run_one(backend, job: Job, dpi: int, retries: int) -> dict:
    """跑一頁，必要時重試。回傳一列摘要。"""
    png = render_page(job.pdf, job.page_index, dpi=dpi)
    result = None
    for attempt in range(retries + 1):
        # BedrockBackend 自己把所有例外收進 result.error，不會拋出來，
        # 所以重試條件看的是錯誤字串而不是 except。
        result = backend.extract(job.key, png)
        if result.error is None or not any(s in result.error for s in RETRYABLE):
            break
        if attempt < retries:
            time.sleep(2 ** attempt)
    return {"job": job, "result": result}


def write_result(out: pathlib.Path, row: dict) -> dict:
    job, result = row["job"], row["result"]
    summary = {
        "key": job.key,
        "source": str(job.pdf),
        "page": job.page_index + 1,
        "backend": result.backend,
        "ok": int(result.ok),
        "error": result.error or "",
        "items": len(result.payload.get("items") or []),
        "issues": len(result.payload.get("issues") or []),
        "checks_passed": len(result.validation.passed),
        "checks_failed": len(result.validation.failed),
        "score": "" if result.validation.score is None
                 else f"{result.validation.score:.3f}",
    }
    if result.payload:
        (out / f"{job.key}.json").write_text(
            json.dumps(result.payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", default="", help="批次模式：學年度，例如 113")
    ap.add_argument("--code", default="", help="批次模式：只跑某一所，例如 N01")
    ap.add_argument("--limit", type=int, default=0, help="批次模式：最多幾份報告")
    ap.add_argument("--out", default="", help="輸出目錄（預設依模式而定）")
    ap.add_argument("--dpi", type=int, default=200,
                    help="影像解析度。越高越清楚也越貴；低解析度判讀不可信"
                         "（見 docs/EXTRACTION_GUIDE.md 硬性規則 7）")
    ap.add_argument("--workers", type=int, default=4,
                    help="併發數。Bedrock 沒有 Batches API，併發與重試是呼叫端的事")
    ap.add_argument("--retries", type=int, default=2, help="限流時的重試次數")
    ap.add_argument("--model", default=None, help="覆寫模型 ID（預設 bedrock.DEFAULT_MODEL）")
    ap.add_argument("--region", default=None, help="覆寫 AWS region")
    ap.add_argument("--dry-run", action="store_true", help="只列出要送哪些頁，不花錢")
    a = ap.parse_args()

    batch = bool(a.year)
    jobs = batch_jobs(a.year, a.code, a.limit) if batch else poc_jobs()
    out = pathlib.Path(a.out) if a.out else (BATCH_OUT if batch else POC_OUT)
    _assert_safe_out(out)

    if not jobs:
        sys.exit("沒有可跑的頁面。")

    mode = f"批次 {a.year}學年度" if batch else "基準頁（可用 score-extraction 評分）"
    print(f"模式：{mode}")
    print(f"輸出：{out}")
    print(f"{len(jobs)} 頁　dpi={a.dpi}　併發={a.workers}\n")
    for job in jobs:
        print("  " + repr(job))

    if a.dry_run:
        print(f"\n--dry-run：沒有送出任何請求。實跑會是 {len(jobs)} 次視覺推論。")
        return

    kwargs = {"region": a.region}
    if a.model:
        kwargs["model"] = a.model
    backend = get_backend("bedrock", **kwargs)
    out.mkdir(parents=True, exist_ok=True)

    print()
    rows = []
    interrupted = False
    t0 = time.monotonic()
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, a.workers))
    try:
        futures = {pool.submit(run_one, backend, j, a.dpi, a.retries): j for j in jobs}
        for future in concurrent.futures.as_completed(futures):
            job = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001 - 一頁失敗不該讓整批停下來
                print(f"  ✗ {job.key:28s} {type(exc).__name__}: {str(exc)[:90]}")
                rows.append({
                    "key": job.key, "source": str(job.pdf), "page": job.page_index + 1,
                    "backend": "bedrock", "ok": 0,
                    "error": f"{type(exc).__name__}: {str(exc)[:200]}",
                    "items": 0, "issues": 0, "checks_passed": 0, "checks_failed": 0,
                    "score": "",
                })
                continue
            summary = write_result(out, row)
            rows.append(summary)
            result = row["result"]
            if result.error:
                print(f"  ✗ {job.key:28s} {result.error[:90]}")
            else:
                mark = "✓" if result.validation.ok else "⚠"
                detail = (f"{summary['items']} 列　恆等式 "
                          f"{summary['checks_passed']}/"
                          f"{summary['checks_passed'] + summary['checks_failed']}")
                note = "" if result.validation.ok else \
                    "　← " + "；".join(result.validation.failed[:2])
                print(f"  {mark} {job.key:28s} {detail}{note}")
    except KeyboardInterrupt:
        # `with` 區塊的 __exit__ 會等所有已排隊的工作跑完——264 頁的批次按了
        # Ctrl-C 之後，剩下 260 頁照樣送進 Bedrock 並計費。取消還沒開始的，
        # 已經送出的那幾個沒辦法收回（錢已經花了），但至少不再往下送。
        interrupted = True
        print("\n⚠ 收到中斷，取消尚未送出的頁面…")
        pool.shutdown(wait=True, cancel_futures=True)
    else:
        pool.shutdown(wait=True)

    if rows:
        rows.sort(key=lambda r: r["key"])
        index = out / "summary.csv"
        with index.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    ok = sum(r["ok"] for r in rows)
    dt = time.monotonic() - t0
    # 分母是「實際跑完的頁」而不是「原本要跑的頁」：中斷或整頁失敗時，
    # 用 len(jobs) 當分母會把沒跑的頁講成沒通過，用 len(rows) 才誠實。
    print(f"\n{ok}/{len(rows)} 頁通過自我驗算　{dt:.0f}s")
    if len(rows) < len(jobs):
        print(f"  {len(jobs) - len(rows)} 頁未執行"
              + ("（中斷）" if interrupted else "（未回報結果）"))
    # 「通過自我驗算」對一張一條恆等式都跑不成的頁面沒有意義：
    # ValidationResult.ok 是「沒有失敗」，不是「有東西驗過」。
    unchecked = [r["key"] for r in rows
                 if r["ok"] and not (r["checks_passed"] + r["checks_failed"])]
    if unchecked:
        print(f"  ⚠ 其中 {len(unchecked)} 頁一條恆等式都跑不成"
              f"（表頭對不上，非通過）：{'、'.join(unchecked[:4])}")
    if rows:
        print(f"寫出 {out}/　摘要 {out / 'summary.csv'}")
    if interrupted:
        sys.exit(130)
    if not batch:
        print("\n接著量測逐格準確率：python run.py score-extraction")
    else:
        print("\n⚠ 批次模式的頁碼是結構推定，未經表頭確認——"
              "產出是待覆核草稿，不要直接當成品用。")


if __name__ == "__main__":
    main()
