# 過夜工作計畫（2026-09-11 夜 → 09-12 晨）

使用者交辦五件事後離開，授權「有決策請自行立即處理」。這份檔是 heartbeat
每次喚醒時的進度錨點——**改動狀態欄，不要重寫需求**。

決賽 2026/9/12–13。目標是交付一個能在黑客松上呈現的東西。

**VLM 相關的抽取工作使用者指定明天處理，今晚不動。**

---

## 狀態總表

| # | 交辦事項 | 狀態 | 產出 |
|---|---|---|---|
| 1 | 伺服器能正確啟用 | ✅ | `python run.py serve` → :8000，294 測試全過 |
| 2 | Mac／Windows 共用同一條管線 | ✅ | [`run.py`](../run.py)、`.gitattributes`、6 類跨平台 bug |
| 3 | 整理目前的觀察與發現成 md | ✅ | [`docs/FINDINGS.md`](FINDINGS.md) |
| 4 | PDF 建索引／RAG 化 | ✅ | [`07-document-index.md`](research/07-document-index.md) + 8.4 MB 索引 |
| 5 | 時間軸拖曳（回測 → 即時監控） | ✅ | 前端時間軸分頁 + `/api/timeline` |

---

## 1. 伺服器正確啟用 ✅

- `fcntl` 只存在於 POSIX，擋住動態版與 53 條測試 →
  [`filelock.py`](../src/smart_watchdog/filelock.py)（POSIX `flock`／Windows
  `msvcrt`，非阻塞失敗統一拋 `BlockingIOError`）。
- cp950 主控台編不出 `⚠️`／`⬜`，輸出導向檔案時警告訊息本身會弄死行程 →
  [`console.py`](../src/smart_watchdog/console.py)。
- **`[hidden]` 沒有作用**：`.pane{display:flex}` 與 `.dossier{display:flex}`
  蓋掉瀏覽器預設，四個分頁全部疊著顯示、卷宗永久遮住整個右側面板——
  分頁按鈕點了沒反應。已補 `[hidden]{display:none !important}`。
- 主辦方 1.8 GB 資料集已還原（162 份 PDF），原本 skip 的 ground-truth
  來源檢查現在真的跑了。

## 2. Mac／Windows 共用管線 ✅

問題不是「Windows 跑不動」，是**同一個 repo 有兩套指令、而兩套會各自腐爛**。
今晚找到並修掉 **6 類**跨平台 bug，其中兩類會造成資料損壞：

| # | 問題 | 後果 |
|---|---|---|
| 1 | `core.autocrlf` 把 CRLF 寫進內容定址快照 | `kids_vehicles.json` 多 23,961 個 CR，SHA-256 不符，**整條管線被擋** |
| 2 | `str(path.relative_to(root))` 在 Windows 給反斜線 | `prune_unreferenced_objects()` 會**刪光整個內容定址儲存區** |
| 3 | 同上，verify 端 | 誤報「儲存區含未被引用的物件」，看起來像資料被竄改 |
| 4 | manifest key 用 `str(Path)` | `--verify-only` 在兩個平台永遠不一致 |
| 5 | `artifact_root=str(...)` 再用 `f"{root}/{file}"` | 路徑混用分隔符，1,501 列事件全部對不上 |
| 6 | `[hidden]` 被 CSS 蓋掉 | 見上 |

解法是 [`run.py`](../run.py)（只用標準函式庫，venv 建好前就能跑）把直譯器、
匯入路徑、編碼收斂到一處，加上 [`.gitattributes`](../.gitattributes) 讓
`data/` 一律不做行尾轉換。兩個平台的指令**字元級相同**：

```
python run.py --list        python run.py serve
python run.py pipeline      python run.py test
```

## 3. 觀察與發現 ✅ → [`FINDINGS.md`](FINDINGS.md)

六個平行讀取者各自重算，再由對抗性驗證者逐條重跑；23 條量化宣稱中 21 條
完全重現。最重要的三件事：

- **歷史裁罰最大宗是「量體超載」（34.0%）不是虐童（12.1%）。**
- **分層後排序翻轉**：整體受罰率私立 51.6% ≫ 非營利 22.2%，但只看三類都適用的
  「不當對待」條文，變成非營利 15.6% ＞ 公立 8.2% ＞ 私立 5.5%。
