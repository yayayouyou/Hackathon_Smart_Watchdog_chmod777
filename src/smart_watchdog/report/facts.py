"""Assemble the verified facts behind one audit recommendation letter.

Nothing here writes prose. This module answers "what do we actually know about
this 園, and where did each piece come from", and the answer is a closed set of
figures and quotations that a narrative layer may arrange but never add to.

Two rules shape the structure:

**Every claim carries its source.** A finding is a rule name, the institution's
*own* 附註二 wording verbatim, the arithmetic, and the 學年度 it belongs to. An
inspector receiving the letter must be able to open page 11 of that report and
see the same sentence.

**What we could not examine is a fact too.** ``coverage`` records the checks that
did not run and why -- no public financial statement, no evaluation on file, a
denominator whose basis the note leaves undefined. A letter that lists three
findings and stays silent about the twelve checks that never ran invites the
reader to treat silence as clearance, which for the 94.8% of 園 that file no
statements would be badly wrong.

The output of :func:`build_facts` is also the allow-list the verifier uses: any
figure in generated prose that is not in here is a fabrication, and
``report/verify.py`` rejects the letter rather than letting it reach a real
institution's name.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any

# 民國 year of the balance-sheet date for 學年度 N is N+1; the statement is filed
# after that date, which is the earliest we could have raised the question.
ROC_OFFSET = 1911


@dataclasses.dataclass(frozen=True)
class Finding:
    """One failed compliance check, with the institution's own rule text."""

    academic_year: int
    rule: str
    rule_text: str
    detail: str
    severity: str

    @property
    def amounts(self) -> list[int]:
        return _amounts(self.detail)


@dataclasses.dataclass(frozen=True)
class AuditFacts:
    """Everything a letter may state about one 園, and nothing else."""

    institution_id: str
    title: str
    establishment_type: str
    town: str
    approved_capacity: int | None

    priority_rank: int
    priority_total: int
    priority_score: float
    review_reasons: list[str]

    prior_penalties: int
    days_since_last_penalty: int | None
    events_365d: int
    latest_event_type: str
    latest_event_date: str

    evaluation_result: str
    evaluation_date: str
    evaluation_partial: bool

    findings: list[Finding]
    reserve_verdicts: list[str]
    staffing: dict[str, Any]
    coverage: list[str]

    def allowed_numbers(self) -> set[int]:
        """Every figure a letter is permitted to contain.

        Deliberately generous about small integers (years, counts) and strict
        about money: an invented amount is the failure mode that matters.
        """
        out: set[int] = set()
        for f in self.findings:
            out.update(f.amounts)
            out.add(f.academic_year)
            out.add(f.academic_year + ROC_OFFSET + 1)
        out.update({self.priority_rank, self.priority_total, self.prior_penalties,
                    self.events_365d})
        if self.approved_capacity:
            out.add(int(self.approved_capacity))
        if self.days_since_last_penalty is not None:
            out.add(int(self.days_since_last_penalty))
        for v in self.staffing.values():
            if isinstance(v, (int, float)) and float(v).is_integer():
                out.add(int(v))
        for text in [self.evaluation_date, self.latest_event_date, *self.coverage,
                     *self.reserve_verdicts]:
            out.update(_amounts(str(text)))
        return out

    def as_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["findings"] = [dataclasses.asdict(f) for f in self.findings]
        return d


_NUM = re.compile(r"\d[\d,]*")


def _amounts(text: str) -> list[int]:
    return [int(m.group(0).replace(",", "")) for m in _NUM.finditer(str(text))]


def _int(value: object) -> int | None:
    try:
        if value is None or value != value:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _coverage_notes(row: Any, findings: list[Finding], staffing: dict) -> list[str]:
    """State plainly which checks did not run, so silence is never read as a pass."""
    notes: list[str] = []
    if not int(row.financial_data_available):
        notes.append(
            "本園非屬應公開財務報告之類型，未進行財務法遵檢核；"
            "本次未發現財務事項，係本系統無資料可查，非該園財務無虞"
        )
    else:
        checked = _int(row.compliance_years_checked) or 0
        notes.append(f"已就 {checked} 個學年度的財務報告完成法遵檢核")
        if not findings:
            notes.append("上開學年度未發現與其自述會計政策不符之處")
    if not str(row.eval_result or "").strip():
        notes.append("查無官方評鑑紀錄；未受評鑑不等於評鑑通過")
    if not staffing:
        notes.append("無員工人數與人事費資料，未進行人力配置對照")
    else:
        notes.append(
            "每人人事費為查證對照而非風險指標：非營利園採成本分攤制，"
            "全體 94 份非開辦年報告中無任何一份落在四分位距之外"
        )
    notes.append(
        "本系統之排序依據為登記、裁罰、營運型態等公開資料，"
        "時序切分實測 AUC 0.640；排序本身不構成違法認定"
    )
    return notes


def build_facts(
    row: Any,
    findings_df: Any,
    crosswalk_df: Any,
    timeseries_df: Any,
    dossier: dict[str, Any] | None = None,
    total: int | None = None,
) -> AuditFacts:
    """Collect one 園's verified facts from the already-computed tables."""
    import json

    codes = set()
    for r in crosswalk_df.itertuples(index=False):
        if row.id[:8] in {i[:8] for i in json.loads(r.registry_ids or "[]")}:
            codes.add(str(r.code))

    findings: list[Finding] = [
        Finding(academic_year=int(f.academic_year), rule=str(f.rule),
                rule_text=str(f.rule_text), detail=str(f.detail),
                severity=str(f.severity))
        for f in findings_df.itertuples(index=False)
        if str(f.code) in codes and str(f.passed) == "False"
    ]
    findings.sort(key=lambda f: (-f.academic_year, f.severity != "high"))

    verdicts = [
        f"{t.reserve}・{t.prev_year}→{t.year} 學年度：{t.verdict}"
        for t in timeseries_df.itertuples(index=False) if str(t.code) in codes
    ]

    staffing: dict[str, Any] = {}
    if dossier:
        for code in codes:
            rows = [s for s in dossier.get(code, {}).get("staff", [])
                    if s.get("st") and s.get("cost")]
            if rows:
                last = rows[-1]
                staffing = {
                    "academic_year": last["y"], "total_staff": last["st"],
                    "educators": last.get("ed"),
                    "personnel_cost_per_head": round(last["cost"] / last["st"]),
                    "enrolment": last.get("en"), "capacity": last.get("cap"),
                }
                break

    reasons = [r for r in str(row.review_reason or "").split("；") if r]
    return AuditFacts(
        institution_id=str(row.id), title=str(row.title),
        establishment_type=str(row.type), town=str(row.town),
        approved_capacity=_int(row.count_approved),
        priority_rank=int(row.priority_rank_overall),
        priority_total=int(total) if total else int(row.priority_rank_overall),
        priority_score=float(row.priority_score),
        review_reasons=reasons,
        prior_penalties=_int(row.n_penalties_prior) or 0,
        days_since_last_penalty=_int(row.days_since_last_penalty),
        events_365d=_int(row.events_365d) or 0,
        latest_event_type=str(row.latest_event_type or "").strip(),
        latest_event_date=str(row.latest_event_date or "").strip(),
        evaluation_result=str(row.eval_result or "").strip(),
        evaluation_date=str(row.eval_date or "").strip(),
        evaluation_partial=bool(int(row.eval_partial or 0)),
        findings=findings, reserve_verdicts=verdicts, staffing=staffing,
        coverage=_coverage_notes(row, findings, staffing),
    )
