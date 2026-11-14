"""测试数据生成：22 个账户、银行流水、ERP 收付款（含尾差、简写、拆分/合并、单边账）。

生成产物：
1. SQLite：accounts / erp_records
2. data/bank_files 下每家银行每个账户的原始交易 JSON（供仿真站渲染下载文件）
3. data/erp/集团ERP收付款.xlsx
"""
from __future__ import annotations

import json
import random
from collections import defaultdict
from datetime import date, timedelta

from ..common import normalize_name, normalize_summary
from ..config import BANKFILE_DIR, ERP_DIR, PERIOD_FROM, PERIOD_TO, RNG_SEED
from ..db import get_conn, init_db, insert, now
from . import (
    ACCOUNT_PLAN,
    BANK_PROFILES,
    COUNTERPARTIES,
    GROUP_ENTITIES,
    SUMMARY_PAIRS,
)

# 场景权重（调整到异常率约 4%，给自动匹配率留缓冲）
SCENARIO_WEIGHTS = [
    ("exact", 58),
    ("near_tail", 10),
    ("short_name", 10),
    ("summary_diff", 8),
    ("one_to_many", 7),
    ("many_to_one", 3),
    ("bank_only", 2),
    ("erp_only", 2),
]

BANK_ACCT_PREFIX = {
    "icbc": "622202", "cmb": "622588", "ccb": "621700",
    "boc": "621661", "citic": "621768",
}


def _workdays(d_from: str, d_to: str) -> list[date]:
    a = date.fromisoformat(d_from)
    b = date.fromisoformat(d_to)
    out, cur = [], a
    while cur <= b:
        if cur.weekday() < 5:
            out.append(cur)
        cur += timedelta(days=1)
    return out


def _make_account_no(rng: random.Random, bank_code: str, seq: int) -> str:
    body = f"{seq:04d}{rng.randint(0, 999999999):09d}"
    return (BANK_ACCT_PREFIX[bank_code] + body)[:19]


def _amount(rng: random.Random) -> float:
    return round(rng.uniform(800, 200_000), 2)


