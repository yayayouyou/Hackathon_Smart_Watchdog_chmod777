# 交接：現況與待辦

**2026-09-12 從 `D:\Hackathon_Smart_Watchdog` 搬到這裡並建 private repo。決賽 9/12–13，
9/13 13:00 前交件。**

先讀這份，再依需要展開到各自的文件。

---

## 30 秒上手

```bash
python run.py setup          # 建 venv 裝相依（已存在就跳過）
cp .env.example .env         # 填憑證，見 docs/ENVIRONMENTS.md
python run.py seed-users     # ⚠️ 沒有帳號就進不去，見下方
python run.py bedrock-check  # 決賽當天第一件事
python run.py frontend
python run.py serve          # → http://127.0.0.1:8000
```

⚠️ **`seed-users` 不可略過。** 動態版有全螢幕登入牆（`webapp/auth.js`），
沒有帳號時地圖、派工提案、時間軸**一個都看不到**。帳密取自 `.env` 的
`SEED_INSPECTOR_EMAIL` 與 `SEED_INSPECTOR_PASSWORD`，兩者缺一就不會建帳號
（腳本會明白說缺什麼，不會預設一組寫死的密碼）。

建表本身是冪等的，`run.py serve` 啟動時也會做一次——所以「登入回 500、
log 寫 `no such table: user_session`」那個狀況不會再發生。`seed-users`
多做的是**建帳號**。

`python run.py --list` 列出全部任務。**Mac 與 Windows 指令完全相同。**

---

## 已經完成的

| 項目 | 狀態 | 文件 |
|---|---|---|
| 跨平台管線（單一入口 `run.py`） | ✅ | [OVERNIGHT_PLAN.md](OVERNIGHT_PLAN.md) §2 |
| 觀察與發現整理 | ✅ | [FINDINGS.md](FINDINGS.md) |
| 文件索引（哪個資訊在哪一頁） | ✅ | [research/07-document-index.md](research/07-document-index.md) |
| 時間軸回測（回測 → 即時監控） | ✅ | [OVERNIGHT_PLAN.md](OVERNIGHT_PLAN.md) §5 |
| Bedrock 接通（三個 AI 落點） | ✅ | [ENVIRONMENTS.md](ENVIRONMENTS.md) §2 |
| 會場斷網降級 | ✅ | [OVERNIGHT_PLAN.md](OVERNIGHT_PLAN.md) §6 |
| **會操作網站的 agent**（12 tool、SSE、稽核軌跡） | ✅ | [MERGE_PLAN.md](MERGE_PLAN.md) |
| **MCP**（同一組 tool 給外部客戶端） | ✅ | [MERGE_PLAN.md](MERGE_PLAN.md) §4 |
| **帳號登入**（agent 回饋要記得是誰） | ✅ | [MERGE_PLAN.md](MERGE_PLAN.md) §1 |
| 容器化與 ECS 部署（目前縮到 0） | ✅ | [DEPLOY.md](DEPLOY.md) |

驗證狀態：`python run.py test` → **379 passed**（本機 Windows）。
那一條 skip 是 POSIX 的 flock 沒有逾時可測，與資料無關。
`python run.py lint` → 全過。

---

## 待辦（依決賽價值排序）

### 1. ~~VLM 抽取~~ ✅ 2026-09-12 已接通並量測

`BedrockBackend` 以前沒有任何東西呼叫它——132 份財報是開發階段由 subagent
抽好進版控的，「交付路徑跑在 Bedrock 上」在程式裡是一句宣告而不是一條路。
現在有驅動程式了：

```bash
python run.py extract-bedrock -- --dry-run   # 先看要送幾頁，不花錢
python run.py extract-bedrock                # 5 張人工核對過的基準頁
python run.py score-extraction               # 對 ground truth 逐格比對
```

實測 **236/236 = 100.00%**，假零 0、錯值 0（見
[ENVIRONMENTS.md](ENVIRONMENTS.md) §2.7）。

⚠️ **但第一次跑是 67.8%，而恆等式 79/79 全過。** 模型把資產負債表的
「占比 %」欄當成一個期間塞進 `values`，整表從第二欄起錯位；占比自己也滿足
加總關係，所以自我驗算抓不到。**恆等式全過不等於抽對了**——ground truth
那 5 張頁面是唯一能發現這件事的東西。修法已寫進 `EXTRACTION_PROMPT` 規則 5。

