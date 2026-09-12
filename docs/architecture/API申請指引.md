# API 申請指引

**先講結論：一項都不申請，系統也完整可跑。** 分析管線、靜態版、動態版、
新聞與 PTT 兩個即時管道都不需要任何金鑰。憑證只會**開啟更多管道**。

依「多久拿得到」排序：

| # | 項目 | 取得時間 | 決賽前來得及？ | 開啟什麼 |
|---|---|---|---|---|
| 1 | Google Maps Platform | **當天** | ✅ | Google 地圖評論、Google 底圖 |
| 2 | AWS Bedrock | 決賽當天由主辦方提供 | ✅ | 財報抽取、建議書生成、聊天 |
| 3 | Apify（Threads） | **當天** ✅ 已接上 | ✅ | Threads 貼文關鍵字搜尋 |
| 4 | Threads 官方 API | 2–4 週審核 | ❌ | 同上，但走官方管道 |

---

## .env 放哪裡

專案根目錄，與 `pyproject.toml` 同層：

```
Hackathon_Smart_Watchdog/
├── .env            ← 放這裡（已列入 .gitignore，不會進版控）
├── .env.example    ← 範本，照抄後改名
├── pyproject.toml
└── src/
```

```bash
cp .env.example .env
# 編輯 .env 填入取得的金鑰
```

確認有沒有讀到：

```bash
PYTHONPATH=src .venv/bin/python -c "
from smart_watchdog import config
for c in config.status():
    print(('✅' if c['present'] else '⬜'), c['key'])"
```

動態版的 `/api/health` 也會列出同一份狀態，畫面上看得到缺哪些。

---

## 1. Google Maps Platform（**建議先申請這個**）

當天就能拿到，而且它是唯一在決賽前能真正多開一個管道的項目。

1. 前往 <https://console.cloud.google.com>，以 Google 帳號登入
2. 建立專案（例如 `smart-watchdog`）
3. 左側「API 和服務」→「已啟用的 API」→ **啟用 API 和服務**
4. 搜尋並啟用 **Places API (New)**
   - 要評論就是這一個。底圖是另一個 **Maps JavaScript API**，可一併啟用
5. 「憑證」→ **建立憑證** → **API 金鑰**
6. 點該金鑰 → **限制金鑰** → API 限制 → 只勾選剛才啟用的那兩個
   - 這一步不要略過。不限制的金鑰外洩會被拿去刷別的服務
7. 需要繫結帳單帳戶（有每月免費額度，小量使用通常不會產生費用）

填入 `.env`：

```
GOOGLE_MAPS_API_KEY=AIza...
```

**費用**：計價依 FieldMask 落在**最高**級距，不是逐級加總。含 `reviews`
即 Enterprise + Atmosphere，**每千次 US$25，每月前 1,000 次免費**；
只要星等不要評論是 Enterprise US$20，走另一個免費桶。各級距的免費額度獨立不共用。

959 園（有 place_id 者）全掃一輪：當月第一次 **US$0**（959 < 1,000），
第二次起每輪 US$23.98。開卷宗的每次查詢也吃同一個免費額度，所以計費器是全域的
（`realtime/ledger.py`），否則 1,000 次會在某個沒人按過「掃描」的下午被耗盡。

一次性解析 1,213 園 place_id 走 Text Search Pro（免費 5,000 次/月），實際 **US$0**。

⚠️ Google 自 2025-04-30 起移除主要服務 6–18 歲學生之教育機構的評論。
本市 1,213 園中約 279 園（23%）的地點實體是其母校，這些園不會有評論。

---

## 2. AWS Bedrock（決賽當天）

競賽規定僅限使用 AWS 提供之基礎模型。主辦方當天提供帳號。

