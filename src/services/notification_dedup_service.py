"""跨任务通知去重：同一个商品被多个任务命中时只推送一次。

**为什么需要这个模块**

既有的通知去重是**单任务内**的：每个任务在自己的进程里按事件 key 去重。
但多个任务的关键词经常重叠（「索尼 A7M4」与「索尼微单」会命中同一件商品），
任务之间互不知情，于是同一件商品在几分钟内被推送 N 次。用户看到的是刷屏，
而不是「发现好货」。

本模块把去重维度提到**商品 ID**，并挂上时间窗：窗口内该商品只推一次，
窗口过后（例如几小时后价格可能已变）允许重新推送。

**为什么去重是「查询」与「写入」分离的两个方法**

:meth:`CrossTaskNotificationDeduper.should_notify` 只读，:meth:`mark_notified`
只写。合并成一个 ``try_claim`` 看似更省事，但推送本身可能失败（webhook 超时、
渠道禁用）——调用方需要选择语义：

- 「先标记再推送」：宁可漏推也不错推，适合高频、低价值的事件
- 「推送成功后再标记」：失败了下一轮还能补推，适合低频、高价值的事件

两个方法分开，调用方自己组合，本模块不替它做决定。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

#: 默认去重时间窗（秒）：1 小时。一轮扫描通常远快于这个量级，窗口足以覆盖
#: 多任务在同一轮里重复命中同一商品的情况。
DEFAULT_WINDOW_SECONDS = 3600.0

#: 默认条目上限。按每条约 100 字节估算，2000 条约占用几百 KB。
DEFAULT_MAX_ENTRIES = 2000

#: 键前缀。加前缀是为了让「商品键」与「回退键」在同一个命名空间里也不会碰撞
#: （否则 ``item_id="T1"`` 会和 ``task_name="T1"`` 撞成同一条）。
_ITEM_KEY_PREFIX = "item:"
_FALLBACK_KEY_PREFIX = "fallback:"

#: 回退键内部的字段分隔符，同样用控制字符避免 ``("ab", "c")`` 与 ``("a", "bc")`` 碰撞。
_FIELD_SEPARATOR = "\x1f"


def _coerce_positive_int(value: object, *, default: int) -> int:
    """把任意输入规整成正整数；非法输入回落到 ``default``，绝不抛异常。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    candidate = int(number)
    if candidate < 1:
        return default
    return candidate