批次模式（`-- --year 113`）的頁碼是結構推定、未經表頭確認，產出是待覆核草稿。
腳本**寫不進 `data/extracted/`**（`_assert_safe_out()` 擋下來）：那 132 份是
下游每一項法遵發現的依據，要取代必須是人明確做的決定。

### 2. 簡報敘事

[FINDINGS.md](FINDINGS.md) §0 與 §4.1 是現成的開場。三個必講的數字：
量體超載 34%（不是虐童）、分層後排序翻轉、56% 未來違規者沒有前科。

### 3. 現場檢查清單

- [ ] `python run.py bedrock-check` — 憑證會過期，**每天早上重跑**
- [ ] 連會場 wifi 後開一次 `http://127.0.0.1:8000`，確認地圖與時間軸
- [ ] `python run.py frontend` 重建一次，確認 payload 是最新的

### 4. 還沒做的（見各自文件）

- 公校決算書頁內文字未進全文索引（[07](research/07-document-index.md) §6）
- 查詢頁籤的 `institution`／`year` 仍要呼叫端給（planner 已接 Bedrock，
  `/api/health` 的 `chat_planner` 會說現在是哪一個）
- **agent 還操作不到的功能**：掃描主控台、地圖控制項（反灰／區名／著色依據／
  群集／派工容量）、建議書清單瀏覽、單園排名軌跡、員工數、即時輿情。
  盤點與估時見 [MERGE_PLAN.md](MERGE_PLAN.md) §9b
- 跨年度比較視圖：「110 到 113 各差多少」要跑四次查詢
- `pyproject.toml` 缺 `[project] name`／`version`，`pip install -e .` 會失敗
  （README 已改指向 `requirements.txt`；**決賽前不動封裝**）

---

## 三件不能忘的界線

1. **輸出是「建議查核」的稽查優先序，不是「疑似不法」的違法認定。**
   不確定時標「資料不足」，不標「低風險」。所有面向使用者的文字都適用。
2. **個別機構分數不對外公開揭露**（[aws-architecture.md](architecture/aws-architecture.md) §6.5）。
   這就是 repo 設 private 的原因——裡面有 1,213 所真實幼兒園的排名與 143 份指名建議書。
3. **憑證絕不進版控。** 競賽規範明文要求。`.env` 已 gitignore；
   打包資料夾給別人前記得先拿掉。

---

## 這台機器的現況（2026-09-12）

| | 狀態 |
|---|---|
| `.venv` | 有，Python 3.11 |
| `data/raw`（1.8 GB） | 有，由 `run.py setup-raw` 從根目錄兩個 zip 還原；**不得轉散布** |
| git remote | `yayayouyou/Hackathon_Smart_Watchdog_chmod777`（private） |
| 本機服務 | `python run.py serve -- --port 8001` |

⚠️ **`data/raw`、`data/runtime`、`data/interim`、`dist` 都不進版控**，
隊友 clone 後要自己跑 `setup` 與 `frontend`。沒有 `data/raw` 也能跑，
只有證據頁截圖會 404（文字證據與頁碼不受影響）。

⚠️ **`data/runtime/watchdog.sqlite` 是各機器獨立的**：帳號、agent 對話稽核
軌跡、稽查員回饋都只存在本機。要跨機器共享得把 `DATABASE_URL` 指向 RDS
（程式不用改）。

---

## 這個 repo 的完整文件地圖

| 檔案 | 內容 |
|---|---|
| [`CLAUDE.md`](../CLAUDE.md) | **開發前必讀**。已踩過的坑，每一條對應一個實際錯誤 |
| [`README.md`](../README.md) | 專案簡介與安裝 |
| [`FINDINGS.md`](FINDINGS.md) | 觀察與發現（裁罰分析、抽取、ML、關聯、被推翻的假設） |
| [`ENVIRONMENTS.md`](ENVIRONMENTS.md) | 各環境怎麼啟用，含 Bedrock 三個實測陷阱與錯誤對照表 |
| [`OVERNIGHT_PLAN.md`](OVERNIGHT_PLAN.md) | 9/11 夜的工作紀錄與決策理由 |
| [`research/01`–`07`](research/) | 資料集、鑑識訊號、外部資料、Phase 1 計畫與結果、即時監看、文件索引 |
| [`architecture/`](architecture/) | AWS 架構、API 申請指引、資料存取需求；**§6 是倫理與治理** |
| [`data/README.md`](../data/README.md) | 每個資料目錄是什麼、哪些要自己準備 |
