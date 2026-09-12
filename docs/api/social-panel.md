# 社群聲音面板 API 契約

給做介面的人。三支唯讀端點，把「民眾在社群上怎麼講這家園」攤成可以直接刻的
JSON。實作在 `src/smart_watchdog/api/social.py`，測試在 `tests/test_social_api.py`。

**這個面板的輸出是「可點回原文的清單」，不是判斷。** 系統整體的定位是
**建議查核的稽查優先序**，不是疑似不法的認定（見 `CLAUDE.md` 輸出定位）；
社群內容更弱一層——它是**未經查證的公開內容**，`06-plan` §1 明訂不把社群聲量
併入永久風險分數。所以這三支端點**不回任何分數、評分、星等平均或風險等級**，
畫面上也不該長出來一個。

---

## 1. 端點總覽

| 端點 | 用途 | 會不會連外 |
|---|---|---|
| `GET /api/social/{institution_id}` | 單一機構的社群聲音全貌 | 會（新聞、PTT、Google 評論）|
| `GET /api/social` | 最近有社群聲音的機構（跨機構瀏覽）| 不會（讀資料庫與建置時快照）|
| `GET /api/social/unattributed` | 歸屬拒配的通報佇列（待人工認園）| 不會 |
| `POST /api/social/{institution_id}/draft-reply` | 替一串 @標註通報擬一份回覆**草稿** | 可能（生成 backend 會呼叫 Bedrock）|

單園端點會即時查新聞與 PTT，回應時間約 1–3 秒。想先把畫面撐起來、稍後再補
即時結果的話，用 `?live=false`：只回建置時快照，不等外部請求。

---

## 2. 讀任何一支之前要先懂的九件事

### 2.1 機構 id 有兩種形態，兩種都收

* **8 碼**（`00957c83`）——地圖、卷宗、派工提案用的，就是 payload 的 `points[].i`。
* **完整 UUID**（`00957c83-0061-4581-a587-97629968f371`）——Threads 通報列存的。

`/api/social/{institution_id}` 兩種都認得，回應裡一律同時給 `id`（8 碼）與
`full_id`（完整）。**前端不要自己截字串**：截錯一碼的結果是 404，而 404 在畫面
上看起來像「這一園沒有資料」。

### 2.2 `has_signal` 與 `reason`：空的時候必須說為什麼

每一段（`threads`／`mentions`／`reviews`）與整份回應都有這兩個欄位。

* `has_signal: true` → 有東西可以畫，`reason` 是空字串。
* `has_signal: false` → **`reason` 必定非空**，而且必定說明是「查了沒有」還是
  「根本沒在查」。

**這是整份契約最重要的一條。** 沒有社群聲音的機構如果只回一個空陣列，前端會
畫成綠燈或「正常」——而那是這個系統不被允許說的話。`CLAUDE.md`：不確定時標
「資料不足」而非「低風險」。

### 2.3 `available` 與 `has_signal` 是兩件事

| | `available` | `has_signal` |
|---|---|---|
| 問的是 | 這個管道**現在還在不在收** | 手上**有沒有**東西 |
| false 的意思 | 缺金鑰／缺授權／缺採購 | 這一園目前沒有內容 |

兩者可以任意組合。最常見的誤會是 `available: false, has_signal: true`——那代表
管道現在關著，但資料庫裡有先前同步進來的通報。**那些通報照樣要顯示。**

### 2.4 `attribution_source`：這一列的歸屬是自己掙來的還是繼承來的

| 值 | 意思 | 畫面上該怎麼講 |
|---|---|---|
| `"own"` | 這一則**自己指名**了機構（「文德幼兒園的收費單有問題」）| 可以說「這則點名了本園」|
| `"inherited"` | 這一則沒指名，沿用它所在那一串的主體（「我也遇過」「+1」）| 要說「串下附和」，**不可以**算成一次指名 |
| `"none"` | 自己沒指名、主貼文也沒歸屬 | 未認園 |
| `null` | **這一列早於這個欄位**（資料庫裡的舊列）| 不是「來源不明」；`institution_id` 有值即等同 `own`，空值即等同 `none` |

為什麼要分：一串十則「+1」繼承下來，在 `institution_id` 上看起來就是十次指名。
`06-plan` §6 —— 分析單位是園所 × 事件群集，轉貼同一事件不得重複加權。

### 2.5 `kind`：`mention` 與 `reply`

| 值 | 意思 |
|---|---|
| `"mention"` | 有人把官方帳號 `@` 進來——**一個對機關說話的動作** |
| `"reply"` | 那串底下的回覆——回覆的人多半在跟原 PO 講話，沒有標註任何機關，很可能不知道機關在讀 |

**計數永遠分開回，前端也不准合併。** 要講「有幾個人向教育局反映」時，看的是
`counts.threads.mentions`，不是 `mentions + replies`。

注意 `kind` 與 `is_reply` 不是同一件事：一則 `@` 我們的貼文本身可能是別人串裡的
回覆（`is_reply: true` 但 `kind: "mention"`）。`is_reply` 是結構，`kind` 是來意。

### 2.6 `institution_id` 為 `null` = 歸屬拒配

意思是「**認不出是哪一園**」，**不是**「與機構無關」。歸屬的預設是拒絕（誤配
過「太平洋新聞網」配到太平洋幼兒園、「林口某雙語補習班」配到林口幼兒園），所以
認不出來是常態而不是故障。這些通報全部留在 `/api/social/unattributed`，那是一份
**待人工認園的工作量清單**。

### 2.7 語氣分類：六個欄位，NULL 一律代表「尚未分類」

分類由 `realtime/classify.py` 做，一則貼文一次 Bedrock 呼叫，**只回封閉值**
（enum 與 bool），不回摘要、引文、建議或風險分數。回應裡每一則貼文帶：

| 欄位 | 值域 | 說明 |
|---|---|---|
| `event_category` | `06-plan` §6 的八類＋`unclear` | 兒少安全／人員管理／衛生健康／交通安全／財務收費／營運穩定／招生契約／一般服務抱怨 |
| `tone` | `negative` `neutral` `question` `unclear` | 「請問這樣合理嗎」是 `question` 不是 `negative` |
| `specificity` | `specific` `vague` `unclear` | 有沒有具體的時間、地點、行為 |
| `stance` | `first_hand` `second_hand` `unknown` | 發文者與事件的關係 |
| `contains_minor_identifiers` | bool | 含兒童姓名／班級／可識別資訊。**不確定時為 `true`**（保守方向是「當成含有」）|
| `classified_at` | ISO 字串或 `null` | **有值＝跑過分類；`null`＝從來沒跑過** |

