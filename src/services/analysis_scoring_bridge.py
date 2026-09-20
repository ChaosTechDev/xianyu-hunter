"""把 AI 的定性结论桥接成加权融合评分所需的定量输入。

**为什么需要这一层**

:mod:`src.services.scoring_service` 需要 ``ai_score``（0~100）与 ``ai_confidence``，
但项目的提示词（``prompts/base_prompt.txt`` + ``prompts/macbook_criteria.txt``）
让模型输出的**不是数字，而是状态词**：

- ``FAIL`` —— 明确不符合标准（如非 M1 芯片、有拆修史、仅限自提）
- ``NEEDS_MANUAL_CHECK`` —— 关键信息缺失，需人工确认（**不等于不符合**）
- 其余值（如 ``PASS``、``OK``、描述性文字）—— 未见明确否决

直接要求模型改吐数字是最省事的做法，但那会破坏既有提示词与用户自定义提示词
的兼容性，且模型给出的"打分"往往无校准、不可复现。因此这里做**确定性映射**：
状态词 -> 分数，映射规则可读、可测、可解释，不让模型引入额外的不确定性。

**核心取舍**

1. ``NEEDS_MANUAL_CHECK`` **不是** FAIL。提示词明确规定"信息缺失不直接导致否决"，
   所以它既不能记 0 分（那等于惩罚"没写"），也不能记满分（那等于假装已确认）。
   折中记 50 分，并在置信度里扣减——这正是 :class:`~src.services.scoring_service`
   中 ``ai_confidence`` 乘数的用途。
2. **一票否决**必须硬生效。提示词列了硬性原则（型号、信用、邮寄、电池、维修史），
   任一 FAIL 就必须压到不及格区间，而不能被其他维度的满分平均回来。
3. 没有可识别的 ``criteria_analysis`` 时不编造分数，返回 ``None``，
   让评分服务走"AI 维度缺失"的降级路径——这比伪造一个数字诚实得多。
"""
from __future__ import annotations

import re
from typing import Any

from src.services.scoring_service import compute_fused_score

#: 状态词到分数的确定性映射
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"
STATUS_PASS = "pass"

STATUS_SCORES = {
    STATUS_FAIL: 0.0,
    STATUS_UNKNOWN: 50.0,
    STATUS_PASS: 100.0,
}

#: 一票否决生效后的 AI 分上限。低于"应当直接拒绝"的直觉区间，
#: 同时保留少量分数以便排序时区分"多个候选都被否决"的优劣。
FAIL_CEILING = 25.0

#: 触发一票否决时，**最终融合分**的硬上限。
#:
#: 为什么必须在融合之后再加一道上限：``compute_fused_score`` 是加权平均，
#: 若只在 AI 维度压分，关键词满分（0.3 权重）与价格满分（0.2 权重）会把
#: 被否决的商品重新拉回及格线（实测 25 分的 AI 维度会被拉到 56 分）。
#: 硬性否决必须能在最终分数上生效，否则"一票否决"在排序里形同虚设。
VETO_SCORE_CAP = 30.0

#: 可识别的缺失/待查状态词（大小写不敏感，容忍模型自由发挥的描述）
_UNKNOWN_TOKENS = (
    "needs_manual_check",
    "needs_manual_review",
    "manual_check",
    "unknown",
    "uncertain",
    "unclear",
    "missing",
    "not_mentioned",
    "insufficient",
)
_FAIL_TOKENS = ("fail", "failed", "reject", "rejected", "not_pass", "does_not_meet")

#: 判定为"有实质结论"的最低状态词长度。短于此长度的内容不足以支撑结论。
_MIN_STATUS_LEN = 3


def _classify_status(raw: Any) -> str:
    """把模型给出的任意状态文本归入三档。

    判定顺序很重要：先看 FAIL，再看缺失，最后才归为 POSS。
    因为 ``"NEEDS_MANUAL_CHECK"`` 里不含 fail，而 ``"FAIL_NEEDS_REVIEW"``
    这类混写必须优先识别为否决（否决是更严重的信号，宁可从严）。
    """
    if raw is None:
        return STATUS_UNKNOWN
    if isinstance(raw, bool):
        # 模型偶尔会把 status 直接写成布尔，True 视为通过
        return STATUS_PASS if raw else STATUS_FAIL
    text = str(raw).strip().lower()
    if not text:
        return STATUS_UNKNOWN
    # 归一化分隔符：'Needs Manual Check' 与 'NEEDS_MANUAL_CHECK' 等价
    normalized = re.sub(r"[\s\-]+", "_", text)
    if any(token in normalized for token in _FAIL_TOKENS):
        return STATUS_FAIL
    if any(token in normalized for token in _UNKNOWN_TOKENS):
        return STATUS_UNKNOWN
    return STATUS_PASS


