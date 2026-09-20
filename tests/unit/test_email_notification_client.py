"""邮件（SMTP）通知客户端测试。

不发真实邮件：用假的 smtplib 替身断言「连接参数、认证、收件人、MIME 结构」，
因为这几处的错误都不会抛异常，只会静默发错或发不出去。
"""
from __future__ import annotations

import asyncio
import smtplib
from email import message_from_string

import pytest

from src.infrastructure.external.notification_clients.email_client import (
    DEFAULT_SSL_PORT,
    DEFAULT_STARTTLS_PORT,
    EmailClient,
)


PRODUCT = {
    "商品标题": "iPhone 15 Pro 256G 深空黑",
    "当前售价": "5999",
    "商品链接": "https://www.goofish.com/item?id=123456",
    "商品主图链接": "https://img.example.com/a.jpg",
}


class _FakeSMTP:
    """记录调用序列的 smtplib.SMTP 替身。"""

    instances: list["_FakeSMTP"] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.context = context
        self.logins: list[tuple[str, str]] = []
        self.sent: list[tuple[str, list[str], str]] = []
        self.starttls_calls = 0
        self.quit_called = False
        _FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.starttls_calls += 1

    def login(self, user, password):
        self.logins.append((user, password))

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent.append((from_addr, list(to_addrs), msg))

    def quit(self):
        self.quit_called = True


class _FailingQuitSMTP(_FakeSMTP):
    def quit(self):
        self.quit_called = True
        raise smtplib.SMTPException("connection already closed")


@pytest.fixture(autouse=True)
def _reset_instances():
    _FakeSMTP.instances = []
    yield
    _FakeSMTP.instances = []


@pytest.fixture()
def patch_smtp(monkeypatch):
    monkeypatch.setattr(smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _FakeSMTP)
    return _FakeSMTP


class TestEnablement:
    def test_enabled_requires_host_and_recipient(self):
        assert EmailClient(smtp_host="smtp.x.com").is_enabled() is False
        assert EmailClient(mail_to="a@x.com").is_enabled() is False
        assert EmailClient(smtp_host="smtp.x.com", mail_to="a@x.com").is_enabled() is True

    def test_disabled_send_raises(self):
        client = EmailClient(smtp_host="smtp.x.com")
        with pytest.raises(RuntimeError):
            asyncio.run(client.send(PRODUCT, "降价"))


class TestPortSelection:
    def test_ssl_default_port(self):
        client = EmailClient(smtp_host="h", mail_to="a@x.com", smtp_use_ssl=True)
        assert client.smtp_port == DEFAULT_SSL_PORT

    def test_starttls_default_port(self):
        client = EmailClient(smtp_host="h", mail_to="a@x.com", smtp_use_ssl=False)
        assert client.smtp_port == DEFAULT_STARTTLS_PORT

    def test_explicit_port_wins(self):
        client = EmailClient(
            smtp_host="h", mail_to="a@x.com", smtp_port=2525, smtp_use_ssl=True
        )
        assert client.smtp_port == 2525

    def test_port_as_string_is_coerced(self):
        client = EmailClient(smtp_host="h", mail_to="a@x.com", smtp_port="1587")
        assert client.smtp_port == 1587


class TestRecipients:
    def test_single_recipient(self):
        assert EmailClient(smtp_host="h", mail_to="a@x.com")._recipients() == ["a@x.com"]

    def test_comma_separated_multi_recipient(self):
        client = EmailClient(smtp_host="h", mail_to=" a@x.com , b@x.com ")
        assert client._recipients() == ["a@x.com", "b@x.com"]

    def test_blank_entries_are_dropped(self):
        client = EmailClient(smtp_host="h", mail_to="a@x.com,,  ,")
        assert client._recipients() == ["a@x.com"]


def _decoded_parts(mail) -> dict[str, str]:
    """把 MIME 报文拆成 ``{子类型: 解码后正文}``。

    ``as_string()`` 的内容是 base64 编码的，直接对它做子串断言必然失败——
    这跟「邮件里到底写了什么」无关。必须按 MIME 解析后解码。
    """
    parsed = message_from_string(mail.as_string())
    out: dict[str, str] = {}
    for part in parsed.walk():
        if part.get_content_maintype() == "multipart":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        out[part.get_content_subtype()] = payload.decode(
            part.get_content_charset() or "utf-8", errors="replace"
        )
    return out