**`unclear` 與 `null` 是兩件事，而且差很遠。**

* `tone: "unclear"` ＝ 跑過分類，模型看不出來。
* `classified_at: null` ＝ **這一則沒有人看過**。

判斷要用 `classified_at`，不要用 `tone == null`。後端已經把這件事算好放在
`tone_bucket`／`tone_label` 兩個欄位裡，前端照著畫即可：

| `tone_bucket` | `tone_label` | 畫面 |
|---|---|---|
| `negative` | 語氣負面 | `--seal`（稽查紅）|
| `question` | 詢問 | `--warn`（琥珀）|
| `neutral` | 中性 | 中性 |
| `unclear` | 語氣不明 | 中性 |
| `unclassified` | 未分類 | 虛線框中性色，與 `.insuff`（資料不足）同一族 |

模型寫的自由文字（`issues`）**刻意不在回應裡**。它的內容受外部貼文影響，
印在官方主控台上等於讓陌生人的句子借用系統的口吻說話；要看原文點 `permalink`。
它也不入庫，只印在 `scripts/sync_threads_mentions.py --classify` 的 CLI 輸出上。

**分類結果不進分數、不進 payload、不進任何 CSV**（`06-plan` §1）。

### 2.8 `is_demo`：這一園是不是示範用的虛構園所

決賽現場要放幾則家長通報，通報就得指名某一園。那些貼文是我們編的，所以它們
**只准指名示範機構**（`data/demo/institutions_demo.csv`，說明見
`realtime/demo_data.py`）——編造的投訴掛在真實機構名下就是捏造指控，而它在
畫面上與真通報長得一模一樣。

| 欄位 | 出現在 | 意思 |
|---|---|---|
| `institution.is_demo` | `GET /api/social/{id}` | 這一園是示範機構 |
| `items[].is_demo` | `GET /api/social` | 同上，列表那一列 |
| `threads.items[].*.is_demo` | 貼文層 | 這一則指名的是示範機構 |
| `demo_note` | 兩支端點 | 示範資料的定位，逐字固定。`is_demo` 為 false 的單園回應是空字串 |

三條規則：

1. **判斷靠 `is_demo`，不靠名字。** 名字（`示範一號`／`範例二號`…）是給人看的，
   旗標是給程式檢查的。用名字判斷的話，貼文措辭一改，標示就會靜靜消失。
2. **畫面上一定要標。** 前端在園名旁印「示範資料」chip（`.tag.demo`），展開後
   在詳情最上方印 `demo_note`。看得出是假的名字不夠：畫面會被截圖，而截圖裡
   沒有人可以問「這一家是真的嗎」。
3. **示範機構只到得了這條路。** 它們不在 `institutions_ntpc.csv`、不在
   `dist/data/payload.json`、不在稽查優先序裡——那份 AUC 0.641／P@100 2.17x
   是量測過的數字。`tests/test_demo_data.py` 把這三個地方都釘住。

### 2.9 新聞／PTT 的標籤：**與 Threads 的語氣不共用、計數不合併**

這是整份契約裡最容易被寫錯的一條，因為兩族長得很像：都是封閉值、都印一排
膠囊、都只用 `--seal` 與 `--warn` 兩個色。但它們講的是兩件事：

| | Threads 貼文 | 新聞／PTT |
|---|---|---|
| 例子 | 「多收教材費，想問這樣合理嗎」 | 「2 教保員強制罪起訴」「教育局開罰 39 萬」 |
| 是什麼 | **未查證的民眾陳述** | **已發生之官方行動的報導** |
| 欄位 | `tone` / `tone_bucket` / `tone_label` | `report_kind` / `report_bucket` / `report_label` |
| 組成 | `threads.tone` | `mentions.labels`、列表的 `items[].news` |

後者不是「語氣負面」。兩者都染紅又共用同一個標籤，就把這個分別抹掉了——
而**那個分別正是稽查員判斷輕重的依據**。

分類由 `realtime/news_classify.py` 做，一則標題一次 Bedrock 呼叫，只回封閉值。

| 欄位 | 值域 | 說明 |
|---|---|---|
| `report_kind` | `事件報導` `爭議未定` `例行報導` `unclear` | 判準是「**有沒有已發生的官方行動或具體事件**」，不是記者的用字 |
| `event_category` | `06-plan` §6 的八類＋`unclear` | **與 Threads 共用同一套**（這一半刻意一致：事件類別是跨來源的分類）|
| `news_classified_at` | ISO 字串或 `null` | **有值＝跑過分類；`null`＝從來沒跑過** |

三個 `report_kind` 的界線：

* `事件報導`＝裁罰、罰鍰金額（「罰30萬」也算）、停招、停辦、廢止、勒令、
  起訴、判決、移送、懲處、稽查結果、確認的傷害或不當對待。
* `爭議未定`＝有指控、投訴、爭議、家長反映，但**看不到官方已採取行動**。
* `例行報導`＝招生、活動、表揚、評鑑通過、揭牌、捐贈、人物特寫。

前端照著畫：

| `report_bucket` | `report_label` | 畫面 |
|---|---|---|
| `事件報導` | 事件報導 | `--seal`（稽查紅）|
| `爭議未定` | 爭議未定 | `--warn`（琥珀）|
| `例行報導` | 例行報導 | 中性 |
| `unclear` | 性質不明 | 虛線框中性色 |
| `unclassified` | 未分類 | 虛線框中性色，與 `.insuff`（資料不足）同一族 |

PTT 的 `kind`（`complaint` / `question`）是既有的看板判定，**與模型那一層並存**，
自己一顆膠囊；`complaint` 走琥珀——那是一則民眾投訴，不是一件官方行動。

四條硬規則：

1. **標籤文字不得互換。** 新聞那一族一律用新聞自己的詞彙（「事件報導」），
   **不可以寫「語氣負面」**；反之亦然。
2. **計數不得合併。** `threads.tone` 與 `mentions.labels`／`items[].news` 是
   兩個欄位，永遠分兩處印。加總就是把未查證的抱怨與已起訴的案件數成同一類
   （§7.1 的另一個形態）。
3. **未分類不是例行。** `news_classified_at: null` 代表這一則沒有人看過。
   沒有 Bedrock 憑證的機器上整份 sidecar 是空的，那時每一則都是「未分類」。
