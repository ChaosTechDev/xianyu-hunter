from __future__ import annotations

import os
from datetime import datetime, timedelta

import asyncio
from playwright.async_api import async_playwright

from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.rotation import load_state_files
from src.services.account_health_service import (
    record_account_failure,
    record_account_success,
    release_account,
    reserve_account,
)
from src.services.session_guard import SessionGuard


DEFAULT_TEMPLATE = "您好，这件商品还在吗？当前价格可以直接购买吗？"


def _default_template() -> str:
    return os.getenv("AUTO_CONSULT_TEMPLATE", DEFAULT_TEMPLATE) or DEFAULT_TEMPLATE


def _cooldown_hours() -> int:
    try:
        return max(1, int(os.getenv("AUTO_CONSULT_COOLDOWN_HOURS", "24")))
    except ValueError:
        return 24


def list_consultation_logs(watch_id: int | None = None, limit: int = 100) -> list[dict]:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        if watch_id is None:
            rows = conn.execute(
                "SELECT * FROM consultation_logs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM consultation_logs WHERE watch_item_id = ? ORDER BY created_at DESC LIMIT ?",
                (watch_id, limit),
            ).fetchall()
    return [dict(row) for row in rows]


def _render(template: str, watch: dict) -> str:
    return (template or _default_template()).format(
        title=watch.get("title") or "",
        price=watch.get("last_price") or "",
        alert_price=watch.get("alert_price") or "",
    ).strip()


def _recently_consulted(watch: dict) -> bool:
    value = watch.get("last_consulted_at")
    if not value:
        return False
    try:
        return datetime.fromisoformat(value) > datetime.now() - timedelta(hours=_cooldown_hours())
    except ValueError:
        return False


