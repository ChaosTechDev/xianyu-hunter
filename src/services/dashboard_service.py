"""
Dashboard 聚合服务
统一汇总任务、结果文件和最近活动，供首页概览使用。
"""
from __future__ import annotations

from typing import Any

from src.domain.models.task import Task
from src.services.dashboard_payloads import (
    build_empty_summary,
    build_task_state_activities,
    normalize_text,
    serialize_timestamp,
    sort_key_by_activity_time,
    sort_key_by_latest_time,
    summarize_result_file,
)
from src.services.result_storage_service import list_result_filenames
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services.ai_usage_service import get_ai_usage_summary

MAX_RECENT_ACTIVITIES = 8


def _build_summary_metrics(tasks: list[Task], summary_list: list[dict[str, Any]], last_updated_at: Any) -> dict[str, Any]:
    with sqlite_connection() as conn:
        operational = conn.execute("""
            SELECT
              (SELECT COUNT(DISTINCT run_id) FROM price_snapshots WHERE snapshot_day = date('now', 'localtime')) scans_today,
              (SELECT COUNT(*) FROM watch_events WHERE event_type = 'low_price' AND substr(created_at,1,10) = date('now','localtime')) low_price_today,
              (SELECT COUNT(*) FROM watch_events WHERE event_type = 'delisted' AND substr(created_at,1,10) = date('now','localtime')) delisted_today,
              (SELECT COUNT(*) FROM account_health WHERE status = 'healthy') healthy_accounts,
              (SELECT COUNT(*) FROM account_health WHERE status IN ('cooling_down','login_required','verification_required')) blocked_accounts
        """).fetchone()
    ai_usage = get_ai_usage_summary(30)
    return {
        "enabled_tasks": sum(1 for task in tasks if task.enabled),
        "running_tasks": sum(1 for task in tasks if task.is_running),
        "paused_tasks": sum(1 for task in tasks if task.runtime_status == "paused"),
        "failed_tasks": sum(1 for task in tasks if task.runtime_status == "failed"),
        "result_files": sum(1 for item in summary_list if item.get("filename")),
        "scanned_items": sum(int(item["total_items"]) for item in summary_list),
        "recommended_items": sum(int(item["recommended_items"]) for item in summary_list),
        "ai_recommended_items": sum(int(item["ai_recommended_items"]) for item in summary_list),
        "keyword_recommended_items": sum(int(item["keyword_recommended_items"]) for item in summary_list),
        "last_updated_at": serialize_timestamp(last_updated_at),
        **dict(operational),
        "ai_cache_hit_rate": ai_usage.get("cache_hit_rate"),
        "ai_cache_supported": ai_usage.get("cache_supported", False),
        "ai_estimated_cost": ai_usage.get("estimated_cost"),
    }


async def build_dashboard_snapshot(tasks: list[Task]) -> dict[str, Any]:
    task_lookup = {normalize_text(task.keyword): task for task in tasks}
    task_summaries: dict[str, dict[str, Any]] = {
        task.task_name: build_empty_summary(task) for task in tasks
    }
    recent_activities = build_task_state_activities(tasks)
    latest_updated_at = None

    for filename in await list_result_filenames():
        summary, activities, file_latest_time = await summarize_result_file(filename, task_lookup)
        if summary:
            task_summaries[summary["task_name"]] = summary
        recent_activities.extend(activities)
        if file_latest_time and (latest_updated_at is None or file_latest_time > latest_updated_at):
            latest_updated_at = file_latest_time

    summary_list = sorted(task_summaries.values(), key=sort_key_by_latest_time, reverse=True)
    focus_file = next((item["filename"] for item in summary_list if item.get("filename")), None)
    return {
        "summary": _build_summary_metrics(tasks, summary_list, latest_updated_at),
        "task_summaries": summary_list,
        "recent_activities": sorted(
            recent_activities,
            key=sort_key_by_activity_time,
            reverse=True,
        )[:MAX_RECENT_ACTIVITIES],
        "focus_file": focus_file,
    }
