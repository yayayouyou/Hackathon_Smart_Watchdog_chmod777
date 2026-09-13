"""Telegram bot 的契約：沒綁定就沒有資料、綁定碼一次一用、群組一律拒絕。

bot 是公開的——任何人搜得到 t.me/Little_Guardian_bot、加得了、傳得了訊息。
派工台的登入保護不會自動延伸到聊天室，所以這支測試釘住的是**那道重新建立的閘門**，
不是訊息長什麼樣子。

不打真的 Telegram：`telegram.call` 換成記錄呼叫的假函式。資料庫照 test_auth.py
的做法指到 tmp_path，不碰 data/runtime/ 那一份。
"""

from __future__ import annotations

import datetime as dt
import io
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
pytest.importorskip("sqlalchemy")
pytest.importorskip("bcrypt")
pytest.importorskip("PIL")

from smart_watchdog.bots import linking, mapimage, telegram
from smart_watchdog.db import session as dbsession
from smart_watchdog.db.models import BindCode, User
from smart_watchdog.report.verify import verdict_words
from smart_watchdog.security import hash_password

PAYLOAD = pathlib.Path(__file__).resolve().parents[1] / "dist/data/payload.json"
needs_payload = pytest.mark.skipif(not PAYLOAD.exists(), reason="尚未建置 payload")

EMAIL = "bot-tester@example.gov.tw"
PASSWORD = "correct-horse-battery"
CHAT = 424242


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)
    monkeypatch.setattr(dbsession, "url",
                        lambda: f"sqlite+pysqlite:///{tmp_path / 'bot.sqlite'}")
    dbsession.init_db()
    s = dbsession.session()
    s.add(User(email=EMAIL, name="測試稽查員", role="inspector", unit="新北市政府教育局",
               towns=["板橋區"], password_hash=hash_password(PASSWORD), is_active=True))
    s.commit()
    yield s
    s.close()
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)


@pytest.fixture
def sent(monkeypatch):
    calls: list[dict] = []

    def fake(method, tok, *, files=None, http_timeout=20, **params):
        calls.append({"method": method, "files": files, **params})
        return {"ok": True, "result": {}}

    monkeypatch.setattr(telegram, "call", fake)
    return calls


def _msg(text: str, chat_type: str = "private", chat: int = CHAT) -> dict:
    return {"update_id": 1, "message": {"message_id": 1, "text": text,
                                        "chat": {"id": chat, "type": chat_type}}}


def _button(data: str) -> dict:
    return {"update_id": 2, "callback_query": {
        "id": "cb", "data": data, "message": {"chat": {"id": CHAT, "type": "private"}}}}


def _text(calls: list[dict]) -> str:
    return "\n".join(str(c.get("text") or c.get("caption") or "") for c in calls)


def _user(db) -> User:
    return db.query(User).filter_by(email=EMAIL).one()


def _link(db, sent) -> None:
    code = linking.issue_code(db, _user(db).id, "telegram")
    telegram.handle_update(_msg(f"/start {code}"), "T")
    sent.clear()


@needs_payload
def test_an_unlinked_chat_gets_no_data_at_all(db, sent) -> None:
    from smart_watchdog.api import server

    for update in (_msg("/map"), _msg("/list"), _msg("三重區有哪些幼兒園？"), _button("map")):
        telegram.handle_update(update, "T")
    assert not any(c["method"] == "sendPhoto" for c in sent), "未綁定的聊天室收到了地圖"
    body = _text(sent)
    assert "綁定" in body
    names = [p["n"] for p in server.get_proposal(20)["proposal"]]
    leaked = [n for n in names if n in body]
    assert not leaked, f"未綁定的聊天室拿到了名單：{leaked}"


def test_a_bind_code_works_once_and_expires(db, sent) -> None:
    user = _user(db)
    code = linking.issue_code(db, user.id, "telegram")
    telegram.handle_update(_msg(f"/start {code}"), "T")
    assert linking.user_for(db, "telegram", str(CHAT)) is not None
    assert linking.redeem(db, code, "telegram", "999") is None, "同一組碼用了第二次"

    stale = linking.issue_code(db, user.id, "telegram")
    row = db.get(BindCode, stale)
    row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)
    db.commit()
    assert linking.redeem(db, stale, "telegram", "888") is None, "過期的碼還能用"

    other = linking.issue_code(db, user.id, "line")
    assert linking.redeem(db, other, "telegram", "777") is None, "LINE 的碼被拿去綁 Telegram"


def test_group_chats_are_refused_even_with_a_valid_code(db, sent) -> None:
    code = linking.issue_code(db, _user(db).id, "telegram")
    telegram.handle_update(_msg(f"/start {code}", chat_type="group", chat=-100), "T")
    assert linking.user_for(db, "telegram", "-100") is None
    assert "私訊" in _text(sent)
    # 碼沒有被群組用掉，本人還能在私訊裡用
    assert linking.redeem(db, code, "telegram", str(CHAT)) is not None


@needs_payload
def test_a_linked_chat_gets_the_map_and_the_list(db, sent) -> None:
    _link(db, sent)
    telegram.handle_update(_button("map"), "T")
    assert any(c["method"] == "answerCallbackQuery" for c in sent), "按鈕要回應，不然會一直轉圈"
    photos = [c for c in sent if c["method"] == "sendPhoto"]
    assert photos, "按了地圖卻沒有圖"
    assert photos[0]["files"]["photo"][1][:8] == b"\x89PNG\r\n\x1a\n"
    body = _text(sent)
    assert "非違法認定" in body
    assert verdict_words(body) == [], verdict_words(body)


