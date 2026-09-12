"""Tests for the pure parsing helpers.

Every expected value here was read off an actual page of the competition dataset
or an actual record from the public registry -- these are regression tests against
real document quirks, not invented cases.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from smart_watchdog.ingest.pdf_utils import (
    Row,
    Word,
    group_rows,
    is_number,
    parse_number,
)
from smart_watchdog.scrape import registry


class TestParseNumber:
    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("33,417,982", 33_417_982),   # 板橋幼兒園 113 基金來源決算數
            ("(1,988,771)", -1_988_771),  # 安溪 113 本期餘絀 -- accounting parentheses
            ("-768,500", -768_500),       # 板橋 學雜費收入 比較增減
            ("87.53", 87.53),             # a percentage column
            ("380", 380),
        ],
    )
    def test_parses_real_cells(self, token: str, expected: float) -> None:
        assert parse_number(token) == expected

    @pytest.mark.parametrize("token", ["基金來源", "462公庫撥款收入", "", "-", "$"])
    def test_rejects_non_numbers(self, token: str) -> None:
        assert not is_number(token)

    @pytest.mark.parametrize("token", ["1,650", "0.00", "(94,176)", "233.50"])
    def test_accepts_numbers(self, token: str) -> None:
        assert is_number(token)


class TestGroupRows:
    def test_groups_by_vertical_midpoint(self) -> None:
        """Words on the same visual line land in one row, sorted left-to-right."""
        words = [
            Word(300, 100, 340, 110, "29,493,000"),
            Word(50, 100, 90, 110, "基金來源"),   # same line, further left
            Word(50, 130, 90, 140, "政府撥入收入"),  # next line
        ]
        rows = group_rows(words)
        assert len(rows) == 2
        assert rows[0].text == "基金來源 29,493,000"
        assert rows[1].text == "政府撥入收入"

    def test_tolerance_keeps_adjacent_table_rows_apart(self) -> None:
        """13pt row pitch must not collapse; the 決算書 body is ~9pt."""
        words = [
            Word(50, 190, 90, 199, "a"),
            Word(50, 203, 90, 212, "b"),
        ]
        assert len(group_rows(words, tol=3.0)) == 2

    def test_row_splits_label_from_numbers(self) -> None:
        row = Row(
            y=0,
            words=[
                Word(0, 0, 10, 9, "46"),
                Word(12, 0, 60, 9, "政府撥入收入"),
                Word(70, 0, 120, 9, "24,562,000"),
                Word(130, 0, 160, 9, "83.28"),
            ],
        )
        # A leading account code is part of the label, not a value column.
        assert row.label() == "46政府撥入收入"
        assert row.numbers() == [24_562_000, 83.28]


class TestRegistryHelpers:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            (
                "新北市安溪非營利幼兒園(委託社團法人桃園市教保服務人員協會辦理)",
                "社團法人桃園市教保服務人員協會",
            ),
            (   # full-width parens -- both forms appear in the registry
                "新北市山北非營利幼兒園（委託財團法人三之三生命教育基金會辦理）",
                "財團法人三之三生命教育基金會",
            ),
            (   # "申請辦理" instead of "委託...辦理"
                "新北市新店及人非營利幼兒園(財團法人新北市私立及人高級中學申請辦理)",
                "財團法人新北市私立及人高級中學",
            ),
        ],
    )
    def test_operator_of(self, title: str, expected: str) -> None:
        assert registry.operator_of(title) == expected

    def test_operator_of_returns_none_without_a_delegate(self) -> None:
        assert registry.operator_of("新北市立板橋幼兒園") is None

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("新北市立五股幼兒園德音分班", "新北市立五股幼兒園"),
            ("新北市立金山幼兒園中正中華分班", "新北市立金山幼兒園"),
            ("新北市立板橋幼兒園", "新北市立板橋幼兒園"),  # 本園 is its own parent
        ],
    )
    def test_parent_of_collapses_branches(self, title: str, expected: str) -> None:
        assert registry.parent_of(title) == expected

    def test_law_article_extracts_the_article_number(self) -> None:
        law = "第33條第1項教保服務機構有教保服務人員對幼兒不當對待行為。"
        assert registry.law_article(law) == "33"

    def test_law_article_handles_missing_law(self) -> None:
        assert registry.law_article(None) is None
        assert registry.law_article("") is None

    @pytest.mark.parametrize(
        ("punishment", "expected"),
        [("罰鍰：60,000元", 60_000), ("罰鍰：9,000元", 9_000), ("罰鍰：600,000元", 600_000)],
    )
    def test_fine_amount(self, punishment: str, expected: int) -> None:
        assert registry.fine_amount(punishment) == expected

    def test_fine_amount_none_when_no_monetary_penalty(self) -> None:
        # Not every 裁罰 is a fine -- some are 停業 / 廢止許可.
        assert registry.fine_amount("命其停止招生") is None
