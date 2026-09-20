"""卖家信用评分：把散落的卖家指标合成一个可解释的信用分与风险信号。

**为什么需要它**

项目提示词（``prompts/macbook_criteria.txt`` 第 7 条）把「卖家信用等级必须是
*卖家信用极好*」列为**硬性一票否决条件**，但这条规则此前只存在于提示词里，
由模型自己判断。模型看不到原始字段、也无法跨商品比较，判定结果不可复现。

本模块把同样的判断**确定性地**做一遍：输入是采集到的卖家字段，
输出是分数 + 命中/缺失的因子清单 + 明确的否决标志。这样：

- 同一份数据永远得到同一个结论（可测试、可回归）；
- 每个扣分点都能溯源到具体字段（可解释，不用猜模型为什么否决）；
- 模型漏判时有独立兜底（两者不一致时可交叉验证）。

**已知的数据不确定性（重要）**

``卖家信用等级`` 的取值来自闲鱼 ``ylzTags`` 的 ``text`` 字段，实测形如
「卖家信用极好」。但**完整的等级枚举无法离线验证**，因此本模块：

- 按关键词包含关系分级（极好 > 很好 > 良好 > 一般），不依赖精确字符串相等；
- 遇到无法识别的等级文本时**不判为差**，而是标记为未知并给中性分——
  把「没见过的写法」当成负面证据，会在平台改文案时误杀全部卖家。
"""
from __future__ import annotations

import math
import re
from typing import Any

#: 信用等级关键词到得分的映射（按包含关系匹配，顺序即优先级）
CREDIT_LEVEL_SCORES: tuple[tuple[str, float], ...] = (
    ("极好", 100.0),
    ("很好", 85.0),
    ("良好", 70.0),
    ("一般", 40.0),
)
#: 无法识别的等级文本给中性分，不给差评
CREDIT_UNKNOWN_SCORE = 60.0

#: 各因子在总分中的权重。合计 1.0。
FACTOR_WEIGHTS: dict[str, float] = {
    "credit_level": 0.40,
    "positive_rate": 0.25,
    "rating_volume": 0.20,
    "account_age": 0.15,
}

#: 风险信号阈值
#: 在售/已售商品数超过此值视为商家（贩子）信号。个人玩家即便长期出二手，
#: 在售数量也极少过百——这个数是经验值，不是平台文档给出的官方阈值。
DEALER_LISTING_THRESHOLD = 100
#: 好评率低于此值触发风险信号
LOW_POSITIVE_RATE_THRESHOLD = 0.95

LEVEL_EXCELLENT = "excellent"
LEVEL_GOOD = "good"
LEVEL_FAIR = "fair"
LEVEL_POOR = "poor"
LEVEL_UNKNOWN = "unknown"

LEVEL_LABELS = {
    LEVEL_EXCELLENT: "信用优秀",
    LEVEL_GOOD: "信用良好",
    LEVEL_FAIR: "信用一般",
    LEVEL_POOR: "信用偏低",
    LEVEL_UNKNOWN: "信用未知",
}


def _to_finite_float(value: Any) -> float | None:
    """收敛成有限 float；bool 与脏数据一律返回 None。

    bool 单独排除：``True`` 是 ``int`` 子类，混进计数或比率字段会被当成 1.0。
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse_count(value: Any) -> float | None:
    """解析「1.2万」「350」「１２３」这类计数文本。

    闲鱼把这些数字以展示文本下发，带千分位、「万」后缀，且可能含全角数字。
    解析失败返回 ``None``（表示未知），**不返回 0**——0 和「未知」在信用评估里
    含义完全相反，混用会让缺数据的卖家被误判为「零交易新号」。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _to_finite_float(value)

    text = str(value).strip()
    if not text:
        return None
    # 全角数字转半角，全角句点转半角
    text = text.translate(str.maketrans("０１２３４５６７８９．", "0123456789."))
    text = text.replace(",", "").replace("，", "").replace(" ", "")

    multiplier = 1.0
    if text.endswith("万"):
        multiplier = 10000.0
        text = text[:-1]
    elif text.endswith("千"):
        multiplier = 1000.0
        text = text[:-1]
    elif text.endswith("+"):
        text = text[:-1]

    if not text:
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number * multiplier


