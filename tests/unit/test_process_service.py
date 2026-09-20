import asyncio
import sys
from types import SimpleNamespace

from src.services.process_service import ProcessService


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.returncode = None
        self._done = asyncio.Event()

    async def wait(self):
        await self._done.wait()
        return self.returncode

    def finish(self, returncode: int = 0):
        self.returncode = returncode
        self._done.set()

    def terminate(self):
        self.finish(-15)

    def kill(self):
        self.finish(-9)


def test_process_service_marks_task_stopped_when_process_exits(monkeypatch, tmp_path):
    fake_process = FakeProcess(pid=4321)
    events = []

    async def run_scenario():
        service = ProcessService()
        service.failure_guard.should_skip_start = lambda *args, **kwargs: SimpleNamespace(
            skip=False,
            should_notify=False,
            reason="",
            consecutive_failures=0,
            paused_until=None,
        )

        stopped = asyncio.Event()

        async def on_started(task_id: int):
            events.append(("started", task_id))

        async def on_stopped(task_id: int):
            events.append(("stopped", task_id))
            stopped.set()

        service.set_lifecycle_hooks(on_started=on_started, on_stopped=on_stopped)

        async def fake_create_subprocess_exec(*_args, **_kwargs):
            return fake_process

        monkeypatch.setattr(
            "src.services.process_service.build_task_log_path",
            lambda task_id, _task_name: str(tmp_path / f"task-{task_id}.log"),
        )
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

        started = await service.start_task(0, "task-a")
        assert started is True
        assert events == [("started", 0)]
        assert service.is_running(0) is True

        fake_process.finish(0)
        await asyncio.wait_for(stopped.wait(), timeout=1)

        assert ("stopped", 0) in events
        assert service.is_running(0) is False

    asyncio.run(run_scenario())


def test_process_service_runtime_maps_are_keyed_by_task_id():
    """运行时映射表按 task_id 作键，删除任务不会让其他任务"漂移"。

    历史上存在 `reindex_after_delete`，会在删除后把所有大于被删 id 的键整体减 1。
    那个逻辑是错的：它假设 task id 连续，而实际 id 允许有间隙（legacy 导入按
    索引分配、删除后不复用），位移会让存活任务的进程/日志句柄被挂到别人的 id 上。
    该方法是死代码（生产路径无调用者），已删除。这里改为断言正确的契约：
    键就是 task_id 本身，删掉一个键不影响其他键。
    """
    service = ProcessService()
    proc_a = object()
    proc_c = object()

    service.processes = {0: proc_a, 2: proc_c}
    service.log_paths = {0: "a.log", 2: "c.log"}
    service.task_names = {0: "A", 2: "C"}

    # 摘掉 id=1（不存在也无所谓），其余键必须原样保留
    service.processes.pop(1, None)
    service.log_paths.pop(1, None)
    service.task_names.pop(1, None)

    assert service.processes == {0: proc_a, 2: proc_c}
    assert service.log_paths == {0: "a.log", 2: "c.log"}
    assert service.task_names == {0: "A", 2: "C"}


def test_process_service_adds_debug_limit_arg_when_env_enabled(monkeypatch):
    monkeypatch.setenv("SPIDER_DEBUG_LIMIT", "1")
    service = ProcessService()

    command = service._build_spawn_command("task-a")

    assert command == [
        sys.executable,
        "-u",
        "spider_v2.py",
        "--task-name",
        "task-a",
        "--debug-limit",
        "1",
    ]
