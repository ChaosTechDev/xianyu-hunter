"""Cookie / 登录态健壮性检查（纯逻辑）。

面向闲鱼采集服务的一个高频故障：**登录态文件存在、cookie 名字都在，但实际已经不可用**。
典型三种形态：

* cookie 名字在，``value`` 却是空串或纯空白（最常见）；
* ``_m_h5_tk`` 缺失或过期——它是 mtop 协议签名用的 token，有效期只有小时级，
  一旦陈旧，所有详情接口都会返回可恢复的鉴权错误；
* Playwright ``storage_state`` 文件结构合法但 ``cookies`` 是空列表。

本模块只做判断，**不读文件、不联网、不启动浏览器**：输入是调用方已经解析好的 cookie 列表与
storage_state 字典，输出是结构化的体检报告。所有时间判断都通过显式 ``now`` 注入，
因此单元测试完全确定。

已知约束：``_m_h5_tk`` 的值形如 ``"<token>_<毫秒时间戳>"``，签名只取前半段
（见 :mod:`src.services.xy_protocol.signer`）。本模块的解析函数返回 ``(token, 毫秒时间戳)``，
便于上层判断登录态新鲜度。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

__all__ = [
    "REQUIRED_COOKIE_NAMES",
    "M_H5_TK_NAME",
    "M_H5_TK_MIN_TOKEN_LENGTH",
    "DEFAULT_STALE_WARN_HOURS",
    "EXPIRING_SOON_HOURS",
    "parse_m_h5_tk",
    "check_required_cookies",
    "assess_cookie_freshness",
    "validate_storage_state",
]


#: 闲鱼（淘宝系）登录态的关键 cookie。
#:
#: * ``_m_h5_tk``     mtop 接口签名 token，**有效期小时级**，缺失或陈旧一切详情请求都会失败
#: * ``cookie2``      淘宝账号长态标识，标识「已登录的账号」本身
#: * ``unb``          用户数字 ID，账号身份的直接依据
#: * ``_tb_token_``   表单/接口防重放 token，与 cookie2 配合使用
#: * ``sgcookie``     风控侧辅助标识，缺失容易触发验证码
#: * ``csg``          风控/登录态辅助位（部分环境才有值，故只作为「应存在」项，不做值语义校验）
REQUIRED_COOKIE_NAMES: tuple[str, ...] = (
    "_m_h5_tk",
    "cookie2",
    "unb",
    "_tb_token_",
    "sgcookie",
    "csg",
)

#: 承载 mtop 签名 token 的 cookie 名（与 ``xy_protocol.signer`` 保持一致）。
M_H5_TK_NAME = "_m_h5_tk"

#: ``_m_h5_tk`` 前半段 token 的最小长度。真实 token 是较长随机串，过短基本可判为残缺。
M_H5_TK_MIN_TOKEN_LENGTH = 8

#: ``_m_h5_tk`` 时间戳距 ``now`` 超过该小时数即视为陈旧，默认 12 小时。
DEFAULT_STALE_WARN_HOURS = 12.0

#: ``expires`` 距 ``now`` 不足该小时数即视为「即将过期」，默认 2 小时。
EXPIRING_SOON_HOURS = 2.0

#: 时间戳单位判定阈值：小于该量级视为「秒」，否则视为「毫秒」。
#: 1e11 秒对应公元 5138 年，任何真实的「秒」级时间戳都不会超过它。
_MILLISECOND_THRESHOLD = 100_000_000_000


# --------------------------------------------------------------------------------------
# 内部工具
# --------------------------------------------------------------------------------------


def _coerce_now(now: object) -> datetime:
    """把 ``now`` 归一化成 ``datetime``（naive 与 aware 都接受）。"""
    if isinstance(now, datetime):
        return now
    raise ValueError(f"now 必须是 datetime，收到：{now!r}")


def _as_aware_utc(moment: datetime) -> datetime:
    """把 naive 时间按 UTC 解释，便于与 ``expires`` 这类 Unix 时间戳统一比较。

    库内时间戳（cookie ``expires``、``_m_h5_tk`` 后半段）本身就是 UTC 秒/毫秒数，
    因此把 naive ``now`` 视为 UTC 是一致的处理方式。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _cookie_items(cookies: object) -> list[dict]:
    """过滤出结构合法的 cookie 条目（必须含 ``name`` 且为字典）。"""
    if not isinstance(cookies, (list, tuple)):
        return []
    return [item for item in cookies if isinstance(item, dict)]


def _cookie_value(item: dict) -> str:
    """取出 cookie 的原始值文本；缺失或非字符串返回空串。"""
    raw = item.get("value")
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


