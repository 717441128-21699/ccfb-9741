"""五家本地仿真网银站点（单 Flask 应用，按 URL 前缀区分银行）。

- 登录方式：账号密码 / U盾 / 短信验证码 / 扫码，页面 DOM 结构各不相同
- 会话超时：按账户配置 timeout_minutes，超时后下载/查询 302 回登录页
- 下载失败注入：确定性（按 账号+日期+文件类型 哈希），首次请求失败、重试即成功，
  用于验证机器人的重试与断点续传能力。
"""
from __future__ import annotations

import hashlib
import time
from functools import wraps

from flask import (
    Flask, abort, g, make_response, redirect, render_template_string,
    request, session, url_for,
)

from ..config import BANKFILE_DIR, BANK_PORT
from ..data_gen import BANK_PROFILES
from ..db import get_conn
from .builders import build_csv, build_ofx, build_pdf_receipt, load_account_transactions

app = Flask(__name__)
app.secret_key = "local-simulation-only-not-a-real-bank"

BANK_ORDER = ["icbc", "cmb", "ccb", "boc", "citic"]

# 故障注入记忆：命中的 key 只在“首次请求”失败，重试即成功（模拟偶发故障）
_fail_seen: set[str] = set()

# 各银行登录页/首页模板（DOM 刻意做成五种风格） ------------------------------

_LOGIN_TPL = {
    "icbc": """
    <div class="panel icbc"><h2>中国工商银行 · 企业网银登录</h2>
      <form method="post" action="{{ url_for('login', bank=bank) }}">
        <label>卡号/用户名</label><input name="username" id="userName">
        <label>登录密码</label><input name="password" type="password" id="loginPwd">
        <button type="submit" id="btnLogin">登 录</button>
      </form><p class="err">{{ err }}</p></div>""",
    "cmb": """
    <div class="panel cmb"><h2>招商银行企业银行 · UKEY 登录</h2>
      <form method="post" action="{{ url_for('login', bank=bank) }}">
        <label>UKEY 编号</label><input name="username" class="ukey-user">
        <label>UKEY PIN 码</label><input name="password" type="password" class="ukey-pin">
        <div class="usb-tip">请插入招商银行 U-BANK 并确认证书</div>
        <button type="submit" class="ukey-submit">证书登录</button>
      </form><p class="err">{{ err }}</p></div>""",
    "ccb": """
    <div class="panel ccb"><h2>中国建设银行 · 短信验证登录</h2>
      <form method="post" action="{{ url_for('login', bank=bank) }}">
        <label>客户识别号</label><input name="username" id="custNo">
        <label>登录密码</label><input name="password" type="password" id="pwd">
        <label>短信验证码</label>
        <div class="sms-row"><input name="smscode" id="smsCode" maxlength="6">
          <button type="button" id="btnSendSms">获取验证码</button></div>
        <div class="sms-hint">仿真验证码固定为 <b>888888</b>（点击按钮后显示）</div>
        <button type="submit" id="ccbLoginBtn">安全登录</button>
      </form><p class="err">{{ err }}</p></div>""",
    "boc": """
    <div class="panel boc"><h2>中国银行企业网银 · 扫码登录</h2>
      <form method="post" action="{{ url_for('login', bank=bank) }}">
        <label>绑定账号</label><input name="username" id="bocUser">
        <div class="qrbox" id="qrBox"><div class="qrfake">▚▞▚ 仿真二维码 ▞▚▞</div>
          <div class="qr-tip">打开中行企业手机银行 APP 扫码确认</div></div>
        <input type="hidden" name="qrsigned" value="1">
        <button type="submit" id="qrConfirm">我已在手机上确认</button>
      </form><p class="err">{{ err }}</p></div>""",
    "citic": """
    <div class="panel citic"><h2>中信银行 · 公司网银</h2>
      <form method="post" action="{{ url_for('login', bank=bank) }}">
        <table class="login-tbl">
          <tr><td>操作员号</td><td><input name="username" data-test="citic-user"></td></tr>
          <tr><td>登录密码</td><td><input name="password" type="password" data-test="citic-pwd"></td></tr>
        </table>
        <button type="submit" data-test="citic-submit">登录网银</button>
      </form><p class="err">{{ err }}</p></div>""",
}

