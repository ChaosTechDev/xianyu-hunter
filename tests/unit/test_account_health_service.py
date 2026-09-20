"""账号健康与租约调度的测试。

账号池是采集链路的关键资源：调度错了会导致同一账号被并发使用（风控），
或把健康账号错误地判定为不可用（采集中断）。因此重点覆盖租约互斥、
状态升级规则、冷却夹取与路径归一。
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import pytest

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.services import account_health_service as ahs


@pytest.fixture(autouse=True)
def _schema():
    bootstrap_sqlite_storage()


# --- 路径归一 ---

#: ``os.path.normpath`` 的分隔符随平台变化（Windows ``\`` / POSIX ``/``），
#: 因此断言一律经过它再比较，避免把某个平台的分隔符写死进用例。
def _norm(expected: str) -> str:
    return expected.replace("\\", os.sep)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("state/a.json", "state\\a.json"),
        ("  a//b/../c  ", "a\\c"),
        ("./state/a.json", "state\\a.json"),
    ],
)
def test_normalize_account_path(raw, expected):
    assert ahs.normalize_account_path(raw) == _norm(expected)


def test_normalize_account_path_is_idempotent_on_platform_separator():
    """已经归一过的路径再归一必须不变（采集链路会多次传递同一路径）。"""
    path = _norm("state\\sub\\x.json")
    assert ahs.normalize_account_path(path) == path
    assert ahs.normalize_account_path(ahs.normalize_account_path(path)) == path


@pytest.mark.skipif(os.sep != "\\", reason="反斜杠仅在 Windows 上是路径分隔符")
def test_normalize_account_path_converts_forward_slashes_on_windows():
    """Windows 上正/反斜杠混用时统一成反斜杠（cookies 路径来自配置，格式不定）。"""
    assert ahs.normalize_account_path("state/sub\\x.json") == "state\\sub\\x.json"


def test_normalize_account_path_empty_input():
    """记录实测行为：``normpath("")`` 返回 ``"."`` 而不是空串。

    这意味着空路径会被当成「当前目录」参与调度。调用方已在上游用
    ``if str(path).strip()`` 过滤空值（见 reserve_account:129），因此这里
    只固定行为，供后续改动时察觉。
    """
    assert ahs.normalize_account_path("") == "."


# --- 初始状态 ---


def test_fresh_account_is_unknown_and_available():
    rows = ahs.list_account_health(["state/new.json"])
    assert len(rows) == 1
    assert rows[0]["status"] == ahs.STATUS_UNKNOWN
    assert rows[0]["available"] is True
    assert rows[0]["consecutive_failures"] == 0


def test_list_account_health_empty_input():
    assert ahs.list_account_health([]) == []


def test_list_account_health_reports_all_requested_paths():
    rows = ahs.list_account_health(["state/a.json", "state/b.json"])
    assert len(rows) == 2
    assert {row["account_path"] for row in rows} == {_norm("state\\a.json"), _norm("state\\b.json")}


# --- 预留与租约互斥 ---


def test_reserve_account_returns_first_available():
    account = ahs.reserve_account(["state/a.json", "state/b.json"], owner="o1")
    assert account is not None
    assert account.endswith("a.json")


def test_reserve_account_skips_already_leased_account():
    """核心互斥断言：同一账号在租约内不得被第二个 owner 拿走。"""
    first = ahs.reserve_account(["state/a.json", "state/b.json"], owner="o1")
    second = ahs.reserve_account(["state/a.json", "state/b.json"], owner="o2")
    assert first != second


def test_reserve_account_round_robins_across_owners():
    first = ahs.reserve_account(["state/a.json", "state/b.json"], owner="o1")
    second = ahs.reserve_account(["state/a.json", "state/b.json"], owner="o2")
    assert {first, second} == {_norm("state\\a.json"), _norm("state\\b.json")}


def test_lease_is_advisory_not_exclusive_for_single_account_pool():
    """**重要契约**：租约是「软」的，不是互斥锁。

    只有一个账号时，即使已被 o1 占用，o2 仍能拿到它 —— 排序里
    ``active_leases ASC`` 只是**优先级**（优先用空闲账号），而非硬性排斥。
    真正的互斥由上层 rotation/excluded_accounts 负责（见 src/scraper.py:587）。
    把它当互斥锁会导致误判「账号被占用」而中断采集，因此显式固定该语义。
    """
    first = ahs.reserve_account(["state/a.json"], owner="o1")
    second = ahs.reserve_account(["state/a.json"], owner="o2")
    assert first == second == _norm("state\\a.json")


def test_reserve_prefers_account_without_active_lease():
    """多账号池下必须优先选空闲账号（软租约的实际作用）。"""
    first = ahs.reserve_account(["state/a.json", "state/b.json"], owner="o1")
    second = ahs.reserve_account(["state/a.json", "state/b.json"], owner="o2")
    assert {first, second} == {_norm("state\\a.json"), _norm("state\\b.json")}


def test_reserve_account_deletes_previous_lease_for_same_owner():
    """同一 owner 重新预留时先清理自己的旧租约，避免租约泄漏堆积。"""
    ahs.reserve_account(["state/a.json"], owner="o1")
    ahs.reserve_account(["state/a.json"], owner="o1")
    with ahs._account_connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM account_leases WHERE owner = ?", ("o1",)
        ).fetchone()["c"]
    assert count == 1


def test_reserve_account_returns_none_for_empty_pool():
    assert ahs.reserve_account([], owner="o1") is None


def test_same_owner_can_re_reserve_its_own_lease():
    """同一 owner 重复预留应延续自己的租约，而不是被自己挡住。"""
    first = ahs.reserve_account(["state/a.json"], owner="o1")
    again = ahs.reserve_account(["state/a.json"], owner="o1")
    assert first == again


def test_release_account_removes_lease_record():
    ahs.reserve_account(["state/a.json"], owner="o1")
    with ahs._account_connection() as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM account_leases WHERE owner = ?", ("o1",)
        ).fetchone()["c"]
    assert before == 1

    ahs.release_account("o1")
    with ahs._account_connection() as conn:
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM account_leases WHERE owner = ?", ("o1",)
        ).fetchone()["c"]
    assert after == 0


def test_release_unknown_owner_is_noop():
    ahs.reserve_account(["state/a.json"], owner="o1")
    ahs.release_account("never-existed")
    with ahs._account_connection() as conn:
        still = conn.execute(
            "SELECT COUNT(*) AS c FROM account_leases WHERE owner = ?", ("o1",)
        ).fetchone()["c"]
    assert still == 1


def test_lease_seconds_is_clamped_to_sixty():
    """**重要契约**：``lease_seconds`` 被 ``max(60, ...)`` 夹取，最小 60 秒。

    源码 account_health_service.py:168。后果：传 1 秒并不会得到 1 秒租约，
    测试或清理逻辑不能依赖「短租约立即过期」。这里通过直接检查
    ``expires_at`` 与 ``created_at`` 的差值来固定该行为（不 sleep）。
    """
    from datetime import datetime

    ahs.reserve_account(["state/a.json"], owner="o1", lease_seconds=1)
    with ahs._account_connection() as conn:
        row = conn.execute(
            "SELECT created_at, expires_at FROM account_leases WHERE owner = ?", ("o1",)
        ).fetchone()
    span = datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"])
    assert span.total_seconds() == 60


def test_expired_lease_is_purged_on_next_reserve():
    """过期租约必须被物理删除，避免租约表无限增长。

    这里用 SQL 直接回拨 ``expires_at`` 来触发清理，避免真实等待 60 秒。
    """
    ahs.reserve_account(["state/a.json"], owner="o1")
    with ahs._account_connection() as conn:
        conn.execute(
            "UPDATE account_leases SET expires_at = ? WHERE owner = ?",
            ("2000-01-01T00:00:00+00:00", "o1"),
        )
        conn.commit()

    ahs.reserve_account(["state/a.json"], owner="o2")

    with ahs._account_connection() as conn:
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM account_leases WHERE owner = ?", ("o1",)
        ).fetchone()["c"]
    assert remaining == 0


# --- 状态记录 ---


def test_record_success_marks_healthy_and_available():
    path = "state/a.json"
    ahs.record_account_success(path)
    row = ahs.list_account_health([path])[0]
    assert row["status"] == ahs.STATUS_HEALTHY
    assert row["available"] is True
    assert row["consecutive_failures"] == 0
    assert row["last_success_at"]


def test_record_failure_defaults_to_cooling_down():
    path = "state/a.json"
    ahs.record_account_failure(path, "风控")
    row = ahs.list_account_health([path])[0]
    assert row["status"] == ahs.STATUS_COOLING_DOWN
    assert row["consecutive_failures"] == 1
    assert row["last_error"] == "风控"


def test_cooling_down_account_is_unavailable_by_default():
    """冷却中的账号默认不参与调度，避免连续触发风控。"""
    path = "state/a.json"
    ahs.record_account_failure(path, "风控", cooldown_seconds=900)
    assert ahs.list_account_health([path])[0]["available"] is False
    assert ahs.reserve_account([path], owner="o1") is None


def test_include_unhealthy_allows_cooling_account():
    """显式 include_unhealthy 时必须仍能取到，供降级重试使用。"""
    path = "state/a.json"
    ahs.record_account_failure(path, "风控", cooldown_seconds=900)
    assert ahs.reserve_account([path], owner="o1", include_unhealthy=True) == _norm("state\\a.json")


def test_login_required_is_blocked_by_default():
    path = "state/a.json"
    ahs.record_account_failure(path, "登录失效", status=ahs.STATUS_LOGIN_REQUIRED)
    row = ahs.list_account_health([path])[0]
    assert row["status"] == ahs.STATUS_LOGIN_REQUIRED
    assert row["available"] is False
    assert ahs.reserve_account([path], owner="o1") is None


def test_include_unhealthy_overrides_even_blocking_status():
    """**重要契约**：``include_unhealthy=True`` 是无条件放行，连硬阻塞状态也绕过。

    实测 ``reserve_account([login_required 账号], include_unhealthy=True)`` 仍会
    返回该账号。这是**有意的设计**：src/scraper.py:578-583 用它实现
    ``forced_account``（用户显式指定的账号必须被使用，哪怕它健康状态是
    login_required，否则用户手动指定账号时会静默失效）。

    注意后果：该参数不是「降级容忍」，而是「跳过全部健康检查」。
    只有 forced_account 路径才可以传 True；正常轮转必须传 False
    （见 src/scraper.py:592）。
    """
    path = "state/a.json"
    ahs.record_account_failure(path, "登录失效", status=ahs.STATUS_LOGIN_REQUIRED)
    assert ahs.reserve_account([path], owner="forced", include_unhealthy=True) == _norm("state\\a.json")


def test_verification_required_blocked_by_default():
    path = "state/a.json"
    ahs.record_account_failure(path, "需要验证", status=ahs.STATUS_VERIFICATION_REQUIRED)
    assert ahs.reserve_account([path], owner="o1") is None


def test_blocking_statuses_membership():
    assert ahs.STATUS_LOGIN_REQUIRED in ahs.BLOCKING_STATUSES
    assert ahs.STATUS_VERIFICATION_REQUIRED in ahs.BLOCKING_STATUSES
    assert ahs.STATUS_COOLING_DOWN not in ahs.BLOCKING_STATUSES
    assert ahs.STATUS_HEALTHY not in ahs.BLOCKING_STATUSES


def test_consecutive_failures_accumulate():
    path = "state/a.json"
    for _ in range(3):
        ahs.record_account_failure(path, "风控")
    assert ahs.list_account_health([path])[0]["consecutive_failures"] == 3


def test_success_resets_failure_counter():
    path = "state/a.json"
    ahs.record_account_failure(path, "风控")
    ahs.record_account_failure(path, "风控")
    ahs.record_account_success(path)
    row = ahs.list_account_health([path])[0]
    assert row["consecutive_failures"] == 0
    assert row["status"] == ahs.STATUS_HEALTHY


def test_zero_cooldown_degrades_to_unknown_and_available():
    """冷却 0 秒：状态从 cooling_down 降级为 unknown（非硬阻塞）且立即可用。"""
    path = "state/a.json"
    ahs.record_account_failure(path, "风控", cooldown_seconds=0)
    row = ahs.list_account_health([path])[0]
    assert row["status"] == ahs.STATUS_UNKNOWN
    assert row["available"] is True


def test_negative_cooldown_is_clamped_not_treated_as_error():
    """负冷却不得导致异常状态；行为应与 0 一致。"""
    path = "state/a.json"
    ahs.record_account_failure(path, "风控", cooldown_seconds=-5)
    row = ahs.list_account_health([path])[0]
    assert row["available"] is True
    assert row["status"] == ahs.STATUS_UNKNOWN


def test_cooling_down_recovers_after_cooldown_expires():
    path = "state/a.json"
    ahs.record_account_failure(path, "风控", cooldown_seconds=1)
    assert ahs.list_account_health([path])[0]["available"] is False
    time.sleep(1.1)
    assert ahs.list_account_health([path])[0]["available"] is True


# --- 重置与删除 ---


def test_reset_account_health_clears_failures():
    path = "state/a.json"
    ahs.record_account_failure(path, "风控", status=ahs.STATUS_LOGIN_REQUIRED)
    ahs.reset_account_health(path)
    row = ahs.list_account_health([path])[0]
    assert row["status"] == ahs.STATUS_UNKNOWN
    assert row["consecutive_failures"] == 0
    assert row["available"] is True


def test_delete_account_health_removes_row():
    path = "state/a.json"
    ahs.record_account_failure(path, "风控")
    ahs.delete_account_health(path)
    # 删除后重新读取会按新账号重建，状态回到 unknown
    assert ahs.list_account_health([path])[0]["status"] == ahs.STATUS_UNKNOWN


def test_reset_unknown_path_does_not_raise():
    ahs.reset_account_health("state/never-seen.json")


def test_delete_unknown_path_does_not_raise():
    ahs.delete_account_health("state/never-seen.json")


# --- 文件变更侦测 ---


def test_changed_state_file_resets_status_to_unknown(tmp_path):
    """账号文件被替换（重新登录）后，历史失败状态必须失效。"""
    account_file = tmp_path / "acc.json"
    account_file.write_text("{}", encoding="utf-8")

    ahs.record_account_failure(str(account_file), "登录失效", status=ahs.STATUS_LOGIN_REQUIRED)
    assert ahs.list_account_health([str(account_file)])[0]["status"] == ahs.STATUS_LOGIN_REQUIRED

    time.sleep(0.01)
    account_file.write_text('{"cookies": []}', encoding="utf-8")
    # 不同文件系统的时间戳精度不同，显式推进 mtime 保证侦测生效
    import os

    future = time.time() + 10
    os.utime(account_file, (future, future))

    row = ahs.list_account_health([str(account_file)])[0]
    assert row["status"] == ahs.STATUS_UNKNOWN
    assert row["available"] is True


def test_list_account_health_sorted_for_stable_display():
    rows = ahs.list_account_health(["state/b.json", "state/a.json"])
    assert len(rows) == 2


# --- 时间工具 ---


def test_now_is_utc_aware():
    assert ahs._now().tzinfo is not None


def test_parse_handles_bad_input():
    assert ahs._parse(None) is None
    assert ahs._parse("") is None
    assert ahs._parse("not-a-date") is None


def test_iso_parse_round_trip():
    moment = ahs._now()
    assert ahs._parse(ahs._iso(moment)) is not None


def test_parse_handles_naive_and_aware_are_comparable():
    """混合时区输入不得让比较抛 TypeError。"""
    naive = (datetime.now() - timedelta(seconds=1)).isoformat()
    assert ahs._parse(naive) is not None