def parse_positive_rate(value: Any) -> float | None:
    """解析好评率，统一归一化到 0~1。

    同时接受 ``"99.5%"``、``"0.995"``、``99.5`` 三种常见写法：
    - 带 ``%`` → 直接除以 100
    - 无 ``%`` 且 > 1 → 视为百分数（如 ``99.5``）
    - 无 ``%`` 且 <= 1 → 视为小数比率（如 ``0.995``）

    结果钳制到 [0, 1]；无法解析返回 ``None``。
    """
    if value is None or isinstance(value, bool):
        return None
    has_percent_sign = "%" in str(value)
    number = parse_count(str(value).replace("%", ""))
    if number is None:
        return None
    if has_percent_sign or number > 1.0:
        number = number / 100.0
    return max(0.0, min(1.0, number))


def _score_credit_level(text: Any) -> tuple[float | None, str | None, bool]:
    """按关键词包含关系给信用等级打分。

    返回 ``(分数, 命中的关键词, 是否为无法识别的写法)``。

    第三个返回值用来把「没见过的等级文案」与「已知的低等级」区分开：
    平台改动文案时会出现前者，把它当成低信用会**误杀全部卖家**。
    """
    if text is None:
        return None, None, False
    normalized = str(text).strip()
    if not normalized:
        return None, None, False
    for keyword, score in CREDIT_LEVEL_SCORES:
        if keyword in normalized:
            return score, keyword, False
    # 见到「暂无」「未知」这类占位文本视为无数据，而非低信用
    if any(token in normalized for token in ("暂无", "未知", "无", "unknown", "-")):
        return None, None, False
    return CREDIT_UNKNOWN_SCORE, normalized, True


def _score_rating_volume(count: float | None) -> float | None:
    """评价样本量得分：样本越多越可信，但边际收益递减。

    ``>= 50`` 条即认为样本充分（满分）。用对数尺度而非线性，因为
    「5 条 -> 50 条」对可信度的提升远大于「500 条 -> 5000 条」。
    """
    if count is None:
        return None
    if count <= 0:
        return 0.0
    if count >= 50:
        return 100.0
    return math.log10(count + 1) / math.log10(51) * 100.0


def _score_account_age(duration_text: Any) -> float | None:
    """从注册时长文本推断账号年龄得分。

    闲鱼下发形如「来闲鱼 3 年 2 个月」「注册 8 年」。越长越可信，
    ``>= 5`` 年满分。解析不出任何数字则返回 ``None``（未知），不猜测。
    """
    if duration_text is None:
        return None
    text = str(duration_text).strip()
    if not text:
        return None
    years = 0.0
    matched = False
    year_match = re.search(r"(\d+(?:\.\d+)?)\s*年", text)
    if year_match:
        years = float(year_match.group(1))
        matched = True
    month_match = re.search(r"(\d+(?:\.\d+)?)\s*个?月", text)
    if month_match:
        years += float(month_match.group(1)) / 12.0
        matched = True
    if not matched:
        bare = re.search(r"(\d+(?:\.\d+)?)", text)
        if bare:
            # 只有裸数字（如「8」）时按年解释——这是常见下发形式
            years = float(bare.group(1))
            matched = True
    if not matched:
        return None
    if years <= 0:
        return 0.0
    return min(100.0, years / 5.0 * 100.0)


