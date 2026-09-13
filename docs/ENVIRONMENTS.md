# 環境怎麼啟用

**給隊友的一份操作卡。** 每一段都可以照抄執行，Mac 與 Windows 指令相同。

> ⚠️ **憑證絕不進版控。** 競賽規範明文要求：上傳 GitHub 前務必確認未包含任何
> AWS Access Key、API Token 或密碼。本專案的所有憑證只放在專案根目錄的
> `.env`，該檔已列入 `.gitignore`。**不要把 `.env` 貼進聊天室、Issue 或簡報。**

---

## 0. 三十秒版

```bash
python run.py setup            # 建 venv、裝相依（有 uv 就用 uv，快很多）
cp .env.example .env           # 然後把憑證填進去，見 §2
python run.py seed-users       # ⚠️ 建帳號。沒有帳號連地圖都看不到
python run.py bedrock-check    # 確認 AWS 打得通
python run.py frontend         # 產生前端資料
python run.py dataroom-slice   # ⚠️ 文件控管室要這份，少了整室只有一行錯誤訊息
python run.py serve            # → http://127.0.0.1:8000
```

⚠️ **`dataroom-slice` 也不是選配。** 它產出的 `data/interim/dataroom` 有
gitignore，所以每一台新機器都要自己跑一次；沒跑的話文件控管室會顯示
「資料室切片不存在」。它在完整 pipeline 裡（`in_pipeline=True`），但照著上面
這份清單跑的人不會經過完整 pipeline——決賽當天換一台機器就是這個情境。

⚠️ **`seed-users` 不是選配。** 動態版有全螢幕登入牆。請在 `.env` 分別設定
`SEED_INSPECTOR_EMAIL/PASSWORD` 與 `SEED_ADMIN_EMAIL/PASSWORD`；兩個 Email
必須不同。帳號固定為「稽查人員」與「系統管理員」，但目前共用相同介面、功能與
全市查詢範圍，`role` 只作身分標記，不是權限邊界。

本機或受控展示站若需要一鍵登入，可另設 `QUICK_LOGIN_ENABLED=true`。正式環境
必須維持 `false`；快速登入由後端直接建立 session，前端不會取得 seed 密碼。

`python run.py --list` 會列出全部任務。

---

## 1. 本機環境（不需要任何憑證）

**一項憑證都不填，系統也完整可跑。** 分析管線、靜態版、動態版、新聞與 PTT
兩個即時管道都不需要金鑰。憑證只**開啟更多管道**。

| 需要 | 說明 |
|---|---|
| Python 3.9+ | 實測用 3.11。`python run.py setup` 會自己建 venv |
| 相依套件 | `requirements.txt`（**不是** `pyproject.toml`，後者缺 `[project] name`，`pip install -e .` 會失敗） |
| `data/raw/` | **可選**。主辦方 1.8 GB PDF，不進版控。抽取結果已進版控，展示不需要它 |

```bash
python run.py setup
python run.py test        # 707 passed（2026-09-13，Windows）
                          # 少了 dataroom-slice 會變成 697 passed, 10 skipped
python run.py serve
```

若要還原主辦方資料集（**不要用 `unzip`**，zip 檔名是 Big5）：

```bash
python run.py setup-raw ~/Downloads      # 放著那兩個 zip 的目錄
```

---

## 2. AWS Bedrock（決賽環境）

### 2.1 開通

1. 開 <https://catalog.us-east-1.prod.workshops.aws/join>
2. 點 **Email one time password**，用**報名時的 Email** 登入
3. 輸入一次性驗證碼
4. 輸入小組 Access Code（見主辦方信件；**組內共用，勿外流**）

開發環境組別編號：**教育 team12**。
**環境僅於 9/12 08:00 – 9/13 13:00 開放。**

### 2.2 拿憑證

workshop 頁面會給四行 PowerShell。把**值**貼進專案根目錄的 `.env`：

```
AWS_DEFAULT_REGION=us-west-2
AWS_REGION=us-west-2
AWS_ACCESS_KEY_ID=ASIA...
AWS_SECRET_ACCESS_KEY=...
AWS_SESSION_TOKEN=IQoJ...
```

