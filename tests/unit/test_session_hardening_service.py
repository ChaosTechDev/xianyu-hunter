"""``session_hardening_service`` 的单元测试。

覆盖三类高频误判：cookie 名字在但值为空（最常见的「看着有其实是废的」登录态）、
``_m_h5_tk`` 格式畸形或陈旧、``storage_state`` 文件结构合法但内容不可用。
所有时间判断都通过显式 ``now`` 注入，结果完全确定。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.services.session_hardening_service import (
    DEFAULT_STALE_WARN_HOURS,
    EXPIRING_SOON_HOURS,
    M_H5_TK_NAME,
    REQUIRED_COOKIE_NAMES,
    assess_cookie_freshness,
    check_required_cookies,
    parse_m_h5_tk,
    validate_storage_state,
)
from src.services.xy_protocol.signer import extract_token

#: 测试基线时间。服务实现把 naive 时间按 UTC 解释（cookie expires 就是 UTC 秒数），
#: 因此这里统一用带 UTC 时区的构造器生成时间戳，避免本地时区把断言带偏。
NOW = datetime(2026, 3, 19, 12, 0, 0)


def _utc(moment: datetime) -> float:
    """把 naive 时间当作 UTC 换算成 Unix 时间戳（秒）。"""
    return moment.replace(tzinfo=timezone.utc).timestamp()


class TestTokenExtractionConsistency:
    """跨模块不变量：签名实际发出去的 token，必须与新鲜度判定看到的 token 相同。

    这两个模块各自演进时最容易出现的问题就是切分规则分叉——若 ``signer`` 用
    ``split("_")[0]`` 而本模块改成别的切法，就会出现「判定为新鲜、实际签名却是
    另一个 token」的静默错配，导致请求持续失败却查不出原因。
    """

    @pytest.mark.parametrize(
        "value",
        [
            "abc12345_1700000000000",
            "tokEN1234567890_1699999999999",
            "x" * 32 + "_1700000000000",
        ],
    )
    def test_well_formed_values_yield_identical_token(self, value):
        parsed = parse_m_h5_tk(value)
        assert parsed is not None
        assert parsed[0] == extract_token(value), "切分规则必须与 signer 一致"

    def test_malformed_extra_underscore_is_rejected_rather_than_guessed(self):
        """后半段含多余下划线时本模块拒绝解析。

        ``signer.extract_token`` 会返回 ``"a"``，而本模块返回 ``None``：这是**刻意**
        的分歧而非疏漏——新鲜度判断宁可把畸形值判为「不可信、需刷新」，
        也不要基于一个残缺 token 得出「登录态健康」的结论。
        """
        assert extract_token("a_b_c_1700000000000") == "a"
        assert parse_m_h5_tk("a_b_c_1700000000000") is None

    def test_short_token_rejected_by_parser(self):
        """token 过短判为残缺；signer 仍会返回它，但格式校验会拦住。"""
        assert parse_m_h5_tk("abc_1700000000000") is None


def _h5_value(issued_at: datetime, token: str = "a1b2c3d4e5f6") -> str:
    """按 ``<token>_<毫秒时间戳>`` 生成一个 _m_h5_tk 值。"""
    return f"{token}_{int(_utc(issued_at) * 1000)}"


def _cookie(name: str, value: str = "v", expires: object = None) -> dict:
    return {"name": name, "value": value, "expires": expires}


def _cookie_jar(**overrides: object) -> list[dict]:
    """生成一份「关键 cookie 齐全且不过期」的基线 cookie 列表。"""
    issued = NOW - timedelta(hours=1)
    far_future = _utc(NOW + timedelta(days=30))
    base = {
        "_m_h5_tk": _h5_value(issued),
        "cookie2": "cookie2-value",
        "unb": "1234567890",
        "_tb_token_": "tb-token-value",
        "sgcookie": "sg-value",
        "csg": "csg-value",
    }
    base.update({key: str(val) for key, val in overrides.items()})
    return [_cookie(name, value, far_future) for name, value in base.items()]


# ======================================================================================
# 交付 2.1：check_required_cookies / parse_m_h5_tk
# ======================================================================================


def test_check_required_cookies_all_present():
    result = check_required_cookies(_cookie_jar())

    assert result["ok"] is True
    assert result["present"] == list(REQUIRED_COOKIE_NAMES)
    assert result["missing"] == []
    assert {"_m_h5_tk", "cookie2", "unb", "_tb_token_"} <= set(REQUIRED_COOKIE_NAMES)


def test_check_required_cookies_reports_single_missing_by_name():
    cookies = [item for item in _cookie_jar() if item["name"] != "cookie2"]
    result = check_required_cookies(cookies)

    assert result["ok"] is False
    assert result["missing"] == ["cookie2"]
    assert "_m_h5_tk" in result["present"]


def test_check_required_cookies_missing_list_keeps_declaration_order():
    cookies = [_cookie("unb", "123"), _cookie("_m_h5_tk", _h5_value(NOW - timedelta(hours=1)))]
    result = check_required_cookies(cookies)

    assert result["missing"] == ["cookie2", "_tb_token_", "sgcookie", "csg"]
    assert result["present"] == ["_m_h5_tk", "unb"]
    assert result["ok"] is False


@pytest.mark.parametrize("empty_value", ["", " ", "   ", "\t", "\n", "\u3000", "\u200b", None])
def test_check_required_cookies_treats_blank_value_as_missing(empty_value):
    """名字在但值为空/纯空白必须算缺失——这是最常见的「看着有其实是废的」登录态。"""
    cookies = [item for item in _cookie_jar() if item["name"] != "_m_h5_tk"]
    cookies.append({"name": "_m_h5_tk", "value": empty_value, "expires": None})
    result = check_required_cookies(cookies)

    assert result["ok"] is False
    assert result["missing"] == ["_m_h5_tk"]
    assert "cookie2" in result["present"]


def test_check_required_cookies_blank_value_does_not_shadow_valid_duplicate():
    """同名重复 cookie：存在一条非空值就算 present，空值那条不能顶掉有效值。"""
    cookies = [item for item in _cookie_jar() if item["name"] != "unb"]
    cookies.append({"name": "unb", "value": "", "expires": None})
    cookies.append({"name": "unb", "value": "99887766", "expires": None})
    result = check_required_cookies(cookies)

    assert result["ok"] is True
    assert result["missing"] == []
    assert "unb" in result["present"]


def test_check_required_cookies_empty_list_marks_everything_missing():
    result = check_required_cookies([])

    assert result == {"present": [], "missing": list(REQUIRED_COOKIE_NAMES), "ok": False}


def test_check_required_cookies_ignores_malformed_entries():
    """非字典项、缺 name 的项、非列表输入都不能让函数抛异常。"""
    cookies = _cookie_jar() + ["not-a-dict", {"value": "no-name"}]  # type: ignore[list-item]
    assert check_required_cookies(cookies)["ok"] is True

    for bad_input in (None, "string", {"name": "x"}, 42):
        result = check_required_cookies(bad_input)  # type: ignore[arg-type]
        assert result["ok"] is False
        assert result["missing"] == list(REQUIRED_COOKIE_NAMES)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("a1b2c3d4_1699999999999", ("a1b2c3d4", 1699999999999)),
        ("abcdefgh_1699999999999", ("abcdefgh", 1699999999999)),
        ("  a1b2c3d4_1699999999999  ", ("a1b2c3d4", 1699999999999)),
        ("a1b2c3d4_1699999999", ("a1b2c3d4", 1699999999000)),
        ("a1b2c3d4e5f6a7b8_1234567890123", ("a1b2c3d4e5f6a7b8", 1234567890123)),
    ],
)
def test_parse_m_h5_tk_accepts_valid_formats(value, expected):
    assert parse_m_h5_tk(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        1234567890123,
        b"a1b2c3d4_1699999999999",
        [],
        "",
        "   ",
        "no-underscore-here",
        "_1699999999999",
        "a1b2c3d4_",
        "a1b2c3d4_   ",
        "a1b2c3d4_abc",
        "a1b2c3d4_1699999999999_extra",
        "a1b2c3d4_0",
        "a1b2c3d4_-1",
        "short_1699999999999",
    ],
)
def test_parse_m_h5_tk_rejects_malformed_input(value):
    assert parse_m_h5_tk(value) is None


def test_parse_m_h5_tk_token_length_boundary():
    """token 长度恰好等于下限算合法，少一个字符就不合法。"""
    assert parse_m_h5_tk("abcdefgh_1700000000000") == ("abcdefgh", 1700000000000)
    assert parse_m_h5_tk("abcdefg_1700000000000") is None


def test_parse_m_h5_tk_seconds_are_promoted_to_milliseconds():
    """秒级时间戳统一升为毫秒，方便上层用同一基准算新鲜度。"""
    token, timestamp = parse_m_h5_tk("a1b2c3d4_1700000000")  # type: ignore[misc]
    assert token == "a1b2c3d4"
    assert timestamp == 1700000000000
    # 刚好落在阈值上的 12 位时间戳按「毫秒」处理，不做提升
    assert parse_m_h5_tk("a1b2c3d4_100000000000") == ("a1b2c3d4", 100000000000)


# ======================================================================================
# 交付 2.2：assess_cookie_freshness
# ======================================================================================


def test_freshness_healthy_jar():
    result = assess_cookie_freshness(_cookie_jar(), now=NOW)

    assert result["expired"] == []
    assert result["expiring_soon"] == []
    assert result["stale_h5_tk"] is False
    assert result["oldest_token_age_hours"] == 1.0
    assert result["verdict"] == "healthy"
    assert result["session_cookies"] == []
    assert "登录态可用" in result["reason"]


def test_freshness_detects_expired_cookie():
    cookies = [
        _cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=1))),
        _cookie("cookie2", "v", _utc(NOW - timedelta(hours=3))),
    ]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["expired"] == ["cookie2"]
    assert result["verdict"] == "expired"
    assert "cookie2" in result["reason"]
    assert "重新登录" in result["reason"]


def test_freshness_detects_expiring_soon_cookie():
    cookies = [
        _cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=1))),
        _cookie("cookie2", "v", _utc(NOW + timedelta(hours=1))),
    ]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["expired"] == []
    assert result["expiring_soon"] == ["cookie2"]
    assert result["verdict"] == "expiring_soon"
    assert result["stale_h5_tk"] is False
    assert str(EXPIRING_SOON_HOURS) in result["reason"]


def test_freshness_expiring_soon_boundary_is_treated_as_valid():
    """剩余时间恰好等于 2 小时阈值时不算「即将过期」，行为必须无歧义。"""
    cookies = [
        _cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=1))),
        _cookie("cookie2", "v", _utc(NOW + timedelta(hours=EXPIRING_SOON_HOURS))),
    ]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["expiring_soon"] == []
    assert result["verdict"] == "healthy"


@pytest.mark.parametrize("session_expires", [None, 0, -1, "abc", "", [], {}])
def test_freshness_session_cookies_are_not_expired(session_expires):
    """expires 为 None / <=0 / 非数值 -> 会话 cookie：不算过期，但要在裁决里提示风险。"""
    cookies = _cookie_jar()
    cookies.append({"name": "tracknick", "value": "someone", "expires": session_expires})
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["expired"] == []
    assert result["expiring_soon"] == []
    assert result["verdict"] == "healthy"
    assert result["session_cookies"] == ["tracknick"]
    assert "会话 cookie" in result["reason"]
    assert "掉线风险" in result["reason"]


def test_freshness_stale_h5_tk_is_suspicious():
    cookies = [
        _cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=13))),
        _cookie("cookie2", "v", _utc(NOW + timedelta(days=30))),
    ]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["stale_h5_tk"] is True
    assert result["oldest_token_age_hours"] == 13.0
    assert result["verdict"] == "suspicious"
    assert "13.0 小时" in result["reason"]
    assert "12.0 小时阈值" in result["reason"]


def test_freshness_stale_threshold_boundary_is_not_stale():
    """年龄恰好等于阈值不算陈旧（判据是严格大于）。"""
    cookies = [_cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=DEFAULT_STALE_WARN_HOURS)))]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["oldest_token_age_hours"] == 12.0
    assert result["stale_h5_tk"] is False
    assert result["verdict"] == "healthy"


def test_freshness_custom_stale_threshold():
    cookies = [_cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=2)))]
    result = assess_cookie_freshness(cookies, now=NOW, stale_warn_hours=1)

    assert result["stale_h5_tk"] is True
    assert result["verdict"] == "suspicious"
    assert "1.0 小时阈值" in result["reason"]


@pytest.mark.parametrize("bad_threshold", [0, -1, None, "abc", True, float("nan"), float("inf")])
def test_freshness_invalid_stale_threshold_falls_back(bad_threshold):
    """非法阈值回落到 12 小时：13 小时前的 token 仍必须判为陈旧。"""
    cookies = [_cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=13)))]
    result = assess_cookie_freshness(cookies, now=NOW, stale_warn_hours=bad_threshold)  # type: ignore[arg-type]

    assert result["stale_h5_tk"] is True
    assert result["verdict"] == "suspicious"
    assert "12.0 小时阈值" in result["reason"]


def test_freshness_missing_h5_tk_is_suspicious():
    cookies = [_cookie("cookie2", "v", _utc(NOW + timedelta(days=30)))]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["stale_h5_tk"] is True
    assert result["oldest_token_age_hours"] is None
    assert result["verdict"] == "suspicious"
    assert "缺少 _m_h5_tk" in result["reason"]


def test_freshness_blank_h5_tk_value_is_suspicious():
    """值为空白的 _m_h5_tk 等同缺失，不能因为名字存在就判健康。"""
    cookies = [{"name": M_H5_TK_NAME, "value": "   ", "expires": None}]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["stale_h5_tk"] is True
    assert result["oldest_token_age_hours"] is None
    assert result["verdict"] == "suspicious"
    assert "值为空" in result["reason"]


def test_freshness_malformed_h5_tk_is_suspicious():
    cookies = [{"name": M_H5_TK_NAME, "value": "not-the-right-format", "expires": None}]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["stale_h5_tk"] is True
    assert result["verdict"] == "suspicious"
    assert "格式无法解析" in result["reason"]


def test_freshness_expired_takes_priority_over_stale():
    """同时存在过期 cookie 与陈旧 token 时，裁决必须是 expired（更严重）。"""
    cookies = [
        _cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=48))),
        _cookie("cookie2", "v", _utc(NOW - timedelta(minutes=30))),
    ]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["verdict"] == "expired"
    assert result["expired"] == ["cookie2"]
    assert result["stale_h5_tk"] is True
    assert "同时 _m_h5_tk 缺失或已陈旧" in result["reason"]


def test_freshness_session_cookies_do_not_downgrade_expired_verdict():
    cookies = [
        _cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=1)), _utc(NOW + timedelta(days=30))),
        _cookie("cookie2", "v", _utc(NOW - timedelta(hours=1))),
        {"name": "tracknick", "value": "someone", "expires": None},
    ]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["verdict"] == "expired"
    assert result["expired"] == ["cookie2"]
    assert result["session_cookies"] == ["tracknick"]
    assert result["stale_h5_tk"] is False


def test_freshness_ignores_blank_value_cookies_in_expiry_stats():
    """值为空的 cookie 不参与过期统计（缺失由 check_required_cookies 负责报出）。"""
    cookies = [
        _cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=1))),
        {"name": "cookie2", "value": "  ", "expires": _utc(NOW - timedelta(days=1))},
    ]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["expired"] == []
    assert result["verdict"] == "healthy"


def test_freshness_token_age_is_rounded_to_one_decimal():
    cookies = [_cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(minutes=90)))]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["oldest_token_age_hours"] == 1.5
    assert result["stale_h5_tk"] is False


def test_freshness_nan_expires_is_treated_as_session():
    """NaN/无穷与非数值字符串一样归入会话 cookie，不能被当成「已过期」。"""
    cookies = [
        _cookie(M_H5_TK_NAME, _h5_value(NOW - timedelta(hours=1)), _utc(NOW + timedelta(days=30))),
        {"name": "cookie2", "value": "v", "expires": float("nan")},
        {"name": "unb", "value": "1", "expires": float("inf")},
        {"name": "_tb_token_", "value": "t", "expires": "abc"},
    ]
    result = assess_cookie_freshness(cookies, now=NOW)

    assert result["expired"] == []
    assert result["expiring_soon"] == []
    assert result["session_cookies"] == ["cookie2", "unb", "_tb_token_"]
    assert result["stale_h5_tk"] is False
    assert result["verdict"] == "healthy"


def test_freshness_empty_cookie_list_is_suspicious():
    """完全空的 cookie 列表：没有 _m_h5_tk，只能判可疑。"""
    result = assess_cookie_freshness([], now=NOW)

    assert result["expired"] == []
    assert result["expiring_soon"] == []
    assert result["stale_h5_tk"] is True
    assert result["verdict"] == "suspicious"


def test_freshness_requires_datetime_now():
    with pytest.raises(ValueError):
        assess_cookie_freshness([], now="2026-03-19 12:00:00")  # type: ignore[arg-type]


# ======================================================================================
# 交付 2.3：validate_storage_state
# ======================================================================================


def test_validate_storage_state_accepts_well_formed_payload():
    payload = {
        "cookies": [
            {"name": "_m_h5_tk", "value": "a1b2c3d4_1700000000000", "domain": ".goofish.com"},
            {"name": "cookie2", "value": "cookie2-value", "domain": ".taobao.com"},
        ],
        "origins": [{"origin": "https://www.goofish.com", "localStorage": []}],
    }
    result = validate_storage_state(payload)

    assert result["valid"] is True
    assert result["cookie_count"] == 2
    assert result["origin_count"] == 1
    assert "结构合法" in result["reason"]


@pytest.mark.parametrize("payload", [None, [], "string", 42, ("cookies",)])
def test_validate_storage_state_rejects_non_dict_payload(payload):
    result = validate_storage_state(payload)  # type: ignore[arg-type]

    assert result["valid"] is False
    assert result["cookie_count"] == 0
    assert "顶层结构必须是字典" in result["reason"]


def test_validate_storage_state_rejects_missing_cookies_key():
    result = validate_storage_state({"origins": []})

    assert result["valid"] is False
    assert result["cookie_count"] == 0
    assert "缺少 cookies 字段" in result["reason"]


@pytest.mark.parametrize("bad_cookies", ["", "abc", 123, {"name": "x"}, True])
def test_validate_storage_state_rejects_non_list_cookies(bad_cookies):
    result = validate_storage_state({"cookies": bad_cookies})

    assert result["valid"] is False
    assert result["cookie_count"] == 0
    assert "必须是列表" in result["reason"]


def test_validate_storage_state_rejects_empty_cookie_list():
    """空 cookie 列表是最典型的「看着有文件其实是废的」，必须判无效。"""
    result = validate_storage_state({"cookies": [], "origins": []})

    assert result["valid"] is False
    assert result["cookie_count"] == 0
    assert "空列表" in result["reason"]
    assert "登录态无效" in result["reason"]


def test_validate_storage_state_rejects_all_blank_cookie_values():
    """名字都在但值全空，同样不可用。"""
    result = validate_storage_state(
        {
            "cookies": [
                {"name": "_m_h5_tk", "value": ""},
                {"name": "cookie2", "value": "   "},
                {"name": "unb", "value": "\u3000"},
            ]
        }
    )

    assert result["valid"] is False
    assert result["cookie_count"] == 3
    assert "值全为空" in result["reason"]


def test_validate_storage_state_rejects_cookie_missing_name():
    result = validate_storage_state({"cookies": [{"value": "v", "domain": ".goofish.com"}]})

    assert result["valid"] is False
    assert result["cookie_count"] == 1
    assert "结构非法" in result["reason"]


def test_validate_storage_state_rejects_non_dict_cookie_entries():
    result = validate_storage_state({"cookies": ["not-a-dict", {"name": "ok", "value": "v"}]})

    assert result["valid"] is False
    assert result["cookie_count"] == 2
    assert "1 条结构非法" in result["reason"]


@pytest.mark.parametrize("origins", [None, [], "nope", 42, {"origin": "x"}])
def test_validate_storage_state_tolerates_bad_origins(origins):
    """origins 只做宽容校验：缺失或结构不对不判无效，但计数要正确。"""
    result = validate_storage_state({"cookies": [{"name": "cookie2", "value": "v"}], "origins": origins})

    assert result["valid"] is True
    assert result["cookie_count"] == 1
    assert result["origin_count"] == 0


def test_validate_storage_state_reports_meaningful_and_blank_counts():
    """报告里要同时反映 cookie 总数与真正非空的数量，便于排查「有一半是废的」。"""
    result = validate_storage_state(
        {
            "cookies": [
                {"name": "_m_h5_tk", "value": "a1b2c3d4_1700000000000"},
                {"name": "cookie2", "value": ""},
                {"name": "unb", "value": "12345"},
            ],
            "origins": [],
        }
    )

    assert result["valid"] is True
    assert result["cookie_count"] == 3
    assert "3 条 cookie（其中 2 条值非空）" in result["reason"]
