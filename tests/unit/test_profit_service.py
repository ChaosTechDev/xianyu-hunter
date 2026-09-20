"""profit_service.estimate_profit 的单元测试。

覆盖点：中位数（而非均值）抗离群值、参考价样本量下限、买入价无效与
buy_price=0 不崩、安全边际与佣金/运费的扣除顺序、利润率与 ROI 口径、
以及 None/NaN/inf/负数/字符串/bool 等脏样本的过滤行为。

全部为纯函数测试，不触数据库、不联网。
"""

from __future__ import annotations

import math

import pytest

from src.services.profit_service import estimate_profit


def _assert_not_estimated(result: dict, keyword: str) -> None:
    """统一校验「不可估算」的返回结构：所有数值位为 None 且原因含指定中文关键词。"""
    assert result["estimated"] is False
    assert result["resale_price"] is None
    assert result["net_profit"] is None
    assert result["profit_margin"] is None
    assert result["roi"] is None
    assert isinstance(result["reason"], str)
    assert keyword in result["reason"]


def test_normal_path_with_median_and_costs() -> None:
    """正常路径：中位数 8150（两个中间值 8100/8200 的均值），全套成本都参与扣减。

    样本 [8000, 8100, 8200, 15000] 的均值是 9825，中位数是 8150。这里刻意放入
    一个 15000 的离群值，验证结果是按中位数走的（8150*0.9=7335，而非 8842.5）。
    """
    result = estimate_profit(
        buy_price=5000.0,
        reference_prices=[8000.0, 8100.0, 8200.0, 15000.0],
        shipping_cost=20.0,
        commission_rate=0.01,
        safety_margin=0.9,
        min_samples=3,
    )

    assert result["estimated"] is True
    assert result["reason"] is None

    resale = 8150.0 * 0.9  # 7335.0
    commission = resale * 0.01  # 73.35
    net = resale - 5000.0 - 20.0 - commission  # 2241.65

    assert result["resale_price"] == pytest.approx(7335.0)
    assert result["net_profit"] == pytest.approx(net)
    assert result["net_profit"] == pytest.approx(2241.65)
    assert result["profit_margin"] == pytest.approx(net / resale)
    assert result["roi"] == pytest.approx(net / 5000.0)

    # 反证：均值口径会得到完全不同的数字，确保没有退化成均值
    mean_resale = (8000.0 + 8100.0 + 8200.0 + 15000.0) / 4 * 0.9
    assert mean_resale == pytest.approx(8842.5)
    assert result["resale_price"] != pytest.approx(mean_resale)


def test_median_robust_to_single_outlier() -> None:
    """奇数样本时中位数取正中间值，单个极端离群值不改变结果。"""
    clean = estimate_profit(
        buy_price=1000.0,
        reference_prices=[1000.0, 1100.0, 1200.0],
    )
    polluted = estimate_profit(
        buy_price=1000.0,
        reference_prices=[1000.0, 1100.0, 1200.0, 99999.0, 0.5],
    )

    assert clean["resale_price"] == pytest.approx(1100.0 * 0.9)
    # 0.5 会被 >= 0 的校验保留（正数），但中位数仍稳定在 1100
    assert polluted["resale_price"] == pytest.approx(1100.0 * 0.9)


def test_exact_min_samples_boundary_passes() -> None:
    """样本数恰好等于 min_samples 时应当可估算。"""
    result = estimate_profit(
        buy_price=1000.0,
        reference_prices=[1500.0, 1600.0, 1700.0],
        min_samples=3,
    )

    assert result["estimated"] is True
    assert result["resale_price"] == pytest.approx(1600.0 * 0.9)


def test_insufficient_samples() -> None:
    """样本数低于下限：estimated=False，原因写明有效样本数与下限。"""
    result = estimate_profit(
        buy_price=1000.0,
        reference_prices=[1500.0, 1600.0],
        min_samples=3,
    )

    _assert_not_estimated(result, "样本不足")
    assert "2" in result["reason"]
    assert "3" in result["reason"]


def test_reference_prices_none_or_empty() -> None:
    """参考价为 None 或空列表时按零样本处理，不抛异常。"""
    _assert_not_estimated(
        estimate_profit(buy_price=1000.0, reference_prices=None),
        "样本不足",
    )
    _assert_not_estimated(
        estimate_profit(buy_price=1000.0, reference_prices=[]),
        "样本不足",
    )


def test_min_samples_one_allows_single_sample() -> None:
    """下限为 1 时单条样本即可估算，用于调用方明确接受低置信度的场景。"""
    result = estimate_profit(
        buy_price=100.0,
        reference_prices=[200.0],
        min_samples=1,
    )

    assert result["estimated"] is True
    assert result["resale_price"] == pytest.approx(180.0)
    assert result["net_profit"] == pytest.approx(80.0)
    assert result["roi"] == pytest.approx(0.8)


