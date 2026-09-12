"""Small, source-faithful helpers for the public procurement pilot.

The civic API mirrors records originating from the official Government
Electronic Procurement System.  Callers must retain each ``detail.url`` as the
official provenance URL and must not treat the mirror as a second authority.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from smart_watchdog.scrape import registry

API_ORIGIN = "https://pcc-api.openfun.app"
SEARCH_PATH = "/api/searchbytitle"
TENDER_PATH = "/api/tender"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
AWARD_TYPES = {"決標公告", "更正決標公告"}
CONTRACT_TITLE_MARKERS = ("委託營運管理", "委託辦理")
_SUPPLIER_KEY_RE = re.compile(r"^投標廠商:投標廠商(?P<index>\d+):(?P<field>.+)$")
_ROC_DATE_RE = re.compile(r"(?P<year>\d{2,3})/(?P<month>\d{1,2})/(?P<day>\d{1,2})")
_AMOUNT_RE = re.compile(
    r"^\s*(?P<amount>(?:0|[1-9]\d*|[1-9]\d{0,2}(?:,\d{3})+))\s*元?\s*$"
)
_NOTICE_REVISION_RE = re.compile(r"^BDM-(?P<sequence>\d+)-")
_REPEATED_SCHOOL_FOUNDATION_RE = re.compile(
    r"^(?P<prefix>.+?學校財團法人)設立之(?P=prefix)"
)


def _require_dict(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _require_records(value: object, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{label} must be an array of objects")
    return value


def parse_search_response(payload: object) -> dict[str, Any]:
    """Validate the stable search envelope without discarding source fields."""
    source = _require_dict(payload, "search response")
    records = _require_records(source.get("records"), "search records")
    for field in ("query", "page", "total_records", "total_pages"):
        if field not in source:
            raise ValueError(f"search response missing {field!r}")
    try:
        page = int(source["page"])
        total_records = int(source["total_records"])
        total_pages = int(source["total_pages"])
    except (TypeError, ValueError) as exc:
        raise ValueError("search pagination fields must be integers") from exc
    if page < 1 or total_records < 0 or total_pages < 0:
        raise ValueError("invalid search pagination values")
    for index, record in enumerate(records):
        brief = _require_dict(record.get("brief"), f"search record {index} brief")
        for field in ("date", "job_number", "unit_id", "unit_name"):
            if field not in record:
                raise ValueError(f"search record {index} missing {field!r}")
        if not isinstance(brief.get("title"), str) or not isinstance(
            brief.get("type"), str
        ):
            raise ValueError(f"search record {index} has invalid brief title/type")
    return {
        "query": str(source["query"]),
        "page": page,
        "total_records": total_records,
        "total_pages": total_pages,
        "records": records,
    }


def discover_contract_tenders(
    payload: object, *, short_name: str
) -> list[dict[str, str]]:
    """Find unique operating-contract tenders, excluding equipment and works."""
    parsed = parse_search_response(payload)
    found: dict[tuple[str, str], dict[str, str]] = {}
    for record in parsed["records"]:
        brief = record["brief"]
        title = str(brief["title"])
        if (
            str(brief["type"]) not in AWARD_TYPES
            or short_name not in title
            or "幼兒園" not in title
            or not any(marker in title for marker in CONTRACT_TITLE_MARKERS)
        ):
            continue
        key = (str(record["unit_id"]), str(record["job_number"]))
        found[key] = {
            "unit_id": key[0],
            "job_number": key[1],
            "unit_name": str(record["unit_name"]),
            "title": title,
        }
    return [found[key] for key in sorted(found)]


def _roc_date(value: object) -> str | None:
    match = _ROC_DATE_RE.search(str(value or ""))
    if not match:
        return None
    year = int(match.group("year")) + 1911
    month = int(match.group("month"))
    day = int(match.group("day"))
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_contract_period(value: object) -> tuple[str | None, str | None]:
    """Parse the first two ROC dates in a source period, preserving unknowns."""
    dates = list(_ROC_DATE_RE.finditer(str(value or "")))
    if len(dates) < 2:
        return None, None
    parsed = [_roc_date(match.group(0)) for match in dates[:2]]
    return parsed[0], parsed[1]


def parse_amount(value: object) -> int | None:
    """Parse a published integer amount; blank/invalid values remain unknown."""
    match = _AMOUNT_RE.fullmatch(str(value or ""))
    if not match:
        return None
    try:
        return int(match.group("amount").replace(",", ""))
    except ValueError:
        return None


def normalise_supplier(value: object) -> str:
    """Normalise known legal-name rendering differences, not arbitrary aliases."""
    compact = "".join(str(value or "").split())
    compact = _REPEATED_SCHOOL_FOUNDATION_RE.sub(r"\g<prefix>", compact)
    return registry.normalise_operator(compact)


def operator_match(supplier_name: object, report_operator: object) -> tuple[bool, str]:
    """Return a conservative match plus auditable comparison method."""
    supplier = "".join(str(supplier_name or "").split())
    operator = "".join(str(report_operator or "").split())
    if not supplier or not operator:
        return False, "missing_name"
    if supplier == operator:
        return True, "exact_name"
    if normalise_supplier(supplier) == normalise_supplier(operator):
        return True, "normalised_legal_name"
    if registry.operators_match(supplier, operator):
        return False, "weak_contains_unconfirmed"
    return False, "no_match"


def _winning_suppliers(detail: dict[str, Any]) -> list[dict[str, object]]:
    grouped: dict[int, dict[str, object]] = {}
    for key, value in detail.items():
        match = _SUPPLIER_KEY_RE.match(str(key))
        if not match:
            continue
        index = int(match.group("index"))
        grouped.setdefault(index, {})[match.group("field")] = value

    winners: list[dict[str, object]] = []
    for index in sorted(grouped):
        fields = grouped[index]
        if str(fields.get("是否得標", "")).strip() != "是":
            continue
        name = str(fields.get("廠商名稱", "")).strip()
        if not name:
            raise ValueError(f"winning supplier {index} has no name")
        identifier_raw = str(fields.get("廠商代碼", "")).strip()
        identifier = identifier_raw if identifier_raw.isdigit() else ""
        amount_raw = str(fields.get("決標金額", "")).strip()
        period_raw = str(fields.get("履約起迄日期", "")).strip()
        start, end = parse_contract_period(period_raw)
        winners.append(
            {
                "source_supplier_index": index,
                "supplier_id": identifier,
                "supplier_id_raw": identifier_raw,
                "supplier_name": name,
                "award_amount": parse_amount(amount_raw),
                "award_amount_raw": amount_raw,
                "contract_period_raw": period_raw,
                "contract_start_date": start,
                "contract_end_date": end,
            }
        )
    return winners


def _award_notice(record: dict[str, Any]) -> bool:
    detail = _require_dict(record.get("detail"), "tender record detail")
    return str(detail.get("type", "")) in AWARD_TYPES


def _revision_sequence(record: dict[str, Any]) -> int:
    detail = _require_dict(record.get("detail"), "tender record detail")
    explicit = str(detail.get("決標資料:公告更正序號", "")).strip()
    if explicit.isdigit():
        return int(explicit) + 1
    match = _NOTICE_REVISION_RE.match(str(record.get("filename", "")))
    return int(match.group("sequence")) if match else 0


def _notice_revision(record: dict[str, Any]) -> dict[str, object]:
    detail = _require_dict(record.get("detail"), "tender record detail")
    official_url = str(detail.get("url", ""))
    parsed = urllib.parse.urlparse(official_url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "web.pcc.gov.tw"
        or parsed.port not in (None, 443)
    ):
        raise ValueError(f"invalid official procurement detail URL: {official_url!r}")
    return {
        "notice_date": str(record.get("date", "")),
        "notice_type": str(detail.get("type", "")),
        "revision_sequence": _revision_sequence(record),
        "filename": str(record.get("filename", "")),
        "official_url": official_url,
    }


def parse_tender_awards(payload: object) -> list[dict[str, object]]:
    """Canonicalise a tender's latest award revision into winner-level records."""
    source = _require_dict(payload, "tender response")
    records = _require_records(source.get("records"), "tender records")
    if not records:
        return []
    award_notices = [record for record in records if _award_notice(record)]
    if not award_notices:
        return []
    award_notices.sort(
        key=lambda row: (
            int(row.get("date", 0)),
            _revision_sequence(row),
            str(row.get("filename", "")),
        )
    )
    canonical = award_notices[-1]
    detail = _require_dict(canonical.get("detail"), "canonical award detail")
    brief = _require_dict(canonical.get("brief"), "canonical award brief")
    winners = _winning_suppliers(detail)
    if not winners:
        raise ValueError("canonical award notice has no winning supplier")

    unit_id = str(canonical.get("unit_id", ""))
    job_number = str(canonical.get("job_number", ""))
    if not unit_id or not job_number:
        raise ValueError("canonical award is missing unit_id/job_number")
    decision_date_raw = str(detail.get("決標資料:決標日期", ""))
    decision_date = _roc_date(decision_date_raw)
    notice_revisions = [_notice_revision(record) for record in award_notices]
    official_url = str(detail["url"])
    title = str(detail.get("已公告資料:標案名稱") or brief.get("title") or "")
    total_raw = str(detail.get("決標資料:總決標金額", "")).strip()
    award_key_seed = f"{unit_id}\0{job_number}\0{decision_date or decision_date_raw}"
    award_key = "pcc-" + hashlib.sha256(award_key_seed.encode("utf-8")).hexdigest()[:20]

    output: list[dict[str, object]] = []
    for winner in winners:
        supplier_seed = str(
            winner.get("supplier_id")
            or normalise_supplier(winner["supplier_name"])
        )
        supplier_award_key = "pccs-" + hashlib.sha256(
            f"{award_key}\0{supplier_seed}".encode()
        ).hexdigest()[:20]
        output.append(
            {
                "award_key": award_key,
                "supplier_award_key": supplier_award_key,
                "unit_id": unit_id,
                "unit_name": str(canonical.get("unit_name") or source.get("unit_name") or ""),
                "job_number": job_number,
                "tender_title": title,
                "award_notice_type": str(detail.get("type", "")),
                "award_notice_date": _roc_date(
                    detail.get("決標資料:決標公告日期")
                ),
                "award_decision_date": decision_date,
                "award_decision_date_raw": decision_date_raw,
                "total_award_amount": parse_amount(total_raw),
                "total_award_amount_raw": total_raw,
                "official_notice_url": official_url,
                "notice_revisions": notice_revisions,
                **winner,
            }
        )
    return output


