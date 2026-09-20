"""
SQLite 连接与 schema 初始化。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.infrastructure.persistence.storage_names import DEFAULT_DATABASE_PATH


BUSY_TIMEOUT_MS = 5000

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY,
        task_name TEXT NOT NULL,
        enabled INTEGER NOT NULL,
        keyword TEXT NOT NULL,
        description TEXT,
        analyze_images INTEGER NOT NULL,
        max_pages INTEGER NOT NULL,
        personal_only INTEGER NOT NULL,
        min_price TEXT,
        max_price TEXT,
        notify_price_below REAL,
        auto_consult INTEGER NOT NULL DEFAULT 0,
        cron TEXT,
        ai_prompt_base_file TEXT NOT NULL,
        ai_prompt_criteria_file TEXT NOT NULL,
        account_state_file TEXT,
        account_strategy TEXT NOT NULL,
        free_shipping INTEGER NOT NULL,
        new_publish_option TEXT,
        region TEXT,
        decision_mode TEXT NOT NULL,
        keyword_rules_json TEXT NOT NULL,
        is_running INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS result_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        result_filename TEXT NOT NULL,
        keyword TEXT NOT NULL,
        task_name TEXT NOT NULL,
        crawl_time TEXT NOT NULL,
        publish_time TEXT,
        price REAL,
        price_display TEXT,
        item_id TEXT,
        title TEXT,
        link TEXT,
        link_unique_key TEXT NOT NULL,
        seller_nickname TEXT,
        is_recommended INTEGER NOT NULL,
        analysis_source TEXT,
        keyword_hit_count INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        raw_json TEXT NOT NULL,
        UNIQUE(result_filename, link_unique_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword_slug TEXT NOT NULL,
        keyword TEXT NOT NULL,
        task_name TEXT NOT NULL,
        snapshot_time TEXT NOT NULL,
        snapshot_day TEXT NOT NULL,
        run_id TEXT NOT NULL,
        item_id TEXT NOT NULL,
        title TEXT,
        price REAL NOT NULL,
        price_display TEXT,
        tags_json TEXT NOT NULL,
        region TEXT,
        seller TEXT,
        publish_time TEXT,
        link TEXT,
        UNIQUE(keyword_slug, run_id, item_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS result_blacklist_rules (
        result_filename TEXT PRIMARY KEY,
        blacklist_keywords_json TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id TEXT NOT NULL UNIQUE,
        result_filename TEXT NOT NULL,
        keyword TEXT NOT NULL,
        task_name TEXT NOT NULL,
        title TEXT NOT NULL,
        link TEXT NOT NULL,
        image_url TEXT,
        alert_price REAL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_price REAL,
        last_seen_at TEXT,
        missing_runs INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        watch_item_id INTEGER NOT NULL,
        event_key TEXT NOT NULL UNIQUE,
        event_type TEXT NOT NULL,
        price REAL,
        previous_price REAL,
        detail TEXT NOT NULL,
        is_read INTEGER NOT NULL DEFAULT 0,
        notified INTEGER NOT NULL DEFAULT 0,
        notification_results_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY(watch_item_id) REFERENCES watch_items(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_health (
        account_path TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'unknown',
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        last_success_at TEXT,
        last_failure_at TEXT,
        cooldown_until TEXT,
        state_mtime REAL,
        last_used_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account_leases (
        owner TEXT PRIMARY KEY,
        account_path TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY(account_path) REFERENCES account_health(account_path) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tasks_name ON tasks(task_name)",
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_crawl
    ON result_items(result_filename, crawl_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_publish
    ON result_items(result_filename, publish_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_price
    ON result_items(result_filename, price DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_results_filename_recommended
    ON result_items(result_filename, is_recommended, analysis_source, crawl_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_time
    ON price_snapshots(keyword_slug, snapshot_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_snapshots_keyword_item_time
    ON price_snapshots(keyword_slug, item_id, snapshot_time DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_watch_items_task_status
    ON watch_items(task_name, enabled, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_watch_events_created
    ON watch_events(created_at DESC, is_read)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_account_health_status
    ON account_health(status, cooldown_until)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_account_leases_path
    ON account_leases(account_path, expires_at)
    """,
)


def get_database_path() -> str:
    return os.getenv("APP_DATABASE_FILE", DEFAULT_DATABASE_PATH)