def test_unlinking_and_deactivation_cut_access_immediately(db, sent) -> None:
    _link(db, sent)
    telegram.handle_update(_msg("/unbind"), "T")
    assert linking.user_for(db, "telegram", str(CHAT)) is None

    _link(db, sent)
    user = _user(db)
    user.is_active = False
    db.commit()
    assert linking.user_for(db, "telegram", str(CHAT)) is None, "帳號停用後聊天室仍有存取"


def test_the_token_never_leaks_into_error_messages(monkeypatch) -> None:
    secret = "123456:SECRET-TOKEN"

    def boom(*args, **kwargs):
        raise httpx.ConnectError(f"failed https://api.telegram.org/bot{secret}/getUpdates")

    monkeypatch.setattr(httpx, "post", boom)
    result = telegram.call("getUpdates", secret)
    assert result["ok"] is False
    assert secret not in result["description"], "token 出現在會送到 /api/health 的錯誤訊息裡"


@needs_payload
def test_the_map_renders_as_a_portrait_png() -> None:
    from PIL import Image

    from smart_watchdog.api import server

    data = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    png = mapimage.render(data["points"], data["boundary"], server.get_proposal(20)["proposal"])
    assert Image.open(io.BytesIO(png)).size == (mapimage.W, mapimage.H)


@needs_payload
def test_the_bind_code_endpoint_requires_login(db, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from smart_watchdog.api.server import app

    monkeypatch.setattr(telegram, "token", lambda: "T")
    monkeypatch.setattr(telegram.STATE, "username", "Little_Guardian_bot")
    client = TestClient(app)
    assert client.post("/api/bot/telegram/bind-code").status_code == 401

    login = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200
    r = client.post("/api/bot/telegram/bind-code")
    assert r.status_code == 200
    body = r.json()
    assert body["deep_link"] == f"https://t.me/Little_Guardian_bot?start={body['code']}"
    assert body["expires_in"] == 600


def test_the_demo_passcode_links_in_one_tap_and_nothing_else_does(db, sent, monkeypatch) -> None:
    """示範暗號：點 t.me/<bot>?start=<暗號> 就綁好。錯的暗號、沒設暗號都不行。

    這條路是為了決賽展示方便而開的，所以要釘住它**只開到這裡**：猜錯拿不到、
    .env 裡沒設時整條路不存在、群組裡照樣拒絕。
    """
    from smart_watchdog import config

    real_get = config.get

    def fake_get(key, default=None):
        if key == "TELEGRAM_DEMO_PASSCODE":
            return "Demo-Pass_123"
        if key == "SEED_INSPECTOR_EMAIL":
            return EMAIL
        return real_get(key, default)

    monkeypatch.setattr(config, "get", fake_get)

    telegram.handle_update(_msg("/start wrong-guess"), "T")
    assert linking.user_for(db, "telegram", str(CHAT)) is None, "猜錯的暗號綁上了"

    telegram.handle_update(_msg("/start Demo-Pass_123", chat_type="group", chat=-5), "T")
    assert linking.user_for(db, "telegram", "-5") is None, "群組用暗號綁上了"

    telegram.handle_update(_msg("/start Demo-Pass_123"), "T")
    user = linking.user_for(db, "telegram", str(CHAT))
    assert user is not None and user.email == EMAIL


def test_without_a_passcode_configured_the_demo_path_does_not_exist(db, sent, monkeypatch) -> None:
    from smart_watchdog import config

    real_get = config.get
    monkeypatch.setattr(config, "get", lambda key, default=None:
                        None if key == "TELEGRAM_DEMO_PASSCODE" else real_get(key, default))
    telegram.handle_update(_msg("/start "), "T")
    telegram.handle_update(_msg("/start anything"), "T")
    assert linking.user_for(db, "telegram", str(CHAT)) is None


def test_typing_bind_or_pasting_the_code_also_links(db, sent) -> None:
    """深層連結在已經開過對話時可能帶不到碼（實際發生：雲端收到兩則空的「開始」）。

    所以另外兩種說法也要能綁：照網頁上的字打「綁定 碼」，或只把碼貼上來。
    """
    user = _user(db)
    telegram.handle_update(_msg(f"綁定 {linking.issue_code(db, user.id, 'telegram')}"), "T")
    assert linking.user_for(db, "telegram", str(CHAT)) is not None

    linking.unlink(db, "telegram", str(CHAT))
    telegram.handle_update(_msg(linking.issue_code(db, user.id, "telegram")), "T")
    assert linking.user_for(db, "telegram", str(CHAT)) is not None, "只貼碼沒有綁上"


def test_a_pasted_wrong_code_still_gets_only_instructions(db, sent) -> None:
    telegram.handle_update(_msg("not-a-real-code"), "T")
    assert linking.user_for(db, "telegram", str(CHAT)) is None
    assert "綁定" in _text(sent)


def test_the_unlinked_message_points_to_a_button_that_exists() -> None:
    """說明文字叫人去身分卡按「綁定 Telegram」，那顆鈕就必須真的在。

    第一版寫了這句話卻沒做那顆鈕，使用者登入後點開身分卡找不到——說明文字
    指向不存在的東西，比沒有說明更糟。
    """
    import re

    webapp = pathlib.Path(__file__).resolve().parents[1] / "webapp"
    html = (webapp / "index.html").read_text(encoding="utf-8")
    assert "身分卡" in telegram.UNLINKED and "綁定 Telegram" in telegram.UNLINKED
    buttons = re.findall(r"<button[^>]*\bdata-tgbind\b[^>]*>\s*綁定 Telegram", html)
    assert len(buttons) == 2, f"中庭與房間內兩張身分卡都要有綁定鈕，找到 {len(buttons)} 顆"
    assert '<script src="/static/botbind.js"></script>' in html
    js = (webapp / "botbind.js").read_text(encoding="utf-8")
    assert "/api/bot/telegram/bind-code" in js