4. **結果不寫回 `data/processed/`。** 標籤存在
   `data/runtime/news_labels.jsonl`（sidecar），鍵是分類時實際送出的那段文字
   的雜湊。釘住的快照 CSV 是 `python run.py verify-external` 的驗證對象，
   改它會讓那支驗證失敗。刪掉 sidecar 再跑一次 CLI 就重建得回來。

補跑：`python run.py classify-news`（`--show` / `--compare` / `--dry-run`
離線可跑，不呼叫模型）。**刻意不掛進 `mention_poller` 的每輪自動分類**——
那條路是為 Threads 通報設計的，新聞快照不是每 5 分鐘變一次。同一則不重跑，
判準是 sidecar 裡有沒有那個鍵。

---

## 3. `GET /api/social/{institution_id}`

### 3.1 參數

| 名稱 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `institution_id` | path | — | 8 碼或完整 UUID |
| `live` | query bool | `true` | `false` 時不即時查新聞與 PTT，只回快照 |

⚠️ **這支端點每呼叫一次就是一次 Google Places 計費查詢**（Place Details 含
reviews，每月前 1,000 次免費，之後每千次 US$25）。後端有額度閘門會擋，但
**前端不要輪詢、不要在列表上逐列預抓**——開卷宗時叫一次就好。額度剩餘量在
`reviews.free_remaining`。新聞與 PTT 不計費，但會讓回應變慢，所以只想撐版面時
用 `live=false`。

### 3.2 回應（實際回傳，蘆洲文德幼兒園）

`sources` 有七條、`threads.items` 有兩串，下面各只列出一項，其餘結構完全相同。
`reviews.reviews[]` 那一則是**編造的示意內容**——其餘欄位都是實際回傳值，
唯獨評論本文不能寫進版控：`may_store: false`，Places API ToS 3.2.3(a) 禁止匯出
與再散布，把它抄進文件就是一次留存。

```json
{
  "institution": {
    "id": "00957c83",
    "full_id": "00957c83-0061-4581-a587-97629968f371",
    "title": "新北市私立文德幼兒園",
    "town": "蘆洲區"
  },
  "sources": [
    {
      "key": "news_rss",
      "label": "新聞（Google News RSS）",
      "status": "live",
      "legal_basis": "公開 RSS feed，無登入、無個資；發布者與日期由來源提供",
      "may_store": true,
      "how_to_enable": "已啟用，無須額外授權",
      "available": true,
      "reason": "",
      "feeds": "mentions",
      "runs_on_open": true
    },
    {
      "key": "vendor_feed",
      "label": "社群資料服務（第三方）",
      "status": "needs_procurement",
      "legal_basis": "由資料服務供應商提供；涵蓋範圍與更新頻率依合約",
      "may_store": true,
      "how_to_enable": "訂閱 Apify／QSearch／OpView 等服務，把每日檔案路徑或查詢端點填入 .env 的 VENDOR_FEED_PATH。端點可用 {query} 佔位符。",
      "available": false,
      "reason": "缺採購：需向資料服務供應商訂閱，本管道目前沒有在看",
      "feeds": "mentions",
      "runs_on_open": true
    }
  ],
  "threads": {
    "available": true,
    "has_signal": true,
    "reason": "",
    "counts": { "threads": 2, "mentions": 2, "replies": 5 },
    "items": [
      {
        "root_threads_id": "18429670012183745",
        "root": {
          "threads_id": "18429670012183745",
          "kind": "mention",
          "username": "yayou._.shih",
          "text": "@smart_watchdog \n蘆洲的文德幼兒園這學期又收了一筆教材費，\n問了說是自由參加但不繳的小孩就沒有材料，想問這樣合理嗎",
          "permalink": "https://www.threads.com/@yayou._.shih/post/DdLgLQVmRfv",
          "posted_at": "2026-09-12T07:57:56+0000",
          "observed_at": "2026-09-12T08:17:32",
          "attribution_source": "own",
          "attribution_basis": "標題以機構名稱形式出現可辨識名稱",
          "institution_id": "00957c83-0061-4581-a587-97629968f371",
          "institution_title": "新北市私立文德幼兒園",
          "reply_to_threads_id": null,
          "is_reply": false,
          "is_this_institution": true
        },
        "root_missing_reason": "",
        "replies": [
          {
            "threads_id": "18459548563186498",
            "kind": "reply",
            "username": "maia.user.eason",
            "text": "我也遇到欸",
            "permalink": "https://www.threads.com/@maia.user.eason/post/DdLgiVhAUH1",
            "posted_at": "2026-09-12T08:01:05+0000",
            "observed_at": "2026-09-12T08:17:34",
            "attribution_source": "inherited",
            "attribution_basis": "繼承自主貼文 18429670012183745 的歸屬：新北市私立文德幼兒園（回覆自身未指名機構）",
            "institution_id": "00957c83-0061-4581-a587-97629968f371",
            "institution_title": "新北市私立文德幼兒園",
            "reply_to_threads_id": "18429670012183745",
            "is_reply": true,
            "is_this_institution": true
          },
          {
            "threads_id": "18623536096019226",
            "kind": "reply",
            "username": "maia.user.eason",
            "text": "新店的小綠芽幼兒園也這樣欸",
            "permalink": "https://www.threads.com/@maia.user.eason/post/DdLhPeFAYDO",
            "posted_at": "2026-09-12T08:07:14+0000",
            "observed_at": "2026-09-12T08:17:35",
            "attribution_source": "own",
            "attribution_basis": "回覆自身指名 新北市私立小綠芽幼兒園；主貼文為 新北市私立文德幼兒園",
            "institution_id": "00d30196-28bb-4e56-939e-29e2936fb7b9",
            "institution_title": "新北市私立小綠芽幼兒園",
            "reply_to_threads_id": "18429670012183745",
            "is_reply": true,
            "is_this_institution": false
          }
        ],
        "permalink": "https://www.threads.com/@yayou._.shih/post/DdLgLQVmRfv",
        "started_at": "2026-09-12T07:57:56+0000",
        "last_activity": "2026-09-12T08:07:14+0000",
        "counts": { "mentions": 1, "replies": 1, "posts_in_thread": 3 },
        "other_institutions": [
          {
            "institution_id": "00d30196-28bb-4e56-939e-29e2936fb7b9",
            "title": "新北市私立小綠芽幼兒園",
            "posts": 1
          }
        ]
      }
    ]
  },
  "mentions": {
    "available": true,
    "has_signal": false,
    "reason": "此管道目前無訊號：查無指名這一園的公開新聞或討論。無訊號不等於無異常；本系統僅代表未取得公開社群內容。",
    "ran": ["news_rss", "ptt"],
    "skipped": [
      { "key": "vendor_feed", "reason": "缺採購：需向資料服務供應商訂閱，本管道目前沒有在看" }
    ],
    "errors": [],
    "snapshot_swept_at": "2026-09-08 17:24",
    "counts": { "news_rss": 0, "ptt": 0, "vendor_feed": 0, "apify_threads": 0, "total": 0 },
    "labels": {
      "counts": { "事件報導": 0, "爭議未定": 0, "例行報導": 0, "unclear": 0, "unclassified": 0 },
      "classified": 0,
      "unclassified": 0,
      "labels": { "事件報導": "事件報導", "爭議未定": "爭議未定", "例行報導": "例行報導", "unclear": "性質不明", "unclassified": "未分類" },
      "note": "新聞／PTT 的標籤是對**這一則報導**的分類，用的是新聞自己的詞彙，與 Threads 貼文的語氣標籤**不共用、也不合併計數**：一則民眾抱怨與一件已經起訴的案子不是同一種東西，加總就會把兩者數成一類。未分類代表尚未跑過分類，不代表性質例行、也不代表沒有問題。"
    },
    "label_note": "新聞／PTT 的標籤是對**這一則報導**的分類，用的是新聞自己的詞彙，與 Threads 貼文的語氣標籤**不共用、也不合併計數**：……",
    "items": []
  },
  "reviews": {
    "available": true,
    "name": "文德幼兒園",
    "rating": 4,
    "review_count": 11,
    "maps_uri": "https://maps.google.com/?cid=4569607669093053131",
    "reviews": [
      {
        "text": "老師很用心，環境也乾淨。",
        "author": "某位家長",
        "author_uri": "https://www.google.com/maps/contrib/1234567890/reviews",
        "rating": 5,
        "published": "2026-03-18"
      }
    ],
    "note": "家長主觀評價，非法遵指標。實測：裁罰 ≥5 件的園評分中位 4.20，無裁罰者 4.60（p=0.061 不顯著），個案不具鑑別力。",
    "free_remaining": 989,
    "meter_source": "本機計數，非 Google 帳單",
    "has_signal": true,
    "reason": "",
    "may_store": false
  },
  "counts": {
    "threads": { "threads": 2, "mentions": 2, "replies": 5 },
    "mentions": { "news_rss": 0, "ptt": 0, "vendor_feed": 0, "apify_threads": 0, "total": 0 },
    "reviews": { "shown": 5 },
    "note": "各來源計數不可相加：一則 @標註、一則串下回覆、一篇新聞、一則 Google 評論不是同一種單位。要講「多少人向教育局反映」時看的是 threads.mentions（@標註我方的主貼文數），不是總列數。"
  },
  "has_signal": true,
  "reason": "",
  "disclaimer": "以下為未經查證的公開社群內容，僅供稽查人員研判參考；不是違法認定，也不計入風險分數。"
}
```

