"""xy_protocol 签名模块测试。

签名正确性靠**已知向量**锁定：签名算法是纯 MD5，输入确定则输出确定，因此可以
预先算好期望值写进断言。这样任何对签名原文格式的意外改动（多加空格、appKey 变了、
token 提取方式变了）都会立即失败，而不是等到线上 401 才发现。

两个历史坑被显式覆盖：
1. ``t`` 必须来自 ``int(time.time() * 1000)``。写成 ``int(time.time()) * 1000``
   末三位恒为 0，签名与真实请求不一致。
2. ``data`` 必须是 ``separators=(",", ":")`` 的紧凑 JSON。默认的
   ``json.dumps`` 会插入空格，导致签名错。
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.services.xy_protocol import (
    APP_KEY,
    build_sign,
    compact_json,
    extract_token,
    sign_params,
)
from src.services.xy_protocol.signer import M_H5_TK_COOKIE_NAME


def _expected_sign(token: str, t_ms: int, data: str) -> str:
    """独立重算一次签名，作为交叉校验（不复用被测实现）。"""
    raw = f"{token}&{t_ms}&34839810&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class TestExtractToken:
    def test_splits_on_underscore_and_keeps_prefix(self):
        assert extract_token("abc123_1699999999999") == "abc123"

    def test_token_without_timestamp_is_returned_as_is(self):
        assert extract_token("abc123") == "abc123"

    def test_only_first_segment_used_when_multiple_underscores(self):
        assert extract_token("a_b_c") == "a"

    def test_empty_and_none_return_empty_string(self):
        assert extract_token("") == ""
        assert extract_token(None) == ""

    def test_surrounding_whitespace_is_stripped(self):
        assert extract_token("  tok_123  ") == "tok"


class TestCompactJson:
    def test_no_spaces_after_separators(self):
        encoded = compact_json({"a": 1, "b": 2})
        assert " " not in encoded
        assert encoded in ('{"a":1,"b":2}', '{"b":2,"a":1}')

    def test_differs_from_default_dumps_exactly_by_whitespace(self):
        payload = {"keyword": "iPhone 15", "page": 1}
        compact = compact_json(payload)
        assert compact != json.dumps(payload), "默认 dumps 会插入空格，必须用 separators"
        assert compact == json.dumps(payload, separators=(",", ":"))

    def test_non_ascii_is_escaped_by_default(self):
        """保持 json.dumps 默认 ensure_ascii=True，与 4 个参考实现一致。"""
        encoded = compact_json({"kw": "手机"})
        assert "\\u" in encoded
        assert "手机" not in encoded

    def test_chinese_keyword_roundtrips(self):
        encoded = compact_json({"kw": "手机"})
        assert json.loads(encoded) == {"kw": "手机"}


class TestBuildSign:
    def test_known_vector_for_fixed_inputs(self):
        token = "a1b2c3"
        t_ms = 1700000000000
        data = '{"itemId":"123"}'
        assert build_sign(token, t_ms, data) == _expected_sign(token, t_ms, data)

    def test_returns_lowercase_hex_md5(self):
        sign = build_sign("tok", 1700000000000, "{}")
        assert len(sign) == 32
        assert sign == sign.lower()
        assert all(c in "0123456789abcdef" for c in sign)

    def test_app_key_is_embedded_in_signature(self):
        """换 appKey 必须改变签名，证明它确实参与原文。"""
        token, t_ms, data = "tok", 1700000000000, "{}"
        raw_with_other_key = f"{token}&{t_ms}&99999999&{data}"
        other = hashlib.md5(raw_with_other_key.encode("utf-8")).hexdigest()
        assert build_sign(token, t_ms, data) != other

    def test_different_inputs_produce_different_signs(self):
        base = build_sign("tok", 1700000000000, "{}")
        assert build_sign("tok2", 1700000000000, "{}") != base
        assert build_sign("tok", 1700000000001, "{}") != base
        assert build_sign("tok", 1700000000000, '{"a":1}') != base


class TestSignParams:
    def test_adds_sign_t_and_appkey(self):
        signed = sign_params({"itemId": "123"}, "tok", t_ms=1700000000000)
        assert set(signed) == {"itemId", "sign", "t", "appKey"}
        assert signed["t"] == 1700000000000
        assert signed["appKey"] == APP_KEY

    def test_signature_matches_hand_computed_value(self):
        params = {"itemId": "123", "page": 1}
        t_ms = 1700000000000
        signed = sign_params(params, "tok", t_ms=t_ms)
        expected = _expected_sign("tok", t_ms, compact_json(params))
        assert signed["sign"] == expected

    def test_original_params_not_mutated(self):
        """入参不得被修改——签名原文必须是不含 sign/t/appKey 的业务参数。"""
        params = {"itemId": "123"}
        sign_params(params, "tok", t_ms=1700000000000)
        assert params == {"itemId": "123"}

    def test_signature_covers_only_business_params(self):
        """sign/t/appKey 三个附加字段本身不参与签名。"""
        params = {"itemId": "123"}
        t_ms = 1700000000000
        signed = sign_params(params, "tok", t_ms=t_ms)
        # 用含附加字段的完整字典重算，结果必须不同（证明原文不含它们）
        naive = hashlib.md5(
            f"tok&{t_ms}&34839810&{compact_json(signed)}".encode("utf-8")
        ).hexdigest()
        assert signed["sign"] != naive
        assert signed["sign"] == _expected_sign("tok", t_ms, compact_json(params))

    def test_signature_changes_with_business_params(self):
        t_ms = 1700000000000
        a = sign_params({"itemId": "1"}, "tok", t_ms=t_ms)["sign"]
        b = sign_params({"itemId": "2"}, "tok", t_ms=t_ms)["sign"]
        assert a != b

    def test_t_ms_none_uses_real_millisecond_clock(self):
        """t 必须来自 ``int(time.time() * 1000)``。

        写成 ``int(time.time()) * 1000`` 会让末三位恒为 0，签名与真实请求不一致，
        而服务端只会返回一个含糊的 401，极难排查。

        检测手法：连续采样多次，真实毫秒时钟不可能每次都落在整秒边界上。
        单次断言 `t % 1000 != 0` 会以约 0.1% 的概率偶然失败，因此这里用多次采样
        判定——20 次全部落在整秒边界的概率是 (1/1000)^20，可忽略。
        """
        import time

        samples = [sign_params({"a": 1}, "tok")["t"] for _ in range(20)]

        assert all(abs(s - time.time() * 1000) < 5000 for s in samples), "t 应接近当前毫秒时间"
        assert any(s % 1000 != 0 for s in samples), (
            "所有采样的毫秒末三位都是 0，说明用了 int(time.time())*1000 而非真毫秒"
        )

    def test_cookie_name_constant(self):
        assert M_H5_TK_COOKIE_NAME == "_m_h5_tk"

    def test_app_key_constant_is_idle_app_key(self):
        assert APP_KEY == "34839810"
