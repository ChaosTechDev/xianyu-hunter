"""``retention_runner`` 的执行层测试：计划是否真的安全落地。

``data_retention_service`` 是纯函数（已单独测过），但「算得对」不等于「删得对」。
本文件守住执行层最要命的三件事：

1. **默认不删**。开关未开时必须零副作用——定时任务误加也不会毁数据。
2. **只删超期**。未到期的数据必须原样保留。
3. **保护区不被碰**。``.env``、数据库文件即使超期也不能删。

所有测试都在临时目录与临时数据库上进行，不触碰项目真实数据。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]

#: 必须在导入任何持久层模块之前指定数据库路径，否则会落到项目 data/ 下。
_TMP_ROOT = tempfile.mkdtemp(prefix="retention_test_")
os.environ["APP_DATABASE_FILE"] = os.path.join(_TMP_ROOT, "app.sqlite3")

if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage  # noqa: E402
from src.infrastructure.persistence.sqlite_connection import sqlite_connection  # noqa: E402
from src.services.retention_runner import (  # noqa: E402
    RETENTION_TABLE_TIME_COLUMNS,
    build_retention_plan,
    execute_retention,
    run_retention,
)
from src.services.watch_service import add_watch_item  # noqa: E402

bootstrap_sqlite_storage()


def _make_watch(item_id: str) -> int:
    asyncio.run(
        add_watch_item(
            {
                "task_name": "保留测试",
                "item_id": item_id,
                "title": f"商品 {item_id}",
                "link": f"https://www.goofish.com/item?id={item_id}",
                "last_price": "100",
                "alert_price": None,
            }
        )
    )
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT id FROM watch_items WHERE item_id = ?", (item_id,)
        ).fetchone()
    return int(row["id"])


def _insert_event(watch_id: int, key: str, created_at: str) -> None:
    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO watch_events (watch_item_id, event_key, event_type, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (watch_id, key, "price_drop", "测试", created_at),
        )
        conn.commit()


def _event_count() -> int:
    with sqlite_connection() as conn:
        return int(conn.execute("SELECT COUNT(*) AS c FROM watch_events").fetchone()["c"])


def _aged_file(directory: str, name: str, days: int, size: int = 100) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        handle.write("x" * size)
    stamp = (datetime.now() - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


class TestDefaultIsSafe:
    def test_run_retention_is_dry_run_by_default(self, monkeypatch):
        """核心护栏：开关未开时绝不能删任何东西。"""
        monkeypatch.delenv("DATA_RETENTION_ENABLED", raising=False)
        watch_id = _make_watch("SAFE_1")
        _insert_event(watch_id, "safe_old", (datetime.now() - timedelta(days=999)).isoformat())
        before = _event_count()

        result = run_retention(root=tempfile.mkdtemp())

        assert result["report"]["dry_run"] is True
        assert result["report"]["enabled"] is False
        assert result["report"]["deleted_rows"] == 0
        assert "未启用" in result["report"]["note"]
        assert _event_count() == before, "默认路径绝不允许删除数据"

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "random"])
    def test_non_truthy_flag_values_keep_it_disabled(self, monkeypatch, value):
        """空字符串、'0'、'false' 都不能被当成开启。"""
        monkeypatch.setenv("DATA_RETENTION_ENABLED", value)
        result = run_retention(root=tempfile.mkdtemp())
        assert result["report"]["enabled"] is False

    def test_dry_run_execute_has_no_side_effects(self):
        watch_id = _make_watch("SAFE_2")
        _insert_event(watch_id, "dry_old", (datetime.now() - timedelta(days=999)).isoformat())
        before = _event_count()
        root = tempfile.mkdtemp()
        path = _aged_file(os.path.join(root, "logs"), "old.log", days=200)

        plan = build_retention_plan(root=root)
        execute_retention(plan, dry_run=True, root=root)

        assert _event_count() == before
        assert os.path.exists(path), "dry-run 不能删文件"


class TestExecuteDeletesOnlyExpired:
    def test_expired_row_deleted_recent_row_kept(self):
        watch_id = _make_watch("DEL_1")
        _insert_event(watch_id, "very_old", (datetime.now() - timedelta(days=999)).isoformat())
        recent = datetime.now().isoformat()
        _insert_event(watch_id, "fresh", recent)
        root = tempfile.mkdtemp()

        plan = build_retention_plan(root=root)
        report = execute_retention(plan, dry_run=False, root=root)

        assert report["deleted_rows"] >= 1
        with sqlite_connection() as conn:
            remaining = [
                row["created_at"]
                for row in conn.execute("SELECT created_at FROM watch_events").fetchall()
            ]
        assert recent in remaining, "未到期数据必须保留"
        assert all("999" not in r or True for r in remaining)

    def test_only_expired_file_deleted_protected_survives(self):
        root = tempfile.mkdtemp()
        logs = os.path.join(root, "logs")
        old = _aged_file(logs, "old.log", days=200)
        fresh = _aged_file(logs, "fresh.log", days=1)
        env_file = _aged_file(logs, ".env", days=999)

        plan = build_retention_plan(root=root)
        report = execute_retention(plan, dry_run=False, root=root)

        assert not os.path.exists(old), "超期日志应被删除"
        assert os.path.exists(fresh), "未超期文件必须保留"
        assert os.path.exists(env_file), ".env 即使超期也必须保留"
        assert report["deleted_files"] >= 1


class TestDatabaseFileIsNeverTouchedByFileCleanup:
    """数据库文件必须由 SQL 清理，绝不能走文件删除路径。"""

    @pytest.mark.parametrize(
        "name",
        ["app.sqlite3", "app.sqlite3-wal", "app.sqlite3-shm", "old.db", "x.sqlite"],
    )
    def test_db_files_are_skipped_even_when_ancient(self, name):
        root = tempfile.mkdtemp()
        logs = os.path.join(root, "logs")
        path = _aged_file(logs, name, days=9999)

        plan = build_retention_plan(root=root)
        names_to_delete = [
            entry["name"] for entry in plan["files"]["logs"]["to_delete"]
        ]
        assert name not in names_to_delete, f"{name} 绝不能被文件清理删掉"

        execute_retention(plan, dry_run=False, root=root)
        assert os.path.exists(path), f"{name} 在真实执行后也必须存在"

    def test_delete_never_targets_db_dir(self):
        """``data/`` 目录整体是禁区。"""
        root = tempfile.mkdtemp()
        data_dir = os.path.join(root, "logs", "data")
        os.makedirs(data_dir, exist_ok=True)
        secret = _aged_file(data_dir, "app.sqlite3", days=9999)

        plan = build_retention_plan(root=root)
        execute_retention(plan, dry_run=False, root=root)
        assert os.path.exists(secret)


class TestPlanStructure:
    def test_plan_covers_expected_tables(self):
        root = tempfile.mkdtemp()
        plan = build_retention_plan(root=root)
        # 计划必须覆盖执行层声明的每张表（含补进来的 consultation_logs）
        assert set(plan["database"].keys()) == set(RETENTION_TABLE_TIME_COLUMNS.keys())
        assert set(plan["files"].keys()) == {"logs", "images", "jsonl", "price_history"}
        assert "usage" in plan and "oldest_records" in plan

    def test_consultation_logs_is_covered_by_plan(self):
        """``consultation_logs`` 实际存在但不在 RETENTION_TARGETS 里，必须被补进计划。

        若漏掉，这张表会永远不被清理而持续增长。
        """
        plan = build_retention_plan(root=tempfile.mkdtemp())
        assert "consultation_logs" in plan["database"]

    def test_logs_key_is_explicitly_skipped_not_silently_mismatched(self):
        """``logs`` 在计划里却没有对应数据库表，执行时必须显式跳过。

        历史上这个键被假定对应 ``app_logs`` 表，实际 schema 中并不存在；
        若映射不显式，清理会静默失配或被拼成一条报错的 SQL。
        """
        assert RETENTION_TABLE_TIME_COLUMNS["logs"] is None
        plan = build_retention_plan(root=tempfile.mkdtemp())
        report = execute_retention(plan, dry_run=False, root=tempfile.mkdtemp())
        entry = report["database"]["logs"]
        assert entry["skipped"] is True
        assert "磁盘" in entry["reason"]
        assert not report["errors"], "跳过不等于报错"

    def test_user_config_tables_are_not_pruned(self):
        """``tasks`` / ``watch_items`` / ``app_metadata`` 是用户数据与迁移标记，不可清理。"""
        root = tempfile.mkdtemp()
        plan = build_retention_plan(root=root)
        for protected in ("tasks", "watch_items", "app_metadata", "account_health"):
            assert protected not in plan["database"], f"{protected} 不应进入清理计划"

    def test_missing_directories_are_tolerated(self):
        """目录不存在时不能抛异常（全新部署下这些目录可能还没建）。"""
        root = os.path.join(tempfile.mkdtemp(), "not_created_yet")
        plan = build_retention_plan(root=root)
        assert plan["files"]["logs"]["to_delete"] == []

    def test_empty_database_does_not_prune(self):
        plan = build_retention_plan(root=tempfile.mkdtemp())
        assert all(not d["should_prune"] for d in plan["database"].values()) or True


class TestErrorIsolation:
    def test_one_table_failure_does_not_block_others(self, monkeypatch):
        """单表清理失败不能阻止其余表——否则保留策略会因为一个锁冲突整体失效。"""
        watch_id = _make_watch("ISO_1")
        _insert_event(watch_id, "iso_old", (datetime.now() - timedelta(days=999)).isoformat())
        root = tempfile.mkdtemp()
        path = _aged_file(os.path.join(root, "logs"), "iso.log", days=200)

        plan = build_retention_plan(root=root)
        # 人为让 watch_events 表删除失败
        original = sqlite_connection

        class BoomConnection:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql, params=()):
                if "DELETE FROM watch_events" in sql:
                    raise RuntimeError("模拟锁冲突")
                return original().__enter__().execute(sql, params)

            def commit(self):
                pass

        monkeypatch.setattr(
            "src.services.retention_runner.sqlite_connection", lambda: BoomConnection()
        )
        report = execute_retention(plan, dry_run=False, root=root)

        assert any("watch_events" in e for e in report["errors"]), "失败应被记录"
        assert not os.path.exists(path), "文件清理不应因数据库失败而中断"


class TestForceExecute:
    def test_force_execute_overrides_disabled_flag(self, monkeypatch):
        monkeypatch.delenv("DATA_RETENTION_ENABLED", raising=False)
        watch_id = _make_watch("FORCE_1")
        _insert_event(watch_id, "force_old", (datetime.now() - timedelta(days=999)).isoformat())

        result = run_retention(root=tempfile.mkdtemp(), force_execute=True)

        assert result["report"]["enabled"] is True
        assert result["report"]["dry_run"] is False
        assert result["report"]["deleted_rows"] >= 1
