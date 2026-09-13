# 單一容器：FastAPI 同時服務 API 與前端（`/` 掛 webapp/），所以**沒有跨網域**，
# 也就沒有 CORS 與 SameSite cookie 的問題。不需要第二個服務。
#
# ⚠️ Apple Silicon 上建映像預設是 arm64，Fargate 要 amd64：
#     docker build --platform linux/amd64 -t watchdog .
# 不加會啟動失敗，而且錯誤訊息不明顯。
#
# **data/raw 只放 132 份非營利財報原件（1.2 GB）**，其餘 0.6 GB 不進映像。
# 證據頁（/api/evidence/page）與文件控管室的原件頁都從原件現場渲染；原件不在
# 映像裡時，雲端只給得出文字與頁碼，截圖一律 404（部署後實測過）。
# 2026-09-13 使用者決定放進來。主辦方資料集不得轉散布，而雲端網址是公開的、
# 快速登入也開著——這是知情下的取捨，不是疏漏。

FROM python:3.11-slim

# pymupdf 要 libgl 才能 render；psycopg 要 libpq。兩者都只裝執行期的。
# fonts-noto-cjk：Telegram／LINE 的地圖是伺服器端用 Pillow 畫的 PNG，slim 映像裡
# 沒有任何中文字型，少了它圖上每個字都是方框——圖照樣送得出去，所以不會報錯，
# 只會在長官手機上看到一張全是方框的地圖。bots/mapimage.py 會挑其中的繁中字族。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 libpq5 fonts-noto-cjk \
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
# 分析產物（已進版控，clone 即可用）與非營利財報原件。data/interim 與 data/raw 的
# 其餘部分由 .dockerignore 排除。
COPY data/ ./data/

# PYTHONUNBUFFERED：容器裡 stdout 不是終端機，print() 會被緩衝住不送出。
# 結果是 [telegram]／[threads] 的紀錄在 CloudWatch 裡一行都沒有——連「bot 已啟動」都看不到，
# 展示時出問題只能盲猜（2026-09-13 實際發生）。
ENV PYTHONPATH=/app/src \
    PYTHONIOENCODING=utf-8 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

# 前端 payload 在建映像時產生，不在啟動時——啟動要快，而且產不出來要在
# build 就失敗，不要等到服務起來才發現 /api/payload 是空的。
RUN python scripts/build_frontend.py

# 文件控管室（02）的切片。同樣在 build 時產生：
#   - 輸出在 `data/interim/`，而那個目錄被 .dockerignore 排除，所以不能靠 COPY。
#   - 少了它，`/api/dataroom/*` 全部回 503，那一室在畫面上是一行錯誤訊息，
#     而其餘四室看起來都正常——最難聯想到是建映像時漏了一步。
# 它會讀 data/raw 找原始 PDF 的路徑，填進索引的 pdf 欄；原件頁沒有上傳檔時就用它。
RUN python scripts/build_dataroom_slice.py

EXPOSE 8080

# 資料庫預設仍是 SQLite（容器內，重啟即失去帳號與稽核軌跡）。
# 要保留就把 DATABASE_URL 指向 RDS：
#   postgresql+psycopg://<user>:<pw>@<endpoint>:5432/<db>
# seed_users.py 是冪等的，所以每次啟動跑一次是安全的。
CMD ["sh", "-c", "python scripts/seed_users.py && exec uvicorn smart_watchdog.api.server:app --host 0.0.0.0 --port ${PORT}"]