三件事要知道：

- **是臨時憑證**（`ASIA` 開頭、含 `AWS_SESSION_TOKEN`），**會過期**。
  過期的表徵是 `ExpiredTokenException`——回 workshop 頁面重新複製四行即可。
- `AWS_REGION` 與 `AWS_DEFAULT_REGION` 兩個都填，boto3 與 SDK 讀的不一定是同一個。
- 不想寫檔也可以直接在 shell 設環境變數，程式會優先讀環境變數。

### 2.3 驗證

```bash
python run.py bedrock-check            # 憑證 + 可用模型
python run.py bedrock-check -- --full  # 再加三個 AI 落點的端到端測試
```

**決賽當天第一件事就是跑這個。** 可用模型是**帳號層級的權限**，換帳號或換一天
都可能不一樣。

### 2.4 這個帳號實測出來的三件事（2026-09-12 驗證）

| # | 發現 | 後果 |
|---|---|---|
| 1 | 所有 Anthropic 模型**只支援推論設定檔** | 模型 ID 必須加 `us.` 前綴。裸 ID 會得到 `on-demand throughput isn't supported` |
| 2 | **`AnthropicBedrockMantle` 回 404** | 要用 `AnthropicBedrock`（InvokeModel 路徑）。功能一樣：視覺、結構化輸出、強制工具皆實測可用 |
| 3 | **Claude 5 世代 AccessDenied** | 27 個設定檔逐一實測，13 個可用。`claude-sonnet-5`／`claude-opus-5`／`claude-fable-5`／`opus-4-7`／`opus-4-8` 全部不可用 |

所以專案用的是**實測打得通的最強者**，不是文件上最新的：

| 落點 | 模型 | 為什麼 |
|---|---|---|
| 財報視覺抽取 | `us.anthropic.claude-sonnet-4-6` | 量大、要讀掃描影像、要結構化輸出 |
| 稽核建議書 | `us.anthropic.claude-opus-4-6-v1` | 寫錯會傷害真實機構，用最強的 |
| 自然語言查詢 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | 只做意圖解析，延遲比深度重要 |

**模型 ID 只有一個來源**：[`src/smart_watchdog/bedrock.py`](../src/smart_watchdog/bedrock.py)。
要換模型改那裡，不要在呼叫端各寫一份。

### 2.5 端到端實測結果

```
✓ 視覺抽取：{'statement': '資產負債表', 'cash': 8868744}
    ← 直接讀掃描影像，數字與 ground truth 完全一致
✓ 自然語言查詢：{"filters": {"town": "板橋區", "type": 2, "has_penalty": true},
                 "sort": "rank", "limit": 100, "intent": "list"}
✓ 稽核建議書：141 份，通過驗證 141／141
```

用 Bedrock 產建議書：

```bash
python run.py letters -- --backend bedrock
```

Bedrock 草稿**沒通過驗證就不會寫出來**，會自動退回模板並記下原因——
一封不能用的信，好過一封寄給真實園所的錯信。

### 2.6 三個落點各自怎麼切換

| 落點 | 預設 | 怎麼指定 |
|---|---|---|
| 財報視覺抽取 | 不自動跑（會花錢） | `python run.py extract-bedrock`；`-- --dry-run` 先試算頁數 |
| 稽核建議書 | `template`（確定性、永不出錯） | `python run.py letters -- --backend bedrock` |
| 自然語言查詢 | **auto**——有憑證就走 Bedrock | 環境變數 `CHAT_PLANNER=bedrock｜keyword｜auto` |

`/api/health` 的 `chat_planner` 欄位會直接回報**現在**是哪一個在做計畫，
不必先問一題才知道。若某次請求打 Bedrock 失敗（斷網、憑證過期、限流），
該次回應會降級成 `planner: "keyword"` 並附上 `planner_fallback` 說明原因——
**降級一定會說出來**，因為悄悄換成關鍵字比對而宣稱跑在 Bedrock 上是同一件事。

