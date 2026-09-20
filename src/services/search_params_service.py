"""搜索参数构建服务：提供与 mtop 搜索接口对齐的纯数据映射函数。

本模块只保留**实际在生产链路中使用的辅助函数**（由 search_filter_injection 引用）：

- ``build_search_filter`` —— 构造 ``propValueStr.searchFilter`` 字符串
- ``build_extra_filter_value`` —— 构造 ``extraFilterValue`` JSON 字符串

**为什么保留这些函数**

它们是对齐平台契约的纯数据层，不含浏览器/网络依赖，可单测、可复用。
一旦某个筛选参数拼错或漏拼，表现是「静默多抓一堆无关商品」，没有任何报错。
因此必须有充分测试覆盖其正确性。

**接口契约的来源（重要）**

本模块的字段名与拼串格式**不是猜的**，而是对齐两个互相独立的真实现：

1. ``goofish-client/src/services/mtop/builders/search-params.builder.ts``
   —— 带完整 TypeScript 类型的构造器，明确定义了 ``searchFilter`` 的拼串规则
   （``priceRange:{from},{to};`` / ``publishDays:{n};`` / ``quickFilter:{a,b,c};``，
   每段以 ``;`` 结尾）与 ``extraFilterValue`` 的 JSON 结构。
2. ``goofish_api/spider/xianyu_sign.py`` —— 可运行的 Python 实现，其
   ``data_dict`` 字段与上面逐项一致（``pageNumber``/``keyword``/``fromFilter``/
   ``rowsPerPage``/``propValueStr``/``extraFilterValue``/``userPositionJson``/
   ``searchReqFromPage='pcSearch'``）。

**这份载荷是 POST body 里 ``data`` 字段的内容，不是 URL query string**，
并且要经过 mtop 签名（见 :mod:`src.services.xy_protocol.signer`）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

# 复用价格链路的唯一权威解析实现做校验。这里只校验「是不是数字」，
# 不做任何归一化/换算，避免与权威实现产生第二套语义。
from src.services.price_history_service import parse_price_value

#: 布尔筛选开关 -> 闲鱼 ``quickFilter`` 取值。
#:
#: 取值逐条取自**平台自己的前端代码**（``p_search-index.js`` 里
#: ``checkBoxFilters`` 的「显示文案 -> value」对照表），这是最硬的来源::
#:
#:     个人闲置 -> filterPersonal        验货宝   -> filterAppraise
#:     验号担保 -> gameAccountInsurance  包邮     -> filterFreePostage
#:     超赞鱼小铺 -> filterHighLevelYxpSeller      全新 -> filterNew
#:     严选     -> inspectedPhone        转卖     -> filterOneKeyResell
#:
#: **注意 ``inspectedPhone`` 是「严选」而不是「验货宝」。** 这个名字极易误读
#: （看着像「验机/验货」），初版就照字面把 ``inspection_service`` 映射到了它，
#: 结果用户勾「验货宝」实际发的是「严选」。这类错配不会报错，
#: 只会静默返回错误的结果集，所以必须以平台的文案对照表为准，不能按字段名猜。
QUICK_FILTER_MAP: dict[str, str] = {
    "personal_only": "filterPersonal",
    "free_shipping": "filterFreePostage",
    "inspection_service": "filterAppraise",
    "certified_guarantee": "gameAccountInsurance",
    "super_shop": "filterHighLevelYxpSeller",
    "brand_new": "filterNew",
    "strict_selection": "inspectedPhone",
    "resell": "filterOneKeyResell",
}

#: 协议支持但任务表单尚未开放的筛选，供将来扩展时对照。
PROTOCOL_ONLY_FILTERS: tuple[str, ...] = (
    "certified_guarantee",
    "strict_selection",
    "resell",
)

#: ``searchFilter`` 里各段的顺序。顺序会影响服务端解析结果的稳定性，
#: 因此固定成常量，不依赖 dict 迭代顺序。
_FILTER_ORDER: tuple[str, ...] = ("priceRange", "publishDays", "quickFilter")

# 纯数字价格：整数或小数，允许正负号。刻意不接受「万」后缀与科学计数法。
_PURE_NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")

#: 默认每页条数，与接口默认一致。
_DEFAULT_ROWS_PER_PAGE = 30


def _normalize_price(value: Any) -> Optional[str]:
    """把价格输入规范化成可写入 ``searchFilter`` 的原始数字字符串，不可用时返回 ``None``。

    设计要点：

    - 去掉 ``¥`` / 逗号 / 空白，与 ``parse_price_value`` 的清洗口径一致。
    - 用 ``parse_price_value`` 校验，保证「价格链路只有一个权威实现」。
    - 但**只写原始数字字符串**，不做换算：``"1.5 万"`` 虽能被解析成 15000.0，
      把 15000 写进去就改变了用户输入的口径；而把「万」静默丢掉只写 ``"1.5"``
      会把预算砍掉四个数量级，是最危险的一类错误。本模块明确拒绝「万」，
      由上游在提交任务前完成换算。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # bool 是 int 的子类；True 在这里没有「价格 1」的语义，属脏输入
        return None
    raw = str(value).strip().replace("¥", "").replace(",", "").replace(" ", "")
    if not raw or not _PURE_NUMBER_PATTERN.match(raw):
        return None
    parsed = parse_price_value(raw)
    if parsed is None:
        return None
    # 负价格不是「更便宜」，而是一个无意义的筛选条件。放行它会让接口收到
    # priceRange:-5,，行为未定义（部分实现会当成 0，部分直接报错），
    # 因此这里直接拒绝，由调用方发现输入有误。
    if parsed < 0:
        return None
    return raw


