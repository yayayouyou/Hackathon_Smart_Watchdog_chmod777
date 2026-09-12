# 交接：現況與待辦

**2026-09-12 從 `D:\Hackathon_Smart_Watchdog` 搬到這裡並建 private repo。決賽 9/12–13，
9/13 13:00 前交件。**

先讀這份，再依需要展開到各自的文件。

---

## 30 秒上手

```bash
python run.py setup          # 建 venv 裝相依（這個資料夾還沒有 venv）
cp .env.example .env         # 填憑證，見 docs/ENVIRONMENTS.md
python run.py bedrock-check  # 決賽當天第一件事
python run.py frontend
python run.py serve          # → http://127.0.0.1:8000
```

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

驗證狀態：`python run.py test` → **294 passed**（有 `data/raw` 時）／
293 passed + 1 skipped（沒有時）。`python run.py lint` → 全過。

---

## 待辦（依決賽價值排序）

### 1. VLM 抽取 ← 使用者指定自己處理

Bedrock 的視覺路徑**已實測可用**：直接讀掃描財報頁，抽出的現金
8,868,744 與 ground truth 完全一致。

```bash
python run.py bedrock-check -- --full     # 會跑一次真實的視覺抽取
```

現況：132 份非營利財報**已全部抽取完成**並進版控，所以展示不依賴重跑。
要重跑的是 `src/smart_watchdog/extract/backends.py` 的 `BedrockBackend`，
已接上正確的 client 與模型 ID。

### 2. 簡報敘事

[FINDINGS.md](FINDINGS.md) §0 與 §4.1 是現成的開場。三個必講的數字：
量體超載 34%（不是虐童）、分層後排序翻轉、56% 未來違規者沒有前科。

### 3. 現場檢查清單

- [ ] `python run.py bedrock-check` — 憑證會過期，**每天早上重跑**
- [ ] 連會場 wifi 後開一次 `http://127.0.0.1:8000`，確認地圖與時間軸
- [ ] `python run.py frontend` 重建一次，確認 payload 是最新的

### 4. 還沒做的（見各自文件）

- 公校決算書頁內文字未進全文索引（[07](research/07-document-index.md) §6）
- 問句理解層：`institution`／`year` 還要呼叫端給
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

## 兩個資料夾同時存在

| | `D:\Hackathon_Smart_Watchdog`（舊） | `D:\Hackathon_Smart_Watchdog_chmod777`（新） |
|---|---|---|
| `.venv` | 有 | **無**，要 `python run.py setup` |
| `data/raw`（1.8 GB） | 有 | 無（不得轉散布，抽取結果已進版控） |
| git remote | 無 | `yayayouyou/Hackathon_Smart_Watchdog_chmod777`（private） |

**動手前先確認在哪一個。** 新的是主線。

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