### 3.3 欄位語意

#### `sources[]` — 管道現況

沿用後端 `Channel.describe()`，所以畫面上列的就是系統**真正**在看的那些。

| 欄位 | 說明 |
|---|---|
| `key` | 管道代號，與 `mentions.items[].channel`、`/api/social?channel=` 同一套 |
| `label` | 給人看的名稱 |
| `status` | `live` / `needs_api_key` / `needs_approval` / `needs_procurement` |
| `legal_basis` | 憑什麼可以看這個來源。**不要刪掉**，這是對外說明時會被問的第一件事 |
| `may_store` | `false` 代表只能即時顯示、不得留存（目前只有 Google 評論）|
| `available` | `status === "live"` 的方便版本 |
| `reason` | 沒開通時的一句話（缺金鑰／缺授權／缺採購）；開通時是空字串 |
| `how_to_enable` | 要怎麼開通，用人話寫的。給操作者看的待辦事項 |
| `feeds` | 這條管道餵回應的哪一段：`"mentions"` / `"threads"` / `"reviews"` / `null` |
| `runs_on_open` | 開啟面板時這條管道會不會真的被呼叫 |

`feeds: null` 的兩條（`threads` 關鍵字搜尋、以及待核准的管道）目前不餵任何區塊，
但**仍然要顯示在管道清單上**——操作者要看得到「還有兩條在等審核」。

`runs_on_open: false` 但 `available: true` 的（`threads_mentions`、`apify_threads`）
代表：管道是通的，但面板不會替你即時呼叫它。`threads_mentions` 讀的是資料庫裡
已同步的通報（同步由排程負責）；`apify_threads` 每次呼叫都計費，必須走
`/api/scan` 的三段授權流程（估算 → 確認 → 執行）。

#### `threads` — @標註串

| 欄位 | 說明 |
|---|---|
| `items[].root` | 主貼文。**可能是 `null`**——只同步到回覆、主貼文不在庫裡時，`root_missing_reason` 會說明。此時不要拿第一則回覆頂替 |
| `items[].replies[]` | 串下回覆，依發文時間排。**不是樹**：巢狀關係在 `reply_to_threads_id`，要畫成樹自己接 |
| `items[].counts` | `mentions` / `replies` 只算**歸屬到這一園**的；`posts_in_thread` 是整串的貼文數 |
| `tone` | 這一園的**語氣組成**，`counts` 的**兄弟不是成員**：`counts` 的每一項都是「幾則」，這一項是那幾則長什麼樣。內容為 `counts`（negative／neutral／question／unclear／unclassified 各幾則）＋`labels`（中文標籤）＋`note`。**沒有單一語氣欄位，也不會有** |
| `tone_note` | 「這不是對機構的判斷」那句話，逐字固定 |
| `items[].tone` | 同上，一串的組成。同樣只算歸屬到這一園的那幾則 |
| `*.tone_bucket` / `*.tone_label` | 這一則該畫成哪一堆，見 §2.7。**前端不要用 `tone` 自己重推**|
| `items[].other_institutions` | 這一串裡歸屬到**別家**的貼文。空陣列是常態；有值代表一串討論裡冒出第二家 |
| `*.is_demo` | 這一則指名的是不是示範機構（§2.8）。前端照這個印「示範資料」，不自己看名字 |
| `*.is_this_institution` | 這一則是不是這一園的。縮排會讓人讀成「這則也在講上面那一園」，所以每一則自己說 |
| `*.attribution_basis` | 歸屬（或拒配）的理由，原文照登。複查的人靠這句判斷機器是怎麼算的 |

