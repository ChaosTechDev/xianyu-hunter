"""虚拟显示兜底逻辑。

背景：闲鱼会识别无头浏览器并返回「非法访问」，搜索接口不发起，采集必然超时
（真机实测，详见 ``official_search_service._launch_search_browser`` 注释）。
解法是 ``RUN_HEADLESS=false``，但容器里没有 XServer 时浏览器会直接起不来，
所以需要按需拉起 Xvfb。

这里守住三条：
1. 无头模式绝不启动 Xvfb——不能给正常配置添副作用。
2. 已有显示时不重复启动——避免抢占别人的 DISPLAY。
3. 没有 Xvfb 时只警告不抛异常——探测失败不该中断采集。
"""
from __future__ import annotations

import pytest

from src.services import virtual_display


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """每个用例都从「没起过 Xvfb」的干净状态开始。"""
    monkeypatch.setattr(virtual_display, "_xvfb_process", None)
    yield


class TestHeadlessNeverStartsXvfb:
    def test_headless_true_is_a_noop(self, monkeypatch):
        monkeypatch.setattr(virtual_display, "RUN_HEADLESS", True)
        monkeypatch.setattr(
            virtual_display, "_display_available", lambda: pytest.fail(
                "无头模式不该探测显示"
            )
        )
        virtual_display.ensure_virtual_display()
        assert virtual_display._xvfb_process is None


class TestExistingDisplayIsRespected:
    def test_skips_when_display_already_available(self, monkeypatch):
        monkeypatch.setattr(virtual_display, "RUN_HEADLESS", False)
        monkeypatch.setattr(virtual_display, "_display_available", lambda: True)
        monkeypatch.setattr(
            virtual_display.shutil, "which", lambda name: pytest.fail(
                "已有显示时不该查找 Xvfb"
            )
        )
        virtual_display.ensure_virtual_display()
        assert virtual_display._xvfb_process is None


class TestMissingXvfbOnlyWarns:
    def test_warns_and_returns_without_raising(self, monkeypatch, capsys):
        monkeypatch.setattr(virtual_display, "RUN_HEADLESS", False)
        monkeypatch.setattr(virtual_display, "_display_available", lambda: False)
        monkeypatch.setattr(virtual_display.shutil, "which", lambda name: None)

        virtual_display.ensure_virtual_display()

        assert virtual_display._xvfb_process is None
        assert "Xvfb" in capsys.readouterr().out


class TestDisplayDetection:
    def test_missing_env_var_means_unavailable(self, monkeypatch):
        monkeypatch.delenv("DISPLAY", raising=False)
        assert virtual_display._display_available() is False

    def test_remote_display_is_assumed_available(self, monkeypatch):
        """host:0 这类远程显示无法用本地 socket 判断，应交给浏览器处理。"""
        monkeypatch.setenv("DISPLAY", "10.0.0.5:0")
        assert virtual_display._display_available() is True

    def test_local_display_without_socket_is_unavailable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DISPLAY", ":77")
        monkeypatch.setattr(virtual_display.os.path, "exists", lambda p: False)
        assert virtual_display._display_available() is False
