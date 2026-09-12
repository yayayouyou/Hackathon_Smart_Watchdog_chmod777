"""Pluggable model backends for statement extraction.

The competition requires the delivered pipeline to run on Amazon Bedrock. During
development the AWS environment is not yet provisioned, so extraction is done by
Claude Code subagents instead. This module exists so that difference is confined
to one class: the schema, the prompt, the validation, and the scoring are shared,
and switching backends cannot change what "correct" means.

    ExtractionBackend           the contract
    ├─ BedrockBackend           the deliverable (anthropic.claude-sonnet-5)
    └─ RecordedBackend          replays dev-phase subagent output from disk

**Bedrock constraints that shape this interface** (see
``docs/architecture/aws-architecture.md`` §1):

* No Files API -- images must be base64-inlined on every request, so the caller
  passes bytes and page selection is the cost control.
* No Batches API -- there is no 50%-off batch path, so concurrency and retry are
  the caller's job rather than the platform's.
* Structured Outputs *is* supported, so the schema can be enforced server-side
  rather than parsed defensively.
"""

from __future__ import annotations

import abc
import base64
import dataclasses
import json
import pathlib
from typing import Any

# 模型 ID 與連線方式統一由 ..bedrock 提供——三個 AI 落點必須講同一組 ID，
# 否則「在我機器上可以」會變成四個各自為政的設定。實測理由見該模組說明。
from ..bedrock import DEFAULT_MODEL as BEDROCK_EXTRACTION_MODEL
from ..bedrock import REASONING_MODEL as BEDROCK_REASONING_MODEL  # noqa: F401
from .schema import EXTRACTION_PROMPT, STATEMENT_SCHEMA, ValidationResult, validate_statement


@dataclasses.dataclass
class ExtractionResult:
    """One extracted statement plus everything needed to judge it."""

    key: str
    payload: dict[str, Any]
    backend: str
    validation: ValidationResult
    raw_response: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.validation.ok


class ExtractionBackend(abc.ABC):
    """Turns (page image, prompt, schema) into a validated statement dict."""

    name: str = "abstract"

    @abc.abstractmethod
    def extract(self, key: str, image_png: bytes) -> ExtractionResult:
        ...

    def _finish(
        self, key: str, payload: dict, raw: str | None = None, error: str | None = None
    ) -> ExtractionResult:
        return ExtractionResult(
            key=key,
            payload=payload,
            backend=self.name,
            validation=validate_statement(payload) if payload else ValidationResult(),
            raw_response=raw,
            error=error,
        )


class BedrockBackend(ExtractionBackend):
    """Amazon Bedrock，經 `AnthropicBedrock`（InvokeModel 路徑）——交付路徑。

    **不是 Mantle。** 原本寫的是 `AnthropicBedrockMantle`，但在競賽帳號實測
    回 404；`AnthropicBedrock` 可用，且同樣提供原生 Messages API 形狀，
    視覺輸入與 `output_config` 結構化輸出皆已實測通過。見 ``..bedrock``。
    """

    name = "bedrock"

    def __init__(
        self,
        region: str | None = None,
        model: str = BEDROCK_EXTRACTION_MODEL,
        max_tokens: int = 16000,
    ) -> None:
        from .. import bedrock as _bedrock

        self.region = region or _bedrock.region()
        self.model = model
        self.max_tokens = max_tokens
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            # 延遲建立，讓開發路徑不需要 boto3 也不需要憑證。
            from .. import bedrock as _bedrock

            self._client = _bedrock.client(self.region)
        return self._client

    def extract(self, key: str, image_png: bytes) -> ExtractionResult:
        client = self._get_client()
        b64 = base64.standard_b64encode(image_png).decode()
        try:
            response = client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # Structured Outputs is available on Bedrock, so the schema is
                # enforced rather than hoped for -- no parse-and-retry loop.
                output_config={
                    "format": {"type": "json_schema", "schema": STATEMENT_SCHEMA}
                },
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": EXTRACTION_PROMPT},
                        ],
                    }
                ],
            )
        except Exception as exc:  # noqa: BLE001 - 任何傳輸失敗都變成一筆結果而不是中斷
            from .. import bedrock as _bedrock

            return self._finish(key, {}, error=_bedrock.explain_error(exc))

        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return self._finish(key, {}, raw=text, error=f"JSON 解析失敗: {exc}")
        return self._finish(key, payload, raw=text)


class RecordedBackend(ExtractionBackend):
    """Replays extraction JSON already written to disk.

    Used two ways: to score the dev-phase subagent output with exactly the same
    code path the Bedrock output will go through, and later as a fixture backend
    so the downstream forensic pipeline can be tested without spending tokens.
    """

    name = "recorded"

    def __init__(self, directory: pathlib.Path | str) -> None:
        self.directory = pathlib.Path(directory)

    def extract(self, key: str, image_png: bytes) -> ExtractionResult:  # noqa: ARG002
        path = self.directory / f"{key}.json"
        if not path.exists():
            return self._finish(key, {}, error=f"找不到已記錄的抽取結果：{path}")
        text = path.read_text(encoding="utf-8")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            return self._finish(key, {}, raw=text, error=f"JSON 解析失敗: {exc}")
        return self._finish(key, payload, raw=text)


def get_backend(kind: str, **kwargs: Any) -> ExtractionBackend:
    """Resolve a backend by name so callers never hardcode one."""
    if kind == "bedrock":
        return BedrockBackend(**kwargs)
    if kind == "recorded":
        return RecordedBackend(**kwargs)
    raise ValueError(f"未知的 backend：{kind}（可用：bedrock, recorded）")
