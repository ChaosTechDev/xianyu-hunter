"""AI 调用治理服务：全局并发闸、结果缓存（TTL+LRU）、预算守卫。

**为什么需要这个模块**

:mod:`src.services.ai_usage_service` 只做「事后记账」：每次 AI 调用结束后把
token 与成本估算写进 ``ai_usage_stats`` 表，供汇总查询。它能告诉你**已经花了多少**，
却拦不住**接下来还会花多少**——没有闸、没有缓存、没有预算闸门。本模块补齐这三件事：

- :class:`AIRequestGate`：进程内全局并发上限。多任务同时跑 AI 分析时，请求数会
  线性叠加，容易撞上服务商限流或把本地连接池打满；闸门把「同时在飞的请求数」钉在
  一个常数上，多出来的排队而不是失败。
- :class:`AIResultCache`：带 TTL 与 LRU 的结果缓存。同一个商品被多个任务命中、
  或在短时间窗内被重复扫描时，分析结果完全一致，重算是纯粹的浪费。
- :func:`check_budget`：纯函数式的预算判定。调用方拿到 ``allow``/``level``
  再决定是否发请求，判定逻辑不碰数据库、不联网，因此可被高频调用与单测覆盖。

**为什么是纯逻辑**

三个能力都刻意做到「不联网、不连库、不读环境变量」：治理策略的正确性不该被
外部依赖掩盖，落库仍由 :mod:`src.services.ai_usage_service` 负责，本模块只做决策。
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

#: 并发闸默认上限。4 是个保守值：既能让多个任务并行推进，又远低于常见服务商限流。
DEFAULT_MAX_CONCURRENCY = 4

#: 结果缓存默认 TTL（秒）。15 分钟足够覆盖一轮扫描内的重复命中。
DEFAULT_CACHE_TTL_SECONDS = 900.0

#: 结果缓存默认条目上限。按单条分析结果几 KB 估算，500 条约占用几 MB。
DEFAULT_CACHE_MAX_ENTRIES = 500

#: 预算预警默认阈值。
DEFAULT_WARN_RATIO = 0.8

#: 缓存键片段分隔符。用 ASCII 单元分隔符而不是 ``:``/``-``，因为模型名、商品 ID
#: 里都可能出现常见符号，用控制字符才能保证 ``("a", "bc")`` 与 ``("ab", "c")``
#: 不会拼成同一个键。
_KEY_SEPARATOR = "\x1f"


def _coerce_positive_int(value: Any, *, default: int) -> int:
    """把任意输入规整成正整数；非法输入回落到 ``default``，绝不抛异常。

    非法包括：``None``、字符串等非数值、布尔值（``True``/``False`` 语义歧义，
    不按 1/0 处理）、``NaN``/``inf``、以及取整后小于 1 的值。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number):
        return default
    candidate = int(number)
    if candidate < 1:
        return default
    return candidate


def _coerce_non_negative_float(value: Any, *, default: float) -> float:
    """把任意输入规整成非负有限浮点数；非法输入（含负数）回落到 ``default``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return default
    return number


def _coerce_spent(value: Any) -> float:
    """把「已花费」规整成非负有限浮点数。

    脏数据（``None`` / ``NaN`` / ``inf`` / 负数）一律按 ``0`` 处理：上游一次
    统计口径错误不应该让全系统停止调用 AI，宁可放过也不误杀。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    if not math.isfinite(number) or number < 0:
        return 0.0
    return number


def _coerce_optional_positive_float(value: Any) -> float | None:
    """规整「预算上限」：非法（``None`` / 非数值 / ``NaN`` / ``inf`` / 非正数）返回 ``None``。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _coerce_warn_ratio(value: Any) -> float:
    """规整预警阈值：只接受 ``(0, 1]`` 区间内的有限数值，其余回落默认值。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return DEFAULT_WARN_RATIO
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > 1:
        return DEFAULT_WARN_RATIO
    return number


