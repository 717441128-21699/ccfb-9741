"""对账后台 Flask 应用：账户配置 / 下载任务 / 流水库 / 对账结果 / 异常处理。"""
from __future__ import annotations

import json
import threading

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, send_file, url_for

from ..config import PERIOD_FROM, PERIOD_TO, SCREENSHOT_DIR, WEB_PORT
from ..db import get_conn
from ..ingest.pipeline import ingest_all
from ..reconcile.engine import (
    exceptions,
    manual_ignore,
    manual_match,
    reconcile,
)
from ..rpa.bot import BotRunner, create_task

app = Flask(__name__)
app.secret_key = "local-recon-admin"
app.template_filter("fromjson")(json.loads)


@app.template_global()
def accounts_for_select():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM accounts WHERE enabled=1 ORDER BY bank_code, id").fetchall()
    conn.close()
    return rows


@app.route("/screenshots/<path:path>")
def static_shot(path):
    # 仅允许访问截图目录，防穿越
    target = (SCREENSHOT_DIR / path).resolve()
    root = SCREENSHOT_DIR.resolve()
    if not str(target).startswith(str(root)):
        abort(404)
    if not target.exists():
        abort(404)
    return send_file(str(target))

_jobs_lock = threading.Lock()
_jobs: list[dict] = []


def _job_add(name: str, target, *args) -> None:
    def runner():
        try:
            target(*args)
            with _jobs_lock:
                _jobs.append({"name": name, "status": "done", "ok": True})
        except Exception as exc:  # noqa: BLE001
            with _jobs_lock:
                _jobs.append({"name": name, "status": "done",
                              "ok": False, "error": str(exc)[:300]})

    threading.Thread(target=runner, daemon=True).start()
    with _jobs_lock:
        _jobs.append({"name": name, "status": "running", "ok": None})


# ---------------- 首页 / 概览 ----------------

@app.route("/")
def dashboard():
    conn = get_conn()
    stats = {
        "accounts": conn.execute("SELECT COUNT(*) c FROM accounts").fetchone()["c"],
        "tasks": conn.execute("SELECT COUNT(*) c FROM download_tasks").fetchone()["c"],
        "files_failed": conn.execute(
            "SELECT COUNT(*) c FROM download_files WHERE status='failed'").fetchone()["c"],
        "txns": conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"],
        "erp": conn.execute("SELECT COUNT(*) c FROM erp_records").fetchone()["c"],
        "groups": conn.execute("SELECT COUNT(*) c FROM match_groups").fetchone()["c"],
    }
    last_rate = conn.execute(
        "SELECT 1 FROM match_groups LIMIT 1").fetchone()
    with _jobs_lock:
        jobs = list(reversed(_jobs[-8:]))
    conn.close()
    return render_template("dashboard.html", stats=stats, jobs=jobs)


# ---------------- 1. 账户配置 ----------------

@app.route("/accounts")
def accounts_page():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM accounts ORDER BY bank_code, id").fetchall()
    # 每个账户的下载进度
    prog = {}
    for r in rows:
        agg = conn.execute(
            "SELECT SUM(status='success') ok, SUM(status='failed') fail,"
            " COUNT(*) total FROM download_files WHERE account_id=?",
            (r["id"],)).fetchone()
        prog[r["id"]] = agg
    conn.close()
    return render_template("accounts.html", accounts=rows, prog=prog)


@app.route("/accounts/toggle/<int:aid>", methods=["POST"])
def accounts_toggle(aid):
    conn = get_conn()
    conn.execute("UPDATE accounts SET enabled=1-enabled WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    return redirect(url_for("accounts_page"))


# ---------------- 2. 下载任务 ----------------

@app.route("/tasks")
def tasks_page():
    conn = get_conn()
    rows = conn.execute(
        "SELECT t.*, a.account_no, a.bank_name, a.bank_code FROM download_tasks t"
        " JOIN accounts a ON a.id=t.account_id ORDER BY t.id DESC").fetchall()
    events = {
        r["id"]: [dict(x) for x in conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 12",
            (r["id"],))]
        for r in rows
    }
    with _jobs_lock:
        running = any(j["status"] == "running" for j in _jobs)
    conn.close()
    return render_template("tasks.html", tasks=rows, events=events,
                           running=running, d_from=PERIOD_FROM, d_to=PERIOD_TO)


@app.route("/tasks/create", methods=["POST"])
def tasks_create():
    account_id = int(request.form["account_id"])
    date_from = request.form["date_from"]
    date_to = request.form["date_to"]
    tid = create_task(account_id, date_from, date_to)
    _job_add(f"下载任务#{tid}", _run_one, tid)
    flash(f"已创建并启动下载任务 #{tid}")
    return redirect(url_for("tasks_page"))


def _run_one(task_id: int) -> None:
    with BotRunner() as bot:
        bot.run_task(task_id)


