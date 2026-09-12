# 合併計畫：把 agent 與 MCP 併進稽查派工台

**基底** `yayayouyou/Hackathon_Smart_Watchdog_chmod777`（本 repo）
**來源** `Eason20050201/hackathon@a0bdada`
**Branch** `feat/agent-mcp`
**目標** 使用者與 agent 對話，agent 邊操作網站邊講解，完成稽查工作。全本機，部署最後做。

---

## 0. 一句話說明要做什麼

chmod777 有**資料與畫面**（1,213 園地圖、25 條端點、4,293 段可引述財報、時間軸回測），
hackathon 有**會操作網站的 agent 與 MCP**。把後者搬進前者。

不是把兩個系統疊在一起——**凡是兩邊都有的，一律留 chmod777 那一份**。

---

## 1. 已完成（階段 0）

帳號處理，因為 agent 的 `record_feedback` 要記「哪一位稽查員」，MCP 的身分也要掛在使用者上。

| 產出 | 說明 |
|---|---|
`src/smart_watchdog/security.py` | bcrypt 雜湊、權杖。搬自來源 repo，原樣 |
`src/smart_watchdog/db/models.py` | 5 張表：`user`／`user_session`／`agent_session`／`agent_message`／`audit_feedback` |
`src/smart_watchdog/db/session.py` | SQLite 引擎，讀 `.env` 的 `DATABASE_URL` |
`src/smart_watchdog/api/auth.py` | `/api/auth/login`、`/logout`、`/me` |
`scripts/seed_users.py` | 建表 + 建帳號，冪等 |
`tests/test_auth.py` | 7 個測試 |

驗證：`299 passed, 2 skipped`、`lint` 全過。

**三個已下的決定**：不搬 alembic（5 張新表 `create_all` 就夠）；`JSONB`→`JSON`（SQLite 不認得）；
`audit_feedback.institution_id` 拆掉外鍵（`institution` 表沒搬）。

---

## 2. 技術棧（決定與理由）

| 層 | 決定 | 為什麼 |
|---|---|---|
前端 | **維持 vanilla JS + Leaflet，不引入 Next.js／React** | 現有 72 KB 手寫 JS 撐起 4 個頁籤；重寫是數天。而且 `webapp/vendor/` 就地保存 Leaflet 是刻意的離線降級，Next.js 會加 build step 與 node runtime |
後端 | **FastAPI**，不變 | 兩邊本來都是 |
資料庫 | **SQLite**（`data/runtime/watchdog.sqlite`） | 只需帳號與 agent 稽核 5 張表。業務資料留在既有 CSV／payload／sqlite。之後上 RDS 只換 `DATABASE_URL` |
agent LLM | **boto3 `converse_stream`** | 原生支援文字與 tool 呼叫交錯。已實測可用（見 §8） |
既有三落點 | **維持 `anthropic` 的 `AnthropicBedrock`** | 實測 7/7 通，不重寫能跑的東西 |
MCP | **fastmcp 4.0.3** | 工具清單由 `build_registry().schemas()` 產生，不會與 registry 漂移 |

放棄的東西講明：來源 repo 的 13 個前端測試、Tailwind、`openapi-typescript` 型別安全、
4 個 React 元件。我們搬它們的**事件協定**，不搬程式碼。

---

## 3. 12 個 tool 對照到既有資料來源

**關鍵槓桿**：`src/smart_watchdog/api/chat.py` 已經有一套篩選引擎——`QueryPlan` 加
`_matches()`，支援 `town`／`type`／`has_penalty`／`has_compliance_failure`／
`has_mentions`／`no_financial`／`sort`／`limit`。那正是 `list_institutions` 要的東西。
**agent 取代的是 `KeywordPlanner`（產生 plan 的那一層），不是整支 chat.py。**

