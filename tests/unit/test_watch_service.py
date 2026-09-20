"""关注链路的数据库层测试（事件判定、去重、下架、趋势）。

``watch_state`` 的纯函数判定已由 test_watch_state.py 覆盖；本文件专注
"落库行为"：事件是否产生、event_key 是否真的去重、下架阈值是否按轮次推进、
通知闸门是否影响入库。这些错误表现为用户收到假通知或漏通知，线上极难排查。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services import watch_service as ws


@pytest.fixture(autouse=True)
def _schema():
    bootstrap_sqlite_storage()


@pytest.fixture(autouse=True)
def _no_real_notifications(monkeypatch):
    """绝不发起真实网络请求：通知服务替换为纯记录替身。"""

    class _StubNotificationService:
        def __init__(self):
            self.calls = []

        async def send_notification(self, product_data, reason):
            self.calls.append((product_data, reason))
            return {
                "stub": {
                    "channel": "stub",
                    "label": "stub",
                    "success": True,
                    "message": "ok",
                }
            }

    stub = _StubNotificationService()
    monkeypatch.setattr(ws, "build_notification_service", lambda: stub)
    return stub


async def _add(item_id="A1", **kwargs):
    payload = {
        "item_id": item_id,
        "title": f"商品 {item_id}",
        "link": f"https://example.invalid/item/{item_id}",
        "task_name": "T1",
        **kwargs,
    }
    return await ws.add_watch_item(payload)


def _event_types():
    return [event["event_type"] for event in ws.list_watch_events(limit=100)]


# --- 新增关注 ---


def test_add_watch_item_rejects_missing_required_fields():
    for bad in (
        {"item_id": "", "title": "t", "link": "l"},
        {"item_id": "1", "title": "", "link": "l"},
        {"item_id": "1", "title": "t", "link": ""},
    ):
        with pytest.raises(ValueError):
            asyncio.run(ws.add_watch_item(bad))


def test_add_watch_item_persists_core_fields():
    watch = asyncio.run(_add(alert_price="900", last_price="1000"))
    assert watch["item_id"] == "A1"
    assert watch["alert_price"] == 900.0
    assert watch["last_price"] == 1000.0
    assert watch["status"] == "active"
    assert watch["enabled"] is True
    assert watch["missing_runs"] == 0


def test_add_watch_item_upserts_same_item_id():
    first = asyncio.run(_add(title="旧标题"))
    second = asyncio.run(_add(title="新标题"))
    assert first["id"] == second["id"]
    assert second["title"] == "新标题"
    assert len(ws.list_watch_items()) == 1


def test_add_item_already_below_alert_price_emits_low_price_once():
    """关注时价格已低于提醒价，应立即产生一条低價事件。"""
    asyncio.run(_add(alert_price="1000", last_price="800"))
    assert _event_types().count("low_price") == 1


def test_add_item_above_alert_price_emits_nothing():
    asyncio.run(_add(alert_price="500", last_price="800"))
    assert _event_types() == []


def test_add_item_without_alert_price_emits_nothing():
    asyncio.run(_add(last_price="800"))
    assert _event_types() == []


# --- 快照处理：价格事件 ---


def test_price_drop_emits_event_with_correct_amounts():
    watch = asyncio.run(_add(last_price="1000"))
    ids = asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert ids
    event = ws.list_watch_events()[0]
    assert event["event_type"] == "price_drop"
    assert event["price"] == 800.0
    assert event["previous_price"] == 1000.0
    assert ws.get_watch_item(watch["id"])["last_price"] == 800.0


def test_price_increase_does_not_emit_drop_event():
    asyncio.run(_add(last_price="1000"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "1500", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert "price_drop" not in _event_types()


def test_equal_price_does_not_emit_drop_event():
    """同价不得产生降价事件（否则每轮采集都会骚扰用户）。"""
    asyncio.run(_add(last_price="1000"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "1000", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert "price_drop" not in _event_types()


def test_low_price_requires_crossing_threshold():
    """已低于提醒价后再降价，不应重复产生 low_price（只在穿越时触发）。"""
    asyncio.run(_add(last_price="1000", alert_price="900"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "700", "run_id": "r2", "snapshot_time": "2026-01-01T11:00:00"}]
        )
    )
    assert _event_types().count("low_price") == 1


def test_low_price_triggers_on_first_observation_below_threshold():
    """首轮就低于提醒价（previous 为 None）也应触发。"""
    asyncio.run(_add(last_price="1000", alert_price="900"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "850", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert "low_price" in _event_types()


# --- 快照处理：脏数据与边界 ---


@pytest.mark.parametrize(
    "record",
    [
        {"price": "800"},  # 缺 item_id
        {"item_id": "", "price": "800"},
        {"item_id": "A1"},  # 缺 price
        {"item_id": "A1", "price": ""},
        {"item_id": "A1", "price": "面议"},
        {"item_id": "A1", "price": None},
    ],
)
def test_process_snapshots_skips_invalid_records(record):
    asyncio.run(_add(last_price="1000"))
    ids = asyncio.run(ws.process_watch_snapshots([record]))
    assert ids == []
    assert _event_types() == []


def test_process_snapshots_ignores_untracked_item():
    asyncio.run(_add(last_price="1000"))
    ids = asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "NOT-TRACKED", "price": "1", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert ids == []


def test_process_snapshots_ignores_disabled_watch_item():
    watch = asyncio.run(_add(last_price="1000"))
    ws.update_watch_item(watch["id"], {"enabled": False})
    ids = asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "500", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert ids == []


def test_process_snapshots_empty_batch_returns_empty():
    assert asyncio.run(ws.process_watch_snapshots([])) == []


def test_process_snapshots_handles_invalid_snapshot_time():
    """非法时间戳不得抛异常（源码回退到 datetime.now()）。"""
    asyncio.run(_add(last_price="1000"))
    ids = asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "900", "run_id": "r1", "snapshot_time": "not-a-timestamp"}]
        )
    )
    assert ids


def test_process_snapshots_resets_missing_runs_and_status():
    watch = asyncio.run(_add(last_price="1000"))
    for index in range(1, 4):
        asyncio.run(
            ws.finalize_watch_scan(task_name="T1", seen_item_ids={"OTHER"}, run_id=f"f{index}")
        )
    assert ws.get_watch_item(watch["id"])["status"] == "delisted"

    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "900", "run_id": "r9", "snapshot_time": "2026-02-01T10:00:00"}]
        )
    )
    refreshed = ws.get_watch_item(watch["id"])
    assert refreshed["missing_runs"] == 0
    assert refreshed["status"] == "active"


# --- 事件去重（event_key UNIQUE）---


def test_same_run_id_does_not_duplicate_events():
    """同一 run_id 重复处理（重试/重放）只应产生一条事件。"""
    asyncio.run(_add(last_price="1000"))
    record = {"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}

    first = asyncio.run(ws.process_watch_snapshots([record]))
    second = asyncio.run(ws.process_watch_snapshots([record]))

    assert first and second == []
    assert _event_types().count("price_drop") == 1


def test_different_run_ids_do_produce_separate_events():
    """护栏：去重不能过度，真实的新一轮降价必须仍能记录。"""
    asyncio.run(_add(last_price="1000"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "900", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r2", "snapshot_time": "2026-01-01T11:00:00"}]
        )
    )
    assert _event_types().count("price_drop") == 2


def test_event_key_unique_constraint_is_enforced_at_db_level():
    """直接插入重复 event_key 必须被 UNIQUE 约束挡下，而不是产生重复行。"""
    watch = asyncio.run(_add(last_price="1000"))
    kwargs = dict(
        watch_id=watch["id"],
        event_key="dup-key",
        event_type="price_drop",
        price=1.0,
        previous_price=2.0,
        detail="d",
        created_at="2026-01-01T00:00:00",
    )
    assert ws._insert_event(**kwargs) is not None
    assert ws._insert_event(**kwargs) is None
    assert len([e for e in ws.list_watch_events(limit=100) if e["event_type"] == "price_drop"]) == 1


# --- 下架判定（DELISTED_MISSING_RUNS = 3）---


def test_delisted_missing_runs_constant_is_three():
    """阈值被产品语义依赖，改动需要同步更新文档与测试。"""
    assert ws.DELISTED_MISSING_RUNS == 3


def test_missing_below_threshold_does_not_delist():
    watch = asyncio.run(_add(last_price="1000"))
    for index in range(1, ws.DELISTED_MISSING_RUNS):
        events = asyncio.run(
            ws.finalize_watch_scan(task_name="T1", seen_item_ids={"OTHER"}, run_id=f"f{index}")
        )
        assert events == []
        assert ws.get_watch_item(watch["id"])["status"] == "active"
    assert ws.get_watch_item(watch["id"])["missing_runs"] == ws.DELISTED_MISSING_RUNS - 1


def test_delist_happens_exactly_at_threshold():
    watch = asyncio.run(_add(last_price="1000"))
    events = []
    for index in range(1, ws.DELISTED_MISSING_RUNS + 1):
        events = asyncio.run(
            ws.finalize_watch_scan(task_name="T1", seen_item_ids={"OTHER"}, run_id=f"f{index}")
        )
    assert events
    assert "delisted" in _event_types()
    assert ws.get_watch_item(watch["id"])["status"] == "delisted"


def test_delist_event_is_not_repeated_on_later_rounds():
    """持续缺失期间只发一次下架事件（边沿触发）。"""
    asyncio.run(_add(last_price="1000"))
    for index in range(1, ws.DELISTED_MISSING_RUNS + 1):
        asyncio.run(ws.finalize_watch_scan(task_name="T1", seen_item_ids={"OTHER"}, run_id=f"f{index}"))
    for index in range(10, 14):
        events = asyncio.run(
            ws.finalize_watch_scan(task_name="T1", seen_item_ids={"OTHER"}, run_id=f"f{index}")
        )
        assert events == []
    assert _event_types().count("delisted") == 1


def test_finalize_skips_seen_items_without_resetting_counter():
    """契约说明：``finalize_watch_scan`` 对「本轮已见」的商品直接跳过。

    缺失计数的**归零**由 ``process_watch_snapshots`` 负责（采到即置 0）；
    finalize 只负责为「未见到」的商品自增。这是有意的职责划分：采集失败的
    轮次不会走到 finalize，因此把归零放在 finalize 里会掩盖真实缺失。

    依赖后果：调用方必须先跑 ``process_watch_snapshots``，再跑 finalize。
    """
    watch = asyncio.run(_add(last_price="1000"))
    asyncio.run(ws.finalize_watch_scan(task_name="T1", seen_item_ids={"OTHER"}, run_id="f1"))
    assert ws.get_watch_item(watch["id"])["missing_runs"] == 1

    # 该商品本轮被见到：finalize 不碰计数（归零交给 process_watch_snapshots）
    assert asyncio.run(ws.finalize_watch_scan(task_name="T1", seen_item_ids={"A1"}, run_id="f2")) == []
    assert ws.get_watch_item(watch["id"])["missing_runs"] == 1

    # 正确的调用顺序：快照处理才是归零点
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "900", "run_id": "r2", "snapshot_time": "2026-01-02T10:00:00"}]
        )
    )
    assert ws.get_watch_item(watch["id"])["missing_runs"] == 0


def test_seen_then_missing_does_not_delist_early():
    """完整流程护栏：商品反复出现/消失（未达阈值）不得判定下架。"""
    watch = asyncio.run(_add(last_price="1000"))
    for index in range(1, 6):
        # 本轮采到 -> 归零
        asyncio.run(
            ws.process_watch_snapshots(
                [
                    {
                        "item_id": "A1",
                        "price": "1000",
                        "run_id": f"r{index}",
                        "snapshot_time": f"2026-01-0{index}T10:00:00",
                    }
                ]
            )
        )
        # 另一个商品缺失，本商品已见
        asyncio.run(ws.finalize_watch_scan(task_name="T1", seen_item_ids={"A1"}, run_id=f"f{index}"))

    assert ws.get_watch_item(watch["id"])["status"] == "active"
    assert "delisted" not in _event_types()


def test_finalize_returns_empty_for_empty_seen_set():
    """空 seen 集合意味着扫描失败，绝不能据此判定下架。"""
    watch = asyncio.run(_add(last_price="1000"))
    assert asyncio.run(ws.finalize_watch_scan(task_name="T1", seen_item_ids=set(), run_id="f1")) == []
    assert ws.get_watch_item(watch["id"])["missing_runs"] == 0


def test_finalize_only_touches_matching_task_name():
    watch_a = asyncio.run(_add(item_id="A1", task_name="TA"))
    watch_b = asyncio.run(_add(item_id="B1", task_name="TB"))
    asyncio.run(ws.finalize_watch_scan(task_name="TA", seen_item_ids={"OTHER"}, run_id="f1"))
    assert ws.get_watch_item(watch_a["id"])["missing_runs"] == 1
    assert ws.get_watch_item(watch_b["id"])["missing_runs"] == 0


def test_finalize_ignores_disabled_items():
    watch = asyncio.run(_add(last_price="1000"))
    ws.update_watch_item(watch["id"], {"enabled": False})
    asyncio.run(ws.finalize_watch_scan(task_name="T1", seen_item_ids={"OTHER"}, run_id="f1"))
    assert ws.get_watch_item(watch["id"])["missing_runs"] == 0


def test_relist_after_delist_emits_event():
    watch = asyncio.run(_add(last_price="1000"))
    for index in range(1, ws.DELISTED_MISSING_RUNS + 1):
        asyncio.run(ws.finalize_watch_scan(task_name="T1", seen_item_ids={"OTHER"}, run_id=f"f{index}"))
    assert ws.get_watch_item(watch["id"])["status"] == "delisted"

    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "900", "run_id": "r9", "snapshot_time": "2026-02-01T10:00:00"}]
        )
    )
    assert "relisted" in _event_types()


# --- 通知闸门与落库 ---


def test_disabled_notify_flag_still_records_event(_no_real_notifications):
    """关闭某类通知后，事件仍必须入库留档（只是不推送）。"""
    watch = asyncio.run(_add(last_price="1000"))
    ws.update_watch_item(watch["id"], {"notify_price_drop": False})

    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )

    events = ws.list_watch_events()
    assert any(event["event_type"] == "price_drop" for event in events)
    drop_event = next(event for event in events if event["event_type"] == "price_drop")
    assert drop_event["notified"] is False
    assert _no_real_notifications.calls == []


def test_enabled_notify_flag_dispatches(_no_real_notifications):
    asyncio.run(_add(last_price="1000"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert len(_no_real_notifications.calls) == 1
    event = ws.list_watch_events()[0]
    assert event["notified"] is True


def test_notification_failure_is_recorded_without_breaking_flow(monkeypatch):
    """通知异常必须被吞掉并记录，不能让采集流程整体失败。"""
    asyncio.run(_add(last_price="1000"))

    class _Boom:
        async def send_notification(self, product_data, reason):
            raise RuntimeError("notification backend down")

    monkeypatch.setattr(ws, "build_notification_service", lambda: _Boom())

    ids = asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert ids
    event = ws.list_watch_events()[0]
    assert event["notified"] is False
    assert event["notification_results"]["internal"]["success"] is False


def test_low_price_with_consult_enabled_calls_consultation(monkeypatch, _no_real_notifications):
    """低价事件 + 开启自动咨询时应调用咨询；这里用替身，绝不真实发送。"""
    calls = []

    async def _fake_send_consultation(watch):
        calls.append(watch["id"])
        return {"status": "skipped", "reason": "冷却中"}

    monkeypatch.setattr(ws, "send_consultation", _fake_send_consultation)

    asyncio.run(_add(last_price="1000", alert_price="900", consult_enabled=True))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert calls


def test_price_drop_does_not_call_consultation(monkeypatch, _no_real_notifications):
    calls = []

    async def _fake_send_consultation(watch):
        calls.append(watch["id"])
        return {"status": "sent"}

    monkeypatch.setattr(ws, "send_consultation", _fake_send_consultation)

    asyncio.run(_add(last_price="1000", alert_price="500", consult_enabled=True))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert calls == []


# --- 更新与删除 ---


def test_update_watch_item_rejects_bad_alert_price():
    watch = asyncio.run(_add())
    with pytest.raises(ValueError):
        ws.update_watch_item(watch["id"], {"alert_price": "面议"})


def test_update_watch_item_clears_alert_price_with_empty_value():
    watch = asyncio.run(_add(alert_price="900"))
    assert ws.update_watch_item(watch["id"], {"alert_price": ""})["alert_price"] is None


def test_update_watch_item_rejects_non_positive_interval():
    watch = asyncio.run(_add())
    with pytest.raises(ValueError):
        ws.update_watch_item(watch["id"], {"refresh_interval_minutes": 0})


def test_update_watch_item_without_changes_returns_current_state():
    watch = asyncio.run(_add(alert_price="900"))
    assert ws.update_watch_item(watch["id"], {})["alert_price"] == 900.0


def test_update_unknown_watch_item_returns_none():
    assert ws.update_watch_item(999999, {"enabled": False}) is None


def test_delete_watch_item_cascades_events():
    watch = asyncio.run(_add(last_price="1000"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert ws.list_watch_events()
    assert ws.delete_watch_item(watch["id"]) is True
    assert ws.list_watch_events() == []


def test_delete_unknown_watch_item_returns_false():
    assert ws.delete_watch_item(999999) is False


# --- 事件读取 ---


def test_mark_event_read_and_unread_filter():
    asyncio.run(_add(last_price="1000"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    event_id = ws.list_watch_events(unread_only=True)[0]["id"]
    assert ws.mark_event_read(event_id) is True
    assert ws.list_watch_events(unread_only=True) == []


def test_mark_all_events_read_returns_count():
    asyncio.run(_add(last_price="1000"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    assert ws.mark_all_events_read() >= 1
    assert ws.mark_all_events_read() == 0


def test_mark_unknown_event_returns_false():
    assert ws.mark_event_read(999999) is False


def test_event_limit_is_clamped():
    asyncio.run(_add(last_price="1000"))
    assert ws.list_watch_events(limit=0) is not None
    assert ws.list_watch_events(limit=99999) is not None


# --- 统计与趋势 ---


def test_watch_stats_counts_delisted_and_unread():
    watch = asyncio.run(_add(last_price="1000"))
    stats = ws.get_watch_stats()
    assert stats["total"] == 1
    assert stats["enabled"] == 1
    assert stats["delisted"] == 0

    for index in range(1, ws.DELISTED_MISSING_RUNS + 1):
        asyncio.run(ws.finalize_watch_scan(task_name="T1", seen_item_ids={"OTHER"}, run_id=f"f{index}"))

    stats = ws.get_watch_stats()
    assert stats["delisted"] == 1
    assert stats["unread_events"] >= 1
    assert ws.get_watch_item(watch["id"])["status"] == "delisted"


def test_watch_stats_on_empty_database():
    stats = ws.get_watch_stats()
    assert stats == {
        "total": 0,
        "enabled": 0,
        "delisted": 0,
        "unread_events": 0,
        "low_price_today": 0,
    }


def test_item_trend_returns_none_for_unknown_watch():
    assert ws.get_item_trend(999999) is None


def test_item_trend_without_snapshots_falls_back_to_last_price():
    watch = asyncio.run(_add(last_price="1000"))
    trend = ws.get_item_trend(watch["id"])
    assert trend["scope"] == "item"
    assert trend["summary"]["current_price"] == 1000.0
    assert trend["summary"]["observation_count"] == 0
    assert trend["summary"]["is_sparse"] is True


def test_item_trend_aggregates_recorded_snapshots():
    from src.services.price_history_service import record_market_snapshots

    watch = asyncio.run(_add(last_price="1000"))
    record_market_snapshots(
        keyword="kw",
        task_name="T1",
        items=[
            {"商品ID": "A1", "商品标题": "商品 A1", "当前售价": "¥1000",
             "商品链接": "https://example.invalid/item/A1"},
        ],
        run_id="r1",
        snapshot_time="2026-01-01T10:00:00",
    )
    record_market_snapshots(
        keyword="kw",
        task_name="T1",
        items=[
            {"商品ID": "A1", "商品标题": "商品 A1", "当前售价": "¥900",
             "商品链接": "https://example.invalid/item/A1"},
        ],
        run_id="r2",
        snapshot_time="2026-01-02T10:00:00",
    )

    trend = ws.get_item_trend(watch["id"])
    assert trend["summary"]["observation_count"] == 2
    assert trend["summary"]["min_price"] == 900.0
    assert trend["summary"]["max_price"] == 1000.0
    assert trend["summary"]["avg_price"] == 950.0
    assert trend["summary"]["is_sparse"] is False
    assert len(trend["points"]) == 2


def test_category_trend_delegates_to_price_history():
    result = ws.get_category_trend("不存在的关键词")
    assert result["scope"] == "category"
    assert result["label"] == "不存在的关键词"


# --- AI 摘要的前置校验（不触网）---


def test_generate_watch_ai_summary_raises_for_unknown_watch():
    with pytest.raises(ValueError):
        asyncio.run(ws.generate_watch_ai_summary(999999))


def test_generate_watch_ai_summary_raises_when_ai_unconfigured(monkeypatch):
    """AI 未配置时必须抛错提示，而不是返回空解读。"""
    watch = asyncio.run(_add(last_price="1000"))

    class _UnavailableClient:
        def is_available(self):
            return False

        async def generate_json(self, prompt):
            raise AssertionError("AI 未配置时不应发起请求")

        async def close(self):
            pass

    monkeypatch.setattr(ws, "AIClient", _UnavailableClient)

    with pytest.raises(RuntimeError):
        asyncio.run(ws.generate_watch_ai_summary(watch["id"]))


# --- 展示层：事件标签 ---


def test_event_labels_cover_known_event_types():
    for event_type in ("price_drop", "low_price", "delisted", "relisted"):
        assert event_type in ws.EVENT_LABELS


def test_row_to_event_survives_corrupted_notification_json():
    """notification_results_json 损坏时不得抛异常。"""
    watch = asyncio.run(_add(last_price="1000"))
    asyncio.run(
        ws.process_watch_snapshots(
            [{"item_id": "A1", "price": "800", "run_id": "r1", "snapshot_time": "2026-01-01T10:00:00"}]
        )
    )
    with sqlite_connection() as conn:
        conn.execute("UPDATE watch_events SET notification_results_json = ?", ("{broken",))
        conn.commit()

    events = ws.list_watch_events()
    assert events[0]["notification_results"] == {}


def test_unknown_event_type_falls_back_to_raw_label():
    watch = asyncio.run(_add(last_price="1000"))
    ws._insert_event(
        watch_id=watch["id"],
        event_key="weird:1",
        event_type="something_new",
        price=1.0,
        previous_price=None,
        detail="d",
        created_at="2026-01-01T00:00:00",
    )
    event = ws.list_watch_events()[0]
    assert event["event_label"] == "something_new"