class AIRequestGate:
    """进程内全局 AI 并发闸门。

    用 :class:`asyncio.Semaphore` 限制同时在飞的 AI 请求数，超出的调用**排队等待**
    而不是直接失败。典型用法是在启动时建一个实例并全局复用（进程级单例），
    每次调用 AI 时套一层 ``async with``::

        gate = AIRequestGate(4)

        async def call_model():
            async with gate:
                return await client.chat(...)

    **异常路径也必须归还槽位**：``async with`` 体内无论正常返回还是抛异常，
    ``__aexit__`` 都会执行；本实现把「计数递减 + 信号量释放」放在同一条无分支路径上，
    因此 ``asyncio.CancelledError`` 之类的异常同样能恢复 ``available``。

    本类只适用于单事件循环内的协作式并发：``in_flight`` 的读改写发生在
    ``await`` 边界之后，不存在线程竞态；跨线程复用请另行加锁。

    **信号量按事件循环惰性绑定（重要）**

    :class:`asyncio.Semaphore` 在**发生争用**（有等待者）时会把 future 绑定到当前
    事件循环，此后在另一个事件循环里使用会抛
    ``RuntimeError: ... is bound to a different event loop``。

    这让「模块级创建、全局共享」的用法直接踩坑：项目里既有 ``asyncio.run(...)``
    逐个跑任务的路径（每个 ``asyncio.run`` 都新建一个事件循环），也会在常驻
    API 进程的单一循环里长期运行。因此本类**不在构造时创建信号量**，而是按当前
    运行中的事件循环分别持有：

    - 同一循环内，所有调用者共享同一个信号量 —— 并发上限才真正生效；
    - 跨循环时各用各的信号量 —— 不会因循环更替而崩，也不会残留上一个循环的
      等待者计数（那种残留会让闸门永久卡死）。
    """

    def __init__(self, max_concurrency: int = DEFAULT_MAX_CONCURRENCY) -> None:
        # 非法输入回落到默认值而不是抛异常：治理组件自身不应该成为新的故障点。
        self._max_concurrency = _coerce_positive_int(
            max_concurrency, default=DEFAULT_MAX_CONCURRENCY
        )
        # 事件循环 -> 信号量。用 id(loop) 作键而非 loop 对象本身，避免持有已关闭
        # 循环的强引用（否则长跑进程里反复 asyncio.run 会持续堆积循环对象）。
        self._semaphores: dict[int, asyncio.Semaphore] = {}
        self._in_flight = 0

    @property
    def max_concurrency(self) -> int:
        """生效的并发上限（已按回落规则规整）。"""
        return self._max_concurrency

    @property
    def in_flight(self) -> int:
        """当前持有槽位的请求数（跨事件循环累计）。"""
        return self._in_flight

    @property
    def available(self) -> int:
        """当前空闲槽位数，下限为 0。

        单事件循环场景下该值精确。若同时存在多个事件循环（多线程各自
        ``asyncio.run``），由于计数跨循环累计，该值偏保守——只会低估可用槽位，
        不会高估，因此不会诱导调用方超额并发。
        """
        return max(self._max_concurrency - self._in_flight, 0)

    def _semaphore_for_current_loop(self) -> asyncio.Semaphore:
        """取得（或惰性创建）当前事件循环专属的信号量。"""
        loop = asyncio.get_running_loop()
        key = id(loop)
        semaphore = self._semaphores.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._max_concurrency)
            self._semaphores[key] = semaphore
        return semaphore

    async def __aenter__(self) -> "AIRequestGate":
        await self._semaphore_for_current_loop().acquire()
        self._in_flight += 1
        return self

    async def __aexit__(self, *exc: object) -> bool:
        self._in_flight -= 1
        self._semaphore_for_current_loop().release()
        # 返回 False 表示不吞异常，原异常继续向上抛。
        return False


@dataclass(frozen=True)
class _CacheEntry:
    """缓存条目：值 + 绝对过期时刻。"""

    value: Any
    expires_at: float


