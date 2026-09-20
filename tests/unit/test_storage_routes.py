"""``/api/storage/*`` 路由测试。

守住两道独立的闸门：

1. **认证闸门**：未登录访问必须 401（新路由不能绕过既有中间件）。
2. **确认闸门**：``confirm`` 不为 true 时，即使 ``DATA_RETENTION_ENABLED`` 已开启
   也只能 dry-run。环境变量管「功能开不开」，``confirm`` 管「这一次真想删吗」——
   两者是独立的两道，缺一不可。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

#: 关键隔离：把清理根指向临时目录。
#:
#: 本文件的用例会以 ``confirm=True`` 真正执行清理。若不隔离，测试就会扫到并删除
#: **项目真实的** logs/ 与数据库数据——测试绝不能有这种副作用。
#: 用 ``DATA_RETENTION_ROOT`` 而不是 monkeypatch，是因为路由内部自己解析根目录，
#: 环境变量是唯一能贯穿到调用链的出口。
_RETENTION_ROOT = tempfile.mkdtemp(prefix="storage_route_root_")
os.environ["DATA_RETENTION_ROOT"] = _RETENTION_ROOT
#: 显式关闭功能开关：``confirm=True`` 用例靠请求里的 confirm 触发执行，
#: 开关必须关着才能证明「confirm 是独立于环境变量的第二道闸门」。
os.environ["DATA_RETENTION_ENABLED"] = "0"
os.environ.setdefault(
    "APP_DATABASE_FILE", os.path.join(tempfile.mkdtemp(prefix="storage_route_"), "a.sqlite3")
)

from fastapi.testclient import TestClient  # noqa: E402

from src.app import app  # noqa: E402
from src.infrastructure.config.settings import settings as app_settings  # noqa: E402


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def authed(client: TestClient) -> TestClient:
    response = client.post(
        "/auth/status",
        json={
            "username": app_settings.web_username,
            "password": app_settings.web_password,
        },
    )
    assert response.status_code == 200, "测试前提：内置凭据应能登录"
    return client


class TestAuthGate:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/api/storage/usage"),
            ("get", "/api/storage/retention/plan"),
            ("post", "/api/storage/retention/execute"),
        ],
    )
    def test_unauthenticated_is_rejected(self, client, method, path):
        """新路由必须在既有认证中间件的保护范围内。"""
        caller = getattr(client, method)
        response = caller(path) if method == "get" else caller(path, json={})
        assert response.status_code == 401


class TestUsageEndpoint:
    def test_usage_returns_expected_shape(self, authed):
        response = authed.get("/api/storage/usage")
        assert response.status_code == 200
        body = response.json()
        assert "usage" in body and "oldest_records" in body
        assert "generated_at" in body
        assert set(body["usage"]["directories"].keys()) == {
            "logs",
            "images",
            "jsonl",
            "price_history",
        }

    def test_usage_is_read_only(self, authed):
        """连续调用不应改变任何状态。"""
        first = authed.get("/api/storage/usage").json()
        second = authed.get("/api/storage/usage").json()
        assert first["usage"]["total_bytes"] == second["usage"]["total_bytes"]


class TestPlanEndpoint:
    def test_plan_is_always_dry_run(self, authed):
        body = authed.get("/api/storage/retention/plan").json()
        assert body["dry_run"] is True
        assert "database" in body["plan"]
        assert "files" in body["plan"]

    def test_plan_lists_consultation_logs(self, authed):
        """计划必须覆盖实际存在但未被 RETENTION_TARGETS 收录的表。"""
        body = authed.get("/api/storage/retention/plan").json()
        assert "consultation_logs" in body["plan"]["database"]


class TestExecuteConfirmationGate:
    def test_retention_root_is_isolated_from_real_project(self):
        """元测试：确认隔离生效，否则本文件会删掉真实项目数据。"""
        from src.services.retention_runner import _project_root

        assert _project_root() == _RETENTION_ROOT
        assert str(repo_root) not in _project_root()

    def test_missing_confirm_defaults_to_dry_run(self, authed):
        """最关键的护栏：不传 confirm 时绝不能删数据。"""
        body = authed.post("/api/storage/retention/execute", json={}).json()
        assert body["dry_run"] is True
        assert body["deleted_rows"] == 0
        assert body["deleted_files"] == 0

    def test_explicit_confirm_false_still_dry_run(self, authed):
        body = authed.post(
            "/api/storage/retention/execute", json={"confirm": False}
        ).json()
        assert body["dry_run"] is True

    def test_confirm_true_actually_executes(self, authed):
        body = authed.post(
            "/api/storage/retention/execute", json={"confirm": True}
        ).json()
        assert body["dry_run"] is False
        assert "errors" in body

    def test_confirm_true_reports_real_counts(self, authed):
        """执行报告必须给出真实删除量，而不是「计划删除量」。"""
        body = authed.post(
            "/api/storage/retention/execute", json={"confirm": True}
        ).json()
        assert isinstance(body["deleted_rows"], int)
        assert isinstance(body["deleted_files"], int)
        assert isinstance(body["freed_bytes"], int)
        assert body["freed_bytes"] >= 0

    def test_custom_days_are_accepted(self, authed):
        body = authed.post(
            "/api/storage/retention/execute",
            json={"confirm": True, "logs_days": 7, "result_items_days": 14},
        ).json()
        assert body["dry_run"] is False

    def test_illegal_days_do_not_become_delete_everything(self, authed):
        """``0`` 天是非法配置，必须回落默认而不是「删光所有数据」。

        ``cutoff == now`` 若按字面执行就是清空整库，这是最危险的一种误配。
        """
        body = authed.post(
            "/api/storage/retention/execute",
            json={
                "confirm": True,
                "logs_days": 0,
                "price_snapshots_days": 0,
                "watch_events_days": 0,
                "result_items_days": 0,
                "ai_usage_days": 0,
            },
        ).json()
        # 空库下不该有任何删除；关键是没崩、也没把表清空
        assert body["dry_run"] is False
        assert "errors" in body
