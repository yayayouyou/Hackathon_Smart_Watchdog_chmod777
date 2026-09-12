"""Verify every pinned external-data artifact without network access.

Run::

    PYTHONPATH=src .venv/bin/python scripts/verify_external_artifacts.py
"""

from __future__ import annotations

import csv
import hashlib
import json
import pathlib
import subprocess
import sys
import urllib.parse
from collections import Counter
from typing import Any

from smart_watchdog.scrape.announcement_observations import (
    verify_listing_observations,
)
from smart_watchdog.scrape.observations import SOURCE_FILES, verify_observations

ROOT = pathlib.Path("data/external")
PROCESSED = pathlib.Path("data/processed")
ROOT_MANIFEST = ROOT / "manifest.json"
OBSERVATIONS = ROOT / "observations"
EVALUATION = ROOT / "evaluation/ntpc-pilot-v1"
PROCUREMENT = ROOT / "procurement/ntpc-pilot-v1"
ANNOUNCEMENTS = ROOT / "education_bureau_announcements/ntpc-pilot-v1"
ANNOUNCEMENT_LISTINGS = (
    ROOT / "education_bureau_announcements/listing_observations"
)
ANNOUNCEMENT_DISCOVERIES = (
    PROCESSED / "education_bureau_discovered_notices_ntpc.csv"
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _array(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return value


def _load_json(path: pathlib.Path) -> dict[str, Any]:
    return _object(json.loads(path.read_text(encoding="utf-8")), str(path))


def _load_csv(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"CSV has no records: {path}")
    return rows


def _safe_path(root: pathlib.Path, relative: object) -> pathlib.Path:
    resolved_root = root.resolve()
    path = (root / str(relative)).resolve()
    try:
        path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"path escapes snapshot: {relative!r}") from exc
    return path


def _verify_blob(path: pathlib.Path, metadata: dict[str, Any], label: str) -> bytes:
    data = path.read_bytes()
    if len(data) != metadata.get("byte_length"):
        raise ValueError(f"{label} byte length mismatch: {path}")
    if _sha256(data) != metadata.get("sha256"):
        raise ValueError(f"{label} SHA-256 mismatch: {path}")
    return data


def _require_fields(rows: list[dict[str, str]], fields: set[str], label: str) -> None:
    missing = fields - set(rows[0])
    if missing:
        raise ValueError(f"{label} is missing fields: {sorted(missing)}")


def _json_list(value: str, label: str) -> list[Any]:
    parsed = json.loads(value or "[]")
    return _array(parsed, label)


def _verify_root_snapshot() -> int:
    manifest = _load_json(ROOT_MANIFEST)
    files = _object(manifest.get("files"), "root manifest files")
    for filename, value in files.items():
        metadata = _object(value, f"root metadata {filename}")
        _verify_blob(ROOT / filename, metadata, f"root external file {filename}")
    return len(files)


def _verify_observation_history() -> tuple[int, str, int]:
    payloads = {filename: (ROOT / filename).read_bytes() for filename in SOURCE_FILES}
    summary = verify_observations(OBSERVATIONS, current_payloads=payloads)
    return (
        int(summary["observation_count"]),
        str(summary["latest_observation_id"]),
        int(summary["total_changes"]),
    )


def _verify_snapshot_files(snapshot: pathlib.Path) -> dict[str, Any]:
    manifest = _load_json(snapshot / "manifest.json")
    requests = _array(manifest.get("requests"), f"{snapshot} requests")
    for entry_value in requests:
        entry = _object(entry_value, f"{snapshot} request entry")
        _verify_blob(
            _safe_path(snapshot, entry.get("raw_file")),
            {
                "sha256": entry.get("response_sha256"),
                "byte_length": entry.get("byte_length"),
            },
            "raw response",
        )
    inputs = _object(manifest.get("inputs", {}), f"{snapshot} inputs")
    for relative, value in inputs.items():
        _verify_blob(
            _safe_path(snapshot, relative),
            _object(value, f"input metadata {relative}"),
            "pinned input",
        )
    return manifest


def _verify_output(
    snapshot: pathlib.Path,
    manifest: dict[str, Any],
    manifest_key: str,
    snapshot_file: str,
    live_file: pathlib.Path | None = None,
) -> bytes:
    outputs = _object(manifest.get("outputs"), f"{snapshot} outputs")
    metadata = _object(outputs.get(manifest_key), f"output metadata {manifest_key}")
    data = _verify_blob(snapshot / snapshot_file, metadata, "snapshot output")
    if live_file is not None and live_file.read_bytes() != data:
        raise ValueError(f"live processed output differs from snapshot: {live_file}")
    return data


def _identity_context() -> tuple[set[str], dict[str, list[str]], set[str]]:
    institutions = _load_csv(PROCESSED / "institutions_ntpc.csv")
    penalties = _load_csv(PROCESSED / "penalties_ntpc.csv")
    by_entity: dict[str, list[str]] = {}
    for row in institutions:
        identifier = row.get("id", "")
        entity = row.get("entity", "")
        if not identifier or not entity:
            raise ValueError("institution identity spine has a blank id/entity")
        by_entity.setdefault(entity, []).append(identifier)
    siblings = {entity: sorted(ids) for entity, ids in by_entity.items()}
    penalty_groups = {row.get("penalty_group_id", "") for row in penalties}
    return {row["id"] for row in institutions}, siblings, penalty_groups


def _verify_siblings(
    row: dict[str, str], registry_ids: set[str], siblings: dict[str, list[str]], label: str
) -> list[str]:
    identifiers = sorted(
        str(value) for value in _json_list(row.get("registry_ids", ""), label)
    )
    unknown = set(identifiers) - registry_ids
    if unknown:
        raise ValueError(f"{label} references unknown registry UUIDs: {sorted(unknown)}")
    entity = row.get("entity", "")
    if entity and identifiers != siblings.get(entity, []):
        raise ValueError(f"{label} omits or adds sibling UUIDs for {entity}")
    return identifiers


def _request_index(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    requests = _array(manifest.get("requests"), "snapshot requests")
    output: dict[str, dict[str, Any]] = {}
    for value in requests:
        entry = _object(value, "request entry")
        raw_file = str(entry.get("raw_file", ""))
        if not raw_file or raw_file in output:
            raise ValueError("request raw_file values must be non-empty and unique")
        output[raw_file] = entry
    return output


def _verify_evaluation(
    registry_ids: set[str], siblings: dict[str, list[str]]
) -> tuple[int, int]:
    manifest = _verify_snapshot_files(EVALUATION)
    _verify_output(
        EVALUATION,
        manifest,
        "parsed/evaluation_results.json",
        "parsed/evaluation_results.json",
    )
    _verify_output(
        EVALUATION,
        manifest,
        "data/processed/evaluations_ntpc.csv",
        "evaluations_ntpc.csv",
        PROCESSED / "evaluations_ntpc.csv",
    )
    rows = _load_csv(PROCESSED / "evaluations_ntpc.csv")
    _require_fields(
        rows,
        {"raw_file", "response_sha256", "registry_ids", "entity", "join_status"},
        "evaluations",
    )
    request_index = _request_index(manifest)
    for index, row in enumerate(rows, start=1):
        _verify_siblings(row, registry_ids, siblings, f"evaluation row {index}")
        request = request_index.get(row["raw_file"])
        if request is None or request.get("response_sha256") != row["response_sha256"]:
            raise ValueError(f"evaluation row {index} has unreachable raw provenance")
    if len(rows) != 9:
        raise ValueError(f"evaluation row invariant changed: {len(rows)}")
    return len(_array(manifest["requests"], "evaluation requests")), len(rows)


def _verify_procurement(
    registry_ids: set[str], siblings: dict[str, list[str]]
) -> tuple[int, int]:
    manifest = _verify_snapshot_files(PROCUREMENT)
    _verify_output(
        PROCUREMENT,
        manifest,
        "parsed/procurement_awards.json",
        "parsed/procurement_awards.json",
    )
    _verify_output(
        PROCUREMENT,
        manifest,
        "data/processed/nonprofit_procurement_contracts.csv",
        "nonprofit_procurement_contracts.csv",
        PROCESSED / "nonprofit_procurement_contracts.csv",
    )
    rows = _load_csv(PROCESSED / "nonprofit_procurement_contracts.csv")
    _require_fields(
        rows,
        {
            "supplier_award_key",
            "official_notice_url",
            "search_raw_file",
            "search_response_sha256",
            "tender_raw_file",
            "tender_response_sha256",
            "registry_ids",
            "entity",
        },
        "procurement",
    )
    request_index = _request_index(manifest)
    for index, row in enumerate(rows, start=1):
        _verify_siblings(row, registry_ids, siblings, f"procurement row {index}")
        official = urllib.parse.urlparse(row["official_notice_url"])
        if official.scheme != "https" or official.hostname != "web.pcc.gov.tw":
            raise ValueError(f"procurement row {index} lacks an official notice URL")
        for file_field, hash_field in (
            ("search_raw_file", "search_response_sha256"),
            ("tender_raw_file", "tender_response_sha256"),
        ):
            request = request_index.get(row[file_field])
            if request is None or request.get("response_sha256") != row[hash_field]:
                raise ValueError(f"procurement row {index} has unreachable raw provenance")
    if len(rows) != 12:
        raise ValueError(f"procurement row invariant changed: {len(rows)}")
    return len(_array(manifest["requests"], "procurement requests")), len(rows)


def _rederive_announcements() -> None:
    """Run the snapshot's no-network raw-to-output reconstruction contract."""
    command = [
        sys.executable,
        "scripts/download_education_bureau_pilot.py",
        "--verify-only",
        "ntpc-pilot-v1",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        details = (completed.stdout + completed.stderr).strip()
        raise ValueError(f"announcement raw reconstruction failed: {details}")


def _verify_announcements(
    registry_ids: set[str],
    siblings: dict[str, list[str]],
    penalty_groups: set[str],
) -> tuple[int, int, int]:
    _rederive_announcements()
    manifest = _verify_snapshot_files(ANNOUNCEMENTS)
    _verify_output(ANNOUNCEMENTS, manifest, "parsed/notices.json", "parsed/notices.json")
    _verify_output(ANNOUNCEMENTS, manifest, "parsed/actions.json", "parsed/actions.json")
    _verify_output(
        ANNOUNCEMENTS,
        manifest,
        "data/processed/education_bureau_notices_ntpc.csv",
        "education_bureau_notices_ntpc.csv",
        PROCESSED / "education_bureau_notices_ntpc.csv",
    )
    _verify_output(
        ANNOUNCEMENTS,
        manifest,
        "data/processed/education_bureau_actions_ntpc.csv",
        "education_bureau_actions_ntpc.csv",
        PROCESSED / "education_bureau_actions_ntpc.csv",
    )
    notices = _load_csv(PROCESSED / "education_bureau_notices_ntpc.csv")
    actions = _load_csv(PROCESSED / "education_bureau_actions_ntpc.csv")
    _require_fields(
        notices,
        {"notice_id", "notice_key", "source_url", "classification", "action_count"},
        "education notices",
    )
    _require_fields(
        actions,
        {
            "action_key",
            "notice_id",
            "improvement_status",
            "raw_detail_file",
            "raw_detail_sha256",
            "raw_pdf_file",
            "raw_pdf_sha256",
            "registry_ids",
            "entity",
            "candidate_penalty_links",
        },
        "education actions",
    )
    if len({row["notice_key"] for row in notices}) != len(notices):
        raise ValueError("education notice keys are not unique")
    if len({row["action_key"] for row in actions}) != len(actions):
        raise ValueError("education action keys are not unique")
    notice_ids = {row["notice_id"] for row in notices}
    if notice_ids != {"6097", "6098", "7077", "15015", "6787"}:
        raise ValueError("education notice allowlist changed")
    if sum(int(row["action_count"]) for row in notices) != len(actions):
        raise ValueError("education notice action counts do not sum to actions")
    request_index = _request_index(manifest)
    status_counts: Counter[str] = Counter()
    for index, row in enumerate(actions, start=1):
        if row["notice_id"] not in {"6097", "6098"}:
            raise ValueError("negative-control notice produced an institution action")
        status_counts[row["improvement_status"]] += 1
        _verify_siblings(row, registry_ids, siblings, f"education action {index}")
        for file_field, hash_field in (
            ("raw_detail_file", "raw_detail_sha256"),
            ("raw_pdf_file", "raw_pdf_sha256"),
        ):
            request = request_index.get(row[file_field])
            if request is None or request.get("response_sha256") != row[hash_field]:
                raise ValueError(f"education action {index} has unreachable raw provenance")
        for candidate in _json_list(
            row["candidate_penalty_links"], f"education action {index} penalty links"
        ):
            value = _object(candidate, "candidate penalty link")
            if value.get("penalty_group_id") not in penalty_groups:
                raise ValueError(f"education action {index} links an unknown penalty group")
            relationship = "candidate only; not asserted to be the same event"
            if value.get("relationship") != relationship:
                raise ValueError(f"education action {index} overstates a penalty relationship")
    if len(notices) != 5 or len(actions) != 98 or status_counts != {"ordered": 98}:
        raise ValueError("education announcement pilot invariants changed")
    request_count = len(_array(manifest["requests"], "announcement requests"))
    return request_count, len(notices), len(actions)


def _verify_announcement_listings() -> tuple[int, int, int]:
    aggregate = ANNOUNCEMENT_DISCOVERIES.read_bytes()
    summary = verify_listing_observations(
        ANNOUNCEMENT_LISTINGS, aggregate_data=aggregate
    )
    with ANNOUNCEMENT_DISCOVERIES.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    if len(rows) != summary["discovery_count"]:
        raise ValueError("announcement discovery aggregate count differs")
    if rows:
        _require_fields(
            rows,
            {
                "observation_id",
                "observed_at",
                "artifact_root",
                "notice_id",
                "notice_key",
                "source_url",
                "publication_date",
                "classification",
                "raw_detail_file",
                "raw_detail_sha256",
            },
            "announcement discoveries",
        )
        if len({row["notice_key"] for row in rows}) != len(rows):
            raise ValueError("announcement discovery keys are not unique")
        if any(
            row["classification"] != "unclassified_official_notice"
            or row.get("action_count") != "0"
            for row in rows
        ):
            raise ValueError("announcement discovery overstates classification or actions")
    return (
        int(summary["observation_count"]),
        int(summary["known_id_count"]),
        len(rows),
    )


def _verify_official_events(
    registry_ids: set[str], siblings: dict[str, list[str]], discovered_notices: int
) -> int:
    """Rebuild the normalized event projection in memory and verify its contract."""
    command = [
        sys.executable,
        "scripts/build_official_events.py",
        "--verify-only",
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        details = (completed.stdout + completed.stderr).strip()
        raise ValueError(f"official event reconstruction failed: {details}")

    manifest_path = PROCESSED / "official_events_ntpc.manifest.json"
    output_path = PROCESSED / "official_events_ntpc.csv"
    manifest = _load_json(manifest_path)
    if manifest.get("schema_version") != "official-events-v1":
        raise ValueError("unsupported official event schema")
    inputs = _object(manifest.get("inputs"), "official event inputs")
    workspace = pathlib.Path.cwd().resolve()
    for relative, value in inputs.items():
        path = pathlib.Path(relative).resolve()
        try:
            path.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(f"official event input escapes workspace: {relative}") from exc
        metadata = _object(value, f"official event input {relative}")
        data = _verify_blob(path, metadata, "official event input")
        if metadata.get("kind") == "csv":
            rows = list(csv.DictReader(data.decode("utf-8").splitlines()))
            if len(rows) != metadata.get("record_count"):
                raise ValueError(f"official event input row count differs: {relative}")

    output_meta = _object(manifest.get("output"), "official event output")
    _verify_blob(output_path, output_meta, "official event output")
    rows = _load_csv(output_path)
    _require_fields(
        rows,
        {
            "event_id",
            "source_record_id",
            "event_family",
            "parent_event_id",
            "entity",
            "registry_ids",
            "identity_status",
            "event_date",
            "event_date_semantics",
            "lifecycle_status",
            "outcome",
            "verification_status",
            "severity",
            "raw_artifacts_json",
            "details_json",
        },
        "official events",
    )
    expected_total = 1501 + discovered_notices
    if len(rows) != expected_total or output_meta.get("record_count") != len(rows):
        raise ValueError(f"official event row invariant changed: {len(rows)}")
    event_ids = {row["event_id"] for row in rows}
    if len(event_ids) != len(rows):
        raise ValueError("official event IDs are not unique")
    family_counts = Counter(row["event_family"] for row in rows)
    expected_counts = {
        "penalty": 1386,
        "evaluation": 9,
        "education_notice": 5 + discovered_notices,
        "corrective_action": 98,
        "procurement_award": 3,
    }
    if family_counts != expected_counts or output_meta.get("family_counts") != expected_counts:
        raise ValueError(f"official event family counts changed: {dict(family_counts)}")

    notice_ids = {
        row["event_id"] for row in rows if row["event_family"] == "education_notice"
    }
    unresolved_actions = 0
    for index, row in enumerate(rows, start=1):
        _verify_siblings(row, registry_ids, siblings, f"official event {index}")
        if row["event_family"] == "corrective_action":
            if row["parent_event_id"] not in notice_ids:
                raise ValueError(f"official event {index} has an unknown notice parent")
            if row["lifecycle_status"] != "ordered" or row["outcome"]:
                raise ValueError(f"official event {index} overstates improvement status")
            unresolved_actions += row["identity_status"].startswith("unresolved")
        elif row["parent_event_id"]:
            raise ValueError(f"official event {index} has an unsupported parent")
        if row["event_family"] != "penalty" and row["severity"]:
            raise ValueError(f"official event {index} has unsupported cross-source severity")
        _array(json.loads(row["raw_artifacts_json"]), "official event raw artifacts")
        _object(json.loads(row["details_json"]), "official event details")
    if unresolved_actions != 16:
        raise ValueError(
            f"official event unresolved action invariant changed: {unresolved_actions}"
        )
    return len(rows)


def main() -> None:
    root_files = _verify_root_snapshot()
    observation_count, latest_observation, source_changes = _verify_observation_history()
    registry_ids, siblings, penalty_groups = _identity_context()
    evaluation_requests, evaluations = _verify_evaluation(registry_ids, siblings)
    procurement_requests, procurement_rows = _verify_procurement(registry_ids, siblings)
    announcement_requests, notices, actions = _verify_announcements(
        registry_ids, siblings, penalty_groups
    )
    listing_observations, listing_known_ids, discovered_notices = (
        _verify_announcement_listings()
    )
    official_events = _verify_official_events(
        registry_ids, siblings, discovered_notices
    )
    print(
        "verified external artifacts: "
        f"{root_files} root files; "
        f"observations {observation_count} snapshots/{source_changes} changes "
        f"(latest {latest_observation}); "
        f"evaluation {evaluation_requests} raw/{evaluations} rows; "
        f"procurement {procurement_requests} raw/{procurement_rows} rows; "
        f"announcements {announcement_requests} raw/{notices} notices/{actions} actions; "
        f"listing monitor {listing_observations} observations/{listing_known_ids} known/"
        f"{discovered_notices} discovered; "
        f"official timeline {official_events} events"
    )
    print(
        "hash reachability, immutable observations, CSV schemas, registry UUIDs, "
        "sibling completeness, and event semantics: OK"
    )


if __name__ == "__main__":
    main()
