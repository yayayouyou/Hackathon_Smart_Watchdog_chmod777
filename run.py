#!/usr/bin/env python3
"""小小守護員的單一指令入口——Mac 與 Windows 跑同一條管線。

    python run.py --list           列出所有任務
    python run.py serve            啟動動態版稽查派工台
    python run.py pipeline         重跑整條離線分析管線
    python run.py test             跑測試

**為什麼需要這支檔案。** 原本每個腳本的 docstring 都寫
`PYTHONPATH=src .venv/bin/python scripts/X.py`，這在 Windows 上三處都錯：
執行檔在 `Scripts\\` 不是 `bin/`、`VAR=value cmd` 不是 PowerShell 語法、
中文語系主控台把輸出導向檔案時會用 cp950 而在印出警告的那一行當掉。
結果是同一個 repo 在兩個平台有兩套指令，而兩套指令會各自腐爛。

這支檔案把那三件事收斂成一處：

* **直譯器**——依平台找 `.venv/Scripts/python.exe` 或 `.venv/bin/python`；
  已經在 venv 裡跑就直接用自己。
* **匯入路徑**——`PYTHONPATH=src` 其實是多餘的（每個腳本都自己
  `sys.path.insert`），但仍然設好，這樣從任何工作目錄呼叫都成立。
* **編碼**——一律 `PYTHONIOENCODING=utf-8`，警告訊息不會把行程弄死。

本檔只用標準函式庫，因此**在建好 venv 之前就能執行**（`python run.py setup`
會把 venv 建起來）。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent
IS_WINDOWS = os.name == "nt"
VENV_PYTHON = ROOT / (".venv/Scripts/python.exe" if IS_WINDOWS else ".venv/bin/python")


def _own_stdout_utf8() -> None:
    """本檔自己的輸出也要 UTF-8。

    `child_env()` 只管子行程；`run.py` 自己印的 ✓／✗ 同樣是 cp950 編不出來的
    字元，`python run.py frontend > log.txt` 會在印出結果那一行當掉。這段不能
    改成 import `smart_watchdog.console`——本檔必須在 venv 建好之前就能執行。
    """
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


_own_stdout_utf8()


class Task:
    """一個管線步驟。needs_raw／needs_network 是給人看的前置條件，不是自動解相依。"""

    def __init__(self, name: str, desc: str, argv: list, *,
                 group: str = "分析", needs_raw: bool = False,
                 needs_network: bool = False, in_pipeline: bool = False):
        self.name = name
        self.desc = desc
        self.argv = argv
        self.group = group
        self.needs_raw = needs_raw
        self.needs_network = needs_network
        self.in_pipeline = in_pipeline

    def badges(self) -> str:
        b = []
        if self.needs_raw:
            b.append("需 data/raw")
        if self.needs_network:
            b.append("需網路")
        return "  [" + "、".join(b) + "]" if b else ""


def _s(script: str) -> list:
    return [str(ROOT / "scripts" / script)]


# 順序即是 pipeline 的執行順序：抽取 → 整併 → 檢核 → 排序 → 前端。
# 每一步只依賴它前面那些步驟的產物，加新步驟時要維持這個性質。
TASKS = [
    # ── 準備 ──────────────────────────────────────────────────────
    Task("setup", "建立 venv 並安裝所有相依（首次執行這個）", [], group="準備"),
    Task("setup-raw", "從主辦方 zip 還原 data/raw（Big5 檔名）",
         _s("setup_raw_data.py"), group="準備"),
    Task("check-credentials", "實測 .env 裡的每一把金鑰",
         _s("check_credentials.py"), group="準備"),
    # 建表是冪等的，服務啟動時也會做一次；這個任務多做的是**建帳號**
    # （取 .env 的 SEED_INSPECTOR_EMAIL／PASSWORD）。沒有帳號就登不進助理頁。
    Task("seed-users", "建立資料表與稽查員帳號（冪等；助理與登入需要）",
         _s("seed_users.py"), group="準備"),
    Task("bedrock-check", "實測 Bedrock：憑證、可用模型、三個 AI 落點",
         _s("check_bedrock.py"), group="準備", needs_network=True),

    # ── 抽取（需要 data/raw）─────────────────────────────────────
    Task("survey-pdfs", "盤點 162 份 PDF 的頁數／文字層／是否需 OCR",
         _s("survey_pdfs.py"), group="抽取", needs_raw=True, in_pipeline=True),
    Task("survey-text", "偵測公校決算書的 PUA 亂碼頁（不可用的文字層）",
         _s("survey_text_quality.py"), group="抽取", needs_raw=True, in_pipeline=True),
    Task("extract-public", "從決算書座標抽出 22 所市立幼兒園財務（零模型成本）",
         _s("extract_public_kindergartens.py"), group="抽取",
         needs_raw=True, in_pipeline=True),
    # 兩者都不進 pipeline：都會真的花錢呼叫模型。用途不同，刻意並存——
    # extract-bedrock 量的是「抽得對不對」（對照人工基準頁），
    # extract-pages 量的是「抽得到多少」（依目錄定向的大量抽取）。
    # 先跑前者確認準確率，再跑後者鋪量，順序反過來就是在賭。
    Task("extract-bedrock", "用 Bedrock 視覺模型實跑抽取（預設 5 張基準頁，--dry-run 可試算）",
         _s("extract_nonprofit_bedrock.py"), group="抽取",
         needs_raw=True, needs_network=True),
    Task("extract-pages", "Bedrock 依目錄定向逐頁抽取 132 份非營利財報（可續跑）",
         _s("extract_pages_bedrock.py"), group="抽取",
         needs_raw=True, needs_network=True),

    # ── 整併 ──────────────────────────────────────────────────────
    Task("institution-master", "建立 1,213 筆機構主檔與裁罰標籤表",
         _s("build_institution_master.py"), in_pipeline=True),
    Task("nonprofit-panel", "把 132 份非營利園抽取結果整併成單一面板",
         _s("build_nonprofit_panel.py"), in_pipeline=True),
    Task("pagewise-facts", "把逐頁抽取正規化成長表（法遵補判的依據）",
         _s("build_pagewise_facts.py"), in_pipeline=True),
    Task("crosswalk", "財報年度對應登記身分（處理法人更替）",
         _s("build_nonprofit_registry_crosswalk.py"), in_pipeline=True),

    # ── 檢核與訊號 ────────────────────────────────────────────────
    Task("compliance", "拿每份報告自己的附註二檢核它自己（軌 B 的核心）",
         _s("run_compliance_checks.py"), in_pipeline=True),
    Task("reserve", "準備金專戶缺口的跨年度走勢（區分時間差與缺口累積）",
         _s("check_reserve_timeseries.py"), in_pipeline=True),
    Task("personnel", "人事費短支與業務發展準備轉列的併存情形",
         _s("analyse_personnel_to_reserve.py"), in_pipeline=True),
    Task("identity", "驗證每份抽取確實屬於檔名所指的那所園",
         _s("verify_extraction_identity.py"), in_pipeline=True),
    Task("ml-features", "把頁級事實整理成園×學年度的寬表（ML 可直接讀）",
         _s("build_ml_features.py"), in_pipeline=True),
    Task("anomaly", "非營利園同儕財務異常排序（同年度、同類型，僅比率）",
         _s("build_nonprofit_anomaly.py"), in_pipeline=True),
    Task("cohort", "挑出 9 案例加 9 對照的配對設計",
         _s("select_forensic_cohort.py"), in_pipeline=True),
    Task("forensic", "測試 12 個鑑識訊號（全部未通過，保留為否證紀錄）",
         _s("test_forensic_signals.py"), in_pipeline=True),
    Task("events", "組出 1,501 筆官方事件時間線",
         _s("build_official_events.py"), in_pipeline=True),

    Task("doc-index", "建文件索引：哪個資訊在哪一份文件的哪一頁",
         _s("build_document_index.py"), in_pipeline=True),
    Task("timeline", "時間軸回測：每年重訓一次，看當時的排序後來對不對",
         _s("build_timeline.py"), in_pipeline=True),

    # ── 輸出 ──────────────────────────────────────────────────────
    Task("priority", "組裝稽查優先序——系統真正的輸出",
         _s("build_audit_priority.py"), group="輸出", in_pipeline=True),
    Task("letters", "為名單上每一所園草擬稽核建議書",
         _s("build_audit_letters.py"), group="輸出", in_pipeline=True),
    Task("neighbor-land", "產生鄰縣市陸地輪廓（地圖反灰只灰陸地、不灰海）",
         _s("build_neighbor_land.py"), group="輸出", needs_network=True),
    Task("frontend", "產生 dist/（靜態單檔版與動態版共用的 payload）",
         _s("build_frontend.py"), group="輸出", in_pipeline=True),
    Task("serve", "啟動動態版稽查派工台，網址 http://127.0.0.1:8000",
         _s("serve.py"), group="輸出"),

    # ── 量測 ──────────────────────────────────────────────────────
    Task("eda", "單變量訊號強度檢定（時序切分）",
         _s("eda_signal_strength.py"), group="量測"),
    Task("baseline", "軌 A 基線模型：AUC 與前 100 名命中率",
         _s("baseline_model.py"), group="量測"),
    Task("panel-model", "非營利面板建模：量化「這個資料量撐不起監督式模型」",
         _s("model_nonprofit_panel.py"), group="量測"),
    Task("score-extraction", "以人工基準量測抽取準確率",
         _s("score_extraction.py"), group="量測", needs_raw=True),
    Task("validate-extraction", "以會計恆等式量化抽取品質",
         _s("validate_extraction.py"), group="量測", needs_raw=True),
    Task("validate-anomaly", "異常排序的三項驗證：Top-K、穩定性、LOO 敏感度",
         _s("validate_nonprofit_anomaly.py"), group="量測"),

    # ── 外部（需要網路）──────────────────────────────────────────
    Task("fee-table", "重抓 109 到 114 學年度收費明細（約 10 分鐘）",
         _s("build_fee_table.py"), group="外部", needs_network=True),
    Task("evaluations", "全量抓取官方評鑑紀錄",
         _s("download_evaluation_ntpc.py"), group="外部", needs_network=True),
    Task("snapshots", "更新或採認外部公開資料快照",
         _s("download_external_snapshots.py"), group="外部", needs_network=True),
    # 逐園裁罰檔：補上 punish_all.json 缺的處分書文號。刻意不進 pipeline——
    # 它不餵模型特徵（會動到已公布的 AUC），只作旁證與前瞻驗證。
    Task("mirror-extras", "抓逐園裁罰檔（補處分書文號；-- --report 看它能回答什麼）",
         _s("download_mirror_extras.py"), group="外部", needs_network=True),
    Task("sweep", "掃一次即時管道並記錄提及",
         _s("run_realtime_sweep.py"), group="外部", needs_network=True),
    # 標 needs_network 是給人看的前置條件，講的是預設路徑：沒有網路時
    # `-- --fixture tests/fixtures/threads_mentions.json` 一樣跑得完，
    # 決賽現場的主線其實是那一條。
    Task("threads-sync", "同步 Threads 上 @標註官方帳號的民眾通報進資料庫",
         _s("sync_threads_mentions.py"), group="外部", needs_network=True),

    # ── 開發 ──────────────────────────────────────────────────────
    Task("test", "跑測試套件", ["-m", "pytest", "tests/", "-q"], group="開發"),
    Task("lint", "跑 ruff", ["-m", "ruff", "check", "src/", "tests/", "scripts/", "run.py"],
         group="開發"),
    Task("verify-external", "離線驗證每一份釘住的外部資料產物",
         _s("verify_external_artifacts.py"), group="開發"),
]

BY_NAME = {t.name: t for t in TASKS}


def interpreter() -> str:
    """要用哪個 python 跑子行程。"""
    if VENV_PYTHON.exists():
        return str(VENV_PYTHON)
    # 已經在某個 venv 裡（使用者自己 activate 過）就用當前直譯器。
    return sys.executable


def child_env() -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    # 中文語系 Windows 把輸出導向檔案時預設 cp950，印警告的那一行會拋
    # UnicodeEncodeError。統一 UTF-8，讓兩個平台的輸出位元組完全一致。
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def run_task(task: Task, extra: list, *, dry: bool = False) -> int:
    if task.name == "setup":
        return do_setup(dry=dry)

    argv = [interpreter(), *task.argv, *extra]
    if dry:
        print("  $ " + " ".join(argv))
        return 0
    if task.needs_raw and not (ROOT / "data/raw").exists():
        print("  ✗ " + task.name + " 需要 data/raw，但它不存在。"
              "先跑：python run.py setup-raw <放 zip 的目錄>")
        return 2
    t0 = time.monotonic()
    proc = subprocess.run(argv, cwd=str(ROOT), env=child_env())
    dt = time.monotonic() - t0
    mark = "✓" if proc.returncode == 0 else "✗"
    print(f"  {mark} {task.name}  ({dt:.1f}s)")
    return proc.returncode


def do_setup(*, dry: bool = False) -> int:
    """建立 venv 並裝好相依。

    有 uv 就用 uv——pip 的解析器在 requirements.txt 這批釘版上會長時間回溯
    （實測 13 分鐘沒裝上任何套件），uv 幾十秒完成且結果相同。
    """
    reqs = ROOT / "requirements.txt"
    web = ["fastapi>=0.110", "uvicorn[standard]>=0.29", "httpx>=0.27"]
    steps = []
    if not VENV_PYTHON.exists():
        steps.append([sys.executable, "-m", "venv", str(ROOT / ".venv")])
    uv = shutil.which("uv")
    if uv:
        steps.append([uv, "pip", "install", "--python", str(VENV_PYTHON),
                      "-r", str(reqs)])
        steps.append([uv, "pip", "install", "--python", str(VENV_PYTHON), *web])
    else:
        steps.append([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(reqs)])
        steps.append([str(VENV_PYTHON), "-m", "pip", "install", *web])
    for argv in steps:
        if dry:
            print("  $ " + " ".join(argv))
            continue
        print("  $ " + " ".join(argv[:3]) + " …")
        proc = subprocess.run(argv, cwd=str(ROOT))
        if proc.returncode != 0:
            return proc.returncode
    if not dry:
        print("  ✓ setup 完成。接著：python run.py frontend && python run.py serve")
    return 0


def show_list() -> None:
    print("小小守護員：Mac 與 Windows 共用的單一指令入口")
    print("用法：python run.py <task> [args…]")
    print()
    seen = []
    for t in TASKS:
        if t.group not in seen:
            seen.append(t.group)
    for group in seen:
        print("── " + group + " " + "─" * max(4, 52 - len(group) * 2))
        for t in TASKS:
            if t.group != group:
                continue
            star = "*" if t.in_pipeline else " "
            print(f" {star} {t.name:22s} {t.desc}{t.badges()}")
        print()
    print("  * = python run.py pipeline 會依序執行的步驟")


def parse_argv(argv: list) -> tuple:
    """拆出（任務, 給腳本的參數, dry_run, want_list）。

    不用 argparse 的 REMAINDER：它會把 `run.py pipeline --dry-run` 的旗標
    當成要傳給腳本的參數，於是 `--dry-run` 靜靜失效而管線真的跑起來——
    這個 footgun 已經咬過一次。

    規則：執行器自己的旗標在 `--` 之前的任何位置都成立；`--` 之後的一切
    原樣傳給腳本，所以子腳本自己的 `--list` 仍然傳得進去。
    """
    dry = want_list = False
    rest = []
    passthrough = False
    for token in argv:
        if passthrough:
            rest.append(token)
        elif token == "--":
            passthrough = True
        elif token == "--dry-run":
            dry = True
        elif token in ("--list", "-l"):
            want_list = True
        else:
            rest.append(token)
    task = rest[0] if rest else None
    return task, rest[1:], dry, want_list


def main() -> int:
    if any(t in ("-h", "--help") for t in sys.argv[1:]):
        argparse.ArgumentParser(
            description="小小守護員：Mac 與 Windows 共用的單一指令入口",
            usage="python run.py <task> [args…]  （--list 看全部任務）").print_help()
        return 0

    task_name, extra, dry_run, want_list = parse_argv(sys.argv[1:])

    if want_list or not task_name:
        show_list()
        return 0

    if task_name == "pipeline":
        steps = [t for t in TASKS if t.in_pipeline]
        if not (ROOT / "data/raw").exists():
            skipped = [t.name for t in steps if t.needs_raw]
            steps = [t for t in steps if not t.needs_raw]
            print("data/raw 不存在，略過需要它的 " + str(len(skipped)) + " 步："
                  + "、".join(skipped))
            print("（其餘步驟吃的是已進版控的 data/extracted/，照樣可重跑）\n")
        print("執行 " + str(len(steps)) + " 個步驟：\n")
        for t in steps:
            rc = run_task(t, [], dry=dry_run)
            if rc != 0:
                print("\n管線在 " + t.name + " 停止（exit=" + str(rc) + "）")
                return rc
        print("\n✓ 管線完成")
        return 0

    task = BY_NAME.get(task_name)
    if task is None:
        print("未知任務：" + task_name + "\n")
        show_list()
        return 2
    return run_task(task, extra, dry=dry_run)


if __name__ == "__main__":
    sys.exit(main())
