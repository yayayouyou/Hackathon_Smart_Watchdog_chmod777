"""Adopt or verify immutable observations of the root external snapshots.

The initial repository snapshot predates precise HTTP timestamps, so its baseline
observation keeps ``observed_at_utc`` null. Future observations are created by
``download_external_snapshots.py`` after a validated upstream check.

Run::

    PYTHONPATH=src .venv/bin/python scripts/record_external_observation.py \
      --adopt-current 20260810T000000Z-adopted-v1
    PYTHONPATH=src .venv/bin/python scripts/record_external_observation.py --verify-only
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape.observations import (
    SOURCE_FILES,
    build_observation,
    latest_observation_id,
    observation_lock,
    prune_unreferenced_objects,
    publish_observation,
    remove_observation,
    sha256,
    verify_observations,
)

EXTERNAL = pathlib.Path("data/external")
OBSERVATIONS = EXTERNAL / "observations"
ROOT_MANIFEST = EXTERNAL / "manifest.json"


def _root_context() -> tuple[dict[str, bytes], dict[str, dict[str, Any]]]:
    manifest = json.loads(ROOT_MANIFEST.read_text(encoding="utf-8"))
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        raise ValueError("root external manifest files must be an object")
    payloads: dict[str, bytes] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for filename in SOURCE_FILES:
        value = files.get(filename)
        if not isinstance(value, dict):
            raise ValueError(f"root manifest lacks metadata for {filename}")
        data = (EXTERNAL / filename).read_bytes()
        if len(data) != value.get("byte_length") or sha256(data) != value.get("sha256"):
            raise ValueError(f"root snapshot differs from manifest: {filename}")
        payloads[filename] = data
        metadata[filename] = value
    return payloads, metadata


def adopt_current(observation_id: str) -> None:
    if latest_observation_id(OBSERVATIONS) is not None:
        raise FileExistsError("immutable observation history already exists")
    payloads, metadata = _root_context()
    bundle = build_observation(
        observation_id=observation_id,
        current_payloads=payloads,
        source_metadata=metadata,
        previous_observation_id=None,
        previous_payloads=None,
        observed_at="",
        observation_kind="adopted_baseline_without_exact_observation_time",
    )
    target = publish_observation(OBSERVATIONS, bundle)
    try:
        summary = verify_observations(OBSERVATIONS, current_payloads=payloads)
    except BaseException:
        remove_observation(OBSERVATIONS, observation_id)
        prune_unreferenced_objects(OBSERVATIONS)
        raise
    print(f"wrote immutable baseline observation: {target}")
    print(
        f"verified {summary['observation_count']} observation(s); "
        f"latest={summary['latest_observation_id']}; changes={summary['total_changes']}"
    )


def verify_only() -> None:
    payloads, _ = _root_context()
    summary = verify_observations(OBSERVATIONS, current_payloads=payloads)
    print(
        f"verified {summary['observation_count']} immutable observation(s); "
        f"latest={summary['latest_observation_id']}; changes={summary['total_changes']}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--adopt-current",
        metavar="OBSERVATION_ID",
        help="create the first immutable observation from current validated root files",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="verify the full immutable chain and bind its latest node to root files",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.adopt_current:
        with observation_lock(OBSERVATIONS):
            adopt_current(args.adopt_current)
    else:
        verify_only()


if __name__ == "__main__":
    main()
