"""RPA 集成测试：仿真站登录、会话超时自动重登、下载失败重试（真实 Playwright）。

只跑 1 个建行（短超时）账户，用独立临时库，约 20 秒。
"""
import importlib
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bank_rpa.banksim import server as bank_server
from bank_rpa.config import BANKFILE_DIR
from bank_rpa.db import get_conn, init_db, insert, now
from bank_rpa.rpa.bot import BotRunner, create_task
from werkzeug.serving import make_server


@pytest.fixture()
def live_env(tmp_path, monkeypatch):
    db = tmp_path / "rpa.db"
    init_db(db)
    conn = get_conn(db)
    aid = insert(conn, "accounts", {
        "account_no": "621700TESTCCB", "account_name": "测试建行户",
        "entity": "华远测试主体", "bank_code": "ccb", "bank_name": "中国建设银行",
        "login_user": "opccb", "login_secret": "Sim@ccb",
        "login_type": "smscode", "timeout_minutes": 1,  # 1 分钟超时
        "enabled": 1, "created_at": now(),
    })
    conn.commit()
    conn.close()

    # 仿真站交易清单
    txn_dir = BANKFILE_DIR / "ccb" / "621700TESTCCB"
    txn_dir.mkdir(parents=True, exist_ok=True)
    import json
    (txn_dir / "transactions.json").write_text(json.dumps([
        {"txn_uid": "CT1", "txn_date": "2026-10-08", "direction": "in",
         "amount": 1234.56, "counterparty": "测试对方有限公司",
         "summary": "货款", "scenario": "exact"},
    ], ensure_ascii=False), encoding="utf-8")

    # 被测模块统一指向临时库
    import bank_rpa.banksim.server as srv_mod
    import bank_rpa.rpa.bot as bot_mod
    monkeypatch.setattr(srv_mod, "get_conn", lambda: get_conn(db))
    monkeypatch.setattr(bot_mod, "get_conn", lambda: get_conn(db))

    httpd = make_server("127.0.0.1", 0, bank_server.app, threaded=True)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    time.sleep(0.5)
    yield f"http://127.0.0.1:{port}", db, aid
    httpd.shutdown()


def test_login_timeout_relogin_and_download(live_env):
    base_url, db, aid = live_env
    with BotRunner(base_url=base_url, headless=True, slow_ms=80) as bot:
        page = bot.browser.new_page(accept_downloads=True)
        conn = get_conn(db)
        acct = dict(conn.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone())

        # 1. 短信验证登录成功
        bot.login(page, conn, 999, acct)
        assert "/ccb/home" in page.url

        # 2. 闲置超过会话超时 → 访问账户页被踢回登录页 → 自动重登
        time.sleep(65)
        page.goto(f"{base_url}/ccb/account/{acct['account_no']}")
        page.wait_for_timeout(300)
        assert "/login" in page.url
        bot._ensure_session(page, conn, 999, acct)
        assert "/ccb/home" in page.url

        events = [r["message"] for r in conn.execute(
            "SELECT * FROM task_events WHERE task_id=999")]
        assert any("会话超时" in m for m in events)

        # 3. 真实执行下载任务（OFX 流水 + PDF 回单），允许故障注入重试
        tid = create_task_safely(db, aid)
        result = bot.run_task(tid)
        assert result["planned"] == 2
        assert result["success"] is True
        files = conn.execute(
            "SELECT status, fmt, attempts FROM download_files WHERE task_id=?",
            (tid,)).fetchall()
        assert {f["status"] for f in files} == {"success"}
        assert {f["fmt"] for f in files} == {"ofx", "pdf"}
        # 截图留痕存在
        shots = conn.execute(
            "SELECT COUNT(*) c FROM task_events WHERE task_id=? AND screenshot<>''",
            (tid,)).fetchone()["c"]
        assert shots >= 2
        conn.close()
        page.close()


def create_task_safely(db, aid: int) -> int:
    conn = get_conn(db)
    cur = conn.execute(
        "INSERT INTO download_tasks (account_id, date_from, date_to, status,"
        " created_at, updated_at) VALUES (?, '2026-10-01', '2026-10-31', 'pending', ?, ?)",
        (aid, now(), now()))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid
