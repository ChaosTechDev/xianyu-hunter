"""无头兜底：当要求有头运行但环境没有 DISPLAY 时，自动拉起 Xvfb。

起因（真机实测）：闲鱼会识别无头浏览器并返回「非法访问」，搜索接口不发起，
采集必然超时。解法是 RUN_HEADLESS=false，但容器里没有 XServer，
headless=False 会直接抛 TargetClosedError（Missing X server or $DISPLAY）。

这里不引入新的系统依赖：Xvfb 由 playwright install --with-deps 带入。
仅在没有可用显示时才启动，并且**只启动一次**（模块级单例），
进程退出时随主进程一起结束。
"""
from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import time

from src.config import RUN_HEADLESS

#: 虚拟显示编号。挑一个不常用的，避免和外部已存在的 X 服务撞车。
_VIRTUAL_DISPLAY = ":99"

_xvfb_process: subprocess.Popen | None = None


def _display_available() -> bool:
    """当前环境是否已有可用的 X 显示。"""
    display = os.environ.get("DISPLAY")
    if not display:
        return False
    if display.startswith(":") and display[1:].split(".")[0].isdigit():
        num = display[1:].split(".")[0]
        # X11 的 abstract socket 形式，存在即可认为显示可用
        if os.path.exists(f"/tmp/.X11-unix/X{num}"):
            return True
    # 远程显示（host:0）无法用文件判断，交给浏览器自行处理
    return not display.startswith(":")


def ensure_virtual_display() -> None:
    """有头运行且没有显示时，自动拉起 Xvfb 并设置 DISPLAY。

    失败不抛异常：调用方宁愿尝试启动浏览器并在那里看到真实错误，
    也不要因为探测显示这一步就让整条采集链路中断。
    """
    global _xvfb_process

    if RUN_HEADLESS or _xvfb_process is not None or _display_available():
        return

    xvfb = shutil.which("Xvfb")
    if not xvfb:
        print(
            "警告：RUN_HEADLESS=false 但环境没有 Xvfb，浏览器将无法启动。"
            "请改用 RUN_HEADLESS=true，或在镜像中安装 xvfb。"
        )
        return

    try:
        _xvfb_process = subprocess.Popen(
            [xvfb, _VIRTUAL_DISPLAY, "-screen", "0", "1920x1080x24", "-nolisten", "tcp"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.environ["DISPLAY"] = _VIRTUAL_DISPLAY
        # Xvfb 需要一点时间创建 socket；轮询而不是死等，通常 100ms 内就绪。
        socket_path = f"/tmp/.X11-unix/X{_VIRTUAL_DISPLAY[1:]}"
        for _ in range(50):
            if os.path.exists(socket_path):
                break
            time.sleep(0.1)
        print(f"已启动虚拟显示 Xvfb {_VIRTUAL_DISPLAY}（对应 RUN_HEADLESS=false）")
        atexit.register(_stop_virtual_display)
    except Exception as exc:
        print(f"启动 Xvfb 失败: {type(exc).__name__}: {exc}")


def _stop_virtual_display() -> None:
    global _xvfb_process
    if _xvfb_process is None:
        return
    try:
        _xvfb_process.terminate()
        _xvfb_process.wait(timeout=5)
    except Exception:
        pass
    finally:
        _xvfb_process = None
