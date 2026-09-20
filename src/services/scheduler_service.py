"""
调度服务
负责管理定时任务的调度
"""
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from typing import List

from src.core.cron_utils import build_cron_trigger
from src.domain.models.task import Task
from src.services.process_service import ProcessService
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection

#: 数据保留清理作业的固定 job id。它不是「任务」，不随任务列表增删而变化，
#: 因此单独用一个 id，便于 ``remove_all_jobs()`` 之后精确重建。
RETENTION_JOB_ID = "data_retention"

#: 每天执行清理的时刻（本地时区）。选凌晨低峰，避免与采集任务抢 SQLite 写锁。
RETENTION_JOB_HOUR = 4
RETENTION_JOB_MINUTE = 30


class SchedulerService:
    """调度服务"""

    def __init__(self, process_service: ProcessService):
        self.scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        self.process_service = process_service

    def start(self):
        """启动调度器"""
        if not self.scheduler.running:
            self.scheduler.start()
            print("调度器已启动")

    def stop(self):
        """停止调度器"""
        if self.scheduler.running:
            self.scheduler.shutdown()
            print("调度器已停止")

    def get_next_run_time(self, task_id: int):
        job = self.scheduler.get_job(f"task_{task_id}")
        if job is None:
            return None

        next_run_time = getattr(job, "next_run_time", None)
        if next_run_time is not None:
            return next_run_time

        trigger = getattr(job, "trigger", None)
        if trigger is None or not hasattr(trigger, "get_next_fire_time"):
            return None

        try:
            now = datetime.now(self.scheduler.timezone)
            return trigger.get_next_fire_time(None, now)
        except Exception:
            return None

    async def reload_jobs(self, tasks: List[Task]):
        """重新加载所有定时任务"""
        print("正在重新加载定时任务...")
        self.scheduler.remove_all_jobs()

        # 一个任务可能关注多个商品。采集进程按任务维度运行，因此把该任务下
        # 所有自定义单品周期合并为最短周期，避免同时注册多套相同采集作业。
        bootstrap_sqlite_storage()
        with sqlite_connection() as conn:
            watch_rows = conn.execute(
                """
                SELECT task_name, MIN(refresh_interval_minutes) AS refresh_interval_minutes
                FROM watch_items
                WHERE enabled = 1
                  AND task_name IS NOT NULL AND task_name != ''
                  AND refresh_interval_minutes IS NOT NULL
                  AND refresh_interval_minutes > 0
                GROUP BY task_name
                """
            ).fetchall()
        watch_intervals = {
            str(row["task_name"]): int(row["refresh_interval_minutes"])
            for row in watch_rows
            if row["refresh_interval_minutes"]
        }

        for task in tasks:
            if not task.enabled:
                continue
            custom_interval = watch_intervals.get(task.task_name)
            if custom_interval:
                trigger = IntervalTrigger(
                    minutes=custom_interval,
                    timezone=self.scheduler.timezone,
                )
                schedule_label = f"单品周期 {custom_interval} 分钟"
            elif task.cron or task.collection_interval_minutes > 0:
                try:
                    trigger = (
                        build_cron_trigger(task.cron, timezone=self.scheduler.timezone)
                        if task.cron
                        else IntervalTrigger(minutes=task.collection_interval_minutes, timezone=self.scheduler.timezone)
                    )
                    schedule_label = task.cron or f"任务周期 {task.collection_interval_minutes} 分钟"
                except ValueError as e:
                    print(f"  -> [警告] 任务 '{task.task_name}' 的 Cron 表达式无效: {e}")
                    continue
            else:
                continue

            self.scheduler.add_job(
                self._run_task,
                trigger=trigger,
                args=[task.id, task.task_name, task.retry_limit, task.retry_backoff_seconds],
                id=f"task_{task.id}",
                name=f"Scheduled: {task.task_name}",
                replace_existing=True,
            )
            print(f"  -> 已为任务 '{task.task_name}' 添加定时规则: '{schedule_label}'")

        print("定时任务加载完成")
        self._schedule_retention_job()

    def _schedule_retention_job(self) -> None:
        """注册每日数据保留清理作业。

        设计要点：

        - **始终注册**，但作业内部按 ``DATA_RETENTION_ENABLED`` 决定是否真删。
          未启用时 ``run_retention`` 只做 dry-run，不写库、不删文件——这样
          「配置没打开」与「功能没接上」在日志里能区分开，不会出现
          「以为在清理，其实一年没跑过」的情况。
        - 该作业由 ``reload_jobs`` 一并重建：``remove_all_jobs()`` 会连它一起清掉，
          因此在末尾显式重加，避免任务列表一变动清理作业就永久消失。
        """
        self.scheduler.add_job(
            self._run_retention,
            trigger=CronTrigger(
                hour=RETENTION_JOB_HOUR,
                minute=RETENTION_JOB_MINUTE,
                timezone=self.scheduler.timezone,
            ),
            id=RETENTION_JOB_ID,
            name="Scheduled: 数据保留清理",
            replace_existing=True,
        )
        print(
            f"  -> 已添加数据保留清理作业: 每天 "
            f"{RETENTION_JOB_HOUR:02d}:{RETENTION_JOB_MINUTE:02d}"
        )

    async def _run_retention(self) -> None:
        """执行数据保留清理。

        清理是同步的磁盘/数据库操作，这里用 ``asyncio.to_thread`` 挪到线程池，
        避免阻塞事件循环——调度器与 Web 服务共用同一个循环，同步 IO 会卡住
        所有 HTTP 请求。

        任何异常都在此消化：清理失败属于运维问题，绝不能让它把调度器带崩。
        """
        import asyncio

        def _work():
            from src.services.retention_runner import run_retention

            return run_retention()

        try:
            result = await asyncio.to_thread(_work)
            # run_retention 返回 {"plan": ..., "report": {...}}；报告里
            # deleted_rows / deleted_files 才是「实际删了多少」。enabled=False
            # 时这两个值恒为 0，日志必须把这一点讲清楚，否则运维会误以为
            # 「清理跑了但没删东西是因为没数据」，而真实原因是开关没开。
            report = (result or {}).get("report") or {}
            enabled = bool(report.get("enabled"))
            deleted_rows = report.get("deleted_rows")
            deleted_files = report.get("deleted_files")
            if enabled:
                print(
                    f"[数据保留] 清理完成：删除 {deleted_rows} 行 / "
                    f"{deleted_files} 个文件"
                )
            else:
                print(f"[数据保留] 仅扫描（未启用删除）：{report.get('note', '')}")
            for warning in report.get("warnings", []) or []:
                print(f"[数据保留] 警告：{warning}")
        except Exception as exc:  # noqa: BLE001 — 清理失败不能影响调度器
            print(f"[数据保留] 清理作业失败（已忽略）：{type(exc).__name__}: {exc}")

    async def _run_task(self, task_id: int, task_name: str, retry_limit: int = 0, retry_backoff_seconds: int = 5):
        """执行定时任务"""
        print(f"定时任务触发: 正在为任务 '{task_name}' 启动爬虫...")
        import asyncio
        for attempt in range(max(0, retry_limit) + 1):
            if await self.process_service.start_task(task_id, task_name):
                return
            if attempt < retry_limit:
                await asyncio.sleep(max(1, retry_backoff_seconds) * (2 ** attempt))
