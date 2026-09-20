"""AI 治理服务的单元测试：并发闸、结果缓存、预算守卫。

测试全部用假时钟推进时间与 ``asyncio.run`` 驱动协程，不 ``sleep``、不联网、
不连数据库，因此既快又不 flaky。项目未配置 ``asyncio_mode``，故统一用
``asyncio.run`` 显式跑（与既有测试保持一致，不引入 pytest-asyncio 依赖）。
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from src.services.ai_governance_service import (
    DEFAULT_CACHE_MAX_ENTRIES,
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_WARN_RATIO,
    AIRequestGate,
    AIResultCache,
    build_cache_key,
    check_budget,
)


class _FakeClock:
    """可手动推进的假时钟，避免测试用 ``sleep`` 等真实时间。"""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class TestGateAcrossEventLoops:
    """回归护栏：模块级共享的闸必须在多次 ``asyncio.run`` 之间可复用。

    这是一个实际踩到的缺陷：``asyncio.Semaphore`` 在**发生争用**（有等待者）时会把
    future 绑定到当时的事件循环，之后在另一个事件循环里使用会抛
    ``RuntimeError: ... is bound to a different event loop``。

    项目里既有「每个任务各跑一次 ``asyncio.run``」的路径，也有常驻 API 进程的
    单一长驻循环；若闸在导入时创建并复用，前一种路径会在第二次运行时报错。
    注意：**必须制造争用**才能暴露该问题——无争用时不创建 future，症状不会出现。
    """

    @staticmethod
    def _contended_run(gate: AIRequestGate) -> None:
        async def held() -> None:
            async with gate:
                await asyncio.sleep(0.01)

        async def waiter() -> None:
            async with gate:
                await asyncio.sleep(0.001)

        async def main() -> None:
            await asyncio.gather(held(), waiter())

        asyncio.run(main())

    def test_shared_gate_is_reusable_across_separate_event_loops(self):
        gate = AIRequestGate(1)  # 上限 1 才能让第二个调用真的排队等待
        for _ in range(3):
            self._contended_run(gate)
        assert gate.available == 1

    def test_concurrency_limit_still_holds_within_one_loop(self):
        gate = AIRequestGate(2)
        peak = 0

        async def worker() -> None:
            nonlocal peak
            async with gate:
                peak = max(peak, gate.in_flight)
                await asyncio.sleep(0.01)

        async def main() -> None:
            await asyncio.gather(*[worker() for _ in range(8)])

        asyncio.run(main())
        assert peak == 2, "同一循环内上限必须仍然生效"
        assert gate.available == 2

    def test_available_never_goes_negative(self):
        gate = AIRequestGate(1)
        assert gate.available == 1
        # 人为制造计数超限（模拟跨循环累计），available 应被钳制在 0
        gate._in_flight = 5
        assert gate.available == 0


# --------------------------------------------------------------------------- #
# 1.1 全局并发闸
# --------------------------------------------------------------------------- #


def test_gate_defaults_to_four_slots():
    gate = AIRequestGate()

    assert gate.max_concurrency == DEFAULT_MAX_CONCURRENCY == 4
    assert gate.in_flight == 0
    assert gate.available == 4


def test_gate_acquire_and_release_updates_counters():
    async def scenario():
        gate = AIRequestGate(2)
        observed = []
        async with gate:
            observed.append((gate.in_flight, gate.available))
            async with gate:
                observed.append((gate.in_flight, gate.available))
            observed.append((gate.in_flight, gate.available))
        observed.append((gate.in_flight, gate.available))
        return observed

    assert asyncio.run(scenario()) == [(1, 1), (2, 0), (1, 1), (0, 2)]


def test_gate_enforces_concurrency_limit_and_queues_excess():
    async def scenario():
        gate = AIRequestGate(2)
        peak = 0
        order: list[int] = []

        async def worker(index: int) -> None:
            nonlocal peak
            async with gate:
                peak = max(peak, gate.in_flight)
                order.append(index)
                # 让出若干轮控制权，保证前两个 worker 真正重叠而不是串行。
                for _ in range(5):
                    await asyncio.sleep(0)
                await asyncio.sleep(0.01)

        await asyncio.gather(*(worker(i) for i in range(6)))
        return gate, peak, order

    gate, peak, order = asyncio.run(scenario())

    assert peak == 2, "同时在飞的请求数不得超过 max_concurrency"
    assert len(order) == 6, "全部 6 个请求最终都要执行完，超限的是排队而非失败"
    assert gate.in_flight == 0
    assert gate.available == 2


def test_gate_max_one_serializes_workers_in_arrival_order():
    async def scenario():
        gate = AIRequestGate(1)
        order: list[str] = []

        async def holder() -> None:
            async with gate:
                order.append("first")
                await asyncio.sleep(0.02)

        async def waiter() -> None:
            async with gate:
                order.append("second")

        await asyncio.gather(holder(), waiter())
        return order

    # 若闸门没生效，两个 worker 的 append 顺序会变成不确定的。
    assert asyncio.run(scenario()) == ["first", "second"]


def test_gate_releases_slot_when_body_raises():
    async def scenario():
        gate = AIRequestGate(1)
        async with gate:
            pass

        with pytest.raises(RuntimeError, match="boom"):
            async with gate:
                assert gate.in_flight == 1
                assert gate.available == 0
                raise RuntimeError("boom")

        snapshot = (gate.in_flight, gate.available)
        # 异常之后闸门必须仍可再次正常使用。
        async with gate:
            pass
        return snapshot, (gate.in_flight, gate.available)

    after_exception, after_reuse = asyncio.run(scenario())

    assert after_exception == (0, 1), "异常路径必须归还槽位"
    assert after_reuse == (0, 1)


def test_gate_releases_slot_on_cancellation():
    async def scenario():
        gate = AIRequestGate(1)
        started = asyncio.Event()

        async def holder() -> None:
            async with gate:
                started.set()
                await asyncio.sleep(5)

        task = asyncio.create_task(holder())
        await started.wait()
        assert gate.available == 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return gate.in_flight, gate.available

    assert asyncio.run(scenario()) == (0, 1), "取消同样不得泄漏槽位"


def test_gate_serializes_more_requests_than_slots():
    async def scenario():
        gate = AIRequestGate(2)
        peak = 0
        completed = 0

        async def worker() -> None:
            nonlocal peak, completed
            async with gate:
                peak = max(peak, gate.in_flight)
                await asyncio.sleep(0.005)
                completed += 1

        await asyncio.gather(*(worker() for _ in range(10)))
        return peak, completed

    peak, completed = asyncio.run(scenario())

    assert peak == 2
    assert completed == 10


@pytest.mark.parametrize(
    "bad_value",
    [0, -1, -100, None, "4", "abc", float("nan"), float("inf"), True, False, [], {}],
)
def test_gate_invalid_max_concurrency_falls_back_to_default(bad_value):
    gate = AIRequestGate(bad_value)

    assert gate.max_concurrency == 4
    assert gate.available == 4


def test_gate_float_max_concurrency_is_truncated():
    assert AIRequestGate(2.9).max_concurrency == 2
    assert AIRequestGate(0.5).max_concurrency == 4, "取整后小于 1 视为非法，回落默认"


# --------------------------------------------------------------------------- #
# 1.2 结果缓存
# --------------------------------------------------------------------------- #


def test_cache_put_then_get_returns_same_value():
    cache = AIResultCache()
    payload = {"verdict": "推荐", "score": 87}

    assert cache.get("missing") is None
    assert cache.size == 0

    cache.put("item:1", payload)
    assert cache.get("item:1") is payload
    assert cache.size == 1

    cache.clear()
    assert cache.size == 0
    assert cache.get("item:1") is None


def test_cache_entry_expires_after_ttl_with_fake_clock():
    clock = _FakeClock()
    cache = AIResultCache(ttl_seconds=60.0, clock=clock)

    cache.put("k", "v")
    clock.advance(59.0)
    assert cache.get("k") == "v", "TTL 内必须命中"

    clock.advance(1.0)
    assert cache.get("k") is None, "到达 TTL 边界即视为过期"
    assert cache.size == 0, "过期条目在 get 时被清除"


def test_cache_put_refreshes_ttl():
    clock = _FakeClock()
    cache = AIResultCache(ttl_seconds=100.0, clock=clock)

    cache.put("k", "v1")
    clock.advance(90.0)
    cache.put("k", "v2")
    clock.advance(90.0)

    assert cache.get("k") == "v2", "重新 put 应重置过期时刻"
    assert cache.size == 1, "重复 put 不得让条目数虚增"


def test_cache_evicts_least_recently_used_entry():
    cache = AIResultCache(ttl_seconds=1000.0, max_entries=3)

    cache.put("a", "A")
    cache.put("b", "B")
    cache.put("c", "C")
    # 访问 a 让它变成最近使用，此时最旧的是 b。
    assert cache.get("a") == "A"

    cache.put("d", "D")

    assert cache.size == 3
    assert cache.get("b") is None, "最久未使用的 b 应先被淘汰"
    assert cache.get("a") == "A"
    assert cache.get("c") == "C"
    assert cache.get("d") == "D"


def test_cache_put_updates_lru_position():
    cache = AIResultCache(max_entries=2)

    cache.put("a", "A")
    cache.put("b", "B")
    # 重写 a 应把它提到最近使用位置，于是 b 成为最旧的。
    cache.put("a", "A2")
    cache.put("c", "C")

    assert cache.get("b") is None
    assert cache.get("a") == "A2"
    assert cache.get("c") == "C"


def test_cache_put_prunes_expired_entries_before_eviction():
    clock = _FakeClock()
    cache = AIResultCache(ttl_seconds=10.0, max_entries=2, clock=clock)

    cache.put("stale", "old")
    clock.advance(11.0)
    cache.put("fresh1", "F1")
    cache.put("fresh2", "F2")

    assert cache.size == 2, "过期条目应被清理，而不是占着 LRU 名额挤掉新条目"
    assert cache.get("fresh1") == "F1"
    assert cache.get("fresh2") == "F2"


def test_cache_respects_max_entries_bound_over_many_writes():
    cache = AIResultCache(max_entries=5)

    for index in range(50):
        cache.put(f"k{index}", index)

    assert cache.size == 5
    assert cache.get("k49") == 49
    assert cache.get("k44") is None


@pytest.mark.parametrize("bad_ttl", [None, -1, "60", float("nan"), True, float("-inf"), []])
def test_cache_invalid_ttl_falls_back_to_default(bad_ttl):
    cache = AIResultCache(ttl_seconds=bad_ttl)

    assert cache.ttl_seconds == DEFAULT_CACHE_TTL_SECONDS == 900.0


@pytest.mark.parametrize("bad_max", [0, -3, None, "500", float("nan"), True, 0.9])
def test_cache_invalid_max_entries_falls_back_to_default(bad_max):
    cache = AIResultCache(max_entries=bad_max)

    assert cache.max_entries == DEFAULT_CACHE_MAX_ENTRIES == 500


def test_cache_zero_ttl_means_immediately_expired():
    clock = _FakeClock()
    cache = AIResultCache(ttl_seconds=0.0, clock=clock)

    cache.put("k", "v")

    assert cache.ttl_seconds == 0.0, "0 是合法 TTL（立刻过期），不该被回落覆盖"
    assert cache.get("k") is None
    assert cache.size == 0


def test_cache_injected_clock_is_used():
    clock = _FakeClock(start=42.0)
    cache = AIResultCache(ttl_seconds=5.0, clock=clock)

    cache.put("k", "v")
    clock.advance(4.999)
    assert cache.get("k") == "v"
    clock.advance(0.001)
    assert cache.get("k") is None


def test_cache_is_usable_from_multiple_threads():
    cache = AIResultCache(max_entries=50)
    errors: list[BaseException] = []

    def worker(base: int) -> None:
        try:
            for index in range(200):
                key = f"k{(base * 200 + index) % 80}"
                cache.put(key, index)
                cache.get(key)
        except BaseException as exc:  # pragma: no cover - 仅在实现有竞态时触发
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert cache.size <= 50


def test_cache_async_usage_with_asyncio():
    async def scenario():
        cache = AIResultCache(ttl_seconds=30.0)
        calls = 0

        async def analyze(item_id: str) -> str:
            nonlocal calls
            key = build_cache_key("deepseek-chat", "prompt-hash", item_id)
            cached = cache.get(key)
            if cached is not None:
                return cached
            calls += 1
            await asyncio.sleep(0)
            result = f"result-for-{item_id}"
            cache.put(key, result)
            return result

        first = await analyze("ITEM-1")
        second = await analyze("ITEM-1")
        third = await analyze("ITEM-2")
        return calls, first, second, third, cache.size

    calls, first, second, third, size = asyncio.run(scenario())

    assert (first, second, third) == ("result-for-ITEM-1", "result-for-ITEM-1", "result-for-ITEM-2")
    assert calls == 2, "重复分析同一商品应命中缓存，不重复计算"
    assert size == 2


# --------------------------------------------------------------------------- #
# build_cache_key
# --------------------------------------------------------------------------- #


def test_build_cache_key_is_stable_and_order_sensitive():
    first = build_cache_key("deepseek-chat", "hash123", "ITEM-1")
    second = build_cache_key("deepseek-chat", "hash123", "ITEM-1")

    assert first == second, "相同片段必须产生稳定 key"
    assert build_cache_key("deepseek-chat", "hash123", "ITEM-2") != first
    assert "ITEM-1" in first
    assert "deepseek-chat" in first


def test_build_cache_key_separator_prevents_collisions():
    assert build_cache_key("a", "bc") != build_cache_key("ab", "c")
    assert build_cache_key("a", "b") == f"a\x1fb"


def test_build_cache_key_filters_none_but_keeps_falsy_values():
    assert build_cache_key("model", None, "ITEM-1") == "model\x1fITEM-1"
    assert build_cache_key(None, None) == ""
    # 0 与 False 是合法取值，不能被当成「缺失」过滤掉。
    assert build_cache_key("model", 0) == "model\x1f0"
    assert build_cache_key("model", False) == "model\x1fFalse"
    assert build_cache_key("model", 0) != build_cache_key("model")


def test_build_cache_key_accepts_numbers():
    assert build_cache_key("task", 42, 3.5) == "task\x1f42\x1f3.5"


# --------------------------------------------------------------------------- #
# 1.3 预算守卫
# --------------------------------------------------------------------------- #


def test_check_budget_level_ok():
    result = check_budget(spent=10.0, limit=100.0)

    assert result == {"allow": True, "level": "ok", "message": None}


def test_check_budget_level_warning_at_default_threshold():
    result = check_budget(spent=80.0, limit=100.0)

    assert result["allow"] is True
    assert result["level"] == "warning"
    assert result["message"] is not None
    assert "即将用完" in result["message"]
    assert "80.0%" in result["message"]


def test_check_budget_level_warning_just_below_limit():
    result = check_budget(spent=99.99, limit=100.0)

    assert result["allow"] is True
    assert result["level"] == "warning"


def test_check_budget_level_exceeded():
    result = check_budget(spent=100.0, limit=100.0)

    assert result["allow"] is False
    assert result["level"] == "exceeded"
    assert result["message"] is not None
    assert "已超出" in result["message"]
    assert "拦截" in result["message"]


def test_check_budget_level_exceeded_far_over_limit():
    result = check_budget(spent=1000.0, limit=100.0)

    assert result["allow"] is False
    assert result["level"] == "exceeded"


def test_check_budget_level_unlimited_when_limit_is_none():
    result = check_budget(spent=5.0, limit=None)

    assert result["allow"] is True
    assert result["level"] == "unlimited"
    assert result["message"] is not None


@pytest.mark.parametrize(
    "bad_limit",
    [None, 0, 0.0, -1, -0.01, "100", float("nan"), float("inf"), float("-inf"), True, False, []],
)
def test_check_budget_invalid_limit_is_unlimited(bad_limit):
    result = check_budget(spent=1_000_000.0, limit=bad_limit)

    assert result["allow"] is True
    assert result["level"] == "unlimited"


@pytest.mark.parametrize(
    "dirty_spent",
    [None, float("nan"), float("inf"), float("-inf"), -1.0, -999.9, "abc", [], {}],
)
def test_check_budget_dirty_spent_is_treated_as_zero(dirty_spent):
    result = check_budget(spent=dirty_spent, limit=100.0)

    assert result["allow"] is True, "脏数据不该拦下所有请求"
    assert result["level"] == "ok"
    assert result["message"] is None


def test_check_budget_dirty_spent_with_warning_ratio_never_exceeds():
    result = check_budget(spent=float("inf"), limit=0.5, warn_ratio=0.8)

    assert result["allow"] is True
    assert result["level"] == "ok"


def test_check_budget_custom_warn_ratio():
    assert check_budget(spent=50.0, limit=100.0, warn_ratio=0.5)["level"] == "warning"
    assert check_budget(spent=49.9, limit=100.0, warn_ratio=0.5)["level"] == "ok"


@pytest.mark.parametrize("bad_ratio", [None, 0, -0.1, 1.5, 2, "0.5", float("nan"), float("inf"), True])
def test_check_budget_invalid_warn_ratio_falls_back_to_default(bad_ratio):
    assert DEFAULT_WARN_RATIO == 0.8
    result = check_budget(spent=80.0, limit=100.0, warn_ratio=bad_ratio)

    assert result["level"] == "warning", "非法阈值回落 0.8，80 应落在预警区间"


def test_check_budget_ratio_one_never_reports_warning():
    assert check_budget(spent=99.99, limit=100.0, warn_ratio=1.0)["level"] == "ok"
    # 先判超限：正好 100 的时候是 exceeded 而不是 warning。
    assert check_budget(spent=100.0, limit=100.0, warn_ratio=1.0)["level"] == "exceeded"


def test_check_budget_zero_spent_is_ok():
    result = check_budget(spent=0.0, limit=10.0)

    assert result == {"allow": True, "level": "ok", "message": None}


def test_check_budget_allows_exactly_when_level_is_not_exceeded():
    verdicts = {
        spent: check_budget(spent=spent, limit=100.0)
        for spent in (0.0, 79.99, 80.0, 99.99, 100.0, 250.0)
    }

    assert verdicts[0.0] == {"allow": True, "level": "ok", "message": None}
    assert verdicts[79.99]["level"] == "ok"
    assert verdicts[80.0]["level"] == "warning"
    assert verdicts[99.99]["level"] == "warning"
    assert verdicts[100.0]["level"] == "exceeded"
    assert verdicts[250.0]["allow"] is False
    assert all(item["allow"] is (item["level"] != "exceeded") for item in verdicts.values())
