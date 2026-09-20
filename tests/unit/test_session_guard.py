"""会话守卫测试。

重点覆盖三处容易写错、且错了不会报错只会静默错行为的语义：

1. **复合键隔离**——同一 chat_id 在不同账号下的暂停必须互不影响
2. **暂停取较晚者**——短暂停不能缩短已有长暂停
3. **四动作位是或语义**——任一条规则要求跳过就必须跳过
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pytest

from src.infrastructure.persistence.sqlite_connection import init_schema, sqlite_connection
from src.services.session_guard import (
    DEFAULT_PAUSE_MINUTES,
    FilterDecision,
    SessionGuard,
    merge_filter_decisions,
    pause_key,
)


@pytest.fixture()
def guard(tmp_path):
    db = tmp_path / "guard.sqlite3"
    with sqlite_connection(str(db)) as conn:
        init_schema(conn)
    return SessionGuard(db_path=str(db))


class TestPauseKey:
    def test_same_chat_different_accounts_gives_different_keys(self):
        assert pause_key("seller-1", "chat-x") != pause_key("seller-2", "chat-x")

    def test_none_and_empty_are_equivalent(self):
        assert pause_key(None, "c") == pause_key("", "c")

    def test_encoding_is_injective_where_naive_join_would_collide(self):
        """``("a","b:c")`` 与 ``("a:b","c")`` 必须得到不同的键。

        朴素拼接（``account + sep + chat_id``）无论 sep 取什么可打印字符都会在这组输入上撞车。
        """
        assert pause_key("a", "b:c") != pause_key("a:b", "c")

    def test_key_is_injective_across_plausible_inputs(self):
        """穷举一批形近输入，确认没有两个组合撞成同一个键。

        注意 ``(`"a"`, `"b\\x1f"`)`` 与 ``(`"a"`, `"b"`)`` **应当**映射到同一个键：
        ``str.strip()`` 把 ``\\x1c..\\x1f`` 也当作空白，两段都做了归一化。
        这是有意行为（页面抓来的文本常带零宽/控制空白），因此比较基准是归一化后的值。
        """
        accounts = ["", "a", "a:b", "a/b", "a\x1f", "C:\\state\\acc.json", "1:2"]
        chats = ["", "c", "c:d", "c/d", "c\x1f", "0:", "11:x"]
        seen: dict[str, tuple[str, str]] = {}
        for acc in accounts:
            for chat in chats:
                key = pause_key(acc, chat)
                normalized = (acc.strip(), chat.strip())
                assert key not in seen or seen[key] == normalized, (
                    f"键冲突: {key!r} 同时来自 {seen.get(key)} 与 {normalized}"
                )
                seen[key] = normalized

    def test_key_can_be_parsed_back(self):
        """长度前缀的意义：任意输入都能唯一还原成两段（按归一化后的值）。"""
        for raw_acc in ("", "a", "a:b", "C:\\x\\y.json", "a\x1fb"):
            for raw_chat in ("", "c", "c:d", "c\x1fd"):
                acc, chat = raw_acc.strip(), raw_chat.strip()
                key = pause_key(raw_acc, raw_chat)
                head, _, rest = key.partition(":")
                n = int(head)
                assert rest[:n] == acc
                assert rest[n] == ":"
                assert rest[n + 1:] == chat

    def test_types_are_coerced(self):
        assert pause_key(12345, "chat") == pause_key("12345", "chat")

    def test_whitespace_is_trimmed(self):
        assert pause_key(" acc ", " chat ") == pause_key("acc", "chat")


class TestPause:
    def test_pause_then_paused(self, guard):
        guard.pause(account="a1", chat_id="c1", minutes=5)
        assert guard.is_paused(account="a1", chat_id="c1") is True

    def test_same_chat_isolated_between_accounts(self, guard):
        """核心语义：A 账号暂停不能影响 B 账号的同号会话。"""
        guard.pause(account="seller-1", chat_id="shared", minutes=5)
        assert guard.is_paused(account="seller-1", chat_id="shared") is True
        assert guard.is_paused(account="seller-2", chat_id="shared") is False
        assert guard.remaining_seconds(account="seller-2", chat_id="shared") == 0

    def test_remaining_seconds_is_positive(self, guard):
        guard.pause(account="a1", chat_id="c1", minutes=10)
        remaining = guard.remaining_seconds(account="a1", chat_id="c1")
        assert 0 < remaining <= 10 * 60

    def test_zero_minutes_does_not_pause(self, guard):
        assert guard.pause(account="a1", chat_id="c1", minutes=0) == 0
        assert guard.is_paused(account="a1", chat_id="c1") is False

    def test_negative_minutes_does_not_pause(self, guard):
        assert guard.pause(account="a1", chat_id="c1", minutes=-5) == 0
        assert guard.is_paused(account="a1", chat_id="c1") is False

    def test_shorter_pause_does_not_shorten_longer_one(self, guard):
        """短暂停不能把已有的长暂停降级——用户设的 1 小时不能被 10 分钟顶掉。"""
        guard.pause(account="a1", chat_id="c1", minutes=60)
        before = guard.remaining_seconds(account="a1", chat_id="c1")
        guard.pause(account="a1", chat_id="c1", minutes=1)
        after = guard.remaining_seconds(account="a1", chat_id="c1")
        assert after >= before - 2  # 允许执行耗时的几秒误差

    def test_longer_pause_extends(self, guard):
        guard.pause(account="a1", chat_id="c1", minutes=1)
        guard.pause(account="a1", chat_id="c1", minutes=120)
        assert guard.remaining_seconds(account="a1", chat_id="c1") > 60 * 60

    def test_expired_pause_is_not_paused(self, guard, tmp_path):
        """到期后自动视为未暂停，无需任何清理任务。"""
        with sqlite_connection(str(tmp_path / "guard.sqlite3")) as conn:
            conn.execute(
                "INSERT INTO session_pauses(pause_key, account, chat_id, expires_at, created_at)"
                " VALUES (?, 'a1', 'c1', ?, ?)",
                (
                    pause_key("a1", "c1"),
                    (datetime.now() - timedelta(seconds=5)).isoformat(),
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
        assert guard.is_paused(account="a1", chat_id="c1") is False

    def test_corrupt_expiry_treated_as_expired(self, guard, tmp_path):
        """脏时间戳不能造成永久暂停。"""
        with sqlite_connection(str(tmp_path / "guard.sqlite3")) as conn:
            conn.execute(
                "INSERT INTO session_pauses(pause_key, account, chat_id, expires_at, created_at)"
                " VALUES (?, 'a1', 'c1', 'not-a-date', ?)",
                (pause_key("a1", "c1"), datetime.now().isoformat()),
            )
            conn.commit()
        assert guard.is_paused(account="a1", chat_id="c1") is False

    def test_resume_clears_pause(self, guard):
        guard.pause(account="a1", chat_id="c1", minutes=30)
        guard.resume(account="a1", chat_id="c1")
        assert guard.is_paused(account="a1", chat_id="c1") is False

    def test_default_minutes_constant_is_used(self, guard):
        guard.pause(account="a1", chat_id="c1")
        remaining = guard.remaining_seconds(account="a1", chat_id="c1")
        assert 0 < remaining <= DEFAULT_PAUSE_MINUTES * 60


class TestOrderLock:
    def test_lock_then_locked(self, guard):
        guard.lock_order(account="a1", chat_id="c1", ttl_seconds=600)
        assert guard.is_order_locked(account="a1", chat_id="c1") is True

    def test_order_lock_isolated_between_accounts(self, guard):
        guard.lock_order(account="seller-1", chat_id="shared", ttl_seconds=600)
        assert guard.is_order_locked(account="seller-1", chat_id="shared") is True
        assert guard.is_order_locked(account="seller-2", chat_id="shared") is False

    def test_zero_ttl_means_expired_immediately(self, guard):
        guard.lock_order(account="a1", chat_id="c1", ttl_seconds=0)
        assert guard.is_order_locked(account="a1", chat_id="c1") is False

    def test_unlock_clears(self, guard):
        guard.lock_order(account="a1", chat_id="c1", ttl_seconds=600)
        guard.unlock_order(account="a1", chat_id="c1")
        assert guard.is_order_locked(account="a1", chat_id="c1") is False

    def test_missing_lock_is_not_locked(self, guard):
        assert guard.is_order_locked(account="nobody", chat_id="nope") is False


class TestCooldown:
    def test_mark_then_in_cooldown(self, guard):
        guard.mark_contacted(account="a1", chat_id="c1")
        assert guard.in_cooldown(account="a1", chat_id="c1") is True
        assert guard.cooldown_remaining(account="a1", chat_id="c1") > 0

    def test_cooldown_isolated_between_accounts(self, guard):
        guard.mark_contacted(account="seller-1", chat_id="shared")
        assert guard.in_cooldown(account="seller-1", chat_id="shared") is True
        assert guard.in_cooldown(account="seller-2", chat_id="shared") is False

    def test_never_contacted_is_not_in_cooldown(self, guard):
        assert guard.in_cooldown(account="a1", chat_id="c1") is False


class TestShouldSkipAutoReply:
    def test_fresh_session_is_not_skipped(self, guard):
        skip, reason = guard.should_skip_auto_reply(account="a1", chat_id="c1")
        assert skip is False
        assert reason == ""

    def test_paused_session_is_skipped(self, guard):
        guard.pause(account="a1", chat_id="c1", minutes=10)
        skip, reason = guard.should_skip_auto_reply(account="a1", chat_id="c1")
        assert skip is True
        assert "暂停" in reason

    def test_order_locked_session_is_skipped(self, guard):
        guard.lock_order(account="a1", chat_id="c1", ttl_seconds=600)
        skip, reason = guard.should_skip_auto_reply(account="a1", chat_id="c1")
        assert skip is True
        assert "订单锁" in reason

    def test_order_lock_takes_priority_over_pause(self, guard):
        """两者都命中时原因应指向订单锁（优先级最高）。"""
        guard.pause(account="a1", chat_id="c1", minutes=10)
        guard.lock_order(account="a1", chat_id="c1", ttl_seconds=600)
        _skip, reason = guard.should_skip_auto_reply(account="a1", chat_id="c1")
        assert "订单锁" in reason

    def test_cooldown_alone_does_not_skip_passive_reply(self, guard):
        """冷却只管主动触达，不应拦掉对买家的被动回复。"""
        guard.mark_contacted(account="a1", chat_id="c1")
        skip, _reason = guard.should_skip_auto_reply(account="a1", chat_id="c1")
        assert skip is False

    def test_other_account_pause_does_not_skip(self, guard):
        guard.pause(account="seller-1", chat_id="shared", minutes=10)
        skip, _ = guard.should_skip_auto_reply(account="seller-2", chat_id="shared")
        assert skip is False


class TestFilterDecision:
    def test_default_is_permissive(self):
        d = FilterDecision()
        assert d.matched is False
        assert d.skip_auto_reply is False
        assert d.skip_ai_reply is False
        assert d.notify_enabled is False

    def test_with_rule_marks_matched_and_accumulates(self):
        d = FilterDecision().with_rule("r1").with_rule("r2")
        assert d.matched is True
        assert d.rules == ("r1", "r2")

    def test_notify_only_rule_keeps_replies(self):
        """只通知不拦截：匹配到了，但两个 skip 位都是假。"""
        d = FilterDecision().with_rule("人工关注").with_rule("sensitive")
        d = FilterDecision(
            matched=True,
            skip_auto_reply=False,
            skip_ai_reply=False,
            notify_enabled=True,
            rules=d.rules,
        )
        assert d.matched is True
        assert d.skip_auto_reply is False
        assert d.skip_ai_reply is False
        assert d.notify_enabled is True

    def test_merge_empty_is_permissive(self):
        merged = merge_filter_decisions([])
        assert merged.matched is False
        assert merged.skip_auto_reply is False
        assert merged.skip_ai_reply is False

    def test_merge_is_or_semantics(self):
        merged = merge_filter_decisions(
            [
                FilterDecision(matched=True, skip_auto_reply=True, rules=("a",)),
                FilterDecision(matched=False, skip_ai_reply=True, notify_enabled=True, rules=("b",)),
            ]
        )
        assert merged.matched is True
        assert merged.skip_auto_reply is True
        assert merged.skip_ai_reply is True
        assert merged.notify_enabled is True
        assert merged.rules == ("a", "b")

    def test_merge_preserves_all_rule_names(self):
        merged = merge_filter_decisions(
            [FilterDecision(matched=True, rules=(f"r{i}",)) for i in range(5)]
        )
        assert merged.rules == ("r0", "r1", "r2", "r3", "r4")

    def test_merge_single_notify_only_does_not_block(self):
        merged = merge_filter_decisions(
            [FilterDecision(matched=True, notify_enabled=True, rules=("n",))]
        )
        assert merged.matched is True
        assert merged.skip_auto_reply is False
        assert merged.skip_ai_reply is False


class TestGuardFailsOpen:
    """守卫位于消息主链路上：数据库不可用时必须放行，不能集体失声。"""

    def test_reads_fail_open_on_bad_db_path(self, tmp_path):
        # 指向一个不可能建库的位置
        guard = SessionGuard(db_path=str(tmp_path / "nope" / "\0bad" / "x.sqlite3"))
        assert guard.is_paused(account="a", chat_id="c") is False
        assert guard.is_order_locked(account="a", chat_id="c") is False
        assert guard.in_cooldown(account="a", chat_id="c") is False
        skip, _ = guard.should_skip_auto_reply(account="a", chat_id="c")
        assert skip is False

    def test_writes_do_not_raise_on_bad_db_path(self, tmp_path):
        guard = SessionGuard(db_path=str(tmp_path / "nope" / "\0bad" / "x.sqlite3"))
        guard.pause(account="a", chat_id="c", minutes=5)
        guard.mark_contacted(account="a", chat_id="c")
        guard.resume(account="a", chat_id="c")


class TestPersistenceAcrossInstances:
    """暂停必须落库：容器重启后仍生效。"""

    def test_pause_survives_new_guard_instance(self, tmp_path):
        db = tmp_path / "guard.sqlite3"
        with sqlite_connection(str(db)) as conn:
            init_schema(conn)
        first = SessionGuard(db_path=str(db))
        first.pause(account="a1", chat_id="c1", minutes=30)
        second = SessionGuard(db_path=str(db))
        assert second.is_paused(account="a1", chat_id="c1") is True

    def test_order_lock_survives_new_instance(self, tmp_path):
        db = tmp_path / "guard.sqlite3"
        with sqlite_connection(str(db)) as conn:
            init_schema(conn)
        SessionGuard(db_path=str(db)).lock_order(account="a1", chat_id="c1", ttl_seconds=600)
        assert SessionGuard(db_path=str(db)).is_order_locked(account="a1", chat_id="c1") is True


class TestSchemaIsIdempotent:
    def test_init_schema_twice_is_safe(self, tmp_path):
        db = str(tmp_path / "twice.sqlite3")
        with sqlite_connection(db) as conn:
            init_schema(conn)
        with sqlite_connection(db) as conn:
            init_schema(conn)
            names = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"session_pauses", "session_order_locks", "session_cooldowns"} <= names
