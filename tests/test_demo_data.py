"""示範機構：畫面上標得出來，真實資料集裡進不去。

這一支釘的是兩個方向的洩漏，兩個都會在無聲無息中發生：

**往外洩漏。** 六筆造出來的園混進 `data/processed/`、`dist/data/payload.json`
或稽查優先序，那份 AUC 0.641／P@100 2.17x 就不再是量測過的那個數字，而且沒有
任何畫面會告訴你多了六筆。所以這裡直接讀那些產物的檔案，確認裡面連
`demo0001` 這幾個字都找不到。

**往內洩漏。** 示範貼文指名了真實機構，就是替一家真實機構捏造投訴——那正是
這批資料被換掉的原因。所以這裡把兩份 fixture 的每一則貼文拿去與**真實主檔**
比對：一則都不准歸屬到真園，全文裡也不准出現任何一所真園的辨識名。

第三件事是標示：`is_demo` 必須靠 id 判斷得出來，不是靠名字看起來假不假。
名字是給人看的，旗標是給程式檢查的。
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from smart_watchdog.features.alerts import attribute, distinctive_name
from smart_watchdog.realtime import demo_data, mention_store
from smart_watchdog.scrape import threads

ROOT = pathlib.Path(__file__).resolve().parents[1]
REAL_CSV = ROOT / "data/processed/institutions_ntpc.csv"
PAYLOAD = ROOT / "dist/data/payload.json"
AUDIT_PRIORITY = ROOT / "data/processed/audit_priority_ntpc.csv"
FIXTURES = [ROOT / "tests/fixtures/threads_mentions.json",
            ROOT / "tests/fixtures/threads_replies.json"]

#: 只有這兩個地方該把示範機構合併進來——社群通報的歸屬與社群面板的顯示。
#: 其餘任何一支模組 import 了 `demo_data`，就是開了一條通往別處的路。
#: 只有「歸屬與社群顯示」這條路可以看到示範機構。清單是白名單而不是黑名單：
#: 新增一個匯入者要在這裡寫一行，那一行就是「我確認這條路不通往分數與 payload」
#: 的簽名。背景輪詢（mention_poller）與 CLI 做的是同一件事——收通報並歸屬——
#: 所以它也在名單上。
ALLOWED_IMPORTERS = {"src/smart_watchdog/api/social.py",
                     "src/smart_watchdog/realtime/mention_poller.py",
                     "scripts/sync_threads_mentions.py"}


def _real() -> list[dict]:
    """真實機構清單。缺檔就跳過那條測試，不假裝通過。"""
    import csv

    if not REAL_CSV.exists():
        pytest.skip(f"缺少 {REAL_CSV}")
    with REAL_CSV.open(encoding="utf-8") as fh:
        return [{"id": r["id"], "title": r.get("title", ""),
                 "town": r.get("town", "")} for r in csv.DictReader(fh)]


def _fixture_texts() -> list[tuple[str, str]]:
    out = []
    for path in FIXTURES:
        out.extend((str(row.get("id", "（無 id）")), str(row.get("text", "")))
                   for row in json.loads(path.read_text(encoding="utf-8"))["data"])
    return out


# ── 往外：示範機構不得進入任何真實產物 ──────────────────────────────


def test_the_demo_institutions_are_not_in_the_real_master_file():
    """`institutions_ntpc.csv` 同時餵 baseline_model 與 build_audit_priority。"""
    text = REAL_CSV.read_text(encoding="utf-8") if REAL_CSV.exists() else ""
    for inst in demo_data.institutions():
        assert inst["id"] not in text
        assert inst["title"] not in text


@pytest.mark.parametrize("path", [PAYLOAD, AUDIT_PRIORITY])
def test_no_demo_row_reached_the_scored_products(path):
    """payload 與稽查優先序是量測過的東西，多六筆造出來的園就不再是它。"""
    if not path.exists():
        pytest.skip(f"尚未產生 {path}")
    text = path.read_text(encoding="utf-8")
    assert demo_data.ID_PREFIX + "0001" not in text
    for inst in demo_data.institutions():
        assert inst["id"] not in text
        assert inst["title"] not in text


def test_demo_data_is_only_imported_on_the_social_and_attribution_path():
    """架構上的保證，不是靠人記得。多一支 import 就等於多一條進得去的路。"""
    importers = set()
    for folder in ("src", "scripts", "run.py"):
        base = ROOT / folder
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in files:
            if "demo_data" in path.read_text(encoding="utf-8"):
                importers.add(path.relative_to(ROOT).as_posix())
    importers.discard("src/smart_watchdog/realtime/demo_data.py")
    assert importers == ALLOWED_IMPORTERS


# ── 往內：示範貼文不得指名真實機構 ──────────────────────────────────


def test_no_fixture_post_can_be_attributed_to_a_real_institution():
    """編造的投訴掛在真機構名下，與真通報在畫面上長得一模一樣。"""
    real = _real()
    for post in threads.load_fixture(FIXTURES[0]) + threads.load_fixture(FIXTURES[1]):
        att = mention_store.attribute_post(post, real)
        assert att.institution_id is None, f"{post.threads_id} 歸屬到了真實機構"


def test_no_real_kindergarten_name_appears_anywhere_in_the_fixture_text():
    """比歸屬更嚴一級：連提到都不行。

    歸屬有「緊貼幼兒園前面」這條規則，所以「XX幼兒園隔壁」不會被歸屬——但它
    仍然是把一所真實園寫進了一則捏造的貼文裡，而讀畫面的人看到的是那個名字。
    """
    cores = {distinctive_name(i["title"]) for i in _real()}
    cores = {c for c in cores if len(c) >= 2}
    for threads_id, text in _fixture_texts():
        hits = sorted(c for c in cores if c in text)
        assert not hits, f"{threads_id} 出現真實園所辨識名：{hits}"


def test_every_post_that_names_a_kindergarten_names_a_demo_one():
    """fixture 裡只要歸屬得上的，被指名的都必須是示範園。

    這條測的是**不變式**（捏造的投訴不得掛在真實機構名下），不是則數。
    第一版寫死「恰好 2 則」，於是 `alerts.py` 的跨縣市規則放寬、那則
    「板橋的示範一號幼兒園」跟著配上之後，測試就紅了——但那次改動完全沒有
    違反這條規則，紅的是斷言把不變式寫成了一個計數。

    改成「至少涵蓋原本那兩個情境、且全部指向示範園」：情境要留著（那是端點
    契約的重點），數量可以增加。
    """
    merged = demo_data.merged(_real())
    named = {}
    for post in threads.load_fixture(FIXTURES[0]) + threads.load_fixture(FIXTURES[1]):
        att = mention_store.attribute_post(post, merged)
        if att.attributed:
            named[post.threads_id] = att.institution_id

    # 不變式：一則都不准掛在真實機構名下。
    assert named, "fixture 至少要有一則歸屬得上，否則端點契約測不到"
    assert all(demo_data.is_demo(i) for i in named.values())
    # 情境：至少兩家不同的示範園被指名（主貼文一家、回覆指名另一家）。
    assert len(set(named.values())) >= 2


# ── 標示：靠 id，不靠名字 ───────────────────────────────────────────


def test_is_demo_answers_for_both_the_full_uuid_and_the_eight_char_form():
    """`threads_mention` 存完整 UUID，payload 的點只有前 8 碼，兩種都會被問。"""
    for inst in demo_data.institutions():
        assert demo_data.is_demo(inst["id"])
        assert demo_data.is_demo(inst["id"][:8])
    assert not demo_data.is_demo(None)
    assert not demo_data.is_demo("")
    for inst in _real()[:50]:
        assert not demo_data.is_demo(inst["id"])


def test_the_demo_file_keeps_the_real_master_columns_so_it_can_be_read_the_same_way():
    """欄位一樣，才不會有人為了讀這份檔案另寫一支解析而漏掉某一欄。"""
    import csv

    with REAL_CSV.open(encoding="utf-8") as fh:
        real_cols = next(csv.reader(fh))
    with demo_data.DEMO_CSV.open(encoding="utf-8") as fh:
        demo_cols = next(csv.reader(fh))
    assert demo_cols == real_cols


def test_each_demo_name_is_attributable_and_unique_against_the_real_register():
    """六筆彼此不撞、也不與真實主檔裡任何一筆撞。

    撞到的後果不是例外，是 `alerts.attribute()` 回「無法唯一歸屬」——於是示範
    貼文靜靜地掉進待人工認園的佇列，而 demo 當天看起來像是功能壞了。
    """
    merged = demo_data.merged(_real())
    cores = [distinctive_name(i["title"]) for i in demo_data.institutions()]
    assert len(set(cores)) == len(cores)
    for core, inst in zip(cores, demo_data.institutions()):
        assert len(core) >= 2
        att = attribute(f"{core}幼兒園的收費有問題", merged)
        if inst["type"] == "公立":
            # 公立園的辨識名帶「OO國民小學」，中間隔著「附設」，所以只有全名
            # 比對得到——真實主檔裡的國小附幼是同一個形狀。
            att = attribute(f"{inst['title']}的收費有問題", merged)
        assert att.institution_id == inst["id"], f"{inst['title']}：{att.basis}"


def test_the_demo_set_covers_more_than_one_establishment_type():
    """示範資料要展示得出分層（公立／非營利／私立的結構性差異）。"""
    assert {"私立", "非營利", "公立"} <= {i["type"] for i in demo_data.institutions()}
