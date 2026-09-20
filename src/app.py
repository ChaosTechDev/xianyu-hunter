"""
新架构的主应用入口
整合所有路由和服务
"""
import asyncio
import hmac
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta

from src.api.routes import (
    dashboard,
    tasks,
    logs,
    settings,
    prompts,
    results,
    login_state,
    websocket,
    accounts,
    watchlist,
    storage,
    search,
)
from src.api import auth as session_auth
from src.api.dependencies import (
    set_process_service,
    set_scheduler_service,
    set_task_generation_service,
)
from src.services.task_service import TaskService
from src.services.process_service import ProcessService
from src.services.scheduler_service import SchedulerService
from src.services.task_log_cleanup_service import cleanup_task_logs
from src.services.task_generation_service import TaskGenerationService
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.infrastructure.config.settings import settings as app_settings
from src.domain.models.task import TaskUpdate


# 全局服务实例
process_service = ProcessService()
scheduler_service = SchedulerService(process_service)
task_generation_service = TaskGenerationService()


async def _sync_task_runtime_status(task_id: int, is_running: bool) -> None:
    task_service = TaskService(SqliteTaskRepository())
    task = await task_service.get_task(task_id)
    if not task or task.is_running == is_running:
        return
    await task_service.update_task_status(task_id, is_running)
    runtime_status = "running"
    failure_reason = None
    if not is_running:
        exit_code = process_service.last_exit_codes.get(task_id)
        manually_stopped = task_id in process_service.manual_stop_task_ids
        if exit_code not in (None, 0) and not manually_stopped:
            runtime_status = "failed"
            failure_reason = f"采集进程异常退出，退出码 {exit_code}"
        else:
            runtime_status = "stopped"
    await task_service.update_task(
        task_id,
        TaskUpdate(runtime_status=runtime_status, last_failure_reason=failure_reason),
    )
    await websocket.broadcast_message(
        "task_status_changed",
        {"id": task_id, "is_running": is_running},
    )


process_service.set_lifecycle_hooks(
    on_started=lambda task_id: _sync_task_runtime_status(task_id, True),
    on_stopped=lambda task_id: _sync_task_runtime_status(task_id, False),
)

# 设置全局 ProcessService 实例供依赖注入使用
set_process_service(process_service)
set_scheduler_service(scheduler_service)
set_task_generation_service(task_generation_service)


async def _account_check_loop() -> None:
    """定期检测账号登录态（保活与失效发现）。"""
    from src.services.account_check_service import check_all_accounts
    interval = max(1, int(app_settings.account_check_interval_hours)) * 3600
    while True:
        try:
            await asyncio.sleep(interval)
            results = await check_all_accounts()
            print(f"[账号检测] 完成，共检测 {len(results)} 个账号")
            for res in results:
                print(f"[账号检测] {res.get('account_path')}: {res.get('status')} - {res.get('detail')}")
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[账号检测] 执行失败: {exc}")

async def _daily_report_loop():
    """行情日报定时任务（每分钟检查，配置变更即时生效）"""
    from src.services.daily_report_service import get_report_config

    def _current_report_schedule():
        cfg = get_report_config()
        hour_str = str(cfg.get("hour", "9:00"))
        try:
            _h, _, _m = hour_str.partition(":")
            _hh = max(0, min(23, int(_h)))
            _mm = max(0, min(59, int(_m or "0")))
        except (ValueError, TypeError):
            _hh, _mm = 9, 0
        return (_hh, _mm, bool(cfg.get("enabled", True)))

    _hh, _mm, enabled = _current_report_schedule()
    print(f"[行情日报] 启动: {'启用' if enabled else '停用'}, 推送时间 {_hh:02d}:{_mm:02d}（每 30 秒检查）")
    last_sent_day = None
    while True:
        try:
            _hh, _mm, enabled = _current_report_schedule()
            now = datetime.now()
            if enabled and now.hour == _hh and now.minute == _mm and last_sent_day != now.date():
                from src.services.daily_report_service import build_and_send_daily_report
                await build_and_send_daily_report()
                last_sent_day = now.date()
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[行情日报] 任务失败: {exc}")
            await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时
    print("正在启动应用...")
    bootstrap_sqlite_storage()
    cleanup_task_logs(keep_days=app_settings.task_log_retention_days)

    # 重置所有任务状态为停止
    task_repo = SqliteTaskRepository()
    task_service = TaskService(task_repo)
    tasks_list = await task_service.get_all_tasks()

    for task in tasks_list:
        if task.is_running:
            await task_service.update_task_status(task.id, False)
        if task.runtime_status in {"running", "paused"}:
            await task_service.update_task(task.id, TaskUpdate(runtime_status="stopped"))

    # 加载定时任务
    await scheduler_service.reload_jobs(tasks_list)
    scheduler_service.start()

    # 启动账号登录态定期检测（保活）
    account_check_task = asyncio.create_task(_account_check_loop())
    # 行情日报定时任务
    daily_report_task = asyncio.create_task(_daily_report_loop())

    print("应用启动完成")

    yield

    # 关闭时
    print("正在关闭应用...")
    scheduler_service.stop()
    account_check_task.cancel()
    daily_report_task.cancel()
    await process_service.stop_all()
    print("应用已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="闲鱼智能监控机器人",
    description="基于AI的闲鱼商品监控系统",
    version="2.0.0",
    lifespan=lifespan
)

