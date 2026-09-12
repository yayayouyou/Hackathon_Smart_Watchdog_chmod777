"""把 Threads 通報寫進資料庫，並在寫入時做一次歸屬。

三個不變式，每一個都對應專案既有的規則：

**追加式，不 UPDATE、不 DELETE。** 與 `AuditFeedback` 同樣的處置。`threads_id`
的唯一索引就是去重機制——同一則重抓一百次只會有一列，第二次之後全部算
`duplicate`。上游改文不覆蓋既有列：`content_hash` 存的是我方當時看到的版本，
覆蓋掉就再也證明不了那件事。

**歸屬在寫入時算一次，拒配照樣入庫。** `features.alerts.attribute()` 的預設是
拒絕，它的模組說明記了四個真實誤配（「太平洋新聞網」配到太平洋幼兒園、
「林口某雙語補習班」配到林口幼兒園）。拒配的列留著並記下理由，因為
「有人通報了但我們認不出是哪一園」本身就是要給人看的東西——把它丟掉，
畫面上就會變成從來沒有人通報過。

**寫進來的東西不進分數、不進 payload、不進任何 CSV。** 與 `realtime/jobs.py`
的不變式一致，而且同樣是架構上的：這支模組只 import `db`，沒有任何一條路徑
通往 `risk/priority.py` 或 `data/processed/`。

**串下的回覆先問它自己指名了誰，指名不出來才繼承主貼文。** 回覆多半只寫
「我也遇過」「+1」，那種沒有主體，主體只能是它所在的那一串；但寫了
「吉尼爾幼兒園也這樣」的那一則，主體就是吉尼爾——串是討論的容器，不是主體的
容器。哪一種算出來的，記在 `attribution_source`，不是記在文案裡。

歸屬依「指名意圖由強到弱」逐個試：**hashtag → 第一行 → 全文**，理由見
`attribution_candidates()`。第一版只吃第一行，於是一則標了 `#文德幼兒園` 的
貼文因為第一行是「原來平衡感不是天生的」而拒配——園名在第 7 行的 hashtag 裡。
"""

from __future__ import annotations

import re

from sqlalchemy import func, or_, select

from ..db.models import ThreadsMention

#: 只有這些前綴的行會被跳過，去找下一行當標題。@標註與純 hashtag 行不帶資訊。
_SKIPPABLE = ("@", "#")

#: `ThreadsMention.kind` 的兩個值。mention = 有人把官方帳號標註進來，
#: reply = 那串底下的回覆（回覆的人多半是在跟原 PO 講話，不是在跟機關講話）。
MENTION, REPLY = "mention", "reply"

#: `ThreadsMention.attribution_source` 的三個值。own 與 inherited 必須分得開：
#: 一串十則「+1」繼承下來，在 institution_id 上看起來像十次指名，實際是一次。
OWN, INHERITED, NONE = "own", "inherited", "none"


def _name(names: dict, institution_id: str | None) -> str:
    """給人看的園名，沒有清單時退回 id——寧可印得醜，不要印得空。"""
    return names.get(institution_id) or str(institution_id or "")


def _headline(text: str) -> str:
    """取貼文的第一行有意義文字，當作歸屬用的「標題」。

    `alerts.attribute()` 是為**新聞標題**調校的：它會先剝掉結尾的發布者後綴
    （`「...」- 聯合影音` 那種），並要求園名緊貼在「幼兒園」前面。把一整篇
    五百字的貼文餵進去，這兩條規則的行為就不是任何人量測過的那個行為了——
    結尾剝除會咬掉最後一行（例如署名「— 一位家長」），而全文裡出現的每一個
    地名都多一次誤配機會。

    第一行也剛好是結構化通報格式（`#教保通報` + 園名一行）該放園名的地方，
    所以之後導入格式時這裡不用改。
    """
    for line in str(text).splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(_SKIPPABLE) and len(line.split()) <= 1:
            continue
        return line
    return str(text).strip().split("\n")[0] if text else ""


#: 抓 hashtag 的本體。允許中英數與底線，遇到空白或標點就停。
_HASHTAG = re.compile(r"#([0-9A-Za-z_一-鿿]{2,40})")


def _hashtags(text: str) -> str:
    """把貼文裡的 hashtag 串成一段，給歸屬優先比對。

    `#文德幼兒園` 是發文者**刻意**指名的機構——比內文任何一句話都明確。
    第一版只看第一行，於是一則標了 `#文德幼兒園` 的貼文因為第一行是
    「原來平衡感不是天生的」而拒配；園名在第 7 行的 hashtag 裡。

    串成一段而不是逐個比對，是為了讓 `attribute()` 的唯一性規則仍然作用：
    同時標了兩所園的貼文應該拒配，逐個比對會變成「第一個標到的贏」。
    """
    tags = _HASHTAG.findall(str(text))
    return " ".join(tags)