整串都會回來，**包含指名了別家的那幾則**——串是討論的容器，不是主體的容器，
濾掉它們會讓複查的人看到一段沒有上下文的對話。

`tone` 是組成不是判斷：**機構那一層只回各語氣的則數，不回單一語氣、不回風險
等級**。`GET /api/social` 的每一列也是同樣的 `tone`（在 `counts` 之外）。
理由見 §7.6。

#### `mentions` — 新聞／PTT

| 欄位 | 說明 |
|---|---|
| `ran` | 本次**實際執行**的管道 |
| `skipped` | 本次沒執行的，附理由 |
| `errors` | 個別管道失敗的紀錄（`{channel, error}`）。一條掛掉不影響其他條 |
| `snapshot_swept_at` | 建置時那次全市掃描的時間 |
| `items[].source` | `"live"`（這次查到的）或 `"snapshot"`（上次全市掃描留下的）|
| `items[].attribution_basis` | 快照來源是 `null`——代表**當時沒記下來**，不是「沒有依據」|
| `items[].kind` | **關鍵字表**（`features/alerts.py::article_kind()`）當時判的（`incident` / `routine` / `unclear`；PTT 另有 `complaint` / `question`）。即時結果是 `null`，關鍵字表那一層不重跑 |
| `items[].report_kind` | **模型**判的報導性質，見 §2.9。`null` ＝尚未分類 |
| `items[].event_category` | 模型判的事件類別（與 Threads 共用同一套八類）。`null` ＝尚未分類 |
| `items[].report_bucket` | 前端該把這一則畫成哪一堆，見 §2.9 的對照表 |
| `items[].report_label` | 該堆的中文標籤（「事件報導」…）。**不是語氣標籤** |
| `items[].news_classified_at` | ISO 字串或 `null`。**有值＝跑過分類；`null`＝從來沒跑過** |
| `labels` | 這一段的**性質組成**（各類的則數）。與 `threads.tone` 是兩個欄位，**不相加** |
| `label_note` | 兩族標籤不共用、計數不合併，逐字固定 |

`source` 一定要顯示出來。把四個月前的快照跟今天查到的混在一起，畫面上會讓一則
舊新聞看起來像剛發生。

`kind` 與 `report_kind` **是兩個欄位、兩層判斷，不互相覆蓋**。關鍵字表判
`unclear`、模型判 `事件報導` 的那幾則，正是最該由人看一眼的。

#### `reviews` — Google 評論

即時取用、即時回傳，**`may_store: false`，不得快取、不得落地、不得再散布**
（Places API ToS 3.2.3(a)(b)）。`note` 那句話**永遠都在**，包含取不到的時候，
因為那是這個來源的定位：*家長主觀評價，非法遵指標*。

`rating` 與 `reviews[].rating` 是 Google 的原始資料，原樣轉發。**不是風險指標**，
理由見第 7 節。

---

## 4. `GET /api/social`

### 4.1 參數

| 名稱 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `limit` | int 1–500 | `50` | 回幾筆 |
| `town` | string | — | 行政區完全比對（例：`蘆洲區`）|
| `channel` | string | — | 管道代號。`threads_mentions` 或任一 `sources[].key`。**打錯會回 400**，不會靜靜回空清單 |
| `since` | ISO 日期 | — | `YYYY-MM-DD`。格式錯回 400 |

### 4.2 回應（實際回傳，`limit=3`）

```json
{
  "count": 3,
  "matched": 10,
  "limit": 3,
  "filters": { "town": null, "channel": null, "since": null },
  "sources": [ "…與單園端點同一份，欄位相同…" ],
  "snapshot_swept_at": "2026-09-08 17:24",
  "coverage": {
    "listed": 10,
    "institutions_total": 1213,
    "note": "只列出目前有社群訊號的機構。未列出的機構代表本系統未取得公開社群內容，不代表無異常。"
  },
  "items": [
    {
      "institution_id": "00d30196",
      "full_id": "00d30196-28bb-4e56-939e-29e2936fb7b9",
      "title": "新北市私立小綠芽幼兒園",
      "town": "新店區",
      "last_activity": "2026-09-12T08:07:14+0000",
      "last_activity_date": "2026-09-12",
      "counts": {
        "threads": { "threads": 1, "mentions": 0, "replies": 1 },
        "mentions": { "news_rss": 0, "ptt": 0, "vendor_feed": 0, "apify_threads": 0, "total": 0 }
      },
      "latest": {
        "source": "threads_mentions",
        "kind": "reply",
        "summary": "新店的小綠芽幼兒園也這樣欸",
        "permalink": "https://www.threads.com/@maia.user.eason/post/DdLhPeFAYDO",
        "posted_at": "2026-09-12T08:07:14+0000",
        "attribution_source": "own"
      },
      "has_signal": true
    },
    {
      "institution_id": "00957c83",
      "full_id": "00957c83-0061-4581-a587-97629968f371",
      "title": "新北市私立文德幼兒園",
      "town": "蘆洲區",
      "last_activity": "2026-09-12T08:01:05+0000",
      "last_activity_date": "2026-09-12",
      "counts": {
        "threads": { "threads": 2, "mentions": 2, "replies": 5 },
        "mentions": { "news_rss": 0, "ptt": 0, "vendor_feed": 0, "apify_threads": 0, "total": 0 }
      },
      "latest": {
        "source": "threads_mentions",
        "kind": "reply",
        "summary": "我也遇到欸",
        "permalink": "https://www.threads.com/@maia.user.eason/post/DdLgiVhAUH1",
        "posted_at": "2026-09-12T08:01:05+0000",
        "attribution_source": "inherited"
      },
      "has_signal": true
    },
    {
      "institution_id": "4273705a",
      "full_id": "4273705a-5297-4025-a88b-f133cf5f6bca",
      "title": "新北市私立吉尼爾幼兒園",
      "town": "新莊區",
      "last_activity": "2026-09-09T21:30:04+0000",
      "last_activity_date": "2026-09-09",
      "counts": {
        "threads": { "threads": 1, "mentions": 0, "replies": 1 },
        "mentions": { "news_rss": 8, "ptt": 0, "vendor_feed": 0, "apify_threads": 0, "total": 8 }
      },
      "latest": {
        "source": "threads_mentions",
        "kind": "reply",
        "summary": "新莊的吉尼爾幼兒園前年也收過類似名目，後來好像有退費，可以問問看。",
        "permalink": "https://www.threads.net/@wugu_mom_c/post/DPeL3xByN9C",
        "posted_at": "2026-09-09T21:30:04+0000",
        "attribution_source": "own"
      },
      "has_signal": true
    }
  ],
  "note": "依最近活動時間排序，不是聲量排行榜；本端點不回任何分數。各來源計數不可相加：…",
  "disclaimer": "以下為未經查證的公開社群內容，僅供稽查人員研判參考；不是違法認定，也不計入風險分數。"
}
```

