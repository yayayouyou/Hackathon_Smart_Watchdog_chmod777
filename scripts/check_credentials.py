"""逐一實測 .env 裡的憑證，失敗時直接給出修法。

    PYTHONPATH=src .venv/bin/python scripts/check_credentials.py

不是檢查「有沒有填」，是**實際打一次 API**。金鑰填了但服務沒啟用、
金鑰限制沒包含該服務、權限尚未核准，這三種情況都會讓「有填」變成一種假象。
"""

from __future__ import annotations

import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.console import use_utf8

use_utf8()

from smart_watchdog import config

CONSOLE = "https://console.cloud.google.com"


def _post(url: str, key: str, body: dict, mask: str) -> tuple[int, dict]:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": mask})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read(500_000))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read(50_000) or b"{}")
    except Exception as exc:  # noqa: BLE001 - 網路問題也要照實回報
        return 0, {"error": {"message": f"{type(exc).__name__}: {exc}"}}


def check_google() -> None:
    key = config.get("GOOGLE_MAPS_API_KEY")
    print("── Google Maps Platform ──")
    if not key:
        print("  ⬜ 未設定 GOOGLE_MAPS_API_KEY")
        return
    print(f"  金鑰已載入（長度 {len(key)}）")

    st, data = _post(
        "https://places.googleapis.com/v1/places:searchText", key,
        {"textQuery": "新北市私立吉尼爾幼兒園", "languageCode": "zh-TW"},
        "places.id,places.displayName,places.rating,places.userRatingCount")

    if st == 200:
        places = data.get("places") or []
        print(f"  ✅ Places API (New) 可用，回傳 {len(places)} 筆")
        if places:
            p = places[0]
            name = (p.get("displayName") or {}).get("text", "")
            print(f"     {name}　評分 {p.get('rating', '—')}"
                  f"（{p.get('userRatingCount', 0)} 則）")
            _check_reviews(key, p["id"])
        return

    reason = ""
    for d in (data.get("error") or {}).get("details", []):
        reason = d.get("reason", reason)
    msg = (data.get("error") or {}).get("message", "")
    print(f"  ❌ HTTP {st}　{reason}")
    print(f"     {msg[:150]}")
    if reason == "API_KEY_SERVICE_BLOCKED" or "not enabled" in msg.lower():
        print("\n  修法（擇一，通常是第一項）：")
        print(f"  1. 啟用服務："
              f"{CONSOLE}/apis/library/places.googleapis.com")
        print("     選對專案 → 「啟用」")
        print("  2. 若金鑰設了 API 限制，把 Places API (New) 加進允許清單：")
        print(f"     {CONSOLE}/apis/credentials → 點該金鑰 → API 限制")
        print("  3. 專案需繫結帳單帳戶（有每月免費額度）")


