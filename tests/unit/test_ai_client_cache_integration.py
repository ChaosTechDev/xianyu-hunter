"""``AIClient`` 结果缓存的接入测试。

缓存是「省钱」的手段，但它有两种典型的危险失效方式，本文件专门守住：

1. **错配**：键没覆盖某个影响结果的维度（提示词、温度、模型、图片），
   于是换了输入却拿到旧结论。这类错误静默且极难排查。
2. **跨用例泄漏**：缓存是模块级全局单例，若不复位，前一个用例的成功响应
   会让后一个用例的「重试/抛错」断言全部失效。

同时确认缓存**只缓存成功响应**：失败与空响应绝不能进缓存，否则一次瞬时故障
会被固化成一小时内所有商品都拿不到分析结果。
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.infrastructure.external.ai_client import AIClient  # noqa: E402


class _StubResponse:
    """最小可用的上游响应替身，只提供 ``extract_ai_response_content`` 需要的形状。"""

    def __init__(self, text: str = '{"ok": true}', usage: object | None = None):
        message = SimpleNamespace(content=text, role="assistant")
        choice = SimpleNamespace(message=message, finish_reason="stop")
        self.choices = [choice]
        self.output_text = text
        self.usage = usage or SimpleNamespace(
            prompt_tokens=10, completion_tokens=5, total_tokens=15
        )
        self.model = "stub-model"
        self.id = "stub-id"
        self.output = []


def _make_client(create_impl, *, calls: list | None = None):
    """构造一个绕过 ``__init__`` 的 AIClient，注入 stub 上游。"""
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


def _messages(tag: str = "A"):
    return [
        {"role": "system", "content": "分析商品"},
        {"role": "user", "content": f"商品数据 {tag}"},
    ]


class TestCacheHits:
    def test_identical_request_served_from_cache(self):
        """同参数第二次调用不应再发请求。"""
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse('{"n": 1}')

        client = _make_client(create)
        first = asyncio.run(client._call_ai(_messages(), temperature=0.1))
        second = asyncio.run(client._call_ai(_messages(), temperature=0.1))

        assert first == second
        assert len(calls) == 1, "第二次应命中缓存，不该再发请求"

    def test_different_prompt_is_a_cache_miss(self):
        """核心正确性：提示词不同必须重新请求，绝不能复用旧结论。"""
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse('{"n": 1}')

        client = _make_client(create)
        asyncio.run(client._call_ai(_messages("A"), temperature=0.1))
        asyncio.run(client._call_ai(_messages("B"), temperature=0.1))
        assert len(calls) == 2

    def test_different_temperature_is_a_cache_miss(self):
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse('{"n": 1}')

        client = _make_client(create)
        asyncio.run(client._call_ai(_messages(), temperature=0.1))
        asyncio.run(client._call_ai(_messages(), temperature=0.9))
        assert len(calls) == 2

    def test_different_model_is_a_cache_miss(self):
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse('{"n": 1}')

        first = _make_client(create)
        asyncio.run(first._call_ai(_messages(), temperature=0.1))

        second = _make_client(create)
        second.settings.model_name = "another-model"
        asyncio.run(second._call_ai(_messages(), temperature=0.1))
        assert len(calls) == 2

    def test_different_max_output_tokens_is_a_cache_miss(self):
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse('{"n": 1}')

        client = _make_client(create)
        asyncio.run(client._call_ai(_messages(), max_output_tokens=100))
        asyncio.run(client._call_ai(_messages(), max_output_tokens=500))
        assert len(calls) == 2


class TestCacheDoesNotMaskFailures:
    def test_error_is_not_cached(self):
        """失败进缓存会把一次瞬时故障固化住，必须只缓存成功响应。"""
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            raise RuntimeError("上游 500")

        client = _make_client(create)
        with pytest.raises(Exception):
            asyncio.run(client._call_ai(_messages(), temperature=0.1))
        with pytest.raises(Exception):
            asyncio.run(client._call_ai(_messages(), temperature=0.1))
        assert len(calls) >= 2, "失败不应进缓存，第二次必须真的重试"

    def test_empty_response_is_not_cached(self):
        """空响应不是有效结论，不能缓存。"""
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            raise ValueError("AI响应内容为空")

        client = _make_client(create)
        for _ in range(2):
            with pytest.raises(Exception):
                asyncio.run(client._call_ai(_messages(), temperature=0.1))
        assert len(calls) >= 2


class TestCacheIsGloballyShared:
    def test_two_clients_share_one_cache(self):
        """跨任务共享是缓存的省钱前提：不同 AIClient 实例应命中同一份缓存。"""
        calls: list = []

        async def create(**kwargs):
            calls.append(kwargs)
            return _StubResponse('{"n": 1}')

        first = _make_client(create)
        asyncio.run(first._call_ai(_messages(), temperature=0.1))

        second = _make_client(create)
        asyncio.run(second._call_ai(_messages(), temperature=0.1))
        assert len(calls) == 1, "全局共享缓存应让第二个实例直接命中"


class TestConftestIsolationWorks:
    def test_cache_starts_empty_in_each_test(self):
        """元测试：确认 conftest 的清缓存夹具生效。

        若夹具失效，本断言会看到上一个用例写入的条目。这也正是本次接入过程中
        真实踩到的坑——全局缓存泄漏曾让 4 个重试类用例全部误通过。
        """
        from src.infrastructure.external import ai_client as module

        assert module._SHARED_AI_CACHE.size == 0
