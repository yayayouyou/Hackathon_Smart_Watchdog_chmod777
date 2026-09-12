"""The output contract for the generative narrative layer.

The model is given established facts and asked to arrange them into 公文 prose.
It is not asked to analyse, weigh, or conclude -- those decisions were made by
the compliance checks and the ranking model, both of which are testable. Keeping
the generative step to arrangement is what makes the verifier's job tractable:
if the model may only restate what it was given, then any figure that is not in
the facts is by definition an error, not a judgement call we have to adjudicate.

Structured Outputs is used so the sections arrive as named fields. Free prose
would force the verifier to guess where a section began, and would let the model
merge a finding with a caveat in a way that reads as a stronger claim than the
facts support.
"""

from __future__ import annotations

from typing import Any

LETTER_SCHEMA: dict[str, Any] = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {
            "subject": {
                "type": "string",
                "description": "主旨，一句話，需為請求說明而非指控",
            },
            "basis": {
                "type": "array", "items": {"type": "string"},
                "description": "列入查核建議之依據，每項一句，只能引用給定事實",
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {"type": "string",
                                    "description": "學年度與規則名稱"},
                        "observation": {"type": "string",
                                        "description": "數字與算式，逐字沿用給定內容"},
                        "basis_text": {"type": "string",
                                       "description": "該報告自身附註原文，不得改寫"},
                    },
                    "required": ["heading", "observation", "basis_text"],
                    "additionalProperties": False,
                },
            },
            "requests": {
                "type": "array", "items": {"type": "string"},
                "description": "請機構檢附或說明的事項",
            },
            "coverage": {
                "type": "array", "items": {"type": "string"},
                "description": "本次未能檢核的範圍，需明確說明未檢核不等於無虞",
            },
        },
        "required": ["subject", "basis", "findings", "requests", "coverage"],
        "additionalProperties": False,
    },
}

LETTER_PROMPT = """你是新北市政府教育局的承辦人員，依據系統提供的既有事實，
撰寫一份請教保機構說明的公文。

**你的角色是編排，不是分析。** 所有判斷已由檢核程式完成，你只負責把既有事實
組織成通順的公文語句。

硬性規則：

1. **只能使用輸入 JSON 中出現的數字。** 不得計算、推估、四捨五入或補充任何
   未出現的金額、比率或日期。若某項資訊不在輸入中，就不要提及。
2. **附註原文（rule_text）必須逐字沿用**，不得改寫、節錄或潤飾。那是機構自己
   申報的會計政策，改動一個字就失去引用價值。
3. **不得使用「違法」「不法」「違規」「舞弊」等字眼。** 本文是請求說明，不是
   違法認定。用「與其自述會計政策不符」「請說明」「請檢附」。
4. **coverage 必須包含未檢核範圍**，並明確寫出「未檢核不等於無虞」的意思。
   收到公文的人若把沉默讀成清白，就是我們的失誤。
5. 只能提及受文的這一所機構，不得出現其他園名。
6. 語氣為行政公文：客觀、具體、可查證。不要加入評價性形容詞。

輸入為 JSON 格式的既有事實，請依 schema 輸出。"""


def letter_to_text(payload: dict[str, Any], facts) -> str:
    """Render the structured sections into the 公文 layout.

    Layout lives here rather than in the model so every letter has the same
    shape regardless of backend, and so the closing disclaimer is appended by
    code -- a required sentence must not depend on the model remembering it.
    """
    L = [f"受文者：{facts.title}",
         f"主旨：{payload.get('subject', '請貴園就下列事項提出說明。')}",
         "", "說明：", "一、列入本次查核建議之依據"]
    L += [f"　　{b}" for b in payload.get("basis", [])]

    findings = payload.get("findings", [])
    if findings:
        L += ["", "二、財務報告與其自述會計政策不符之處"]
        for i, f in enumerate(findings[:4]):
            L += [f"　（{'一二三四'[i]}）{f.get('heading', '')}",
                  f"　　　{f.get('observation', '')}",
                  f"　　　依據：{f.get('basis_text', '')}"]

    requests = payload.get("requests", [])
    if requests:
        L += ["", f"{'三' if findings else '二'}、請說明事項"]
        L += [f"　　{r}" for r in requests]

    head = "四" if (findings and requests) else ("三" if (findings or requests) else "二")
    L += ["", f"{head}、本次查核之資料涵蓋範圍"]
    L += [f"　　{c}" for c in payload.get("coverage", [])]
    L += ["", "※ 本文係依公開資料生成之查核建議，非違法認定；"
          "上開事項之最終認定，仍以本局實地查核及貴園說明為準。"]
    return "\n".join(L)
