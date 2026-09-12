# 小小守護員 Smart Watchdog

新北市教保機構風險預警系統。AI × 鑑識會計。
2026 新北市 AI 智慧城市黑客松，教育局組。決賽 2026/9/12–13。

**輸出是「建議查核」的稽查優先序，不是「疑似不法」的違法認定。**
全市 1,213 園中 94.8% 沒有公開財報——對這些園而言那是涵蓋範圍限制，
不是合規證明。不確定時標「資料不足」，不標「低風險」。

---

> **第一次接手這個專案？先讀 [`docs/HANDOVER.md`](docs/HANDOVER.md)**——現況、待辦、三條不能忘的界線，都在那一份。

---

## 在新機器上跑起來

相依清單是 `requirements.txt`，**不是** `pyproject.toml`——後者只放 ruff 與
pytest 的設定，刻意不含 `[project]`，所以 `pip install -e .` 不適用。
（那張表原本只寫了 `optional-dependencies` 而缺 `name`／`version`，
會讓 `ruff check .` 整份解析失敗、一行程式都沒檢查。）

macOS／Linux：

```bash
git clone <repo> && cd Hackathon_Smart_Watchdog

python3 -m venv .venv                      # 需要 Python 3.9+
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install "fastapi>=0.110" "uvicorn[standard]>=0.29" "httpx>=0.27"

PYTHONPATH=src .venv/bin/python scripts/build_frontend.py   # 產生 dist/
PYTHONPATH=src .venv/bin/python scripts/serve.py            # → :8000
```

Windows（PowerShell）——執行檔在 `Scripts\` 不是 `bin/`，`PYTHONPATH` 要另外設：

```powershell
py -3.11 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pip install "fastapi>=0.110" "uvicorn[standard]>=0.29" "httpx>=0.27"

$env:PYTHONPATH = "src"
.venv\Scripts\python scripts\build_frontend.py
.venv\Scripts\python scripts\serve.py
```

> pip 的相依解析器在這批釘版上可能長時間回溯（實測 13 分鐘沒裝上任何套件）。
> 有 [uv](https://github.com/astral-sh/uv) 的話 `uv pip install --python .venv/Scripts/python.exe -r requirements.txt`
> 幾十秒就完成，結果一樣。

開 <http://127.0.0.1:8000>。**不是 `dist/index.html`**——那是靜態單檔版，
沒有圖磚地圖、沒有查詢、沒有助理、沒有掃描主控台（Artifact 的 CSP 擋掉 fetch
與非白名單腳本，那些在靜態版做不到）。

驗證（把 `.venv/bin/python` 換成 `.venv\Scripts\python` 即為 Windows 版）：

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/ -q
# 424 passed   ← POSIX 上是 423 passed + 1 skipped，
#                那一條 skip 是 flock 沒有逾時可測，正常
.venv/bin/ruff check .
```

## 憑證

**一項都不填也完整可跑。** 分析管線、靜態版、動態版、新聞與 PTT 兩個即時
管道都不需要金鑰。憑證只**開啟更多管道**。

`.env` 放在專案根目錄（已 gitignore，不會跟著 repo 過去，要自己重建）：

```bash
cp .env.example .env
```

| 變數 | 開啟什麼 | 取得 |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | Google 底圖、卷宗內的地圖評論 | console.cloud.google.com，當天可得 |
| `APIFY_TOKEN` | Threads 貼文掃描 | apify.com → Settings → Integrations |
| `AWS_*` | 三個 AI 落點改走 Bedrock | 決賽當天由主辦方提供 |

確認讀到了：

```bash
PYTHONPATH=src .venv/bin/python scripts/check_credentials.py
```

詳細申請步驟見 [`docs/architecture/API申請指引.md`](docs/architecture/API申請指引.md)。

## 資料

`data/processed/` 的分析結果**已進版控**，所以 clone 下來就能直接建前端、
跑服務、跑測試，不需要原始資料集。

`data/raw/`（主辦方資料集，1.8 GB）**不進版控也不得轉散布**。少了它只影響
兩件事：重跑抽取管線，以及 `test_every_statement_declares_its_source`
那一條會 skip（它要核對 ground truth 引用的來源 PDF 是否存在）。

`data/runtime/` 是掃描主控台的花費帳本與任務紀錄，機器各自獨立，不同步。

## 這個系統在做什麼

**軌 A（廣度）** 全市 1,213 園的統計排序。時序切分實測 AUC 0.640，
前 100 名命中率為隨機抽查的 2.17 倍。

**軌 B（深度）** 132 份非營利財報的視覺抽取與法遵檢核，產出可引述條號的
稽核發現。10 園有發現，其中 4 園屬高嚴重度。

> 這裡曾經寫 22 園。附註五的檢核用一元的絕對容差比對「揭露數」與「年末應付
> 受託法人餘額」，但這兩個數字本來就會差掉年度內以現金結清的部分——全語料庫
> 127 個園-年的差額中位數是 0.00%，而被判未通過的 17 筆分成兩群：四筆
> +70%～+140%，其餘十三筆落在 ±3% 內（最小的只差 0.5%）。改成相對重大性
> 門檻 10% 後剩 4 筆，**高嚴重度的 4 園一個沒少**——被移除的全是誤判。
> 見 `features/compliance.py::NOTE5_MATERIALITY` 與
> `tests/test_compliance_materiality.py`。

兩軌**刻意不混成一個分數**：軌 B 是可引述的事實，軌 A 是統計推論。
稽查員打電話給園所時，這兩者要講的話完全不同。

**唯一經驗證的提前訊號**是官方評鑑「部分指標通過」：OR 2.72（p=0.0009），
分層後仍成立（私立 2.31、無前科 2.44），中位提前 **268 天**。
新聞、PTT、Threads 的提前量都是 0——它們消費的是已經發生的官方裁罰紀錄，
所以定位是即時監看，不是預測。

## 目錄

```
src/smart_watchdog/
  extract/      財報視覺抽取。backends.py 抽象後端（Bedrock / Recorded）
  features/     特徵工程。每個函式吃 as_of，拒絕看它之後的資料
  risk/         優先序組裝
  report/       稽核建議書生成與驗證
  realtime/     即時管道、掃描主控台的價目表與花費帳本
  api/          FastAPI 端點
  scrape/       外部資料擷取
webapp/         動態版前端
frontend/       靜態版樣板 → dist/index.html
scripts/        管線與工具
docs/           架構決策與研究紀錄
```

開發前先讀 [`CLAUDE.md`](CLAUDE.md)——裡面是已經踩過的坑，
每一條都對應一個實際發生過的錯誤。

## 資料來源與授權

機構基本資料、裁罰紀錄與收費明細的**原始來源**是
[全國教保資訊網](https://ap.ece.moe.edu.tw/webecems/pubSearch.aspx)（教育部），
**取得管道**是 [`kiang/ap.ece.moe.edu.tw`](https://github.com/kiang/ap.ece.moe.edu.tw)
鏡像（江明宗維護）。上游把兩者分開授權：**程式碼 MIT、資料 CC-BY**，
而 CC-BY 要求標註到原始來源——那是授權條件，不是禮貌。

為什麼走鏡像而不是官方即時頁面：官方裁罰紀錄**有保存期限會下架**，
只爬官方會拿到被截斷的標籤，而且它長得像乾淨資料、不會報錯。
完整的來源與授權對照（含評鑑、採購、公告、界線各自的現況）見
[`data/external/README.md`](data/external/README.md)。
