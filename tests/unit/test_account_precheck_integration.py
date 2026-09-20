"""``account_check_service`` 本地预检的接入测试。

``session_hardening_service`` 是纯逻辑模块，本文件守住「它真的接进了账号检测」
以及最关键的**方向性**约束：

预检只能**否证**，不能**证成**。cookie 名字齐全、未过期，并不代表服务端接受
这个登录态（cookie 可能在服务端已被吊销），因此预检通过时必须继续走浏览器
校验。若把它当成「健康」的充分证据，就会把真正失效的登录态误报为可用，
进而让采集任务在启动后才发现登录失败。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.services.account_check_service import _precheck_storage_state  # noqa: E402


def _write(directory: str, name: str, payload) -> str:
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        if isinstance(payload, str):
            handle.write(payload)
        else:
            json.dump(payload, handle)
    return path


def _healthy_cookies() -> list[dict]:
    """构造一份关键 cookie 齐全且未过期的 cookies 列表。"""
    future = time.time() + 86400
    token = f"tok1234567890_{int(time.time() * 1000)}"
    values = {
        "_m_h5_tk": token,
        "cookie2": "c2",
        "unb": "123456789",
        "_tb_token_": "tb",
        "sgcookie": "sg",
        "csg": "cs",
    }
    return [
        {"name": name, "value": value, "expires": future}
        for name, value in values.items()
    ]


class TestPrecheckOnlyDenies:
    def test_healthy_cookies_return_none_so_browser_check_still_runs(self):
        """核心护栏：预检通过必须返回 None，不能直接判健康。"""
        directory = tempfile.mkdtemp()
        path = _write(directory, "good.json", {"cookies": _healthy_cookies(), "origins": []})
        assert _precheck_storage_state(path) is None

    def test_empty_cookie_list_short_circuits(self):
        directory = tempfile.mkdtemp()
        path = _write(directory, "empty.json", {"cookies": [], "origins": []})
        result = _precheck_storage_state(path)
        assert result is not None
        assert result["status"] == "login_required"
        assert result["available"] is False
        assert result["precheck"] is True

    def test_missing_required_cookie_short_circuits_and_names_it(self):
        directory = tempfile.mkdtemp()
        cookies = [c for c in _healthy_cookies() if c["name"] != "unb"]
        path = _write(directory, "miss.json", {"cookies": cookies, "origins": []})
        result = _precheck_storage_state(path)
        assert result is not None
        assert result["status"] == "login_required"
        assert "unb" in result["detail"]

    def test_empty_cookie_value_is_treated_as_missing(self):
        """名字在但值为空是最常见的「看着有其实是废的」登录态。"""
        directory = tempfile.mkdtemp()
        cookies = _healthy_cookies()
        for cookie in cookies:
            if cookie["name"] == "_m_h5_tk":
                cookie["value"] = "   "
        path = _write(directory, "blank.json", {"cookies": cookies, "origins": []})
        result = _precheck_storage_state(path)
        assert result is not None
        assert "_m_h5_tk" in result["detail"]

    def test_expired_cookies_short_circuit(self):
        directory = tempfile.mkdtemp()
        past = time.time() - 86400
        cookies = [
            {"name": c["name"], "value": c["value"], "expires": past}
            for c in _healthy_cookies()
        ]
        path = _write(directory, "expired.json", {"cookies": cookies, "origins": []})
        result = _precheck_storage_state(path)
        assert result is not None
        assert result["status"] == "login_required"


class TestPrecheckPreservesOriginalBehavior:
    def test_unparseable_json_returns_none_so_full_check_reports_the_error(self):
        """文件不是合法 JSON 时不做判断，交给完整校验流程——避免改变既有错误语义。

        分界在于「能否确定结论」：
        - **无法解析** -> 我们并不知道内容是什么，返回 None 交由原流程报错；
        - **能解析但结构明显不对**（下面的数组/标量用例）-> 可以确定它不是合法
          ``storage_state``，此时短路判失效是安全的，因为它同样只是「否证」。
        """
        directory = tempfile.mkdtemp()
        path = _write(directory, "bad.json", "{not valid json")
        assert _precheck_storage_state(path) is None

    @pytest.mark.parametrize("payload", ["[]", "null", '"str"'])
    def test_parseable_but_non_dict_short_circuits_as_invalid(self, payload):
        """合法 JSON 但不是对象 -> 可确定结构不合法，短路判失效（仍属否证）。"""
        directory = tempfile.mkdtemp()
        path = _write(directory, "nondict.json", payload)
        result = _precheck_storage_state(path)
        assert result is not None
        assert result["status"] == "login_required"
        assert result["available"] is False

    def test_missing_file_returns_none(self):
        assert _precheck_storage_state(os.path.join(tempfile.mkdtemp(), "nope.json")) is None

    def test_cookies_key_absent_short_circuits_as_invalid(self):
        """缺 cookies 键 -> 可确定结构不合法，短路判失效；但绝不能抛异常。"""
        directory = tempfile.mkdtemp()
        path = _write(directory, "nocookies.json", {"origins": []})
        result = _precheck_storage_state(path)
        assert result is not None
        assert result["status"] == "login_required"

    def test_stale_token_alone_does_not_short_circuit(self):
        """``_m_h5_tk`` 陈旧属于「可疑」而非「确定失效」，不应短路。

        陈旧的 token 仍可能可用，直接判死会让采集错失本可用的账号；
        真正失效会在浏览器校验里暴露。
        """
        directory = tempfile.mkdtemp()
        stale_ms = int((time.time() - 48 * 3600) * 1000)
        cookies = _healthy_cookies()
        for cookie in cookies:
            if cookie["name"] == "_m_h5_tk":
                cookie["value"] = f"tok1234567890_{stale_ms}"
        path = _write(directory, "stale.json", {"cookies": cookies, "origins": []})
        assert _precheck_storage_state(path) is None
