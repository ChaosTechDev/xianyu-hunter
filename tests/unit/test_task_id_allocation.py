"""任务 id 分配契约：单调递增、永不复用。

覆盖一个在 NAS 真机实测中复现的真实缺陷：原先 id 用 ``COALESCE(MAX(id), -1) + 1``
分配，删除当前最大 id 后，下一个新任务会重新拿到同一个 id
（实测：建任务得 id=0 → 删除 → 再建又是 id=0）。
id 复用会让新任务继承已删任务的残留运行时状态——日志文件按
``resolve_task_log_path(task_id, task_name)`` 定位，进程/日志句柄表按 task_id
作键，因此复用 id 可能读到上一个任务的日志或进程槽位。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.domain.models.task import Task, TaskCreate
from src.infrastructure.persistence.sqlite_task_repository import (
    TASK_ID_HIGH_WATERMARK_KEY,
    SqliteTaskRepository,
)
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services.task_service import TaskService


@pytest.fixture
def repo(tmp_path):
    """每个用例一个全新 SQLite 库，且不使用 legacy config.json 迁移。"""
    return SqliteTaskRepository(
        db_path=str(tmp_path / "tasks.sqlite3"),
        legacy_config_file=None,
    )


@pytest.fixture
def service(repo):
    return TaskService(repo)


def _create(service: TaskService, name: str) -> Task:
    payload = TaskCreate(
        task_name=name,
        keyword=f"kw-{name}",
        description="测试用",
        enabled=False,
    )
    return asyncio.run(service.create_task(payload))


def _ids(service: TaskService) -> list[int]:
    tasks = asyncio.run(service.get_all_tasks())
    return [t.id for t in tasks]


def test_first_task_gets_id_zero(service):
    """首个任务是 id=0（既有契约，前端与日志路径都依赖它可用）。"""
    task = _create(service, "first")
    assert task.id == 0


def test_ids_increment_monotonically(service):
    a, b, c = _create(service, "a"), _create(service, "b"), _create(service, "c")
    assert [a.id, b.id, c.id] == [0, 1, 2]


def test_id_is_not_reused_after_deleting_last_task(service):
    """删除最大 id 后再建任务，不得复用那个 id。

    这是本文件的核心回归点。修复前：删除 id=1 后新建任务又拿到 id=1。
    """
    _create(service, "a")        # id 0
    second = _create(service, "b")  # id 1
    assert second.id == 1

    assert asyncio.run(service.delete_task(second.id)) is True
    assert _ids(service) == [0]

    third = _create(service, "c")
    assert third.id == 2, "删除最大 id 后新任务必须拿到更大的 id，不能复用 1"


def test_id_is_not_reused_after_deleting_all_tasks(service):
    _create(service, "a")
    _create(service, "b")
    for task_id in _ids(service):
        asyncio.run(service.delete_task(task_id))
    assert _ids(service) == []

    nxt = _create(service, "after-cleanup")
    assert nxt.id == 2, "清空任务表后 id 也不得回退复用"


def test_id_survives_repository_reinstantiation(repo, tmp_path):
    """高水位标记必须持久化在库里，而不是只存在仓储实例的内存里。"""
    first = TaskService(repo)
    _create(first, "a")
    second = _create(first, "b")
    asyncio.run(first.delete_task(second.id))

    # 换一个全新的仓储实例指向同一个库文件，模拟应用重启
    fresh_repo = SqliteTaskRepository(
        db_path=str(tmp_path / "tasks.sqlite3"),
        legacy_config_file=None,
    )
    nxt = _create(TaskService(fresh_repo), "c")
    assert nxt.id == 2, "重启后 id 也不得复用，高水位必须落库"


def test_watermark_recorded_in_app_metadata(repo, service):
    task = _create(service, "a")
    with sqlite_connection(str(repo.db_path)) as conn:
        row = conn.execute(
            "SELECT value FROM app_metadata WHERE key = ?",
            (TASK_ID_HIGH_WATERMARK_KEY,),
        ).fetchone()
    assert row is not None, "应写入 id 高水位标记"
    assert int(row["value"]) == task.id


def test_update_keeps_existing_id(service):
    """更新不得改变 id，也不得推进高水位。"""
    task = _create(service, "a")
    again = asyncio.run(service.get_task(task.id))
    again.description = "改过了"
    saved = asyncio.run(service.repository.save(again))
    assert saved.id == task.id

    # 更新不应让下一个新任务跳号
    nxt = _create(service, "b")
    assert nxt.id == 1


def test_watermark_tolerates_corrupted_value(repo, service):
    """高水位值被写坏时必须降级，不能抛异常阻断建任务。"""
    _create(service, "a")
    with sqlite_connection(str(repo.db_path)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
            (TASK_ID_HIGH_WATERMARK_KEY, "not-an-int"),
        )
        conn.commit()

    nxt = _create(service, "b")
    # 损坏值被忽略，回退到 MAX(id)+1
    assert nxt.id == 1
