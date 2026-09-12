"""Stateful ASP.NET WebForms client and source-faithful evaluation parser.

The official evaluation search page generates control names at runtime.  This
module therefore inventories the actual HTML, carries every successful form
control forward, and refreshes hidden WebForms state after every response.
Network acquisition remains explicit in ``scripts/download_evaluation_pilot.py``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import http.cookiejar
import re
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any

DEFAULT_URL = "https://ap.ece.moe.edu.tw/webecems/evaSearch.aspx"
DEFAULT_MAX_BYTES = 5_000_000
NO_RESULT_MARKERS = (
    "查無資料",
    "查無符合",
    "查無相關",
    "沒有符合",
    "無符合條件",
)


def _normalise_text(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _decode_html(data: bytes) -> str:
    """Decode an official page without silently replacing invalid bytes."""
    head = data[:4096].decode("ascii", errors="ignore")
    match = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", head, re.IGNORECASE)
    candidates = [match.group(1)] if match else []
    candidates.extend(["utf-8-sig", "big5", "cp950"])
    attempted: set[str] = set()
    for encoding in candidates:
        encoding = encoding.lower()
        if encoding in attempted:
            continue
        attempted.add(encoding)
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise ValueError("evaluation response uses an unsupported or invalid encoding")


def _attrs(values: list[tuple[str, str | None]]) -> dict[str, str]:
    return {name.lower(): value or "" for name, value in values}


@dataclasses.dataclass
class _Table:
    attrs: dict[str, str]
    rows: list[list[dict[str, Any]]] = dataclasses.field(default_factory=list)
    row: list[dict[str, Any]] | None = None
    cell: dict[str, Any] | None = None


class _DocumentParser(HTMLParser):
    """Small purpose-built parser for forms and tabular WebForms output."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: list[dict[str, Any]] = []
        self.form_index: int | None = None
        self.labels: dict[str, str] = {}
        self._label_for: str | None = None
        self._label_text: list[str] = []
        self._label_controls: list[dict[str, Any]] = []
        self._select: dict[str, Any] | None = None
        self._option: dict[str, Any] | None = None
        self._button: dict[str, Any] | None = None
        self._textarea: dict[str, Any] | None = None
        self.tables: list[_Table] = []
        self._table_stack: list[_Table] = []
        self.text: list[str] = []
        self.element_text: dict[str, str] = {}
        self.element_attrs: dict[str, dict[str, str]] = {}
        self._id_captures: list[dict[str, Any]] = []

    def _form(self) -> dict[str, Any] | None:
        if self.form_index is None:
            return None
        return self.forms[self.form_index]

    def _add_control(self, control: dict[str, Any]) -> None:
        form = self._form()
        if form is not None:
            form["controls"].append(control)
            if self._label_for is not None:
                self._label_controls.append(control)

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attributes = _attrs(attrs)
        element_id = attributes.get("id")
        if element_id and tag not in {
            "area",
            "base",
            "br",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "source",
            "track",
            "wbr",
        }:
            self._id_captures.append(
                {"tag": tag, "id": element_id, "text_parts": []}
            )
            self.element_attrs[element_id] = attributes
        if tag == "form":
            self.forms.append(
                {
                    "action": attributes.get("action", ""),
                    "method": attributes.get("method", "get").lower(),
                    "id": attributes.get("id", ""),
                    "name": attributes.get("name", ""),
                    "controls": [],
                }
            )
            self.form_index = len(self.forms) - 1
        elif tag == "label":
            self._label_for = attributes.get("for", "")
            self._label_text = []
            self._label_controls = []
        elif tag == "input":
            control = {
                "tag": "input",
                "type": attributes.get("type", "text").lower(),
                "name": attributes.get("name", ""),
                "id": attributes.get("id", ""),
                "value": attributes.get("value", ""),
                "disabled": "disabled" in attributes,
                "checked": "checked" in attributes,
                "onclick": attributes.get("onclick", ""),
                "label": "",
            }
            self._add_control(control)
        elif tag == "select":
            self._select = {
                "tag": "select",
                "type": "select",
                "name": attributes.get("name", ""),
                "id": attributes.get("id", ""),
                "disabled": "disabled" in attributes,
                "multiple": "multiple" in attributes,
                "onchange": attributes.get("onchange", ""),
                "options": [],
                "label": "",
            }
            self._add_control(self._select)
        elif tag == "option" and self._select is not None:
            self._option = {
                "value": attributes.get("value", ""),
                "selected": "selected" in attributes,
                "disabled": "disabled" in attributes,
                "text_parts": [],
            }
        elif tag == "button":
            self._button = {
                "tag": "button",
                "type": attributes.get("type", "submit").lower(),
                "name": attributes.get("name", ""),
                "id": attributes.get("id", ""),
                "value": attributes.get("value", ""),
                "disabled": "disabled" in attributes,
                "onclick": attributes.get("onclick", ""),
                "text_parts": [],
                "label": "",
            }
            self._add_control(self._button)
        elif tag == "textarea":
            self._textarea = {
                "tag": "textarea",
                "type": "textarea",
                "name": attributes.get("name", ""),
                "id": attributes.get("id", ""),
                "disabled": "disabled" in attributes,
                "value_parts": [],
                "label": "",
            }
            self._add_control(self._textarea)

        if tag == "table":
            table = _Table(attributes)
            self.tables.append(table)
            self._table_stack.append(table)
        elif tag == "tr" and self._table_stack:
            self._table_stack[-1].row = []
        elif tag in {"th", "td"} and self._table_stack:
            table = self._table_stack[-1]
            if table.row is not None:
                table.cell = {
                    "tag": tag,
                    "attrs": attributes,
                    "text_parts": [],
                    "links": [],
                    "onclick": [],
                }
        elif tag == "a" and self._table_stack:
            table = self._table_stack[-1]
            if table.cell is not None:
                if attributes.get("href"):
                    table.cell["links"].append(attributes["href"])
                if attributes.get("onclick"):
                    table.cell["onclick"].append(attributes["onclick"])

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        for capture in self._id_captures:
            capture["text_parts"].append(data)
        if self._label_for is not None:
            self._label_text.append(data)
        if self._option is not None:
            self._option["text_parts"].append(data)
        if self._button is not None:
            self._button["text_parts"].append(data)
        if self._textarea is not None:
            self._textarea["value_parts"].append(data)
        if self._table_stack and self._table_stack[-1].cell is not None:
            self._table_stack[-1].cell["text_parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._id_captures) - 1, -1, -1):
            capture = self._id_captures[index]
            if capture["tag"] == tag:
                self._id_captures.pop(index)
                self.element_text[capture["id"]] = _normalise_text(
                    "".join(capture["text_parts"])
                )
                break
        if tag == "label" and self._label_for is not None:
            label = _normalise_text("".join(self._label_text))
            if self._label_for:
                self.labels[self._label_for] = label
            for control in self._label_controls:
                control["label"] = label
            self._label_for = None
            self._label_text = []
            self._label_controls = []
        elif tag == "option" and self._select is not None and self._option is not None:
            option = dict(self._option)
            option["text"] = _normalise_text("".join(option.pop("text_parts")))
            self._select["options"].append(option)
            self._option = None
        elif tag == "select":
            self._select = None
        elif tag == "button" and self._button is not None:
            self._button["text"] = _normalise_text(
                "".join(self._button.pop("text_parts"))
            )
            self._button = None
        elif tag == "textarea" and self._textarea is not None:
            self._textarea["value"] = "".join(
                self._textarea.pop("value_parts")
            )
            self._textarea = None
        elif tag == "form":
            self.form_index = None

        if tag in {"th", "td"} and self._table_stack:
            table = self._table_stack[-1]
            if table.cell is not None and table.row is not None:
                cell = dict(table.cell)
                cell["text"] = _normalise_text("".join(cell.pop("text_parts")))
                table.row.append(cell)
                table.cell = None
        elif tag == "tr" and self._table_stack:
            table = self._table_stack[-1]
            if table.row:
                table.rows.append(table.row)
            table.row = None
            table.cell = None
        elif tag == "table" and self._table_stack:
            self._table_stack.pop()

    def close(self) -> None:
        super().close()
        for form in self.forms:
            for control in form["controls"]:
                if not control.get("label"):
                    control["label"] = self.labels.get(control.get("id", ""), "")


def _parse_document(html: bytes) -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(_decode_html(html))
    parser.close()
    return parser


def _public_control(control: dict[str, Any]) -> dict[str, Any]:
    public = dict(control)
    if public.get("type") == "hidden":
        value = str(public.pop("value", ""))
        public["value_length"] = len(value)
    return public


def inventory_controls(html: bytes) -> dict[str, object]:
    """Describe actual WebForms controls without assuming generated names.

    Hidden values are intentionally redacted. Their names, lengths, and hashes
    are sufficient for reproducibility while avoiding duplicate viewstate in a
    manifest beside the pinned raw HTML.
    """
    document = _parse_document(html)
    forms: list[dict[str, Any]] = []
    for form in document.forms:
        controls = [_public_control(control) for control in form["controls"]]
        forms.append(
            {
                **{
                    key: form[key]
                    for key in ("action", "method", "id", "name")
                },
                "controls": controls,
            }
        )

    controls = [control for form in forms for control in form["controls"]]
    return {
        "forms": forms,
        "hidden_names": [
            control["name"]
            for control in controls
            if control.get("type") == "hidden" and control.get("name")
        ],
        "selects": [control for control in controls if control.get("tag") == "select"],
        "text_inputs": [
            control
            for control in controls
            if control.get("tag") == "input"
            and control.get("type") in {"text", "search", "email", "number"}
        ],
        "submit_controls": [
            control
            for control in controls
            if control.get("type") in {"submit", "image"}
        ],
        "table_summaries": [
            {
                "index": index,
                "id": table.attrs.get("id", ""),
                "class": table.attrs.get("class", ""),
                "row_count": len(table.rows),
                "max_column_count": max((len(row) for row in table.rows), default=0),
                "first_row": [cell["text"] for cell in table.rows[0]] if table.rows else [],
            }
            for index, table in enumerate(document.tables)
        ],
    }


def form_payload(html: bytes, form_index: int = 0) -> list[tuple[str, str]]:
    """Return ordered successful controls, excluding submit controls.

    This carries all hidden fields including split viewstate fields and keeps
    duplicate control names in document order, as required by HTML forms.
    """
    document = _parse_document(html)
    try:
        form = document.forms[form_index]
    except IndexError as exc:
        raise ValueError(f"evaluation page has no form at index {form_index}") from exc

    payload: list[tuple[str, str]] = []
    for control in form["controls"]:
        name = control.get("name", "")
        if not name or control.get("disabled"):
            continue
        tag = control["tag"]
        control_type = control.get("type", "")
        if control_type in {"submit", "button", "reset", "image", "file"}:
            continue
        if tag == "input":
            if control_type in {"checkbox", "radio"} and not control.get("checked"):
                continue
            payload.append((name, str(control.get("value", ""))))
        elif tag == "textarea":
            payload.append((name, str(control.get("value", ""))))
        elif tag == "select":
            options = [option for option in control["options"] if not option["disabled"]]
            selected = [option for option in options if option["selected"]]
            if not selected and options and not control.get("multiple"):
                selected = options[:1]
            payload.extend((name, str(option["value"])) for option in selected)
    return payload


def form_action(html: bytes, base_url: str, form_index: int = 0) -> str:
    """Resolve the selected form's action against the response URL."""
    document = _parse_document(html)
    try:
        action = document.forms[form_index]["action"]
    except IndexError as exc:
        raise ValueError(f"evaluation page has no form at index {form_index}") from exc
    return urllib.parse.urljoin(base_url, action or base_url)


def _deduplicate_headers(headers: list[str]) -> list[str]:
    counts: dict[str, int] = {}
    out: list[str] = []
    for index, header in enumerate(headers, start=1):
        base = header or f"column_{index}"
        counts[base] = counts.get(base, 0) + 1
        out.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return out


def parse_evaluation_results(html: bytes) -> list[dict[str, object]]:
    """Parse every source-faithful evaluation row from one pinned response.

    The outer GridView is card-shaped rather than tabular, while each institution
    owns a nested evaluation table. Official labels become keys without semantic
    reinterpretation, and the institution fields are copied onto every result.
    """
    document = _parse_document(html)
    page_text = _normalise_text(" ".join(document.text))
    if any(marker in page_text for marker in NO_RESULT_MARKERS):
        return []

    institution_fields = {
        "園名": "lblSchName",
        "縣市": "lblCity",
        "鄉鎮": "lblArea",
        "設立別": "lblPub",
        "地址": "hlAddr",
        "電話": "lblTel",
        "園所網址": "hlUrl",
        "核定人數": "lblGenStd",
        "兼辦國小課後": "lblChildSvc",
    }
    rows: list[dict[str, object]] = []
    recognized_tables = 0
    for table_index, table in enumerate(document.tables):
        header_index = next(
            (
                index
                for index, row in enumerate(table.rows)
                if "評鑑學年度" in {cell["text"] for cell in row}
                and "評鑑結果" in {cell["text"] for cell in row}
            ),
            None,
        )
        if header_index is None:
            continue
        recognized_tables += 1
        headers = _deduplicate_headers(
            [cell["text"] for cell in table.rows[header_index]]
        )
        table_id = table.attrs.get("id", "")
        match = re.search(r"_(\d+)$", table_id)
        institution_index = match.group(1) if match else ""
        institution_source = {
            label: document.element_text.get(
                f"GridView1_{element_name}_{institution_index}", ""
            )
            for label, element_name in institution_fields.items()
        }

        for source_row_index, cells in enumerate(
            table.rows[header_index + 1 :], start=header_index + 1
        ):
            values = [cell["text"] for cell in cells]
            if not any(values) or len(cells) != len(headers):
                continue
            source = {**institution_source, **dict(zip(headers, values))}
            rows.append(
                {
                    "source_table_index": table_index,
                    "source_table_id": table_id,
                    "source_row_index": source_row_index,
                    "source": source,
                    "source_links": [
                        link for cell in cells for link in cell["links"]
                    ],
                    "source_onclick": [
                        script for cell in cells for script in cell["onclick"]
                    ],
                }
            )

    if recognized_tables:
        return rows
    raise ValueError(
        "evaluation response has neither result tables nor an explicit no-result marker"
    )


POSTBACK_RE = re.compile(
    r"__doPostBack\(['\"](?P<target>[^'\"]*)['\"],"
    r"['\"](?P<argument>[^'\"]*)['\"]\)"
)


def postback_controls(html: bytes) -> list[dict[str, str]]:
    """Return server-generated postback targets discovered in the response."""
    document = _parse_document(html)
    controls: list[dict[str, str]] = []
    for element_id, attributes in document.element_attrs.items():
        match = POSTBACK_RE.search(attributes.get("href", ""))
        if match:
            controls.append(
                {
                    "id": element_id,
                    "text": document.element_text.get(element_id, ""),
                    "event_target": match.group("target"),
                    "event_argument": match.group("argument"),
                }
            )
    return controls


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
            raise ValueError(f"refused redirected evaluation origin: {newurl!r}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclasses.dataclass(frozen=True)
class ResponseArtifact:
    """One in-memory response in a stateful WebForms request chain."""

    sequence: int
    method: str
    source_url: str
    final_url: str
    status: int
    content_type: str | None
    charset: str | None
    retrieved_at_utc: str
    request_body_sha256: str | None
    predecessor_sequence: int | None
    body: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()


class EvaluationClient:
    """Verified-TLS, same-origin, in-memory-cookie evaSearch client."""

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        timeout: int = 60,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443):
            raise ValueError("evaluation URL must be HTTPS on the default TLS port")
        self.url = url
        self.timeout = timeout
        self.max_bytes = max_bytes
        self._expected = parsed
        self._cookies = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookies),
            _SameOriginRedirectHandler(url),
        )
        self._sequence = 0

    def _validate_url(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self._expected.hostname
            or parsed.port not in (None, 443)
        ):
            raise ValueError(f"refused evaluation request origin: {url!r}")

    def _request(
        self,
        method: str,
        url: str,
        data: bytes | None,
        predecessor_sequence: int | None,
    ) -> ResponseArtifact:
        self._validate_url(url)
        headers = {
            "User-Agent": "smart-watchdog-evaluation-pilot/1",
            "Accept": "text/html,application/xhtml+xml",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self._opener.open(request, timeout=self.timeout) as response:
            final_url = response.geturl()
            self._validate_url(final_url)
            body = response.read(self.max_bytes + 1)
            if len(body) > self.max_bytes:
                raise ValueError(f"evaluation response exceeds {self.max_bytes} bytes")
            content_type = response.headers.get_content_type()
            if content_type not in {"text/html", "application/xhtml+xml"}:
                raise ValueError(f"unexpected evaluation content type: {content_type!r}")
            charset = response.headers.get_content_charset()
            status = response.status

        self._sequence += 1
        retrieved = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        return ResponseArtifact(
            sequence=self._sequence,
            method=method,
            source_url=url,
            final_url=final_url,
            status=status,
            content_type=content_type,
            charset=charset,
            retrieved_at_utc=retrieved.isoformat().replace("+00:00", "Z"),
            request_body_sha256=hashlib.sha256(data).hexdigest() if data is not None else None,
            predecessor_sequence=predecessor_sequence,
            body=body,
        )

    def get(self) -> ResponseArtifact:
        """Start a fresh WebForms response chain in this cookie session."""
        return self._request("GET", self.url, None, None)

    def post(
        self,
        previous: ResponseArtifact,
        payload: list[tuple[str, str]],
    ) -> ResponseArtifact:
        """POST ordered controls using the previous response as state provenance."""
        action = form_action(previous.body, previous.final_url)
        encoded = urllib.parse.urlencode(payload, doseq=True).encode("ascii")
        return self._request("POST", action, encoded, previous.sequence)
