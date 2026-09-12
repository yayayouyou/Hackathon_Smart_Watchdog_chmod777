"""實測 Bedrock：憑證通不通、哪些模型打得通、三個 AI 落點會不會動。

    python run.py bedrock-check            憑證 + 模型可用性
    python run.py bedrock-check -- --full  再加上三個落點的端到端煙霧測試

**換帳號或換一天就要重跑。** 可用模型是帳號層級的權限，不是程式決定的；
主辦方發的又是會過期的臨時憑證。決賽當天第一件事就是跑這個。
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import io
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8

use_utf8()

from smart_watchdog import bedrock


def _probe(model: str) -> tuple:
    t0 = time.perf_counter()
    try:
        c = bedrock.client()
        c.messages.create(model=model, max_tokens=4,
                          messages=[{"role": "user", "content": "hi"}])
        return model, True, f"{(time.perf_counter() - t0) * 1000:.0f}ms", ""
    except Exception as exc:  # noqa: BLE001 - 這支腳本的工作就是回報任何失敗
        return model, False, "", bedrock.explain_error(exc)


def check_identity() -> bool:
    print("── 憑證")
    added = bedrock.load_env()
    if added:
        print(f"  從 .env 補上 {len(added)} 個環境變數")
    if not bedrock.credentials_present():
        print("  ✗ 找不到 AWS_ACCESS_KEY_ID／AWS_SECRET_ACCESS_KEY。")
        print("    把主辦方給的四個 $Env:AWS_... 值寫進專案根目錄的 .env（已 gitignore）。")
        return False
    try:
        import boto3

        who = boto3.client("sts", region_name=bedrock.region()).get_caller_identity()
        print(f"  ✓ 帳號 {who['Account']}　region {bedrock.region()}")
        print(f"    {who['Arn']}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ {bedrock.explain_error(exc)}")
        return False


def check_models(all_profiles: bool = False) -> list:
    print("\n── 模型可用性（逐一實際呼叫，不是查清單）")
    targets = list(bedrock.KNOWN_GOOD)
    if all_profiles:
        try:
            import boto3

            br = boto3.client("bedrock", region_name=bedrock.region())
            targets = sorted(
                p["inferenceProfileId"]
                for p in br.list_inference_profiles(maxResults=100)[
                    "inferenceProfileSummaries"]
                if "anthropic" in p["inferenceProfileId"])
        except Exception as exc:  # noqa: BLE001
            print(f"  （列出推論設定檔失敗，改用內建清單：{type(exc).__name__}）")
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        rows = list(ex.map(_probe, targets))
    good = []
    for model, ok, ms, err in rows:
        if ok:
            good.append(model)
            print(f"  ✓ {model:48s} {ms:>7s}")
        else:
            print(f"  ✗ {model:48s} {err[:70]}")
    print(f"\n  可用 {len(good)}/{len(rows)}")
    for role, mid in (("抽取", bedrock.DEFAULT_MODEL),
                      ("建議書", bedrock.REASONING_MODEL),
                      ("查詢", bedrock.FAST_MODEL)):
        mark = "✓" if mid in good else "✗ ← 專案預設打不通，改 src/smart_watchdog/bedrock.py"
        print(f"  {role:4s} {mid:48s} {mark}")
    return good


def _sample_page_png() -> bytes | None:
    """渲染一張真實的財報頁當視覺測試輸入。沒有 data/raw 就跳過。"""
    pdf = pathlib.Path("data/raw/資料集/非營利園財報/113學年度/N01安溪_113學年度財務報告.pdf")
    if not pdf.exists():
        return None
    try:
        import fitz
    except ImportError:
        return None
    doc = fitz.open(pdf)
    try:
        return doc[4].get_pixmap(dpi=110).tobytes("png")   # 印刷頁碼 p.5 的資產負債表
    finally:
        doc.close()


def check_landings() -> None:
    """三個 AI 落點的端到端煙霧測試。"""
    print("\n── 三個 AI 落點")
    c = bedrock.client()

    # 1. 視覺抽取：讀真實的掃描財報頁，並要求結構化輸出。
    png = _sample_page_png()
    if png is None:
        print("  ⬜ 視覺抽取：缺 data/raw，跳過（抽取結果已進版控，展示不受影響）")
    else:
        schema = {"type": "object",
                  "properties": {"statement": {"type": "string"},
                                 "cash": {"type": ["number", "null"]}},
                  "required": ["statement", "cash"], "additionalProperties": False}
        try:
            b64 = base64.standard_b64encode(png).decode()
            ask = "抽出報表名稱與現金及銀行存款。空白格填 null，絕不填 0。"
            r = c.messages.create(
                model=bedrock.DEFAULT_MODEL, max_tokens=300,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": [
                    {"type": "image",
                     "source": {"type": "base64", "media_type": "image/png",
                                "data": b64}},
                    {"type": "text", "text": ask}]}])
            got = json.loads(r.content[0].text)
            # 8,868,744 是這一頁上的實際數字，也是 ground truth 記的值。
            ok = got.get("cash") == 8868744
            print(f"  {'✓' if ok else '⚠'} 視覺抽取：{got}"
                  f"{'' if ok else '　← 與 ground truth 8868744 不符，需人工看一下'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ 視覺抽取：{bedrock.explain_error(exc)}")

    # 2. 自然語言查詢：問句 → 檢索條件。
    try:
        from smart_watchdog.api.chat import get_planner

        plan = get_planner("bedrock").plan("板橋區有裁罰紀錄的私立幼兒園")
        # QueryPlan 本身就是 dict（見 api/chat.py）——它是檢索條件，不是結論。
        print(f"  ✓ 自然語言查詢：{json.dumps(plan, ensure_ascii=False)[:110]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 自然語言查詢：{bedrock.explain_error(exc)}")

    # 3. 稽核建議書：直接跑既有腳本產一份。事實層要四張表才拼得出來，
    #    在這裡重拼一份等於複製一套邏輯——而測試用的事實一旦與正式管線分岔，
    #    測過了也不代表正式路徑會過。
    import subprocess

    try:
        # `--out` 不可省：`--limit 1` 搭配預設目錄會把 141 列的 index.csv
        # 重寫成 1 列。這條指令是決賽當天每天早上要跑的，冒煙測試毀掉正式
        # 產出等於每天自己砍自己一刀。build_audit_letters.py 現在也會擋，
        # 但這裡明確指定，讓「寫到哪裡」在呼叫端就看得見。
        proc = subprocess.run(
            [sys.executable, str(pathlib.Path(__file__).parent / "build_audit_letters.py"),
             "--backend", "bedrock", "--limit", "1",
             "--out", "data/interim/letters_smoke"],
            capture_output=True, text=True, encoding="utf-8", timeout=300)
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()
        if proc.returncode == 0:
            print(f"  ✓ 稽核建議書：{tail[-1][:100] if tail else '完成'}")
        else:
            print(f"  ✗ 稽核建議書：{(tail[-1] if tail else '')[:160]}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 稽核建議書：{bedrock.explain_error(exc)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--full", action="store_true",
                    help="加跑三個 AI 落點的端到端測試（會花少量 token）")
    ap.add_argument("--all-profiles", action="store_true",
                    help="實測帳號裡全部 Anthropic 推論設定檔，不只內建清單")
    a = ap.parse_args()

    buf = io.StringIO()
    if not check_identity():
        sys.exit(1)
    good = check_models(all_profiles=a.all_profiles)
    if not good:
        print("\n沒有任何模型打得通——先確認憑證是否過期。")
        sys.exit(1)
    if a.full:
        check_landings()
    print(f"\n{buf.getvalue()}完成。模型 ID 統一定義在 src/smart_watchdog/bedrock.py。")


if __name__ == "__main__":
    main()
