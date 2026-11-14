"""命令行入口。

用法:
  py -3.12 -m bank_rpa.cli generate       # 生成测试数据（重置库）
  py -3.12 -m bank_rpa.cli bankweb        # 启动 5 家仿真网银站点
  py -3.12 -m bank_rpa.cli admin          # 启动对账后台
  py -3.12 -m bank_rpa.cli download       # 为全部启用账户创建并执行下载任务
  py -3.12 -m bank_rpa.cli ingest         # 解析下载文件并写入流水库
  py -3.12 -m bank_rpa.cli reconcile      # 运行自动对账并打印指标
  py -3.12 -m bank_rpa.cli demo           # 一键全流程（生成→下载→解析→对账）
  py -3.12 -m bank_rpa.cli test           # 运行单元测试
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.request

from .config import BANK_PORT, PERIOD_FROM, PERIOD_TO, WEB_PORT


def _wait_port(port: int, timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
            return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"端口 {port} 服务未就绪")


def cmd_generate() -> None:
    from .data_gen.generator import generate
    print(json.dumps(generate(), ensure_ascii=False, indent=2))


def cmd_bankweb() -> None:
    from .banksim.server import run
    print(f"仿真网银站点: http://127.0.0.1:{BANK_PORT}/ （Ctrl+C 退出）")
    run()


def cmd_admin() -> None:
    from .web.app import run
    print(f"对账后台: http://127.0.0.1:{WEB_PORT}/ （Ctrl+C 退出）")
    run()


def cmd_download(force: bool = False) -> None:
    from .rpa.bot import create_task, run_tasks
    from .db import get_conn
    conn = get_conn()
    accts = [r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE enabled=1 ORDER BY id")]
    existing = {r["id"] for r in conn.execute(
        "SELECT account_id id FROM download_tasks WHERE date_from=? AND date_to=?",
        (PERIOD_FROM, PERIOD_TO))}
    conn.close()
    tids = [create_task(a, PERIOD_FROM, PERIOD_TO) for a in accts if a not in existing]
    print(f"新建 {len(tids)} 个下载任务，开始执行（已存在任务跳过创建，可在网页重跑）")
    results = run_tasks(tids, force=force)
    print(json.dumps(results, ensure_ascii=False, indent=2))


def cmd_ingest() -> None:
    from .ingest.pipeline import ingest_all
    print(json.dumps(ingest_all(), ensure_ascii=False, indent=2))


def cmd_reconcile() -> None:
    from .reconcile.engine import reconcile
    print(json.dumps(reconcile(), ensure_ascii=False, indent=2))


def cmd_demo() -> None:
    """端到端：生成数据 → 起仿真站 → RPA 下载 → 解析 → 对账 → 指标校验。"""
    from .banksim.server import app as bank_app
    from .ingest.pipeline import ingest_all
    from .reconcile.engine import reconcile
    from .rpa.bot import create_task, run_tasks
    from .data_gen.generator import generate

    print("=" * 64)
    print("步骤 1/5 生成测试数据（22 账户 / 5 银行 / 含尾差·简写·拆分·单边账）")
    print("=" * 64)
    summary = generate()
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    print("=" * 64)
    print("步骤 2/5 启动仿真网银站点（线程内）")
    print("=" * 64)
    import threading
    from werkzeug.serving import make_server
    srv = make_server("127.0.0.1", BANK_PORT, bank_app, threaded=True)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _wait_port(BANK_PORT)
    print(f"仿真站已就绪: http://127.0.0.1:{BANK_PORT}/")

    try:
        print("=" * 64)
        print("步骤 3/5 Playwright 机器人执行全部下载任务")
        print("=" * 64)
        from .db import get_conn
        conn = get_conn()
        accts = [r["id"] for r in conn.execute(
            "SELECT id FROM accounts WHERE enabled=1 ORDER BY id")]
        conn.close()
        tids = [create_task(a, PERIOD_FROM, PERIOD_TO) for a in accts]
        results = run_tasks(tids)
        failed_files = sum(1 for r in results if not r["success"])
        print(f"任务 {len(results)} 个，含失败文件的任务 {failed_files} 个（均已自动重试）")

        # 再跑一遍验证断点续传：应全部秒过、无重复下载
        print("-" * 64)
        print("断点续传验证：再次执行相同任务（期望全部跳过，不产生重复文件）")
        again = run_tasks(tids)
        print(json.dumps(again, ensure_ascii=False))

        print("=" * 64)
        print("步骤 4/5 解析 PDF/CSV/OFX → 统一流水结构 → 回单/流水去重入库")
        print("=" * 64)
        ing = ingest_all()
        print(json.dumps(ing, ensure_ascii=False, indent=2))

        print("=" * 64)
        print("步骤 5/5 自动对账 + 指标校验（目标自动匹配率 ≥ 95%）")
        print("=" * 64)
        stat = reconcile()
        print(json.dumps(stat, ensure_ascii=False, indent=2))
        rate = stat["auto_rate"]
        print("-" * 64)
        if rate >= 95:
            print(f"✅ 自动对账率 {rate}% 达标（≥95%），剩余 {stat['unmatched_txn']} 笔银行流水、"
                  f"{stat['unmatched_erp']} 笔 ERP 单据均在异常处理页可见")
        else:
            print(f"❌ 自动对账率 {rate}% 未达标，需要调参")
        print(f"后台地址: http://127.0.0.1:{WEB_PORT}/ （运行: py -3.12 -m bank_rpa.cli admin）")
    finally:
        srv.shutdown()


def cmd_test() -> None:
    raise SystemExit(subprocess.call(
        [sys.executable, "-m", "pytest", "-v", "-s", "tests"], cwd="."))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    cmd = sys.argv[1]
    force = "--force" in sys.argv
    {
        "generate": cmd_generate,
        "bankweb": cmd_bankweb,
        "admin": cmd_admin,
        "download": lambda: cmd_download(force=force),
        "ingest": cmd_ingest,
        "reconcile": cmd_reconcile,
        "demo": cmd_demo,
        "test": cmd_test,
    }.get(cmd, lambda: print(__doc__))()


if __name__ == "__main__":
    main()
