"""Immutable observations and record-level diffs for root external snapshots.

An observation stores content-addressed source bytes plus a deterministic diff
against its predecessor. A missing record is described only as
``removed_from_source``; it is never interpreted as a resolved sanction, closed
institution, compliant vehicle, or low-risk outcome.
"""

from __future__ import annotations

import contextlib
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
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from .. import filelock

SOURCE_FILES = ("preschools.json", "punish_all.json", "kids_vehicles.json")
SOURCE_URLS = {
    filename: f"https://kiang.github.io/ap.ece.moe.edu.tw/{filename}"
    for filename in SOURCE_FILES
}
OBSERVATION_SCHEMA_VERSION = "external-observation-v1"
RECORD_SCHEMA_VERSIONS = {
    "preschools.json": "registry-uuid-record-v1",
    "punish_all.json": "conservative-penalty-occurrence-v1",
    "kids_vehicles.json": "institution-plate-occurrence-v1",
}
OBSERVATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LAW_CITATION_RE = re.compile(r"第\s*(\d+)\s*條(?:\s*第\s*(\d+)\s*項)?")
REMOVAL_SEMANTICS = (
    "removed_from_source means only that a record present in the previous "
    "observation is absent now. It is not evidence of resolution, compliance, "
    "institution closure, vehicle inactivity, or low risk."
)
CHANGE_FIELDS = [
    "change_id",
    "source_file",
    "change_type",
    "record_key",
    "record_registry_id",
    "old_record_hash",
    "new_record_hash",
    "observed_at",
    "absence_semantics",
    "details_json",
]


@dataclasses.dataclass(frozen=True)
class SourceRecord:
    record_key: str
    record_hash: str
    registry_id: str
    identity_parts: tuple[str, ...]
    value: object


