"""Low-level PDF helpers shared by the public-school and non-profit ingest paths.

The public-school 決算書 carry a real text layer, but ``page.get_text()`` returns
words in creation order, which interleaves the columns of a financial table into
nonsense. Everything here works from word bounding boxes instead, so a table row
comes back as a row.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
from collections.abc import Iterable, Iterator

import fitz

# A number as it appears in a 決算書 cell: 1,234,567 / -1,234 / 12.45 / (1,988,771)
NUMBER_RE = re.compile(r"^\(?-?[\d,]+(?:\.\d+)?\)?$")


@dataclasses.dataclass(frozen=True)
class Word:
    """One positioned word on a page."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclasses.dataclass
class Row:
    """A visual line of a table: words sorted left-to-right."""

    y: float
    words: list[Word]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    def _split(self) -> tuple[list[str], list[str]]:
        """Split the row into its 項目 label and its value cells.

        Rows in the 決算書 open with an account code that may or may not be glued
        to the name: ``415違規罰款收入`` arrives as one token, while ``46`` and
        ``政府撥入收入`` arrive as two. A naive "stop at the first number" rule
        would read that bare ``46`` as a value column and lose the label, so a
        leading code is absorbed into the label as long as a name follows it.
        """
        words = [w.text for w in self.words]
        first_name = next((i for i, t in enumerate(words) if not is_number(t)), None)
        if first_name is None:
            return [], words  # continuation row: values only, no 項目
        # Absorb a leading account code (short, no thousands separator or decimal).
        start = 0 if all(_is_code(t) for t in words[:first_name]) else first_name
        end = first_name
        while end < len(words) and not is_number(words[end]):
            end += 1
        return words[start:end], words[end:]

    def numbers(self) -> list[float]:
        """The row's value cells, left to right, excluding any leading account code.

        Do not index this positionally to identify a column. An empty cell emits no
        word at all, so a row whose 預算數 is blank yields a shorter list and every
        later value shifts left -- ``41徵收及依法分配收入`` comes back as
        ``[0.0, 380.0, 0.0]`` where a fully populated row has eight values. Map
        values to columns by comparing :attr:`Word.cx` against the header positions.
        """
        _, values = self._split()
        return [parse_number(t) for t in values if is_number(t)]

    def value_words(self) -> list[Word]:
        """The value cells as positioned words, for x-coordinate column matching."""
        labels, _ = self._split()
        return [w for w in self.words[len(labels):] if is_number(w.text)]

    def label(self) -> str:
        """The 項目 name at the start of the row, account code included."""
        label, _ = self._split()
        return "".join(label)


def is_number(token: str) -> bool:
    return bool(NUMBER_RE.match(token.strip()))


def _is_code(token: str) -> bool:
    """Whether a token looks like an account code (``4``, ``41``, ``462``).

    Distinguishes codes from value cells, which carry a thousands separator or a
    decimal point at the magnitudes these statements use.
    """
    t = token.strip()
    return len(t) <= 4 and "," not in t and "." not in t


def parse_number(token: str) -> float:
    """Parse a 決算書 numeric cell. Parentheses mean negative, as in accounting."""
    t = token.strip().replace(",", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()")
    val = float(t)
    return -val if neg else val


def page_words(page: fitz.Page) -> list[Word]:
    """All words on a page as :class:`Word`, in reading-independent order."""
    return [
        Word(x0, y0, x1, y1, text)
        for x0, y0, x1, y1, text, *_ in page.get_text("words")
        if text.strip()
    ]


def group_rows(words: Iterable[Word], tol: float = 3.0) -> list[Row]:
    """Cluster words into visual rows by vertical midpoint.

    ``tol`` is the vertical slack in points within which two words count as being
    on the same line. 3pt is tight enough to keep adjacent table rows apart at the
    ~9pt body size these documents use.
    """
    ws = sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    rows: list[Row] = []
    for w in ws:
        mid = (w.y0 + w.y1) / 2
        if rows and abs(mid - rows[-1].y) <= tol:
            rows[-1].words.append(w)
        else:
            rows.append(Row(y=mid, words=[w]))
    for r in rows:
        r.words.sort(key=lambda w: w.x0)
    return rows


def page_rows(page: fitz.Page, tol: float = 3.0) -> list[Row]:
    return group_rows(page_words(page), tol=tol)


def iter_pages(path: pathlib.Path | str) -> Iterator[fitz.Page]:
    doc = fitz.open(path)
    try:
        yield from doc
    finally:
        doc.close()
