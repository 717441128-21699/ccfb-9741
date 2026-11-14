"""端到端对账测试：临时库造数据，覆盖尾差/简写/一对多/多对一/人工记忆。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bank_rpa.common import normalize_name, normalize_summary
from bank_rpa.db import get_conn, init_db, insert, now
from bank_rpa.reconcile.engine import (
    exceptions,
    manual_ignore,
    manual_match,
    reconcile,
)


@pytest.fixture()
def mem_db(tmp_path, monkeypatch):
    db = tmp_path / "test.db"
    init_db(db)
    # 让被测模块都使用这个临时库
    import bank_rpa.reconcile.engine as eng
    monkeypatch.setattr(eng, "get_conn", lambda: get_conn(db))
    import bank_rpa.ingest.pipeline as pipe
    monkeypatch.setattr(pipe, "get_conn", lambda: get_conn(db))
    conn = get_conn(db)
    aid = insert(conn, "accounts", {
        "account_no": "622202TEST", "account_name": "测试账户",
        "entity": "测试主体", "bank_code": "icbc", "bank_name": "工商银行",
        "login_user": "u", "login_secret": "p", "login_type": "pwd",
        "timeout_minutes": 10, "enabled": 1, "created_at": now(),
    })
    conn.commit()
    conn.close()
    return db, aid


def _txn(db, aid, uid, d, direction, amt, cp, summary):
    conn = get_conn(db)
    insert(conn, "transactions", {
        "account_id": aid, "txn_uid": uid, "txn_date": d,
        "direction": direction, "amount": amt, "counterparty": cp,
        "counterparty_norm": normalize_name(cp), "summary": summary,
        "summary_norm": normalize_summary(summary),
        "source_types": json.dumps(["receipt", "statement"]),
        "source_files": json.dumps(["f"]), "raw_line": "", "created_at": now(),
    })
    conn.commit()
    conn.close()


def _erp(db, aid, no, d, direction, amt, cp, summary):
    conn = get_conn(db)
    insert(conn, "erp_records", {
        "erp_no": no, "record_date": d, "direction": direction, "amount": amt,
        "account_id": aid, "counterparty": cp,
        "counterparty_norm": normalize_name(cp), "summary": summary,
        "summary_norm": normalize_summary(summary),
        "source_file": "x.xlsx", "created_at": now(),
    })
    conn.commit()
    conn.close()


def test_full_recon_scenarios(mem_db):
    db, aid = mem_db
    # 1) 精确
    _txn(db, aid, "T1", "2026-10-08", "in", 10000.0, "北京星辰科技有限公司", "货款")
    _erp(db, aid, "E1", "2026-10-08", "in", 10000.0, "北京星辰科技有限公司", "支付货款")
    # 2) 尾差 0.05 + 简写 + 日期差 1 天
    _txn(db, aid, "T2", "2026-10-09", "out", 5000.0, "上海云海国际贸易有限公司", "服务费")
    _erp(db, aid, "E2", "2026-10-10", "out", 4999.95, "云海贸易", "咨询服务费")
    # 3) 一对多：1 笔流水 12000，ERP 拆 7000+5000
    _txn(db, aid, "T3", "2026-10-12", "out", 12000.0, "广州恒基建设工程有限公司", "工程款")
    _erp(db, aid, "E3", "2026-10-12", "out", 7000.0, "广州恒基建设工程有限公司", "工程进度款")
    _erp(db, aid, "E4", "2026-10-12", "out", 5000.0, "广州恒基建设工程有限公司", "工程进度款")
    # 4) 多对一：流水 3000+4000，ERP 1 笔 7000
    _txn(db, aid, "T4", "2026-10-13", "in", 3000.0, "深圳锐创电子有限公司", "货款")
    _txn(db, aid, "T5", "2026-10-13", "in", 4000.0, "深圳锐创电子有限公司", "货款")
    _erp(db, aid, "E5", "2026-10-13", "in", 7000.0, "深圳锐创电子有限公司", "支付货款")
    # 5) 银行单边（异常）
    _txn(db, aid, "T6", "2026-10-14", "out", 66.0, "中国电信股份有限公司北京分公司", "手续费")

    stat = reconcile()
    assert stat["exact"] == 1
    assert stat["near11"] == 1
    assert stat["one_to_many"] == 1
    assert stat["many_to_one"] == 1
    assert stat["unmatched_txn"] == 1
    assert stat["unmatched_erp"] == 0
    assert stat["auto_rate"] == pytest.approx(100 * 5 / 6, abs=0.01)

    exc = exceptions()
    assert len(exc["bank_only"]) == 1
    assert exc["bank_only"][0]["txn_uid"] == "T6"


def test_manual_resolution_memory(mem_db):
    db, aid = mem_db
    _txn(db, aid, "T1", "2026-10-08", "out", 88.88, "北京东方公证处", "公证费")
    # 首轮：无 ERP，进入异常
    stat = reconcile()
    assert stat["unmatched_txn"] == 1
    # 人工忽略
    conn = get_conn(db)
    tid = conn.execute("SELECT id FROM transactions WHERE txn_uid='T1'").fetchone()["id"]
    conn.close()
    manual_ignore([tid], "银行代扣，无 ERP 单据")
    # 重新对账：指纹复用，不再报异常
    stat2 = reconcile()
    assert stat2["unmatched_txn"] == 0
    assert stat2["manual_reuse"] >= 1
    assert exceptions()["bank_only"] == []


def test_manual_match_pair(mem_db):
    db, aid = mem_db
    _txn(db, aid, "T1", "2026-10-08", "in", 100.0, "厦门鹭岛食品有限公司", "货款")
    _erp(db, aid, "E1", "2026-10-20", "in", 100.0, "厦门鹭岛食品有限公司", "货款")  # 日期隔太远自动匹配不上
    conn = get_conn(db)
    tid = conn.execute("SELECT id FROM transactions").fetchone()["id"]
    eid = conn.execute("SELECT id FROM erp_records").fetchone()["id"]
    conn.close()
    manual_match([tid], [eid], "跨月到账，人工确认")
    stat = reconcile()
    assert stat["unmatched_txn"] == 0 and stat["unmatched_erp"] == 0