@dataclasses.dataclass(frozen=True)
class ObservationBundle:
    observation_id: str
    manifest_data: bytes
    changes_data: bytes
    objects: dict[str, bytes]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_value(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _canonical_hash(value: object) -> str:
    return sha256(_json_value(value).encode("utf-8"))


def _digest(parts: Iterable[object]) -> str:
    return _canonical_hash([str(part or "") for part in parts])[:20]


def _iso_timestamp(value: object, label: str, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if not text and allow_empty:
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


def _decode_source(filename: str, data: bytes) -> object:
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"observation source is not valid JSON: {filename}") from exc


def _decode_sources(payloads: dict[str, bytes]) -> dict[str, object]:
    if set(payloads) != set(SOURCE_FILES):
        raise ValueError("observation payload set differs from root source contract")
    return {
        filename: _decode_source(filename, payloads[filename])
        for filename in SOURCE_FILES
    }


def _preschool_records(raw: object) -> dict[str, SourceRecord]:
    if not isinstance(raw, dict) or raw.get("type") != "FeatureCollection":
        raise ValueError("preschools observation must be a GeoJSON FeatureCollection")
    features = raw.get("features")
    if not isinstance(features, list):
        raise ValueError("preschools observation features must be an array")
    records: dict[str, SourceRecord] = {}
    for feature in features:
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise ValueError("preschools observation contains an invalid feature")
        identifier = str(feature["properties"].get("id") or "")
        if not identifier:
            raise ValueError("preschool feature has a blank registry UUID")
        key = f"preschool:{identifier}"
        if key in records:
            raise ValueError(f"duplicate preschool registry UUID: {identifier}")
        records[key] = SourceRecord(
            record_key=key,
            record_hash=_canonical_hash(feature),
            registry_id=identifier,
            identity_parts=(identifier,),
            value=feature,
        )
    return records


def _law_identity(law: object) -> str:
    text = "".join(str(law or "").split())
    match = LAW_CITATION_RE.search(text)
    if match:
        article, paragraph = match.groups(default="")
        return f"article:{article}:paragraph:{paragraph}"
    return f"law:{text}"


def _occurrence_records(
    grouped: dict[tuple[str, ...], list[tuple[str, object, str]]],
    *,
    prefix: str,
) -> dict[str, SourceRecord]:
    """Key duplicate records by content, so unrelated duplicates never renumber."""
    records: dict[str, SourceRecord] = {}
    for identity in sorted(grouped):
        identity_digest = _digest(identity)
        by_hash: dict[str, list[tuple[object, str]]] = defaultdict(list)
        for record_hash, value, registry_id in grouped[identity]:
            by_hash[record_hash].append((value, registry_id))
        for record_hash in sorted(by_hash):
            identical = by_hash[record_hash]
            for occurrence, (value, registry_id) in enumerate(identical, start=1):
                key = f"{prefix}:{identity_digest}:{record_hash}:{occurrence}"
                records[key] = SourceRecord(
                    record_key=key,
                    record_hash=record_hash,
                    registry_id=registry_id,
                    identity_parts=identity,
                    value=value,
                )
    return records


def _penalty_records(raw: object) -> dict[str, SourceRecord]:
    if not isinstance(raw, dict):
        raise ValueError("penalty observation root must be an object")
    grouped: dict[tuple[str, ...], list[tuple[str, object, str]]] = defaultdict(list)
    for actor, values in raw.items():
        if not isinstance(values, list):
            raise ValueError(f"penalty actor records must be an array: {actor!r}")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError(f"penalty record must be an object: {actor!r}")
            record = {"actor": str(actor), **value}
            registry_id = str(value.get("id") or "")
            identity = (
                registry_id,
                str(value.get("date") or "").strip(),
                str(actor),
                _law_identity(value.get("law")),
                str(value.get("punishment") or "").strip(),
            )
            grouped[identity].append(
                (_canonical_hash(record), record, registry_id)
            )
    return _occurrence_records(grouped, prefix="penalty")


def _vehicle_records(raw: object) -> dict[str, SourceRecord]:
    if not isinstance(raw, dict):
        raise ValueError("vehicle observation root must be an object")
    grouped: dict[tuple[str, ...], list[tuple[str, object, str]]] = defaultdict(list)
    for registry_id, values in raw.items():
        if not isinstance(values, list):
            raise ValueError(f"vehicle records must be an array: {registry_id!r}")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError(f"vehicle record must be an object: {registry_id!r}")
            record = {"registry_id": str(registry_id), **value}
            identity = (str(registry_id), str(value.get("plate_no") or "").strip())
            grouped[identity].append(
                (_canonical_hash(record), record, str(registry_id))
            )
    return _occurrence_records(grouped, prefix="vehicle")


def record_maps(payloads: dict[str, bytes]) -> dict[str, dict[str, SourceRecord]]:
    """Create deterministic record indexes for all root snapshot sources."""
    decoded = _decode_sources(payloads)
    return {
        "preschools.json": _preschool_records(decoded["preschools.json"]),
        "punish_all.json": _penalty_records(decoded["punish_all.json"]),
        "kids_vehicles.json": _vehicle_records(decoded["kids_vehicles.json"]),
    }


def _changed_paths(old: object, new: object, prefix: str = "") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        paths: list[str] = []
        for key in sorted(set(old) | set(new)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in old or key not in new:
                paths.append(child)
            else:
                paths.extend(_changed_paths(old[key], new[key], child))
        return paths
    if old != new:
        return [prefix or "$record"]
    return []


def penalty_source_record_hash(
    *, actor: object, registry_id: object, date: object, law: object, punishment: object
) -> str:
    """Hash one source-faithful penalty row for knowledge-time lookup."""
    return _canonical_hash(
        {
            "actor": str(actor or ""),
            "id": str(registry_id or ""),
            "date": str(date or ""),
            "law": str(law or ""),
            "punishment": str(punishment or ""),
        }
    )


def _reconciliation_pairs(
    filename: str,
    old_records: dict[str, SourceRecord],
    new_records: dict[str, SourceRecord],
) -> list[tuple[SourceRecord, SourceRecord, str]]:
    """Conservatively pair unique source corrections without inventing identity."""
    candidates: dict[str, list[str]] = defaultdict(list)
    reverse: dict[str, list[str]] = defaultdict(list)
    for old_key, old in old_records.items():
        for new_key, new in new_records.items():
            method = ""
            if filename == "kids_vehicles.json":
                if old.identity_parts == new.identity_parts:
                    method = "unique_same_institution_and_plate"
            elif filename == "punish_all.json":
                if old.registry_id != new.registry_id:
                    continue
                agreements = sum(
                    left == right
                    for left, right in zip(
                        old.identity_parts[1:], new.identity_parts[1:]
                    )
                )
                if agreements >= 3:
                    method = "unique_registry_and_three_of_four_penalty_fields"
            if method:
                candidates[old_key].append(new_key)
                reverse[new_key].append(old_key)
    pairs: list[tuple[SourceRecord, SourceRecord, str]] = []
    for old_key, new_keys in candidates.items():
        if len(new_keys) != 1 or len(reverse[new_keys[0]]) != 1:
            continue
        new_key = new_keys[0]
        method = (
            "unique_same_institution_and_plate"
            if filename == "kids_vehicles.json"
            else "unique_registry_and_three_of_four_penalty_fields"
        )
        pairs.append((old_records[old_key], new_records[new_key], method))
    return pairs


def _change_row(
    *,
    filename: str,
    change_type: str,
    old: SourceRecord | None,
    new: SourceRecord | None,
    observed_at: str,
    details: dict[str, object] | None = None,
) -> dict[str, str]:
    selected = new or old
    if selected is None:
        raise AssertionError("a change must have an old or new record")
    old_hash = old.record_hash if old else ""
    new_hash = new.record_hash if new else ""
    record_key = old.record_key if old else selected.record_key
    change_id = "chg_" + _digest(
        (
            filename,
            change_type,
            old.record_key if old else "",
            new.record_key if new else "",
            old_hash,
            new_hash,
        )
    )
    return {
        "change_id": change_id,
        "source_file": filename,
        "change_type": change_type,
        "record_key": record_key,
        "record_registry_id": selected.registry_id,
        "old_record_hash": old_hash,
        "new_record_hash": new_hash,
        "observed_at": observed_at,
        "absence_semantics": (
            REMOVAL_SEMANTICS if change_type == "removed_from_source" else ""
        ),
        "details_json": _json_value(details or {}),
    }


def build_changes(
    previous_payloads: dict[str, bytes] | None,
    current_payloads: dict[str, bytes],
    *,
    observed_at: str,
) -> list[dict[str, str]]:
    """Return deterministic changes; a baseline intentionally emits no rows."""
    observed = _iso_timestamp(observed_at, "observation time", allow_empty=True)
    current = record_maps(current_payloads)
    if previous_payloads is None:
        return []
    previous = record_maps(previous_payloads)
    changes: list[dict[str, str]] = []
    for filename in SOURCE_FILES:
        old_records = previous[filename]
        new_records = current[filename]
        common_keys = set(old_records) & set(new_records)
        for record_key in sorted(common_keys):
            old = old_records[record_key]
            new = new_records[record_key]
            if old.record_hash == new.record_hash:
                continue
            changes.append(
                _change_row(
                    filename=filename,
                    change_type="changed",
                    old=old,
                    new=new,
                    observed_at=observed,
                    details={"changed_fields": _changed_paths(old.value, new.value)},
                )
            )

        unmatched_old = {
            key: value for key, value in old_records.items() if key not in common_keys
        }
        unmatched_new = {
            key: value for key, value in new_records.items() if key not in common_keys
        }
        reconciled_old: set[str] = set()
        reconciled_new: set[str] = set()
        for old, new, method in _reconciliation_pairs(
            filename, unmatched_old, unmatched_new
        ):
            reconciled_old.add(old.record_key)
            reconciled_new.add(new.record_key)
            changes.append(
                _change_row(
                    filename=filename,
                    change_type="changed",
                    old=old,
                    new=new,
                    observed_at=observed,
                    details={
                        "changed_fields": _changed_paths(old.value, new.value),
                        "replacement_record_key": new.record_key,
                        "reconciliation": method,
                    },
                )
            )
        changes.extend(
            _change_row(
                filename=filename,
                change_type="removed_from_source",
                old=unmatched_old[key],
                new=None,
                observed_at=observed,
            )
            for key in sorted(set(unmatched_old) - reconciled_old)
        )
        changes.extend(
            _change_row(
                filename=filename,
                change_type="added",
                old=None,
                new=unmatched_new[key],
                observed_at=observed,
            )
            for key in sorted(set(unmatched_new) - reconciled_new)
        )
    return sorted(
        changes,
        key=lambda row: (
            row["source_file"],
            row["change_type"],
            row["record_key"],
        ),
    )


def changes_csv_bytes(changes: list[dict[str, str]]) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=CHANGE_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(changes)
    return handle.getvalue().encode("utf-8")


def build_observation(
    *,
    observation_id: str,
    current_payloads: dict[str, bytes],
    source_metadata: dict[str, dict[str, Any]],
    previous_observation_id: str | None,
    previous_payloads: dict[str, bytes] | None,
    observed_at: str,
    observation_kind: str,
) -> ObservationBundle:
    """Build an immutable observation bundle without writing files."""
    if not OBSERVATION_ID_RE.fullmatch(observation_id) or observation_id in {".", ".."}:
        raise ValueError("invalid observation ID")
    observed = _iso_timestamp(observed_at, "observation time", allow_empty=True)
    if (previous_observation_id is None) != (previous_payloads is None):
        raise ValueError("previous observation ID and payloads must be supplied together")
    if previous_observation_id is None:
        if observation_kind != "adopted_baseline_without_exact_observation_time":
            raise ValueError("root observation must be an adopted baseline")
        if observed:
            raise ValueError("adopted baseline observation time must be unknown")
    else:
        if observation_kind != "validated_upstream_check":
            raise ValueError("non-root observation must be a validated upstream check")
        if not observed:
            raise ValueError("validated upstream observation requires a timestamp")
    maps = record_maps(current_payloads)
    changes = build_changes(previous_payloads, current_payloads, observed_at=observed)
    changes_data = changes_csv_bytes(changes)
    objects: dict[str, bytes] = {}
    object_entries: dict[str, dict[str, object]] = {}
    for filename in SOURCE_FILES:
        data = current_payloads[filename]
        digest = sha256(data)
        metadata = source_metadata.get(filename)
        if not isinstance(metadata, dict):
            raise ValueError(f"missing source metadata for {filename}")
        if metadata.get("sha256") != digest or metadata.get("byte_length") != len(data):
            raise ValueError(f"source metadata does not match bytes: {filename}")
        if metadata.get("record_count") != len(maps[filename]):
            raise ValueError(f"source metadata record count differs: {filename}")
        object_path = f"objects/{digest}.json"
        objects[object_path] = data
        object_entries[filename] = {
            "object_path": object_path,
            "sha256": digest,
            "byte_length": len(data),
            "record_count": len(maps[filename]),
            "record_schema_version": RECORD_SCHEMA_VERSIONS[filename],
            "source_url": metadata.get("source_url"),
            "final_url": metadata.get("final_url"),
            "retrieved_at_utc": metadata.get("retrieved_at_utc"),
            "retrieved_on": metadata.get("retrieved_on"),
            "last_checked_at_utc": metadata.get("last_checked_at_utc"),
            "http_status": metadata.get("http_status"),
            "etag": metadata.get("etag"),
            "last_modified": metadata.get("last_modified"),
        }
    type_counts = dict(sorted(Counter(row["change_type"] for row in changes).items()))
    source_counts = dict(sorted(Counter(row["source_file"] for row in changes).items()))
    manifest = {
        "manifest_version": 1,
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "observation_id": observation_id,
        "observation_kind": observation_kind,
        "observed_at_utc": observed or None,
        "previous_observation_id": previous_observation_id,
        "objects": object_entries,
        "changes": {
            "path": "changes.csv",
            "sha256": sha256(changes_data),
            "byte_length": len(changes_data),
            "record_count": len(changes),
            "change_type_counts": type_counts,
            "source_counts": source_counts,
            "removal_semantics": REMOVAL_SEMANTICS,
        },
    }
    return ObservationBundle(
        observation_id=observation_id,
        manifest_data=_json_bytes(manifest),
        changes_data=changes_data,
        objects=objects,
    )


def _observation_directory_ids(root: pathlib.Path) -> set[str]:
    if not root.exists():
        return set()
    return {
        path.name
        for path in root.iterdir()
        if path.is_dir()
        and path.name != "objects"
        and not path.name.startswith(".")
        and OBSERVATION_ID_RE.fullmatch(path.name)
    }


def list_observation_ids(root: pathlib.Path) -> list[str]:
    """Return predecessor-linked chain order, rejecting branches and cycles."""
    identifiers = _observation_directory_ids(root)
    if not identifiers:
        return []
    previous = {
        observation_id: _load_manifest(root, observation_id).get(
            "previous_observation_id"
        )
        for observation_id in identifiers
    }
    roots = [key for key, value in previous.items() if value is None]
    if len(roots) != 1:
        raise ValueError(f"observation chain must have one root, found {len(roots)}")
    children: dict[str, list[str]] = defaultdict(list)
    for observation_id, predecessor in previous.items():
        if predecessor is None:
            continue
        if predecessor not in identifiers:
            raise ValueError(
                f"observation has an unknown predecessor: {observation_id}"
            )
        children[str(predecessor)].append(observation_id)
    branches = {key: values for key, values in children.items() if len(values) > 1}
    if branches:
        raise ValueError(f"observation chain contains branches: {branches}")
    ordered: list[str] = []
    current = roots[0]
    while True:
        if current in ordered:
            raise ValueError(f"observation chain contains a cycle at {current}")
        ordered.append(current)
        descendants = children.get(current, [])
        if not descendants:
            break
        current = descendants[0]
    if set(ordered) != identifiers:
        raise ValueError("observation chain contains an orphan or disconnected cycle")
    return ordered


def latest_observation_id(root: pathlib.Path) -> str | None:
    identifiers = list_observation_ids(root)
    return identifiers[-1] if identifiers else None


def _load_manifest(root: pathlib.Path, observation_id: str) -> dict[str, Any]:
    path = root / observation_id / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"observation manifest must be an object: {path}")
    if value.get("manifest_version") != 1:
        raise ValueError(f"unsupported observation manifest version: {observation_id}")
    if value.get("observation_id") != observation_id:
        raise ValueError(f"observation directory and manifest ID differ: {observation_id}")
    if value.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        raise ValueError(f"unsupported observation schema: {observation_id}")
    predecessor = value.get("previous_observation_id")
    if predecessor is not None and (
        not isinstance(predecessor, str)
        or not OBSERVATION_ID_RE.fullmatch(predecessor)
    ):
        raise ValueError(f"invalid observation predecessor: {observation_id}")
    kind = value.get("observation_kind")
    observed_at = value.get("observed_at_utc")
    if predecessor is None:
        if kind != "adopted_baseline_without_exact_observation_time":
            raise ValueError(f"root observation kind differs: {observation_id}")
        if observed_at is not None:
            raise ValueError(f"root observation time must be null: {observation_id}")
    else:
        if kind != "validated_upstream_check":
            raise ValueError(f"non-root observation kind differs: {observation_id}")
        normalized = _iso_timestamp(observed_at, "observation time")
        if observed_at != normalized:
            raise ValueError(f"observation time is not canonical UTC: {observation_id}")
    return value


def _validate_provenance(
    value: dict[str, Any], filename: str, manifest: dict[str, Any]
) -> None:
    provenance_fields = {
        "source_url",
        "final_url",
        "retrieved_at_utc",
        "retrieved_on",
        "last_checked_at_utc",
        "http_status",
        "etag",
        "last_modified",
    }
    if not provenance_fields.issubset(value):
        raise ValueError(f"observation provenance metadata differs: {filename}")
    expected_url = SOURCE_URLS[filename]
    if value.get("source_url") != expected_url:
        raise ValueError(f"observation source URL differs: {filename}")
    final_url = value.get("final_url")
    if not isinstance(final_url, str):
        raise ValueError(f"observation final URL is invalid: {filename}")
    final = urllib.parse.urlparse(final_url)
    expected = urllib.parse.urlparse(expected_url)
    if (
        final.scheme != "https"
        or final.hostname != expected.hostname
        or final.port not in (None, 443)
    ):
        raise ValueError(f"observation final URL origin differs: {filename}")
    for field in ("retrieved_at_utc", "last_checked_at_utc"):
        timestamp = value.get(field)
        if timestamp is not None:
            normalized = _iso_timestamp(timestamp, f"{filename} {field}")
            if timestamp != normalized:
                raise ValueError(
                    f"observation {field} is not canonical UTC: {filename}"
                )
    retrieved_on = value.get("retrieved_on")
    if retrieved_on is not None:
        try:
            dt.date.fromisoformat(str(retrieved_on))
        except ValueError as exc:
            raise ValueError(
                f"observation retrieved_on is not an ISO date: {filename}"
            ) from exc
    status = value.get("http_status")
    kind = manifest.get("observation_kind")
    if kind == "adopted_baseline_without_exact_observation_time":
        no_request_fields = (
            "retrieved_at_utc",
            "last_checked_at_utc",
            "http_status",
            "etag",
            "last_modified",
        )
        if any(value.get(field) is not None for field in no_request_fields):
            raise ValueError(
                f"adopted baseline cannot claim HTTP request metadata: {filename}"
            )
    else:
        observed_at = manifest.get("observed_at_utc")
        if value.get("last_checked_at_utc") != observed_at:
            raise ValueError(
                f"observation check time differs from poll time: {filename}"
            )
        if status not in {200, 304}:
            raise ValueError(f"observation HTTP status is invalid: {filename}")
    for field in ("etag", "last_modified"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise ValueError(f"observation {field} is invalid: {filename}")


def load_observation_payloads(
    root: pathlib.Path, observation_id: str
) -> dict[str, bytes]:
    manifest = _load_manifest(root, observation_id)
    entries = manifest.get("objects")
    if not isinstance(entries, dict) or set(entries) != set(SOURCE_FILES):
        raise ValueError(f"observation object set differs: {observation_id}")
    payloads: dict[str, bytes] = {}
    root_resolved = root.resolve()
    for filename, value in entries.items():
        if not isinstance(value, dict):
            raise ValueError(f"invalid observation object metadata: {filename}")
        digest = str(value.get("sha256", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"observation object hash is invalid: {filename}")
        expected_relative = f"objects/{digest}.json"
        if value.get("object_path") != expected_relative:
            raise ValueError(f"observation object is not content-addressed: {filename}")
        if value.get("record_schema_version") != RECORD_SCHEMA_VERSIONS[filename]:
            raise ValueError(f"observation record schema differs: {filename}")
        _validate_provenance(value, filename, manifest)
        path = (root / expected_relative).resolve()
        try:
            path.relative_to(root_resolved)
        except ValueError as exc:
            raise ValueError(f"observation object escapes root: {filename}") from exc
        data = path.read_bytes()
        if len(data) != value.get("byte_length") or sha256(data) != digest:
            raise ValueError(f"observation object integrity mismatch: {path}")
        payloads[filename] = data
    maps = record_maps(payloads)
    for filename, value in entries.items():
        if len(maps[filename]) != value.get("record_count"):
            raise ValueError(f"observation record count differs: {filename}")
    return payloads


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


@contextlib.contextmanager
def observation_lock(root: pathlib.Path):
    """Hold the non-blocking repository lock shared by all observation writers."""
    root.parent.mkdir(parents=True, exist_ok=True)
    lock_path = root.parent / ".snapshot-update.lock"
    with lock_path.open("a+b") as handle:
        try:
            filelock.acquire(handle, blocking=False)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"another external snapshot update holds {lock_path}"
            ) from exc
        try:
            yield
        finally:
            filelock.release(handle)


def publish_observation(root: pathlib.Path, bundle: ObservationBundle) -> pathlib.Path:
    """Durably publish content objects and one predecessor-linked observation."""
    root.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(bundle.manifest_data)
    if not isinstance(manifest, dict):
        raise ValueError("observation manifest must be an object")
    expected_predecessor = latest_observation_id(root)
    if manifest.get("previous_observation_id") != expected_predecessor:
        raise ValueError("observation predecessor is not the current chain tip")
    target = root / bundle.observation_id
    if target.exists():
        raise FileExistsError(f"refusing to overwrite observation: {target}")
    objects_root = root / "objects"
    objects_root.mkdir(exist_ok=True)
    for relative, data in bundle.objects.items():
        path = root / relative
        if path.parent != objects_root or path.name != f"{sha256(data)}.json":
            raise ValueError(f"unexpected observation object path: {relative}")
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f"content-addressed object collision: {path}")
            continue
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        _write_durable(temporary, data)
        temporary.replace(path)
        _fsync_directory(objects_root)

    with tempfile.TemporaryDirectory(prefix=".observation-", dir=root) as temporary:
        stage = pathlib.Path(temporary)
        _write_durable(stage / "manifest.json", bundle.manifest_data)
        _write_durable(stage / "changes.csv", bundle.changes_data)
        _fsync_directory(stage)
        stage.replace(target)
        _fsync_directory(root)
    return target


def remove_observation(root: pathlib.Path, observation_id: str) -> None:
    """Remove only a newly published observation."""
    target = root / observation_id
    if target.exists():
        shutil.rmtree(target)
        _fsync_directory(root)


def _relative_key(path: pathlib.Path, root: pathlib.Path) -> str:
    """把檔案系統路徑轉成 manifest 裡使用的那一種寫法。

    manifest 存的是 POSIX 形式（`objects/<sha256>.json`，見 publish 端寫入的
    `f"objects/{digest}.json"`），而 `str(path.relative_to(root))` 在 Windows 上
    會給出 `objects\\<sha256>.json`。兩個集合因此永遠不相交，後果分兩種：

    * `verify_observations()` 把每個物件都判成 unreferenced，報「儲存區含未被
      引用的物件」——看起來像資料被竄改，其實只是路徑分隔符；
    * `prune_unreferenced_objects()` 更糟，它會把**整個內容定址儲存區刪光**，
      而它正是 `download_external_snapshots.py` 每次更新快照時會呼叫的函式。

    內容定址的 key 必須與平台無關，所以一律正規化成 POSIX。
    """
    return path.relative_to(root).as_posix()


def referenced_object_paths(root: pathlib.Path) -> set[str]:
    references: set[str] = set()
    for observation_id in list_observation_ids(root):
        manifest = _load_manifest(root, observation_id)
        entries = manifest.get("objects")
        if not isinstance(entries, dict):
            raise ValueError(f"observation object metadata missing: {observation_id}")
        for value in entries.values():
            if not isinstance(value, dict):
                raise ValueError(f"invalid object metadata: {observation_id}")
            references.add(str(value.get("object_path", "")))
    return references


def prune_unreferenced_objects(root: pathlib.Path) -> list[pathlib.Path]:
    """Delete only content objects not referenced by any published observation."""
    objects_root = root / "objects"
    if not objects_root.exists():
        return []
    referenced = referenced_object_paths(root)
    removed: list[pathlib.Path] = []
    for path in objects_root.iterdir():
        relative = _relative_key(path, root)
        if path.is_file() and relative not in referenced:
            path.unlink()
            removed.append(path)
    return removed


def first_observed_record_hashes(
    root: pathlib.Path, source_file: str
) -> dict[str, str]:
    """Map each source record version hash to its first known observation time."""
    if source_file not in SOURCE_FILES:
        raise ValueError(f"unsupported observation source: {source_file}")
    first_seen: dict[str, str] = {}
    for observation_id in list_observation_ids(root):
        manifest = _load_manifest(root, observation_id)
        observed_at = str(manifest.get("observed_at_utc") or "")
        payloads = load_observation_payloads(root, observation_id)
        for record in record_maps(payloads)[source_file].values():
            if record.record_hash not in first_seen:
                first_seen[record.record_hash] = observed_at
    return first_seen


def verify_observations(
    root: pathlib.Path, *, current_payloads: dict[str, bytes] | None = None
) -> dict[str, object]:
    """Verify the complete chain and optionally bind its latest node to root files."""
    identifiers = list_observation_ids(root)
    if not identifiers:
        raise ValueError(f"no immutable observations in {root}")
    previous_id: str | None = None
    previous_manifest: dict[str, Any] | None = None
    previous_payloads: dict[str, bytes] | None = None
    previous_time: dt.datetime | None = None
    total_changes = 0
    latest_payloads: dict[str, bytes] | None = None
    for observation_id in identifiers:
        manifest = _load_manifest(root, observation_id)
        if manifest.get("previous_observation_id") != previous_id:
            raise ValueError(f"observation predecessor chain differs: {observation_id}")
        observed_at = str(manifest.get("observed_at_utc") or "")
        if observed_at:
            current_time = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            if previous_time is not None and current_time < previous_time:
                raise ValueError(f"observation time moves backward: {observation_id}")
            previous_time = current_time
        payloads = load_observation_payloads(root, observation_id)
        if previous_manifest is not None:
            current_objects = manifest.get("objects")
            previous_objects = previous_manifest.get("objects")
            if not isinstance(current_objects, dict) or not isinstance(
                previous_objects, dict
            ):
                raise ValueError(
                    f"observation object metadata missing: {observation_id}"
                )
            for filename in SOURCE_FILES:
                current_entry = current_objects[filename]
                previous_entry = previous_objects[filename]
                if not isinstance(current_entry, dict) or not isinstance(
                    previous_entry, dict
                ):
                    raise ValueError(
                        f"observation object metadata differs: {observation_id}"
                    )
                changed = current_entry.get("sha256") != previous_entry.get("sha256")
                if changed:
                    if current_entry.get("http_status") != 200:
                        raise ValueError(
                            f"changed object lacks HTTP 200 provenance: {filename}"
                        )
                    if current_entry.get("retrieved_at_utc") != observed_at:
                        raise ValueError(
                            f"changed object retrieval time differs: {filename}"
                        )
                    if current_entry.get("retrieved_on") != observed_at[:10]:
                        raise ValueError(
                            f"changed object retrieval date differs: {filename}"
                        )
                else:
                    for field in ("retrieved_at_utc", "retrieved_on"):
                        if current_entry.get(field) != previous_entry.get(field):
                            raise ValueError(
                                f"unchanged object {field} differs: {filename}"
                            )
        expected_changes = changes_csv_bytes(
            build_changes(previous_payloads, payloads, observed_at=observed_at)
        )
        changes_meta = manifest.get("changes")
        if not isinstance(changes_meta, dict):
            raise ValueError(f"observation changes metadata missing: {observation_id}")
        if changes_meta.get("path") != "changes.csv":
            raise ValueError(f"observation changes path differs: {observation_id}")
        changes_path = root / observation_id / "changes.csv"
        actual_changes = changes_path.read_bytes()
        if actual_changes != expected_changes:
            raise ValueError(f"observation changes are not reproducible: {observation_id}")
        if (
            len(actual_changes) != changes_meta.get("byte_length")
            or sha256(actual_changes) != changes_meta.get("sha256")
        ):
            raise ValueError(f"observation changes integrity mismatch: {observation_id}")
        reader = csv.DictReader(io.StringIO(actual_changes.decode("utf-8")))
        rows = list(reader)
        if reader.fieldnames != CHANGE_FIELDS:
            raise ValueError(f"observation change schema differs: {observation_id}")
        expected_type_counts = dict(
            sorted(Counter(row["change_type"] for row in rows).items())
        )
        expected_source_counts = dict(
            sorted(Counter(row["source_file"] for row in rows).items())
        )
        if len(rows) != changes_meta.get("record_count"):
            raise ValueError(f"observation change count differs: {observation_id}")
        if changes_meta.get("change_type_counts") != expected_type_counts:
            raise ValueError(f"observation change type counts differ: {observation_id}")
        if changes_meta.get("source_counts") != expected_source_counts:
            raise ValueError(f"observation source counts differ: {observation_id}")
        if changes_meta.get("removal_semantics") != REMOVAL_SEMANTICS:
            raise ValueError(f"observation removal semantics differ: {observation_id}")
        total_changes += len(rows)
        previous_id = observation_id
        previous_manifest = manifest
        previous_payloads = payloads
        latest_payloads = payloads

    referenced = referenced_object_paths(root)
    objects_root = root / "objects"
    actual_objects = {
        _relative_key(path, root)
        for path in objects_root.iterdir()
        if path.is_file()
    }
    unexpected = actual_objects - referenced
    if unexpected:
        raise ValueError(
            f"observation store contains unreferenced objects: {sorted(unexpected)}"
        )
    missing = referenced - actual_objects
    if missing:
        raise ValueError(f"observation store lacks referenced objects: {sorted(missing)}")

    if current_payloads is not None:
        if set(current_payloads) != set(SOURCE_FILES):
            raise ValueError("current root payload set differs")
        for filename in SOURCE_FILES:
            latest = latest_payloads and latest_payloads[filename]
            if latest != current_payloads[filename]:
                raise ValueError(
                    f"latest immutable observation differs from root snapshot: {filename}"
                )
    return {
        "observation_count": len(identifiers),
        "latest_observation_id": identifiers[-1],
        "total_changes": total_changes,
    }