def _prepare_database_file(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def init_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    _migrate_result_items_status(conn)
    _migrate_fusion_fields(conn)
    conn.commit()


def _migrate_fusion_fields(conn: sqlite3.Connection) -> None:
    migrations = {
        "tasks": {
            "runtime_status": "TEXT NOT NULL DEFAULT 'stopped'",
            "collection_interval_minutes": "INTEGER NOT NULL DEFAULT 0",
            "retry_limit": "INTEGER NOT NULL DEFAULT 2",
            "retry_backoff_seconds": "INTEGER NOT NULL DEFAULT 5",
            "last_failure_reason": "TEXT",
            "notify_price_below": "REAL",
            "auto_consult": "INTEGER NOT NULL DEFAULT 0",
            "notify_mode": "TEXT NOT NULL DEFAULT 'keyword'",
            "strict_keyword_match": "INTEGER NOT NULL DEFAULT 1",
        },
        "watch_items": {
            "refresh_interval_minutes": "INTEGER",
            "last_refresh_at": "TEXT",
            "next_refresh_at": "TEXT",
            "notify_price_drop": "INTEGER NOT NULL DEFAULT 1",
            "notify_low_price": "INTEGER NOT NULL DEFAULT 1",
            "notify_delisted": "INTEGER NOT NULL DEFAULT 1",
            "notify_relisted": "INTEGER NOT NULL DEFAULT 1",
            "consult_enabled": "INTEGER NOT NULL DEFAULT 0",
            "consult_template": "TEXT",
            "consult_account_strategy": "TEXT NOT NULL DEFAULT 'pool'",
            "last_consulted_at": "TEXT",
            # --- 售罄检测（粘性状态机）---
            # dead: 是否已判定「死亡」（售出或下架）。粘性：一旦置 1 永不自动回退，
            #       避免风控抖动导致状态反复横跳、反复发通知。
            #       只有人工重新激活或商品重新被完整采集到才会清零（见 watch_service）。
            "dead": "INTEGER NOT NULL DEFAULT 0",
            # dead_reason: 判定死亡的依据（已售出 / 已删除 / 已下架 / 连续缺失），
            #              便于事后排查误判。
            "dead_reason": "TEXT",
            # dead_since: 判定死亡的时间戳。
            "dead_since": "TEXT",
            # --- 通知分类型开关 ---
            # notify_on_sold: 售出通知独立开关（与 notify_delisted 区分：
            #                 下架是卖家主动撤下，售出是买家买走，两者含义不同）
            "notify_on_sold": "INTEGER NOT NULL DEFAULT 1",
            "notify_on_favorite": "INTEGER NOT NULL DEFAULT 1",
            "notify_on_login": "INTEGER NOT NULL DEFAULT 1",
            # --- 静音延期 ---
            # muted_until: 静音截止时间（ISO8601）。到期后自动恢复提醒，
            #              比「永久排除」更优雅：现在嫌贵不想看，过几天还在就值得看看。
            #              查询侧用 (muted_until IS NULL OR muted_until <= now) 自动恢复。
            "muted_until": "TEXT",
            # prev_reduce_price: 上次观测到的闲鱼原生「收藏后降价」金额。
            #                    这是平台侧记录的降价，能覆盖「首次观测前就已降过」的情况，
            #                    而仅靠跨次比价只能看到观测之后的降价。
            "prev_reduce_price": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    for table, columns in migrations.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consultation_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_item_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            account_path TEXT,
            message TEXT NOT NULL,
            status TEXT NOT NULL,
            error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(watch_item_id) REFERENCES watch_items(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ai_usage_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model TEXT NOT NULL,
            request_type TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_hit_tokens INTEGER,
            cache_miss_tokens INTEGER,
            estimated_cost REAL,
            created_at TEXT NOT NULL
        )
    """)
    # 会话守卫三张表。键统一是 (账号, 会话) 复合键——只用 chat_id 会让不同账号
    # 下的同号会话互相影响（见 src/services/session_guard.py 的说明）。
    # 到期判定交给查询侧做（expires_at <= now 视为失效），因此不需要清理任务：
    # 少一个定时任务就少一处「任务没跑导致状态卡死」的风险。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_pauses (
            pause_key TEXT PRIMARY KEY,
            account TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL DEFAULT '',
            expires_at TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_order_locks (
            lock_key TEXT PRIMARY KEY,
            account TEXT NOT NULL DEFAULT '',
            chat_id TEXT NOT NULL DEFAULT '',
            expires_at TEXT,
            reason TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_cooldowns (
            cooldown_key TEXT PRIMARY KEY,
            expires_at TEXT,
            updated_at TEXT NOT NULL
        )
    """)


def _migrate_result_items_status(conn: sqlite3.Connection) -> None:
    """为 result_items 表添加 status 列（仅执行一次）。"""
    row = conn.execute(
        "SELECT value FROM app_metadata WHERE key = 'migration:result_items_status'"
    ).fetchone()
    if row is not None:
        return
    cols = [r[1] for r in conn.execute("PRAGMA table_info(result_items)").fetchall()]
    if "status" not in cols:
        conn.execute(
            "ALTER TABLE result_items ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
    conn.execute(
        "INSERT OR REPLACE INTO app_metadata(key, value) VALUES ('migration:result_items_status', 'done')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_results_filename_status_crawl"
        " ON result_items(result_filename, status, crawl_time DESC)"
    )


@contextmanager
def sqlite_connection(
    db_path: str | None = None,
) -> Iterator[sqlite3.Connection]:
    path = db_path or get_database_path()
    _prepare_database_file(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _apply_pragmas(conn)
        yield conn
    finally:
        conn.close()