def generate(reset: bool = True) -> dict:
    rng = random.Random(RNG_SEED)
    init_db()
    conn = get_conn()

    if reset:
        for tbl in (
            "match_groups", "manual_resolutions", "transactions", "erp_records",
            "download_files", "task_events", "download_tasks", "accounts",
        ):
            conn.execute(f"DELETE FROM {tbl}")
        conn.commit()

    workdays = _workdays(PERIOD_FROM, PERIOD_TO)
    scenarios = [s for s, w in SCENARIO_WEIGHTS for _ in range(w)]

    # ---------- 1. 账户 ----------
    accounts: list[dict] = []
    seq = 0
    for bank_code, cnt in ACCOUNT_PLAN.items():
        prof = BANK_PROFILES[bank_code]
        for i in range(cnt):
            seq += 1
            entity = GROUP_ENTITIES[(seq - 1) % len(GROUP_ENTITIES)]
            acct_no = _make_account_no(rng, bank_code, seq)
            aid = insert(conn, "accounts", {
                "account_no": acct_no,
                "account_name": f"{entity[:4]}账户{i + 1:02d}",
                "entity": entity,
                "bank_code": bank_code,
                "bank_name": prof["bank_name"],
                "login_user": f"op{seq:03d}",
                "login_secret": f"Sim@{seq:04d}",
                "login_type": prof["login_type"],
                "timeout_minutes": prof["timeout_minutes"],
                "enabled": 1,
                "created_at": now(),
            })
            accounts.append({
                "id": aid, "account_no": acct_no,
                "entity": entity, "bank_code": bank_code,
                "bank_name": prof["bank_name"],
                "login_type": prof["login_type"],
            })

    # ---------- 2. 银行流水 + ERP ----------
    all_txns: list[dict] = []
    all_erp: list[dict] = []
    by_account: dict[int, list[dict]] = defaultdict(list)
    stat = defaultdict(int)
    uid_seq = 0

    def next_uid() -> str:
        nonlocal uid_seq
        uid_seq += 1
        return f"TXN2026{uid_seq:08d}"

    def erp_no() -> str:
        return f"ERP{len(all_erp) + 1:08d}"

    def jitter_date(base: date) -> str:
        return (base + timedelta(days=rng.choice([0, 0, 0, 0, 1, -1, 2]))).isoformat()

    for acct in accounts:
        aid = acct["id"]
        # 每个工作日 1~3 笔
        for d in workdays:
            for _ in range(rng.randint(1, 3)):
                scenario = rng.choice(scenarios)
                direction = rng.choice(["in", "out"])
                cp_bank, cp_erp = rng.choice(COUNTERPARTIES)
                sum_bank, sum_erp = rng.choice(SUMMARY_PAIRS)
                amt = _amount(rng)
                base_txn = dict(
                    account_id=aid, txn_date=d.isoformat(), direction=direction,
                    amount=amt, counterparty=cp_bank, summary=sum_bank,
                    scenario=scenario,
                )

                if scenario in ("exact", "near_tail", "short_name", "summary_diff", "bank_only"):
                    t = dict(base_txn, txn_uid=next_uid())
                    all_txns.append(t)
                    by_account[aid].append(t)
                    if scenario == "bank_only":
                        stat["bank_only"] += 1
                        continue
                    erp_amt = amt
                    if scenario == "near_tail":
                        erp_amt = round(amt + rng.choice([-1, 1]) * rng.choice([0.01, 0.02, 0.05, 0.08]), 2)
                    erp_cp = cp_erp if scenario == "short_name" else cp_bank
                    erp_sum = sum_erp if scenario in ("summary_diff", "short_name") else sum_bank
                    e = dict(
                        erp_no=erp_no(), record_date=jitter_date(d), direction=direction,
                        amount=erp_amt, account_id=aid, counterparty=erp_cp,
                        summary=erp_sum, scenario=scenario,
                    )
                    all_erp.append(e)
                    stat[scenario] += 1

                elif scenario == "one_to_many":
                    # 1 笔银行流水 ← N 笔 ERP（拆分付款）
                    n = rng.choice([2, 3])
                    weights = sorted([rng.random() for _ in range(n)])
                    cuts = [0.0] + weights + [1.0]
                    parts = [round(amt * (cuts[i + 1] - cuts[i]), 2) for i in range(n)]
                    parts[-1] = round(amt - sum(parts[:-1]), 2)
                    t = dict(base_txn, txn_uid=next_uid())
                    all_txns.append(t)
                    by_account[aid].append(t)
                    for p in parts:
                        all_erp.append(dict(
                            erp_no=erp_no(), record_date=jitter_date(d),
                            direction=direction, amount=p, account_id=aid,
                            counterparty=cp_bank, summary=sum_erp,
                            scenario="one_to_many",
                        ))
                    stat["one_to_many"] += 1

                elif scenario == "many_to_one":
                    # N 笔银行流水 → 1 笔 ERP（合并到账/扣款）
                    n = rng.choice([2, 3])
                    weights = sorted([rng.random() for _ in range(n)])
                    cuts = [0.0] + weights + [1.0]
                    parts = [round(amt * (cuts[i + 1] - cuts[i]), 2) for i in range(n)]
                    parts[-1] = round(amt - sum(parts[:-1]), 2)
                    for p in parts:
                        t = dict(
                            account_id=aid, txn_uid=next_uid(),
                            txn_date=d.isoformat(), direction=direction, amount=p,
                            counterparty=cp_bank, summary=sum_bank,
                            scenario="many_to_one",
                        )
                        all_txns.append(t)
                        by_account[aid].append(t)
                    all_erp.append(dict(
                        erp_no=erp_no(), record_date=jitter_date(d),
                        direction=direction, amount=amt, account_id=aid,
                        counterparty=cp_bank, summary=sum_erp,
                        scenario="many_to_one",
                    ))
                    stat["many_to_one"] += 1

                elif scenario == "erp_only":
                    all_erp.append(dict(
                        erp_no=erp_no(), record_date=d.isoformat(), direction=direction,
                        amount=amt, account_id=aid, counterparty=cp_bank,
                        summary=sum_erp, scenario="erp_only",
                    ))
                    stat["erp_only"] += 1

    # ---------- 3. 落库 ERP ----------
    for e in all_erp:
        insert(conn, "erp_records", {
            "erp_no": e["erp_no"], "record_date": e["record_date"],
            "direction": e["direction"], "amount": e["amount"],
            "account_id": e["account_id"], "counterparty": e["counterparty"],
            "counterparty_norm": normalize_name(e["counterparty"]),
            "summary": e["summary"], "summary_norm": normalize_summary(e["summary"]),
            "source_file": "集团ERP收付款.xlsx", "created_at": now(),
        })
    conn.commit()

    # ---------- 4. 写出仿真站原始交易 JSON（按账户） ----------
    acct_index = {a["id"]: a for a in accounts}
    for aid, txns in by_account.items():
        acct = acct_index[aid]
        out_dir = BANKFILE_DIR / acct["bank_code"] / acct["account_no"]
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "txn_uid": t["txn_uid"], "txn_date": t["txn_date"],
                "direction": t["direction"], "amount": t["amount"],
                "counterparty": t["counterparty"], "summary": t["summary"],
                "scenario": t["scenario"],
            }
            for t in sorted(by_account[aid], key=lambda x: (x["txn_date"], x["txn_uid"]))
        ]
        (out_dir / "transactions.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    # ---------- 5. ERP xlsx ----------
    _write_erp_xlsx(all_erp, acct_index)

    conn.close()
    summary = {
        "accounts": len(accounts),
        "bank_transactions": len(all_txns),
        "erp_records": len(all_erp),
        "scenario_stat": dict(stat),
    }
    (BANKFILE_DIR.parent / "gen_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return summary


def _write_erp_xlsx(rows: list[dict], acct_index: dict) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "收付款明细"
    ws.append(["单据编号", "记账日期", "收付方向", "金额", "银行账号", "对方户名", "摘要", "场景标记"])
    dir_cn = {"in": "收", "out": "付"}
    for e in rows:
        acct = acct_index[e["account_id"]]
        ws.append([
            e["erp_no"], e["record_date"], dir_cn[e["direction"]], e["amount"],
            acct["account_no"], e["counterparty"], e["summary"], e["scenario"],
        ])
    for col, width in zip("ABCDEFGH", (14, 12, 8, 14, 22, 30, 18, 12)):
        ws.column_dimensions[col].width = width
    ERP_DIR.mkdir(parents=True, exist_ok=True)
    wb.save(ERP_DIR / "集团ERP收付款.xlsx")


if __name__ == "__main__":
    print(json.dumps(generate(), ensure_ascii=False, indent=2))