class AIResultCache:
    """带 TTL 与 LRU 淘汰的 AI 结果缓存。

    **键由调用方构造**：本类不做哈希推导，调用方用 :func:`build_cache_key` 把
    ``(model, prompt_hash, item_id)`` 这类片段拼成字符串，键的语义留在业务侧。

    **TTL 惰性过期**：``get`` 时才发现条目已过期并顺手删除，因此 ``size`` 可能
    短暂包含尚未被访问到的过期条目；``put`` 会先清理一遍过期条目，避免陈旧数据
    白占 LRU 名额把刚写入的热数据挤掉。

    **LRU 顺序**由 ``dict`` 的插入顺序维护：命中即把键移到尾部（删除再插入），
    字典头部就是最久未使用的那条，淘汰时从头部删。

    **可注入时钟**：默认 ``time.monotonic``，测试注入假时钟即可推进时间，
    完全不需要 ``sleep``（也就不会有 flaky 的时序测试）。

    **线程安全**：内部用轻量 :class:`threading.Lock` 保护字典，因为缓存可能被
    API 线程池与事件循环线程同时访问。锁区内没有 ``await``、没有外部回调，
    持锁时间只有几次字典操作，开销可忽略。
    """

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._ttl_seconds = _coerce_non_negative_float(
            ttl_seconds, default=DEFAULT_CACHE_TTL_SECONDS
        )
        self._max_entries = _coerce_positive_int(
            max_entries, default=DEFAULT_CACHE_MAX_ENTRIES
        )
        self._clock: Callable[[], float] = clock if callable(clock) else time.monotonic
        # 保持插入/最近使用顺序：头部最旧，尾部最新。
        self._entries: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    @property
    def ttl_seconds(self) -> float:
        """生效的 TTL（已按回落规则规整）。"""
        return self._ttl_seconds

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
    def _normalize_key(key: Any) -> str:
        return key if isinstance(key, str) else str(key)

    def get(self, key: str) -> Any | None:
        """取缓存值；未命中或已过期返回 ``None``。命中会刷新 LRU 位置。"""
        normalized = self._normalize_key(key)
        now = self._now()
        with self._lock:
            entry = self._entries.get(normalized)
            if entry is None:
                return None
            if now >= entry.expires_at:
                del self._entries[normalized]
                return None
            # 命中即移到尾部（最近使用）。
            del self._entries[normalized]
            self._entries[normalized] = entry
            return entry.value

    def put(self, key: str, value: Any) -> None:
        """写入缓存；键已存在则更新值并刷新 TTL 与 LRU 位置。"""
        normalized = self._normalize_key(key)
        now = self._now()
        expires_at = now + self._ttl_seconds
        with self._lock:
            self._purge_expired_locked(now)
            # 先删后插：既刷新 LRU 位置，也保证重复 put 不会让 size 虚增。
            self._entries.pop(normalized, None)
            self._entries[normalized] = _CacheEntry(value=value, expires_at=expires_at)
            while len(self._entries) > self._max_entries:
                oldest_key = next(iter(self._entries))
                del self._entries[oldest_key]

    def clear(self) -> None:
        """清空全部条目。"""
        with self._lock:
            self._entries.clear()

    def _purge_expired_locked(self, now: float) -> None:
        """清理已过期条目（调用方必须已持锁）。"""
        expired = [key for key, entry in self._entries.items() if now >= entry.expires_at]
        for key in expired:
            del self._entries[key]


def build_cache_key(*parts: Any) -> str:
    """把多个片段拼成稳定的缓存键。

    - ``None`` 片段被过滤（「没有这个维度」不应与「维度值为空串」混淆）
    - ``0`` / ``False`` 这类假值**不会**被过滤，它们是合法取值
    - 片段之间用 ``\\x1f`` 分隔，避免拼接碰撞

    例如商品分析场景::

        build_cache_key("deepseek-chat", prompt_hash, "ITEM-1")
    """
    return _KEY_SEPARATOR.join(str(part) for part in parts if part is not None)


def check_budget(
    *,
    spent: float,
    limit: float | None,
    warn_ratio: float = DEFAULT_WARN_RATIO,
) -> dict:
    """判定当前预算状态，返回 ``{"allow": bool, "level": str, "message": str | None}``。

    ``level`` 四档：

    - ``unlimited``：``limit`` 为 ``None`` / 非数值 / 非正数 → 不设预算，``allow=True``
    - ``ok``：用量低于预警线，``allow=True``
    - ``warning``：``spent >= limit * warn_ratio``，仍放行但要提醒，``allow=True``
    - ``exceeded``：``spent >= limit``，``allow=False``

    判定顺序上「超限」先于「预警」，所以 ``warn_ratio=1.0`` 也不会把超限误报成预警。

    脏数据按 0 处理：``spent`` 为 ``None``/``NaN``/``inf``/负数时视为 0，
    ``warn_ratio`` 非法时回落 ``DEFAULT_WARN_RATIO``——统计口径出错不该拦下所有请求。
    """
    spent_value = _coerce_spent(spent)
    ratio = _coerce_warn_ratio(warn_ratio)
    limit_value = _coerce_optional_positive_float(limit)

    if limit_value is None:
        return {
            "allow": True,
            "level": "unlimited",
            "message": "未配置有效预算上限，不做预算拦截",
        }

    if spent_value >= limit_value:
        return {
            "allow": False,
            "level": "exceeded",
            "message": (
                f"AI 预算已超出：已花费 {spent_value:.4f}，上限 {limit_value:.4f}，"
                "本次调用已拦截"
            ),
        }

    if spent_value >= limit_value * ratio:
        used_percent = spent_value / limit_value * 100
        return {
            "allow": True,
            "level": "warning",
            "message": (
                f"AI 预算即将用完：已使用 {used_percent:.1f}%，"
                f"上限 {limit_value:.4f}，请留意消耗速度"
            ),
        }

    return {"allow": True, "level": "ok", "message": None}
