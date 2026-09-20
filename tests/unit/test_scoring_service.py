"""scoring_service.compute_fused_score 的单元测试。

覆盖点：多维度加权正常路径、置信度乘数语义、价格与关键词维度边界值
（ratio 恰好 0.7 / 1.0、命中恰好 3 次）、风险标签去重与 20 分上限、
分数钳制与下限、None/NaN/inf/bool 等脏输入不产生 NaN，以及缺失维度
触发权重重新归一化（只给 ai_score 时结果应等于 ai_score * confidence）。

全部为纯函数测试，不触数据库、不联网。

浮点约定：加权结果一律用 pytest.approx 比较。形如 100.0 * 0.3 的乘积在
IEEE754 下得到 30.000000000000004，精确相等断言会假失败；只有确凿精确的
值（clamp 边界、risk_penalty 的 5/10/20、由 2 的幂除法得到的权重）才用 ==。
"""

from __future__ import annotations

import math

import pytest

from src.services.scoring_service import compute_fused_score


def test_full_dimensions_normal_path() -> None:
    """三维度齐全且都在优秀区间：0.5*90 + 0.3*100 + 0.2*100 = 95。"""
    result = compute_fused_score(
        ai_score=90.0,
        ai_confidence=1.0,
        keyword_hit_count=3,
        has_price_reference=True,
        price_ratio=0.7,
    )

    assert result["score"] == pytest.approx(95.0)
    assert result["risk_penalty"] == 0.0
    assert result["degraded"] is False
    assert result["available_dimensions"] == ["ai", "keyword", "price"]
    # 权重列直接可见，且必须是归一化后的实际权重
    assert result["components"]["ai"]["weight"] == 0.5
    assert result["components"]["keyword"]["weight"] == 0.3
    assert result["components"]["price"]["weight"] == 0.2
    assert result["components"]["ai"]["raw_score"] == 90.0
    assert result["components"]["ai"]["confidence"] == 1.0
    assert result["components"]["keyword"]["score"] == 100.0
    assert result["components"]["keyword"]["hit_count"] == 3.0
    assert result["components"]["price"]["score"] == 100.0
    assert result["components"]["price"]["price_ratio"] == 0.7
    assert result["components"]["ai"]["weighted"] == pytest.approx(45.0)
    assert result["components"]["keyword"]["weighted"] == pytest.approx(30.0)
    assert result["components"]["price"]["weighted"] == pytest.approx(20.0)


def test_mixed_dimensions_normal_path() -> None:
    """置信度做乘数：ai 维度 = 80*0.5 = 40；命中 1 次 = 100/3；ratio 0.85 = 50。

    加权：40*0.5 + 33.33*0.3 + 50*0.2 = 40。低分来自低置信度而非「AI 说商品差」，
    这正是置信度做乘数的意义。
    """
    result = compute_fused_score(
        ai_score=80.0,
        ai_confidence=0.5,
        keyword_hit_count=1,
        has_price_reference=True,
        price_ratio=0.85,
    )

    assert result["score"] == pytest.approx(40.0)
    assert result["components"]["ai"]["score"] == pytest.approx(40.0)
    assert result["components"]["ai"]["weighted"] == pytest.approx(20.0)
    assert result["components"]["keyword"]["score"] == pytest.approx(100.0 / 3.0)
    assert result["components"]["keyword"]["weighted"] == pytest.approx(10.0)
    assert result["components"]["price"]["score"] == pytest.approx(50.0)
    assert result["components"]["price"]["weighted"] == pytest.approx(10.0)


def test_none_confidence_defaults_to_one() -> None:
    """置信度缺失不惩罚：视为 1.0，AI 维度得分等于原始 AI 分。"""
    result = compute_fused_score(
        ai_score=88.0,
        ai_confidence=None,
        keyword_hit_count=3,
        has_price_reference=True,
        price_ratio=0.5,
    )

    assert result["components"]["ai"]["confidence"] == 1.0
    assert result["components"]["ai"]["score"] == pytest.approx(88.0)
    # 0.5*88 + 0.3*100 + 0.2*100 = 44 + 30 + 20 = 94
    assert result["score"] == pytest.approx(94.0)


def test_price_ratio_boundaries() -> None:
    """价格维度边界：恰好 1.0 得 0 分，恰好 0.7 得满分，超出两端仍是 0/满分。"""

    def price_score(ratio: float) -> float:
        return compute_fused_score(
            ai_score=None,
            ai_confidence=None,
            keyword_hit_count=None,  # type: ignore[arg-type]
            has_price_reference=True,
            price_ratio=ratio,
        )["components"]["price"]["score"]

    assert price_score(1.0) == 0.0
    assert price_score(1.2) == 0.0
    assert price_score(0.7) == 100.0
    assert price_score(0.4) == 100.0
    assert price_score(0.85) == pytest.approx(50.0)
    assert price_score(0.8) == pytest.approx((1.0 - 0.8) / 0.3 * 100.0)


def test_keyword_hit_boundaries() -> None:
    """关键词维度边界：0 次 0 分，恰好 3 次满分，2 次为 2/3 分，超过 3 次仍满分。"""

    def keyword_score(hits: int) -> float:
        return compute_fused_score(
            ai_score=None,
            ai_confidence=None,
            keyword_hit_count=hits,
            has_price_reference=False,
        )["components"]["keyword"]["score"]

    assert keyword_score(0) == 0.0
    assert keyword_score(-2) == 0.0
    assert keyword_score(2) == pytest.approx(200.0 / 3.0)
    assert keyword_score(3) == 100.0
    assert keyword_score(9) == 100.0


def test_only_ai_score_equals_score_times_confidence() -> None:
    """只给 ai_score 不给 keyword：剩余维度权重归一化到 1.0，分数 = ai_score*confidence。"""
    result = compute_fused_score(
        ai_score=80.0,
        ai_confidence=0.9,
        keyword_hit_count=None,  # type: ignore[arg-type]  模拟该维度信息缺失
        has_price_reference=False,
    )

    assert result["score"] == pytest.approx(72.0)
    assert result["components"]["ai"]["weight"] == 1.0
    assert result["components"]["ai"]["weighted"] == pytest.approx(72.0)
    assert list(result["components"].keys()) == ["ai"]
    assert result["available_dimensions"] == ["ai"]


def test_missing_ai_score_reweights_remaining_dimensions() -> None:
    """AI 分缺失时不按 0 分计入，剩余维度按基数权重比例 0.3:0.2 放大为 0.6 / 0.4。

    keyword 满分、ratio 0.85 得 50 分：0.6*100 + 0.4*50 = 80。
    这里刻意断言权重被放大（0.6 / 0.4 而非原始的 0.3 / 0.2），锁住
    「缺失维度让剩余权重归一化」的行为，避免日后被误改成「缺失按 0 分」。
    """
    result = compute_fused_score(
        ai_score=float("nan"),  # NaN 视为缺失，不是 0 分
        ai_confidence=1.0,
        keyword_hit_count=3,
        has_price_reference=True,
        price_ratio=0.85,
    )

    assert set(result["components"].keys()) == {"keyword", "price"}
    assert "ai" not in result["components"]
    assert result["components"]["keyword"]["weight"] == 0.6
    assert result["components"]["price"]["weight"] == 0.4
    assert result["components"]["price"]["score"] == pytest.approx(50.0)
    assert result["score"] == pytest.approx(80.0)


def test_all_dimensions_missing_returns_zero() -> None:
    """全部维度缺失时给出 0 分兜底，权重和无法定义，不产出 NaN。"""
    result = compute_fused_score(
        ai_score=None,
        ai_confidence=None,
        keyword_hit_count=None,  # type: ignore[arg-type]
        has_price_reference=True,  # 声称有参考价却没给 ratio，价格维度同样失效
        price_ratio=None,
    )

    assert result["score"] == 0.0
    assert result["components"] == {}
    assert result["available_dimensions"] == []
    assert result["degraded"] is True


def test_price_dimension_disabled_without_reference() -> None:
    """无参考价则降级为 AI 0.6 + 关键词 0.4 并置 degraded，0.6*90 + 0.4*100 = 94。"""
    result = compute_fused_score(
        ai_score=90.0,
        ai_confidence=1.0,
        keyword_hit_count=3,
        has_price_reference=False,
        price_ratio=0.5,  # 即便传了 ratio，没有参考价声明也不算数
    )

    assert result["degraded"] is True
    assert "price" not in result["components"]
    assert result["components"]["ai"]["weight"] == 0.6
    assert result["components"]["keyword"]["weight"] == 0.4
    assert result["score"] == pytest.approx(94.0)