@app.route("/tasks/create-all", methods=["POST"])
def tasks_create_all():
    conn = get_conn()
    accts = [r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE enabled=1 ORDER BY id")]
    date_from = request.form.get("date_from", PERIOD_FROM)
    date_to = request.form.get("date_to", PERIOD_TO)
    conn.close()
    tids = [create_task(aid, date_from, date_to) for aid in accts]

    def batch():
        with BotRunner() as bot:
            for tid in tids:
                bot.run_task(tid)

    _job_add(f"批量下载 {len(tids)} 个账户", batch)
    flash(f"已为 {len(tids)} 个账户创建下载任务并开始批量执行")
    return redirect(url_for("tasks_page"))


@app.route("/tasks/<int:tid>/rerun", methods=["POST"])
def tasks_rerun(tid):
    _job_add(f"重跑任务#{tid}", _run_one, tid)
    flash(f"任务 #{tid} 已加入重跑队列（已成功的文件会自动跳过）")
    return redirect(url_for("tasks_page"))


@app.route("/tasks/ingest", methods=["POST"])
def tasks_ingest():
    _job_add("解析入库", ingest_all)
    flash("开始解析全部已下载文件并写入流水库")
    return redirect(url_for("tasks_page"))


# ---------------- 3. 流水库 ----------------

@app.route("/transactions")
def txns_page():
    bank = request.args.get("bank", "")
    direction = request.args.get("direction", "")
    q = request.args.get("q", "")
    sql = ("SELECT t.*, a.account_no, a.bank_name FROM transactions t"
           " JOIN accounts a ON a.id=t.account_id WHERE 1=1")
    params: list = []
    if bank:
        sql += " AND a.bank_code=?"
        params.append(bank)
    if direction:
        sql += " AND t.direction=?"
        params.append(direction)
    if q:
        sql += " AND (t.counterparty LIKE ? OR t.txn_uid LIKE ? OR t.summary LIKE ?)"
        params += [f"%{q}%"] * 3
    sql += " ORDER BY t.txn_date DESC, t.id DESC LIMIT 500"
    conn = get_conn()
    rows = conn.execute(sql, params).fetchall()
    src_stat = conn.execute(
        "SELECT source_types, COUNT(*) c FROM transactions GROUP BY source_types"
    ).fetchall()
    conn.close()
    return render_template("transactions.html", txns=rows, bank=bank,
                           direction=direction, q=q, src_stat=src_stat)


# ---------------- 4. 对账结果 ----------------

@app.route("/matches")
def matches_page():
    conn = get_conn()
    groups = [dict(r) for r in conn.execute(
        "SELECT * FROM match_groups ORDER BY id DESC LIMIT 1000")]
    for g in groups:
        t_ids = json.loads(g["txn_ids"])
        e_ids = json.loads(g["erp_ids"])
        g["txns"] = [dict(r) for r in conn.execute(
            f"SELECT * FROM transactions WHERE id IN ({','.join('?' * len(t_ids))})",
            t_ids)] if t_ids else []
        g["erps"] = [dict(r) for r in conn.execute(
            f"SELECT * FROM erp_records WHERE id IN ({','.join('?' * len(e_ids))})",
            e_ids)] if e_ids else []
    rule_stat = conn.execute(
        "SELECT rule_code, status, COUNT(*) c FROM match_groups GROUP BY rule_code, status"
    ).fetchall()
    total_txn = conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"]
    matched_ids = set()
    for g in conn.execute("SELECT txn_ids FROM match_groups"):
        matched_ids.update(json.loads(g["txn_ids"]))
    matched = len(matched_ids)
    rate = round(100 * matched / total_txn, 2) if total_txn else 0
    conn.close()
    return render_template("matches.html", groups=groups, rule_stat=rule_stat,
                           rate=rate, total_txn=total_txn)


@app.route("/reconcile/run", methods=["POST"])
def reconcile_run():
    _job_add("自动对账", reconcile)
    flash("对账引擎已在后台启动")
    return redirect(url_for("matches_page"))


# ---------------- 5. 异常处理 ----------------

@app.route("/exceptions")
def exceptions_page():
    data = exceptions()
    conn = get_conn()
    resolutions = conn.execute(
        "SELECT * FROM manual_resolutions ORDER BY id DESC").fetchall()
    conn.close()
    return render_template("exceptions.html",
                           bank_only=data["bank_only"], erp_only=data["erp_only"],
                           resolutions=resolutions)


@app.route("/exceptions/match", methods=["POST"])
def exceptions_match():
    txn_ids = [int(x) for x in request.form.getlist("txn_ids")]
    erp_ids = [int(x) for x in request.form.getlist("erp_ids")]
    note = request.form.get("note", "")
    if not txn_ids or not erp_ids:
        flash("请至少各勾选一条银行流水和 ERP 记录")
        return redirect(url_for("exceptions_page"))
    manual_match(txn_ids, erp_ids, note)
    flash(f"人工匹配完成（{len(txn_ids)} 笔流水 ↔ {len(erp_ids)} 笔 ERP），已记忆，下次不再报")
    return redirect(url_for("exceptions_page"))


@app.route("/exceptions/ignore", methods=["POST"])
def exceptions_ignore():
    txn_ids = [int(x) for x in request.form.getlist("txn_ids")]
    note = request.form.get("note", "")
    if not txn_ids:
        flash("请先勾选要忽略的银行流水")
        return redirect(url_for("exceptions_page"))
    manual_ignore(txn_ids, note)
    flash(f"已忽略 {len(txn_ids)} 笔流水并记忆，下次不再报")
    return redirect(url_for("exceptions_page"))


# ---------------- JSON 状态（前端轮询） ----------------

@app.route("/api/jobs")
def api_jobs():
    with _jobs_lock:
        return jsonify(list(reversed(_jobs[-10:])))


def run(host: str = "127.0.0.1", port: int | None = None, **kw) -> None:
    app.run(host=host, port=port or WEB_PORT, threaded=True, **kw)


if __name__ == "__main__":
    run()
