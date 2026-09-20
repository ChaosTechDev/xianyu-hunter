"""关注商品的状态机与通知决策（纯函数，无 IO）。

把「什么时候判定死亡」「什么时候该发通知」这类容易出错、又最需要测试的
判断逻辑从 ``watch_service`` 里抽出来，做成不依赖数据库、不依赖时钟的纯函数，
便于单元测试穷举边界。

三个核心设计：

1. **粘性死亡（sticky dead）**
   一旦判定死亡，``dead`` 置 1 后**永不自动回退**。理由是闲鱼的风控会导致
   采集结果抖动（这一轮返回、下一轮因风控缺项），如果状态跟着抖动，就会
   反复产生「下架→重新上架→下架」的假事件。要复活必须显式重置。

2. **边沿触发（edge-triggered）**
   通知只在状态**跃迁的瞬间**发出一次，而不是每个采集轮次都发。
   ``was_dead=False -> dead=True`` 才通知；持续 dead 不再重复通知。

3. **保守判活**
   拿不到明确死亡信号时判活。误杀在售商品的代价（用户错过好货）
   高于漏掉一个死链。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

#: 判定死亡的原因标签
REASON_SOLD = "sold_out"
REASON_DELISTED = "delisted"
REASON_DELETED = "deleted"
REASON_MISSING = "missing"

DEAD_REASON_LABELS = {
    REASON_SOLD: "已售出",
    REASON_DELISTED: "已下架",
    REASON_DELETED: "已删除",
    REASON_MISSING: "连续采集缺失",
}


@dataclass(frozen=True)
class DeathDecision:
    """一次采集结果的死亡判定结论。"""

    #: 是否判定死亡
    dead: bool
    #: 死亡原因（``dead`` 为 False 时为 None）
    reason: Optional[str]
    #: 用于展示/排查的说明
    detail: str


def evaluate_death_signal(
    *,
    alive_signal: Optional[bool],
    missing_runs: int,
    delisted_missing_runs: int = 3,
    explicit_reason: Optional[str] = None,
) -> DeathDecision:
    """根据采集信号判断商品是否已死亡。

    优先级：显式死亡信号（详情接口 ret 码 / 收藏项状态）> 连续缺失兜底。

    :param alive_signal: 显式探活结果。``False`` 表示拿到明确死亡信号；
                         ``True`` 表示确认存活；``None`` 表示未探活或探测失败
                         （**此时绝不据此判死**）。
    :param missing_runs: 连续未在完整采集结果中出现的轮次（含本轮）。
    :param delisted_missing_runs: 连续缺失多少轮判定下架。
    :param explicit_reason: 显式死亡信号对应的原因标签。

    保守原则：``alive_signal`` 为 ``None`` 时，只有连续缺失达到阈值才判死；
    缺失本身是弱信号，所以需要多轮确认。
    """
    # 显式死亡信号最可信，立刻采信
    if alive_signal is False:
        reason = explicit_reason or REASON_DELISTED
        return DeathDecision(
            dead=True,
            reason=reason,
            detail=f"检测到明确的死亡信号：{DEAD_REASON_LABELS.get(reason, reason)}",
        )

    # 显式确认存活 -> 不判死，交给调用方清零缺失计数
    if alive_signal is True:
        return DeathDecision(dead=False, reason=None, detail="确认存活")

    # 未探活：仅靠连续缺失兜底，需达阈值
    if missing_runs >= delisted_missing_runs:
        return DeathDecision(
            dead=True,
            reason=REASON_MISSING,
            detail=f"连续 {missing_runs} 次完整采集未发现该商品，判定已下架",
        )

    return DeathDecision(
        dead=False,
        reason=None,
        detail=f"连续缺失 {missing_runs}/{delisted_missing_runs} 次，尚未判定",
    )


def resolve_sticky_death(
    *,
    was_dead: bool,
    decision: DeathDecision,
) -> DeathDecision:
    """把新判定结果与历史粘性状态合并。

    粘性语义：已经死亡的商品，**只有显式确认存活**才能复活；
    采集缺失、探活失败、未探活都不足以推翻死亡结论。
    """
    if not was_dead:
        return decision

    # 已经死亡：只有决定性的存活信号能翻案
    if decision.dead:
        return decision

    # decision 为「活」时，区分是「确认存活」还是「只是没探到死亡」
    if decision.detail == "确认存活":
        return DeathDecision(dead=False, reason=None, detail="确认存活，状态已恢复")

    # 其余情况维持死亡状态（粘性）
    return DeathDecision(
        dead=True,
        reason=REASON_MISSING,
        detail="此前已判定死亡，本次未获得存活证据，维持死亡状态",
    )


def should_emit_edge(*, was_dead: bool, dead: bool) -> bool:
    """边沿触发判定：是否应当发出一次状态变更通知。

    只在 ``False -> True``（死亡）或 ``True -> False``（复活）的跃迁瞬间返回 True。
    持续同一状态不重复通知，避免每个采集轮次都骚扰用户。
    """
    return was_dead != dead


def is_muted(*, muted_until: Optional[str], now: Optional[datetime] = None) -> bool:
    """判断当前是否处于静音期。

    ``muted_until`` 为 ISO8601 字符串，到期后自动恢复提醒（无需人工解除）。
    解析失败时按「未静音」处理 —— 宁可多发一条通知，也不要因为脏数据永久静音。
    """
    if not muted_until:
        return False
    reference = now or datetime.now()
    try:
        deadline = datetime.fromisoformat(str(muted_until))
    except (ValueError, TypeError):
        return False
    # 容忍有时区/无时区混用：无法比较时按未静音处理
    try:
        return deadline > reference
    except TypeError:
        return False


def resolve_notify_flag(
    *,
    event_type: str,
    watch: dict,
    muted_until: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """决定某类事件是否应当推送通知。

    两道闸门：
    1. **分类型开关** —— 用户可按事件类型关闭通知，但事件本身仍会入库留档。
       这样「静音但仍留档」成为可能：不打扰用户，历史记录不丢。
    2. **静音延期** —— 静音期内一律不推送，到期自动恢复。

    未知事件类型默认放行，避免新增事件类型时被静默吞掉。
    """
    if is_muted(muted_until=muted_until, now=now):
        return False

    flag_key = f"notify_{event_type}"
    if flag_key in watch:
        return bool(watch.get(flag_key, True))

    # 售出事件优先用独立开关，回退到通用开关
    if event_type == REASON_SOLD:
        if "notify_on_sold" in watch:
            return bool(watch.get("notify_on_sold", True))
        return bool(watch.get("notify_delisted", True))

    return True


def detect_reduce_price_delta(
    *,
    current_reduce: int,
    previous_reduce: int,
) -> int:
    """计算闲鱼原生「收藏后降价」的增量。

    闲鱼详情/收藏接口会返回平台侧记录的累计降价额 ``reducePrice``。
    它比跨次比价更强的一点是：**能覆盖首次观测之前就已发生的降价**。
    跨次比价只能看到「我们开始监控之后」的降价，而捡漏场景里，
    商品在首次被发现时往往已经降过一轮，这部分增量会被完全漏掉。

    只在增量 > 0 时返回正数，避免重复通知同一个降价。
    """
    try:
        cur = int(current_reduce or 0)
        prev = int(previous_reduce or 0)
    except (TypeError, ValueError):
        return 0
    delta = cur - prev
    return delta if delta > 0 else 0
