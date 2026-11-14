"""对账引擎：银行流水 ↔ ERP 收付款自动匹配。

匹配顺序：
1. 人工处理记忆（manual_resolutions）：同指纹直接复用，已处理过的不再报异常
2. 1:1 评分匹配（金额/日期/对方户名/摘要，含尾差与简写）
3. 一对多 / 多对一组合匹配（子集和 + 尾差容忍）
4. 剩余进入异常清单，交由后台人工处理
"""
from __future__ import annotations

import json
from itertools import combinations

from ..common import (
    days_between,
    fingerprint,
    name_similarity,
    normalize_name,
    summary_similarity,
)
from ..config import (
    AUTO_MATCH_SCORE,
    DATE_WINDOW_DAYS,
    GROUP_TOLERANCE_RATE,
    TAIL_TOLERANCE,
)
from ..db import get_conn, now, transaction

# 1:1 评分权重
W_AMOUNT, W_NAME, W_DATE, W_SUMMARY = 45, 25, 18, 12


def _score_1to1(t: dict, e: dict) -> tuple[float, str]:
    amt_diff = abs(t["amount"] - e["amount"])
    if amt_diff <= 0.005:
        s_amt, rule = 100.0, "exact"
    elif amt_diff <= TAIL_TOLERANCE:
        s_amt, rule = 92.0, "near11"
    else:
        return 0.0, ""
    s_name = name_similarity(t["counterparty"], e["counterparty"])
    d_gap = days_between(t["txn_date"], e["record_date"])
    if d_gap > DATE_WINDOW_DAYS:
        return 0.0, ""
    s_date = max(0.0, 100.0 * (1 - d_gap / (DATE_WINDOW_DAYS + 1)))
    s_sum = summary_similarity(t["summary"], e["summary"])
    score = (W_AMOUNT * s_amt + W_NAME * 100 * s_name +
             W_DATE * s_date + W_SUMMARY * 100 * s_sum) / 100
    return score, rule


def _candidates(t: dict, erp_pool: list[dict]) -> list[dict]:
    """同账户、同方向、日期窗口、金额近似（尾差放宽到 1 元做候选，评分再卡）。"""
    out = []
    for e in erp_pool:
        if e["account_id"] != t["account_id"] or e["direction"] != t["direction"]:
            continue
        if days_between(t["txn_date"], e["record_date"]) > DATE_WINDOW_DAYS:
            continue
        if abs(t["amount"] - e["amount"]) > max(TAIL_TOLERANCE, 1.0):
            continue
        out.append(e)
    return out


def _group_match(target: dict, pool: list[dict], target_is_txn: bool) -> tuple[list[dict], float] | None:
    """在 pool 中找 2~4 条组合，总额贴近 target（尾差容忍）。"""
    tol = max(TAIL_TOLERANCE, abs(target["amount"]) * GROUP_TOLERANCE_RATE)
    best: tuple[list[dict], float] | None = None
    # 先按金额接近排序并限制候选规模，避免组合爆炸
    pool = sorted(pool, key=lambda x: abs(x["amount"] - target["amount"]))[:10]
    for size in (2, 3, 4):
        for combo in combinations(pool, size):
            total = round(sum(c["amount"] for c in combo), 2)
            diff = round(abs(total - target["amount"]), 2)
            if diff <= tol:
                # 名称/日期也要基本对得上
                if any(days_between(
                        target["txn_date"] if target_is_txn else target["record_date"],
                        c["record_date"] if target_is_txn else c["txn_date"])
                       > DATE_WINDOW_DAYS + 1 for c in combo):
                    continue
                if best is None or diff < best[1]:
                    best = (list(combo), diff)
        if best:
            break
    return best


def _save_group(conn, rule: str, txns: list[dict], erps: list[dict],
                status: str = "auto", note: str | None = None) -> None:
    txn_total = round(sum(t["amount"] for t in txns), 2)
    erp_total = round(sum(e["amount"] for e in erps), 2)
    conn.execute(
        "INSERT INTO match_groups (rule_code, match_date, txn_ids, erp_ids,"
        " txn_total, erp_total, diff, status, note, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rule, now(),
         json.dumps([t["id"] for t in txns]),
         json.dumps([e["id"] for e in erps]),
         txn_total, erp_total, round(txn_total - erp_total, 2),
         status, note, now()),
    )


