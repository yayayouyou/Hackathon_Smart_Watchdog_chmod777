"""Build one deterministic timeline from the pinned official source tables.

This script never performs network I/O. ``--verify-only`` reconstructs the CSV
and manifest in memory and compares them with the checked-in artifacts without
writing files.

Run::

    PYTHONPATH=src .venv/bin/python scripts/build_official_events.py
    PYTHONPATH=src .venv/bin/python scripts/build_official_events.py --verify-only
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import pathlib
import sys
from collections import Counter
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.events import (
    EXPECTED_FAMILY_COUNTS,
    OFFICIAL_EVENT_FIELDS,
    SCHEMA_VERSION,
    SourceContext,
    build_official_events,
)
from smart_watchdog.scrape.announcement_observations import (
    aggregate_discovery_csv,
    latest_listing_observation_id,
    list_listing_observation_ids,
    verify_listing_observations,
)
from smart_watchdog.scrape.observations import (
    SOURCE_FILES,
    first_observed_record_hashes,
    latest_observation_id,
    verify_observations,
)

PROCESSED = pathlib.Path("data/processed")
EXTERNAL = pathlib.Path("data/external")
OUTPUT = PROCESSED / "official_events_ntpc.csv"
OUTPUT_MANIFEST = PROCESSED / "official_events_ntpc.manifest.json"

INSTITUTIONS = PROCESSED / "institutions_ntpc.csv"
PENALTIES = PROCESSED / "penalties_ntpc.csv"
EVALUATIONS = PROCESSED / "evaluations_ntpc.csv"
NOTICES = PROCESSED / "education_bureau_notices_ntpc.csv"
DISCOVERED_NOTICES = PROCESSED / "education_bureau_discovered_notices_ntpc.csv"
ACTIONS = PROCESSED / "education_bureau_actions_ntpc.csv"
PROCUREMENT = PROCESSED / "nonprofit_procurement_contracts.csv"

ROOT_MANIFEST = EXTERNAL / "manifest.json"
OBSERVATIONS_ROOT = EXTERNAL / "observations"
EVALUATION_ROOT = EXTERNAL / "evaluation/ntpc-pilot-v1"
PROCUREMENT_ROOT = EXTERNAL / "procurement/ntpc-pilot-v1"
EDUCATION_ROOT = EXTERNAL / "education_bureau_announcements/ntpc-pilot-v1"
EDUCATION_LISTING_ROOT = (
    EXTERNAL / "education_bureau_announcements/listing_observations"
)
EVALUATION_MANIFEST = EVALUATION_ROOT / "manifest.json"
PROCUREMENT_MANIFEST = PROCUREMENT_ROOT / "manifest.json"
EDUCATION_MANIFEST = EDUCATION_ROOT / "manifest.json"

CSV_INPUTS = [
    INSTITUTIONS,
    PENALTIES,
    EVALUATIONS,
    NOTICES,
    DISCOVERED_NOTICES,
    ACTIONS,
    PROCUREMENT,
]
MANIFEST_INPUTS = [
    ROOT_MANIFEST,
    EVALUATION_MANIFEST,
    PROCUREMENT_MANIFEST,
    EDUCATION_MANIFEST,
]
ABSENCE_SEMANTICS = (
    "The evaluation, procurement, and education-announcement sources are bounded "
    "pilots. Absence of an event is never evidence of compliance, completion, "
    "no contract, or low risk."
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_csv(
    path: pathlib.Path, *, allow_empty: bool = False
) -> tuple[bytes, list[dict[str, str]]]:
    data = path.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"CSV input must be UTF-8: {path}") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    rows = list(reader)
    if not rows and not allow_empty:
        raise ValueError(f"CSV input has no records: {path}")
    if reader.fieldnames is None:
        raise ValueError(f"CSV input has no header: {path}")
    return data, rows


def _load_manifest(path: pathlib.Path) -> tuple[bytes, dict[str, Any]]:
    data = path.read_bytes()
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError(f"manifest must be an object: {path}")
    return data, value


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(
        handle, fieldnames=OFFICIAL_EVENT_FIELDS, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue().encode("utf-8")


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _source_contexts(
    root_manifest: dict[str, Any],
    observation_manifest: dict[str, Any],
    evaluation_manifest: dict[str, Any],
    procurement_manifest: dict[str, Any],
    education_manifest: dict[str, Any],
) -> dict[str, SourceContext]:
    root_files = _object(root_manifest.get("files"), "root manifest files")
    penalty = _object(root_files.get("punish_all.json"), "punish_all metadata")
    observed_objects = _object(
        observation_manifest.get("objects"), "observation objects"
    )
    observed_penalty = _object(
        observed_objects.get("punish_all.json"), "observed punish_all object"
    )
    if observed_penalty.get("sha256") != penalty.get("sha256"):
        raise ValueError("latest observation and root penalty snapshot differ")
    return {
        "penalty": SourceContext(
            source_system="kiang_preschool_penalty_mirror",
            source_authority="official_mirror",
            verification_status="immutable_observation_validated",
            source_url=str(penalty.get("source_url", "")),
            observed_at="",
            snapshot_id=str(observation_manifest.get("observation_id", "")),
            artifact_root=OBSERVATIONS_ROOT.as_posix(),
            snapshot_sha256=str(penalty.get("sha256", "")),
            snapshot_object_path=str(observed_penalty.get("object_path", "")),
        ),
        "evaluation": SourceContext(
            source_system="moe_evaluation_search",
            source_authority="official",
            verification_status="pinned_parsed_derived",
            source_url=str(evaluation_manifest.get("source_url", "")),
            observed_at=str(evaluation_manifest.get("completed_at_utc") or ""),
            snapshot_id=str(evaluation_manifest.get("snapshot_id", "")),
            artifact_root=EVALUATION_ROOT.as_posix(),
        ),
        "education": SourceContext(
            source_system="ntpc_education_bureau_announcements",
            source_authority="official",
            verification_status="pinned_raw_rederived",
            source_url=str(education_manifest.get("official_origin", "")),
            observed_at=str(education_manifest.get("completed_at_utc") or ""),
            snapshot_id=str(education_manifest.get("snapshot_id", "")),
            artifact_root=EDUCATION_ROOT.as_posix(),
        ),
        "procurement": SourceContext(
            source_system="g0v_procurement_official_mirror",
            source_authority="official_mirror",
            verification_status="pinned_raw_rederived",
            source_url=str(procurement_manifest.get("official_source_url", "")),
            observed_at=str(procurement_manifest.get("completed_at_utc") or ""),
            snapshot_id=str(procurement_manifest.get("snapshot_id", "")),
            artifact_root=PROCUREMENT_ROOT.as_posix(),
        ),
    }


def derive() -> tuple[bytes, bytes, dict[str, int]]:
    loaded_csv = {
        path: _load_csv(path, allow_empty=path == DISCOVERED_NOTICES)
        for path in CSV_INPUTS
    }
    listing_summary = verify_listing_observations(
        EDUCATION_LISTING_ROOT,
        aggregate_data=loaded_csv[DISCOVERED_NOTICES][0],
    )
    if aggregate_discovery_csv(EDUCATION_LISTING_ROOT) != loaded_csv[DISCOVERED_NOTICES][0]:
        raise ValueError("announcement discovery aggregate differs from listing chain")
    listing_observation_id = latest_listing_observation_id(EDUCATION_LISTING_ROOT)
    if listing_observation_id != listing_summary["latest_observation_id"]:
        raise ValueError("verified announcement listing tip differs")
    listing_manifest_inputs = [
        EDUCATION_LISTING_ROOT / observation_id / "manifest.json"
        for observation_id in list_listing_observation_ids(EDUCATION_LISTING_ROOT)
    ]
    current_payloads = {
        filename: (EXTERNAL / filename).read_bytes() for filename in SOURCE_FILES
    }
    observation_summary = verify_observations(
        OBSERVATIONS_ROOT, current_payloads=current_payloads
    )
    observation_id = latest_observation_id(OBSERVATIONS_ROOT)
    if observation_id is None:
        raise ValueError("official events require an immutable root observation")
    if observation_summary["latest_observation_id"] != observation_id:
        raise ValueError("verified observation tip differs from selected observation")
    penalty_observed_at = first_observed_record_hashes(
        OBSERVATIONS_ROOT, "punish_all.json"
    )
    observation_manifest = OBSERVATIONS_ROOT / observation_id / "manifest.json"
    manifest_inputs = [
        *MANIFEST_INPUTS,
        observation_manifest,
        *listing_manifest_inputs,
    ]
    loaded_manifests = {path: _load_manifest(path) for path in manifest_inputs}
    contexts = _source_contexts(
        loaded_manifests[ROOT_MANIFEST][1],
        loaded_manifests[observation_manifest][1],
        loaded_manifests[EVALUATION_MANIFEST][1],
        loaded_manifests[PROCUREMENT_MANIFEST][1],
        loaded_manifests[EDUCATION_MANIFEST][1],
    )
    events = build_official_events(
        institutions=loaded_csv[INSTITUTIONS][1],
        penalties=loaded_csv[PENALTIES][1],
        evaluations=loaded_csv[EVALUATIONS][1],
        notices=[
            *loaded_csv[NOTICES][1],
            *loaded_csv[DISCOVERED_NOTICES][1],
        ],
        actions=loaded_csv[ACTIONS][1],
        procurement=loaded_csv[PROCUREMENT][1],
        contexts=contexts,
        penalty_observed_at_by_record_hash=penalty_observed_at,
    )
    output_data = _csv_bytes(events)
    family_counts = dict(sorted(Counter(row["event_family"] for row in events).items()))
    expected_family_counts = {
        **EXPECTED_FAMILY_COUNTS,
        "education_notice": (
            len(loaded_csv[NOTICES][1]) + len(loaded_csv[DISCOVERED_NOTICES][1])
        ),
    }
    unresolved_actions = sum(
        row["event_family"] == "corrective_action"
        and row["identity_status"].startswith("unresolved")
        for row in events
    )
    # manifest 的 key 一律用 POSIX 寫法。`str(Path)` 在 Windows 會給反斜線，
    # 使得同一份輸入在兩個平台產生不同的 manifest，--verify-only 永遠失敗。
    inputs: dict[str, dict[str, object]] = {}
    for path, (data, rows) in loaded_csv.items():
        inputs[path.as_posix()] = {
            "kind": "csv",
            "sha256": _sha256(data),
            "byte_length": len(data),
            "record_count": len(rows),
        }
    for path, (data, _) in loaded_manifests.items():
        inputs[path.as_posix()] = {
            "kind": "manifest",
            "sha256": _sha256(data),
            "byte_length": len(data),
        }
    manifest = {
        "manifest_version": 1,
        "schema_version": SCHEMA_VERSION,
        "full_population_claim": False,
        "absence_semantics": ABSENCE_SEMANTICS,
        "inputs": inputs,
        "source_snapshots": {
            name: {
                "snapshot_id": context.snapshot_id,
                "observed_at": context.observed_at or None,
                "source_authority": context.source_authority,
                "verification_status": context.verification_status,
            }
            for name, context in sorted(contexts.items())
        },
        "knowledge_time": {
            "penalty_method": "first_observed_exact_source_record_version",
            "education_listing_method": "row_level_listing_observation",
            "education_listing_tip": listing_observation_id,
            "adopted_baseline_time": None,
            "non_baseline_requires_observed_at": True,
        },
        "output": {
            "path": OUTPUT.as_posix(),
            "sha256": _sha256(output_data),
            "byte_length": len(output_data),
            "record_count": len(events),
            "family_counts": family_counts,
            "unresolved_corrective_actions": unresolved_actions,
        },
        "semantic_guards": {
            "cross_source_deduplication": False,
            "penalty_group_is_asserted_single_event": False,
            "corrective_order_implies_noncompletion": False,
            "procurement_uncovered_implies_no_contract": False,
            "non_penalty_cross_source_severity": False,
        },
    }
    manifest_data = _json_bytes(manifest)
    if family_counts != dict(sorted(expected_family_counts.items())):
        raise ValueError("manifest event counts differ from the v1 contract")
    if unresolved_actions != 16:
        raise ValueError(
            f"historical unresolved action invariant changed: {unresolved_actions}"
        )
    return output_data, manifest_data, family_counts


def _replace(path: pathlib.Path, data: bytes) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def publish(output_data: bytes, manifest_data: bytes) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    originals = {
        path: path.read_bytes() if path.exists() else None
        for path in (OUTPUT, OUTPUT_MANIFEST)
    }
    try:
        _replace(OUTPUT, output_data)
        _replace(OUTPUT_MANIFEST, manifest_data)
    except BaseException:
        for path, original in originals.items():
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _replace(path, original)
        raise


def verify_only(output_data: bytes, manifest_data: bytes) -> None:
    if OUTPUT.read_bytes() != output_data:
        raise ValueError(f"live official event CSV differs from derivation: {OUTPUT}")
    if OUTPUT_MANIFEST.read_bytes() != manifest_data:
        raise ValueError(
            f"live official event manifest differs from derivation: {OUTPUT_MANIFEST}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="derive and compare checked-in outputs without writing files",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_data, manifest_data, family_counts = derive()
    if args.verify_only:
        verify_only(output_data, manifest_data)
        verb = "verified"
    else:
        publish(output_data, manifest_data)
        verb = "wrote"
    print(
        f"{verb} {OUTPUT} ({sum(family_counts.values())} rows; "
        + ", ".join(f"{name}={count}" for name, count in family_counts.items())
        + ")"
    )
    print(f"{verb} {OUTPUT_MANIFEST}; no network")


if __name__ == "__main__":
    main()
