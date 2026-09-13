# AWS 實際部署架構（HTTPS 版）

> 2026-09-13 以 AWS CLI 唯讀查詢線上資源整理，區域 us-west-2（CloudFront 為全球服務）。
> 列的是**實際在跑的設定**，不是規劃。

**8 項 AWS 服務，分成四層：公開入口、後端運算、生成式 AI、跨層安全與維運**

請求路徑：`使用者 ─HTTPS→ CloudFront ─HTTP:80→ ALB ─8080→ ECS Fargate 任務 ─→ Bedrock`

---

## 01 公開入口：Amazon CloudFront

- **唯一的 HTTPS 入口**，使用 CloudFront 內建的 `*.cloudfront.net` 憑證（不需自有網域與 ACM）
- HTTP 一律轉址到 HTTPS（`redirect-to-https`），支援 HTTP/2、IPv6
- **單一預設行為**：前端頁面與 `/api/*` 全部轉給 ALB
  - 前端由 FastAPI 同源提供，所以沒有 S3 origin，也沒有跨網域（CORS）設定
- 快取政策 `Managed-CachingDisabled`：不快取，避免 A 的登入回應被回給 B
- 來源請求政策 `Managed-AllViewer`：cookie、header、查詢字串全部轉送，登入才能成立
- 來源讀取逾時 60 秒、**關閉壓縮**：避免緩衝助理的 SSE 串流回應

## 02 後端運算：ALB + ECS Fargate + ECR

**Application Load Balancer（`watchdog-alb`）**
- internet-facing，橫跨 4 個可用區
- 監聽 HTTP:80，轉發到目標群組 `watchdog-tg`
- 目標群組為 IP 型（Fargate awsvpc 必須），HTTP:8080
- 健康檢查 `/api/health`，每 15 秒一次，連續 2 次成功判定健康

**Amazon ECS on Fargate（叢集／服務 `watchdog`）**
- **1 vCPU / 2 GB · 1 個任務**，x86_64（amd64），Fargate 平台 1.4.0
- 單一容器：`python:3.11-slim` + uvicorn + FastAPI，port 8080，同時服務 API 與前端
- 滾動部署（最少 100% / 最多 200%），健康檢查寬限 120 秒，避免冷啟動被判死
- 固定 1 個任務：速率上限與掃描工作狀態存在記憶體，多開會各自為政

**Amazon ECR（`watchdog`）**
- 映像約 470 MB，AES-256 加密
- 標籤：`latest`（目前版本）、`rollback-0913`（退版用）
- 建映像時產生前端資料與文件控管室切片；132 份非營利財報原件（1.2 GB）隨映像部署，供證據頁截圖，其餘原始資料不進映像

## 03 生成式 AI：Amazon Bedrock

- 全部使用 Anthropic Claude，經 `us.` 跨區域推論設定檔呼叫
- 伺服器上的五個 AI 落點：

| 功能 | 模型 | 呼叫方式 |
|---|---|---|
| 智慧助理（多步操作、工具呼叫、逐句串流講解） | Claude Sonnet 4.6 | `ConverseStream` |
| 自然語言查詢的意圖解析 | Claude Haiku 4.5 | InvokeModel |
| 文件控管室：上傳掃描財報，逐頁視覺抽取 | Claude Sonnet 4.6 | InvokeModel（影像輸入） |
| Threads 標註貼文分類（背景每 300 秒，每輪上限 20 則） | Claude Sonnet 4.6 | InvokeModel |
| 對外回覆草稿／對內簡報草稿 | Claude Opus 4.6 | InvokeModel |

- 132 份財報的批次抽取與稽核建議書在本機執行，同樣走 Bedrock，產物隨映像部署

## 04 應用狀態：容器內 SQLite

- 帳號與對話稽核軌跡存在容器內的 SQLite，**容器重啟即清空**
- 分析產物（1,213 所機構的查核優先序、財報原文索引）隨映像唯讀部署
- 程式只讀 `DATABASE_URL`，改接 RDS 不需改程式，但本次部署未使用

---

## 跨層安全與維運

- **VPC / 安全群組**
  - 預設 VPC（172.31.0.0/16），任務分布於 4 個子網
  - `watchdog-alb`：80 ← 0.0.0.0/0
  - `watchdog-sg`（任務）：8080 **只接受** ALB 安全群組，任務不直接對外開放
- **IAM**
  - `watchdogExec` 任務執行角色（`AmazonECSTaskExecutionRolePolicy`）：拉取 ECR 映像、寫入日誌
  - 呼叫 Bedrock 使用主辦方發放的臨時 STS 憑證
- **設定與金鑰**：以任務定義環境變數注入（Bedrock 憑證、Session 金鑰、預設帳號、Google Maps／Threads／Apify 權杖）
- **CloudWatch Logs**：`/ecs/watchdog`，awslogs 驅動，記錄應用程式與執行期日誌
