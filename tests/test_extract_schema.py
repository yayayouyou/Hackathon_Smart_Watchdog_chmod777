"""Tests for the extraction contract and its validator.

The validator is what decides whether a model-produced number is trustworthy
without a human reading the page, so its own behaviour has to be pinned down.
Two properties matter most:

* It must not pass a sparse extraction by omission -- an identity whose operands
  are missing is *skipped*, never counted as passed.
* It must treat a blank cell and a zero cell as different things, because the
  documents do.
"""

from __future__ import annotations

import pathlib
import sys
import typing

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from smart_watchdog.extract.schema import (
    EXTRACTION_PROMPT,
    STATEMENT_SCHEMA,
    validate_statement,
)


def balance_sheet(**overrides: list) -> dict:
    """A minimal single-column balance sheet that satisfies every identity."""
    items = {
        "流動資產合計": [100],
        "非流動資產合計": [900],
        "資產總計": [1000],
        "流動負債合計": [200],
        "非流動負債合計": [500],
        "負債總額": [700],
        "累積餘絀": [250],
        "本期餘絀": [50],
        "餘絀總額": [300],
        "負債及餘絀總計": [1000],
    }
    items.update(overrides)
    return {
        "statement_type": "資產負債表",
        "period_labels": ["114/7/31"],
        "items": [{"label": k, "values": v} for k, v in items.items()],
        "issues": [],
    }


class TestBalanceSheetValidation:
    def test_consistent_sheet_passes_every_identity(self) -> None:
        r = validate_statement(balance_sheet())
        assert r.ok
        assert r.failed == []
        assert len(r.passed) == 5  # 4 identities + assets == liabilities+equity
        assert r.score == 1.0

    def test_broken_asset_total_is_caught(self) -> None:
        r = validate_statement(balance_sheet(資產總計=[9999]))
        assert not r.ok
        assert any("資產總計 = 流動資產合計" in f for f in r.failed)

    def test_broken_equity_split_is_caught(self) -> None:
        r = validate_statement(balance_sheet(本期餘絀=[999]))
        assert not r.ok
        assert any("餘絀總額" in f for f in r.failed)

    def test_missing_operands_are_skipped_not_passed(self) -> None:
        """A sparse extraction must not score well by leaving rows out."""
        payload = {
            "statement_type": "資產負債表",
            "period_labels": ["114/7/31"],
            "items": [{"label": "現金及銀行存款", "values": [100]}],
            "issues": [],
        }
        r = validate_statement(payload)
        assert r.passed == []
        assert r.failed == []
        assert r.skipped, "缺運算元的恆等式必須列為 skipped"
        assert r.score is None

    def test_full_width_label_still_matches(self) -> None:
        sheet = balance_sheet()
        for item in sheet["items"]:
            if item["label"] == "負債及餘絀總計":
                item["label"] = "負債及餘絀總　計"
        r = validate_statement(sheet)
        assert r.ok, "標籤比對必須忽略全形空白"

    def test_two_column_sheet_checks_both_columns(self) -> None:
        sheet = balance_sheet()
        for item in sheet["items"]:
            item["values"] = [*item["values"], item["values"][0]]
        sheet["period_labels"] = ["114/7/31", "113/7/31"]
        r = validate_statement(sheet)
        assert r.ok
        assert len(r.passed) == 10, "兩欄應各檢查 5 項"

    def test_blank_cell_is_not_treated_as_zero_operand(self) -> None:
        """A null operand must not silently contribute 0 to a passing sum.

        累積餘絀 = null with 本期餘絀 = 50 and 餘絀總額 = 300 is inconsistent; the
        validator must flag it rather than read null as 0 and then also fail.
        """
        r = validate_statement(balance_sheet(累積餘絀=[None]))
        assert not r.ok


