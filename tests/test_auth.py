"""登入的契約：會話能建立、能失效，且失敗不洩漏帳號是否存在。

離線與隔離保證：`db` fixture 把引擎指到 `tmp_path` 下的一個 SQLite 檔，並在
前後都清掉 `db.session` 的模組級單例。因此**不會碰到
`data/runtime/watchdog.sqlite`**——那裡有真實的密碼雜湊與對話稽核軌跡。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("sqlalchemy")
pytest.importorskip("bcrypt")

from fastapi.testclient import TestClient

from smart_watchdog.api.auth import COOKIE_NAME
from smart_watchdog.api.server import app
from smart_watchdog.db import session as dbsession
from smart_watchdog.db.models import User, UserSession
from smart_watchdog.security import hash_password

EMAIL = "tester@example.gov.tw"
PASSWORD = "correct-horse-battery"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)
    monkeypatch.setattr(
        dbsession, "url", lambda: f"sqlite+pysqlite:///{tmp_path / 'test.sqlite'}"
    )
    dbsession.init_db()
    s = dbsession.session()
    s.add(User(
        email=EMAIL, name="測試稽查員", role="inspector",
        unit="新北市政府教育局", towns=["板橋區"],
        password_hash=hash_password(PASSWORD), is_active=True,
    ))
    s.commit()
    yield s
    s.close()
    monkeypatch.setattr(dbsession, "_engine", None, raising=False)
    monkeypatch.setattr(dbsession, "_Session", None, raising=False)


@pytest.fixture
def client(db):
    # 只為相依而要求：`db` 建好表與帳號，這裡不需要它的值。
    del db
    return TestClient(app)


def _login(client, email=EMAIL, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def test_me_without_cookie_is_401(client) -> None:
    assert client.get("/api/auth/me").status_code == 401


def test_login_sets_cookie_and_me_returns_the_user(client) -> None:
    r = _login(client)
    assert r.status_code == 200
    assert r.json()["email"] == EMAIL
    assert COOKIE_NAME in r.cookies

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["towns"] == ["板橋區"]


def test_wrong_password_is_401_and_creates_no_session(client, db) -> None:
    assert _login(client, password="nope").status_code == 401
    assert db.query(UserSession).count() == 0


def test_unknown_account_is_indistinguishable_from_wrong_password(client) -> None:
    """兩者都必須是 401 且訊息相同，否則可以用來列舉真實的稽查員 Email。"""
    unknown = _login(client, email="nobody@example.com", password="nope")
    wrong = _login(client, password="nope")
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json()["detail"] == wrong.json()["detail"]


def test_inactive_user_cannot_log_in(client, db) -> None:
    db.query(User).filter(User.email == EMAIL).one().is_active = False
    db.commit()
    assert _login(client).status_code == 401


def test_logout_invalidates_the_session_row(client, db) -> None:
    _login(client)
    assert db.query(UserSession).count() == 1

    assert client.post("/api/auth/logout").status_code == 204
    db.expire_all()
    assert db.query(UserSession).count() == 0
    assert client.get("/api/auth/me").status_code == 401


def test_expired_session_is_rejected(client, db) -> None:
    """過期的列不會被自動刪掉，所以要確認是「比較時間」而不是「查得到就放行」。"""
    import datetime as dt

    _login(client)
    row = db.query(UserSession).one()
    row.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
    db.commit()
    assert client.get("/api/auth/me").status_code == 401
