"""邮件渠道在配置服务里的接线测试。

这些测试针对一类**静默错误**：字段能被写入、能读回、状态页也显示"已配置"，
但实际发送时行为不对。最典型的是布尔字段——``model_construct`` 不做类型转换，
``"false"`` 字符串的真值判断为真，于是开关关不掉。
"""
from __future__ import annotations

import pytest

from src.services.notification_config_service import (
    BOOLEAN_NOTIFICATION_FIELDS,
    CHANNEL_NOTIFICATION_FIELDS,
    NOTIFICATION_FIELD_MAP,
    SECRET_NOTIFICATION_FIELDS,
    build_configured_channels,
    build_notification_settings_response,
    build_notification_status_flags,
    prepare_notification_test_settings,
    prepare_notification_settings_update,
)


def _settings(**overrides):
    """构造一个只有邮件字段被设置的 NotificationSettings。"""
    base = {
        "smtp_host": None,
        "smtp_port": None,
        "smtp_user": None,
        "smtp_password": None,
        "mail_from": None,
        "mail_to": None,
        "smtp_use_ssl": True,
        "smtp_use_starttls": False,
    }
    base.update(overrides)
    from src.services.notification_config_service import _build_notification_settings_model

    return _build_notification_settings_model(base)


class TestFieldRegistration:
    def test_all_email_fields_are_in_the_map(self):
        for env_name in (
            "SMTP_HOST",
            "SMTP_PORT",
            "SMTP_USER",
            "SMTP_PASSWORD",
            "MAIL_FROM",
            "MAIL_TO",
            "SMTP_USE_SSL",
            "SMTP_USE_STARTTLS",
        ):
            assert env_name in NOTIFICATION_FIELD_MAP, env_name

    def test_email_is_a_registered_channel(self):
        assert "email" in CHANNEL_NOTIFICATION_FIELDS
        assert "SMTP_HOST" in CHANNEL_NOTIFICATION_FIELDS["email"]
        assert "MAIL_TO" in CHANNEL_NOTIFICATION_FIELDS["email"]

    def test_password_is_treated_as_secret(self):
        """密码不能回显给前端，与其它渠道的密钥同等对待。"""
        assert "SMTP_PASSWORD" in SECRET_NOTIFICATION_FIELDS

    def test_boolean_fields_are_registered(self):
        assert {"SMTP_USE_SSL", "SMTP_USE_STARTTLS"} <= BOOLEAN_NOTIFICATION_FIELDS

    def test_every_channel_field_is_mapped(self):
        """渠道声明的字段必须都能在字段表里找到，否则配置会被静默丢弃。"""
        for channel, fields in CHANNEL_NOTIFICATION_FIELDS.items():
            for env_name in fields:
                assert env_name in NOTIFICATION_FIELD_MAP, f"{channel} -> {env_name}"


class TestConfiguredChannels:
    def test_email_listed_when_host_and_recipient_present(self):
        channels = build_configured_channels(
            _settings(smtp_host="smtp.x.com", mail_to="a@x.com")
        )
        assert "email" in channels

    def test_email_absent_when_only_host(self):
        """只有服务器没有收件人 → 发不出去，不能显示为已配置。"""
        channels = build_configured_channels(_settings(smtp_host="smtp.x.com"))
        assert "email" not in channels

    def test_email_absent_when_only_recipient(self):
        channels = build_configured_channels(_settings(mail_to="a@x.com"))
        assert "email" not in channels

    def test_other_channels_unaffected(self):
        """加了邮件渠道不能把原有渠道的判断打乱。"""
        channels = build_configured_channels(
            _settings(smtp_host="h", mail_to="a@x.com", ntfy_topic_url="https://ntfy.sh/t")
        )
        assert set(channels) == {"ntfy", "email"}


class TestStatusFlags:
    def test_email_flags_present(self):
        flags = build_notification_status_flags(
            _settings(smtp_host="h", mail_to="a@x.com", smtp_password="p")
        )
        assert flags["smtp_host_set"] is True
        assert flags["mail_to_set"] is True
        assert flags["smtp_password_set"] is True

    def test_absent_email_flags_are_false(self):
        flags = build_notification_status_flags(_settings())
        assert flags["smtp_host_set"] is False
        assert flags["mail_to_set"] is False
        assert flags["smtp_password_set"] is False