class TestIncomeStatementValidation:
    def statement(self, **overrides: list) -> dict:
        items = {
            "教保費收入": [1000, 900, -100, 90],
            "其他收入": [None, 50, 50, None],
            "收入合計": [1000, 950, -50, None],
            "人事費": [800, 700, -100, 88],
            "行政管理費": [100, 100, None, 100],
            "支出合計": [900, 800, -100, None],
            "本期稅前餘絀": [100, 150, 50, None],
        }
        items.update(overrides)
        return {
            "statement_type": "收支餘絀表",
            "period_labels": ["預算數", "決算數", "差異數", "執行率%"],
            "items": [{"label": k, "values": v} for k, v in items.items()],
            "issues": [],
        }

    def test_consistent_statement_passes(self) -> None:
        r = validate_statement(self.statement())
        assert r.ok, r.failed

    def test_variance_column_is_checked_per_item(self) -> None:
        r = validate_statement(self.statement(人事費=[800, 700, -999, 88]))
        assert not r.ok
        assert any("人事費 差異數" in f for f in r.failed)

    def test_surplus_identity_is_checked(self) -> None:
        r = validate_statement(self.statement(本期稅前餘絀=[100, 999, 899, None]))
        assert not r.ok
        assert any("本期餘絀" in f for f in r.failed)

    def test_ratio_column_is_not_summed(self) -> None:
        """執行率 is a percentage; summing line items in it would be meaningless."""
        r = validate_statement(self.statement())
        assert not any("執行率" in label for label in r.passed + r.failed)

    def test_unbudgeted_expense_does_not_break_totals(self) -> None:
        """其他收入 has no budget figure -- a null there must sum as absent."""
        r = validate_statement(self.statement())
        assert any("預算數" in p and "收入合計" in p for p in r.passed)


class TestContract:
    def test_schema_forbids_extra_properties(self) -> None:
        assert STATEMENT_SCHEMA["additionalProperties"] is False
        item = STATEMENT_SCHEMA["properties"]["items"]["items"]
        assert item["additionalProperties"] is False

    def test_schema_allows_null_values(self) -> None:
        values = STATEMENT_SCHEMA["properties"]["items"]["items"]["properties"]["values"]
        assert "null" in values["items"]["type"], "空白格必須能表達為 null"

    def test_schema_requires_issues_so_problems_cannot_be_silent(self) -> None:
        assert "issues" in STATEMENT_SCHEMA["required"]

    @pytest.mark.parametrize(
        "rule",
        ["null", "括號", "視覺位置", "不要猜測", "逐字照抄"],
    )
    def test_prompt_states_the_load_bearing_rules(self, rule: str) -> None:
        """These five rules are why the output is safe to act on; none may be dropped."""
        assert rule in EXTRACTION_PROMPT


class TestRealPrintedHeaders:
    """表頭要照抄，所以檢核必須認得照抄後的樣子。

    規則 1 要求 `period_labels` 逐字照抄，而報表上印的是
    `預算數(a)`／`決算數(b)`／`差異數(c)=(b)-(a)`／`執行率(%)(d)=(b)/(a)`。
    這個類別裡的 fixture 用的就是真實表頭——其餘測試用的裸名
    （`預算數`／`決算數`…）在整個語料庫裡一次都沒出現過，所以那些測試
    全綠也證明不了檢核在真實資料上跑得動。實際上它曾經永遠跑不動。
    """

    HEADERS: typing.ClassVar[list] = [
        "預算數(a)", "決算數(b)", "差異數(c)=(b)-(a)", "執行率(%)(d)=(b)/(a)"]

    def statement(self, **overrides: list) -> dict:
        items = {
            "學雜費收入": [1000, 950, -50, 95],
            "收入合計": [1000, 950, -50, 95],
            "人事費": [800, 700, -100, 88],
            "行政管理費": [100, 100, None, 100],
            "支出合計": [900, 800, -100, None],
            "本期稅前餘絀": [100, 150, 50, None],
        }
        items.update(overrides)
        return {
            "statement_type": "收支餘絀表",
            "period_labels": self.HEADERS,
            "items": [{"label": k, "values": v} for k, v in items.items()],
            "issues": [],
        }

    def test_variance_is_actually_checked_with_printed_headers(self) -> None:
        """以前這裡是 skipped，不是 passed——而摘要只印 passed/(passed+failed)。"""
        r = validate_statement(self.statement())
        assert r.ok, r.failed
        assert any("差異數" in p for p in r.passed), "逐列差異數檢核沒有跑到"
        assert not any("找不到對應欄位" in s for s in r.skipped)

    def test_a_wrong_variance_is_caught(self) -> None:
        r = validate_statement(self.statement(人事費=[800, 700, -999, 88]))
        assert not r.ok
        assert any("人事費 差異數" in f for f in r.failed)

    def test_swapping_the_actual_and_variance_columns_is_caught(self) -> None:
        """整欄互換是自我驗算最容易漏掉的一類錯，也是最該抓到的一類。"""
        swapped = self.statement()
        for it in swapped["items"]:
            v = it["values"]
            v[1], v[2] = v[2], v[1]
        r = validate_statement(swapped)
        assert not r.ok, "決算數與差異數整欄互換卻通過了自我驗算"