def test_price_ratio_non_positive_is_dirty() -> None:
    """ratio <= 0 属脏数据（价格恒为正），价格维度失效并降级，不得当作超值满分。"""
    for bad_ratio in (0.0, -0.5, None):
        result = compute_fused_score(
            ai_score=60.0,
            ai_confidence=1.0,
            keyword_hit_count=6,
            has_price_reference=True,
            price_ratio=bad_ratio,
        )

        assert "price" not in result["components"]
        assert result["degraded"] is True
        assert result["score"] == pytest.approx(0.6 * 60.0 + 0.4 * 100.0)


def test_nan_and_inf_inputs_do_not_produce_nan() -> None:
    """NaN/inf 视为缺失：维度被排除，分数仍是 0~100 内的有限值。"""
    cases = [
        {"ai_score": float("nan"), "ai_confidence": 1.0, "keyword_hit_count": 3},
        {"ai_score": float("inf"), "ai_confidence": 1.0, "keyword_hit_count": 3},
        {"ai_score": float("-inf"), "ai_confidence": 1.0, "keyword_hit_count": 3},
        {"ai_score": 70.0, "ai_confidence": float("nan"), "keyword_hit_count": 3},
        {"ai_score": 70.0, "ai_confidence": float("inf"), "keyword_hit_count": 3},
        {"ai_score": 70.0, "ai_confidence": 1.0, "keyword_hit_count": float("inf")},
    ]

    for case in cases:
        result = compute_fused_score(
            has_price_reference=True,
            price_ratio=0.8,
            **case,  # type: ignore[arg-type]
        )
        assert math.isfinite(result["score"]) is True
        assert math.isnan(result["score"]) is False
        assert 0.0 <= result["score"] <= 100.0
        assert all(
            math.isfinite(item["weighted"]) for item in result["components"].values()
        )

    # NaN 置信度按 1.0 处理，AI 维度不被污染
    nan_confidence = compute_fused_score(
        ai_score=70.0,
        ai_confidence=float("nan"),
        keyword_hit_count=3,
        has_price_reference=False,
    )
    assert nan_confidence["components"]["ai"]["confidence"] == 1.0
    assert nan_confidence["score"] == pytest.approx(0.6 * 70.0 + 0.4 * 100.0)

    # inf 命中数视为缺失，剩余维度正常归一化
    inf_hits = compute_fused_score(
        ai_score=70.0,
        ai_confidence=1.0,
        keyword_hit_count=float("inf"),  # type: ignore[arg-type]
        has_price_reference=False,
    )
    assert "keyword" not in inf_hits["components"]
    assert inf_hits["components"]["ai"]["weight"] == 1.0
    assert inf_hits["score"] == pytest.approx(70.0)


def test_bool_inputs_treated_as_dirty() -> None:
    """bool 是 int 子类，但出现在命中数里必是脏数据，不能悄悄当成 1 参与加权。"""
    result = compute_fused_score(
        ai_score=90.0,
        ai_confidence=1.0,
        keyword_hit_count=True,  # type: ignore[arg-type]
        has_price_reference=False,
    )

    assert "keyword" not in result["components"]
    assert result["components"]["ai"]["weight"] == 1.0
    assert result["score"] == pytest.approx(90.0)

    bool_ai = compute_fused_score(
        ai_score=True,  # type: ignore[arg-type]
        ai_confidence=1.0,
        keyword_hit_count=3,
        has_price_reference=False,
    )
    assert "ai" not in bool_ai["components"]
    assert bool_ai["score"] == pytest.approx(100.0)


def test_out_of_range_ai_score_is_clamped() -> None:
    """AI 分越界按 0~100 边界裁剪，而不是丢弃该维度。"""
    too_high = compute_fused_score(
        ai_score=150.0,
        ai_confidence=1.0,
        keyword_hit_count=None,  # type: ignore[arg-type]
        has_price_reference=False,
    )
    assert too_high["score"] == 100.0

    too_low = compute_fused_score(
        ai_score=-20.0,
        ai_confidence=1.0,
        keyword_hit_count=None,  # type: ignore[arg-type]
        has_price_reference=False,
    )
    assert too_low["score"] == 0.0