```
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

拿到後要做的事（依序，約 15 分鐘）：

1. 在 Bedrock 主控台 →「模型存取」→ 申請 Anthropic 模型存取權
2. `pip install anthropic`
3. 三個落點各自切換後端：
   - 財報抽取：`extract/backends.py` → `get_backend("bedrock")`
   - 稽查建議書：`scripts/build_audit_letters.py --backend bedrock`
   - 自然語言查詢：`api/chat.py` → `get_planner("bedrock")`

模型 ID 需 `anthropic.` 前綴，用 `AnthropicBedrockMantle` client。
**Bedrock 沒有 Batches API 與 Files API**，影像須 base64 內嵌——
這是既有架構決策的原因，見 `docs/architecture/aws-architecture.md` §1。

---

## 3. 社群資料：第三方服務（當天可用）或官方 API（需審核）

### 官方 API 為什麼決賽前來不及

[Meta 的 App Review 每個權限需個別送審，需附操作螢幕錄影，審核 2–4 週](https://singhamandeep.com/threads-api-app-review-permissions/)。
決賽在 4 天後。**在核准前，關鍵字搜尋只會搜到「你自己應用程式的貼文」，
搜不到公開貼文**——等於沒有用。

### 官方 API 申請步驟（供後續實施）

1. <https://developers.facebook.com> → 建立應用程式 → 選 **Threads use case**
   - 系統會產生 Threads 專用的 App ID 與密鑰，用那組，不要用一般的
2. 加入 **Threads API** 產品
3. 在「權限」申請：
   - `threads_basic`（所有端點都需要）
   - `threads_keyword_search`（關鍵字搜尋）
4. 送 App Review：**每個權限各一份**，各附一段螢幕錄影，
   完整展示該權限在你的應用程式中的使用流程
5. 核准後走 OAuth 取得權杖：
   授權碼（1 小時）→ 短期權杖（1 小時）→ **長期權杖（60 天）** → 到期前更新
6. 填入 `.env`：`THREADS_ACCESS_TOKEN=...`

需要一台**公開可連的伺服器**作為 OAuth redirect 目的地。

### 已知限制（核准後仍存在）

- **敏感詞回空陣列**：官方文件載明「對我方認定為敏感或冒犯之關鍵字回傳空陣列」。
  「虐童」「不當對待」這類我們最需要的詞很可能被涵蓋，須核准後實測。
- **速率限制**：官方文件寫每使用者每 24 小時 2,200 次查詢；
  [另有來源指出帶 keyword_search scope 時為每 7 天 500 次](https://www.socialcrawl.dev/blog/threads-api)。
  兩者不一致，以核准後實測為準。無結果的查詢不計入。
- 權杖 60 天到期，需排程更新。

### Apify（已接上，決賽採用這條）

官方 API 需審核，決賽前拿不到。改用 Apify 上的 actor，當天訂閱即可用。

```
APIFY_TOKEN=apify_api_...
APIFY_THREADS_ACTOR=futurizerush~meta-threads-scraper-zh-tw   # 選填，有預設
```

申請：<https://apify.com> 註冊 → Settings → Integrations → API token。

**成本是設計限制，不是註腳。** 計價自 2026-07-02 起是 PAY_PER_EVENT：

```
啟動費 $0.02/GB（FREE）或 $0.005/GB（BRONZE 以上）× ceil(記憶體 GB)
+ 每筆寫進 dataset 的資料 $0.0025
```

actor 的 build 把記憶體寫死 4096MB（`min = max = 4096`），`memory` 參數沒有
下修空間，所以 FREE 方案的啟動費固定 **US$0.08**。對帳（本專案帳號三次實際執行）：

| 回傳筆數 | 實付 |
|---:|---:|
| 0 | US$0.08 |
| 10 | US$0.105 |
| 100 | **US$0.33** |

**「一次執行 US$0.08」是地板價，不是總價。** FREE 方案每月 US$5，
一次 50 筆的掃描 US$0.205，約 24 次；逐園查詢 1,213 × 0.205 ≈ US$249。因此：

- `sweep()` 是主要用法——一次廣詞查詢，本地對 1,213 園做歸屬，一次執行覆蓋全市
- `search()` 單園查詢**預設關閉**（`allow_per_institution=False`）
- 價目表集中在 `realtime/pricing.py`，估算與執行共用同一份，
  「一次掃描要花多少錢」不可能有兩個答案
- `realtime/ledger.py` 在送出請求**之前**預留額度，供應商回報後以實付結算；
  當機的預留不釋放（重啟不得讓額度變多）

換 actor 只需改 `FIELDS` 對應表（目前對應 `text_content`／`post_url`／
`created_at`／`username`）。

其他可選供應商（走 `VendorFeedChannel`，填 `VENDOR_FEED_PATH`）：

| 選項 | 說明 |
|---|---|
| [QSearch](https://www.brainmax-marketing.com/article_d.php?lang=tw&tb=6&id=1445) | 台灣廠商，涵蓋 Threads／PTT／Dcard／FB |
| OpView（意藍） | 已列共同供應契約，收錄逾 22 萬頻道 |

---

## 4. 輿情監測服務（採購）

取得社群全網覆蓋的做法。Dcard、Threads 網頁端與 Facebook 公開社團皆有登入牆，
且 Facebook Groups API 已於 2024-04-22 下架，改由資料服務供應商取得。

| 廠商 | 說明 |
|---|---|
| [OpView（意藍資訊）](https://www.opview.com.tw/gov) | 已列共同供應契約、為最多政府單位採用，收錄逾 22 萬頻道 |
| QSearch | 唯一明確涵蓋 Threads |
| KEYPO | 另一可比較選項 |

採購時於契約載明：**須供應 API 或每日檔案**（不能只有網頁介面）、
須涵蓋 PTT／Dcard／FB 公開社團／Threads、須附貼文原始連結與發布時間供人工查證。

取得後把每日檔案路徑填入 `.env` 的 `VENDOR_FEED_PATH`，
並依廠商格式完成 `VendorFeedChannel.search()`（介面已定義）。

---

## 決賽前的優先順序

1. ~~**Google Maps**~~ ✅ 已接上，1,213 園解析完成、959 筆有 place_id
2. ~~**Apify（Threads）**~~ ✅ 已接上
3. **AWS Bedrock**（當天由主辦方提供）— 三個 AI 落點全部切換

目前 6 個即時管道中 4 個運作中：新聞 RSS、PTT、Threads（Apify）、
Google 地圖評論。