| tool | 既有依據 | 工作 |
|---|---|---|
`list_institutions` | `chat._matches()` + `GET /api/institutions` | 包一層 |
`get_ranking` | `GET /api/proposal?n=&town=&financial_only=`（已含 `tier`） | 包一層 |
`open_institution` | `GET /api/institutions/{id}`（五區塊卷宗） | 包一層 |
`get_penalties` | 卷宗內 `institution` | 包一層 |
`get_findings` | 卷宗內 `dossier.findings`（`compliance_findings.csv`） | 包一層 |
`set_time_machine` | `GET /api/timeline/{as_of}?n=`（逐園排序含 `hit`） | 包一層 |
`get_model_card` | `GET /api/timeline`（7 點 AUC／precision／lift） | 包一層 |
`search_documents` ⭐ | `GET /api/docsearch?q=`（4,293 段，回檔名＋頁碼） | 包一層。**新 tool，來源 repo 沒有** |
`open_memo` | `data/processed/audit_letters/*.txt`（144 份） | 新寫小端點 |
`show_evidence` | 渲染頁截圖（需先還原 `data/raw`） | 見 §7 |
`export_schedule` | 無 | 新寫（CSV blob 下載） |
`record_feedback` | 無 | 新寫（寫 `audit_feedback` 表） |
`load_skill` | 4 份 `.md` | 複製後改寫，見 §6 |

計 8 個包一層、3 個新寫、1 個複製。**唯一寫入型 tool 是 `record_feedback`**——
registry 裡沒有能改分數、改建議書、對外送資料的 tool，所以即使 LLM 被說服要做那些事，
也沒有工具可用。這條是架構上的，不是提示詞裡的請求。

---

## 4. `ui_action` 對照到既有前端函式

`webapp/app.js:533` 已經有全域匯出面：

```js
window.SW = { api, post, $, esc, nf, state, openDossier, TYPE, drawMarkers, refresh };
```

`state` 的形狀（`app.js:24`）：`{ cap, cluster, flaggedOnly, types:Set, timeline, points, byId, ... }`

所以分派表是把 agent 事件接到**已經存在的函式**，不是做新畫面：

| `ui_action` | 接到什麼 |
|---|---|
`navigate` | 點 `.tabs button[data-t=…]`（切 `pane-list`／`pane-chat`／`pane-scan`／`pane-timeline`），或 `SW.openDossier(id)` |
`set_filters` | 改 `SW.state`（`types`／`flaggedOnly`／`cap`）後呼叫 `SW.drawMarkers()` |
`open_drawer` | `SW.openDossier(id)` |
`close_drawer` | 既有的卷宗收合 |
`highlight` | `drawMarkers()` 的 flagged 路徑（`pinIcon(p, flagged)`）加一組 highlight id |
`download` | 新寫：blob + `a.click()` |

**待補**：地圖目前沒有行政區篩選控制項（只有 `f-cluster`／`f-districts`／`f-flagged`）。
`set_filters` 要支援 `town` 得加一個控制項，或讓 agent 改走 `list_institutions` 換資料。

---

## 5. 要搬的檔案

```
來源 repo                                    →  本 repo
backend/app/agent/loop.py                    →  src/smart_watchdog/agent/loop.py       近乎原樣
backend/app/agent/registry.py                →  src/smart_watchdog/agent/registry.py   近乎原樣
backend/app/agent/narration.py               →  src/smart_watchdog/agent/narration.py  原樣
backend/app/agent/memory.py                  →  src/smart_watchdog/agent/memory.py     原樣
backend/app/agent/tools.py                   →  src/smart_watchdog/agent/tools.py      handler 全部重寫（見 §3）
backend/app/agent/skills/*.md                →  src/smart_watchdog/agent/skills/       改寫（見 §6）
backend/app/mcp_server.py                    →  src/smart_watchdog/agent/mcp_server.py 調整 import
src/smart_watchdog/llm/agent.py              →  src/smart_watchdog/agent/protocol.py   事件型別，原樣
src/smart_watchdog/llm/agent_bedrock.py      →  src/smart_watchdog/agent/backend.py    原樣
src/smart_watchdog/llm/mcp_tokens.py         →  src/smart_watchdog/agent/tokens.py     原樣
components/agent/*.tsx                       →  webapp/agent.js                        改寫成 vanilla
```

**不搬**：Next.js 全部、Postgres 那 9 張業務表、alembic、memo 五道閘門
（本 repo 有自己的 `report/verify.py`）、`pg_advisory_xact_lock`（改用既有 `filelock.py`）。

因為兩個 repo 沒有共同歷史，`git merge` 不可行。每個 commit 要註明來源 commit hash。

---

## 6. SOP 必須改寫，不能照搬

