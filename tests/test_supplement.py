import copy
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

from smart_watchdog.extract.supplement import SUPPLEMENT_SCHEMA, validate_supplement

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUPPLEMENT_DIR = ROOT / "data" / "extracted" / "nonprofit_supplement"
MAIN_DIR = ROOT / "data" / "extracted" / "nonprofit"
PILOTS = (
    "N01_安溪_110.json",
    "N26_新店及人_111.json",
    "N18_福營_113.json",
)


def load_pair(name: str) -> tuple[dict, dict]:
    supplement = json.loads((SUPPLEMENT_DIR / name).read_text(encoding="utf-8"))
    main = json.loads((MAIN_DIR / name).read_text(encoding="utf-8"))
    return supplement, main


class TestPilotReconciliation:
    @pytest.mark.parametrize("name", PILOTS)
    def test_shape_and_main_statement_cross_checks_pass(self, name: str) -> None:
        supplement, main = load_pair(name)
        result = validate_supplement(supplement, main)

        assert result.schema_errors == []
        current_period = supplement["cash_flow_statement"]["periods"][0]
        checks_by_name = {check.name: check for check in result.checks}
        required_checks = {
            "補充 JSON code = 主 JSON code",
            "補充 JSON short_name = 主 JSON short_name",
            "補充 JSON academic_year = 主 JSON academic_year",
            "淨值變動表期末累積餘絀 = 主 JSON 資產負債表",
            "淨值變動表期末本期餘絀 = 主 JSON 資產負債表",
            "淨值變動表期末餘絀總額 = 主 JSON 資產負債表",
            "本期期末現金 = 主 JSON 資產負債表現金",
            "現金流量表本期稅前餘絀 = 主 JSON 本期餘絀",
            f"現金流量表 {current_period} 期末現金 = 期初現金 + 現金淨增加",
        }
        assert required_checks <= checks_by_name.keys()
        assert all(checks_by_name[name].status == "passed" for name in required_checks)

    @pytest.mark.parametrize("name", PILOTS[:2])
    def test_consistent_pilots_have_no_failed_checks(self, name: str) -> None:
        supplement, main = load_pair(name)
        assert validate_supplement(supplement, main).failed == []

    def test_n18_preserves_and_flags_source_one_dollar_difference(self) -> None:
        supplement, main = load_pair("N18_福營_113.json")
        result = validate_supplement(supplement, main)

        assert len(result.failed) == 1
        failure = result.failed[0]
        assert failure.name == "淨值變動表「113學年度餘絀」列加總"
        assert failure.lhs == 922796
        assert failure.rhs == 922795
        assert failure.difference == 1
        assert supplement["net_asset_changes"]["rows"][5]["total"] == 922796
        assert supplement["reconciliation"][0]["status"] == "failed"

    def test_null_cells_are_not_rewritten_as_zero(self) -> None:
        supplement, main = load_pair("N01_安溪_110.json")
        transfer = supplement["net_asset_changes"]["rows"][1]
        investing = supplement["cash_flow_statement"]["rows"][9]

        assert transfer["total"] is None
        assert investing["values"] == [None, None]
        result = validate_supplement(supplement, main)
        transfer_check = next(check for check in result.checks if "108學年度" in check.name)
        assert transfer_check.status == "passed"
        assert "原輸出仍保留 null" in transfer_check.detail


class TestSupplementContract:
    def test_root_contract_is_closed_and_requires_provenance(self) -> None:
        assert SUPPLEMENT_SCHEMA["additionalProperties"] is False
        assert "page_provenance" in SUPPLEMENT_SCHEMA["required"]
        assert "reconciliation" in SUPPLEMENT_SCHEMA["required"]

    def test_unusable_text_layer_and_manual_verification_are_required(self) -> None:
        supplement, main = load_pair("N01_安溪_110.json")
        broken = copy.deepcopy(supplement)
        broken["page_provenance"][0]["text_usable"] = True
        broken["page_provenance"][1]["manually_verified"] = False

        errors = validate_supplement(broken, main).schema_errors
        assert any("unusable text layer" in error for error in errors)
        assert any("manually verified" in error for error in errors)

    def test_invalid_nested_shapes_return_errors_instead_of_crashing(self) -> None:
        supplement, main = load_pair("N01_安溪_110.json")
        broken = copy.deepcopy(supplement)
        broken["net_asset_changes"]["rows"][0] = []
        broken["cash_flow_statement"]["rows"][0]["values"] = [218557]

        result = validate_supplement(broken, main)
        assert result.schema_errors
        assert result.checks == []

    def test_missing_section_total_is_rejected(self) -> None:
        supplement, main = load_pair("N18_福營_113.json")
        broken = copy.deepcopy(supplement)
        broken["cash_flow_statement"]["rows"] = [
            row
            for row in broken["cash_flow_statement"]["rows"]
            if row["kind"] != "investing_net"
        ]

        errors = validate_supplement(broken, main).schema_errors
        assert any("exactly one investing_net" in error for error in errors)

    def test_main_document_identity_mismatch_fails(self) -> None:
        supplement, main = load_pair("N01_安溪_110.json")
        wrong_main = copy.deepcopy(main)
        wrong_main["code"] = "N99"

        result = validate_supplement(supplement, wrong_main)
        identity = next(
            check
            for check in result.checks
            if check.name == "補充 JSON code = 主 JSON code"
        )
        assert identity.status == "failed"
        assert not result.ok

    def test_persisted_reconciliation_must_match_computed_result(self) -> None:
        supplement, main = load_pair("N01_安溪_110.json")
        broken = copy.deepcopy(supplement)
        broken["reconciliation"][0]["lhs"] = 1
        broken["reconciliation"][0]["rhs"] = 1
        broken["reconciliation"][0]["difference"] = 0

        errors = validate_supplement(broken, main).schema_errors
        assert any("does not match computed result" in error for error in errors)

    @pytest.mark.parametrize("name", PILOTS)
    def test_audit_evidence_contains_key_opinion_language(self, name: str) -> None:
        supplement, _ = load_pair(name)
        audit = supplement["audit_report"]

        assert audit["opinion_type"] == "unmodified"
        assert "足以允當表達" in audit["opinion_text"]
        assert "足夠及適切之查核證據" in audit["basis_text"]
        assert "僅供新北市政府教育局監督" in audit["emphasis_of_matter_text"]
