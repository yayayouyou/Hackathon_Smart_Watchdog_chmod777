"""回覆草稿：測的是它不會做的事，以及它被退件時會怎樣。

這份文件是這套系統唯一一個「以陌生人寫的字為輸入、產生對外文字」的產物，
所以它壞掉的方式不是文法不通，是措辭上多說了一句：

* 官方帳號在一則未查證的指控底下公開回覆並**點名那一園**——在任何人查證之前，
  由機關出面把那一園與那則指控綁在一起。
* 模型把貼文裡的句子**搬進本局自己的話**裡，於是讀起來像機關在複述指控。
* 免責句掉了一句，整篇就從「受理說明」變成「已受理的案件」。
* 有一天有人加了一支發文函式，草稿變成自動回覆。

離線保證：整支測試不需要 AWS、不需要網路。樣板後端是確定性的；生成那條路用
注入的假後端，包含一個故意寫出違法認定字眼的、一個照抄原文的。
"""

from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.report import reply, verify

ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "smart_watchdog"
WEBAPP = ROOT / "webapp"

PERMALINK = "https://www.threads.net/@luzhou_mama/post/DPa7xQ2yk1L"
THREAD = [
    {"threads_id": "M1", "username": "luzhou_mama", "kind": "mention",
     "posted_at": "2026-09-08T11:24:51+0000", "permalink": PERMALINK,
     "text": "@ntpc_watchdog 蘆洲文德幼兒園的收費單有問題，"
             "上個月多收了一筆「教材升級費」3,600 元，問了也要不到收據。"},
    {"threads_id": "R1", "username": "another", "kind": "reply",
     "posted_at": "2026-09-08T12:03:22+0000",
     "permalink": "https://www.threads.net/@another/post/R1",
     "text": "我也遇過 +1"},
]
INSTITUTION = {"title": "新北市私立文德幼兒園"}


class FakeReplyBackend(reply.ReplyBackend):
    name = "fake"

    def __init__(self, paragraphs, *, boom: bool = False) -> None:
        self._paragraphs = list(paragraphs)
        self.boom = boom

    def paragraphs(self, institution, posts):
        del institution, posts
        if self.boom:
            raise RuntimeError("Bedrock 打不通")
        return list(self._paragraphs)


def _draft(backend=None, **kw):
    return reply.draft(INSTITUTION, THREAD, backend=backend, **kw)


# ── 樣板是地板 ───────────────────────────────────────────────────────


def test_the_template_draft_always_passes_the_gate():
    """地板的定義：它的輸出恆過閘門，所以生成那條路可以很嚴。"""
    d = _draft()
    assert d.backend == "template"
    assert d.verified, d.problems
    assert d.problems == []


def test_the_draft_carries_the_permalink_and_both_declarations():
    """permalink、未經查證、非違法認定。少任何一項都不該被當成可用的草稿。"""
    d = _draft()
    assert PERMALINK in d.text
    assert verify.REQUIRED_UNVERIFIED in d.text
    assert verify.REQUIRED_DISCLAIMER in d.text


def test_the_draft_says_it_needs_a_human_before_it_goes_out():
    d = _draft()
    assert "不會自動送出" in d.text
    for item in reply.CHECKLIST:
        assert item in d.text


def test_the_sendable_half_names_no_institution():
    """公開回覆不點名，但承辦人須知那一段要有園名——他得知道這是哪一家。"""
    d = _draft()
    assert INSTITUTION["title"] in d.text
    assert INSTITUTION["title"] not in d.sendable
    assert not re.findall(r"[一-鿿]{2,12}(?:幼兒園|教保服務中心)", d.sendable)


def test_the_two_halves_are_separated_by_a_marker_the_caller_can_rely_on():
    """前端不自己切字串；切錯會把園名連同「這是未查證的通報」一起貼出去。"""
    d = _draft()
    assert reply.SENDABLE_MARK in d.text
    assert d.text.endswith(d.sendable)


def test_the_draft_does_not_quote_the_report_back():
    """複述指控 = 機關確認了指控。樣板一個字都沒有搬。"""
    assert "教材升級費" not in _draft().text.split(reply.SENDABLE_MARK)[1]
    assert "3,600" not in _draft().sendable


# ── 閘門 ─────────────────────────────────────────────────────────────


def test_a_verdict_word_is_rejected_and_rewritten_by_the_template():
    """FORBIDDEN 命中就退件重寫。退件不等於沒有草稿。"""
    bad = FakeReplyBackend(["本局已查明該園違規收費，將依法裁處。"])
    d = _draft(bad)
    assert d.backend == "template"
    assert d.fell_back
    assert "違規" in d.fallback_reason
    assert d.verified, d.problems


def test_the_rejected_draft_can_be_inspected_when_asked_for():
    """`fallback=False` 給除錯用：看得到模型寫了什麼、為什麼被退。"""
    bad = FakeReplyBackend(["本局已查明該園違規收費，將依法裁處。"])
    d = _draft(bad, fallback=False)
    assert d.backend == "fake"
    assert not d.verified
    assert any("違規" in p for p in d.problems)


def test_naming_an_institution_in_the_sendable_half_is_rejected():
    bad = FakeReplyBackend(["本局將就新北市私立文德幼兒園之收費情形查明。"])
    d = _draft(bad, fallback=False)
    assert not d.verified
    assert any("文德幼兒園" in p for p in d.problems)


