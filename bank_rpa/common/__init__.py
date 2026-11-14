"""通用归一化工具：金额、日期、户名、摘要。"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# ---- 金额 ----------------------------------------------------------------

_CN_AMOUNT_UNIT = {"万": 10_000, "千": 1_000, "百": 100}


def parse_amount(raw) -> float:
    """把各种金额写法统一成正数 float（元）。

    支持: 12,345.67 / ￥12345.67 / 1.23万 / 12345.67元 / (1,200.00) 负数括号
    """
    if raw is None:
        raise ValueError("空金额")
    if isinstance(raw, (int, float, Decimal)):
        return round(abs(float(raw)), 2)
    s = str(raw).strip()
    if not s:
        raise ValueError("空金额")
    neg = s.startswith("(") and s.endswith(")")
    s = s.replace("(", "").replace(")", "")
    s = re.sub(r"[￥¥$€£\s,，元人民币RMB¥A-Za-z]", "", s, flags=re.IGNORECASE)
    multiplier = 1.0
    m = re.search(r"([万千百])$", s)
    if m:
        multiplier = _CN_AMOUNT_UNIT[m.group(1)]
        s = s[: m.start()]
    try:
        val = float(Decimal(s) * Decimal(str(multiplier)))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"无法解析金额: {raw!r}") from exc
    if neg:
        val = -val
    return round(abs(val), 2)


def money_key(amount: float, tolerance: float = 0.10) -> int:
    """金额分桶键：尾差 tolerance 内落到同一桶，减少候选集。"""
    return int(math.floor((amount + tolerance) / (2 * tolerance + 1e-9)))


# ---- 日期 ----------------------------------------------------------------

_DATE_PATTERNS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d",
    "%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S",
)


def parse_date(raw) -> str:
    """统一输出 ISO 日期 YYYY-MM-DD。"""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw.isoformat()
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    s = str(raw).strip()
    if not s:
        raise ValueError("空日期")
    s = s.split()[0] if " " in s and "-" in s else s
    for pat in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, pat).date().isoformat()
        except ValueError:
            continue
    # 中文 2026年10月1日
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", s)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
    raise ValueError(f"无法解析日期: {raw!r}")


def days_between(d1: str, d2: str) -> int:
    a = datetime.strptime(d1, "%Y-%m-%d").date()
    b = datetime.strptime(d2, "%Y-%m-%d").date()
    return abs((a - b).days)


# ---- 户名 / 摘要 ----------------------------------------------------------

_COMPANY_SUFFIX = re.compile(r"(股份有限|有限责任|有限)?(公司|集团|银行|厂|店|中心|事务所|合伙企业|合作社)")
_CORP_WORDS = re.compile(r"(有限责任|股份有限|有限公司|有限|股份|集团|公司|总公司|分公司)")
_NOISE = re.compile(r"[\s（）()【】\[\]\-—_·.。,，、:：;；'\"“”‘’#]")
_BRACKET = re.compile(r"[（(]([^）)]*)[）)]")


def normalize_name(raw) -> str:
    """户名归一化：去空白标点、括号补充名、公司类后缀，用于简写匹配。"""
    if not raw:
        return ""
    s = str(raw).strip()
    # 括号内容往往是地域/备注，去掉主名中的括号块
    s = _BRACKET.sub("", s)
    s = _NOISE.sub("", s)
    s = _CORP_WORDS.sub("", s)
    return s.lower()


def name_similarity(a: str, b: str) -> float:
    """0~1 相似度：归一化后互为子串给高分，否则按字符集合 Jaccard。"""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        short, long_ = (na, nb) if len(na) <= len(nb) else (nb, na)
        return 0.75 + 0.25 * len(short) / len(long_)
    sa, sb = set(na), set(nb)
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


_SUMMARY_PREFIX = re.compile(
    r"^(网上银行|网银|手机银行|跨行|同城|实时|普通|加急|转账|汇款|汇兑|支付|收款|付款|代缴|代发)+[:：]?"
)
_SUMMARY_TAIL = re.compile(r"[0-9a-zA-Z]{6,}$")  # 末尾流水号


def normalize_summary(raw) -> str:
    if not raw:
        return ""
    s = str(raw).strip()
    prev = None
    while prev != s:
        prev = s
        s = _SUMMARY_PREFIX.sub("", s)
    s = _SUMMARY_TAIL.sub("", s)
    return _NOISE.sub("", s).lower()


def summary_similarity(a: str, b: str) -> float:
    na, nb = normalize_summary(a), normalize_summary(b)
    if not na or not nb:
        return 0.3  # 摘要缺失不一票否决
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.85
    sa, sb = set(na), set(nb)
    union = len(sa | sb)
    return len(sa & sb) / union if union else 0.0


def fingerprint(conn, account_id: int, direction: str, counterparty: str, amount: float) -> str:
    """人工处理指纹：账户+方向+归一户名+金额到分。"""
    return f"{account_id}|{direction}|{normalize_name(counterparty)}|{round(amount, 2):.2f}"
