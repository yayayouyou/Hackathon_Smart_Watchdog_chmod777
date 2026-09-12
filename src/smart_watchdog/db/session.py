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


def _add_column_sql(table: str, column, dialect) -> str:
    """一句 `ALTER TABLE ... ADD COLUMN`，或者拒絕。

    只接受兩種欄位：可為 NULL 的，以及帶 `server_default` 的。其餘（NOT NULL
    又沒有預設值）在既有列上無解——SQLite 會直接拒絕，而隨手塞一個預設值就是
    替既有資料編造內容。那種變更要人來決定，所以這裡丟例外而不是安靜跳過。
    """
    spec = f'ALTER TABLE "{table}" ADD COLUMN "{column.name}" {column.type.compile(dialect)}'
    default = getattr(column.server_default, "arg", None)
    if column.nullable:
        return spec
    if isinstance(default, str):
        return f"{spec} NOT NULL DEFAULT '{default}'"
    raise RuntimeError(
        f"{table}.{column.name} 是 NOT NULL 又沒有 server_default，"
        "無法自動補到既有表上——既有列要填什麼必須由人決定。")


def _add_missing_columns(eng: Engine) -> list[str]:
    """替既有表補上後來才加的欄位，回傳實際執行的 DDL。

    `create_all()` 只建**還不存在的表**；既有表少了欄位它一句話也不會說，
    於是第一個 SELECT 才會以 `no such column` 炸掉，而那時人已經在 demo 了。

    沒有 alembic（`models.py` 說明第 3 點），而這裡實際需要的一直是同一種變更：
    加一個新欄位。所以只做加法——不改型別、不改可否為空、不刪任何東西，
    既有的值一個都不會被碰到。真正的 schema 演進（改型別、搬資料）不在這裡做。
    """
    from sqlalchemy import inspect as sa_inspect

    inspector = sa_inspect(eng)
    existing = set(inspector.get_table_names())
    applied: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue        # create_all 剛建的，欄位必然齊全
        have = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in have:
                continue
            sql = _add_column_sql(table.name, column, eng.dialect)
            with eng.begin() as conn:
                conn.exec_driver_sql(sql)
            applied.append(sql)
    return applied


def init_db() -> None:
    """建表，並替既有表補上新欄位。沒有 alembic，理由見 `models.py` 說明第 3 點。"""
    eng = engine()
    Base.metadata.create_all(eng)
    for sql in _add_missing_columns(eng):
        # 動到既有資料庫的事情不該安靜發生，即使只是加一欄。
        print(f"[db] {sql}")


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
