"""闲鱼 mtop 协议支撑层：签名、状态判定、错误分类。

三个子模块都是**纯函数**实现，不发任何网络请求、不依赖数据库、不读配置文件，
因此可以在测试里直接调用断言。

关于 httpx：本包按设计不含任何网络代码，所以没有 import httpx。项目既有的 HTTP
调用方（``src/infrastructure/external`` 下的 client）负责发请求，拿到响应后把
payload 交给 :func:`src.services.xy_protocol.status.judge_status` 和
:func:`src.services.xy_protocol.errors.classify_error` 判定即可。这样签名与判定
逻辑可以脱离网络独立验证。

用法::

    from src.services.xy_protocol import (
        extract_token, sign_params, judge_status, classify_error,
    )

    token = extract_token(cookies.get("_m_h5_tk", ""))
    params = sign_params({"itemId": "123456"}, token)   # 自动加 sign/t/appKey
    ...
    status = judge_status("123456", payload)
    if not status.alive:
        ...   # 只有拿到明确死亡信号才会走到这里
"""

from __future__ import annotations

from .errors import (
    CLASS_FATAL,
    CLASS_RECOVERABLE,
    CLASS_RISK_CONTROL,
    CLASS_UNKNOWN,
    FATAL_KEYWORDS,
    RECOVERABLE_KEYWORDS,
    RISK_CONTROL_KEYWORDS,
    classify_error,
    extract_ret,
    is_retryable,
)
from .signer import (
    APP_KEY,
    M_H5_TK_COOKIE_NAME,
    build_sign,
    compact_json,
    extract_token,
    now_ms,
    sign_params,
)
from .status import (
    ALIVE_MARKER,
    DEAD_MARKERS,
    ItemStatus,
    judge_status,
)

__all__ = [
    # signer
    "APP_KEY",
    "M_H5_TK_COOKIE_NAME",
    "build_sign",
    "compact_json",
    "extract_token",
    "now_ms",
    "sign_params",
    # status
    "ALIVE_MARKER",
    "DEAD_MARKERS",
    "ItemStatus",
    "judge_status",
    # errors
    "CLASS_FATAL",
    "CLASS_RECOVERABLE",
    "CLASS_RISK_CONTROL",
    "CLASS_UNKNOWN",
    "FATAL_KEYWORDS",
    "RECOVERABLE_KEYWORDS",
    "RISK_CONTROL_KEYWORDS",
    "classify_error",
    "extract_ret",
    "is_retryable",
]
