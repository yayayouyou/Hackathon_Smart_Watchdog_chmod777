"""登入、登出、我是誰。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/routers/auth.py` 與 `deps.py`，
兩處改動：

1. **前綴對齊本專案。** 原本是 `/api/v1/auth`，全站其他端點都是 `/api/...`，統一。
2. **`get_current_user` 併進這支檔案。** 原專案放在獨立的 `deps.py`；這裡只有一個
   依賴，拆成兩檔沒有好處。

**cookie 能成立的前提是同源。** 前端由這個 FastAPI 自己提供（`/` 掛 `webapp/`），
所以 `SameSite=Lax` 就夠，`CORSMiddleware` 的 `allow_origins=["*"]` 也不衝突
（它沒有開 `allow_credentials`）。哪天前後端拆成兩個網域，就要改成
`SameSite=None; Secure` 並收斂 `allow_origins`——`.env` 的 `COOKIE_SAMESITE`
與 `COOKIE_SECURE` 就是為那一天留的。

為什麼要有帳號：`record_feedback` 記錄的是「**哪一位**稽查員認同這條建議」，
沒有身分那張人在迴圈的資料集就無從回溯；而治理文件要求個別機構分數不對外
公開揭露，有登入才能主張這是內部系統。
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .. import config
from ..db.models import User, UserSession
from ..db.session import get_db
from ..security import DUMMY_HASH, new_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_NAME = "wd_session"
SESSION_TTL_HOURS = 12


def _cookie_flags() -> dict:
    samesite = (config.get("COOKIE_SAMESITE") or "lax").lower()
    secure = (config.get("COOKIE_SECURE") or "false").lower() == "true"
    return {"samesite": samesite, "secure": secure}


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    name: str
    role: str
    unit: Optional[str] = None
    towns: list = []

    model_config = {"from_attributes": True}


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


@router.post("/login", response_model=UserOut)
def login(body: LoginRequest, response: Response, db: Session = Depends(get_db)) -> User:
    email = body.email.strip().lower()
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    # 帳號不存在時仍走一次雜湊比對，否則回應時間會洩漏哪些 Email 是真帳號。
    ok = verify_password(body.password, user.password_hash if user else DUMMY_HASH)
    if user is None or not user.is_active or not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "帳號或密碼錯誤")
    token = new_token()
    db.add(UserSession(
        token=token, user_id=user.id,
        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=SESSION_TTL_HOURS),
    ))
    db.commit()
    response.set_cookie(
        COOKIE_NAME, token, httponly=True,
        max_age=SESSION_TTL_HOURS * 3600, **_cookie_flags(),
    )
    return user


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