### 4.3 欄位語意

| 欄位 | 說明 |
|---|---|
| `count` | 本次回傳幾筆（受 `limit` 影響）|
| `matched` | 套完篩選後總共幾筆。`matched > count` 代表還有更多 |
| `coverage.listed` / `institutions_total` | 有訊號的機構數 / 全市機構數。**這兩個數字要一起顯示** |
| `last_activity` | 來源給什麼就是什麼：Threads 給完整時戳，建置快照只給日期 |
| `last_activity_date` | `YYYY-MM-DD`，排序與 `since` 比對用的那一個 |
| `counts.threads.threads` | **串數**。要顯示「數量」時用這個 |
| `tone` | 這一園**Threads 貼文**的語氣組成（`classify.compose()`）|
| `news` | 這一園**新聞／PTT** 的報導性質組成（`news_classify.compose()`）。與 `tone` 是兩個欄位，**不相加、不合併成一個「關注度」**，理由見 §2.9 |
| `label_note` | 兩族標籤不共用、計數不合併，逐字固定（回應層級）|
| `latest.summary` | 最新一則的第一行，截 120 字。是摘要不是全文，**要點進去才算讀過** |

排序是 `last_activity` 新到舊，同日以串數作為穩定排序的 tie-break。
**這不是排行榜**：排在第一位只代表最近有人講到，不代表最該查。

`items` 只包含**目前有訊號**的機構。不在清單上的 1,203 家不是「沒問題」，
是「本系統沒有取得公開社群內容」——`coverage.note` 就是為了這句話而存在。

---

## 5. `GET /api/social/unattributed`

### 5.1 參數

| 名稱 | 型別 | 預設 | 說明 |
|---|---|---|---|
| `limit` | int 1–500 | `50` | 回幾筆 |

### 5.2 回應（實際回傳）

```json
{
  "count": 2,
  "limit": 50,
  "available": true,
  "has_signal": true,
  "reason": "",
  "items": [
    {
      "threads_id": "17850447712389105",
      "username": "tiny.bear.mama",
      "text": "@ntpc_watchdog\n#教保通報\n真的很無力，孩子讀的園所老師一年換了四個，每次問行政都說「還在找人」，\n註冊費倒是一毛都沒少收。不想寫出園名怕被認出來，但真的希望有人去看一下。",
      "permalink": "https://www.threads.net/@tiny.bear.mama/post/DPgB1nXyPq4",
      "posted_at": "2026-09-10T13:47:02+0000",
      "observed_at": "2026-09-12T07:39:30",
      "kind": "mention",
      "attribution_source": null,
      "attribution_basis": "標題未以機構名稱形式出現可辨識名稱",
      "root_threads_id": null,
      "reply_to_threads_id": null
    },
    {
      "threads_id": "17851990341270562",
      "username": "sanchong_papa",
      "text": "@ntpc_watchdog\n剛剛看到台北市那則不當管教的新聞，名字跟蘆洲的文德幼兒園好像，嚇得我先問群組。\n到底是不是同一家？如果不是，是不是該請園方出來講一下，\n不然被誤會的那家也很無辜。",
      "permalink": "https://www.threads.net/@sanchong_papa/post/DPTm4rBybC8",
      "posted_at": "2026-09-05T21:03:17+0000",
      "observed_at": "2026-09-12T07:39:30",
      "kind": "mention",
      "attribution_source": null,
      "attribution_basis": "標題提及其他縣市（台北市），不予歸屬",
      "root_threads_id": null,
      "reply_to_threads_id": null
    }
  ],
  "note": "institution_id 為 NULL 代表「認不出是哪一園」，不是「與機構無關」。這份清單是稽查工作量，需人工逐則認園。",
  "disclaimer": "以下為未經查證的公開社群內容，僅供稽查人員研判參考；不是違法認定，也不計入風險分數。"
}
```

### 5.3 這份清單是什麼

**待人工認園的工作量。** 每一筆都是有人真的向教育局反映了，而系統認不出是哪
一園。第二筆是很好的例子：民眾自己在問「台北市那則新聞跟蘆洲文德是不是同一
家」——歸屬規則因為出現其他縣市而拒配，這是正確的（`attribution_basis` 說得很
清楚），而這一則顯然需要有人看。

畫面上不要叫它「雜訊」「未分類」「其他」。它是一個待辦佇列，數字應該和「本週
待處理」放在一起，不是和「已忽略」放在一起。

`text` 是**全文**，不是摘要——認園這件事只能由讀過原文的人做。

---

## 6. `POST /api/social/{institution_id}/draft-reply`

替一串 @標註通報擬一份給承辦人的**回覆草稿**。

**這支端點不送出任何東西，也沒有任何程式路徑可以送出。** `scrape/threads.py`
是唯讀的，它沒有發文、回覆或刪除的函式可以被 import。官方帳號在任何人讀過
內容之前自動回一句「已收到您的通報」，是一個公開的受理表態——那是設計決定，
不是還沒做完的功能。

### 6.1 請求

```json
POST /api/social/00957c83-0061-4581-a587-97629968f371/draft-reply
{ "root_threads_id": "17849251066204813", "backend": "template" }
```

| 欄位 | 說明 |
|---|---|
| `root_threads_id` | 要回哪一串。整串（主貼文＋回覆）都會被當成脈絡 |
| `backend` | `"template"`（確定性組裝，離線可用）或 `"bedrock"`（生成）。不給時自動選：有 AWS 憑證走 bedrock，沒有走 template |

### 6.2 回應

