"""索引的 SQLite 結構。

四張表加一個 FTS5 虛擬表，各自回答一種問題：

    documents  這批語料裡有哪些文件？哪些是掃描影像、哪些有可用文字層？
    sections   某個區段（資產負債表／附註二／某分基金）在哪一份文件的哪幾頁？
    facts      某個數字是多少？出處在哪？
    passages   哪些文件提到某段文字？（全文檢索）
    coverage   這個索引「涵蓋到什麼程度」？沒涵蓋到的是什麼？

**`facts.value` 允許 NULL，而 NULL 的意思是「空白／未編列」，不是 0。**
這條規則在 CLAUDE.md 是鐵則：業務發展費預算欄空白代表未編列預算（稽查發現），
填 0 等於謊稱編列了零元。整條管線都靠它，索引不能在這裡破功。

**`year_kind` 一定要跟著 `year` 走。** 非營利園用學年度、公校決算書用年度，
兩者不可直接對齊（學年度 N 的資產負債表基準日是 (N+1)/7/31）。把兩種年度
放進同一個欄位而不標明種類，是這個資料集最容易犯、也最難發現的錯。
"""

from __future__ import annotations

import pathlib
import sqlite3

#: 索引檔位置。這是衍生產物，可由 `python run.py doc-index` 重建。
DEFAULT_PATH = pathlib.Path("data/processed/document_index.sqlite")

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS documents (
    doc_id      TEXT PRIMARY KEY,
    path        TEXT NOT NULL,          -- 相對專案根目錄，可直接開啟原始 PDF
    kind        TEXT NOT NULL,          -- 非營利財報 / 公校決算書 / 封面 / 命題文件
    institution TEXT,                   -- 公校決算書為 NULL：一冊含多園
    code        TEXT,                   -- N01 / 13601
    year        INTEGER,
    year_kind   TEXT,                   -- 學年度 | 年度  ← 兩者不可直接對齊
    pages       INTEGER,
    text_pages  INTEGER,                -- 有可用文字層的頁數
    needs_ocr   INTEGER,                -- 1 = 純掃描影像，文字只能來自視覺抽取
    note        TEXT
);

CREATE TABLE IF NOT EXISTS sections (
    section_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      TEXT NOT NULL REFERENCES documents(doc_id),
    kind        TEXT NOT NULL,          -- 資產負債表 / 收支餘絀表 / 附註一…五 / 分基金
    label       TEXT NOT NULL,
    institution TEXT,
    code        TEXT,
    year        INTEGER,
    year_kind   TEXT,
    -- 兩種頁碼刻意分開存，混用會產生差一頁的錯誤引用（已經發生過一次）：
    --   page_start/page_end 是文件**自己印在頁尾的頁碼**（1-based），
    --     也是抽取結果記錄的值，稽查員看到的就是這個；
    --   pdf_index_* 是 PyMuPDF 的 `page.number`（0-based），開檔案才用。
    -- 驗證：N01安溪 113 資產負債表在 PDF index 4，頁尾印 "N01-5"，抽取記 5。
    page_start      INTEGER NOT NULL,
    page_end        INTEGER NOT NULL,
    pdf_index_start INTEGER,
    pdf_index_end   INTEGER,
    page_label      TEXT                -- 給人看的引用字串，例如 "p.12-15"
);

CREATE TABLE IF NOT EXISTS facts (
    fact_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    institution TEXT NOT NULL,
    code        TEXT,
    year        INTEGER NOT NULL,
    year_kind   TEXT NOT NULL,
    field       TEXT NOT NULL,          -- 正規化欄位名（程式用）
    label       TEXT NOT NULL,          -- 中文欄位名（人看）
    value       REAL,                   -- NULL = 空白／未編列，**不是 0**
    unit        TEXT,
    doc_id      TEXT REFERENCES documents(doc_id),
    page        INTEGER,                -- 印在頁尾的頁碼（1-based），同 sections
    section_id  INTEGER REFERENCES sections(section_id)
);

CREATE TABLE IF NOT EXISTS coverage (
    scope    TEXT PRIMARY KEY,
    covered  INTEGER NOT NULL,
    total    INTEGER NOT NULL,
    note     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_sections_lookup
    ON sections(institution, year, kind);
CREATE INDEX IF NOT EXISTS ix_sections_code
    ON sections(code, year);
CREATE INDEX IF NOT EXISTS ix_facts_lookup
    ON facts(institution, year, field);
CREATE INDEX IF NOT EXISTS ix_facts_field
    ON facts(field, year);
CREATE INDEX IF NOT EXISTS ix_facts_code
    ON facts(code, year);
"""

# trigram 分詞器做字元層級子字串比對，中文不需要斷詞字典即可檢索。
# 代價是查詢字串至少要 3 個字元——`search.py` 會處理太短的查詢。
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
    text,
    doc_id      UNINDEXED,
    section_id  UNINDEXED,
    page        UNINDEXED,
    kind        UNINDEXED,
    institution UNINDEXED,
    year        UNINDEXED,
    label       UNINDEXED,
    tokenize = 'trigram'
);
"""


def connect(path: pathlib.Path | str = DEFAULT_PATH) -> sqlite3.Connection:
    """開啟索引。回傳的連線以 `sqlite3.Row` 取列，欄位可用名字取用。"""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    """建立（或確認）結構。可重複呼叫。"""
    conn.executescript(SCHEMA)
    conn.executescript(FTS_SCHEMA)
    conn.commit()


def reset(conn: sqlite3.Connection) -> None:
    """清空內容但保留結構——重建索引時用，避免舊列殘留造成重複命中。"""
    for table in ("passages", "facts", "sections", "documents", "coverage", "meta"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
