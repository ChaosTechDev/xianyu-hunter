"""``scheduler_service`` 的数据保留作业接线测试。

覆盖三件事：

1. 作业**确实**被注册，且 ``reload_jobs`` 不会把它弄丢
   （``remove_all_jobs()`` 会连它一起清掉，必须在末尾重建）
2. 未启用开关时**只扫描不删除**（默认安全）
3. 作业内部抛异常不会把调度器带崩，且返回值解析对得上 ``run_retention`` 的真实形状
"""
from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.services.scheduler_service import (  # noqa: E402
    RETENTION_JOB_HOUR,
    RETENTION_JOB_ID,
    RETENTION_JOB_MINUTE,
    SchedulerService,
)


class _FakeProcessService:
    async def start_task(self, task_id, task_name):  # pragma: no cover - 未被本文件调用
        return True


@pytest.fixture
def service():
    return SchedulerService(_FakeProcessService())


class TestRetentionJobRegistration:
    """注意三点 APScheduler 行为：

    1. 未启动时 job 进 ``_pending_jobs``，``replace_existing`` 不生效、
       ``get_jobs()`` 会返回重复项。生产路径是 ``start()`` → ``reload_jobs()``，
       所以每个用例都在**运行中的事件循环里**启动调度器。
    2. ``AsyncIOScheduler.start()`` 需要当前线程有运行中的事件循环，
       因此用 ``asyncio.run`` 包住整个用例体，而不是只包住被调用的协程。
    3. ``shutdown()`` 会清空 jobstore，**断言必须写在 shutdown 之前**。
       所以这里把断言作为 ``body`` 的一部分传进去，而不是在 ``_run`` 返回后再查。
    """

    @staticmethod
    def _in_loop(service, body):
        async def _main():
            service.scheduler.start()
            try:
                result = body()
                if inspect.isawaitable(result):
                    result = await result
                return result
            finally:
                if service.scheduler.running:
                    service.scheduler.shutdown(wait=False)

        return asyncio.run(_main())

    def test_job_is_registered_with_stable_id(self, service):
        def _body():
            service._schedule_retention_job()
            assert service.scheduler.get_job(RETENTION_JOB_ID) is not None

        self._in_loop(service, _body)

    def test_job_uses_low_traffic_hour(self, service):
        """凌晨执行，避免与采集任务抢 SQLite 写锁。"""

        def _body():
            service._schedule_retention_job()
            job = service.scheduler.get_job(RETENTION_JOB_ID)
            fields = {f.name: str(f) for f in job.trigger.fields}
            assert fields["hour"] == str(RETENTION_JOB_HOUR)
            assert fields["minute"] == str(RETENTION_JOB_MINUTE)

        self._in_loop(service, _body)

    def test_registration_is_idempotent(self, service):
        def _body():
            service._schedule_retention_job()
            service._schedule_retention_job()
            service._schedule_retention_job()
            jobs = [j for j in service.scheduler.get_jobs() if j.id == RETENTION_JOB_ID]
            assert len(jobs) == 1, "重复注册必须被 replace_existing 合并"

        self._in_loop(service, _body)

    def test_reload_jobs_restores_retention_job(self, service):
        """``remove_all_jobs()`` 会连清理作业一起清掉，reload 必须把它加回来。"""

        async def _main():
            service.scheduler.start()
            try:
                service._schedule_retention_job()
                assert service.scheduler.get_job(RETENTION_JOB_ID) is not None
                await service.reload_jobs([])
                assert service.scheduler.get_job(RETENTION_JOB_ID) is not None, (
                    "reload_jobs 后清理作业消失了——任务列表一变，数据就会无限增长"
                )
            finally:
                if service.scheduler.running:
                    service.scheduler.shutdown(wait=False)

        asyncio.run(_main())

    def test_retention_job_survives_task_reload_with_tasks(self, service):
        """有真实（可调度）任务时同样不能丢。

        任务必须带排程（``cron`` 或 ``collection_interval_minutes > 0``），
        否则 ``reload_jobs`` 会按设计跳过它——那属于正常行为，不是缺陷。
        """

        class _Task:
            id = 1
            task_name = "t1"
            enabled = True
            cron = None
            collection_interval_minutes = 30
            retry_limit = 0
            retry_backoff_seconds = 5

        async def _main():
            service.scheduler.start()
            try:
                await service.reload_jobs([_Task()])
                assert service.scheduler.get_job(RETENTION_JOB_ID) is not None
                assert service.scheduler.get_job("task_1") is not None
            finally:
                if service.scheduler.running:
                    service.scheduler.shutdown(wait=False)

        asyncio.run(_main())

    def test_task_without_schedule_is_skipped(self, service):
        """无排程的任务不注册 job——确认上一条用例的前提成立。"""

        class _Task:
            id = 2
            task_name = "no_schedule"
            enabled = True
            cron = None
            collection_interval_minutes = 0
            retry_limit = 0
            retry_backoff_seconds = 5

        async def _main():
            service.scheduler.start()
            try:
                await service.reload_jobs([_Task()])
                assert service.scheduler.get_job("task_2") is None
                # 关键：任务被跳过，但清理作业仍然在
                assert service.scheduler.get_job(RETENTION_JOB_ID) is not None
            finally:
                if service.scheduler.running:
                    service.scheduler.shutdown(wait=False)

        asyncio.run(_main())

    def test_disabled_task_is_skipped_but_retention_remains(self, service):
        class _Task:
            id = 3
            task_name = "disabled"
            enabled = False
            cron = None
            collection_interval_minutes = 30
            retry_limit = 0
            retry_backoff_seconds = 5

        async def _main():
            service.scheduler.start()
            try:
                await service.reload_jobs([_Task()])
                assert service.scheduler.get_job("task_3") is None
                assert service.scheduler.get_job(RETENTION_JOB_ID) is not None
            finally:
                if service.scheduler.running:
                    service.scheduler.shutdown(wait=False)

        asyncio.run(_main())





