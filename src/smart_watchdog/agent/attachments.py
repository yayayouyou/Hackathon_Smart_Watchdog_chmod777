"""使用者在助理對話框用「+」附加的檔案。

檔案先暫存在這裡，訊息只帶代號；真正把它放進文件控管室的是助理的 tool
（``add_to_dataroom``），結果由伺服器做完回報，不是前端自己說的。

代號綁使用者：拿到別人的代號也讀不到——與 agent session 同一個物件層級授權
的理由（見 ``api/agent.py`` 的 ``post_message``）。
"""

from __future__ import annotations

import json
import pathlib
import re
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[3]
DIR = ROOT / "data/runtime/agent_attachments"
_ID = re.compile(r"^[0-9a-f]{32}$")   # 代號直接拼進路徑，格式不對一律不碰檔案系統


def save(user_id: int, filename: str, blob: bytes) -> dict:
    from ..dataroom.store import page_count

    att = uuid.uuid4().hex
    meta = {"id": att, "user_id": user_id,
            "filename": pathlib.Path(filename or "附件.pdf").name,
            "bytes": len(blob), "pages": page_count(blob), "ts": time.time()}
    DIR.mkdir(parents=True, exist_ok=True)
    (DIR / f"{att}.pdf").write_bytes(blob)
    (DIR / f"{att}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return {k: meta[k] for k in ("id", "filename", "bytes", "pages")}


def meta(att_id: str, user_id: int | None) -> dict | None:
    if user_id is None or not _ID.match(att_id or ""):
        return None
    f = DIR / f"{att_id}.json"
    if not f.exists():
        return None
    m = json.loads(f.read_text(encoding="utf-8"))
    return m if m.get("user_id") == user_id else None


def load(att_id: str, user_id: int | None) -> tuple[dict, bytes] | None:
    m = meta(att_id, user_id)
    if m is None:
        return None
    return m, (DIR / f"{att_id}.pdf").read_bytes()


def note(m: dict) -> str:
    """附在使用者訊息後面給模型看的一行。"""
    pages = f"{m['pages']} 頁，" if m.get("pages") else ""
    return f"（附加檔案：{m['filename']}，{pages}附件代號 {m['id']}）"