class TestResponseShape:
    def test_password_is_blanked_out(self):
        response = build_notification_settings_response(
            _settings(smtp_host="h", mail_to="a@x.com", smtp_password="secret")
        )
        assert response["SMTP_PASSWORD"] == ""
        assert response["SMTP_PASSWORD_SET"] is True

    def test_non_secret_fields_are_echoed(self):
        response = build_notification_settings_response(
            _settings(smtp_host="smtp.x.com", mail_to="a@x.com", smtp_user="u")
        )
        assert response["SMTP_HOST"] == "smtp.x.com"
        assert response["MAIL_TO"] == "a@x.com"
        assert response["SMTP_USER"] == "u"

    def test_booleans_stay_boolean(self):
        """布尔字段必须是真布尔，前端才能正确渲染开关。"""
        response = build_notification_settings_response(
            _settings(smtp_use_ssl=False, smtp_use_starttls=True)
        )
        assert response["SMTP_USE_SSL"] is False
        assert response["SMTP_USE_STARTTLS"] is True


class TestBooleanNormalization:
    """核心回归：字符串 "false" 绝不能被当成真。"""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (False, False),
            (True, True),
            ("false", False),
            ("False", False),
            ("0", False),
            ("no", False),
            ("", False),
            ("true", True),
            ("1", True),
            ("yes", True),
        ],
    )
    def test_smtp_use_ssl_normalization(self, raw, expected):
        _, _, settings = prepare_notification_settings_update(
            {"SMTP_USE_SSL": raw}, _settings(smtp_host="h", mail_to="a@x.com")
        )
        assert settings.smtp_use_ssl is expected

    def test_disabling_ssl_actually_sticks(self):
        """开关关得掉——这是 ``bool("false") == True`` 会踩的坑。"""
        _, _, settings = prepare_notification_settings_update(
            {"SMTP_USE_SSL": "false"}, _settings(smtp_host="h", mail_to="a@x.com")
        )
        assert settings.smtp_use_ssl is False


class TestPortNormalization:
    def test_port_becomes_int(self):
        _, _, settings = prepare_notification_settings_update(
            {"SMTP_PORT": "1587"}, _settings(smtp_host="h", mail_to="a@x.com")
        )
        assert settings.smtp_port == 1587
        assert isinstance(settings.smtp_port, int)

    def test_empty_port_becomes_none(self):
        _, _, settings = prepare_notification_settings_update(
            {"SMTP_PORT": ""}, _settings(smtp_host="h", mail_to="a@x.com", smtp_port=465)
        )
        assert settings.smtp_port is None

    def test_invalid_port_is_rejected(self):
        from src.services.notification_config_service import (
            NotificationSettingsValidationError,
        )

        with pytest.raises(NotificationSettingsValidationError, match="整数"):
            prepare_notification_settings_update(
                {"SMTP_PORT": "not-a-port"}, _settings(smtp_host="h", mail_to="a@x.com")
            )


class TestChannelTestPayload:
    """渠道测试只应带该渠道的字段，不能把别的渠道配置一起带上。"""

    def test_email_test_carries_only_email_fields(self):
        merged = prepare_notification_test_settings(
            {"SMTP_HOST": "smtp.x.com", "MAIL_TO": "a@x.com", "TELEGRAM_BOT_TOKEN": "leak"},
            _settings(),
            channel="email",
        )
        assert merged.smtp_host == "smtp.x.com"
        assert merged.mail_to == "a@x.com"
        # 别的渠道的字段必须被隔离掉
        assert merged.telegram_bot_token is None

    def test_unknown_channel_rejected(self):
        from src.services.notification_config_service import (
            NotificationSettingsValidationError,
        )

        with pytest.raises(NotificationSettingsValidationError, match="不支持"):
            prepare_notification_test_settings({}, _settings(), channel="carrier_pigeon")
