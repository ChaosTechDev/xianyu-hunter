"""把闲鱼原生筛选注入到浏览器自己发出的搜索请求中。

**为什么不是「拼 URL 参数」**

闲鱼搜索结果页的原生筛选（个人闲置 / 包邮 / 验货宝 / 全新 / 区域 / 价格区间）
**不通过 URL query 传参**。页面加载后由前端 JS 自己发一个 POST 到::

    https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/

业务参数放在 body 的 ``data`` 字段（紧凑 JSON），``sign`` / ``t`` / ``appKey``
放在 URL query。因此「往搜索 URL 上拼 ``&personal_only=true``」是无效的——
服务端只会忽略不认识的 query 键，表现为**筛选静默失效**。

现有实现走的是「用 Playwright 点击页面上的筛选按钮」，靠文案选择器定位：

- ``page.click("text=个人闲置")`` / ``page.click("text=包邮")``
- 区域筛选靠 ``.areaWrap--FaZHsn8E`` 这类 **CSS Module 哈希类名**

哈希类名由平台前端构建时随机生成，**一次改版就失效**，且失效表现是静默跳过
（见主报告的 issue #1）。点击式筛选还额外要求 UI 布局稳定、元素可见可点，
每一步都要等一次搜索响应，慢且脆。

**本模块的做法**：拦截浏览器自己发出的那个 POST，把筛选合并进 ``data``，
用请求自带的 ``t`` 与 cookie 里的 token **重算签名**，再放行。

好处：
- 不依赖任何 DOM 结构或哈希类名，平台改前端样式不影响
- 一次请求就带全筛选，不需要额外点击与等待
- 签名算法已验证为纯 MD5（见 :mod:`src.services.xy_protocol.signer`），无设备指纹依赖

**失败处理原则：失败即放行。** 本模块是纯优化，注入失败（payload 结构不认识、
签名算不出、Playwright 抛错）时必须把**原始请求原样放行**，绝不能中断采集。
因此所有异常都在 handler 内部消化并记录，不向外抛。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.services.search_params_service import (
    QUICK_FILTER_MAP,
    build_extra_filter_value,
    build_search_filter,
)
from src.services.xy_protocol.signer import build_sign, compact_json, extract_token

#: 搜索接口 URL 片段。与 :data:`src.services.search_pagination.SEARCH_RESULTS_API_FRAGMENT` 同源，
#: 这里独立定义避免把 Playwright 依赖引进纯逻辑模块（本模块要能在无浏览器环境下单测）。
SEARCH_API_FRAGMENT = "/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"

#: 前端「新发布」下拉的中文文案 -> 接口 ``publishDays`` 取值（**时间窗**分支）。
#:
#: 文案逐条取自 ``web-ui/src/components/tasks/TaskForm.vue`` 的 ``SelectItem``
#: value（前端直接把中文文案当值存入任务），取值范围与 ``goofish-client``
#: 的 ``PublishDays`` 枚举（``1``/``3``/``7``/``14``）一致。
#:
#: **``"最新"`` 不在这里。** 它在平台上走的是排序分支（见
#: :data:`SORT_LABELS`），把它当 ``publishDays`` 会把「最新」误翻成「1 天内」，
#: 让用户莫名丢掉更早发布的商品。
PUBLISH_DAYS_LABELS: dict[str, str] = {
    "1天内": "1",
    "3天内": "3",
    "7天内": "7",
    "14天内": "14",
}

#: 前端文案 -> ``(sortValue, sortField)``。**逐条抄自平台自己的筛选构造器**
#: （``p_search-index.js`` 内 ``function S(e)`` 的 ``switch(r.belongTo)``）::
#:
#:     case"新发布": case"create": t.sortValue="desc"; t.sortField="create"
#:     case"价格":   case"desc":   t.sortValue="desc"; t.sortField="price"
#:                   case"asc":    t.sortValue="asc";  t.sortField="price"
#:     case"新降价":               t.sortValue="desc"; t.sortField="reduce"
#:
#: 这里**只收录前端任务表单真正能选出的值**。「综合/pos」「综合/credit」
#: 「距离」这些平台有、但本项目的任务表单没有对应下拉项的，一律不收录：
#: 收进来就意味着要发明一套 UI，而发明 UI 不在本次范围。
#:
#: **「最新」是本表存在的理由。** 它此前被判定为「无法映射，交给点击兜底」，
#: 但平台明确把它翻成 ``sortValue=desc / sortField=create``。不映射的真实后果
#: 是：用户在页面上选「最新」，注入层放行、点击层点的是「新发布」下拉里的
#: 排序项——最终下发的是**不带任何排序**的请求，等于筛选静默失效。
SORT_LABELS: dict[str, tuple[str, str]] = {
    "最新": ("desc", "create"),
    "价格从低到高": ("asc", "price"),
    "价格从高到低": ("desc", "price"),
    "最新降价": ("desc", "reduce"),
}


def resolve_sort(option: str | None) -> tuple[str, str] | None:
    """把前端排序文案翻成 ``(sortValue, sortField)``；无法识别返回 ``None``。"""
    if not option:
        return None
    return SORT_LABELS.get(str(option).strip())


def resolve_publish_days(option: str | None) -> str | None:
    """把前端「新发布」文案翻译成 ``publishDays`` 取值；无法识别返回 ``None``。

    返回 ``None`` 时调用方应**保留原有的点击式筛选**，而不是当没配。
    """
    if not option:
        return None
    return PUBLISH_DAYS_LABELS.get(str(option).strip())


def _parse_query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query, keep_blank_values=True))


def _rebuild_url(url: str, query: dict[str, str]) -> str:
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


def extract_data_payload(post_data: str) -> Optional[str]:
    """从请求体里取出 ``data`` 字段的原始 JSON 字符串；取不到返回 ``None``。

    仅识别 mtop 的 ``application/x-www-form-urlencoded`` 形态（``data=<json>&...``）。
    刻意不做「猜 JSON body」的兼容：这个接口的形态是确定的，
    多一条猜测分支就多一条在生产里走错路的机会，而走错的后果是发一个
    服务端不认识的请求。
    """
    if not post_data:
        return None
    for key, value in parse_qsl(post_data, keep_blank_values=True):
        if key == "data" and value:
            return value
    return None


def merge_filters(
    data_json: str,
    *,
    search_filter: str | None = None,
    extra_filter_value: str | None = None,
    sort_value: str | None = None,
    sort_field: str | None = None,
) -> str:
    """把筛选合并进既有 ``data`` 载荷，返回新的紧凑 JSON 字符串。

    只覆盖 ``propValueStr.searchFilter``、``extraFilterValue`` 与排序三项，
    其余字段（``pageNumber`` / ``keyword`` / ``rowsPerPage`` …）保持浏览器给出
    原值不动——翻页这些由页面自身驱动，本模块只补筛选与排序。

    ``sort_value``/``sort_field`` 必须**成对**给：平台构造器里两者永远一起赋值
    （``case"create": t.sortValue="desc", t.sortField="create"``）。
    只给一个会发出半截排序条件，服务端要么忽略要么按默认序返回，
    表现为「排序没生效」且无任何报错。因此这里要求同时非空才写入。

    ``fromFilter`` 会被同步更新为「是否有任何筛选」，与权威实现
    （``search-params.builder.ts:85`` 的 ``hasFilter``）语义一致：
    它告诉服务端「这是带筛选的请求」，值不对会让筛选被忽略。
    平台在 ``新发布/create`` 分支里同样置 ``fromFilter=!0``，
    所以排序也算筛选的一部分。

    传入 ``None`` 表示该字段不改。载荷不是 JSON 对象时抛 :class:`ValueError`，
    由调用方决定是否放行原请求。
    """
    parsed = json.loads(data_json)
    if not isinstance(parsed, dict):
        raise ValueError(f"搜索载荷顶层必须是 JSON 对象，实际是 {type(parsed).__name__}")

    if search_filter is not None:
        prop = parsed.get("propValueStr")
        if not isinstance(prop, dict):
            prop = {}
        prop["searchFilter"] = search_filter
        parsed["propValueStr"] = prop

    if extra_filter_value is not None:
        parsed["extraFilterValue"] = extra_filter_value

    # 排序必须成对写入，理由见 docstring
    if sort_value and sort_field:
        parsed["sortValue"] = sort_value
        parsed["sortField"] = sort_field

    # fromFilter 必须反映最终是否带筛选，而不是「本次有没有注入」
    final_filter = str((parsed.get("propValueStr") or {}).get("searchFilter") or "")
    has_region = False
    raw_extra = parsed.get("extraFilterValue")
    if isinstance(raw_extra, str) and raw_extra.strip():
        try:
            extra = json.loads(raw_extra)
            has_region = bool(isinstance(extra, dict) and extra.get("divisionList"))
        except (ValueError, TypeError):
            has_region = False
    parsed["fromFilter"] = bool(
        final_filter or has_region or parsed.get("sortValue") or parsed.get("sortField")
    )

    return compact_json(parsed)


def resign(
    url: str,
    body_data_json: str,
    cookie_header: str,
    *,
    original_body: str = "",
    sign_builder: Callable[[str, int, str], str] = build_sign,
) -> tuple[str, str]:
    """重算签名并返回 ``(新 URL, 新请求体)``。

    ``original_body`` 是浏览器原本要发的请求体。**必须传**：mtop 的真实 body
    除 ``data`` 之外还带平台注入的风控字段（真机抓包实测为
    ``bx-ua`` / ``bx-umidtoken`` / ``bx_et``）。只发 ``data`` 会把这些一并丢掉，
    风控特征缺失可能导致请求被拦。本函数保留原 body 的所有其它字段与顺序，
    只替换 ``data`` 的值。

    ``t`` 保持请求原值不动：签名原文用哪个 ``t``，query 就必须发哪个 ``t``，
    两者必须成对，重新取当前时间反而会让它们错位。

    cookie 里取不到 ``_m_h5_tk`` 时抛 :class:`ValueError`——没有 token 就没有
    合法签名，此时只能放行原请求。
    """
    query = _parse_query(url)
    raw_t = query.get("t")
    app_key = query.get("appKey")
    if not raw_t or not app_key:
        raise ValueError("搜索请求缺少 t / appKey，无法重算签名")

    try:
        t_ms = int(raw_t)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"搜索请求的 t 不是整数毫秒时间戳：{raw_t!r}") from exc

    match = re.search(r"(?:^|;\s*)_m_h5_tk=([^;]+)", cookie_header or "")
    if not match:
        raise ValueError("cookie 中缺少 _m_h5_tk，无法重算签名")
    token = extract_token(match.group(1))
    if not token:
        raise ValueError("_m_h5_tk 为空，无法重算签名")

    # 用注入的 sign_builder，便于测试注入固定实现；默认就是权威签名函数
    new_sign = sign_builder(token, t_ms, body_data_json)
    query["sign"] = new_sign

    return _rebuild_url(url, query), rebuild_body(original_body, body_data_json)


def rebuild_body(original_body: str, body_data_json: str) -> str:
    """保留原 body 的其它字段（含风控 token）与字段顺序，只替换 ``data``。

    原 body 为空或没有 ``data`` 时退化为只发 ``data``——那说明我们面对的
    不是预期的 mtop 形态，此时按最简单形态提交即可，不擅自发明字段。
    """
    if not original_body:
        return urlencode({"data": body_data_json})

    pairs = parse_qsl(original_body, keep_blank_values=True)
    if not any(key == "data" for key, _ in pairs):
        return urlencode({"data": body_data_json})

    rebuilt: list[tuple[str, str]] = []
    seen_data = False
    for key, value in pairs:
        if key == "data":
            # 只替换第一处 data，重复 data 属异常形态，多余的很可能是攻击面
            if not seen_data:
                rebuilt.append((key, body_data_json))
                seen_data = True
            continue
        rebuilt.append((key, value))
    return urlencode(rebuilt)


def build_route_handler(
    *,
    personal_only: bool = False,
    free_shipping: bool = False,
    inspection_service: bool = False,
    super_shop: bool = False,
    brand_new: bool = False,
    region: str | None = None,
    min_price: Any = None,
    max_price: Any = None,
    new_publish_option: str | None = None,
    exclude_multi_places_sellers: bool = False,
    logger: Callable[[str], None] | None = None,
) -> Callable[..., Any]:
    """构造一个 Playwright ``page.route`` handler，用于注入原生筛选。

    返回的 handler 是 ``async def handler(route, request)``。它只处理搜索接口的
    POST；其余请求一律 ``route.continue_()`` 直接放行。

    当用户没有配置任何筛选时，handler 仍然会被安装，但只会原样放行——
    这样调用方不必在外部判断「要不要装 handler」，少一处分支少一处出错点。

    所有内部异常都被消化：注入失败时记录原因并**放行原请求**，
    保证采集链路的可用性不受本优化影响。
    """

    def _log(message: str) -> None:
        if logger is not None:
            logger(message)

    # 预先把筛选算好：这些值是纯数据映射，与请求无关，不必每个请求重算。
    # 取值统一走 QUICK_FILTER_MAP——两处各写一份必然漂移
    # （曾因按字段名猜「验货宝」而把 inspectedPhone 用错，见该常量注释）。
    quick_filters: list[str] = [
        QUICK_FILTER_MAP[name]
        for name, flag in (
            ("personal_only", personal_only),
            ("free_shipping", free_shipping),
            ("inspection_service", inspection_service),
            ("super_shop", super_shop),
            ("brand_new", brand_new),
        )
        if flag and name in QUICK_FILTER_MAP
    ]

    # 「最新」等无法映射为时间窗的文案会得到 None，此时不注入 publishDays，
    # 由 scraper 里保留的点击式筛选兜底。
    publish_days = resolve_publish_days(new_publish_option)

    # 「最新」走的是**排序**分支而不是时间窗（平台原文：
    # case"新发布": case"create": t.sortValue="desc", t.sortField="create"）。
    # 两条分支互斥，同一个文案不会同时落进两边。
    sort_pair = resolve_sort(new_publish_option)

    search_filter = build_search_filter(
        quick_filters=quick_filters,
        min_price=min_price,
        max_price=max_price,
        publish_days=publish_days,
    )
    region_parts = [
        part.strip() for part in (region or "").split("/") if part.strip()
    ]
    extra_filter_value = (
        build_extra_filter_value(
            region_parts=region_parts,
            exclude_multi_places_sellers=exclude_multi_places_sellers,
        )
        if region_parts
        else None
    )
    # 只有排序也要注入：用户选「最新」时没有任何 quickFilter/priceRange/region，
    # 若把 enabled 只按筛选算，整个 handler 会直接放行，
    # 「最新」就又变回静默失效——这正是修这个缺陷要避免的。
    enabled = bool(search_filter or region_parts or sort_pair)

    async def handler(route: Any, request: Any) -> None:
        try:
            if SEARCH_API_FRAGMENT not in str(getattr(request, "url", "")):
                await route.continue_()
                return
            if str(getattr(request, "method", "")).upper() != "POST":
                await route.continue_()
                return
            if not enabled:
                await route.continue_()
                return

            raw_body = request.post_data or ""
            data_json = extract_data_payload(raw_body)
            if not data_json:
                _log("[筛选注入] 请求体里没有 data 字段，放行原请求。")
                await route.continue_()
                return

            merged = merge_filters(
                data_json,
                search_filter=search_filter or None,
                extra_filter_value=extra_filter_value,
                sort_value=sort_pair[0] if sort_pair else None,
                sort_field=sort_pair[1] if sort_pair else None,
            )
            cookie_header = ""
            try:
                cookie_header = request.headers.get("cookie", "")
            except Exception:
                cookie_header = ""
            new_url, new_body = resign(
                str(request.url),
                merged,
                cookie_header,
                original_body=raw_body,
            )
            _log(
                f"[筛选注入] 已注入 {search_filter or ''}"
                f"{'region=' + '/'.join(region_parts) if region_parts else ''}"
            )
            await route.continue_(url=new_url, post_data=new_body)
        except Exception as exc:  # noqa: BLE001 — 注入是优化，任何失败都必须放行原请求
            _log(f"[筛选注入] 失败，已放行原请求：{type(exc).__name__}: {exc}")
            try:
                await route.continue_()
            except Exception:
                # route 已失效（例如页面已关闭），此时无事可做
                pass

    return handler
