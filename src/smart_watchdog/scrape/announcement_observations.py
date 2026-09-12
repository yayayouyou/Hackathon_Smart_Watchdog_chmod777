"""Immutable observations for the NTPC Important Announcements listing.

This store intentionally has its own HTML/listing record model.  It does not
reuse the root external JSON observation model because a bounded paginated
listing has different identity, boundary, and absence semantics.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from typing import Any

from smart_watchdog.scrape.announcements import (
    IMPORTANT_LISTING_URL_TEMPLATE,
    AnnouncementClient,
    ResponseArtifact,
    parse_important_announcements_listing,
    parse_notice_detail,
)

MANIFEST_VERSION = 1
SCHEMA_VERSION = "ntpc-important-announcement-listing-observation-v1"
OBSERVATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
DEFAULT_BOOTSTRAP_PAGES = 2
DEFAULT_PAGE_CAP = 6
DEFAULT_DETAIL_CAP = 20
RECORDS_PER_FULL_PAGE = 30
BOUNDARY_ANCHOR_COUNT = 10
ARTIFACT_ROOT = "data/external/education_bureau_announcements/listing_observations"
ABSENCE_SEMANTICS = (
    "This bounded Important Announcements listing prefix is not a population census. "
    "Absence from discoveries or the covered prefix is not evidence of compliance, "
    "completion, closure, inactivity, or low risk."
)
DISCOVERY_FIELDS = [
    "observation_id",
    "observed_at",
    "artifact_root",
    "notice_id",
    "notice_key",
    "source_url",
    "title",
    "publication_date",
    "listing_publication_date",
    "detail_publication_date",
    "listing_page",
    "listing_source_index",
    "listing_object_path",
    "listing_object_sha256",
    "raw_detail_file",
    "raw_detail_sha256",
    "classification",
    "body_sha256",
    "attachments_json",
    "action_count",
    "full_population_claim",
    "absence_semantics",
]


@dataclasses.dataclass(frozen=True)
class ListingObservationCandidate:
    observation_id: str
    manifest_data: bytes
    discoveries_data: bytes
    objects: dict[str, bytes]
    summary: dict[str, object]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _csv_bytes(rows: list[dict[str, object]]) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=DISCOVERY_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue().encode("utf-8")


def _canonical_timestamp(value: object, label: str) -> str:
    text = str(value or "")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} is not an ISO timestamp: {text!r}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    canonical = parsed.astimezone(dt.timezone.utc).replace(microsecond=0)
    result = canonical.isoformat().replace("+00:00", "Z")
    if text != result:
        raise ValueError(f"{label} is not canonical UTC")
    return result


def _validate_observation_id(observation_id: str) -> None:
    if not OBSERVATION_ID_RE.fullmatch(observation_id) or observation_id in {".", ".."}:
        raise ValueError("invalid announcement listing observation ID")


def validate_listing_observation_id(observation_id: str) -> None:
    """Reject IDs that cannot name one immediate observation directory."""
    _validate_observation_id(observation_id)


def _validate_response_order(
    listing_artifacts: list[ResponseArtifact],
    detail_artifacts: list[ResponseArtifact],
    page1_recheck: ResponseArtifact,
) -> None:
    artifacts = [*listing_artifacts, *detail_artifacts, page1_recheck]
    sequences = [artifact.sequence for artifact in artifacts]
    if any(sequence <= 0 for sequence in sequences) or any(
        right <= left for left, right in zip(sequences, sequences[1:])
    ):
        raise ValueError("announcement response provenance is not in request order")


def _artifact_entry(
    artifact: ResponseArtifact,
    *,
    role: str,
    page_number: int | None = None,
    notice_id: str | None = None,
) -> tuple[dict[str, object], str, bytes]:
    if artifact.status != 200:
        raise ValueError(f"{role} response status is not HTTP 200")
    if artifact.content_type not in {"text/html", "application/xhtml+xml"}:
        raise ValueError(f"{role} response is not official HTML")
    if artifact.source_url != artifact.final_url:
        raise ValueError(f"{role} response redirected away from its exact canonical URL")
    _canonical_timestamp(artifact.retrieved_at_utc, f"{role} retrieval time")
    digest = artifact.sha256
    object_path = f"objects/{digest}.html"
    entry: dict[str, object] = {
        "sequence": artifact.sequence,
        "role": role,
        "source_url": artifact.source_url,
        "final_url": artifact.final_url,
        "status": artifact.status,
        "content_type": artifact.content_type,
        "charset": artifact.charset,
        "retrieved_at_utc": artifact.retrieved_at_utc,
        "object_path": object_path,
        "sha256": digest,
        "byte_length": len(artifact.body),
    }
    if page_number is not None:
        entry["page_number"] = page_number
    if notice_id is not None:
        entry["notice_id"] = notice_id
    return entry, object_path, artifact.body


def _add_object(objects: dict[str, bytes], path: str, data: bytes) -> None:
    existing = objects.get(path)
    if existing is not None and existing != data:
        raise ValueError(f"content-addressed announcement object collision: {path}")
    objects[path] = data


def _listing_pages(
    artifacts: list[ResponseArtifact],
) -> tuple[list[dict[str, object]], list[dict[str, object]], dict[str, bytes]]:
    if not artifacts:
        raise ValueError("announcement listing candidate has no pages")
    entries: list[dict[str, object]] = []
    parsed_pages: list[dict[str, object]] = []
    objects: dict[str, bytes] = {}
    total_pages: int | None = None
    expected_url = IMPORTANT_LISTING_URL_TEMPLATE.format(page=1)
    for expected_page, artifact in enumerate(artifacts, start=1):
        if artifact.source_url != expected_url:
            raise ValueError("announcement listing page sequence is discontinuous")
        parsed = parse_important_announcements_listing(artifact.body, artifact.source_url)
        if parsed["page_number"] != expected_page:
            raise ValueError("announcement listing page number is discontinuous")
        parsed_total = int(parsed["total_pages"])
        if total_pages is None:
            total_pages = parsed_total
        elif parsed_total != total_pages:
            raise ValueError("announcement listing totalPage changed within one poll")
        entry, path, data = _artifact_entry(
            artifact, role="listing_page", page_number=expected_page
        )
        entry["record_count"] = len(parsed["records"])
        entries.append(entry)
        parsed_pages.append(parsed)
        _add_object(objects, path, data)
        expected_url = str(parsed["next_page_url"] or "")
        if expected_page < len(artifacts) and not expected_url:
            raise ValueError("announcement listing ended before supplied candidate pages")
    return entries, parsed_pages, objects


def _flatten_listing(
    pages: list[dict[str, object]], entries: list[dict[str, object]]
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    seen: dict[str, tuple[str, str, str]] = {}
    for parsed, entry in zip(pages, entries):
        page_number = int(parsed["page_number"])
        for raw in parsed["records"]:
            if not isinstance(raw, dict):
                raise ValueError("announcement listing parser emitted a non-object")
            notice_id = str(raw["notice_id"])
            identity = (
                str(raw["detail_url"]),
                str(raw["title"]),
                str(raw["publication_date"]),
            )
            if notice_id in seen:
                qualifier = "conflicting" if seen[notice_id] != identity else "duplicate"
                raise ValueError(f"announcement listing prefix has {qualifier} ID {notice_id}")
            seen[notice_id] = identity
            records.append(
                {
                    **raw,
                    "page_number": page_number,
                    "listing_object_path": entry["object_path"],
                    "listing_object_sha256": entry["sha256"],
                }
            )
    return records


def _prefix_through_anchors(
    index: list[dict[str, object]], anchors: list[str]
) -> list[dict[str, object]]:
    """Trim the final fetched page immediately after the last fixed anchor."""
    positions = {
        str(row["notice_id"]): position for position, row in enumerate(index)
    }
    missing = set(anchors) - positions.keys()
    if missing:
        raise ValueError(
            f"announcement fixed boundary anchors are missing: {sorted(missing)}"
        )
    return index[: max(positions[anchor] for anchor in anchors) + 1]


def _page1_recheck(
    artifact: ResponseArtifact,
    first_page: dict[str, object],
    total_pages: int,
    objects: dict[str, bytes],
) -> dict[str, object]:
    expected_url = IMPORTANT_LISTING_URL_TEMPLATE.format(page=1)
    if artifact.source_url != expected_url:
        raise ValueError("announcement page-1 recheck URL differs")
    parsed = parse_important_announcements_listing(artifact.body, artifact.source_url)
    if parsed["page_number"] != 1 or int(parsed["total_pages"]) != total_pages:
        raise ValueError("announcement page-1 recheck pagination differs")
    if parsed["records"] != first_page["records"]:
        raise ValueError("announcement page 1 changed during acquisition")
    entry, path, data = _artifact_entry(artifact, role="page1_recheck", page_number=1)
    entry["record_count"] = len(parsed["records"])
    _add_object(objects, path, data)
    return entry


def _base_manifest(
    *,
    observation_id: str,
    observation_kind: str,
    observed_at: str | None,
    previous_observation_id: str | None,
    listing_entries: list[dict[str, object]],
    recheck_entry: dict[str, object],
    detail_entries: list[dict[str, object]],
    listing_index: list[dict[str, object]],
    coverage: dict[str, object],
    discoveries_data: bytes,
) -> dict[str, object]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "schema_version": SCHEMA_VERSION,
        "observation_id": observation_id,
        "observation_kind": observation_kind,
        "observed_at_utc": observed_at,
        "previous_observation_id": previous_observation_id,
        "artifacts": {
            "listing_pages": listing_entries,
            "page1_recheck": recheck_entry,
            "details": detail_entries,
        },
        "listing_index": listing_index,
        "coverage": coverage,
        "discoveries": {
            "path": "discoveries.csv",
            "sha256": _sha256(discoveries_data),
            "byte_length": len(discoveries_data),
            "record_count": len(list(csv.DictReader(io.StringIO(discoveries_data.decode())))),
            "schema": DISCOVERY_FIELDS,
            "absence_semantics": ABSENCE_SEMANTICS,
        },
    }


def build_bootstrap_candidate(
    *,
    observation_id: str,
    listing_artifacts: list[ResponseArtifact],
    page1_recheck: ResponseArtifact,
    bootstrap_pages: int = DEFAULT_BOOTSTRAP_PAGES,
    page_cap: int = DEFAULT_PAGE_CAP,
    detail_cap: int = DEFAULT_DETAIL_CAP,
) -> ListingObservationCandidate:
    """Build a bootstrap candidate entirely from caller-supplied response bytes."""
    _validate_observation_id(observation_id)
    if bootstrap_pages <= 0 or page_cap <= 0 or detail_cap <= 0:
        raise ValueError("announcement listing acquisition caps must be positive")
    if bootstrap_pages > page_cap or len(listing_artifacts) != bootstrap_pages:
        raise ValueError("bootstrap must contain exactly pages 1..bootstrap_pages")
    _validate_response_order(listing_artifacts, [], page1_recheck)
    entries, pages, objects = _listing_pages(listing_artifacts)
    if any(len(page["records"]) != RECORDS_PER_FULL_PAGE for page in pages):
        raise ValueError("bootstrap listing pages must each contain exactly 30 records")
    index = _flatten_listing(pages, entries)
    expected_count = bootstrap_pages * RECORDS_PER_FULL_PAGE
    if len(index) != expected_count:
        raise ValueError("bootstrap listing record count differs from covered full pages")
    total_pages = int(pages[0]["total_pages"])
    recheck_entry = _page1_recheck(page1_recheck, pages[0], total_pages, objects)
    anchors = [str(row["notice_id"]) for row in index[-BOUNDARY_ANCHOR_COUNT:]]
    discoveries_data = _csv_bytes([])
    known_ids = [str(row["notice_id"]) for row in index]
    coverage = {
        "bootstrap_pages": bootstrap_pages,
        "page_cap": page_cap,
        "detail_cap": detail_cap,
        "pages_fetched": len(pages),
        "records_covered": len(index),
        "fixed_boundary_anchors": anchors,
        "boundary_found": True,
        "boundary_page": bootstrap_pages,
        "previous_known_ids": [],
        "known_ids": known_ids,
        "new_ids": [],
        "metadata_changed_ids": [],
        "page1_stable": True,
        "total_pages": total_pages,
    }
    manifest = _base_manifest(
        observation_id=observation_id,
        observation_kind="bootstrap",
        observed_at=None,
        previous_observation_id=None,
        listing_entries=entries,
        recheck_entry=recheck_entry,
        detail_entries=[],
        listing_index=index,
        coverage=coverage,
        discoveries_data=discoveries_data,
    )
    return ListingObservationCandidate(
        observation_id=observation_id,
        manifest_data=_json_bytes(manifest),
        discoveries_data=discoveries_data,
        objects=objects,
        summary={
            "pages": len(pages),
            "covered": len(index),
            "new": 0,
            "metadata_changed": 0,
            "requests": len(listing_artifacts) + 1,
        },
    )


def _previous_contract(previous: dict[str, Any]) -> tuple[list[str], list[str]]:
    coverage = previous.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("previous announcement listing coverage is missing")
    known = coverage.get("known_ids")
    anchors = coverage.get("fixed_boundary_anchors")
    if (
        not isinstance(known, list)
        or not known
        or len(known) != len(set(map(str, known)))
        or not isinstance(anchors, list)
        or len(anchors) != BOUNDARY_ANCHOR_COUNT
        or len(anchors) != len(set(map(str, anchors)))
    ):
        raise ValueError("previous announcement listing identity contract differs")
    return [str(value) for value in known], [str(value) for value in anchors]


def _detail_discoveries(
    *,
    observation_id: str,
    observed_at: str,
    new_records: list[dict[str, object]],
    detail_artifacts: list[ResponseArtifact],
    objects: dict[str, bytes],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if len(detail_artifacts) != len(new_records):
        raise ValueError("new announcement IDs and detail response count differ")
    entries: list[dict[str, object]] = []
    discoveries: list[dict[str, object]] = []
    for record, artifact in zip(new_records, detail_artifacts):
        notice_id = str(record["notice_id"])
        if artifact.source_url != record["detail_url"]:
            raise ValueError(f"detail response URL differs for new notice {notice_id}")
        detail = parse_notice_detail(artifact.body, artifact.source_url)
        entry, path, data = _artifact_entry(
            artifact, role="new_notice_detail", notice_id=notice_id
        )
        entry.update(
            {
                "title": detail["title"],
                "publication_date": detail["publication_date"],
                "body_sha256": detail["body_sha256"],
                "attachment_count": len(detail["attachments"]),
            }
        )
        entries.append(entry)
        _add_object(objects, path, data)
        discoveries.append(
            {
                "observation_id": observation_id,
                "observed_at": observed_at,
                "artifact_root": ARTIFACT_ROOT,
                "notice_id": notice_id,
                "notice_key": f"ntpc-kidedu-{notice_id}",
                "source_url": record["detail_url"],
                "title": detail["title"],
                "publication_date": (
                    detail["publication_date"] or record["publication_date"]
                ),
                "listing_publication_date": record["publication_date"],
                "detail_publication_date": detail["publication_date"] or "",
                "listing_page": record["page_number"],
                "listing_source_index": record["source_index"],
                "listing_object_path": record["listing_object_path"],
                "listing_object_sha256": record["listing_object_sha256"],
                "raw_detail_file": path,
                "raw_detail_sha256": artifact.sha256,
                "classification": "unclassified_official_notice",
                "body_sha256": detail["body_sha256"],
                "attachments_json": _json_value(detail["attachments"]),
                "action_count": 0,
                "full_population_claim": "false",
                "absence_semantics": ABSENCE_SEMANTICS,
            }
        )
    return entries, discoveries


def build_update_candidate(
    *,
    observation_id: str,
    observed_at: str,
    previous_manifest: dict[str, Any],
    listing_artifacts: list[ResponseArtifact],
    page1_recheck: ResponseArtifact,
    detail_artifacts: list[ResponseArtifact],
    page_cap: int = DEFAULT_PAGE_CAP,
    detail_cap: int = DEFAULT_DETAIL_CAP,
) -> ListingObservationCandidate:
    """Build a validated-poll candidate from supplied listing and detail bytes."""
    _validate_observation_id(observation_id)
    observed = _canonical_timestamp(observed_at, "announcement observation time")
    completed_at = _canonical_timestamp(
        page1_recheck.retrieved_at_utc,
        "announcement final page-1 recheck time",
    )
    if observed != completed_at:
        raise ValueError(
            "announcement observation time must equal the final page-1 recheck"
        )
    if page_cap <= 0 or detail_cap <= 0 or len(listing_artifacts) > page_cap:
        raise ValueError("announcement update exceeds its acquisition caps")
    previous_id = str(previous_manifest.get("observation_id") or "")
    _validate_observation_id(previous_id)
    _validate_response_order(listing_artifacts, detail_artifacts, page1_recheck)
    previous_known, anchors = _previous_contract(previous_manifest)
    entries, pages, objects = _listing_pages(listing_artifacts)
    full_index = _flatten_listing(pages, entries)
    anchor_set = set(anchors)
    cumulative: set[str] = set()
    boundary_page: int | None = None
    for parsed in pages:
        cumulative.update(str(row["notice_id"]) for row in parsed["records"])
        if anchor_set.issubset(cumulative):
            boundary_page = int(parsed["page_number"])
            break
    if boundary_page is None:
        reason = "page cap" if len(pages) >= page_cap else "candidate pages"
        raise ValueError(f"announcement fixed boundary anchors not found within {reason}")
    if boundary_page != len(pages):
        raise ValueError("announcement candidate continued after finding fixed anchors")
    index = _prefix_through_anchors(full_index, anchors)
    current_ids = [str(row["notice_id"]) for row in index]
    current_set = set(current_ids)
    missing_previous = set(previous_known) - current_set
    if missing_previous:
        raise ValueError(
            "announcement candidate prefix is missing previously covered IDs: "
            f"{sorted(missing_previous)}"
        )
    new_set = current_set - set(previous_known)
    new_records = [row for row in index if str(row["notice_id"]) in new_set]
    new_ids = [str(row["notice_id"]) for row in new_records]
    if len(new_ids) > detail_cap:
        raise ValueError("new announcement detail count exceeds detail cap")

    previous_index_raw = previous_manifest.get("listing_index")
    if not isinstance(previous_index_raw, list):
        raise ValueError("previous announcement listing index is missing")
    previous_index = {
        str(row.get("notice_id")): row
        for row in previous_index_raw
        if isinstance(row, dict)
    }
    metadata_changed: list[str] = []
    for row in index:
        notice_id = str(row["notice_id"])
        old = previous_index.get(notice_id)
        if old is None:
            continue
        fields = ("detail_url", "title", "publication_date")
        if any(str(old.get(field)) != str(row.get(field)) for field in fields):
            metadata_changed.append(notice_id)

    total_pages = int(pages[0]["total_pages"])
    recheck_entry = _page1_recheck(page1_recheck, pages[0], total_pages, objects)
    detail_entries, discoveries = _detail_discoveries(
        observation_id=observation_id,
        observed_at=observed,
        new_records=new_records,
        detail_artifacts=detail_artifacts,
        objects=objects,
    )
    discoveries_data = _csv_bytes(discoveries)
    coverage = {
        "bootstrap_pages": int(previous_manifest["coverage"]["bootstrap_pages"]),
        "page_cap": page_cap,
        "detail_cap": detail_cap,
        "pages_fetched": len(pages),
        "records_covered": len(index),
        "fixed_boundary_anchors": anchors,
        "boundary_found": True,
        "boundary_page": boundary_page,
        "previous_known_ids": previous_known,
        "known_ids": current_ids,
        "new_ids": new_ids,
        "metadata_changed_ids": metadata_changed,
        "page1_stable": True,
        "total_pages": total_pages,
    }
    manifest = _base_manifest(
        observation_id=observation_id,
        observation_kind="validated_poll",
        observed_at=observed,
        previous_observation_id=previous_id,
        listing_entries=entries,
        recheck_entry=recheck_entry,
        detail_entries=detail_entries,
        listing_index=index,
        coverage=coverage,
        discoveries_data=discoveries_data,
    )
    return ListingObservationCandidate(
        observation_id=observation_id,
        manifest_data=_json_bytes(manifest),
        discoveries_data=discoveries_data,
        objects=objects,
        summary={
            "pages": len(pages),
            "covered": len(index),
            "new": len(new_ids),
            "metadata_changed": len(metadata_changed),
            "requests": len(listing_artifacts) + len(detail_artifacts) + 1,
        },
    )


def acquire_bootstrap_candidate(
    *,
    observation_id: str,
    client: AnnouncementClient,
    bootstrap_pages: int = DEFAULT_BOOTSTRAP_PAGES,
    page_cap: int = DEFAULT_PAGE_CAP,
    detail_cap: int = DEFAULT_DETAIL_CAP,
) -> ListingObservationCandidate:
    """Fetch only the bounded bootstrap listing pages and a page-1 recheck."""
    artifacts: list[ResponseArtifact] = []
    next_url = IMPORTANT_LISTING_URL_TEMPLATE.format(page=1)
    for _ in range(bootstrap_pages):
        artifact = client.get(next_url)
        artifacts.append(artifact)
        parsed = parse_important_announcements_listing(artifact.body, artifact.source_url)
        next_url = str(parsed["next_page_url"] or "")
    recheck = client.get(IMPORTANT_LISTING_URL_TEMPLATE.format(page=1))
    return build_bootstrap_candidate(
        observation_id=observation_id,
        listing_artifacts=artifacts,
        page1_recheck=recheck,
        bootstrap_pages=bootstrap_pages,
        page_cap=page_cap,
        detail_cap=detail_cap,
    )


def acquire_update_candidate(
    *,
    observation_id: str,
    previous_manifest: dict[str, Any],
    client: AnnouncementClient,
    page_cap: int = DEFAULT_PAGE_CAP,
    detail_cap: int = DEFAULT_DETAIL_CAP,
) -> ListingObservationCandidate:
    """Fetch a bounded prefix through the fixed anchors, new details, and recheck."""
    _, anchors = _previous_contract(previous_manifest)
    anchor_set = set(anchors)
    listing_artifacts: list[ResponseArtifact] = []
    covered: set[str] = set()
    next_url = IMPORTANT_LISTING_URL_TEMPLATE.format(page=1)
    for _ in range(page_cap):
        if not next_url:
            raise ValueError("announcement listing ended before fixed boundary anchors")
        artifact = client.get(next_url)
        listing_artifacts.append(artifact)
        parsed = parse_important_announcements_listing(artifact.body, artifact.source_url)
        covered.update(str(row["notice_id"]) for row in parsed["records"])
        if anchor_set.issubset(covered):
            break
        next_url = str(parsed["next_page_url"] or "")
    if not anchor_set.issubset(covered):
        raise ValueError("announcement fixed boundary anchors not found within page cap")

    _, parsed_pages, _ = _listing_pages(listing_artifacts)
    temporary_entries = [
        {"object_path": "", "sha256": ""} for _ in listing_artifacts
    ]
    full_index = _flatten_listing(parsed_pages, temporary_entries)
    index = _prefix_through_anchors(full_index, anchors)
    previous_known, _ = _previous_contract(previous_manifest)
    new_set = {str(row["notice_id"]) for row in index} - set(previous_known)
    new_records = [row for row in index if str(row["notice_id"]) in new_set]
    if len(new_records) > detail_cap:
        raise ValueError("new announcement detail count exceeds detail cap")
    detail_artifacts = [client.get(str(row["detail_url"])) for row in new_records]
    recheck = client.get(IMPORTANT_LISTING_URL_TEMPLATE.format(page=1))
    observed_at = recheck.retrieved_at_utc
    return build_update_candidate(
        observation_id=observation_id,
        observed_at=observed_at,
        previous_manifest=previous_manifest,
        listing_artifacts=listing_artifacts,
        page1_recheck=recheck,
        detail_artifacts=detail_artifacts,
        page_cap=page_cap,
        detail_cap=detail_cap,
    )


def _manifest_path(root: pathlib.Path, observation_id: str) -> pathlib.Path:
    return root / observation_id / "manifest.json"


def load_listing_manifest(root: pathlib.Path, observation_id: str) -> dict[str, Any]:
    """Load and minimally validate one listing observation manifest."""
    _validate_observation_id(observation_id)
    path = _manifest_path(root, observation_id)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"announcement listing manifest must be an object: {path}")
    if value.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError(f"unsupported announcement listing manifest: {observation_id}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"unsupported announcement listing schema: {observation_id}")
    if value.get("observation_id") != observation_id:
        raise ValueError("announcement listing directory and manifest ID differ")
    predecessor = value.get("previous_observation_id")
    kind = value.get("observation_kind")
    observed = value.get("observed_at_utc")
    if predecessor is None:
        if kind != "bootstrap" or observed is not None:
            raise ValueError("announcement listing root must be an undated bootstrap")
    else:
        if not isinstance(predecessor, str):
            raise ValueError("announcement listing predecessor is invalid")
        _validate_observation_id(predecessor)
        if kind != "validated_poll":
            raise ValueError("announcement listing non-root kind differs")
        _canonical_timestamp(observed, "announcement observation time")
    return value


def _directory_ids(root: pathlib.Path) -> set[str]:
    if not root.exists():
        return set()
    identifiers: set[str] = set()
    for path in root.iterdir():
        if path.name == "objects":
            if path.is_symlink() or not path.is_dir():
                raise ValueError("announcement objects entry is not a directory")
            continue
        if (
            path.name.startswith(".")
            or path.is_symlink()
            or not path.is_dir()
            or not OBSERVATION_ID_RE.fullmatch(path.name)
        ):
            raise ValueError(f"unexpected announcement observation entry: {path.name}")
        identifiers.add(path.name)
    return identifiers


def list_listing_observation_ids(root: pathlib.Path) -> list[str]:
    """Return predecessor order while rejecting branches, cycles, and orphans."""
    identifiers = _directory_ids(root)
    if not identifiers:
        return []
    previous = {
        observation_id: load_listing_manifest(root, observation_id).get(
            "previous_observation_id"
        )
        for observation_id in identifiers
    }
    roots = [key for key, value in previous.items() if value is None]
    if len(roots) != 1:
        raise ValueError("announcement listing chain must have exactly one root")
    children: dict[str, list[str]] = defaultdict(list)
    for observation_id, predecessor in previous.items():
        if predecessor is None:
            continue
        if predecessor not in identifiers:
            raise ValueError("announcement listing chain has an orphan")
        children[str(predecessor)].append(observation_id)
    if any(len(values) > 1 for values in children.values()):
        raise ValueError("announcement listing chain contains a branch")
    ordered: list[str] = []
    current = roots[0]
    while True:
        if current in ordered:
            raise ValueError("announcement listing chain contains a cycle")
        ordered.append(current)
        descendants = children.get(current, [])
        if not descendants:
            break
        current = descendants[0]
    if set(ordered) != identifiers:
        raise ValueError("announcement listing chain contains a disconnected cycle")
    return ordered


def latest_listing_observation_id(root: pathlib.Path) -> str | None:
    identifiers = list_listing_observation_ids(root)
    return identifiers[-1] if identifiers else None


def _artifact_from_entry(root: pathlib.Path, entry: dict[str, Any]) -> ResponseArtifact:
    required = {
        "sequence",
        "source_url",
        "final_url",
        "status",
        "content_type",
        "charset",
        "retrieved_at_utc",
        "object_path",
        "sha256",
        "byte_length",
    }
    if not required.issubset(entry):
        raise ValueError("announcement artifact provenance fields are missing")
    digest = str(entry["sha256"])
    expected_path = f"objects/{digest}.html"
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or entry["object_path"] != expected_path:
        raise ValueError("announcement artifact is not content-addressed")
    root_resolved = root.resolve()
    path = (root / expected_path).resolve()
    try:
        path.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError("announcement artifact path escapes observation root") from exc
    data = path.read_bytes()
    if len(data) != entry["byte_length"] or _sha256(data) != digest:
        raise ValueError("announcement artifact content integrity mismatch")
    return ResponseArtifact(
        sequence=int(entry["sequence"]),
        source_url=str(entry["source_url"]),
        final_url=str(entry["final_url"]),
        status=int(entry["status"]),
        content_type=(
            str(entry["content_type"])
            if entry["content_type"] is not None
            else None
        ),
        charset=(str(entry["charset"]) if entry["charset"] is not None else None),
        retrieved_at_utc=str(entry["retrieved_at_utc"]),
        body=data,
    )


def _candidate_from_stored(
    root: pathlib.Path,
    manifest: dict[str, Any],
    previous: dict[str, Any] | None,
) -> ListingObservationCandidate:
    artifacts = manifest.get("artifacts")
    coverage = manifest.get("coverage")
    if not isinstance(artifacts, dict) or not isinstance(coverage, dict):
        raise ValueError("announcement listing manifest sections are missing")
    listing_entries = artifacts.get("listing_pages")
    recheck_entry = artifacts.get("page1_recheck")
    detail_entries = artifacts.get("details")
    if (
        not isinstance(listing_entries, list)
        or not listing_entries
        or not isinstance(recheck_entry, dict)
        or not isinstance(detail_entries, list)
    ):
        raise ValueError("announcement listing artifact inventory differs")
    listing = [
        _artifact_from_entry(root, entry)
        for entry in listing_entries
        if isinstance(entry, dict)
    ]
    if len(listing) != len(listing_entries):
        raise ValueError("announcement listing artifact entry is invalid")
    recheck = _artifact_from_entry(root, recheck_entry)
    details = [
        _artifact_from_entry(root, entry)
        for entry in detail_entries
        if isinstance(entry, dict)
    ]
    if len(details) != len(detail_entries):
        raise ValueError("announcement detail artifact entry is invalid")
    if previous is None:
        return build_bootstrap_candidate(
            observation_id=str(manifest["observation_id"]),
            listing_artifacts=listing,
            page1_recheck=recheck,
            bootstrap_pages=int(coverage["bootstrap_pages"]),
            page_cap=int(coverage["page_cap"]),
            detail_cap=int(coverage["detail_cap"]),
        )
    return build_update_candidate(
        observation_id=str(manifest["observation_id"]),
        observed_at=str(manifest["observed_at_utc"]),
        previous_manifest=previous,
        listing_artifacts=listing,
        page1_recheck=recheck,
        detail_artifacts=details,
        page_cap=int(coverage["page_cap"]),
        detail_cap=int(coverage["detail_cap"]),
    )


def aggregate_discovery_csv(root: pathlib.Path) -> bytes:
    """Deterministically aggregate discovery rows across predecessor order."""
    rows: list[dict[str, object]] = []
    for observation_id in list_listing_observation_ids(root):
        path = root / observation_id / "discoveries.csv"
        reader = csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"), newline=""))
        if reader.fieldnames != DISCOVERY_FIELDS:
            raise ValueError(f"announcement discovery schema differs: {observation_id}")
        rows.extend(dict(row) for row in reader)
    return _csv_bytes(rows)


def verify_listing_observations(
    root: pathlib.Path, *, aggregate_data: bytes | None = None
) -> dict[str, object]:
    """Fully rederive the chain, its diffs, details, and optional live aggregate."""
    identifiers = list_listing_observation_ids(root)
    if not identifiers:
        raise ValueError(f"no announcement listing observations in {root}")
    previous: dict[str, Any] | None = None
    previous_time: dt.datetime | None = None
    referenced: set[str] = set()
    discovery_count = 0
    new_count = 0
    for observation_id in identifiers:
        observation_directory = root / observation_id
        children = list(observation_directory.iterdir())
        if {path.name for path in children} != {"manifest.json", "discoveries.csv"}:
            raise ValueError(
                f"announcement observation node layout differs: {observation_id}"
            )
        if any(path.is_symlink() or not path.is_file() for path in children):
            raise ValueError(
                f"announcement observation node contains a non-file: {observation_id}"
            )
        manifest_path = _manifest_path(root, observation_id)
        manifest_data = manifest_path.read_bytes()
        manifest = load_listing_manifest(root, observation_id)
        if manifest.get("previous_observation_id") != (
            previous.get("observation_id") if previous else None
        ):
            raise ValueError("announcement listing predecessor order differs")
        observed = manifest.get("observed_at_utc")
        if observed is not None:
            current_time = dt.datetime.fromisoformat(str(observed).replace("Z", "+00:00"))
            if previous_time is not None and current_time < previous_time:
                raise ValueError("announcement listing observation time moves backward")
            previous_time = current_time
        candidate = _candidate_from_stored(root, manifest, previous)
        discoveries_path = root / observation_id / "discoveries.csv"
        discoveries_data = discoveries_path.read_bytes()
        if candidate.manifest_data != manifest_data:
            raise ValueError(
                "announcement listing manifest is not reproducible: "
                f"{observation_id}"
            )
        if candidate.discoveries_data != discoveries_data:
            raise ValueError(
                f"announcement discoveries are not reproducible: {observation_id}"
            )
        referenced.update(candidate.objects)
        discovery_count += int(manifest["discoveries"]["record_count"])
        new_count += len(manifest["coverage"]["new_ids"])
        previous = manifest

    objects_root = root / "objects"
    if objects_root.is_symlink() or not objects_root.is_dir():
        raise ValueError("announcement object store is missing or invalid")
    object_entries = list(objects_root.rglob("*"))
    if any(
        path.is_symlink() or not path.is_file() or path.parent != objects_root
        for path in object_entries
    ):
        raise ValueError("announcement object store contains nested or non-file entries")
    # POSIX 正規化：manifest 存 `objects/<hash>.html`，Windows 的 str() 會給
    # 反斜線，兩集合永不相交。同 observations._relative_key 的理由。
    actual = {path.relative_to(root).as_posix() for path in object_entries}
    if actual - referenced:
        raise ValueError(f"unreferenced announcement objects: {sorted(actual - referenced)}")
    if referenced - actual:
        raise ValueError(f"missing announcement objects: {sorted(referenced - actual)}")
    expected_aggregate = aggregate_discovery_csv(root)
    if aggregate_data is not None and aggregate_data != expected_aggregate:
        raise ValueError("live aggregate announcement discovery CSV differs")
    latest = previous or {}
    return {
        "observation_count": len(identifiers),
        "latest_observation_id": identifiers[-1],
        "discovery_count": discovery_count,
        "new_id_count": new_count,
        "known_id_count": len(latest.get("coverage", {}).get("known_ids", [])),
        "aggregate_sha256": _sha256(expected_aggregate),
    }


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_durable(path: pathlib.Path, data: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def publish_listing_observation(
    root: pathlib.Path, candidate: ListingObservationCandidate
) -> pathlib.Path:
    """Durably publish content objects and one predecessor-linked observation."""
    root.mkdir(parents=True, exist_ok=True)
    _validate_observation_id(candidate.observation_id)
    manifest = json.loads(candidate.manifest_data)
    if manifest.get("observation_id") != candidate.observation_id:
        raise ValueError("announcement candidate ID differs from its manifest")
    expected = latest_listing_observation_id(root)
    if manifest.get("previous_observation_id") != expected:
        raise ValueError("announcement candidate predecessor is not the chain tip")
    target = root / candidate.observation_id
    if target.exists():
        raise FileExistsError(f"refusing to overwrite announcement observation: {target}")
    objects_root = root / "objects"
    objects_root.mkdir(exist_ok=True)
    for relative, data in candidate.objects.items():
        path = root / relative
        if path.parent != objects_root or path.name != f"{_sha256(data)}.html":
            raise ValueError(f"unexpected announcement object path: {relative}")
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f"announcement object collision: {path}")
            continue
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        _write_durable(temporary, data)
        temporary.replace(path)
        _fsync_directory(objects_root)
    with tempfile.TemporaryDirectory(prefix=".listing-observation-", dir=root) as temporary:
        stage = pathlib.Path(temporary)
        _write_durable(stage / "manifest.json", candidate.manifest_data)
        _write_durable(stage / "discoveries.csv", candidate.discoveries_data)
        _fsync_directory(stage)
        stage.replace(target)
        _fsync_directory(root)
    return target


def remove_listing_observation(root: pathlib.Path, observation_id: str) -> None:
    """Remove only one validated immediate-child observation directory."""
    _validate_observation_id(observation_id)
    root_resolved = root.resolve()
    target = (root / observation_id).resolve()
    if target.parent != root_resolved:
        raise ValueError("announcement observation removal target escapes root")
    if target.exists():
        shutil.rmtree(target)
        _fsync_directory(root)


def referenced_listing_object_paths(root: pathlib.Path) -> set[str]:
    references: set[str] = set()
    for observation_id in list_listing_observation_ids(root):
        manifest = load_listing_manifest(root, observation_id)
        artifacts = manifest.get("artifacts", {})
        entries = list(artifacts.get("listing_pages", []))
        entries.append(artifacts.get("page1_recheck"))
        entries.extend(artifacts.get("details", []))
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("announcement artifact inventory is invalid")
            references.add(str(entry.get("object_path", "")))
    return references


def prune_unreferenced_listing_objects(root: pathlib.Path) -> list[pathlib.Path]:
    """Remove abandoned staging entries and objects not referenced by the chain."""
    removed: list[pathlib.Path] = []
    if root.exists():
        for path in root.iterdir():
            if path.name.startswith(".listing-observation-") and path.is_dir():
                shutil.rmtree(path)
                removed.append(path)
        if removed:
            _fsync_directory(root)
    objects_root = root / "objects"
    if not objects_root.exists():
        return removed
    referenced = referenced_listing_object_paths(root)
    object_removed = False
    for path in list(objects_root.iterdir()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or path.is_dir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
            removed.append(path)
            object_removed = True
        elif path.is_file() and relative not in referenced:
            path.unlink()
            removed.append(path)
            object_removed = True
    if object_removed:
        _fsync_directory(objects_root)
    return removed


def listing_change_summary(manifest: dict[str, Any]) -> dict[str, int]:
    """Return compact counts for CLI output without assigning policy meaning."""
    coverage = manifest.get("coverage", {})
    return dict(
        Counter(
            {
                "pages": int(coverage.get("pages_fetched", 0)),
                "covered": int(coverage.get("records_covered", 0)),
                "new": len(coverage.get("new_ids", [])),
                "metadata_changed": len(coverage.get("metadata_changed_ids", [])),
            }
        )
    )
