"""
统一配置管理模块
使用 Pydantic 进行类型安全的配置管理
"""
try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    _USING_PYDANTIC_SETTINGS = True
except ImportError:
    from pydantic import BaseSettings
    _USING_PYDANTIC_SETTINGS = False
from pydantic import Field
from typing import Optional
import os

DEFAULT_TELEGRAM_API_BASE_URL = "https://api.telegram.org"

# 配置占位符：这些值出现在 .env / 环境变量里意味着"用户尚未真正配置"。
# 若不做判定，has_any_notification_enabled() 会把占位符当成已配置，
# 系统随后向无效地址推送且不报错，用户收不到任何通知也难以定位原因。
_PLACEHOLDER_MARKERS = (
    "your_",          # YOUR_BARK_KEY / your_admin_user / your_admin_password
    "yourkey",
    "sk-xxx",
    "changeme",
    "placeholder",
)


def _is_placeholder(value: Optional[str]) -> bool:
    """判断配置值是否为占位符/未填写。

    只匹配明确的占位符模式，避免误伤真实凭据（例如真实 key 恰好含某个子串）。
    """
    if not value:
        return True
    text = str(value).strip()
    if not text:
        return True
    lowered = text.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _env_field(default, env_name: str, **kwargs):
    if _USING_PYDANTIC_SETTINGS:
        return Field(default, validation_alias=env_name, **kwargs)
    return Field(default, env=env_name, **kwargs)


if _USING_PYDANTIC_SETTINGS:
    class _EnvSettings(BaseSettings):
        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
            protected_namespaces=(),
        )

        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls,
            init_settings,
            env_settings,
            dotenv_settings,
            file_secret_settings,
        ):
            """让 .env 文件优先于进程环境变量。

            pydantic-settings 默认顺序是 env > dotenv，而 docker-compose 的
            environment 块会注入 OPENAI_API_KEY="" 这类占位符，把用户在设置页
            写入 config/.env 的真实配置压掉（已用真实模块实测坐实）。
            这里调整优先级，确立 .env 为唯一配置真源。
            """
            return (init_settings, dotenv_settings, env_settings, file_secret_settings)
else:
    class _EnvSettings(BaseSettings):
        class Config:
            env_file = ".env"
            env_file_encoding = "utf-8"
            extra = "ignore"
            protected_namespaces = ()


class AISettings(_EnvSettings):
    """AI模型配置"""
    api_key: Optional[str] = _env_field(None, "OPENAI_API_KEY")
    base_url: str = _env_field("", "OPENAI_BASE_URL")
    model_name: str = _env_field("", "OPENAI_MODEL_NAME")
    proxy_url: Optional[str] = _env_field(None, "PROXY_URL")
    debug_mode: bool = _env_field(False, "AI_DEBUG_MODE")
    enable_response_format: bool = _env_field(True, "ENABLE_RESPONSE_FORMAT")
    enable_thinking: bool = _env_field(False, "ENABLE_THINKING")
    skip_analysis: bool = _env_field(False, "SKIP_AI_ANALYSIS")
    image_mode: str = _env_field("auto", "AI_IMAGE_MODE")

    def is_configured(self) -> bool:
        """检查 AI 是否已正确配置。

        必须同时校验 api_key：原先只查 base_url/model_name，导致 compose 注入的
        占位符（base_url 非空、model 非空、api_key 为空串）被判为"已配置"，
        系统不提示未配置，而是带着空 key 发请求，最终以 401 失败。
        """
        return bool(self.base_url and self.model_name and self.api_key)


