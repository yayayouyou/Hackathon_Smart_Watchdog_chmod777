"""六張表：帳號兩張、agent 三張、外部通報一張。

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


# ── 外部通報 ──────────────────────────────────────────────────────────


class ThreadsMention(Base):
    """民眾在 Threads 上 @標註本帳號的貼文。一則一列，追加式。

    **為什麼這張進資料庫，而機構資料不進。** 模組說明第 2 點講的是同一批資料
    不要有兩套表述；這張表沒有第二套表述——它是外部來的、會持續累積的收件匣，
    在 `data/processed/` 與 payload 裡都不存在對應物。它也需要一個唯一索引來
    去重、一個穩定的游標來續抓，那是資料庫比 CSV 適合的地方。

    **三個時間分開存**（`06-plan` §2.1 要求「明確區分事件時間、發布時間與系統
    觀測時間」）：`posted_at` 是平台給的發文時間，原樣保存不重新解析；
    `observed_at` 是我方寫入時間。兩者不可互相推導——一則兩年前的舊文今天才
    同步進來，是完全正常的事。

    **`institution_id` 為 NULL 代表「歸屬拒配」，不是「與機構無關」。**
    `features.alerts.attribute()` 的預設是拒絕，它的模組說明記了四個真實誤配
    案例。拒配的列照樣留著，`attribution_basis` 會說明為什麼拒——把它當成沒有
    這則通報，跟把 `$ -` 記成 0 是同一種錯。

    **這張表不進分數、不進 payload、不進任何 CSV。** 與 `realtime/jobs.py` 的
    不變式一致：要進卷宗必須人工逐則採用，而且沒有那條程式路徑。

    **`kind` 分開存主貼文與串下的回覆，因為兩者的來意不同。** `mention` 是有人
    把官方帳號 @ 進來——那是一個對機關說話的動作；`reply` 是那串底下的回覆，
    回覆的人多半在跟原 PO 講話，沒有標註任何機關，很可能根本不知道機關在讀。
    把兩者混成同一種「通報」會讓「有 N 個人向教育局反映」這句話灌水 N 倍，
    而那個數字正是稽查優先序的說明會拿去講的東西。`kind` 與 `is_reply` 不是
    同一件事：一則 @標註我們的貼文本身可能是別人串裡的回覆（`is_reply=True`
    但 `kind='mention'`），那是結構，這裡記的是來意。

    **`attribution_source` 說這一列的歸屬是自己掙來的還是繼承來的。** 一則回覆
    寫「文德幼兒園真的很誇張」是**自己指名**了一家；寫「我也遇過」則什麼都沒
    指名，只能沿用它所在那一串的主體。兩者的 `institution_id` 可能一模一樣，
    意義卻差很遠：`06-plan` §6 說分析單位是園所 × 事件群集、轉貼同一事件不得
    重複加權，而一串十則「+1」繼承下來，在 `institution_id` 上看起來就是十次
    指名。所以這件事要有自己的欄位——靠 `attribution_basis LIKE '%繼承%'` 去
    解析文案，是把語意藏在給人看的句子裡，改一次文案就壞。

    也因此**回覆指名的若是另一家，就算另一家的**（`own`）。串是討論的容器，
    不是主體的容器；有人在文德那串底下說「吉尼爾也這樣」，那則講的就是吉尼爾。
    NULL 代表這一列早於這個欄位（那時只有主貼文，own／none 由 `institution_id`
    是否為空即可推回），不代表「來源不明」。

    **`root_threads_id` 讓一串留在一起。** 主貼文的 root 是它自己，所以一串永遠
    是一次查詢。`reply_to_threads_id` 存平台給的直接回覆對象，不靠回傳順序推
    ——Threads 會先回子留言再回父留言，靠順序接樹在上游是踩過的坑
    （見 `threads_bridge._reconcile_parent_links` 的說明）。兩欄都允許 NULL：
    舊列與平台沒說的情況都是 NULL，不是 0 也不是空字串猜一個。
    """

    __tablename__ = "threads_mention"
    __table_args__ = (
        Index("ix_threads_mention_institution_posted", "institution_id", "posted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 平台給的貼文 id。唯一索引就是去重機制本身——同一則重抓一百次只會有一列。
    threads_id: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    # 發文者的 Threads 帳號名稱。這是識別資料，只用於同一事件的多則貼文對帳；
    # `06-plan` §2.2 非目標明訂「不建立個人使用者可信度或人物風險檔案」。
    username: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    permalink: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 平台回傳的 ISO-8601 字串，原樣。不轉成 DateTime：時區與精度由平台決定，
    # 轉一次就多一個我方可能弄錯的地方，而原字串永遠可以再解析。
    posted_at: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    is_reply: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 這則屬於哪一串。主貼文填自己的 threads_id，所以整串一次查得到。
    root_threads_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # 直接回覆的對象，平台說的。NULL = 平台沒說或這則就是主貼文。
    reply_to_threads_id: Mapped[Optional[str]] = mapped_column(String(64))
    # own = 這則自己指名了機構；inherited = 沿用同串主貼文的；none = 兩者皆無。
    # 主貼文只會是 own 或 none。NULL = 這一列早於這個欄位。
    attribution_source: Mapped[Optional[str]] = mapped_column(String(16))
    # mention = 有 @我們的貼文；reply = 那串底下的回覆。理由見類別說明。
    # server_default 不是裝飾：既有資料庫補這欄時，舊列必須落在 mention 那一邊。
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="mention", server_default="mention"
    )
    # 內文 sha256。上游事後編輯或刪文時，這是唯一能證明我方存的是哪個版本的東西。
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # 歸屬結果。NULL = 拒配，理由見類別說明。
    institution_id: Mapped[Optional[str]] = mapped_column(String(32), index=True)
    attribution_basis: Mapped[Optional[str]] = mapped_column(Text)
    # 平台原始回應的那一則，完整保存。之後想補欄位時不必重抓。
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    observed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
