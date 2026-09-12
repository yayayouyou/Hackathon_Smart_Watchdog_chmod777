"""五張表：帳號兩張、agent 三張。

搬自 `Eason20050201/hackathon@a0bdada` 的 `backend/app/db/models.py`，但**只取五張**，
並改了三處讓它能在 SQLite 上跑：

1. **`JSONB` → `JSON`。** `JSONB` 是 `sqlalchemy.dialects.postgresql` 的型別，
   SQLite 完全不認得。通用 `JSON` 在 SQLite 上存成 TEXT、在 Postgres 上仍是 JSON，
   之後換 RDS 不用再改這裡。
2. **`audit_feedback.institution_id` 的外鍵拆掉。** 原本指向 `institution` 表，
   但那張表沒有搬過來——chmod777 的機構資料在 `data/processed/` 與 payload，
   不在資料庫裡。留成純字串欄位，存 payload 的 8 碼 id（例如 `ecca529e`）。
   代價講明：資料庫層不再擋「不存在的機構 id」，由 tool 的參數驗證負責。
3. **沒有 alembic。** 五張全新表用 `create_all` 就夠；原專案那 8 個 migration
   是 Postgres 的演進史，對這裡沒有意義。

沒搬的九張（`institution`／`penalty`／`fee_year`／`financial_report`／`finding`／
`memo`／`evidence_page`／`ranking_snapshot`／`ranking_metric`）是**刻意**的：
它們與 chmod777 既有的 `payload.json`、`audit_priority_ntpc.csv`、
`compliance_findings.csv` 是同一批資料的兩套表述，並存會產生兩個互相矛盾的
機構清單與兩套名次。agent 的 tool 一律讀既有那一套。
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── 帳號 ──────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # inspector|supervisor|admin
    unit: Mapped[Optional[str]] = mapped_column(Text)
    # 這個稽查員負責的行政區。agent 的「使用者沒說行政區就用他負責的」靠這欄。
    towns: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # 使用者層記憶。**只由程式寫**（`agent/memory.py::remember`，呼叫點在路由），
    # 不給 tool 寫——模型能寫進自己下一輪脈絡的東西，就是一條能自我強化的管道。
    agent_memory: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class UserSession(Base):
    __tablename__ = "user_session"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    user: Mapped[User] = relationship()


# ── agent ────────────────────────────────────────────────────────────


class AgentSession(Base):
    __tablename__ = "agent_session"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship()


class AgentMessage(Base):
    """一輪對話的完整軌跡，逐則追加。稽核用：每個 tool 呼叫與其參數都在這裡。

    ``step_id`` 把「講解句、tool 呼叫、結果」綁在一起；使用者訊息的 step_id 是 0。
    """

    __tablename__ = "agent_message"
    __table_args__ = (Index("ix_agent_message_session_step", "session_id", "step_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_session.id"), nullable=False, index=True
    )
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    step_id: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user|assistant|tool
    # text|tool_call|tool_result|ui_action
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AuditFeedback(Base):
    """稽查員對建議書單項的回饋。追加式：不 UPDATE、不 DELETE，改變立場再寫一列。

    ``session_id`` 把回饋列接回產生它的那一輪對話——否則這張「人在迴圈」的
    資料集沒辦法回溯是哪次對話、哪個情境下按下的認同／不認同。
    """

    __tablename__ = "audit_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        ForeignKey("agent_session.id"), nullable=False, index=True
    )
    # chmod777 payload 的 8 碼機構 id。沒有外鍵，理由見模組說明第 2 點。
    institution_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    memo_item_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agrees: Mapped[bool] = mapped_column(Boolean, nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