| 欄位 | 說明 |
|---|---|
| `draft` | 草稿全文，**兩段**：「承辦人須知」（內部）＋「建議回覆內文」（可能對外）|
| `sendable` | **只有可送出的那一段**。前端要顯示「可以貼出去的是哪些字」時用這個，**不要自己切 `draft`**——切錯會把園名連同「這是未查證的通報」一起貼到公開平台上 |
| `sendable_mark` | 兩段之間的分隔線，與 `draft` 裡的那一行相同 |
| `verified` | 有沒有通過 `report/verify.py` 的閘門 |
| `problems[]` | 沒通過的理由 |
| `fell_back` / `fallback_reason` | 生成那條路被退件時會改用樣板，這兩欄說明為什麼 |
| `checklist[]` | 承辦人送出前一定要自己確認的事項，逐字固定 |
| `auto_send` | 恆為 `false`。**前端不得把它畫成「已送出」「已受理」或任何完成狀態** |
| `posts_used` / `attributed` | 用了整串幾則；那一串有沒有歸屬到機構 |

### 6.3 草稿裡的三條界線

1. **可送出的那一段不出現任何機構名稱。** 官方帳號在一則未查證的指控底下公開
   回覆「本局將就○○幼兒園查明」，等於在任何人查證之前由機關把那一園與那則指控
   綁在一起。承辦人需要園名——它在「承辦人須知」那一段，那一段不對外。
2. **可送出的那一段不照抄原貼文。** 把指控引述進本局自己的句子裡，讀起來就是
   機關在複述指控。這同時是 prompt injection 的停損點：陌生人寫的字沒有任何
   一條路徑可以穿過模型進到官方發言裡。
3. **兩句聲明都必須在**：「非違法認定」與「未經查證」。只有前者的回覆讀起來
   仍像機關已經確認發生過什麼、只是還沒定性。

三條都由 `report/verify.py::verify_reply()` 機械檢查，用的是**與稽核建議書
同一份** `FORBIDDEN` 清單與同一套否定詞剝除規則（`verdict_words()`）。
退件時換確定性樣板，不是把退件的草稿送上去，也不是回一個錯誤讓畫面空著。

### 6.4 錯誤

| 情況 | 回應 |
|---|---|
| 機構不存在 | `404` |
| 那一串不在庫裡 | `404`，「查無這一串通報 {id}」 |
| 只同步到回覆、主貼文不在庫裡 | `409`——要回的那個人不在手上，草稿沒有收件人 |
| 那一串歸屬到別家 | `409`，請從該園的面板擬定回覆 |

---

## 7. 前端不可以做的事

### 7.1 不要把各來源計數加總成一個數字

```js
// ✗ 絕對不要
const buzz = counts.threads.mentions + counts.threads.replies
           + counts.mentions.total + counts.reviews.shown;
```

一則 `@標註`、一則串下「+1」、一篇新聞、一則 Google 評論不是同一種單位。
加起來得到的那個數字沒有任何意義，卻會被讀成「聲量」，然後被拿去排序。
`06-plan` §6：分析單位是園所 × 事件群集，轉貼同一事件不得重複加權。

尤其**不要把 `mentions` 與 `replies` 加起來**。一串十則附和只有一個人標註了官方
帳號；合併之後，「有 11 個人向教育局反映」這句話就灌水了十倍，而那個數字正是
說明會上會被講出去的東西。要講人數時用 `counts.threads.mentions`。

### 7.2 不要把「無訊號」畫成綠燈或「正常」

`has_signal: false` 代表**本系統沒有取得公開社群內容**，不代表這家園沒事。

* ✗ 綠燈、打勾、「正常」、「無異常」、「低風險」
* ✓ 灰色、「查無社群訊號」、「資料不足」，並把 `reason` 原文顯示出來

同理，**不在 `/api/social` 清單上的機構不是安全的**。如果面板有「全市總覽」，
不要把沒列出來的 1,203 家畫成綠色。

### 7.3 不要把 Google 星等當風險指標

實測：裁罰 ≥5 件的園評分中位 **4.20**、無裁罰者 **4.60**（p=0.061，不顯著），
而個案完全不具鑑別力——16 件裁罰的幼苗國際 4.7 星、13 件的南蒂亞 4.9 星、
因虐童停招的吉尼爾 4.4 星。

* ✗ 用星等上色、排序、設門檻、做成儀表板指標
* ✓ 原樣顯示，旁邊放 `reviews.note`（「家長主觀評價，非法遵指標」）

它的用途是：稽查員到場前值得看一眼的家長觀感。就這樣。

另外 `may_store: false` —— **不要**把評論存進 localStorage、不要做前端快取、
不要放進匯出的報表。Places API ToS 3.2.3(a)(b) 禁止匯出、爬取與快取。

### 7.4 每一則都要能點回原文

每一則通報、新聞、回覆都有 `permalink`（評論是 `author_uri` 與 `maps_uri`）。
**不要只顯示摘要就結束。** 這個面板的產品定位是「可點回原文的清單，判斷留給
人」——拿不到原文的清單就變成了系統在替人下判斷。

`latest.summary` 是截斷過的第一行，不要拿它當全文。

### 7.5 不要自己算分數、等級或情緒

回應裡沒有 `score`、`risk`、`level`、`sentiment` 這些欄位，是刻意的
（`tests/test_social_api.py::test_no_endpoint_returns_a_score_or_a_risk_level`
會擋住任何人加回去）。前端也不要自己算一個出來——不管是「熱度」「關注度」還是
「三顆火焰」。

### 7.6 不要把一整園染成單一風險色

語氣分類是對**單一貼文**的標記。把一整園（清單那一列、地圖上的標記、卷宗的
標題）染成紅色或任何一個「代表這一園語氣」的顏色，就是用一批未經查證的貼文
對一家真實機構下風險判斷——而那正是 `features/alerts.py` 整支模組存在的理由：
它的模組說明記了四個真實誤配，其中一則是某園在新聞裡被誤認後**公開澄清**
「衰被誤認虐童」。

機構那一層要印的是**組成**：

* ✓ 「3 則語氣負面 · 1 則中性 · 1 則詢問」——讀的人看得到分母
* ✗ 一顆紅點、一條紅色底線、一個「負面」標籤掛在園名旁邊

`tone` 刻意只給各類的則數，沒有任何一個「主要語氣」「情緒分數」欄位，
前端也不要自己從則數推一個出來（例如「負面最多就標紅」）。

另外：語氣的顏色只用 `--seal` 與 `--warn`，**不得沿用 `--c1~c4`**
（那是建議查核密度）或 `--k0~k4`（那是歷史裁罰件數）。這兩族已經各有語意，
第三套借用它們的色階，同一張畫面上就會有三套顏色互相解釋。
**每個顏色旁邊一定要有文字標籤**，不得出現沒有說明的色點。

