"""关注商品、价格事件和趋势服务。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Iterable

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.external.ai_client import AIClient
from src.services.notification_service import build_notification_service
from src.services.price_history_service import build_price_history_insights, parse_price_value
from src.services.consultation_service import send_consultation
from src.services.watch_state import (
    REASON_DELETED,
    REASON_DELISTED,
    REASON_SOLD,
    detect_reduce_price_delta,
    evaluate_death_signal,
    resolve_notify_flag,
    resolve_sticky_death,
    should_emit_edge,
)


DELISTED_MISSING_RUNS = 3
EVENT_LABELS = {
    "price_drop": "价格下降",
    "low_price": "低价提醒",
    "delisted": "商品下架",
    "relisted": "重新上架",
    # 售出与下架语义不同：下架是卖家主动撤下，售出是买家买走。
    # 对捡漏用户来说「被别人买走了」信息量更大（手慢了的信号）。
    "sold_out": "商品已售出",
    "reduce_price": "收藏后降价",
}


def _col(row, name: str, default=None):
    """安全读取行字段。

    迁移新增的列在旧库中可能不存在（行对象无该键），这里统一兜底，
    避免为了读取一个新字段而在所有调用点写防御代码。
    """
    try:
        keys = row.keys()
    except AttributeError:
        return default
    if name not in keys:
        return default
    value = row[name]
    return default if value is None else value


def _default_consult_template() -> str:
    return os.getenv(
        "AUTO_CONSULT_TEMPLATE",
        "您好，这件商品还在吗？当前价格可以直接购买吗？",
    ) or "您好，这件商品还在吗？当前价格可以直接购买吗？"


def _default_consult_enabled() -> bool:
    return str(os.getenv("AUTO_CONSULT_DEFAULT_ENABLED", "false")).strip().lower() in {
        "1", "true", "yes", "on"
    }


def _now() -> str:
    return datetime.now().isoformat()


def _row_to_watch(row) -> dict:
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "result_filename": row["result_filename"],
        "keyword": row["keyword"],
        "task_name": row["task_name"],
        "title": row["title"],
        "link": row["link"],
        "image_url": row["image_url"],
        "alert_price": row["alert_price"],
        "enabled": bool(row["enabled"]),
        "last_price": row["last_price"],
        "last_seen_at": row["last_seen_at"],
        "missing_runs": row["missing_runs"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "refresh_interval_minutes": row["refresh_interval_minutes"],
        "last_refresh_at": row["last_refresh_at"],
        "next_refresh_at": row["next_refresh_at"],
        "notify_price_drop": bool(row["notify_price_drop"]),
        "notify_low_price": bool(row["notify_low_price"]),
        "notify_delisted": bool(row["notify_delisted"]),
        "notify_relisted": bool(row["notify_relisted"]),
        "consult_enabled": bool(row["consult_enabled"]),
        "consult_template": row["consult_template"] or _default_consult_template(),
        "consult_account_strategy": row["consult_account_strategy"] or "pool",
        "last_consulted_at": row["last_consulted_at"],
        # --- 售罄检测（粘性）---
        "dead": bool(_col(row, "dead", 0)),
        "dead_reason": _col(row, "dead_reason"),
        "dead_since": _col(row, "dead_since"),
        # --- 通知分类型开关 ---
        "notify_on_sold": bool(_col(row, "notify_on_sold", 1)),
        "notify_on_favorite": bool(_col(row, "notify_on_favorite", 1)),
        "notify_on_login": bool(_col(row, "notify_on_login", 1)),
        # --- 静音延期 ---
        "muted_until": _col(row, "muted_until"),
        # --- 闲鱼原生降价信号 ---
        "prev_reduce_price": int(_col(row, "prev_reduce_price", 0) or 0),
    }


def _row_to_event(row) -> dict:
    try:
        notification_results = json.loads(row["notification_results_json"] or "{}")
    except json.JSONDecodeError:
        notification_results = {}
    return {
        "id": row["id"],
        "watch_item_id": row["watch_item_id"],
        "item_id": row["item_id"],
        "title": row["title"],
        "link": row["link"],
        "event_type": row["event_type"],
        "event_label": EVENT_LABELS.get(row["event_type"], row["event_type"]),
        "price": row["price"],
        "previous_price": row["previous_price"],
        "detail": row["detail"],
        "is_read": bool(row["is_read"]),
        "notified": bool(row["notified"]),
        "notification_results": notification_results,
        "created_at": row["created_at"],
    }


def list_watch_items(*, include_disabled: bool = True) -> list[dict]:
    bootstrap_sqlite_storage()
    where = "" if include_disabled else "WHERE enabled = 1"
    with sqlite_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM watch_items {where} ORDER BY updated_at DESC, id DESC"
        ).fetchall()
    return [_row_to_watch(row) for row in rows]


def get_watch_item(watch_id: int) -> dict | None:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        row = conn.execute("SELECT * FROM watch_items WHERE id = ?", (watch_id,)).fetchone()
    return _row_to_watch(row) if row else None


async def add_watch_item(payload: dict[str, Any]) -> dict:
    item_id = str(payload.get("item_id") or "").strip()
    title = str(payload.get("title") or "").strip()
    link = str(payload.get("link") or "").strip()
    if not item_id or not title or not link:
        raise ValueError("商品 ID、标题和链接不能为空")
    alert_price = parse_price_value(payload.get("alert_price"))
    last_price = parse_price_value(payload.get("last_price"))
    timestamp = _now()
    refresh_interval = payload.get("refresh_interval_minutes")
    refresh_interval = int(refresh_interval) if refresh_interval not in (None, "") else None
    next_refresh_at = (
        (datetime.now() + timedelta(minutes=refresh_interval)).isoformat()
        if refresh_interval else None
    )
    consult_enabled = payload.get("consult_enabled")
    if consult_enabled is None:
        consult_enabled = _default_consult_enabled()
    consult_template = str(payload.get("consult_template") or _default_consult_template())
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM watch_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO watch_items (
                item_id, result_filename, keyword, task_name, title, link,
                image_url, alert_price, enabled, last_price, last_seen_at,
                missing_runs, status, created_at, updated_at,
                refresh_interval_minutes, next_refresh_at,
                notify_price_drop, notify_low_price, notify_delisted, notify_relisted,
                consult_enabled, consult_template, consult_account_strategy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 0, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                result_filename = excluded.result_filename,
                keyword = excluded.keyword,
                task_name = excluded.task_name,
                title = excluded.title,
                link = excluded.link,
                image_url = COALESCE(excluded.image_url, watch_items.image_url),
                alert_price = excluded.alert_price,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (
                item_id,
                str(payload.get("result_filename") or ""),
                str(payload.get("keyword") or ""),
                str(payload.get("task_name") or ""),
                title,
                link,
                str(payload.get("image_url") or "") or None,
                alert_price,
                last_price,
                timestamp if last_price is not None else None,
                timestamp,
                timestamp,
                refresh_interval,
                next_refresh_at,
                int(bool(payload.get("notify_price_drop", True))),
                int(bool(payload.get("notify_low_price", True))),
                int(bool(payload.get("notify_delisted", True))),
                int(bool(payload.get("notify_relisted", True))),
                int(bool(consult_enabled)),
                consult_template,
                str(payload.get("consult_account_strategy") or "pool"),
            ),
        )
        row = conn.execute("SELECT * FROM watch_items WHERE item_id = ?", (item_id,)).fetchone()
        conn.commit()
    watch = _row_to_watch(row)
    if existing is None and alert_price is not None and last_price is not None and last_price <= alert_price:
        event_id = _insert_event(
            watch_id=watch["id"],
            event_key=f"low_price:{watch['id']}:watch:{timestamp}",
            event_type="low_price",
            price=last_price,
            previous_price=None,
            detail=f"关注时价格 ¥{last_price:.2f} 已低于提醒价 ¥{alert_price:.2f}",
            created_at=timestamp,
        )
        if event_id:
            await _dispatch_event(event_id)
    return get_watch_item(watch["id"]) or watch


def update_watch_item(watch_id: int, changes: dict[str, Any]) -> dict | None:
    allowed: dict[str, Any] = {}
    if "alert_price" in changes:
        raw = changes.get("alert_price")
        allowed["alert_price"] = None if raw in (None, "") else parse_price_value(raw)
        if raw not in (None, "") and allowed["alert_price"] is None:
            raise ValueError("提醒价格格式不正确")
    if "enabled" in changes:
        allowed["enabled"] = 1 if bool(changes["enabled"]) else 0
    if "refresh_interval_minutes" in changes:
        raw_interval = changes.get("refresh_interval_minutes")
        interval = int(raw_interval) if raw_interval not in (None, "") else None
        if interval is not None and interval < 1:
            raise ValueError("刷新周期必须大于 0 分钟")
        allowed["refresh_interval_minutes"] = interval
        allowed["next_refresh_at"] = (
            (datetime.now() + timedelta(minutes=interval)).isoformat() if interval else None
        )
    for key in ("notify_price_drop", "notify_low_price", "notify_delisted", "notify_relisted", "consult_enabled"):
        if key in changes:
            allowed[key] = 1 if bool(changes[key]) else 0
    if "consult_template" in changes:
        template = str(changes.get("consult_template") or "").strip()
        if changes.get("consult_enabled") and not template:
            raise ValueError("启用自动咨询时话术不能为空")
        allowed["consult_template"] = template or None
    if "consult_account_strategy" in changes:
        allowed["consult_account_strategy"] = "pool"
    if not allowed:
        return get_watch_item(watch_id)
    allowed["updated_at"] = _now()
    assignments = ", ".join(f"{key} = ?" for key in allowed)
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute(
            f"UPDATE watch_items SET {assignments} WHERE id = ?",
            (*allowed.values(), watch_id),
        )
        conn.commit()
    return get_watch_item(watch_id) if cursor.rowcount else None


def delete_watch_item(watch_id: int) -> bool:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute("DELETE FROM watch_items WHERE id = ?", (watch_id,))
        conn.commit()
    return bool(cursor.rowcount)


def _insert_event(
    *,
    watch_id: int,
    event_key: str,
    event_type: str,
    price: float | None,
    previous_price: float | None,
    detail: str,
    created_at: str,
) -> int | None:
    with sqlite_connection() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO watch_events (
                watch_item_id, event_key, event_type, price, previous_price,
                detail, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (watch_id, event_key, event_type, price, previous_price, detail, created_at),
        )
        conn.commit()
        return int(cursor.lastrowid) if cursor.rowcount else None


async def _dispatch_event(event_id: int) -> None:
    with sqlite_connection() as conn:
        row = conn.execute(
            """
            SELECT e.*, w.item_id, w.title, w.link, w.image_url
            FROM watch_events e JOIN watch_items w ON w.id = e.watch_item_id
            WHERE e.id = ?
            """,
            (event_id,),
        ).fetchone()
    if row is None:
        return
    watch = get_watch_item(int(row["watch_item_id"]))
    if watch is None:
        return
    label = EVENT_LABELS.get(row["event_type"], "关注商品变动")
    product_data = {
        "商品标题": row["title"],
        "当前售价": f"¥{row['price']:.2f}" if row["price"] is not None else "暂无",
        "商品链接": row["link"],
        "商品主图链接": row["image_url"],
        "通知标题": f"{label} · {row['title'][:24]}",
    }
    results = {}
    # 通知闸门：分类型开关 + 静音延期。
    # 注意「静音但仍留档」——事件已在 _insert_event 入库，静音只是不推送，
    # 历史记录不丢，且因为事件已记录，下一轮不会重复扫描/重复发同一事件。
    if resolve_notify_flag(
        event_type=row["event_type"],
        watch=watch,
        muted_until=watch.get("muted_until"),
    ):
        try:
            results = await build_notification_service().send_notification(product_data, row["detail"])
        except Exception as exc:
            results = {
                "internal": {
                    "channel": "internal",
                    "label": "通知服务",
                    "success": False,
                    "message": str(exc),
                }
            }
    else:
        results = {
            "internal": {
                "channel": "internal",
                "label": "通知服务",
                "success": False,
                "message": "已按通知设置静音（事件仍已记录）",
            }
        }
    if row["event_type"] == "low_price" and watch.get("consult_enabled"):
        consultation = await send_consultation(watch)
        results["consultation"] = {
            "channel": "consultation",
            "label": "自动咨询",
            "success": consultation.get("status") in {"sent", "skipped"},
            "message": consultation.get("error") or consultation.get("reason") or "发送成功",
        }
    notified = any(bool(result.get("success")) for result in results.values())
    with sqlite_connection() as conn:
        conn.execute(
            """
            UPDATE watch_events
            SET notified = ?, notification_results_json = ?
            WHERE id = ?
            """,
            (1 if notified else 0, json.dumps(results, ensure_ascii=False), event_id),
        )
        conn.commit()


async def process_watch_snapshots(records: Iterable[dict]) -> list[int]:
    """处理一批真实采集快照，并发送新产生的关注事件。"""
    bootstrap_sqlite_storage()
    event_ids: list[int] = []
    for record in records:
        item_id = str(record.get("item_id") or "").strip()
        price = parse_price_value(record.get("price"))
        if not item_id or price is None:
            continue
        with sqlite_connection() as conn:
            row = conn.execute(
                "SELECT * FROM watch_items WHERE item_id = ? AND enabled = 1",
                (item_id,),
            ).fetchone()
        if row is None:
            continue
        watch = _row_to_watch(row)
        timestamp = str(record.get("snapshot_time") or _now())
        interval_minutes = watch.get("refresh_interval_minutes")
        if not interval_minutes and watch.get("task_name"):
            with sqlite_connection() as conn:
                task_row = conn.execute(
                    "SELECT collection_interval_minutes FROM tasks WHERE task_name = ? LIMIT 1",
                    (watch["task_name"],),
                ).fetchone()
            interval_minutes = task_row["collection_interval_minutes"] if task_row else None
        try:
            refresh_base = datetime.fromisoformat(timestamp)
        except ValueError:
            refresh_base = datetime.now()
        next_refresh_at = (
            (refresh_base + timedelta(minutes=int(interval_minutes))).isoformat()
            if interval_minutes else None
        )
        run_id = str(record.get("run_id") or timestamp)
        previous_price = watch["last_price"]
        was_delisted = watch["status"] == "delisted"
        # 粘性死亡状态（可能比 status 更早/更准地反映「已售出」）
        was_dead = bool(watch.get("dead"))
        # 闲鱼原生的累计降价额。它记录的是「卖家相对于挂牌价降了多少」，
        # 因此能捕获「我们开始监控之前」就已发生的降价 —— 这是跨次比价
        # 完全看不到的部分，而捡漏场景里首次发现时往往已降过一轮。
        prev_reduce = int(watch.get("prev_reduce_price") or 0)
        cur_reduce = record.get("reduce_price")
        with sqlite_connection() as conn:
            conn.execute(
                """
                UPDATE watch_items SET title = ?, link = ?, last_price = ?,
                    last_seen_at = ?, missing_runs = 0, status = 'active', updated_at = ?,
                    last_refresh_at = ?, next_refresh_at = ?
                WHERE id = ?
                """,
                (
                    str(record.get("title") or watch["title"]),
                    str(record.get("link") or watch["link"]),
                    price,
                    timestamp,
                    timestamp,
                    timestamp,
                    next_refresh_at,
                    watch["id"],
                ),
            )
            conn.commit()
        # 采集到了 = 明确的存活证据。若此前被判定死亡，这是一次「复活」，
        # 走边沿触发发一次通知。粘性状态下只有这种情况能翻案。
        if was_dead or was_delisted:
            revived = resolve_sticky_death(
                was_dead=was_dead,
                decision=evaluate_death_signal(alive_signal=True, missing_runs=0),
            )
            if should_emit_edge(was_dead=was_dead, dead=revived.dead):
                with sqlite_connection() as conn:
                    conn.execute(
                        """
                        UPDATE watch_items
                        SET dead = 0, dead_reason = NULL, dead_since = NULL
                        WHERE id = ?
                        """,
                        (watch["id"],),
                    )
                    conn.commit()
                event_id = _insert_event(
                    watch_id=watch["id"], event_key=f"relisted:{watch['id']}:{run_id}",
                    event_type="relisted", price=price, previous_price=previous_price,
                    detail=f"商品重新出现在采集结果中，当前价 ¥{price:.2f}", created_at=timestamp,
                )
                if event_id:
                    event_ids.append(event_id)
        if previous_price is not None and price < float(previous_price):
            event_id = _insert_event(
                watch_id=watch["id"], event_key=f"price_drop:{watch['id']}:{run_id}",
                event_type="price_drop", price=price, previous_price=float(previous_price),
                detail=f"价格从 ¥{float(previous_price):.2f} 降至 ¥{price:.2f}", created_at=timestamp,
            )
            if event_id:
                event_ids.append(event_id)
        # 闲鱼原生降价信号（增量 > 0 才发，避免重复通知同一笔降价）
        if cur_reduce is not None:
            try:
                cur_reduce_int = int(cur_reduce)
            except (TypeError, ValueError):
                cur_reduce_int = 0
            if cur_reduce_int:
                delta = detect_reduce_price_delta(
                    current_reduce=cur_reduce_int, previous_reduce=prev_reduce
                )
                if delta > 0:
                    event_id = _insert_event(
                        watch_id=watch["id"],
                        event_key=f"reduce_price:{watch['id']}:{run_id}",
                        event_type="reduce_price",
                        price=price,
                        previous_price=previous_price,
                        detail=(
                            f"卖家已降价 ¥{delta / 100:.2f}"
                            f"（累计降价 ¥{cur_reduce_int / 100:.2f}）"
                        ),
                        created_at=timestamp,
                    )
                    if event_id:
                        event_ids.append(event_id)
                if cur_reduce_int != prev_reduce:
                    with sqlite_connection() as conn:
                        conn.execute(
                            "UPDATE watch_items SET prev_reduce_price = ? WHERE id = ?",
                            (cur_reduce_int, watch["id"]),
                        )
                        conn.commit()
        alert_price = watch["alert_price"]
        crossed_threshold = (
            alert_price is not None
            and price <= float(alert_price)
            and (previous_price is None or float(previous_price) > float(alert_price))
        )
        if crossed_threshold:
            event_id = _insert_event(
                watch_id=watch["id"], event_key=f"low_price:{watch['id']}:{run_id}",
                event_type="low_price", price=price, previous_price=previous_price,
                detail=f"当前价 ¥{price:.2f} 已达到提醒价 ¥{float(alert_price):.2f}", created_at=timestamp,
            )
            if event_id:
                event_ids.append(event_id)
    for event_id in event_ids:
        await _dispatch_event(event_id)
    return event_ids


async def finalize_watch_scan(
    *,
    task_name: str,
    seen_item_ids: set[str],
    run_id: str,
    liveness_probe: "Callable[[str], Awaitable[Any]] | None" = None,
) -> list[int]:
    """完整扫描成功后更新缺失次数；被动缺失先探活，确认死亡才判定下架。

    ``liveness_probe`` 是可注入的探活回调：接收 ``item_id``，返回
    :class:`src.services.xy_protocol.status.ItemStatus`（或任何带 ``alive`` 属性的对象）。

    为什么要探活：单单「这一轮没被采到」是**弱信号**——闲鱼翻页不稳定、风控会
    吞掉部分结果，仅凭缺失判死会误杀在售商品。因此在对某商品累计缺失、准备判死
    之前，先调一次详情接口拿权威 ``ret`` 码：

    - 探活返回 ``alive=True`` → 立即清零缺失计数，不判死（商品还在，只是这轮没采到）
    - 探活返回 ``alive=False``（明确死亡信号）→ 直接判死，不必等缺失阈值
    - 探活失败/未注入/风控（``None``）→ 回退到原有的连续缺失阈值逻辑

    探活回调为 ``None`` 时行为与改造前**完全一致**，因此不会破坏既有调用方。
    """
    if not seen_item_ids:
        return []
    bootstrap_sqlite_storage()
    timestamp = _now()
    event_ids: list[int] = []
    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM watch_items WHERE task_name = ? AND enabled = 1",
            (task_name,),
        ).fetchall()
    for row in rows:
        watch = _row_to_watch(row)
        if watch["item_id"] in seen_item_ids:
            continue
        missing_runs = int(watch["missing_runs"] or 0) + 1

        # 被动缺失是弱信号：判死前先做一次权威探活
        alive_signal: bool | None = None
        explicit_reason: str | None = None
        if liveness_probe is not None:
            try:
                probe = await liveness_probe(watch["item_id"])
            except Exception as exc:  # 探活本身失败绝不能影响主流程
                print(f"[关注列表] 探活失败 {watch['item_id']}: {exc}")
                probe = None
            if probe is not None:
                alive = getattr(probe, "alive", None)
                if alive is True:
                    alive_signal = True
                elif alive is False:
                    alive_signal = False
                    # 原因标签由探活结果推导：删除 vs 下架
                    reason = getattr(probe, "reason", "") or ""
                    explicit_reason = REASON_DELETED if "删除" in reason else REASON_DELISTED

        # 用状态机统一判定，并保留粘性：已判死的商品不会因为「这轮又没采到」
        # 而反复产生事件（否则每个采集周期都会重复推进 missing_runs 但状态不变）。
        decision = evaluate_death_signal(
            alive_signal=alive_signal,
            missing_runs=missing_runs,
            delisted_missing_runs=DELISTED_MISSING_RUNS,
            explicit_reason=explicit_reason,
        )
        decision = resolve_sticky_death(was_dead=bool(watch.get("dead")), decision=decision)
        new_status = "delisted" if decision.dead else watch["status"]
        # 确认存活时清零缺失计数：否则计数会一直累积，把「偶尔漏采」拖到阈值
        new_missing_runs = 0 if alive_signal is True else missing_runs
        with sqlite_connection() as conn:
            conn.execute(
                """
                UPDATE watch_items
                SET missing_runs = ?, status = ?, dead = ?, dead_reason = ?,
                    dead_since = COALESCE(dead_since, ?), updated_at = ?
                WHERE id = ?
                """,
                (
                    new_missing_runs,
                    new_status,
                    1 if decision.dead else 0,
                    decision.reason,
                    timestamp if decision.dead else None,
                    timestamp,
                    watch["id"],
                ),
            )
            conn.commit()
        # 边沿触发：只在「活 -> 死」跃迁瞬间发一次，持续死亡不再重复通知
        was_dead = bool(watch.get("dead"))
        if decision.dead and should_emit_edge(was_dead=was_dead, dead=True):
            # 售出优先用专用事件类型，语义比「下架」更准确
            event_type = "sold_out" if decision.reason == REASON_SOLD else "delisted"
            event_id = _insert_event(
                watch_id=watch["id"],
                event_key=f"{event_type}:{watch['id']}:{watch['last_seen_at'] or run_id}",
                event_type=event_type,
                price=watch["last_price"],
                previous_price=watch["last_price"],
                detail=decision.detail,
                created_at=timestamp,
            )
            if event_id:
                event_ids.append(event_id)
    for event_id in event_ids:
        await _dispatch_event(event_id)
    return event_ids


def list_watch_events(*, unread_only: bool = False, limit: int = 100) -> list[dict]:
    bootstrap_sqlite_storage()
    where = "WHERE e.is_read = 0" if unread_only else ""
    with sqlite_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT e.*, w.item_id, w.title, w.link
            FROM watch_events e JOIN watch_items w ON w.id = e.watch_item_id
            {where} ORDER BY e.created_at DESC, e.id DESC LIMIT ?
            """,
            (max(1, min(limit, 500)),),
        ).fetchall()
    return [_row_to_event(row) for row in rows]