def test_confidence_above_one_is_clamped() -> None:
    """置信度越界裁剪到 1.0，避免 ai_score * 1.5 抬高总分。"""
    result = compute_fused_score(
        ai_score=60.0,
        ai_confidence=1.5,
        keyword_hit_count=None,  # type: ignore[arg-type]
        has_price_reference=False,
    )

    assert result["components"]["ai"]["confidence"] == 1.0
    assert result["score"] == pytest.approx(60.0)

    negative = compute_fused_score(
        ai_score=60.0,
        ai_confidence=-0.3,
        keyword_hit_count=None,  # type: ignore[arg-type]
        has_price_reference=False,
    )
    assert negative["components"]["ai"]["confidence"] == 0.0
    assert negative["score"] == 0.0


def test_score_clamped_to_100() -> None:
    """各维度满分时总分恰好 100，不越界。"""
    result = compute_fused_score(
        ai_score=100.0,
        ai_confidence=1.0,
        keyword_hit_count=5,
        has_price_reference=True,
        price_ratio=0.5,
    )

    assert result["score"] == 100.0


def test_score_never_below_zero() -> None:
    """四维度全 0 再加 20 分风险扣分时钳制到 0，不产生负分。"""
    result = compute_fused_score(
        ai_score=0.0,
        ai_confidence=1.0,
        keyword_hit_count=0,
        risk_labels=["假货", "翻新", "拆修", "无保修"],
        has_price_reference=True,
        price_ratio=1.0,
    )

    assert result["risk_penalty"] == 20.0
    assert result["score"] == 0.0


def test_risk_penalty_cap_at_20() -> None:
    """10 个不同风险标签按每标签 5 分本应扣 50，上限 20 分封顶。"""
    labels = [f"风险{i}" for i in range(10)]
    result = compute_fused_score(
        ai_score=100.0,
        ai_confidence=1.0,
        keyword_hit_count=3,
        risk_labels=labels,
        has_price_reference=True,
        price_ratio=0.5,
    )

    assert result["risk_penalty"] == 20.0
    assert result["score"] == pytest.approx(80.0)


def test_risk_penalty_under_cap_is_linear() -> None:
    """未触顶时每个标签线性扣 5 分。"""
    one = compute_fused_score(
        ai_score=100.0,
        ai_confidence=1.0,
        keyword_hit_count=3,
        risk_labels=["假货"],
        has_price_reference=True,
        price_ratio=0.5,
    )
    assert one["risk_penalty"] == 5.0
    assert one["score"] == pytest.approx(95.0)

    two = compute_fused_score(
        ai_score=100.0,
        ai_confidence=1.0,
        keyword_hit_count=3,
        risk_labels=["假货", "翻新"],
        has_price_reference=True,
        price_ratio=0.5,
    )
    assert two["risk_penalty"] == 10.0
    assert two["score"] == pytest.approx(90.0)


def test_risk_penalty_dedup_and_cleanup() -> None:
    """重复标签只扣一次；空串/None/纯空白被忽略；裸字符串按单个标签处理。"""
    duplicated = compute_fused_score(
        ai_score=100.0,
        ai_confidence=1.0,
        keyword_hit_count=3,
        risk_labels=["假货", "假货", " 假货 ", "", None, "   "],  # type: ignore[list-item]
        has_price_reference=True,
        price_ratio=0.5,
    )
    assert duplicated["risk_penalty"] == 5.0

    string_label = compute_fused_score(
        ai_score=100.0,
        ai_confidence=1.0,
        keyword_hit_count=3,
        risk_labels="假货",  # type: ignore[arg-type]
        has_price_reference=True,
        price_ratio=0.5,
    )
    assert string_label["risk_penalty"] == 5.0

    no_labels = compute_fused_score(
        ai_score=100.0,
        ai_confidence=1.0,
        keyword_hit_count=3,
        risk_labels=None,
        has_price_reference=True,
        price_ratio=0.5,
    )
    assert no_labels["risk_penalty"] == 0.0


def test_risk_labels_unusable_type_is_ignored() -> None:
    """风险标签传了既非字符串也非可迭代容器的脏类型时按无风险处理，不抛异常。"""
    result = compute_fused_score(
        ai_score=100.0,
        ai_confidence=1.0,
        keyword_hit_count=3,
        risk_labels=12345,  # type: ignore[arg-type]
        has_price_reference=True,
        price_ratio=0.5,
    )

    assert result["risk_penalty"] == 0.0
    assert result["score"] == pytest.approx(100.0)


