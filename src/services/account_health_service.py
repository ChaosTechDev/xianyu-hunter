"""Persistent health and lease management for login-state files."""
from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from src.infrastructure.persistence.sqlite_connection import init_schema, sqlite_connection


STATUS_UNKNOWN = "unknown"
STATUS_HEALTHY = "healthy"
STATUS_COOLING_DOWN = "cooling_down"
STATUS_LOGIN_REQUIRED = "login_required"
STATUS_VERIFICATION_REQUIRED = "verification_required"

BLOCKING_STATUSES = {STATUS_LOGIN_REQUIRED, STATUS_VERIFICATION_REQUIRED}


@contextmanager
def _account_connection():
    with sqlite_connection() as conn:
        init_schema(conn)
        yield conn


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalize_account_path(path: str) -> str:
    return os.path.normpath(str(path).strip())


def _mtime(path: str) -> float | None:
    try:
        return Path(path).stat().st_mtime
    except OSError:
        return None


def _ensure_row(conn, path: str, now: datetime) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO account_health(account_path, state_mtime, updated_at)
        VALUES (?, ?, ?)
        """,
        (path, _mtime(path), _iso(now)),
    )


def _refresh_changed_state(conn, path: str, now: datetime) -> None:
    row = conn.execute(
        "SELECT status, state_mtime FROM account_health WHERE account_path = ?",
        (path,),
    ).fetchone()
    current_mtime = _mtime(path)
    if row is None or current_mtime is None:
        return
    previous_mtime = row["state_mtime"]
    if previous_mtime is not None and current_mtime > float(previous_mtime):
        conn.execute(
            """
            UPDATE account_health
            SET status = ?, consecutive_failures = 0, last_error = NULL,
                cooldown_until = NULL, state_mtime = ?, updated_at = ?
            WHERE account_path = ?
            """,
            (STATUS_UNKNOWN, current_mtime, _iso(now), path),
        )


def list_account_health(paths: Iterable[str]) -> list[dict]:
    normalized = [normalize_account_path(path) for path in paths if str(path).strip()]
    now = _now()
    with _account_connection() as conn:
        for path in normalized:
            _ensure_row(conn, path, now)
            _refresh_changed_state(conn, path, now)
        conn.execute("DELETE FROM account_leases WHERE expires_at <= ?", (_iso(now),))
        rows = conn.execute(
            """
            SELECT h.*,
                   (SELECT COUNT(*) FROM account_leases l
                    WHERE l.account_path = h.account_path AND l.expires_at > ?) active_leases
            FROM account_health h
            """,
            (_iso(now),),
        ).fetchall()
        conn.commit()

    by_path = {row["account_path"]: dict(row) for row in rows}
    result = []
    for path in normalized:
        item = by_path[path]
        cooldown = _parse(item.get("cooldown_until"))
        if item["status"] == STATUS_COOLING_DOWN and (cooldown is None or cooldown <= now):
            item["status"] = STATUS_UNKNOWN
        item["available"] = item["status"] not in BLOCKING_STATUSES and not (
            item["status"] == STATUS_COOLING_DOWN and cooldown and cooldown > now
        )
        result.append(item)
    return result


def reserve_account(
    paths: Iterable[str],
    *,
    owner: str,
    lease_seconds: int = 1800,
    include_unhealthy: bool = False,
) -> str | None:
    candidates = [normalize_account_path(path) for path in paths if str(path).strip()]
    if not candidates:
        return None
    now = _now()
    placeholders = ",".join("?" for _ in candidates)
    with _account_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM account_leases WHERE expires_at <= ?", (_iso(now),))
        conn.execute("DELETE FROM account_leases WHERE owner = ?", (owner,))
        for path in candidates:
            _ensure_row(conn, path, now)
            _refresh_changed_state(conn, path, now)
        rows = conn.execute(
            f"""
            SELECT h.*,
                   (SELECT COUNT(*) FROM account_leases l
                    WHERE l.account_path = h.account_path AND l.expires_at > ?) active_leases
            FROM account_health h
            WHERE h.account_path IN ({placeholders})
            ORDER BY active_leases ASC,
                     CASE h.status WHEN 'healthy' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END,
                     COALESCE(h.last_used_at, '') ASC,
                     h.account_path ASC
            """,
            (_iso(now), *candidates),
        ).fetchall()
        chosen = None
        for row in rows:
            status = row["status"]
            cooldown = _parse(row["cooldown_until"])
            blocked = status in BLOCKING_STATUSES or (
                status == STATUS_COOLING_DOWN and cooldown and cooldown > now
            )
            if include_unhealthy or not blocked:
                chosen = row["account_path"]
                break
        if chosen is None:
            conn.commit()
            return None
        expires_at = now + timedelta(seconds=max(60, int(lease_seconds)))
        conn.execute(
            "INSERT INTO account_leases(owner, account_path, expires_at, created_at) VALUES (?, ?, ?, ?)",
            (owner, chosen, _iso(expires_at), _iso(now)),
        )
        conn.execute(
            "UPDATE account_health SET last_used_at = ?, updated_at = ? WHERE account_path = ?",
            (_iso(now), _iso(now), chosen),
        )
        conn.commit()
        return chosen


def release_account(owner: str) -> None:
    with _account_connection() as conn:
        conn.execute("DELETE FROM account_leases WHERE owner = ?", (owner,))
        conn.commit()


def record_account_success(path: str) -> None:
    normalized = normalize_account_path(path)
    now = _now()
    with _account_connection() as conn:
        _ensure_row(conn, normalized, now)
        conn.execute(
            """
            UPDATE account_health
            SET status = ?, consecutive_failures = 0, last_error = NULL,
                last_success_at = ?, cooldown_until = NULL, state_mtime = ?, updated_at = ?
            WHERE account_path = ?
            """,
            (STATUS_HEALTHY, _iso(now), _mtime(normalized), _iso(now), normalized),
        )
        conn.commit()


def record_account_failure(
    path: str,
    reason: str,
    *,
    status: str = STATUS_COOLING_DOWN,
    cooldown_seconds: int = 900,
) -> None:
    normalized = normalize_account_path(path)
    now = _now()
    cooldown_until = now + timedelta(seconds=max(0, int(cooldown_seconds)))
    with _account_connection() as conn:
        _ensure_row(conn, normalized, now)
        conn.execute(
            """
            UPDATE account_health
            SET status = ?, consecutive_failures = consecutive_failures + 1,
                last_error = ?, last_failure_at = ?, cooldown_until = ?,
                state_mtime = ?, updated_at = ?
            WHERE account_path = ?
            """,
            (
                status,
                str(reason)[:1000],
                _iso(now),
                _iso(cooldown_until),
                _mtime(normalized),
                _iso(now),
                normalized,
            ),
        )
        conn.commit()


def reset_account_health(path: str) -> None:
    normalized = normalize_account_path(path)
    now = _now()
    with _account_connection() as conn:
        _ensure_row(conn, normalized, now)
        conn.execute(
            """
            UPDATE account_health
            SET status = ?, consecutive_failures = 0, last_error = NULL,
                cooldown_until = NULL, state_mtime = ?, updated_at = ?
            WHERE account_path = ?
            """,
            (STATUS_UNKNOWN, _mtime(normalized), _iso(now), normalized),
        )
        conn.commit()


def delete_account_health(path: str) -> None:
    normalized = normalize_account_path(path)
    with _account_connection() as conn:
        conn.execute("DELETE FROM account_health WHERE account_path = ?", (normalized,))
        conn.commit()