def mark_event_read(event_id: int) -> bool:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute("UPDATE watch_events SET is_read = 1 WHERE id = ?", (event_id,))
        conn.commit()
    return bool(cursor.rowcount)


def mark_all_events_read() -> int:
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        cursor = conn.execute("UPDATE watch_events SET is_read = 1 WHERE is_read = 0")
        conn.commit()
    return int(cursor.rowcount or 0)


def get_watch_stats() -> dict:
    bootstrap_sqlite_storage()
    today = datetime.now().date().isoformat()
    with sqlite_connection() as conn:
        watch = conn.execute(
            """SELECT COUNT(*) total,
                      SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) enabled,
                      SUM(CASE WHEN status = 'delisted' THEN 1 ELSE 0 END) delisted
               FROM watch_items"""
        ).fetchone()
        events = conn.execute(
            """SELECT SUM(CASE WHEN is_read = 0 THEN 1 ELSE 0 END) unread,
                      SUM(CASE WHEN event_type = 'low_price' AND substr(created_at, 1, 10) = ? THEN 1 ELSE 0 END) low_price_today
               FROM watch_events""",
            (today,),
        ).fetchone()
    return {
        "total": int(watch["total"] or 0),
        "enabled": int(watch["enabled"] or 0),
        "delisted": int(watch["delisted"] or 0),
        "unread_events": int(events["unread"] or 0),
        "low_price_today": int(events["low_price_today"] or 0),
    }


