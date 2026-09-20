"""src/api/auth.py 会话 token 的安全测试。

认证是最不能只测 happy path 的模块：这里的 bug 不会报错，只会静默放行。
因此重点覆盖篡改检测、过期判定、密钥加载优先级与畸形输入。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest

import src.api.auth as auth


@pytest.fixture()
def clean_secret(tmp_path, monkeypatch):
    """隔离密钥来源，避免真实 data/.session_secret 或环境变量干扰。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEB_SESSION_SECRET", raising=False)
    monkeypatch.setattr(auth, "_memory_secret", None)
    monkeypatch.setattr(auth.app_settings, "session_secret", None)
    yield tmp_path
    monkeypatch.setattr(auth, "_memory_secret", None)


def _forge_token(payload: str, secret: str) -> str:
    """用指定密钥手工签发 token，用于构造过期/篡改样本。"""
    signature = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{signature}".encode()).decode()


# --- 正常签发与校验 ---


def test_issued_token_round_trips_username(clean_secret):
    token = auth.create_session_token("alice")
    assert auth.verify_session_token(token) == "alice"


def test_token_encoding_is_urlsafe_base64_without_padding_issues(clean_secret):
    token = auth.create_session_token("admin")
    raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    username, expiry, signature = raw.split("|")
    assert username == "admin"
    assert int(expiry) > time.time()
    assert len(signature) == 64  # sha256 hexdigest


def test_unicode_username_round_trips(clean_secret):
    token = auth.create_session_token("用户一")
    assert auth.verify_session_token(token) == "用户一"


# --- 篡改检测（核心安全断言）---


def test_tampered_payload_signature_mismatch_is_rejected(clean_secret):
    """把 payload 里的用户名改掉但沿用旧签名，必须失败。"""
    token = auth.create_session_token("admin")
    raw = base64.urlsafe_b64decode(token.encode()).decode()
    _, expiry, signature = raw.split("|")
    forged = base64.urlsafe_b64encode(f"root|{expiry}|{signature}".encode()).decode()

    assert auth.verify_session_token(forged) is None


def test_tampered_expiry_is_rejected(clean_secret):
    """延长过期时间也必须失败（正是最常见的越权尝试）。"""
    token = auth.create_session_token("admin")
    raw = base64.urlsafe_b64decode(token.encode()).decode()
    username, _, signature = raw.split("|")
    far_future = int(time.time()) + 10 ** 9
    forged = base64.urlsafe_b64encode(f"{username}|{far_future}|{signature}".encode()).decode()

    assert auth.verify_session_token(forged) is None


def test_tampered_signature_is_rejected(clean_secret):
    token = auth.create_session_token("admin")
    raw = base64.urlsafe_b64decode(token.encode()).decode()
    payload, _, _ = raw.rpartition("|")
    forged = _forge_token(payload, "0" * 64)

    assert auth.verify_session_token(forged) is None


def test_token_signed_with_other_secret_is_rejected(clean_secret, monkeypatch):
    """换一个密钥签发的 token 不能通过校验（防跨实例伪造）。"""
    forged = _forge_token(f"admin|{int(time.time()) + 3600}", "attacker-secret")
    assert auth.verify_session_token(forged) is None


# --- 过期判定 ---


def test_expired_token_is_rejected(clean_secret):
    secret = auth._load_or_create_secret()
    expired = _forge_token(f"admin|{int(time.time()) - 10}", secret)
    assert auth.verify_session_token(expired) is None


def test_token_expiring_exactly_now_is_rejected(clean_secret):
    """边界：expiry == now 视为已过期（源码用 ``int(expiry) < time.time()``）。"""
    secret = auth._load_or_create_secret()
    payload = f"admin|{int(time.time())}"
    token = _forge_token(payload, secret)
    # int(expiry) < time.time() 在时间继续前进后必然成立
    assert auth.verify_session_token(token) is None


def test_boundary_one_second_in_future_still_valid(clean_secret):
    secret = auth._load_or_create_secret()
    token = _forge_token(f"admin|{int(time.time()) + 2}", secret)
    assert auth.verify_session_token(token) == "admin"


# --- 畸形输入 ---


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "!!!not-base64!!!",
        "YWJj",  # base64 合法但内容无分隔符
        base64.urlsafe_b64encode(b"admin|not-a-number|deadbeef").decode(),
        base64.urlsafe_b64encode(b"|123|sig").decode(),  # 空用户名
        base64.urlsafe_b64encode(b"admin|123|").decode(),  # 空签名
        "a" * 500,
    ],
)
def test_malformed_tokens_return_none_without_raising(clean_secret, token):
    """任何畸形输入都必须安静返回 None，不能抛异常打崩请求。"""
    assert auth.verify_session_token(token) is None


# --- 密钥加载优先级 ---


def test_env_secret_takes_priority_over_file(clean_secret, monkeypatch):
    monkeypatch.setenv("WEB_SESSION_SECRET", "env-level-secret-0001")
    assert auth._load_or_create_secret() == "env-level-secret-0001"


def test_settings_secret_used_when_env_absent(clean_secret, monkeypatch):
    monkeypatch.setattr(auth.app_settings, "session_secret", "settings-secret-0001")
    assert auth._load_or_create_secret() == "settings-secret-0001"


def test_secret_file_is_created_and_reused(clean_secret):
    first = auth._load_or_create_secret()
    secret_file = clean_secret / "data" / ".session_secret"
    assert secret_file.is_file()
    assert len(first) == 64

    # 再次读取应复用文件内容（重启后会话不失效）
    auth._memory_secret = None
    assert auth._load_or_create_secret() == first


def test_short_secret_file_is_regenerated(clean_secret):
    """文件内容短于 16 字符视为无效，必须重新生成。"""
    secret_dir = clean_secret / "data"
    secret_dir.mkdir(parents=True, exist_ok=True)
    (secret_dir / ".session_secret").write_text("short", encoding="utf-8")

    value = auth._load_or_create_secret()
    assert value != "short"
    assert len(value) == 64


def test_memory_fallback_when_file_unwritable(clean_secret, monkeypatch):
    """写文件失败时回退到进程内存随机密钥，而不是抛异常。"""
    monkeypatch.setattr(auth, "_memory_secret", None)

    def _boom(*_args, **_kwargs):
        raise OSError("disk is read-only")

    monkeypatch.setattr("builtins.open", _boom)

    value = auth._load_or_create_secret()
    assert len(value) == 64
    # 内存密钥应被缓存，保证同一进程内签发/校验一致
    assert auth._load_or_create_secret() == value


def test_signing_is_stable_across_calls_with_same_secret(clean_secret):
    payload = "admin|1234567890"
    assert auth._sign(payload) == auth._sign(payload)


def test_session_max_age_matches_configured_ttl(clean_secret, monkeypatch):
    monkeypatch.setattr(auth.app_settings, "session_ttl_hours", 5)
    assert auth.session_max_age_seconds() == 5 * 3600


def test_cookie_name_constant_is_stable():
    """Cookie 名被前端/路由依赖，改名会静默登出所有用户。"""
    assert auth.SESSION_COOKIE_NAME == "session"