def _has_meaningful_value(item: dict) -> bool:
    """判断 cookie 的 ``value`` 是否「有内容」。

    ``None``、空串、纯空白（含全角空格）都算没有内容——名字在但值为空是最常见的
    「看着有其实是废的」登录态。
    """
    value = _cookie_value(item)
    if not value.strip():
        return False
    # 全角空格 / 零宽字符等不参与 ASCII strip 的空白也一并视为空
    return bool(value.replace("\u3000", "").replace("\u200b", "").strip())


def _find_cookie(cookies: list[dict], name: str) -> dict | None:
    """按名字找第一个「值非空」的 cookie；没有则返回 ``None``。"""
    for item in cookies:
        if str(item.get("name") or "") == name and _has_meaningful_value(item):
            return item
    return None


# --------------------------------------------------------------------------------------
# 交付 2.1：关键 cookie 校验
# --------------------------------------------------------------------------------------


def parse_m_h5_tk(value: str) -> tuple[str, int] | None:
    """解析 ``_m_h5_tk`` 的值为 ``(token, 毫秒时间戳)``，格式不对返回 ``None``。

    合法格式：``"<token>_<时间戳>"``，即含 ``_``、两段都非空、token 长度不小于
    :data:`M_H5_TK_MIN_TOKEN_LENGTH`、后半段整体是数字且为正。

    分隔符取**第一个** ``_``，与 :func:`src.services.xy_protocol.signer.extract_token`
    的 ``split("_")[0]`` 语义一致——真实 token 是十六进制串、本身不含下划线，两种读法结果相同；
    取第一个可以保证「解析出的 token」与「签名实际使用的 token」永远一致，避免判断新鲜度时
    看到的 token 和请求里发出去的不是同一个。因此后半段含多余 ``_`` 的输入一律判为畸形。

    时间戳单位兼容：秒级（10 位）会乘以 1000 转成毫秒，毫秒级（13 位）原样返回。
    理由是不同环境下发过的 cookie 单位存在差异，而上层的「新鲜度」判断统一以毫秒为基准。

    以下输入一律返回 ``None``：``None`` / 非字符串 / 空串 / 纯空白、没有 ``_``、
    下划线在首尾导致某段为空、时间戳非纯数字（``"abc_17x"``）、时间戳为 ``0`` 或负数、
    token 过短（``"a_1699999999999"``）、后半段多余下划线（``"abc_1_2"``）。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "_" not in text:
        return None

    token, _, raw_timestamp = text.partition("_")
    token = token.strip()
    raw_timestamp = raw_timestamp.strip()
    if not token or not raw_timestamp:
        return None
    if len(token) < M_H5_TK_MIN_TOKEN_LENGTH:
        return None
    # 只接受 ASCII 十进制数字，避免 isdigit() 放过全角/上标数字后 int() 抛异常
    if not (raw_timestamp.isascii() and raw_timestamp.isdecimal()):
        return None

    timestamp = int(raw_timestamp)
    if timestamp <= 0:
        return None
    if timestamp < _MILLISECOND_THRESHOLD:
        timestamp *= 1000
    return token, timestamp


def check_required_cookies(cookies: list[dict]) -> dict:
    """校验关键 cookie 是否齐全且值非空。

    :param cookies: ``[{"name": "_m_h5_tk", "value": "...", "expires": 1234.5}, ...]``；
        ``expires`` 可缺省。非列表/非字典项一律忽略

    :return: ``{"present": [...], "missing": [...], "ok": bool}``

    ``present`` / ``missing`` 都按 :data:`REQUIRED_COOKIE_NAMES` 的声明顺序输出，
    因此比较集合时不需要排序。判据是「名字存在 **且** 值非空非纯空白」：
    值为空的 cookie 计入 ``missing``，因为它在实际请求里等于不存在。
    """
    items = _cookie_items(cookies)
    present: list[str] = []
    missing: list[str] = []
    for name in REQUIRED_COOKIE_NAMES:
        if _find_cookie(items, name) is not None:
            present.append(name)
        else:
            missing.append(name)
    return {"present": present, "missing": missing, "ok": not missing}


# --------------------------------------------------------------------------------------
# 交付 2.2：过期与新鲜度
# --------------------------------------------------------------------------------------


def _classify_cookie_expiry(item: dict, reference: datetime) -> tuple[str, float | None]:
    """单条 cookie 的过期分类。

    :return: ``("session" | "expired" | "expiring_soon" | "valid", 剩余小时数)``。
        ``expires`` 为 ``None`` / ``<= 0`` / 非数值 -> ``"session"``：属于会话 cookie
        （浏览器关闭即失效），**不算过期**，但上层要在裁决里提示风险。
    """
    raw = item.get("expires")
    number: float | None = None
    if isinstance(raw, bool) or raw is None:
        return "session", None
    if isinstance(raw, (int, float)):
        number = float(raw)
    elif isinstance(raw, str):
        try:
            number = float(raw.strip())
        except (TypeError, ValueError):
            return "session", None
    else:
        return "session", None

    if not math.isfinite(number) or number <= 0:
        return "session", None

    expires_at = datetime.fromtimestamp(number, tz=timezone.utc)
    remaining_hours = (expires_at - reference).total_seconds() / 3600.0
    if remaining_hours <= 0:
        return "expired", remaining_hours
    if remaining_hours < EXPIRING_SOON_HOURS:
        return "expiring_soon", remaining_hours
    return "valid", remaining_hours


def assess_cookie_freshness(
    cookies: list[dict],
    *,
    now: datetime,
    stale_warn_hours: float = DEFAULT_STALE_WARN_HOURS,
) -> dict:
    """评估 cookie 的过期情况与 ``_m_h5_tk`` 的新鲜度。

    :param cookies: 已读取的 cookie 列表
    :param now: 参考时间点，必须是 ``datetime``
    :param stale_warn_hours: ``_m_h5_tk`` 陈旧阈值（小时）；非法值（<=0 / None / 非数值）
        回落到 :data:`DEFAULT_STALE_WARN_HOURS`

    :return: ``{"expired", "expiring_soon", "stale_h5_tk", "oldest_token_age_hours",
        "verdict", "reason", "session_cookies"}``

    * ``expired`` / ``expiring_soon``：cookie **名字**列表，按出现顺序去重
    * ``stale_h5_tk``：``_m_h5_tk`` 时间戳距 ``now`` 超过阈值，或该 cookie 缺失/格式错时为 ``True``
    * ``oldest_token_age_hours``：``_m_h5_tk`` 的年龄（小时，保留 1 位小数）；
      无法解析时为 ``None``
    * ``verdict``：``"healthy"`` / ``"expiring_soon"`` / ``"expired"`` / ``"suspicious"``
    * ``reason``：中文裁决说明
    * ``session_cookies``：被判定为会话 cookie 的名字列表（不计过期，但要在界面上提示风险）

    裁决优先级：``expired`` > ``suspicious``（``_m_h5_tk`` 陈旧/缺失/格式错）>
    ``expiring_soon`` > ``healthy``。把 ``suspicious`` 排在 ``expiring_soon`` 之前，
    是因为 token 陈旧会让请求立刻失败，属于更紧迫的可用性风险。
    """
    reference = _as_aware_utc(_coerce_now(now))
    items = _cookie_items(cookies)

    threshold = stale_warn_hours
    if isinstance(threshold, bool) or threshold is None:
        threshold = DEFAULT_STALE_WARN_HOURS
    elif isinstance(threshold, str):
        try:
            threshold = float(threshold.strip())
        except (TypeError, ValueError):
            threshold = DEFAULT_STALE_WARN_HOURS
    elif not isinstance(threshold, (int, float)):
        threshold = DEFAULT_STALE_WARN_HOURS
    if not isinstance(threshold, (int, float)) or not math.isfinite(float(threshold)) or float(threshold) <= 0:
        threshold = DEFAULT_STALE_WARN_HOURS
    threshold = float(threshold)

    expired: list[str] = []
    expiring_soon: list[str] = []
    session_cookies: list[str] = []

    for item in items:
        name = str(item.get("name") or "")
        if not name or not _has_meaningful_value(item):
            # 空值 cookie 没有登录态语义，不参与过期统计（缺失由 check_required_cookies 报）
            continue
        state, _ = _classify_cookie_expiry(item, reference)
        if state == "expired":
            if name not in expired:
                expired.append(name)
        elif state == "expiring_soon":
            if name not in expiring_soon:
                expiring_soon.append(name)
        elif state == "session":
            if name not in session_cookies:
                session_cookies.append(name)

    h5_item = _find_cookie(items, M_H5_TK_NAME)
    oldest_token_age_hours: float | None = None
    if h5_item is None:
        stale_h5_tk = True
    else:
        parsed = parse_m_h5_tk(_cookie_value(h5_item))
        if parsed is None:
            stale_h5_tk = True
        else:
            _, issued_at_ms = parsed
            issued_at = datetime.fromtimestamp(issued_at_ms / 1000.0, tz=timezone.utc)
            age_hours = (reference - issued_at).total_seconds() / 3600.0
            oldest_token_age_hours = round(age_hours, 1)
            stale_h5_tk = age_hours > threshold

    if expired:
        verdict = "expired"
        reason = (
            f"存在已过期的 cookie：{', '.join(expired)}；登录态已经失效，"
            "需要重新登录并保存 storage_state。"
        )
        if stale_h5_tk:
            reason += " 同时 _m_h5_tk 缺失或已陈旧。"
    elif stale_h5_tk:
        verdict = "suspicious"
        if h5_item is None:
            detail = "缺少 _m_h5_tk 或它的值为空"
        elif oldest_token_age_hours is None:
            detail = "_m_h5_tk 格式无法解析（期望 <token>_<毫秒时间戳>）"
        else:
            detail = f"_m_h5_tk 已签发 {oldest_token_age_hours} 小时，超过 {threshold} 小时阈值"
        reason = (
            f"登录态可疑：{detail}。该 token 有效期只有小时级，mtop 详情接口会因此报可恢复的鉴权错误，"
            "建议先刷新 _m_h5_tk 再继续采集。"
        )
    elif expiring_soon:
        verdict = "expiring_soon"
        reason = (
            f"以下 cookie 将在 {EXPIRING_SOON_HOURS} 小时内过期：{', '.join(expiring_soon)}；"
            "建议在过期前完成刷新，避免长跑任务中途掉登录。"
        )
    else:
        verdict = "healthy"
        reason = "关键 cookie 未过期，_m_h5_tk 仍然新鲜，登录态可用。"

    if session_cookies:
        reason += (
            f" 另有 {len(session_cookies)} 个会话 cookie（expires 为空/<=0）："
            f"{', '.join(session_cookies)}；它们随浏览器关闭失效，长跑场景存在掉线风险。"
        )

    return {
        "expired": expired,
        "expiring_soon": expiring_soon,
        "stale_h5_tk": stale_h5_tk,
        "oldest_token_age_hours": oldest_token_age_hours,
        "verdict": verdict,
        "reason": reason,
        "session_cookies": session_cookies,
    }


# --------------------------------------------------------------------------------------
# 交付 2.3：storage_state 快照校验
# --------------------------------------------------------------------------------------


def validate_storage_state(payload: dict) -> dict:
    """校验 Playwright ``storage_state`` 快照的结构是否可用。

    :param payload: 已读取并解析好的字典，形如 ``{"cookies": [...], "origins": [...]}``
    :return: ``{"valid": bool, "reason": str, "cookie_count": int, "origin_count": int}``

    判定为无效的情形（``reason`` 均为中文说明）：

    * ``payload`` 不是字典
    * ``cookies`` 缺失 / 不是列表
    * ``cookies`` 是空列表——文件存在但没有任何 cookie，属于最常见的「看着有文件其实是废的」
    * ``cookies`` 里没有任何一条「名字非空且值非空」的条目——全是占位空值同样不可用
    * 非空 ``cookies`` 里存在结构非法的条目（不是字典或缺 ``name``）

    ``origins`` 只做宽容校验：缺失或不是列表时按空列表计，不因此判无效，
    因为闲鱼登录态的有效性完全由 cookie 承载，``origins``（localStorage 快照）缺失不影响接口调用。
    """
    if not isinstance(payload, dict):
        return {
            "valid": False,
            "reason": f"storage_state 顶层结构必须是字典（JSON 对象），实际是 {type(payload).__name__}。",
            "cookie_count": 0,
            "origin_count": 0,
        }

    cookies = payload.get("cookies")
    if cookies is None:
        return {
            "valid": False,
            "reason": "storage_state 缺少 cookies 字段；没有 cookie 就没有登录态。",
            "cookie_count": 0,
            "origin_count": 0,
        }
    if not isinstance(cookies, (list, tuple)):
        return {
            "valid": False,
            "reason": f"storage_state.cookies 必须是列表，实际是 {type(cookies).__name__}；"
            "该文件可能不是 Playwright 导出的 storage_state。",
            "cookie_count": 0,
            "origin_count": 0,
        }
    if len(cookies) == 0:
        return {
            "valid": False,
            "reason": "storage_state.cookies 是空列表：文件存在但登录态无效，"
            "通常是导出了匿名会话或登录流程未完成。",
            "cookie_count": 0,
            "origin_count": 0,
        }

    malformed = [item for item in cookies if not isinstance(item, dict) or not str(item.get("name") or "")]
    if malformed:
        return {
            "valid": False,
            "reason": f"storage_state.cookies 中有 {len(malformed)} 条结构非法（不是字典或缺少 name），"
            "文件内容不完整。",
            "cookie_count": len(cookies),
            "origin_count": 0,
        }

    meaningful = [item for item in cookies if _has_meaningful_value(item)]
    if not meaningful:
        return {
            "valid": False,
            "reason": f"storage_state.cookies 的 {len(cookies)} 条 cookie 值全为空或纯空白，"
            "登录态实际不可用。",
            "cookie_count": len(cookies),
            "origin_count": 0,
        }

    origins = payload.get("origins")
    origin_count = len(origins) if isinstance(origins, (list, tuple)) else 0

    return {
        "valid": True,
        "reason": f"storage_state 结构合法：{len(cookies)} 条 cookie（其中 {len(meaningful)} 条值非空），"
        f"{origin_count} 条 origin。",
        "cookie_count": len(cookies),
        "origin_count": origin_count,
    }