def reconcile() -> dict:
    """执行全量对账（先清空旧的自动匹配组，人工组保留）。"""
    conn = get_conn()
    with transaction(conn):
        conn.execute("DELETE FROM match_groups WHERE status='auto'")

        txns = [dict(r) for r in conn.execute(
            "SELECT * FROM transactions ORDER BY account_id, txn_date, id")]
        erps = [dict(r) for r in conn.execute(
            "SELECT * FROM erp_records ORDER BY account_id, record_date, id")]
        resolutions = {
            r["fingerprint"]: dict(r)
            for r in conn.execute("SELECT * FROM manual_resolutions")
        }

        used_txn: set[int] = set()
        used_erp: set[int] = set()
        stat = {"exact": 0, "near11": 0, "one_to_many": 0,
                "many_to_one": 0, "manual_reuse": 0, "unmatched_txn": 0,
                "unmatched_erp": 0}

        # ---- 1. 人工记忆复用（同指纹：账户+方向+归一户名+金额） ----
        for t in txns:
            fp = fingerprint(conn, t["account_id"], t["direction"],
                             t["counterparty"], t["amount"])
            res = resolutions.get(fp)
            if not res:
                continue
            if res["kind"] == "ignore":
                _save_group(conn, "manual", [t], [], status="ignored",
                            note=f"复用人工忽略：{res['note'] or ''}")
                used_txn.add(t["id"])
                stat["manual_reuse"] += 1
            else:
                # 人工匹配记忆：找同指纹的 ERP
                for e in erps:
                    if e["id"] in used_erp:
                        continue
                    efp = fingerprint(conn, e["account_id"], e["direction"],
                                      e["counterparty"], e["amount"])
                    if efp == fp:
                        _save_group(conn, "manual", [t], [e], status="manual",
                                    note=f"复用人工匹配：{res['note'] or ''}")
                        used_txn.add(t["id"])
                        used_erp.add(e["id"])
                        stat["manual_reuse"] += 1
                        break

        # ---- 2. 1:1 评分匹配（贪心，全局取分最高的配对） ----
        pairs: list[tuple[float, str, dict, dict]] = []
        for t in txns:
            if t["id"] in used_txn:
                continue
            for e in _candidates(t, [e for e in erps if e["id"] not in used_erp]):
                score, rule = _score_1to1(t, e)
                if score >= AUTO_MATCH_SCORE:
                    pairs.append((score, rule, t, e))
        pairs.sort(key=lambda x: x[0], reverse=True)
        for score, rule, t, e in pairs:
            if t["id"] in used_txn or e["id"] in used_erp:
                continue
            _save_group(conn, rule, [t], [e])
            used_txn.add(t["id"])
            used_erp.add(e["id"])
            stat[rule] += 1

        # ---- 3. 一对多（1 流水 vs N ERP） ----
        for t in txns:
            if t["id"] in used_txn:
                continue
            pool = [e for e in erps
                    if e["id"] not in used_erp
                    and e["account_id"] == t["account_id"]
                    and e["direction"] == t["direction"]
                    and normalize_name(e["counterparty"])
                        and name_similarity(t["counterparty"], e["counterparty"]) >= 0.5]
            hit = _group_match(t, pool, target_is_txn=True)
            if hit:
                combo, _diff = hit
                _save_group(conn, "one_to_many", [t], combo)
                used_txn.add(t["id"])
                used_erp.update(c["id"] for c in combo)
                stat["one_to_many"] += 1

        # ---- 4. 多对一（N 流水 vs 1 ERP） ----
        for e in erps:
            if e["id"] in used_erp:
                continue
            pool = [t for t in txns
                    if t["id"] not in used_txn
                    and t["account_id"] == e["account_id"]
                    and t["direction"] == e["direction"]
                    and name_similarity(t["counterparty"], e["counterparty"]) >= 0.5]
            hit = _group_match(e, pool, target_is_txn=False)
            if hit:
                combo, _diff = hit
                _save_group(conn, "many_to_one", combo, [e])
                used_erp.add(e["id"])
                used_txn.update(c["id"] for c in combo)
                stat["many_to_one"] += 1

        # ---- 5. 剩余即异常 ----
        left_txn = [t for t in txns if t["id"] not in used_txn]
        left_erp = [e for e in erps if e["id"] not in used_erp]
        stat["unmatched_txn"] = len(left_txn)
        stat["unmatched_erp"] = len(left_erp)
        total_txn = len(txns)
        matched_txn = total_txn - len(left_txn)
        stat["auto_rate"] = round(100 * matched_txn / total_txn, 2) if total_txn else 100.0
        stat["total_txn"] = total_txn
        stat["total_erp"] = len(erps)

    conn.close()
    return stat


