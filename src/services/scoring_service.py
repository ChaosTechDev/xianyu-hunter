"""加权融合评分服务：把多维度判断压成一个可比、可调参、可解释的分数。

为什么需要它：推荐与否原本散落在 AI 评分、关键词命中、价格对比、风险标签
几处判断里，阈值各自为政，既不好排序也不好解释。这里把它们合成单一分数，
并把「每个维度贡献了多少」显式返回，方便后续调参与排障。

几条关键取舍：

1. 缺失维度不按 0 分参与，而是把剩余维度的权重重新归一化到 1.0。
   AI 调用失败或超时、关键词命中数未知，都属于「信息缺失」，不是「商品差」；
   若按 0 分计入，会把缺失当成负面证据，直接压掉本来不错的商品。
2. 置信度只做 AI 维度的乘数，不单独占一个维度。
   置信度脱离 AI 分没有独立语义，乘法语义也能让低置信度的判断被自然压低；
   而置信度本身缺失时按 1.0 处理，因为「模型没给置信度」不等于「模型不可信」。
3. 风险标签走独立扣分通道，不参与加权。
   风险是相对确定的负面信号，若做成负向维度会被其他高分维度稀释掉。
4. 缺少可用价格依据时整模降级为 AI 0.6 + 关键词 0.4，并置 degraded=True，
   让上游明确知道这次评分少了价格视角。
"""

from __future__ import annotations

import math
from typing import Any, Iterable

# 有参考价时的基础权重：价格优势是独立的第三视角
_WEIGHTS_WITH_PRICE: dict[str, float] = {"ai": 0.5, "keyword": 0.3, "price": 0.2}
# 无参考价时的降级权重：原价格的 0.2 按 AI:关键词 = 5:3 的比例摊回，总和恰好 1.0
_WEIGHTS_WITHOUT_PRICE: dict[str, float] = {"ai": 0.6, "keyword": 0.4}

_KEYWORD_FULL_HIT_SCORE = 3.0  # 命中 3 次即视为关键词维度满分
_PRICE_BEST_RATIO = 0.7  # 价格低到参考价的 70% 即视为价格维度满分
_PRICE_WORST_RATIO = 1.0  # 价格不低于参考价则价格维度得 0 分
_RISK_PENALTY_PER_LABEL = 5.0
_RISK_PENALTY_CAP = 20.0

SCORE_MIN = 0.0
SCORE_MAX = 100.0

DIMENSION_AI = "ai"
DIMENSION_KEYWORD = "keyword"
DIMENSION_PRICE = "price"


