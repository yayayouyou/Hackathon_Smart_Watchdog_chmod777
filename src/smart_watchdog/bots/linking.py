"""聊天室 ↔ 派工台帳號的綁定。**bot 端所有資料存取的唯一閘門。**

流程只有一條，而且起點一定在派工台：

    已登入的使用者 → POST /api/bot/telegram/bind-code → 一次性碼（10 分鐘、一次）
    → 點 t.me/<bot>?start=<碼> → bot 收到 /start <碼> → redeem() → ChatLink

反方向不存在：bot 端沒有任何路徑可以用 Email、姓名或「我是某某稽查員」建立綁定。
一個公開搜得到的 bot，任何能被聊天室裡的人自己說出口的憑據，都等於沒有憑據。

`user_for()` 每次都重新讀 `User.is_active`，不快取。帳號在派工台停用的那一刻，
聊天室就拿不到資料了——不必有人記得去解綁。
"""

from __future__ import annotations

import datetime as dt
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import BindCode, ChatLink, User

CODE_TTL = dt.timedelta(minutes=10)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _aware(value: dt.datetime) -> dt.datetime:
    # SQLite 讀回來是 naive datetime（與 auth.py 同一個處理）。
    return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)


def issue_code(db: Session, user_id: int, platform: str) -> str:
    code = secrets.token_urlsafe(16)
    db.add(BindCode(code=code, user_id=user_id, platform=platform,
                    expires_at=_now() + CODE_TTL))
    db.commit()
    return code


def redeem(db: Session, code: str, platform: str, chat_id: str) -> User | None:
    """用掉一組碼並綁定。無效、過期、用過、平台不符、帳號停用——一律回 None。

    同一個聊天室再綁一次會改綁到新帳號（換人使用同一支手機的情境），不會留下兩列。
    """
    row = db.get(BindCode, str(code or "").strip())
    if (row is None or row.platform != platform or row.used_at is not None
            or _aware(row.expires_at) < _now()):
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    row.used_at = _now()
    return link(db, user, platform, chat_id)


def link(db: Session, user: User, platform: str, chat_id: str) -> User:
    """建立或改綁。一個聊天室只對應一位使用者。"""
    row = db.execute(select(ChatLink).where(
        ChatLink.platform == platform, ChatLink.chat_id == str(chat_id))).scalar_one_or_none()
    if row is None:
        db.add(ChatLink(platform=platform, chat_id=str(chat_id), user_id=user.id))
    else:
        row.user_id = user.id
    db.commit()
    return user


def demo_user(db: Session, email: str | None) -> User | None:
    """示範暗號綁定到哪個帳號：優先用 SEED_INSPECTOR_EMAIL，否則第一個啟用中的稽查人員。"""
    if email:
        user = db.execute(select(User).where(
            User.email == email.strip().lower())).scalar_one_or_none()
        if user is not None and user.is_active:
            return user
    return db.execute(select(User).where(
        User.is_active.is_(True), User.role == "inspector",
    ).order_by(User.id)).scalars().first()


def user_for(db: Session, platform: str, chat_id: str) -> User | None:
    link = db.execute(select(ChatLink).where(
        ChatLink.platform == platform, ChatLink.chat_id == str(chat_id))).scalar_one_or_none()
    if link is None:
        return None
    user = db.get(User, link.user_id)
    return user if user is not None and user.is_active else None


def unlink(db: Session, platform: str, chat_id: str) -> bool:
    link = db.execute(select(ChatLink).where(
        ChatLink.platform == platform, ChatLink.chat_id == str(chat_id))).scalar_one_or_none()
    if link is None:
        return False
    db.delete(link)
    db.commit()
    return True


def is_linked(db: Session, user_id: int, platform: str) -> bool:
    return db.execute(select(ChatLink.id).where(
        ChatLink.platform == platform, ChatLink.user_id == user_id)).first() is not None
