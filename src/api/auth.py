"""
轻量级 session 认证工具
基于 HMAC-SHA256 签名 + HttpOnly Cookie，无需数据库、无需改动前端。
token 格式: base64url(username|expiry_ts|hmac_hex)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from src.infrastructure.config.settings import settings as app_settings

SESSION_COOKIE_NAME = "session"
_SECRET_FILE = os.path.join("data", ".session_secret")

_memory_secret: str | None = None


def _load_or_create_secret() -> str:
    """
    获取签名密钥，优先级：
    1. 环境变量 WEB_SESSION_SECRET（管理员显式指定，重启不失效）
    2. data/.session_secret 持久化文件（自动生成，重启不失效）
    3. 进程内存随机值（最后兜底，重启后所有会话失效）
    """
    global _memory_secret
    env_secret = os.environ.get("WEB_SESSION_SECRET") or (app_settings.session_secret or "")
    if env_secret:
        return env_secret
    try:
        if os.path.isfile(_SECRET_FILE):
            with open(_SECRET_FILE, "r", encoding="utf-8") as f:
                value = f.read().strip()
            if len(value) >= 16:
                return value
        os.makedirs(os.path.dirname(_SECRET_FILE) or ".", exist_ok=True)
        value = secrets.token_hex(32)
        with open(_SECRET_FILE, "w", encoding="utf-8") as f:
            f.write(value)
        return value
    except Exception:
        if _memory_secret is None:
            _memory_secret = secrets.token_hex(32)
        return _memory_secret


def _sign(payload: str) -> str:
    return hmac.new(
        _load_or_create_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_session_token(username: str) -> str:
    """为登录用户签发带过期时间的签名 token。"""
    ttl_seconds = int(app_settings.session_ttl_hours) * 3600
    expiry = int(time.time()) + ttl_seconds
    payload = f"{username}|{expiry}"
    token = f"{payload}|{_sign(payload)}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")


def verify_session_token(token: str | None) -> str | None:
    """校验 token，合法则返回用户名，否则返回 None。"""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        payload, _, sig = raw.rpartition("|")
        if not payload or not sig:
            return None
        if not hmac.compare_digest(sig, _sign(payload)):
            return None
        username, _, expiry = payload.rpartition("|")
        if not username or int(expiry) < time.time():
            return None
        return username
    except Exception:
        return None


def session_max_age_seconds() -> int:
    return int(app_settings.session_ttl_hours) * 3600