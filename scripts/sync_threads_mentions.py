"""把 Threads 上 @標註官方帳號的貼文——以及那些貼文底下的回覆——同步進資料庫。

    PYTHONPATH=src .venv/bin/python scripts/sync_threads_mentions.py
    PYTHONPATH=src .venv/bin/python scripts/sync_threads_mentions.py \
        --fixture tests/fixtures/threads_mentions.json \
        --replies-fixture tests/fixtures/threads_replies.json
    PYTHONPATH=src .venv/bin/python scripts/sync_threads_mentions.py --show
    PYTHONPATH=src .venv/bin/python scripts/sync_threads_mentions.py \
        --thread 17849251066204813
    PYTHONPATH=src .venv/bin/python scripts/sync_threads_mentions.py --classify-only

沒有 `THREADS_ACCESS_TOKEN` 時不是失敗，是「這個管道還沒開」——照實說，
回 exit 0。缺的是授權不是資料，這個分別在畫面上與在退出碼上都要成立。

**`--classify` 與 `--classify-only` 分開，因為它們的成本不同。** `--classify`
只跑本次新進的那幾則（接在同步後面）；`--classify-only` 補跑庫裡所有還沒跑過
的，不碰任何外部端點。每一則都是一次模型呼叫，把整庫重跑當成同步的副作用，
會在某個沒有人按過任何按鈕的下午燒掉一整批額度。沒有 AWS 憑證時照實說
「未分類」並回 exit 0——**未分類不等於沒有問題，也不等於語氣中性。**

**回覆的三種結局要分開講。** 讀不到（端點拒絕）、沒有人回覆、連不上，是三件
不同的事，混成一句「0 則回覆」就會讓操作的人以為功能正常而公眾沉默。這與
`realtime/jobs.py` 把結局拆成六種是同一個理由，也是 `scrape/threads.py` 為此
定義 `ThreadsPermissionError` 的理由。

**live 與離線在「抓哪幾串」上刻意不同。** live 只對本次**新存進去**的主貼文各
抓一次回覆：每次同步都對歷史上每一串重打一輪 API，成本與速率限制都不划算。
`--replies-fixture` 沒有 API 成本，那份檔案就是已經抓回來的答案，所以裡面提到
的每一串都會套用——否則離線 demo 第二次跑就只剩空串。要補抓某一串用 `--thread`。

`--fixture` 走離線路徑：讀一份存下來的 API 回應，其餘流程完全相同。決賽現場
的主線是這一條，live 取用當 bonus——會場網路與權杖效期是唯二不能在事前驗證
的東西。

寫進 `threads_mention` 表的東西**不進分數、不進 payload、不進任何 CSV**。
要進卷宗必須人工逐則採用，與 `realtime/jobs.py` 的不變式一致。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.db import session as db_session
from smart_watchdog.realtime import demo_data, mention_store
from smart_watchdog.scrape import threads

INSTITUTIONS = pathlib.Path("data/processed/institutions_ntpc.csv")

#: 讀不到回覆時要給的修法。印在失敗當下，不要讓人自己去翻文件。
PERMISSION_HELP = (
    "   修法：權杖需要 threads_read_replies 權限（Meta 開發者後台加，需重新授權）。\n"
    "   另一種可能不是權限：Conversations 端點 2026-09-12 實測讀得到別人發的串，\n"
    "   但那是一次實測不是保證——貼文可見性、對方帳號設定、權杖效期都會改變它。\n"
    "   /replies 端點已自動再試一次，兩個都被拒才會印出這一段：\n"
    "   也就是說這是**讀不到**，不是這一串沒有人回覆。"
)


def _real_institutions() -> list[dict]:
    """主檔裡的真實機構。讀不到就是空的，不在這裡印任何話——`--show` 也會叫它，
    而那條路上根本沒有在歸屬，印「本次不做歸屬」會是一句不成立的話。"""
    if not INSTITUTIONS.exists():
        return []
    frame = pd.read_csv(INSTITUTIONS)
    return frame[["id", "title", "town"]].to_dict("records")


def _names() -> dict[str, str]:
    """id → 園名，真實與示範都在裡面。只給顯示用，不做歸屬。"""
    return {i["id"]: i["title"] for i in demo_data.merged(_real_institutions())}


def _institutions() -> list[dict]:
    """歸屬用的機構清單：真實機構 + 示範機構。

    真實那半與 run_realtime_sweep.py 餵給其他管道的是同一份。示範那半來自
    `realtime/demo_data.py`，**只在這條社群／歸屬路徑上合併**：示範貼文指名的
    是示範園，沒有它們就會整批躺進「待人工認園」，而那份佇列是給人看工作量的。
    合併寫在 `demo_data.merged()` 裡，API 那邊用的是同一支函式。
    """
    real = _real_institutions()
    if not real:
        print(f"找不到 {INSTITUTIONS}，真實機構本次不做歸屬"
              "（通報照樣入庫，institution_id 留空）；示範機構仍會歸屬")
    return demo_data.merged(real)


def _fixture_replies(path: str) -> dict:
    """把一份串端點的回應依 root 分組。

    live 路徑不需要這一步——`replies_of()` 是指名對某一串問的。離線檔沒有那個
    問題可問，而 `root_post` 實測**不保證回傳**，所以缺的那些要沿 `replied_to`
    往上接：規則與理由在 `threads.group_by_root()`。
    """
    grouped = threads.group_by_root(threads.load_fixture(path))
    orphans = len(grouped.pop("", []))
    if orphans:
        print(f"⚠ 離線回覆檔有 {orphans} 則接不回任何一串"
              "（既沒有 root_post 也沒有 replied_to），略過不寫")
    return grouped


def _sync_replies(db, roots, fetch_replies, institutions) -> list:
    """對每一串抓一次回覆並入庫，結局分開報。回傳本次真的寫進去的 threads_id。

    `fetch_replies(root)` 要嘛回一串貼文，要嘛丟 `ThreadsError`——空陣列在這裡
    **只代表**「這串沒有人回覆」，所以取用層不准用它來表示讀不到。

    回傳 id 而不只是計數，理由與 `mention_store.record()` 的 `inserted_ids`
    一樣：`--classify` 要對**本次新進的那幾則**跑分類，只給計數的話它只能用
    「最近 N 則」去猜是哪幾則，猜錯就是重跑舊的、漏跑新的。
    """
    inserted_ids: list = []
    inserted = duplicate = empty = 0
    own = inherited = unattributed = 0
    others: list[dict] = []
    blocked: list[tuple[str, Exception]] = []
    failed: list[tuple[str, Exception]] = []
    orphaned: list[tuple[str, str]] = []

    for root in roots:
        try:
            replies = fetch_replies(root)
        except threads.ThreadsPermissionError as exc:
            blocked.append((root, exc))
            continue
        except threads.ThreadsError as exc:
            failed.append((root, exc))
            continue
        if not replies:
            empty += 1
            continue
        counts = mention_store.record_replies(db, root, replies, institutions)
        inserted_ids.extend(counts["inserted_ids"])
        inserted += counts["inserted"]
        duplicate += counts["duplicate"]
        own += counts["attributed_own"]
        inherited += counts["attributed_inherited"]
        unattributed += counts["unattributed"]
        others.extend(counts["named_others"])
        if counts["skipped"]:
            orphaned.append((root, counts["reason"]))

    # 「沒有人回覆」只在真的查得到且真的是空的時候講。下面那兩段（讀不到、
    # 連不上）都不是這件事，混在同一句裡就等於把故障講成公眾的沉默。
    print(f"回覆：新增 {inserted} 則、重複略過 {duplicate} 則"
          + (f"、{empty} 串目前沒有回覆（查得到，就是沒有人回）" if empty else ""))
    if inserted:
        # 三類分開印。全部加起來報成「歸屬 N 則」會讓 N 則「+1」看起來像 N 次
        # 指名，而那正是 06-plan §6 不准的重複加權。
        print(f"   歸屬來源：自身指名 {own} 則、繼承主貼文 {inherited} 則、"
              f"無法歸屬 {unattributed} 則")
    if others:
        print(f"⚠ 有 {len(others)} 則回覆指名了主貼文以外的機構——"
              "一串討論裡冒出第二家，是新線索不是雜訊：")
        for item in others:
            print(f"   {item['threads_id']} → {item['title']}"
                  f"（主貼文：{item['root_title']}）")
    if blocked:
        print(f"⚠ {len(blocked)} 串讀不到回覆——端點拒絕，不是沒有人回覆：")
        for root, exc in blocked:
            print(f"   {root}：{str(exc)[:140]}")
        print(PERMISSION_HELP)
    if failed:
        print(f"⚠ {len(failed)} 串抓取失敗（連線或 API 錯誤，與權限無關，重跑即可）：")
        for root, exc in failed:
            print(f"   {root}：{str(exc)[:140]}")
    if orphaned:
        print(f"{len(orphaned)} 串的主貼文不在庫裡，回覆未入庫（不寫孤兒列）：")
        for _, reason in orphaned:
            print(f"   {reason}")
    return inserted_ids


def _classify(db, threads_ids=None) -> None:
    """對還沒分類過的貼文跑一次分類。沒有憑證時照實說「未分類」，不是失敗。

    `threads_ids=None` 代表「庫裡所有還沒跑過的」（`--classify-only`）；
    給一份 id 時只跑那幾則（`--classify` 接在同步後面）。

    **未分類與中性不是同一件事**，所以這裡印的是「N 則仍未分類」而不是把它們
    算進任何一種語氣。分類結果不進分數、不進 payload、不進任何 CSV，與
    `threads_mention` 這張表的其餘欄位一樣。
    """
    from smart_watchdog.realtime import classify

    rows = classify.pending(db, threads_ids=threads_ids)
    if not rows:
        print("分類：沒有待分類的貼文。")
        return

    backend = classify.get_backend("auto")
    counts = classify.classify_rows(db, rows, backend)
    if counts["reason"]:
        # 符號一律用 Big5 有的那幾個：Windows 控制台預設 cp950，
        # 印不出來的字不是少一個圖標，是整支腳本倒在 print 上。
        print(f"※ 分類：{len(rows)} 則仍未分類——{counts['reason']}")
        print("   要跑分類請設定 AWS 憑證（.env 的四個 AWS_... 值），再重跑 "
              "--classify-only。")
        return

    tones = "、".join(f"{classify.TONE_LABELS.get(k, k)} {v}"
                     for k, v in sorted(counts["by_tone"].items()))
    cats = "、".join(f"{k} {v}" for k, v in sorted(counts["by_category"].items()))
    print(f"分類（{counts['backend']}）：{counts['classified']} 則完成"
          + (f"、{counts['failed']} 則呼叫失敗（維持未分類）" if counts["failed"] else ""))
    if tones:
        print(f"   語氣：{tones}")
    if cats:
        print(f"   事件類別：{cats}")
    # issues 只印在這裡，不入庫也不上畫面——它是模型寫的自由文字，內容受外部
    # 貼文影響。理由見 classify.Classification。
    for item in counts["issues"][:10]:
        print(f"   ※ {item['threads_id']}：{item['note']}")
    for item in counts["errors"][:5]:
        print(f"   × {item['threads_id']}：{str(item['error'])[:120]}")


def _who(names: dict[str, str], institution_id: str | None) -> str:
    """一列印出來的園名。示範機構帶前綴，不靠名字看起來假不假。"""
    if not institution_id:
        return "（未歸屬）"
    name = names.get(institution_id, institution_id)
    return f"［示範］{name}" if demo_data.is_demo(institution_id) else name


def _show(db) -> int:
    summary = mention_store.stats(db)
    print(f"共 {summary['total']} 列："
          f"{summary['roots']} 則主貼文（有人 @我們）、"
          f"{summary['replies']} 則串下回覆；"
          f"{summary['attributed']} 列已歸屬、"
          f"{summary['unattributed']} 列待人工認園")
    if summary["latest_posted_at"]:
        print(f"最新一則發文時間：{summary['latest_posted_at']}")
    # 印園名不印 id：庫裡存的是 36 字元的 UUID，直接印出來既排不齊也沒人讀得懂，
    # 而「這則是哪一園的」正是看這份清單的唯一理由。認不出來的照樣要列出來。
    # 示範機構前面加標記：這份清單會被貼進報告與截圖，而截圖裡沒有人可以問
    # 「這一家是真的嗎」。
    names = _names()
    # 主貼文與它底下的回覆一起印。回覆單獨列成一行沒有意義——回覆的人是在跟
    # 原 PO 講話，脫離那一串就只剩「+1」。
    for row in mention_store.recent(db, limit=10, kind=mention_store.MENTION):
        who = _who(names, row["institution_id"])
        head = row["text"].replace("\n", " ")[:40]
        print(f"  {row['posted_at'][:10]}  {who:<18}  @{row['username']:<18}  {head}")
        for reply in mention_store.thread(db, row["threads_id"])[1:]:
            body = reply["text"].replace("\n", " ")[:30] or "（無文字，貼圖或圖片）"
            # 縮排代表「回的是上面那一串」，不代表樹的層數——巢狀關係在
            # reply_to_threads_id 裡，這裡只排時間序。
            # 指名了別家的那一則要標出來：它縮排在這一串底下，講的卻是另一家，
            # 而縮排本身會讓人讀成「這則也是在講上面那一園」。
            mark = ""
            if reply["institution_id"] != row["institution_id"]:
                mark = f"   ← 自身指名 {_who(names, reply['institution_id'])}"
            print(f"       └ {reply['posted_at'][:10]}  "
                  f"@{reply['username']:<18}  {body}{mark}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", help="改讀一份存下來的 API 回應，不連網")
    ap.add_argument("--replies-fixture",
                    help="離線的串回應（/{id}/conversation 存下來的那種）")
    ap.add_argument("--show", action="store_true", help="只列出庫裡已有的，不同步")
    ap.add_argument("--include-reply-mentions", "--replies", action="store_true",
                    dest="reply_mentions",
                    help="連「本身是別人串裡的回覆、但有 @我們」的那些一起收"
                         "（與 --no-replies 無關：那個管的是我方主貼文底下的回覆）")
    ap.add_argument("--no-replies", action="store_true",
                    help="不去抓主貼文底下的回覆，只同步 @標註本身")
    ap.add_argument("--thread", help="只抓這一串的回覆（給測試與補抓用）")
    ap.add_argument("--classify", action="store_true",
                    help="同步後對本次新進的貼文跑一次分類（需 AWS 憑證）")
    ap.add_argument("--classify-only", action="store_true", dest="classify_only",
                    help="不同步，只對庫裡還沒分類過的貼文補跑分類")
    args = ap.parse_args()

    db_session.init_db()
    db = next(db_session.get_db())
    try:
        if args.show:
            return _show(db)
        # --classify-only 不碰任何外部端點，所以它連機構清單都不需要讀。
        if args.classify_only:
            _classify(db)
            return 0

        institutions = _institutions()
        fixture_replies = (_fixture_replies(args.replies_fixture)
                           if args.replies_fixture else None)

        def fetch_replies(root: str) -> list:
            if fixture_replies is not None:
                return fixture_replies.get(root, [])
            return threads.replies_of(root)

        # --thread：只補抓指定的那一串，不碰 /me/mentions。
        if args.thread:
            fresh = _sync_replies(db, [args.thread], fetch_replies, institutions)
            if args.classify:
                _classify(db, fresh)
            return 0

        if args.fixture:
            posts = threads.load_fixture(args.fixture)
            print(f"離線來源 {args.fixture}：{len(posts)} 則")
        elif not threads.token():
            print("THREADS_ACCESS_TOKEN 未設定——Threads @標註管道尚未開啟。")
            print("這是缺授權，不是沒有人通報。填入 .env 後重跑；"
                  "或用 --fixture 走離線路徑。")
            return 0
        else:
            limit = threads.cutoff()
            if limit is None:
                print("⚠ 未設 THREADS_MENTIONS_NOT_BEFORE："
                      "本次會匯入帳號歷年所有 @標註，每一則都會帶今天的觀測時間。")
            posts = threads.mentions()
            print(f"Threads /me/mentions：{len(posts)} 則"
                  + (f"（{limit.date()} 之後）" if limit else ""))

        counts = mention_store.record(
            db, posts, institutions, replies=args.reply_mentions)
        print(f"新增 {counts['inserted']} 則"
              f"（其中 {counts['attributed']} 則歸屬成功）、"
              f"重複略過 {counts['duplicate']} 則、"
              f"非主貼文略過 {counts['skipped_reply']} 則")
        if counts["inserted"] and counts["inserted"] > counts["attributed"]:
            print(f"有 {counts['inserted'] - counts['attributed']} 則認不出是哪一園，"
                  "已入庫待人工判讀——不是雜訊，是工作量。")

        fresh = list(counts["inserted_ids"])
        if not args.no_replies:
            # live 只追本次新存進去的那幾串；離線那份檔案已經抓回來了，全套用。
            roots = (sorted(fixture_replies) if fixture_replies is not None
                     else counts["inserted_ids"])
            if roots:
                fresh += _sync_replies(db, roots, fetch_replies, institutions)
            else:
                print("沒有新的主貼文，本次不抓回覆（要補抓用 --thread <id>）")

        # 分類只跑本次新進的那幾則。舊列用 --classify-only 補，兩者分開是因為
        # 每一則都是一次模型呼叫——把整庫重跑一遍當成同步的副作用，會在某個
        # 沒有人按過任何按鈕的下午燒掉一整批額度。
        if args.classify:
            _classify(db, fresh)
        return 0
    except threads.ThreadsError as exc:
        print(f"Threads API 失敗：{exc}", file=sys.stderr)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
