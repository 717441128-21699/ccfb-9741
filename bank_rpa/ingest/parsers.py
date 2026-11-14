"""下载文件解析器：PDF 回单 / CSV 流水 / OFX 流水 → 统一交易字典列表。

统一结构字段：
txn_uid, txn_date(ISO), direction(in/out), amount(正浮点), counterparty, summary,
source_type(receipt/statement), source_file, raw_line
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from pypdf import PdfReader

from ..common import parse_amount, parse_date

# PDF 各银行标签（与 builders._LABEL_SETS 对应，按标签语义提取）
_PDF_LABELS = {
    "icbc":  dict(uid="交易流水号", date="交易日期", direction="借贷标志", amount="交易金额", cp="对方户名", summary="摘要"),
    "cmb":   dict(uid="参考号",     date="记账日期", direction="交易类型", amount="发生额",   cp="交易对手", summary="交易摘要"),
    "ccb":   dict(uid="凭证编号",   date="交易时间", direction="收付标志", amount="金额",     cp="对方账户名称", summary="用途"),
    "boc":   dict(uid="业务参考号", date="成交日期", direction="业务类型", amount="交易金额", cp="付款人/收款人", summary="附言"),
    "citic": dict(uid="凭证号",     date="交易日期", direction="收支类型", amount="人民币金额", cp="对方名称", summary="交易摘要"),
}
_IN_WORDS = ("贷", "收", "入")
_OUT_WORDS = ("借", "付", "出")


def _extract_label(text: str, label: str) -> str | None:
    # 标签可能含 / 等特殊字符，用 re.escape；值取到行尾或下一个标签前
    m = re.search(re.escape(label) + r"[：:]\s*([^\n]+)", text)
    return m.group(1).strip() if m else None


def _direction_from_text(text: str) -> str:
    return "in" if any(w in text for w in _IN_WORDS) else "out"


def parse_pdf(bank_code: str, file_path: str | Path) -> list[dict]:
    labels = _PDF_LABELS[bank_code]
    reader = PdfReader(str(file_path))
    full = "\n".join(page.extract_text() or "" for page in reader.pages)
    # 以流水号标签切块，每块一笔交易
    chunks = re.split(r"(?=" + re.escape(labels["uid"]) + r"[：:])", full)
    out: list[dict] = []
    for chunk in chunks:
        uid = _extract_label(chunk, labels["uid"])
        if not uid:
            continue
        d_raw = _extract_label(chunk, labels["date"])
        amt_raw = _extract_label(chunk, labels["amount"])
        dir_raw = _extract_label(chunk, labels["direction"]) or ""
        out.append({
            "txn_uid": uid,
            "txn_date": parse_date(d_raw),
            "direction": _direction_from_text(dir_raw),
            "amount": parse_amount(amt_raw),
            "counterparty": _extract_label(chunk, labels["cp"]) or "",
            "summary": _extract_label(chunk, labels["summary"]) or "",
            "source_type": "receipt",
            "source_file": str(file_path),
            "raw_line": " | ".join(x for x in (uid, d_raw, dir_raw, amt_raw) if x),
        })
    return out


# ---------------- OFX ----------------

_OFX_TAG = re.compile(r"<(/?[A-Z0-9]+)>([^<\r\n]*)")


def parse_ofx(bank_code: str, file_path: str | Path) -> list[dict]:
    raw = Path(file_path).read_bytes()
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    out: list[dict] = []
    cur: dict | None = None
    acct_id = ""
    m = re.search(r"<ACCTID>([^<]+)", text)
    if m:
        acct_id = m.group(1).strip()
    for tag, val in _OFX_TAG.findall(text):
        if tag == "STMTTRN":
            cur = {}
        elif tag == "/STMTTRN":
            if cur and "_uid" in cur:
                cur.update({
                    "txn_date": parse_date(cur.pop("_date")),
                    "direction": "in" if cur.pop("_type") == "CREDIT" else "out",
                    "amount": parse_amount(cur.pop("_amt")),
                    "txn_uid": cur.pop("_uid"),
                    "counterparty": cur.pop("_name", ""),
                    "summary": cur.pop("_memo", ""),
                    "source_type": "statement",
                    "source_file": str(file_path),
                    "raw_line": f"{acct_id} OFX {cur.get('txn_uid', '')}",
                })
                out.append(cur)
            cur = None
        elif cur is not None:
            if tag == "TRNTYPE":
                cur["_type"] = val.strip()
            elif tag == "DTPOSTED":
                cur["_date"] = val.strip()[:8]
            elif tag == "TRNAMT":
                cur["_amt"] = val.strip()
            elif tag == "FITID":
                cur["_uid"] = val.strip()
            elif tag == "NAME":
                cur["_name"] = val.strip()
            elif tag == "MEMO":
                cur["_memo"] = val.strip()
    return out


# ---------------- CSV（三家布局） ----------------

def parse_csv(bank_code: str, file_path: str | Path) -> list[dict]:
    raw = Path(file_path).read_bytes()
    for enc in ("utf-8-sig", "gbk", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []
    out: list[dict] = []

    if bank_code == "icbc":
        # 交易日期,交易时间,借方发生额,贷方发生额,对方户名,摘要,交易流水号
        for r in rows[1:]:
            if len(r) < 7 or not r[6].strip():
                continue
            direction = "out" if r[2].strip() else "in"
            amt_raw = r[2] or r[3]
            out.append({
                "txn_uid": r[6].strip(), "txn_date": parse_date(r[0]),
                "direction": direction, "amount": parse_amount(amt_raw),
                "counterparty": r[4].strip(), "summary": r[5].strip(),
                "source_type": "statement", "source_file": str(file_path),
                "raw_line": ",".join(r),
            })
    elif bank_code == "cmb":
        # Trans Date,Amount,Balance,Counterparty,Memo,Reference (DD/MM/YYYY, 带符号金额)
        # 户名/摘要可能含逗号，按“前 3 列固定 + 末列为 Reference + 倒数第 2 列为 Memo”定位
        for r in rows[1:]:
            if len(r) < 6 or not r[-1].strip():
                continue
            ref = r[-1].strip()
            memo = r[-2].strip()
            counterparty = ",".join(r[3:-2]).strip()
            signed = parse_amount(r[1])
            direction = "in" if float(str(r[1]).replace(",", "")) >= 0 else "out"
            out.append({
                "txn_uid": ref, "txn_date": parse_date(r[0]),
                "direction": direction, "amount": signed,
                "counterparty": counterparty, "summary": memo,
                "source_type": "statement", "source_file": str(file_path),
                "raw_line": ",".join(r),
            })
    elif bank_code == "citic":
        # 记账日期,收支标志,交易金额,对方名称,交易摘要,凭证号
        for r in rows[1:]:
            if len(r) < 6 or not r[5].strip():
                continue
            out.append({
                "txn_uid": r[5].strip(), "txn_date": parse_date(r[0]),
                "direction": "in" if "收" in r[1] else "out",
                "amount": parse_amount(r[2]),
                "counterparty": r[3].strip(), "summary": r[4].strip(),
                "source_type": "statement", "source_file": str(file_path),
                "raw_line": ",".join(r),
            })
    else:
        raise ValueError(f"{bank_code} 无 CSV 解析器")
    return out


def parse_file(bank_code: str, fmt: str, file_path: str | Path) -> list[dict]:
    fmt = fmt.lower()
    if fmt == "pdf":
        return parse_pdf(bank_code, file_path)
    if fmt == "csv":
        return parse_csv(bank_code, file_path)
    if fmt == "ofx":
        return parse_ofx(bank_code, file_path)
    raise ValueError(f"不支持的文件格式: {fmt}")