可用性判斷只看本機有沒有 `anthropic` 與 AWS 金鑰，**不打網路**。
否則會場斷網時每一次查詢都要先等一個 timeout 才降級，等於沒有降級。

### 2.7 視覺抽取的實測準確率（2026-09-12）

`python run.py extract-bedrock` 跑 5 張人工核對過的基準頁，
再用 `python run.py score-extraction` 對 ground truth 逐格比對：

```
儲存格準確率：236/236 = 100.00%
恆等式：63/63 通過
會產生錯誤財務陳述的格數（假零＋錯值＋多餘）：0 (0.00%)
```

> 恆等式從 44 變成 63，不是因為抽得更好，而是因為**有 19 條以前根本沒跑**。
> `_validate_variance()` 用完全相等比對找 `預算數`／`決算數`／`差異數` 三欄，
> 但規則 1 要求逐字照抄，表上印的是 `預算數(a)`／`決算數(b)`／`差異數(c)=(b)-(a)`
> ——永遠對不上，整族逐列檢核靜靜變成 skipped，而回報的「N/N 通過」看不出少驗了什麼。
> 改成含有比對後，收支餘絀表的決算數欄與差異數欄整欄互換才抓得到。

⚠️ **這個數字是修過一次 prompt 之後才拿到的，過程本身就是結論。**
第一次跑的結果是**恆等式 79/79 全過、逐格準確率卻只有 67.8%**：
模型把資產負債表的「占比 %」小欄也當成一個期間塞進 `values`，
於是 `values[1]` 是本期占比而不是比較期金額，整份表從第二欄起錯位。
**自我驗算抓不到它**——占比在同一個分母下同樣滿足加總關係，
各明細的 % 加起來就等於小計的 %。

意思是：**恆等式全過不等於抽對了。** 修法是把欄位約定寫進
`extract/schema.py` 的 `EXTRACTION_PROMPT` 規則 5（資產負債表的 % 進
`percents`；收支餘絀表的「執行率」則是表頭上自成一欄，留在 `values`）。
這也是為什麼 ground truth 那 5 張頁面不能省：沒有它，
我們會拿著 79/79 的恆等式報告一個 67.8% 的抽取。

---

## 3. 其他憑證（全部可選）

| 變數 | 開啟什麼 | 取得 | 沒有的話 |
|---|---|---|---|
| `GOOGLE_MAPS_API_KEY` | Google 底圖、卷宗內地圖評論 | console.cloud.google.com → 啟用 **Places API (New)** → 建金鑰 → 限制為 Places API | 用 OpenStreetMap 底圖，其餘功能不受影響 |
| `APIFY_TOKEN` | Threads 貼文掃描 | apify.com → Settings → Integrations | 少一個即時管道 |
| `THREADS_ACCESS_TOKEN` | Threads 官方 API | developers.facebook.com（需 App Review，2–4 週） | 同上 |
| `VENDOR_FEED_PATH` | PTT／Dcard／FB 全網覆蓋 | 循共同供應契約採購輿情服務 | 同上 |

```bash
python run.py check-credentials     # 實際打一次 API，不是只檢查有沒有填
```

---

## 4. 會場網路

**地圖函式庫已就地保存**在 `webapp/vendor/`，會場擋 CDN 也能用。
圖磚（openstreetmap.org）無法就地保存，但程式偵測到連續三次圖磚失敗會**自動
改用行政區界線**，標記與所有分析功能不受影響。實測：擋掉所有非 127.0.0.1 的
請求後，1,217 個標記、時間軸、右側面板全部正常。

**Bedrock 需要網路。** 會場若連不上 AWS，三個 AI 落點會退回非模型路徑
（抽取用已進版控的結果、建議書用模板、查詢用關鍵字規則），系統仍完整可展示。

---

## 5. 常見錯誤對照