def _coerce_non_negative_float(value: object, *, default: float) -> float:
    """把任意输入规整成非负有限浮点数；非法输入（含负数）回落到 ``default``。

    ``0`` 是**合法**的：``window_seconds=0`` 表示「窗口为零」，即每次调用都
    允许通知，等价于关闭去重——这是有意义的显式配置，不该被当作脏数据覆盖掉。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return default
    return number


@dataclass(frozen=True)
class _DedupEntry:
    """去重条目：键对应的通知登记时间。"""

    marked_at: float


class CrossTaskNotificationDeduper:
    """跨任务通知去重器（时间窗 + LRU 淘汰 + 线程安全）。

    **键的构造规则（重要）**

    商品 ID 优先：``item_id`` 非空时键只由它构成，``task_name`` 与 ``event_type``
    都**不参与**——这正是「跨任务去重」的定义：不同任务命中同一件商品，必须被认成
    同一个键，否则去重就退化回单任务级别了。

    仅当 ``item_id`` 为空（脏数据、采集缺字段）时才回退到
    ``(task_name, event_type)`` 组合键。理由：如果空 ID 也共用一个键，那么所有
    「ID 缺失」的通知会互相误杀——第一件缺 ID 的商品推送后，后续所有缺 ID 的商品
    在整个窗口内全部沉默。用任务名 + 事件类型兜底，至少能把去重粒度保持在一个
    任务的一种事件上，误杀范围可控。

    **时间窗语义**：:meth:`should_notify` 只有在「该键已被 :meth:`mark_notified`
    标记过，且距离标记时刻**严格小于** ``window_seconds``」时返回 ``False``；
    其余情况返回 ``True``。恰好等于窗口长度的时刻即视为可再次通知。

    **内存上限**：条目数超过 ``max_entries`` 时淘汰最久未使用的键。键在
    :meth:`mark_notified` 写入或 :meth:`should_notify` 命中时都会被移到「最近使用」
    位置，因此被频繁查询的热商品不会被淘汰，而早已不再出现的商品会先出局。
    过期条目在写入与查询时惰性清理，长期运行下内存不会无界增长。

    **线程安全**：内部字典由 :class:`threading.Lock` 保护。这个 deduper 很可能被
    采集线程、调度线程与 API 线程同时调用，锁是必需的；锁区内只做字典读写与
    时钟调用，没有 ``await``、不做 I/O，持锁时间在微秒级，不会成为瓶颈。

    **可注入时钟**：默认 ``time.monotonic``。注入假时钟即可在测试中推进窗口，
    无需 ``sleep``，测试既快又不 flaky。
    """

    def __init__(
        self,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._window_seconds = _coerce_non_negative_float(
            window_seconds, default=DEFAULT_WINDOW_SECONDS
        )
        self._max_entries = _coerce_positive_int(max_entries, default=DEFAULT_MAX_ENTRIES)
        self._clock: Callable[[], float] = clock if callable(clock) else time.monotonic
        # 保持使用顺序：头部最旧，尾部最新（访问或标记都会移到尾部）。
        self._entries: dict[str, _DedupEntry] = {}
        self._lock = threading.Lock()

    @property
    def window_seconds(self) -> float:
        """生效的时间窗（已按回落规则规整）。"""
        return self._window_seconds

    @property
    def max_entries(self) -> int:
        """生效的条目上限（已按回落规则规整）。"""
        return self._max_entries

    @property
    def size(self) -> int:
        """当前条目数（可能包含尚未被访问触发的过期条目）。"""
        with self._lock:
            return len(self._entries)

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _normalize_id(value: object) -> str:
        if value is None:
            return ""
        text = value if isinstance(value, str) else str(value)
        return text.strip()

    def _build_key(self, item_id: object, task_name: object, event_type: object) -> str:
        """按「商品 ID 优先，否则回退到任务名 + 事件类型」构造去重键。"""
        normalized_item = self._normalize_id(item_id)
        if normalized_item:
            return f"{_ITEM_KEY_PREFIX}{normalized_item}"
        task = self._normalize_id(task_name)
        event = self._normalize_id(event_type)
        return f"{_FALLBACK_KEY_PREFIX}{task}{_FIELD_SEPARATOR}{event}"

    def _is_fresh(self, entry: _DedupEntry, now: float) -> bool:
        """判断条目是否仍在去重窗口内（纯计算，不碰字典）。"""
        return (now - entry.marked_at) < self._window_seconds

    def _touch_locked(self, key: str, entry: _DedupEntry) -> None:
        """把键移到「最近使用」位置（调用方必须已持锁）。"""
        del self._entries[key]
        self._entries[key] = entry

    def _prune_expired_locked(self, now: float) -> None:
        """清理已出窗口的条目（调用方必须已持锁）。"""
        expired = [
            key
            for key, entry in self._entries.items()
            if (now - entry.marked_at) >= self._window_seconds
        ]
        for key in expired:
            del self._entries[key]

    def _evict_overflow_locked(self) -> None:
        """淘汰超出上限的最旧条目（调用方必须已持锁）。"""
        while len(self._entries) > self._max_entries:
            oldest_key = next(iter(self._entries))
            del self._entries[oldest_key]

    def should_notify(
        self,
        item_id: str,
        *,
        task_name: str = "",
        event_type: str = "",
    ) -> bool:
        """查询现在是否**应该**推送该商品。

        返回 ``True`` 表示「窗口内还没通知过（或已出窗口），可以去推」；
        返回 ``False`` 表示「窗口内已经通知过，跳过」。

        本方法**不写标记**：调用方决定推成功后自己调 :meth:`mark_notified`，
        或为了「宁可漏推」而先标记。命中（返回 ``False``）会刷新该键的 LRU 位置。
        """
        key = self._build_key(item_id, task_name, event_type)
        now = self._now()
        with self._lock:
            self._prune_expired_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                return True
            self._touch_locked(key, entry)
            return not self._is_fresh(entry, now)

    def mark_notified(
        self,
        item_id: str,
        *,
        task_name: str = "",
        event_type: str = "",
    ) -> None:
        """登记「该商品已被通知」，开启（或重置）它的去重窗口。

        重复标记同一键是幂等的，但**会刷新窗口起点**——这与「刚刚又推了一次」
        的事实一致。标记时顺带清理过期条目并执行 LRU 淘汰，保证内存有界。
        """
        key = self._build_key(item_id, task_name, event_type)
        now = self._now()
        with self._lock:
            self._prune_expired_locked(now)
            self._entries.pop(key, None)
            self._entries[key] = _DedupEntry(marked_at=now)
            self._evict_overflow_locked()

    def clear(self) -> None:
        """清空全部去重记录（例如任务全部重建后主动重置）。"""
        with self._lock:
            self._entries.clear()
