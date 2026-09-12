# 資料目錄說明

給隊友的第一份文件。**clone 下來就能用的資料，與需要自己準備的資料，分得很清楚。**

## 一句話總結

`extracted/`、`processed/`、`external/`、`ground_truth/` **都在版控裡，clone 即可用**。
只有 `raw/`（主辦方 1.8 GB PDF）和 `interim/`（渲染影像）需要自己準備。

---

## 目錄結構

| 目錄 | 版控 | 大小 | 內容 |
|---|---|---|---|
| `raw/` | ❌ | 1.8 GB | 主辦方原始 PDF（162 檔）|
| `external/` | ✅ | 12 MB | 公開登記資料時點快照 |
| `extracted/` | ✅ | 704 KB | **從 PDF 抽出的結構化資料（最貴的產物）** |
| `processed/` | ✅ | 6 MB | 分析用整理表 |
| `ground_truth/` | ✅ | 16 KB | 人工核對基準 |
| `interim/` | ❌ | 123 MB | 渲染影像、暫存 |

---

## ⚠️ `raw/` 要自己準備

主辦方資料集不在版控裡：全隊都有下載連結，且我們無權重新散布。

```bash
# 向隊長索取兩個 zip，放到任一目錄後：
.venv/bin/python scripts/setup_raw_data.py ~/Downloads
```

**不要用 `unzip`。** zip 的檔名是 Big5 編碼且未設 UTF-8 flag，`unzip` 會在每個
中文檔名上失敗（`Illegal byte sequence`）。上面的腳本會正確解碼。

沒有 `raw/` 也能做很多事——`extracted/` 裡已經有抽好的結果。
只有要**重新抽取或人工覆核頁面**時才需要它。

---

## `extracted/` — 最重要的目錄

這裡是**從 PDF 抽出來、無法從網路重生**的資料。沒有 `raw/` 就重生不了，
所以刻意進版控。

| 檔案 | 內容 | 產生方式 |
|---|---|---|
| `public_kindergartens.csv` | 22 所市立幼兒園 × 112–114 年度，63 筆（58 筆乾淨）| 決算書文字層座標抽取（**零模型成本**）|
| `nonprofit/*.json` | **132 / 132 份非營利園決算報告，全數抽取完成**（110–113 學年度各 28/32/34/38 份）。每份均含資產負債表、收支餘絀表與附註一／二／三／五；四個附註欄位皆為 **132/132**。| 視覺模型抽取 |
| `poc/*.json` | 5 張完整報表（4 資產負債表 + 1 收支餘絀表）| 視覺模型抽取 |
| `pdf_survey.csv` | 162 份 PDF 的頁數／文字層／OCR 需求 | `scripts/survey_pdfs.py` |
| `text_quality.csv` | 公校 11,232 頁的 PUA 亂碼偵測 | `scripts/survey_text_quality.py` |

### `nonprofit/*.json` 的欄位

每檔一所園一學年度，schema 見
[`src/smart_watchdog/extract/forensic.py`](../src/smart_watchdog/extract/forensic.py)：

```
balance_sheet     現金、預收款項、業務發展準備金/準備、資遣費準備金/準備、
                  累積餘絀、本期餘絀、各項合計總計
income_statement  各科目的 預算數 / 決算數 / 執行率（lines 陣列）
note_1            受託法人、契約期間、核定與實際招收人數、員工數、教保人員數
note_2            附註二「重大會計政策」完整逐字轉錄與起訖頁碼
note_3            其他收入／其他支出明細、總額入帳判斷與原文
note_5            關係人交易、行政管理費、應付受託法人款項與原文
issues            模型自報的辨識困難與文件矛盾 ← 別忽略這欄，見下
```

**`issues` 欄位是資產不是雜訊。** 抽取模型在裡面回報了我們沒設計的發現，
例如鷺江的資遣費準備金與負債不符且與該報告自身附註矛盾、
新月的核定人數與官方主檔差 302 人。找異常時**先讀這欄**。

### 讀取範例

```python
import json, pathlib
from smart_watchdog.extract.forensic import compute_signals, validate_forensic

for f in sorted(pathlib.Path("data/extracted/nonprofit").glob("*.json")):
    d = json.loads(f.read_text(encoding="utf-8"))
    passed, failed = validate_forensic(d)      # 恆等式自我驗算
    sig = compute_signals(d)                   # 12 個鑑識訊號
    print(f.stem, sig.prepaid_coverage, len(failed), d["issues"])
```

---

## `processed/` — 分析用整理表

