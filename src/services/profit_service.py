"""利润估算服务：基于历史参考价样本判断「买入再转卖」是否值得。

为什么用中位数而不是均值：同关键词的历史价格里常混着配件残件、拆机件、
标错价的诱饵帖，这些离群值会把均值整体拉偏，导致系统对一件普通商品给出
虚高的转卖预期。中位数对这类污染不敏感，是更保守也更诚实的选择。

为什么要有 min_samples：单条或两条样本不足以支撑「市场价」的判断，
样本量不足时宁可明确报告不可估算（estimated=False），也不要给出一个
看起来精确、实际不可信的数字，避免误导决策。

为什么要有 safety_margin：转卖价是估算值，实际成交往往低于中位数
（砍价、成色差异、滞销降价）。默认 0.9 把估算整体打个折扣，让利润判断
偏向保守，减少「账面赚钱、实际亏钱」的情况。
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Iterable

DEFAULT_SAFETY_MARGIN = 0.9
DEFAULT_MIN_SAMPLES = 3


def _to_finite_float(value: Any) -> float | None:
    """把任意输入收敛成有限 float；脏数据统一返回 None。

    bool 单独排除：True/False 出现在价格字段里必然是上游解析出错，
    若被当成 1.0 参与中位数计算会悄悄污染结果。
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _positive_float(value: Any) -> float | None:
    """收敛成有限且为正的 float；非正数、NaN、inf、非数值一律视为无效。"""
    number = _to_finite_float(value)
    if number is None or number <= 0.0:
        return None
    return number


def _clean_reference_prices(reference_prices: Iterable[Any] | None) -> list[float]:
    """清洗参考价样本：丢弃 None、非数值、非有限值、非正数以及 bool。"""
    if reference_prices is None:
        return []
    if isinstance(reference_prices, (str, bytes)):
        return []
    try:
        raw_items = list(reference_prices)
    except TypeError:
        return []

    cleaned: list[float] = []
    for raw in raw_items:
        price = _positive_float(raw)
        if price is not None:
            cleaned.append(price)
    return cleaned


def _non_negative_float(value: Any, fallback: float) -> float:
    """把可选成本类参数收敛为非负有限值，脏数据回落到默认值。"""
    number = _to_finite_float(value)
    if number is None or number < 0.0:
        return fallback
    return number


def estimate_profit(
    *,
    buy_price: float | None,
    reference_prices: list[float] | None,
    shipping_cost: float = 0.0,
    commission_rate: float = 0.0,
    safety_margin: float = DEFAULT_SAFETY_MARGIN,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict:
    """估算转卖利润，返回 ``{"estimated", "resale_price", "net_profit", "profit_margin", "roi", "reason"}``。

    任何一项前置条件不成立（买入价无效、样本不足、转卖价非正）时，
    ``estimated=False``，数值字段为 None，并在 ``reason`` 里用中文写明原因；
    调用方据 ``estimated`` 决定展示还是跳过，不必解析 reason 字符串。
    """
    reference_list = _clean_reference_prices(reference_prices)
    buy = _positive_float(buy_price)

    shipping = _non_negative_float(shipping_cost, 0.0)
    commission_rate_value = _non_negative_float(commission_rate, 0.0)
    safety_margin_value = _non_negative_float(safety_margin, DEFAULT_SAFETY_MARGIN)

    sample_floor = _to_finite_float(min_samples)
    if sample_floor is None or sample_floor < 1.0:
        # 配置非法（None/NaN/inf/非数值/小于 1）时一律回落到保守默认值 3。
        # 不能放宽到 1：那样一处配置笔误就等于放弃「样本量」这道校验，
        # 会让单条甚至零条样本也算出看起来很精确的利润。
        sample_floor = float(DEFAULT_MIN_SAMPLES)
    sample_floor_int = int(sample_floor)

    def _failure(reason: str) -> dict:
        return {
            "estimated": False,
            "resale_price": None,
            "net_profit": None,
            "profit_margin": None,
            "roi": None,
            "reason": reason,
        }

    if buy is None:
        return _failure("买入价无效（缺失、非数值或非正数），无法估算利润。")

    if len(reference_list) < sample_floor_int:
        return _failure(
            f"参考价样本不足：有效样本 {len(reference_list)} 条，"
            f"低于下限 {sample_floor_int} 条，无法可靠估算转卖价。"
        )

    median_price = statistics.median(reference_list)
    resale_price = median_price * safety_margin_value

    # 安全边际为 0（或中位数被极端值压到 0）时转卖价非正，后续比率失去意义
    if resale_price <= 0.0 or not math.isfinite(resale_price):
        return _failure("安全边际系数导致转卖价非正数，无法估算利润。")

    commission = resale_price * commission_rate_value
    net_profit = resale_price - buy - shipping - commission

    # resale_price 已保证为正，这里的除法是安全的；buy 同样为正，roi 无除零风险
    profit_margin = net_profit / resale_price
    roi = net_profit / buy

    return {
        "estimated": True,
        "resale_price": resale_price,
        "net_profit": net_profit,
        "profit_margin": profit_margin,
        "roi": roi,
        "reason": None,
    }
