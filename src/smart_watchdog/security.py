"""密碼雜湊與權杖。純函式，不碰 FastAPI，也不碰資料庫。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/security.py`，原樣保留。

`DUMMY_HASH` 不是裝飾：帳號不存在時仍要走一次 `verify_password`，否則
「查無此帳號」會比「密碼錯誤」快一個數量級，可以被用來列舉出哪些 Email
是真的稽查員帳號。
"""

from __future__ import annotations

import secrets

import bcrypt


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode()


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(raw.encode(), hashed.encode())
    except ValueError:
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


DUMMY_HASH = hash_password("dummy-password-for-timing")
