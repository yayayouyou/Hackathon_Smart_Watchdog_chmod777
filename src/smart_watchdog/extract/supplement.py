"""Supplemental extraction contract for audited nonprofit-kindergarten reports.

The normalized 132-report dataset deliberately stays unchanged.  This module
covers fields that require OCR on separate pages: the auditor's report, statement
of changes in net assets, and statement of cash flows.  Blank or dash cells remain
``None`` in the payload; arithmetic may treat them as zero only after a row or
section has been established as present.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable

SCHEMA_VERSION = "1.0"
NUMBER_OR_NULL = {"type": ["number", "null"]}

SUPPLEMENT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "code": {"type": "string"},
        "short_name": {"type": "string"},
        "academic_year": {"type": "string"},
        "source_pdf": {"type": "string"},
        "source_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "issues": {"type": "array", "items": {"type": "string"}},
        "page_provenance": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "page": {"type": "integer"},
                    "page_footer": {"type": "string"},
                    "text_chars": {"type": "integer"},
                    "text_usable": {"type": "boolean"},
                    "route": {"type": "string"},
                    "tool": {"type": "string"},
                    "ocr_languages": {"type": "array", "items": {"type": "string"}},
                    "render_dpi": {"type": "integer"},
                    "ocr_line_count": {"type": "integer"},
                    "ocr_mean_confidence": {"type": "number"},
                    "raw_text_path": {"type": "string"},
                    "manually_verified": {"type": "boolean"},
                },
                "required": [
                    "page",
                    "page_footer",
                    "text_chars",
                    "text_usable",
                    "route",
                    "tool",
                    "ocr_languages",
                    "render_dpi",
                    "ocr_line_count",
                    "ocr_mean_confidence",
                    "raw_text_path",
                    "manually_verified",
                ],
            },
        },
        "audit_report": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "pages": {"type": "array", "items": {"type": "integer"}},
                "opinion_type": {"enum": ["unmodified", "qualified", "adverse", "disclaimer"]},
                "firm": {"type": "string"},
                "accountants": {"type": "array", "items": {"type": "string"}},
                "report_date": {"type": "string"},
                "opinion_text": {"type": "string"},
                "basis_text": {"type": "string"},
                "emphasis_of_matter_text": {"type": "string"},
            },
            "required": [
                "pages",
                "opinion_type",
                "firm",
                "accountants",
                "report_date",
                "opinion_text",
                "basis_text",
                "emphasis_of_matter_text",
            ],
        },
        "net_asset_changes": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "page": {"type": "integer"},
                "page_footer": {"type": "string"},
                "columns": {"const": ["accumulated_surplus", "current_surplus", "total"]},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "label": {"type": "string"},
                            "accumulated_surplus": NUMBER_OR_NULL,
                            "current_surplus": NUMBER_OR_NULL,
                            "total": NUMBER_OR_NULL,
                        },
                        "required": [
                            "label",
                            "accumulated_surplus",
                            "current_surplus",
                            "total",
                        ],
                    },
                },
            },
            "required": ["page", "page_footer", "columns", "rows"],
        },
        "cash_flow_statement": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "page": {"type": "integer"},
                "page_footer": {"type": "string"},
                "periods": {"type": "array", "items": {"type": "string"}},
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "label": {"type": "string"},
                            "kind": {
                                "enum": [
                                    "operating_item",
                                    "operating_net",
                                    "investing_item",
                                    "investing_net",
                                    "financing_item",
                                    "financing_net",
                                    "net_change",
                                    "cash_beginning",
                                    "cash_ending",
                                ]
                            },
                            "values": {"type": "array", "items": NUMBER_OR_NULL},
                        },
                        "required": ["label", "kind", "values"],
                    },
                },
            },
            "required": ["page", "page_footer", "periods", "rows"],
        },
        "reconciliation": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "name": {"type": "string"},
                    "status": {"enum": ["passed", "failed", "skipped"]},
                    "lhs": NUMBER_OR_NULL,
                    "rhs": NUMBER_OR_NULL,
                    "difference": NUMBER_OR_NULL,
                    "detail": {"type": ["string", "null"]},
                },
                "required": ["name", "status", "lhs", "rhs", "difference"],
            },
        },
    },
    "required": [
        "schema_version",
        "code",
        "short_name",
        "academic_year",
        "source_pdf",
        "source_sha256",
        "issues",
        "page_provenance",
        "audit_report",
        "net_asset_changes",
        "cash_flow_statement",
        "reconciliation",
    ],
}


@dataclasses.dataclass(frozen=True)
class ReconciliationCheck:
    """One exact-dollar reconciliation, with ``difference = lhs - rhs``."""

    name: str
    status: str
    lhs: float | None = None
    rhs: float | None = None
    difference: float | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class SupplementValidationResult:
    """Structural and arithmetic validation outcome for one supplement."""

    schema_errors: list[str] = dataclasses.field(default_factory=list)
    checks: list[ReconciliationCheck] = dataclasses.field(default_factory=list)

    @property
    def passed(self) -> list[ReconciliationCheck]:
        return [check for check in self.checks if check.status == "passed"]

    @property
    def failed(self) -> list[ReconciliationCheck]:
        return [check for check in self.checks if check.status == "failed"]

    @property
    def skipped(self) -> list[ReconciliationCheck]:
        return [check for check in self.checks if check.status == "skipped"]

    @property
    def ok(self) -> bool:
        return not self.schema_errors and not self.failed


def _is_number_or_none(value: object) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


def _shape_errors(payload: dict) -> list[str]:
    """Validate the load-bearing contract without adding a jsonschema dependency."""
    errors: list[str] = []
    required = set(SUPPLEMENT_SCHEMA["required"])
    errors.extend(f"missing root field: {key}" for key in required if key not in payload)
    errors.extend(f"unexpected root field: {key}" for key in payload if key not in required)

    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    errors.extend(
        f"{key} must be a non-empty string"
        for key in ("code", "short_name", "academic_year", "source_pdf")
        if not isinstance(payload.get(key), str) or not payload.get(key)
    )
    digest = payload.get("source_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(
        char not in "0123456789abcdef" for char in digest
    ):
        errors.append("source_sha256 must be 64 lowercase hexadecimal characters")
    issues = payload.get("issues")
    if not isinstance(issues, list) or any(not isinstance(issue, str) for issue in issues):
        errors.append("issues must be a list of strings")

    provenance_fields = {
        "page",
        "page_footer",
        "text_chars",
        "text_usable",
        "route",
        "tool",
        "ocr_languages",
        "render_dpi",
        "ocr_line_count",
        "ocr_mean_confidence",
        "raw_text_path",
        "manually_verified",
    }
    provenance = payload.get("page_provenance")
    if not isinstance(provenance, list) or not provenance:
        errors.append("page_provenance must be a non-empty list")
    else:
        pages = [entry.get("page") for entry in provenance if isinstance(entry, dict)]
        if pages != [3, 4, 8, 9]:
            errors.append("page_provenance must contain pages 3, 4, 8, 9 in order")
        for index, entry in enumerate(provenance):
            if not isinstance(entry, dict):
                errors.append("page_provenance entries must be objects")
                continue
            errors.extend(
                f"page_provenance[{index}] missing field: {key}"
                for key in provenance_fields
                if key not in entry
            )
            errors.extend(
                f"page_provenance[{index}] unexpected field: {key}"
                for key in entry
                if key not in provenance_fields
            )
            if entry.get("text_chars") != 0 or entry.get("text_usable") is not False:
                errors.append(f"page {entry.get('page')} must record the unusable text layer")
            if entry.get("manually_verified") is not True:
                errors.append(f"page {entry.get('page')} must be manually verified")
            if not isinstance(entry.get("ocr_line_count"), int):
                errors.append(f"page {entry.get('page')} ocr_line_count must be an integer")
            if not _is_number_or_none(entry.get("ocr_mean_confidence")):
                errors.append(f"page {entry.get('page')} confidence must be numeric")

    audit_fields = {
        "pages",
        "opinion_type",
        "firm",
        "accountants",
        "report_date",
        "opinion_text",
        "basis_text",
        "emphasis_of_matter_text",
    }
    audit = payload.get("audit_report")
    if not isinstance(audit, dict):
        errors.append("audit_report must be an object")
    else:
        errors.extend(
            f"audit_report missing field: {key}" for key in audit_fields if key not in audit
        )
        errors.extend(
            f"audit_report unexpected field: {key}" for key in audit if key not in audit_fields
        )
        errors.extend(
            f"audit_report.{key} must be a non-empty string"
            for key in audit_fields - {"pages", "accountants"}
            if not isinstance(audit.get(key), str) or not audit.get(key)
        )
        if audit.get("pages") != [3, 4]:
            errors.append("audit_report.pages must be [3, 4]")
        if audit.get("opinion_type") not in {
            "unmodified",
            "qualified",
            "adverse",
            "disclaimer",
        }:
            errors.append("audit_report.opinion_type is invalid")
        accountants = audit.get("accountants")
        if not isinstance(accountants, list) or not accountants or any(
            not isinstance(name, str) or not name for name in accountants
        ):
            errors.append("audit_report.accountants must be a non-empty string list")

    net = payload.get("net_asset_changes")
    net_fields = {"page", "page_footer", "columns", "rows"}
    net_row_fields = {"label", "accumulated_surplus", "current_surplus", "total"}
    net_rows: object = None
    if not isinstance(net, dict):
        errors.append("net_asset_changes must be an object")
    else:
        errors.extend(
            f"net_asset_changes missing field: {key}" for key in net_fields if key not in net
        )
        errors.extend(
            f"net_asset_changes unexpected field: {key}"
            for key in net
            if key not in net_fields
        )
        if net.get("page") != 8:
            errors.append("net_asset_changes.page must be 8")
        if net.get("columns") != ["accumulated_surplus", "current_surplus", "total"]:
            errors.append("net_asset_changes.columns has an invalid order")
        net_rows = net.get("rows")
    if not isinstance(net_rows, list) or not net_rows:
        errors.append("net_asset_changes.rows must be a non-empty list")
    else:
        for index, row in enumerate(net_rows):
            if not isinstance(row, dict):
                errors.append(f"net_asset_changes.rows[{index}] must be an object")
                continue
            errors.extend(
                f"net_asset_changes.rows[{index}] missing field: {key}"
                for key in net_row_fields
                if key not in row
            )
            errors.extend(
                f"net_asset_changes.rows[{index}] unexpected field: {key}"
                for key in row
                if key not in net_row_fields
            )
            if not isinstance(row.get("label"), str) or not row.get("label"):
                errors.append(f"net_asset_changes.rows[{index}].label must not be empty")
            errors.extend(
                f"net_asset_changes.rows[{index}].{key} must be number or null"
                for key in ("accumulated_surplus", "current_surplus", "total")
                if not _is_number_or_none(row.get(key))
            )
        closing = net_rows[-1]
        if isinstance(closing, dict) and any(
            not _is_number_or_none(closing.get(key)) or closing.get(key) is None
            for key in ("accumulated_surplus", "current_surplus", "total")
        ):
            errors.append("net_asset_changes closing row must contain all three amounts")

    cash = payload.get("cash_flow_statement")
    cash_fields = {"page", "page_footer", "periods", "rows"}
    cash_row_fields = {"label", "kind", "values"}
    allowed_kinds = {
        "operating_item",
        "operating_net",
        "investing_item",
        "investing_net",
        "financing_item",
        "financing_net",
        "net_change",
        "cash_beginning",
        "cash_ending",
    }
    periods: object = None
    cash_rows: object = None
    if not isinstance(cash, dict):
        errors.append("cash_flow_statement must be an object")
    else:
        errors.extend(
            f"cash_flow_statement missing field: {key}"
            for key in cash_fields
            if key not in cash
        )
        errors.extend(
            f"cash_flow_statement unexpected field: {key}"
            for key in cash
            if key not in cash_fields
        )
        if cash.get("page") != 9:
            errors.append("cash_flow_statement.page must be 9")
        periods, cash_rows = cash.get("periods"), cash.get("rows")
    if not isinstance(periods, list) or len(periods) != 2 or any(
        not isinstance(period, str) or not period for period in periods
    ):
        errors.append("cash_flow_statement.periods must contain exactly two strings")
    elif any("～" in period for period in periods):
        errors.append("cash_flow_statement.periods must use normalized ASCII ~")
    if not isinstance(cash_rows, list) or not cash_rows:
        errors.append("cash_flow_statement.rows must be a non-empty list")
    elif isinstance(periods, list):
        for index, row in enumerate(cash_rows):
            if not isinstance(row, dict):
                errors.append(f"cash_flow_statement.rows[{index}] must be an object")
                continue
            errors.extend(
                f"cash_flow_statement.rows[{index}] missing field: {key}"
                for key in cash_row_fields
                if key not in row
            )
            errors.extend(
                f"cash_flow_statement.rows[{index}] unexpected field: {key}"
                for key in row
                if key not in cash_row_fields
            )
            if row.get("kind") not in allowed_kinds:
                errors.append(f"cash_flow_statement.rows[{index}].kind is invalid")
            values = row.get("values")
            if not isinstance(values, list) or len(values) != len(periods):
                errors.append(
                    f"cash_flow_statement.rows[{index}].values must align to periods"
                )
            elif any(not _is_number_or_none(value) for value in values):
                errors.append(
                    f"cash_flow_statement.rows[{index}].values must contain numbers or null"
                )
        required_kinds = (
            "operating_net",
            "investing_net",
            "financing_net",
            "net_change",
            "cash_beginning",
            "cash_ending",
        )
        for kind in required_kinds:
            count = sum(
                isinstance(row, dict) and row.get("kind") == kind for row in cash_rows
            )
            if count != 1:
                errors.append(f"cash_flow_statement requires exactly one {kind} row")
        for item_kind, net_kind in (
            ("operating_item", "operating_net"),
            ("investing_item", "investing_net"),
            ("financing_item", "financing_net"),
        ):
            net_rows = [
                row
                for row in cash_rows
                if isinstance(row, dict) and row.get("kind") == net_kind
            ]
            if len(net_rows) != 1 or not isinstance(net_rows[0].get("values"), list):
                continue
            for period_index in range(len(periods)):
                has_detail = any(
                    isinstance(row, dict)
                    and row.get("kind") == item_kind
                    and isinstance(row.get("values"), list)
                    and len(row["values"]) > period_index
                    and row["values"][period_index] is not None
                    for row in cash_rows
                )
                net_values = net_rows[0]["values"]
                if has_detail and (
                    len(net_values) <= period_index or net_values[period_index] is None
                ):
                    errors.append(
                        f"cash_flow_statement {net_kind} is missing for period {period_index}"
                    )

    reconciliation = payload.get("reconciliation")
    reconciliation_fields = {"name", "status", "lhs", "rhs", "difference", "detail"}
    if not isinstance(reconciliation, list):
        errors.append("reconciliation must be a list")
    else:
        for index, entry in enumerate(reconciliation):
            if not isinstance(entry, dict):
                errors.append(f"reconciliation[{index}] must be an object")
                continue
            required_reconciliation = reconciliation_fields - {"detail"}
            errors.extend(
                f"reconciliation[{index}] missing field: {key}"
                for key in required_reconciliation
                if key not in entry
            )
            errors.extend(
                f"reconciliation[{index}] unexpected field: {key}"
                for key in entry
                if key not in reconciliation_fields
            )
            if not isinstance(entry.get("name"), str) or not entry.get("name"):
                errors.append(f"reconciliation[{index}].name must not be empty")
            if entry.get("status") not in {"passed", "failed", "skipped"}:
                errors.append(f"reconciliation[{index}].status is invalid")
            if "detail" in entry and entry["detail"] is not None and not isinstance(
                entry["detail"], str
            ):
                errors.append(f"reconciliation[{index}].detail must be string or null")
            lhs, rhs, difference = entry.get("lhs"), entry.get("rhs"), entry.get("difference")
            if not all(_is_number_or_none(value) for value in (lhs, rhs, difference)):
                errors.append(f"reconciliation[{index}] operands must be number or null")
            elif lhs is not None and rhs is not None:
                expected = float(lhs) - float(rhs)
                if difference != expected:
                    errors.append(f"reconciliation[{index}].difference must equal lhs - rhs")
                expected_status = "passed" if expected == 0 else "failed"
                if entry.get("status") != expected_status:
                    errors.append(f"reconciliation[{index}].status contradicts its operands")
    return errors


def _check(
    name: str,
    lhs: float | None,
    rhs: float | None,
    *,
    tolerance: float,
    detail: str | None = None,
    required: bool = False,
) -> ReconciliationCheck:
    if not _is_number_or_none(lhs) or not _is_number_or_none(rhs):
        return ReconciliationCheck(name, "failed", None, None, None, "operand is not numeric")
    if lhs is None or rhs is None:
        status = "failed" if required else "skipped"
        missing_detail = detail or "required reconciliation operand is missing"
        return ReconciliationCheck(name, status, lhs, rhs, None, missing_detail)
    difference = float(lhs) - float(rhs)
    status = "passed" if abs(difference) <= tolerance else "failed"
    return ReconciliationCheck(name, status, lhs, rhs, difference, detail)


def _identity_check(name: str, lhs: object, rhs: object) -> ReconciliationCheck:
    if lhs is None or rhs is None:
        return ReconciliationCheck(name, "failed", detail="document identity is missing")
    status = "passed" if lhs == rhs else "failed"
    detail = None if status == "passed" else f"supplement={lhs!r}, main={rhs!r}"
    return ReconciliationCheck(name, status, detail=detail)


def _only(rows: list[dict], kind: str, period_index: int) -> float | None:
    matches = [row for row in rows if row.get("kind") == kind]
    if len(matches) != 1:
        return None
    values = matches[0].get("values") or []
    return values[period_index] if period_index < len(values) else None


def _sum_present(values: Iterable[float | None]) -> float | None:
    materialized = list(values)
    if not any(value is not None for value in materialized):
        return None
    return sum(value or 0 for value in materialized)


def validate_supplement(
    payload: dict,
    main_payload: dict | None = None,
    tolerance: float = 0.0,
) -> SupplementValidationResult:
    """Validate a supplemental extraction and optionally reconcile its main JSON.

    All statement amounts are whole New Taiwan dollars, so the default is exact
    equality.  This intentionally catches the one-dollar source inconsistency in
    N18 p08 rather than accepting it under a generic rounding tolerance.
    """
    result = SupplementValidationResult(schema_errors=_shape_errors(payload))
    if result.schema_errors:
        return result
    checks = result.checks

    net_rows = (payload.get("net_asset_changes") or {}).get("rows") or []
    for row in net_rows:
        values = [
            row.get("accumulated_surplus"),
            row.get("current_surplus"),
            row.get("total"),
        ]
        if not any(value is not None for value in values):
            checks.append(
                ReconciliationCheck(
                    f"淨值變動表「{row.get('label', '')}」列加總",
                    "skipped",
                    detail="三欄均為 null，文件未列數字",
                )
            )
            continue
        lhs = row.get("total") or 0
        rhs = (row.get("accumulated_surplus") or 0) + (row.get("current_surplus") or 0)
        checks.append(
            _check(
                f"淨值變動表「{row.get('label', '')}」列加總",
                lhs,
                rhs,
                tolerance=tolerance,
                detail="null/破折號僅於本次算術中視為 0；原輸出仍保留 null",
            )
        )

    if main_payload is not None:
        for key in ("code", "short_name", "academic_year"):
            checks.append(
                _identity_check(
                    f"補充 JSON {key} = 主 JSON {key}",
                    payload.get(key),
                    main_payload.get(key),
                )
            )
        closing = net_rows[-1]
        balance = main_payload.get("balance_sheet") or {}
        for supplement_key, main_key, label in (
            ("accumulated_surplus", "accumulated_surplus", "期末累積餘絀"),
            ("current_surplus", "current_surplus", "期末本期餘絀"),
            ("total", "equity_total", "期末餘絀總額"),
        ):
            checks.append(
                _check(
                    f"淨值變動表{label} = 主 JSON 資產負債表",
                    closing.get(supplement_key),
                    balance.get(main_key),
                    tolerance=tolerance,
                    required=True,
                )
            )

    cash = payload.get("cash_flow_statement") or {}
    periods, cash_rows = cash.get("periods") or [], cash.get("rows") or []
    for index, period in enumerate(periods):
        operating_items = [
            row.get("values", [None] * len(periods))[index]
            for row in cash_rows
            if row.get("kind") == "operating_item"
        ]
        investing_items = [
            row.get("values", [None] * len(periods))[index]
            for row in cash_rows
            if row.get("kind") == "investing_item"
        ]
        financing_items = [
            row.get("values", [None] * len(periods))[index]
            for row in cash_rows
            if row.get("kind") == "financing_item"
        ]
        operating_net = _only(cash_rows, "operating_net", index)
        investing_net = _only(cash_rows, "investing_net", index)
        financing_net = _only(cash_rows, "financing_net", index)
        net_change = _only(cash_rows, "net_change", index)
        beginning = _only(cash_rows, "cash_beginning", index)
        ending = _only(cash_rows, "cash_ending", index)

        for label, net, items in (
            ("營業活動", operating_net, operating_items),
            ("投資活動", investing_net, investing_items),
            ("籌資活動", financing_net, financing_items),
        ):
            checks.append(
                _check(
                    f"現金流量表 {period} {label}淨額 = 明細加總",
                    net,
                    _sum_present(items),
                    tolerance=tolerance,
                )
            )

        section_sum = _sum_present([operating_net, investing_net, financing_net])
        checks.append(
            _check(
                f"現金流量表 {period} 現金淨增加 = 三類活動淨額",
                net_change,
                section_sum,
                tolerance=tolerance,
                detail="未列數字的活動淨額於三類加總中視為 0",
            )
        )
        cash_rollforward = (
            None if beginning is None or net_change is None else beginning + net_change
        )
        checks.append(
            _check(
                f"現金流量表 {period} 期末現金 = 期初現金 + 現金淨增加",
                ending,
                cash_rollforward,
                tolerance=tolerance,
            )
        )

    if len(periods) == 2:
        checks.append(
            _check(
                "本期初現金 = 比較期期末現金",
                _only(cash_rows, "cash_beginning", 0),
                _only(cash_rows, "cash_ending", 1),
                tolerance=tolerance,
            )
        )

    if main_payload is not None and periods:
        balance = main_payload.get("balance_sheet") or {}
        checks.append(
            _check(
                "本期期末現金 = 主 JSON 資產負債表現金",
                _only(cash_rows, "cash_ending", 0),
                balance.get("cash"),
                tolerance=tolerance,
                required=True,
            )
        )
        current_surplus = next(
            (
                row.get("values", [None])[0]
                for row in cash_rows
                if row.get("kind") == "operating_item"
                and "本期稅前餘絀" in row.get("label", "")
            ),
            None,
        )
        checks.append(
            _check(
                "現金流量表本期稅前餘絀 = 主 JSON 本期餘絀",
                current_surplus,
                balance.get("current_surplus"),
                tolerance=tolerance,
                required=True,
            )
        )

    computed_by_name = {check.name: check for check in checks}
    for index, stored in enumerate(payload.get("reconciliation") or []):
        computed = computed_by_name.get(stored["name"])
        if computed is None:
            result.schema_errors.append(
                f"reconciliation[{index}] does not match a computed check"
            )
            continue
        for key in ("status", "lhs", "rhs", "difference"):
            if stored.get(key) != getattr(computed, key):
                result.schema_errors.append(
                    f"reconciliation[{index}].{key} does not match computed result"
                )

    return result
