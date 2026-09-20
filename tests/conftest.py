import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# Add repository root to the path so package imports work consistently
repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo_root))

from src.api import dependencies as deps
from src.api.routes import tasks
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.task_service import TaskService
from src.services.task_generation_service import TaskGenerationService


@pytest.fixture(autouse=True)
def isolated_sqlite_database(tmp_path, monkeypatch):
    """把 SQLite 落库路径指向临时目录。

    src.infrastructure.persistence.sqlite_connection.get_database_path() 每次
    调用都会重新读取 APP_DATABASE_FILE，因此在这里覆盖环境变量即可让全部
    继承测试与新增测试都在隔离库上运行，不再写坏仓库根的 data/app.sqlite3。
    """
    monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "test_app.sqlite3"))


@pytest.fixture(autouse=True)
def isolated_ai_result_cache():
    """每个用例前后清空进程内的 AI 结果缓存。

    ``ai_client`` 里的结果缓存是**模块级全局单例**（跨任务共享，这正是它省钱的
    前提）。但全局状态会跨用例泄漏：前一个用例把某个消息哈希的「成功响应」写进
    缓存后，后一个用例即使把上游 stub 成报错，也会直接命中缓存而不发请求，
    于是「重试」「抛错」这类断言全部失效。

    这里在每个用例前后各清一次，保证用例互不影响。
    """
    from src.infrastructure.external import ai_client as ai_client_module

    def _clear():
        cache = getattr(ai_client_module, "_SHARED_AI_CACHE", None)
        if cache is not None:
            try:
                cache.clear()
            except Exception:
                pass

    _clear()
    yield
    _clear()


@pytest.fixture()
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture()
def load_json_fixture(fixtures_dir):
    def _load(name: str):
        return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))

    return _load


@pytest.fixture()
def sample_task_payload():
    return {
        "task_name": "Sony A7M4",
        "enabled": True,
        "keyword": "sony a7m4",
        "description": "Good condition body with accessories",
        "analyze_images": True,
        "max_pages": 2,
        "personal_only": True,
        "min_price": "8000",
        "max_price": "16000",
        "cron": "*/15 * * * *",
        "ai_prompt_base_file": "prompts/base_prompt.txt",
        "ai_prompt_criteria_file": "prompts/sony_a7m4_criteria.txt",
        "decision_mode": "ai",
        "keyword_rules": [],
    }


class FakeProcessService:
    def __init__(self):
        self.started = []
        self.stopped = []
        self._on_started = None
        self._on_stopped = None

    def set_lifecycle_hooks(self, *, on_started=None, on_stopped=None):
        self._on_started = on_started
        self._on_stopped = on_stopped

    async def start_task(self, task_id: int, task_name: str) -> bool:
        self.started.append((task_id, task_name))
        if self._on_started:
            await self._on_started(task_id)
        return True

    async def stop_task(self, task_id: int):
        self.stopped.append(task_id)
        if self._on_stopped:
            await self._on_stopped(task_id)


class FakeSchedulerService:
    def __init__(self):
        self.reload_calls = 0
        self.next_run_times = {}

    async def reload_jobs(self, tasks):
        self.reload_calls += 1
        base = datetime(2026, 3, 19, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.next_run_times = {
            task.id: base + timedelta(minutes=(index + 1) * 15)
            for index, task in enumerate(tasks)
            if task.id is not None and task.enabled and task.cron
        }

    def get_next_run_time(self, task_id: int):
        return self.next_run_times.get(task_id)


@pytest.fixture()
def api_context(tmp_path):
    config_file = tmp_path / "config.json"
    config_file.write_text("[]", encoding="utf-8")
    db_path = tmp_path / "app.sqlite3"

    repository = SqliteTaskRepository(
        db_path=str(db_path),
        legacy_config_file=None,
    )
    task_service = TaskService(repository)
    process_service = FakeProcessService()
    scheduler_service = FakeSchedulerService()
    task_generation_service = TaskGenerationService()

    app = FastAPI()
    app.include_router(tasks.router)

    def override_get_task_service():
        return task_service

    def override_get_process_service():
        return process_service

    def override_get_scheduler_service():
        return scheduler_service

    def override_get_task_generation_service():
        return task_generation_service

    async def mark_started(task_id: int):
        await task_service.update_task_status(task_id, True)

    async def mark_stopped(task_id: int):
        task = await task_service.get_task(task_id)
        if task:
            await task_service.update_task_status(task_id, False)

    process_service.set_lifecycle_hooks(on_started=mark_started, on_stopped=mark_stopped)

    app.dependency_overrides[deps.get_task_service] = override_get_task_service
    app.dependency_overrides[deps.get_process_service] = override_get_process_service
    app.dependency_overrides[deps.get_scheduler_service] = override_get_scheduler_service
    app.dependency_overrides[deps.get_task_generation_service] = override_get_task_generation_service

    return {
        "app": app,
        "config_file": config_file,
        "db_path": db_path,
        "process_service": process_service,
        "scheduler_service": scheduler_service,
        "task_generation_service": task_generation_service,
    }


@pytest.fixture()
def api_client(api_context):
    return TestClient(api_context["app"])