def _normalize_positive_int(
    value: Any, *, default: int, minimum: int = 1
) -> int:
    """把输入规整成 >= ``minimum`` 的整数；非法输入回落 ``default``。"""
    if isinstance(value, bool) or value is None:
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if number < minimum:
        return minimum
    return number


def _normalize_region_parts(region: Any) -> list[str]:
    """把 ``"省/市/区"`` 拆成非空片段列表。"""
    if region is None:
        return []
    text = str(region).strip()
    if not text:
        return []
    return [part.strip() for part in text.split("/") if part.strip()]


def _normalize_division(
    province: str | None = None,
    city: str | None = None,
    area: str | None = None,
) -> dict[str, str]:
    """把「省/市/区」合成 ``divisionList`` 的一个元素。

    真实契约（平台自己的 JS，``p_search-index.js`` 中 ``eg`` 函数）::

        o = t.length
            ? [{province: n.na, city: r.na, area: e.na}]   // 选了区县
            : [{province: n.na, city: r.na}];               // 只到市

    也就是说：**一个元素承载一条完整的地域路径**，``province``/``city``/``area``
    是同一个对象里的平级字段，而不是「省一个元素、市一个元素」。

    这两者差别很大：交给服务端 ``[{"city":"浙江省"},{"city":"杭州市"}]``
    会被理解成「浙江省 **或** 杭州市」两个并列城市，而不是「杭州市（属浙江省）」。
    漏掉 ``province`` 会让服务端在按城市名匹配时可能匹配到同名城市。

    只填有值的层级；三者皆空时返回空 dict（调用方负责过滤掉）。
    """
    division: dict[str, str] = {}
    if province:
        division["province"] = province
    if city:
        division["city"] = city
    if area:
        division["area"] = area
    return division


