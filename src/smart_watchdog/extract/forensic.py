"""Forensic field set for 非營利園 reports, plus signal computation.

Deliberately narrower than :mod:`schema`, which transcribes a whole statement.
Here we ask only for the fields the forensic signals need, plus the subtotals
required to verify them -- fewer cells to read means fewer chances to misread, and
every field still lands inside an arithmetic check.

Field groups and the signal each one feeds:

    資產負債表   現金 / 預收款項          -> prepaid_coverage (惡性倒閉預警)
                 業務發展準備金 vs 準備    -> reserve_funding_gap
                 累積餘絀 / 本期餘絀       -> equity_erosion
    收支餘絀表   各科目執行率              -> deferred_maintenance, related_party_priority
    附註一       核定/實際招收人數         -> enrolment_utilisation
                 員工數 / 教保人員數       -> staff_ratio (幼照法法遵)
"""

from __future__ import annotations

import dataclasses

FORENSIC_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "code": {"type": "string"},
        "short_name": {"type": "string"},
        "academic_year": {"type": "string"},
        "balance_sheet": {
            "type": "object",
            "properties": {
                "page": {"type": ["integer", "null"]},
                "date_current": {"type": ["string", "null"]},
                "cash": {"type": ["number", "null"]},
                "prepaid_receipts": {"type": ["number", "null"], "description": "預收款項"},
                "current_assets_total": {"type": ["number", "null"]},
                "current_liabilities_total": {"type": ["number", "null"]},
                "reserve_asset": {
                    "type": ["number", "null"],
                    "description": "業務發展準備金（資產）",
                },
                "reserve_liability": {
                    "type": ["number", "null"],
                    "description": "業務發展準備（負債）",
                },
                "severance_asset": {
                    "type": ["number", "null"],
                    "description": "資遣費準備金（資產）",
                },
                "severance_liability": {
                    "type": ["number", "null"],
                    "description": "資遣費準備（負債）",
                },
                "accumulated_surplus": {"type": ["number", "null"], "description": "累積餘絀"},
                "current_surplus": {"type": ["number", "null"], "description": "本期餘絀"},
                "equity_total": {"type": ["number", "null"], "description": "餘絀總額"},
                "total_assets": {"type": ["number", "null"]},
                "total_liabilities": {"type": ["number", "null"]},
            },
        },
        "income_statement": {
            "type": "object",
            "properties": {
                "page": {"type": ["integer", "null"]},
                "period": {"type": ["string", "null"]},
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "budget": {"type": ["number", "null"]},
                            "actual": {"type": ["number", "null"]},
                            "execution_pct": {"type": ["number", "null"]},
                        },
                        "required": ["label", "budget", "actual"],
                    },
                },
            },
        },
        "note_1": {
            "type": "object",
            "properties": {
                "page": {"type": ["integer", "null"]},
                "operator": {"type": ["string", "null"]},
                "contract_period": {"type": ["string", "null"]},
                "approved_capacity": {
                    "type": ["number", "null"],
                    "description": "核定招收總人數",
                },
                "actual_enrolment": {
                    "type": ["number", "null"],
                    "description": "實際招收總人數",
                },
                "total_staff": {"type": ["number", "null"], "description": "員工人數"},
                "educators": {"type": ["number", "null"], "description": "其中教保人員數"},
            },
        },
        "issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["code", "balance_sheet", "income_statement", "note_1", "issues"],
}


@dataclasses.dataclass
class ForensicSignals:
    """Signals derived from one 園-year. ``None`` where inputs are unavailable."""

    code: str
    short_name: str
    prepaid_coverage: float | None = None
    reserve_funding_gap: float | None = None
    equity_erosion: float | None = None
    maintenance_execution: float | None = None
    repair_execution: float | None = None
    personnel_execution: float | None = None
    admin_fee_execution: float | None = None
    related_party_priority: float | None = None
    enrolment_utilisation: float | None = None
    staff_ratio: float | None = None
    cost_per_child: float | None = None
    unbudgeted_spend: float | None = None

    def as_dict(self) -> dict:
        return dataclasses.asdict(self)


# Expense lines that represent spending on the children and the facility, used as
# the comparison base for 行政管理費 (the fee paid to the 受託法人 itself).
OPERATING_LINES = ("人事費", "業務費", "材料費", "維護費", "修繕購置費", "雜支")
ADMIN_LINE = "行政管理費"


