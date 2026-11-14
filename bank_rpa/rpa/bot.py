"""Playwright 下载机器人：按账户/日期区间登录仿真网银下载回单与流水。

能力：
- 已下载文件跳过（download_files 唯一键 + 文件哈希），中途中断后重跑可续
- 每个文件独立重试 3 次（应对仿真站注入的偶发下载失败）
- 会话超时（被踢回登录页）自动重新登录后继续
- 每一步截图留痕，事件写入 task_events，失败任务可单独重跑
"""
from __future__ import annotations

import hashlib
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

from ..config import (
    BANK_BASE_URL,
    DOWNLOAD_DIR,
    DOWNLOAD_TIMEOUT_MS,
    HEADLESS,
    SCREENSHOT_DIR,
    STEP_DELAY_MS,
)
from ..db import get_conn, now
from .flows import BANK_FLOWS


class LoginExpired(Exception):
    """导航/下载过程中被仿真站踢回登录页。"""


class BotRunner:
    def __init__(self, base_url: str | None = None, headless: bool | None = None,
                 slow_ms: int | None = None):
        self.base_url = (base_url or BANK_BASE_URL).rstrip("/")
        self.headless = HEADLESS if headless is None else headless
        self.slow_ms = STEP_DELAY_MS if slow_ms is None else slow_ms
        self._pw = None
        self.browser = None

    # ---------- 生命周期 ----------

    def __enter__(self):
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)
        return self

    def __exit__(self, *exc):
        if self.browser:
            self.browser.close()
        if self._pw:
            self._pw.stop()

    # ---------- 日志/截图 ----------

    def _shot(self, page, task_id: int, tag: str) -> str:
        rel = Path(f"task_{task_id}") / f"{int(time.time() * 1000)}_{tag}.png"
        path = SCREENSHOT_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(path), full_page=False)
        except Exception:
            return ""
        return str(path)

    @staticmethod
    def _event(conn, task_id: int, level: str, message: str, screenshot: str = "") -> None:
        conn.execute(
            "INSERT INTO task_events (task_id, level, message, screenshot, created_at)"
            " VALUES (?,?,?,?,?)",
            (task_id, level, message, screenshot, now()),
        )
        conn.commit()

    @staticmethod
    def _set_task(conn, task_id: int, **fields) -> None:
        fields["updated_at"] = now()
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE download_tasks SET {sets} WHERE id=?",
                     (*fields.values(), task_id))
        conn.commit()

    # ---------- 登录 ----------

    def _on_login_page(self, page) -> bool:
        return "/login" in page.url

    def login(self, page, conn, task_id: int, acct: dict) -> None:
        flow = BANK_FLOWS[acct["bank_code"]]
        cfg = flow["login"]
        page.goto(self.base_url + cfg["url_path"], wait_until="domcontentloaded")
        page.wait_for_timeout(self.slow_ms)
        self._shot(page, task_id, f"login_{acct['bank_code']}_open")

        page.fill(cfg["user_sel"], acct["login_user"])
        if cfg.get("pwd_sel"):
            page.fill(cfg["pwd_sel"], acct["login_secret"])

        extra = cfg.get("extra")
        if extra == "usbk":
            # 仿真 U 盾：页面出现证书提示即视为已插盾
            page.wait_for_selector(".usb-tip", timeout=5000)
            self._shot(page, task_id, "login_usbk_ready")
        elif extra == "smscode":
            page.click(cfg["sms_btn_sel"])
            page.wait_for_timeout(self.slow_ms)
            page.fill(cfg["sms_input_sel"], cfg["sms_code"])
            self._shot(page, task_id, "login_sms_filled")
        elif extra == "qrcode":
            page.wait_for_selector(cfg["qr_wait_sel"], timeout=5000)
            self._shot(page, task_id, "login_qrcode_polling")
            page.wait_for_timeout(self.slow_ms)  # 仿真：手机端“扫码确认”耗时

        page.click(cfg["submit_sel"])
        page.wait_for_url(f"**{flow['home_url']}", timeout=10_000)
        page.wait_for_timeout(self.slow_ms)
        shot = self._shot(page, task_id, "login_success")
        self._event(conn, task_id, "info",
                    f"{acct['bank_name']} 登录成功（方式：{acct['login_type']}）", shot)

    def _ensure_session(self, page, conn, task_id: int, acct: dict) -> None:
        if self._on_login_page(page):
            self._event(conn, task_id, "warn", "检测到会话超时/未登录，自动重新登录…",
                        self._shot(page, task_id, "session_expired"))
            self.login(page, conn, task_id, acct)

    # ---------- 下载 ----------

    def _goto_account(self, page, conn, task_id: int, acct: dict,
                      date_from: str, date_to: str) -> None:
        flow = BANK_FLOWS[acct["bank_code"]]
        self._ensure_session(page, conn, task_id, acct)
        page.goto(
            f"{self.base_url}/{acct['bank_code']}/account/{acct['account_no']}"
            f"?date_from={date_from}&date_to={date_to}",
            wait_until="domcontentloaded",
        )
        page.wait_for_timeout(self.slow_ms)
        if self._on_login_page(page):
            raise LoginExpired("进入账户页时被踢回登录页")
        page.fill(flow["date_from_sel"], date_from)
        page.fill(flow["date_to_sel"], date_to)

    def _record_file(self, conn, task_id: int, acct_id: int, biz_date: str,
                     file_type: str, fmt: str, status: str, file_path: str | None,
                     error: str | None, attempts: int) -> int:
        """按 (账户,日期,类型,格式) upsert 一条 download_files，返回行 id。"""
        row = conn.execute(
            "SELECT id FROM download_files WHERE account_id=? AND biz_date=?"
            " AND file_type=? AND fmt=?",
            (acct_id, biz_date, file_type, fmt)).fetchone()
        file_hash = ""
        if file_path and Path(file_path).exists():
            h = hashlib.sha256()
            h.update(Path(file_path).read_bytes())
            file_hash = h.hexdigest()
        if row:
            conn.execute(
                "UPDATE download_files SET task_id=?, status=?, file_path=?, file_hash=?,"
                " attempts=?, error=?, downloaded_at=? WHERE id=?",
                (task_id, status, file_path, file_hash, attempts, error,
                 now() if status == "success" else None, row["id"]))
            fid = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO download_files (task_id, account_id, biz_date, file_type, fmt,"
                " file_path, file_hash, status, attempts, error, downloaded_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, acct_id, biz_date, file_type, fmt, file_path, file_hash,
                 status, attempts, error, now() if status == "success" else None))
            fid = cur.lastrowid
        conn.commit()
        return fid

    def _click_and_wait_download(self, page, selector: str):
        """点击下载链接，竞速等待 download 事件或 5xx 错误响应。"""
        box: dict = {}

        def on_dl(dl):
            box["dl"] = dl

        def on_resp(resp):
            if resp.status >= 500 and "/download/" in resp.url:
                box["err"] = resp.status

        page.on("download", on_dl)
        page.on("response", on_resp)
        try:
            page.click(selector)
            deadline = time.time() + DOWNLOAD_TIMEOUT_MS / 1000
            while time.time() < deadline:
                if "dl" in box:
                    return box["dl"]
                if "err" in box:
                    raise RuntimeError(f"站点返回 HTTP {box['err']}（偶发故障，自动重试）")
                page.wait_for_timeout(100)
            raise RuntimeError("等待下载事件超时")
        finally:
            page.remove_listener("download", on_dl)
            page.remove_listener("response", on_resp)

    def _download_once(self, page, conn, task_id: int, acct: dict, kind: str,
                       date_from: str, date_to: str, biz_date: str,
                       file_type: str, fmt: str) -> str:
        """单次下载尝试，返回落地路径；失败抛异常。"""
        flow = BANK_FLOWS[acct["bank_code"]]
        self._goto_account(page, conn, task_id, acct, date_from, date_to)
        page.fill(flow["receipt_date_sel"], biz_date)
        page.wait_for_timeout(self.slow_ms)

        download = self._click_and_wait_download(page, flow[f"{kind}_sel"])
        if download.failure():
            raise RuntimeError(f"浏览器下载失败: {download.failure()}")

        suffix = f"{biz_date}_{kind}.{fmt}"
        dest_dir = DOWNLOAD_DIR / acct["bank_code"] / acct["account_no"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{acct['bank_code']}_{acct['account_no']}_{suffix}"
        download.save_as(str(dest))
        self._shot(page, task_id, f"ok_{kind}_{biz_date}")
        return str(dest)

    def _download_with_retry(self, page, conn, task_id: int, acct: dict, kind: str,
                             date_from: str, date_to: str, biz_date: str,
                             file_type: str, fmt: str, max_attempts: int = 3) -> bool:
        """带重试与超时重登的单文件下载，结果落 download_files。"""
        last_err = ""
        for attempt in range(1, max_attempts + 1):
            try:
                path = self._download_once(
                    page, conn, task_id, acct, kind, date_from, date_to,
                    biz_date, file_type, fmt)
                self._record_file(conn, task_id, acct["id"], biz_date,
                                  file_type, fmt, "success", path, None, attempt)
                self._event(conn, task_id, "info",
                            f"下载成功 {file_type}/{fmt} {biz_date}（第 {attempt} 次尝试）")
                return True
            except LoginExpired as exc:
                last_err = str(exc)
            except Exception as exc:
                last_err = str(exc)[:300]
            self._event(conn, task_id, "warn",
                        f"下载失败 {file_type}/{fmt} {biz_date} 第 {attempt}/{max_attempts} 次：{last_err}",
                        self._shot(page, task_id, f"fail_{kind}_{biz_date}_{attempt}"))
            # 失败后若落在登录页，下一轮 ensure_session 会自动重登
            try:
                self._ensure_session(page, conn, task_id, acct)
            except Exception as exc:
                last_err = f"重登失败: {exc}"
        self._record_file(conn, task_id, acct["id"], biz_date, file_type, fmt,
                          "failed", None, last_err, max_attempts)
        self._event(conn, task_id, "error",
                    f"放弃下载 {file_type}/{fmt} {biz_date}：{last_err}")
        return False

    # ---------- 任务执行 ----------

    def run_task(self, task_id: int, force: bool = False) -> dict:
        conn = get_conn()
        task = conn.execute("SELECT * FROM download_tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            conn.close()
            raise ValueError(f"任务不存在: {task_id}")
        acct = conn.execute("SELECT * FROM accounts WHERE id=?",
                            (task["account_id"],)).fetchone()
        acct = dict(acct)
        date_from, date_to = task["date_from"], task["date_to"]

        # 任务计划文件清单：1 个区间流水 + 区间内每个有交易工作日的回单。
        # 回单日从仿真站交易清单推导（首次运行时流水库尚未解析）。
        from ..banksim.builders import load_account_transactions
        from ..config import BANKFILE_DIR
        all_txns = load_account_transactions(acct["bank_code"], acct["account_no"], BANKFILE_DIR)
        workdays = sorted({t["txn_date"] for t in all_txns
                           if date_from <= t["txn_date"] <= date_to})
        from ..data_gen import BANK_PROFILES
        stmt_fmt = BANK_PROFILES[acct["bank_code"]]["statement_fmt"]
        plan = [("statement", "statement", stmt_fmt, date_to)]
        plan += [("receipt", "receipt", "pdf", d) for d in workdays]

        self._set_task(conn, task_id, status="running", message=None,
                       total_files=len(plan), done_files=0)
        self._event(conn, task_id, "info",
                    f"开始下载任务：{acct['bank_name']} {acct['account_no']} "
                    f"{date_from}~{date_to}，计划 {len(plan)} 个文件")

        page = self.browser.new_page(accept_downloads=True)
        ok = True
        done = 0
        try:
            self.login(page, conn, task_id, acct)
            for kind, file_type, fmt, biz_date in plan:
                # 断点续传：已成功且文件仍存在的直接跳过（force 可强制重下）
                row = conn.execute(
                    "SELECT * FROM download_files WHERE account_id=? AND biz_date=?"
                    " AND file_type=? AND fmt=?",
                    (acct["id"], biz_date, file_type, fmt)).fetchone()
                if row and row["status"] == "success" and not force \
                        and row["file_path"] and Path(row["file_path"]).exists():
                    conn.execute("UPDATE download_files SET task_id=? WHERE id=?",
                                 (task_id, row["id"]))
                    conn.commit()
                    self._event(conn, task_id, "info",
                                f"跳过已下载文件 {file_type}/{fmt} {biz_date}")
                    done += 1
                    self._set_task(conn, task_id, done_files=done)
                    continue

                success = self._download_with_retry(
                    page, conn, task_id, acct, kind, date_from, date_to,
                    biz_date, file_type, fmt)
                ok = ok and success
                done += 1
                self._set_task(conn, task_id, done_files=done)
        except Exception as exc:
            ok = False
            self._event(conn, task_id, "error", f"任务异常终止: {exc}",
                        self._shot(page, task_id, "task_abort"))
        finally:
            page.close()

        failed = conn.execute(
            "SELECT COUNT(*) c FROM download_files WHERE task_id=? AND status='failed'",
            (task_id,)).fetchone()["c"]
        self._set_task(
            conn, task_id,
            status="success" if ok else "failed",
            message=None if ok else f"{failed} 个文件下载失败，可在任务页单独重跑",
        )
        self._event(conn, task_id, "info" if ok else "error",
                    f"任务结束，状态 {'成功' if ok else '部分失败'}")
        conn.close()
        return {"task_id": task_id, "success": ok, "planned": len(plan), "failed": failed}


def create_task(account_id: int, date_from: str, date_to: str) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO download_tasks (account_id, date_from, date_to, status,"
        " created_at, updated_at) VALUES (?,?,?,'pending',?,?)",
        (account_id, date_from, date_to, now(), now()))
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def run_tasks(task_ids: list[int] | None = None, force: bool = False,
              base_url: str | None = None) -> list[dict]:
    """执行一批任务（或全部 pending/failed 任务）。"""
    conn = get_conn()
    if task_ids is None:
        rows = conn.execute(
            "SELECT id FROM download_tasks WHERE status IN ('pending','failed')"
            " ORDER BY id").fetchall()
        task_ids = [r["id"] for r in rows]
    conn.close()
    results = []
    with BotRunner(base_url=base_url) as bot:
        for tid in task_ids:
            results.append(bot.run_task(tid, force=force))
    return results
