"""关注监控 API。"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.services.watch_service import (
    add_watch_item,
    delete_watch_item,
    get_category_trend,
    get_item_trend,
    get_watch_stats,
    generate_watch_ai_summary,
    list_watch_events,
    list_watch_items,
    mark_all_events_read,
    mark_event_read,
    update_watch_item,
    get_watch_item,
)
from src.services.consultation_service import list_consultation_logs, send_consultation
from src.api.dependencies import get_process_service, get_scheduler_service, get_task_service
from src.services.process_service import ProcessService
from src.services.scheduler_service import SchedulerService
from src.services.task_service import TaskService


router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class CreateWatchRequest(BaseModel):
    item_id: str = Field(min_length=1)
    result_filename: str = ""
    keyword: str = ""
    task_name: str = ""
    title: str = Field(min_length=1)
    link: str = Field(min_length=1)
    image_url: str | None = None
    alert_price: float | None = Field(default=None, ge=0)
    last_price: float | None = Field(default=None, ge=0)
    refresh_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    notify_price_drop: bool = True
    notify_low_price: bool = True
    notify_delisted: bool = True
    notify_relisted: bool = True
    consult_enabled: bool | None = None
    consult_template: str | None = None
    consult_account_strategy: str = "pool"


class UpdateWatchRequest(BaseModel):
    alert_price: float | None = Field(default=None, ge=0)
    enabled: bool | None = None
    refresh_interval_minutes: int | None = Field(default=None, ge=1, le=10080)
    notify_price_drop: bool | None = None
    notify_low_price: bool | None = None
    notify_delisted: bool | None = None
    notify_relisted: bool | None = None
    consult_enabled: bool | None = None
    consult_template: str | None = None
    consult_account_strategy: str | None = None

    def changed_values(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


@router.get("")
async def get_watchlist(include_disabled: bool = Query(True)):
    return {"items": list_watch_items(include_disabled=include_disabled)}


@router.post("", status_code=201)
async def create_watch(
    body: CreateWatchRequest,
    task_service: TaskService = Depends(get_task_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
):
    try:
        payload = body.model_dump()
        # 搜索页只知道关键词时，自动绑定同关键词的现有任务，
        # 这样关注后立即采集和周期调度可以直接生效。
        if not payload.get("task_name") and payload.get("keyword"):
            tasks = await task_service.get_all_tasks()
            matched = next(
                (
                    task
                    for task in tasks
                    if str(task.keyword or "").strip().lower()
                    == str(payload["keyword"]).strip().lower()
                ),
                None,
            )
            if matched:
                payload["task_name"] = matched.task_name
        item = await add_watch_item(payload)
        await scheduler_service.reload_jobs(await task_service.get_all_tasks())
        return item
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/{watch_id}")
async def patch_watch(
    watch_id: int,
    body: UpdateWatchRequest,
    task_service: TaskService = Depends(get_task_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
):
    try:
        item = update_watch_item(watch_id, body.changed_values())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if item is None:
        raise HTTPException(status_code=404, detail="关注商品不存在")
    await scheduler_service.reload_jobs(await task_service.get_all_tasks())
    return item


@router.delete("/{watch_id}")
async def remove_watch(
    watch_id: int,
    task_service: TaskService = Depends(get_task_service),
    scheduler_service: SchedulerService = Depends(get_scheduler_service),
):
    if not delete_watch_item(watch_id):
        raise HTTPException(status_code=404, detail="关注商品不存在")
    await scheduler_service.reload_jobs(await task_service.get_all_tasks())
    return {"message": "已取消关注"}


@router.get("/summary/stats")
async def watch_stats():
    return get_watch_stats()


@router.get("/events/list")
async def watch_events(unread_only: bool = Query(False), limit: int = Query(100, ge=1, le=500)):
    return {"items": list_watch_events(unread_only=unread_only, limit=limit)}


@router.post("/events/read-all")
async def read_all_events():
    return {"updated": mark_all_events_read()}


@router.post("/events/{event_id}/read")
async def read_event(event_id: int):
    if not mark_event_read(event_id):
        raise HTTPException(status_code=404, detail="事件不存在")
    return {"message": "已读"}


@router.get("/trends/item/{watch_id}")
async def item_trend(watch_id: int):
    trend = get_item_trend(watch_id)
    if trend is None:
        raise HTTPException(status_code=404, detail="关注商品不存在")
    return trend


@router.get("/trends/category")
async def category_trend(keyword: str = Query(min_length=1)):
    return get_category_trend(keyword)


@router.post("/ai-summary/{watch_id}")
async def ai_watch_summary(watch_id: int):
    try:
        return await generate_watch_ai_summary(watch_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))


@router.get("/consultations/logs")
async def consultation_logs(
    watch_id: int | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"items": list_consultation_logs(watch_id, limit)}


@router.post("/{watch_id}/consult")
async def test_consultation(watch_id: int):
    watch = get_watch_item(watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="关注商品不存在")
    result = await send_consultation(watch, force=True)
    if result.get("status") == "failed":
        raise HTTPException(status_code=503, detail=result.get("error") or "咨询发送失败")
    return result


@router.post("/{watch_id}/refresh")
async def refresh_watch_item(
    watch_id: int,
    task_service: TaskService = Depends(get_task_service),
    process_service: ProcessService = Depends(get_process_service),
):
    watch = get_watch_item(watch_id)
    if not watch:
        raise HTTPException(status_code=404, detail="关注商品不存在")
    tasks = await task_service.get_all_tasks()
    task = next((item for item in tasks if item.task_name == watch.get("task_name")), None)
    if task is None:
        raise HTTPException(status_code=409, detail="该关注商品没有关联采集任务")
    if process_service.is_running(task.id):
        return {"message": "关联任务正在采集", "task_id": task.id}
    if not await process_service.start_task(task.id, task.task_name):
        raise HTTPException(status_code=503, detail="立即采集启动失败")
    return {"message": "已开始立即采集", "task_id": task.id}
