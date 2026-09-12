"""憑證與設定：從 `.env` 讀取，缺了就明白說缺了什麼、去哪裡申請。

沒有任何憑證時整套系統仍可運作——分析管線、靜態版、動態版、新聞與 PTT 兩個
即時管道都不需要金鑰。憑證只會**開啟更多管道**，不會決定系統能不能跑。

這一點刻意做成不可誤解：`missing()` 回報的每一項都附上「這會開啟什麼」與
「去哪裡申請」，`/api/health` 也會照實列出，這樣看畫面的人知道缺的是授權
而不是資料。
"""

from __future__ import annotations

import dataclasses
import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV_PATH = ROOT / ".env"


@dataclasses.dataclass(frozen=True)
class Credential:
    key: str
    label: str
    enables: str
    how: str
    required: bool = False


CREDENTIALS = (
    Credential(
        key="GOOGLE_MAPS_API_KEY",
        label="Google Maps Platform 金鑰",
        enables="卷宗內的 Google 地圖評論（即時顯示）；另可切換 Google 底圖",
        how="console.cloud.google.com → 建立專案 → 啟用 Places API (New) "
            "→ 憑證 → 建立 API 金鑰 → 限制為 Places API",
    ),
    Credential(
        key="THREADS_ACCESS_TOKEN",
        label="Threads 使用者存取權杖",
        # 同一把 token 開兩個管道，但門檻不同：@標註只要帳號授權就能讀，
        # 關鍵字搜尋要過 App Review。先拿到 token 就先有一個管道可用。
        enables="Threads @標註官方帳號的通報（有 token 即可用）；"
                "另加關鍵字搜尋（須另過 App Review）",
        how="developers.facebook.com 建立應用程式 → 加入 Threads API "
            "→ threads_basic 取得權杖即可讀 @標註；"
            "關鍵字搜尋另需 threads_keyword_search 權限並過 App Review",
    ),
    Credential(
        key="APIFY_TOKEN",
        label="Apify API token",
        enables="Threads 貼文（第三方 actor，當天可用）",
        how="apify.com 註冊 → Settings → Integrations → API token",
    ),
    Credential(
        key="VENDOR_FEED_PATH",
        label="輿情監測服務資料檔路徑",
        enables="PTT／Dcard／FB 公開社團／Threads 的全網覆蓋",
        how="循共同供應契約採購 OpView／QSearch／KEYPO，要求供應每日檔案或 API",
    ),
    Credential(
        key="AWS_REGION",
        label="AWS 區域（Bedrock）",
        enables="財報視覺抽取、稽查建議書生成、自然語言查詢改用 Bedrock",
        how="決賽當天由主辦方提供；另需 AWS_ACCESS_KEY_ID 與 AWS_SECRET_ACCESS_KEY",
    ),
)


def load_env(path: pathlib.Path = ENV_PATH) -> dict[str, str]:
    """讀 .env 並填入 os.environ（既有環境變數優先，不覆蓋）。

    自行解析而不依賴 python-dotenv：它是 uvicorn 的相依，不是本專案的，
    分析管線在沒裝 web extra 的機器上也要能讀設定。
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        values[key] = value
        os.environ.setdefault(key, value)
    return values


def get(key: str, default: str | None = None) -> str | None:
    load_env()
    value = os.environ.get(key, default)
    return value or None


def credentials() -> dict[str, str | None]:
    """即時管道建構子要的參數名稱，對應到目前實際拿得到的值。"""
    return {
        "places_api_key": get("GOOGLE_MAPS_API_KEY"),
        "threads_token": get("THREADS_ACCESS_TOKEN"),
        "apify_token": get("APIFY_TOKEN"),
        "vendor_feed_path": get("VENDOR_FEED_PATH"),
    }


def status() -> list[dict[str, object]]:
    """每一項憑證的現況，含缺少時該去哪裡申請。"""
    load_env()
    return [
        {
            "key": c.key, "label": c.label, "enables": c.enables,
            "how_to_get": c.how, "present": bool(os.environ.get(c.key)),
        }
        for c in CREDENTIALS
    ]


def aws_identity(timeout_s: float = 4.0) -> dict[str, object]:
    """AWS 憑證**現在能不能用**——不是「有沒有設」。

    `status()` 只看環境變數在不在，而黑客松發的是臨時憑證：過期之後變數還在，
    每一次 Bedrock 呼叫卻都失敗。於是健康檢查顯示一切正常，台上一問話就爆。
    實際發生過（2026-09-12）。所以這裡真的打一次 STS。

    「打不通」與「憑證壞了」要分開講：會場斷網時回 `None`（無法確認），
    不要謊稱憑證有問題——那會讓人去改一個沒有壞的東西。
    """
    load_env()
    if not os.environ.get("AWS_ACCESS_KEY_ID"):
        return {"usable": False, "detail": "沒有設定 AWS_ACCESS_KEY_ID"}
    try:
        import boto3
        from botocore.config import Config

        who = boto3.client("sts", config=Config(
            connect_timeout=timeout_s, read_timeout=timeout_s,
            retries={"total_max_attempts": 1},
        )).get_caller_identity()
    except Exception as exc:  # noqa: BLE001 - 任何失敗都要回報，不能讓 health 掛掉
        text = f"{type(exc).__name__} {exc}"
        if "ExpiredToken" in text:
            return {"usable": False, "detail": "憑證已過期，請更新 .env 後重啟"}
        if "InvalidClientTokenId" in text or "UnrecognizedClient" in text:
            return {"usable": False, "detail": "憑證無效（可能已撤銷或貼漏一段）"}
        return {"usable": None, "detail": f"無法確認：{type(exc).__name__}"}
    return {"usable": True, "account": who.get("Account"), "detail": "可用"}


def missing() -> list[dict[str, object]]:
    return [c for c in status() if not c["present"]]
