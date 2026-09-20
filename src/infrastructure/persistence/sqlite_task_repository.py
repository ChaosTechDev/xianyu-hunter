"""
基于 SQLite 的任务仓储实现。
"""
from __future__ import annotations

import asyncio
import json
from typing import List, Optional

from src.domain.models.task import Task
from src.domain.repositories.task_repository import TaskRepository
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection

# app_metadata 里记录已分配的最大任务 id，保证 id 永不复用（见 _next_task_id）
TASK_ID_HIGH_WATERMARK_KEY = "tasks:id_high_watermark"


def _row_to_task(row) -> Task:
    payload = dict(row)
    payload["enabled"] = bool(payload["enabled"])
    payload["analyze_images"] = bool(payload["analyze_images"])
    payload["personal_only"] = bool(payload["personal_only"])
    payload["free_shipping"] = bool(payload["free_shipping"])
    payload["is_running"] = bool(payload["is_running"])
    payload["runtime_status"] = payload.get("runtime_status") or (
        "running" if payload["is_running"] else "stopped"
    )
    payload["keyword_rules"] = json.loads(payload.pop("keyword_rules_json") or "[]")
    return Task(**payload)


def find_task_by_name_sync(task_name: str) -> Task | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_name = ? ORDER BY id ASC LIMIT 1",
            (task_name,),
        ).fetchone()
    return _row_to_task(row) if row else None


class SqliteTaskRepository(TaskRepository):
    """基于 SQLite 的任务仓储"""

    def __init__(
        self,
        db_path: str | None = None,
        legacy_config_file: str | None = "config.json",
    ):
        self.db_path = db_path
        self.legacy_config_file = legacy_config_file

    async def find_all(self) -> List[Task]:
        return await asyncio.to_thread(self._find_all_sync)

    async def find_by_id(self, task_id: int) -> Optional[Task]:
        return await asyncio.to_thread(self._find_by_id_sync, task_id)

    async def save(self, task: Task) -> Task:
        return await asyncio.to_thread(self._save_sync, task)

    async def delete(self, task_id: int) -> bool:
        return await asyncio.to_thread(self._delete_sync, task_id)

    def _find_all_sync(self) -> List[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            rows = conn.execute("SELECT * FROM tasks ORDER BY id ASC").fetchall()
        return [_row_to_task(row) for row in rows]

    def _find_by_id_sync(self, task_id: int) -> Optional[Task]:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def _save_sync(self, task: Task) -> Task:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            task_id = task.id
            if task_id is None:
                task_id = self._next_task_id(conn)
            payload = self._task_values(task.model_copy(update={"id": task_id}))
            conn.execute(
                """
                INSERT OR REPLACE INTO tasks (
                    id, task_name, enabled, keyword, description, analyze_images,
                    max_pages, personal_only, min_price, max_price, notify_price_below, auto_consult, notify_mode, strict_keyword_match, cron,
                    ai_prompt_base_file, ai_prompt_criteria_file, account_state_file,
                    account_strategy, free_shipping, new_publish_option, region,
                    decision_mode, keyword_rules_json, is_running,
                    runtime_status, collection_interval_minutes, retry_limit,
                    retry_backoff_seconds, last_failure_reason
                ) VALUES (
                    :id, :task_name, :enabled, :keyword, :description, :analyze_images,
                    :max_pages, :personal_only, :min_price, :max_price, :notify_price_below, :auto_consult, :notify_mode, :strict_keyword_match, :cron,
                    :ai_prompt_base_file, :ai_prompt_criteria_file, :account_state_file,
                    :account_strategy, :free_shipping, :new_publish_option, :region,
                    :decision_mode, :keyword_rules_json, :is_running,
                    :runtime_status, :collection_interval_minutes, :retry_limit,
                    :retry_backoff_seconds, :last_failure_reason
                )
                """,
                payload,
            )
            # 记录高水位，确保删除任务后 id 不被复用
            self._record_id_watermark(conn, task_id)
            conn.commit()
        return task.model_copy(update={"id": task_id})

    def _delete_sync(self, task_id: int) -> bool:
        bootstrap_sqlite_storage(
            self.db_path,
            legacy_config_file=self.legacy_config_file,
        )
        with sqlite_connection(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
        return cursor.rowcount > 0

    def _next_task_id(self, conn) -> int:
        """分配下一个任务 id，保证**单调递增、永不复用**。

        不能只写 ``MAX(id) + 1``：删掉当前最大的 id 后，下一个新任务会重新拿到
        同一个 id（NAS 实测复现：建任务得 id=0 → 删除 → 再建又是 id=0）。
        id 复用会让新任务"继承"已删任务的残留数据——任务日志文件按
        ``resolve_task_log_path(task_id, task_name)`` 定位、进程/日志句柄表按
        task_id 作键，复用 id 可能读到上一个任务的日志。

        因此额外在 ``app_metadata`` 里维护一个高水位标记，取两者较大值 +1。
        水标记只增不减，删除任务不会让它回退。
        """
        row = conn.execute("SELECT COALESCE(MAX(id), -1) AS max_id FROM tasks").fetchone()
        max_existing = int(row["max_id"])

        wm_row = conn.execute(
            "SELECT value FROM app_metadata WHERE key = ?",
            (TASK_ID_HIGH_WATERMARK_KEY,),
        ).fetchone()
        watermark = -1
        if wm_row is not None:
            raw = str(wm_row["value"]).strip()
            try:
                watermark = int(raw)
            except (TypeError, ValueError):
                watermark = -1

        return max(max_existing, watermark) + 1

    def _record_id_watermark(self, conn, task_id: int) -> None:
        """把已分配的最高 id 写入高水位标记（只增不减）。"""
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
            (TASK_ID_HIGH_WATERMARK_KEY, str(int(task_id))),
        )

    def _task_values(self, task: Task) -> dict:
        values = task.model_dump()
        values["enabled"] = int(task.enabled)
        values["analyze_images"] = int(task.analyze_images)
        values["personal_only"] = int(task.personal_only)
        values["free_shipping"] = int(task.free_shipping)
        values["is_running"] = int(task.is_running)
        values["auto_consult"] = int(task.auto_consult)
        values["notify_mode"] = getattr(task, "notify_mode", "keyword") or "keyword"
        values["strict_keyword_match"] = 1 if getattr(task, "strict_keyword_match", True) else 0
        values["keyword_rules_json"] = json.dumps(task.keyword_rules or [], ensure_ascii=False)
        values.pop("keyword_rules", None)
        return values