def _write_log(watch_id: int, message: str, status: str, account: str | None, error: str | None) -> None:
    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO consultation_logs(
                watch_item_id, event_type, account_path, message, status, error, created_at
            ) VALUES (?, 'low_price', ?, ?, ?, ?, ?)
            """,
            (watch_id, account, message, status, error, datetime.now().isoformat()),
        )
        conn.commit()


async def send_consultation(watch: dict, *, force: bool = False) -> dict:
    message = _render(watch.get("consult_template") or _default_template(), watch)
    if not force and _recently_consulted(watch):
        return {"status": "skipped", "reason": f"{_cooldown_hours()} 小时内已咨询"}

    state_dir = os.getenv("ACCOUNT_STATE_DIR", "state")
    owner = f"consult:{watch['id']}:{datetime.now().timestamp()}"
    account = reserve_account(load_state_files(state_dir), owner=owner, lease_seconds=300)
    if not account:
        _write_log(watch["id"], message, "failed", None, "没有可用账号")
        return {"status": "failed", "error": "没有可用账号"}

    # 会话守卫：同一 (账号, 会话) 处于暂停/订单锁/冷却期时不重复触达。
    # 放在**拿到具体账号之后**——键是 (账号, 会话) 复合键，没有账号就没有键。
    guard = SessionGuard()
    chat_id = str(watch.get("id") or "")
    skip, reason = guard.should_skip_auto_reply(account=account, chat_id=chat_id)
    if skip:
        release_account(owner)
        _write_log(watch["id"], message, "skipped", account, reason)
        return {"status": "skipped", "account_path": account, "reason": reason}
    if guard.in_cooldown(account=account, chat_id=chat_id):
        release_account(owner)
        remaining = guard.cooldown_remaining(account=account, chat_id=chat_id)
        _write_log(watch["id"], message, "skipped", account, f"冷却中（剩余 {remaining} 秒）")
        return {"status": "skipped", "account_path": account, "reason": "会话冷却中"}

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=account)
            page = await context.new_page()

            # 反爬：先访问首页并停留（模拟真实用户，降低风控拦截概率）
            try:
                await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
            except Exception:
                pass

            await page.goto(watch["link"], wait_until="domcontentloaded", timeout=60000)
            contact = page.get_by_text("聊一聊", exact=True).or_(page.get_by_text("我想要", exact=True)).first
            await contact.click(timeout=15000)
            editor = page.locator("textarea, [contenteditable='true']").last
            await editor.fill(message, timeout=15000)
            send_btn = page.get_by_text("发送", exact=True).last
            await send_btn.click(timeout=15000)

            # 验证发送成功：等待"发送"按钮从页面消失
            sent = False
            try:
                await page.wait_for_selector(
                    'button:has-text("发送")',
                    state="detached",
                    timeout=5000,
                )
                sent = True
            except Exception:
                pass

            await context.close()
            await browser.close()
        now = datetime.now().isoformat()
        status = "sent" if sent else "uncertain"
        with sqlite_connection() as conn:
            conn.execute(
                "UPDATE watch_items SET last_consulted_at = ?, updated_at = ? WHERE id = ?",
                (now, now, watch["id"]),
            )
            conn.commit()
        record_account_success(account)
        # 记冷却：失败与「不确定」也记，因为消息可能已经发出去了。
        # 只记成功会让「发送按钮没消失」的用例下次立刻重发，重复打扰卖家。
        guard.mark_contacted(account=account, chat_id=chat_id)
        _write_log(watch["id"], message, status, account,
                   None if sent else "发送按钮未消失，可能未成功")
        return {"status": status, "account_path": account, "message": message}
    except Exception as exc:
        error = str(exc)
        record_account_failure(account, error)
        _write_log(watch["id"], message, "failed", account, error)
        return {"status": "failed", "account_path": account, "error": error}
    finally:
        release_account(owner)

# 任务级自动咨询：进程内已咨询商品链接去重（重启后重置）
_consulted_links: set = set()


async def maybe_send_task_consultation(product_data: dict) -> dict:
    """任务级自动咨询入口：推荐商品自动私聊卖家（进程内去重 + 落库冷却 + 会话守卫）。"""
    info = product_data.get("商品信息", {})
    link = product_data.get("商品链接") or info.get("商品链接", "")
    title = info.get("商品标题", "")
    price = info.get("当前售价", "")
    if not link:
        return {"status": "skipped", "reason": "无商品链接"}
    key = str(link).split("&")[0]
    if key in _consulted_links:
        return {"status": "skipped", "reason": "已咨询过"}
    _consulted_links.add(key)
    try:
        result = await send_task_consultation(link, title, price)
        print(f"[自动咨询] {title} -> {result.get('status')}")
        return result
    except Exception as exc:
        print(f"[自动咨询] {title} 失败: {exc}")
        return {"status": "failed", "error": str(exc)}


async def send_task_consultation(
    link: str,
    title: str,
    price: str,
    template: str | None = None,
) -> dict:
    """向商品卖家发送咨询消息（任务级，无 watch_item 记录）。"""
    message = (template or _default_template()).format(
        title=title or "",
        price=price or "",
        alert_price="",
    ).strip()

    state_dir = os.getenv("ACCOUNT_STATE_DIR", "state")
    owner = f"task_consult:{datetime.now().timestamp()}"
    account = reserve_account(load_state_files(state_dir), owner=owner, lease_seconds=300)
    if not account:
        return {"status": "failed", "error": "没有可用账号"}

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=account)
            page = await context.new_page()

            # 反爬：先访问首页并停留
            try:
                await page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(2)
            except Exception:
                pass

            await page.goto(link, wait_until="domcontentloaded", timeout=60000)
            contact = page.get_by_text("聊一聊", exact=True).or_(page.get_by_text("我想要", exact=True)).first
            await contact.click(timeout=15000)
            editor = page.locator("textarea, [contenteditable='true']").last
            await editor.fill(message, timeout=15000)
            send_btn = page.get_by_text("发送", exact=True).last
            await send_btn.click(timeout=15000)

            sent = False
            try:
                await page.wait_for_selector('button:has-text("发送")', state="detached", timeout=5000)
                sent = True
            except Exception:
                pass

            await context.close()
            await browser.close()
        record_account_success(account)
        return {"status": "sent" if sent else "uncertain", "account_path": account}
    except Exception as exc:
        record_account_failure(account, str(exc))
        return {"status": "failed", "account_path": account, "error": str(exc)}
    finally:
        release_account(owner)