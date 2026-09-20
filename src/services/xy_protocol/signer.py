"""闲鱼 mtop 协议签名模块。

签名算法（已由 4 个开源实现交叉验证，纯 MD5，无加密库依赖）::

    token = _m_h5_tk.split("_")[0]                      # "abc_1699999999999" -> "abc"
    t_ms  = int(time.time() * 1000)                     # 必须是真毫秒
    data  = json.dumps(params, separators=(",", ":"))   # 必须紧凑无空格
    sign  = md5(f"{token}&{t_ms}&{APP_KEY}&{data}")

设计约束：

* 只依赖标准库（``hashlib`` / ``json`` / ``time``），不引入任何新依赖。
* 除 :func:`now_ms` 外全部是纯函数：不发网络请求、不写文件、不改入参。
  时间读取集中在一个入口，测试时可注入固定 ``t_ms`` 获得确定性结果。
"""

from __future__ import annotations

import hashlib
import json
import time

#: mtop 请求固定使用的 appKey（闲鱼 H5 端）
APP_KEY = "34839810"

#: 承载 token 的 cookie 名
M_H5_TK_COOKIE_NAME = "_m_h5_tk"


def now_ms() -> int:
    """返回当前真实毫秒时间戳。

    本模块唯一读取系统时钟的地方，其余函数均为纯函数。
    测试签名时请显式传入 ``t_ms``，不要依赖调用时刻。
    """
    return int(time.time() * 1000)


def extract_token(m_h5_tk: str) -> str:
    """从 ``_m_h5_tk`` cookie 值中取出 token。

    cookie 形如 ``"abc123_1699999999999"``，下划线后是签发时间，签名只用前半段。
    传入空值 / ``None`` 时返回空字符串，由调用方决定是否重新取 cookie。
    """
    if not m_h5_tk:
        return ""
    return str(m_h5_tk).strip().split("_")[0]


def compact_json(obj: object) -> str:
    """把对象强制序列化为紧凑 JSON（无任何多余空格），作为签名原文的一部分。

    ``separators=(",", ":")`` 是签名正确性的必要条件：``json.dumps`` 默认会在
    ``", "`` 与 ``": "`` 处插入空格，导致签名原文与真实请求不一致。

    ``ensure_ascii`` 保持 ``json.dumps`` 的默认值（``True``，非 ASCII 会转义成
    ``\\uXXXX``）。四个参考实现均使用默认行为，此处保持一致；若后续实测发现闲鱼
    要求原样 UTF-8 中文，改动点只有这一行。
    """
    return json.dumps(obj, separators=(",", ":"))


def build_sign(token: str, t_ms: int, data: str) -> str:
    """计算 mtop 签名。

    :param token: :func:`extract_token` 的返回值
    :param t_ms: 毫秒时间戳，必须来自 ``int(time.time() * 1000)``
    :param data: 业务参数的紧凑 JSON 字符串，必须来自 :func:`compact_json`
    :return: 32 位小写十六进制 MD5
    """
    raw = f"{token}&{t_ms}&{APP_KEY}&{data}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def sign_params(params: dict[str, object], token: str, t_ms: int | None = None) -> dict[str, object]:
    """返回可直接作为 mtop 请求参数的完整字典。

    在原业务参数之上追加 ``sign`` / ``t`` / ``appKey`` 三个字段。签名原文只覆盖
    **业务参数**（即传入的 ``params``），三个附加字段本身不参与签名，这与 mtop
    标准做法一致。

    :param params: 业务参数（不含 sign/t/appKey）
    :param token: 从 ``_m_h5_tk`` 取出的 token
    :param t_ms: 毫秒时间戳；``None`` 时取当前时间（唯一非确定性路径）
    :return: 新的参数字典，原 ``params`` 不被修改
    """
    effective_t = now_ms() if t_ms is None else int(t_ms)
    data = compact_json(params)
    signed = dict(params)
    signed["sign"] = build_sign(token, effective_t, data)
    signed["t"] = effective_t
    signed["appKey"] = APP_KEY
    return signed
