# 官方近即時事件與社群預警實作計畫

## 1. 決策摘要

本專案不把社群聲量直接併入永久風險分數，而採兩層設計：

1. **Baseline risk**：使用登記、裁罰、財報、評鑑、採購與教育局公告等可稽核資料，回答長期稽查優先序。
2. **Realtime alerts**：使用新出現的官方事件，後續再依序評估新聞、公開評論與公開社群，回答近期是否需要人工查看。

排序時先看 `urgent/review` 警報，再以 baseline risk 排同層事件。未經查證的社群內容不改寫歷史風險，不作違規標籤，也不公開指控機構。

第一個實作切片只做**可離線、可重建的官方事件正規化層**。它不新增 crawler、不宣稱 pilot 是完整母體，也不進行跨來源事件自動合併。

---

## 2. 目標與非目標

### 2.1 目標

- 建立跨來源共用的官方事件 schema。
- 將既有 pinned 產物正規化成單一事件時間線。
- 保留原始來源、hash、snapshot、園所 entity 與完整 sibling UUID。
- 明確區分事件時間、發布時間與系統觀測時間。
- 支援無網路 deterministic rebuild 與 no-write verification。
- 為後續增量監控、新聞、評論與社群 adapter 提供穩定 contract。

### 2.2 非目標

- 不把 pilot 的查無資料解讀成合規或低風險。
- 不將相似的裁罰、改善公告或新聞自動判為同一事件。
- 不由期限已過推論改善未完成。
- 不把採購 `uncovered` 解讀成沒有契約。
- 不在 v1 建立跨來源統一嚴重度分數。
- 不蒐集私人社團、登入後內容、兒童姓名、臉部影像或聯絡資訊。
- 不建立個人使用者可信度或人物風險檔案。

---

## 3. 分階段架構

```text
官方/公開來源
    ↓
raw-first snapshot + manifest + hash
    ↓
source-specific parser
    ↓
source-faithful processed tables
    ↓
normalized official events
    ↓
observed snapshot diff / alert candidates
    ↓
human review and disposition
    ↓
verified event overlay + baseline risk ordering
```

### Phase 0 — 統一官方事件底座（現在）

輸入現有版控產物：

- `institutions_ntpc.csv`
- `penalties_ntpc.csv`
- `evaluations_ntpc.csv`
- `education_bureau_notices_ntpc.csv`
- `education_bureau_actions_ntpc.csv`
- `nonprofit_procurement_contracts.csv`
- 對應 external manifests

輸出：

- `data/processed/official_events_ntpc.csv`
- `data/processed/official_events_ntpc.manifest.json`

固定 event grain：

| Event family | Grain | 預期筆數 |
|---|---|---:|
| penalty | 每個 canonical sanction record | 1,386 |
| evaluation | 每筆官方評鑑結果 | 9 |
| education_notice | 每份 bounded pilot 公告 | 5 |
| corrective_action | 每個園所改善／追蹤 action | 98 |
| procurement_award | 每個 unique supplier award | 3 |
| **合計** |  | **1,501** |

採購 processed table 的 12 列是 3 個 award × 4 個財報年度，事件層必須收斂為 3 筆，不得計成 12 個事件。

### Phase 1 — 官方增量監控

- 對 root snapshots 建立 immutable observed snapshots 與 record-level diff。
- 教育局公告由固定 pilot 進展為有邊界的 listing pagination／watermark。
- 評鑑與採購先完成 identity inputs pinning，再擴大查詢範圍。
- 每次 poll 保存 `observed_at`、HTTP metadata、raw hash、added/changed/removed records。
- removed 只標示來源消失，不刪除歷史事件，也不推論事件撤銷。
- Root observation 目前只核准 engineering pilot；person-linked retention policy
  通過前不得設為 recurring scheduler。

### Phase 2 — 新聞與 RSS pilot

- 只使用公開、可引用來源。
- 以園所正式名稱、aliases、法人名稱、地址與行政區做 entity resolution。
- 先做事件 taxonomy、去重與人工覆核，不做情緒分數。
- 量測相對官方事件的 lead time、precision 與每日人工負荷。