_BASE = """
<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>{{ title }}</title>
<style>
body{font-family:'Microsoft YaHei',sans-serif;margin:0;background:#f2f4f7;color:#222}
.top{background:{{ color }};color:#fff;padding:14px 28px;font-size:18px;font-weight:bold}
.wrap{max-width:1080px;margin:24px auto;background:#fff;padding:24px;border-radius:8px;box-shadow:0 1px 6px #ccc}
.panel{max-width:420px;margin:60px auto;background:#fff;padding:30px;border-radius:8px;box-shadow:0 2px 10px #bbb}
h2{margin-top:0;font-size:17px}label{display:block;margin:12px 0 4px;color:#555;font-size:13px}
input{width:100%;padding:9px;border:1px solid #bbb;border-radius:4px;box-sizing:border-box}
button{margin-top:18px;background:{{ color }};color:#fff;border:0;padding:10px 26px;border-radius:4px;cursor:pointer;font-size:14px}
.err{color:#c00;font-size:13px;min-height:18px}.usb-tip,.sms-hint,.qr-tip{font-size:12px;color:#888;margin:8px 0}
.sms-row{display:flex;gap:8px}.sms-row button{margin:0;padding:9px 12px;white-space:nowrap;font-size:12px}
.qrfake{background:#111;color:#7CFC00;text-align:center;padding:34px 10px;font-size:15px;letter-spacing:3px;border-radius:6px}
.login-tbl td{padding:6px}.login-tbl input{width:240px}
table.grid{border-collapse:collapse;width:100%;margin-top:14px;font-size:13px}
table.grid th,table.grid td{border:1px solid #ddd;padding:8px 10px;text-align:left}
table.grid th{background:#f5f7fa}.tag{display:inline-block;padding:2px 8px;border-radius:10px;background:#e8f0fe;color:#1a56c4;font-size:12px}
.bar{display:flex;gap:14px;align-items:flex-end;flex-wrap:wrap;margin-bottom:10px}
.bar label{margin:0 0 4px}.bar input{width:150px}
a.btn{display:inline-block;background:{{ color }};color:#fff;padding:7px 14px;border-radius:4px;text-decoration:none;font-size:13px}
.meta{font-size:12px;color:#999;text-align:right;margin-top:20px}
.nav a{margin-right:16px;color:{{ color }};text-decoration:none;font-size:13px}
</style></head><body>
<div class="top">{{ bank_name }}（本地仿真环境 · 禁止用于真实业务）</div>
<div class="wrap">{{ body|safe }}</div>
</body></html>"""

_HOME_TPL = """
<div class="nav"><a href="{{ url_for('home', bank=bank) }}">账户首页</a>
  <a href="{{ url_for('logout', bank=bank) }}">退出登录</a></div>
<h2>账户列表 <span class="tag">操作员 {{ user }}</span></h2>
<table class="grid"><tr><th>账号</th><th>户名</th><th>登录方式</th><th>会话超时(分)</th><th>操作</th></tr>
{% for a in accounts %}
<tr><td>{{ a.account_no }}</td><td>{{ a.entity }}</td><td>{{ a.login_type }}</td>
<td>{{ a.timeout_minutes }}</td>
<td><a class="btn" href="{{ url_for('account_page', bank=bank, account_no=a.account_no) }}">进入账户</a></td></tr>
{% endfor %}</table>
<p class="meta">会话剩余：{{ remain }} 秒（无操作超时后自动退出）</p>"""

