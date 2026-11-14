"""五家银行的页面流程定义：登录选择器/步骤、下载入口选择器（各家 DOM 不同）。"""
from __future__ import annotations

# 每家银行一套选择器与登录步骤，RPA 主流程按 bank_code 分派
BANK_FLOWS = {
    "icbc": {
        "login": {
            "url_path": "/icbc/login",
            "user_sel": "#userName",
            "pwd_sel": "#loginPwd",
            "submit_sel": "#btnLogin",
            "extra": None,
        },
        "home_url": "/icbc/home",
        "acct_link_text": "进入账户",
        "statement_sel": "#dlStatement",
        "receipt_sel": "#dlReceipt",
        "date_from_sel": "#dateFrom",
        "date_to_sel": "#dateTo",
        "receipt_date_sel": "#receiptDate",
    },
    "cmb": {
        "login": {
            "url_path": "/cmb/login",
            "user_sel": ".ukey-user",
            "pwd_sel": ".ukey-pin",
            "submit_sel": ".ukey-submit",
            "extra": "usbk",  # 先确认 U 盾证书提示
        },
        "home_url": "/cmb/home",
        "acct_link_text": "进入账户",
        "statement_sel": "#dlStatement",
        "receipt_sel": "#dlReceipt",
        "date_from_sel": "#dateFrom",
        "date_to_sel": "#dateTo",
        "receipt_date_sel": "#receiptDate",
    },
    "ccb": {
        "login": {
            "url_path": "/ccb/login",
            "user_sel": "#custNo",
            "pwd_sel": "#pwd",
            "submit_sel": "#ccbLoginBtn",
            "extra": "smscode",  # 先点“获取验证码”，再填固定 888888
            "sms_btn_sel": "#btnSendSms",
            "sms_input_sel": "#smsCode",
            "sms_code": "888888",
        },
        "home_url": "/ccb/home",
        "acct_link_text": "进入账户",
        "statement_sel": "#dlStatement",
        "receipt_sel": "#dlReceipt",
        "date_from_sel": "#dateFrom",
        "date_to_sel": "#dateTo",
        "receipt_date_sel": "#receiptDate",
    },
    "boc": {
        "login": {
            "url_path": "/boc/login",
            "user_sel": "#bocUser",
            "pwd_sel": None,  # 扫码登录无密码框
            "submit_sel": "#qrConfirm",
            "extra": "qrcode",  # 等待二维码渲染后点“已确认”
            "qr_wait_sel": "#qrBox",
        },
        "home_url": "/boc/home",
        "acct_link_text": "进入账户",
        "statement_sel": "#dlStatement",
        "receipt_sel": "#dlReceipt",
        "date_from_sel": "#dateFrom",
        "date_to_sel": "#dateTo",
        "receipt_date_sel": "#receiptDate",
    },
    "citic": {
        "login": {
            "url_path": "/citic/login",
            "user_sel": '[data-test="citic-user"]',
            "pwd_sel": '[data-test="citic-pwd"]',
            "submit_sel": '[data-test="citic-submit"]',
            "extra": None,
        },
        "home_url": "/citic/home",
        "acct_link_text": "进入账户",
        "statement_sel": "#dlStatement",
        "receipt_sel": "#dlReceipt",
        "date_from_sel": "#dateFrom",
        "date_to_sel": "#dateTo",
        "receipt_date_sel": "#receiptDate",
    },
}
