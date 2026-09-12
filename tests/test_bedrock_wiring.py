"""三個 AI 落點實際接到 Bedrock 了沒有——以及接不上時會怎麼樣。

`BedrockPlanner` 與 `BedrockBackend` 存在很久了，但長期沒有任何東西呼叫它們：
`/api/chat` 寫死 `KeywordPlanner`，而視覺抽取沒有驅動程式。兩者都能通過當時
的測試，因為當時的測試問的是「這個類別寫對了嗎」，不是「正式路徑會走到它嗎」。

所以這一檔測的是**接線**：端點會不會挑到 Bedrock、挑不到時會不會安靜降級、
以及降級有沒有照實說出來。最後一項是重點——會場斷網時悄悄換成關鍵字比對而
不說，跟宣稱跑在 Bedrock 上是同一件事。
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.api import chat as chat_mod

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_script(name: str):
    """scripts/ 不是套件，依路徑載入。"""
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── 該用哪一個 planner ───────────────────────────────────────────────
def test_env_var_forces_a_planner(monkeypatch):
    """強制指定要蓋過自動偵測，否則沒辦法單獨測某一條路。"""
    monkeypatch.setenv(chat_mod.PLANNER_ENV, "keyword")
    assert chat_mod.resolve_kind() == "keyword"
    monkeypatch.setenv(chat_mod.PLANNER_ENV, "bedrock")
    assert chat_mod.resolve_kind() == "bedrock"


def test_auto_falls_back_to_keyword_without_bedrock(monkeypatch):
    """沒有憑證或沒裝 anthropic 時，auto 必須落到關鍵字而不是爆炸。"""
    monkeypatch.delenv(chat_mod.PLANNER_ENV, raising=False)
    monkeypatch.setattr(chat_mod, "bedrock_available", lambda: False)
    assert chat_mod.resolve_kind() == "keyword"


def test_auto_picks_bedrock_when_available(monkeypatch):
    monkeypatch.delenv(chat_mod.PLANNER_ENV, raising=False)
    monkeypatch.setattr(chat_mod, "bedrock_available", lambda: True)
    assert chat_mod.resolve_kind() == "bedrock"


def test_availability_check_does_not_call_the_network(monkeypatch):
    """降級必須是免費的。

    若可用性檢查自己去 call Bedrock，斷網時每一次查詢都要先等一個 timeout
    才降級——那等於沒有降級。這裡把 client 換成會爆的東西來確認沒被碰到。
    """
    from smart_watchdog import bedrock as bedrock_mod

    def explode(*_a, **_k):
        raise AssertionError("bedrock_available() 不該建立 client")

    monkeypatch.setattr(bedrock_mod, "client", explode)
    chat_mod.bedrock_available()  # 不論回 True 或 False，都不該拋


def test_answer_default_is_still_keyword():
    """`answer()` 是純函式，預設不能因為本機剛好有憑證就改變行為。

    選哪一個 planner 是應用層的決定（見 api/server.py），不是這支函式的。
    否則同一份程式在有無憑證的機器上會得到不同的預設路徑，測試也就不可重現。
    """
    assert chat_mod.answer("全市概況", _FAKE)["planner"] == "keyword"


# ── 端點降級 ────────────────────────────────────────────────────────
_FAKE = {
    "schema_version": 1,
    "points": [
        {"i": "a1", "n": "甲", "full": "新北市私立甲幼兒園", "t": 2, "d": "板橋區",
         "x": 121.4, "y": 25.0, "s": 0.5, "r": 1, "why": "分數前100",
         "fin": 0, "cf": 0, "ch": 0, "ct": "", "e90": 0, "e365": 0, "np": 3,
         "cap": 60, "fee": 12000, "ev": "", "evd": "", "er": "", "erd": "", "ep": 0},
    ],
    "districts": [], "dossier": {}, "bench": {}, "boundary": [],
    "realtime": {"swept_at": "", "channels_live": 0, "channels_total": 0,
                 "channels": [], "by_institution": {}},
}


class _BrokenPlanner(chat_mod.Planner):
    """模擬斷網、憑證過期、被限流——三者在呼叫端看起來都是這樣。"""

    name = "bedrock"

    def plan(self, question: str):  # noqa: ARG002 - 簽名要與 Planner 一致
        raise RuntimeError("The security token included in the request is expired")


def test_chat_endpoint_degrades_instead_of_500(monkeypatch):
    fastapi = pytest.importorskip("fastapi")  # noqa: F841
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from smart_watchdog.api import server as server_mod

    monkeypatch.setattr(server_mod, "chat_planner", lambda: _BrokenPlanner())
    monkeypatch.setattr(server_mod, "payload", lambda: _FAKE)

    r = TestClient(server_mod.app).post("/api/chat", json={"question": "全市概況"})
    assert r.status_code == 200
    body = r.json()
    # 降級後仍然是一個完整的答案……
    assert body["results"]
    assert "非危險或違法認定" in body["caveat"]
    # ……而且照實說了是誰做的計畫，以及為什麼換人。
    assert body["planner"] == "keyword"
    assert "planner_fallback" in body
    assert "憑證已過期" in body["planner_fallback"]


def test_health_reports_which_planner_is_live(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from smart_watchdog.api import server as server_mod

    monkeypatch.setenv(chat_mod.PLANNER_ENV, "keyword")
    h = TestClient(server_mod.app).get("/api/health").json()
    assert h["chat_planner"] == "keyword"


# ── 抽取驅動程式 ────────────────────────────────────────────────────
def test_extraction_driver_refuses_to_overwrite_verified_data():
    """`data/extracted/` 是人工核對過並已進版控的結果。

    模型輸出寫進去必須是人明確搬過去的動作。這裡測的是那道防線真的擋得住，
    因為一旦被一次預設行為蓋掉，下游每一項法遵發現都會悄悄改變依據。
    """
    driver = _load_script("extract_nonprofit_bedrock.py")
    for bad in ("data/extracted", "data/extracted/nonprofit", "./data/extracted/poc"):
        with pytest.raises(SystemExit):
            driver._assert_safe_out(pathlib.Path(bad))


def test_extraction_driver_allows_interim(tmp_path):
    driver = _load_script("extract_nonprofit_bedrock.py")
    driver._assert_safe_out(pathlib.Path("data/interim/poc_extract"))
    driver._assert_safe_out(tmp_path)  # 不該拋


def test_extraction_driver_is_registered_in_run_py():
    """寫得出來但列不出來的任務等於不存在。"""
    run_py = (ROOT / "run.py").read_text(encoding="utf-8")
    assert "extract_nonprofit_bedrock.py" in run_py
    assert "extract-bedrock" in run_py


def test_anthropic_is_a_pinned_requirement():
    """三個 AI 落點都靠 `from anthropic import AnthropicBedrock`。

    它曾經只存在於 `check_credentials.py` 的一行提示裡（「決賽當天：
    pip install anthropic」），於是 `run.py setup` 跑完的 venv 每一項測試都過、
    卻連不上 Bedrock——而失敗訊息會指向憑證，不會指向缺套件。
    """
    reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert any(line.startswith("anthropic==") for line in reqs.splitlines())


# ── 摘要必須描述實際回傳的內容 ──────────────────────────────────────
class _WidePlanner(chat_mod.Planner):
    """BedrockPlanner 的 schema 對 limit 沒有上限，實測回過 limit=100。"""

    name = "bedrock"

    def plan(self, question: str):  # noqa: ARG002 - 簽名要與 Planner 一致
        return chat_mod.QueryPlan(filters={}, sort="rank", limit=100, intent="list")


def test_summary_never_claims_more_rows_than_it_returned():
    """「以下為前 N 所」的 N 必須等於實際回傳列數。

    `answer()` 把 limit 夾在 HARD_LIMIT，而 `_summarise()` 原本各自重算一次
    且沒有夾。KeywordPlanner 自己先夾到 50 所以永遠碰不到；換成 Bedrock 後
    limit=100、命中 70 所，摘要就寫成「以下為前 70 所」而實際只回 50 列。
    """
    payload = {
        "schema_version": 1,
        "points": [
            dict(_FAKE["points"][0], i=f"x{n:03d}", r=n) for n in range(1, 71)
        ],
        "districts": [], "dossier": {}, "bench": {}, "boundary": [],
        "realtime": {"by_institution": {}},
    }
    r = chat_mod.answer("全市概況", payload, planner=_WidePlanner())
    assert r["matched"] == 70
    assert r["returned"] == chat_mod.HARD_LIMIT == 50
    assert f"以下為前 {r['returned']} 所" in r["summary"]
    assert "前 70 所" not in r["summary"]


# ── 冒煙測試不得破壞正式產出 ────────────────────────────────────────
def test_limit_refuses_to_truncate_the_real_letter_index():
    """`--limit N` 寫進預設目錄會把完整索引重寫成 N 列。

    index.csv 是以 "w" 整個重寫的，所以 `--limit 1` 把 141 列砍成 1 列。
    而 `bedrock-check --full` 的冒煙測試正是用 `--limit 1` 跑的——決賽當天
    每天早上要跑的那條指令，每跑一次就毀掉一次名單。實際發生過。
    """
    import os
    import subprocess

    # 子行程的編碼要自己交代清楚，不能靠呼叫端剛好設對。
    # 直接跑 pytest（不經 run.py）時 PYTHONIOENCODING 沒有被設，子行程用 cp950
    # 寫中文錯誤訊息，而這裡用 utf-8 解 → subprocess 的 reader thread 拋
    # UnicodeDecodeError，proc.stderr 變成 None，測試以 TypeError 收場。
    # 正是 smart_watchdog/console.py 說的那個「只在警告成立的那一次才當機」。
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_audit_letters.py"),
         "--limit", "1"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT), env=env, timeout=120)
    assert proc.returncode != 0
    assert "--out" in ((proc.stdout or "") + (proc.stderr or ""))


def test_bedrock_check_smoke_test_writes_somewhere_disposable():
    src = (ROOT / "scripts" / "check_bedrock.py").read_text(encoding="utf-8")
    assert '"--out", "data/interim/letters_smoke"' in src


# ── 憑證死掉時說了什麼 ───────────────────────────────────────────────
#
# 黑客松發的是臨時憑證，幾小時就過期。2026-09-12 實際發生：健康檢查回報一切
# 正常（因為它只看環境變數在不在），助理則把 boto3 原文貼進對話框——
# 「ExpiredTokenException … (reached max retries: 0)」。台上沒有人知道該做什麼。


class _Boom:
    """假的 bedrock-runtime client，呼叫就丟指定的錯。"""

    def __init__(self, message: str) -> None:
        self._message = message

    def converse_stream(self, **_kw):
        raise RuntimeError(self._message)


def _stream_error(message: str) -> str:
    from smart_watchdog.agent.backend import AgentError, BedrockAgentBackend

    backend = BedrockAgentBackend.__new__(BedrockAgentBackend)
    backend.model_id, backend.region = "m", "us-west-2"
    backend.max_tokens, backend.timeout_s = 64, 45
    backend._client = _Boom(message)
    with pytest.raises(AgentError) as got:
        list(backend.stream(system="s", messages=[], tools=[]))
    return str(got.value)


def test_an_expired_token_is_explained_as_something_to_go_and_fix() -> None:
    """過期是最可能在示範中途發生的第二種失敗（第一種是逾時）。

    使用者要看到的是「去改哪裡、然後重啟」，不是 botocore 的例外類別名稱。
    """
    said = _stream_error(
        "An error occurred (ExpiredTokenException) when calling the ConverseStream "
        "operation (reached max retries: 0): The security token ... is expired")
    assert "過期" in said and ".env" in said and "重啟" in said
    assert "ExpiredTokenException" not in said, "原文不該貼給使用者看"


def test_an_invalid_token_is_not_reported_as_expired() -> None:
    """兩者的處置不同：過期要換新的，無效多半是貼漏了一段。
    講錯會讓人去做沒有用的事。"""
    said = _stream_error(
        "An error occurred (UnrecognizedClientException): The security token "
        "included in the request is invalid")
    assert "無效" in said and "過期" not in said


def test_an_unknown_failure_still_carries_the_original_text() -> None:
    """只有認得的失敗才翻譯。認不得的照原樣帶出來，否則就沒得查了。"""
    assert "這是沒看過的錯" in _stream_error("這是沒看過的錯")


def test_health_checks_whether_the_credentials_actually_work(monkeypatch) -> None:
    """`status()` 只看環境變數在不在——過期的憑證變數還在，所以它會說一切正常。

    這支測的是另一個問題：**現在打得通嗎**。三種結果要分得開，
    尤其是「斷網」不可以被說成「憑證有問題」——那會讓人去改一個沒壞的東西。
    """
    import boto3

    from smart_watchdog import config

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKE")

    def _raise(message):
        def _factory(*_a, **_kw):
            raise RuntimeError(message)
        return _factory

    monkeypatch.setattr(boto3, "client", _raise(
        "An error occurred (ExpiredTokenException): token is expired"))
    assert config.aws_identity()["usable"] is False

    monkeypatch.setattr(boto3, "client", _raise(
        "An error occurred (InvalidClientTokenId): token is invalid"))
    assert config.aws_identity()["usable"] is False

    # 斷網：不知道，不是壞掉。None 與 False 必須分得開。
    monkeypatch.setattr(boto3, "client", _raise("EndpointConnectionError: 連不上"))
    assert config.aws_identity()["usable"] is None


def test_missing_credentials_are_reported_without_calling_aws(monkeypatch) -> None:
    """沒設就沒得打，不要白花四秒逾時去問 STS。"""
    import boto3

    from smart_watchdog import config

    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.setattr(config, "load_env", lambda: None)

    def _boom(*_a, **_kw):
        raise AssertionError("不該打 AWS")

    monkeypatch.setattr(boto3, "client", _boom)
    assert config.aws_identity()["usable"] is False
