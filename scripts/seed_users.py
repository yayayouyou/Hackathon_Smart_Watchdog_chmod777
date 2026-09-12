"""建表並建立兩種固定身分帳號；冪等，重跑會把帳號更新回這份規格。

    python run.py seed-users
    python run.py seed-users -- --reset

帳密取自 `.env`，每個身分各有一組變數：

    SEED_INSPECTOR_EMAIL / SEED_INSPECTOR_PASSWORD   role=inspector
    SEED_ADMIN_EMAIL     / SEED_ADMIN_PASSWORD       role=admin

每組缺一就跳過該帳號並明白列出缺項；不提供寫死的預設密碼。兩組 Email 必須不同，
否則第二個身分會覆蓋第一個固定角色，腳本會在碰資料庫前直接拒絕執行。

`--reset` 只刪除 user 與 agent 的五張開發資料表，再交由 `init_db()` 重建；它不會刪
`threads_mention` 等持續累積的外部通報資料。這仍只適用本機開發：正式資料庫的 schema
變更必須使用 migration，不能靠 drop。
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from smart_watchdog import config
from smart_watchdog.console import use_utf8
from smart_watchdog.db.models import (
    AgentMessage,
    AgentSession,
    AuditFeedback,
    User,
    UserSession,
)
from smart_watchdog.db.session import engine, init_db, session, url
from smart_watchdog.security import hash_password

use_utf8()


@dataclass(frozen=True)
class Seed:
    """一個示範帳號的規格。`env_prefix` 決定它讀 `.env` 的哪兩個變數。"""

    env_prefix: str
    name: str
    role: str
    unit: str
    towns: list[str] = field(default_factory=list)

    @property
    def email_key(self) -> str:
        return f"SEED_{self.env_prefix}_EMAIL"

    @property
    def password_key(self) -> str:
        return f"SEED_{self.env_prefix}_PASSWORD"


# 兩種身分目前同功能、同資料範圍；空 towns 代表預設查詢全新北市。
ACCOUNTS = [
    Seed(
        env_prefix="INSPECTOR",
        name="示範稽查人員",
        role="inspector",
        unit="新北市政府教育局",
        towns=[],
    ),
    Seed(
        env_prefix="ADMIN",
        name="系統管理員",
        role="admin",
        unit="新北市政府教育局",
        towns=[],
    ),
]

# 依外鍵相依順序由子表往父表刪；ThreadsMention 刻意不在清單裡。
AUTH_MODELS_IN_DROP_ORDER = (
    AuditFeedback,
    AgentMessage,
    AgentSession,
    UserSession,
    User,
)


def _duplicate_email() -> tuple[str, str, str] | None:
    seen: dict[str, str] = {}
    for spec in ACCOUNTS:
        raw = config.get(spec.email_key)
        if not raw:
            continue
        email = raw.strip().lower()
        previous = seen.get(email)
        if previous:
            return email, previous, spec.role
        seen[email] = spec.role
    return None


def _reset_auth_tables() -> None:
    bind = engine()
    for model in AUTH_MODELS_IN_DROP_ORDER:
        model.__table__.drop(bind, checkfirst=True)


def upsert(db, spec: Seed) -> str | None:
    """建立或更新一個固定身分帳號。缺帳密就回 None，由呼叫端列出缺項。"""
    email = config.get(spec.email_key)
    password = config.get(spec.password_key)
    if not email or not password:
        return None

    email = email.strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        db.add(User(
            email=email,
            name=spec.name,
            role=spec.role,
            unit=spec.unit,
            towns=list(spec.towns),
            password_hash=hash_password(password),
            is_active=True,
        ))
        action = "建立"
    else:
        # seed 是這兩個展示帳號的規格真相；重跑時同步固定角色與全市範圍。
        user.name = spec.name
        user.role = spec.role
        user.unit = spec.unit
        user.towns = list(spec.towns)
        user.password_hash = hash_password(password)
        user.is_active = True
        action = "更新"
    return f"{action}帳號 {email}｜身分 {spec.role}｜全市"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="只重建帳號與 agent 五張表；保留 Threads 等外部通報資料",
    )
    args = parser.parse_args()

    config.load_env()
    duplicate = _duplicate_email()
    if duplicate:
        email, first_role, second_role = duplicate
        print(
            f"✗ {first_role} 與 {second_role} 不可共用 Email：{email}\n"
            "  請修正 .env 後再執行；資料庫尚未變更。"
        )
        return 2

    print(f"資料庫：{url()}")
    if args.reset:
        _reset_auth_tables()
        print("  ⚠️ 已重建帳號與 agent 五張表；Threads 外部通報資料保留")
    init_db()
    print(
        "  ✓ 帳號與 agent 五張表就緒（user、user_session、agent_session、"
        "agent_message、audit_feedback）"
    )

    db = session()
    try:
        results = [(spec, upsert(db, spec)) for spec in ACCOUNTS]
        db.commit()
    finally:
        db.close()

    print()
    for spec, line in results:
        if line is None:
            print(
                f"  ⬜ 未建 {spec.role} 帳號：.env 缺 "
                f"{spec.email_key} 或 {spec.password_key}"
            )
        else:
            print(f"  ✓ {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
