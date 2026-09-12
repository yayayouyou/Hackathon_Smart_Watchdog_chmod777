# 04 — 初期可以做什麼（Phase 1）

決賽是 **9/12–13，30 小時**。現在（8/10）到決賽還有一個月，
但決賽當天才交件，所以**現在該做的是把不確定性消掉，不是把功能做完**。

排序原則：**先做零成本且能立刻驗證的，再做要花錢／花時間的。**

---

## ✅ 已完成（本次）

| 項目 | 產出 |
|---|---|
| 專案骨架 | `src/smart_watchdog/{ingest,extract,scrape,features,risk,api}` |
| 資料集就位 | `data/raw/資料集/` 162 檔 1.9GB |
| PDF 全量普查 | `data/interim/pdf_survey.csv`（162 列）|
| 文字品質普查 | `data/interim/text_quality.csv`（11,232 頁）→ 找出 PUA 垃圾頁 |
| 公校座標式表格重建 | `ingest/pdf_utils.py`，實測可還原 板橋幼兒園 基金來源用途餘絀表 |
| 公校分冊切割 | `ingest/public_school.py`，用頁尾 `13601-4` 切出 24 個分基金 |
| 外部登記資料客戶端 | `scrape/registry.py`（鏡像優先，含分班聚合、法人解析）|
| 新北市機構主檔＋標籤 | `data/processed/institutions_ntpc.csv`（1,213）、`penalties_ntpc.csv`（1,386 canonical／1,457 source rows）、`operators_ntpc.csv`（25）|
| 官方評鑑 WebForms pilot | `data/external/evaluation/ntpc-pilot-v1/`（9 responses）＋ `data/processed/evaluations_ntpc.csv`（9 events，3 entities，全部 exact-title join）|
| 政府採購決標 pilot | `data/external/procurement/ntpc-pilot-v1/`（8 responses）＋ `data/processed/nonprofit_procurement_contracts.csv`（3 awards × 4 report years；官方公告 URL 與 sibling UUID 完整保留）|
| 教育局公告／改善追蹤 pilot | `data/external/education_bureau_announcements/ntpc-pilot-v1/`（5 detail HTML＋2 PDF）＋ 5 notices／98 actions；82 exact-title、16 歷史園所 unresolved，負面控制零誤分類 |
| 全 external 產物驗證 | `scripts/verify_external_artifacts.py` 不連外檢查 24 份 raw responses、根快照、processed hashes、schema、UUID 與 sibling 完整性 |
| 研究文件 | 資料集分析、鑑識訊號設計、外部資料策略、AWS 架構 |

**現在就已經有一個可用的、零 OCR 成本的風險資料集。** 這是最重要的既得成果。

---

## Phase 1（建議接下來 1–2 週，全部零／低成本）

### P1-1 ⭐ 受託法人風險特徵（最高投資報酬）
**為什麼先做**：不需要 OCR、不需要 Bedrock、純字串處理，
但它是**唯一能對「尚無不良紀錄的機構」提出預警**的機制，
直接命中命題的「事前主動示警」。

實測已見：中華音樂舞蹈暨表演藝術教育協會 4 園全數受罰（9 件），
對比三之三 13 園僅 2 園受罰。

待處理：法人—園—期間去重（北大／新林／安興 對到多個法人，研判為契約更替）。

### P1-2 ⭐ 時序切分的廣度模型 baseline
用 2017–2024 裁罰預測 2025–2026，特徵先只用免 OCR 的欄位：
類型、行政區、核定人數、每人室內面積（`size_in / count_approved`）、
月費、立案年資、法人歷史風險、負責人跨園關聯、歷史裁罰時序。

**必須時序切分**，隨機切分會讓歷史裁罰洩漏未來標籤，得到虛高 AUC。
先跑出一個誠實的 baseline 分數，簡報時才有東西可比。

### P1-3 公校決算書全量抽取（免費，9,067 頁）
`ingest/public_school.py` 已可切割，接下來寫欄位映射：
21 所市立幼兒園 × 3 年度（112–114）的
基金來源／用途、學雜費短收率、政府依賴度、員工人數、用人費用、期末基金餘額。

218 頁 PUA 垃圾頁先跳過並記錄，Phase 2 補 OCR。

### P1-4 非營利財報抽取 PoC（只做 3 所 × 3 頁）
**不要一次做 132 份。** 先挑 3 所（1 所受罰、1 所未受罰、1 所新設）
共 9 頁，驗證整條鏈：
頁面定位 → Bedrock vision → Structured Outputs → 表內驗算 → 執行率反算。

驗算通過再擴大。這一步的目的是**證明可行並量出準確率**，不是產出資料。

### P1-5 收費明細抓取（超收偵測的基礎）
`registry.fetch_fee_slip()` 已可用。抓 新北市 109–114 學年度全量，
建立各園歷年申報數額時序，找跳動與同區偏離。
第43條＋第38條合計 200 件，是第 2 大裁罰類別，值得投入。

### P1-6 AWS 環境與權限
在 8/15、8/16 工作坊前把 Bedrock 模型存取權限開好
（`anthropic.claude-sonnet-5`、`anthropic.claude-opus-5`），
並用 token counting 對 P1-4 的 9 頁估出單頁成本，反推全量預算。

---

## Phase 2（決賽前，需成本／需時間）

- 132 份非營利財報 × 3 頁全量抽取（約 400 次 vision 呼叫）
- 跨年雙盲對帳 → 產出量化抽取準確率（防守評審質疑的核心）
- 218 頁 PUA 頁 OCR 補齊
- 輿情爬取與 Bedrock 分類（權重須低於裁罰與財報，見架構文件 §6）
- Bedrock Knowledge Base 灌入幼照法、非營利幼兒園實施辦法
- 稽查建議書生成 + 引用
- 儀表板與 Live Demo 腳本

## Phase 3（決賽 30 小時內）
只做整合、Demo 腳本、簡報。**不要在決賽當天才開始抽資料。**

---

## 三個現在就該確認的未決問題

### Q1 非營利園準備金會計慣例（阻塞 RED-07 訊號）
安溪 113 年「業務發展準備」(負債) 15,899,844 大於
「業務發展準備金」(資產) 13,699,844，差額 2,200,000，且同年虧損 1,988,771。
**這是異常還是正常提列時序差？** 需查《非營利幼兒園實施辦法》
與會計處理注意事項。確認前不得輸出為「疑似不法」。

### Q2 行政管理費是否有法定上限比率
若辦法明訂上限（例如佔總營運成本某百分比），
`RED-02 關係人費用優先支付` 就能從「相對訊號」升級為
**「法遵違反」硬指標**，價值大幅提高。值得優先查證。

### Q3 師生比分齡人數
附註只給總招收人數與教保人員數，未必分齡。
2–3 歲 1:8、3 歲以上 1:15，不分齡只能算保守上界。
需確認附註或其他公開來源是否有分齡班級數。

---

## 刻意不做的事（避免浪費時間）

- ❌ **不用 59 個公共化園的 12 個正樣本訓練監督式模型** — 必然過擬合。
  改當訊號驗證集（見 03 文件 §6 軌 B）。
- ❌ **不一次 OCR 全部 5,162 頁** — Phase 1 只要 9 頁就能驗證管線。
- ❌ **不把 Benford 當主力訊號** — 樣本小、科目異質，只能當輔助且須說明限制。
- ❌ **不在公校與非營利之間混算 z-score** — 財源結構差太多（公校 87.5% 靠公庫撥款）。
