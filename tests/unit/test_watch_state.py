"""关注状态机与通知决策的单元测试。

重点覆盖异常分支与边界，而不是 happy path —— 这些逻辑一旦出错，
表现是「用户收到假通知」或「该通知的没通知」，线上很难排查。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.services.watch_state import (
    REASON_DELETED,
    REASON_MISSING,
    REASON_SOLD,
    DeathDecision,
    detect_reduce_price_delta,
    evaluate_death_signal,
    is_muted,
    resolve_notify_flag,
    resolve_sticky_death,
    should_emit_edge,
)


class TestEvaluateDeathSignal:
    """死亡判定：显式信号优先，缺失兜底，保守判活。"""

    def test_explicit_dead_signal_wins_immediately(self):
        """拿到明确死亡信号应立刻判死，无需等待连续缺失。"""
        d = evaluate_death_signal(
            alive_signal=False, missing_runs=0, explicit_reason=REASON_SOLD
        )
        assert d.dead is True
        assert d.reason == REASON_SOLD

    def test_explicit_alive_signal_clears(self):
        d = evaluate_death_signal(alive_signal=True, missing_runs=99)
        assert d.dead is False, "确认存活时，缺失计数不应导致误杀"

    def test_none_signal_below_threshold_stays_alive(self):
        """未探活 + 缺失未达阈值 -> 保守判活。"""
        d = evaluate_death_signal(alive_signal=None, missing_runs=2, delisted_missing_runs=3)
        assert d.dead is False

    def test_none_signal_at_threshold_dies(self):
        d = evaluate_death_signal(alive_signal=None, missing_runs=3, delisted_missing_runs=3)
        assert d.dead is True
        assert d.reason == REASON_MISSING

    def test_probe_failure_never_kills(self):
        """探活失败（None）是保守判活，绝不当成死亡。

        这是最重要的一条：风控/超时导致探不到，不能误杀在售商品。
        """
        for missing in (0, 1, 2):
            d = evaluate_death_signal(alive_signal=None, missing_runs=missing)
            assert d.dead is False, f"missing_runs={missing} 时不应判死"

    def test_default_reason_when_not_specified(self):
        d = evaluate_death_signal(alive_signal=False, missing_runs=0)
        assert d.dead is True
        assert d.reason is not None

    def test_deleted_reason_passthrough(self):
        d = evaluate_death_signal(
            alive_signal=False, missing_runs=0, explicit_reason=REASON_DELETED
        )
        assert d.reason == REASON_DELETED


class TestStickyDeath:
    """粘性死亡：一旦死了，缺证据不足以复活。"""

    def test_already_dead_stays_dead_without_evidence(self):
        """已死 + 本次无明确存活证据 -> 维持死亡（防抖核心）。"""
        was_dead = True
        decision = evaluate_death_signal(alive_signal=None, missing_runs=1)
        merged = resolve_sticky_death(was_dead=was_dead, decision=decision)
        assert merged.dead is True, "缺失不应推翻已确认的死亡状态"

    def test_already_dead_revives_only_on_confirmed_alive(self):
        was_dead = True
        decision = evaluate_death_signal(alive_signal=True, missing_runs=0)
        merged = resolve_sticky_death(was_dead=was_dead, decision=decision)
        assert merged.dead is False, "确认存活时必须能复活"

    def test_not_dead_passes_through(self):
        decision = DeathDecision(dead=True, reason=REASON_MISSING, detail="x")
        merged = resolve_sticky_death(was_dead=False, decision=decision)
        assert merged.dead is True

    def test_dead_stays_dead_when_still_dead(self):
        was_dead = True
        decision = evaluate_death_signal(alive_signal=False, missing_runs=0)
        merged = resolve_sticky_death(was_dead=was_dead, decision=decision)
        assert merged.dead is True

    def test_sticky_prevents_flapping(self):
        """模拟风控抖动：连续多轮缺失/探活失败，状态必须稳定为死。"""
        was_dead = False
        # 第一轮：明确死亡
        d1 = evaluate_death_signal(alive_signal=False, missing_runs=0)
        s1 = resolve_sticky_death(was_dead=was_dead, decision=d1)
        assert s1.dead is True
        # 后续三轮都是「探不到」，不能复活
        for _ in range(3):
            d = evaluate_death_signal(alive_signal=None, missing_runs=1)
            s = resolve_sticky_death(was_dead=True, decision=d)
            assert s.dead is True, "抖动期不得反复横跳"


class TestEdgeTrigger:
    """边沿触发：只在跃迁瞬间通知一次。"""

    def test_alive_to_dead_emits(self):
        assert should_emit_edge(was_dead=False, dead=True) is True

    def test_dead_to_alive_emits(self):
        assert should_emit_edge(was_dead=True, dead=False) is True

    def test_no_change_does_not_emit(self):
        assert should_emit_edge(was_dead=True, dead=True) is False
        assert should_emit_edge(was_dead=False, dead=False) is False

    def test_no_repeat_notification_while_dead(self):
        """持续死亡期间，多轮调用只应在第一轮发出通知。"""
        emissions = 0
        was_dead = False
        for _ in range(5):
            dead = True
            if should_emit_edge(was_dead=was_dead, dead=dead):
                emissions += 1
            was_dead = dead
        assert emissions == 1, f"持续死亡应只通知一次，实际 {emissions} 次"


class TestIsMuted:
    """静音延期：到期自动恢复。"""

    def test_no_mute_when_none(self):
        assert is_muted(muted_until=None) is False

    def test_no_mute_when_empty_string(self):
        assert is_muted(muted_until="") is False

    def test_muted_when_future(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        future = (now + timedelta(days=7)).isoformat()
        assert is_muted(muted_until=future, now=now) is True

    def test_not_muted_when_past(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        past = (now - timedelta(days=1)).isoformat()
        assert is_muted(muted_until=past, now=now) is False, "到期后应自动恢复提醒"

    def test_expiry_boundary_exact_now_is_not_muted(self):
        now = datetime(2026, 1, 1, 12, 0, 0)
        assert is_muted(muted_until=now.isoformat(), now=now) is False

    def test_bad_value_does_not_mute(self):
        """脏数据绝不能导致永久静音。"""
        for bad in ("not-a-date", "2026-13-45", "null", "undefined"):
            assert is_muted(muted_until=bad) is False, f"{bad!r} 不应导致静音"


class TestResolveNotifyFlag:
    """通知闸门：分类型开关 + 静音延期。"""

    def test_respects_per_type_switch(self):
        assert resolve_notify_flag(
            event_type="price_drop", watch={"notify_price_drop": False}
        ) is False

    def test_enabled_by_default(self):
        assert resolve_notify_flag(
            event_type="price_drop", watch={"notify_price_drop": True}
        ) is True

    def test_missing_key_defaults_to_allow(self):
        """新增事件类型不应被静默吞掉。"""
        assert resolve_notify_flag(event_type="brand_new_event", watch={}) is True

    def test_mute_overrides_enabled_switch(self):
        future = (datetime.now() + timedelta(days=3)).isoformat()
        assert resolve_notify_flag(
            event_type="price_drop",
            watch={"notify_price_drop": True},
            muted_until=future,
        ) is False, "静音期内即使开关打开也不推送"

    def test_mute_expiry_restores(self):
        past = (datetime.now() - timedelta(days=3)).isoformat()
        assert resolve_notify_flag(
            event_type="price_drop",
            watch={"notify_price_drop": True},
            muted_until=past,
        ) is True

    def test_sold_uses_dedicated_switch(self):
        """售出与下架语义不同，应有独立开关。"""
        assert resolve_notify_flag(
            event_type="sold_out", watch={"notify_on_sold": False, "notify_delisted": True}
        ) is False

    def test_sold_falls_back_to_delisted(self):
        assert resolve_notify_flag(
            event_type="sold_out", watch={"notify_delisted": True}
        ) is True

    def test_silence_still_records(self):
        """静音只是不推送；本函数不负责入库，事件仍应留档。

        这里断言「不推送」，留档由 service 层保证（见 watch_service 注释）。
        """
        future = (datetime.now() + timedelta(days=1)).isoformat()
        assert resolve_notify_flag(
            event_type="price_drop", watch={"notify_price_drop": True}, muted_until=future
        ) is False


class TestReducePriceDelta:
    """闲鱼原生降价信号：能覆盖首次观测前的降价。"""

    def test_positive_delta(self):
        assert detect_reduce_price_delta(current_reduce=500, previous_reduce=200) == 300

    def test_zero_when_unchanged(self):
        assert detect_reduce_price_delta(current_reduce=500, previous_reduce=500) == 0

    def test_zero_when_decreased(self):
        assert detect_reduce_price_delta(current_reduce=100, previous_reduce=500) == 0

    def test_first_observation_catches_prior_drop(self):
        """关键场景：首次监控时商品已降过价，previous=0 仍能捕获。

        这是跨次比价无法做到的 —— 捡漏场景的核心价值。
        """
        delta = detect_reduce_price_delta(current_reduce=1500, previous_reduce=0)
        assert delta == 1500, "首次观测必须能捕获历史降价"

    def test_handles_none_and_bad_values(self):
        assert detect_reduce_price_delta(current_reduce=None, previous_reduce=0) == 0
        assert detect_reduce_price_delta(current_reduce="abc", previous_reduce=0) == 0
        assert detect_reduce_price_delta(current_reduce=100, previous_reduce=None) == 100

    def test_handles_string_numbers(self):
        assert detect_reduce_price_delta(current_reduce="500", previous_reduce="200") == 300
