"""Normalize pinned official records into an auditable cross-source event timeline.

The adapters in this module are deliberately offline and source-faithful. They do
not merge similar records across sources, infer missing lifecycle outcomes, or
turn absence from a bounded pilot into evidence of compliance.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from smart_watchdog.features.build import category_of, severity_of
from smart_watchdog.scrape.observations import penalty_source_record_hash

SCHEMA_VERSION = "official-events-v1"
OFFICIAL_EVENT_FIELDS = [
    "event_id",
    "source_system",
    "source_record_id",
    "event_family",
    "event_type",
    "parent_event_id",
    "entity",
    "source_registry_id",
    "registry_ids",
    "registry_titles",
    "identity_status",
    "identity_evidence",
    "event_date",
    "event_date_semantics",
    "period_start",
    "period_end",
    "published_date",
    "observed_at",
    "title",
    "summary",
    "lifecycle_status",
    "outcome",
    "amount",
    "source_authority",
    "verification_status",
    "source_url",
    "raw_artifacts_json",
    "source_hashes_json",
    "severity",
    "severity_basis",
    "details_json",
]
EXPECTED_FAMILY_COUNTS = {
    "penalty": 1386,
    "evaluation": 9,
    "education_notice": 5,
    "corrective_action": 98,
    "procurement_award": 3,
}
REQUIRED_EDUCATION_NOTICE_KEYS = {
    "ntpc-kidedu-6097",
    "ntpc-kidedu-6098",
    "ntpc-kidedu-7077",
    "ntpc-kidedu-15015",
    "ntpc-kidedu-6787",
}


@dataclasses.dataclass(frozen=True)
class SourceContext:
    """Pinned provenance shared by every event from one source."""

    source_system: str
    source_authority: str
    verification_status: str
    source_url: str
    observed_at: str
    snapshot_id: str
    artifact_root: str
    snapshot_sha256: str = ""
    snapshot_object_path: str = ""


class IdentityIndex:
    """Resolve registry UUIDs and physical entities without dropping siblings."""

    def __init__(self, institutions: list[dict[str, str]]) -> None:
        _require_fields(institutions, {"id", "title", "entity"}, "institutions")
        self.by_id: dict[str, dict[str, str]] = {}
        grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in institutions:
            identifier = row.get("id", "")
            entity = row.get("entity", "")
            if not identifier or not entity:
                raise ValueError("institution identity spine has a blank id/entity")
            if identifier in self.by_id:
                raise ValueError(f"duplicate institution registry UUID: {identifier}")
            self.by_id[identifier] = row
            grouped[entity].append(row)
        self.by_entity = {
            entity: sorted(rows, key=lambda row: row["id"])
            for entity, rows in grouped.items()
        }

    def from_registry_id(self, identifier: str) -> dict[str, Any]:
        row = self.by_id.get(identifier)
        if row is None:
            raise ValueError(f"event references unknown registry UUID: {identifier}")
        return self.from_entity(
            row["entity"],
            identity_status="matched_source_registry_id",
            identity_evidence=f"source_registry_id:{identifier}",
            provided_ids=None,
            provided_titles=None,
        )

    def from_entity(
        self,
        entity: str,
        *,
        identity_status: str,
        identity_evidence: str,
        provided_ids: list[str] | None,
        provided_titles: list[str] | None,
    ) -> dict[str, Any]:
        if not entity:
            if provided_ids:
                raise ValueError("unresolved identity cannot retain registry IDs")
            return {
                "entity": "",
                "registry_ids": [],
                "registry_titles": [],
                "identity_status": identity_status,
                "identity_evidence": identity_evidence,
            }
        siblings = self.by_entity.get(entity)
        if siblings is None:
            raise ValueError(f"event references unknown entity: {entity}")
        identifiers = [row["id"] for row in siblings]
        titles = [row["title"] for row in siblings]
        if provided_ids is not None and sorted(provided_ids) != identifiers:
            raise ValueError(f"event omits or adds sibling UUIDs for {entity}")
        if provided_titles is not None and sorted(provided_titles) != sorted(titles):
            raise ValueError(f"event registry titles differ for {entity}")
        return {
            "entity": entity,
            "registry_ids": identifiers,
            "registry_titles": titles,
            "identity_status": identity_status,
            "identity_evidence": identity_evidence,
        }


def _require_fields(
    rows: list[dict[str, str]], required: set[str], label: str
) -> None:
    if not rows:
        raise ValueError(f"{label} has no records")
    missing = required - set(rows[0])
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_list(value: object, label: str) -> list[Any]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} must be valid JSON") from exc
    if not isinstance(parsed, list):
        raise ValueError(f"{label} must be a JSON array")
    return parsed


def _iso_date(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.replace("/", "-")
    try:
        return dt.date.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO-compatible date: {text!r}") from exc


def _iso_timestamp(value: object, label: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _digest(parts: Iterable[object]) -> str:
    payload = _json_value([str(part or "") for part in parts]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


def _artifacts(*values: tuple[str, str]) -> str:
    unique = {
        (path, sha256)
        for path, sha256 in values
        if str(path).strip() and str(sha256).strip()
    }
    return _json_value(
        [{"path": path, "sha256": sha256} for path, sha256 in sorted(unique)]
    )


def _base_event(
    *,
    event_id: str,
    source_record_id: str,
    event_family: str,
    event_type: str,
    context: SourceContext,
    identity: dict[str, Any],
    observed_at: str | None = None,
    source_registry_id: str = "",
    parent_event_id: str = "",
    event_date: str = "",
    event_date_semantics: str = "",
    period_start: str = "",
    period_end: str = "",
    published_date: str = "",
    title: str = "",
    summary: str = "",
    lifecycle_status: str = "",
    outcome: str = "",
    amount: str = "",
    source_url: str = "",
    raw_artifacts_json: str = "[]",
    source_hashes: object | None = None,
    severity: int | str = "",
    severity_basis: str = "",
    details: object | None = None,
) -> dict[str, str]:
    event = {
        "event_id": event_id,
        "source_system": context.source_system,
        "source_record_id": source_record_id,
        "event_family": event_family,
        "event_type": event_type,
        "parent_event_id": parent_event_id,
        "entity": str(identity["entity"]),
        "source_registry_id": source_registry_id,
        "registry_ids": _json_value(identity["registry_ids"]),
        "registry_titles": _json_value(identity["registry_titles"]),
        "identity_status": str(identity["identity_status"]),
        "identity_evidence": str(identity["identity_evidence"]),
        "event_date": event_date,
        "event_date_semantics": event_date_semantics,
        "period_start": period_start,
        "period_end": period_end,
        "published_date": published_date,
        "observed_at": _iso_timestamp(
            context.observed_at if observed_at is None else observed_at,
            "observed_at",
        ),
        "title": title,
        "summary": summary,
        "lifecycle_status": lifecycle_status,
        "outcome": outcome,
        "amount": amount,
        "source_authority": context.source_authority,
        "verification_status": context.verification_status,
        "source_url": source_url or context.source_url,
        "raw_artifacts_json": raw_artifacts_json,
        "source_hashes_json": _json_value(source_hashes or {}),
        "severity": str(severity),
        "severity_basis": severity_basis,
        "details_json": _json_value(details or {}),
    }
    if set(event) != set(OFFICIAL_EVENT_FIELDS):
        raise AssertionError("official event fields differ from schema")
    return event


def _penalty_events(
    rows: list[dict[str, str]],
    identity_index: IdentityIndex,
    context: SourceContext,
    observed_at_by_record_hash: dict[str, str],
) -> list[dict[str, str]]:
    _require_fields(
        rows,
        {
            "id",
            "title",
            "date",
            "article",
            "law",
            "fine",
            "actor",
            "actor_role",
            "actor_name",
            "punishment",
            "sanction_type",
            "penalty_group_id",
            "group_record_index",
            "group_record_count",
            "source_row_count",
            "law_variants",
        },
        "penalties",
    )
    events = []
    for row in rows:
        identifier = row["id"]
        identity = identity_index.from_registry_id(identifier)
        source_record_id = (
            f"{row['penalty_group_id']}:{row['group_record_index']}"
        )
        article = row.get("article", "")
        source_record_hash = penalty_source_record_hash(
            actor=row.get("actor"),
            registry_id=identifier,
            date=row.get("date"),
            law=row.get("law"),
            punishment=row.get("punishment"),
        )
        if source_record_hash not in observed_at_by_record_hash:
            raise ValueError(
                "processed penalty row is absent from immutable observation: "
                f"{source_record_id}"
            )
        details = {
            "article": article,
            "category": category_of(article),
            "law": row.get("law", ""),
            "law_variants": _json_list(row.get("law_variants"), "law_variants"),
            "actor_role": row.get("actor_role", ""),
            "actor_name": row.get("actor_name", ""),
            "sanction_type": row.get("sanction_type", ""),
            "penalty_group_id": row.get("penalty_group_id", ""),
            "group_record_index": int(row.get("group_record_index") or 0),
            "group_record_count": int(row.get("group_record_count") or 0),
            "source_row_count": int(row.get("source_row_count") or 0),
        }
        events.append(
            _base_event(
                event_id=f"penalty:{source_record_id}",
                source_record_id=source_record_id,
                event_family="penalty",
                event_type=row.get("sanction_type", ""),
                context=context,
                identity=identity,
                observed_at=observed_at_by_record_hash[source_record_hash],
                source_registry_id=identifier,
                event_date=_iso_date(row.get("date"), "penalty date"),
                event_date_semantics="sanction_date",
                title=f"裁罰：{row.get('title', '')}",
                summary="；".join(
                    value
                    for value in (row.get("law", ""), row.get("punishment", ""))
                    if value
                ),
                lifecycle_status="issued",
                outcome=row.get("sanction_type", ""),
                amount=row.get("fine", ""),
                raw_artifacts_json=_artifacts(
                    (
                        f"{context.artifact_root}/{context.snapshot_object_path}",
                        context.snapshot_sha256,
                    )
                ),
                source_hashes={"snapshot_sha256": context.snapshot_sha256},
                severity=severity_of(article),
                severity_basis="幼兒教育及照顧法條次 taxonomy",
                details=details,
            )
        )
    return events


def _evaluation_events(
    rows: list[dict[str, str]], identity_index: IdentityIndex, context: SourceContext
) -> list[dict[str, str]]:
    _require_fields(
        rows,
        {
            "query_id",
            "raw_file",
            "response_sha256",
            "source_table_id",
            "source_row_index",
            "source_title",
            "evaluation_academic_year",
            "evaluation_completed_date",
            "evaluation_result",
            "evaluation_report_detail_path",
            "entity",
            "registry_ids",
            "registry_titles",
            "join_status",
            "join_evidence",
        },
        "evaluations",
    )
    events = []
    for row in rows:
        entity = row.get("entity", "")
        identity = identity_index.from_entity(
            entity,
            identity_status=row.get("join_status", ""),
            identity_evidence=row.get("join_evidence", ""),
            provided_ids=[
                str(value)
                for value in _json_list(
                    row["registry_ids"], "evaluation registry_ids"
                )
            ],
            provided_titles=[
                str(value)
                for value in _json_list(
                    row["registry_titles"], "evaluation registry_titles"
                )
            ],
        )
        source_record_id = "eva_" + _digest(
            (
                row.get("query_id"),
                row.get("source_table_id"),
                row.get("source_row_index"),
                row.get("source_title"),
                row.get("evaluation_academic_year"),
                row.get("evaluation_completed_date"),
                row.get("evaluation_result"),
            )
        )
        detail_path = row.get("evaluation_report_detail_path", "")
        source_url = urllib.parse.urljoin(context.source_url, detail_path)
        events.append(
            _base_event(
                event_id=f"evaluation:{source_record_id}",
                source_record_id=source_record_id,
                event_family="evaluation",
                event_type="official_evaluation_result",
                context=context,
                identity=identity,
                source_registry_id=row.get("registry_id", ""),
                event_date=_iso_date(
                    row.get("evaluation_completed_date"), "evaluation completed date"
                ),
                event_date_semantics="evaluation_completed_date",
                title=f"評鑑：{row.get('source_title', '')}",
                summary=row.get("evaluation_result", ""),
                lifecycle_status="completed",
                outcome=row.get("evaluation_result", ""),
                source_url=source_url,
                raw_artifacts_json=_artifacts(
                    (
                        f"{context.artifact_root}/{row.get('raw_file', '')}",
                        row.get("response_sha256", ""),
                    )
                ),
                source_hashes={"response_sha256": row.get("response_sha256", "")},
                details={
                    "query_id": row.get("query_id", ""),
                    "source_table_id": row.get("source_table_id", ""),
                    "source_row_index": row.get("source_row_index", ""),
                    "academic_year": row.get("evaluation_academic_year", ""),
                    "report_label": row.get("evaluation_report_label", ""),
                    "report_postback": row.get("evaluation_report_postback", ""),
                    "crosswalk_report_keys": _json_list(
                        row.get("crosswalk_report_keys"), "crosswalk_report_keys"
                    ),
                },
            )
        )
    return events


def _notice_events(
    rows: list[dict[str, str]], context: SourceContext
) -> list[dict[str, str]]:
    _require_fields(
        rows,
        {
            "notice_key",
            "source_url",
            "title",
            "publication_date",
            "classification",
            "body_sha256",
            "attachments_json",
            "raw_detail_file",
            "raw_detail_sha256",
            "action_count",
            "full_population_claim",
            "absence_semantics",
        },
        "education notices",
    )
    identity = {
        "entity": "",
        "registry_ids": [],
        "registry_titles": [],
        "identity_status": "not_applicable_policy_or_general_notice",
        "identity_evidence": "notice scope is not an institution identity assertion",
    }
    events = []
    for row in rows:
        publication_date = _iso_date(
            row.get("publication_date"), "notice publication date"
        )
        artifact_root = row.get("artifact_root") or context.artifact_root
        raw_detail_file = row.get("raw_detail_file", "")
        observed_at = row.get("observed_at") or None
        events.append(
            _base_event(
                event_id=f"education_notice:{row['notice_key']}",
                source_record_id=row["notice_key"],
                event_family="education_notice",
                event_type=row.get("classification", ""),
                context=context,
                identity=identity,
                observed_at=observed_at,
                event_date=publication_date,
                event_date_semantics="explicit_publication_date_if_available",
                published_date=publication_date,
                title=row.get("title", ""),
                summary=row.get("classification", ""),
                lifecycle_status="published",
                outcome=row.get("classification", ""),
                source_url=row.get("source_url", ""),
                raw_artifacts_json=_artifacts(
                    (
                        f"{artifact_root}/{raw_detail_file}",
                        row.get("raw_detail_sha256", ""),
                    )
                ),
                source_hashes={
                    "body_sha256": row.get("body_sha256", ""),
                    "raw_detail_sha256": row.get("raw_detail_sha256", ""),
                },
                details={
                    "notice_id": row.get("notice_id", ""),
                    "classification": row.get("classification", ""),
                    "attachments": _json_list(
                        row.get("attachments_json"), "notice attachments"
                    ),
                    "action_count": int(row.get("action_count") or 0),
                    "full_population_claim": row.get("full_population_claim", ""),
                    "absence_semantics": row.get("absence_semantics", ""),
                },
            )
        )
    return events


def _action_events(
    rows: list[dict[str, str]],
    notices: list[dict[str, str]],
    identity_index: IdentityIndex,
    context: SourceContext,
) -> list[dict[str, str]]:
    _require_fields(
        rows,
        {
            "action_key",
            "notice_key",
            "institution_source_title",
            "action_type",
            "improvement_status",
            "ordered_date",
            "deadline_date",
            "visit_date",
            "raw_detail_file",
            "raw_detail_sha256",
            "raw_pdf_file",
            "raw_pdf_sha256",
            "registry_ids",
            "registry_titles",
            "entity",
            "join_status",
            "join_evidence",
            "candidate_penalty_links",
        },
        "education actions",
    )
    notice_index = {row["notice_key"]: row for row in notices}
    events = []
    for row in rows:
        notice = notice_index.get(row.get("notice_key", ""))
        if notice is None:
            raise ValueError(f"action has unknown notice parent: {row.get('notice_key')}")
        provided_ids = [
            str(value)
            for value in _json_list(row.get("registry_ids"), "action registry_ids")
        ]
        provided_titles = [
            str(value)
            for value in _json_list(row.get("registry_titles"), "action registry_titles")
        ]
        identity = identity_index.from_entity(
            row.get("entity", ""),
            identity_status=row.get("join_status", ""),
            identity_evidence=row.get("join_evidence", ""),
            provided_ids=provided_ids,
            provided_titles=provided_titles,
        )
        events.append(
            _base_event(
                event_id=f"corrective_action:{row['action_key']}",
                source_record_id=row["action_key"],
                event_family="corrective_action",
                event_type=row.get("action_type", ""),
                context=context,
                identity=identity,
                parent_event_id=f"education_notice:{row['notice_key']}",
                event_date=_iso_date(row.get("ordered_date"), "action ordered date"),
                event_date_semantics="corrective_order_date",
                published_date=_iso_date(
                    notice.get("publication_date"), "parent notice publication date"
                ),
                title=f"改善追蹤：{row.get('institution_source_title', '')}",
                summary="主管機關命改善並排定追蹤訪視；完成狀態未知",
                lifecycle_status=row.get("improvement_status", ""),
                outcome="",
                source_url=notice.get("source_url", ""),
                raw_artifacts_json=_artifacts(
                    (
                        f"{context.artifact_root}/{row.get('raw_detail_file', '')}",
                        row.get("raw_detail_sha256", ""),
                    ),
                    (
                        f"{context.artifact_root}/{row.get('raw_pdf_file', '')}",
                        row.get("raw_pdf_sha256", ""),
                    ),
                ),
                source_hashes={
                    "raw_detail_sha256": row.get("raw_detail_sha256", ""),
                    "raw_pdf_sha256": row.get("raw_pdf_sha256", ""),
                },
                details={
                    "notice_id": row.get("notice_id", ""),
                    "institution_source_title": row.get(
                        "institution_source_title", ""
                    ),
                    "institution_source_town": row.get("institution_source_town", ""),
                    "institution_source_type": row.get("institution_source_type", ""),
                    "deadline_date": _iso_date(
                        row.get("deadline_date"), "action deadline date"
                    ),
                    "visit_date": _iso_date(row.get("visit_date"), "action visit date"),
                    "source_language": row.get("source_language", ""),
                    "source_evidence": row.get("source_evidence", ""),
                    "pdf_page": row.get("pdf_page", ""),
                    "pdf_row": row.get("pdf_row", ""),
                    "candidate_penalty_links": _json_list(
                        row.get("candidate_penalty_links"),
                        "candidate penalty links",
                    ),
                },
            )
        )
    return events


def _procurement_events(
    rows: list[dict[str, str]], identity_index: IdentityIndex, context: SourceContext
) -> list[dict[str, str]]:
    _require_fields(
        rows,
        {
            "entity",
            "award_key",
            "supplier_award_key",
            "unit_id",
            "unit_name",
            "job_number",
            "tender_title",
            "award_notice_type",
            "award_notice_date",
            "award_decision_date",
            "supplier_id",
            "supplier_name",
            "award_amount",
            "total_award_amount",
            "contract_start_date",
            "contract_end_date",
            "official_notice_url",
            "notice_revisions",
            "search_raw_file",
            "search_response_sha256",
            "tender_raw_file",
            "tender_response_sha256",
            "report_key",
            "academic_year",
            "registry_ids",
            "registry_titles",
            "procurement_contract_covers_year",
            "temporal_join_status",
        },
        "procurement contracts",
    )
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        grouped[row.get("supplier_award_key", "")].append(row)
    if "" in grouped:
        raise ValueError("procurement row has a blank supplier_award_key")

    invariant_fields = {
        "entity",
        "award_key",
        "supplier_award_key",
        "unit_id",
        "unit_name",
        "job_number",
        "tender_title",
        "award_notice_type",
        "award_notice_date",
        "award_decision_date",
        "supplier_id",
        "supplier_name",
        "award_amount",
        "total_award_amount",
        "contract_start_date",
        "contract_end_date",
        "official_notice_url",
        "notice_revisions",
        "search_raw_file",
        "search_response_sha256",
        "tender_raw_file",
        "tender_response_sha256",
        "registry_ids",
        "registry_titles",
    }
    events = []
    for supplier_award_key, award_rows in grouped.items():
        first = award_rows[0]
        for field in invariant_fields:
            if len({row.get(field, "") for row in award_rows}) != 1:
                raise ValueError(
                    f"procurement award {supplier_award_key} varies in {field}"
                )
        identity = identity_index.from_entity(
            first.get("entity", ""),
            identity_status="matched_crosswalk_entity",
            identity_evidence="all report-year projections share one crosswalk entity",
            provided_ids=[
                str(value)
                for value in _json_list(first["registry_ids"], "procurement registry_ids")
            ],
            provided_titles=[
                str(value)
                for value in _json_list(
                    first["registry_titles"], "procurement registry_titles"
                )
            ],
        )
        report_time_joins = sorted(
            [
                {
                    "report_key": row.get("report_key", ""),
                    "academic_year": row.get("academic_year", ""),
                    "coverage": row.get("procurement_contract_covers_year", ""),
                    "temporal_join_status": row.get("temporal_join_status", ""),
                }
                for row in award_rows
            ],
            key=lambda item: item["report_key"],
        )
        events.append(
            _base_event(
                event_id=f"procurement_award:{supplier_award_key}",
                source_record_id=supplier_award_key,
                event_family="procurement_award",
                event_type="operating_contract_award",
                context=context,
                identity=identity,
                event_date=_iso_date(
                    first.get("award_decision_date"), "award decision date"
                ),
                event_date_semantics="award_decision_date",
                period_start=_iso_date(
                    first.get("contract_start_date"), "contract start date"
                ),
                period_end=_iso_date(
                    first.get("contract_end_date"), "contract end date"
                ),
                published_date=_iso_date(
                    first.get("award_notice_date"), "award notice date"
                ),
                title=first.get("tender_title", ""),
                summary=f"決標予 {first.get('supplier_name', '')}",
                lifecycle_status="awarded",
                outcome=first.get("award_notice_type", ""),
                amount=first.get("award_amount", ""),
                source_url=first.get("official_notice_url", ""),
                raw_artifacts_json=_artifacts(
                    (
                        f"{context.artifact_root}/{first.get('search_raw_file', '')}",
                        first.get("search_response_sha256", ""),
                    ),
                    (
                        f"{context.artifact_root}/{first.get('tender_raw_file', '')}",
                        first.get("tender_response_sha256", ""),
                    ),
                ),
                source_hashes={
                    "search_response_sha256": first.get(
                        "search_response_sha256", ""
                    ),
                    "tender_response_sha256": first.get(
                        "tender_response_sha256", ""
                    ),
                },
                details={
                    "award_key": first.get("award_key", ""),
                    "unit_id": first.get("unit_id", ""),
                    "unit_name": first.get("unit_name", ""),
                    "job_number": first.get("job_number", ""),
                    "supplier_id": first.get("supplier_id", ""),
                    "supplier_name": first.get("supplier_name", ""),
                    "total_award_amount": first.get("total_award_amount", ""),
                    "contract_period_raw": first.get("contract_period_raw", ""),
                    "notice_revisions": _json_list(
                        first.get("notice_revisions"), "notice revisions"
                    ),
                    "report_time_joins": report_time_joins,
                },
            )
        )
    return events


def build_official_events(
    *,
    institutions: list[dict[str, str]],
    penalties: list[dict[str, str]],
    evaluations: list[dict[str, str]],
    notices: list[dict[str, str]],
    actions: list[dict[str, str]],
    procurement: list[dict[str, str]],
    contexts: dict[str, SourceContext],
    penalty_observed_at_by_record_hash: dict[str, str],
) -> list[dict[str, str]]:
    """Build and validate the fixed v1 official-event projection."""
    expected_contexts = {"penalty", "evaluation", "education", "procurement"}
    if set(contexts) != expected_contexts:
        raise ValueError(
            f"event source contexts differ: {sorted(set(contexts) ^ expected_contexts)}"
        )
    identity_index = IdentityIndex(institutions)
    events = [
        *_penalty_events(
            penalties,
            identity_index,
            contexts["penalty"],
            penalty_observed_at_by_record_hash,
        ),
        *_evaluation_events(evaluations, identity_index, contexts["evaluation"]),
        *_notice_events(notices, contexts["education"]),
        *_action_events(actions, notices, identity_index, contexts["education"]),
        *_procurement_events(procurement, identity_index, contexts["procurement"]),
    ]
    counts = Counter(event["event_family"] for event in events)
    expected_counts = {
        **EXPECTED_FAMILY_COUNTS,
        "education_notice": len(notices),
    }
    if dict(counts) != expected_counts:
        raise ValueError(
            f"official event family counts changed: {dict(counts)} != "
            f"{expected_counts}"
        )
    notice_keys = {row.get("notice_key", "") for row in notices}
    if not REQUIRED_EDUCATION_NOTICE_KEYS.issubset(notice_keys):
        raise ValueError("historical education notice baseline is incomplete")
    event_ids = [event["event_id"] for event in events]
    if len(event_ids) != len(set(event_ids)):
        duplicates = sorted(
            event_id for event_id, count in Counter(event_ids).items() if count > 1
        )
        raise ValueError(f"official event IDs are not unique: {duplicates[:5]}")
    notice_ids = {
        event["event_id"]
        for event in events
        if event["event_family"] == "education_notice"
    }
    for event in events:
        if event["event_family"] == "corrective_action":
            if event["parent_event_id"] not in notice_ids:
                raise ValueError(
                    f"corrective action has an unknown parent: {event['event_id']}"
                )
            if event["lifecycle_status"] != "ordered" or event["outcome"]:
                raise ValueError("corrective action overstates completion semantics")
        if event["event_family"] != "penalty" and event["severity"]:
            raise ValueError("v1 cross-source severity must remain penalty-only")
        for field in ("event_date", "period_start", "period_end", "published_date"):
            _iso_date(event[field], f"{event['event_id']} {field}")
    return sorted(
        events,
        key=lambda event: (
            event["event_date"] or "9999-12-31",
            event["event_family"],
            event["event_id"],
        ),
    )
