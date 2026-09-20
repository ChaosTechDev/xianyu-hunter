"""商品分析结果缓存。

此前的缺陷：商品分析走 ``create_ai_response_async`` 直连，绕过了
``AIClient._call_ai`` 里的 ``_shared_ai_cache()``。那份缓存的注释写明其设计
意图是「同一个商品被多个任务命中时，第二个任务直接拿缓存」，但在生产主路径上
从来没被调用过 —— 同一商品被两个关键词任务同时搜到时会各付一次 token。

这些测试锁住三件事：
  1. 相同输入第二次调用不再发起真实请求（真的省下 token）
  2. 输入或采样参数变化必须换键（防止「换提示词拿到旧结论」这类静默错误）
  3. 失败/不合规的响应绝不进缓存（否则坏数据会污染后续同商品的分析）
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from src import ai_handler

VALID_PAYLOAD = {
    "prompt_version": "EagleEye-V6.4",
    "is_recommended": True,
    "reason": "自用卡，价格合理",
    "risk_tags": [],
    # 注意：seller_type 必须在 criteria_analysis **内部**，契约校验就是这么要求的
    "criteria_analysis": {
        "model_chip": {"status": "PASS"},
        "seller_type": {"status": "PASS"},
    },
}


def _messages(text: str = "item-A"):
    return [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": text},
    ]


class _FakeCache:
    """记录调用的假缓存，避免测试依赖真实 LRU 的 TTL 行为。"""

    def __init__(self):
        self.store: dict = {}
        self.gets: list = []
        self.puts: list = []

    def get(self, key):
        self.gets.append(key)
        return self.store.get(key)

    def put(self, key, value):
        self.puts.append(key)
        self.store[key] = value


class _CallCounter:
    def __init__(self, payload=None, fail_times: int = 0):
        self.n = 0
        self.payload = VALID_PAYLOAD if payload is None else payload
        self.fail_times = fail_times

    async def __call__(self, *_a, **_kw):
        self.n += 1
        if self.n <= self.fail_times:
            # 模拟被截断的响应：缺必需字段
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content='{"is_recommended": true}', reasoning_content=None
                        ),
                    )
                ]
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        content=json.dumps(self.payload, ensure_ascii=False),
                        reasoning_content=None,
                    ),
                )
            ]
        )


@pytest.fixture
def patched(monkeypatch):
    """把请求发出、用量记录、客户端可用性都替换掉，只观察缓存行为。"""
    cache = _FakeCache()
    counter = _CallCounter()
    monkeypatch.setattr(ai_handler, "_analysis_cache", lambda: cache)
    monkeypatch.setattr(ai_handler, "client", object())
    monkeypatch.setattr(ai_handler, "record_ai_usage", lambda *a, **k: None)
    monkeypatch.setattr(
        ai_handler, "create_ai_response_async", counter, raising=False
    )
    return cache, counter


def _analyze(product_id="1", title="测试商品"):
    return asyncio.run(
        ai_handler.get_ai_analysis(
            {"商品信息": {"商品ID": product_id, "商品标题": title}},
            image_paths=None,
            prompt_text="PROMPT",
        )
    )


class TestCacheActuallySavesTokens:
    def test_second_identical_call_hits_cache(self, patched):
        cache, counter = patched
        first = _analyze()
        second = _analyze()
        assert first is not None and second is not None
        assert counter.n == 1, "第二次相同调用不应再发请求"
        assert len(cache.puts) == 1

    def test_first_call_misses(self, patched):
        cache, counter = patched
        _analyze()
        assert cache.gets, "应查询过缓存"
        assert counter.n == 1


class TestCacheKeySensitivity:
    """任何影响结果的输入变化都必须换键。"""

    def test_different_item_does_not_hit(self, patched):
        _, counter = patched
        _analyze(title="商品A")
        _analyze(title="商品B")
        assert counter.n == 2, "不同商品不应互相命中缓存"

    def test_key_depends_on_temperature(self):
        k1 = ai_handler._build_analysis_cache_key(
            _messages(), temperature=0.1, max_output_tokens=8192
        )
        k2 = ai_handler._build_analysis_cache_key(
            _messages(), temperature=0.05, max_output_tokens=8192
        )
        assert k1 != k2

    def test_key_depends_on_max_tokens(self):
        k1 = ai_handler._build_analysis_cache_key(
            _messages(), temperature=0.1, max_output_tokens=4000
        )
        k2 = ai_handler._build_analysis_cache_key(
            _messages(), temperature=0.1, max_output_tokens=8192
        )
        assert k1 != k2, "改输出预算必须换键，否则会命中按旧预算截断的结果"

    def test_key_depends_on_prompt(self):
        k1 = ai_handler._build_analysis_cache_key(
            _messages("item-A"), temperature=0.1, max_output_tokens=8192
        )
        k2 = ai_handler._build_analysis_cache_key(
            _messages("item-B"), temperature=0.1, max_output_tokens=8192
        )
        assert k1 != k2


class TestBadResultsNeverCached:
    def test_invalid_payload_is_not_cached(self, monkeypatch):
        cache = _FakeCache()
        # 每次返回缺失字段的响应 -> 全部重试都失败
        counter = _CallCounter(fail_times=99)
        monkeypatch.setattr(ai_handler, "_analysis_cache", lambda: cache)
        monkeypatch.setattr(ai_handler, "client", object())
        monkeypatch.setattr(ai_handler, "record_ai_usage", lambda *a, **k: None)
        monkeypatch.setattr(
            ai_handler, "create_ai_response_async", counter, raising=False
        )
        with pytest.raises(Exception):
            _analyze()
        assert cache.puts == [], "不合规的响应绝不能写入缓存"


class TestCacheFailureIsFailOpen:
    def test_missing_cache_does_not_break_analysis(self, monkeypatch):
        monkeypatch.setattr(ai_handler, "_analysis_cache", lambda: None)
        counter = _CallCounter()
        monkeypatch.setattr(ai_handler, "client", object())
        monkeypatch.setattr(ai_handler, "record_ai_usage", lambda *a, **k: None)
        monkeypatch.setattr(
            ai_handler, "create_ai_response_async", counter, raising=False
        )
        result = _analyze()
        assert result is not None
        assert counter.n == 1

    def test_cache_accessor_returns_the_same_instance(self):
        """必须返回同一实例。

        ``_shared_ai_cache()`` 是工厂函数，每次调用都新建空缓存。若 ``_analysis_cache()``
        每次都去调它，写入与读取会落在两个不同对象上，缓存永远不命中 ——
        实测表现为缓存条目数恒为 0、每个商品都重新付费分析。
        """
        monkeypatch_off = ai_handler._ANALYSIS_CACHE
        ai_handler._ANALYSIS_CACHE = None
        try:
            first = ai_handler._analysis_cache()
            second = ai_handler._analysis_cache()
            assert first is second, "缓存访问器必须返回同一个实例"
            if first is not None:
                first.put("probe-key", "probe-value")
                assert ai_handler._analysis_cache().get("probe-key") == "probe-value"
        finally:
            ai_handler._ANALYSIS_CACHE = monkeypatch_off
