"""会话管理强化：复合键暂停、订单锁、四动作位过滤、冷却窗口。

**为什么需要复合键**

暂停必须以 ``(账号, 会话)`` 为键，不能只用 ``chat_id``。同一个 ``chat_id`` 在不同卖家账号下
是完全不同的两个买家会话；只用 ``chat_id`` 做键，一个账号里拉黑/暂停的会话会把另一个账号里
同号会话一起暂停——用户看到的是「A 账号手动回了话，B 账号莫名其妙不回消息了」。
：func:`pause_key` 是唯一的键构造函数，全模块只经过它，避免各处手拼字符串拼错。

**为什么暂停必须落库**

进程内字典在重启后清零，而重启在容器化部署里随时发生（升级、OOM、compose 重启）。
暂停失效的代价是「用户刚手动接管，机器人立刻又插话」。因此暂停写 SQLite，
重启后仍生效；到期时间一到查询侧自动视为未暂停，无需清理任务。

**四动作位过滤**

参考实现把过滤结果拆成四个独立位，而不是一个 ``matched`` 布尔：

- ``skip_auto_reply`` —— 跳过规则/关键词/默认回复
- ``skip_ai_reply`` —— 跳过 AI 回复
- ``notify_enabled`` —— 是否给用户发通知
- ``matched`` —— 是否命中规则（用于统计）

拆开的意义在于「只通知不拦截」这种组合是真实需求：命中敏感词时想让用户知道，
但不必因此停止自动回复。用一个布尔就会把这两种意图压成一种。

**订单锁**

会话进入交易流程后（买家已下单），自动回复容易说错话（重复报价、承诺库存）。
订单锁给出一个明确的排他闸：锁定期间自动回复一律不发，直到解锁或 TTL 到期。

**设计取向**：本模块的所有判定都是「守卫」，不是「执行器」。
拿不准时一律返回**放行**（不暂停、不加锁），因为误拦会让用户漏掉真实买家消息，
代价高于多发一条不该发的消息。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from src.infrastructure.persistence.sqlite_connection import sqlite_connection

#: 默认暂停时长（分钟）。用户手动发过消息后，短期内不应再由机器人插话。
DEFAULT_PAUSE_MINUTES = 10

#: 咨询冷却窗口（秒）。同一会话在该窗口内不重复自动咨询。
DEFAULT_COOLDOWN_SECONDS = 600

#: 订单锁默认 TTL（秒）。给一个上限，避免下单流程异常结束后锁永久残留。
DEFAULT_ORDER_LOCK_TTL_SECONDS = 24 * 3600


def pause_key(account: str | None, chat_id: str | None) -> str:
    """构造 ``(账号, 会话)`` 复合键。

    两段都做字符串化与去空白：真实调用里 ``account`` 常是文件路径或整数 id，
    ``chat_id`` 来自页面文本，类型不统一。

    编码用**长度前缀**（``"<len>:<account>:<chat_id>"``），而不是「值 + 分隔符 + 值」。
    任何固定分隔符都不是单射的——只要某一侧的值里出现该字符，两组不同输入就会撞成
    同一个键：

    - 用 ``:`` 时 ``("a", "b:c")`` 与 ``("a:b", "c")`` 撞车（账号是 Windows 路径时很现实）
    - 用 ``\\x1f`` 时 ``("", "c")`` 与 ``("", "c\\x1f")`` 撞车

    长度前缀没有这个问题：解析时先读数字得到第一段长度，再按长度切分，任意输入都唯一可还原。
    这个键是数据库主键，撞键意味着两个会话互相暂停——静默且难排查，值得用严格编码。
    """
    left = str(account or "").strip()
    right = str(chat_id or "").strip()
    return f"{len(left)}:{left}:{right}"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _expires_at(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=max(0, int(seconds)))).isoformat()


def _remaining_seconds(expires_at: Optional[str], now: Optional[datetime] = None) -> int:
    """剩余秒数。解析失败按 0（视为已过期）——脏数据不能造成永久暂停。"""
    if not expires_at:
        return 0
    reference = now or datetime.now()
    try:
        deadline = datetime.fromisoformat(str(expires_at))
    except (ValueError, TypeError):
        return 0
    try:
        delta = (deadline - reference).total_seconds()
    except TypeError:
        # 时区混用无法比较，同样按已过期处理
        return 0
    return max(0, int(delta))


@dataclass(frozen=True)
class FilterDecision:
    """四动作位过滤结果。

    ``matched`` 只表示「命中了规则」，不表示「要拦」；
    拦不拦由 ``skip_auto_reply`` / ``skip_ai_reply`` 分别决定。
    """

    matched: bool = False
    skip_auto_reply: bool = False
    skip_ai_reply: bool = False
    notify_enabled: bool = False
    rules: tuple[str, ...] = ()

    def with_rule(self, name: str) -> "FilterDecision":
        return FilterDecision(
            matched=True,
            skip_auto_reply=self.skip_auto_reply,
            skip_ai_reply=self.skip_ai_reply,
            notify_enabled=self.notify_enabled,
            rules=self.rules + (name,),
        )


def merge_filter_decisions(decisions: Iterable[FilterDecision]) -> FilterDecision:
    """按位合并多条规则的结果。

    位之间是 **或** 语义：任一条规则要求跳过 AI 回复，最终就跳过。
    ``rules`` 累积所有命中项，便于日志说明「为什么没回」。
    空输入返回全默认值（放行）。
    """
    matched = False
    skip_auto = False
    skip_ai = False
    notify = False
    rules: tuple[str, ...] = ()
    for decision in decisions:
        matched = matched or decision.matched
        skip_auto = skip_auto or decision.skip_auto_reply
        skip_ai = skip_ai or decision.skip_ai_reply
        notify = notify or decision.notify_enabled
        rules = rules + tuple(decision.rules)
    return FilterDecision(
        matched=matched,
        skip_auto_reply=skip_auto,
        skip_ai_reply=skip_ai,
        notify_enabled=notify,
        rules=rules,
    )


class SessionGuard:
    """会话守卫：暂停、订单锁、冷却。

    所有写操作都落 SQLite；所有读操作在库不可用时**放行**。
    这不是偷懒——守卫位于消息处理主链路上，数据库抖动不应升级成「机器人集体失声」。
    """

    def __init__(self, *, db_path: str | None = None) -> None:
        self._db_path = db_path

    # ---------- 暂停 ----------

    def pause(
        self,
        *,
        account: str | None,
        chat_id: str | None,
        minutes: int = DEFAULT_PAUSE_MINUTES,
        reason: str = "",
    ) -> int:
        """暂停某账号下的某个会话，返回剩余秒数。

        与参考实现一致取**较晚**的到期时间：新暂停若短于既有暂停，不缩短既有暂停。
        否则「手动回复触发 10 分钟暂停」会把用户刚设的 1 小时暂停意外降级。
        ``minutes <= 0`` 直接不暂停。
        """
        try:
            minutes = int(minutes or 0)
        except (TypeError, ValueError):
            minutes = 0
        if minutes <= 0:
            return 0

        key = pause_key(account, chat_id)
        new_expires = _expires_at(minutes * 60)
        existing = self.remaining_seconds(account=account, chat_id=chat_id)
        if existing > minutes * 60:
            # 既有暂停更长，保持不动
            return existing

        try:
            with sqlite_connection(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO session_pauses(pause_key, account, chat_id, expires_at, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pause_key) DO UPDATE SET
                        expires_at = excluded.expires_at,
                        reason = excluded.reason
                    """,
                    (
                        key,
                        str(account or ""),
                        str(chat_id or ""),
                        new_expires,
                        str(reason or ""),
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
        except Exception:
            # 守卫写入失败不能中断主链路
            return 0
        return minutes * 60

    def remaining_seconds(self, *, account: str | None, chat_id: str | None) -> int:
        """该会话剩余暂停秒数；0 表示未暂停。"""
        key = pause_key(account, chat_id)
        try:
            with sqlite_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT expires_at FROM session_pauses WHERE pause_key = ?", (key,)
                ).fetchone()
        except Exception:
            return 0
        if row is None:
            return 0
        return _remaining_seconds(row["expires_at"])

    def is_paused(self, *, account: str | None, chat_id: str | None) -> bool:
        return self.remaining_seconds(account=account, chat_id=chat_id) > 0

    def resume(self, *, account: str | None, chat_id: str | None) -> None:
        """人工解除暂停。"""
        key = pause_key(account, chat_id)
        try:
            with sqlite_connection(self._db_path) as conn:
                conn.execute("DELETE FROM session_pauses WHERE pause_key = ?", (key,))
                conn.commit()
        except Exception:
            pass

    # ---------- 订单锁 ----------

    def lock_order(
        self,
        *,
        account: str | None,
        chat_id: str | None,
        ttl_seconds: int = DEFAULT_ORDER_LOCK_TTL_SECONDS,
        reason: str = "",
    ) -> int:
        """锁定会话（进入交易流程）。返回剩余秒数。"""
        key = pause_key(account, chat_id)
        try:
            ttl_seconds = int(ttl_seconds or 0)
        except (TypeError, ValueError):
            ttl_seconds = 0
        expires = _expires_at(ttl_seconds)
        try:
            with sqlite_connection(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO session_order_locks(lock_key, account, chat_id, expires_at, reason, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(lock_key) DO UPDATE SET
                        expires_at = excluded.expires_at,
                        reason = excluded.reason
                    """,
                    (
                        key,
                        str(account or ""),
                        str(chat_id or ""),
                        expires,
                        str(reason or ""),
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
        except Exception:
            return 0
        return max(0, ttl_seconds)

    def is_order_locked(self, *, account: str | None, chat_id: str | None) -> bool:
        key = pause_key(account, chat_id)
        try:
            with sqlite_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT expires_at FROM session_order_locks WHERE lock_key = ?", (key,)
                ).fetchone()
        except Exception:
            return False
        if row is None:
            return False
        return _remaining_seconds(row["expires_at"]) > 0

    def unlock_order(self, *, account: str | None, chat_id: str | None) -> None:
        key = pause_key(account, chat_id)
        try:
            with sqlite_connection(self._db_path) as conn:
                conn.execute("DELETE FROM session_order_locks WHERE lock_key = ?", (key,))
                conn.commit()
        except Exception:
            pass

    # ---------- 冷却 ----------

    def mark_contacted(self, *, account: str | None, chat_id: str | None) -> None:
        key = pause_key(account, chat_id)
        try:
            with sqlite_connection(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT INTO session_cooldowns(cooldown_key, expires_at, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(cooldown_key) DO UPDATE SET
                        expires_at = excluded.expires_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        key,
                        _expires_at(DEFAULT_COOLDOWN_SECONDS),
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
        except Exception:
            pass

    def cooldown_remaining(self, *, account: str | None, chat_id: str | None) -> int:
        key = pause_key(account, chat_id)
        try:
            with sqlite_connection(self._db_path) as conn:
                row = conn.execute(
                    "SELECT expires_at FROM session_cooldowns WHERE cooldown_key = ?", (key,)
                ).fetchone()
        except Exception:
            return 0
        if row is None:
            return 0
        return _remaining_seconds(row["expires_at"])

    def in_cooldown(self, *, account: str | None, chat_id: str | None) -> bool:
        return self.cooldown_remaining(account=account, chat_id=chat_id) > 0

    # ---------- 综合闸门 ----------

    def should_skip_auto_reply(self, *, account: str | None, chat_id: str | None) -> tuple[bool, str]:
        """自动回复总闸。返回 ``(是否跳过, 原因)``。

        订单锁优先级最高——已进入交易的会话，任何自动消息都可能造成实际损失；
        暂停次之；冷却只管「主动触达」类动作，不拦被动回复，因此不在这里。
        """
        if self.is_order_locked(account=account, chat_id=chat_id):
            return True, "订单锁生效中"
        remaining = self.remaining_seconds(account=account, chat_id=chat_id)
        if remaining > 0:
            return True, f"会话暂停中（剩余 {remaining} 秒）"
        return False, ""
