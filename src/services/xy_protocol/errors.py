"""闲鱼 mtop 错误码分类模块。

把响应里的 ``ret`` 码归类成四档，供上游决定「重取 cookie」「退避等待」「直接放弃」::

    recoverable  -> 可恢复：cookie / token 问题，重新取一次 _m_h5_tk 即可重试
    risk_control -> 风控：请求过于频繁或被判定异常，需要退避、换账号或人工介入
    fatal        -> 不可恢复：账号或设备已被限制，重取 cookie 也无济于事
    unknown      -> 未识别：保守按「可重试」对待由上游自行决定

关键词全部来自 4 个开源实现的交叉验证，包含闲鱼真实返回的错误码拼写。
"""

from __future__ import annotations

from typing import Any

#: 可恢复错误码关键词（重取 cookie / token 后可重试）
RECOVERABLE_KEYWORDS: tuple[str, ...] = (
    "FAIL_SYS_TOKEN_EMPTY",
    "FAIL_SYS_TOKEN_ILLEGAL",
    "FAIL_SYS_SESSION_EXPIRED",
    # 注意：闲鱼服务端实际返回的就是 FAIL_SYS_TOKEN_EXOIRED（少了 P），
    # 同时兼容拼写正确的 FAIL_SYS_TOKEN_EXPIRED，两种都要覆盖。
    "FAIL_SYS_TOKEN_EXOIRED",
    "FAIL_SYS_TOKEN_EXPIRED",
)

#: 风控错误码关键词（需退避或人工介入）
RISK_CONTROL_KEYWORDS: tuple[str, ...] = (
    "RGV587_ERROR",
    "FAIL_SYS_USER_VALIDATE",
    "/punish",
)

#: 不可恢复错误码关键词（重取凭证无效）
FATAL_KEYWORDS: tuple[str, ...] = (
    "FAIL_SYS_ILLEGAL_ACCESS",
)

#: 分类结果字面量
CLASS_RECOVERABLE = "recoverable"
CLASS_RISK_CONTROL = "risk_control"
CLASS_FATAL = "fatal"
CLASS_UNKNOWN = "unknown"


def _normalize(ret: list[str] | str | None) -> str:
    """把 ``ret`` 归一化成用于关键词匹配的单个字符串。"""
    if ret is None:
        return ""
    if isinstance(ret, str):
        return ret
    if isinstance(ret, (list, tuple)):
        return " ".join(str(item) for item in ret)
    return str(ret)


def classify_error(ret: list[str] | str) -> str:
    """根据 ``ret`` 内容返回错误分类。

    匹配优先级：``fatal`` > ``risk_control`` > ``recoverable`` > ``unknown``。
    正常情况下三类关键词互斥；优先级只用于对端返回多个 ret 时的确定性裁决
    （不可恢复信号最不可逆转，优先采信）。

    :param ret: 详情接口返回的 ``ret``，可以是列表或单个字符串
    :return: ``"recoverable"`` / ``"risk_control"`` / ``"fatal"`` / ``"unknown"``
    """
    joined = _normalize(ret)

    if not joined:
        return CLASS_UNKNOWN

    for keyword in FATAL_KEYWORDS:
        if keyword in joined:
            return CLASS_FATAL

    for keyword in RISK_CONTROL_KEYWORDS:
        if keyword in joined:
            return CLASS_RISK_CONTROL

    for keyword in RECOVERABLE_KEYWORDS:
        if keyword in joined:
            return CLASS_RECOVERABLE

    return CLASS_UNKNOWN


def is_retryable(ret: list[str] | str) -> bool:
    """辅助判断：该错误是否值得重试。

    ``recoverable`` 与 ``unknown`` 值得重试；``risk_control`` 需要退避后再试，
    这里也视为可重试，但上游应拉长间隔；``fatal`` 直接放弃。
    """
    return classify_error(ret) != CLASS_FATAL


def extract_ret(payload: Any) -> list[str]:
    """从响应体里安全取出 ``ret`` 列表，取不到时返回空列表。

    供调用方在 :func:`classify_error` 之前做归一化，避免到处写类型判断。
    """
    if not isinstance(payload, dict):
        return []
    raw = payload.get("ret")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return [str(raw)]
