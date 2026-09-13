"""LINE bot 的契約：webhook 驗簽、沒綁定沒資料、群組拒絕、地圖只給簽章網址。

LINE 的 webhook 網址是公開的——任何人都能對它 POST 一個看起來像 LINE 的事件。
所以這支比 Telegram 那支多釘兩件事：簽章驗不過就不處理，以及那張地圖不能變成
一個誰都下載得到的固定網址。

不打真的 LINE：`line.call` 換成記錄呼叫的假函式；憑證一律用 monkeypatch 蓋掉
`config.get`，不讀 .env 裡的真值。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import pathlib
import sys
import time
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("sqlalchemy")
pytest.importorskip("bcrypt")
pytest.importorskip("PIL")

from smart_watchdog.bots import line, linking
from smart_watchdog.db import session as dbsession
from smart_watchdog.db.models import User
from smart_watchdog.report.verify import verdict_words
from smart_watchdog.security import hash_password

PAYLOAD = pathlib.Path(__file__).resolve().parents[1] / "dist/data/payload.json"
needs_payload = pytest.mark.skipif(not PAYLOAD.exists(), reason="尚未建置 payload")

EMAIL = "line-tester@example.gov.tw"
SECRET = "test-channel-secret"
PASS = "Demo-Pass_123"


def _env(monkeypatch, **values) -> None:
    from smart_watchdog import config

    real = config.get

    def fake(key, default=None):
        if key in values:
            return values[key]
        return real(key, default)

    monkeypatch.setattr(config, "get", fake)


@pytest.fixture
def configured(monkeypatch):
    _env(monkeypatch, LINE_CHANNEL_SECRET=SECRET, LINE_CHANNEL_ACCESS_TOKEN="test-token",
         LINE_CHANNEL_ID="123", LINE_DEMO_PASSCODE=PASS, SEED_INSPECTOR_EMAIL=EMAIL,
         PUBLIC_BASE_URL="https://demo.example")


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)
    monkeypatch.setattr(dbsession, "url",
                        lambda: f"sqlite+pysqlite:///{tmp_path / 'line.sqlite'}")
    dbsession.init_db()
    s = dbsession.session()
    s.add(User(email=EMAIL, name="測試稽查員", role="inspector", unit="新北市政府教育局",
               towns=["板橋區"], password_hash=hash_password("x-password-123"),
               is_active=True))
    s.commit()
    yield s
    s.close()
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)


@pytest.fixture
def replies(monkeypatch):
    calls: list[dict] = []
    def fake_call(path, payload):
        _ = path                    # 簽名要與 line.call(path, payload) 一致
        calls.append(payload)
        return {}

    monkeypatch.setattr(line, "call", fake_call)
    return calls


def _ev(kind: str = "message", text: str | None = None, data: str | None = None,
        src: str = "user", uid: str = "U1") -> dict:
    ev: dict = {"type": kind, "replyToken": "rt", "source": {"type": src, "userId": uid}}
    if kind == "message":
        ev["message"] = {"type": "text", "text": text}
    if kind == "postback":
        ev["postback"] = {"data": data}
    return ev


def _messages(calls: list[dict]) -> list[dict]:
    return [m for c in calls for m in c.get("messages", [])]


def _texts(calls: list[dict]) -> str:
    return "\n".join(m.get("text", "") for m in _messages(calls))


def _sig(body: bytes, key: str = SECRET) -> str:
    return base64.b64encode(hmac.new(key.encode(), body, hashlib.sha256).digest()).decode()


@pytest.mark.usefixtures("configured")
def test_the_webhook_rejects_a_forged_signature(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from smart_watchdog.api.server import app

    seen: list = []
    monkeypatch.setattr(line, "dispatch_async", lambda events: seen.append(events))
    client = TestClient(app)
    body = json.dumps({"events": [_ev(text="地圖")]}).encode()

    bad = client.post(line.WEBHOOK_PATH, content=body,
                      headers={"X-Line-Signature": _sig(body, "someone-else")})
    assert bad.status_code == 400
    assert not seen, "簽章不對的事件被處理了"

    ok = client.post(line.WEBHOOK_PATH, content=body, headers={"X-Line-Signature": _sig(body)})
    assert ok.status_code == 200
    assert seen and seen[0][0]["message"]["text"] == "地圖"


def test_an_unconfigured_webhook_quietly_accepts_and_does_nothing(monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from smart_watchdog.api.server import app

    _env(monkeypatch, LINE_CHANNEL_SECRET=None, LINE_CHANNEL_ACCESS_TOKEN=None,
         LINE_CHANNEL_ID=None)
    seen: list = []
    monkeypatch.setattr(line, "dispatch_async", lambda events: seen.append(events))
    r = TestClient(app).post(line.WEBHOOK_PATH, content=b'{"events":[{"type":"message"}]}')
    assert r.status_code == 200 and not seen


@needs_payload
@pytest.mark.usefixtures("configured", "db")
def test_an_unlinked_line_user_gets_no_data(replies) -> None:
    from smart_watchdog.api import server

    for ev in (_ev(text="地圖"), _ev(kind="postback", data="list"),
               _ev(text="三重區有哪些？")):
        line.handle_event(ev)
    assert not any(m["type"] == "image" for m in _messages(replies))
    body = _texts(replies)
    assert "綁定" in body
    names = [p["n"] for p in server.get_proposal(20)["proposal"]]
    assert not [n for n in names if n in body], "未綁定的 LINE 使用者拿到了名單"


@pytest.mark.usefixtures("configured")
def test_groups_are_refused_even_with_the_passcode(db, replies) -> None:
    line.handle_event(_ev(text=f"綁定 {PASS}", src="group"))
    assert linking.user_for(db, "line", "U1") is None
    assert "一對一" in _texts(replies)


@pytest.mark.usefixtures("configured", "replies")
def test_a_wrong_passcode_does_not_link(db) -> None:
    line.handle_event(_ev(text="綁定 guess-guess"))
    assert linking.user_for(db, "line", "U1") is None


@needs_payload
@pytest.mark.usefixtures("configured")
def test_the_map_goes_out_as_a_signed_short_lived_image(db, replies) -> None:
    line.handle_event(_ev(text=f"綁定 {PASS}"))
    assert linking.user_for(db, "line", "U1") is not None
    replies.clear()

    line.handle_event(_ev(kind="postback", data="map"))
    msgs = _messages(replies)
    image = next(m for m in msgs if m["type"] == "image")
    url = urlparse(image["originalContentUrl"])
    assert (url.scheme, url.netloc, url.path) == (
        "https", "demo.example", "/api/bot/map.png")
    q = parse_qs(url.query)
    assert line.check_map_signature(q["exp"][0], q["sig"][0])
    body = _texts(replies)
    assert "非違法認定" in body and verdict_words(body) == []


@needs_payload
@pytest.mark.usefixtures("db")
def test_without_a_public_url_the_map_falls_back_to_text(monkeypatch, replies) -> None:
    _env(monkeypatch, LINE_CHANNEL_SECRET=SECRET, LINE_CHANNEL_ACCESS_TOKEN="t",
         LINE_DEMO_PASSCODE=PASS, SEED_INSPECTOR_EMAIL=EMAIL, PUBLIC_BASE_URL=None)
    line.handle_event(_ev(text=f"綁定 {PASS}"))
    replies.clear()
    line.handle_event(_ev(kind="postback", data="map"))
    assert not any(m["type"] == "image" for m in _messages(replies))
    assert "公開" in _texts(replies)


@pytest.mark.usefixtures("configured")
def test_the_map_signature_expires_and_rejects_tampering() -> None:
    exp = int(time.time()) + 60
    good = line._sign(exp)
    assert line.check_map_signature(str(exp), good)
    tampered = good[:-1] + ("A" if good[-1] != "A" else "B")
    assert not line.check_map_signature(str(exp), tampered)
    assert not line.check_map_signature(str(exp + 1), good), "換了到期時間簽章還能用"
    past = int(time.time()) - 1
    assert not line.check_map_signature(str(past), line._sign(past)), "過期的地圖網址還能用"
    far = int(time.time()) + 10 * 24 * 3600
    assert not line.check_map_signature(str(far), line._sign(far)), (
        "自己簽一個很久以後的到期時間")


@needs_payload
@pytest.mark.usefixtures("configured")
def test_the_signed_map_route_serves_png_only_with_a_valid_signature() -> None:
    from fastapi.testclient import TestClient

    from smart_watchdog.api.server import app

    client = TestClient(app)
    assert client.get("/api/bot/map.png").status_code == 403
    assert client.get("/api/bot/map.png?exp=1&sig=x").status_code == 403
    q = parse_qs(urlparse(line.map_url()).query)
    r = client.get(f"/api/bot/map.png?exp={q['exp'][0]}&sig={q['sig'][0]}")
    assert r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.usefixtures("configured")
def test_credentials_never_leak_into_error_messages(monkeypatch) -> None:
    import httpx

    def boom(*_args, **_kwargs):
        raise httpx.ConnectError(f"auth failed for {SECRET} / test-token")

    monkeypatch.setattr(httpx, "post", boom)
    line.call("/message/reply", {"replyToken": "rt", "messages": []})
    assert SECRET not in line.STATE.last_error and "test-token" not in line.STATE.last_error
