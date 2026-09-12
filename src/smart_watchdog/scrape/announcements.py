"""Bounded client and source-faithful parsers for NTPC education notices.

Network access is deliberately limited to :class:`AnnouncementClient`.  The HTML
and PDF parsers operate only on caller-supplied bytes so a pinned snapshot can be
re-derived without network access.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

import fitz

OFFICIAL_ORIGIN = "https://kidedu.ntpc.edu.tw"
DEFAULT_MAX_RESPONSE_BYTES = 15_000_000
DEFAULT_MAX_TOTAL_BYTES = 35_000_000
DEFAULT_MAX_REQUESTS = 7
DEFAULT_DEADLINE_SECONDS = 180
DEFAULT_PDF_PAGE_CAP = 20
IMPROVEMENT_STATUSES = frozenset(
    {"ordered", "reported_complete", "verified_complete", "not_complete", "unknown"}
)
DOWNLOAD_ACTION = "downloadfile"
BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
DATE_TOKEN_RE = re.compile(
    r"(?:(?P<year>\d{2,4})\s*[年./-]\s*)?"
    r"(?P<month>1[0-2]|0?[1-9])\s*[月./-]\s*"
    r"(?P<day>3[01]|[12]\d|0?[1-9])\s*日?"
)
PUBLICATION_LABEL_RE = re.compile(
    r"(?:發布|刊登|公告)日期\s*[：:]?\s*"
    r"(?P<date>\d{2,4}\s*[年./-]\s*\d{1,2}\s*[月./-]\s*\d{1,2}\s*日?)"
)
DISTRICT_RE = re.compile(
    r"(板橋|三重|中和|永和|新莊|新店|土城|蘆洲|樹林|汐止|鶯歌|三峽|淡水|瑞芳|"
    r"五股|泰山|林口|深坑|石碇|坪林|三芝|石門|八里|平溪|雙溪|貢寮|金山|萬里|"
    r"烏來)區"
)
INSTITUTION_END_RE = re.compile(r"幼兒園(?:[\u4e00-\u9fff]{1,16}分班)?")
IMPORTANT_LISTING_URL_TEMPLATE = (
    "https://kidedu.ntpc.edu.tw/p/403-1000-9-{page}.php?Lang=zh-tw"
)
IMPORTANT_LISTING_URL_PREFIX = (
    "https://kidedu.ntpc.edu.tw/p/403-1000-9-PAGE.php?Lang=zh-tw"
)
IMPORTANT_DETAIL_URL_RE = re.compile(
    r"https://kidedu\.ntpc\.edu\.tw/p/406-1000-(?P<notice_id>\d+),r9\.php"
)
IMPORTANT_LISTING_PATH_RE = re.compile(r"/p/403-1000-9-(?P<page>\d+)\.php")
IMPORTANT_OPTION_RE = re.compile(
    r"var\s+option\s*=\s*\{(?P<body>.*?)\}", re.DOTALL
)
IMPORTANT_OPTION_FIELDS = {
    "page_mode": re.compile(r"\bpageMode\s*:\s*['\"](?P<value>\d+)['\"]"),
    "current": re.compile(r"\bcurrentPage\s*:\s*(?P<value>\d+)"),
    "prefix": re.compile(r"\burlPrefix\s*:\s*['\"](?P<value>[^'\"]+)['\"]"),
    "total": re.compile(r"\btotalPage\s*:\s*(?P<value>\d+)"),
}
IMPORTANT_CATEGORY_RE = re.compile(
    r"a\.push\(\{\s*name\s*:\s*['\"]Rcg['\"]\s*,\s*"
    r"value\s*:\s*['\"]9['\"]\s*\}\)"
)
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class _ImportantListingParser(HTMLParser):
    """Collect listing rows and embedded pagination without interpreting either."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, object]] = []
        self.scripts: list[str] = []
        self._script_depth = 0
        self._script_parts: list[str] = []
        self._row_depth = 0
        self._date_depth = 0
        self._link_depth = 0
        self._date_parts: list[str] = []
        self._link_parts: list[str] = []
        self._hrefs: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attributes = _attributes(attrs)
        if tag == "script":
            self._script_depth += 1
            if self._script_depth == 1:
                self._script_parts = []
        elif self._script_depth and tag not in VOID_TAGS:
            self._script_depth += 1

        if tag == "div" and _has_class(attributes, "mtitle"):
            if self._row_depth:
                raise ValueError("nested Important Announcements listing rows")
            self._row_depth = 1
            self._date_parts = []
            self._link_parts = []
            self._hrefs = []
        elif self._row_depth and tag not in VOID_TAGS:
            self._row_depth += 1

        if self._row_depth and tag == "i" and _has_class(attributes, "mdate"):
            if self._date_depth:
                raise ValueError("nested Important Announcements listing dates")
            self._date_depth = 1
        elif self._date_depth and tag not in VOID_TAGS:
            self._date_depth += 1

        if self._row_depth and tag == "a":
            if self._link_depth:
                raise ValueError("nested Important Announcements listing links")
            href = attributes.get("href", "")
            if href:
                self._hrefs.append(href)
                self._link_depth = 1
        elif self._link_depth and tag not in VOID_TAGS:
            self._link_depth += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._script_depth:
            self._script_parts.append(data)
        if self._date_depth:
            self._date_parts.append(data)
        if self._link_depth:
            self._link_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self._script_depth == 1:
            self.scripts.append("".join(self._script_parts))
        if tag not in VOID_TAGS and self._script_depth:
            self._script_depth -= 1

        if tag not in VOID_TAGS and self._date_depth:
            self._date_depth -= 1
        if tag not in VOID_TAGS and self._link_depth:
            self._link_depth -= 1
        if tag not in VOID_TAGS and self._row_depth:
            self._row_depth -= 1
            if self._row_depth == 0:
                self.rows.append(
                    {
                        "hrefs": list(self._hrefs),
                        "title": " ".join(_normalise_lines(self._link_parts).split()),
                        "publication_date": "".join(self._date_parts).strip(),
                    }
                )


