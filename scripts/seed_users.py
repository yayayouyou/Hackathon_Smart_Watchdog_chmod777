"""建表並建立稽查員帳號。冪等：重跑只更新密碼與負責行政區，不重複建帳號。

    PYTHONPATH=src .venv/bin/python scripts/seed_users.py

帳密取自 `.env` 的 `SEED_INSPECTOR_EMAIL` 與 `SEED_INSPECTOR_PASSWORD`。
兩者缺一就不建帳號並明白說缺什麼——不預設一組寫死的密碼，那種東西會跟著
專案一路帶到部署環境。
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import select

from smart_watchdog import config
from smart_watchdog.console import use_utf8
from smart_watchdog.db.models import User
from smart_watchdog.db.session import init_db, session, url
from smart_watchdog.security import hash_password

use_utf8()

# 示範帳號負責的行政區。agent 的「使用者沒說行政區就用他負責的」會讀這欄。
TOWNS = ["板橋區", "三重區", "新莊區"]


def main() -> int:
    config.load_env()
    email = config.get("SEED_INSPECTOR_EMAIL")
    password = config.get("SEED_INSPECTOR_PASSWORD")

    print(f"資料庫：{url()}")
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
