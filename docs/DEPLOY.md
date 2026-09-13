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
`QUICK_LOGIN_ENABLED` | 決賽展示設 `true`，見下方說明 | 不顯示免密碼快速登入（安全預設） |
`AGENT_BACKEND` | `bedrock` | 預設就是 bedrock，可省略 |
`DATABASE_URL` | 只有接 RDS 時才設 | 預設 SQLite |

> **`QUICK_LOGIN_ENABLED=true` 等於把整個系統對任何拿到網址的人開放。**
> 站上是 1,213 所真實機構的查核優先序，而 CLAUDE.md 的輸出定位寫明個別機構
> 分數不對外公開揭露——這也是這個 repo 私有的理由。決賽期間為了讓評審能一鍵
> 進來看而打開，是一個**知情的取捨**，不是預設值。
>
> 展示結束後把它設回 `false`，或直接停掉服務。要留著給人看又想收斂風險的話，
> 最小的做法是在 ALB 前面加一層 IP 允許清單或 Cognito，而不是靠這個開關。

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

## 九、2026-09-13 實際部署紀錄（照這個做過一次，全部驗證通過）

入口是 **ALB**，不是任務的公開 IP——任務一重啟 IP 就換，發出去的網址會死。

| 資源 | 名稱 | 備註 |
|---|---|---|
| ALB | `watchdog-alb` | internet-facing，HTTP:80，網址見 `aws elbv2 describe-load-balancers` |
| Target group | `watchdog-tg` | **target-type 必須是 `ip`**（Fargate 的 awsvpc 沒有 instance）；健康檢查 `/api/health` |
| ALB 安全群組 | `watchdog-alb` | 80 ← 0.0.0.0/0（不限 IP） |
| 容器安全群組 | 原本那個 | 8080 **只接受** ALB 安全群組，不再對 0.0.0.0/0 開 |
| 服務 | `watchdog/watchdog` | health-check grace 120 秒，冷啟動時才不會被 ALB 判死 |
| 映像標籤 | `latest` = `deploy-0913`；`rollback-0913` = 部署前的舊版 | 退回：任務定義映像改成 `:rollback-0913` 再強制部署 |

踩到、而且會再踩的三件事：

1. **文件控管室的切片要在建映像時產生。** 它輸出到 `data/interim/dataroom/`，
   而 `.dockerignore` 排除整個 `data/interim/`，所以不能靠 COPY。Dockerfile
   現在有 `RUN python scripts/build_dataroom_slice.py`。它會讀 `data/raw`
   找原始 PDF 檔名，但找不到會跳過，切片照樣完整（6.7 MB）。
   少了這一步，其他四室都正常、只有 02 整室是一行 503——最難聯想到是 build 漏了。
2. **zsh 會吃掉 `$REPO:latest` 的 `:l`。** 它把 `:l` 當成「轉小寫」修飾符，
   推出去的名字變成 `watchdogatest`，然後報「repository 不存在」。一律寫
   `"${REPO}:latest"`。
3. **任務定義裡的環境變數是部署當下的快照。** workshop 的 AWS 臨時金鑰會過期，
   換金鑰要**註冊新的任務定義 revision** 再強制部署，改 `.env` 不會影響雲端。
   快速登入需要三個變數同時在：`QUICK_LOGIN_ENABLED=true`、`SEED_ADMIN_EMAIL`、
   `SEED_ADMIN_PASSWORD`（容器啟動時 `seed_users.py` 會依它們建帳號）。

本機先驗再推：`docker run -p 8090:8080 --env-file .env -e DATABASE_URL= watchdog:new`，
確認 `/api/dataroom/overview`、`/api/auth/options` 都是 200，才推 ECR。

## 十、HTTPS：CloudFront 放在 ALB 前面（不需要網域）

ACM 的憑證要綁自己的網域，workshop 帳號沒有。**CloudFront 自帶
`*.cloudfront.net` 的憑證**，所以直接在 ALB 前面加一層就有 HTTPS，
ALB、服務、HTTP 網址都不必動——建失敗也不影響原本的入口。

| 設定 | 值 | 為什麼 |
|---|---|---|
| Origin | ALB 的 DNS，`http-only`，port 80 | ALB 只開 HTTP |
| Viewer protocol | `redirect-to-https` | 打 http 會 301 到 https |
| Allowed methods | 全部七種 | 登入、助理都是 POST |
| Cache policy | `Managed-CachingDisabled` | **一定要關**。預設會快取，最糟是 A 的 `/api/auth/me` 被回給 B |
| Origin request policy | `Managed-AllViewer` | cookie、header、查詢字串全部轉送，登入才會成立 |
| Origin read timeout | 60 秒（不申請配額的上限） | 助理是 SSE 串流；每步上限 10 秒，留足餘裕 |
| Compress | 關 | 避免任何可能緩衝串流的處理 |

