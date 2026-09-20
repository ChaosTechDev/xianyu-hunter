"""自动咨询的冷却与去重测试。

本模块顶层依赖 playwright（`consultation_service.py:7`），因此缺少该依赖时
整体跳过。发送链路绝不真实触网：只测不必打开浏览器的分支（冷却、去重、
模板渲染、无可用账号）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

pytest.importorskip("playwright", reason="缺少 playwright 依赖，跳过自动咨询相关用例")

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage  # noqa: E402
from src.services import consultation_service as cs  # noqa: E402
from src.services import watch_service as ws  # noqa: E402


@pytest.fixture(autouse=True)
def _schema():
    bootstrap_sqlite_storage()


@pytest.fixture()
def watch_id():
    """consultation_logs.watch_item_id 有外键约束，必须先建关注项。"""
    watch = asyncio.run(
        ws.add_watch_item(
            {
                "item_id": "C1",
                "title": "咨询测试商品",
                "link": "https://example.invalid/item/C1",
                "task_name": "TC",
            }
        )
    )
    return watch["id"]


@pytest.fixture(autouse=True)
def _silence_notifications(monkeypatch):
    """建关注项若产生事件会触发通知，替换为替身避免真实网络请求。"""

    class _Stub:
        async def send_notification(self, product_data, reason):
            return {"stub": {"channel": "stub", "success": True}}

    monkeypatch.setattr(ws, "build_notification_service", lambda: _Stub())


@pytest.fixture(autouse=True)
def _clean_link_cache():
    """_consulted_links 是模块级全局，必须逐用例清空，否则用例互相污染。"""
    cs._consulted_links.clear()
    yield
    cs._consulted_links.clear()


# --- 冷却时长解析 ---


def test_cooldown_defaults_to_24_hours(monkeypatch):
    monkeypatch.delenv("AUTO_CONSULT_COOLDOWN_HOURS", raising=False)
    assert cs._cooldown_hours() == 24


def test_cooldown_reads_env_override(monkeypatch):
    monkeypatch.setenv("AUTO_CONSULT_COOLDOWN_HOURS", "48")
    assert cs._cooldown_hours() == 48


@pytest.mark.parametrize("raw", ["abc", "", "  ", "1.5", "none"])
def test_cooldown_falls_back_on_invalid_value(monkeypatch, raw):
    """非法值必须回落 24，而不是抛 ValueError 中断整轮采集。"""
    monkeypatch.setenv("AUTO_CONSULT_COOLDOWN_HOURS", raw)
    assert cs._cooldown_hours() == 24


@pytest.mark.parametrize("raw", ["0", "-5", "-100"])
def test_cooldown_never_below_one_hour(monkeypatch, raw):
    """下限被夹到 1 小时：0/负数会导致狂发咨询，属于风控高危。"""
    monkeypatch.setenv("AUTO_CONSULT_COOLDOWN_HOURS", raw)
    assert cs._cooldown_hours() == 1


# --- 冷却判定 ---


def test_recently_consulted_false_without_timestamp():
    assert cs._recently_consulted({}) is False
    assert cs._recently_consulted({"last_consulted_at": None}) is False
    assert cs._recently_consulted({"last_consulted_at": ""}) is False


def test_recently_consulted_true_within_window(monkeypatch):
    monkeypatch.setenv("AUTO_CONSULT_COOLDOWN_HOURS", "24")
    recent = (datetime.now() - timedelta(hours=1)).isoformat()
    assert cs._recently_consulted({"last_consulted_at": recent}) is True


def test_recently_consulted_false_after_window(monkeypatch):
    monkeypatch.setenv("AUTO_CONSULT_COOLDOWN_HOURS", "24")
    old = (datetime.now() - timedelta(hours=25)).isoformat()
    assert cs._recently_consulted({"last_consulted_at": old}) is False


def test_recently_consulted_false_on_corrupted_timestamp():
    """脏时间戳必须按「未咨询」处理：宁可多发一次，也不能永久静默。"""
    for bad in ("not-a-date", "2026-13-45", "null"):
        assert cs._recently_consulted({"last_consulted_at": bad}) is False


def test_recently_consulted_respects_cooldown_env(monkeypatch):
    """同一时间戳在 24h 冷却下算「刚咨询过」，在 1h 冷却下已过期。"""
    stamp = (datetime.now() - timedelta(hours=5)).isoformat()
    watch = {"last_consulted_at": stamp}

    monkeypatch.setenv("AUTO_CONSULT_COOLDOWN_HOURS", "24")
    assert cs._recently_consulted(watch) is True

    monkeypatch.setenv("AUTO_CONSULT_COOLDOWN_HOURS", "1")
    assert cs._recently_consulted(watch) is False


# --- 模板渲染 ---


def test_default_template_is_non_empty():
    assert cs.DEFAULT_TEMPLATE.strip()


def test_default_template_env_override(monkeypatch):
    monkeypatch.setenv("AUTO_CONSULT_TEMPLATE", "自定义：{title}")
    assert cs._default_template() == "自定义：{title}"


def test_default_template_falls_back_when_env_empty(monkeypatch):
    monkeypatch.setenv("AUTO_CONSULT_TEMPLATE", "")
    assert cs._default_template() == cs.DEFAULT_TEMPLATE


def test_render_substitutes_known_placeholders():
    rendered = cs._render("商品:{title} 现价:{price} 提醒价:{alert_price}",
                          {"title": "MacBook", "last_price": 800, "alert_price": 900})
    assert rendered == "商品:MacBook 现价:800 提醒价:900"


def test_render_tolerates_missing_fields():
    """watch 缺字段时用空串兜底，不得抛 KeyError。"""
    assert cs._render("{title}|{price}|{alert_price}", {}) == "||"


def test_render_falls_back_to_default_template_when_empty():
    rendered = cs._render("", {"title": "X"})
    assert rendered == cs.DEFAULT_TEMPLATE


def test_render_strips_surrounding_whitespace():
    assert cs._render("  {title}  ", {"title": "X"}) == "X"


# --- 无可用账号 ---


def test_send_consultation_without_account_records_failure(monkeypatch, watch_id):
    """没有可用账号时必须写日志并返回 failed，不能抛异常打断采集。"""
    monkeypatch.setattr(cs, "load_state_files", lambda _dir: [])
    monkeypatch.setattr(cs, "reserve_account", lambda *a, **k: None)

    result = asyncio.run(
        cs.send_consultation({"id": watch_id, "title": "X", "link": "https://example.invalid/x"})
    )

    assert result["status"] == "failed"
    assert result["error"] == "没有可用账号"

    logs = cs.list_consultation_logs()
    assert logs and logs[0]["status"] == "failed"
    assert logs[0]["error"] == "没有可用账号"


def test_send_consultation_skips_within_cooldown_without_reserving(monkeypatch, watch_id):
    """冷却期内必须直接跳过，且不得占用账号 lease。"""
    reserved = []
    monkeypatch.setattr(cs, "reserve_account", lambda *a, **k: reserved.append(a) or None)

    recent = (datetime.now() - timedelta(minutes=10)).isoformat()
    result = asyncio.run(
        cs.send_consultation(
            {"id": watch_id, "title": "X", "link": "https://example.invalid/x",
             "last_consulted_at": recent}
        )
    )

    assert result["status"] == "skipped"
    assert "小时" in result["reason"]
    assert reserved == []


def test_force_bypasses_cooldown(monkeypatch, watch_id):
    """force=True 必须无视冷却；这里让它走到「无可用账号」以证明未被冷却拦下。"""
    monkeypatch.setattr(cs, "load_state_files", lambda _dir: [])
    monkeypatch.setattr(cs, "reserve_account", lambda *a, **k: None)

    recent = (datetime.now() - timedelta(minutes=10)).isoformat()
    result = asyncio.run(
        cs.send_consultation(
            {"id": watch_id, "title": "X", "link": "https://example.invalid/x",
             "last_consulted_at": recent},
            force=True,
        )
    )

    assert result["status"] == "failed"
    assert result["error"] == "没有可用账号"


# --- 任务级去重（不触网）---


def test_maybe_send_task_consultation_requires_link():
    result = asyncio.run(cs.maybe_send_task_consultation({"商品信息": {"商品标题": "X"}}))
    assert result["status"] == "skipped"
    assert result["reason"] == "无商品链接"


def test_maybe_send_task_consultation_dedupes_by_link():
    """同一链接第二次调用必须被进程内去重拦下。"""
    payload = {
        "商品链接": "https://example.invalid/item/1?spm=abc",
        "商品信息": {"商品标题": "X", "当前售价": "¥100"},
    }

    # 第一次：进入真实发送前的去重登记，这里让 send_task_consultation 直接返回
    async def _fake_send(link, title, price, template=None):
        return {"status": "sent"}

    import src.services.consultation_service as module

    original = module.send_task_consultation
    module.send_task_consultation = _fake_send
    try:
        first = asyncio.run(cs.maybe_send_task_consultation(dict(payload)))
        second = asyncio.run(cs.maybe_send_task_consultation(dict(payload)))
    finally:
        module.send_task_consultation = original

    assert first["status"] == "sent"
    assert second["status"] == "skipped"
    assert second["reason"] == "已咨询过"


def test_dedupe_key_truncates_at_ampersand():
    """去重键按 ``&`` 截断（源码 ``str(link).split("&")[0]``）。

    因此闲鱼链接的追踪参数（``?a=1&b=2`` 中的 ``&b=2``）会被忽略，
    但第一个 ``?`` 之后的参数会保留。两处行为差异一旦被误改成 ``?``
    截断，同一商品会因参数不同而被重复咨询（风控风险）。
    """

    async def _fake_send(link, title, price, template=None):
        return {"status": "sent"}

    import src.services.consultation_service as module

    original = module.send_task_consultation
    module.send_task_consultation = _fake_send
    try:
        payload_a = {"商品链接": "https://example.invalid/i/9?a=1&b=2", "商品信息": {"商品标题": "X"}}
        payload_b = {"商品链接": "https://example.invalid/i/9?a=1&c=3", "商品信息": {"商品标题": "X"}}

        first = asyncio.run(cs.maybe_send_task_consultation(dict(payload_a)))
        second = asyncio.run(cs.maybe_send_task_consultation(dict(payload_b)))
    finally:
        module.send_task_consultation = original

    assert first["status"] == "sent"
    assert second["status"] == "skipped"
    assert second["reason"] == "已咨询过"


def test_dedupe_distinguishes_different_first_query_param():
    """护栏：``?`` 之前的首参不同 => 视为不同商品，不得误去重。"""

    async def _fake_send(link, title, price, template=None):
        return {"status": "sent"}

    import src.services.consultation_service as module

    original = module.send_task_consultation
    module.send_task_consultation = _fake_send
    try:
        asyncio.run(
            cs.maybe_send_task_consultation(
                {"商品链接": "https://example.invalid/i/9?a=1", "商品信息": {"商品标题": "X"}}
            )
        )
        second = asyncio.run(
            cs.maybe_send_task_consultation(
                {"商品链接": "https://example.invalid/i/9?b=2", "商品信息": {"商品标题": "X"}}
            )
        )
    finally:
        module.send_task_consultation = original

    assert second["status"] == "sent"


def test_maybe_send_task_consultation_swallows_send_failure():
    """发送异常必须被吞掉并返回 failed，不能让整批任务崩掉。"""

    async def _boom(link, title, price, template=None):
        raise RuntimeError("browser crashed")

    import src.services.consultation_service as module

    original = module.send_task_consultation
    module.send_task_consultation = _boom
    try:
        result = asyncio.run(
            cs.maybe_send_task_consultation(
                {"商品链接": "https://example.invalid/item/err", "商品信息": {"商品标题": "X"}}
            )
        )
    finally:
        module.send_task_consultation = original

    assert result["status"] == "failed"
    assert "browser crashed" in result["error"]


# --- 日志查询 ---


def test_consultation_logs_empty_initially():
    assert cs.list_consultation_logs() == []


def test_consultation_logs_filter_by_watch_id(watch_id):
    other = asyncio.run(
        ws.add_watch_item(
            {
                "item_id": "C2",
                "title": "另一个商品",
                "link": "https://example.invalid/item/C2",
                "task_name": "TC",
            }
        )
    )
    cs._write_log(watch_id, "m1", "sent", "/acc/1.json", None)
    cs._write_log(other["id"], "m2", "failed", None, "boom")

    assert len(cs.list_consultation_logs(watch_id)) == 1
    assert cs.list_consultation_logs(watch_id)[0]["message"] == "m1"
    assert len(cs.list_consultation_logs()) == 2


def test_consultation_log_limit_is_respected(watch_id):
    for index in range(5):
        cs._write_log(watch_id, f"m{index}", "sent", None, None)
    assert len(cs.list_consultation_logs(limit=3)) == 3


def test_write_log_persists_error_detail(watch_id):
    cs._write_log(watch_id, "msg", "failed", "/acc/x.json", "登录失效")
    row = cs.list_consultation_logs(watch_id)[0]
    assert row["error"] == "登录失效"
    assert row["account_path"] == "/acc/x.json"
    assert row["event_type"] == "low_price"


def test_write_log_requires_existing_watch_item():
    """外键约束：watch_item_id 必须指向真实关注项。

    consultation_logs 定义 ``FOREIGN KEY(watch_item_id) REFERENCES
    watch_items(id)``，且连接启用了 ``PRAGMA foreign_keys=ON``
    （sqlite_connection.py）。因此写入不存在的 watch_id 必须被拒绝。
    """
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        cs._write_log(999999, "msg", "sent", None, None)