def _important_listing_page_number(url: str) -> int:
    parsed = urllib.parse.urlparse(url)
    match = IMPORTANT_LISTING_PATH_RE.fullmatch(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.hostname != urllib.parse.urlparse(OFFICIAL_ORIGIN).hostname
        or parsed.port not in (None, 443)
        or parsed.params
        or parsed.fragment
        or parsed.query != "Lang=zh-tw"
        or match is None
    ):
        raise ValueError("Important Announcements listing URL is not canonical")
    page_number = int(match.group("page"))
    if page_number <= 0 or url != IMPORTANT_LISTING_URL_TEMPLATE.format(page=page_number):
        raise ValueError("Important Announcements listing URL is not canonical")
    return page_number


def parse_important_announcements_listing(
    html: bytes, source_url: str
) -> dict[str, object]:
    """Strictly parse one live Important Announcements listing page.

    Pagination URLs are derived only from the validated JavaScript contract;
    JavaScript links in the rendered document are never followed or interpreted.
    """
    expected_page = _important_listing_page_number(source_url)
    parser = _ImportantListingParser()
    parser.feed(_decode_html(html))
    parser.close()

    option_matches = [
        match
        for script in parser.scripts
        for match in IMPORTANT_OPTION_RE.finditer(script)
        if "urlPrefix" in match.group("body")
    ]
    category_scripts = [
        script for script in parser.scripts if IMPORTANT_CATEGORY_RE.search(script)
    ]
    if len(option_matches) != 1 or len(category_scripts) != 1:
        raise ValueError("Important Announcements pagination contract is missing or ambiguous")
    option_body = option_matches[0].group("body")
    option_values: dict[str, str] = {}
    for name, pattern in IMPORTANT_OPTION_FIELDS.items():
        matches = list(pattern.finditer(option_body))
        if len(matches) != 1:
            raise ValueError(
                f"Important Announcements pagination field {name} is ambiguous"
            )
        option_values[name] = matches[0].group("value")
    page_number = int(option_values["current"])
    total_pages = int(option_values["total"])
    if option_values["page_mode"] != "2":
        raise ValueError("Important Announcements pagination pageMode differs")
    if option_values["prefix"] != IMPORTANT_LISTING_URL_PREFIX:
        raise ValueError("Important Announcements pagination urlPrefix differs")
    if page_number != expected_page or total_pages < page_number or total_pages <= 0:
        raise ValueError("Important Announcements pagination values are malformed")

    records: list[dict[str, object]] = []
    seen: dict[str, tuple[str, str, str]] = {}
    for source_index, raw in enumerate(parser.rows, start=1):
        hrefs = raw["hrefs"]
        if not isinstance(hrefs, list) or len(hrefs) != 1:
            raise ValueError("Important Announcements row must have one detail link")
        detail_url = str(hrefs[0])
        detail_match = IMPORTANT_DETAIL_URL_RE.fullmatch(detail_url)
        if detail_match is None:
            raise ValueError("Important Announcements detail link is noncanonical")
        notice_id = detail_match.group("notice_id")
        title = str(raw["title"])
        publication_date = str(raw["publication_date"])
        if not title:
            raise ValueError("Important Announcements row has a blank title")
        if not ISO_DATE_RE.fullmatch(publication_date):
            raise ValueError("Important Announcements row has a blank or malformed date")
        try:
            dt.date.fromisoformat(publication_date)
        except ValueError as exc:
            raise ValueError("Important Announcements row has an invalid date") from exc
        identity = (detail_url, title, publication_date)
        if notice_id in seen:
            qualifier = "conflicting" if seen[notice_id] != identity else "duplicate"
            raise ValueError(f"Important Announcements listing has {qualifier} ID {notice_id}")
        seen[notice_id] = identity
        records.append(
            {
                "notice_id": notice_id,
                "detail_url": detail_url,
                "title": title,
                "publication_date": publication_date,
                "source_index": source_index,
            }
        )
    if not records:
        raise ValueError("Important Announcements listing has no rows")

    next_page_url = None
    if page_number < total_pages:
        next_page_url = IMPORTANT_LISTING_URL_PREFIX.replace("PAGE", str(page_number + 1))
    return {
        "records": records,
        "page_number": page_number,
        "total_pages": total_pages,
        "next_page_url": next_page_url,
    }