# 注册路由
app.include_router(tasks.router)
app.include_router(dashboard.router)
app.include_router(logs.router)
app.include_router(settings.router)
app.include_router(prompts.router)
app.include_router(results.router)
app.include_router(login_state.router)
app.include_router(websocket.router)
app.include_router(accounts.router)
app.include_router(watchlist.router)
app.include_router(storage.router)
app.include_router(search.router)

# 挂载静态文件
# 旧的静态文件目录（用于截图等）
app.mount("/static", StaticFiles(directory="static"), name="static")

# 挂载 Vue 3 前端构建产物
# 注意：需要在所有 API 路由之后挂载，以避免覆盖 API 路由
import os
if os.path.exists("dist"):
    app.mount("/assets", StaticFiles(directory="dist/assets"), name="assets")


# 健康检查端点
@app.get("/health")
async def health_check():
    """健康检查（无需认证）"""
    return {"status": "healthy", "message": "服务正常运行"}


# 认证状态检查端点
from fastapi import Request, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/status")
async def auth_status(payload: LoginRequest):
    """检查认证状态，登录成功后签发 HttpOnly session cookie"""
    # 用常数时间比较，避免通过响应时间侧信道逐字节猜测密码/用户名
    _user_ok = hmac.compare_digest(
        payload.username.encode("utf-8"), app_settings.web_username.encode("utf-8")
    )
    _pass_ok = hmac.compare_digest(
        payload.password.encode("utf-8"), app_settings.web_password.encode("utf-8")
    )
    if _user_ok and _pass_ok:
        token = session_auth.create_session_token(payload.username)
        response = JSONResponse({"authenticated": True, "username": payload.username})
        response.set_cookie(
            key=session_auth.SESSION_COOKIE_NAME,
            value=token,
            max_age=session_auth.session_max_age_seconds(),
            httponly=True,
            samesite="lax",
            path="/",
        )
        return response
    raise HTTPException(status_code=401, detail="认证失败")


@app.middleware("http")
async def require_auth_middleware(request: Request, call_next):
    """保护所有 /api/* 接口：必须携带有效的 session cookie。"""
    path = request.url.path
    if path.startswith("/api/"):
        token = request.cookies.get(session_auth.SESSION_COOKIE_NAME)
        if session_auth.verify_session_token(token) is None:
            return JSONResponse(status_code=401, content={"detail": "未登录或会话已过期"})
    return await call_next(request)


@app.post("/auth/logout")
async def auth_logout():
    """退出登录：清除服务端的 session cookie。

    **为什么必须有这个端点**

    前端原来的 ``logout()`` 只清了 localStorage 里的标记，而真正的凭证是
    **HttpOnly** cookie——JS 根本读不到它，自然也删不掉。结果是「登出」只是把界面
    骗回登录页：cookie 仍然有效，浏览器后退或直接访问任意页面都会重新进入系统，
    直到 72 小时的 TTL 自然到期。

    在共享设备上这不是体验问题，是凭证问题。删除 cookie 必须由服务端下发
    ``Set-Cookie`` 完成（HttpOnly 的设计目的就是不让 JS 碰它）。

    实现上用 ``delete_cookie`` 并显式补齐同样的 path/samesite/httponly：
    cookie 的唯一性由 (name, domain, path) 决定，path 不一致会删不掉原 cookie，
    只留下一个同名新 cookie，看着像成功实际没生效。
    """
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        key=session_auth.SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
    )
    return response


# 主页路由 - 服务 Vue 3 SPA
from fastapi.responses import JSONResponse

@app.get("/")
async def read_root(request: Request):
    """提供 Vue 3 SPA 的主页面"""
    if os.path.exists("dist/index.html"):
        return FileResponse("dist/index.html")
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "前端构建产物不存在，请先运行 cd web-ui && npm run build"}
        )


# Catch-all 路由 - 处理所有前端路由（必须放在最后）
@app.get("/{full_path:path}")
async def serve_spa(request: Request, full_path: str):
    """
    Catch-all 路由，将所有非 API 请求重定向到 index.html
    这样可以支持 Vue Router 的 HTML5 History 模式
    """
    # 如果请求的是静态资源（如 favicon.ico），返回 404
    if full_path.endswith(('.ico', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.css', '.js', '.json')):
        return JSONResponse(status_code=404, content={"error": "资源未找到"})

    # 其他所有路径都返回 index.html，让前端路由处理
    if os.path.exists("dist/index.html"):
        return FileResponse("dist/index.html")
    else:
        return JSONResponse(
            status_code=500,
            content={"error": "前端构建产物不存在，请先运行 cd web-ui && npm run build"}
        )


if __name__ == "__main__":
    import uvicorn
    from src.infrastructure.config.settings import settings

    print(f"启动新架构应用，端口: {app_settings.server_port}")
    uvicorn.run(app, host="0.0.0.0", port=app_settings.server_port)