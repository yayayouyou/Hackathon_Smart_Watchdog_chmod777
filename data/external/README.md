# data/external — 外部公開資料快照

## 為什麼要提交快照到版控

因為**上游資料會消失**。全國教保資訊網的裁罰紀錄有保存期限，過期即下架
（詳見 [../../docs/research/03-external-data.md](../../docs/research/03-external-data.md) §1.2）。
一份釘住日期的快照，是這個專案能重現與被檢驗的前提；
若只在執行時抓取，同一份程式碼在不同日期會得到不同標籤，而且不會報錯。

## 內容

| 檔案 | 取得日期 | 來源 | 內容 |
|---|---|---|---|
| `preschools.json` | 2026-08-10 | `kiang.github.io/ap.ece.moe.edu.tw/preschools.json` | 全國 7,664 所教保機構 GeoJSON |
| `punish_all.json` | 2026-08-10 | `kiang.github.io/ap.ece.moe.edu.tw/punish_all.json` | 6,982 個原始裁罰 source rows |
| `kids_vehicles.json` | 2026-08-10 | `kiang.github.io/ap.ece.moe.edu.tw/kids_vehicles.json` | 3,360 筆幼童車最後交易狀態／1,900 個園所 UUID |
| `manifest.json` | — | 本專案產生 | 每檔 URL、SHA-256、bytes、schema、筆數、HTTP metadata 與品質摘要 |
| `observations/` | 見各 observation manifest | 本專案產生 | Root 三份 JSON 的 content-addressed immutable objects、前序 observation chain 與 record-level `added`／`changed`／`removed_from_source` diff；baseline 不把既有資料偽裝成新事件 |
| `evaluation/ntpc-pilot-v1/` | 見子目錄 manifest | 官方 `evaSearch.aspx` | 5 個 allowlisted 查詢的 9 份 WebForms HTML、source-faithful JSON 與 9 筆評鑑事件 |
| `procurement/ntpc-pilot-v1/` | 見子目錄 manifest | g0v/openfun API（資料源為政府電子採購網）| 5 個 allowlisted 查詢、8 份 raw JSON、3 筆 canonical 受託營運決標與 12 列財報年度時間軸 |
| `education_bureau_announcements/ntpc-pilot-v1/` | 見子目錄 manifest | [新北市幼兒教育資源網](https://kidedu.ntpc.edu.tw/app/home.php)／[重要公告](https://kidedu.ntpc.edu.tw/p/403-1000-9-1.php) | 5 份官方 detail HTML、2 份追蹤評鑑 PDF、5 筆 notices 與 98 筆園所 actions |
| `education_bureau_announcements/listing_observations/` | 見各 observation manifest | 新北市幼教重要公告 listing | Phase 1B content-addressed listing observations、固定 predecessor chain、page-1 stability check、2-page／60-ID bootstrap horizon 與 stable-ID discovery diff；bootstrap 為 0 discoveries |

`manifest.json` 誠實標示這批既有快照沒有保留下來的 HTTP response headers 與精確
取得時間；只有原先文件記錄的取得日期。後續每次由 downloader 更新都會保存
`retrieved_at_utc`、`last_checked_at_utc`、ETag、Last-Modified 與前後筆數差。

評鑑 pilot 使用另一份子目錄 manifest：同一個記憶體 cookie session 依序 GET／POST，
每次 response 都重新抽取完整 hidden state，再用官方頁面實際提供的 control name/value
查詢。初始 controls 為 `ddlCityS=03`、`txtSchNameS`、`btnSearch=搜尋`；頁面沒有
city AutoPostBack，因此沒有偽造額外 event。歷史評鑑則按實際 `__doPostBack`
`lbPrev` target 展開。manifest 只保留 request body hash，不保存 cookie 或 viewstate
值；所有 redirect 皆限制為相同 HTTPS origin，沒有 TLS bypass。

範圍刻意限制為安溪、北大、碧城、新店及人與一個明確零結果 control。前三所得到
9 筆事件（含前期及追蹤評鑑），後兩個查詢為 0；這些 0 只能解讀為「live 查詢未回
傳資料」，不能解讀成通過、未受評或低風險。這是 parser／identity join pilot，**不
代表新北市完整評鑑母體**。

採購 pilot 以 [g0v/openfun 採購 API](https://pcc.g0v.ronny.tw/) 做搜尋與公告歷史
取得，但每筆決標都保留 [政府電子採購網](https://web.pcc.gov.tw/) 的官方 detail URL；
mirror 只提供結構化存取，不被當成第二個官方權威。舊 API host 現會 301 到
`https://pcc-api.openfun.app` 且不保留 path，因此 downloader 直接 allowlist 新 HTTPS
origin，限制 same-origin redirect、response bytes、頁數、筆數、公告數與總 request 數。

5 個 query 中，安溪、北大、三多各找到 1 筆受託營運決標；安溪以最新更正公告取代
原公告但保留 revision chain，北大只取 `是否得標=是` 的法人，三多以可稽核的重複
學校財團法人名稱正規化對上財報法人。碧城只有設備案，新店及人為 0 source hits；
兩者都不表示沒有契約或低風險。3 筆 award 各展開至 crosswalk 的 4 個財報年度，得到
12 列，其中 1 列履約期間 covered、11 列 uncovered；`unconfirmed` 永遠保留第三態。

教育局公告 pilot 只取 [重要公告](https://kidedu.ntpc.edu.tw/p/403-1000-9-1.php)
人工盤點後固定的 5 個 notice IDs，不是 170 頁 archive 的全量 crawl。6097／6098 的
官方 PDF 分別列 9／89 所追蹤訪視園所；98 筆 action 全部標 `ordered`，表示已命改善
且排定訪視，不能由期限已過推論未改善。7077 是政策修訂；15015 是內文含停止招生
通用規則但未指名園所的負面控制；6787 是普通招生負面控制，兩者皆產生 0 action。
82 筆 exact-title join 保留 entity 全部 sibling UUID，16 筆已不在現行 registry 的歷史
園所維持 unresolved。公告與裁罰連結一律標 candidate-only，不跨來源去重。

Phase 1B 另以 listing observation chain 監控 CMS 實際提供的 30-record pages。Reviewed
bootstrap 固定前 2 頁的 60 個 stable notice IDs，並以最舊邊界的 10 IDs 作 anchors；
不使用最大 ID 或日期當 watermark，也不把 bootstrap 既有列當 alert。後續 poll 必須由
page 1 依已驗證 URL pattern 逐頁走到全部 anchors，且 traversal 前後 page 1 ordered
records 一致、舊 covered IDs 無缺漏、未超過 page/detail caps，才接受 stable-ID set
difference。新 ID 只下載官方 detail HTML，不自動下載附件，不自動配對園所或判定風險，
先標 `unclassified_official_notice`；完整 raw body 留在 immutable object，processed
aggregate 只保留 title、dates、hash、附件 metadata 與 row-level observation provenance。

這仍是 manual engineering monitor，不是 170 頁 archive 的全量聲明，也未核准 recurring
scheduler。Boundary 未到、舊 ID 消失或 cap 用盡都表示 coverage unknown 並 fail closed，
不能解讀為沒有新公告、公告撤回、合規或低風險。

Content was rephrased for compliance with licensing restrictions.

## 來源與授權

上游為「台灣幼兒園地圖」（作者：江明宗 Finjon Kiang，MIT License），
其資料彙整自政府公開資料平台與全國教保資訊網。
原始資料為政府公開資料；此處僅作為競賽研究用途的時點快照。

- 專案：https://github.com/kiang/preschools
- 資料：https://github.com/kiang/ap.ece.moe.edu.tw
- 政府電子採購網：https://web.pcc.gov.tw/
- g0v/openfun 採購 API：https://pcc.g0v.ronny.tw/
- 新北市幼兒教育資源網：https://kidedu.ntpc.edu.tw/app/home.php
- 新北市幼教重要公告：https://kidedu.ntpc.edu.tw/p/403-1000-9-1.php

採購 API 後端程式採開源授權；API 內容仍源自政府電子採購網，使用時依原始來源
的合理使用與註明出處要求辦理。

## 更新方式

更新快照是**明確、獨立**的動作；processed build 預設只讀版控中的 pinned
snapshots，不會在不知情下取得不同日期的標籤。

```bash
# Conditional GET；三檔全部通過 JSON/schema/status 驗證後才替換，並更新 manifest
# 成功更新同時建立 immutable observation 與 record-level diff
.venv/bin/python scripts/download_external_snapshots.py

# 只下載、驗證並預覽 root file delta + record-level diff，不寫入
.venv/bin/python scripts/download_external_snapshots.py --dry-run

# 不連外：驗證完整 observation predecessor chain、content objects、diff 重導與 latest=root
PYTHONPATH=src .venv/bin/python scripts/record_external_observation.py --verify-only

# 官方 evaSearch pilot；每個 response 都釘住 raw HTML、hash 與前序 request
PYTHONPATH=src .venv/bin/python scripts/download_evaluation_pilot.py \
  --snapshot-id YYYY-MM-DD-pilot-v2

# 不連外：核對 9 份 raw hashes 與 parsed hash，再重建 evaluations_ntpc.csv
PYTHONPATH=src .venv/bin/python scripts/download_evaluation_pilot.py \
  --rebuild-from ntpc-pilot-v1

# 採購決標 pilot；raw JSON 與官方公告 provenance 先釘住再產生 CSV
PYTHONPATH=src .venv/bin/python scripts/download_procurement_pilot.py \
  --snapshot-id YYYY-MM-DD-pilot-v2

# 不連外：核對 8 份 raw hashes 與 parsed hash，再重建 12 列時間軸
PYTHONPATH=src .venv/bin/python scripts/download_procurement_pilot.py \
  --rebuild-from ntpc-pilot-v1

# 新北市幼教官方公告／改善追蹤 bounded pilot
PYTHONPATH=src .venv/bin/python scripts/download_education_bureau_pilot.py \
  --snapshot-id YYYY-MM-DD-pilot-v2

# 不連外：由 5 份 detail HTML／2 份 PDF 重導並重建 notices/actions CSV
PYTHONPATH=src .venv/bin/python scripts/download_education_bureau_pilot.py \
  --rebuild-from ntpc-pilot-v1

# Phase 1B：連網完整驗證 bounded candidate，但不寫入
PYTHONPATH=src .venv/bin/python scripts/monitor_education_bureau_listings.py \
  --preview

# Phase 1B：人工確認 preview 後才接受一個新 observation
PYTHONPATH=src .venv/bin/python scripts/monitor_education_bureau_listings.py \
  --update YYYYMMDDTHHMMSSZ

# 不連外／不寫入：重導 listing chain、discovery diff 與 live aggregate
PYTHONPATH=src .venv/bin/python scripts/monitor_education_bureau_listings.py \
  --verify-only

# 不連外：核對根快照及 evaluation/procurement/announcement 全部 provenance
PYTHONPATH=src .venv/bin/python scripts/verify_external_artifacts.py

# 從 pinned snapshots 重建 processed tables（不連外）
PYTHONPATH=src .venv/bin/python scripts/build_institution_master.py
```

Root updater 以 `data/external/.snapshot-update.lock` 防止重疊執行，並用 durable
transaction journal 在下一次啟動時回復被中斷的 multi-file replacement；journal
完成或回復後會清除未被 observation manifest 引用的 objects。Verifier 也拒絕
branch/cycle/orphan chain、非 content-addressed 路徑、summary/time/provenance tampering
與 unreferenced objects。

**目前 observation 只屬 engineering pilot，不代表已核准 recurring 排程。** Root
payload 含園所地址／電話／負責人、裁罰 actor name 與車牌；這些值只作 provenance
與機構事件核對，不作個人評分，也不由產品 API 暴露。Retention period、存取邊界與
更正／刪除流程通過 privacy review 前，不累積新的 recurring immutable versions；
合法更正或刪除義務優先於技術上的 immutable 設計。

若任一上游檔案筆數下降或 registry UUID 消失，downloader 預設拒絕覆蓋；
裁罰下降通常代表官方保存期限下架，而不是資料修正。人工核對移除內容後，才能
明確傳入 `--allow-count-decrease`。可先使用 `--dry-run` 只下載、驗證並顯示差異。
新增未知的車輛 `txn_name` 也會拒絕更新，避免把新狀態默認成有效車。

初次接管沒有 HTTP metadata 的既有快照可使用：

```bash
.venv/bin/python scripts/download_external_snapshots.py \
  --adopt-existing --retrieved-on YYYY-MM-DD
```

`build_institution_master.py` 會將原始裁罰保守 canonicalize：actor role/name 一定
保留；只有同 actor、同園、同日、同條項、同處分，且短版 law 可唯一對應一個
長版 law 時才合併。完全同值但沒有 upstream event ID 的列不會武斷刪除，而以
共用 `penalty_group_id` 標記為待稽核候選。非金錢處分的 `fine` 維持空值，不能
解讀成 0 元。
