"""Turn a reconstructed 決算書 page into a labelled table.

The tables are visually well aligned but structurally hostile to header parsing:
every CJK character in a header is its own positioned word, so ``本 年 度 預 算 數``
arrives as six tokens and the sub-header is ``金 額 % 金 額 % ...``. Rather than
reassemble that, columns are derived **from the data cells themselves** by
clustering their x-centres. That is self-calibrating, so a year whose layout
shifts by a few points still parses, and a statement with a different number of
columns needs no new code.
"""

from __future__ import annotations

import dataclasses

from .pdf_utils import Row, is_number


@dataclasses.dataclass
class Table:
    """A parsed statement: one row per 項目, values placed into detected columns."""

    column_centres: list[float]
    rows: list[tuple[str, list[float | None]]]

    @property
    def n_columns(self) -> int:
        return len(self.column_centres)

    def find(self, *needles: str) -> tuple[str, list[float | None]] | None:
        """First row whose label contains every needle.

        Labels carry an account code prefix (``46政府撥入收入``) and the 決算書 uses
        full-width spacing inside some labels (``合　　計``), so matching is done on
        substrings of a whitespace-stripped label rather than on equality.
        """
        for label, values in self.rows:
            flat = "".join(label.split())
            if all(n in flat for n in needles):
                return label, values
        return None

    def value(self, *needles: str, column: int) -> float | None:
        hit = self.find(*needles)
        if hit is None or column >= len(hit[1]):
            return None
        return hit[1][column]


def detect_columns(rows: list[Row], min_support: int = 3, gap: float = 14.0) -> list[float]:
    """Cluster the x-centres of value cells into column centres.

    ``gap`` is the minimum horizontal separation between two columns. Amount and
    percentage columns sit ~35pt apart in these statements while digits within one
    cell are ~6pt apart, so 14pt separates columns without splitting a number.
    ``min_support`` drops stray numerals in headings and footnotes, which would
    otherwise invent columns of their own.
    """
    centres = sorted(w.cx for r in rows for w in r.value_words())
    if not centres:
        return []
    clusters: list[list[float]] = [[centres[0]]]
    for c in centres[1:]:
        if c - clusters[-1][-1] <= gap:
            clusters[-1].append(c)
        else:
            clusters.append([c])
    return [sum(c) / len(c) for c in clusters if len(c) >= min_support]


def assign_row(row: Row, centres: list[float], tol: float = 20.0) -> list[float | None]:
    """Place a row's value cells into ``centres``, leaving gaps for empty cells.

    This is the whole point of coordinate-based extraction: an empty budget cell
    emits no word, so positional indexing of the raw value list silently shifts
    every later figure into the wrong column.
    """
    out: list[float | None] = [None] * len(centres)
    for w in row.value_words():
        if not centres:
            break
        idx = min(range(len(centres)), key=lambda i: abs(centres[i] - w.cx))
        if abs(centres[idx] - w.cx) <= tol and out[idx] is None:
            out[idx] = float(row.__class__(y=row.y, words=[w]).numbers()[0])
    return out


def parse_table(rows: list[Row], min_support: int = 3) -> Table:
    """Parse the value rows of a statement page into a :class:`Table`."""
    centres = detect_columns(rows, min_support=min_support)
    out: list[tuple[str, list[float | None]]] = []
    for r in rows:
        if not r.value_words():
            continue
        label = r.label()
        if not label:
            continue  # continuation / total-only line with no 項目
        out.append((label, assign_row(r, centres)))
    return Table(column_centres=centres, rows=out)


def is_footnote(row: Row) -> bool:
    """Whether a row is explanatory prose rather than a table line.

    Footnotes in these statements carry figures too (``決算金額為542,940元``), so
    they must be excluded before column detection or they pull the clustering off.
    """
    flat = "".join(row.text.split())
    return flat.startswith(("填表說明", "附註", "說明")) or "係指" in flat


def statement_rows(rows: list[Row]) -> list[Row]:
    """Drop footnote prose, keeping only candidate table rows."""
    out = []
    for r in rows:
        if is_footnote(r):
            break  # footnotes run to the end of the page
        out.append(r)
    return out


def numeric_token_count(row: Row) -> int:
    return sum(1 for w in row.words if is_number(w.text))