| 檔案 | 列數 | 內容 |
|---|---|---|
| `institutions_ntpc.csv` | 1,213 | 新北市機構主檔。⚠️ 是**登記筆數**，不是實體園——用 `entity` 欄位去重（1,149 所實體園）|
| `penalties_ntpc.csv` | 1,386 | canonical 裁罰紀錄；由 1,457 個 source rows 保守合併 71 個唯一短／長版，完全同值但無 event ID 的列保留並共用 `penalty_group_id`；非金錢處分的 `fine` 為空值而非 0 |
| `operators_ntpc.csv` | 25 | 非營利園受託法人彙總 |
| `nonprofit_registry_crosswalk.csv` | 132 | 每個財報年度對應實體園、全部 sibling registry UUID、當期法人與契約期間 |
| `evaluations_ntpc.csv` | 9 | 官方 `evaSearch.aspx` pilot 的 source-faithful 評鑑事件；3 所實體園、9 筆歷史／追蹤評鑑，全部 exact-title join 並保留 multi-UUID siblings；不是全量母體 |
| `nonprofit_procurement_contracts.csv` | 12 | 3 筆受託營運決標各對應 4 個財報年度；保留得標法人統編／原名、金額、履約期間、官方公告 URL、全部 sibling UUID 與 covered/uncovered/unconfirmed 時間狀態；不是全量母體 |
| `education_bureau_notices_ntpc.csv` | 5 | 新北市幼兒教育資源網 bounded pilot 公告；2 筆限期改善／追蹤訪視、1 筆評鑑政策修訂、2 筆避免誤分類的政策／一般招生負面控制；不是全量母體 |
| `education_bureau_discovered_notices_ntpc.csv` | 0（bootstrap） | Phase 1B listing monitor 的增量 discovery aggregate；bootstrap 只建立 2 頁／60 IDs 的 bounded horizon，不把既有公告偽裝成新事件；未來新 ID 以 row-level observation time 與 content-addressed detail provenance 加入 |
| `education_bureau_actions_ntpc.csv` | 98 | 103、104 學年度追蹤評鑑 PDF 所列園所 action；全部維持 `ordered`，82 筆 exact-title join、16 筆歷史園所未解析；候選裁罰連結不宣稱同一事件 |
| `official_events_ntpc.csv` | 1,501 | 可離線重建的官方事件時間線：1,386 裁罰、9 評鑑、5 公告、98 改善 action、3 個 unique 採購決標。這是 source-faithful projection，不是新的風險分數；`verification_status`、`identity_status`、`lifecycle_status` 各自獨立 |
| `official_events_ntpc.manifest.json` | — | 上述時間線的 input/output hashes、來源 snapshot、family counts、16 筆 unresolved actions 與語意護欄；`scripts/build_official_events.py --verify-only` 可 no-write 驗證 |
| `fee_summary_ntpc.csv` | 1,202 | 各園 109–114 學年度收費與最大漲幅 |
| `forensic_cohort.csv` | 18 | 9 案例 + 9 對照的配對設計 |
| `forensic_signals.csv` | 18 | 上述 18 所算出的 12 個訊號。⚠️ **全部未通過檢定**，保留是為了留下否證紀錄，不要拿去當風險分數 |
| `nonprofit_panel.csv` | 每份抽取一列 | 非營利園財報主表（資產負債＋收支餘絀關鍵科目）|
| `nonprofit_issues.csv` | 每則一列 | 抽取時發現的文件內部矛盾與待人工判讀事項 |
| `compliance_findings.csv` | 每（園×學年度×規則）一列 | **目前最有用的一張表**：拿每份報告自己的附註二去檢核它自己 |
| `reserve_timeseries.csv` | 每（園×準備金×年度轉換）一列 | 準備金專戶缺口的跨年度走勢，用來區分撥付時間差與缺口累積 |
| `ocr_crosscheck.csv` | 每份抽取一列 | Tesseract 對已渲染頁面的數字級交叉驗證，只能否證不能主張（需本機安裝 tesseract）|
| `personnel_to_reserve.csv` | 每份抽取一列 | 人事費短支與業務發展準備轉列的併存情形。⚠️ 佔全體 92%，是**產業結構描述，不是紅旗** |
| `extraction_identity_audit.csv` | 每份抽取一列 | 抽取結果是否確實屬於檔名所指的那所園（三項獨立交叉比對）|

`compliance_findings.csv` 的 `passed` 欄有三種值，**不要把後兩者當成同一件事**：
`True` 通過、`False` 未通過、空值 = **待判讀**（資料不足，或認定基礎本身有爭議
而不宜斷言，例如提列比率落在 20–22% 的臨界帶）。

**不在版控的兩個大檔**（可用 `scripts/build_fee_table.py` 從網路重抓，約 10 分鐘）：
`fees_ntpc.csv`（46 萬列，45 MB）、`fee_totals_ntpc.csv`（5.5 MB）。

---

## `external/` — 公開資料快照

見 [external/README.md](external/README.md)。重點：**刻意釘住時點**，
因為官方裁罰紀錄有保存期限會下架，只在執行時抓會得到截斷且不會報錯的標籤。

