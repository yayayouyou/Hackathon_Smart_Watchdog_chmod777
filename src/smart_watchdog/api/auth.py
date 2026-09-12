"""登入、登出、我是誰，以及展示環境的受控快速登入。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/routers/auth.py` 與 `deps.py`，
本專案把路由統一在 `/api/...`，並把唯一的登入依賴放在同一支檔案。

**cookie 能成立的前提是同源。** 前端由這個 FastAPI 自己提供（`/` 掛 `webapp/`），
所以 `SameSite=Lax` 就夠。哪天前後端拆成兩個網域，就要改成
`SameSite=None; Secure` 並收斂 CORS；`.env` 的 `COOKIE_SAMESITE` 與
`COOKIE_SECURE` 是為那一天保留的。

角色目前是身分描述，不是權限邊界：inspector 與 admin 使用相同端點、介面與功能。
一般登入可帶 role，後端只用它核對資料庫中的固定角色，絕不採信前端自行升級角色。
快速登入則是刻意的免密碼展示入口，必須同時設定 `QUICK_LOGIN_ENABLED=true` 與
對應 seed 帳號；正式環境預設關閉，密碼也不會送進前端。
"""

from __future__ import annotations

import datetime as dt
from typing import Literal, Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import User, UserSession
from ..db.session import get_db
from ..security import DUMMY_HASH, new_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "wd_session"
SESSION_TTL_HOURS = 12
LoginRole = Literal["inspector", "admin"]
QUICK_LOGIN_EMAIL_KEYS: dict[LoginRole, str] = {
    "inspector": "SEED_INSPECTOR_EMAIL",
    "admin": "SEED_ADMIN_EMAIL",
}
_TRUE_VALUES = {"1", "true", "yes", "on"}


def _cookie_flags() -> dict:
    samesite = (config.get("COOKIE_SAMESITE") or "lax").lower()
    secure = (config.get("COOKIE_SECURE") or "false").lower() == "true"
    return {"samesite": samesite, "secure": secure}


def _quick_login_enabled() -> bool:
    return (config.get("QUICK_LOGIN_ENABLED") or "false").strip().lower() in _TRUE_VALUES


class LoginRequest(BaseModel):
    email: str
    password: str
    # Optional keeps existing API clients compatible. The web UI always sends it and the
    # server verifies it against User.role; the request can never choose a different role.
    role: LoginRole | None = None


class QuickLoginRequest(BaseModel):
    role: LoginRole


class AuthOptionsOut(BaseModel):
    quick_login_enabled: bool
    quick_login_roles: list[LoginRole] = Field(default_factory=list)


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    role: str
    unit: Optional[str] = None
    towns: list = Field(default_factory=list)

    model_config = {"from_attributes": True}


def _configured_quick_user(db: Session, role: LoginRole) -> User | None:
    email = config.get(QUICK_LOGIN_EMAIL_KEYS[role])
    if not email:
        return None
    user = db.execute(
        select(User).where(User.email == email.strip().lower())
    ).scalar_one_or_none()
    if user is None or not user.is_active or user.role != role:
        return None
    return user


def _start_session(user: User, response: Response, db: Session) -> User:
    token = new_token()
    db.add(UserSession(
        token=token,
        user_id=user.id,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=SESSION_TTL_HOURS),
    ))
    db.commit()
    response.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        max_age=SESSION_TTL_HOURS * 3600,
        **_cookie_flags(),
    )
    return user


def get_current_user(
    db: Session = Depends(get_db),
    wd_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> User:
    if not wd_session:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登入")
    sess = db.execute(
        select(UserSession).where(UserSession.token == wd_session)
    ).scalar_one_or_none()
    now = dt.datetime.now(dt.UTC)
    expires = sess.expires_at if sess else None
    # SQLite 讀回來的 datetime 沒有 tzinfo，直接比較會 TypeError。
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.UTC)
    if sess is None or expires < now or not sess.user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登入")
    return sess.user


@router.get("/options", response_model=AuthOptionsOut)
def auth_options(db: Session = Depends(get_db)) -> AuthOptionsOut:
    """告訴登入頁哪些快速入口真的可用；不回傳帳號或任何密碼。"""
    enabled = _quick_login_enabled()
    roles = [
        role for role in QUICK_LOGIN_EMAIL_KEYS
        if enabled and _configured_quick_user(db, role) is not None
    ]
    return AuthOptionsOut(quick_login_enabled=enabled, quick_login_roles=roles)


@router.post("/login", response_model=UserOut)
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)) -> User:
    email = body.email.strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    # 帳號不存在時仍走一次雜湊比對，否則回應時間會洩漏哪些 Email 是真帳號。
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    role_matches = user is not None and (body.role is None or user.role == body.role)
    if user is None or not user.is_active or not ok or not role_matches:
        # 角色不符也回同一句，避免藉 selector 探測某個 Email 的真實角色。
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "帳號或密碼錯誤")
    return _start_session(user, response, db)


@router.post("/quick-login", response_model=UserOut)
def quick_login(
    body: QuickLoginRequest,
    response: Response,
    db: Session = Depends(get_db),
) -> User:
    if not _quick_login_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "快速登入未啟用")
    user = _configured_quick_user(db, body.role)
    if user is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "快速登入帳號尚未建立或身分設定不符",
        )
    return _start_session(user, response, db)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    db: Session = Depends(get_db),
    wd_session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME),
) -> None:
    if wd_session:
        db.execute(delete(UserSession).where(UserSession.token == wd_session))
        db.commit()
    response.delete_cookie(COOKIE_NAME)


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> User:
    return user