def score_seller(seller_info: dict | None) -> dict:
    """给卖家信用打分。

    返回::

        {
          "score": float|None,     # 0~100；一个因子都没拿到时为 None（≠ 0 分）
          "level": str,            # excellent/good/fair/poor/unknown
          "level_label": str,      # 中文标签
          "vetoed": bool,          # 是否触发硬性否决（信用等级不达标）
          "veto_reason": str|None,
          "factors": {名称: {"score": float|None, "weight": float, "weighted": float|None, "raw": Any}},
          "available_factors": [str],
          "risk_flags": [str],     # 中文风险描述
          "unknown_factors": [str],
        }

    **权重归一化**：只对拿得到数据的因子加权，缺失因子整条排除并把剩余权重
    重新归一化。理由与 ``scoring_service`` 一致——「没拿到数据」不是「数据差」，
    按 0 分计入会把缺字段的卖家全部压低。

    **硬性否决**：信用等级能识别且**低于 ``很好``** 时置 ``vetoed=True``。
    提示词要求必须是「极好」，但把「很好」也一并否决会过于激进（实测很多
    真实个人卖家是「很好」），因此这里只在低于「很好」时否决，并在
    ``level`` 上如实反映真实档位，让调用方自行决定是否更严格。
    拿不到等级数据时**不否决**——缺证据不等于有罪。
    """
    info = seller_info if isinstance(seller_info, dict) else {}

    level_score, level_keyword, level_unrecognized = _score_credit_level(
        info.get("卖家信用等级")
    )
    rate_score = None
    positive_rate = parse_positive_rate(
        info.get("作为卖家的好评率") or info.get("好评率")
    )
    if positive_rate is not None:
        # 好评率映射：>= 0.99 满分，<= 0.90 得 0 分，中间线性
        if positive_rate >= 0.99:
            rate_score = 100.0
        elif positive_rate <= 0.90:
            rate_score = 0.0
        else:
            rate_score = (positive_rate - 0.90) / 0.09 * 100.0

    volume_score = _score_rating_volume(
        parse_count(info.get("卖家收到的评价总数"))
    )
    age_score = _score_account_age(info.get("卖家注册时长"))

    raw_values = {
        "credit_level": info.get("卖家信用等级"),
        "positive_rate": info.get("作为卖家的好评率") or info.get("好评率"),
        "rating_volume": info.get("卖家收到的评价总数"),
        "account_age": info.get("卖家注册时长"),
    }
    factor_scores = {
        "credit_level": level_score,
        "positive_rate": rate_score,
        "rating_volume": volume_score,
        "account_age": age_score,
    }

    available = {name: s for name, s in factor_scores.items() if s is not None}
    total_weight = sum(FACTOR_WEIGHTS[name] for name in available)
    factors: dict[str, dict] = {}
    weighted_sum = 0.0
    for name, dim_score in factor_scores.items():
        entry: dict[str, Any] = {
            "score": dim_score,
            "weight": FACTOR_WEIGHTS[name],
            "raw": raw_values[name],
        }
        if dim_score is not None and total_weight > 0:
            weight = FACTOR_WEIGHTS[name] / total_weight
            weighted = dim_score * weight
            weighted_sum += weighted
            entry["normalized_weight"] = weight
            entry["weighted"] = weighted
        else:
            entry["normalized_weight"] = 0.0
            entry["weighted"] = None
        factors[name] = entry

    # score 为 None 表示**一个因子都没拿到**。此时绝不能给 0.0——那等于断言
    # 「这个卖家信用为零」，而事实是「我们对这个卖家一无所知」。调用方应据
    # score is None 决定是否展示信用分。
    score = weighted_sum if available else None

    # 等级档位判定。无法识别的写法单独归一档，避免被误标为「信用偏低」。
    if level_score is None:
        level = LEVEL_UNKNOWN
    elif level_unrecognized:
        level = LEVEL_UNKNOWN
    elif level_score >= CREDIT_LEVEL_SCORES[0][1]:
        level = LEVEL_EXCELLENT
    elif level_score >= CREDIT_LEVEL_SCORES[1][1]:
        level = LEVEL_GOOD
    elif level_score >= CREDIT_LEVEL_SCORES[2][1]:
        level = LEVEL_FAIR
    else:
        level = LEVEL_POOR

    # 硬性否决：等级可识别且低于「很好」。无法识别的写法不否决（缺证据不等于有罪）。
    vetoed = False
    veto_reason = None
    if level_score is not None and level_keyword is not None and not level_unrecognized:
        if level_score < CREDIT_LEVEL_SCORES[1][1] and level_score != CREDIT_UNKNOWN_SCORE:
            vetoed = True
            veto_reason = f"卖家信用等级为「{raw_values['credit_level']}」，未达到要求"

    risk_flags: list[str] = []
    listing_count = parse_count(info.get("卖家在售/已售商品数"))
    if listing_count is not None and listing_count > DEALER_LISTING_THRESHOLD:
        risk_flags.append(
            f"在售/已售商品数异常偏高（{int(listing_count)} 件），疑似商家或贩子"
        )
    if positive_rate is not None and positive_rate < LOW_POSITIVE_RATE_THRESHOLD:
        risk_flags.append(f"好评率偏低（{positive_rate * 100:.1f}%）")
    if level == LEVEL_POOR:
        risk_flags.append("信用等级偏低")
    if vetoed:
        risk_flags.append("信用等级触发硬性条件不满足")

    unknown_factors = [name for name, s in factor_scores.items() if s is None]

    return {
        "score": score,
        "level": level,
        "level_label": LEVEL_LABELS[level],
        "vetoed": vetoed,
        "veto_reason": veto_reason,
        "factors": factors,
        "available_factors": list(available.keys()),
        "risk_flags": risk_flags,
        "unknown_factors": unknown_factors,
    }