_ACCT_TPL = """
<div class="nav"><a href="{{ url_for('home', bank=bank) }}">返回账户列表</a>
  <a href="{{ url_for('logout', bank=bank) }}">退出登录</a></div>
<h2>账户 {{ account.account_no }} · {{ account.entity }}</h2>
<div class="bar">
  <div><label>起始日期</label><input type="date" id="dateFrom" value="{{ d_from }}"></div>
  <div><label>结束日期</label><input type="date" id="dateTo" value="{{ d_to }}"></div>
</div>
<p>
  <a class="btn" id="dlStatement" href="#" onclick="return dl('statement')">下载{{ stmt_name }}</a>
  <a class="btn" id="dlReceipt" href="#" onclick="return dl('receipt')">按日下载PDF回单</a>
</p>
<div id="dlPanel">
  <label>回单指定日期</label><input type="date" id="receiptDate" value="{{ d_from }}">
</div>
<h3>区间交易预览（{{ txn_count }} 笔）</h3>
<table class="grid"><tr><th>日期</th><th>方向</th><th>金额</th><th>对方户名</th><th>摘要</th><th>流水号</th></tr>
{% for t in txns[:12] %}
<tr><td>{{ t.txn_date }}</td><td>{{ t.direction }}</td><td>{{ '%.2f'|format(t.amount) }}</td>
<td>{{ t.counterparty }}</td><td>{{ t.summary }}</td><td>{{ t.txn_uid }}</td></tr>
{% endfor %}</table>
<p class="meta">本页所有数据均为仿真生成</p>
<script>
function dl(kind){
  var f=document.getElementById('dateFrom').value;
  var to=document.getElementById('dateTo').value;
  var rd=document.getElementById('receiptDate').value;
  var url="{{ url_for('download', bank=bank, account_no=account.account_no, kind='KIND') }}"
    .replace('KIND',kind)+'?date_from='+encodeURIComponent(f)+'&date_to='+encodeURIComponent(to)+'&biz_date='+encodeURIComponent(rd);
  window.location.href=url; return false;
}
</script>"""


# ---------------- 会话与公共逻辑 ----------------

def _bank_color(bank: str) -> str:
    return {"icbc": "#b5292f", "cmb": "#a61b29", "ccb": "#0066b3",
            "boc": "#961d1d", "citic": "#c8102e"}[bank]


def _current_account() -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM accounts WHERE id=?", (session.get("account_id"),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def login_required(fn):
    @wraps(fn)
    def wrapper(bank, *args, **kwargs):
        if not session.get("account_id") or session.get("bank") != bank:
            return redirect(url_for("login", bank=bank, expired=1))
        acct = _current_account()
        if not acct:
            session.clear()
            return redirect(url_for("login", bank=bank, expired=1))
        idle = time.time() - session.get("last_seen", 0)
        if idle > acct["timeout_minutes"] * 60:
            session.clear()
            return redirect(url_for("login", bank=bank, expired=1))
        session["last_seen"] = time.time()
        g.account = acct
        return fn(bank, *args, **kwargs)
    return wrapper


def _render(bank: str, body: str, title: str, **ctx) -> str:
    prof = BANK_PROFILES[bank]
    return render_template_string(
        _BASE, body=body, title=title, bank=bank,
        bank_name=prof["bank_name"], color=_bank_color(bank), **ctx
    )


def _should_fail(account_no: str, biz_key: str) -> bool:
    """偶发故障注入：哈希命中的请求第一次失败（HTTP 500），再次请求即成功。"""
    key = f"{account_no}|{biz_key}"
    h = hashlib.md5(key.encode()).hexdigest()
    if int(h[:8], 16) % 7 != 0:
        return False
    if key in _fail_seen:
        return False
    _fail_seen.add(key)
    return True


# ---------------- 路由 ----------------

@app.route("/")
def portal():
    items = "".join(
        f'<li><a style="color:{_bank_color(b)};font-size:16px" href="{url_for("login", bank=b)}">'
        f'{BANK_PROFILES[b]["bank_name"]}（{BANK_PROFILES[b]["login_type"]}）</a></li>'
        for b in BANK_ORDER
    )
    return f"<h2 style='margin:40px'>仿真网银门户（全部为本地假站点）</h2><ul style='margin-left:40px;line-height:2.2'>{items}</ul>"


@app.route("/<bank>/login", methods=["GET", "POST"])
def login(bank):
    if bank not in BANK_PROFILES:
        abort(404)
    err = ""
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM accounts WHERE bank_code=? AND login_user=?",
                (bank, username)).fetchone()
        finally:
            conn.close()
        if not row or row["login_secret"] != password:
            err = "用户名或密码错误（仿真环境）"
        elif bank == "ccb" and request.form.get("smscode") != "888888":
            err = "短信验证码错误，固定为 888888"
        elif bank == "boc" and request.form.get("qrsigned") != "1":
            err = "请先在手机银行确认扫码"
        else:
            session["account_id"] = row["id"]
            session["bank"] = bank
            session["last_seen"] = time.time()
            return redirect(url_for("home", bank=bank))
    expired = request.args.get("expired")
    body = render_template_string(
        _LOGIN_TPL[bank], bank=bank,
        err=("会话已超时，请重新登录。" if expired else err),
    )
    return _render(bank, body, f"{BANK_PROFILES[bank]['bank_name']}登录")


