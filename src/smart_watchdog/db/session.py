"""引擎與 session。連線字串從 `.env` 的 `DATABASE_URL` 來，走既有的 `config.get()`。

本機是 SQLite，檔案落在 `data/runtime/`——那個目錄已經 gitignore，而這個資料庫裡
有密碼雜湊與對話稽核軌跡，**不該進版控**。

SQLite 的兩個設定不是風格偏好：

- `check_same_thread=False`：FastAPI 的同步端點跑在 threadpool，每個請求可能落在
  不同的 thread。不關掉這個檢查，第二個請求就會炸。
- `PRAGMA foreign_keys=ON`：SQLite **預設不檢查外鍵**。不開的話
  `agent_message.session_id` 可以指向不存在的 session，稽核軌跡會出現孤兒列。
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .. import config
from .models import Base

ROOT = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_URL = "sqlite+pysqlite:///data/runtime/watchdog.sqlite"

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def url() -> str:
    return config.get("DATABASE_URL") or DEFAULT_URL


def engine() -> Engine:
    global _engine, _Session
    if _engine is not None:
        return _engine

    u = url()
    kwargs: dict = {"future": True}
    if u.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # 相對路徑要相對於專案根目錄，不是相對於「啟動服務時所在的資料夾」。
        prefix = "sqlite+pysqlite:///"
        if u.startswith(prefix) and not u.startswith(prefix + "/") and ":memory:" not in u:
            path = ROOT / u[len(prefix):]
            path.parent.mkdir(parents=True, exist_ok=True)
            u = prefix + str(path)

    _engine = create_engine(u, **kwargs)

    if _engine.dialect.name == "sqlite":
        @event.listens_for(_engine, "connect")
        def _fk_on(dbapi_conn, _record):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def init_db() -> None:
    """建表。沒有 alembic，理由見 `models.py` 的模組說明第 3 點。"""
    Base.metadata.create_all(engine())


def get_db() -> Iterator[Session]:
    """FastAPI 依賴：一個請求一個 session。"""
    engine()
    assert _Session is not None
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def session() -> Session:
    """給腳本與測試用的 session（不經過 FastAPI 的依賴注入）。"""
    engine()
    assert _Session is not None
    return _Session()
