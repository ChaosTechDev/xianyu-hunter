"""AI 预算闸门的接入测试。

``check_budget`` 本身是纯函数（已在 ``test_ai_governance_service.py`` 覆盖），
本文件守住的是**接入层**的四个关键语义：

1. 未配置 ``AI_BUDGET_LIMIT`` → 零行为（默认部署不受影响）。
2. 超限 → 抛 :class:`AIBudgetExceededError`，且**不**发出请求。
3. 命中缓存 → 不受预算影响（读缓存不产生新费用，拦它没有意义）。
4. 统计失败/配置非法 → **放行**，绝不因为一个聚合查询出错就停掉整条采集链路。
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.infrastructure.external import ai_client as module  # noqa: E402
from src.infrastructure.external.ai_client import (  # noqa: E402
    AIBudgetExceededError,
    AIClient,
)


class _StubResponse:
    def __init__(self) -> None:
        message = SimpleNamespace(content='{"n": 1}', role="assistant")
        self.choices = [SimpleNamespace(message=message, finish_reason="stop")]
        self.output_text = '{"n": 1}'
        self.usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        self.model = "stub-model"
        self.id = "stub-id"
        self.output = []


def _make_client(create_impl):
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="stub-model",
        base_url="http://stub.local/v1",
        enable_response_format=False,
        enable_thinking=False,
        image_mode="off",
    )
    client.client = SimpleNamespace(
        responses=SimpleNamespace(create=create_impl),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create_impl)),
    )
    client._temperature_unsupported = False
    return client


def _messages():
    return [{"role": "user", "content": "商品数据"}]


@pytest.fixture()
def reset_budget(monkeypatch):
    """清空预算花费缓存，保证每个用例从确定状态开始。

    ``at=None`` 才是「无缓存」：``at`` 存的是 ``time.monotonic()`` 读数，
    用 ``0.0`` 代替会被当成一个真实且仍新鲜的时间戳（新进程的 monotonic
    读数很小），从而跳过查询、用例拿到过期金额。
    """
    module._budget_spent_cache["value"] = 0.0
    module._budget_spent_cache["at"] = None
    yield
    module._budget_spent_cache["value"] = 0.0
    module._budget_spent_cache["at"] = None


def _patch_spent(monkeypatch, spent: float):
    """把已花费金额固定为 ``spent``（绕过真实聚合查询）。"""
    import src.services.ai_usage_service as usage_module

    monkeypatch.setattr(
        usage_module,
        "get_ai_usage_summary",
        lambda days=30: {"estimated_cost": spent},
    )


class TestUnconfiguredIsNoOp:
    def test_no_limit_configured_allows_and_sends_request(self, monkeypatch, reset_budget):
        monkeypatch.delenv("AI_BUDGET_LIMIT", raising=False)
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse()

        result = asyncio.run(_make_client(create)._call_ai(_messages(), temperature=0.1))
        assert result == '{"n": 1}'
        assert len(calls) == 1

    def test_blank_limit_is_treated_as_unset(self, monkeypatch, reset_budget):
        monkeypatch.setenv("AI_BUDGET_LIMIT", "   ")
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse()

        asyncio.run(_make_client(create)._call_ai(_messages(), temperature=0.11))
        assert len(calls) == 1


class TestExceededBlocks:
    def test_exceeded_raises_and_does_not_call_upstream(self, monkeypatch, reset_budget):
        """核心护栏：超限时必须拦在发请求之前，否则拦截毫无意义。"""
        monkeypatch.setenv("AI_BUDGET_LIMIT", "1.0")
        _patch_spent(monkeypatch, 5.0)
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse()

        with pytest.raises(AIBudgetExceededError):
            asyncio.run(_make_client(create)._call_ai(_messages(), temperature=0.12))
        assert calls == [], "被拦下时绝不能已经发出请求"

    def test_below_limit_passes(self, monkeypatch, reset_budget):
        monkeypatch.setenv("AI_BUDGET_LIMIT", "10.0")
        _patch_spent(monkeypatch, 1.0)
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse()

        asyncio.run(_make_client(create)._call_ai(_messages(), temperature=0.13))
        assert len(calls) == 1

    def test_warning_level_still_passes(self, monkeypatch, reset_budget, capsys):
        """预警档只提醒不拦截——否则「快超了」会变成「立刻停摆」。"""
        monkeypatch.setenv("AI_BUDGET_LIMIT", "10.0")
        monkeypatch.setenv("AI_BUDGET_WARN_RATIO", "0.5")
        _patch_spent(monkeypatch, 8.0)
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse()

        asyncio.run(_make_client(create)._call_ai(_messages(), temperature=0.14))
        assert len(calls) == 1, "预警档仍应放行"


class TestCacheBypassesBudget:
    def test_cached_hit_is_not_blocked_by_budget(self, monkeypatch, reset_budget):
        """读缓存不产生新费用，拦下它只会白白丢掉已有结果。"""
        monkeypatch.delenv("AI_BUDGET_LIMIT", raising=False)
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse()

        client = _make_client(create)
        first = asyncio.run(client._call_ai(_messages(), temperature=0.15))

        # 现在把预算压到超限，但同一请求应命中缓存照常返回
        monkeypatch.setenv("AI_BUDGET_LIMIT", "0.5")
        _patch_spent(monkeypatch, 999.0)
        module._budget_spent_cache["at"] = None

        second = asyncio.run(_make_client(create)._call_ai(_messages(), temperature=0.15))
        assert second == first
        assert len(calls) == 1, "命中缓存不该再发请求"


class TestFailOpen:
    def test_usage_query_failure_does_not_block(self, monkeypatch, reset_budget):
        """统计查询失败必须放行：不能让一个聚合 SQL 出错停掉整条链路。"""
        monkeypatch.setenv("AI_BUDGET_LIMIT", "1.0")

        import src.services.ai_usage_service as usage_module

        def boom(days=30):
            raise RuntimeError("数据库损坏")

        monkeypatch.setattr(usage_module, "get_ai_usage_summary", boom)
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse()

        asyncio.run(_make_client(create)._call_ai(_messages(), temperature=0.16))
        assert len(calls) == 1, "统计失败时应放行而非拦截"

    def test_invalid_limit_value_does_not_block(self, monkeypatch, reset_budget):
        monkeypatch.setenv("AI_BUDGET_LIMIT", "not-a-number")
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse()

        asyncio.run(_make_client(create)._call_ai(_messages(), temperature=0.17))
        assert len(calls) == 1

    def test_spend_cache_avoids_repeated_queries(self, monkeypatch, reset_budget):
        """花费金额应被缓存，否则每次 AI 调用都要多打一次聚合查询。"""
        monkeypatch.setenv("AI_BUDGET_LIMIT", "100.0")
        monkeypatch.setenv("AI_BUDGET_CACHE_SECONDS", "300")
        queries: list = []

        import src.services.ai_usage_service as usage_module

        def counting_summary(days=30):
            queries.append(days)
            return {"estimated_cost": 0.0}

        monkeypatch.setattr(usage_module, "get_ai_usage_summary", counting_summary)
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse()

        client = _make_client(create)
        asyncio.run(client._call_ai(_messages(), temperature=0.18))
        asyncio.run(client._call_ai(_messages(), temperature=0.19))
        asyncio.run(client._call_ai(_messages(), temperature=0.20))
        assert len(queries) == 1, "TTL 内不应重复查询花费"
