"""Narrative backends for the audit letter.

    NarrativeBackend
      ├─ TemplateBackend   deterministic; always available, never wrong
      └─ BedrockBackend    交付路徑（Bedrock 上帳號可用的最強模型，見 ../bedrock.py）

Same arrangement as ``extract/backends.py`` and for the same reason: the facts,
the prompt, and the verification are shared, so swapping the backend changes the
prose and nothing about what the letter is allowed to say. Both backends are
handed the identical :class:`AuditFacts` and both outputs go through
``verify.verify_letter`` before a caller may use them.

The template backend is not a placeholder to be deleted once Bedrock works. It is
the floor: if the model is unreachable, returns malformed output, or writes a
figure that is not in the facts, the letter still goes out with the same content
in plainer language. A system that can only produce its deliverable when an
external service cooperates is not a system an inspector can rely on.
"""

from __future__ import annotations

import abc

from .facts import AuditFacts
from .schema import LETTER_PROMPT, LETTER_SCHEMA, letter_to_text
from .verify import verify_letter


class NarrativeBackend(abc.ABC):
    """Turns established facts into official correspondence."""

    name = "base"

    @abc.abstractmethod
    def draft(self, facts: AuditFacts) -> str:
        """Return the letter body."""

    def write(self, facts: AuditFacts, known_titles: set[str] | None = None) -> dict:
        """Draft and verify. Never returns unverified prose."""
        text = self.draft(facts)
        result = verify_letter(text, facts, known_titles)
        return {"backend": self.name, "text": text, "verified": result.ok,
                "problems": result.problems}


def _num(value: object) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


class TemplateBackend(NarrativeBackend):
    """Deterministic assembly. The content floor for every letter."""

    name = "template"

    def draft(self, facts: AuditFacts) -> str:
        L: list[str] = [
            f"受文者：{facts.title}",
            "主旨：本府教育局就下列事項，請貴園提出說明。",
            "",
            "說明：",
            "一、列入本次查核建議之依據",
            f"　　貴園於本市教保機構查核優先序中列第 {facts.priority_rank} 名"
            f"（全市 {facts.priority_total} 園）。",
        ]
        if facts.review_reasons:
            L.append(f"　　列入理由：{'；'.join(facts.review_reasons)}。")
        if facts.prior_penalties:
            L.append(f"　　歷來裁罰紀錄 {facts.prior_penalties} 件。")
        if facts.events_365d:
            latest = (f"，最近一筆 {facts.latest_event_date}"
                      if facts.latest_event_date else "")
            L.append(f"　　近一年官方事件 {facts.events_365d} 件{latest}。")
        if facts.evaluation_result:
            L.append(f"　　最近一次評鑑（{facts.evaluation_date}）："
                     f"{facts.evaluation_result}。")

        if facts.findings:
            L += ["", "二、財務報告與其自述會計政策不符之處"]
            for i, f in enumerate(facts.findings[:4], start=1):
                L += [f"　（{'一二三四'[i - 1]}）{f.academic_year} 學年度・{f.rule}",
                      f"　　　{f.detail}",
                      f"　　　依據：{f.rule_text}"]
            if facts.reserve_verdicts:
                L.append("　　跨年度判讀：" + "；".join(facts.reserve_verdicts) + "。")
            L += ["", "三、請說明事項",
                  "　　請檢附專戶存款餘額證明書、預算流用核准文件及相關支出憑證，",
                  "　　於文到 15 日內函復本局。"]

        if facts.staffing:
            s = facts.staffing
            L += ["", f"{'四' if facts.findings else '二'}、人力配置對照"
                  f"（{s['academic_year']} 學年度）",
                  f"　　員工 {_num(s['total_staff'])} 人，其中教保服務人員"
                  f" {_num(s.get('educators'))} 人；每人人事費"
                  f" {_num(s.get('personnel_cost_per_head'))} 元。"]

        head = "五" if (facts.findings and facts.staffing) else (
            "四" if (facts.findings or facts.staffing) else "二")
        L += ["", f"{head}、本次查核之資料涵蓋範圍"]
        L += [f"　　{n}。" for n in facts.coverage]
        L += ["", "※ 本文係依公開資料生成之查核建議，非違法認定；"
              "上開事項之最終認定，仍以本局實地查核及貴園說明為準。"]
        return "\n".join(L)


class BedrockBackend(NarrativeBackend):
    """Amazon Bedrock. The competition requires an AWS-provided foundation model.

    Structured Outputs is used rather than free prose so the sections stay fixed
    and the verifier has something predictable to check; Bedrock has no Batches
    or Files API, so this is a plain per-letter call.
    """

    name = "bedrock"

    # 建議書寫錯會對真實機構造成傷害，所以這個落點用帳號裡**最強可用**的模型，
    # 不是最省的那個。ID 與 region 由 ..bedrock 統一提供。
    def __init__(self, model: str | None = None,
                 region: str | None = None, max_tokens: int = 2000) -> None:
        from .. import bedrock as _bedrock

        self.model = model or _bedrock.REASONING_MODEL
        self.region = region or _bedrock.region()
        self.max_tokens = max_tokens
        self._client = None

    def _get_client(self):
        if self._client is None:
            from .. import bedrock as _bedrock

            # 建議書是長文輸出，逾時給得寬；但仍要有上限，否則 143 份的批次
            # 可能被單一卡住的請求拖住十分鐘以上（SDK 預設 600 秒）。
            self._client = _bedrock.client(
                self.region, timeout=_bedrock.TIMEOUT_REASONING, max_retries=1)
        return self._client

    def draft(self, facts: AuditFacts) -> str:
        import json

        response = self._get_client().messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=LETTER_PROMPT,
            messages=[{"role": "user", "content": json.dumps(
                facts.as_dict(), ensure_ascii=False, indent=2)}],
            output_config={"format": LETTER_SCHEMA},
        )
        payload = json.loads(response.content[0].text)
        return letter_to_text(payload, facts)


def get_backend(kind: str = "template", **kwargs) -> NarrativeBackend:
    if kind == "template":
        return TemplateBackend()
    if kind == "bedrock":
        return BedrockBackend(**kwargs)
    raise ValueError(f"未知的 backend：{kind}（可用：template, bedrock）")
