"""建表並建立稽查員帳號。冪等：重跑只更新密碼與負責行政區，不重複建帳號。

    PYTHONPATH=src .venv/bin/python scripts/seed_users.py
    PYTHONPATH=src .venv/bin/python scripts/seed_users.py --reset   # 先砍掉五張表

帳密取自 `.env` 的 `SEED_INSPECTOR_EMAIL` 與 `SEED_INSPECTOR_PASSWORD`。
兩者缺一就不建帳號並明白說缺什麼——不預設一組寫死的密碼，那種東西會跟著
專案一路帶到部署環境。

`--reset` 存在的理由是**沒有 alembic**（見 `db/models.py` 模組說明第 3 點）：
`create_all` 只建不存在的表，不會 ALTER 既有表。所以每次改 `models.py` 的欄位，
既有的開發資料庫就會少一欄，症狀是 API 回 500 而 log 寫 `no such column`。
這在本機可以接受（`data/runtime/` 是 gitignore 的開發資料），但**上了 RDS
之後就要改用 migration**，不能靠 drop。
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from smart_watchdog import config
from smart_watchdog.console import use_utf8
from smart_watchdog.db.models import Base, User
from smart_watchdog.db.session import engine, init_db, session, url
from smart_watchdog.security import hash_password

use_utf8()

# 示範帳號負責的行政區。agent 的「使用者沒說行政區就用他負責的」會讀這欄。
TOWNS = ["板橋區", "三重區", "新莊區"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true",
                    help="先 drop 五張表再重建（改過 models.py 的欄位後要用）")
    args = ap.parse_args()

    config.load_env()
    email = config.get("SEED_INSPECTOR_EMAIL")
    password = config.get("SEED_INSPECTOR_PASSWORD")

    print(f"資料庫：{url()}")
    if args.reset:
        Base.metadata.drop_all(engine())
        print("  ⚠️ 已 drop 五張表（含既有的對話稽核軌跡）")
    init_db()
    print("  ✓ 五張表就緒（user、user_session、agent_session、"
          "agent_message、audit_feedback）")

    if not email or not password:
        print("\n  ⬜ 未建帳號：.env 缺 SEED_INSPECTOR_EMAIL 或 SEED_INSPECTOR_PASSWORD")
        return 0

    email = email.strip().lower()
    db = session()
    try:
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if user is None:
            db.add(User(
                email=email, name="示範稽查員", role="inspector",
                unit="新北市政府教育局", towns=TOWNS,
                password_hash=hash_password(password), is_active=True,
            ))
            action = "建立"
        else:
            user.password_hash = hash_password(password)
            user.towns = TOWNS
            user.is_active = True
            action = "更新"
        db.commit()
    finally:
        db.close()

    print(f"  ✓ {action}帳號 {email}（負責 {'、'.join(TOWNS)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
