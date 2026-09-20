"""数据保留执行器：把 ``data_retention_service`` 的纯逻辑计划真正落到数据库与磁盘。

**为什么计划与执行要分开**

:mod:`src.services.data_retention_service` 只负责算「该删什么」，是纯函数、可穷举
测试。真正的破坏性动作（SQL DELETE、删文件）放在这里，因为它必须依赖真实连接、
真实目录，而且需要**默认不执行**的开关——清理是无人值守的定时任务，
一次误删不可恢复，所以要给人留一道显式的闸门。

**安全设计**

1. **默认关闭**。``DATA_RETENTION_ENABLED`` 未显式设为真值时，``run_retention``
   只做扫描并返回计划（dry-run），绝不删任何东西。
2. **数据库瘦身走 SQL，不删文件**。``build_file_cleanup_plan`` 的白名单已挡住
   ``*.sqlite3`` / ``-wal`` / ``-shm``；这里也不去碰它们——WAL 里可能压着尚未
   checkpoint 的已提交事务，删掉等于丢数据。
3. **先删子表再删主表**。``price_snapshots`` 与 ``result_items`` 都以
   ``task_name`` 关联，独立清理互不阻塞，但顺序固定下来便于每步各自失败重试。
4. **每张表独立 try**。一张表清理失败不能阻止其余表——否则一个锁冲突会让保留
   策略整体失效，磁盘持续增长。
5. **返回真实删除行数**，而不是「计划删除行数」。只有实际 ``cursor.rowcount``
   才说明真的生效了。

**已知取舍**：``result_items`` 与 ``price_snapshots`` 按时间列清理。``sqlite`` 在
WAL 模式下 DELETE 不会立刻归还磁盘空间，需要 ``VACUUM``；但 ``VACUUM`` 会重写
整个库且持有写锁，必须由调用方在**空闲时段**显式触发，本模块不自动执行。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Callable

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.services.data_retention_service import (
    RetentionPolicy,
    build_cleanup_plan,
    build_file_cleanup_plan,
    format_bytes,
    resolve_retention_days,
    summarize_usage,
)
#: 表名 -> 用于判断「最早记录」的时间列。这些列名经实际 schema 核对。
#:
#: 注意 ``logs``：``data_retention_service`` 的计划里保留了这个键（它假定存在
#: 一张 ``app_logs`` 表），但**实际 schema 中并不存在该表**——运行日志落在磁盘
#: 的 ``logs/`` 目录里，由文件清理路径处理。因此这里显式映射为 ``None``，
#: 表示「无对应数据库表，跳过 SQL 清理」，而不是静默失配。
#:
#: ``consultation_logs`` 是实际存在但计划未覆盖的表，:func:`build_retention_plan`
#: 会补进计划，否则它会无限增长。
#:
#: 为什么不是每张表都清理：``tasks`` 是用户配置、``app_metadata`` 存迁移标记、
#: ``watch_items`` 是用户主动添加的关注项、``result_blacklist_rules`` 是用户规则——
#: 删掉它们不是「清理垃圾」而是删功能。
RETENTION_TABLE_TIME_COLUMNS: dict[str, str | None] = {
    "result_items": "crawl_time",
    "price_snapshots": "snapshot_time",
    "watch_events": "created_at",
    "ai_usage_stats": "created_at",
    "consultation_logs": "created_at",
    # 运行日志在磁盘上，不落库
    "logs": None,
}

#: 计划中未覆盖、但需要一并清理的「附加表」-> 复用哪张表的保留天数。
#: 这些表在 ``RETENTION_TARGETS`` 里没有条目，但同样会无限增长。
ADDITIONAL_RETENTION_TABLES: dict[str, str] = {
    "consultation_logs": "logs_days",
}

#: 各保留天数字段的默认值，与 ``RetentionPolicy`` 的字段默认保持一致。
#: 用于在 policy 传入非法值时给出合理回落，而不是硬编码 30 天。
RETENTION_FIELD_DEFAULTS: dict[str, int] = {
    "result_items_days": 90,
    "price_snapshots_days": 180,
    "watch_events_days": 180,
    "logs_days": 30,
    "ai_usage_days": 365,
}

#: 需要清理的磁盘目录（项目根下的相对路径）
RETENTION_DIRECTORIES: tuple[str, ...] = ("logs", "images", "jsonl", "price_history")


def _env_flag(name: str, default: bool = False) -> bool:
    """读取布尔环境变量。只有明确的真值才算开启，避免「设了空字符串=开启」。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _project_root() -> str:
    """清理根目录。本文件位于 ``src/services/`` 下，默认取其上两级。

    可用 ``DATA_RETENTION_ROOT`` 覆盖。这个出口不只是为了测试：容器部署时
    日志目录常常挂在独立的数据卷上，需要一个显式指向，而不是假定它一定在
    代码目录旁边。
    """
    override = (os.getenv("DATA_RETENTION_ROOT") or "").strip()
    if override:
        return os.path.abspath(override)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