def attribution_candidates(text: str) -> list[str]:
    """依「指名意圖由強到弱」排出要拿去比對的候選字串。

    1. **hashtag**——發文者明確標記的機構
    2. **第一行**——最接近 `attribute()` 調校時的那種標題
    3. **全文**——最後才用；內文裡順帶提到的園名也算指名，這與回覆的處理
       一致（`record_replies()` 的說明：串是討論的容器，不是主體的容器）

    逐個試、取第一個配得上的。**不放寬 `attribute()` 的任何規則**——
    跨縣市否決、唯一候選、名稱要緊貼「幼兒園」前，每個候選都照樣要過。
    這裡改的只是「拿哪段文字去問」，不是「問得寬鬆一點」。
    """
    out = []
    tags = _hashtags(text)
    if tags:
        out.append(tags)
    head = _headline(text)
    if head and head not in out:
        out.append(head)
    body = " ".join(str(text).split())
    if body and body not in out:
        out.append(body)
    return out


def attribute_post(post, institutions: list[dict]):
    """跑一次歸屬。回傳 `alerts.Attribution`，拒配時 `institution_id` 是 None。

    `institutions` 的每一筆要有 `id`／`title`／`town`，與 `run_realtime_sweep.py`
    餵給其他管道的是同一份清單。

    依 `attribution_candidates()` 的順序逐個試，取第一個配得上的；全部拒配時
    回傳**第一個候選**的拒配理由，因為那是意圖最強的那一段——回「hashtag 裡
    沒有可辨識的機構名」比回「全文第 37 個字不對」有用得多。
    """
    from ..features.alerts import attribute

    first = None
    for candidate in attribution_candidates(post.text):
        result = attribute(candidate, institutions)
        if result.attributed:
            return result
        first = first or result
    return first or attribute("", institutions)


def record(db, posts, institutions: list[dict] | None = None,
           *, replies: bool = False) -> dict:
    """把一批貼文寫進 `threads_mention`。回傳各類計數。

    `replies=False`（預設）時跳過回覆：一則順帶提到我們的回覆是別人對話裡的
    片段，不是寄給我們的通報。要全收就傳 `replies=True`，計數會分開報。

    這裡進來的每一則都 `kind="mention"`——它們全部來自 `/me/mentions`，也就是
    有人把官方帳號打進去了。串下的回覆走 `record_replies()`，那邊才是 `reply`。

    計數裡多一個 `inserted_ids`：**本次真的新寫進去的** threads_id。呼叫端要
    接著抓這幾串的回覆，而只給計數的話它只能用「最近 N 則」去猜是哪幾則，
    猜錯就是對一串已經抓過的重抓、對新的那串漏抓。
    """
    institutions = institutions or []
    counts: dict = {"inserted": 0, "duplicate": 0, "skipped_reply": 0,
                    "attributed": 0, "inserted_ids": []}

    for post in posts:
        if not replies and not post.addressed_to_us:
            counts["skipped_reply"] += 1
            continue

        exists = db.execute(
            select(ThreadsMention.id).where(ThreadsMention.threads_id == post.threads_id)
        ).first()
        if exists:
            counts["duplicate"] += 1
            continue

        att = attribute_post(post, institutions) if institutions else None
        db.add(ThreadsMention(
            threads_id=post.threads_id,
            username=post.username,
            text=post.text,
            permalink=post.permalink,
            posted_at=post.posted_at,
            is_reply=post.is_reply,
            content_hash=post.content_hash,
            institution_id=att.institution_id if att else None,
            attribution_basis=(att.basis if att else "未提供機構清單，未執行歸屬"),
            # 主貼文沒有可繼承的對象，所以只會是「自己指名」或「沒有」。
            attribution_source=(OWN if (att and att.attributed) else NONE),
            # 主貼文自己就是那一串的 root，所以整串一次查得到。`/me/mentions`
            # 不回 root_post，真有值時（例如改由串端點餵進來）以平台說的為準。
            root_threads_id=post.root_threads_id or post.threads_id,
            reply_to_threads_id=post.reply_to_threads_id or None,
            kind=MENTION,
            raw=post.raw,
        ))
        counts["inserted"] += 1
        counts["inserted_ids"].append(post.threads_id)
        if att and att.attributed:
            counts["attributed"] += 1

    db.commit()
    return counts


