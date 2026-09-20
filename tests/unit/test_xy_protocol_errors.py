"""xy_protocol 错误分类测试。

分类结果直接决定上游动作：重取 cookie / 退避 / 放弃，因此每档都要有确定断言。
特别注意 **FAIL_SYS_TOKEN_EXOIRED** 这个拼写（闲鱼服务端返回的确实是少了个 P 的
错拼），必须与拼写正确的 EXPIRED 一起覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.services.xy_protocol import (
    CLASS_FATAL,
    CLASS_RECOVERABLE,
    CLASS_RISK_CONTROL,
    CLASS_UNKNOWN,
    classify_error,
    extract_ret,
    is_retryable,
)


class TestRecoverable:
    @pytest.mark.parametrize(
        "ret",
        [
            "FAIL_SYS_TOKEN_EMPTY::令牌为空",
            "FAIL_SYS_TOKEN_ILLEGAL::令牌非法",
            "FAIL_SYS_SESSION_EXPIRED::session 过期",
            "FAIL_SYS_TOKEN_EXPIRED::token 过期",
        ],
    )
    def test_recoverable_codes(self, ret):
        assert classify_error(ret) == CLASS_RECOVERABLE

    def test_misspelled_exoired_is_recoverable(self):
        """闲鱼服务端真实返回的是 EXOIRED（少一个 P），必须识别。"""
        assert classify_error("FAIL_SYS_TOKEN_EXOIRED::令牌过期") == CLASS_RECOVERABLE

    def test_recoverable_as_list(self):
        assert classify_error(["FAIL_SYS_TOKEN_EMPTY::x"]) == CLASS_RECOVERABLE

    def test_recoverable_is_retryable(self):
        assert is_retryable("FAIL_SYS_TOKEN_EMPTY::x") is True


class TestRiskControl:
    @pytest.mark.parametrize(
        "ret",
        [
            "RGV587_ERROR::小宝贝被挤爆啦",
            "FAIL_SYS_USER_VALIDATE::需要验证",
            "/punish::被 punished",
        ],
    )
    def test_risk_control_codes(self, ret):
        assert classify_error(ret) == CLASS_RISK_CONTROL

    def test_risk_control_is_still_retryable_after_backoff(self):
        """风控值得重试，但上游应拉长间隔。"""
        assert is_retryable("RGV587_ERROR::x") is True


class TestFatal:
    def test_illegal_access_is_fatal(self):
        assert classify_error("FAIL_SYS_ILLEGAL_ACCESS::非法访问") == CLASS_FATAL

    def test_fatal_is_not_retryable(self):
        assert is_retryable("FAIL_SYS_ILLEGAL_ACCESS::x") is False

    def test_fatal_wins_over_risk_control_and_recoverable(self):
        """多条 ret 同时出现时，不可恢复信号优先（最不可逆）。"""
        joined = "FAIL_SYS_ILLEGAL_ACCESS::x RGV587_ERROR::y FAIL_SYS_TOKEN_EMPTY::z"
        assert classify_error(joined) == CLASS_FATAL

    def test_fatal_wins_over_recoverable_in_list(self):
        ret = ["FAIL_SYS_TOKEN_EMPTY::x", "FAIL_SYS_ILLEGAL_ACCESS::y"]
        assert classify_error(ret) == CLASS_FATAL


class TestUnknown:
    def test_unrecognised_code_is_unknown(self):
        assert classify_error("FAIL_SYS_BRAND_NEW_THING::x") == CLASS_UNKNOWN

    def test_empty_string_is_unknown(self):
        assert classify_error("") == CLASS_UNKNOWN

    def test_none_is_unknown(self):
        assert classify_error(None) == CLASS_UNKNOWN

    def test_empty_list_is_unknown(self):
        assert classify_error([]) == CLASS_UNKNOWN

    def test_unknown_is_retryable(self):
        """未识别错误保守视为可重试，交由上游决定。"""
        assert is_retryable("SOMETHING_NEW::x") is True

    def test_success_is_unknown_not_fatal(self):
        """SUCCESS 不是错误分类的范畴，但绝不能归到 fatal。"""
        assert classify_error("SUCCESS::调用成功") == CLASS_UNKNOWN


class TestPriorityMatrix:
    def test_risk_control_wins_over_recoverable(self):
        joined = "FAIL_SYS_TOKEN_EMPTY::x RGV587_ERROR::y"
        assert classify_error(joined) == CLASS_RISK_CONTROL


class TestExtractRet:
    def test_list_passthrough_as_strings(self):
        assert extract_ret({"ret": ["a", "b"]}) == ["a", "b"]

    def test_string_wrapped_in_list(self):
        assert extract_ret({"ret": "a single ret"}) == ["a single ret"]

    def test_missing_ret_returns_empty(self):
        assert extract_ret({}) == []
        assert extract_ret({"other": 1}) == []

    def test_none_ret_returns_empty(self):
        assert extract_ret({"ret": None}) == []

    def test_non_dict_payload_returns_empty(self):
        for bad in (None, "str", 123, ["list"]):
            assert extract_ret(bad) == []

    def test_non_string_entries_are_coerced(self):
        assert extract_ret({"ret": [1, 2]}) == ["1", "2"]

    def test_tuple_is_accepted(self):
        assert extract_ret({"ret": ("a", "b")}) == ["a", "b"]

    def test_extracted_ret_feeds_classifier(self):
        """两个函数的衔接契约：extract_ret 的输出可直接喂给 classify_error。"""
        payload = {"ret": ["FAIL_SYS_ILLEGAL_ACCESS::x"]}
        assert classify_error(extract_ret(payload)) == CLASS_FATAL