#: 各保留天数字段对应的环境变量名。用于 :func:`policy_from_env`。
RETENTION_ENV_VARS: dict[str, str] = {
    "result_items_days": "RESULT_ITEMS_RETENTION_DAYS",
    "price_snapshots_days": "PRICE_SNAPSHOTS_RETENTION_DAYS",
    "watch_events_days": "WATCH_EVENTS_RETENTION_DAYS",
    "logs_days": "LOGS_RETENTION_DAYS",
    "ai_usage_days": "AI_USAGE_RETENTION_DAYS",
}


def policy_from_env() -> RetentionPolicy:
    """从环境变量构造保留策略；未设置或非法时沿用 ``RetentionPolicy`` 的默认值。

    非法值（``0``/负数/非数值）由 :func:`resolve_retention_days` 统一回落，
    因此 ``LOGS_RETENTION_DAYS=0`` 只会退回默认天数，**不会**变成「清空整库」。
    """
    defaults = RetentionPolicy()
    resolved: dict[str, int] = {}
    for field_name, env_name in RETENTION_ENV_VARS.items():
        default_days = getattr(defaults, field_name, 30)
        raw = os.getenv(env_name)
        resolved[field_name] = resolve_retention_days(raw, default=default_days)
    return RetentionPolicy(**resolved)


def _oldest_timestamps(rows_spec: dict[str, str | None]) -> dict[str, str | None]:
    """读取各表最早记录的时间文本。表不存在或为空都返回 ``None``。"""
    result: dict[str, str | None] = {table: None for table in rows_spec}
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        for table, column in rows_spec.items():
            if column is None:
                # 没有对应数据库表（如 logs 落在磁盘上），不查、不报错
                continue
            try:
                row = conn.execute(
                    f"SELECT MIN({column}) AS oldest FROM {table}"
                ).fetchone()
            except Exception:
                # 表不存在（旧库未迁移）时按空表处理，不中断整轮清理
                continue
            if row is not None and row["oldest"]:
                result[table] = str(row["oldest"])
    return result


def _collect_directory_entries(directory: str) -> list[dict]:
    """用 ``os.scandir`` 收集目录下第一层条目（不递归）。

    只收第一层是刻意的：``build_file_cleanup_plan`` 的白名单按路径逐段判断，
    但递归扫描会显著放大误删面（例如把用户自己放进去的备份一并扫进来）。
    """
    entries: list[dict] = []
    if not os.path.isdir(directory):
        return entries
    try:
        with os.scandir(directory) as iterator:
            for entry in iterator:
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "name": entry.name,
                        "mtime": datetime.fromtimestamp(stat.st_mtime),
                        "size": int(stat.st_size),
                        "is_dir": entry.is_dir(),
                    }
                )
    except OSError:
        return entries
    return entries


def build_retention_plan(
    *,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
    root: str | None = None,
) -> dict:
    """生成一份完整的保留计划（数据库 + 磁盘 + 用量），**不做任何删除**。

    计划以 ``data_retention_service.build_cleanup_plan`` 的输出为基础，再补上
    :data:`ADDITIONAL_RETENTION_TABLES` 里那些「实际存在但计划未覆盖」的表。
    补这一步是必要的：``consultation_logs`` 不在 ``RETENTION_TARGETS` 里，
    若不补进计划它就永远不会被清理，长期运行会持续增长。
    """
    policy = policy or RetentionPolicy()
    moment = now or datetime.now()
    base = root or _project_root()

    oldest = _oldest_timestamps(RETENTION_TABLE_TIME_COLUMNS)
    db_plan = build_cleanup_plan(policy, now=moment, oldest_dates=oldest)

    for table, days_field in ADDITIONAL_RETENTION_TABLES.items():
        if table in db_plan:
            continue
        # 借用同语义表的保留天数，再走同一套判定，避免另写一份逻辑。
        # 传 default 是为了在 policy 字段非法时仍回落到合理阈值而不是 30 天硬编码。
        days = resolve_retention_days(
            getattr(policy, days_field, None),
            default=RETENTION_FIELD_DEFAULTS.get(days_field, 30),
        )
        cutoff = moment - timedelta(days=days)
        cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
        raw = oldest.get(table)
        should_prune = False
        if raw:
            try:
                parsed = datetime.fromisoformat(str(raw))
                should_prune = parsed < cutoff
            except (TypeError, ValueError):
                should_prune = False
        db_plan[table] = {
            "cutoff": cutoff_text,
            "should_prune": should_prune,
            "reason": (
                f"{table} 最早记录 {raw} 早于保留界限 {cutoff_text}"
                f"（保留 {days} 天），可执行删除。"
                if should_prune
                else f"{table} 无超出保留期（{days} 天）的记录，本次不清理。"
            ),
        }

    file_plans: dict[str, dict] = {}
    usage_input: dict[str, dict] = {}
    for name in RETENTION_DIRECTORIES:
        directory = os.path.join(base, name)
        entries = _collect_directory_entries(directory)
        days_column = {
            "logs": policy.logs_days,
            "images": policy.logs_days,
            "jsonl": policy.result_items_days,
            "price_history": policy.price_snapshots_days,
        }.get(name, policy.logs_days)
        file_plans[name] = build_file_cleanup_plan(
            now=moment,
            directory=name,
            entries=entries,
            retention_days=days_column,
        )
        total_bytes = sum(int(e.get("size") or 0) for e in entries)
        usage_input[name] = {"bytes": total_bytes, "file_count": len(entries)}

    return {
        "generated_at": moment.isoformat(),
        "database": db_plan,
        "files": file_plans,
        "usage": summarize_usage(directories=usage_input),
        "oldest_records": oldest,
    }