def get_item_trend(watch_id: int) -> dict | None:
    watch = get_watch_item(watch_id)
    if watch is None:
        return None
    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        rows = conn.execute(
            """SELECT snapshot_time, snapshot_day, price
               FROM price_snapshots WHERE item_id = ?
               ORDER BY snapshot_time ASC, id ASC""",
            (watch["item_id"],),
        ).fetchall()
    points = [
        {"time": row["snapshot_time"], "day": row["snapshot_day"], "price": row["price"]}
        for row in rows
    ]
    prices = [float(point["price"]) for point in points]
    from statistics import median
    return {
        "scope": "item",
        "label": watch["title"],
        "watch_item": watch,
        "summary": {
            "current_price": watch["last_price"],
            "min_price": min(prices) if prices else watch["last_price"],
            "max_price": max(prices) if prices else watch["last_price"],
            "avg_price": round(sum(prices) / len(prices), 2) if prices else None,
            "median_price": round(float(median(prices)), 2) if prices else None,
            "observation_count": len(points),
            "latest_snapshot_at": points[-1]["time"] if points else None,
            "is_sparse": len(points) < 2,
        },
        "points": points,
    }


def get_category_trend(keyword: str) -> dict:
    return {
        "scope": "category",
        "label": keyword,
        **build_price_history_insights(keyword),
    }


