# 部署手冊

> **現況（2026-09-12）：雲端已縮到 0，專案跑在本機。**
> ECR 映像、ECS 叢集、任務定義、IAM 角色、安全群組、log group 都還在，
> 要重新上線只要 `aws ecs update-service --cluster watchdog --service watchdog
> --desired-count 1 --region us-west-2`。
> agent 仍走 Bedrock（競賽規定），其餘全部本地。

**照著做，不是說明文。** 每一節都是可以貼上就跑的步驟。
遇到不確定的地方，優先選「本機能動的那條路」，不要現場研究。

---

## 零、先知道三件會改變做法的事

這三件都是 2026-09-12 在競賽帳號（`550561128629`）上**實測**出來的，不是推測。

### 1. App Runner 不能用

```
apprunner:CreateService   →  implicitDeny
```

`docs/MERGE_PLAN.md` §8 的權限掃描結果：RDS、ECR、ECS、Amplify、S3、Lambda、
CloudFormation、IAM 都可以建，**只有 App Runner 被鎖**。所以後端走 **ECS Fargate**。

### 2. 不需要拆前後端

FastAPI 自己服務前端（`api/server.py` 把 `/` 掛到 `webapp/`），所以前後端**同源**。

這一點消掉了原本預期的三個地雷：CORS、`SameSite=None; Secure` 的 cookie、
以及「登入成功但每個 API 都回 401」。**不要**為了架構好看把前端拆去 Amplify，
那只會把這三個問題請回來。

### 3. 環境在交件那一刻關閉

AWS 環境只開放到 **9/13 13:00**，而交件也是 9/13 13:00。憑證是臨時的、會過期
（`ExpiredTokenException` 就是過期，回 workshop 頁面重拿四行）。

**所以 AWS 上的東西是展示用，交付物是 repo + 本機可跑。** 每個步驟都要能重入，
因為做到一半憑證過期是常態。

---

## 一、當天第一件事

```bash
python run.py bedrock-check
```

可用模型是**帳號層級的權限**，換帳號或換一天都可能不一樣。
現在的狀態是 7/7 可用，三個落點都通。

---

## 二、本機先把映像建起來跑過

**不要第一次建映像就在雲端建。**

```bash
# Apple Silicon 一定要加 --platform，Fargate 是 amd64。
# 不加會啟動失敗，而且錯誤訊息不明顯。
docker build --platform linux/amd64 -t watchdog:latest .

docker run --rm -p 8080:8080 --env-file .env watchdog:latest
curl localhost:8080/api/health
```

映像內容的兩個取捨，先知道再決定要不要補：

| 不進映像 | 後果 |
|---|---|
`data/raw`（1.8 GB，不得轉散布） | **證據頁截圖在雲端回 404**。文字證據（檔名＋頁碼）不受影響，因為 `document_index.sqlite` 已進版控 |
`data/runtime`（SQLite） | 容器重啟會失去帳號與對話稽核軌跡。要保留就接 RDS（見 §四） |

`seed_users.py` 是冪等的，所以容器每次啟動跑一次是安全的。

---

## 三、推到 ECR

```bash
REGION=us-west-2
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REPO=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/watchdog

aws ecr create-repository --repository-name watchdog --region $REGION
aws ecr get-login-password --region $REGION \
  | docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.$REGION.amazonaws.com

docker tag watchdog:latest $REPO:latest
docker push $REPO:latest
```

憑證在 push 中途過期就重拿四行、重跑 `get-login-password`，然後再 push 一次
——已經上傳的層不會重傳。

---

## 四、資料庫：先不要建 RDS

預設走容器內的 SQLite。**只有在你需要「重啟後帳號與稽核軌跡還在」時才建 RDS**，
因為它要多花約 10 分鐘 provision，還要處理安全群組。

要建的話：

```bash
aws rds create-db-instance --db-instance-identifier watchdog \
  --db-instance-class db.t4g.micro --engine postgres \
  --master-username watchdog --master-user-password '<自己設>' \
  --allocated-storage 20 --region $REGION
```

然後在任務定義裡設：

```
DATABASE_URL=postgresql+psycopg://watchdog:<pw>@<endpoint>:5432/postgres
```

安全群組要允許 Fargate 任務所在的子網連入 5432。
程式面**不用改任何一行**——`db/session.py` 只讀 `DATABASE_URL`。

---

## 五、ECS Fargate

用預設 VPC 最快。任務定義要的環境變數：