def execute_retention(
    plan: dict,
    *,
    dry_run: bool = True,
    root: str | None = None,
) -> dict:
    """执行（或模拟）一份保留计划，返回**实际**删除量。

    ``dry_run=True`` 时只汇总将要删除的内容；这是默认值。
    """
    base = root or _project_root()
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "database": {},
        "files": {},
        "errors": [],
        "deleted_rows": 0,
        "deleted_files": 0,
        "freed_bytes": 0,
    }

    for table, decision in (plan.get("database") or {}).items():
        # column 为 None 表示该键没有对应的数据库表（例如 logs 实际落在磁盘上），
        # 必须显式跳过而不是拼出一条会报错的 SQL。
        column = RETENTION_TABLE_TIME_COLUMNS.get(table)
        if column is None:
            report["database"][table] = {
                "deleted": 0,
                "skipped": True,
                "reason": "无对应数据库表（该数据存放在磁盘目录中，由文件清理路径处理）",
            }
            continue
        if not decision.get("should_prune"):
            report["database"][table] = {"deleted": 0, "skipped": True}
            continue
        cutoff = decision.get("cutoff")
        if not column or not cutoff:
            report["database"][table] = {"deleted": 0, "skipped": True}
            continue
        if dry_run:
            report["database"][table] = {
                "deleted": 0,
                "would_delete": True,
                "cutoff": cutoff,
                "column": column,
            }
            continue
        try:
            with sqlite_connection() as conn:
                cursor = conn.execute(
                    f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)
                )
                deleted = max(0, int(cursor.rowcount or 0))
                conn.commit()
            report["database"][table] = {"deleted": deleted, "cutoff": cutoff}
            report["deleted_rows"] += deleted
        except Exception as exc:
            # 单表失败不影响其余表：否则一个锁冲突会让保留策略整体失效
            report["database"][table] = {"deleted": 0, "error": str(exc)}
            report["errors"].append(f"{table}: {exc}")

    for name, file_plan in (plan.get("files") or {}).items():
        directory = os.path.join(base, name)
        removed: list[str] = []
        freed = 0
        for entry in file_plan.get("to_delete") or []:
            target = os.path.join(directory, str(entry.get("name")))
            if dry_run:
                removed.append(entry.get("name"))
                freed += int(entry.get("size") or 0)
                continue
            try:
                size = os.path.getsize(target)
                os.remove(target)
                removed.append(entry.get("name"))
                freed += size
            except OSError as exc:
                report["errors"].append(f"{target}: {exc}")
        report["files"][name] = {
            "deleted": removed,
            "kept": len(file_plan.get("kept") or []),
            "skipped": len(file_plan.get("skipped") or []),
            "freed_bytes": freed,
        }
        report["deleted_files"] += len(removed)
        report["freed_bytes"] += freed

    report["freed_human"] = format_bytes(report["freed_bytes"])
    return report


def run_retention(
    *,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
    root: str | None = None,
    force_execute: bool = False,
) -> dict:
    """扫描并（在显式允许时）执行保留清理。

    **默认只扫描不删除**：``DATA_RETENTION_ENABLED`` 未开启且未传
    ``force_execute=True`` 时，本函数等价于 dry-run。这样定时任务即使被误加，
    在有开关之前也不会造成任何破坏。
    策略来源优先级：显式传入的 ``policy`` > 环境变量 > ``RetentionPolicy`` 默认值。
    """
    plan = build_retention_plan(
        policy=policy if policy is not None else policy_from_env(), now=now, root=root
    )
    enabled = force_execute or _env_flag("DATA_RETENTION_ENABLED", default=False)
    report = execute_retention(plan, dry_run=not enabled, root=root)
    report["enabled"] = enabled
    if not enabled:
        report["note"] = (
            "保留清理未启用（DATA_RETENTION_ENABLED 未开启），本次仅扫描未删除任何数据。"
        )
    return {"plan": plan, "report": report}