來源 repo 的 4 份 skill（240 行）裡寫死了**對它自己前端**的畫面對應。照搬會讓 agent 講錯話：

| 原文寫的 | 在本 repo 的實情 |
|---|---|
「`get_ranking` **不換頁**，疊一個面板；不要說『我把它們在名單上標起來了』」 | 本 repo **有地圖**，真的可以標。這句禁令要改成允許 |
「`/list?town=&type=&no_prior=&limit=`」 | 本 repo 沒有 `/list` 路由，是頁籤 `pane-list` |
「`export_schedule` 直接觸發下載」 | 仍成立，但實作不同 |

**要保留的部分**（這些與前端無關，是輸出定位的界線）：
禁用詞（不得說違法／造假／有問題）、量詞（一律說「筆」不說「家」）、
`priority_rank` 與 `rank` 不可混用、`tier` 要翻成中文、不要輸出 markdown、
每句不超過 30 字、講出來的每個數字都必須是 tool 剛回傳的。

新增一份 `read_evidence.md` 對應新的 `search_documents`。

---

## 7. `show_evidence`：文字 + 截圖都做

- **文字**（零風險）：`document_index.sqlite` 已進版控，4,293 段，回傳檔名與頁碼
- **截圖**（約 1 小時）：本 repo 根目錄的 `E_教育局-資料集.zip`（1.5 GB，已驗證完整）
  → `scripts/setup_raw_data.py` 還原 `data/raw` → 渲染財報頁 PNG

還原 `data/raw` 另有一個副作用好處：`test_every_statement_declares_its_source`
那條一直 skip 的測試會開始跑。

---

## 8. 已經實測過的前提（不需要再驗）

| 項目 | 結果 |
|---|---|
Bedrock 帳號 | `550561128629` / `WSParticipantRole` / us-west-2，7/7 模型可用 |
`converse_stream` + `toolConfig` | ✅ 回 `stopReason: tool_use`，講解句「列出板橋區前10筆機構名單。」（13 字）+ `list_institutions{"town":"板橋區","limit":10}` |
`bedrock:InvokeModelWithResponseStream` | 權限有（agent 串流可行） |
App Runner | ❌ `apprunner:CreateService` implicitDeny → 部署要改 ECS Fargate |
RDS／ECR／ECS／Amplify／S3／Lambda／CloudFormation | 建立權限都有；但帳號目前**空的**，沒有任何現存資料庫 |

⚠️ AWS 環境只開放到 **9/13 13:00**（＝交件時刻），憑證是會過期的臨時憑證。
**AWS 上的東西是展示用，交付物必須是 repo + 本機可跑。**

---

## 9. 階段與驗收條件

每一階段結束都必須是「可跑、測試全綠」的狀態。

| # | 階段 | 驗收條件 | 可砍 |
|---|---|---|---|
0 | 帳號處理 | ✅ 已完成，299 passed | — |
1 | agent 骨架 | `POST /api/agent/messages` 串流出 `text`／`tool_call`／`tool_result`／`ui_action`；只註冊 `list_institutions`；畫面真的切到名單頁 | ❌ |
2 | 唯讀 tool 補齊 | §3 的 8 個包一層 + `load_skill` 全部可用，每個都有測試 | ❌ |
3 | 前端 agent 面板 | `webapp/agent.js`：SSE 消費、講解句打字、`ui_action` 分派到 `window.SW` | ❌ |
4 | MCP | `fastmcp` 掛載，用 Claude Code CLI 連進來呼叫同一組 tool 成功 | ❌ 你明確要的 |
5 | 三個新寫 tool | `open_memo`／`export_schedule`／`record_feedback`（寫入 `audit_feedback`） | ⚠️ |
6 | SOP 改寫 | 4 份改寫 + 1 份新增，畫面對應與本 repo 一致 | ❌ 不改 agent 會講錯 |
7 | `data/raw` + 截圖 | 證據抽屜同時給圖與頁碼 | ✅ 第一個砍 |
8 | 部署 | RDS + ECR + **ECS Fargate**（非 App Runner），單 task 前後端同源避開 cookie 跨域 | ✅ 最後做 |

---

## 9b. 完成狀態（2026-09-12）

八個階段全部完成，四個 commit。實測結果：