### Phase 3 — Google Reviews pilot

- 使用官方 API，不抓取登入後或未授權資料。
- 對 50–100 所按類型、行政區、baseline risk 與既有裁罰分層抽樣。
- 保存最小化 metadata、來源 URL、hash 與衍生事件分類；不建立永久全文語料庫。
- 驗證評論事件是否提供 baseline 之外的增量價值。

### Phase 4 — 公開社群 pilot

- 只有在 API／平台條款、隱私與保存政策確認後才啟動。
- 不存兒童個資、不進私人群組、不繞過登入、不追蹤個人帳號。
- 單篇貼文只能產生 `watch` candidate；需獨立佐證或官方後續才能升級。

### Phase 5 — 回測與產品整合

- 以嚴格時間切分驗證社群／新聞是否提前命中後續官方裁罰、改善或評鑑事件。
- Dashboard 分開呈現 baseline risk 與 realtime alert，不顯示混合黑箱分數。
- 所有警報保留人工 disposition、申訴、更正與到期機制。

---

## 4. Official event schema v1

核心欄位：

| 區塊 | 欄位 | 語意 |
|---|---|---|
| Key | `event_id` | deterministic、全表唯一 |
|  | `source_system` | upstream authority／mirror 系統 |
|  | `source_record_id` | source-local stable key |
|  | `event_family` / `event_type` | 事件家族與細分類 |
|  | `parent_event_id` | notice/action 等明確 parent-child 關係 |
| Identity | `entity` | 實體園 identity，可空 |
|  | `source_registry_id` | 原始事件直接帶的 UUID，可空 |
|  | `registry_ids` / `registry_titles` | entity 的完整 sibling set |
|  | `identity_status` / `identity_evidence` | 配對方法，不混入證據驗證狀態 |
| Time | `event_date` | 事件發生／決定日期 |
|  | `event_date_semantics` | sanction/evaluation completion/order/award decision 等 |
|  | `period_start` / `period_end` | 契約或有效期間 |
|  | `published_date` | 來源發布日期 |
|  | `observed_at` | 本系統首次看到此版本的時間；未知保持空值 |
| Content | `title` / `summary` | source-faithful 摘要 |
|  | `lifecycle_status` / `outcome` | ordered、completed、passed 等來源語意 |
|  | `amount` | 金額；非金錢處分保持空值，不填 0 |
| Trust | `source_authority` | official / official_mirror |
|  | `verification_status` | pinned raw/snapshot 是否可重導驗證 |
|  | `source_url` | 原始或官方證據 URL |
|  | `raw_artifacts_json` / `source_hashes_json` | provenance |
| Interpretation | `severity` / `severity_basis` | v1 僅裁罰可依既有法條 taxonomy；其他可空 |
|  | `details_json` | source-specific、可稽核的補充欄位 |

三種狀態不得混用：

- `verification_status`：來源 bytes／重導是否可驗。
- `identity_status`：是否及如何配對到機構。
- `lifecycle_status/outcome`：事件本身進度或結果。

### 4.1 Event ID 規則

- penalty：`penalty:<penalty_group_id>:<group_record_index>`；共享 group ID 的 ambiguous records仍各自存在。
- evaluation：由 source authority、query/source row、園名、學年度、完成日與結果產生 deterministic digest。
- notice：`education_notice:<notice_key>`。
- action：`corrective_action:<action_key>`，parent 指向 notice event。
- procurement：`procurement_award:<supplier_award_key>`。

### 4.2 時間與 leakage 規則

- `event_date` 與 `observed_at` 是不同時間軸。
- 歷史 feature 在 `as_of` 時只能使用 `event_date < as_of` 且 `observed_at <= as_of` 的資訊。
- 若舊 snapshot 未保留精確首次觀測時間，`observed_at` 保持空值，不用 snapshot 日期臆測。
- 學年度只是 period label，不自動轉成事件日期。

---

## 5. 來源與語意政策

### 5.1 Penalties

- 每個 canonical record 是一筆 event。
- 完全同值且無 upstream event ID 的 records 保留，不武斷合併。
- `penalty_group_id` 是稽核群組，不一定是一個已證實事件。
- actor role/name 保留在 details；不同角色不可合併。