def test_components_expose_effective_weights_summing_to_one() -> None:
    """参与维度的实际权重之和必须为 1.0，且各维度加权和等于 score + risk_penalty。"""
    cases = [
        {
            "ai_score": 70.0,
            "ai_confidence": 0.8,
            "keyword_hit_count": 2,
            "has_price_reference": True,
            "price_ratio": 0.9,
        },
        {
            "ai_score": 70.0,
            "ai_confidence": 0.8,
            "keyword_hit_count": None,
            "has_price_reference": False,
            "price_ratio": None,
        },
        {
            "ai_score": None,
            "ai_confidence": None,
            "keyword_hit_count": 1,
            "has_price_reference": True,
            "price_ratio": 0.6,
        },
        {
            "ai_score": None,
            "ai_confidence": None,
            "keyword_hit_count": None,
            "has_price_reference": True,
            "price_ratio": 0.9,
        },
    ]

    for case in cases:
        result = compute_fused_score(**case)  # type: ignore[arg-type]
        weights = [item["weight"] for item in result["components"].values()]
        weighted = [item["weighted"] for item in result["components"].values()]

        assert math.isclose(sum(weights), 1.0, rel_tol=1e-9)
        assert sum(weighted) == pytest.approx(result["score"] + result["risk_penalty"])
        # 每个维度的 weighted 必须等于 score * weight，便于直接对账
        for item in result["components"].values():
            assert item["weighted"] == pytest.approx(item["score"] * item["weight"])


def test_weight_renormalization_matches_documented_ratio() -> None:
    """只剩两个维度时，权重严格按基数比例放大；只剩一个维度时权重恒为 1.0。

    有价格参考时基数为 ai 0.5 / keyword 0.3 / price 0.2：
    去 ai 后为 0.6 / 0.4，去 keyword 后为 0.5/0.7 与 0.2/0.7。
    """
    no_ai = compute_fused_score(
        ai_score=None,
        ai_confidence=None,
        keyword_hit_count=3,
        has_price_reference=True,
        price_ratio=0.7,
    )
    assert no_ai["components"]["keyword"]["weight"] == pytest.approx(0.3 / 0.5)
    assert no_ai["components"]["price"]["weight"] == pytest.approx(0.2 / 0.5)

    no_keyword = compute_fused_score(
        ai_score=80.0,
        ai_confidence=1.0,
        keyword_hit_count=None,  # type: ignore[arg-type]
        has_price_reference=True,
        price_ratio=0.7,
    )
    assert no_keyword["components"]["ai"]["weight"] == pytest.approx(0.5 / 0.7)
    assert no_keyword["components"]["price"]["weight"] == pytest.approx(0.2 / 0.7)
    assert no_keyword["score"] == pytest.approx(80.0 * (0.5 / 0.7) + 100.0 * (0.2 / 0.7))

    only_price = compute_fused_score(
        ai_score=None,
        ai_confidence=None,
        keyword_hit_count=None,  # type: ignore[arg-type]
        has_price_reference=True,
        price_ratio=0.7,
    )
    assert only_price["components"]["price"]["weight"] == 1.0
    assert only_price["score"] == pytest.approx(100.0)


def test_missing_ai_is_not_penalized_as_zero() -> None:
    """回归护栏：AI 缺失时分数不得等于「AI 记 0 分」的结果。

    若把缺失当 0 分，0*0.5 + 100*0.3 + 100*0.2 = 50；正确做法是重归一化后
    关键词与价格维度直接决定分数，结果应接近 100 而非 50。
    """
    missing_ai = compute_fused_score(
        ai_score=None,
        ai_confidence=None,
        keyword_hit_count=5,
        has_price_reference=True,
        price_ratio=0.5,
    )
    zero_ai = compute_fused_score(
        ai_score=0.0,
        ai_confidence=1.0,
        keyword_hit_count=5,
        has_price_reference=True,
        price_ratio=0.5,
    )

    assert missing_ai["score"] == pytest.approx(100.0)
    assert zero_ai["score"] == pytest.approx(50.0)
    assert missing_ai["score"] > zero_ai["score"]
