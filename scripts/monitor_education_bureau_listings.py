"""Monitor the bounded NTPC Important Announcements listing.

Network modes build a fully validated immutable candidate before publication.
Verification and rebuild modes operate only from pinned content-addressed HTML.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape.announcement_observations import (
    DEFAULT_BOOTSTRAP_PAGES,
    DEFAULT_DETAIL_CAP,
    DEFAULT_PAGE_CAP,
    ListingObservationCandidate,
    acquire_bootstrap_candidate,
    acquire_update_candidate,
    aggregate_discovery_csv,
    latest_listing_observation_id,
    list_listing_observation_ids,
    load_listing_manifest,
    prune_unreferenced_listing_objects,
    publish_listing_observation,
    remove_listing_observation,
    validate_listing_observation_id,
    verify_listing_observations,
)
from smart_watchdog.scrape.announcements import AnnouncementClient
from smart_watchdog.scrape.observations import observation_lock

EXTERNAL_ROOT = pathlib.Path("data/external/education_bureau_announcements")
OBSERVATION_ROOT = EXTERNAL_ROOT / "listing_observations"
AGGREGATE_PATH = pathlib.Path(
    "data/processed/education_bureau_discovered_notices_ntpc.csv"
)
TRANSACTION_PATH = EXTERNAL_ROOT / ".listing-update-transaction.json"
TRANSACTION_VERSION = 1


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _fsync_directory(path: pathlib.Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    _fsync_directory(path.parent)


def _write_transaction(transaction: dict[str, object]) -> None:
    _atomic_write(TRANSACTION_PATH, _json_bytes(transaction))


def _remove_transaction() -> None:
    TRANSACTION_PATH.unlink(missing_ok=True)
    _fsync_directory(EXTERNAL_ROOT)


def _load_transaction() -> dict[str, Any] | None:
    if not TRANSACTION_PATH.exists():
        return None
    value = json.loads(TRANSACTION_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("transaction_version") != TRANSACTION_VERSION:
        raise ValueError(f"invalid announcement listing transaction: {TRANSACTION_PATH}")
    if value.get("state") != "prepared":
        raise ValueError("unsupported announcement listing transaction state")
    new_id = value.get("new_observation_id")
    previous_id = value.get("previous_observation_id")
    if not isinstance(new_id, str) or not new_id:
        raise ValueError("announcement listing transaction lacks new observation ID")
    validate_listing_observation_id(new_id)
    if previous_id is not None:
        if not isinstance(previous_id, str) or not previous_id:
            raise ValueError("announcement listing transaction predecessor is invalid")
        validate_listing_observation_id(previous_id)
    if new_id == previous_id:
        raise ValueError("announcement listing transaction IDs must differ")
    return value


def _write_verified_aggregate() -> bytes:
    data = aggregate_discovery_csv(OBSERVATION_ROOT)
    _atomic_write(AGGREGATE_PATH, data)
    if list_listing_observation_ids(OBSERVATION_ROOT):
        verify_listing_observations(OBSERVATION_ROOT, aggregate_data=data)
    return data


def _recover_transaction() -> None:
    """Complete a valid publication or remove only the journaled invalid node."""
    transaction = _load_transaction()
    if transaction is None:
        prune_unreferenced_listing_objects(OBSERVATION_ROOT)
        return
    new_id = str(transaction["new_observation_id"])
    previous_id = transaction.get("previous_observation_id")
    target = OBSERVATION_ROOT / new_id
    if target.exists():
        try:
            summary = verify_listing_observations(OBSERVATION_ROOT)
            if summary["latest_observation_id"] != new_id:
                raise ValueError("journaled announcement observation is not chain tip")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            try:
                journaled_manifest = load_listing_manifest(OBSERVATION_ROOT, new_id)
            except (OSError, ValueError, json.JSONDecodeError) as manifest_exc:
                raise ValueError(
                    "cannot safely identify journaled announcement observation"
                ) from manifest_exc
            if journaled_manifest.get("previous_observation_id") != previous_id:
                raise ValueError(
                    "journaled announcement predecessor differs; refusing removal"
                ) from exc
            remove_listing_observation(OBSERVATION_ROOT, new_id)
            prune_unreferenced_listing_objects(OBSERVATION_ROOT)
            tip = latest_listing_observation_id(OBSERVATION_ROOT)
            if tip != previous_id:
                raise ValueError(
                    "announcement transaction rollback tip differs"
                ) from exc
        else:
            aggregate = _write_verified_aggregate()
            expected = transaction.get("aggregate_sha256")
            if expected is not None and expected != _sha256(aggregate):
                raise ValueError("recovered announcement aggregate hash differs")
            _remove_transaction()
            return
    else:
        prune_unreferenced_listing_objects(OBSERVATION_ROOT)
        tip = latest_listing_observation_id(OBSERVATION_ROOT)
        if tip != previous_id:
            raise ValueError("incomplete announcement transaction predecessor differs")
    _write_verified_aggregate()
    _remove_transaction()


def _new_client(args: argparse.Namespace) -> AnnouncementClient:
    return AnnouncementClient(
        timeout=args.timeout,
        max_requests=args.page_cap + args.detail_cap + 1,
    )


def _candidate_for_mode(
    args: argparse.Namespace,
    *,
    observation_id: str,
) -> tuple[ListingObservationCandidate, AnnouncementClient]:
    client = _new_client(args)
    latest = latest_listing_observation_id(OBSERVATION_ROOT)
    if latest is None:
        candidate = acquire_bootstrap_candidate(
            observation_id=observation_id,
            client=client,
            bootstrap_pages=args.bootstrap_pages,
            page_cap=args.page_cap,
            detail_cap=args.detail_cap,
        )
    else:
        verify_listing_observations(OBSERVATION_ROOT)
        previous = load_listing_manifest(OBSERVATION_ROOT, latest)
        candidate = acquire_update_candidate(
            observation_id=observation_id,
            previous_manifest=previous,
            client=client,
            page_cap=args.page_cap,
            detail_cap=args.detail_cap,
        )
    return candidate, client


def _print_candidate(
    candidate: ListingObservationCandidate,
    *,
    request_count: int,
    preview: bool,
) -> None:
    summary = candidate.summary
    print(
        f"pages={summary['pages']} covered={summary['covered']} new={summary['new']} "
        f"metadata_changed={summary['metadata_changed']} requests={request_count}"
    )
    if preview:
        print("preview: validated candidate; no files written")


def preview(args: argparse.Namespace) -> None:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    mode = "bootstrap" if latest_listing_observation_id(OBSERVATION_ROOT) is None else "update"
    candidate, client = _candidate_for_mode(
        args, observation_id=f"preview-{mode}-{now}"
    )
    _print_candidate(candidate, request_count=client.request_count, preview=True)


def _publish_candidate(
    candidate: ListingObservationCandidate,
    *,
    previous_id: str | None,
) -> None:
    existing_aggregate = aggregate_discovery_csv(OBSERVATION_ROOT)
    candidate_rows = candidate.discoveries_data.splitlines(keepends=True)[1:]
    expected_aggregate = existing_aggregate + b"".join(candidate_rows)
    transaction = {
        "transaction_version": TRANSACTION_VERSION,
        "state": "prepared",
        "previous_observation_id": previous_id,
        "new_observation_id": candidate.observation_id,
        "aggregate_sha256": _sha256(expected_aggregate),
    }
    _write_transaction(transaction)
    try:
        publish_listing_observation(OBSERVATION_ROOT, candidate)
        summary = verify_listing_observations(OBSERVATION_ROOT)
        if summary["latest_observation_id"] != candidate.observation_id:
            raise ValueError("published announcement observation is not chain tip")
        aggregate = _write_verified_aggregate()
        if aggregate != expected_aggregate:
            raise ValueError("published announcement aggregate differs from candidate")
        _remove_transaction()
    except BaseException:
        _recover_transaction()
        raise


def bootstrap(args: argparse.Namespace) -> None:
    if latest_listing_observation_id(OBSERVATION_ROOT) is not None:
        raise FileExistsError("announcement listing history already exists")
    client = _new_client(args)
    candidate = acquire_bootstrap_candidate(
        observation_id=args.bootstrap,
        client=client,
        bootstrap_pages=args.bootstrap_pages,
        page_cap=args.page_cap,
        detail_cap=args.detail_cap,
    )
    _publish_candidate(candidate, previous_id=None)
    _print_candidate(candidate, request_count=client.request_count, preview=False)
    print(f"wrote immutable announcement listing observation {candidate.observation_id}")
    print(f"wrote {AGGREGATE_PATH}")


def update(args: argparse.Namespace) -> None:
    latest = latest_listing_observation_id(OBSERVATION_ROOT)
    if latest is None:
        raise ValueError("announcement listing bootstrap is required before update")
    verify_listing_observations(OBSERVATION_ROOT)
    previous = load_listing_manifest(OBSERVATION_ROOT, latest)
    client = _new_client(args)
    candidate = acquire_update_candidate(
        observation_id=args.update,
        previous_manifest=previous,
        client=client,
        page_cap=args.page_cap,
        detail_cap=args.detail_cap,
    )
    _publish_candidate(candidate, previous_id=latest)
    _print_candidate(candidate, request_count=client.request_count, preview=False)
    print(f"wrote immutable announcement listing observation {candidate.observation_id}")
    print(f"wrote {AGGREGATE_PATH}")


def verify_only() -> None:
    aggregate = AGGREGATE_PATH.read_bytes()
    summary = verify_listing_observations(OBSERVATION_ROOT, aggregate_data=aggregate)
    print(
        f"verified {summary['observation_count']} announcement listing observations, "
        f"tip={summary['latest_observation_id']} discoveries={summary['discovery_count']}"
    )
    print(f"verified live {AGGREGATE_PATH}; no files written")


def rebuild() -> None:
    summary = verify_listing_observations(OBSERVATION_ROOT)
    aggregate = _write_verified_aggregate()
    print(
        f"verified {summary['observation_count']} announcement listing observations, "
        f"tip={summary['latest_observation_id']}"
    )
    print(f"rebuilt {AGGREGATE_PATH} ({_sha256(aggregate)}) without network")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preview", action="store_true")
    mode.add_argument("--bootstrap", metavar="OBSERVATION_ID")
    mode.add_argument("--update", metavar="OBSERVATION_ID")
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--rebuild", action="store_true")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--page-cap", type=int, default=DEFAULT_PAGE_CAP)
    parser.add_argument("--detail-cap", type=int, default=DEFAULT_DETAIL_CAP)
    parser.add_argument(
        "--bootstrap-pages", type=int, default=DEFAULT_BOOTSTRAP_PAGES
    )
    args = parser.parse_args()
    if min(args.timeout, args.page_cap, args.detail_cap, args.bootstrap_pages) <= 0:
        parser.error("timeout and all acquisition caps must be positive")
    if args.bootstrap_pages > args.page_cap:
        parser.error("--bootstrap-pages cannot exceed --page-cap")
    return args


def main() -> None:
    args = parse_args()
    if args.preview:
        preview(args)
    elif args.verify_only:
        verify_only()
    else:
        with observation_lock(OBSERVATION_ROOT):
            _recover_transaction()
            if args.bootstrap:
                bootstrap(args)
            elif args.update:
                update(args)
            else:
                rebuild()


if __name__ == "__main__":
    main()