### 5.2 Evaluations

- 每列是 evaluation event；通過結果是正向／中性 outcome，不是風險事件。
- `evaluation_completed_date` 是 event date；學年度保留於 details。
- pilot 查無結果不建立「通過」事件。

### 5.3 Education notices and actions

- notice 是 parent event，可不指向特定 entity。
- action 是 institution event，parent 指向 notice。
- 98 筆 action 的 `ordered` 只表示命改善／排訪，期限經過不能改成 `not_complete`。
- candidate penalty links 不用來跨來源 dedupe。

### 5.4 Procurement

- 12 個 report-year rows 收斂為 3 個 supplier awards。
- award decision date 是 event date，notice date 是 published date，履約日期是 period。
- `uncovered/unconfirmed` 是財報年度 temporal join，不是 award lifecycle 或風險狀態。
- mirror 只提供結構化存取；事件保留政府電子採購網官方 URL。

---

## 6. Alert governance（後續來源共用）

警報層使用離散狀態，不把社群加總進 baseline score：

- `none`：沒有近期 candidate。
- `watch`：單一或低可信度公開訊號。
- `review`：有獨立佐證，需人工判讀。
- `urgent`：高嚴重度，需立即人工查看；不等於指控成立。
- `resolved`：已有官方結果或人工結案。

事件分類優先於 sentiment：兒少安全、人員管理、衛生健康、交通安全、財務收費、營運穩定、招生契約、一般服務抱怨。

社群與評論的分析單位是「園所 × 事件群集」，不是貼文數；轉貼同一事件不得重複加權。

---

## 7. 驗收與決策門檻

### 7.1 Phase 0 驗收

- 無網路可重建。
- 同 inputs 產生 byte-identical CSV 與 manifest。
- 1,501 unique event IDs，family counts 固定。
- 98 action parent 全部可解析到 5 notices。
- 3 個 unique procurement awards，不是 12。
- 每個非空 entity 的 `registry_ids` 等於 institutions 完整 siblings。
- 16 筆歷史園所 action 保持 unresolved，不被刪除或模糊硬配。
- 空罰鍰不轉成 0。
- 所有日期為 ISO 或空值，且具明確 semantics。
- Builder `--verify-only` 不寫檔。
- 整合 verifier 驗 input hashes、output hash、counts、parent references 與 siblings。

### 7.2 Phase 1A engineering pilot 治理門檻

- Root snapshots 含園所地址／電話／負責人、裁罰 actor name 與車牌；這些欄位只作
  source provenance 與機構事件核對，不作個人風險評分，也不得由產品 API 暴露。
- Baseline observation 可供離線重導；在 retention period、存取邊界與合法目的完成
  書面審查前，不啟用 recurring poll、不累積新的 person-linked immutable versions。
- Upstream correction／removal 先進人工 review。`immutable` 是完整性機制，不凌駕
  更正或刪除義務；合法請求成立時可停止發布並移除／重建受影響 history，同時保存
  不含該識別資料的決策稽核紀錄。
- Updater 必須持有單一 repository lock、使用 durable transaction journal，並在恢復或
  baseline adoption 失敗後刪除未被 manifest 引用的 content objects。
- 正式排程前仍需加入 multi-node、tamper、correction、duplicate、crash recovery、
  lock contention 與 future knowledge-time 的自動化 regression coverage。

### 7.3 新聞／評論／社群 pilot 的 go/no-go metrics

- Entity match accuracy。
- Event-cluster duplicate rate。
- Alert precision 與 false-positive workload。
- 相對官方事件的 median lead time。
- 加入訊號後相對 baseline 的 incremental lift。
- 每日人工覆核量。
- 類型、行政區與數位活躍度 coverage bias。
- 兒童／家長 PII 泄漏事件必須為 0。

只有在增量價值與人工負荷達標、平台條款允許、隱私審查通過後，才擴大來源。

---

## 8. 實作順序