def test_invalid_buy_price_returns_not_estimated() -> None:
    """买入价缺失/非正数/非数值时明确报告不可估算，且不产出可比数字。"""
    for bad_price in (None, 0.0, -100.0, 0, float("nan"), float("inf"), "1000"):
        result = estimate_profit(
            buy_price=bad_price,  # type: ignore[arg-type]
            reference_prices=[1500.0, 1600.0, 1700.0, 1800.0],
        )
        _assert_not_estimated(result, "买入价")


def test_buy_price_zero_does_not_crash() -> None:
    """buy_price=0 不得抛 ZeroDivisionError，而是走无效买入价分支。"""
    result = estimate_profit(buy_price=0, reference_prices=[100.0, 200.0, 300.0])

    _assert_not_estimated(result, "买入价")
    assert result["roi"] is None


def test_buy_price_none_with_insufficient_samples_prefers_buy_price_reason() -> None:
    """两个前置条件都不满足时优先报告买入价问题（更根本、更易被上游修复）。"""
    result = estimate_profit(buy_price=None, reference_prices=[])

    _assert_not_estimated(result, "买入价")


def test_dirty_reference_samples_are_filtered() -> None:
    """脏样本被过滤：None、NaN、inf、<=0、字符串、bool 全部剔除后只剩 3 条有效样本。"""
    result = estimate_profit(
        buy_price=1000.0,
        reference_prices=[
            None,  # type: ignore[list-item]
            float("nan"),
            float("inf"),
            float("-inf"),
            -100.0,
            0.0,
            "8000",  # type: ignore[list-item]
            True,  # type: ignore[list-item]
            9000.0,
            9000.0,
            9000.0,
        ],
    )

    assert result["estimated"] is True
    assert result["resale_price"] == pytest.approx(9000.0 * 0.9)
    assert result["net_profit"] == pytest.approx(8100.0 - 1000.0)


def test_dirty_samples_can_drop_below_min_samples() -> None:
    """有效样本不足下限时报告样本不足，而不是用脏数据凑数。"""
    result = estimate_profit(
        buy_price=1000.0,
        reference_prices=[float("nan"), -5.0, None, "9000", 9000.0],  # type: ignore[list-item]
    )

    _assert_not_estimated(result, "样本不足")
    assert "1" in result["reason"]


def test_all_dirty_samples_are_not_estimated() -> None:
    """全部为脏样本时等价于零样本，仍是有限且结构完整的返回。"""
    result = estimate_profit(
        buy_price=1000.0,
        reference_prices=[None, float("nan"), float("inf"), -1.0, 0.0],  # type: ignore[list-item]
    )

    _assert_not_estimated(result, "样本不足")


def test_reference_prices_unusable_type_is_not_estimated() -> None:
    """不可迭代的类型（字符串/整数）不得被当作样本容器，应走样本不足分支。"""
    _assert_not_estimated(
        estimate_profit(buy_price=1000.0, reference_prices="8000"),  # type: ignore[arg-type]
        "样本不足",
    )
    _assert_not_estimated(
        estimate_profit(buy_price=1000.0, reference_prices=8000),  # type: ignore[arg-type]
        "样本不足",
    )


def test_safety_margin_scales_resale_price() -> None:
    """安全边际直接乘在中位数上，取 1.0 时不做折扣。"""
    full = estimate_profit(
        buy_price=100.0,
        reference_prices=[200.0, 200.0, 200.0],
        safety_margin=1.0,
    )
    assert full["resale_price"] == pytest.approx(200.0)

    discounted = estimate_profit(
        buy_price=100.0,
        reference_prices=[200.0, 200.0, 200.0],
        safety_margin=0.5,
    )
    assert discounted["resale_price"] == pytest.approx(100.0)

    default_margin = estimate_profit(
        buy_price=100.0,
        reference_prices=[200.0, 200.0, 200.0],
    )
    assert default_margin["resale_price"] == pytest.approx(180.0)  # 默认 0.9


def test_invalid_safety_margin_falls_back_to_default() -> None:
    """安全边际为负数/NaN/None/字符串时回落到默认 0.9，避免出现负转卖价。"""
    for bad_margin in (-0.5, float("nan"), None, "0.5"):
        result = estimate_profit(
            buy_price=100.0,
            reference_prices=[200.0, 200.0, 200.0],
            safety_margin=bad_margin,  # type: ignore[arg-type]
        )

        assert result["estimated"] is True
        assert result["resale_price"] == pytest.approx(180.0)


def test_zero_safety_margin_reports_not_estimated() -> None:
    """安全边际为 0 时转卖价非正，比率失去意义，应当报告不可估算而不是给出 inf。"""
    result = estimate_profit(
        buy_price=100.0,
        reference_prices=[200.0, 200.0, 200.0],
        safety_margin=0.0,
    )

    _assert_not_estimated(result, "安全边际")


