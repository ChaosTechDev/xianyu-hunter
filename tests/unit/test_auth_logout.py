"""``/auth/logout`` 的行为测试。

为什么值得单独测：真正的会话凭证是 **HttpOnly** cookie，JS 读不到也删不掉。
前端原来只清 localStorage，接口层面「仍是登录态」——这是凭证缺陷而非体验问题。
下面这些断言锁的就是「登出之后服务端真的不认这个 cookie 了」，
而不是「响应里出现了 Set-Cookie 字样」。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import src.api.auth as session_auth  # noqa: E402

COOKIE_NAME = session_auth.SESSION_COOKIE_NAME


@pytest.fixture()
def auth_client(tmp_path, monkeypatch):
    """针对**真实应用**的客户端。

    不能复用 ``auth_client``：那个 fixture 用一组最小路由拼了个替身 app，
    里面既没有认证中间件也没有 ``/auth/*``。用它测登出会得到 404，
    而「测替身」的结论对真实部署毫无意义——本次要验的恰好是中间件与
    cookie 属性的配合，必须打真实 ``src.app.app``。
    """
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "auth_test.sqlite3"))
    monkeypatch.setenv("WEB_SESSION_SECRET", "test-secret-for-logout-tests")
    from src.app import app

    with TestClient(app) as client:
        yield client


def _login(client, username: str, password: str):
    return client.post(
        "/auth/status", json={"username": username, "password": password}
    )


class TestLogoutClearsServerSession:
    def test_logout_endpoint_exists_and_returns_unauthenticated(self, auth_client):
        response = auth_client.post("/auth/logout")
        assert response.status_code == 200
        assert response.json() == {"authenticated": False}

    def test_logout_removes_cookie_so_protected_route_rejects(self, auth_client):
        """核心断言：登出后拿旧 cookie 访问受保护接口必须被拒。"""
        from src.infrastructure.config.settings import settings

        login = _login(auth_client, settings.web_username, settings.web_password)
        assert login.status_code == 200
        assert COOKIE_NAME in auth_client.cookies

        # 登出前：受保护接口可达（200/非 401）
        before = auth_client.get("/api/tasks")
        assert before.status_code != 401

        auth_client.post("/auth/logout")
        assert COOKIE_NAME not in auth_client.cookies

        after = auth_client.get("/api/tasks")
        assert after.status_code == 401

    def test_delete_cookie_header_matches_original_attributes(self, auth_client):
        """删除时必须补齐 path/samesite/httponly。

        cookie 的唯一性由 (name, domain, path) 决定；path 不一致会删不掉原 cookie，
        只多出一个同名新 cookie，浏览器看着像成功、实际旧凭证仍然有效。
        """
        from src.infrastructure.config.settings import settings

        _login(auth_client, settings.web_username, settings.web_password)
        response = auth_client.post("/auth/logout")

        headers = [
            v for k, v in response.headers.items() if k.lower() == "set-cookie"
        ]
        assert headers, "登出必须下发 Set-Cookie 才能让浏览器删除 HttpOnly cookie"

        raw = " ".join(headers).lower()
        assert f"{COOKIE_NAME.lower()}=" in raw
        assert "path=/" in raw
        # HttpOnly cookie 的删除同样需要 HttpOnly 属性
        assert "httponly" in raw
        assert "samesite=lax" in raw

    def test_logout_does_not_require_authentication(self, auth_client):
        """未登录时调用登出也应成功（幂等），否则前端会在登出页报错。"""
        auth_client.cookies.clear()
        response = auth_client.post("/auth/logout")
        assert response.status_code == 200

    def test_logout_is_idempotent(self, auth_client):
        from src.infrastructure.config.settings import settings

        _login(auth_client, settings.web_username, settings.web_password)
        first = auth_client.post("/auth/logout")
        second = auth_client.post("/auth/logout")
        assert first.status_code == second.status_code == 200

    def test_token_itself_remains_valid_but_cookie_is_gone(self, auth_client):
        """端点的职责是让**浏览器**不再持有 cookie。

        这里如实记录边界：签发的是无状态 HMAC token，服务端没有吊销名单，
        所以抓到此 token 的人仍可在 TTL 内使用它。要真正做到服务端吊销需要
        引入会话存储——当前设计是有意取舍（见 auth.py 顶部「无需数据库」）。
        本测试把这个边界钉住，避免有人误以为登出等于吊销。
        """
        from src.infrastructure.config.settings import settings

        login = _login(auth_client, settings.web_username, settings.web_password)
        token = auth_client.cookies.get(COOKIE_NAME)
        assert token

        auth_client.post("/auth/logout")
        # 服务端签名校验仍认这个 token —— 无状态设计下这是预期行为
        assert session_auth.verify_session_token(token) is not None


class TestProtectedRouteBoundary:
    def test_protected_route_rejects_without_cookie(self, auth_client):
        auth_client.cookies.clear()
        assert auth_client.get("/api/tasks").status_code == 401

    def test_protected_route_rejects_forged_cookie(self, auth_client):
        auth_client.cookies.clear()
        auth_client.cookies.set(COOKIE_NAME, "not-a-real-token")
        assert auth_client.get("/api/tasks").status_code == 401

    def test_health_is_reachable_without_cookie(self, auth_client):
        """健康检查必须免认证，否则容器编排无法判活。"""
        auth_client.cookies.clear()
        assert auth_client.get("/health").status_code == 200