1. 建立 pure event adapters 與固定 schema。
2. 建立薄 CLI builder、manifest 與 `--verify-only`。
3. 產生並驗證 1,501-row official event projection。
4. 將 event artifact 納入 external artifact verifier 與資料文件。
5. 補 root snapshot 的 immutable observations/diff state。
6. 將教育局公告擴為有邊界 listing monitor。
7. 補 evaluation identity inputs pinning，再擴查詢。
8. 進行新聞／RSS pilot。
9. 進行 Google Reviews pilot。
10. 通過回測與治理門檻後才評估公開社群 adapter。

---

## 9. 實作進度

### Phase 0 — 完成

- 已產生 1,501 筆 `official_events_ntpc.csv`。
- 已建立 deterministic manifest 與 no-write `--verify-only`。
- 已納入全域 external artifact verifier。

### Phase 1A — Engineering pilot（provisional，未核准排程）

- Root 三份公開資料已建立 content-addressed immutable objects。
- 初始 adopted baseline 為 `20260810T000000Z-adopted-v1`；因原始取得時間未保存，`observed_at_utc` 誠實維持空值。
- Updater 以 repository lock、durable journal 與啟動恢復保護 root generation；成功後才新增 observation 與 deterministic `changes.csv`。
- 支援 `added`、`changed`、`removed_from_source`；baseline 為零 changes，唯一的一欄裁罰修正會保守 reconciliation 為 `changed`。
- Chain 依 predecessor topology 重建；manifest、content-address、provenance、diff summaries、時間規則與 latest=root 全部 fail closed。
- Official penalty event 以 exact source-record version 的首次 observation 產生 `observed_at`；baseline 未知值保持空白。
- `--dry-run` 會計算 file-level delta 與 record-level diff，但不寫檔。
- 目前只有 baseline 進入版控。Root 內含人物／聯絡／車牌欄位，因此本階段不代表可建立 recurring scheduler；正式排程仍待 retention/privacy approval 與 incremental automated regression coverage。

### Phase 1B — Bounded listing monitor（engineering complete，未核准排程）

- 保留原本 5-notice／98-action `ntpc-pilot-v1`，另建 content-addressed
  `listing_observations/` predecessor chain，不改寫歷史 pilot 語意。
- Reviewed bootstrap `20260816T013500Z-bootstrap-v1` 固定 listing 前 2 頁、60 個 stable
  notice IDs 與最舊邊界 10 anchors；bootstrap 為零 discoveries，不把現存公告標成新事件。
- 後續 poll 從 page 1 依官方 CMS 的 validated URL pattern 逐頁前進，直到全部 fixed
  anchors 出現；不使用 max ID／date 作 watermark，支援 backdated／reordered listing。
- Poll 必須通過 page-1 前後 ordered-record stability、previous-covered-ID completeness、
  page/detail caps、same-origin HTTP provenance 與 deterministic offline rederivation；任一失敗
  都不寫入，也不得宣稱「沒有新公告」。
- Stable-ID 新增才下載 detail HTML；不自動下載附件、不做園所 identity inference、
  corrective-action／severity 推論，先標 `unclassified_official_notice`、`action_count=0`。
- Discovery aggregate 只保存必要 metadata/hash，不保存完整 body；每列帶自己的
  `observation_id`、`observed_at`、artifact root/path，official event 不會用最新 poll time
  回填歷史公告。
- CLI 明確區分 network `--preview`、人工接受 `--bootstrap/--update`、offline
  `--verify-only/--rebuild`；publication 使用 shared lock、durable journal 與 orphan prune。
- Official-event builder 與 integrated verifier 已 pin/rederive listing chain；事件 family count
  對 verified discovery aggregate 動態增加，同時守住原始 5 notices、98 ordered actions 與
  16 unresolved historical actions。

### Phase 1 整體狀態

Phase 1A root observations 與 Phase 1B announcement listing monitor 的工程路徑均已完成。
但兩者都仍是 manual engineering pilot：retention/privacy approval 與 checked-in incremental
regression coverage 完成前，不建立 recurring scheduler。下一個來源切片是 evaluation／
procurement identity-input pinning 與 bounded expansion，仍先不接社群。
