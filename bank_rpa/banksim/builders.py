"""仿真网银下载文件构建器：CSV/OFX 流水 + PDF 回单（各银行版式不同）。

同时也是解析器的“另一端”：构建时刻意使用各银行真实风格的日期/金额写法，
由 ingest 层负责归一化。
"""
from __future__ import annotations

import io
import json
import csv

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
PDF_FONT = "STSong-Light"

DIR_CN = {"in": "贷", "out": "借"}


# ---------------- CSV（三家布局） ----------------

def build_csv(bank_code: str, account: dict, txns: list[dict], date_from: str, date_to: str) -> bytes:
    rows = [t for t in txns if date_from <= t["txn_date"] <= date_to]
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    if bank_code == "icbc":
        # 工行：GBK 风格中文表头，借贷标记列，金额带千分位（引号包裹）
        writer.writerow(["交易日期", "交易时间", "借方发生额", "贷方发生额", "对方户名", "摘要", "交易流水号"])
        for t in rows:
            debit = f"{t['amount']:,.2f}" if t["direction"] == "out" else ""
            credit = f"{t['amount']:,.2f}" if t["direction"] == "in" else ""
            writer.writerow([t["txn_date"].replace("-", ""), "093015", debit, credit,
                             t["counterparty"], t["summary"], t["txn_uid"]])
    elif bank_code == "cmb":
        # 招行：英文表头，带符号金额，斜杠日期
        writer.writerow(["Trans Date", "Amount", "Balance", "Counterparty", "Memo", "Reference"])
        for t in rows:
            signed = -t["amount"] if t["direction"] == "out" else t["amount"]
            d = "/".join(reversed(t["txn_date"].split("-")))
            writer.writerow([d, f"{signed:.2f}", "0.00",
                             t["counterparty"], t["summary"], t["txn_uid"]])
    elif bank_code == "citic":
        # 中信：BOM + 中文表头，方向列“收入/支出”，金额带￥
        writer.writerow(["记账日期", "收支标志", "交易金额", "对方名称", "交易摘要", "凭证号"])
        for t in rows:
            flag = "收入" if t["direction"] == "in" else "支出"
            d = f"{t['txn_date'][:4]}/{t['txn_date'][5:7]}/{t['txn_date'][8:]}"
            writer.writerow([d, flag, f"￥{t['amount']:,.2f}",
                             t["counterparty"], t["summary"], t["txn_uid"]])
    else:
        raise ValueError(f"{bank_code} 不提供 CSV 流水")
    data = buf.getvalue().encode("utf-8")
    if bank_code == "citic":
        data = b"\xef\xbb\xbf" + data
    return data


# ---------------- OFX（建行/中行） ----------------

def build_ofx(bank_code: str, account: dict, txns: list[dict], date_from: str, date_to: str) -> bytes:
    rows = [t for t in txns if date_from <= t["txn_date"] <= date_to]
    fid = {"ccb": "105", "boc": "104"}[bank_code]
    lines = [
        "OFXHEADER:100", "DATA:OFXSGML", "VERSION:160", "SECURITY:NONE",
        "ENCODING:UTF-8", "CHARSET:NONE", "COMPRESSION:NONE", "OLDFILEUID:NONE",
        "NEWFILEUID:NONE", "",
        "<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>CNY",
        f"<BANKACCTFROM><BANKID>{fid}</BANKID><ACCTID>{account['account_no']}</ACCTID><ACCTTYPE>CHECKING</BANKACCTFROM>",
        "<BANKTRANLIST>",
        f"<DTSTART>{date_from.replace('-', '')}000000",
        f"<DTEND>{date_to.replace('-', '')}000000",
    ]
    for t in rows:
        trntype = "CREDIT" if t["direction"] == "in" else "DEBIT"
        signed = t["amount"] if t["direction"] == "in" else -t["amount"]
        dt = t["txn_date"].replace("-", "") + "093015"
        lines += [
            "<STMTTRN>",
            f"<TRNTYPE>{trntype}",
            f"<DTPOSTED>{dt}",
            f"<TRNAMT>{signed:.2f}",
            f"<FITID>{t['txn_uid']}",
            f"<NAME>{t['counterparty']}",
            f"<MEMO>{t['summary']}",
            "</STMTTRN>",
        ]
    total_in = sum(t["amount"] for t in rows if t["direction"] == "in")
    total_out = sum(t["amount"] for t in rows if t["direction"] == "out")
    lines += [
        "</BANKTRANLIST>",
        f"<LEDGERBAL><BALAMT>{total_in - total_out:.2f}</BALAMT><DTASOF>{date_to.replace('-', '')}000000</LEDGERBAL>",
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>",
    ]
    return "\r\n".join(lines).encode("utf-8")


