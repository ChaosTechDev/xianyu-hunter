"""探活接入 watch_service.finalize_watch_scan 的集成契约测试。

这是 Phase 3「协议层状态核验」的落地点：关注列表原本只能靠「这轮没采到」判死，
而那是弱信号（闲鱼翻页不稳、风控吞结果，在售商品可能某一轮就采不到）。
现在判死前会先调详情接口拿权威 ``ret`` 码，把误杀率降下来。

本文件用**注入的探活回调**验证接入逻辑，不联网：
- 探活说「活」→ 不判死且缺失计数清零
- 探活说「死」→ 立即判死，不必等缺失阈值
- 探活返回 None（配额用尽/未探活）→ 回退到连续缺失阈值
- 探活自身抛异常 → 不影响主流程
- 不注入探活回调 → 行为与改造前完全一致

同时覆盖 LivenessProbe 的配额与缓存语义。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.services.liveness_service import LivenessProbe
from src.services.watch_service import (
    DELISTED_MISSING_RUNS,
    add_watch_item,
    finalize_watch_scan,
)
from src.services.xy_protocol import ItemStatus


TASK = "探活测试任务"
OTHER_SEEN = {"SOME_OTHER_ITEM"}


class FakeProbe:
    """可编程的探活回调，记录调用过的商品 ID。"""

    def __init__(self, results: dict[str, ItemStatus | None] | None = None):
        self.results = results or {}
        self.calls: list[str] = []

    async def __call__(self, item_id: str):
        self.calls.append(item_id)
        return self.results.get(item_id)


def _make_watch(item_id: str, *, task_name: str = TASK) -> int:
    """创建一个关注项，返回其 id。

    ``add_watch_item`` 是 async 且接收 payload dict；插入后按 item_id 查回 id。
    """
    asyncio.run(
        add_watch_item(
            {
                "task_name": task_name,
                "item_id": item_id,
                "title": f"商品 {item_id}",
                "link": f"https://www.goofish.com/item?id={item_id}",
                "last_price": "100",
                "alert_price": None,
            }
        )
    )
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection

    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT id FROM watch_items WHERE item_id = ?", (item_id,)
        ).fetchone()
    return int(row["id"])


def _read_watch(watch_id: int) -> dict:
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection

    with sqlite_connection() as conn:
        row = conn.execute("SELECT * FROM watch_items WHERE id = ?", (watch_id,)).fetchone()
    return dict(row)


def _count_events(watch_id: int) -> int:
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection

    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM watch_events WHERE watch_item_id = ?", (watch_id,)
        ).fetchone()
    return int(row["c"])


def _scan(probe=None, *, seen=None, task_name: str = TASK):
    return asyncio.run(
        finalize_watch_scan(
            task_name=task_name,
            seen_item_ids=seen if seen is not None else set(OTHER_SEEN),
            run_id="run-1",
            liveness_probe=probe,
        )
    )


class TestProbeSaysAlive:
    def test_alive_probe_prevents_death_even_after_many_missing_runs(self):
        """探活确认在售时，无论缺失多少轮都不判死，且缺失计数清零。"""
        wid = _make_watch("ITEM_ALIVE")
        probe = FakeProbe({"ITEM_ALIVE": ItemStatus("ITEM_ALIVE", True, "详情接口返回 SUCCESS")})

        # 连续跑 DELISTED_MISSING_RUNS + 2 轮，远超缺失阈值
        for _ in range(DELISTED_MISSING_RUNS + 2):
            _scan(probe)
            row = _read_watch(wid)
            assert row["dead"] == 0, "探活确认在售时绝不能判死"
            assert row["missing_runs"] == 0, "确认存活后缺失计数必须清零"

        assert _count_events(wid) == 0, "未死亡不应产生下架/售出事件"
        assert probe.calls.count("ITEM_ALIVE") >= 2, "缺失轮次中应持续探活"

    def test_without_probe_missing_runs_accumulate_and_eventually_die(self):
        """对照组：不注入探活时，行为与改造前一致——连续缺失达到阈值即判死。"""
        wid = _make_watch("ITEM_NOPROBE")
        for _ in range(DELISTED_MISSING_RUNS):
            _scan(None)
        row = _read_watch(wid)
        assert row["dead"] == 1
        assert _count_events(wid) == 1


class TestProbeSaysDead:
    def test_dead_probe_judges_dead_immediately_without_waiting_threshold(self):
        """拿到明确死亡信号应立即判死，不必等满缺失阈值。"""
        wid = _make_watch("ITEM_DEAD")
        probe = FakeProbe(
            {"ITEM_DEAD": ItemStatus("ITEM_DEAD", False, "已删除或不存在",
                                     raw_ret=["FAIL_BIZ_ITEM_DEL_NOT_FOUND::x"])}
        )

        _scan(probe)  # 只跑一轮，远未达阈值

        row = _read_watch(wid)
        assert row["dead"] == 1, "有权威死亡信号时首轮即应判死"
        assert row["dead_reason"] is not None
        assert _count_events(wid) == 1, "应产生一条售出/下架事件"

    def test_deleted_reason_maps_to_deleted_label(self):
        """探活原因含「删除」时事件类型应归类为删除而非下架。"""
        wid = _make_watch("ITEM_DEL")
        probe = FakeProbe({"ITEM_DEL": ItemStatus("ITEM_DEL", False, "已删除或不存在")})
        _scan(probe)
        row = _read_watch(wid)
        assert row["dead_reason"] == "deleted"

    def test_delisted_reason_maps_to_delisted_label(self):
        wid = _make_watch("ITEM_DOWN")
        probe = FakeProbe({"ITEM_DOWN": ItemStatus("ITEM_DOWN", False, "已下架")})
        _scan(probe)
        assert _read_watch(wid)["dead_reason"] == "delisted"


class TestProbeReturnsNone:
    def test_none_probe_falls_back_to_missing_threshold(self):
        """返回 None 表示「未探活」（配额用尽），应回退到缺失兜底，不能当成判活。

        阈值语义：缺失计数在**第 DELISTED_MISSING_RUNS 轮**达到阈值即判死，
        因此前 ``DELISTED_MISSING_RUNS - 1`` 轮不应判死。
        """
        wid = _make_watch("ITEM_NONE")
        probe = FakeProbe({"ITEM_NONE": None})

        for i in range(DELISTED_MISSING_RUNS - 1):
            _scan(probe)
            row = _read_watch(wid)
            assert row["dead"] == 0, f"第 {i + 1} 轮未达阈值，不应判死"
            assert row["missing_runs"] == i + 1

        # 达到阈值的那一轮
        _scan(probe)
        assert _read_watch(wid)["dead"] == 1
        assert probe.calls == ["ITEM_NONE"] * DELISTED_MISSING_RUNS, "每轮都应尝试探活"

    def test_unknown_item_returns_none_and_falls_back(self):
        wid = _make_watch("ITEM_UNKNOWN")
        probe = FakeProbe({})  # 没有该商品的配置 → 返回 None
        for _ in range(DELISTED_MISSING_RUNS):
            _scan(probe)
        assert _read_watch(wid)["dead"] == 1


class TestProbeRaises:
    def test_probe_exception_does_not_break_scan(self):
        """探活自身异常必须被吞掉，不能影响关注列表主流程。"""

        class BoomProbe:
            async def __call__(self, item_id: str):
                raise RuntimeError("probe exploded")

        wid = _make_watch("ITEM_BOOM")
        for _ in range(DELISTED_MISSING_RUNS):
            _scan(BoomProbe())
        # 异常被吞，回退到缺失兜底逻辑，仍然正常判死
        assert _read_watch(wid)["dead"] == 1


class TestSeenItemsUnaffected:
    def test_items_seen_this_round_are_not_probed(self):
        """本轮采集到的商品不该消耗探活配额。"""
        wid = _make_watch("ITEM_SEEN")
        probe = FakeProbe({})
        _scan(probe, seen={"ITEM_SEEN"})
        assert probe.calls == [], "已在采集结果中的商品无需探活"
        assert _read_watch(wid)["dead"] == 0

    def test_empty_seen_set_returns_early(self):
        """整轮采集失败（seen 为空）时不得推进缺失计数，避免误杀全部关注项。"""
        wid = _make_watch("ITEM_EMPTY")
        probe = FakeProbe({})
        events = _scan(probe, seen=set())
        assert events == []
        assert probe.calls == []
        assert _read_watch(wid)["missing_runs"] == 0


class TestLivenessProbeQuota:
    def test_quota_exhaustion_returns_none(self, monkeypatch):
        """配额用尽后必须返回 None（未探活），而不是伪造一个判定结果。"""
        import src.services.liveness_service as mod

        async def fake_probe(item_id, *, storage_state=None, timeout_ms=25000):
            return ItemStatus(item_id, True, "fake")

        monkeypatch.setattr(mod, "probe_item_alive", fake_probe)
        probe = LivenessProbe(max_probes=2, min_interval=0)

        ids = [asyncio.run(probe(f"ITEM_{i}")) for i in range(4)]
        assert ids[0] is not None and ids[1] is not None
        assert ids[2] is None and ids[3] is None
        assert probe.exhausted is True
        assert probe.probes_used == 2

    def test_repeated_item_uses_cache_and_does_not_consume_extra_quota(self, monkeypatch):
        import src.services.liveness_service as mod

        calls = []

        async def fake_probe(item_id, *, storage_state=None, timeout_ms=25000):
            calls.append(item_id)
            return ItemStatus(item_id, True, "fake")

        monkeypatch.setattr(mod, "probe_item_alive", fake_probe)
        probe = LivenessProbe(max_probes=1, min_interval=0)

        first = asyncio.run(probe("ITEM_X"))
        second = asyncio.run(probe("ITEM_X"))

        assert first is second, "同一商品的重复查询应命中缓存"
        assert calls == ["ITEM_X"], "不应重复发起网络探活"
        assert probe.probes_used == 1

    def test_zero_quota_never_probes(self, monkeypatch):
        import src.services.liveness_service as mod

        async def fake_probe(item_id, *, storage_state=None, timeout_ms=25000):
            raise AssertionError("不应被调用")

        monkeypatch.setattr(mod, "probe_item_alive", fake_probe)
        probe = LivenessProbe(max_probes=0, min_interval=0)
        assert asyncio.run(probe("ITEM_Y")) is None
        assert probe.exhausted is True

    def test_empty_item_id_returns_none(self):
        probe = LivenessProbe(max_probes=5, min_interval=0)
        assert asyncio.run(probe("")) is None