| 訊息 | 意思 | 怎麼辦 |
|---|---|---|
| `ExpiredTokenException` | 臨時憑證過期 | 回 workshop 頁面重拿四行，更新 `.env` |
| `on-demand throughput isn't supported` | 模型 ID 少了 `us.` 前綴 | 用 `bedrock.py` 裡的 ID |
| `AccessDeniedException ... is not available` | 該模型本帳號未開通 | `python run.py bedrock-check` 看哪些可用 |
| `404` + 用了 Mantle | Mantle 端點在本帳號不存在 | 改用 `AnthropicBedrock` |
| `Unable to locate credentials` | `.env` 沒填或沒被讀到 | 確認在專案根目錄，且 key 沒有多餘空白 |
| `snapshot integrity mismatch` | git 把 CRLF 寫進內容定址快照 | `.gitattributes` 已修；若仍發生，`git rm --cached -r data && git checkout -- data` |
| `ModuleNotFoundError: fcntl` | 用到舊版程式碼 | 已修（`src/smart_watchdog/filelock.py`），確認是最新版 |
| Telegram bot 沒反應，`/api/health` 的 `telegram_bot.last_error` 寫 409 | 另一個行程也在用同一個 token 輪詢 | 只留一個；其他地方設 `TELEGRAM_BOT_POLLING=false` |
| LINE 後台按「驗證」失敗，或傳訊息沒回應 | webhook 不是公開 HTTPS、或 secret 不對（簽章 400） | 走 §6 的通道；確認 `LINE_CHANNEL_SECRET` |
| `ServiceWorker script evaluation failed` | `sw.js` 執行時丟例外（語法檢查照樣會過） | 看 `tests/test_pwa.py` 的實際執行測試；檔頭說明一律用 `//`，見 §6 |
| 手機上沒有「安裝／加入主畫面」 | 不是 HTTPS（區網 IP 也不算） | 走 §6 的 cloudflared 通道 |

程式裡的 `bedrock.explain_error()` 會把上面前五種翻成中文並附下一步。

---

## 6. 手機（PWA）

派工台可以裝成手機 app：Android Chrome 會跳「安裝」，iOS Safari 用分享選單的
「加入主畫面」。安裝後全螢幕開啟、主畫面上是守護犬圖示。

### 6.1 手機要連得到，而且**必須是 HTTPS**

Service worker 只在安全來源執行。`http://127.0.0.1` 在**本機**算安全，但手機連
`http://192.168.x.x:8000` **不算**——網頁照樣打得開，只是不會出現安裝、也沒有離線殼，
而且不會有任何錯誤訊息。本機目前沒有通道工具，用 winget 裝 cloudflared：

```powershell
winget install Cloudflare.cloudflared      # 一次就好
python run.py serve                        # 照常啟動（127.0.0.1:8000）
cloudflared tunnel --url http://localhost:8000
```

cloudflared 會印出一個 `https://<隨機字>.trycloudflare.com`，手機開那個網址即可。
**LINE webhook 也要 HTTPS，同一條通道可以一起用**（`/api/webhook/line`）。
注意：快速通道每次重開網址都會變，LINE 後台的 webhook URL 要跟著改；
要固定網址得登入 Cloudflare 建具名通道。

⚠️ 這個網址是公開的。派工台有登入保護，但**展示結束就關掉 cloudflared**，
不要讓一份含真實機構的稽查名單長時間掛在公開網址上。

### 6.2 快取了什麼、沒快取什麼

- **快取**：HTML、CSS、JS、Leaflet、圖示——也就是「殼」。斷線時打得開，並顯示離線提示。
- **絕不快取**：`/api/*` 與 `/mcp`。稽查名單、通報、卷宗一律走網路；手機遺失時裝置上
  **沒有**任何機構資料。這條由 `tests/test_pwa.py` 釘住。
- 線上時一律拿新檔（網路優先），改了前端不必叫大家清快取。

### 6.3 兩個踩過的坑

1. **`sw.js` 的說明不能寫在區塊註解裡。** 說明文字裡只要出現「星號＋斜線」（例如把
   /api/ 加星號寫成粗體），註解就會提早結束，後面的字被當成程式。語法合法、
   `node --check` 照過，一執行就 ReferenceError，瀏覽器只說「script evaluation failed」，
   PWA 安靜地註冊不上。MaiCoin 專案的 `frontend-pixel/public/sw.js` 就是這個狀態。
