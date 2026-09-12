"""Bedrock 連線與模型 ID：唯一寫著「要呼叫哪個模型、怎麼連」的地方。

競賽規定只能使用 Amazon Bedrock／SageMaker 提供的基礎模型，所以這是三個
AI 落點（財報視覺抽取、稽核建議書生成、自然語言查詢）共用的入口。

## 三件在競賽帳號實測出來、與文件預期不同的事

**1. 模型 ID 必須加 `us.` 前綴。** 所有 Anthropic 模型在這個帳號都只支援
`INFERENCE_PROFILE`（`list_foundation_models` 的 `inferenceTypesSupported`
只有這一項），裸 ID 會得到：

    Invocation of model ID anthropic.claude-sonnet-4-6 with on-demand
    throughput isn't supported.

**2. 不能用 `AnthropicBedrockMantle`。** Mantle（Messages API 的 Bedrock
端點）在這個帳號回 404。可用的是 `AnthropicBedrock`（InvokeModel 路徑），
它同樣提供原生 Messages API 形狀——視覺輸入、`output_config` 結構化輸出、
強制工具呼叫三者實測皆可用。

**3. Claude 5 世代在競賽帳號是 AccessDenied。** 27 個 Anthropic 推論設定檔
逐一實測，13 個可用；`claude-sonnet-5`、`claude-opus-5`、`claude-fable-5`、
`claude-opus-4-7`、`claude-opus-4-8` 全部 AccessDenied。所以下面選的是
**實測打得通**的最強者，而不是文件上最新的那一個。

實測時點 2026-09-12，帳號 550561128629，region us-west-2。
`python run.py bedrock-check` 可以隨時重跑這份驗證——**換帳號或換天就要重跑**，
因為可用模型是帳號層級的權限，不是程式決定的。

## 憑證

主辦方發的是**臨時 STS 憑證**（`ASIA...` 開頭，含 `AWS_SESSION_TOKEN`），
會過期，且整個 workshop 環境只在 9/12 08:00 – 9/13 13:00 開放。
過期的表徵是 `ExpiredTokenException`；`explain_error()` 會把它翻成人話。

憑證只放 `.env`（已 gitignore）。**絕不可進版控**——競賽規範明文要求。
"""

from __future__ import annotations

import os
import pathlib
from typing import Any

#: 預設區域。主辦方給的環境是 us-west-2。
DEFAULT_REGION = "us-west-2"

# ── 模型 ID ──────────────────────────────────────────────────────────
# 全部帶 `us.` 前綴，理由見模組說明第 1 點。三個角色分開命名，是因為它們的
# 取捨不同：抽取要看圖且量大（Sonnet），推理要最強（Opus），查詢要快（Haiku）。

#: 主力：財報視覺抽取。量大、要讀掃描影像、要結構化輸出。
DEFAULT_MODEL = "us.anthropic.claude-sonnet-4-6"

#: 最強可用：稽核建議書生成這類「寫錯會對真實機構造成傷害」的落點。
REASONING_MODEL = "us.anthropic.claude-opus-4-6-v1"

#: 最快最省：自然語言查詢的意圖解析，延遲比深度重要。
FAST_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

#: `run.py bedrock-check` 會逐一實測這些。順序即偏好順序。
KNOWN_GOOD = (
    REASONING_MODEL,
    DEFAULT_MODEL,
    FAST_MODEL,
    "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "us.anthropic.claude-opus-4-1-20250805-v1:0",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-sonnet-4-20250514-v1:0",
)

#: 在競賽帳號實測為 AccessDenied 的。列出來是為了讓下一個人不用再撞一次。
KNOWN_DENIED = (
    "us.anthropic.claude-opus-5",
    "us.anthropic.claude-sonnet-5",
    "us.anthropic.claude-fable-5",
    "us.anthropic.claude-fable-5-1",
    "us.anthropic.claude-opus-4-7",
    "us.anthropic.claude-opus-4-8",
)

ENV_PATH = pathlib.Path(__file__).resolve().parents[2] / ".env"


def load_env(path: pathlib.Path | None = None) -> list:
    """把 `.env` 灌進 `os.environ`，回傳這次補上的 key。

    boto3 只讀環境變數與 `~/.aws`，不會自己看 `.env`。已經設好的環境變數
    優先——CI 或 workshop shell 裡 `$Env:AWS_...` 設過的值不該被檔案蓋掉。
    """
    path = path or ENV_PATH
    added = []
    if not path.exists():
        return added
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value and key not in os.environ:
            os.environ[key] = value
            added.append(key)
    return added


def region() -> str:
    return (os.environ.get("AWS_REGION")
            or os.environ.get("AWS_DEFAULT_REGION")
            or DEFAULT_REGION)


def credentials_present() -> bool:
    return bool(os.environ.get("AWS_ACCESS_KEY_ID")
                and os.environ.get("AWS_SECRET_ACCESS_KEY"))


def client(region_name: str | None = None) -> Any:
    """建立 Bedrock 的 Anthropic client。

    用 `AnthropicBedrock` 而非 `AnthropicBedrockMantle`，理由見模組說明第 2 點。
    延遲匯入，讓沒有裝 `anthropic` 的環境仍能匯入本模組讀模型 ID。
    """
    load_env()
    from anthropic import AnthropicBedrock

    return AnthropicBedrock(aws_region=region_name or region())


def explain_error(exc: BaseException) -> str:
    """把 Bedrock 的例外翻成「現在該做什麼」。

    這三種在競賽當天都會遇到，而原始訊息都不會告訴你下一步。
    """
    text = str(exc)
    name = type(exc).__name__
    if "ExpiredToken" in text or "security token included in the request is expired" in text:
        return ("AWS 臨時憑證已過期。到 workshop 頁面重新複製 $Env:AWS_... 四行，"
                "更新專案根目錄的 .env，再重跑。"
                "（workshop 環境僅 9/12 08:00–9/13 13:00 開放）")
    if "AccessDenied" in name or "AccessDenied" in text:
        return (f"這個模型在本帳號沒有開通。改用 {DEFAULT_MODEL}，"
                "或先跑 `python run.py bedrock-check` 看哪些打得通。")
    if "on-demand throughput isn't supported" in text:
        return ("模型 ID 少了 `us.` 前綴。本帳號的 Anthropic 模型只支援推論設定檔，"
                f"例如 {DEFAULT_MODEL}。")
    if "could not be found" in text or name == "NotFoundError":
        return ("端點或模型不存在。若用的是 AnthropicBedrockMantle，"
                "改用 AnthropicBedrock——Mantle 端點在本帳號回 404。")
    if "Unable to locate credentials" in text or "NoCredentials" in name:
        return ("找不到 AWS 憑證。把主辦方給的四個 $Env:AWS_... 值寫進專案根目錄的 "
                ".env（該檔已 gitignore），或在 shell 裡 export。")
    return f"{name}: {text[:300]}"
