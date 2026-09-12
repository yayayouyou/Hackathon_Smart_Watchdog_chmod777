"""Verify the hand-transcribed ground truth is internally consistent.

The ground truth is what a Bedrock extraction will be scored against, so an error
in it would silently corrupt every accuracy number we report. These tests assert
the accounting identities each statement must satisfy, which catches a mistyped
digit without needing a second reader.
"""

from __future__ import annotations

import json
import pathlib

import pytest

GT_PATH = pathlib.Path("data/ground_truth/nonprofit_statements.json")
GROUND_TRUTH = json.loads(GT_PATH.read_text(encoding="utf-8"))
STATEMENTS = {k: v for k, v in GROUND_TRUTH.items() if not k.startswith("_")}
BALANCE_SHEETS = [k for k in STATEMENTS if "資產負債表" in k]


def col(stmt: dict, item: str, i: int) -> float:
    """One cell, treating an absent line or a blank cell as zero for summing."""
    values = stmt["items"].get(item)
    if values is None or i >= len(values) or values[i] is None:
        return 0.0
    return float(values[i])


def n_cols(stmt: dict) -> int:
    return len(stmt["columns"])


@pytest.mark.parametrize("key", BALANCE_SHEETS)
class TestBalanceSheetIdentities:
    def test_total_assets_equals_current_plus_noncurrent(self, key: str) -> None:
        s = STATEMENTS[key]
        for i in range(n_cols(s)):
            assert col(s, "資產總計", i) == pytest.approx(
                col(s, "流動資產合計", i) + col(s, "非流動資產合計", i)
            ), f"{key} 第{i}欄 資產總計"

    def test_liabilities_equals_current_plus_noncurrent(self, key: str) -> None:
        s = STATEMENTS[key]
        for i in range(n_cols(s)):
            assert col(s, "負債總額", i) == pytest.approx(
                col(s, "流動負債合計", i) + col(s, "非流動負債合計", i)
            ), f"{key} 第{i}欄 負債總額"

    def test_equity_equals_accumulated_plus_current(self, key: str) -> None:
        s = STATEMENTS[key]
        for i in range(n_cols(s)):
            assert col(s, "餘絀總額", i) == pytest.approx(
                col(s, "累積餘絀", i) + col(s, "本期餘絀", i)
            ), f"{key} 第{i}欄 餘絀總額"

    def test_balance_sheet_balances(self, key: str) -> None:
        """The defining identity: assets = liabilities + equity."""
        s = STATEMENTS[key]
        for i in range(n_cols(s)):
            assert col(s, "負債及餘絀總計", i) == pytest.approx(
                col(s, "負債總額", i) + col(s, "餘絀總額", i)
            ), f"{key} 第{i}欄 負債及餘絀總計"
            assert col(s, "資產總計", i) == pytest.approx(
                col(s, "負債及餘絀總計", i)
            ), f"{key} 第{i}欄 資產 = 負債及餘絀"

    def test_current_assets_sum_of_components(self, key: str) -> None:
        s = STATEMENTS[key]
        components = [
            "現金及銀行存款", "應收帳款淨額", "其他應收款",
            "預付款項", "其他流動資產",
        ]
        for i in range(n_cols(s)):
            assert col(s, "流動資產合計", i) == pytest.approx(
                sum(col(s, c, i) for c in components)
            ), f"{key} 第{i}欄 流動資產合計"

    def test_reserve_asset_vs_liability_gap_is_recorded(self, key: str) -> None:
        """業務發展準備金 (asset) vs 業務發展準備 (liability).

        Not an identity -- the gap is the forensic signal itself -- but whenever a
        gap exists the entry must document it, so a transcription slip cannot be
        mistaken for a finding.
        """
        s = STATEMENTS[key]
        for i in range(n_cols(s)):
            gap = col(s, "業務發展準備", i) - col(s, "業務發展準備金", i)
            if abs(gap) > 1:
                assert "forensic_note" in s, f"{key} 第{i}欄 有 {gap:,.0f} 缺口卻未記錄"


class TestIncomeStatementIdentities:
    KEY = "N01_112_收支餘絀表"

    @property
    def stmt(self) -> dict:
        return STATEMENTS[self.KEY]

    def test_revenue_total(self) -> None:
        s = self.stmt
        items = [
            "教保費收入", "教保費收入減項", "延長照顧服務收入淨額",
            "利息收入", "其他收入",
        ]
        assert col(s, "收入合計", 1) == pytest.approx(sum(col(s, i, 1) for i in items))

    def test_expense_total(self) -> None:
        s = self.stmt
        items = [
            "人事費", "業務費", "材料費", "維護費", "修繕購置費", "雜支",
            "行政管理費", "業務發展費", "延長照顧服務支出", "其他支出", "呆帳損失",
        ]
        assert col(s, "支出合計", 1) == pytest.approx(sum(col(s, i, 1) for i in items))

    def test_surplus_is_revenue_minus_expense(self) -> None:
        s = self.stmt
        assert col(s, "本期稅前餘絀", 1) == pytest.approx(
            col(s, "收入合計", 1) - col(s, "支出合計", 1)
        )

    def test_budget_column_also_balances(self) -> None:
        s = self.stmt
        assert col(s, "本期稅前餘絀", 0) == pytest.approx(
            col(s, "收入合計", 0) - col(s, "支出合計", 0)
        )

    def test_variance_is_actual_minus_budget(self) -> None:
        s = self.stmt
        for item, values in s["items"].items():
            if values[2] is None:
                continue
            budget = values[0] or 0
            actual = values[1] or 0
            assert values[2] == pytest.approx(actual - budget), f"{item} 差異數"

    def test_execution_rate_matches_ratio(self) -> None:
        s = self.stmt
        for item, values in s["items"].items():
            rate, budget, actual = values[3], values[0], values[1]
            if rate is None or not budget:
                continue
            assert rate == pytest.approx(actual / budget * 100, abs=0.6), (
                f"{item} 執行率 {rate} vs {actual / budget * 100:.2f}"
            )


@pytest.mark.skipif(
    not pathlib.Path("data/raw").exists(),
    reason="data/raw 是主辦方資料集（1.8 GB），不進版控。"
           "在只 clone 程式碼的機器上無從檢查來源 PDF——"
           "這是缺少資料，不是 ground truth 有問題。")
def test_every_statement_declares_its_source() -> None:
    for key, s in STATEMENTS.items():
        assert pathlib.Path(s["file"]).exists(), f"{key} 來源 PDF 不存在"
        assert isinstance(s["page_index"], int), f"{key} 缺 page_index"
        assert s["columns"], f"{key} 缺 columns"
