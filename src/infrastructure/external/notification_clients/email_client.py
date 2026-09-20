"""邮件（SMTP）通知客户端。

**为什么用标准库 smtplib 而不是引第三方库**

邮件只占通知功能的一小部分，为此增加一个运行时依赖不划算——尤其是本项目
自包含化之后，每个新依赖都会成为构建与安全维护的负担。``smtplib`` + ``email``
覆盖了 SMTP / SMTP_SSL / STARTTLS 三种连接方式与 MIME 编码，够用。

**发送在 executor 里执行**

``smtplib`` 是同步阻塞的，握手 + 投递常有一到数秒。本项目采集与通知共用
一个事件循环，直接在协程里调用会卡住所有 HTTP 请求与其它通知渠道。
因此整段发送逻辑都丢进 ``run_in_executor``。

**认证失败与投递失败要分开报**

``smtplib`` 对这两类问题的异常类型不同（``SMTPAuthenticationError`` vs
``SMTPRecipientsRefused`` 等），日志里区分开能省下大量排查时间：
认证失败是授权码/账号问题，收件人被拒是收件地址问题。
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict

from .base import NotificationClient, NotificationMessage

#: 默认端口与加密方式的对应（显式指定端口时以配置为准）。
DEFAULT_SSL_PORT = 465
DEFAULT_STARTTLS_PORT = 587


class EmailClient(NotificationClient):
    """邮件通知客户端（SMTP）。"""

    channel_key = "email"
    display_name = "邮件"

    def __init__(
        self,
        smtp_host: str | None = None,
        smtp_port: int | None = None,
        smtp_user: str | None = None,
        smtp_password: str | None = None,
        mail_from: str | None = None,
        mail_to: str | None = None,
        smtp_use_ssl: bool = True,
        smtp_use_starttls: bool = False,
        pcurl_to_mobile: bool = True,
    ):
        # 启用条件必须同时具备「服务器 + 收件人」：只有服务器发不出去，
        # 只有收件人无从连接。少任何一个都视为未配置，避免进入发送失败分支。
        enabled = bool(smtp_host) and bool(mail_to)
        super().__init__(enabled=enabled, pcurl_to_mobile=pcurl_to_mobile)
        self.smtp_host = smtp_host
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.mail_from = mail_from or smtp_user
        self.mail_to = mail_to
        self.smtp_use_ssl = smtp_use_ssl
        self.smtp_use_starttls = smtp_use_starttls
        # 端口：显式配置优先；否则按加密方式取常见默认值
        self.smtp_port = int(smtp_port) if smtp_port else (
            DEFAULT_SSL_PORT if smtp_use_ssl else DEFAULT_STARTTLS_PORT
        )

    def _recipients(self) -> list[str]:
        """支持逗号分隔的多个收件人。"""
        raw = self.mail_to or ""
        return [addr.strip() for addr in raw.split(",") if addr.strip()]

    def build_mime(self, product_data: Dict, reason: str) -> MIMEMultipart:
        """构造 MIME 邮件。抽成独立方法便于测试，不必真的发信。"""
        message = self._build_message(product_data, reason)
        mail = MIMEMultipart("alternative")
        # 中文主题必须走 Header 编码，否则被当作 latin-1 会乱码或被服务器拒收
        mail["Subject"] = Header(message.notification_title, "utf-8")
        mail["From"] = self.mail_from or ""
        mail["To"] = ", ".join(self._recipients())

        text_body = f"{message.title}\n{message.content}"
        mail.attach(MIMEText(text_body, "plain", "utf-8"))

        html_rows = [
            f"<p><b>{_escape(message.title)}</b></p>",
            f"<p>价格: {_escape(message.price)}</p>",
            f"<p>原因: {_escape(reason)}</p>",
            f'<p><a href="{_escape(message.desktop_link)}">电脑端链接</a></p>',
        ]
        if message.mobile_link:
            html_rows.append(f'<p><a href="{_escape(message.mobile_link)}">手机端链接</a></p>')
        if message.image_url:
            html_rows.append(f'<p><img src="{_escape(message.image_url)}" alt="商品主图"/></p>')
        mail.attach(MIMEText("<html><body>" + "".join(html_rows) + "</body></html>", "html", "utf-8"))
        return mail

    def _send_sync(self, product_data: Dict, reason: str) -> None:
        mail = self.build_mime(product_data, reason)
        payload = mail.as_string()
        recipients = self._recipients()
        context = ssl.create_default_context()

        if self.smtp_use_ssl:
            server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, timeout=20, context=context)
        else:
            server = smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=20)

        try:
            if not self.smtp_use_ssl and self.smtp_use_starttls:
                server.starttls(context=context)
            if self.smtp_user and self.smtp_password:
                server.login(self.smtp_user, self.smtp_password)
            server.sendmail(self.mail_from or self.smtp_user, recipients, payload)
        finally:
            try:
                server.quit()
            except smtplib.SMTPException:
                # 连接可能已被服务端关闭；quit 失败不影响投递结果
                pass

    async def send(self, product_data: Dict, reason: str) -> None:
        if not self.is_enabled():
            raise RuntimeError("邮件通知未启用")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: self._send_sync(product_data, reason))


def _escape(value: str | None) -> str:
    """HTML 转义。商品标题来自卖家，直接内插进 HTML 会被当成标签。"""
    import html

    return html.escape(value or "", quote=True)