def test_copying_the_post_into_official_prose_is_rejected():
    """prompt injection 的停損點：陌生人寫的字穿不過模型進到官方發言裡。"""
    stolen = THREAD[0]["text"][10:10 + verify.ECHO_WINDOW + 4]
    bad = FakeReplyBackend([f"本局收到您反映{stolen}，將依規定辦理。"])
    d = _draft(bad, fallback=False)
    assert not d.verified
    assert any("照抄" in p for p in d.problems)


def test_an_invented_figure_is_rejected():
    bad = FakeReplyBackend(["本局已受理，案號 20260915，將於期限內回覆。"])
    d = _draft(bad, fallback=False)
    assert not d.verified
    assert any("無來源的數字" in p for p in d.problems)


def test_the_hotlines_are_the_only_numbers_that_need_no_source():
    """1999／113／110 是固定的公開號碼，不是本案的事實。"""
    result = verify.verify_reply(
        f"請撥 1999 或 113。{verify.REQUIRED_DISCLAIMER}、{verify.REQUIRED_UNVERIFIED}",
        sendable="請撥 1999 或 113。", sources=[""], permalink="")
    assert result.ok, result.problems


def test_a_missing_declaration_is_rejected():
    for text in (f"本局已收到。{verify.REQUIRED_DISCLAIMER}",
                 f"本局已收到。{verify.REQUIRED_UNVERIFIED}"):
        assert not verify.verify_reply(text).ok


def test_both_documents_share_one_word_list():
    """不要另外發明一套用詞檢核。建議書與回覆草稿共用 `verdict_words()`。"""
    src = (SRC / "report" / "verify.py").read_text(encoding="utf-8")
    assert src.count("FORBIDDEN = ") == 1
    assert src.count("NEGATED = ") == 1
    assert src.count("for phrase in NEGATED") == 1
    # 免責句自己含有「違法」，剝除規則錯的話它會退掉自己的免責句。
    assert verify.verdict_words(verify.REQUIRED_DISCLAIMER) == []
    assert verify.verdict_words("該園違法") == ["違法"]


def test_the_reply_module_never_invents_its_own_wording_check():
    code = (SRC / "report" / "reply.py").read_text(encoding="utf-8")
    assert "FORBIDDEN" not in code.replace("from .verify import", "")
    assert "verify_reply" in code


# ── 沒有任何發文路徑 ─────────────────────────────────────────────────


def test_the_threads_client_has_no_write_functions():
    """`scrape/threads.py` 是唯讀的。加一支發文函式就是自動受理表態。"""
    src = (SRC / "scrape" / "threads.py").read_text(encoding="utf-8")
    names = re.findall(r"^def (\w+)", src, re.M)
    writes = [n for n in names
              if any(k in n for k in ("publish", "create", "delete", "send", "post_"))]
    assert not writes, f"threads.py 出現寫入函式：{writes}"


def test_no_module_sends_anything_to_the_threads_api():
    """整個 src/ 與 scripts/ 都沒有 POST 到 Threads 的路徑。

    `/me/threads` 是建立貼文容器的端點，`/me/threads_publish` 是發布它的。
    兩個字串在這個 repo 裡一次都不該出現。
    """
    paths = list(SRC.rglob("*.py")) + list((ROOT / "scripts").glob("*.py"))
    for path in paths:
        src = path.read_text(encoding="utf-8")
        for banned in ("threads_publish", "/me/threads?", "/me/threads'",
                       '/me/threads"'):
            assert banned not in src, f"{path.name} 出現發文端點 {banned}"
        if "graph.threads.net" in src or "API_ROOT" in src:
            assert 'method="POST"' not in src, f"{path.name} 對 Threads 送了 POST"


def test_the_frontend_button_only_asks_for_text():
    """「擬定回覆」按下去只跟後端要一份草稿，不送出任何東西。"""
    for name in ("social.js", "memos.js"):
        src = (WEBAPP / name).read_text(encoding="utf-8")
        # 前端連 Threads 的網域都不該出現：permalink 是資料帶來的，不是寫死的。
        for banned in ("threads.net", "graph.threads", "threads_publish"):
            assert banned not in src, f"{name} 不得碰 {banned}"
    src = (WEBAPP / "social.js").read_text(encoding="utf-8")
    assert "draft-reply" in src


def test_the_endpoint_says_out_loud_that_it_never_sends():
    src = (SRC / "api" / "social.py").read_text(encoding="utf-8")
    assert '"auto_send": False' in src


# ── 降級 ─────────────────────────────────────────────────────────────


def test_a_backend_that_blows_up_still_produces_a_draft():
    """模型不通不該讓承辦人拿不到草稿。"""
    d = _draft(FakeReplyBackend([], boom=True))
    assert d.backend == "template"
    assert d.verified
    assert "呼叫失敗" in d.fallback_reason


def test_an_unattributed_thread_still_gets_a_draft_that_says_so():
    """拒配的照樣擬得出來，但草稿不會印一個猜出來的園名。"""
    d = reply.draft({"title": ""}, THREAD)
    assert d.verified
    assert "未歸屬" in d.text