| 階段 | 驗收 |
|---|---|
0 帳號 | 登入六個案例正確；查無帳號與密碼錯誤回應時間相近（不洩漏帳號存在與否） |
1 骨架 | `session → text → tool_call → tool_result → ui_action → text → done` |
2 tool | 12 個註冊，8 個包既有端點 |
3 前端 | 助理頁籤、登入層、`ui_action` 分派到 `window.SW` |
4 MCP | `/mcp` 掛載；無 token 與未註冊 tool 都被 registry 擋下 |
5 寫入 | `record_feedback` 追加式寫入，帶稽查員身分 |
6 SOP | 五份改寫；實測 agent 會自己 `load_skill` 再照步驟做 |
7 證據 | `data/raw` 還原（162 份）；隨用隨渲染，文字與原始頁面核對一致 |
8 部署 | ECS Fargate 跑起來，雲端 agent 走 Bedrock 正常 |

**379 passed**（POSIX 上 378 passed + 1 skipped——唯一的 skip 是 POSIX 沒有逾時可測的 flock；Windows 走另一條實作，不 skip）。
之後又與 main 合併（版面以 main 為準，助理與建議書加進上方功能列），
並補了前端靜態健檢。

### 一個沒有補上的內容缺口

來源專案的 `docs/known-weaknesses.md` 有一份誠實的自評：**無前科子群的
AUC 是 0.517（2023-12-31 快照），lift 0.60——比隨機還差**。那份文件明寫
「不可以調參數把這個數字修好」「不可以只展示好看的那兩份快照」。

本 repo 的 `timeline.json` **沒有這個維度**（欄位只有整體 `auc`、`precision_at`、
`lift_at`，沒有分子群）。所以 `get_model_card` 現在只能報整體指標，被問到
「無前科的園你們分得出來嗎」時答不出那個對自己不利的數字。

要補的話不是搬程式，是**重算時間軸並加上分子群指標**。在補上之前，
回答這個問題要靠人，不要讓 agent 用整體 AUC 帶過——那正是來源專案警告的
「把整體 AUC 拿來當『我們找得到沒前科的高風險園』的證據」。

### 還沒做但知道的事

- **講解句長度沒守住 30 字。** 量詞、禁用詞、界線句都對，但要列多筆時會寫成
  一長段。SOP 已加「只說明前三筆」，仍需要再收。
- **證據頁在雲端是 404**，因為 `data/raw` 不進映像（見 `docs/DEPLOY.md`）。

---

## 10. 待決事項

1. ~~`pyproject.toml` 的 `target-version = "py39"` 與 venv（3.11）不符~~
   → **維持 py39**。實測提到 py311 會讓既有程式跳出 30 條 lint 錯誤，
   那是去改沒被要求碰的檔案。改為把新檔加進 `per-file-ignores`。
2. **agent 還操作不到的功能**（下一個分支）：

   | 缺的 tool | 對應 | 估時 |
   |---|---|---|
   | `list_memos` | 建議書清單瀏覽 | 20min |
   | `get_rank_track` | 單園排名軌跡 | 20min |
   | `get_staffing` | 員工與師生比 | 20min |
   | `get_realtime` | 即時輿情＋Google 評論 | 40min |
   | 擴充 `set_filters` | 真的套用 town／flagged／cluster／cap，以及 main 新增的反灰／區名／著色依據 | 1.5h |
   | 掃描那組 | **會花錢**，建議只給 `scan_estimate`（算錢不花錢） | 3h |

3. **無前科子群的指標補不上**（見 §9b）。要重算時間軸加分子群維度，
   不是搬程式。在那之前這一題要人回答，不要讓 agent 用整體 AUC 帶過。

---

## 11. 不能跨越的界線（合併後一樣適用）

1. 輸出是**建議查核的稽查優先序**，不是違法認定。不確定時標「資料不足」，不標「低風險」。
   agent 的講解句同樣適用——system prompt 與 4 份 SOP 都要寫進去。
2. **個別機構分數不對外公開揭露**，這是本 repo 設 private 的理由。
3. **憑證絕不進版控。** `.env` 已 gitignore 並設 600；`data/runtime/` 也在 gitignore
   （裡面有密碼雜湊與對話稽核軌跡）。
4. agent 的 tool registry 裡**不得出現**能改分數、改建議書、對外送資料的 tool。
   安全邊界在 registry，不在前端。