| 變數 | 值 | 沒設會怎樣 |
|---|---|---|
`AWS_REGION` / `AWS_DEFAULT_REGION` | `us-west-2` | Bedrock 打不通 |
`AWS_ACCESS_KEY_ID` / `..._SECRET_ACCESS_KEY` / `..._SESSION_TOKEN` | workshop 那四行 | 同上 |
`SESSION_SECRET` | 隨機字串 | — |
`SEED_INSPECTOR_EMAIL` / `..._PASSWORD` | 自己設 | 稽查人員帳號不會建立 |
`SEED_ADMIN_EMAIL` / `..._PASSWORD` | 自己設，且 Email 不可與 inspector 相同 | 管理員帳號不會建立 |
`QUICK_LOGIN_ENABLED` | 正式環境固定 `false` | 不顯示免密碼快速登入（安全預設） |
`AGENT_BACKEND` | `bedrock` | 預設就是 bedrock，可省略 |
`DATABASE_URL` | 只有接 RDS 時才設 | 預設 SQLite |

> **正式環境應該用任務角色（task role）而不是把金鑰塞進環境變數。**
> 這裡用環境變數是因為 workshop 給的就是臨時金鑰，而且環境 20 小時後就消失。
> 這個做法不要帶到真正的部署。

要點：

- **任務執行角色要有 `bedrock:InvokeModel` 與 `bedrock:InvokeModelWithResponseStream`。**
  第二個是 agent 串流用的，很容易漏。（實測這個帳號兩個都有）
- **最小／最大任務數都設 1。** 速率上限（`api/agent.py` 的 `_RATE`）與掃描任務
  狀態都是記憶體字典，多開一個就各自為政。
- **SSE 不能被緩衝。** agent 的講解句走 Server-Sent Events，若走 ALB 要確認沒有
  回應緩衝。症狀是講解句一次全部跳出來，而不是逐步出現。
- 只開一個任務又不想架 ALB 的話，直接用任務的 public IP + 8080。
  沒有 HTTPS，所以 `COOKIE_SECURE` 維持 `false`（預設就是）。

---

## 六、上台前的檢查清單

- [ ] `python run.py bedrock-check` → 7/7
- [ ] 一般登入：稽查人員／系統管理員各自帳密可用，選錯身分會被拒絕
- [ ] 展示環境：兩個快速登入都可用；正式環境不顯示快速登入
- [ ] 助理說一句話，**畫面真的跟著動**（頁籤切換、地圖標記）
- [ ] `幫我排板橋區這週的稽查` → 會載入 SOP、取排序、匯出 CSV
- [ ] `資遣費準備金寫在哪一頁` → 回得出檔名與頁碼
      （雲端上沒有 `data/raw`，截圖會 404，這是預期的——先想好怎麼講）
- [ ] `你這個排序準不準` → 會調出 `get_model_card`，**包含不好看的數字**
- [ ] 時間軸五個時點都點過
- [ ] MCP：`POST /api/agent/mcp-token` 拿得到權杖

---

## 七、接不上時怎麼講

**沒有離線退路後端，這是刻意的。** 在 AI 競賽上展示一個假裝聽懂的 agent，
比誠實說「這部分接不上」更糟。

Bedrock 連不上時，誠實說明，然後把重心切到**不需要即時 LLM 呼叫**的部分——
那本來就該是提案的重心：

- **派工提案與地圖**：1,213 園、分層理由，都是已算好的 payload
- **卷宗與法遵發現**：924 項檢核，逐條可追溯到財報頁碼
- **文件檢索**：4,293 段財報原文，每筆帶檔名與頁碼
- **時間軸回測**：五個時點的 AUC 與前 N 名命中率
- **144 份既有建議書**

這些全部不經過任何一次即時模型呼叫。

**不要在台上臨時切後端或重試。** 連不上就是連不上，照上面的順序展示，
比硬要證明 agent 那一層也能動更穩。

---

## 八、已知會咬人的地方

- **Apple Silicon 建的映像預設是 arm64**，Fargate 要 amd64。
- **憑證會在操作中途過期。** push、部署、查狀態各自重拿即可，步驟都能重入。
- **速率上限與掃描任務狀態是記憶體字典**，多個任務就不一致。demo 期間固定 1 個。
- **證據頁在雲端是 404**，因為 `data/raw` 不進映像（不得轉散布）。
  要在雲端展示截圖，得先在有原始 PDF 的機器上渲染好再放 S3。
- **`data/runtime` 不進映像**，所以容器重啟會失去帳號與稽核軌跡。
  接 RDS 才會留下來。
