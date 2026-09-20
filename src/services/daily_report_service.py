"""
行情日报服务
每天定时统计关注关键词的平均价/最低价/最高价/涨跌，推送到 Bark
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta

from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_task_repository import SqliteTaskRepository
from src.services.notification_service import build_notification_service
from src.services.task_service import TaskService

REPORT_PREV_KEY = "daily_report_prev"
REPORT_CONFIG_KEY = "daily_report_config"

DEFAULT_REPORT_CONFIG = {
    "enabled": True,
    "hour": "9:00",
    "keywords": [],
    "include": {
        "avg": True,
        "min": True,
        "max": True,
        "change": True,
        "rec": True,
    },
}


def _normalize_hour(value) -> str:
    """统一为 'H:MM' 格式字符串（兼容 int 小时/旧数据）"""
    if isinstance(value, str):
        value = value.strip()
        if ":" in value:
            h, _, m = value.partition(":")
            try:
                hh = max(0, min(23, int(h)))
                mm = max(0, min(59, int(m)))
                return f"{hh}:{mm:02d}"
            except ValueError:
                pass
        try:
            hh = max(0, min(23, int(float(value))))
            return f"{hh}:00"
        except ValueError:
            pass
    else:
        try:
            hh = max(0, min(23, int(value)))
            return f"{hh}:00"
        except (TypeError, ValueError):
            pass
    return "9:00"


def get_report_config() -> dict:
    """读取行情日报配置（存 app_metadata，绕开 .env 权限问题）"""
    bootstrap_sqlite_storage()
    cfg = copy.deepcopy(DEFAULT_REPORT_CONFIG)
    try:
        with sqlite_connection() as conn:
            row = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (REPORT_CONFIG_KEY,)).fetchone()
        if row:
            saved = json.loads(row["value"])
            if isinstance(saved, dict):
                cfg.update(saved)
                cfg["include"] = {**DEFAULT_REPORT_CONFIG["include"], **(saved.get("include") or {})}
                cfg["keywords"] = saved.get("keywords") or []
    except Exception:
        pass
    return cfg


def save_report_config(cfg: dict) -> dict:
    """保存行情日报配置"""
    # 必须自举建表：本函数会被 PUT /api/settings/report 直接调用，而 fresh 数据库
    # 上 app_metadata 表可能尚未创建（bootstrap 只在 lifespan 里跑过一次，若此时
    # 数据库文件被删除/重建，这里就会 no such table: app_metadata）。
    bootstrap_sqlite_storage()
    normalized = copy.deepcopy(DEFAULT_REPORT_CONFIG)
    normalized.update(cfg or {})
    normalized["enabled"] = bool(normalized.get("enabled", True))
    normalized["hour"] = _normalize_hour(normalized.get("hour", "9:00"))
    normalized["keywords"] = [str(k).strip() for k in (normalized.get("keywords") or []) if str(k).strip()]
    inc = normalized.get("include") or {}
    normalized["include"] = {k: bool(inc.get(k, True)) for k in DEFAULT_REPORT_CONFIG["include"]}
    with sqlite_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
            (REPORT_CONFIG_KEY, json.dumps(normalized, ensure_ascii=False)),
        )
        conn.commit()
    return normalized


def _get_prev_report() -> dict:
    """读取上一次日报的平均价（用于环比涨跌）"""
    try:
        with sqlite_connection() as conn:
            row = conn.execute("SELECT value FROM app_metadata WHERE key = ?", (REPORT_PREV_KEY,)).fetchone()
        if row:
            data = json.loads(row["value"])
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def _save_prev_report(report: dict) -> None:
    try:
        with sqlite_connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
                (REPORT_PREV_KEY, json.dumps(report, ensure_ascii=False)),
            )
            conn.commit()
    except Exception as exc:
        print(f"[行情日报] 保存上次均价失败: {exc}")


async def _collect_keywords() -> list[dict]:
    """从任务列表中收集所有关键词（含任务名）"""
    repo = SqliteTaskRepository()
    task_service = TaskService(repo)
    tasks = await task_service.get_all_tasks()
    seen = set()
    keywords = []
    for task in tasks:
        kw = (task.keyword or "").strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        keywords.append({"keyword": kw, "task_name": task.task_name or kw})
    return keywords


def _build_keyword_stats(keyword: str, since: str) -> dict | None:
    """统计某关键词最近 since 之后的价格样本"""
    try:
        with sqlite_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt,
                       COALESCE(AVG(price), 0) AS avg_price,
                       COALESCE(MIN(price), 0) AS min_price,
                       COALESCE(MAX(price), 0) AS max_price,
                       COALESCE(SUM(is_recommended), 0) AS rec
                FROM result_items
                WHERE keyword = ? AND crawl_time >= ?
                """,
                (keyword, since),
            ).fetchone()
        if not row or not row["cnt"]:
            return None
        return {
            "cnt": row["cnt"],
            "avg": row["avg_price"],
            "min": row["min_price"],
            "max": row["max_price"],
            "rec": row["rec"],
        }
    except Exception:
        return None