2. **在 Windows 用無頭 Chrome 驗 PWA，設定檔目錄要放在短路徑。** 快取目錄會再往下疊
   好幾層雜湊資料夾，超過 260 字元時 Cache Storage 寫入失敗，錯誤訊息卻是
   「Entry already exists」而快取是空的——看起來像 SW 寫壞了，其實是測試環境的問題。
   另外無頭 Chrome 的 `--window-size=390` 實際排版寬度是 526px，驗手機版面要用
   DevTools Protocol 的行動裝置模擬，不能只縮視窗。

---

## 7. Telegram bot（小彩蛋）

`t.me/Little_Guardian_bot`：在手機私訊裡按一下，看**建議查核分布地圖＋待稽核清單**，
或直接打一句話答詢（與派工台的查詢同一條路，只查不做）。

```
TELEGRAM_BOT_TOKEN=<BotFather 給的 token>     # 能控制整個 bot，勿外流
TELEGRAM_BOT_USERNAME=Little_Guardian_bot
TELEGRAM_DEMO_PASSCODE=<一組暗號>              # 展示用，結束後移除
```

**展示時只要一步**：手機打開 `https://t.me/Little_Guardian_bot?start=<暗號>`，按「開始」
就綁到示範稽查員帳號，接著按「🗺 地圖＋待稽核清單」。

三件事要知道：

- **沒綁定的聊天室拿不到任何資料**，群組一律拒絕。bot 是公開搜得到的，而清單是真實
  機構——登入頁寫著「僅供內部使用」，這道門不能因為換到聊天室就消失。正式的綁定走
  派工台登入後 `POST /api/bot/telegram/bind-code` 產生的一次性碼（10 分鐘、一次）。
- **暗號可以重複使用**，所以展示結束請從 `.env` 拿掉或換掉；沒設時這條路不存在。
- **同一個 token 同時只能有一個行程在輪詢**（long polling，不需要公開網址）。
  本機跑著 `run.py serve` 時，另開的測試 server 請設 `TELEGRAM_BOT_POLLING=false`，
  否則兩邊都會拿到 409。狀態看 `/api/health` 的 `telegram_bot`。

地圖是伺服器端用 Pillow 畫的 PNG，需要中文字型：Windows 用微軟正黑體，
Linux 容器請裝 `fonts-noto-cjk`，否則字會變成方框（圖照樣產得出來）。

---

## 8. LINE bot

官方帳號 **Guardian_BOT（@727ndzcn）**。功能與 Telegram 相同（地圖＋待稽核清單、一句話答詢），
內容與閘門共用 `bots/content.py`、`bots/linking.py`，只換傳輸層。

```
LINE_CHANNEL_ID=<Channel ID>
LINE_CHANNEL_SECRET=<Channel secret>          # 驗簽用，勿外流
LINE_CHANNEL_ACCESS_TOKEN=<access token>      # 可用 ID+secret 換 30 天短期 token；401 時程式會自動重換
PUBLIC_BASE_URL=https://<通道網址>            # 地圖圖片要用；沒設時只傳文字清單
LINE_DEMO_PASSCODE=<暗號>                     # 沒設就沿用 TELEGRAM_DEMO_PASSCODE
```

**LINE 一定要公開的 HTTPS 網址，兩個地方都要：**

1. **Webhook**：LINE 只會把訊息推過來，不能像 Telegram 那樣自己去拉。
   LINE Developers → Messaging API → Webhook URL 填 `https://<通道網址>/api/bot/line/webhook`，
   打開 **Use webhook**，並關掉「自動回應訊息」（否則官方帳號會自己搶先回罐頭訊息）。
2. **地圖圖片**：LINE 的圖片訊息不能上傳檔案，只收網址。地圖由 `/api/bot/map.png` 以
   **簽章網址**提供，10 分鐘過期、改一個字就 403——名單上的紅點不能變成誰都下載得到的圖。

**展示時**：手機加好友 → 傳「綁定 ＋ 暗號」→ 按下方「地圖＋清單」。
群組與多人聊天室一律拒絕；webhook 簽章驗不過一律 400。狀態看 `GET /api/bot/line/status`（需登入）。
