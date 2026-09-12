"""Acquire and rebuild a bounded New Taipei education-announcement pilot.

Only five immutable official detail URLs are requested.  The two follow-up
notices additionally download their first official ``Action=downloadfile`` PDF.
Raw evidence and identity inputs are pinned before deterministic JSON/CSV outputs
are published.

Run::

    PYTHONPATH=src .venv/bin/python scripts/download_education_bureau_pilot.py \
        --snapshot-id ntpc-pilot-v1
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import pathlib
import re
import shutil
import sys
import tempfile
import urllib.parse
from collections import Counter
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape import registry
from smart_watchdog.scrape.announcements import (
    DEFAULT_DEADLINE_SECONDS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    IMPROVEMENT_STATUSES,
    OFFICIAL_ORIGIN,
    AnnouncementClient,
    ResponseArtifact,
    parse_followup_schedule_pdf,
    parse_notice_detail,
)

SNAPSHOT_ROOT = pathlib.Path("data/external/education_bureau_announcements")
NOTICES_PROCESSED_PATH = pathlib.Path(
    "data/processed/education_bureau_notices_ntpc.csv"
)
ACTIONS_PROCESSED_PATH = pathlib.Path(
    "data/processed/education_bureau_actions_ntpc.csv"
)
INSTITUTIONS_PATH = pathlib.Path("data/processed/institutions_ntpc.csv")
PENALTIES_PATH = pathlib.Path("data/processed/penalties_ntpc.csv")
PARSER_SCHEMA_VERSION = "ntpc-education-announcements-v1"
SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
EXPECTED_REQUESTS = 7
EXPECTED_NOTICES = 5
EXPECTED_ACTIONS = 98
PDF_PAGE_CAP = 20
ABSENCE_SEMANTICS = (
    "This five-notice bounded pilot is not a population census. Absence of an action, "
    "penalty candidate, or notice is never evidence of compliance, completion, or low risk."
)

PILOT_NOTICES = (
    {
        "id": "6097",
        "classification": "corrective_order_followup",
        "expected_actions": 9,
        "download_first_pdf": True,
        "title_marker": "103學年度私立幼兒園追蹤評鑑",
        "ordered_date": "2016-05-11",
        "visit_year": 2016,
    },
    {
        "id": "6098",
        "classification": "corrective_order_followup",
        "expected_actions": 89,
        "download_first_pdf": True,
        "title_marker": "104學年度公私立幼兒園追蹤評鑑",
        "ordered_date": "2016-05-03",
        "visit_year": 2016,
    },
    {
        "id": "7077",
        "classification": "policy_revision",
        "expected_actions": 0,
        "download_first_pdf": False,
        "title_marker": "基礎評鑑暨追蹤評鑑實施計畫",
        "ordered_date": None,
        "visit_year": None,
    },
    {
        "id": "15015",
        "classification": "policy_eligibility_negative_control",
        "expected_actions": 0,
        "download_first_pdf": False,
        "title_marker": "準公共教保服務機構申請及審核作業",
        "ordered_date": None,
        "visit_year": None,
    },
    {
        "id": "6787",
        "classification": "neutral_enrollment_negative_control",
        "expected_actions": 0,
        "download_first_pdf": False,
        "title_marker": "昌平非營利幼兒園招生",
        "ordered_date": None,
        "visit_year": None,
    },
)
PILOT_BY_ID = {str(spec["id"]): spec for spec in PILOT_NOTICES}

NOTICE_FIELDS = [
    "notice_id",
    "notice_key",
    "source_url",
    "title",
    "publication_date",
    "classification",
    "body_text",
    "body_sha256",
    "attachments_json",
    "raw_detail_file",
    "raw_detail_sha256",
    "action_count",
    "full_population_claim",
    "absence_semantics",
]
ACTION_FIELDS = [
    "action_key",
    "notice_id",
    "notice_key",
    "institution_source_title",
    "institution_source_town",
    "institution_source_type",
    "institution_row_index",
    "action_type",
    "improvement_status",
    "ordered_date",
    "deadline_date",
    "visit_date",
    "source_language",
    "source_evidence",
    "raw_detail_file",
    "raw_detail_sha256",
    "raw_pdf_file",
    "raw_pdf_sha256",
    "pdf_page",
    "pdf_row",
    "registry_ids",
    "registry_titles",
    "entity",
    "join_status",
    "join_evidence",
    "candidate_penalty_links",
]


def _detail_url(notice_id: str) -> str:
    return f"{OFFICIAL_ORIGIN}/p/404-1000-{notice_id}.php"


def _require_official_url(url: object, *, label: str) -> str:
    value = str(url or "")
    expected = urllib.parse.urlparse(OFFICIAL_ORIGIN)
    parsed = urllib.parse.urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != expected.hostname
        or parsed.port not in (None, 443)
    ):
        raise ValueError(f"{label} is outside the official HTTPS origin: {value!r}")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _csv_rows_from_bytes(data: bytes, *, label: str) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} input must be UTF-8") from exc
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    if not rows:
        raise ValueError(f"{label} input has no rows")
    return rows


def _input_context() -> tuple[
    bytes,
    list[dict[str, str]],
    bytes,
    list[dict[str, str]],
]:
    institutions_data = INSTITUTIONS_PATH.read_bytes()
    penalties_data = PENALTIES_PATH.read_bytes()
    institutions = _csv_rows_from_bytes(institutions_data, label="institutions")
    penalties = _csv_rows_from_bytes(penalties_data, label="penalties")
    required_institution_fields = {"id", "title", "entity", "type", "town"}
    required_penalty_fields = {
        "id",
        "penalty_group_id",
        "group_record_index",
        "group_record_count",
    }
    if not required_institution_fields.issubset(institutions[0]):
        raise ValueError("institutions input lacks identity fields")
    if not required_penalty_fields.issubset(penalties[0]):
        raise ValueError("penalties input lacks candidate-link fields")
    return institutions_data, institutions, penalties_data, penalties


def _name_core(value: object) -> str:
    text = registry.entity_key("".join(str(value or "").split()))
    text = text.removeprefix("新北市")
    for prefix in ("私立", "市立", "公立"):
        text = text.removeprefix(prefix)
    return text


def _identity_join(
    source_title: str,
    source_town: str,
    institutions: list[dict[str, str]],
) -> dict[str, object]:
    exact = [row for row in institutions if row.get("title") == source_title]
    source_entity = registry.entity_key(source_title)
    entity_candidates = [
        row for row in institutions if row.get("entity") == source_entity
    ]
    core = _name_core(source_title)
    core_candidates = [
        row
        for row in institutions
        if core
        and _name_core(row.get("title")) == core
        and (not source_town or row.get("town") == source_town)
    ]

    if len(exact) == 1:
        selected = exact
        status = "matched_exact_title"
    elif entity_candidates:
        selected = entity_candidates
        status = "matched_entity"
    else:
        core_entities = {row.get("entity", "") for row in core_candidates}
        if core_candidates and len(core_entities) == 1:
            selected = core_candidates
            status = "matched_unique_name_core"
        elif core_candidates:
            selected = []
            status = "unresolved_ambiguous_name_core"
        else:
            selected = []
            status = "unresolved_historical_institution"

    entity_values = sorted({row.get("entity", "") for row in selected if row.get("entity")})
    entity = entity_values[0] if len(entity_values) == 1 else ""
    siblings = sorted(
        [row for row in institutions if entity and row.get("entity") == entity],
        key=lambda row: row.get("id", ""),
    )
    if not siblings:
        siblings = sorted(selected, key=lambda row: row.get("id", ""))
    evidence = ";".join(
        [
            f"exact_title:{len(exact)}",
            f"entity_candidates:{len(entity_candidates)}",
            f"name_core_candidates:{len(core_candidates)}",
            f"sibling_registry_ids:{len(siblings)}",
        ]
    )
    return {
        "registry_ids": [row.get("id", "") for row in siblings],
        "registry_titles": [row.get("title", "") for row in siblings],
        "entity": entity,
        "join_status": status,
        "join_evidence": evidence,
    }


def _penalty_candidates(
    registry_ids: list[object], penalties: list[dict[str, str]]
) -> list[dict[str, object]]:
    identifiers = {str(value) for value in registry_ids if value}
    candidates = [row for row in penalties if row.get("id") in identifiers]
    candidates.sort(
        key=lambda row: (
            row.get("penalty_group_id", ""),
            int(row.get("group_record_index") or 0),
            row.get("date", ""),
            row.get("law", ""),
        )
    )
    return [
        {
            "penalty_group_id": row.get("penalty_group_id", ""),
            "group_record_index": int(row.get("group_record_index") or 0),
            "group_record_count": int(row.get("group_record_count") or 0),
            "penalty_registry_id": row.get("id", ""),
            "basis": "shared sibling registry UUID only",
            "relationship": "candidate only; not asserted to be the same event",
        }
        for row in candidates
    ]


def _source_language(body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    evidence = [
        line for line in lines if "限期改善" in line or "實地訪視" in line
    ]
    return "\n".join(evidence)


def _action_key(notice_id: str, row: dict[str, object]) -> str:
    identity = "|".join(
        [
            notice_id,
            str(row.get("institution_row_index", "")),
            str(row.get("institution_source_title", "")),
            str(row.get("visit_date") or ""),
            str(row.get("pdf_page", "")),
            str(row.get("pdf_row", "")),
        ]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"ntpc-kidedu-{notice_id}-action-{digest}"


def _verified_path(snapshot: pathlib.Path, relative: object) -> pathlib.Path:
    root = snapshot.resolve()
    path = (snapshot / str(relative)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("manifest path escapes announcement snapshot") from exc
    return path


def _request_entry(
    artifact: ResponseArtifact,
    *,
    role: str,
    notice_id: str,
    raw_file: str,
    attachment_order: int | None = None,
) -> dict[str, object]:
    return {
        "sequence": artifact.sequence,
        "role": role,
        "method": "GET",
        "notice_id": notice_id,
        "attachment_order": attachment_order,
        "source_url": artifact.source_url,
        "final_url": artifact.final_url,
        "response_sha256": artifact.sha256,
        "byte_length": len(artifact.body),
        "status": artifact.status,
        "content_type": artifact.content_type,
        "charset": artifact.charset,
        "retrieved_at_utc": artifact.retrieved_at_utc,
        "raw_file": raw_file,
        "parser_schema_version": PARSER_SCHEMA_VERSION,
    }


def _derive_records(
    snapshot: pathlib.Path,
    requests: list[dict[str, object]],
    institutions: list[dict[str, str]],
    penalties: list[dict[str, str]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    by_notice: dict[str, dict[str, dict[str, object]]] = {}
    for entry in requests:
        notice_id = str(entry.get("notice_id", ""))
        if notice_id not in PILOT_BY_ID:
            raise ValueError(f"unknown announcement notice ID: {notice_id!r}")
        role = str(entry.get("role", ""))
        if role not in {"detail", "attachment_pdf"}:
            raise ValueError(f"unsupported announcement request role: {role!r}")
        roles = by_notice.setdefault(notice_id, {})
        if role in roles:
            raise ValueError(f"duplicate {role} request for notice {notice_id}")
        roles[role] = entry

    notices: list[dict[str, object]] = []
    actions: list[dict[str, object]] = []
    for spec in PILOT_NOTICES:
        notice_id = str(spec["id"])
        roles = by_notice.get(notice_id, {})
        detail_entry = roles.get("detail")
        if detail_entry is None:
            raise ValueError(f"missing detail response for notice {notice_id}")
        expected_detail_url = _detail_url(notice_id)
        if detail_entry.get("source_url") != expected_detail_url:
            raise ValueError(f"notice {notice_id} raw detail is not bound to its fixed URL")
        _require_official_url(detail_entry.get("final_url"), label="detail final URL")
        detail_path = _verified_path(snapshot, detail_entry["raw_file"])
        detail = parse_notice_detail(detail_path.read_bytes(), expected_detail_url)
        if str(spec["title_marker"]) not in str(detail["title"]):
            raise ValueError(f"notice {notice_id} title no longer matches pilot contract")

        attachment_records = [dict(item) for item in detail["attachments"]]
        notice_actions: list[dict[str, object]] = []
        if spec["download_first_pdf"]:
            pdf_entry = roles.get("attachment_pdf")
            if pdf_entry is None:
                raise ValueError(f"missing scheduled PDF response for notice {notice_id}")
            if pdf_entry.get("attachment_order") != 1:
                raise ValueError(f"notice {notice_id} did not pin its first attachment")
            if not attachment_records:
                raise ValueError(f"notice {notice_id} no longer has a download attachment")
            expected_pdf_url = str(attachment_records[0]["url"])
            _require_official_url(expected_pdf_url, label="first attachment URL")
            if pdf_entry.get("source_url") != expected_pdf_url:
                raise ValueError(
                    f"notice {notice_id} PDF is not bound to its first attachment URL"
                )
            _require_official_url(pdf_entry.get("final_url"), label="PDF final URL")
            pdf_path = _verified_path(snapshot, pdf_entry["raw_file"])
            parsed_rows = parse_followup_schedule_pdf(
                pdf_path.read_bytes(),
                visit_year=int(spec["visit_year"]),
                page_cap=PDF_PAGE_CAP,
            )
            attachment_records[0].update(
                {
                    "downloaded": True,
                    "raw_file": str(pdf_entry["raw_file"]),
                    "response_sha256": str(pdf_entry["response_sha256"]),
                    "byte_length": int(pdf_entry["byte_length"]),
                }
            )
            evidence = _source_language(str(detail["body_text"]))
            if "限期改善" not in evidence or "實地訪視" not in evidence:
                raise ValueError(f"notice {notice_id} lacks explicit follow-up evidence")
            for parsed_row in parsed_rows:
                identity = _identity_join(
                    str(parsed_row["institution_source_title"]),
                    str(parsed_row["institution_source_town"]),
                    institutions,
                )
                registry_ids = list(identity["registry_ids"])
                action = {
                    "notice_id": notice_id,
                    "notice_key": f"ntpc-kidedu-{notice_id}",
                    **parsed_row,
                    "action_type": "corrective_order_followup",
                    "improvement_status": "ordered",
                    "ordered_date": spec["ordered_date"],
                    "deadline_date": None,
                    "source_language": evidence,
                    "source_evidence": str(parsed_row["source_row_text"]),
                    "raw_detail_file": str(detail_entry["raw_file"]),
                    "raw_detail_sha256": str(detail_entry["response_sha256"]),
                    "raw_pdf_file": str(pdf_entry["raw_file"]),
                    "raw_pdf_sha256": str(pdf_entry["response_sha256"]),
                    **identity,
                    "candidate_penalty_links": _penalty_candidates(
                        registry_ids, penalties
                    ),
                }
                action.pop("source_row_text", None)
                action["action_key"] = _action_key(notice_id, action)
                notice_actions.append(action)
        elif "attachment_pdf" in roles:
            raise ValueError(
                f"negative/policy control {notice_id} unexpectedly has a PDF request"
            )

        for attachment in attachment_records:
            attachment.setdefault("downloaded", False)
        notices.append(
            {
                "notice_id": notice_id,
                "notice_key": f"ntpc-kidedu-{notice_id}",
                "source_url": _detail_url(notice_id),
                "title": detail["title"],
                "publication_date": detail["publication_date"],
                "classification": spec["classification"],
                "body_text": detail["body_text"],
                "body_sha256": detail["body_sha256"],
                "attachments": attachment_records,
                "raw_detail_file": str(detail_entry["raw_file"]),
                "raw_detail_sha256": str(detail_entry["response_sha256"]),
                "action_count": len(notice_actions),
                "full_population_claim": False,
                "absence_semantics": ABSENCE_SEMANTICS,
            }
        )
        actions.extend(notice_actions)
    _validate_invariants(requests, notices, actions)
    return notices, actions


def _validate_invariants(
    requests: list[dict[str, object]],
    notices: list[dict[str, object]],
    actions: list[dict[str, object]],
) -> None:
    expected_ids = [str(spec["id"]) for spec in PILOT_NOTICES]
    if len(requests) != EXPECTED_REQUESTS:
        raise ValueError(
            f"announcement request invariant changed: {len(requests)} != {EXPECTED_REQUESTS}"
        )
    actual_ids = [row["notice_id"] for row in notices]
    if len(notices) != EXPECTED_NOTICES or actual_ids != expected_ids:
        raise ValueError("announcement notice ID/order invariant changed")
    expected_classes = {
        str(spec["id"]): str(spec["classification"]) for spec in PILOT_NOTICES
    }
    actual_classes = {str(row["notice_id"]): str(row["classification"]) for row in notices}
    if actual_classes != expected_classes:
        raise ValueError("announcement classification invariant changed")
    expected_counts = {
        str(spec["id"]): int(spec["expected_actions"]) for spec in PILOT_NOTICES
    }
    actual_counts = {str(row["notice_id"]): int(row["action_count"]) for row in notices}
    if actual_counts != expected_counts or len(actions) != EXPECTED_ACTIONS:
        raise ValueError(
            f"announcement action invariant changed: {actual_counts}, total={len(actions)}"
        )
    action_notice_ids = {str(row["notice_id"]) for row in actions}
    if action_notice_ids != {"6097", "6098"}:
        raise ValueError("policy or negative-control notice produced an institution action")
    if any(row.get("action_type") != "corrective_order_followup" for row in actions):
        raise ValueError("unexpected education-bureau action type")
    statuses = {str(row.get("improvement_status", "")) for row in actions}
    if not statuses.issubset(IMPROVEMENT_STATUSES) or statuses != {"ordered"}:
        raise ValueError(f"unexpected improvement statuses: {sorted(statuses)}")
    if any(str(row.get("visit_date", ""))[:4] != "2016" for row in actions):
        raise ValueError("follow-up visit year differs from fixed 2016 pilot contract")


def _notice_csv_rows(notices: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        {
            "notice_id": row["notice_id"],
            "notice_key": row["notice_key"],
            "source_url": row["source_url"],
            "title": row["title"],
            "publication_date": row["publication_date"] or "",
            "classification": row["classification"],
            "body_text": row["body_text"],
            "body_sha256": row["body_sha256"],
            "attachments_json": _json_value(row["attachments"]),
            "raw_detail_file": row["raw_detail_file"],
            "raw_detail_sha256": row["raw_detail_sha256"],
            "action_count": row["action_count"],
            "full_population_claim": "false",
            "absence_semantics": row["absence_semantics"],
        }
        for row in notices
    ]


def _action_csv_rows(actions: list[dict[str, object]]) -> list[dict[str, object]]:
    json_fields = {"registry_ids", "registry_titles", "candidate_penalty_links"}
    return [
        {
            field: (
                _json_value(row[field])
                if field in json_fields
                else row.get(field) or ""
            )
            for field in ACTION_FIELDS
        }
        for row in actions
    ]


def _csv_bytes(rows: list[dict[str, object]], fields: list[str]) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue().encode("utf-8")


def _output_bytes(
    notices: list[dict[str, object]], actions: list[dict[str, object]]
) -> tuple[bytes, bytes]:
    return (
        _csv_bytes(_notice_csv_rows(notices), NOTICE_FIELDS),
        _csv_bytes(_action_csv_rows(actions), ACTION_FIELDS),
    )


def _replace_file(path: pathlib.Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _publish_outputs(notices_data: bytes, actions_data: bytes) -> None:
    NOTICES_PROCESSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in (NOTICES_PROCESSED_PATH, ACTIONS_PROCESSED_PATH)
    }
    try:
        _replace_file(NOTICES_PROCESSED_PATH, notices_data)
        _replace_file(ACTIONS_PROCESSED_PATH, actions_data)
    except BaseException:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _replace_file(path, original)
        raise


def _persist_response(
    stage: pathlib.Path,
    artifact: ResponseArtifact,
    *,
    role: str,
    notice_id: str,
    extension: str,
    requests: list[dict[str, object]],
    attachment_order: int | None = None,
) -> dict[str, object]:
    raw_file = f"raw/{artifact.sequence:03d}_{notice_id}_{role}.{extension}"
    (stage / raw_file).write_bytes(artifact.body)
    entry = _request_entry(
        artifact,
        role=role,
        notice_id=notice_id,
        raw_file=raw_file,
        attachment_order=attachment_order,
    )
    requests.append(entry)
    return entry


def acquire(snapshot_id: str, timeout: int) -> None:
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id) or snapshot_id in {".", ".."}:
        raise ValueError("snapshot ID may contain only letters, digits, dot, dash, underscore")
    target = SNAPSHOT_ROOT / snapshot_id
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing snapshot: {target}")

    institutions_data, institutions, penalties_data, penalties = _input_context()
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    client = AnnouncementClient(timeout=timeout)
    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    requests: list[dict[str, object]] = []

    temporary_prefix = "education-announcements-"
    with tempfile.TemporaryDirectory(prefix=temporary_prefix, dir=SNAPSHOT_ROOT) as tmp:
        stage = pathlib.Path(tmp)
        (stage / "raw").mkdir()
        (stage / "parsed").mkdir()
        (stage / "inputs").mkdir()
        (stage / "inputs" / INSTITUTIONS_PATH.name).write_bytes(institutions_data)
        (stage / "inputs" / PENALTIES_PATH.name).write_bytes(penalties_data)

        for spec in PILOT_NOTICES:
            notice_id = str(spec["id"])
            detail_artifact = client.get(_detail_url(notice_id))
            if detail_artifact.content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError(
                    f"notice {notice_id} returned {detail_artifact.content_type!r}, not HTML"
                )
            _persist_response(
                stage,
                detail_artifact,
                role="detail",
                notice_id=notice_id,
                extension="html",
                requests=requests,
            )
            detail = parse_notice_detail(detail_artifact.body, _detail_url(notice_id))
            if spec["download_first_pdf"]:
                attachments = list(detail["attachments"])
                if not attachments:
                    raise ValueError(
                        f"notice {notice_id} has no Action=downloadfile attachment"
                    )
                first = attachments[0]
                pdf_artifact = client.get(str(first["url"]))
                if not pdf_artifact.body.startswith(b"%PDF"):
                    raise ValueError(f"notice {notice_id} first download is not a PDF")
                _persist_response(
                    stage,
                    pdf_artifact,
                    role="attachment_pdf",
                    notice_id=notice_id,
                    extension="pdf",
                    requests=requests,
                    attachment_order=1,
                )

        if client.request_count != EXPECTED_REQUESTS or len(requests) != EXPECTED_REQUESTS:
            raise ValueError("announcement acquisition did not use exactly seven requests")
        notices, actions = _derive_records(stage, requests, institutions, penalties)
        notices_payload = {
            "schema_version": PARSER_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "records": notices,
        }
        actions_payload = {
            "schema_version": PARSER_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "records": actions,
        }
        notices_json = _json_bytes(notices_payload)
        actions_json = _json_bytes(actions_payload)
        (stage / "parsed" / "notices.json").write_bytes(notices_json)
        (stage / "parsed" / "actions.json").write_bytes(actions_json)
        notices_csv, actions_csv = _output_bytes(notices, actions)
        (stage / NOTICES_PROCESSED_PATH.name).write_bytes(notices_csv)
        (stage / ACTIONS_PROCESSED_PATH.name).write_bytes(actions_csv)

        completed = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        classification_counts = Counter(str(row["classification"]) for row in notices)
        join_counts = Counter(str(row["join_status"]) for row in actions)
        inputs = {
            f"inputs/{INSTITUTIONS_PATH.name}": {
                "source_path": INSTITUTIONS_PATH.as_posix(),
                "sha256": _sha256(institutions_data),
                "byte_length": len(institutions_data),
                "record_count": len(institutions),
            },
            f"inputs/{PENALTIES_PATH.name}": {
                "source_path": PENALTIES_PATH.as_posix(),
                "sha256": _sha256(penalties_data),
                "byte_length": len(penalties_data),
                "record_count": len(penalties),
            },
        }
        outputs = {
            "parsed/notices.json": {
                "sha256": _sha256(notices_json),
                "byte_length": len(notices_json),
                "record_count": len(notices),
            },
            "parsed/actions.json": {
                "sha256": _sha256(actions_json),
                "byte_length": len(actions_json),
                "record_count": len(actions),
            },
            NOTICES_PROCESSED_PATH.as_posix(): {
                "snapshot_file": NOTICES_PROCESSED_PATH.name,
                "sha256": _sha256(notices_csv),
                "byte_length": len(notices_csv),
                "record_count": len(notices),
            },
            ACTIONS_PROCESSED_PATH.as_posix(): {
                "snapshot_file": ACTIONS_PROCESSED_PATH.name,
                "sha256": _sha256(actions_csv),
                "byte_length": len(actions_csv),
                "record_count": len(actions),
            },
        }
        manifest = {
            "manifest_version": 1,
            "snapshot_id": snapshot_id,
            "official_origin": OFFICIAL_ORIGIN,
            "started_at_utc": started.isoformat().replace("+00:00", "Z"),
            "completed_at_utc": completed.isoformat().replace("+00:00", "Z"),
            "tls_verification": "system trust store; no bypass",
            "session_cookies_persisted": False,
            "parser_schema_version": PARSER_SCHEMA_VERSION,
            "pilot_scope": {
                "notices": list(PILOT_NOTICES),
                "detail_url_pattern": f"{OFFICIAL_ORIGIN}/p/404-1000-ID.php",
                "full_population_claim": False,
                "absence_semantics": ABSENCE_SEMANTICS,
                "request_cap": EXPECTED_REQUESTS,
                "pdf_page_cap": PDF_PAGE_CAP,
            },
            "budgets": {
                "request_cap": EXPECTED_REQUESTS,
                "requests_used": client.request_count,
                "response_byte_cap": DEFAULT_MAX_RESPONSE_BYTES,
                "aggregate_byte_cap": DEFAULT_MAX_TOTAL_BYTES,
                "response_bytes_used": client.total_bytes,
                "deadline_seconds": DEFAULT_DEADLINE_SECONDS,
            },
            "requests": requests,
            "inputs": inputs,
            "outputs": outputs,
            "classification_summary": dict(sorted(classification_counts.items())),
            "join_summary": dict(sorted(join_counts.items())),
        }
        (stage / "manifest.json").write_bytes(_json_bytes(manifest))

        lock = target.with_name(f"{target.name}.lock")
        lock_acquired = False
        published = False
        try:
            lock.mkdir()
            lock_acquired = True
            if target.exists():
                raise FileExistsError(f"refusing to overwrite existing snapshot: {target}")
            stage.replace(target)
            published = True
            _publish_outputs(notices_csv, actions_csv)
        except BaseException:
            if published:
                shutil.rmtree(target)
            raise
        finally:
            if lock_acquired:
                lock.rmdir()

    print(
        f"wrote {target} ({len(requests)} responses, {len(notices)} notices, "
        f"{len(actions)} actions)"
    )
    print(f"wrote {NOTICES_PROCESSED_PATH} and {ACTIONS_PROCESSED_PATH}")


def _verify_blob(path: pathlib.Path, metadata: dict[str, object], label: str) -> bytes:
    data = path.read_bytes()
    expected_hash = metadata.get("sha256") or metadata.get("response_sha256")
    if (
        not expected_hash
        or len(data) != metadata.get("byte_length")
        or _sha256(data) != expected_hash
    ):
        raise ValueError(f"{label} integrity mismatch: {path}")
    return data


def _manifest_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"announcement manifest {label} must be an object")
    return value


def rebuild_from_snapshot(snapshot_id: str, *, publish: bool = True) -> None:
    """Verify every pinned layer and byte-identically derive both CSV outputs."""
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id) or snapshot_id in {".", ".."}:
        raise ValueError("invalid snapshot ID")
    snapshot = SNAPSHOT_ROOT / snapshot_id
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("manifest_version") != 1:
        raise ValueError("unsupported announcement snapshot manifest version")
    if manifest.get("parser_schema_version") != PARSER_SCHEMA_VERSION:
        raise ValueError("unsupported announcement parser schema version")
    if manifest.get("snapshot_id") != snapshot_id:
        raise ValueError("snapshot directory and manifest ID differ")
    if manifest.get("official_origin") != OFFICIAL_ORIGIN:
        raise ValueError("snapshot origin is not the allowlisted education host")
    scope = _manifest_object(manifest.get("pilot_scope"), "pilot_scope")
    if scope.get("notices") != list(PILOT_NOTICES):
        raise ValueError("snapshot bounded notice allowlist differs")
    if scope.get("full_population_claim") is not False:
        raise ValueError("snapshot makes an unsupported full-population claim")

    request_entries = manifest.get("requests")
    if not isinstance(request_entries, list) or len(request_entries) != EXPECTED_REQUESTS:
        raise ValueError("snapshot must contain exactly seven request entries")
    for entry in request_entries:
        if not isinstance(entry, dict):
            raise ValueError("snapshot request entry must be an object")
        if entry.get("method") != "GET":
            raise ValueError("announcement snapshot contains a non-GET request")
        if entry.get("parser_schema_version") != PARSER_SCHEMA_VERSION:
            raise ValueError("announcement request parser schema differs")
        _verify_blob(
            _verified_path(snapshot, entry.get("raw_file")),
            entry,
            "raw announcement response",
        )

    inputs_meta = _manifest_object(manifest.get("inputs"), "inputs")
    institution_meta = _manifest_object(
        inputs_meta.get(f"inputs/{INSTITUTIONS_PATH.name}"), "institutions input"
    )
    penalty_meta = _manifest_object(
        inputs_meta.get(f"inputs/{PENALTIES_PATH.name}"), "penalties input"
    )
    institutions_data = _verify_blob(
        _verified_path(snapshot, f"inputs/{INSTITUTIONS_PATH.name}"),
        institution_meta,
        "pinned institutions",
    )
    penalties_data = _verify_blob(
        _verified_path(snapshot, f"inputs/{PENALTIES_PATH.name}"),
        penalty_meta,
        "pinned penalties",
    )
    institutions = _csv_rows_from_bytes(institutions_data, label="pinned institutions")
    penalties = _csv_rows_from_bytes(penalties_data, label="pinned penalties")
    if len(institutions) != institution_meta.get("record_count"):
        raise ValueError("pinned institution record count differs")
    if len(penalties) != penalty_meta.get("record_count"):
        raise ValueError("pinned penalty record count differs")

    outputs = _manifest_object(manifest.get("outputs"), "outputs")
    notices_meta = _manifest_object(outputs.get("parsed/notices.json"), "notices JSON")
    actions_meta = _manifest_object(outputs.get("parsed/actions.json"), "actions JSON")
    pinned_notices_json = _verify_blob(
        _verified_path(snapshot, "parsed/notices.json"), notices_meta, "parsed notices"
    )
    pinned_actions_json = _verify_blob(
        _verified_path(snapshot, "parsed/actions.json"), actions_meta, "parsed actions"
    )

    notices, actions = _derive_records(snapshot, request_entries, institutions, penalties)
    derived_notices_json = _json_bytes(
        {
            "schema_version": PARSER_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "records": notices,
        }
    )
    derived_actions_json = _json_bytes(
        {
            "schema_version": PARSER_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "records": actions,
        }
    )
    if derived_notices_json != pinned_notices_json:
        raise ValueError("parsed notices differ from pinned raw HTML/PDF derivation")
    if derived_actions_json != pinned_actions_json:
        raise ValueError("parsed actions differ from pinned raw HTML/PDF derivation")

    notices_csv, actions_csv = _output_bytes(notices, actions)
    notices_csv_meta = _manifest_object(
        outputs.get(NOTICES_PROCESSED_PATH.as_posix()), "notices CSV"
    )
    actions_csv_meta = _manifest_object(
        outputs.get(ACTIONS_PROCESSED_PATH.as_posix()), "actions CSV"
    )
    pinned_notices_csv = _verify_blob(
        _verified_path(snapshot, notices_csv_meta.get("snapshot_file")),
        notices_csv_meta,
        "snapshot notices CSV",
    )
    pinned_actions_csv = _verify_blob(
        _verified_path(snapshot, actions_csv_meta.get("snapshot_file")),
        actions_csv_meta,
        "snapshot actions CSV",
    )
    if notices_csv != pinned_notices_csv or actions_csv != pinned_actions_csv:
        raise ValueError("offline rebuild is not byte-identical to pinned CSV outputs")
    print(f"verified {len(request_entries)} pinned responses and raw derivation")
    if publish:
        _publish_outputs(notices_csv, actions_csv)
        print(
            f"rebuilt {NOTICES_PROCESSED_PATH} ({len(notices)} rows) and "
            f"{ACTIONS_PROCESSED_PATH} ({len(actions)} rows), no network"
        )
    else:
        live_notices = NOTICES_PROCESSED_PATH.read_bytes()
        live_actions = ACTIONS_PROCESSED_PATH.read_bytes()
        if live_notices != notices_csv or live_actions != actions_csv:
            raise ValueError("live announcement CSVs differ from raw-derived pinned outputs")
        print(
            f"verified live {NOTICES_PROCESSED_PATH} ({len(notices)} rows) and "
            f"{ACTIONS_PROCESSED_PATH} ({len(actions)} rows), no writes"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--snapshot-id", help="immutable ID for a new official snapshot")
    mode.add_argument(
        "--rebuild-from",
        metavar="SNAPSHOT_ID",
        help="verify a pinned snapshot and rebuild both CSVs without network",
    )
    mode.add_argument(
        "--verify-only",
        metavar="SNAPSHOT_ID",
        help="derive and compare pinned/live outputs without network or writes",
    )
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not args.snapshot_id and not args.rebuild_from and not args.verify_only:
        args.snapshot_id = f"{dt.date.today().isoformat()}-pilot-v1"
    return args


def main() -> None:
    args = parse_args()
    if args.verify_only:
        rebuild_from_snapshot(args.verify_only, publish=False)
    elif args.rebuild_from:
        rebuild_from_snapshot(args.rebuild_from)
    else:
        acquire(args.snapshot_id, args.timeout)


if __name__ == "__main__":
    main()