def record_replies(db, root_threads_id: str, replies,
                   institutions: list[dict] | None = None) -> dict:
    """把一串底下的回覆寫進同一張表。先問它自己指名了誰，問不出來才繼承。

    三種情形，`attribution_source` 分別記成 `own`／`inherited`／`none`：

    * **回覆自己指名了機構**（「吉尼爾幼兒園也這樣」）→ 算它指名的那一家，
      即使那不是主貼文那一家。串是討論的容器，不是主體的容器：在文德那串底下
      講吉尼爾的那一則，講的就是吉尼爾。一串討論裡冒出第二個機構是新線索，
      把它壓成主貼文的那一家，就是把線索改寫成附和。
    * **回覆沒有指名**（「我也遇過」「+1」）→ 繼承主貼文的。這種句子沒有主體，
      獨立跑歸屬只會整串拒配，一串十則全部躺進「待人工認園」——那份清單是給人
      看工作量的，灌進十則「+1」就不再是工作量了。
    * **兩者皆無**（主貼文也拒配）→ `none`，`institution_id` 留 NULL。誠實的
      傳遞，不是失敗。

    `own` 與 `inherited` 為什麼要分得開：見 `ThreadsMention` 的類別說明。
    簡短說，十則繼承而來的「+1」不等於十次指名，而下游只看 `institution_id`
    的話會把它們數成十次。

    非教保機構不會被比對到，那是對的：「XX補習班也這樣」拒配 → 走繼承。
    短期補習班不是教保服務機構、不在 `institutions_ntpc.csv` 裡，也不該為了讓
    它配得上而去動 `alerts.py` 的比對規則。

    `institutions` 是歸屬用的機構清單，順便把 id 換成園名寫進 basis。給 root
    不在庫裡的一批回覆時**不寫孤兒列**，整批算 `skipped`：一則掛不到主貼文的
    回覆，在畫面上是一句沒有上下文的抱怨。
    """
    root = str(root_threads_id or "").strip()
    institutions = institutions or []
    names = {str(i.get("id")): str(i.get("title") or "") for i in institutions}
    counts: dict = {"inserted": 0, "duplicate": 0, "skipped": 0,
                    "attributed_own": 0, "attributed_inherited": 0,
                    "unattributed": 0, "inserted_ids": [], "named_others": [],
                    "reason": ""}

    row = db.execute(
        select(ThreadsMention).where(ThreadsMention.threads_id == root)
    ).scalars().first()
    if row is None:
        counts["skipped"] = len(list(replies))
        counts["reason"] = f"主貼文 {root} 不在庫裡，回覆不單獨入庫"
        return counts

    root_id = row.institution_id
    for post in replies:
        # 端點會把主貼文本身回進來；它已經以 mention 的身分在庫裡了。
        if post.threads_id == root:
            counts["duplicate"] += 1
            continue
        exists = db.execute(
            select(ThreadsMention.id).where(ThreadsMention.threads_id == post.threads_id)
        ).first()
        if exists:
            counts["duplicate"] += 1
            continue

        att = attribute_post(post, institutions) if institutions else None
        if att and att.attributed:
            institution_id, source = att.institution_id, OWN
            basis = f"回覆自身指名 {_name(names, institution_id)}"
            if root_id and institution_id != root_id:
                # 主貼文那一家也寫進去：複查的人要知道這則是在誰的串裡講的。
                basis += f"；主貼文為 {_name(names, root_id)}"
                counts["named_others"].append({
                    "threads_id": post.threads_id,
                    "institution_id": institution_id,
                    "title": _name(names, institution_id),
                    "root_title": _name(names, root_id),
                })
            elif not root_id:
                basis += "；主貼文未歸屬"
            counts["attributed_own"] += 1
        elif root_id:
            institution_id, source = root_id, INHERITED
            basis = (f"繼承自主貼文 {root} 的歸屬：{_name(names, root_id)}"
                     "（回覆自身未指名機構）")
            counts["attributed_inherited"] += 1
        else:
            institution_id, source = None, NONE
            basis = f"回覆未指名機構，主貼文 {root} 也未歸屬"
            counts["unattributed"] += 1

        db.add(ThreadsMention(
            threads_id=post.threads_id,
            username=post.username,
            text=post.text,
            permalink=post.permalink,
            posted_at=post.posted_at,
            is_reply=True,
            content_hash=post.content_hash,
            institution_id=institution_id,
            attribution_basis=basis,
            attribution_source=source,
            root_threads_id=root,
            # 平台沒說回的是誰就留 NULL。用「大概是回主貼文」補起來，樹就長成
            # 我方猜的形狀而不是它本來的形狀。
            reply_to_threads_id=post.reply_to_threads_id or None,
            kind=REPLY,
            raw=post.raw,
        ))
        counts["inserted"] += 1
        counts["inserted_ids"].append(post.threads_id)

    db.commit()
    return counts