class _SameOriginRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, expected_url: str) -> None:
        self.expected = urllib.parse.urlparse(expected_url)
        super().__init__()

    def redirect_request(
        self,
        req: Any,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Any:
        target = urllib.parse.urlparse(newurl)
        if (
            target.scheme != "https"
            or target.hostname != self.expected.hostname
            or target.port not in (None, 443)
        ):
            raise ValueError(f"refused redirected procurement origin: {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclasses.dataclass(frozen=True)
class ResponseArtifact:
    """One verified in-memory JSON response."""

    sequence: int
    source_url: str
    final_url: str
    status: int
    content_type: str | None
    retrieved_at_utc: str
    body: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def json(self) -> object:
        return json.loads(self.body)


class ProcurementClient:
    """Verified-TLS, same-origin, size-capped client for the civic API mirror."""

    def __init__(
        self,
        origin: str = API_ORIGIN,
        *,
        timeout: int = 60,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        parsed = urllib.parse.urlparse(origin)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
        ):
            raise ValueError("procurement API origin must be an HTTPS origin")
        self.origin = origin.rstrip("/")
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._expected = parsed
        self._opener = urllib.request.build_opener(_SameOriginRedirectHandler(origin))
        self._sequence = 0

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self._expected.hostname
            or parsed.port not in (None, 443)
        ):
            raise ValueError(f"refused procurement request origin: {url!r}")

    def get(self, path: str, params: dict[str, object]) -> ResponseArtifact:
        if path not in {SEARCH_PATH, TENDER_PATH}:
            raise ValueError(f"procurement path is not allowlisted: {path!r}")
        query = urllib.parse.urlencode(params)
        url = f"{self.origin}{path}?{query}"
        self._validate_url(url)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "smart-watchdog-procurement-pilot/1",
                "Accept": "application/json",
            },
            method="GET",
        )
        deadline = time.monotonic() + self.timeout
        with self._opener.open(request, timeout=self.timeout) as response:
            final_url = response.geturl()
            self._validate_url(final_url)
            content_type = response.headers.get_content_type()
            if content_type != "application/json":
                raise ValueError(f"unexpected procurement content type: {content_type!r}")
            chunks: list[bytes] = []
            byte_length = 0
            while True:
                if time.monotonic() >= deadline:
                    raise TimeoutError("procurement response exceeded total deadline")
                chunk = response.read(min(64 * 1024, self.max_bytes + 1 - byte_length))
                if not chunk:
                    break
                chunks.append(chunk)
                byte_length += len(chunk)
                if byte_length > self.max_bytes:
                    raise ValueError(
                        f"procurement response exceeds {self.max_bytes} bytes"
                    )
            body = b"".join(chunks)
            status = response.status
        self._sequence += 1
        retrieved = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        return ResponseArtifact(
            sequence=self._sequence,
            source_url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            retrieved_at_utc=retrieved.isoformat().replace("+00:00", "Z"),
            body=body,
        )

    def search_title(self, query: str, *, page: int = 1) -> ResponseArtifact:
        return self.get(SEARCH_PATH, {"query": query, "page": page})

    def tender(self, unit_id: str, job_number: str) -> ResponseArtifact:
        return self.get(TENDER_PATH, {"unit_id": unit_id, "job_number": job_number})
