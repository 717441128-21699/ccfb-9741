"""全局配置：路径、端口、业务参数。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("BANK_RPA_DATA", BASE_DIR / "data"))

DB_PATH = DATA_DIR / "bank_rpa.db"
DOWNLOAD_DIR = DATA_DIR / "downloads"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
BANKFILE_DIR = DATA_DIR / "bank_files"
ERP_DIR = DATA_DIR / "erp"
LOG_DIR = DATA_DIR / "logs"

for _d in (DATA_DIR, DOWNLOAD_DIR, SCREENSHOT_DIR, BANKFILE_DIR, ERP_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 仿真网银 / 后台端口
BANK_PORT = int(os.environ.get("BANK_RPA_BANK_PORT", "5001"))
WEB_PORT = int(os.environ.get("BANK_RPA_WEB_PORT", "5000"))
BANK_BASE_URL = os.environ.get("BANK_RPA_BANK_URL", f"http://127.0.0.1:{BANK_PORT}")

# Playwright
HEADLESS = os.environ.get("BANK_RPA_HEADLESS", "0") == "1"
DOWNLOAD_TIMEOUT_MS = 15_000
STEP_DELAY_MS = 150  # 模拟人工操作节奏

# 对账参数
TAIL_TOLERANCE = 0.10          # 尾差自动容忍额度（元）
DATE_WINDOW_DAYS = 2           # 自动匹配允许的日期偏差
AUTO_MATCH_SCORE = 70          # 1:1 自动匹配分数阈值
GROUP_TOLERANCE_RATE = 0.005   # 多笔组合匹配总额容忍比例

# 数据生成区间（演示账期）
PERIOD_FROM = "2026-10-01"
PERIOD_TO = "2026-10-31"
RNG_SEED = 20261114
