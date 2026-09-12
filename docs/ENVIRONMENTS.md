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
python run.py bedrock-check    # 確認 AWS 打得通
python run.py frontend         # 產生前端資料
python run.py serve            # → http://127.0.0.1:8000
```

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
python run.py test        # 294 passed（有 data/raw 時）
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

程式裡的 `bedrock.explain_error()` 會把上面前五種翻成中文並附下一步。