### 引用時必須帶著的標註

機構主檔、裁罰與收費三份資料是 **CC-BY**，標註是授權條件而不是禮貌；
簡報、報告與任何對外輸出引用到這些數字時都要帶著它：

> 資料來源：全國教保資訊網 <https://ap.ece.moe.edu.tw/webecems/pubSearch.aspx>；
> 取得管道：台灣幼兒園地圖資料庫 <https://github.com/kiang/ap.ece.moe.edu.tw>
> （江明宗維護，程式 MIT）；快照日期 **2026-08-10**。

上游把**程式（MIT）**與**資料（CC-BY）**分開聲明，我們不能只寫其中一個。
其他外部資料（官方評鑑、採購、教育局公告、新聞、行政區界線）各有不同的來源與
授權狀態，逐項列在 [external/README.md](external/README.md) 的「來源與授權」；
查不到明文條款的一律標「待確認」，不自行推定。

Root 三份公開資料另有 `external/observations/` immutable observation chain：每次成功
上游檢查保留 content-addressed bytes 與 record-level added／changed／removed diff。
`removed_from_source` 只表示上游不再回傳，不代表事件解決、機構停業或低風險。
裁罰事件的 `observed_at` 由 exact source-record version 在 chain 中的首次出現推導；
adopted baseline 因時間未知保持空白，不能用 snapshot 日期回填。

此 chain 目前是 **engineering pilot**，不是 recurring collection 的治理核准。Raw
snapshot 含人物／聯絡／車牌欄位，只供 provenance 與機構事件核對，不作個人風險
評分或產品 API 輸出；retention、access、correction／erasure policy 通過前不排程累積。

---

## `ground_truth/` — 人工核對基準

`nonprofit_statements.json`：5 張表逐格判讀轉錄，用來量測抽取準確率。
`tests/test_ground_truth.py` 以會計恆等式驗證其自身一致性（61 項測試）。

⚠️ 裡面有個 `corrections` 欄位記錄我們自己犯的錯（把 `$ -` 記成 0），
是抽取模型交叉比對後指出的。要新增基準時請照同樣標準驗算。

---

## 已知的資料陷阱

寫在這裡是因為每一個都曾讓我們得到錯誤結論，詳細記錄在
[`../CLAUDE.md`](../CLAUDE.md) 與 [`../docs/research/`](../docs/research/)。

1. **`institutions_ntpc.csv` 的列是登記筆數，不是園。** 7 所非營利園因契約更替
   有 2 筆登記，園名只差括號內的法人。用 `entity` 欄位去重，否則會重複計數
   並漏掉登記在另一筆下的裁罰。
2. **學年度 N 的資產負債表基準日是 (N+1)/7/31。** 公校用年度、非營利用學年度，
   串接時極易錯。
3. **空白格不是 0。** 抽取結果裡的 `null` 代表「未編列預算」，是稽查發現；
   當成 0 就是對機構捏造財務陳述。
4. **`pdf_survey.csv` 的 `text_ratio` 不能判斷可抽取性**——它把 PUA 亂碼算成
   有效文字。要判斷請用 `text_quality.csv` 的 `pua_ratio`。
5. **`kids_vehicles.json` 的 `next_exam_dt` 全部逾期是上游停止更新**，不是
   332 所幼兒園都開未驗車。該欄只保留作 provenance，不產生逾期標籤；車數只計
   `txn_name` 分類為 active 的 472 輛新北市車，報廢／繳銷／註銷與暫停狀態不計。
   因交易狀態沒有日期，`as_of` 早於 2026-08-10 snapshot 時車輛特徵維持 NA，
   不會拿現在的報廢狀態回寫歷史。
6. **裁罰的受處分角色不能合併。** `負責人` 與 `行為人` 即使同園、同日、同條、
   同金額也可能是不同處分；canonicalization 將 role/name 納入 key，並以
   `source_row_count`、`law_variants` 保留短版／長版原文合併證據。
7. **採購 `uncovered` 不是「沒有契約」。** 它只表示 pinned 決標公告的履約期間不與該
   財報學年度重疊；後續續約可能未被 pilot 查詢命中。`unconfirmed` 也必須維持第三態，
   不得轉成 false。碧城與新店及人查無營運決標同樣不能解讀成低風險。
8. **公告期限已過不等於未改善。** `education_bureau_actions_ntpc.csv` 的 `ordered`
   只證明主管機關已命改善並排定追蹤；沒有後續公告時維持未知，不可推論
   `not_complete`。一般招生公告與只列通用資格規則的「停止招生」文字也不是園所處分。
9. **任何在公立／非營利／私立間有結構差異的特徵，分層前都會看起來很強。**
   `monthly` 與收費漲幅都踩過（分層後效果量腰斬）。