def _line(lines: list[dict], label: str) -> dict | None:
    for ln in lines:
        if label in "".join(str(ln.get("label", "")).split()):
            return ln
    return None


def _execution(lines: list[dict], label: str) -> float | None:
    """決算 / 預算 for one line, recomputed rather than trusting the printed %.

    The printed 執行率 is rounded to whole percent, and recomputing it also
    cross-checks the two amounts we extracted.
    """
    ln = _line(lines, label)
    if not ln:
        return None
    budget, actual = ln.get("budget"), ln.get("actual")
    if not budget:
        return None
    return (actual or 0) / budget


def compute_signals(payload: dict) -> ForensicSignals:
    """Derive the forensic signals from one extracted report."""
    bs = payload.get("balance_sheet") or {}
    inc = payload.get("income_statement") or {}
    note = payload.get("note_1") or {}
    lines = inc.get("lines") or []

    sig = ForensicSignals(
        code=payload.get("code", ""),
        short_name=payload.get("short_name", ""),
    )

    cash, prepaid = bs.get("cash"), bs.get("prepaid_receipts")
    if cash is not None and prepaid:
        # <1 means parents' prepaid fees have already been spent -- the clearest
        # public-data warning of a sudden closure.
        sig.prepaid_coverage = cash / prepaid

    ra, rl = bs.get("reserve_asset"), bs.get("reserve_liability")
    if ra is not None and rl is not None:
        sig.reserve_funding_gap = rl - ra

    acc, cur = bs.get("accumulated_surplus"), bs.get("current_surplus")
    if acc and cur is not None:
        sig.equity_erosion = cur / abs(acc)

    sig.maintenance_execution = _execution(lines, "維護費")
    sig.repair_execution = _execution(lines, "修繕購置費")
    sig.personnel_execution = _execution(lines, "人事費")
    sig.admin_fee_execution = _execution(lines, ADMIN_LINE)

    others = [
        e for label in OPERATING_LINES
        if (e := _execution(lines, label)) is not None
    ]
    if sig.admin_fee_execution is not None and others:
        # Positive means the operator's own fee was executed more fully than the
        # money meant for children and the building.
        sig.related_party_priority = sig.admin_fee_execution - sum(others) / len(others)

    cap, act = note.get("approved_capacity"), note.get("actual_enrolment")
    if cap and act is not None:
        sig.enrolment_utilisation = act / cap

    educators = note.get("educators")
    if act and educators:
        sig.staff_ratio = act / educators

    total_expense = _line(lines, "支出合計")
    if total_expense and act:
        actual = total_expense.get("actual")
        if actual:
            sig.cost_per_child = actual / act

    # Spending on lines with no appropriation at all.
    sig.unbudgeted_spend = sum(
        (ln.get("actual") or 0)
        for ln in lines
        if not ln.get("budget")
        and ln.get("actual")
        and "合計" not in "".join(str(ln.get("label", "")).split())
        and "餘絀" not in "".join(str(ln.get("label", "")).split())
    ) or None

    return sig


def validate_forensic(payload: dict, tol: float = 1.0) -> tuple[list[str], list[str]]:
    """Arithmetic checks over the narrowed field set. Returns (passed, failed)."""
    passed: list[str] = []
    failed: list[str] = []
    bs = payload.get("balance_sheet") or {}
    inc = payload.get("income_statement") or {}
    lines = inc.get("lines") or []

    def check(name: str, lhs: object, rhs: object) -> None:
        if lhs is None or rhs is None:
            return
        (passed if abs(float(lhs) - float(rhs)) <= tol else failed).append(name)

    if bs.get("total_assets") is not None and bs.get("total_liabilities") is not None:
        check(
            "資產總計 = 負債總額 + 餘絀總額",
            bs["total_assets"],
            (bs["total_liabilities"] or 0) + (bs.get("equity_total") or 0),
        )
    check(
        "餘絀總額 = 累積餘絀 + 本期餘絀",
        bs.get("equity_total"),
        (bs.get("accumulated_surplus") or 0) + (bs.get("current_surplus") or 0)
        if bs.get("equity_total") is not None
        else None,
    )

    for label in (*OPERATING_LINES, ADMIN_LINE):
        ln = _line(lines, label)
        if not ln or not ln.get("budget") or ln.get("execution_pct") is None:
            continue
        got = (ln.get("actual") or 0) / ln["budget"] * 100
        # The statement prints 執行率 rounded to whole percent.
        check(f"{label} 執行率", ln["execution_pct"], round(got))

    return passed, failed
