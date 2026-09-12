"""Acquire and parse a small, reproducible evaSearch WebForms pilot.

The script is the only network entry point for evaluation data. It uses verified
TLS, an in-memory cookie session, actual server-generated controls, and ordered
hidden state refreshed after every POST. Raw HTML is pinned before a processed
CSV is built; cookies and viewstate values are never copied into the manifest.

Run::

    PYTHONPATH=src .venv/bin/python scripts/download_evaluation_pilot.py \
        --snapshot-id YYYY-MM-DD-pilot-v2
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
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.scrape import registry
from smart_watchdog.scrape.evaluation import (
    EvaluationClient,
    ResponseArtifact,
    form_payload,
    inventory_controls,
    parse_evaluation_results,
    postback_controls,
)

SOURCE_URL = registry.OFFICIAL["evaluation"]
SNAPSHOT_ROOT = pathlib.Path("data/external/evaluation")
PROCESSED_PATH = pathlib.Path("data/processed/evaluations_ntpc.csv")
INSTITUTIONS_PATH = pathlib.Path("data/processed/institutions_ntpc.csv")
CROSSWALK_PATH = pathlib.Path("data/processed/nonprofit_registry_crosswalk.csv")
PARSER_SCHEMA_VERSION = "eva-search-results-v1"
CITY_LABEL = "新北市"
MAX_HISTORY_POSTS_PER_QUERY = 5
MAX_ROWS_PER_QUERY = 50
SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
POSTBACK_RE = re.compile(
    r"__doPostBack\(['\"](?P<target>[^'\"]*)['\"],"
    r"['\"](?P<argument>[^'\"]*)['\"]\)"
)
DETAIL_URL_RE = re.compile(r"window\.open\(['\"]([^'\"]+)['\"]")

PILOT_QUERIES = (
    {
        "id": "single_uuid_anxi",
        "name": "新北市安溪非營利幼兒園",
        "case": "single registry UUID with historical evaluations",
        "expect_zero": False,
    },
    {
        "id": "multi_uuid_beida",
        "name": "新北市北大非營利幼兒園",
        "case": "operator retendering with sibling registry UUIDs",
        "expect_zero": False,
    },
    {
        "id": "multi_uuid_bicheng",
        "name": "新北市碧城非營利幼兒園",
        "case": "second operator-retendering sibling UUID case",
        "expect_zero": False,
    },
    {
        "id": "operator_parentheses_xindianjiren",
        "name": "新北市新店及人非營利幼兒園",
        "case": "known registry entity with 申請辦理 title and no live result",
        "expect_zero": True,
    },
    {
        "id": "explicit_zero_control",
        "name": "__SMART_WATCHDOG_NO_MATCH__",
        "case": "explicit zero-result parser control",
        "expect_zero": True,
    },
)

CSV_FIELDS = [
    "query_id",
    "query_name",
    "raw_file",
    "response_sha256",
    "source_table_id",
    "source_row_index",
    "source_title",
    "city",
    "town",
    "establishment_type",
    "address",
    "phone",
    "website",
    "approved_capacity",
    "after_school",
    "evaluation_academic_year",
    "evaluation_completed_date",
    "evaluation_result",
    "evaluation_report_label",
    "evaluation_report_postback",
    "evaluation_report_detail_path",
    "registry_id",
    "entity",
    "registry_ids",
    "registry_titles",
    "crosswalk_report_keys",
    "join_status",
    "join_evidence",
]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_array(values: list[object]) -> str:
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


def _select_control(
    controls: list[dict[str, Any]], *, option_label: str
) -> tuple[dict[str, Any], str]:
    matches = [
        (control, str(option.get("value", "")))
        for control in controls
        for option in control.get("options", [])
        if option.get("text") == option_label
    ]
    if len(matches) != 1:
        raise ValueError(
            f"expected one select option labelled {option_label!r}, found {len(matches)}"
        )
    return matches[0]


def _discover_controls(initial_html: bytes) -> dict[str, str]:
    inventory = inventory_controls(initial_html)
    selects = list(inventory["selects"])
    city, city_value = _select_control(selects, option_label=CITY_LABEL)
    area, area_value = _select_control(selects, option_label="全部鄉鎮")
    evaluation, evaluation_value = _select_control(selects, option_label="不拘")

    text_inputs = list(inventory["text_inputs"])
    if len(text_inputs) != 1 or not text_inputs[0].get("name"):
        raise ValueError("expected exactly one named preschool search text input")
    submit_matches = [
        control
        for control in inventory["submit_controls"]
        if (control.get("value") or control.get("text")) == "搜尋"
        and control.get("name")
    ]
    if len(submit_matches) != 1:
        raise ValueError("expected exactly one named 搜尋 submit control")

    names = [
        city.get("name"),
        area.get("name"),
        evaluation.get("name"),
        text_inputs[0].get("name"),
        submit_matches[0].get("name"),
    ]
    if len(set(names)) != len(names) or not all(names):
        raise ValueError("evaluation controls must have distinct non-empty names")
    return {
        "city_name": str(city["name"]),
        "city_value": city_value,
        "city_onchange": str(city.get("onchange", "")),
        "area_name": str(area["name"]),
        "area_value": area_value,
        "evaluation_name": str(evaluation["name"]),
        "evaluation_value": evaluation_value,
        "preschool_name": str(text_inputs[0]["name"]),
        "search_name": str(submit_matches[0]["name"]),
        "search_value": str(
            submit_matches[0].get("value")
            or submit_matches[0].get("text")
            or ""
        ),
        "event_target_name": "__EVENTTARGET",
        "event_argument_name": "__EVENTARGUMENT",
    }


def _replace_payload(
    payload: list[tuple[str, str]], replacements: dict[str, str]
) -> list[tuple[str, str]]:
    replaced = [(name, value) for name, value in payload if name not in replacements]
    replaced.extend(replacements.items())
    return replaced


def _query_payload(
    html: bytes, controls: dict[str, str], preschool_name: str
) -> list[tuple[str, str]]:
    return _replace_payload(
        form_payload(html),
        {
            controls["preschool_name"]: preschool_name,
            controls["city_name"]: controls["city_value"],
            controls["area_name"]: controls["area_value"],
            controls["evaluation_name"]: controls["evaluation_value"],
            controls["search_name"]: controls["search_value"],
        },
    )


def _postback_payload(
    html: bytes,
    controls: dict[str, str],
    event_target: str,
    event_argument: str,
) -> list[tuple[str, str]]:
    return _replace_payload(
        form_payload(html),
        {
            controls["event_target_name"]: event_target,
            controls["event_argument_name"]: event_argument,
        },
    )


def _public_inventory(html: bytes) -> dict[str, object]:
    """Keep control structure but no hidden values or response cookies."""
    return inventory_controls(html)


def _request_entry(
    artifact: ResponseArtifact,
    *,
    role: str,
    raw_file: str,
    public_query: dict[str, object] | None,
    parsed_count: int | None,
) -> dict[str, object]:
    return {
        "sequence": artifact.sequence,
        "role": role,
        "method": artifact.method,
        "source_url": artifact.source_url,
        "final_url": artifact.final_url,
        "public_query": public_query,
        "request_body_sha256": artifact.request_body_sha256,
        "response_sha256": artifact.sha256,
        "byte_length": len(artifact.body),
        "status": artifact.status,
        "content_type": artifact.content_type,
        "charset": artifact.charset,
        "retrieved_at_utc": artifact.retrieved_at_utc,
        "predecessor_sequence": artifact.predecessor_sequence,
        "raw_file": raw_file,
        "parser_schema_version": PARSER_SCHEMA_VERSION,
        "parsed_result_count": parsed_count,
    }


def _load_csv(path: pathlib.Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _identity_context() -> tuple[list[dict[str, str]], dict[str, list[str]]]:
    institutions = _load_csv(INSTITUTIONS_PATH)
    if not institutions:
        raise ValueError(f"no institution rows in {INSTITUTIONS_PATH}")
    crosswalk_rows = _load_csv(CROSSWALK_PATH)
    report_keys: dict[str, list[str]] = {}
    for row in crosswalk_rows:
        entity = row.get("entity", "")
        if entity:
            report_keys.setdefault(entity, []).append(row.get("report_key", ""))
    return institutions, {
        entity: sorted(set(keys)) for entity, keys in report_keys.items()
    }


def _join_identity(
    source_title: str,
    institutions: list[dict[str, str]],
    crosswalk_report_keys: dict[str, list[str]],
) -> dict[str, str]:
    exact = [row for row in institutions if row.get("title") == source_title]
    candidate_entity = registry.entity_key(source_title)
    entity_candidates = [
        row for row in institutions if row.get("entity") == candidate_entity
    ]
    entities = sorted(
        {
            row.get("entity", "")
            for row in entity_candidates
            if row.get("entity")
        }
    )

    if len(exact) == 1 and len(entities) == 1:
        status = "matched_exact_title"
        registry_id = exact[0].get("id", "")
    elif not entity_candidates:
        status = "unresolved_no_entity"
        registry_id = ""
    elif len(entities) != 1:
        status = "unresolved_ambiguous_entity"
        registry_id = ""
    elif len(exact) > 1:
        status = "unresolved_ambiguous_exact_title"
        registry_id = ""
    else:
        status = "matched_entity_only"
        registry_id = ""

    entity = entities[0] if len(entities) == 1 else ""
    siblings = sorted(entity_candidates, key=lambda row: row.get("id", ""))
    evidence = ";".join(
        [
            f"exact_title:{len(exact)}",
            f"entity_candidates:{len(entity_candidates)}",
            f"entities:{len(entities)}",
            f"crosswalk_reports:{len(crosswalk_report_keys.get(entity, []))}",
        ]
    )
    return {
        "registry_id": registry_id,
        "entity": entity,
        "registry_ids": _json_array([row.get("id", "") for row in siblings]),
        "registry_titles": _json_array([row.get("title", "") for row in siblings]),
        "crosswalk_report_keys": _json_array(crosswalk_report_keys.get(entity, [])),
        "join_status": status,
        "join_evidence": evidence,
    }


def _detail_path(row: dict[str, object]) -> str:
    scripts = row.get("source_onclick") or []
    for script in scripts:
        match = DETAIL_URL_RE.search(str(script))
        if match:
            return match.group(1)
    return ""


def _processed_rows(
    records: list[dict[str, object]],
    institutions: list[dict[str, str]],
    crosswalk_report_keys: dict[str, list[str]],
) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for record in records:
        source = record["source"]
        if not isinstance(source, dict):
            raise ValueError("parsed evaluation source must be an object")
        source_title = str(source.get("園名", ""))
        identity = _join_identity(
            source_title, institutions, crosswalk_report_keys
        )
        links = [str(link) for link in record.get("source_links") or []]
        report_postback = next(
            (link for link in links if "__doPostBack" in link), ""
        )
        output.append(
            {
                "query_id": str(record["query_id"]),
                "query_name": str(record["query_name"]),
                "raw_file": str(record["raw_file"]),
                "response_sha256": str(record["response_sha256"]),
                "source_table_id": str(record.get("source_table_id", "")),
                "source_row_index": str(record.get("source_row_index", "")),
                "source_title": source_title,
                "city": str(source.get("縣市", "")),
                "town": str(source.get("鄉鎮", "")),
                "establishment_type": str(source.get("設立別", "")),
                "address": str(source.get("地址", "")),
                "phone": str(source.get("電話", "")),
                "website": str(source.get("園所網址", "")),
                "approved_capacity": str(source.get("核定人數", "")),
                "after_school": str(source.get("兼辦國小課後", "")),
                "evaluation_academic_year": str(source.get("評鑑學年度", "")),
                "evaluation_completed_date": str(source.get("評鑑完成日", "")),
                "evaluation_result": str(source.get("評鑑結果", "")),
                "evaluation_report_label": str(source.get("評鑑報告", "")),
                "evaluation_report_postback": report_postback,
                "evaluation_report_detail_path": _detail_path(record),
                **identity,
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


def acquire(snapshot_id: str, timeout: int) -> None:
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id) or snapshot_id in {".", ".."}:
        raise ValueError("snapshot ID may contain only letters, digits, dot, dash, underscore")
    target = SNAPSHOT_ROOT / snapshot_id
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing snapshot: {target}")

    institutions, crosswalk_report_keys = _identity_context()
    SNAPSHOT_ROOT.mkdir(parents=True, exist_ok=True)
    client = EvaluationClient(SOURCE_URL, timeout=timeout)
    started = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    requests: list[dict[str, object]] = []
    parsed_records: list[dict[str, object]] = []

    with tempfile.TemporaryDirectory(prefix="evaluation-", dir=SNAPSHOT_ROOT) as tmp:
        stage = pathlib.Path(tmp)
        raw_dir = stage / "raw"
        parsed_dir = stage / "parsed"
        raw_dir.mkdir()
        parsed_dir.mkdir()

        def persist(
            artifact: ResponseArtifact,
            *,
            role: str,
            stem: str,
            public_query: dict[str, object] | None,
            parsed_count: int | None,
        ) -> str:
            filename = f"raw/{artifact.sequence:03d}_{stem}.html"
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
            return filename

        current = client.get()
        persist(
            current,
            role="initial_get",
            stem="initial",
            public_query=None,
            parsed_count=None,
        )
        controls = _discover_controls(current.body)
        initial_inventory = _public_inventory(current.body)

        onchange = controls["city_onchange"]
        if onchange:
            match = POSTBACK_RE.search(onchange)
            if not match:
                raise ValueError("city onchange exists but has no recognizable postback")
            payload = _replace_payload(
                form_payload(current.body),
                {
                    controls["city_name"]: controls["city_value"],
                    controls["event_target_name"]: match.group("target"),
                    controls["event_argument_name"]: match.group("argument"),
                },
            )
            current = client.post(current, payload)
            persist(
                current,
                role="city_autopostback",
                stem="city_new_taipei",
                public_query={"city_label": CITY_LABEL},
                parsed_count=None,
            )

        for query in PILOT_QUERIES:
            public_query = {
                "query_id": query["id"],
                "city_label": CITY_LABEL,
                "preschool_name": query["name"],
                "case": query["case"],
            }
            current = client.post(
                current,
                _query_payload(current.body, controls, str(query["name"])),
            )
            search_rows = parse_evaluation_results(current.body)
            search_file = persist(
                current,
                role="query_post",
                stem=f"{query['id']}_search",
                public_query=public_query,
                parsed_count=len(search_rows),
            )

            history_posts = 0
            while True:
                history = [
                    control
                    for control in postback_controls(current.body)
                    if re.search(r"_lbPrev_\d+$", control["id"])
                ]
                if not history:
                    break
                if history_posts + len(history) > MAX_HISTORY_POSTS_PER_QUERY:
                    raise ValueError(f"{query['id']}: too many history postbacks")
                event = history[0]
                current = client.post(
                    current,
                    _postback_payload(
                        current.body,
                        controls,
                        event["event_target"],
                        event["event_argument"],
                    ),
                )
                history_posts += 1
                expanded_rows = parse_evaluation_results(current.body)
                search_file = persist(
                    current,
                    role="history_post",
                    stem=f"{query['id']}_history_{history_posts}",
                    public_query={
                        **public_query,
                        "event_meaning": "expand prior evaluations",
                    },
                    parsed_count=len(expanded_rows),
                )

            rows = parse_evaluation_results(current.body)
            if len(rows) > MAX_ROWS_PER_QUERY:
                raise ValueError(f"{query['id']}: pilot row limit exceeded")
            if bool(query["expect_zero"]) != (len(rows) == 0):
                expected = "zero" if query["expect_zero"] else "one or more"
                raise ValueError(
                    f"{query['id']}: expected {expected} rows, found {len(rows)}"
                )
            unique_titles = {
                str(row["source"].get("園名", ""))
                for row in rows
                if isinstance(row.get("source"), dict)
            }
            if len(unique_titles) > 1:
                raise ValueError(
                    f"{query['id']}: exact pilot query returned multiple institutions"
                )
            if any(
                control["event_argument"].startswith("Page$")
                for control in postback_controls(current.body)
            ):
                raise ValueError(
                    f"{query['id']}: exact query unexpectedly requires pagination"
                )

            parsed_records.extend(
                {
                    **row,
                    "query_id": query["id"],
                    "query_name": query["name"],
                    "raw_file": search_file,
                    "response_sha256": current.sha256,
                }
                for row in rows
            )

        parsed_payload = {
            "schema_version": PARSER_SCHEMA_VERSION,
            "snapshot_id": snapshot_id,
            "records": parsed_records,
        }
        parsed_bytes = (
            json.dumps(parsed_payload, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
        parsed_path = parsed_dir / "evaluation_results.json"
        parsed_path.write_bytes(parsed_bytes)

        processed_rows = _processed_rows(
            parsed_records, institutions, crosswalk_report_keys
        )
        unresolved = [
            row for row in processed_rows if row["join_status"].startswith("unresolved")
        ]
        if unresolved:
            details = sorted({row["source_title"] for row in unresolved})
            raise ValueError(f"evaluation identity join unresolved: {details}")

        processed_bytes = _csv_bytes(processed_rows)
        processed_stage = stage / "evaluations_ntpc.csv"
        processed_stage.write_bytes(processed_bytes)
        completed = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        join_counts = Counter(row["join_status"] for row in processed_rows)
        manifest = {
            "manifest_version": 1,
            "snapshot_id": snapshot_id,
            "source_url": SOURCE_URL,
            "started_at_utc": started.isoformat().replace("+00:00", "Z"),
            "completed_at_utc": completed.isoformat().replace("+00:00", "Z"),
            "tls_verification": "system trust store; no bypass",
            "session_cookies_persisted": False,
            "parser_schema_version": PARSER_SCHEMA_VERSION,
            "pilot_scope": {
                "city_label": CITY_LABEL,
                "queries": list(PILOT_QUERIES),
                "full_population_claim": False,
                "pagination_policy": (
                    "Exact-name queries must not paginate; unexpected pagination "
                    "fails closed rather than expanding pilot scope."
                ),
            },
            "control_names": controls,
            "initial_control_inventory": initial_inventory,
            "requests": requests,
            "outputs": {
                "parsed/evaluation_results.json": {
                    "sha256": _sha256(parsed_bytes),
                    "byte_length": len(parsed_bytes),
                    "record_count": len(parsed_records),
                },
                "data/processed/evaluations_ntpc.csv": {
                    "sha256": _sha256(processed_bytes),
                    "byte_length": len(processed_bytes),
                    "record_count": len(processed_rows),
                },
            },
            "join_summary": dict(sorted(join_counts.items())),
        }
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        # The processed copy is staged beside raw evidence first. On runtime
        # failure after the directory move, remove only the newly created target.
        try:
            stage.replace(target)
            source_csv = target / "evaluations_ntpc.csv"
            PROCESSED_PATH.parent.mkdir(parents=True, exist_ok=True)
            temporary_csv = PROCESSED_PATH.with_suffix(".csv.tmp")
            shutil.copyfile(source_csv, temporary_csv)
            temporary_csv.replace(PROCESSED_PATH)
        except BaseException:
            if target.exists():
                shutil.rmtree(target)
            raise

    print(f"wrote {target} ({len(requests)} responses, {len(parsed_records)} records)")
    print(f"wrote {PROCESSED_PATH} ({len(processed_rows)} rows)")
    print(f"join summary: {dict(sorted(join_counts.items()))}")


def rebuild_from_snapshot(snapshot_id: str) -> None:
    """Verify pinned evidence and rebuild the processed CSV without network I/O."""
    if not SNAPSHOT_ID_RE.fullmatch(snapshot_id) or snapshot_id in {".", ".."}:
        raise ValueError("invalid snapshot ID")
    snapshot = SNAPSHOT_ROOT / snapshot_id
    manifest_path = snapshot / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("snapshot_id") != snapshot_id:
        raise ValueError("snapshot directory and manifest ID differ")
    if manifest.get("source_url") != SOURCE_URL:
        raise ValueError("snapshot source URL is not the allowlisted evaSearch endpoint")

    snapshot_root = snapshot.resolve()
    for entry in manifest.get("requests", []):
        raw_path = (snapshot / str(entry["raw_file"])).resolve()
        try:
            raw_path.relative_to(snapshot_root)
        except ValueError as exc:
            raise ValueError("manifest raw path escapes snapshot directory") from exc
        data = raw_path.read_bytes()
        if (
            len(data) != entry.get("byte_length")
            or _sha256(data) != entry.get("response_sha256")
        ):
            raise ValueError(f"raw response integrity mismatch: {raw_path}")

    parsed_path = snapshot / "parsed" / "evaluation_results.json"
    parsed_data = parsed_path.read_bytes()
    parsed_expected = manifest["outputs"]["parsed/evaluation_results.json"]
    if (
        len(parsed_data) != parsed_expected.get("byte_length")
        or _sha256(parsed_data) != parsed_expected.get("sha256")
    ):
        raise ValueError("parsed evaluation snapshot integrity mismatch")
    parsed = json.loads(parsed_data)
    if parsed.get("schema_version") != PARSER_SCHEMA_VERSION:
        raise ValueError("unsupported pinned evaluation parser schema")
    records = parsed.get("records")
    if not isinstance(records, list):
        raise ValueError("pinned evaluation records must be a list")

    institutions, crosswalk_report_keys = _identity_context()
    rows = _processed_rows(records, institutions, crosswalk_report_keys)
    output = _csv_bytes(rows)
    expected = manifest["outputs"]["data/processed/evaluations_ntpc.csv"]
    if (
        len(output) != expected.get("byte_length")
        or _sha256(output) != expected.get("sha256")
    ):
        raise ValueError(
            "offline rebuild differs from pinned output; review parser or identity drift"
        )
    _write_processed(output)
    print(f"verified {len(manifest['requests'])} pinned responses")
    print(f"rebuilt {PROCESSED_PATH} ({len(rows)} rows, no network)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--snapshot-id",
        help="immutable ID for a new network-acquired snapshot",
    )
    mode.add_argument(
        "--rebuild-from",
        metavar="SNAPSHOT_ID",
        help="verify pinned responses and rebuild processed CSV without network",
    )
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not args.snapshot_id and not args.rebuild_from:
        args.snapshot_id = f"{dt.date.today().isoformat()}-pilot-v1"
    return args


def main() -> None:
    """Acquire a new snapshot, or rebuild processed output from pinned evidence."""
    args = parse_args()
    if args.rebuild_from:
        rebuild_from_snapshot(args.rebuild_from)
    else:
        acquire(args.snapshot_id, args.timeout)


if __name__ == "__main__":
    main()