def test_commission_is_computed_from_resale_price() -> None:
    """佣金的基数是转卖价而非买入价：费率 0.1 时扣 200*0.1 = 20。

    若误用买入价做基数只会扣 10，因此这条断言专门区分两种口径。
    """
    result = estimate_profit(
        buy_price=100.0,
        reference_prices=[200.0, 200.0, 200.0],
        commission_rate=0.1,
        safety_margin=1.0,
    )

    assert result["resale_price"] == pytest.approx(200.0)
    commission = 200.0 * 0.1
    assert result["net_profit"] == pytest.approx(200.0 - 100.0 - commission)
    assert result["net_profit"] == pytest.approx(80.0)
    # 佣金若错按买入价计算则会是 90，显式排除
    assert result["net_profit"] != pytest.approx(200.0 - 100.0 - 100.0 * 0.1)


def test_shipping_and_commission_both_deducted() -> None:
    """运费与佣金同时生效时逐个扣减。"""
    result = estimate_profit(
        buy_price=1000.0,
        reference_prices=[2000.0, 2000.0, 2000.0],
        shipping_cost=30.0,
        commission_rate=0.05,
        safety_margin=1.0,
    )

    # 2000 - 1000 - 30 - (2000*0.05 = 100) = 870
    assert result["net_profit"] == pytest.approx(870.0)
    assert result["profit_margin"] == pytest.approx(870.0 / 2000.0)
    assert result["roi"] == pytest.approx(870.0 / 1000.0)


def test_dirty_shipping_and_commission_fall_back_to_zero() -> None:
    """运费/佣金为负数、NaN、None 时按 0 处理，不放大利润也不崩。"""
    for bad_value in (-10.0, float("nan"), None, "5"):
        result = estimate_profit(
            buy_price=100.0,
            reference_prices=[200.0, 200.0, 200.0],
            shipping_cost=bad_value,  # type: ignore[arg-type]
            commission_rate=bad_value,  # type: ignore[arg-type]
            safety_margin=1.0,
        )

        assert result["net_profit"] == pytest.approx(100.0)


def test_negative_profit_when_buy_price_above_resale() -> None:
    """买入价高于转卖价时利润与 ROI 为负，而不是被钳制为 0，调用方需自行判断。"""
    result = estimate_profit(
        buy_price=1000.0,
        reference_prices=[800.0, 800.0, 800.0],
        safety_margin=1.0,
    )

    assert result["estimated"] is True
    assert result["resale_price"] == pytest.approx(800.0)
    assert result["net_profit"] == pytest.approx(-200.0)
    assert result["profit_margin"] == pytest.approx(-0.25)
    assert result["roi"] == pytest.approx(-0.2)


def test_results_are_finite_floats() -> None:
    """正常路径下所有数值字段都是有限 float，方便直接落库或展示。"""
    result = estimate_profit(
        buy_price=1234.5,
        reference_prices=[2222.0, 3333.0, 4444.0],
        shipping_cost=15.5,
        commission_rate=0.015,
    )

    for key in ("resale_price", "net_profit", "profit_margin", "roi"):
        assert isinstance(result[key], float)
        assert math.isfinite(result[key]) is True


def test_invalid_min_samples_falls_back_to_conservative_default() -> None:
    """min_samples 为 0/负数/NaN/None 属于配置错误，回落到保守默认值 3，而非放宽校验。"""
    # 非法下限 + 只有 2 条样本：回落成 3 之后仍应判定为样本不足
    for bad_min in (0, -3, float("nan"), None):
        result = estimate_profit(
            buy_price=100.0,
            reference_prices=[200.0, 200.0],
            min_samples=bad_min,  # type: ignore[arg-type]
        )
        _assert_not_estimated(result, "样本不足")
        # 原因里应体现回落后的下限 3，而不是原始脏值
        assert "3" in result["reason"]

    # 合法下限 1 时单条样本仍可估算，证明回落逻辑没有影响正常配置
    explicit_one = estimate_profit(
        buy_price=100.0,
        reference_prices=[200.0],
        min_samples=1,
    )
    assert explicit_one["estimated"] is True

    # 非法下限 + 样本充足：回落成 3 之后照样能估算
    enough_samples = estimate_profit(
        buy_price=100.0,
        reference_prices=[200.0, 200.0, 200.0],
        min_samples=0,
    )
    assert enough_samples["estimated"] is True
    assert enough_samples["resale_price"] == pytest.approx(180.0)


def test_tuple_input_is_accepted() -> None:
    """参考价以元组传入同样有效，避免上游因容器类型不同而失效。"""
    result = estimate_profit(
        buy_price=100.0,
        reference_prices=(200.0, 200.0, 200.0),  # type: ignore[arg-type]
        safety_margin=1.0,
    )

    assert result["estimated"] is True
    assert result["resale_price"] == pytest.approx(200.0)