class NotificationSettings(_EnvSettings):
    """通知服务配置"""
    ntfy_topic_url: Optional[str] = _env_field(None, "NTFY_TOPIC_URL")
    gotify_url: Optional[str] = _env_field(None, "GOTIFY_URL")
    gotify_token: Optional[str] = _env_field(None, "GOTIFY_TOKEN")
    bark_url: Optional[str] = _env_field(None, "BARK_URL")
    wx_bot_url: Optional[str] = _env_field(None, "WX_BOT_URL")
    telegram_bot_token: Optional[str] = _env_field(None, "TELEGRAM_BOT_TOKEN")
    telegram_chat_id: Optional[str] = _env_field(None, "TELEGRAM_CHAT_ID")
    telegram_api_base_url: Optional[str] = _env_field(
        DEFAULT_TELEGRAM_API_BASE_URL,
        "TELEGRAM_API_BASE_URL",
    )
    webhook_url: Optional[str] = _env_field(None, "WEBHOOK_URL")
    webhook_method: str = _env_field("POST", "WEBHOOK_METHOD")
    webhook_headers: Optional[str] = _env_field(None, "WEBHOOK_HEADERS")
    webhook_content_type: str = _env_field("JSON", "WEBHOOK_CONTENT_TYPE")
    webhook_query_parameters: Optional[str] = _env_field(None, "WEBHOOK_QUERY_PARAMETERS")
    webhook_body: Optional[str] = _env_field(None, "WEBHOOK_BODY")
    # --- 邮件（SMTP）通知渠道 ---
    # 启用只需 SMTP_HOST + MAIL_TO；其余按需。
    # SMTP_USE_SSL 默认 True（465 端口），STARTTLS 常见于 587。
    smtp_host: Optional[str] = _env_field(None, "SMTP_HOST")
    smtp_port: Optional[int] = _env_field(None, "SMTP_PORT")
    smtp_user: Optional[str] = _env_field(None, "SMTP_USER")
    smtp_password: Optional[str] = _env_field(None, "SMTP_PASSWORD")
    mail_from: Optional[str] = _env_field(None, "MAIL_FROM")
    mail_to: Optional[str] = _env_field(None, "MAIL_TO")
    smtp_use_ssl: bool = _env_field(True, "SMTP_USE_SSL")
    smtp_use_starttls: bool = _env_field(False, "SMTP_USE_STARTTLS")
    pcurl_to_mobile: bool = _env_field(True, "PCURL_TO_MOBILE")

    def has_any_notification_enabled(self) -> bool:
        """检查是否配置了任何通知服务。

        会过滤占位符：例如 compose 里默认的
        BARK_URL="https://api.day.app/YOUR_BARK_KEY" 不应被视为"已配置"，
        否则系统会向无效地址推送且不报错。
        """
        return any([
            not _is_placeholder(self.ntfy_topic_url) and self.ntfy_topic_url,
            not _is_placeholder(self.wx_bot_url) and self.wx_bot_url,
            not _is_placeholder(self.gotify_url) and self.gotify_token,
            not _is_placeholder(self.bark_url) and self.bark_url,
            not _is_placeholder(self.telegram_bot_token) and self.telegram_chat_id,
            not _is_placeholder(self.webhook_url) and self.webhook_url,
            not _is_placeholder(self.smtp_host) and self.smtp_host and self.mail_to,
        ])


class ScraperSettings(_EnvSettings):
    """爬虫相关配置"""
    run_headless: bool = _env_field(True, "RUN_HEADLESS")
    login_is_edge: bool = _env_field(False, "LOGIN_IS_EDGE")
    running_in_docker: bool = _env_field(False, "RUNNING_IN_DOCKER")
    state_file: str = _env_field("xianyu_state.json", "STATE_FILE")


class AppSettings(_EnvSettings):
    """应用主配置"""
    server_port: int = _env_field(8000, "SERVER_PORT")
    web_username: str = _env_field("admin", "WEB_USERNAME")
    web_password: str = _env_field("admin123", "WEB_PASSWORD")
    task_log_retention_days: int = _env_field(7, "TASK_LOG_RETENTION_DAYS", ge=1)
    session_secret: Optional[str] = _env_field(None, "WEB_SESSION_SECRET")
    session_ttl_hours: int = _env_field(72, "SESSION_TTL_HOURS", ge=1)
    account_check_interval_hours: int = _env_field(4, "ACCOUNT_CHECK_INTERVAL_HOURS", ge=1)
    daily_report_enabled: bool = _env_field(True, "DAILY_REPORT_ENABLED")
    daily_report_hour: int = _env_field(9, "DAILY_REPORT_HOUR", ge=0, le=23)

    # 文件路径配置
    config_file: str = "config.json"
    image_save_dir: str = "images"
    task_image_dir_prefix: str = "task_images_"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # 创建必要的目录
        os.makedirs(self.image_save_dir, exist_ok=True)


# 全局配置实例（单例模式）
_settings_instance = None

def get_settings() -> AppSettings:
    """获取全局配置实例"""
    global _settings_instance
    if _settings_instance is None:
        _settings_instance = AppSettings()
    return _settings_instance


def reload_settings() -> None:
    """重新加载全局配置实例"""
    global _settings_instance, settings, ai_settings, notification_settings, scraper_settings
    from dotenv import load_dotenv
    from src.infrastructure.config.env_manager import env_manager

    load_dotenv(dotenv_path=env_manager.env_file, override=True)
    _settings_instance = None
    settings = get_settings()
    ai_settings = AISettings()
    notification_settings = NotificationSettings()
    scraper_settings = ScraperSettings()


# 导出便捷访问的配置实例
settings = get_settings()
ai_settings = AISettings()
notification_settings = NotificationSettings()
scraper_settings = ScraperSettings()
