"""Explicitly update or adopt the versioned external JSON snapshots.

The updater validates all three payloads before replacing any pinned file and
writes data/external/manifest.json with hashes, HTTP provenance, counts, and
quality summaries.

Examples
--------
Adopt the repository's existing snapshots without network access::

    .venv/bin/python scripts/download_external_snapshots.py \
        --adopt-existing --retrieved-on 2026-08-10

Check upstream and update changed snapshots::

    .venv/bin/python scripts/download_external_snapshots.py
"""

from __future__ import annotations

import argparse
import base64
import binascii
import collections
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.build import classify_vehicle_status
from smart_watchdog.scrape.observations import (
    build_observation,
    latest_observation_id,
    load_observation_payloads,
    observation_lock,
    prune_unreferenced_objects,
    publish_observation,
    remove_observation,
    verify_observations,
)
from smart_watchdog.scrape.registry import sanction_type

EXTERNAL_DIR = pathlib.Path("data/external")
OBSERVATIONS_DIR = EXTERNAL_DIR / "observations"
MANIFEST_PATH = EXTERNAL_DIR / "manifest.json"
TRANSACTION_PATH = EXTERNAL_DIR / ".snapshot-update-transaction.json"
TRANSACTION_VERSION = 1
MIRROR = "https://kiang.github.io/ap.ece.moe.edu.tw"
SOURCES = {
    "preschools.json": f"{MIRROR}/preschools.json",
    "punish_all.json": f"{MIRROR}/punish_all.json",
    "kids_vehicles.json": f"{MIRROR}/kids_vehicles.json",
}
SCHEMA_VERSIONS = {
    "preschools.json": "preschools-geojson-v1",
    "punish_all.json": "actor-keyed-penalties-v1",
    "kids_vehicles.json": "preschool-keyed-vehicles-v1",
}
REQUIRED_PRESCHOOL_FIELDS = {"id", "title", "city", "type"}
REQUIRED_PENALTY_FIELDS = {"id", "date", "law", "punishment"}
REQUIRED_VEHICLE_FIELDS = {
    "plate_no",
    "on_production_date",
    "next_exam_dt",
    "txn_name",
}


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def _iso(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_json(name: str, data: bytes) -> object:
    if not data:
        raise ValueError(f"{name}: empty response")
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{name}: response is not valid JSON") from exc


def _validate_preschools(raw: object) -> tuple[int, set[str], dict[str, Any]]:
    if not isinstance(raw, dict) or raw.get("type") != "FeatureCollection":
        raise ValueError("preschools.json: expected a GeoJSON FeatureCollection")
    features = raw.get("features")
    if not isinstance(features, list) or not features:
        raise ValueError("preschools.json: features must be a non-empty list")
    ids: set[str] = set()
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or not isinstance(feature.get("properties"), dict):
            raise ValueError(f"preschools.json: invalid feature at index {index}")
        properties = feature["properties"]
        missing = REQUIRED_PRESCHOOL_FIELDS - properties.keys()
        if missing:
            raise ValueError(f"preschools.json: feature {index} missing {sorted(missing)}")
        ids.add(str(properties["id"]))
    if len(ids) != len(features):
        raise ValueError("preschools.json: duplicate registry UUIDs")
    return len(features), ids, {"unique_registry_ids": len(ids)}


def _validate_penalties(raw: object) -> tuple[int, set[str], dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("punish_all.json: expected a non-empty actor object")
    count = 0
    ids: set[str] = set()
    dates: list[str] = []
    roles: collections.Counter[str] = collections.Counter()
    for actor, records in raw.items():
        if not isinstance(records, list):
            raise ValueError(f"punish_all.json: actor {actor!r} is not a list")
        role = str(actor).split("：", 1)[0]
        roles[role] += len(records)
        for record in records:
            if not isinstance(record, dict):
                raise ValueError(f"punish_all.json: non-object record for {actor!r}")
            missing = REQUIRED_PENALTY_FIELDS - record.keys()
            if missing:
                raise ValueError(f"punish_all.json: record missing {sorted(missing)}")
            count += 1
            ids.add(str(record["id"]))
            dates.append(str(record["date"]))
            sanction_type(record["punishment"])
    return count, ids, {
        "actor_keys": len(raw),
        "actor_role_record_counts": dict(sorted(roles.items())),
        "max_penalty_date": max(dates) if dates else None,
    }


def _validate_vehicles(raw: object) -> tuple[int, set[str], dict[str, Any]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("kids_vehicles.json: expected a non-empty UUID object")
    count = 0
    ids: set[str] = set()
    exam_dates: list[str] = []
    statuses: collections.Counter[str] = collections.Counter()
    status_classes: collections.Counter[str] = collections.Counter()
    for preschool_id, vehicles in raw.items():
        if not isinstance(vehicles, list):
            raise ValueError(f"kids_vehicles.json: {preschool_id} is not a list")
        ids.add(str(preschool_id))
        for vehicle in vehicles:
            if not isinstance(vehicle, dict):
                raise ValueError(f"kids_vehicles.json: non-object vehicle for {preschool_id}")
            missing = REQUIRED_VEHICLE_FIELDS - vehicle.keys()
            if missing:
                raise ValueError(f"kids_vehicles.json: vehicle missing {sorted(missing)}")
            txn_name = str(vehicle.get("txn_name") or "").strip()
            status_class = classify_vehicle_status(txn_name)
            statuses[txn_name] += 1
            status_classes[status_class] += 1
            count += 1
            if vehicle.get("next_exam_dt"):
                exam_dates.append(str(vehicle["next_exam_dt"]))
    unknown = sorted(
        status for status in statuses if classify_vehicle_status(status) == "unknown"
    )
    if unknown:
        raise ValueError(f"kids_vehicles.json: unclassified txn_name values: {unknown}")
    return count, ids, {
        "institution_ids": len(ids),
        "transaction_status_counts": dict(sorted(statuses.items())),
        "status_class_counts": dict(sorted(status_classes.items())),
        "max_next_exam_dt": max(exam_dates) if exam_dates else None,
        "next_exam_dt_is_risk_label": False,
    }


def _orphan_summary(ids: set[str], preschool_ids: set[str]) -> dict[str, Any]:
    orphans = sorted(ids - preschool_ids)
    return {
        "orphan_registry_id_count": len(orphans),
        "orphan_registry_id_sample": orphans[:20],
    }


def _validate_all(payloads: dict[str, object]) -> dict[str, dict[str, Any]]:
    preschool_count, preschool_ids, preschool_quality = _validate_preschools(
        payloads["preschools.json"]
    )
    penalty_count, penalty_ids, penalty_quality = _validate_penalties(
        payloads["punish_all.json"]
    )
    vehicle_count, vehicle_ids, vehicle_quality = _validate_vehicles(
        payloads["kids_vehicles.json"]
    )
    penalty_quality.update(_orphan_summary(penalty_ids, preschool_ids))
    vehicle_quality.update(_orphan_summary(vehicle_ids, preschool_ids))
    return {
        "preschools.json": {"record_count": preschool_count, "quality": preschool_quality},
        "punish_all.json": {"record_count": penalty_count, "quality": penalty_quality},
        "kids_vehicles.json": {"record_count": vehicle_count, "quality": vehicle_quality},
    }


def _load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        return {"files": {}}
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("files"), dict):
        raise ValueError(f"invalid manifest: {MANIFEST_PATH}")
    return raw


def _verify_existing_manifest(
    byte_payloads: dict[str, bytes], manifest: dict[str, Any]
) -> None:
    files = manifest.get("files") or {}
    for name, data in byte_payloads.items():
        entry = files.get(name)
        if not isinstance(entry, dict) or not entry.get("sha256"):
            raise ValueError(
                f"manifest has no trusted hash for {name}; use --adopt-existing"
            )
        if _sha256(data) != entry["sha256"] or len(data) != entry.get("byte_length"):
            raise ValueError(
                f"local {name} does not match its manifest; review the change and "
                "use --adopt-existing instead of conditional update"
            )


def _read_existing() -> tuple[dict[str, bytes], dict[str, object]]:
    byte_payloads: dict[str, bytes] = {}
    decoded: dict[str, object] = {}
    for name in SOURCES:
        path = EXTERNAL_DIR / name
        data = path.read_bytes()
        byte_payloads[name] = data
        decoded[name] = _decode_json(name, data)
    return byte_payloads, decoded


def _base_entry(
    *,
    name: str,
    data: bytes,
    summary: dict[str, Any],
    retrieved_on: str | None,
) -> dict[str, Any]:
    return {
        "source_url": SOURCES[name],
        "final_url": SOURCES[name],
        "retrieved_at_utc": None,
        "retrieved_on": retrieved_on,
        "last_checked_at_utc": None,
        "http_status": None,
        "etag": None,
        "last_modified": None,
        "content_type": None,
        "byte_length": len(data),
        "sha256": _sha256(data),
        "schema_version": SCHEMA_VERSIONS[name],
        "record_count": summary["record_count"],
        "previous_sha256": None,
        "previous_record_count": None,
        "count_delta": None,
        "changed": None,
        "quality": summary["quality"],
    }


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    _fsync_directory(path.parent)


def _manifest_bytes(manifest: dict[str, Any]) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _write_manifest(manifest: dict[str, Any]) -> None:
    _atomic_write(MANIFEST_PATH, _manifest_bytes(manifest))


def _write_transaction(transaction: dict[str, Any]) -> None:
    _atomic_write(TRANSACTION_PATH, _manifest_bytes(transaction))


def _remove_transaction() -> None:
    TRANSACTION_PATH.unlink(missing_ok=True)
    _fsync_directory(EXTERNAL_DIR)


def _load_transaction() -> dict[str, Any] | None:
    if not TRANSACTION_PATH.exists():
        return None
    value = json.loads(TRANSACTION_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("transaction_version") != TRANSACTION_VERSION:
        raise ValueError(f"invalid snapshot transaction journal: {TRANSACTION_PATH}")
    if value.get("state") not in {"prepared", "committed"}:
        raise ValueError(f"invalid snapshot transaction state: {TRANSACTION_PATH}")
    for field in ("previous_observation_id", "new_observation_id", "previous_manifest_base64"):
        if not isinstance(value.get(field), str) or not value[field]:
            raise ValueError(f"snapshot transaction lacks {field}: {TRANSACTION_PATH}")
    return value


def _restore_previous_generation(transaction: dict[str, Any]) -> None:
    previous_id = str(transaction["previous_observation_id"])
    new_id = str(transaction["new_observation_id"])
    previous_payloads = load_observation_payloads(OBSERVATIONS_DIR, previous_id)
    try:
        previous_manifest = base64.b64decode(
            str(transaction["previous_manifest_base64"]), validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise ValueError("snapshot transaction contains invalid manifest bytes") from exc
    for name in SOURCES:
        _atomic_write(EXTERNAL_DIR / name, previous_payloads[name])
    _atomic_write(MANIFEST_PATH, previous_manifest)
    remove_observation(OBSERVATIONS_DIR, new_id)
    prune_unreferenced_objects(OBSERVATIONS_DIR)
    restored_bytes, _ = _read_existing()
    restored_manifest = _load_manifest()
    _verify_existing_manifest(restored_bytes, restored_manifest)
    summary = verify_observations(
        OBSERVATIONS_DIR, current_payloads=restored_bytes
    )
    if summary["latest_observation_id"] != previous_id:
        raise ValueError("transaction recovery did not restore the previous observation")
    _remove_transaction()


def _recover_snapshot_transaction() -> None:
    transaction = _load_transaction()
    if transaction is None:
        prune_unreferenced_objects(OBSERVATIONS_DIR)
        return
    if transaction["state"] == "committed":
        try:
            current_bytes, _ = _read_existing()
            current_manifest = _load_manifest()
            _verify_existing_manifest(current_bytes, current_manifest)
            summary = verify_observations(
                OBSERVATIONS_DIR, current_payloads=current_bytes
            )
            expected_hashes = transaction.get("new_root_sha256")
            if not isinstance(expected_hashes, dict) or any(
                _sha256(current_bytes[name]) != expected_hashes.get(name)
                for name in SOURCES
            ):
                raise ValueError("committed snapshot hashes differ from journal")
            if summary["latest_observation_id"] != transaction["new_observation_id"]:
                raise ValueError("committed observation tip differs from journal")
        except (OSError, ValueError, json.JSONDecodeError):
            _restore_previous_generation(transaction)
        else:
            _remove_transaction()
        return
    _restore_previous_generation(transaction)


def adopt_existing(retrieved_on: str) -> None:
    dt.date.fromisoformat(retrieved_on)
    if latest_observation_id(OBSERVATIONS_DIR) is not None:
        raise FileExistsError(
            "immutable observation history already exists; refusing to re-adopt root files"
        )
    byte_payloads, decoded = _read_existing()
    summaries = _validate_all(decoded)
    now = _utc_now()
    manifest = {
        "manifest_version": 1,
        "source_project": "https://github.com/kiang/ap.ece.moe.edu.tw",
        "generated_at_utc": _iso(now),
        "provenance_note": (
            "Existing repository snapshots adopted without an HTTP request; "
            "the exact retrieval time and response headers were not retained."
        ),
        "files": {
            name: _base_entry(
                name=name,
                data=byte_payloads[name],
                summary=summaries[name],
                retrieved_on=retrieved_on,
            )
            for name in SOURCES
        },
    }
    observation_id = retrieved_on.replace("-", "") + "T000000Z-adopted-v1"
    bundle = build_observation(
        observation_id=observation_id,
        current_payloads=byte_payloads,
        source_metadata=manifest["files"],
        previous_observation_id=None,
        previous_payloads=None,
        observed_at="",
        observation_kind="adopted_baseline_without_exact_observation_time",
    )
    publish_observation(OBSERVATIONS_DIR, bundle)
    try:
        _write_manifest(manifest)
    except BaseException:
        remove_observation(OBSERVATIONS_DIR, observation_id)
        prune_unreferenced_objects(OBSERVATIONS_DIR)
        raise
    print(f"wrote {MANIFEST_PATH} from existing validated snapshots")
    print(f"wrote immutable baseline observation {observation_id}")


def _validate_final_url(name: str, url: str) -> None:
    final = urllib.parse.urlparse(url)
    expected = urllib.parse.urlparse(SOURCES[name])
    if (
        final.scheme != "https"
        or final.hostname != expected.hostname
        or final.port not in (None, 443)
    ):
        raise ValueError(f"{name}: refused redirected origin {url!r}")


def _download_one(
    name: str,
    prior: dict[str, Any],
    existing_data: bytes,
    timeout: int,
) -> tuple[bytes, dict[str, Any]]:
    headers = {"User-Agent": "smart-watchdog-snapshot-updater/1"}
    if prior.get("etag"):
        headers["If-None-Match"] = str(prior["etag"])
    if prior.get("last_modified"):
        headers["If-Modified-Since"] = str(prior["last_modified"])
    request = urllib.request.Request(SOURCES[name], headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            final_url = str(prior.get("final_url") or SOURCES[name])
            _validate_final_url(name, final_url)
            return existing_data, {
                "http_status": 304,
                "final_url": final_url,
                "etag": prior.get("etag"),
                "last_modified": prior.get("last_modified"),
                "content_type": prior.get("content_type"),
            }
        raise
    with response:
        final_url = response.geturl()
        _validate_final_url(name, final_url)
        return response.read(), {
            "http_status": response.status,
            "final_url": final_url,
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "content_type": response.headers.get("Content-Type"),
        }


def update_snapshots(
    *, timeout: int, allow_count_decrease: bool, dry_run: bool
) -> None:
    existing_bytes, existing_decoded = _read_existing()
    existing_summaries = _validate_all(existing_decoded)
    prior_manifest = _load_manifest()
    _verify_existing_manifest(existing_bytes, prior_manifest)
    previous_observation_id = latest_observation_id(OBSERVATIONS_DIR)
    if previous_observation_id is None:
        raise ValueError(
            "root snapshots have no immutable baseline observation; run "
            "record_external_observation.py --adopt-current first"
        )
    verify_observations(OBSERVATIONS_DIR, current_payloads=existing_bytes)
    prior_files = prior_manifest.get("files", {})

    downloaded: dict[str, bytes] = {}
    http_metadata: dict[str, dict[str, Any]] = {}
    decoded: dict[str, object] = {}
    for name in SOURCES:
        data, metadata = _download_one(
            name,
            prior_files.get(name, {}),
            existing_bytes[name],
            timeout,
        )
        downloaded[name] = data
        http_metadata[name] = metadata
        decoded[name] = _decode_json(name, data)

    summaries = _validate_all(decoded)
    decreases = [
        f"{name}: {existing_summaries[name]['record_count']} -> "
        f"{summaries[name]['record_count']}"
        for name in SOURCES
        if summaries[name]["record_count"]
        < existing_summaries[name]["record_count"]
    ]
    old_preschool_ids = _validate_preschools(existing_decoded["preschools.json"])[1]
    new_preschool_ids = _validate_preschools(decoded["preschools.json"])[1]
    old_vehicle_ids = _validate_vehicles(existing_decoded["kids_vehicles.json"])[1]
    new_vehicle_ids = _validate_vehicles(decoded["kids_vehicles.json"])[1]
    old_penalty_ids = _validate_penalties(existing_decoded["punish_all.json"])[1]
    new_penalty_ids = _validate_penalties(decoded["punish_all.json"])[1]
    removed_ids = {
        "preschools.json": sorted(old_preschool_ids - new_preschool_ids),
        "punish_all.json": sorted(old_penalty_ids - new_penalty_ids),
        "kids_vehicles.json": sorted(old_vehicle_ids - new_vehicle_ids),
    }
    removals = [
        f"{name}: {len(ids)} registry IDs removed"
        for name, ids in removed_ids.items()
        if ids
    ]
    if (decreases or removals) and not allow_count_decrease:
        details = "; ".join(decreases + removals)
        raise ValueError(
            f"snapshot shrinkage requires review ({details}); rerun with "
            "--allow-count-decrease only after inspecting removals"
        )

    now = _utc_now()
    entries: dict[str, dict[str, Any]] = {}
    for name in SOURCES:
        prior = prior_files.get(name, {})
        previous_hash = _sha256(existing_bytes[name])
        current_hash = _sha256(downloaded[name])
        changed = current_hash != previous_hash
        previous_count = existing_summaries[name]["record_count"]
        current_count = summaries[name]["record_count"]
        metadata = http_metadata[name]
        entries[name] = {
            "source_url": SOURCES[name],
            "final_url": metadata["final_url"],
            "retrieved_at_utc": _iso(now) if changed else prior.get("retrieved_at_utc"),
            "retrieved_on": now.date().isoformat() if changed else prior.get("retrieved_on"),
            "last_checked_at_utc": _iso(now),
            "http_status": metadata["http_status"],
            "etag": metadata.get("etag"),
            "last_modified": metadata.get("last_modified"),
            "content_type": metadata.get("content_type"),
            "byte_length": len(downloaded[name]),
            "sha256": current_hash,
            "schema_version": SCHEMA_VERSIONS[name],
            "record_count": current_count,
            "previous_sha256": previous_hash,
            "previous_record_count": previous_count,
            "count_delta": current_count - previous_count,
            "changed": changed,
            "quality": summaries[name]["quality"],
        }

    manifest = {
        "manifest_version": 1,
        "source_project": "https://github.com/kiang/ap.ece.moe.edu.tw",
        "generated_at_utc": _iso(now),
        "files": entries,
    }
    observation_id = now.strftime("%Y%m%dT%H%M%SZ")
    observation = build_observation(
        observation_id=observation_id,
        current_payloads=downloaded,
        source_metadata=entries,
        previous_observation_id=previous_observation_id,
        previous_payloads=existing_bytes,
        observed_at=_iso(now),
        observation_kind="validated_upstream_check",
    )
    observation_manifest = json.loads(observation.manifest_data)
    change_summary = observation_manifest["changes"]

    if dry_run:
        for name, entry in entries.items():
            print(
                f"{name}: status={entry['http_status']} changed={entry['changed']} "
                f"records={entry['record_count']} delta={entry['count_delta']}"
            )
        print(
            f"record diff: {change_summary['record_count']} changes "
            f"{change_summary['change_type_counts']}"
        )
        print("dry run: validated candidate snapshots and diff; no files replaced")
        return

    previous_manifest_data = MANIFEST_PATH.read_bytes()
    transaction = {
        "transaction_version": TRANSACTION_VERSION,
        "state": "prepared",
        "previous_observation_id": previous_observation_id,
        "new_observation_id": observation_id,
        "previous_manifest_base64": base64.b64encode(
            previous_manifest_data
        ).decode("ascii"),
        "new_root_sha256": {
            name: _sha256(downloaded[name]) for name in SOURCES
        },
    }
    _write_transaction(transaction)
    try:
        publish_observation(OBSERVATIONS_DIR, observation)
        for name in SOURCES:
            if entries[name]["changed"]:
                _atomic_write(EXTERNAL_DIR / name, downloaded[name])
        _atomic_write(MANIFEST_PATH, _manifest_bytes(manifest))
        _verify_existing_manifest(downloaded, manifest)
        summary = verify_observations(
            OBSERVATIONS_DIR, current_payloads=downloaded
        )
        if summary["latest_observation_id"] != observation_id:
            raise ValueError("published observation is not the verified chain tip")
        transaction["state"] = "committed"
        _write_transaction(transaction)
        _remove_transaction()
    except BaseException:
        _recover_snapshot_transaction()
        raise

    for name, entry in entries.items():
        print(
            f"{name}: status={entry['http_status']} changed={entry['changed']} "
            f"records={entry['record_count']} delta={entry['count_delta']}"
        )
    print(
        f"wrote immutable observation {observation_id}: "
        f"{change_summary['record_count']} changes "
        f"{change_summary['change_type_counts']}"
    )
    print(f"wrote {MANIFEST_PATH}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--adopt-existing",
        action="store_true",
        help="validate current files and create a baseline manifest without HTTP",
    )
    parser.add_argument(
        "--retrieved-on",
        help="known YYYY-MM-DD retrieval date (required with --adopt-existing)",
    )
    parser.add_argument(
        "--allow-count-decrease",
        action="store_true",
        help="accept reviewed record-count or registry-ID removals",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="download and validate candidates without replacing snapshots",
    )
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()
    if args.adopt_existing and not args.retrieved_on:
        parser.error("--retrieved-on is required with --adopt-existing")
    if not args.adopt_existing and args.retrieved_on:
        parser.error("--retrieved-on is only valid with --adopt-existing")
    if args.adopt_existing and args.dry_run:
        parser.error("--dry-run applies only to network updates")
    return args


def main() -> None:
    """Update snapshots, or adopt existing files without network access."""
    args = parse_args()
    with observation_lock(OBSERVATIONS_DIR):
        _recover_snapshot_transaction()
        if args.adopt_existing:
            adopt_existing(args.retrieved_on)
        else:
            update_snapshots(
                timeout=args.timeout,
                allow_count_decrease=args.allow_count_decrease,
                dry_run=args.dry_run,
            )


if __name__ == "__main__":
    main()