- **`baseline_model.py` 一度把自己的結論印反**：它用 `max(key=AP)` 挑模型做
  冷啟動診斷，而 ② GB 只贏 ③ LR 約 0.0003，贏家換人後印出與文件相反的結論。
  已改為一律用**部署配置**評估，並更新 `05-phase1-results.md` §3/§4。

## 4. 文件索引 ✅ → [`07-document-index.md`](research/07-document-index.md)

**刻意不用向量檢索**：132 份財報是純掃描影像（沒有文字可嵌入）、218 頁公校
文字層是造字亂碼、而稽查需要的是可引用的頁碼不是近似相似度。

SQLite + FTS5 trigram，只用標準函式庫。148 文件／1,144 區段／11,448 事實／
4,306 段落，8.4 MB，單次查詢 3–10 ms。中文檢索**用語料自己當字典**探測詞，
並公開用過的詞讓稽查員可複現。

## 5. 時間軸 ✅

`python run.py timeline` 逐年重新訓練一次，把「當時會怎麼排」與「後來真的
出了什麼事」疊在一起：

| 預測時點 | AUC | P@100 | 提升 |
|---|---:|---:|---:|
| 2021 | 0.641 | 23.0% | 2.49x |
| 2022 | 0.690 | 33.0% | 2.53x |
| 2023 | 0.661 | 35.0% | 2.08x |
| 2024 | 0.641 | 34.0% | 2.05x |

四個完整觀察年度平均 AUC 0.658、平均提升 2.29 倍。右設限的年份用虛線標出，
**不與完整觀察的年份連成同一條趨勢線**；最右端「今天」沒有命中率可言，
那一格是待辦清單不是成績。

---

## 6. 會場網路風險 ✅（09-11 夜追加）

動態版原本從 unpkg.com 載 Leaflet，會場擋 CDN 就沒有地圖。已就地保存到
[`webapp/vendor/`](../webapp/vendor/)（896 KB，出處與雜湊記在 `SOURCES.txt`）。

圖磚無法就地保存（數百 MB），所以改成**偵測到連續三次圖磚載入失敗就自動
打開行政區界線**——那份資料本來就在 payload 裡。實測：Playwright 擋掉所有
非 127.0.0.1 的請求後，Leaflet 載入、1,217 個標記、行政區界線、時間軸全部
正常，無 page error。

字型仍走 CDN，但 `style.css` 已列本機備援（PingFang TC／Noto Sans TC／
微軟正黑），斷線只會換字體。

---

## 待辦（heartbeat 每輪挑一項推進）

1. ~~會場網路風險~~ ✅ 已完成（見 §6）
2. **公校決算書頁內文字未進全文索引**（見 [07 文件](research/07-document-index.md) §6）。
   11,014 頁可用文字層，需先排除 PUA 頁並重建表格列。
3. **問句理解層**：`institution`／`year` 目前要由呼叫端給，
   「三多 113 學年度」還不會自動從問句解析出來。
4. **跨年度比較視圖**：「110 到 113 各差多少」目前要跑四次查詢。
5. **`pyproject.toml` 仍缺 `[project] name`/`version`**，`pip install -e .` 會失敗。
   README 已改指向 `requirements.txt`。**決賽前不動封裝**（已決定）。

**VLM 抽取相關一律不碰**——使用者指定明天親自處理。

**未 commit。** 今晚的改動散在 ~30 個檔案，使用者沒有要求 commit，
所以保持未提交狀態等他決定。

## 決策紀錄

- **用 `run.py` 而不是改 68 處 docstring**：docstring 會再度分岔，單一入口不會。
- **`extract_public_kindergartens.py` 改寫到 `data/extracted/`**：與版控裡那份
  一致（原本寫 `data/processed/`，兩邊對不上，且沒有任何程式讀它）。
- **索引不做語意改寫**：稽查場景要的是「我查了這個字串，這些文件有」，
  改寫會讓稽查員無法複現同一次檢索。
- **時間軸用與正式版完全相同的模型**：展示的數字若不是上線那個系統的數字，
  就不算數。
- **不動 `pyproject.toml` 的封裝**：決賽前一晚不改封裝方式。