### 7.7 不要把「未分類」當成中性

`classified_at: null` 代表**這一則沒有人看過**，不是「語氣中性」、不是
「沒有問題」、也不是「已檢查無異常」。

* ✗ `const tone = p.tone || "neutral"` ——一行把整批沒跑過分類的貼文變成中性
* ✗ 把未分類的那些從組成裡拿掉，只印已分類的三個數字（分母會少一截）
* ✓ 印「未分類 N 則」，樣式與 `.insuff`（資料不足）同一族，不與「中性」同族

這與 §2.2「無訊號不是綠燈」是同一條原則的第二個形態：**我們沒看過的東西，
畫面上不可以長得像我們看過而且沒事。**

### 7.8 不要把新聞的標籤與 Threads 的語氣混為一談

完整規則在 §2.9。前端最容易犯的三個形態：

* ✗ 把新聞那一則畫上「語氣負面」的標籤（它是**已發生之官方行動的報導**，
  不是情緒）
* ✗ 把 `threads.tone.counts` 與 `mentions.labels.counts`／`items[].news.counts`
  加起來，得到一個「總共幾則負面」——那是把未查證的抱怨與已起訴的案件數成
  同一類，§7.1 的另一個形態
* ✗ 把兩個組成印在同一行、中間放一個「·」——沒有抬頭的話兩串數字會讀成一串

✓ 兩個組成分兩行、各自帶抬頭（「貼文語氣」／「新聞／PTT」），文字各用各的
詞彙。顏色可以共用（這個介面只有 `--seal` 與 `--warn` 兩個語意色），
**詞彙不行**。

排序控制（若有）也適用同一條：「關注程度」這類排序**只能依其中一族**排，
而且要說清楚依的是哪一族；把兩族混成一個分數就是上面第二點。

### 7.9 用詞界線

社群內容是**未經查證的公開內容**。面向使用者的文字一律用：

* ✓ 「民眾反映」「公開討論」「建議查看」「待人工研判」
* ✗ 「疑似違規」「檢舉案件」「已證實」「風險偏高」

`disclaimer` 欄位每一支端點都會回，請顯示在面板上，不要只放在 tooltip 裡。

---

## 8. 管道未開通時，畫面該長什麼樣

管道沒開**不能靜靜地回空陣列**——那會教操作的人「這一園很平靜」，而事實是沒有
人在聽。`sources[]` 永遠回七條，沒開通的那幾條要顯示，並附上怎麼開通。

| `status` | 意思 | 建議畫面 |
|---|---|---|
| `live` | 正在看 | 正常顯示該區塊 |
| `needs_api_key` | 缺金鑰，填一把就能用 | 灰底卡片：「尚未啟用——{reason}」＋ `how_to_enable`，標成**可立即處理** |
| `needs_approval` | 等平台審核（2–4 週）| 灰底卡片＋「申請中／待申請」，標成**需時間** |
| `needs_procurement` | 需採購第三方服務 | 灰底卡片＋「需編列預算採購」，標成**需採購** |

建議在面板頂端放一行覆蓋率摘要，例如
**「7 個管道，5 個已啟用，1 個待申請、1 個待採購」**——操作者看到這行才知道
要去要什麼。這也是 `sources` 把沒開通的管道一起回來的全部理由。

三種未開通狀態**要分得開**。「填一把金鑰」與「等 Meta 審核四週」是兩種完全不同
的待辦事項，混成一句「未啟用」的話，能今天解決的那一項就會被排到和不能解決的
那一項一起。

同一把 Threads 權杖會開出兩條管道（`threads_mentions` 有權杖就能用、
`threads` 關鍵字搜尋另需 App Review），所以它們刻意是兩列，狀態也會不同。

---

## 9. 錯誤與邊界

| 情況 | 回應 |
|---|---|
| 機構不存在 | `404`，`detail` 為「查無機構 {id}」 |
| `channel` 打錯 | `400`，附可用的管道清單 |
| `since` 格式錯 | `400`，「since 需為 ISO 日期（YYYY-MM-DD）」 |
| 沒有 Google 金鑰 | **不是 500**。`reviews.available: false`，`reason` 說明缺什麼 |
| Google 額度用盡 | `reviews.available: false`，`reason` 說「額度限制，不是這家園沒有評論」 |
| 新聞／PTT 查詢失敗 | 不影響其他區塊。`mentions.errors[]` 有 `{channel, error}` |
| 主貼文不在庫裡 | `threads.items[].root` 為 `null`，`root_missing_reason` 說明 |

**查無此園（404）與這一園沒有聲音（`has_signal: false`）是兩件事**，
不可以長成同一個畫面。

---

## 10. 相關檔案

| 路徑 | 內容 |
|---|---|
| `src/smart_watchdog/api/social.py` | 三支端點的實作 |
| `src/smart_watchdog/realtime/sources.py` | 管道定義與 `legal_basis`（模組說明講了 availability 為何是一等公民）|
| `src/smart_watchdog/realtime/mention_store.py` | Threads 通報的寫入與查詢，`attribution_source` 的三個值 |
| `src/smart_watchdog/db/models.py::ThreadsMention` | 欄位語意的權威來源 |
| `src/smart_watchdog/realtime/classify.py` | 貼文分類：封閉值、一次性補完、不接 agent、不做歸屬 |
| `src/smart_watchdog/realtime/news_classify.py` | 新聞／PTT 分類：同樣的隔離，但用**新聞自己的詞彙**（§2.9），結果寫 sidecar |
| `scripts/classify_news_mentions.py` | 補跑新聞分類的 CLI（`python run.py classify-news`），有快取與硬上限 |
| `data/runtime/news_labels.jsonl` | 新聞標籤的 sidecar。可刪可重建；**釘住的快照 CSV 不會被寫入** |
| `tests/test_news_classify.py` | §2.9 每一條保證的對應測試 |
| `src/smart_watchdog/report/reply.py` | 回覆草稿產生器：樣板地板＋Bedrock 生成，兩者都過 `verify.py` |
| `src/smart_watchdog/report/verify.py` | `verify_letter()` 與 `verify_reply()` 共用同一份用詞清單 |
| `docs/research/06-realtime-event-monitoring-plan.md` | §1 不併入風險分數、§6 事件分類與 alert governance |
| `tests/test_social_api.py` | 本文件每一條保證的對應測試 |
