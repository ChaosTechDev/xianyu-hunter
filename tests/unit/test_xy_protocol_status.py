"""xy_protocol 状态判定测试。

**核心契约：保守原则（fail-safe to alive）。** 只有在拿到**明确死亡信号**时才判死，
其余所有不确定情况（风控、超时、缺 ret、结构异常）一律判活。

理由：把在售商品误判为已下架，用户会错过好货；把死链误判为在售，只多浪费一次抓取。
这个不对称是本模块最容易写错的地方，因此每个不确定分支都有独立用例。
"""
from __future__ import annotations

import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.services.xy_protocol import judge_status
from src.services.xy_protocol.status import ALIVE_MARKER, DEAD_MARKERS


# 真实抓包得到的两个权威 ret 取值
RET_ALIVE = "SUCCESS::调用成功"
RET_DEAD = "FAIL_BIZ_ITEM_DEL_NOT_FOUND::您要看的宝贝不存在或已被删除啦!"


class TestExplicitDeathSignal:
    def test_dead_ret_is_judged_dead(self):
        status = judge_status("123", {"ret": [RET_DEAD]})
        assert status.alive is False
        assert status.reason

    def test_dead_ret_as_bare_string_also_works(self):
        status = judge_status("123", {"ret": RET_DEAD})
        assert status.alive is False

    def test_dead_marker_wins_over_alive_marker(self):
        """同时出现时死亡标记优先（明确信号优先采信）。"""
        status = judge_status("123", {"ret": [RET_ALIVE, RET_DEAD]})
        assert status.alive is False

    def test_raw_ret_preserved_for_debugging(self):
        status = judge_status("123", {"ret": [RET_DEAD]})
        assert status.raw_ret == [RET_DEAD]
        assert status.item_id == "123"


class TestExplicitAliveSignal:
    def test_alive_ret_is_judged_alive(self):
        status = judge_status("123", {"ret": [RET_ALIVE]})
        assert status.alive is True
        assert status.error is None

    def test_alive_marker_constant_is_success(self):
        assert ALIVE_MARKER == "SUCCESS"


class TestConservativeFallback:
    """以下每个分支都**必须判活**，这是本模块存在的意义。"""

    def test_unknown_ret_is_alive(self):
        status = judge_status("123", {"ret": ["FAIL_SYS_SOMETHING_NEW"]})
        assert status.alive is True
        assert status.error is not None, "未识别 ret 应记录到 error 供上游排查"

    def test_risk_control_ret_is_alive(self):
        """风控说明不了商品是否下架，必须判活。"""
        status = judge_status("123", {"ret": ["RGV587_ERROR::小宝贝被挤爆啦"]})
        assert status.alive is True

    def test_punish_ret_is_alive(self):
        status = judge_status("123", {"ret": ["/punish"]})
        assert status.alive is True

    def test_missing_ret_is_alive(self):
        status = judge_status("123", {"data": {}})
        assert status.alive is True
        assert status.raw_ret == []

    def test_empty_ret_list_is_alive(self):
        status = judge_status("123", {"ret": []})
        assert status.alive is True

    def test_none_payload_is_alive(self):
        status = judge_status("123", None)
        assert status.alive is True

    def test_non_dict_payload_is_alive(self):
        for bad in ("a string", 123, ["list"], 3.14):
            status = judge_status("123", bad)
            assert status.alive is True, f"payload={bad!r} 应保守判活"

    def test_request_error_is_alive(self):
        status = judge_status("123", None, error=TimeoutError("timed out"))
        assert status.alive is True
        assert "TimeoutError" in status.error

    def test_error_takes_precedence_over_payload(self):
        """即使拿到了 payload，只要有异常就判活（异常说明响应不可信）。"""
        status = judge_status("123", {"ret": [RET_DEAD]}, error=RuntimeError("boom"))
        assert status.alive is True

    def test_empty_dict_payload_is_alive(self):
        assert judge_status("123", {}).alive is True

    def test_dead_marker_list_is_not_treated_as_alive(self):
        """防回归：DEAD_MARKERS 与 ALIVE_MARKER 不得互相包含。"""
        for marker in DEAD_MARKERS:
            assert ALIVE_MARKER not in marker


class TestItemStatusMetaIsAuxiliaryOnly:
    """``itemStatus`` 语义在各实现间自相矛盾，只作展示，不参与判定。"""

    def test_item_status_fields_extracted_for_display(self):
        payload = {
            "ret": [RET_ALIVE],
            "data": {"itemDO": {"itemStatus": 0, "itemStatusStr": "在售"}},
        }
        status = judge_status("123", payload)
        assert status.item_status_code == 0
        assert status.item_status_str == "在售"

    def test_item_status_does_not_override_dead_ret(self):
        """itemStatus 说"在售"，但 ret 是明确死亡信号 → 仍判死。"""
        payload = {
            "ret": [RET_DEAD],
            "data": {"itemDO": {"itemStatus": 0, "itemStatusStr": "在售"}},
        }
        assert judge_status("123", payload).alive is False

    def test_item_status_does_not_override_alive_ret(self):
        """itemStatus 说"已售出"，但 ret 是 SUCCESS → 仍判活（保守）。"""
        payload = {
            "ret": [RET_ALIVE],
            "data": {"itemDO": {"itemStatus": 1, "itemStatusStr": "已售出"}},
        }
        assert judge_status("123", payload).alive is True

    def test_missing_item_do_yields_none_meta(self):
        status = judge_status("123", {"ret": [RET_ALIVE]})
        assert status.item_status_code is None
        assert status.item_status_str is None

    def test_item_status_as_numeric_string_is_parsed(self):
        payload = {
            "ret": [RET_ALIVE],
            "data": {"itemDO": {"itemStatus": "2"}},
        }
        assert judge_status("123", payload).item_status_code == 2

    def test_item_status_as_bool_is_treated_as_missing(self):
        """bool 是 int 子类，但作为状态码无意义。"""
        payload = {
            "ret": [RET_ALIVE],
            "data": {"itemDO": {"itemStatus": True}},
        }
        assert judge_status("123", payload).item_status_code is None

    def test_malformed_item_do_does_not_crash(self):
        payload = {"ret": [RET_ALIVE], "data": {"itemDO": "not a dict"}}
        assert judge_status("123", payload).alive is True
        payload2 = {"ret": [RET_ALIVE], "data": "not a dict"}
        assert judge_status("123", payload2).alive is True
