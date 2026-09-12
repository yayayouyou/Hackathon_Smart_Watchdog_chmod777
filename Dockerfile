# 單一容器：FastAPI 同時服務 API 與前端（`/` 掛 webapp/），所以**沒有跨網域**，
# 也就沒有 CORS 與 SameSite cookie 的問題。不需要第二個服務。
#
# ⚠️ Apple Silicon 上建映像預設是 arm64，Fargate 要 amd64：
#     docker build --platform linux/amd64 -t watchdog .
# 不加會啟動失敗，而且錯誤訊息不明顯。
#
# **data/raw 不進映像**：1.8 GB，且主辦方資料集不得轉散布。後果是雲端上的
# 證據頁（/api/evidence/page）會回 404 並說「data/raw 是否已還原」。文字證據
# （檔名與頁碼）不受影響，因為 document_index.sqlite 已進版控。這是刻意的
# 取捨，不是疏漏——要在雲端展示截圖，得另外把渲染好的 PNG 放 S3。

FROM python:3.11-slim

# pymupdf 要 libgl 才能 render；psycopg 要 libpq。兩者都只裝執行期的。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 相依先裝，讓程式碼改動不會使這一層失效。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir "fastapi>=0.110" "uvicorn[standard]>=0.29" \
      "httpx>=0.27" "psycopg[binary]>=3.2"

COPY src/ ./src/
COPY webapp/ ./webapp/
# frontend/ 是靜態單檔版的樣板，build_frontend.py 要讀 frontend/index.html
# 才產得出 dist/。只有 32 KB，但少了它 build 會在最後一步失敗。
COPY frontend/ ./frontend/
COPY scripts/ ./scripts/
COPY run.py ./
# 分析產物（已進版控，clone 即可用）。data/raw 與 data/interim 由 .dockerignore 排除。
COPY data/ ./data/

ENV PYTHONPATH=/app/src \
    PYTHONIOENCODING=utf-8 \
    PORT=8080

# 前端 payload 在建映像時產生，不在啟動時——啟動要快，而且產不出來要在
# build 就失敗，不要等到服務起來才發現 /api/payload 是空的。
RUN python scripts/build_frontend.py

# 文件控管室（02）的切片。同樣在 build 時產生：
#   - 輸出在 `data/interim/`，而那個目錄被 .dockerignore 排除，所以不能靠 COPY。
#   - 少了它，`/api/dataroom/*` 全部回 503，那一室在畫面上是一行錯誤訊息，
#     而其餘四室看起來都正常——最難聯想到是建映像時漏了一步。
# 它會讀 data/raw 去找原始 PDF 檔名，但 data/raw 不進映像（1.8 GB，且主辦方
# 資料不得轉散布）。實測過：找不到就跳過，切片照樣完整（6.7 MB）。
RUN python scripts/build_dataroom_slice.py

EXPOSE 8080

# 資料庫預設仍是 SQLite（容器內，重啟即失去帳號與稽核軌跡）。
# 要保留就把 DATABASE_URL 指向 RDS：
#   postgresql+psycopg://<user>:<pw>@<endpoint>:5432/<db>
# seed_users.py 是冪等的，所以每次啟動跑一次是安全的。
CMD ["sh", "-c", "python scripts/seed_users.py && exec uvicorn smart_watchdog.api.server:app --host 0.0.0.0 --port ${PORT}"]
