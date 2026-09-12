"""Acquire and rebuild a bounded New Taipei procurement award pilot.

Discovery uses the public g0v/openfun civic API mirror.  Every canonical award
retains its official ``web.pcc.gov.tw`` notice URL, and raw JSON is pinned before
any processed report-year timeline is written.

Run::

    PYTHONPATH=src .venv/bin/python scripts/download_procurement_pilot.py \
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
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.compliance import contract_covers_year
from smart_watchdog.scrape.procurement import (
    API_ORIGIN,
    ProcurementClient,
    ResponseArtifact,
    discover_contract_tenders,
    operator_match,
    parse_search_response,
    parse_tender_awards,
)

SNAPSHOT_ROOT = pathlib.Path("data/external/procurement")
PROCESSED_PATH = pathlib.Path("data/processed/nonprofit_procurement_contracts.csv")
CROSSWALK_PATH = pathlib.Path("data/processed/nonprofit_registry_crosswalk.csv")
PARSER_SCHEMA_VERSION = "pcc-operating-contract-awards-v1"
OFFICIAL_SOURCE_URL = "https://web.pcc.gov.tw/"
CIVIC_PROJECT_URL = "https://pcc.g0v.ronny.tw/"
MAX_SEARCH_RECORDS = 50
MAX_SEARCH_PAGES = 1
MAX_TENDER_NOTICES = 10
MAX_REQUESTS = 8
SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

PILOT_QUERIES = (
    {
        "id": "single_uuid_anxi",
        "entity": "新北市安溪非營利幼兒園",
        "short_name": "安溪",
        "query": "安溪 幼兒園 非營利",
        "case": "single UUID and corrected award notice",
        "expected_contract_tenders": 1,
    },
    {
        "id": "multi_uuid_beida",
        "entity": "新北市北大非營利幼兒園",
        "short_name": "北大",
        "query": "北大 非營利幼兒園",
        "case": "multiple bidders and sibling registry UUIDs",
        "expected_contract_tenders": 1,
    },
    {
        "id": "multi_uuid_sanduo",
        "entity": "新北市三多非營利幼兒園",
        "short_name": "三多",
        "query": "三多 非營利幼兒園",
        "case": "repeated school-foundation legal-name stress case",
        "expected_contract_tenders": 1,
    },
    {
        "id": "no_contract_bicheng",
        "entity": "新北市碧城非營利幼兒園",
        "short_name": "碧城",
        "query": "碧城 非營利幼兒園",
        "case": "search hits equipment only; no operating contract",
        "expected_contract_tenders": 0,
    },
    {
        "id": "zero_source_xindianjiren",
        "entity": "新北市新店及人非營利幼兒園",
        "short_name": "新店及人",
        "query": "新店及人 非營利幼兒園",
        "case": "known 申請辦理 registry title with zero source hits",
        "expected_contract_tenders": 0,
    },
)

CSV_FIELDS = [
    "query_id",
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
    "award_decision_date_raw",
    "supplier_id",
    "supplier_id_raw",
    "supplier_name",
    "source_supplier_index",
    "award_amount",
    "award_amount_raw",
    "total_award_amount",
    "total_award_amount_raw",
    "contract_period_raw",
    "contract_start_date",
    "contract_end_date",
    "official_notice_url",
    "notice_revisions",
    "search_raw_file",
    "search_response_sha256",
    "tender_raw_file",
    "tender_response_sha256",
    "report_file",
    "report_key",
    "code",
    "short_name",
    "academic_year",
    "report_operator",
    "report_contract_period",
    "registry_ids",
    "registry_titles",
    "registry_operators",
    "operator_matched_ids",
    "supplier_operator_match",
    "supplier_operator_match_method",
    "procurement_contract_covers_year",
    "temporal_join_status",
    "join_evidence",
]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_array(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _crosswalk_rows_from_bytes(data: bytes) -> list[dict[str, str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("crosswalk must be UTF-8") from exc
    rows = list(csv.DictReader(io.StringIO(text, newline="")))
    if not rows:
        raise ValueError("no crosswalk rows")
    pilot_entities = {str(query["entity"]) for query in PILOT_QUERIES}
    present = {row.get("entity", "") for row in rows}
    missing = sorted(pilot_entities - present)
    if missing:
        raise ValueError(f"pilot entities absent from crosswalk: {missing}")
    return rows


def _live_crosswalk() -> tuple[bytes, list[dict[str, str]]]:
    data = CROSSWALK_PATH.read_bytes()
    return data, _crosswalk_rows_from_bytes(data)


def _coverage_period(award: dict[str, object]) -> str:
    dates: list[dt.date] = []
    for field in ("contract_start_date", "contract_end_date"):
        value = award.get(field)
        if not value:
            return ""
        try:
            dates.append(dt.date.fromisoformat(str(value)))
        except ValueError:
            return ""
    return (
        f"{dates[0].year - 1911}年{dates[0].month}月{dates[0].day}日"
        f"至{dates[1].year - 1911}年{dates[1].month}月{dates[1].day}日"
    )


def _coverage_label(value: bool | None) -> str:
    if value is True:
        return "covered"
    if value is False:
        return "uncovered"
    return "unconfirmed"


def _processed_rows(
    awards: list[dict[str, object]], crosswalk: list[dict[str, str]]
) -> list[dict[str, str]]:
    by_entity: dict[str, list[dict[str, str]]] = {}
    for row in crosswalk:
        by_entity.setdefault(row.get("entity", ""), []).append(row)

    output: list[dict[str, str]] = []
    for award in awards:
        entity = str(award.get("entity", ""))
        report_rows = sorted(
            by_entity.get(entity, []), key=lambda row: row.get("report_key", "")
        )
        if not report_rows:
            raise ValueError(f"award entity absent from crosswalk: {entity!r}")
        coverage_period = _coverage_period(award)
        for report in report_rows:
            matched, match_method = operator_match(
                award.get("supplier_name"), report.get("report_operator")
            )
            coverage = contract_covers_year(
                coverage_period, report.get("academic_year")
            )
            coverage_status = _coverage_label(coverage)
            if not matched:
                join_status = "supplier_operator_unconfirmed"
            elif coverage is True:
                join_status = "matched_operator_contract_covered"
            elif coverage is False:
                join_status = "matched_operator_contract_uncovered"
            else:
                join_status = "matched_operator_contract_unconfirmed"
            evidence = ";".join(
                [
                    f"operator:{match_method}",
                    f"award_period:{coverage_status}",
                    f"registry_ids:{len(json.loads(report.get('registry_ids') or '[]'))}",
                    f"crosswalk:{report.get('join_status', '')}",
                ]
            )
            output.append(
                {
                    "query_id": str(award.get("query_id", "")),
                    "entity": entity,
                    "award_key": str(award.get("award_key", "")),
                    "supplier_award_key": str(award.get("supplier_award_key", "")),
                    "unit_id": str(award.get("unit_id", "")),
                    "unit_name": str(award.get("unit_name", "")),
                    "job_number": str(award.get("job_number", "")),
                    "tender_title": str(award.get("tender_title", "")),
                    "award_notice_type": str(award.get("award_notice_type", "")),
                    "award_notice_date": str(award.get("award_notice_date") or ""),
                    "award_decision_date": str(award.get("award_decision_date") or ""),
                    "award_decision_date_raw": str(
                        award.get("award_decision_date_raw", "")
                    ),
                    "supplier_id": str(award.get("supplier_id", "")),
                    "supplier_id_raw": str(award.get("supplier_id_raw", "")),
                    "supplier_name": str(award.get("supplier_name", "")),
                    "source_supplier_index": str(
                        award.get("source_supplier_index", "")
                    ),
                    "award_amount": (
                        ""
                        if award.get("award_amount") is None
                        else str(award["award_amount"])
                    ),
                    "award_amount_raw": str(award.get("award_amount_raw", "")),
                    "total_award_amount": (
                        ""
                        if award.get("total_award_amount") is None
                        else str(award["total_award_amount"])
                    ),
                    "total_award_amount_raw": str(
                        award.get("total_award_amount_raw", "")
                    ),
                    "contract_period_raw": str(
                        award.get("contract_period_raw", "")
                    ),
                    "contract_start_date": str(
                        award.get("contract_start_date") or ""
                    ),
                    "contract_end_date": str(award.get("contract_end_date") or ""),
                    "official_notice_url": str(
                        award.get("official_notice_url", "")
                    ),
                    "notice_revisions": _json_array(
                        award.get("notice_revisions") or []
                    ),
                    "search_raw_file": str(award.get("search_raw_file", "")),
                    "search_response_sha256": str(
                        award.get("search_response_sha256", "")
                    ),
                    "tender_raw_file": str(award.get("tender_raw_file", "")),
                    "tender_response_sha256": str(
                        award.get("tender_response_sha256", "")
                    ),
                    "report_file": report.get("report_file", ""),
                    "report_key": report.get("report_key", ""),
                    "code": report.get("code", ""),
                    "short_name": report.get("short_name", ""),
                    "academic_year": report.get("academic_year", ""),
                    "report_operator": report.get("report_operator", ""),
                    "report_contract_period": report.get("contract_period", ""),
                    "registry_ids": report.get("registry_ids", "[]"),
                    "registry_titles": report.get("registry_titles", "[]"),
                    "registry_operators": report.get("registry_operators", "[]"),
                    "operator_matched_ids": report.get(
                        "operator_matched_ids", "[]"
                    ),
                    "supplier_operator_match": str(matched).lower(),
                    "supplier_operator_match_method": match_method,
                    "procurement_contract_covers_year": coverage_status,
                    "temporal_join_status": join_status,
                    "join_evidence": evidence,
                }
            )
    return output


def _csv_bytes(rows: list[dict[str, str]]) -> bytes:
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return handle.getvalue().encode("utf-8")


def _write_processed(data: bytes) -> None:
    PROCESSED_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = PROCESSED_PATH.with_suffix(".csv.tmp")
    temporary.write_bytes(data)
    temporary.replace(PROCESSED_PATH)


def _request_entry(
    artifact: ResponseArtifact,
    *,
    role: str,
    raw_file: str,
    public_query: dict[str, object],
    parsed_count: int,
) -> dict[str, object]:
    return {
        "sequence": artifact.sequence,
        "role": role,
        "method": "GET",
        "source_url": artifact.source_url,
        "final_url": artifact.final_url,
        "public_query": public_query,
        "response_sha256": artifact.sha256,
        "byte_length": len(artifact.body),
        "status": artifact.status,
        "content_type": artifact.content_type,
        "retrieved_at_utc": artifact.retrieved_at_utc,
        "raw_file": raw_file,
        "parser_schema_version": PARSER_SCHEMA_VERSION,
        "parsed_result_count": parsed_count,
    }


def acquire(snapshot_id: str, timeout: int) -> None:
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id) or snapshot_id in {".", ".."}:
        raise ValueError("snapshot ID may contain only letters, digits, dot, dash, underscore")
    target = SNAPSHOT_ROOT / snapshot_id
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing snapshot: {target}")

    crosswalk_data, crosswalk = _live_crosswalk()
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    client = ProcurementClient(timeout=timeout)
    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    requests: list[dict[str, object]] = []
    parsed_awards: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory(prefix="procurement-", dir=SNAPSHOT_ROOT) as tmp:
        stage = pathlib.Path(tmp)
        raw_dir = stage / "raw"
        parsed_dir = stage / "parsed"
        inputs_dir = stage / "inputs"
        raw_dir.mkdir()
        parsed_dir.mkdir()
        inputs_dir.mkdir()
        (inputs_dir / CROSSWALK_PATH.name).write_bytes(crosswalk_data)

        def persist(
            artifact: ResponseArtifact,
            *,
            role: str,
            stem: str,
            public_query: dict[str, object],
            parsed_count: int,
        ) -> str:
            filename = f"raw/{artifact.sequence:03d}_{stem}.json"
            (stage / filename).write_bytes(artifact.body)
            requests.append(
                _request_entry(
                    artifact,
                    role=role,
                    raw_file=filename,
                    public_query=public_query,
                    parsed_count=parsed_count,
                )
            )
            if len(requests) > MAX_REQUESTS:
                raise ValueError("procurement pilot request cap exceeded")
            return filename

        def require_request_budget() -> None:
            if len(requests) >= MAX_REQUESTS:
                raise ValueError("procurement pilot request cap exhausted")

        for query in PILOT_QUERIES:
            public_query = {
                "query_id": query["id"],
                "entity": query["entity"],
                "search_text": query["query"],
                "case": query["case"],
            }
            require_request_budget()
            search = client.search_title(str(query["query"]))
            search_payload = parse_search_response(search.json())
            if search_payload["total_pages"] > MAX_SEARCH_PAGES:
                raise ValueError(f"{query['id']}: search pagination cap exceeded")
            if len(search_payload["records"]) > MAX_SEARCH_RECORDS:
                raise ValueError(f"{query['id']}: search result cap exceeded")
            if len(search_payload["records"]) > search_payload["total_records"]:
                raise ValueError(f"{query['id']}: impossible search record count")
            tenders = discover_contract_tenders(
                search_payload, short_name=str(query["short_name"])
            )
            expected = int(query["expected_contract_tenders"])
            if len(tenders) != expected:
                raise ValueError(
                    f"{query['id']}: expected {expected} operating-contract tenders, "
                    f"found {len(tenders)}"
                )
            search_file = persist(
                search,
                role="title_search",
                stem=f"{query['id']}_search",
                public_query=public_query,
                parsed_count=len(tenders),
            )

            for tender in tenders:
                require_request_budget()
                detail = client.tender(tender["unit_id"], tender["job_number"])
                detail_payload = detail.json()
                if not isinstance(detail_payload, dict) or not isinstance(
                    detail_payload.get("records"), list
                ):
                    raise ValueError(f"{query['id']}: invalid tender response envelope")
                if len(detail_payload["records"]) > MAX_TENDER_NOTICES:
                    raise ValueError(f"{query['id']}: tender notice cap exceeded")
                awards = parse_tender_awards(detail_payload)
                if len(awards) != 1:
                    raise ValueError(
                        f"{query['id']}: expected one canonical winning supplier, "
                        f"found {len(awards)}"
                    )
                detail_file = persist(
                    detail,
                    role="tender_history",
                    stem=f"{query['id']}_{tender['unit_id']}_{tender['job_number']}",
                    public_query={
                        **public_query,
                        "unit_id": tender["unit_id"],
                        "job_number": tender["job_number"],
                    },
                    parsed_count=len(awards),
                )
                parsed_awards.extend(
                    {
                        **award,
                        "query_id": query["id"],
                        "entity": query["entity"],
                        "search_raw_file": search_file,
                        "search_response_sha256": search.sha256,
                        "tender_raw_file": detail_file,
                        "tender_response_sha256": detail.sha256,
                    }
                    for award in awards
                )

        if len(requests) != MAX_REQUESTS:
            raise ValueError(
                f"pilot request invariant changed: expected {MAX_REQUESTS}, "
                f"found {len(requests)}"
            )
        parsed_payload = {
            "schema_version": PARSER_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "records": parsed_awards,
        }
        parsed_bytes = (
            json.dumps(parsed_payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        (parsed_dir / "procurement_awards.json").write_bytes(parsed_bytes)

        processed_rows = _processed_rows(parsed_awards, crosswalk)
        if len(processed_rows) != 12:
            raise ValueError(
                "pilot timeline invariant changed: expected 12 rows, "
                f"found {len(processed_rows)}"
            )
        processed_bytes = _csv_bytes(processed_rows)
        (stage / "nonprofit_procurement_contracts.csv").write_bytes(processed_bytes)
        join_counts = Counter(row["temporal_join_status"] for row in processed_rows)
        match_counts = Counter(
            row["supplier_operator_match_method"] for row in processed_rows
        )
        completed = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        manifest = {
            "manifest_version": 1,
            "snapshot_id": snapshot_id,
            "official_source_url": OFFICIAL_SOURCE_URL,
            "civic_project_url": CIVIC_PROJECT_URL,
            "api_origin": API_ORIGIN,
            "source_relationship": (
                "The civic API mirrors Government Electronic Procurement System "
                "records; official web.pcc.gov.tw notice URLs are retained per award."
            ),
            "started_at_utc": started.isoformat().replace("+00:00", "Z"),
            "completed_at_utc": completed.isoformat().replace("+00:00", "Z"),
            "tls_verification": "system trust store; no bypass",
            "parser_schema_version": PARSER_SCHEMA_VERSION,
            "pilot_scope": {
                "queries": list(PILOT_QUERIES),
                "full_population_claim": False,
                "search_page_cap": MAX_SEARCH_PAGES,
                "search_record_cap": MAX_SEARCH_RECORDS,
                "tender_notice_cap": MAX_TENDER_NOTICES,
                "request_cap": MAX_REQUESTS,
                "zero_result_semantics": (
                    "No discovered operating-contract award is not evidence of no "
                    "contract or low risk."
                ),
            },
            "requests": requests,
            "inputs": {
                "inputs/nonprofit_registry_crosswalk.csv": {
                    "source_path": CROSSWALK_PATH.as_posix(),
                    "sha256": _sha256(crosswalk_data),
                    "byte_length": len(crosswalk_data),
                    "record_count": len(crosswalk),
                }
            },
            "outputs": {
                "parsed/procurement_awards.json": {
                    "sha256": _sha256(parsed_bytes),
                    "byte_length": len(parsed_bytes),
                    "record_count": len(parsed_awards),
                },
                "data/processed/nonprofit_procurement_contracts.csv": {
                    "sha256": _sha256(processed_bytes),
                    "byte_length": len(processed_bytes),
                    "record_count": len(processed_rows),
                },
            },
            "join_summary": dict(sorted(join_counts.items())),
            "operator_match_summary": dict(sorted(match_counts.items())),
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        try:
            stage.replace(target)
            source_csv = target / "nonprofit_procurement_contracts.csv"
            PROCESSED_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary_csv = PROCESSED_PATH.with_suffix(".csv.tmp")
            shutil.copyfile(source_csv, temporary_csv)
            temporary_csv.replace(PROCESSED_PATH)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            raise

    print(f"wrote {target} ({len(requests)} responses, {len(parsed_awards)} awards)")
    print(f"wrote {PROCESSED_PATH} ({len(processed_rows)} report-year rows)")
    print(f"temporal joins: {dict(sorted(join_counts.items()))}")


def _verified_path(snapshot: pathlib.Path, relative: object) -> pathlib.Path:
    snapshot_root = snapshot.resolve()
    path = (snapshot / str(relative)).resolve()
    try:
        path.relative_to(snapshot_root)
    except ValueError as exc:
        raise ValueError("manifest path escapes snapshot directory") from exc
    return path


def _derive_awards_from_raw(
    snapshot: pathlib.Path, manifest: dict[str, object]
) -> list[dict[str, object]]:
    query_specs = {str(query["id"]): query for query in PILOT_QUERIES}
    search_context: dict[str, dict[str, object]] = {}
    awards: list[dict[str, object]] = []
    request_entries = manifest.get("requests")
    if not isinstance(request_entries, list) or len(request_entries) != MAX_REQUESTS:
        raise ValueError("pinned procurement request count differs from pilot contract")

    for entry in request_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("public_query"), dict):
            raise ValueError("invalid pinned procurement request entry")
        public = entry["public_query"]
        query_id = str(public.get("query_id", ""))
        query = query_specs.get(query_id)
        if query is None:
            raise ValueError(f"unknown pinned procurement query: {query_id!r}")
        raw_path = _verified_path(snapshot, entry["raw_file"])
        payload = json.loads(raw_path.read_bytes())
        role = entry.get("role")
        if role == "title_search":
            parsed = parse_search_response(payload)
            if parsed["total_pages"] > MAX_SEARCH_PAGES:
                raise ValueError(f"{query_id}: pinned search pagination cap exceeded")
            if len(parsed["records"]) > MAX_SEARCH_RECORDS:
                raise ValueError(f"{query_id}: pinned search result cap exceeded")
            tenders = discover_contract_tenders(
                parsed, short_name=str(query["short_name"])
            )
            if len(tenders) != int(query["expected_contract_tenders"]):
                raise ValueError(f"{query_id}: pinned contract discovery changed")
            if entry.get("parsed_result_count") != len(tenders):
                raise ValueError(f"{query_id}: pinned search parsed count differs")
            search_context[query_id] = {
                "raw_file": str(entry["raw_file"]),
                "sha256": str(entry["response_sha256"]),
                "tenders": {
                    (tender["unit_id"], tender["job_number"]) for tender in tenders
                },
            }
        elif role == "tender_history":
            context = search_context.get(query_id)
            if context is None:
                raise ValueError(f"{query_id}: tender response precedes its search")
            unit_id = str(public.get("unit_id", ""))
            job_number = str(public.get("job_number", ""))
            if (unit_id, job_number) not in context["tenders"]:
                raise ValueError(f"{query_id}: tender was not discovered by pinned search")
            tender_records = payload.get("records") if isinstance(payload, dict) else None
            if not isinstance(tender_records, list):
                raise ValueError(f"{query_id}: invalid pinned tender envelope")
            if len(tender_records) > MAX_TENDER_NOTICES:
                raise ValueError(f"{query_id}: pinned tender notice cap exceeded")
            parsed_awards = parse_tender_awards(payload)
            if entry.get("parsed_result_count") != len(parsed_awards):
                raise ValueError(f"{query_id}: pinned tender parsed count differs")
            awards.extend(
                {
                    **award,
                    "query_id": query_id,
                    "entity": query["entity"],
                    "search_raw_file": context["raw_file"],
                    "search_response_sha256": context["sha256"],
                    "tender_raw_file": str(entry["raw_file"]),
                    "tender_response_sha256": str(entry["response_sha256"]),
                }
                for award in parsed_awards
            )
        else:
            raise ValueError(f"unsupported pinned procurement request role: {role!r}")

    if set(search_context) != set(query_specs) or len(awards) != 3:
        raise ValueError("pinned procurement query/award invariant changed")
    return awards


def rebuild_from_snapshot(snapshot_id: str) -> None:
    """Verify pinned evidence and rebuild the processed timeline without network."""
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id) or snapshot_id in {".", ".."}:
        raise ValueError("invalid snapshot ID")
    snapshot = SNAPSHOT_ROOT / snapshot_id
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("snapshot_id") != snapshot_id:
        raise ValueError("snapshot directory and manifest ID differ")
    if manifest.get("api_origin") != API_ORIGIN:
        raise ValueError("snapshot API origin is not the allowlisted procurement host")
    if manifest.get("official_source_url") != OFFICIAL_SOURCE_URL:
        raise ValueError("snapshot official source URL differs")
    pilot_scope = manifest.get("pilot_scope")
    if not isinstance(pilot_scope, dict) or pilot_scope.get("queries") != list(
        PILOT_QUERIES
    ):
        raise ValueError("snapshot pilot query contract differs")
    request_entries = manifest.get("requests")
    if not isinstance(request_entries, list):
        raise ValueError("snapshot requests must be an array")

    for entry in request_entries:
        if not isinstance(entry, dict):
            raise ValueError("snapshot request entry must be an object")
        raw_path = _verified_path(snapshot, entry["raw_file"])
        data = raw_path.read_bytes()
        if (
            len(data) != entry.get("byte_length")
            or _sha256(data) != entry.get("response_sha256")
        ):
            raise ValueError(f"raw response integrity mismatch: {raw_path}")

    parsed_path = snapshot / "parsed" / "procurement_awards.json"
    parsed_data = parsed_path.read_bytes()
    parsed_expected = manifest["outputs"]["parsed/procurement_awards.json"]
    if (
        len(parsed_data) != parsed_expected.get("byte_length")
        or _sha256(parsed_data) != parsed_expected.get("sha256")
    ):
        raise ValueError("parsed procurement snapshot integrity mismatch")
    parsed = json.loads(parsed_data)
    if parsed.get("schema_version") != PARSER_SCHEMA_VERSION:
        raise ValueError("unsupported pinned procurement parser schema")
    records = parsed.get("records")
    if not isinstance(records, list) or not all(
        isinstance(record, dict) for record in records
    ):
        raise ValueError("pinned procurement records must be an array of objects")
    derived_records = _derive_awards_from_raw(snapshot, manifest)
    if records != derived_records:
        raise ValueError("parsed procurement awards differ from pinned raw derivation")

    inputs = manifest.get("inputs")
    if not isinstance(inputs, dict):
        raise ValueError("snapshot inputs must be an object")
    input_entry = inputs.get("inputs/nonprofit_registry_crosswalk.csv")
    if not isinstance(input_entry, dict):
        raise ValueError("snapshot is missing pinned crosswalk metadata")
    crosswalk_path = _verified_path(
        snapshot, "inputs/nonprofit_registry_crosswalk.csv"
    )
    crosswalk_data = crosswalk_path.read_bytes()
    if (
        len(crosswalk_data) != input_entry.get("byte_length")
        or _sha256(crosswalk_data) != input_entry.get("sha256")
    ):
        raise ValueError("pinned crosswalk integrity mismatch")
    crosswalk = _crosswalk_rows_from_bytes(crosswalk_data)
    if len(crosswalk) != input_entry.get("record_count"):
        raise ValueError("pinned crosswalk record count differs")

    rows = _processed_rows(derived_records, crosswalk)
    output = _csv_bytes(rows)
    expected = manifest["outputs"][
        "data/processed/nonprofit_procurement_contracts.csv"
    ]
    if (
        len(output) != expected.get("byte_length")
        or _sha256(output) != expected.get("sha256")
    ):
        raise ValueError(
            "offline rebuild differs from pinned output; review parser or crosswalk drift"
        )
    _write_processed(output)
    print(f"verified {len(request_entries)} pinned responses and raw derivation")
    print(f"rebuilt {PROCESSED_PATH} ({len(rows)} rows, no network)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--snapshot-id", help="immutable ID for a new snapshot")
    mode.add_argument(
        "--rebuild-from",
        metavar="SNAPSHOT_ID",
        help="verify a pinned snapshot and rebuild without network",
    )
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not args.snapshot_id and not args.rebuild_from:
        args.snapshot_id = f"{dt.date.today().isoformat()}-pilot-v1"
    return args


def main() -> None:
    args = parse_args()
    if args.rebuild_from:
        rebuild_from_snapshot(args.rebuild_from)
    else:
        acquire(args.snapshot_id, args.timeout)


if __name__ == "__main__":
    main()
