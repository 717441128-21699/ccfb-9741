"""核心逻辑单元测试：归一化、三种文件解析、去重、对账规则与人工记忆。

运行: py -3.12 -m pytest tests -v
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bank_rpa.banksim.builders import build_csv, build_ofx, build_pdf_receipt
from bank_rpa.common import (
    days_between,
    name_similarity,
    normalize_name,
    normalize_summary,
    parse_amount,
    parse_date,
)
from bank_rpa.ingest.parsers import parse_csv, parse_ofx, parse_pdf


# ---------- 归一化 ----------

@pytest.mark.parametrize("raw,expect", [
    ("12,345.67", 12345.67),
    ("￥1,200.00元", 1200.0),
    ("1.23万", 12300.0),
    ("(500.00)", 500.0),
    (99.9, 99.9),
])
def test_parse_amount(raw, expect):
    assert parse_amount(raw) == expect


@pytest.mark.parametrize("raw,expect", [
    ("20261001", "2026-10-01"),
    ("01/10/2026", "2026-10-01"),
    ("2026年10月1日", "2026-10-01"),
    ("2026/10/01", "2026-10-01"),
])
def test_parse_date(raw, expect):
    assert parse_date(raw) == expect
    assert days_between("2026-10-01", "2026-10-03") == 2


def test_name_and_summary_norm():
    assert normalize_name("北京星辰科技有限公司") == "北京星辰科技"
    assert name_similarity("北京星辰科技有限公司", "星辰科技") > 0.9
    assert name_similarity("上海云海国际贸易有限公司", "云海贸易") > 0.5
    assert normalize_summary("网银转账:货款ABC123456") == "货款"


# ---------- 文件往返解析 ----------

ACCT = {"account_no": "6222020001", "entity": "华远控股集团有限公司"}
TXNS = [
    {"txn_uid": "TXN001", "txn_date": "2026-10-08", "direction": "in",
     "amount": 12345.67, "counterparty": "北京星辰科技有限公司", "summary": "货款", "scenario": "exact"},
    {"txn_uid": "TXN002", "txn_date": "2026-10-09", "direction": "out",
     "amount": 8000.0, "counterparty": "上海云海国际贸易有限公司", "summary": "服务费", "scenario": "exact"},
]


def test_csv_roundtrip(tmp_path):
    for bank in ("icbc", "cmb", "citic"):
        p = tmp_path / f"{bank}.csv"
        p.write_bytes(build_csv(bank, ACCT, TXNS, "2026-10-01", "2026-10-31"))
        rows = parse_csv(bank, p)
        assert len(rows) == 2
        assert {r["txn_uid"] for r in rows} == {"TXN001", "TXN002"}
        r1 = next(r for r in rows if r["txn_uid"] == "TXN001")
        assert r1["direction"] == "in" and r1["amount"] == 12345.67
        assert r1["txn_date"] == "2026-10-08"
        r2 = next(r for r in rows if r["txn_uid"] == "TXN002")
        assert r2["direction"] == "out"


def test_ofx_roundtrip(tmp_path):
    for bank in ("ccb", "boc"):
        p = tmp_path / f"{bank}.ofx"
        p.write_bytes(build_ofx(bank, ACCT, TXNS, "2026-10-01", "2026-10-31"))
        rows = parse_ofx(bank, p)
        assert len(rows) == 2
        r1 = next(r for r in rows if r["txn_uid"] == "TXN001")
        assert r1["amount"] == 12345.67 and r1["direction"] == "in"
        assert r1["counterparty"] == "北京星辰科技有限公司"


def test_pdf_roundtrip_all_banks(tmp_path):
    for bank in ("icbc", "cmb", "ccb", "boc", "citic"):
        p = tmp_path / f"{bank}.pdf"
        p.write_bytes(build_pdf_receipt(bank, ACCT, TXNS, "2026-10-08"))
        rows = parse_pdf(bank, p)
        assert len(rows) == 2, bank
        r1 = next(r for r in rows if r["txn_uid"] == "TXN001")
        assert r1["amount"] == 12345.67
        assert r1["txn_date"] == "2026-10-08"
        assert r1["counterparty"] == "北京星辰科技有限公司"
        assert r1["source_type"] == "receipt"
        r2 = next(r for r in rows if r["txn_uid"] == "TXN002")
        assert r2["direction"] == "out"