def build_search_filter(
    *,
    quick_filters: list[str] | None = None,
    min_price: Any = None,
    max_price: Any = None,
    publish_days: Any = None,
) -> str:
    """构造 ``propValueStr.searchFilter`` 字符串。

    格式（逐条对齐权威构造器 ``search-params.builder.ts:127-148``）：

    - ``priceRange:{from},{to};`` —— ``to`` 缺省时为空串（表示不设上限）
    - ``publishDays:{n};``
    - ``quickFilter:{a,b,c};``

    没有生效的筛选时返回空字符串（接口的默认值）。
    """
    parts: dict[str, str] = {}

    low = _normalize_price(min_price)
    high = _normalize_price(max_price)
    if low is not None or high is not None:
        # 权威实现只要求 from 必填；用 0 兜底「只给了上限」的场景
        parts["priceRange"] = f"priceRange:{low or '0'},{high or ''}"

    normalized_days = None
    if publish_days is not None and not isinstance(publish_days, bool):
        text_days = str(publish_days).strip()
        if text_days.isdigit() and int(text_days) > 0:
            normalized_days = text_days
    if normalized_days:
        parts["publishDays"] = f"publishDays:{normalized_days}"

    valid_quick = [name for name in (quick_filters or []) if name]
    if valid_quick:
        parts["quickFilter"] = f"quickFilter:{','.join(valid_quick)}"

    return "".join(f"{parts[key]};" for key in _FILTER_ORDER if key in parts)


def region_parts_to_division_list(
    region_parts: list[str] | None,
) -> list[dict[str, str]]:
    """把 ``["浙江省", "杭州市"]`` 变成 ``[{"province":"浙江省","city":"杭州市"}]``。

    **一个元素 = 一条完整地域路径**（见 :func:`_normalize_division`）。
    顺序固定为 省/市/区，与前端 :file:`TaskRegionSelector.vue` 的
    ``currentPath``（``[省，市，区].join('/')``）一致。

    超过 3 段的部分丢弃：契约只有三级，多余片段无法表达，
    硬塞进 ``area`` 会让服务端拿到不存在的行政区名。
    """
    parts = [str(p).strip() for p in (region_parts or []) if str(p).strip()]
    if not parts:
        return []
    province = parts[0] if len(parts) > 0 else None
    city = parts[1] if len(parts) > 1 else None
    area = parts[2] if len(parts) > 2 else None
    division = _normalize_division(province=province, city=city, area=area)
    return [division] if division else []


def build_extra_filter_value(
    *,
    region_parts: list[str] | None = None,
    exclude_multi_places_sellers: bool = False,
    extra_division: str = "",
) -> str:
    """构造 ``extraFilterValue`` 的 JSON 字符串。

    结构（对齐平台自己的 JS ``eg`` 函数与 ``search-params.builder.ts:153-167``）::

        {"divisionList":[{"province":"浙江省","city":"杭州市"}],
         "excludeMultiPlacesSellers":"0","extraDivision":""}

    **完全无地域配置时返回 ``"{}"``**：真机抓包实测，平台在没有任何地域筛选时发出的
    就是空对象。无脑补上 ``divisionList:[]`` 会发出一个平台自己不会发的形态——
    虽然接口通常容忍，但「与浏览器发出的请求不一致」本身就是风控关注点，
    而且这类差异无法靠单元测试发现，只能靠真机对照。

    两个易错点：

    - ``divisionList`` 的每个元素是**一条完整地域路径**，不是「一层一个元素」。
    - ``excludeMultiPlacesSellers`` 是**字符串** ``"1"``/``"0"``，不是布尔值——
      权威实现显式做了字符串化，``extraDivision`` 同理。写成 ``true``/``false``
      接口不认。
    """
    division_list = region_parts_to_division_list(region_parts)
    # 只有「确实有地域信息或显式开启了跨地域卖家过滤」时才构造结构；
    # 否则与平台保持一致发空对象。
    if not division_list and not exclude_multi_places_sellers and not extra_division:
        return "{}"
    return json.dumps(
        {
            "divisionList": division_list,
            "excludeMultiPlacesSellers": "1" if exclude_multi_places_sellers else "0",
            "extraDivision": str(extra_division or ""),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
