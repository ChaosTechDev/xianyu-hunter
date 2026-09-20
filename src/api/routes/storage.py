"""存储与数据保留路由。

把 ``retention_runner`` 的能力开放给界面：查看各目录磁盘占用、预览清理计划、
（显式确认后）执行清理。

**为什么扫描与执行分成两个端点**

清理是不可逆的破坏性操作。合成一个端点固然省事，但会让「点一下看看占用」
和「点一下删数据」变成同一个动作。分开之后：

- ``GET /api/storage/usage`` 纯只读，随便点；
- ``POST /api/storage/retention/execute`` 必须显式带 ``confirm=true``，
  否则即使 ``DATA_RETENTION_ENABLED`` 已开启也只做 dry-run 并如实告知。

这样误触不会造成任何后果。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from src.services.retention_runner import build_retention_plan, run_retention


router = APIRouter(prefix="/api/storage", tags=["storage"])


class RetentionExecuteRequest(BaseModel):
    #: 必须显式为 true 才真正删除。默认 false 保证「没传就是只预览」。
    confirm: bool = False
    #: 自定义保留天数；不传则用 RetentionPolicy 的默认值
    result_items_days: int | None = None
    price_snapshots_days: int | None = None
    watch_events_days: int | None = None
    logs_days: int | None = None
    ai_usage_days: int | None = None


def _policy_from_request(payload: RetentionExecuteRequest):
    """按请求构造保留策略。

    优先级：请求里显式给出的字段 > 环境变量配置 > ``RetentionPolicy`` 默认值。
    以环境变量为基底是必要的，否则网页上「只改日志天数」会把其余表悄悄重置回
    默认值，与用户在 ``.env`` 里的配置冲突。
    """
    from src.services.data_retention_service import RetentionPolicy
    from src.services.retention_runner import policy_from_env

    base = policy_from_env()
    defaults = RetentionPolicy()
    return RetentionPolicy(
        result_items_days=payload.result_items_days
        if payload.result_items_days is not None
        else getattr(base, "result_items_days", defaults.result_items_days),
        price_snapshots_days=payload.price_snapshots_days
        if payload.price_snapshots_days is not None
        else getattr(base, "price_snapshots_days", defaults.price_snapshots_days),
        watch_events_days=payload.watch_events_days
        if payload.watch_events_days is not None
        else getattr(base, "watch_events_days", defaults.watch_events_days),
        logs_days=payload.logs_days
        if payload.logs_days is not None
        else getattr(base, "logs_days", defaults.logs_days),
        ai_usage_days=payload.ai_usage_days
        if payload.ai_usage_days is not None
        else getattr(base, "ai_usage_days", defaults.ai_usage_days),
    )


@router.get("/usage")
async def get_storage_usage() -> dict:
    """查看磁盘占用与各表最早记录时间（只读，不产生任何副作用）。"""
    try:
        plan = build_retention_plan()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"统计磁盘占用失败: {exc}") from exc
    return {
        "usage": plan["usage"],
        "oldest_records": plan["oldest_records"],
        "generated_at": plan["generated_at"],
    }


@router.get("/retention/plan")
async def get_retention_plan() -> dict:
    """预览清理计划（只读）。界面据此展示「将要删除什么」。"""
    try:
        plan = build_retention_plan()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成清理计划失败: {exc}") from exc
    return {"plan": plan, "dry_run": True}


@router.post("/retention/execute")
async def execute_retention(payload: RetentionExecuteRequest) -> dict:
    """执行保留清理。

    ``confirm`` 不为 true 时只做 dry-run —— 与是否设置 ``DATA_RETENTION_ENABLED``
    无关，这是接口层独立的第二道闸门：环境变量管「这个功能要不要开」，
    ``confirm`` 管「这一次调用是不是真的想删」。
    """
    policy = _policy_from_request(payload)
    try:
        result = run_retention(policy=policy, force_execute=bool(payload.confirm))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"执行保留清理失败: {exc}") from exc

    report = result["report"]
    return {
        "dry_run": report["dry_run"],
        "deleted_rows": report["deleted_rows"],
        "deleted_files": report["deleted_files"],
        "freed_bytes": report["freed_bytes"],
        "freed_human": report.get("freed_human"),
        "database": report["database"],
        "files": report["files"],
        "errors": report["errors"],
        "note": report.get("note")
        or (
            "已按计划执行删除。"
            if not report["dry_run"]
            else "本次为预览（未确认执行），未删除任何数据。"
        ),
    }