# ---------- 后台人工操作 ----------

def manual_match(txn_ids: list[int], erp_ids: list[int], note: str) -> int:
    conn = get_conn()
    with transaction(conn):
        txns = [dict(r) for r in conn.execute(
            f"SELECT * FROM transactions WHERE id IN ({','.join('?' * len(txn_ids))})",
            txn_ids)]
        erps = [dict(r) for r in conn.execute(
            f"SELECT * FROM erp_records WHERE id IN ({','.join('?' * len(erp_ids))})",
            erp_ids)]
        if not txns or not erps:
            conn.close()
            raise ValueError("人工匹配至少各选一条流水与 ERP 记录")
        _save_group(conn, "manual", txns, erps, status="manual", note=note)
        # 写指纹记忆（取第一条双方指纹，下次同特征自动复用）
        for t in txns:
            fp = fingerprint(conn, t["account_id"], t["direction"],
                             t["counterparty"], t["amount"])
            conn.execute(
                "INSERT OR IGNORE INTO manual_resolutions (kind, fingerprint, note, created_at)"
                " VALUES ('match',?,?,?)", (fp, note, now()))
    conn.close()
    return len(txns)


def manual_ignore(txn_ids: list[int], note: str) -> None:
    conn = get_conn()
    with transaction(conn):
        txns = [dict(r) for r in conn.execute(
            f"SELECT * FROM transactions WHERE id IN ({','.join('?' * len(txn_ids))})",
            txn_ids)]
        for t in txns:
            fp = fingerprint(conn, t["account_id"], t["direction"],
                             t["counterparty"], t["amount"])
            conn.execute(
                "INSERT OR IGNORE INTO manual_resolutions (kind, fingerprint, note, created_at)"
                " VALUES ('ignore',?,?,?)", (fp, note, now()))
            _save_group(conn, "manual", [t], [], status="ignored", note=note)
    conn.close()


def exceptions() -> dict:
    """返回当前异常清单（未进入任何匹配组的流水与 ERP）。"""
    conn = get_conn()
    groups = conn.execute("SELECT txn_ids, erp_ids FROM match_groups").fetchall()
    matched_txn, matched_erp = set(), set()
    for g in groups:
        matched_txn.update(json.loads(g["txn_ids"]))
        matched_erp.update(json.loads(g["erp_ids"]))
    if matched_txn:
        ph = ",".join("?" * len(matched_txn))
        left_txn = [dict(r) for r in conn.execute(
            f"SELECT t.*, a.account_no, a.bank_name FROM transactions t"
            f" JOIN accounts a ON a.id=t.account_id"
            f" WHERE t.id NOT IN ({ph}) ORDER BY t.txn_date", tuple(matched_txn))]
        left_erp = [dict(r) for r in conn.execute(
            f"SELECT e.*, a.account_no FROM erp_records e"
            f" JOIN accounts a ON a.id=e.account_id"
            f" WHERE e.id NOT IN ({','.join('?' * len(matched_erp))})"
            f" ORDER BY e.record_date", tuple(matched_erp))]
    else:
        left_txn = [dict(r) for r in conn.execute(
            "SELECT t.*, a.account_no, a.bank_name FROM transactions t"
            " JOIN accounts a ON a.id=t.account_id ORDER BY t.txn_date")]
        left_erp = [dict(r) for r in conn.execute(
            "SELECT e.*, a.account_no FROM erp_records e"
            " JOIN accounts a ON a.id=e.account_id ORDER BY e.record_date")]
    conn.close()
    return {"bank_only": left_txn, "erp_only": left_erp}