class TestRetentionJobExecution:
    def test_disabled_by_default_only_scans(self, service, tmp_path, monkeypatch):
        """未设 DATA_RETENTION_ENABLED 时必须只扫描。"""
        monkeypatch.delenv("DATA_RETENTION_ENABLED", raising=False)
        monkeypatch.setenv("DATA_RETENTION_ROOT", str(tmp_path))
        captured = []

        class _Result(dict):
            pass

        def _fake_run(*args, **kwargs):
            return {
                "plan": {},
                "report": {
                    "enabled": False,
                    "deleted_rows": 0,
                    "deleted_files": 0,
                    "note": "保留清理未启用",
                    "warnings": [],
                },
            }

        import src.services.retention_runner as runner

        monkeypatch.setattr(runner, "run_retention", _fake_run)
        monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(a))

        asyncio.run(service._run_retention())
        assert any("仅扫描" in str(a) for a in captured)

    def test_enabled_reports_deletion_counts(self, service, monkeypatch):
        captured = []

        def _fake_run(*args, **kwargs):
            return {
                "plan": {},
                "report": {
                    "enabled": True,
                    "deleted_rows": 12,
                    "deleted_files": 3,
                    "warnings": [],
                },
            }

        import src.services.retention_runner as runner

        monkeypatch.setattr(runner, "run_retention", _fake_run)
        monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(a))

        asyncio.run(service._run_retention())
        text = " ".join(str(a) for a in captured)
        assert "12" in text and "3" in text

    def test_failure_is_swallowed_and_logged(self, service, monkeypatch):
        captured = []

        def _boom(*args, **kwargs):
            raise RuntimeError("disk on fire")

        import src.services.retention_runner as runner

        monkeypatch.setattr(runner, "run_retention", _boom)
        monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(a))

        # 不得抛异常
        asyncio.run(service._run_retention())
        assert any("失败" in str(a) for a in captured)

    def test_warnings_are_surfaced(self, service, monkeypatch):
        captured = []

        def _fake_run(*args, **kwargs):
            return {
                "plan": {},
                "report": {
                    "enabled": False,
                    "deleted_rows": 0,
                    "deleted_files": 0,
                    "note": "n",
                    "warnings": ["某个表没有时间列，已跳过"],
                },
            }

        import src.services.retention_runner as runner

        monkeypatch.setattr(runner, "run_retention", _fake_run)
        monkeypatch.setattr("builtins.print", lambda *a, **k: captured.append(a))

        asyncio.run(service._run_retention())
        assert any("没有时间列" in str(a) for a in captured)


class TestRealRunRetentionShape:
    """防止出现「我以为它返回对象，实际返回 dict」这类静默失配。

    调度器初版按 ``result.executed`` 读属性，而 ``run_retention`` 返回的是 dict，
    于是日志恒打印 ``executed=None``——不报错，但什么也没说明。
    这里直接用真实函数钉住返回契约。
    """

    def test_returns_dict_with_plan_and_report(self, tmp_path, monkeypatch):
        from src.services.retention_runner import run_retention

        monkeypatch.setenv("DATA_RETENTION_ROOT", str(tmp_path))
        monkeypatch.delenv("DATA_RETENTION_ENABLED", raising=False)
        monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "x.sqlite3"))

        result = run_retention()
        assert isinstance(result, dict)
        assert "plan" in result and "report" in result
        assert "enabled" in result["report"]

    def test_default_is_scan_only(self, tmp_path, monkeypatch):
        from src.services.retention_runner import run_retention

        monkeypatch.setenv("DATA_RETENTION_ROOT", str(tmp_path))
        monkeypatch.delenv("DATA_RETENTION_ENABLED", raising=False)
        monkeypatch.setenv("APP_DATABASE_FILE", str(tmp_path / "x.sqlite3"))

        report = run_retention()["report"]
        assert report["enabled"] is False
        assert report["deleted_rows"] == 0
        assert report["deleted_files"] == 0
        assert "note" in report