政策 ID 不要寫死，用名稱查：
`aws cloudfront list-cache-policies --type managed` 找 `Managed-CachingDisabled`，
`list-origin-request-policies` 找 `Managed-AllViewer`。

兩個踩到的坑：

1. **`CachedMethods` 要放在 `AllowedMethods` 底下**，不是跟它同一層；放錯會在
   CLI 參數檢查就被擋，什麼都不會建。
2. **狀態還是 `InProgress` 時通常就已經能連**，正式標成 `Deployed` 要再等幾分鐘。

驗證方式：同一個網址連打兩次，`x-cache` 都要是 `Miss from cloudfront`
（代表沒快取）；登入回應要看得到 `set-cookie`。

`COOKIE_SECURE` 維持 `false`：HTTP 網址還開著，設成 true 的話從 HTTP 那邊會
登不進去。等確定只用 HTTPS、並把 ALB 限縮成只收 CloudFront 之後再改。

## 十一、PWA 與通訊軟體 bot 上雲

2026-09-13 探過 `https://d1unpo09166qaz.cloudfront.net`：**雲端還是舊版**——`/sw.js`、
`/manifest.webmanifest`、`/api/bot/line/webhook` 都是 404，`/api/health` 沒有 `telegram_bot`。
CloudFront 本身的設定（§十）剛好都符合，不用改：

| 需要 | §十 的設定 | 結果 |
|---|---|---|
| LINE webhook 是 POST | Allowed methods 全部七種 | ✓ |
| LINE 驗簽要 `X-Line-Signature` 標頭 | `Managed-AllViewer` 轉送全部標頭 | ✓ |
| 簽章地圖要 `?exp=&sig=` | `Managed-AllViewer` 轉送查詢字串 | ✓ |
| SW 與簽章地圖不能被快取 | `Managed-CachingDisabled` | ✓ |
| PWA 要 HTTPS | `*.cloudfront.net` 憑證 | ✓ |

要做的只有三件：

**1. 重建映像。** Dockerfile 已加 `fonts-noto-cjk`。少了它，bot 送出的地圖每個字都是
方框，而且不會有任何錯誤。

**2. 任務定義註冊新 revision，加上這些環境變數**（`.env` 不進映像；任務定義是快照）：

| 變數 | 值 | 沒設會怎樣 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 給的 | Telegram bot 不啟動，`/api/health` 寫原因 |
| `TELEGRAM_BOT_USERNAME` | `Little_Guardian_bot` | 綁定連結少了帳號名稱 |
| `TELEGRAM_DEMO_PASSCODE` | 展示暗號 | 只能用派工台產生的一次性碼綁定 |
| `LINE_CHANNEL_ID` / `LINE_CHANNEL_SECRET` | LINE Developers 上的值 | LINE webhook 靜默回 200、不處理 |
| `LINE_CHANNEL_ACCESS_TOKEN` | 可省略 | 省略時程式用 ID＋secret 自己換 30 天 token |
| `PUBLIC_BASE_URL` | `https://d1unpo09166qaz.cloudfront.net` | LINE 只送文字清單、不附地圖 |
| `SEED_INSPECTOR_EMAIL` | 已有 | 暗號會綁到第一個稽查人員帳號 |

**3. Telegram 同一個 token 只能一個地方輪詢。** 雲端開著時，本機 `.env` 要設
`TELEGRAM_BOT_POLLING=false`，否則兩邊都 409、bot 看起來像壞了。反過來用本機展示時，
雲端任務定義設 `false`。

**LINE 後台**（LINE Developers → Messaging API）：Webhook URL 填
`https://d1unpo09166qaz.cloudfront.net/api/bot/line/webhook`、打開 Use webhook，
並在 LINE Official Account Manager 關掉「自動回應訊息」（否則官方帳號會搶先回罐頭訊息）。
**部署完成之後再設**——現在雲端還是 404，設了也只是讓 LINE 那邊一直失敗。

部署後的驗證：

```bash
B=https://d1unpo09166qaz.cloudfront.net
curl -sI $B/sw.js | grep -i "200\|service-worker-allowed"       # 200、Service-Worker-Allowed: /
curl -s  $B/api/health | grep -o '"telegram_bot":{[^}]*}'         # enabled true、cycles 會增加
curl -s -o /dev/null -w "%{http_code}
" -X POST -d '{"events":[]}' $B/api/bot/line/webhook   # 400（沒簽章）
```

最後一行回 **400** 才是對的：代表 LINE 憑證有進容器、而且驗簽在擋。回 200 代表憑證沒設
（靜默模式），回 404 代表還是舊映像。

⚠️ 綁定關係存在容器內的 SQLite（`data/runtime` 不進映像）。容器重啟之後要重新點一次
暗號連結——這是展示環境可以接受的代價，要保留就接 RDS（§四）。

