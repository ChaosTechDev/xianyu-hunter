"""
闲鱼账号管理路由
"""
import json
import os
import re
import aiofiles
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
from src.infrastructure.config.env_manager import env_manager
from src.services.account_check_service import check_account_login_state, check_all_accounts
from src.services.account_health_service import (
    delete_account_health,
    list_account_health,
    reset_account_health,
)


router = APIRouter(prefix="/api/accounts", tags=["accounts"])

ACCOUNT_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,50}$")


class AccountCreate(BaseModel):
    name: str
    content: str


class AccountUpdate(BaseModel):
    content: str


class AccountCheckRequest(BaseModel):
    names: List[str] = []


def _strip_quotes(value: str) -> str:
    if not value:
        return value
    if value.startswith(("\"", "'")) and value.endswith(("\"", "'")):
        return value[1:-1]
    return value


def _state_dir() -> str:
    raw = env_manager.get_value("ACCOUNT_STATE_DIR", "state") or "state"
    return _strip_quotes(raw.strip())


def _ensure_state_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def _validate_name(name: str) -> str:
    trimmed = name.strip()
    if not trimmed or not ACCOUNT_NAME_RE.match(trimmed):
        raise HTTPException(status_code=400, detail="账号名称只能包含字母、数字、下划线或短横线。")
    return trimmed


def _account_path(name: str) -> str:
    filename = f"{name}.json"
    return os.path.join(_state_dir(), filename)


def _validate_json(content: str) -> None:
    try:
        json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="提供的内容不是有效的JSON格式。")


@router.get("", response_model=List[dict])
async def list_accounts():
    state_dir = _state_dir()
    if not os.path.isdir(state_dir):
        return []
    files = sorted(f for f in os.listdir(state_dir) if f.endswith(".json"))
    paths = [os.path.join(state_dir, filename) for filename in files]
    health_by_path = {
        item["account_path"]: item for item in list_account_health(paths)
    }
    accounts = []
    for filename, path in zip(files, paths):
        name = filename[:-5]
        health = health_by_path.get(os.path.normpath(path), {})
        accounts.append({
            "name": name,
            "path": path,
            "status": health.get("status", "unknown"),
            "available": health.get("available", True),
            "active_leases": health.get("active_leases", 0),
            "consecutive_failures": health.get("consecutive_failures", 0),
            "last_success_at": health.get("last_success_at"),
            "last_failure_at": health.get("last_failure_at"),
            "last_error": health.get("last_error"),
            "cooldown_until": health.get("cooldown_until"),
        })
    return accounts


@router.get("/{name}", response_model=dict)
async def get_account(name: str):
    account_name = _validate_name(name)
    path = _account_path(account_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="账号不存在")
    async with aiofiles.open(path, "r", encoding="utf-8") as f:
        content = await f.read()
    return {"name": account_name, "path": path, "content": content}


@router.post("", response_model=dict)
async def create_account(data: AccountCreate):
    account_name = _validate_name(data.name)
    _validate_json(data.content)
    state_dir = _state_dir()
    _ensure_state_dir(state_dir)
    path = _account_path(account_name)
    if os.path.exists(path):
        raise HTTPException(status_code=409, detail="账号已存在")
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(data.content)
    reset_account_health(path)
    return {"message": "账号已添加", "name": account_name, "path": path}


@router.put("/{name}", response_model=dict)
async def update_account(name: str, data: AccountUpdate):
    account_name = _validate_name(name)
    _validate_json(data.content)
    state_dir = _state_dir()
    _ensure_state_dir(state_dir)
    path = _account_path(account_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="账号不存在")
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(data.content)
    reset_account_health(path)
    return {"message": "账号已更新", "name": account_name, "path": path}


@router.delete("/{name}", response_model=dict)
async def delete_account(name: str):
    account_name = _validate_name(name)
    path = _account_path(account_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="账号不存在")
    os.remove(path)
    delete_account_health(path)
    return {"message": "账号已删除"}


@router.post("/{name}/reset-health", response_model=dict)
async def reset_health(name: str):
    account_name = _validate_name(name)
    path = _account_path(account_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="账号不存在")
    reset_account_health(path)
    return {"message": "账号状态已重置", "name": account_name, "path": path}


@router.post("/check", response_model=dict)
async def check_accounts(data: AccountCheckRequest | None = None):
    """一键检测全部（或指定）账号的登录态"""
    if data and data.names:
        results = []
        for name in data.names:
            account_name = _validate_name(name)
            path = _account_path(account_name)
            if not os.path.exists(path):
                raise HTTPException(status_code=404, detail=f"账号 {account_name} 不存在")
            results.append(await check_account_login_state(path))
        return {"results": results}
    results = await check_all_accounts(_state_dir())
    return {"results": results}


@router.post("/{name}/check", response_model=dict)
async def check_account(name: str):
    """检测单个账号的登录态"""
    account_name = _validate_name(name)
    path = _account_path(account_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="账号不存在")
    result = await check_account_login_state(path)
    return {"result": result}