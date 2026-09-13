"""Tests for parsing the signing audit firm/accountant off 會計師查核報告 pages,
and for deciding whether a name difference is a real change or OCR noise.

These are pinned against the actual shape ``nonprofit_pages`` produces (verbatim
``text_sections`` transcripts, see EXTRACTION_GUIDE.md and pagewise.py) and
against the real variation this project found when it scanned the corpus:
`會計師：張景嵐` reappears as 張景崗／張景巍／張燕嵐／張惠嵐／張忠崗 across different
園-學年度 while the 核准文號 stays byte-identical -- OCR noise, not a rotation
of accountants. A test suite for this module has to keep that distinction
central, because getting it backwards means either manufacturing a false "換人"
finding out of a scanning artefact, or missing a real one.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.auditor import (
    AuditorSignature,
    _compare_accountant_names_fallback,
    classify_opinion_type,
    compare_accountant_names,
    parse_auditor_signature,
)


def page(heading: str, text: str) -> dict:
    return {
        "page_kind": "auditor_report",
        "text_sections": [{"heading": heading, "text": text}],
    }


# --- parse_auditor_signature --------------------------------------------------

SIGNATURE_TEXT = (
    "本會計師與治理單位溝通之事項，包括所規劃之查核範圍及時間，"
    "以及重大查核發現。\n\n誠明聯合會計師事務所\n\n會計師：張景嵐\n\n"
    "核准文號：台財證登六字第4319號\n\n民國114年9月30日"
)


def test_parses_firm_accountant_and_licence_from_signature_block():
    sig = parse_auditor_signature([page("會計師查核財務報表之責任", SIGNATURE_TEXT)])
    assert sig == AuditorSignature(
        firm="誠明聯合會計師事務所",
        accountant="張景嵐",
        licence_no="台財證登六字第4319號",
    )


def test_signature_spread_across_two_pages_is_still_found():
    """查核意見在 p3、簽名區塊在 p4 是常見版面（見 N01 安溪）。"""
    p3 = page("查核意見", "新北市安溪非營利幼兒園...業經本會計師查核竣事。")
    p4 = page("會計師查核財務報表之責任", SIGNATURE_TEXT)
    sig = parse_auditor_signature([p3, p4])
    assert sig.firm == "誠明聯合會計師事務所"
    assert sig.accountant == "張景嵐"
    assert sig.licence_no == "台財證登六字第4319號"


def test_missing_signature_block_returns_none_fields_not_guesses():
    """N02 山北 113：抽到的頁面把責任段截在簽名之前，三個欄位都該是 None。"""
    truncated = (
        "本會計師與治理單位溝通之事項，包括所規劃之查核範圍及時間，"
        "以及重大查核發現（包括於查核過程中所辨認之內部控制顯著缺失）。"
    )
    sig = parse_auditor_signature([page("會計師查核財務報表之責任", truncated)])
    assert sig == AuditorSignature(firm=None, accountant=None, licence_no=None)


def test_licence_number_whitespace_is_normalised():
    """有些報告在字號中間印了全形空白（「台財證登六字第 4319 號」）。"""
    spaced = SIGNATURE_TEXT.replace("台財證登六字第4319號", "台財證登六字第 4319 號")
    sig = parse_auditor_signature([page("會計師查核財務報表之責任", spaced)])
    assert sig.licence_no == "台財證登六字第4319號"


# --- classify_opinion_type ----------------------------------------------------

def test_default_opinion_is_unmodified_not_a_guess_about_missing_data():
    pages = [page("查核意見", "...業經本會計師查核竣事。足以允當表達...")]
    assert classify_opinion_type(pages) == "unmodified"


def test_qualified_opinion_is_read_from_the_heading():
    pages = [page("保留意見", "除...外，...在所有重大方面允當表達...")]
    assert classify_opinion_type(pages) == "qualified"


def test_unmodified_is_not_misread_as_qualified_by_substring():
    """「無保留意見」內含「保留意見」，不可讓子字串比對誤判。"""
    pages = [page("無保留意見", "...在所有重大方面允當表達...")]
    assert classify_opinion_type(pages) == "unmodified"


def test_adverse_and_disclaimer_are_distinguished():
    assert classify_opinion_type([page("否定意見", "...並未允當表達...")]) == "adverse"
    assert classify_opinion_type(
        [page("無法表示意見", "...本會計師無法取得足夠證據...")]) == "disclaimer"


# --- compare_accountant_names (deterministic fallback) -----------------------
#
# The LLM path needs live Bedrock credentials, so these pin the fallback that
# runs when Bedrock is unavailable -- the same one every offline test run and
# CI exercises. classify_opinion_type and parse_auditor_signature never touch
# the network, so this is the only place that distinction matters.

def test_identical_names_short_circuit_before_any_comparison():
    result = compare_accountant_names("張景嵐", "張景嵐")
    assert result.same_person is True
    assert result.method == "deterministic"


def test_fallback_treats_different_surname_as_different_person():
    result = _compare_accountant_names_fallback("張景嵐", "李景嵐")
    assert result.same_person is False
    assert result.method == "deterministic"


def test_fallback_treats_single_character_difference_as_likely_misread():
    """張景嵐 vs 張景崗：same surname, one character differs -- the corpus's
    actual OCR-noise pattern."""
    result = _compare_accountant_names_fallback("張景嵐", "張景崗")
    assert result.same_person is True
    assert result.method == "deterministic"


def test_fallback_treats_multi_character_difference_as_different_person():
    result = _compare_accountant_names_fallback("張景嵐", "張大明")
    assert result.same_person is False
    assert result.method == "deterministic"


def test_fallback_reports_missing_name_without_asserting_same_or_different():
    result = _compare_accountant_names_fallback("", "張景嵐")
    assert result.same_person is False
    assert "缺失" in result.reason
