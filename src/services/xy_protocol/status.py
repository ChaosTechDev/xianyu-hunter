"""闲鱼商品状态判定模块。

**核心设计：保守原则（fail-safe to alive）。**

商品的存活判定以详情接口返回的 ``ret`` 码为唯一权威依据，而不是 ``itemStatus``
字段——该字段是数值，语义在各开源实现间自相矛盾（有的当 1=在售，有的当 0=在售），
因此本模块只把它作为辅助展示信息（``ItemStatus.item_status_code`` /
``item_status_str``），绝不参与判定。只有在拿到**明确的死亡信号**时才判死；
风控、超时、拿不到 ``ret``、JSON 解析失败、响应为空等情况一律判**活**。

理由：把在售商品误判为已下架，用户会错过好货；把死链误判为在售，只是多浪费一次
后续抓取。前者的代价远高于后者，因此所有不确定分支都倒向 ``alive=True``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: 明确的死亡标记。``ret`` 中任意一项包含其中任一子串即判定为已下架。
#: 保持为列表便于后续扩展（例如新增其他下架提示语）。
DEAD_MARKERS: list[str] = [
    "FAIL_BIZ_ITEM_DEL_NOT_FOUND",
]

#: 存活标记。``ret`` 中任意一项包含该子串即判定为在售。
ALIVE_MARKER = "SUCCESS"

#: 各 marker 对应的中文原因说明
_REASON_DEAD = "已删除或不存在"
_REASON_ALIVE = "详情接口返回 SUCCESS"


@dataclass
class ItemStatus:
    """单个商品的存活判定结果。

    :param item_id: 商品 ID
    :param alive: 是否在售。不确定时**恒为 True**（保守原则）
    :param reason: 人类可读的判定依据
    :param raw_ret: 原始的 ``ret`` 列表，未做裁剪，便于事后排查
    :param item_status_code: ``data.itemDO.itemStatus`` 数值。语义不确定，仅作辅助参考
    :param item_status_str: ``data.itemDO.itemStatusStr`` 文案，可直接显示
    :param error: 异常 / 风控 / 未知 ret 的记录，供上游决定是否退避重试
    """

    item_id: str
    alive: bool
    reason: str
    raw_ret: list[str] = field(default_factory=list)
    item_status_code: int | None = None
    item_status_str: str | None = None
    error: str | None = None


def _normalize_ret(raw: Any) -> list[str]:
    """把各种形态的 ``ret`` 归一化成 ``list[str]``。

    mtop 的 ``ret`` 在实测中总是列表，但对端异常时也可能给出字符串或 ``None``，
    这里统一兜底，避免判定逻辑抛异常。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw]
    return [str(raw)]


def _extract_item_meta(payload: dict[str, Any]) -> tuple[int | None, str | None]:
    """从 ``payload.data.itemDO`` 中提取辅助状态字段。

    这两个字段**不参与存活判定**，只作为展示信息随结果一起返回。
    """
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, None
    item_do = data.get("itemDO")
    if not isinstance(item_do, dict):
        return None, None

    raw_code = item_do.get("itemStatus")
    code: int | None
    if isinstance(raw_code, bool):
        # bool 是 int 的子类，但作为状态码没有意义，视为缺失
        code = None
    elif isinstance(raw_code, int):
        code = raw_code
    elif isinstance(raw_code, str) and raw_code.strip().lstrip("-").isdigit():
        code = int(raw_code.strip())
    else:
        code = None

    raw_str = item_do.get("itemStatusStr")
    status_str = raw_str if isinstance(raw_str, str) else None
    return code, status_str


def judge_status(
    item_id: str,
    payload: dict[str, Any] | None,
    error: Exception | None = None,
) -> ItemStatus:
    """依据详情接口响应判定商品存活状态（纯函数）。

    判定顺序：

    1. 传入 ``error`` 或 ``payload`` 不是 dict → 判活（保守）
    2. ``ret`` 缺失或为空 → 判活（保守），并记录原因
    3. ``ret`` 命中 :data:`DEAD_MARKERS` → 判死
    4. ``ret`` 命中 :data:`ALIVE_MARKER` → 判活
    5. 其他 ret（风控 / 系统错误等）→ 判活（保守），并把 ret 写入 ``error``

    注意第 3 步优先于第 4 步：死亡标记是明确信号，优先采信。

    :param item_id: 商品 ID
    :param payload: 详情接口返回的 JSON 对象；``None`` 表示未拿到响应
    :param error: 请求或解析过程中捕获的异常，若非 ``None`` 则直接判活
    """
    if error is not None:
        return ItemStatus(
            item_id=item_id,
            alive=True,
            reason="请求异常，保守判活",
            error=f"{type(error).__name__}: {error}",
        )

    if not isinstance(payload, dict):
        return ItemStatus(
            item_id=item_id,
            alive=True,
            reason="响应为空或结构非法，保守判活",
            error=None if payload is None else f"payload 类型异常: {type(payload).__name__}",
        )

    ret = _normalize_ret(payload.get("ret"))
    code, status_str = _extract_item_meta(payload)

    if not ret:
        return ItemStatus(
            item_id=item_id,
            alive=True,
            reason="响应无 ret 字段，保守判活",
            raw_ret=ret,
            item_status_code=code,
            item_status_str=status_str,
            error="缺少 ret 字段",
        )

    joined = " ".join(ret)

    # 明确死亡信号优先
    for marker in DEAD_MARKERS:
        if marker in joined:
            return ItemStatus(
                item_id=item_id,
                alive=False,
                reason=_REASON_DEAD,
                raw_ret=ret,
                item_status_code=code,
                item_status_str=status_str,
            )

    if ALIVE_MARKER in joined:
        return ItemStatus(
            item_id=item_id,
            alive=True,
            reason=_REASON_ALIVE,
            raw_ret=ret,
            item_status_code=code,
            item_status_str=status_str,
        )

    # 风控、系统错误等未识别 ret：无法证明商品已下架，保守判活
    return ItemStatus(
        item_id=item_id,
        alive=True,
        reason="ret 非成功亦非明确死亡信号，保守判活",
        raw_ret=ret,
        item_status_code=code,
        item_status_str=status_str,
        error=joined,
    )
