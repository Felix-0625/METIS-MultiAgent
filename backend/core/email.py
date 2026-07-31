"""
邮件发送模块
支持两种发送方式（按优先级）：
1. Brevo REST API（BREVO_API_KEY）— 走 HTTPS 443，兼容 Render Free 出站限制
2. SMTP（SMTP_HOST 等）— 需 Render Starter 解锁端口

均无需第三方依赖（标准库 json + http.client / smtplib + email.mime）。
未配置时 is_email_configured() 返回 False。
"""

import json
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from http.client import HTTPSConnection

logger = logging.getLogger(__name__)

# ── SMTP 配置 ────────────────────────────────────────────────────────────────
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
_SMTP_TIMEOUT = 10

# ── Brevo 配置（Render Free 推荐）────────────────────────────────────────────
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "")
BREVO_SENDER_NAME = os.environ.get("BREVO_SENDER_NAME", "MeTis")
BREVO_SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL", "")
BREVO_API_HOST = "api.brevo.com"


def _smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM)


def _brevo_configured() -> bool:
    return bool(BREVO_API_KEY and BREVO_SENDER_EMAIL)


def is_email_configured() -> bool:
    return _brevo_configured() or _smtp_configured()


def _send_via_brevo(to: str, subject: str, body_html: str) -> bool:
    payload = json.dumps({
        "sender": {
            "name": BREVO_SENDER_NAME,
            "email": BREVO_SENDER_EMAIL,
        },
        "to": [{"email": to}],
        "subject": subject,
        "htmlContent": body_html,
    })

    resp_status = None
    resp_body = ""
    try:
        conn = HTTPSConnection(BREVO_API_HOST, timeout=15)
        conn.request(
            "POST",
            "/v3/smtp/email",
            body=payload,
            headers={
                "accept": "application/json",
                "api-key": BREVO_API_KEY,
                "content-type": "application/json",
            }
        )
        resp = conn.getresponse()
        resp_status = resp.status
        resp_body = resp.read().decode("utf-8")
        conn.close()
    except Exception as e:
        logger.error("Brevo 网络请求异常: %s", e)
        raise RuntimeError(f"Brevo 连接失败: {e}")

    if 200 <= resp_status < 300:
        logger.info("Brevo 邮件已发送至 %s (status=%s)", to, resp_status)
        return True
    else:
        # Provider error bodies are untrusted and may echo request headers or
        # credentials. Keep them out of logs and API-visible exceptions.
        logger.error("Brevo 发送失败 to=%s status=%s", to, resp_status)
        raise RuntimeError(f"Brevo 返回错误 {resp_status}")


def _send_via_smtp(to: str, subject: str, body_html: str) -> bool:
    if not _smtp_configured():
        raise RuntimeError("SMTP 未配置")

    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=_SMTP_TIMEOUT) as server:
        if SMTP_USE_TLS:
            server.starttls()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(msg)

    logger.info("SMTP 邮件已发送至 %s", to)
    return True


def send_email(to: str, subject: str, body_html: str) -> bool:
    if _brevo_configured():
        return _send_via_brevo(to, subject, body_html)
    if _smtp_configured():
        return _send_via_smtp(to, subject, body_html)
    raise RuntimeError("邮件服务未配置，请设置 BREVO_API_KEY 或 SMTP_* 环境变量")


def send_verification_email(to: str, username: str, code: str) -> bool:
    subject = f"MeTis 邮箱验证码：{code}"
    body_html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px">
  <div style="text-align:center;margin-bottom:24px">
    <h1 style="color:#5450a4;letter-spacing:4px;margin:0">M E T I S</h1>
  </div>
  <h2 style="font-weight:400">你好，{username}</h2>
  <p>你的 MeTis 邮箱验证码是：</p>
  <div style="background:#f5f3ff;padding:20px;text-align:center;border-radius:8px;margin:16px 0">
    <span style="font-size:32px;font-weight:700;letter-spacing:8px;color:#5450a4">{code}</span>
  </div>
  <p style="color:#888;font-size:13px">验证码 10 分钟内有效。如非本人操作，请忽略此邮件。</p>
</body></html>"""
    return send_email(to, subject, body_html)


def send_reset_email(to: str, username: str, reset_url: str) -> bool:
    subject = "MeTis 密码重置"
    body_html = f"""<!DOCTYPE html>
<html><body style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;padding:24px">
  <div style="text-align:center;margin-bottom:24px">
    <h1 style="color:#5450a4;letter-spacing:4px;margin:0">M E T I S</h1>
  </div>
  <h2 style="font-weight:400">你好，{username}</h2>
  <p>请点击下方按钮重置你的密码：</p>
  <div style="text-align:center;margin:24px 0">
    <a href="{reset_url}" style="display:inline-block;padding:14px 32px;background:linear-gradient(135deg,#5450a4,#7c3aed);color:#fff;text-decoration:none;border-radius:8px;font-weight:600">重置密码</a>
  </div>
  <p style="color:#888;font-size:13px">此链接 10 分钟内有效。如非本人操作，请忽略此邮件。</p>
</body></html>"""
    return send_email(to, subject, body_html)