def _as_dict(row) -> dict:
    return {
        "threads_id": row.threads_id,
        "username": row.username,
        "text": row.text,
        "permalink": row.permalink,
        "posted_at": row.posted_at,
        "content_hash": row.content_hash,
        "institution_id": row.institution_id,
        "attribution_basis": row.attribution_basis,
        "attribution_source": row.attribution_source,
        "kind": row.kind,
        "root_threads_id": row.root_threads_id,
        "reply_to_threads_id": row.reply_to_threads_id,
        "is_reply": row.is_reply,
        "observed_at": row.observed_at.isoformat() if row.observed_at else None,
        # 分類結果（`realtime/classify.py`）。六欄一起帶，因為呼叫端要判斷的
        # 是「跑過沒有」而不是「tone 有沒有值」——`classified_at` 是那個判準，
        # 少帶它的話，`tone` 為 NULL 的舊列在畫面上會被讀成中性。
        "event_category": row.event_category,
        "tone": row.tone,
        "specificity": row.specificity,
        "stance": row.stance,
        "contains_minor_identifiers": row.contains_minor_identifiers,
        "classified_at": (row.classified_at.isoformat()
                          if row.classified_at else None),
    }


def thread(db, root_threads_id: str) -> list[dict]:
    """一整串：主貼文在最前面，回覆依發文時間排。

    `root_threads_id` 在主貼文上填的是它自己，所以一串是一次查詢而不是兩次。
    回覆之間**不重排成樹**：巢狀關係在 `reply_to_threads_id` 裡，要畫成樹的
    人自己接，這裡只保證時間序——那是唯一不必猜的順序。
    """
    root = str(root_threads_id or "").strip()
    rows = db.execute(
        select(ThreadsMention).where(or_(
            ThreadsMention.root_threads_id == root,
            ThreadsMention.threads_id == root,
        ))
    ).scalars().all()
    return [_as_dict(r) for r in
            sorted(rows, key=lambda r: (r.threads_id != root, r.posted_at or ""))]


def recent(db, *, limit: int = 50, institution_id: str | None = None,
           kind: str | None = None, unattributed: bool = False) -> list[dict]:
    """最近的通報，新的在前。`institution_id` 給定時只回那一園的。

    `kind` 給定時只回那一類（`"mention"` 主貼文／`"reply"` 串下回覆）。預設
    兩類都回：一份「最近有什麼」的清單把回覆藏起來，就會漏掉一串在主貼文之後
    才長出來的十則附和。

    `unattributed=True` 只回歸屬拒配的那些（`institution_id IS NULL`），
    也就是待人工認園的佇列。**這個條件必須在 SQL 裡**，不能讓呼叫端取回
    最近 N 列再自己過濾：LIMIT 先砍、過濾後砍，佇列會無聲地少掉一截，而少掉的
    那幾則正是這份清單存在的理由。`stats()` 已經在報這個數字，條件就該與它
    在同一層。

    注意回傳的是 dict 而不是 ORM 物件：呼叫端（API、CLI）不該持有 session 綁定
    的物件，那是 `db.close()` 之後才會炸的那種問題。
    """
    stmt = select(ThreadsMention).order_by(ThreadsMention.posted_at.desc()).limit(limit)
    if institution_id:
        stmt = stmt.where(ThreadsMention.institution_id == institution_id)
    if kind:
        stmt = stmt.where(ThreadsMention.kind == kind)
    if unattributed:
        stmt = stmt.where(ThreadsMention.institution_id.is_(None))
    return [_as_dict(row) for row in db.execute(stmt).scalars()]


def stats(db) -> dict:
    """給 console 與 /api/health 用的一行摘要。

    `unattributed` 單獨報，不與 `total` 合併：那是待人工認園的工作量，
    不是雜訊。

    `roots` 與 `replies` 同樣分開報。`total` 是列數，不是「有多少人向教育局
    反映」——一串二十則附和只有一個人標註了官方帳號。要講後面那個數字時該看的
    是 `roots`。
    """
    total = db.execute(select(func.count(ThreadsMention.id))).scalar_one()
    attributed = db.execute(
        select(func.count(ThreadsMention.id))
        .where(ThreadsMention.institution_id.is_not(None))
    ).scalar_one()
    latest = db.execute(
        select(func.max(ThreadsMention.posted_at))
    ).scalar_one()
    roots = db.execute(
        select(func.count(ThreadsMention.id)).where(ThreadsMention.kind == MENTION)
    ).scalar_one()
    replies = db.execute(
        select(func.count(ThreadsMention.id)).where(ThreadsMention.kind == REPLY)
    ).scalar_one()
    return {
        "total": total,
        "attributed": attributed,
        "unattributed": total - attributed,
        "roots": roots,
        "replies": replies,
        "latest_posted_at": latest,
    }
