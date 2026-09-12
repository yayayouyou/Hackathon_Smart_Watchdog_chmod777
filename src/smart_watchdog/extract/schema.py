"""Backend-agnostic contract for extracting a statement from a scanned page.

The competition requires the delivered pipeline to run on Amazon Bedrock. This
module therefore defines the extraction **contract** -- the JSON schema, the
prompt, and the validation -- with no reference to any particular model or SDK,
so the dev-phase backend and the Bedrock backend produce and are judged against
exactly the same thing. Swapping backends must never change what "correct" means.

One schema covers both statement shapes because they differ only in their column
headers:

    資產負債表   columns = ["114/7/31", "113/7/31"]        (or one column, new 園)
    收支餘絀表   columns = ["預算數", "決算數", "差異數", "執行率%"]

``values`` is therefore positionally aligned to ``columns``, with ``null`` for a
blank cell. A blank cell is not zero: 業務發展費 with no budget figure is
"unbudgeted", which is a forensic finding, while zero would be a false statement.
"""

from __future__ import annotations

import dataclasses

STATEMENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "statement_type": {
            "type": "string",
            "description": "報表名稱，逐字照抄表頭，例如 資產負債表、收支餘絀表",
        },
        "institution": {
            "type": ["string", "null"],
            "description": "機構全名。若紅色關防遮住無法辨識則填 null，不要猜測",
        },
        "operator": {
            "type": ["string", "null"],
            "description": "受託法人，取自括號內「委託…辦理」；無則 null",
        },
        "period_labels": {
            "type": "array",
            "items": {"type": "string"},
            "description": "各金額欄的期間或欄位標題，逐字照抄且維持左至右順序",
        },
        "unit": {"type": ["string", "null"], "description": "金額單位，例如 新臺幣元"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "項目名稱，逐字照抄"},
                    "note_ref": {
                        "type": ["string", "null"],
                        "description": "附註欄內容，例如「二、三」；無則 null",
                    },
                    "values": {
                        "type": "array",
                        "items": {"type": ["number", "null"]},
                        "description": (
                            "金額，順序對齊 period_labels。空白格填 null 而非 0。"
                            "括號代表負數，須轉為負值。破折號或 - 視為空白填 null"
                        ),
                    },
                    "percents": {
                        "type": ["array", "null"],
                        "items": {"type": ["number", "null"]},
                        "description": "百分比欄（若表上有），順序對齊 period_labels",
                    },
                },
                "required": ["label", "values"],
                "additionalProperties": False,
            },
        },
        "issues": {
            "type": "array",
            "items": {"type": "string"},
            "description": "辨識困難之處，例如關防遮住標題、數字模糊、欄位對齊不確定",
        },
    },
    "required": ["statement_type", "period_labels", "items", "issues"],
    "additionalProperties": False,
}


EXTRACTION_PROMPT = """你正在把一張掃描的幼兒園財務報表轉成結構化資料。這份資料會用於
政府監理稽查排序，數字錯誤會導致對合法機構的錯誤指控，因此**準確性遠重於完整性**。

規則：

1. **逐字照抄，不要正規化。** 項目名稱、期間標題、單位都照表面文字抄，
   包含全形空白與標點。不要把「合　　計」改成「合計」。

2. **空白格填 null，不要填 0。** 這兩者意義完全不同：
   「業務發展費」預算欄空白代表「未編列預算」，是稽查發現；填 0 是錯誤陳述。
   破折號（—、-）也視為空白，填 null。

3. **括號是負數。** `(1,988,771)` 要輸出 `-1988771`。

4. **欄位對齊靠視覺位置，不是靠順序。** 某些列會跳過前面幾欄
   （例如只有上年度有數字），此時前面的欄位必須填 null 讓後面的數字落在正確位置。
   這是最容易出錯的地方，請逐欄對照表頭的垂直位置確認。

5. **看不清楚就說看不清楚。** 若紅色關防遮住文字、數字模糊、或欄位歸屬不確定，
   把該格填 null 並在 `issues` 說明。**絕對不要猜測數字。**
   寧可回報缺漏讓人工補，也不要產生看似合理的錯誤數字。

6. **包含所有小計與合計列**（流動資產合計、資產總計、負債及餘絀總計等）。
   這些是驗算用的，缺了就無法自動檢核。

7. **章節標題列也要收錄**（流動資產、非流動資產、流動負債、餘絀、收入、支出）。
   這些列本身沒有金額，`values` 全填 null。保留它們才能還原表格層級。

8. **`label` 只放項目名稱本身，不要保留階層縮排的前導空白。**
   報表用縮排表示層級（明細縮一字、小計縮兩字），但那是版面而非名稱。
   項目名稱內部的全形空白要保留（如「合　　計」），行首的縮排空白要去掉。

9. **貨幣符號 `$` 不計入數值。** 部分列的金額前印有 `$`，那是幣別標記。

只輸出符合 schema 的 JSON，不要加任何說明文字。"""


@dataclasses.dataclass
class ValidationResult:
    """Outcome of checking one extracted statement against its own arithmetic."""

    passed: list[str] = dataclasses.field(default_factory=list)
    failed: list[str] = dataclasses.field(default_factory=list)
    skipped: list[str] = dataclasses.field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def score(self) -> float | None:
        total = len(self.passed) + len(self.failed)
        return None if total == 0 else len(self.passed) / total


def _lookup(items: list[dict], *needles: str) -> list | None:
    for it in items:
        flat = "".join(str(it.get("label", "")).split())
        if all(n in flat for n in needles):
            return it.get("values") or []
    return None