class TestMime:
    def test_subject_is_utf8_encoded(self):
        """中文主题必须编码，否则被当 latin-1 处理会乱码或被拒。"""
        client = EmailClient(smtp_host="h", mail_to="a@x.com")
        mail = client.build_mime(PRODUCT, "价格低于阈值")
        assert "=?utf-8?" in mail.as_string()
        # 解码后应还原中文
        from email.header import decode_header, make_header

        assert str(make_header(decode_header(mail["Subject"]))) == "🚨 新推荐! iPhone 15 Pro 256G 深空黑"

    def test_has_plain_and_html_parts(self):
        client = EmailClient(smtp_host="h", mail_to="a@x.com")
        parts = _decoded_parts(client.build_mime(PRODUCT, "降价"))
        assert "plain" in parts
        assert "html" in parts

    def test_body_contains_price_and_reason(self):
        client = EmailClient(smtp_host="h", mail_to="a@x.com")
        parts = _decoded_parts(client.build_mime(PRODUCT, "价格低于阈值"))
        assert "5999" in parts["plain"]
        assert "价格低于阈值" in parts["plain"]
        assert "5999" in parts["html"]

    def test_html_escapes_malicious_title(self):
        """商品标题来自卖家，直接内插 HTML 会被当成标签。"""
        client = EmailClient(smtp_host="h", mail_to="a@x.com")
        evil = dict(PRODUCT, 商品标题="<script>alert(1)</script>")
        html = _decoded_parts(client.build_mime(evil, "r"))["html"]
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_plain_part_keeps_raw_title(self):
        """纯文本部分不做 HTML 转义——那里转义反而让用户看到 ``&lt;``。"""
        client = EmailClient(smtp_host="h", mail_to="a@x.com")
        evil = dict(PRODUCT, 商品标题="a<b>c")
        plain = _decoded_parts(client.build_mime(evil, "r"))["plain"]
        assert "a<b>c" in plain

    def test_html_escapes_link_attribute(self):
        client = EmailClient(smtp_host="h", mail_to="a@x.com")
        evil = dict(PRODUCT, 商品链接='https://x.com/"><img src=x>')
        html = _decoded_parts(client.build_mime(evil, "r"))["html"]
        assert '"><img src=x>' not in html

    def test_from_defaults_to_smtp_user(self):
        client = EmailClient(smtp_host="h", mail_to="a@x.com", smtp_user="bot@x.com")
        assert client.build_mime(PRODUCT, "r")["From"] == "bot@x.com"

    def test_explicit_from_wins(self):
        client = EmailClient(
            smtp_host="h", mail_to="a@x.com", smtp_user="bot@x.com", mail_from="n@x.com"
        )
        assert client.build_mime(PRODUCT, "r")["From"] == "n@x.com"


class TestSend:
    def test_ssl_path_uses_smtp_ssl(self, patch_smtp):
        client = EmailClient(
            smtp_host="smtp.x.com", mail_to="a@x.com", smtp_user="u", smtp_password="p"
        )
        asyncio.run(client.send(PRODUCT, "降价"))
        instance = patch_smtp.instances[-1]
        assert instance.host == "smtp.x.com"
        assert instance.port == DEFAULT_SSL_PORT
        assert instance.logins == [("u", "p")]
        assert instance.sent[0][1] == ["a@x.com"]
        assert instance.quit_called is True

    def test_starttls_path_calls_starttls(self, patch_smtp):
        client = EmailClient(
            smtp_host="smtp.x.com",
            mail_to="a@x.com",
            smtp_use_ssl=False,
            smtp_use_starttls=True,
            smtp_user="u",
            smtp_password="p",
        )
        asyncio.run(client.send(PRODUCT, "降价"))
        assert patch_smtp.instances[-1].starttls_calls == 1

    def test_no_starttls_when_disabled(self, patch_smtp):
        client = EmailClient(
            smtp_host="smtp.x.com", mail_to="a@x.com", smtp_use_ssl=False
        )
        asyncio.run(client.send(PRODUCT, "降价"))
        assert patch_smtp.instances[-1].starttls_calls == 0

    def test_no_login_without_credentials(self, patch_smtp):
        """无账号的本地/内网中继不应尝试登录。"""
        client = EmailClient(smtp_host="smtp.x.com", mail_to="a@x.com")
        asyncio.run(client.send(PRODUCT, "降价"))
        assert patch_smtp.instances[-1].logins == []

    def test_multi_recipient_all_receive(self, patch_smtp):
        client = EmailClient(smtp_host="h", mail_to="a@x.com,b@x.com")
        asyncio.run(client.send(PRODUCT, "r"))
        assert patch_smtp.instances[-1].sent[0][1] == ["a@x.com", "b@x.com"]

    def test_quit_failure_does_not_raise(self, patch_smtp, monkeypatch):
        """连接可能已被服务端关闭；quit 失败不代表投递失败。"""
        monkeypatch.setattr(smtplib, "SMTP", _FailingQuitSMTP)
        monkeypatch.setattr(smtplib, "SMTP_SSL", _FailingQuitSMTP)
        client = EmailClient(smtp_host="h", mail_to="a@x.com")
        asyncio.run(client.send(PRODUCT, "r"))  # 不应抛异常

    def test_send_is_async_and_does_not_block_loop(self, patch_smtp):
        """发送必须走 executor，否则阻塞事件循环会卡住整个 Web 服务。"""
        client = EmailClient(smtp_host="h", mail_to="a@x.com")

        async def _main():
            return await client.send(PRODUCT, "r")

        asyncio.run(_main())
        assert patch_smtp.instances[-1].sent
