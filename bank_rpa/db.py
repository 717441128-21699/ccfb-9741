"""SQLite 数据库：表结构与通用访问层（线程安全，check_same_thread=False）。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_no TEXT NOT NULL UNIQUE,
    account_name TEXT NOT NULL,           -- 集团内账户名称
    entity TEXT NOT NULL,                 -- 所属主体/子公司
    bank_code TEXT NOT NULL,              -- icbc/cmb/ccb/boc/citic
    bank_name TEXT NOT NULL,
    login_user TEXT NOT NULL,
    login_secret TEXT NOT NULL,           -- 仿真站口令（非真实凭据）
    login_type TEXT NOT NULL,             -- pwd / usbk / smscode / qrcode
    timeout_minutes INTEGER NOT NULL DEFAULT 10,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS download_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    date_from TEXT NOT NULL,
    date_to TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', -- pending/running/success/failed
    total_files INTEGER NOT NULL DEFAULT 0,
    done_files INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS download_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    biz_date TEXT NOT NULL,
    file_type TEXT NOT NULL,              -- receipt/statement
    fmt TEXT NOT NULL,                    -- pdf/csv/ofx
    file_path TEXT,
    file_hash TEXT,
    status TEXT NOT NULL,                 -- success/failed/skipped
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    downloaded_at TEXT,
    UNIQUE(account_id, biz_date, file_type, fmt),
    FOREIGN KEY(task_id) REFERENCES download_tasks(id)
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL,
    level TEXT NOT NULL,                  -- info/warn/error
    message TEXT NOT NULL,
    screenshot TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(task_id) REFERENCES download_tasks(id)
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    txn_uid TEXT NOT NULL,                -- 银行流水唯一号（去重主键来源）
    txn_date TEXT NOT NULL,               -- ISO 日期
    direction TEXT NOT NULL,              -- in/out
    amount REAL NOT NULL,                 -- 统一为正数元
    counterparty TEXT,                    -- 对方户名（原文）
    counterparty_norm TEXT,
    summary TEXT,                         -- 摘要（原文）
    summary_norm TEXT,
    source_types TEXT NOT NULL,           -- receipt,statement（去重后保留来源集合）
    source_files TEXT NOT NULL,           -- JSON 数组：来源文件路径
    raw_line TEXT,                        -- 原始解析行（留痕）
    created_at TEXT NOT NULL,
    UNIQUE(account_id, txn_uid),
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS erp_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    erp_no TEXT NOT NULL UNIQUE,
    record_date TEXT NOT NULL,
    direction TEXT NOT NULL,              -- in/out
    amount REAL NOT NULL,
    account_id INTEGER NOT NULL,
    counterparty TEXT,
    counterparty_norm TEXT,
    summary TEXT,
    summary_norm TEXT,
    source_file TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(account_id) REFERENCES accounts(id)
);

CREATE TABLE IF NOT EXISTS match_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_code TEXT NOT NULL,              -- exact/near11/one_to_many/many_to_one/manual
    match_date TEXT NOT NULL,
    txn_ids TEXT NOT NULL,                -- JSON 数组
    erp_ids TEXT NOT NULL,                -- JSON 数组
    txn_total REAL NOT NULL,
    erp_total REAL NOT NULL,
    diff REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'auto',  -- auto/manual/ignored
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_resolutions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                   -- match / ignore
    fingerprint TEXT NOT NULL UNIQUE,     -- 指纹：账户+方向+对方+金额特征
    note TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_txn_account ON transactions(account_id);
CREATE INDEX IF NOT EXISTS idx_txn_date ON transactions(txn_date);
CREATE INDEX IF NOT EXISTS idx_erp_account ON erp_records(account_id);
CREATE INDEX IF NOT EXISTS idx_files_account ON download_files(account_id);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query_all(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def query_one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def insert(conn: sqlite3.Connection, table: str, data: dict) -> int:
    cols = ", ".join(data.keys())
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(data.values()))
    return cur.lastrowid