@app.route("/<bank>/logout")
def logout(bank):
    session.clear()
    return redirect(url_for("login", bank=bank))


@app.route("/<bank>/home")
@login_required
def home(bank):
    conn = get_conn()
    try:
        accts = [dict(r) for r in conn.execute(
            "SELECT * FROM accounts WHERE bank_code=? AND enabled=1 ORDER BY id", (bank,))]
    finally:
        conn.close()
    remain = max(0, g.account["timeout_minutes"] * 60 - int(time.time() - session["last_seen"]))
    body = render_template_string(_HOME_TPL, bank=bank, accounts=accts,
                                  user=g.account["login_user"], remain=remain)
    return _render(bank, body, "账户首页")


@app.route("/<bank>/account/<account_no>")
@login_required
def account_page(bank, account_no):
    conn = get_conn()
    try:
        acct = conn.execute("SELECT * FROM accounts WHERE account_no=? AND bank_code=?",
                            (account_no, bank)).fetchone()
        if not acct:
            abort(404)
        acct = dict(acct)
    finally:
        conn.close()
    d_from = request.args.get("date_from", "2026-10-01")
    d_to = request.args.get("date_to", "2026-10-31")
    txns = load_account_transactions(bank, account_no, BANKFILE_DIR)
    txns = [t for t in txns if d_from <= t["txn_date"] <= d_to]
    stmt_name = {"csv": "CSV流水", "ofx": "OFX流水"}[BANK_PROFILES[bank]["statement_fmt"]]
    body = render_template_string(
        _ACCT_TPL, bank=bank, account=acct, d_from=d_from, d_to=d_to,
        txns=txns, txn_count=len(txns), stmt_name=stmt_name)
    return _render(bank, body, "账户详情")


@app.route("/<bank>/account/<account_no>/download/<kind>")
@login_required
def download(bank, account_no, kind):
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    biz_date = request.args.get("biz_date", date_from)
    conn = get_conn()
    try:
        acct = conn.execute("SELECT * FROM accounts WHERE account_no=? AND bank_code=?",
                            (account_no, bank)).fetchone()
        if not acct:
            abort(404)
        acct = dict(acct)
    finally:
        conn.close()

    txns = load_account_transactions(bank, account_no, BANKFILE_DIR)
    prof = BANK_PROFILES[bank]

    if kind == "statement":
        if _should_fail(account_no, f"statement|{date_from}|{date_to}"):
            abort(500, description="仿真站点注入的临时故障：流水生成超时，请重试")
        if prof["statement_fmt"] == "csv":
            data = build_csv(bank, acct, txns, date_from, date_to)
            mime, ext = "text/csv", "csv"
        else:
            data = build_ofx(bank, acct, txns, date_from, date_to)
            mime, ext = "application/x-ofx", "ofx"
        fname = f"{bank}_{account_no}_{date_from}_{date_to}_statement.{ext}"
    elif kind == "receipt":
        if _should_fail(account_no, f"receipt|{biz_date}"):
            abort(500, description="仿真站点注入的临时故障：回单服务暂不可用，请重试")
        day_txns = [t for t in txns if t["txn_date"] == biz_date]
        if not day_txns:
            abort(404, description="该日期无交易回单")
        data = build_pdf_receipt(bank, acct, day_txns, biz_date)
        mime, ext = "application/pdf", "pdf"
        fname = f"{bank}_{account_no}_{biz_date}_receipt.pdf"
    else:
        abort(404)

    resp = make_response(data)
    resp.headers["Content-Type"] = f"{mime}; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="{fname}"'
    return resp


def run(host: str = "127.0.0.1", port: int | None = None, **kw) -> None:
    app.run(host=host, port=port or BANK_PORT, threaded=True, **kw)


if __name__ == "__main__":
    run()
