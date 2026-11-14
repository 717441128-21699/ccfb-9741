"""解析入库管道：扫描已下载文件 → 解析 → 回单/流水去重合并 → 统一流水库。"""
from __future__ import annotations

import json

from ..common import normalize_name, normalize_summary
from ..db import get_conn, now
from .parsers import parse_file


def ingest_all() -> dict:
    """解析所有成功下载且未被当前流水库覆盖的文件，按 (账户,流水号) 去重入库。"""
    conn = get_conn()
    files = conn.execute(
        "SELECT * FROM download_files WHERE status='success' ORDER BY id"
    ).fetchall()

    parsed_count = 0
    file_errors: list[str] = []
    for f in files:
        try:
            rows = parse_file(
                conn.execute("SELECT bank_code FROM accounts WHERE id=?",
                             (f["account_id"],)).fetchone()["bank_code"],
                f["fmt"], f["file_path"],
            )
        except Exception as exc:  # 单文件失败不阻断整体
            file_errors.append(f"{f['file_path']}: {exc}")
            continue
        parsed_count += 1
        for r in rows:
            _upsert_txn(conn, f["account_id"], r)
    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    conn.close()
    return {
        "files_parsed": parsed_count,
        "file_errors": file_errors,
        "transactions_in_db": total,
    }


def _upsert_txn(conn, account_id: int, r: dict) -> None:
    existing = conn.execute(
        "SELECT * FROM transactions WHERE account_id=? AND txn_uid=?",
        (account_id, r["txn_uid"]),
    ).fetchone()
    sources = {r["source_type"]}
    src_files = {r["source_file"]}
    if existing:
        sources |= set(json.loads(existing["source_types"]))
        src_files |= set(json.loads(existing["source_files"]))
        conn.execute(
            """UPDATE transactions SET source_types=?, source_files=?, raw_line=?
               WHERE id=?""",
            (json.dumps(sorted(sources), ensure_ascii=False),
             json.dumps(sorted(src_files), ensure_ascii=False),
             r.get("raw_line", existing["raw_line"]), existing["id"]),
        )
        return
    conn.execute(
        """INSERT INTO transactions
           (account_id, txn_uid, txn_date, direction, amount, counterparty,
            counterparty_norm, summary, summary_norm, source_types, source_files,
            raw_line, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (account_id, r["txn_uid"], r["txn_date"], r["direction"], r["amount"],
         r["counterparty"], normalize_name(r["counterparty"]),
         r["summary"], normalize_summary(r["summary"]),
         json.dumps(sorted(sources), ensure_ascii=False),
         json.dumps([r["source_file"]], ensure_ascii=False),
         r.get("raw_line", ""), now()),
    )