def _to_finite_float(value: Any) -> float | None:
    """把任意输入收敛成有限 float；脏数据统一返回 None，而不是抛异常或产出 NaN。

    bool 是 int 的子类，但 True/False 出现在评分、价格、命中数这类字段里
    必然是脏数据，直接判为无效，避免 True 被悄悄当成 1.0 参与计算。
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize_risk_labels(risk_labels: Iterable[Any] | None) -> list[str]:
    """清洗风险标签：去空白、去空值、去重。

    去重是刻意的：同一个风险被重复上报不应叠加扣分，否则上游一次标签复制
    就能把商品直接扣到 0 分。
    """
    if risk_labels is None:
        return []
    if isinstance(risk_labels, str):
        raw_items: list[Any] = [risk_labels]
    elif isinstance(risk_labels, (list, tuple, set, frozenset)):
        raw_items = list(risk_labels)
    else:
        return []

    labels: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        labels.append(text)
    return labels


def _keyword_score(hit_count: float) -> float:
    """关键词维度：0 次得 0 分，>= 3 次满分，中间线性。"""
    if hit_count <= 0.0:
        return SCORE_MIN
    if hit_count >= _KEYWORD_FULL_HIT_SCORE:
        return SCORE_MAX
    return hit_count / _KEYWORD_FULL_HIT_SCORE * SCORE_MAX


def _price_score(price_ratio: float) -> float:
    """价格维度：price_ratio 越小得分越高。

    >= 1（不低于参考价）得 0 分，<= 0.7（明显低于参考价）得满分，中间线性。
    """
    if price_ratio >= _PRICE_WORST_RATIO:
        return SCORE_MIN
    if price_ratio <= _PRICE_BEST_RATIO:
        return SCORE_MAX
    span = _PRICE_WORST_RATIO - _PRICE_BEST_RATIO
    return (_PRICE_WORST_RATIO - price_ratio) / span * SCORE_MAX


def compute_fused_score(
    *,
    ai_score: float | None,
    ai_confidence: float | None,
    keyword_hit_count: int,
    risk_labels: list[str] | None = None,
    has_price_reference: bool = False,
    price_ratio: float | None = None,
) -> dict:
    """把 AI 评分、关键词命中、价格优势与风险标签融合成 0~100 的单一分数。

    返回 ``{"score", "components", "risk_penalty", "degraded", "available_dimensions"}``：

    - ``score``：最终分数，恒为 0.0~100.0 的有限值。
    - ``components``：各参与维度的归一化得分、实际权重与加权贡献；
      缺失维度不出现在其中，看 ``available_dimensions`` 即可知道谁被排除了。
    - ``risk_penalty``：风险标签造成的扣分（正数，上限 20）。
    - ``degraded``：是否在缺少价格依据的降级权重下运行。
    """
    ai_value = _to_finite_float(ai_score)
    if ai_value is not None:
        # AI 分按 0~100 标尺定义，越界值按边界处理而不是丢掉这条信息
        ai_value = _clamp(ai_value, SCORE_MIN, SCORE_MAX)

    confidence = _to_finite_float(ai_confidence)
    if confidence is None:
        confidence = 1.0  # 缺置信度不惩罚：没有该信息不等于模型不可信
    else:
        confidence = _clamp(confidence, 0.0, 1.0)

    keyword_hits = _to_finite_float(keyword_hit_count)
    if keyword_hits is not None:
        keyword_hits = max(keyword_hits, 0.0)

    ratio = _to_finite_float(price_ratio)
    # 非正数 ratio 物理上不可能（价格必然为正），按脏数据处理而不是当作超值机会，
    # 否则一条 0 价样本就能刷出满分推荐。
    usable_ratio = ratio if (ratio is not None and ratio > 0.0) else None
    price_active = bool(has_price_reference) and usable_ratio is not None

    if price_active:
        base_weights = _WEIGHTS_WITH_PRICE
        degraded = False
    else:
        base_weights = _WEIGHTS_WITHOUT_PRICE
        degraded = True
        usable_ratio = None

    # 只有拿得到信息的维度才进入加权，缺失维度整条排除
    dimension_scores: dict[str, float] = {}
    if ai_value is not None:
        dimension_scores[DIMENSION_AI] = ai_value * confidence
    if keyword_hits is not None:
        dimension_scores[DIMENSION_KEYWORD] = _keyword_score(keyword_hits)
    if usable_ratio is not None:
        dimension_scores[DIMENSION_PRICE] = _price_score(usable_ratio)

    # 剩余维度权重重新归一化到 1.0；全缺失时权重和为 0，直接给出 0 分兜底
    total_weight = sum(base_weights[name] for name in dimension_scores)
    weights = {
        name: (base_weights[name] / total_weight if total_weight > 0.0 else 0.0)
        for name in dimension_scores
    }

    components: dict[str, dict[str, Any]] = {}
    weighted_sum = 0.0
    for name, dim_score in dimension_scores.items():
        weight = weights[name]
        weighted = dim_score * weight
        weighted_sum += weighted
        detail: dict[str, Any] = {
            "score": dim_score,
            "weight": weight,
            "weighted": weighted,
        }
        # 补充原始输入，排障时能一眼看出分数是被哪一步拉低/拉高的
        if name == DIMENSION_AI:
            detail["raw_score"] = ai_value
            detail["confidence"] = confidence
        elif name == DIMENSION_KEYWORD:
            detail["hit_count"] = keyword_hits
        else:
            detail["price_ratio"] = usable_ratio
        components[name] = detail

    labels = _normalize_risk_labels(risk_labels)
    risk_penalty = min(len(labels) * _RISK_PENALTY_PER_LABEL, _RISK_PENALTY_CAP)

    score = _clamp(weighted_sum - risk_penalty, SCORE_MIN, SCORE_MAX)
    if not math.isfinite(score):
        # 理论上不会走到这里；保留兜底以免任何脏输入把 NaN 泄漏给上层
        score = SCORE_MIN
    if not math.isfinite(risk_penalty):
        risk_penalty = 0.0

    return {
        "score": score,
        "components": components,
        "risk_penalty": risk_penalty,
        "degraded": degraded,
        "available_dimensions": list(dimension_scores.keys()),
    }
