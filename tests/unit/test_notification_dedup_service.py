"""跨任务通知去重服务的单元测试。

与 AI 治理测试同样的原则：假时钟推进时间窗、不 ``sleep``、不联网、不连库。
重点覆盖「跨任务」这一核心语义——不同任务命中同一商品必须只推一次；
以及空 ``item_id`` 时的回退键行为，避免所有缺 ID 的通知互相误杀。
"""

from __future__ import annotations

import threading

import pytest

from src.services.notification_dedup_service import (
    DEFAULT_MAX_ENTRIES,
    DEFAULT_WINDOW_SECONDS,
    CrossTaskNotificationDeduper,
)


class _FakeClock:
    """可手动推进的假时钟。"""

    def __init__(self, start: float = 5_000.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


# --------------------------------------------------------------------------- #
# 基本语义
# --------------------------------------------------------------------------- #


def test_new_item_should_notify_and_dedupe_after_mark():
    deduper = CrossTaskNotificationDeduper()

    assert deduper.size == 0
    # 尚未标记过：第一次查询必须放行。
    assert deduper.should_notify("ITEM-1") is True
    # 只查询不标记：仍然放行（查询是只读的，不产生副作用）。
    assert deduper.should_notify("ITEM-1") is True
    assert deduper.size == 0, "should_notify 不得写入状态"

    deduper.mark_notified("ITEM-1")

    assert deduper.size == 1
    assert deduper.should_notify("ITEM-1") is False, "窗口内重复命中应被去重"


def test_query_alone_never_blocks_and_mark_is_what_blocks():
    deduper = CrossTaskNotificationDeduper()

    for _ in range(5):
        assert deduper.should_notify("ITEM-9") is True

    deduper.mark_notified("ITEM-9")

    for _ in range(5):
        assert deduper.should_notify("ITEM-9") is False


def test_cross_task_dedupes_same_item_across_different_tasks():
    deduper = CrossTaskNotificationDeduper()

    assert deduper.should_notify("ITEM-1", task_name="任务A", event_type="新推荐") is True
    deduper.mark_notified("ITEM-1", task_name="任务A", event_type="新推荐")

    # 另一个任务命中同一商品：商品 ID 优先，任务名不参与键，必须被去重。
    assert deduper.should_notify("ITEM-1", task_name="任务B", event_type="新推荐") is False
    assert deduper.should_notify("ITEM-1", task_name="任务C", event_type="降价") is False
    assert deduper.size == 1, "同一商品的多个任务命中只占一个条目"


def test_different_items_do_not_interfere():
    deduper = CrossTaskNotificationDeduper()

    deduper.mark_notified("ITEM-1")

    assert deduper.should_notify("ITEM-1") is False
    assert deduper.should_notify("ITEM-2") is True
    assert deduper.should_notify("ITEM-3") is True
    assert deduper.size == 1


# --------------------------------------------------------------------------- #
# 时间窗
# --------------------------------------------------------------------------- #


def test_window_expiry_allows_notification_again():
    clock = _FakeClock()
    deduper = CrossTaskNotificationDeduper(window_seconds=3600.0, clock=clock)

    deduper.mark_notified("ITEM-1")
    assert deduper.should_notify("ITEM-1") is False

    clock.advance(3599.0)
    assert deduper.should_notify("ITEM-1") is False, "窗口内仍去重"

    clock.advance(1.0)
    assert deduper.should_notify("ITEM-1") is True, "窗口结束即可再次通知"

    deduper.mark_notified("ITEM-1")
    assert deduper.should_notify("ITEM-1") is False


def test_window_expiry_is_strictly_less_than_window():
    clock = _FakeClock()
    deduper = CrossTaskNotificationDeduper(window_seconds=60.0, clock=clock)

    deduper.mark_notified("ITEM-1")
    clock.advance(60.0)

    assert deduper.should_notify("ITEM-1") is True, "恰好等于窗口长度视为已出窗口"


def test_expired_entries_are_purged_on_next_write():
    clock = _FakeClock()
    deduper = CrossTaskNotificationDeduper(window_seconds=100.0, clock=clock)

    deduper.mark_notified("ITEM-1")
    deduper.mark_notified("ITEM-2")
    assert deduper.size == 2

    clock.advance(101.0)
    deduper.mark_notified("ITEM-3")

    assert deduper.size == 1, "过期条目应被惰性清理，不长期占内存"
    assert deduper.should_notify("ITEM-3") is False


def test_mark_notified_resets_window_start():
    clock = _FakeClock()
    deduper = CrossTaskNotificationDeduper(window_seconds=100.0, clock=clock)

    deduper.mark_notified("ITEM-1")
    clock.advance(80.0)
    deduper.mark_notified("ITEM-1")
    clock.advance(80.0)

    assert deduper.should_notify("ITEM-1") is False, "重复标记应刷新窗口起点"
    assert deduper.size == 1


def test_zero_window_disables_dedup():
    deduper = CrossTaskNotificationDeduper(window_seconds=0.0)

    assert deduper.window_seconds == 0.0, "0 是合法窗口（关闭去重），不该被回落覆盖"
    deduper.mark_notified("ITEM-1")

    assert deduper.should_notify("ITEM-1") is True
    assert deduper.should_notify("ITEM-1") is True


def test_clear_resets_all_state():
    deduper = CrossTaskNotificationDeduper()

    deduper.mark_notified("ITEM-1")
    deduper.mark_notified("ITEM-2")
    assert deduper.size == 2

    deduper.clear()

    assert deduper.size == 0
    assert deduper.should_notify("ITEM-1") is True


# --------------------------------------------------------------------------- #
# 空 item_id 的回退键
# --------------------------------------------------------------------------- #


def test_empty_item_id_falls_back_to_task_and_event_key():
    deduper = CrossTaskNotificationDeduper()

    deduper.mark_notified("", task_name="任务A", event_type="新推荐")

    # 同一任务同一事件类型：去重。
    assert deduper.should_notify("", task_name="任务A", event_type="新推荐") is False
    # 换任务或换事件类型：不同回退键，互不干扰。
    assert deduper.should_notify("", task_name="任务B", event_type="新推荐") is True
    assert deduper.should_notify("", task_name="任务A", event_type="降价") is True


def test_empty_item_ids_do_not_mutually_kill_across_tasks():
    deduper = CrossTaskNotificationDeduper()

    # 两个都缺 ID 的商品，分属不同任务：不能互相误杀。
    assert deduper.should_notify("", task_name="任务A", event_type="新推荐") is True
    deduper.mark_notified("", task_name="任务A", event_type="新推荐")

    assert deduper.should_notify("", task_name="任务B", event_type="新推荐") is True
    assert deduper.size == 1


@pytest.mark.parametrize("empty_id", ["", "   ", "\t", None])
def test_blank_and_none_item_ids_use_fallback_key(empty_id):
    deduper = CrossTaskNotificationDeduper()

    deduper.mark_notified(empty_id, task_name="任务A", event_type="新推荐")

    assert deduper.should_notify(empty_id, task_name="任务A", event_type="新推荐") is False
    assert deduper.should_notify(empty_id, task_name="任务A", event_type="降价") is True


def test_item_key_does_not_collide_with_fallback_key():
    deduper = CrossTaskNotificationDeduper()

    # 商品 ID 恰好等于任务名的极端情况：两者必须是不同的键。
    deduper.mark_notified("任务A", task_name="ignored", event_type="ignored")

    assert deduper.should_notify("任务A") is False
    assert deduper.should_notify("", task_name="任务A", event_type="") is True, (
        "item 键与 fallback 键需分属不同命名空间，不得互相污染"
    )


def test_fallback_key_separator_prevents_collisions():
    deduper = CrossTaskNotificationDeduper()

    deduper.mark_notified("", task_name="ab", event_type="c")

    assert deduper.should_notify("", task_name="ab", event_type="c") is False
    assert deduper.should_notify("", task_name="a", event_type="bc") is True, (
        "不同字段切分不该拼成同一个键"
    )


def test_item_id_is_stripped_before_building_key():
    deduper = CrossTaskNotificationDeduper()

    deduper.mark_notified("  ITEM-1  ")

    assert deduper.should_notify("ITEM-1") is False
    assert deduper.size == 1


# --------------------------------------------------------------------------- #
# 上限与淘汰
# --------------------------------------------------------------------------- #


def test_max_entries_evicts_oldest_entry():
    deduper = CrossTaskNotificationDeduper(max_entries=3)

    deduper.mark_notified("ITEM-1")
    deduper.mark_notified("ITEM-2")
    deduper.mark_notified("ITEM-3")
    assert deduper.size == 3

    deduper.mark_notified("ITEM-4")

    assert deduper.size == 3, "条目数不得超过上限"
    assert deduper.should_notify("ITEM-1") is True, "最旧的条目应被淘汰"
    assert deduper.should_notify("ITEM-2") is False
    assert deduper.should_notify("ITEM-3") is False
    assert deduper.should_notify("ITEM-4") is False


def test_recently_used_entry_survives_eviction():
    deduper = CrossTaskNotificationDeduper(max_entries=3)

    deduper.mark_notified("ITEM-1")
    deduper.mark_notified("ITEM-2")
    deduper.mark_notified("ITEM-3")
    # 查询命中会刷新 ITEM-1 的使用位置，此时最旧的是 ITEM-2。
    assert deduper.should_notify("ITEM-1") is False

    deduper.mark_notified("ITEM-4")

    assert deduper.should_notify("ITEM-1") is False, "被访问过的热商品不该先被淘汰"
    assert deduper.should_notify("ITEM-2") is True


def test_mark_notified_updates_recent_usage_position():
    deduper = CrossTaskNotificationDeduper(max_entries=2)

    deduper.mark_notified("ITEM-1")
    deduper.mark_notified("ITEM-2")
    deduper.mark_notified("ITEM-1")  # 重新标记，ITEM-1 变为最近使用
    deduper.mark_notified("ITEM-3")

    assert deduper.should_notify("ITEM-2") is True, "最久未使用的 ITEM-2 被淘汰"
    assert deduper.should_notify("ITEM-1") is False
    assert deduper.should_notify("ITEM-3") is False


def test_size_stays_bounded_under_heavy_churn():
    deduper = CrossTaskNotificationDeduper(max_entries=10)

    for index in range(500):
        deduper.mark_notified(f"ITEM-{index}")

    assert deduper.size == 10
    # 最后写入的 10 个（ITEM-490 ~ ITEM-499）被保留，更早的已被淘汰。
    assert deduper.should_notify("ITEM-499") is False
    assert deduper.should_notify("ITEM-490") is False
    assert deduper.should_notify("ITEM-489") is True


def test_max_entries_one_keeps_only_newest():
    deduper = CrossTaskNotificationDeduper(max_entries=1)

    deduper.mark_notified("ITEM-1")
    deduper.mark_notified("ITEM-2")

    assert deduper.size == 1
    assert deduper.should_notify("ITEM-1") is True
    assert deduper.should_notify("ITEM-2") is False


# --------------------------------------------------------------------------- #
# 非法参数回落
# --------------------------------------------------------------------------- #


def test_defaults():
    deduper = CrossTaskNotificationDeduper()

    assert deduper.window_seconds == DEFAULT_WINDOW_SECONDS == 3600.0
    assert deduper.max_entries == DEFAULT_MAX_ENTRIES == 2000


@pytest.mark.parametrize(
    "bad_window",
    [None, -1, -3600.0, "3600", float("nan"), float("inf"), float("-inf"), True, []],
)
def test_invalid_window_falls_back_to_default(bad_window):
    deduper = CrossTaskNotificationDeduper(window_seconds=bad_window)

    assert deduper.window_seconds == 3600.0


@pytest.mark.parametrize("bad_max", [0, -1, None, "2000", float("nan"), True, 0.5])
def test_invalid_max_entries_falls_back_to_default(bad_max):
    deduper = CrossTaskNotificationDeduper(max_entries=bad_max)

    assert deduper.max_entries == 2000


def test_deduper_accepts_injected_clock_callable():
    clock = _FakeClock(start=0.0)
    deduper = CrossTaskNotificationDeduper(window_seconds=10.0, clock=clock)

    deduper.mark_notified("ITEM-1")
    clock.advance(9.5)
    assert deduper.should_notify("ITEM-1") is False
    clock.advance(0.5)
    assert deduper.should_notify("ITEM-1") is True


# --------------------------------------------------------------------------- #
# 线程安全
# --------------------------------------------------------------------------- #


def test_concurrent_access_keeps_state_consistent():
    deduper = CrossTaskNotificationDeduper(window_seconds=1000.0, max_entries=64)
    errors: list[BaseException] = []
    notified: list[str] = []
    lock = threading.Lock()

    def worker(task_name: str) -> None:
        try:
            for index in range(300):
                item_id = f"ITEM-{index % 40}"
                if deduper.should_notify(item_id, task_name=task_name):
                    deduper.mark_notified(item_id, task_name=task_name)
                    with lock:
                        notified.append(item_id)
        except BaseException as exc:  # pragma: no cover - 仅在实现有竞态时触发
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"任务{i}",)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert deduper.size <= 64
    # 去重生效：40 个商品在窗口内最多被放行 40 次（首轮各一次），允许并发窗口下的少量
    # 竞态重复（检查与标记不是原子操作），但绝不该接近 6*300 次。
    assert len(notified) <= 40 * 6
    assert set(notified) == {f"ITEM-{i}" for i in range(40)}