def _fmt_price(value: float) -> str:
    try:
        if value >= 10000:
            return f"{value / 10000:.2f}万"
        return f"¥{value:,.0f}" if value >= 1000 else f"¥{value:.0f}"
    except (TypeError, ValueError):
        return "-"


def _pct(current: float, previous: float) -> str:
    """环比涨跌百分比"""
    try:
        if not previous or previous <= 0:
            return ""
        diff = (current - previous) / previous * 100
        if abs(diff) < 0.01:
            return "（持平）"
        arrow = "↑" if diff > 0 else "↓"
        return f"（{arrow}{abs(diff):.1f}%）"
    except (TypeError, ValueError):
        return ""


async def build_and_send_daily_report() -> dict:
    """生成并推送行情日报（应用自定义配置）"""
    try:
        since = (datetime.now() - timedelta(days=1)).isoformat()
        cfg = get_report_config()
        if not cfg.get("enabled", True):
            return {"status": "skipped", "reason": "日报未启用"}
        inc = cfg.get("include", {})

        prev = _get_prev_report()
        keywords = await _collect_keywords()

        sections = []
        current_report = {}
        keyword_filter = cfg.get("keywords") or []

        for item in keywords:
            kw = item["keyword"]
            if keyword_filter and kw not in keyword_filter:
                continue
            stats = _build_keyword_stats(kw, since)
            if not stats:
                continue
            prev_avg = float((prev.get(kw) or {}).get("avg") or 0)
            lines = [f"📦 {kw}（{item['task_name']}）"]
            if inc.get("rec", True):
                lines.append(f"   样本 {stats['cnt']} 条 | 推荐 {stats['rec']} 条")
            if inc.get("avg", True):
                change_part = _pct(stats["avg"], prev_avg) if inc.get("change", True) else ""
                lines.append(f"   平均价 {_fmt_price(stats['avg'])} {change_part}")
            if inc.get("min", True) or inc.get("max", True):
                parts = []
                if inc.get("min", True):
                    parts.append(f"最低 {_fmt_price(stats['min'])}")
                if inc.get("max", True):
                    parts.append(f"最高 {_fmt_price(stats['max'])}")
                lines.append("   " + " | ".join(parts))
            sections.append("\n".join(lines))
            current_report[kw] = {"avg": stats["avg"], "cnt": stats["cnt"]}

        if not sections:
            return {"status": "no_data", "reason": "最近 24 小时无采集数据"}

        _save_prev_report(current_report)

        title = "📊 闲鱼行情日报"
        body = "近 24 小时关注商品行情：\n\n" + "\n\n".join(sections)
        service = build_notification_service()
        payload = {
            "商品标题": title,
            "当前售价": body,
            "商品链接": "#",
            "通知标题": title,
        }
        await service.send_notification(payload, "行情日报（每日定时汇总）")
        print("[行情日报] 已推送")
        return {"status": "sent", "keywords": len(sections)}
    except Exception as exc:
        print(f"[行情日报] 生成失败: {exc}")
        return {"status": "error", "error": str(exc)}