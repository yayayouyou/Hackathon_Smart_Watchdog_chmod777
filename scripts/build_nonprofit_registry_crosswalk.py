"""Build a report-year to registry identity crosswalk for nonprofit preschools.

The extracted financial-report JSON files are inputs only and are never modified.
The output keeps every registry UUID for the same physical preschool while also
identifying the UUID whose operator matches each report period.  Plural CSV fields
are JSON arrays so UUID/title/operator alignment is deterministic and lossless.

Run: PYTHONPATH=src .venv/bin/python scripts/build_nonprofit_registry_crosswalk.py
"""

from __future__ import annotations

import csv
import json
import pathlib
import re
import sys
from collections.abc import Iterable

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.compliance import contract_covers_year
from smart_watchdog.scrape import registry

EXTRACT_DIR = pathlib.Path("data/extracted/nonprofit")
INSTITUTIONS = pathlib.Path("data/processed/institutions_ntpc.csv")
OUT = pathlib.Path("data/processed/nonprofit_registry_crosswalk.csv")
FILENAME_RE = re.compile(r"^(N\d\d)_(.+?)_(\d{3})$")

FIELDS = [
    "report_file",
    "report_key",
    "code",
    "short_name",
    "academic_year",
    "entity",
    "report_operator",
    "contract_period",
    "registry_ids",
    "registry_titles",
    "registry_operators",
    "registry_reg_dates",
    "operator_matched_ids",
    "operator_match",
    "contract_covers_year",
    "candidate_count",
    "entity_count",
    "report_capacity",
    "registry_capacities",
    "capacity_exact_match",
    "capacity_gap_min",
    "join_status",
    "join_evidence",
]


def _json_array(values: Iterable[object]) -> str:
    return json.dumps(list(values), ensure_ascii=False, separators=(",", ":"))


def _optional_int(value: object) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return None


def _load_registry_rows(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [row for row in csv.DictReader(fh) if row.get("type") == "非營利"]
    if not rows:
        raise ValueError(f"no nonprofit registry rows in {path}")
    return rows


def _load_report(path: pathlib.Path) -> tuple[dict[str, object], re.Match[str]]:
    match = FILENAME_RE.fullmatch(path.stem)
    if not match:
        raise ValueError(f"unexpected report filename: {path.name}")

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"report root must be an object: {path.name}")

    code, short_name, academic_year = match.groups()
    claimed = (
        str(payload.get("code") or ""),
        str(payload.get("short_name") or ""),
        str(payload.get("academic_year") or ""),
    )
    if claimed != (code, short_name, academic_year):
        raise ValueError(
            f"filename/payload identity mismatch in {path.name}: {claimed!r}"
        )
    return payload, match


def _join_status(
    *, entity_count: int, operator_match: bool, period_match: bool
) -> str:
    if entity_count == 0:
        return "no_entity"
    if entity_count > 1:
        return "ambiguous_entity"
    if not operator_match:
        return "operator_unmatched"
    if not period_match:
        return "contract_uncovered"
    return "matched"