def _cell(items: list[dict], label_parts: tuple[str, ...], i: int) -> float | None:
    values = _lookup(items, *label_parts)
    if values is None or i >= len(values):
        return None
    v = values[i]
    return None if v is None else float(v)


def _sum(items: list[dict], labels: list[tuple[str, ...]], i: int) -> float:
    """Sum present components, treating an absent line as absent rather than zero."""
    return sum(_cell(items, lp, i) or 0.0 for lp in labels)


BALANCE_IDENTITIES: list[tuple[str, tuple[str, ...], list[tuple[str, ...]]]] = [
    ("資產總計 = 流動資產合計 + 非流動資產合計",
     ("資產總計",), [("流動資產合計",), ("非流動資產合計",)]),
    ("負債總額 = 流動負債合計 + 非流動負債合計",
     ("負債總額",), [("流動負債合計",), ("非流動負債合計",)]),
    ("餘絀總額 = 累積餘絀 + 本期餘絀",
     ("餘絀總額",), [("累積餘絀",), ("本期餘絀",)]),
    ("負債及餘絀總計 = 負債總額 + 餘絀總額",
     ("負債及餘絀總計",), [("負債總額",), ("餘絀總額",)]),
]


def validate_statement(payload: dict, tol: float = 1.0) -> ValidationResult:
    """Check an extracted statement against the identities it must satisfy.

    This is what makes a model-produced number trustworthy without a human
    reading the page: a mis-read digit almost always breaks one of these sums.
    Any identity whose operands are missing is *skipped*, never counted as passed,
    so a sparse extraction cannot score well by omission.
    """
    res = ValidationResult()
    items = payload.get("items") or []
    n_cols = len(payload.get("period_labels") or [])
    stype = "".join(str(payload.get("statement_type", "")).split())

    if "資產負債" in stype:
        for name, target, parts in BALANCE_IDENTITIES:
            for i in range(n_cols):
                lhs = _cell(items, target, i)
                operands = [_cell(items, p, i) for p in parts]
                label = f"[欄{i}] {name}"
                if lhs is None or all(o is None for o in operands):
                    res.skipped.append(label)
                    continue
                rhs = sum(o or 0.0 for o in operands)
                (res.passed if abs(lhs - rhs) <= tol else res.failed).append(label)
        # Assets must equal liabilities + equity -- the defining identity.
        for i in range(n_cols):
            a = _cell(items, ("資產總計",), i)
            b = _cell(items, ("負債及餘絀總計",), i)
            label = f"[欄{i}] 資產總計 = 負債及餘絀總計"
            if a is None or b is None:
                res.skipped.append(label)
            else:
                (res.passed if abs(a - b) <= tol else res.failed).append(label)

    elif "收支餘絀" in stype or "收支" in stype:
        # Column order is 預算數, 決算數, 差異數, 執行率%.
        for i, col_name in enumerate(payload.get("period_labels") or []):
            flat = "".join(str(col_name).split())
            if "率" in flat:
                continue  # a ratio column, not an amount
            for total, comment in [(("收入合計",), "收入"), (("支出合計",), "支出")]:
                lhs = _cell(items, total, i)
                label = f"[欄{i}:{flat}] {comment}合計 = 各科目加總"
                if lhs is None:
                    res.skipped.append(label)
                    continue
                components = _income_components(items, comment)
                if not components:
                    res.skipped.append(label)
                    continue
                rhs = _sum(items, components, i)
                (res.passed if abs(lhs - rhs) <= tol else res.failed).append(label)

            surplus = _cell(items, ("本期",), i)
            rev, exp = _cell(items, ("收入合計",), i), _cell(items, ("支出合計",), i)
            label = f"[欄{i}:{flat}] 本期餘絀 = 收入合計 − 支出合計"
            if None in (surplus, rev, exp):
                res.skipped.append(label)
            else:
                (res.passed if abs(surplus - (rev - exp)) <= tol else res.failed).append(label)

        res_var = _validate_variance(items, payload, tol)
        res.passed.extend(res_var.passed)
        res.failed.extend(res_var.failed)
        res.skipped.extend(res_var.skipped)

    return res


def _income_components(items: list[dict], side: str) -> list[tuple[str, ...]]:
    """Line items on one side of the 收支餘絀表, excluding its own total lines."""
    revenue_keys = ("收入", "收益")
    out: list[tuple[str, ...]] = []
    for it in items:
        flat = "".join(str(it.get("label", "")).split())
        if any(k in flat for k in ("合計", "本期", "所得稅")):
            continue
        is_revenue = any(k in flat for k in revenue_keys)
        if (side == "收入") == is_revenue:
            out.append((flat,))
    return out


def _validate_variance(items: list[dict], payload: dict, tol: float) -> ValidationResult:
    """差異數 = 決算數 − 預算數, per line item."""
    res = ValidationResult()
    labels = ["".join(str(c).split()) for c in (payload.get("period_labels") or [])]
    try:
        bi, ai = labels.index("預算數"), labels.index("決算數")
        di = labels.index("差異數")
    except ValueError:
        res.skipped.append("差異數 = 決算數 − 預算數（找不到對應欄位）")
        return res
    for it in items:
        values = it.get("values") or []
        if max(bi, ai, di) >= len(values):
            continue
        d = values[di]
        if d is None:
            continue
        got = (values[ai] or 0) - (values[bi] or 0)
        label = f"{it.get('label')} 差異數"
        (res.passed if abs(d - got) <= tol else res.failed).append(label)
    return res
