from __future__ import annotations

import glob
import os
import shutil
from urllib.parse import urlencode

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from src.config import RUN_HEADLESS
from src.parsers import _parse_search_results_json
from src.services.account_health_service import record_account_failure, record_account_success
from src.services.price_history_service import parse_price_value
from src.services.search_pagination import advance_search_page, is_search_results_response
from src.services.virtual_display import ensure_virtual_display
from src.utils import random_sleep


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


async def _launch_search_browser(playwright):
    executable_path = _find_browser_executable()
    # 这里的参数必须和 src/scraper.py 保持一致，并且**跟随 RUN_HEADLESS**。
    #
    # 真机实测（NAS 容器内，同一出口 IP、同一登录态，唯一变量是 headless）：
    #   headless=True  -> 页面返回「非法访问 为了保障您的体验，请使用正常浏览器访问闲鱼~」，
    #                     搜索接口 mtop.taobao.idlemtopsearch.pc.search 一次都不发起，
    #                     expect_response 干等 60 秒后抛超时。
    #   headless=False -> 搜索接口正常发起（用 Xvfb 提供虚拟显示）。
    # 注意：纯 HTTP 请求（无浏览器）拿同一个 URL 反而返回 200 正常页面，
    # 所以这不是 IP 被风控，而是无头浏览器指纹被识别。
    # 注意：这里**不再**调用 ensure_virtual_display()。显示必须在
    # async_playwright() 之前准备好，见 search_official_items()。
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-setuid-sandbox",
    ]
    if executable_path:
        return await playwright.chromium.launch(
            headless=RUN_HEADLESS,
            executable_path=executable_path,
            args=launch_args,
        )
    try:
        return await playwright.chromium.launch(headless=RUN_HEADLESS, args=launch_args)
    except Exception as exc:
        raise RuntimeError(
            "搜索浏览器未安装。请在容器内安装 Chromium，或设置 PLAYWRIGHT_EXECUTABLE_PATH。"
        ) from exc


def _normalize_item(item: dict) -> dict:
    return {
        "item_id": str(item.get("商品ID") or ""),
        "title": str(item.get("商品标题") or ""),
        "price": parse_price_value(item.get("当前售价")),
        "price_display": str(item.get("当前售价") or ""),
        "original_price": str(item.get("商品原价") or ""),
        "seller": str(item.get("卖家昵称") or ""),
        "region": str(item.get("发货地区") or ""),
        "publish_time": str(item.get("发布时间") or ""),
        "link": str(item.get("商品链接") or ""),
        "image_url": item.get("商品主图链接"),
        "tags": item.get("商品标签") or [],
    }


async def search_official_items(keyword: str, account_path: str, page_number: int, page_size: int) -> dict:
    # 账号文件缺失/失效时自动回退匿名搜索（闲鱼 PC 搜索允许游客模式）
    storage_state = account_path if (account_path and os.path.isfile(account_path)) else None
    anonymous = storage_state is None
    try:
        # 必须在 ``async_playwright()`` 之前准备显示：进入上下文管理器会立刻
        # 启动 Playwright 驱动进程，之后再设 DISPLAY 已经来不及。
        ensure_virtual_display()
        async with async_playwright() as playwright:
            browser = await _launch_search_browser(playwright)
            context = (
                await browser.new_context(storage_state=storage_state)
                if storage_state
                else await browser.new_context()
            )
            page = await context.new_page()

            # 反爬：先访问首页并停留，模拟真实用户
            try:
                await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60000)
                await random_sleep(1, 2)
            except Exception:
                pass

            search_url = f"https://www.goofish.com/search?{urlencode({'q': keyword})}"
            response = None
            last_error = None
            for attempt in range(2):
                try:
                    async with page.expect_response(is_search_results_response, timeout=60000) as response_info:
                        await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
                    response = await response_info.value
                    break
                except PlaywrightTimeoutError as exc:
                    last_error = exc
                    current_url = page.url
                    if "login" in current_url.lower() or "passport" in current_url.lower():
                        raise RuntimeError("账号登录态已失效，请到「账号管理」重新提取登录态") from exc
                    if attempt == 0:
                        await random_sleep(2, 4)
            if response is None:
                raise RuntimeError(f"等待闲鱼搜索响应超时: {last_error}")

            for current_page in range(1, page_number):
                advanced = await advance_search_page(page=page, page_num=current_page + 1)
                if not advanced.advanced or advanced.response is None:
                    break
                response = advanced.response
            payload = await response.json()
            parsed = await _parse_search_results_json(payload, f"official-search:{page_number}")
            await context.close()
            await browser.close()
        if not anonymous:
            record_account_success(storage_state)
        items = [_normalize_item(item) for item in parsed]
        return {
            "items": items[:page_size],
            "page": page_number,
            "page_size": page_size,
            "has_more": len(items) >= page_size,
            "account_path": account_path,
        }
    except Exception as exc:
        if not anonymous and storage_state:
            record_account_failure(storage_state, str(exc))
        raise RuntimeError(str(exc)) from exc