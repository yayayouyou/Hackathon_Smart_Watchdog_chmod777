"""證據頁：把財報 PDF 的某一頁渲染成 PNG。

為什麼是**隨用隨渲染 + 快取**，而不是預先產好：

- 132 份非營利財報共 5,162 頁。全部預渲染是 120 MB 以上，而一次展示會翻開的
  大概五頁。來源專案的做法是預先產進 `backend/static/evidence/` 並打包進容器，
  代價是**那個目錄在沒有 `data/raw` 的機器上是空的**（實測 0 個檔），
  證據抽屜就整個壞掉。
- 隨用隨渲染只依賴 `data/raw` 在**這台機器**上存在。沒有它就回 404 並說原因，
  不會讓整個服務起不來。
- 檔案快取在 `data/interim/evidence/`（gitignore），第二次開同一頁是讀檔。

⚠️ 這條路徑會把原始財報頁面的影像送給瀏覽器。那是主辦方資料集的內容，
**不得轉散布**——所以它要求登入，與其他端點一致。
"""

from __future__ import annotations

import hashlib
import pathlib
import re

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse

from ..db.models import User
from .auth import get_current_user

router = APIRouter(prefix="/api/evidence", tags=["evidence"])

ROOT = pathlib.Path(__file__).resolve().parents[3]
RAW = ROOT / "data/raw"
CACHE = ROOT / "data/interim/evidence"
DPI = 150

# 只允許 data/raw 底下的 PDF。路徑由 docsearch 的結果提供，但那是經過模型的
# 字串，所以這裡自己再驗一次——不是不信任 docsearch，是因為這個參數是可被
# 任意指定的，而它決定要開哪一個檔案。
_SAFE = re.compile(r"^data/raw/[^\x00]+\.pdf$", re.IGNORECASE)


def _resolve(rel_path: str) -> pathlib.Path:
    if not _SAFE.match(rel_path):
        raise HTTPException(400, "只接受 data/raw/ 底下的 PDF 路徑")
    target = (ROOT / rel_path).resolve()
    # 解析後再確認仍在 data/raw 內——擋掉 ../ 逃逸。
    if not str(target).startswith(str(RAW.resolve())):
        raise HTTPException(400, "路徑超出 data/raw/")
    if not target.exists():
        raise HTTPException(404, f"找不到 {rel_path}（data/raw 是否已還原？）")
    return target


@router.get("/page")
def evidence_page(
    path: str = Query(description="data/raw/ 底下的 PDF 相對路徑"),
    page: int = Query(ge=1, description="頁碼，從 1 起算"),
    user: User = Depends(get_current_user),
) -> FileResponse:
    """回傳那一頁的 PNG。第一次渲染，之後讀快取。"""
    del user
    pdf = _resolve(path)
    key = hashlib.sha256(f"{path}:{page}:{DPI}".encode()).hexdigest()[:16]
    out = CACHE / f"{key}.png"

    if not out.exists():
        try:
            import pymupdf
        except ImportError as exc:  # pragma: no cover - 相依缺失才會走到
            raise HTTPException(503, "伺服器缺 pymupdf，無法渲染證據頁") from exc
        with pymupdf.open(pdf) as doc:
            if page > doc.page_count:
                raise HTTPException(404, f"{pdf.name} 只有 {doc.page_count} 頁")
            CACHE.mkdir(parents=True, exist_ok=True)
            pix = doc[page - 1].get_pixmap(dpi=DPI)
            pix.save(out)

    return FileResponse(str(out), media_type="image/png",
                        headers={"Cache-Control": "private, max-age=3600"})