# ---------------- PDF 回单（五家版式） ----------------

_RECEIPT_TITLE = {
    "icbc": "中国工商银行网上银行电子回单",
    "cmb": "招商银行企业网上银行 付款/收款回单",
    "ccb": "中国建设银行电子转账回单",
    "boc": "中国银行电子回单（对公业务）",
    "citic": "中信银行网上交易回单凭证",
}
_LABEL_SETS = {
    "icbc":  dict(uid="交易流水号", date="交易日期", direction="借贷标志", amount="交易金额", cp="对方户名", summary="摘要"),
    "cmb":   dict(uid="参考号",     date="记账日期", direction="交易类型", amount="发生额",   cp="交易对手", summary="交易摘要"),
    "ccb":   dict(uid="凭证编号",   date="交易时间", direction="收付标志", amount="金额",     cp="对方账户名称", summary="用途"),
    "boc":   dict(uid="业务参考号", date="成交日期", direction="业务类型", amount="交易金额", cp="付款人/收款人", summary="附言"),
    "citic": dict(uid="凭证号",     date="交易日期", direction="收支类型", amount="人民币金额", cp="对方名称", summary="交易摘要"),
}
_DIR_TEXT = {
    "icbc": {"in": "贷方", "out": "借方"},
    "cmb": {"in": "贷记", "out": "借记"},
    "ccb": {"in": "收款", "out": "付款"},
    "boc": {"in": "收入", "out": "支出"},
    "citic": {"in": "收入", "out": "支出"},
}
_DATE_FMT = {
    "icbc": lambda d: d.replace("-", ""),
    "cmb": lambda d: "/".join(reversed(d.split("-"))),
    "ccb": lambda d: d + " 09:30:15",
    "boc": lambda d: f"{d[:4]}年{int(d[5:7])}月{int(d[8:])}日",
    "citic": lambda d: d,
}
_AMT_FMT = {
    "icbc": lambda a: f"￥{a:,.2f}",
    "cmb": lambda a: f"{a:,.2f} CNY",
    "ccb": lambda a: f"人民币 {a:,.2f} 元",
    "boc": lambda a: f"CNY {a:.2f}",
    "citic": lambda a: f"￥{a:.2f}",
}


def build_pdf_receipt(bank_code: str, account: dict, day_txns: list[dict], biz_date: str) -> bytes:
    """单日回单 PDF：一个文件可包含多笔交易块。"""
    lab = _LABEL_SETS[bank_code]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 20 * mm

    def line(text: str, size: int = 10, gap: float = 7 * mm):
        nonlocal y
        if y < 20 * mm:
            c.showPage()
            y = height - 20 * mm
        c.setFont(PDF_FONT, size)
        c.drawString(20 * mm, y, text)
        y -= gap

    line(_RECEIPT_TITLE[bank_code], size=15, gap=10 * mm)
    line(f"账号：{account['account_no']}    户名：{account['entity']}    回单日期：{biz_date}")
    line("-" * 64, gap=8 * mm)

    for idx, t in enumerate(day_txns, 1):
        line(f"【第 {idx} 笔】", size=11)
        line(f"{lab['uid']}：{t['txn_uid']}")
        line(f"{lab['date']}：{_DATE_FMT[bank_code](t['txn_date'])}")
        line(f"{lab['direction']}：{_DIR_TEXT[bank_code][t['direction']]}")
        line(f"{lab['amount']}：{_AMT_FMT[bank_code](t['amount'])}")
        line(f"{lab['cp']}：{t['counterparty']}")
        line(f"{lab['summary']}：{t['summary']}")
        line("-" * 64, gap=8 * mm)

    line("（本回单为本地仿真系统生成，仅用于 RPA 联调，不具备任何银行效力）", size=8)
    c.showPage()
    c.save()
    return buf.getvalue()


def load_account_transactions(bank_code: str, account_no: str, base_dir) -> list[dict]:
    p = base_dir / bank_code / account_no / "transactions.json"
    return json.loads(p.read_text(encoding="utf-8"))
