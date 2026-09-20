"""
账号登录态检测服务
主动验证闲鱼账号登录状态是否有效（不依赖任务运行）
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
from datetime import datetime

from playwright.async_api import async_playwright

from src.services.account_health_service import (
    STATUS_HEALTHY,
    STATUS_LOGIN_REQUIRED,
    STATUS_UNKNOWN,
    STATUS_VERIFICATION_REQUIRED,
    record_account_failure,
    record_account_success,
    list_account_health,
)
from src.utils import random_sleep

_LOGIN_PATH_MARKERS = ("login", "passport", "login.taobao")
_VERIFY_DIALOG_SELECTOR = "div.baxia-dialog-mask, div.J_MIDDLEWARE_FRAME_WIDGET"
_HOME_URL = "https://www.goofish.com/"


def _find_browser_executable() -> str | None:
    """Find a full Chromium binary when the Playwright headless shell is absent."""
    configured = os.getenv("PLAYWRIGHT_EXECUTABLE_PATH") or os.getenv("BROWSER_EXECUTABLE_PATH")
    candidates = [configured] if configured else []
    candidates.extend(
        [
            shutil.which("chromium"),
            shutil.which("chromium-browser"),
            shutil.which("google-chrome"),
            shutil.which("google-chrome-stable"),
        ]
    )
    browser_roots = [
        os.getenv("PLAYWRIGHT_BROWSERS_PATH"),
        "/ms-playwright",
        "/root/.cache/ms-playwright",
    ]
    browser_patterns = [
        "chromium-*/chrome-linux/chrome",
        "chromium-*/chrome-linux64/chrome",
        "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-linux/headless_shell",
    ]
    for root in browser_roots:
        if not root:
            continue
        for pattern in browser_patterns:
            candidates.extend(glob.glob(os.path.join(root, pattern)))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


async def _launch_browser(playwright):
    executable_path = _find_browser_executable()
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    if executable_path:
        return await playwright.chromium.launch(
            headless=True,
            executable_path=executable_path,
            args=launch_args,
        )
    return await playwright.chromium.launch(headless=True, args=launch_args)


def _precheck_storage_state(account_path: str) -> dict | None:
    """读本地 storage_state 做快速预检。

    返回 ``None`` 表示「预检通过，继续走浏览器校验」；返回 dict 表示
    「已可断定不可用，直接以此为结论」。

    设计要点：预检**只能否证、不能证成**。cookie 名字齐全且未过期并不意味着
    服务端接受这个登录态（cookie 可能在服务端已被吊销），所以通过预检时
    必须继续走完整校验；而不通过时则可以放心短路——``storage_state`` 里
    连关键 cookie 都没有，浏览器里必然也拿不到。

    这样能把「必然失败」的账号从一次数秒的浏览器启动 + 一次风控暴露中省下来。
    """
    try:
        with open(account_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        # 文件读不出/JSON 坏掉时不做判断，交给完整校验流程去报错（保持原行为）
        return None

    from src.services.session_hardening_service import (
        assess_cookie_freshness,
        check_required_cookies,
        validate_storage_state,
    )

    structure = validate_storage_state(payload)
    if not structure.get("valid"):
        return {
            "account_path": account_path,
            "status": STATUS_LOGIN_REQUIRED,
            "available": False,
            "detail": f"登录态文件无效：{structure.get('reason', '结构不符合 storage_state 格式')}",
            "precheck": True,
        }

    cookies = payload.get("cookies") or []
    required = check_required_cookies(cookies)
    if not required.get("ok"):
        missing = "、".join(required.get("missing") or [])
        return {
            "account_path": account_path,
            "status": STATUS_LOGIN_REQUIRED,
            "available": False,
            "detail": f"登录态缺少关键 cookie：{missing}",
            "precheck": True,
        }

    freshness = assess_cookie_freshness(cookies, now=datetime.now())
    if freshness.get("verdict") == "expired":
        return {
            "account_path": account_path,
            "status": STATUS_LOGIN_REQUIRED,
            "available": False,
            "detail": f"登录态 cookie 已过期：{freshness.get('reason', '')}",
            "precheck": True,
        }

    # 预检通过：必须继续走浏览器校验，不能据此判定健康
    return None


async def check_account_login_state(account_path: str) -> dict:
    """
    检测单个账号登录态。
    加载账号 storage_state 访问闲鱼首页，判断是否跳转登录页/触发验证码。
    检测结果同步写入账号健康记录。
    """
    if not os.path.isfile(account_path):
        return {
            "account_path": account_path,
            "status": "missing",
            "available": False,
            "detail": "账号文件不存在",
        }

    # 快速预检：先读本地 storage_state 判断关键 cookie 是否齐全/新鲜，
    # 若不通过就直接给出结论，省掉一次浏览器启动（每次约数秒 + 一次风控暴露）。
    #
    # 注意方向性：预检**只用于快速判失败**，不能用来判成功——cookie 名字齐全
    # 不代表服务端认可该登录态。预检通过时仍走完整浏览器校验。
    precheck = _precheck_storage_state(account_path)
    if precheck is not None:
        return precheck

    result = {
        "account_path": account_path,
        "status": STATUS_UNKNOWN,
        "available": True,
        "detail": "",
    }

    try:
        async with async_playwright() as playwright:
            browser = await _launch_browser(playwright)
            context = await browser.new_context(storage_state=account_path)
            page = await context.new_page()
            try:
                await page.goto(_HOME_URL, wait_until="domcontentloaded", timeout=60000)
                # 等待页面 JS 执行（登录跳转/验证码弹窗通常在数秒内出现）
                await asyncio.sleep(4)

                current_url = (page.url or "").lower()
                if any(marker in current_url for marker in _LOGIN_PATH_MARKERS):
                    result["status"] = STATUS_LOGIN_REQUIRED
                    result["detail"] = "登录态已失效（被重定向到登录页）"
                elif await page.locator(_VERIFY_DIALOG_SELECTOR).count() > 0:
                    result["status"] = STATUS_VERIFICATION_REQUIRED
                    result["detail"] = "触发闲鱼验证码（风控拦截）"
                else:
                    result["status"] = STATUS_HEALTHY
                    result["detail"] = "在线可用"
            finally:
                await context.close()
                await browser.close()
    except Exception as exc:
        result["status"] = STATUS_UNKNOWN
        result["detail"] = f"检测失败: {str(exc)[:400]}"
        result["available"] = False

    # 读取检测前的健康状态（用于判断状态变化，避免重复通知）
    previous_status = None
    try:
        prev_rows = list_account_health([account_path])
        if prev_rows:
            previous_status = prev_rows[0].get("status")
    except Exception:
        pass

    # 同步健康记录
    if result["status"] == STATUS_HEALTHY:
        record_account_success(account_path)
    elif result["status"] == STATUS_LOGIN_REQUIRED:
        record_account_failure(
            account_path,
            result["detail"],
            status=STATUS_LOGIN_REQUIRED,
            cooldown_seconds=0,
        )
    elif result["status"] == STATUS_VERIFICATION_REQUIRED:
        record_account_failure(
            account_path,
            result["detail"],
            status=STATUS_VERIFICATION_REQUIRED,
            cooldown_seconds=0,
        )
    # 账号掉线通知：状态变为阻塞且之前未阻塞时推送
    blocking = {STATUS_LOGIN_REQUIRED, STATUS_VERIFICATION_REQUIRED}
    if result["status"] in blocking and previous_status not in blocking:
        await _notify_account_offline(account_path, result)

    return result


async def check_all_accounts(state_dir: str = "state") -> list[dict]:
    """检测 state 目录下所有账号（串行执行，账号间随机间隔降低风控风险）。"""
    if not os.path.isdir(state_dir):
        return []
    files = sorted(f for f in os.listdir(state_dir) if f.endswith(".json"))
    results: list[dict] = []
    for filename in files:
        path = os.path.join(state_dir, filename)
        results.append(await check_account_login_state(path))
        await random_sleep(2, 5)
    return results
async def _notify_account_offline(account_path: str, result: dict) -> None:
    """账号登录态失效时发送 Bark 通知（只读安全操作）"""
    try:
        from src.services.notification_service import build_notification_service

        name = os.path.basename(account_path)
        if name.endswith(".json"):
            name = name[:-5]
        service = build_notification_service()
        payload = {
            "商品标题": f"[账号异常] {name}",
            "当前售价": "N/A",
            "商品链接": "#",
            "通知标题": "⚠️ 闲鱼账号掉线",
        }
        reason = f"账号 {name} 状态异常: {result.get('detail') or result.get('status')}。请在「账号管理」重新提取登录态。"
        await service.send_notification(payload, reason)
        print(f"[账号检测] 已发送掉线通知: {name}")
    except Exception as exc:
        print(f"[账号检测] 发送掉线通知失败: {exc}")