def _check_reviews(key: str, place_id: str) -> None:
    """評論是另一個欄位權限，單獨確認一次。"""
    req = urllib.request.Request(
        f"https://places.googleapis.com/v1/places/{place_id}",
        headers={"X-Goog-Api-Key": key,
                 "X-Goog-FieldMask": "id,displayName,rating,reviews"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read(500_000))
    except urllib.error.HTTPError as e:
        print(f"     ⚠️ 評論欄位取用失敗 HTTP {e.code}")
        return
    reviews = data.get("reviews") or []
    print(f"  ✅ 評論欄位可用，取得 {len(reviews)} 則")
    for rv in reviews[:2]:
        text = (rv.get("originalText") or rv.get("text") or {}).get("text", "")
        author = (rv.get("authorAttribution") or {}).get("displayName", "")
        print(f"     [{str(rv.get('publishTime', ''))[:10]}] {author}："
              f"{text[:52]}")


def _threads_get(path: str, token: str, **params) -> tuple[int, dict]:
    """一次唯讀的 Threads API 呼叫。權杖走 header，不走 query string。

    舊版把 `access_token=` 放在網址裡。網址會進 access log、進 proxy 紀錄、
    進例外訊息——`urllib` 的 HTTPError 字串就含完整 URL，這支腳本自己
    印出來就會把權杖印在畫面上。header 不會。
    """
    url = f"https://graph.threads.net{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read(500_000))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read(20_000) or b"{}")
    except Exception as exc:  # noqa: BLE001 - 網路問題也要照實回報
        return 0, {"error": {"message": f"{type(exc).__name__}: {exc}"}}


def _threads_error(status: int, data: dict) -> str:
    return (f"HTTP {status}　"
            f"{(data.get('error') or {}).get('message', '')[:120]}")


def check_threads() -> None:
    """兩個管道共用一把權杖，但門檻不同——所以分開測，分開報。

    `/me/mentions` 只要帳號授權（`threads_manage_mentions`）就能讀，
    `keyword_search` 要過 App Review。把兩者混在一起測，會讓「@標註管道
    其實已經可用」被 keyword_search 的失敗蓋掉。
    """
    token = config.get("THREADS_ACCESS_TOKEN")
    print("\n── Threads ──")
    if not token:
        print("  ⬜ 未設定 THREADS_ACCESS_TOKEN")
        print("     設定後即可讀 @標註通報；關鍵字搜尋另需 App Review（2–4 週）")
        return
    print(f"  權杖已載入（長度 {len(token)}）")

    # ① 權杖本身。順帶印出帳號名稱——改過名之後，這是確認改動生效的地方。
    status, data = _threads_get("/me", token, fields="id,username")
    if status != 200:
        print(f"  ❌ 權杖無效或已過期：{_threads_error(status, data)}")
        print("     修法：重新取得使用者存取權杖（短期權杖約 1 小時，"
              "長期權杖約 60 天）")
        return
    print(f"  ✅ threads_basic：@{data.get('username', '(不明)')}")

    # ② @標註收件匣——這是本專案實際使用的端點。
    status, data = _threads_get(
        "/me/mentions", token,
        fields="id,text,username,timestamp,permalink,is_reply", limit=5)
    if status == 200:
        rows = data.get("data") or []
        print(f"  ✅ threads_manage_mentions：可讀，最新一頁 {len(rows)} 則")
        if rows:
            newest = str(rows[0].get("timestamp", ""))[:10]
            print(f"     最新一則 {newest}"
                  f"　@{rows[0].get('username', '')}")
        else:
            # 0 則是正常狀態，不是故障。這個分別要講出來，否則看畫面的人
            # 會以為管道壞了，而實際上只是沒有人標註過這個帳號。
            print("     （目前沒有人 @標註這個帳號——這是狀態，不是故障）")
        _threads_cutoff_note(bool(rows))
    else:
        print(f"  ❌ threads_manage_mentions：{_threads_error(status, data)}")
        print("     修法：在 Meta 應用程式設定裡加入 threads_manage_mentions "
              "權限並重新授權取得新權杖")

    # ③ 關鍵字搜尋——要過 App Review，未核准是預期狀態而非錯誤。
    status, data = _threads_get(
        "/v1.0/keyword_search", token, q="幼兒園", search_type="TOP",
        fields="id,text,permalink,timestamp")
    if status == 200:
        n = len(data.get("data") or [])
        print(f"  ✅ threads_keyword_search：可用，回傳 {n} 筆")
        if n == 0:
            print("     ⚠️ 0 筆可能代表：權限未核准（只搜得到自己的貼文），"
                  "或該關鍵字被判定為敏感而回空陣列")
    else:
        print(f"  ⬜ threads_keyword_search：尚未開通"
              f"（{_threads_error(status, data)}）")
        print("     這不影響 @標註管道；兩者是分開的權限")


def _threads_cutoff_note(has_rows: bool) -> None:
    """沒設時間下限的話，第一次同步會把歷年舊貼文全部當成新通報。

    沿用既有帳號時這一點特別要緊：那個帳號過去被標註的每一則，
    都會以今天的觀測時間進庫。
    """
    limit = config.get("THREADS_MENTIONS_NOT_BEFORE")
    if limit:
        print(f"     時間下限 THREADS_MENTIONS_NOT_BEFORE={limit}")
    elif has_rows:
        print("     ⚠️ 未設 THREADS_MENTIONS_NOT_BEFORE：首次同步會把這個帳號"
              "歷年被標註的貼文全部匯入，每一則都帶今天的觀測時間")


def check_vendor() -> None:
    path = config.get("VENDOR_FEED_PATH")
    print("\n── 輿情監測服務 ──")
    if not path:
        print("  ⬜ 未設定 VENDOR_FEED_PATH")
        return
    p = pathlib.Path(path)
    if not p.exists():
        print(f"  ❌ 檔案不存在：{p}")
        return
    print(f"  ✅ {p}（{p.stat().st_size // 1024} KB）")


def check_bedrock() -> None:
    print("\n── AWS Bedrock ──")
    region = config.get("AWS_REGION")
    has_key = bool(config.get("AWS_ACCESS_KEY_ID"))
    if not region:
        print("  ⬜ 未設定 AWS_REGION")
        return
    print(f"  區域 {region}　金鑰 {'已設定' if has_key else '未設定'}")
    try:
        import anthropic  # noqa: F401
        print("  ✅ anthropic 套件已安裝")
    except ImportError:
        # 這是缺套件，不是缺憑證——但 bedrock.client() 的匯入失敗會被
        # check_bedrock.py 的 except 吞掉，顯示成「7 個模型全部打不通」，
        # 方向完全相反。所以這裡要講清楚下一步是重裝相依而不是換金鑰。
        print("  ❌ 缺 anthropic 套件——三個 AI 落點都連不上（會誤報成憑證問題）")
        print("     修法：python run.py setup"
              "（已釘在 requirements.txt，正常情況不該缺）")


def main() -> None:
    config.load_env()
    print(f"設定檔：{config.ENV_PATH}"
          f"（{'存在' if config.ENV_PATH.exists() else '不存在'}）\n")
    check_google()
    check_threads()
    check_vendor()
    check_bedrock()
    print("\n沒有任何憑證時，新聞與 PTT 兩個管道仍可運作。")


if __name__ == "__main__":
    main()