def build_crosswalk(
    extract_dir: pathlib.Path = EXTRACT_DIR,
    institutions_path: pathlib.Path = INSTITUTIONS,
) -> list[dict[str, object]]:
    """Return one deterministic crosswalk row per extracted report."""
    registry_rows = _load_registry_rows(institutions_path)
    rows: list[dict[str, object]] = []

    report_paths = sorted(extract_dir.glob("*.json"))
    if not report_paths:
        raise ValueError(f"no report JSON files in {extract_dir}")

    for path in report_paths:
        payload, filename_match = _load_report(path)
        code, short_name, academic_year = filename_match.groups()
        note = payload.get("note_1") or {}
        if not isinstance(note, dict):
            raise ValueError(f"note_1 must be an object: {path.name}")

        candidates = sorted(
            (row for row in registry_rows if short_name in row.get("title", "")),
            key=lambda row: row.get("id", ""),
        )
        entities = sorted(
            {
                row.get("entity") or registry.entity_key(row.get("title", ""))
                for row in candidates
            }
        )
        entities = [entity for entity in entities if entity]

        report_operator = str(note.get("operator") or "").strip()
        matched_candidates = [
            row
            for row in candidates
            if registry.operators_match(report_operator, row.get("operator"))
        ]
        operator_match = bool(matched_candidates)

        contract_period = str(note.get("contract_period") or "").strip()
        period_match = contract_covers_year(contract_period, int(academic_year))

        report_capacity = _optional_int(note.get("approved_capacity"))
        registry_capacities = sorted(
            {
                capacity
                for row in candidates
                if (capacity := _optional_int(row.get("count_approved"))) is not None
            }
        )
        capacity_exact: bool | None = None
        capacity_gap: int | None = None
        if report_capacity is not None and registry_capacities:
            capacity_exact = report_capacity in registry_capacities
            capacity_gap = min(
                abs(report_capacity - capacity) for capacity in registry_capacities
            )

        status = _join_status(
            entity_count=len(entities),
            operator_match=operator_match,
            period_match=period_match,
        )
        capacity_evidence = (
            "unknown"
            if capacity_exact is None
            else ("exact" if capacity_exact else f"gap:{capacity_gap}")
        )
        evidence = ";".join(
            [
                f"short_name:{len(candidates)}_candidate(s)",
                f"entity:{len(entities)}",
                f"operator:{len(matched_candidates)}_matched_uuid(s)",
                f"contract:{'covered' if period_match else 'uncovered'}",
                f"capacity:{capacity_evidence}",
            ]
        )

        rows.append(
            {
                "report_file": path.name,
                "report_key": f"{code}_{academic_year}",
                "code": code,
                "short_name": short_name,
                "academic_year": academic_year,
                "entity": entities[0] if len(entities) == 1 else "",
                "report_operator": report_operator,
                "contract_period": contract_period,
                "registry_ids": _json_array(row.get("id", "") for row in candidates),
                "registry_titles": _json_array(
                    row.get("title", "") for row in candidates
                ),
                "registry_operators": _json_array(
                    row.get("operator", "") for row in candidates
                ),
                "registry_reg_dates": _json_array(
                    row.get("reg_date", "") for row in candidates
                ),
                "operator_matched_ids": _json_array(
                    row.get("id", "") for row in matched_candidates
                ),
                "operator_match": operator_match,
                "contract_covers_year": period_match,
                "candidate_count": len(candidates),
                "entity_count": len(entities),
                "report_capacity": report_capacity,
                "registry_capacities": _json_array(registry_capacities),
                "capacity_exact_match": capacity_exact,
                "capacity_gap_min": capacity_gap,
                "join_status": status,
                "join_evidence": evidence,
            }
        )

    return rows


def validate_crosswalk(rows: list[dict[str, object]]) -> None:
    """Fail closed if any report lacks a unique and period-consistent identity."""
    report_keys = [str(row["report_key"]) for row in rows]
    duplicate_keys = sorted(
        {key for key in report_keys if report_keys.count(key) > 1}
    )
    if duplicate_keys:
        raise ValueError(f"duplicate report keys: {duplicate_keys}")

    failures = [row for row in rows if row["join_status"] != "matched"]
    if failures:
        details = ", ".join(
            f"{row['report_file']}={row['join_status']}" for row in failures
        )
        raise ValueError(f"crosswalk validation failed: {details}")

    sibling_ids_by_entity: dict[str, str] = {}
    for row in rows:
        entity = str(row["entity"])
        sibling_ids = str(row["registry_ids"])
        previous = sibling_ids_by_entity.setdefault(entity, sibling_ids)
        if previous != sibling_ids:
            raise ValueError(f"inconsistent sibling UUID set for {entity}")


def write_crosswalk(rows: list[dict[str, object]], path: pathlib.Path = OUT) -> None:
    """Atomically persist a validated crosswalk."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def main() -> None:
    """Build, validate, and persist the crosswalk."""
    rows = build_crosswalk()
    validate_crosswalk(rows)
    write_crosswalk(rows)

    entities = {str(row["entity"]) for row in rows}
    multi_uuid_entities = {
        str(row["entity"])
        for row in rows
        if int(row["candidate_count"]) > 1
    }
    print(f"wrote {OUT} ({len(rows)} report-years)")
    print(f"physical entities: {len(entities)}")
    print(f"entities retaining sibling UUIDs: {len(multi_uuid_entities)}")
    print(
        "operator + contract matched: "
        f"{sum(row['join_status'] == 'matched' for row in rows)}/{len(rows)}"
    )


if __name__ == "__main__":
    main()
