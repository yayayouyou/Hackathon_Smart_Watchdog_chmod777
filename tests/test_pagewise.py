"""Regression tests for the page-level extraction container.

Every case here is a shape that the first version of ``pagewise`` silently got
wrong on real pages of N01 安溪 113. They are pinned as tests because each one
produces *valid JSON that means something false* -- the failure mode structured
output cannot protect against, and the one most likely to be reintroduced by a
well-meaning simplification of the schema.

  p17  two tables on one page whose periods differ by a year. Under a
       page-level ``period_labels`` the 業務費 figures were filed under the
       人事費 table's 112.8.1~113.7.31 heading: a silent one-year shift.
  p22  a note with an embedded 關係人交易 table -- two values, zero column
       headers. 49 rows across the 86-page pilot looked like this.
  p23  the same, plus a header-only row whose ``values`` is empty; that row is
       not ragged, it simply has no figures, and must not be counted as one.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.extract.pagewise import (
    PAGE_SCHEMA,
    has_content,
    identity_ok,
    validate_page,
)

# ── the p17 shape: one page, two tables, two different periods ──────────────
P17 = {
    "footer_code": "N01", "printed_page": 17, "page_kind": "detail_schedule",
    "tables": [
        {"title": "人事費明細表", "unit": "新臺幣元",
         "period_labels": ["112.8.1~113.7.31 預算數", "112.8.1~113.7.31 決算數"],
         "items": [{"label": "加班費", "values": [1003968, 85581]}]},
        {"title": "業務費明細表", "unit": "新臺幣元",
         "period_labels": ["113.8.1~114.7.31 預算數", "113.8.1~114.7.31 決算數"],
         "items": [{"label": "教材教具費", "values": [240000, 198450]}]},
    ],
    "text_sections": [], "issues": [],
}

P22 = {
    "footer_code": "N01", "printed_page": 22, "page_kind": "note",
    "tables": [
        {"title": "關係人交易明細", "period_labels": [],
         "items": [{"label": "社團法人桃園市教保服務人員協會（行政管理費）",
                    "values": [290580, 324572]}]},
    ],
    "text_sections": [{"heading": "四、賸餘款執行概況說明", "text": "本學年度賸餘款……"}],
    "issues": [],
}

P23 = {
    "footer_code": "N01", "printed_page": 23, "page_kind": "note",
    "tables": [
        {"title": None, "period_labels": ["113.8.1~114.7.31", "112.8.1~113.7.31"],
         "items": [
             {"label": "關係人名稱", "values": []},
             {"label": "社團法人桃園市教保服務人員協會", "values": [290580, 324572]},
         ]},
    ],
    "text_sections": [], "issues": [],
}


def test_two_tables_keep_their_own_periods() -> None:
    """The p17 regression: a second table must not inherit the first's columns."""
    problems = validate_page(P17)
    assert problems == []
    a, b = P17["tables"]
    assert a["period_labels"] != b["period_labels"]
    assert a["aligned"] and b["aligned"]
    # The 業務費 figures stay under 113.8.1~114.7.31, not 112.8.1~113.7.31.
    assert "113.8.1" in b["period_labels"][0]


def test_values_without_any_column_headers_is_a_problem() -> None:
    """The p22 regression: numbers with no periods are not aggregatable."""
    problems = validate_page(P22)
    assert len(problems) == 1
    assert "period_labels" in problems[0]
    assert P22["tables"][0]["aligned"] is False


def test_header_only_row_is_not_ragged() -> None:
    """A row with no figures is absent, not misaligned -- p23's 關係人名稱 row."""
    problems = validate_page(P23)
    assert problems == []
    assert P23["tables"][0]["aligned"] is True


def test_ragged_row_is_caught_and_counted() -> None:
    page = {
        "footer_code": "N01", "page_kind": "balance_sheet",
        "tables": [{"title": "資產負債表", "period_labels": ["114/7/31", "113/7/31"],
                    "items": [{"label": "現金", "values": [100]}]}],
        "text_sections": [], "issues": [],
    }
    problems = validate_page(page)
    assert len(problems) == 1
    assert "1 列" in problems[0]
    assert page["tables"][0]["aligned"] is False


def test_unreadable_footer_is_unknown_not_pass() -> None:
    """An absent footer must be distinguishable from a matching one."""
    assert identity_ok({"footer_code": "N01"}, "N01") is True
    assert identity_ok({"footer_code": "n01 "}, "N01") is True
    assert identity_ok({"footer_code": "N16"}, "N01") is False
    assert identity_ok({"footer_code": None}, "N01") is None
    assert identity_ok({}, "N01") is None


def test_has_content_sees_both_tables_and_prose() -> None:
    assert has_content(P17)
    assert has_content(P22)
    assert not has_content(
        {"page_kind": "blank", "tables": [], "text_sections": [], "issues": []}
    )
    # A table object with no rows is not content.
    assert not has_content(
        {"page_kind": "blank", "tables": [{"period_labels": [], "items": []}],
         "text_sections": [{"heading": None, "text": "  "}], "issues": []}
    )


def test_schema_puts_period_labels_on_the_table_not_the_page() -> None:
    """The container-level guarantee: a page cannot declare one set of periods."""
    assert "period_labels" not in PAGE_SCHEMA["properties"]
    table = PAGE_SCHEMA["properties"]["tables"]["items"]
    assert "period_labels" in table["properties"]
    assert "period_labels" in table["required"]
    # Structured output must not let the model invent extra keys anywhere.
    assert PAGE_SCHEMA["additionalProperties"] is False
    assert table["additionalProperties"] is False


def test_schema_is_json_serialisable_for_bedrock() -> None:
    """It is sent over the wire on every call; a non-serialisable schema fails late."""
    json.dumps(PAGE_SCHEMA, ensure_ascii=False)
