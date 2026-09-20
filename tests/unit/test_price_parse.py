"""价格解析的边界测试。

``parse_price_value`` 是价格链路的唯一入口：关注提醒、降价判定、行情统计
全部依赖它。这里的静默错误（把垃圾解析成数字、把价格解析成 0）不会抛异常，
只会让用户收到错误提醒或漏掉捡漏，因此重点覆盖异常输入。
"""
from __future__ import annotations

import math

import pytest

from src.services.price_history_service import parse_price_value


# --- 正常格式 ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1234", 1234.0),
        ("¥1234", 1234.0),
        ("¥1,234.56", 1234.56),
        ("1,234.56", 1234.56),
        ("¥ 99.9", 99.9),
        ("  ¥ 99.9  ", 99.9),
        ("0", 0.0),
        ("¥0", 0.0),
        ("0.0", 0.0),
        (1234, 1234.0),
        (1234.567, 1234.57),
        (0, 0.0),
        ("1万", 10000.0),
        ("1.5万", 15000.0),
    ],
)
def test_parse_price_value_common_formats(raw, expected):
    assert parse_price_value(raw) == expected


def test_parse_price_value_returns_float_type():
    assert isinstance(parse_price_value("100"), float)
    assert isinstance(parse_price_value(100), float)


# --- 无法解析的输入 ---


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "面议",
        "价格异常",
        "暂无",
        "-",
        "N/A",
        "abc",
        "￥￥",
    ],
)
def test_parse_price_value_unparseable_returns_none(raw):
    """面议/暂无/空值必须返回 None，调用方据此跳过而非当 0 处理。"""
    assert parse_price_value(raw) is None


def test_parse_price_value_never_returns_zero_for_unparseable():
    """回归护栏：把「面议」当成 0 会让所有无价商品触发低价提醒。"""
    for raw in ("面议", "价格异常", "暂无", "N/A", "", None, "abc"):
        assert parse_price_value(raw) != 0


# --- 边界与异常 ---


def test_parse_price_value_negative_keeps_sign():
    """负数当前会被原样保留（不做拒绝）。

    这在闲鱼真实数据里不存在，但一旦上游解析出错产生负价，下游
    ``price <= alert_price`` 会直接误报低价提醒，因此行为需要被固定下来。
    """
    assert parse_price_value("-100") == -100.0
    assert parse_price_value(-100) == -100.0


@pytest.mark.parametrize("raw", ["nan", "inf", "-inf", "1e400", float("inf"), float("nan")])
def test_parse_price_value_rejects_non_finite_values(raw):
    """非有限值必须被拒绝。

    ``float("1e400")`` / ``"inf"`` / ``"nan"`` 都能通过 ``float()``，若放行会被
    当成正常价格写入 last_price 与 price_snapshots：之后 min/max/avg 全变
    inf/nan，``price <= alert_price`` 的判定也随之失真。

    该缺陷曾是继承自上游的真实 bug（实测 ``'nan' -> nan``），现已在
    ``parse_price_value`` 里用 ``math.isfinite`` 修复，故此处为正向断言。
    """
    result = parse_price_value(raw)
    assert result is None or math.isfinite(result)


def test_parse_price_value_finite_inputs_are_always_finite():
    """护栏：正常输入不得产生非有限值。"""
    for raw in ("1234", "¥1,234.56", "1万", "0", 100):
        result = parse_price_value(raw)
        assert result is not None and math.isfinite(result)


def test_parse_price_thousand_separator_normalisation():
    """逗号被无条件删除，因此畸形分组也被接受为有效数字。

    记录当前行为：``12,34.5`` -> ``1234.5``。闲鱼真实数据是标准千分位，
    这里不做收紧断言，只固定「不抛异常且返回数字」的契约。
    """
    assert parse_price_value("12,34.5") == 1234.5


def test_parse_price_whitespace_only_variants():
    assert parse_price_value("\t") is None
    assert parse_price_value("\n") is None


def test_parse_price_large_but_finite_value_is_accepted():
    assert parse_price_value("99999999") == 99999999.0


def test_parse_price_wan_suffix_boundaries():
    assert parse_price_value("0万") == 0.0
    assert parse_price_value("0.5万") == 5000.0


@pytest.mark.parametrize("raw", ["abc万", "万", "面议万", "--万", "N/A万", "1.5万万", "  万  "])
def test_parse_price_wan_with_bad_number_returns_none(raw):
    """以「万」结尾但前缀非数字时必须返回 None，而不是抛 ValueError。

    历史上源码把 ``float(text[:-1])`` 放在 try/except 之外，``"abc万"`` 会直接
    抛未捕获的 ValueError。该函数有 20+ 个调用点（watch_service /
    result_storage_service / dashboard_payloads 等），多数没包 try，一旦采集
    数据里出现这类脏值就会中断整批处理。现已修复，故为正向断言。
    """
    assert parse_price_value(raw) is None


def test_parse_price_wan_suffix_valid_prefix_still_works():
    """护栏：修复「万」异常时不得破坏正常换算。"""
    assert parse_price_value("1万") == 10000.0
    assert parse_price_value("1.5万") == 15000.0


def test_parse_price_list_input_returns_none():
    """容器类型走 str() 后无法解析，必须返回 None 而不是抛异常。"""
    assert parse_price_value(["100"]) is None
    assert parse_price_value({"price": 100}) is None


def test_parse_price_bool_input_follows_int_branch():
    """bool 是 int 子类，会走数值分支（True -> 1.0）。"""
    assert parse_price_value(True) == 1.0
    assert parse_price_value(False) == 0.0