async def generate_watch_ai_summary(watch_id: int) -> dict:
    """基于已落库的真实价格和事件生成可选 AI 行情解读。"""
    trend = get_item_trend(watch_id)
    if trend is None:
        raise ValueError("关注商品不存在")
    events = [
        event for event in list_watch_events(limit=100)
        if event["watch_item_id"] == watch_id
    ][:10]
    context = {
        "商品": trend["watch_item"]["title"],
        "分类": trend["watch_item"]["keyword"],
        "提醒价": trend["watch_item"]["alert_price"],
        "状态": trend["watch_item"]["status"],
        "价格统计": trend["summary"],
        "最近价格": trend["points"][-20:],
        "最近事件": [
            {
                "类型": event["event_label"],
                "价格": event["price"],
                "说明": event["detail"],
                "时间": event["created_at"],
            }
            for event in events
        ],
    }
    prompt = (
        "你是二手商品价格分析助手。只能依据给定的真实采集数据，不要编造平台数据。"
        "请输出 JSON，字段固定为 summary（两句话摘要）、outlook（价格走向）、"
        "signals（字符串数组，最多3条）、risks（字符串数组，最多3条）。\n数据："
        + json.dumps(context, ensure_ascii=False)
    )
    client = AIClient()
    try:
        if not client.is_available():
            raise RuntimeError("AI 尚未配置，请先在系统设置中配置并测试模型")
        result = await client.generate_json(prompt)
    finally:
        await client.close()
    if not isinstance(result, dict):
        raise RuntimeError("AI 未返回有效的结构化行情解读")
    return {
        "summary": str(result.get("summary") or ""),
        "outlook": str(result.get("outlook") or ""),
        "signals": [str(value) for value in (result.get("signals") or [])][:3],
        "risks": [str(value) for value in (result.get("risks") or [])][:3],
        "generated_at": _now(),
    }