def _normalise_lines(parts: list[str]) -> str:
    """Create stable rendered text while retaining paragraph and list boundaries."""
    text = "".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.replace("\xa0", " ").split()) for line in text.split("\n")]
    output: list[str] = []
    for line in lines:
        if line:
            output.append(line)
        elif output and output[-1] != "":
            output.append("")
    while output and output[-1] == "":
        output.pop()
    return "\n".join(output)


def _decode_html(data: bytes) -> str:
    head = data[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", head, re.IGNORECASE)
    candidates = [match.group(1)] if match else []
    candidates.extend(["utf-8-sig", "big5", "cp950"])
    attempted: set[str] = set()
    for encoding in candidates:
        key = encoding.lower()
        if key in attempted:
            continue
        attempted.add(key)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise ValueError("announcement response uses an unsupported or invalid encoding")


def _attributes(values: list[tuple[str, str | None]]) -> dict[str, str]:
    return {name.lower(): value or "" for name, value in values}


def _has_class(attributes: dict[str, str], expected: str) -> bool:
    return expected in attributes.get("class", "").split()


class _DetailParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.body_parts: list[str] = []
        self.publication_parts: list[str] = []
        self.attachments: list[dict[str, object]] = []
        self.all_parts: list[str] = []
        self._title_depth = 0
        self._content_depth = 0
        self._body_depth = 0
        self._publication_depth = 0
        self._link: dict[str, object] | None = None

    @staticmethod
    def _separator(parts: list[str]) -> None:
        if parts and not parts[-1].endswith("\n"):
            parts.append("\n")

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attributes = _attributes(attrs)
        if tag == "h2" and _has_class(attributes, "hdline"):
            self._title_depth = 1
        elif self._title_depth and tag not in VOID_TAGS:
            self._title_depth += 1
        if tag == "div" and _has_class(attributes, "mcont"):
            self._content_depth = 1
        elif self._content_depth and tag not in VOID_TAGS:
            self._content_depth += 1
        if (
            tag == "div"
            and _has_class(attributes, "meditor")
            and self._content_depth
        ):
            self._body_depth = 1
        elif self._body_depth and tag not in VOID_TAGS:
            self._body_depth += 1

        marker = " ".join(
            [attributes.get("class", ""), attributes.get("id", "")]
        ).lower()
        publication_marker = any(
            token in marker for token in ("publish", "posted", "date", "time")
        )
        if publication_marker:
            self._publication_depth = max(self._publication_depth, 1)
        elif self._publication_depth and tag not in VOID_TAGS:
            self._publication_depth += 1
        for name in ("data-date", "datetime", "content"):
            value = attributes.get(name, "")
            if value and any(char.isdigit() for char in value):
                self.publication_parts.append(value)
                self.publication_parts.append("\n")

        if tag == "a" and attributes.get("href"):
            self._link = {
                "href": attributes["href"],
                "label_parts": [],
                "title": attributes.get("title", ""),
            }
        if tag in BLOCK_TAGS:
            if self._title_depth:
                self._separator(self.title_parts)
            if self._body_depth:
                self._separator(self.body_parts)
            self._separator(self.all_parts)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        self.all_parts.append(data)
        if self._title_depth:
            self.title_parts.append(data)
        if self._body_depth:
            self.body_parts.append(data)
        if self._publication_depth:
            self.publication_parts.append(data)
        if self._link is not None:
            label_parts = self._link["label_parts"]
            if isinstance(label_parts, list):
                label_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._link is not None:
            href = str(self._link["href"])
            if _is_downloadfile_href(href):
                label_parts = self._link["label_parts"]
                label = _normalise_lines(label_parts) if isinstance(label_parts, list) else ""
                self.attachments.append(
                    {
                        "source_order": len(self.attachments) + 1,
                        "href": href,
                        "label": label,
                        "title": str(self._link.get("title", "")),
                    }
                )
            self._link = None
        if tag in BLOCK_TAGS:
            if self._title_depth:
                self._separator(self.title_parts)
            if self._body_depth:
                self._separator(self.body_parts)
            self._separator(self.all_parts)
        if tag not in VOID_TAGS:
            if self._title_depth:
                self._title_depth -= 1
            if self._body_depth:
                self._body_depth -= 1
            if self._content_depth:
                self._content_depth -= 1
            if self._publication_depth:
                self._publication_depth -= 1


def _is_downloadfile_href(href: str) -> bool:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    return any(
        value.lower() == DOWNLOAD_ACTION
        for key, values in query.items()
        if key.lower() == "action"
        for value in values
    )


def _iso_date(text: str, *, default_year: int | None = None) -> str | None:
    match = DATE_TOKEN_RE.search(text)
    if not match:
        return None
    year_text = match.group("year")
    if year_text:
        year = int(year_text)
        if year < 1911:
            year += 1911
    elif default_year is not None:
        year = default_year
    else:
        return None
    try:
        value = dt.date(year, int(match.group("month")), int(match.group("day")))
    except ValueError:
        return None
    return value.isoformat()


def parse_notice_detail(html: bytes, source_url: str) -> dict[str, object]:
    """Parse one official detail page without interpreting its policy meaning."""
    parsed_url = urllib.parse.urlparse(source_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != urllib.parse.urlparse(OFFICIAL_ORIGIN).hostname
        or parsed_url.port not in (None, 443)
    ):
        raise ValueError("notice detail URL is outside the official HTTPS origin")
    parser = _DetailParser()
    parser.feed(_decode_html(html))
    parser.close()
    title = _normalise_lines(parser.title_parts)
    body = _normalise_lines(parser.body_parts)
    if not title:
        raise ValueError("announcement detail has no h2.hdline title")
    if not body:
        raise ValueError("announcement detail has no div.mcont body")

    labelled = PUBLICATION_LABEL_RE.search(_normalise_lines(parser.all_parts))
    publication_date = _iso_date(labelled.group("date")) if labelled else None

    attachments: list[dict[str, object]] = []
    for attachment in parser.attachments:
        url = urllib.parse.urljoin(source_url, str(attachment["href"]))
        attachments.append({**attachment, "url": url})
    return {
        "title": title,
        "publication_date": publication_date,
        "body_text": body,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "attachments": attachments,
    }


def _clean_institution_title(value: str) -> str:
    text = "".join(value.replace("\u3000", " ").split())
    match = INSTITUTION_END_RE.search(text)
    if not match:
        return ""
    text = text[: match.end()]
    text = re.sub(r"^\d+[.)、]?", "", text)
    text = re.sub(r"^(?:上午|下午)?\d{1,2}[：:]\d{2}(?:[-~～至]\d{1,2}[：:]\d{2})?", "", text)
    district = DISTRICT_RE.search(text)
    if district and not text.startswith("新北市"):
        text = text[district.end() :]
    for prefix in ("公立", "私立", "非營利"):
        doubled = prefix + prefix
        while text.startswith(doubled):
            text = text[len(prefix) :]
    if text.startswith("私立"):
        text = "新北市" + text
    if len(text) < 5 or any(
        marker in text for marker in ("日程表", "受評園", "公私立幼兒園", "追蹤評鑑")
    ):
        return ""
    return text


def _line_date(text: str, visit_year: int) -> str | None:
    value = _iso_date(text, default_year=visit_year)
    if value and int(value[:4]) != visit_year:
        raise ValueError(f"PDF visit year differs from fixed pilot year: {value}")
    return value


def parse_followup_schedule_pdf(
    data: bytes,
    *,
    visit_year: int,
    page_cap: int = DEFAULT_PDF_PAGE_CAP,
) -> list[dict[str, object]]:
    """Parse listed institutions from a追蹤評鑑實地訪視日程表 PDF.

    The source PDFs expose each table row as a stable text sequence beginning
    with a numeric source index.  Names can span several lines, so parsing uses
    the next numeric index as the row boundary and the visit-date token as the
    end of the name cell.  Parenthesised rename notes remain in row evidence but
    are not emitted as institutions.  No completion status is inferred.
    """
    if not data.startswith(b"%PDF"):
        raise ValueError("announcement attachment is not a PDF payload")
    document = fitz.open(stream=data, filetype="pdf")
    try:
        if document.page_count <= 0 or document.page_count > page_cap:
            raise ValueError(
                f"announcement PDF page count outside 1..{page_cap}: {document.page_count}"
            )
        joined = "".join(document[index].get_text() for index in range(document.page_count))
        source_marker = joined.replace(" ", "")
        if (
            "追蹤評鑑" not in source_marker
            or "實地訪視" not in source_marker
            or "日程表" not in source_marker
        ):
            raise ValueError("PDF is not a追蹤評鑑實地訪視日程表")

        records: list[dict[str, object]] = []
        source_indices: set[int] = set()
        establishment_tokens = {
            "私人",
            "財團法人",
            "附設",
            "團體附設",
            "國小附設",
            "(屬)",
            "（屬）",
            "直轄市立",
        }
        visit_date_re = re.compile(r"^\s*\d{1,2}\s*月\s*\d{1,2}\s*日\s*$")

        for page_index in range(document.page_count):
            lines = [line.strip() for line in document[page_index].get_text().splitlines()]
            starts = [index for index, line in enumerate(lines) if re.fullmatch(r"\d+", line)]
            for position, start in enumerate(starts):
                source_index = int(lines[start])
                end = starts[position + 1] if position + 1 < len(starts) else len(lines)
                segment = [line for line in lines[start:end] if line]
                if len(segment) < 5:
                    continue
                district_position = next(
                    (
                        index
                        for index, line in enumerate(segment[1:], start=1)
                        if DISTRICT_RE.fullmatch(line)
                    ),
                    None,
                )
                date_position = next(
                    (
                        index
                        for index, line in enumerate(segment[1:], start=1)
                        if visit_date_re.fullmatch(line)
                    ),
                    None,
                )
                if (
                    district_position is None
                    or date_position is None
                    or date_position <= district_position
                ):
                    continue
                visit_date = _line_date(segment[date_position], visit_year)
                if visit_date is None:
                    continue

                name_parts: list[str] = []
                for line in segment[district_position + 1 : date_position]:
                    compact = "".join(line.split())
                    if compact in establishment_tokens or compact.startswith(("(原", "（原")):
                        continue
                    compact = re.sub(r"^團體附設(?=新北市)", "", compact)
                    name_parts.append(compact)
                source_title = _clean_institution_title("".join(name_parts))
                if not source_title:
                    continue
                if source_index in source_indices:
                    raise ValueError(f"duplicate PDF schedule source index: {source_index}")
                source_indices.add(source_index)

                source_town = segment[district_position]
                evidence = "\n".join(segment)
                if "非營利" in source_title or "非營利" in evidence:
                    establishment_type = "非營利"
                elif "私立" in source_title or "私人" in evidence:
                    establishment_type = "私立"
                elif source_title.startswith("新北市立") or any(
                    token in evidence for token in ("公立", "國小附設", "直轄市立")
                ):
                    establishment_type = "公立"
                else:
                    establishment_type = ""
                records.append(
                    {
                        "institution_source_title": source_title,
                        "institution_source_town": source_town,
                        "institution_source_type": establishment_type,
                        "institution_row_index": source_index,
                        "visit_date": visit_date,
                        "pdf_page": page_index + 1,
                        "pdf_row": str(source_index),
                        "source_row_text": evidence,
                    }
                )

        return sorted(records, key=lambda row: int(row["institution_row_index"]))
    finally:
        document.close()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Expose redirect responses so the client can budget each hop itself."""

    def redirect_request(
        self,
        _req: Any,
        _fp: Any,
        _code: int,
        _msg: str,
        _headers: Any,
        _newurl: str,
    ) -> None:
        return None


@dataclasses.dataclass(frozen=True)
class ResponseArtifact:
    sequence: int
    source_url: str
    final_url: str
    status: int
    content_type: str | None
    charset: str | None
    retrieved_at_utc: str
    body: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class AnnouncementClient:
    """Verified-TLS, same-host, in-memory client with aggregate budgets."""

    def __init__(
        self,
        *,
        timeout: int = 60,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
        max_requests: int = DEFAULT_MAX_REQUESTS,
        deadline_seconds: int = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        if min(
            timeout,
            max_response_bytes,
            max_total_bytes,
            max_requests,
            deadline_seconds,
        ) <= 0:
            raise ValueError("announcement client limits must be positive")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.max_total_bytes = max_total_bytes
        self.max_requests = max_requests
        self.deadline_seconds = deadline_seconds
        self._expected = urllib.parse.urlparse(OFFICIAL_ORIGIN)
        self._started = time.monotonic()
        self._sequence = 0
        self._total_bytes = 0
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies),
            _NoRedirectHandler(),
        )

    @property
    def request_count(self) -> int:
        return self._sequence

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self._expected.hostname
            or parsed.port not in (None, 443)
        ):
            raise ValueError(f"refused announcement request origin: {url!r}")

    def _remaining_time(self) -> float:
        return self.deadline_seconds - (time.monotonic() - self._started)

    def _preflight(self, url: str) -> tuple[int, float]:
        self._validate_url(url)
        remaining_time = self._remaining_time()
        remaining_bytes = self.max_total_bytes - self._total_bytes
        if self._sequence >= self.max_requests:
            raise ValueError("announcement request budget exhausted")
        if remaining_bytes <= 0:
            raise ValueError("announcement aggregate byte budget exhausted")
        if remaining_time <= 0:
            raise TimeoutError("announcement acquisition deadline exhausted")
        return min(self.max_response_bytes, remaining_bytes), remaining_time

    @staticmethod
    def _set_socket_timeout(response: Any, timeout: float) -> None:
        stream = getattr(getattr(response, "fp", None), "raw", None)
        socket = getattr(stream, "_sock", None)
        if socket is not None:
            socket.settimeout(timeout)

    def _read_bounded(self, response: Any, read_limit: int) -> bytes:
        body = bytearray()
        reader = getattr(response, "read1", response.read)
        while len(body) <= read_limit:
            remaining_time = self._remaining_time()
            if remaining_time <= 0:
                raise TimeoutError("announcement acquisition deadline exceeded")
            self._set_socket_timeout(response, min(float(self.timeout), remaining_time))
            chunk_size = min(65_536, read_limit + 1 - len(body))
            chunk = reader(chunk_size)
            if not chunk:
                break
            body.extend(chunk)
            if self._remaining_time() <= 0:
                raise TimeoutError("announcement acquisition deadline exceeded")
        return bytes(body)

    def _enforce_size_limit(self, body: bytes, read_limit: int) -> None:
        if len(body) <= read_limit:
            return
        if read_limit < self.max_response_bytes:
            raise ValueError("announcement aggregate byte budget exceeded")
        raise ValueError(f"announcement response exceeds {self.max_response_bytes} bytes")

    def get(self, url: str) -> ResponseArtifact:
        """GET one allowlisted response while budgeting every redirect hop."""
        source_url = url
        current_url = url
        redirects = 0
        redirect_codes = {301, 302, 303, 307, 308}
        while True:
            read_limit, remaining_time = self._preflight(current_url)
            self._sequence += 1
            request = urllib.request.Request(
                current_url,
                headers={
                    "User-Agent": "smart-watchdog-education-announcement-pilot/1",
                    "Accept": "text/html,application/xhtml+xml,application/pdf",
                },
                method="GET",
            )
            request_timeout = min(float(self.timeout), remaining_time)
            try:
                response = self._opener.open(request, timeout=request_timeout)
            except urllib.error.HTTPError as exc:
                if exc.code not in redirect_codes:
                    raise
                response = exc

            with response:
                status = int(getattr(response, "status", response.code))
                location = response.headers.get("Location")
                next_url: str | None = None
                if status in redirect_codes:
                    if not location:
                        raise ValueError("announcement redirect has no Location header")
                    next_url = urllib.parse.urljoin(current_url, location)
                    self._validate_url(next_url)
                body = self._read_bounded(response, read_limit)
                self._enforce_size_limit(body, read_limit)
                content_type = response.headers.get_content_type()
                charset = response.headers.get_content_charset()
            self._total_bytes += len(body)

            if next_url is not None:
                redirects += 1
                if redirects > 5:
                    raise ValueError("announcement redirect cap exceeded")
                current_url = next_url
                continue
            self._validate_url(current_url)
            retrieved = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
            return ResponseArtifact(
                sequence=self._sequence,
                source_url=source_url,
                final_url=current_url,
                status=status,
                content_type=content_type,
                charset=charset,
                retrieved_at_utc=retrieved.isoformat().replace("+00:00", "Z"),
                body=body,
            )