def _iter_criterion_statuses(criteria_analysis: dict) -> list[tuple[str, Any]]:
    """取出各评估项的状态。

    只看每个**顶层评估项**的 ``status`` 字段。``seller_type`` 下面还有
    ``analysis_details`` 等嵌套结构，那些没有 ``status``，天然不会被收进来——
    这是刻意的：嵌套字段是论证过程，顶层 ``status`` 才是结论。
    """
    found: list[tuple[str, Any]] = []
    for name, detail in criteria_analysis.items():
        if not isinstance(detail, dict):
            continue
        if "status" in detail:
            found.append((str(name), detail.get("status")))
    return found


def derive_ai_score(ai_analysis: dict | None) -> dict:
    """从 AI 分析结果推导 ``ai_score`` / ``ai_confidence`` 及其解释。

    返回 ``{"ai_score", "ai_confidence", "statuses", "failures", "unknowns"}``；
    当拿不到可用的 ``criteria_analysis`` 时 ``ai_score`` 与 ``ai_confidence``
    均为 ``None``（表示"未获得 AI 维度信息"，交给评分服务走降级路径）。
    """
    empty = {
        "ai_score": None,
        "ai_confidence": None,
        "statuses": {},
        "failures": [],
        "unknowns": [],
    }
    if not isinstance(ai_analysis, dict):
        return empty
    criteria = ai_analysis.get("criteria_analysis")
    if not isinstance(criteria, dict) or not criteria:
        return empty
    statuses = _iter_criterion_statuses(criteria)
    if not statuses:
        return empty

    scores: list[float] = []
    failures: list[str] = []
    unknowns: list[str] = []
    resolved = 0
    for name, raw_status in statuses:
        bucket = _classify_status(raw_status)
        scores.append(STATUS_SCORES[bucket])
        if bucket == STATUS_FAIL:
            failures.append(name)
        elif bucket == STATUS_UNKNOWN:
            unknowns.append(name)
        else:
            resolved += 1

    ai_score = sum(scores) / len(scores)

    # 一票否决硬生效：任一 FAIL 就把分数压到否决区间，不允许被其他项平均回来
    if failures:
        ai_score = min(ai_score, FAIL_CEILING)

    # 置信度 = 有实质结论的评估项占比。缺失项越多，这个 AI 判断越不可信。
    # 下限 0.05 而非 0：完全靠"待查"得出的结论仍有极弱的信号价值，
    # 直接归零会让它与"完全没做 AI 分析"混为一谈。
    ai_confidence = max(resolved / len(statuses), 0.05) if statuses else None

    return {
        "ai_score": ai_score,
        "ai_confidence": ai_confidence,
        "statuses": {name: _classify_status(raw) for name, raw in statuses},
        "failures": failures,
        "unknowns": unknowns,
    }


def compute_analysis_score(
    *,
    ai_analysis: dict | None,
    keyword_hit_count: int = 0,
    reference_price: float | None = None,
    current_price: float | None = None,
    risk_labels: list[str] | None = None,
) -> dict:
    """把一份 AI 分析结果折算成融合评分。

    价格维度只在**同时**拿到有效的当前价与参考价、且参考价为正时才启用；
    否则走降级权重，并通过 ``degraded`` 明确告知上游"这次评分少了价格视角"。

    风险标签优先取调用方传入的 ``risk_labels``，回退到 AI 结果里的 ``risk_tags``
    （提示词定义的字段名）。
    """
    derived = derive_ai_score(ai_analysis)

    labels = risk_labels
    if labels is None and isinstance(ai_analysis, dict):
        raw_tags = ai_analysis.get("risk_tags")
        labels = raw_tags if isinstance(raw_tags, list) else None

    ratio: float | None = None
    has_reference = False
    if reference_price is not None and current_price is not None:
        try:
            ref = float(reference_price)
            cur = float(current_price)
        except (TypeError, ValueError):
            ref = cur = 0.0
        if ref > 0.0:
            has_reference = True
            ratio = cur / ref

    result = compute_fused_score(
        ai_score=derived["ai_score"],
        ai_confidence=derived["ai_confidence"],
        keyword_hit_count=keyword_hit_count,
        risk_labels=labels,
        has_price_reference=has_reference,
        price_ratio=ratio,
    )
    # 一票否决必须在最终分数上生效：加权平均会把被否决的 AI 分拉回及格线，
    # 这里补一道硬上限，保证"硬性原则不满足"的商品始终排在通过者之后。
    if derived["failures"]:
        result["score"] = min(result["score"], VETO_SCORE_CAP)
        result["vetoed"] = True
    else:
        result["vetoed"] = False
    result["ai_failures"] = derived["failures"]
    result["ai_unknowns"] = derived["unknowns"]
    result["criterion_statuses"] = derived["statuses"]
    return result
