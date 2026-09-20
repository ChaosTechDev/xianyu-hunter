"""``search_filter_injection`` 的单元测试。

覆盖三类风险：

1. **正确性**：注入后的载荷与签名必须自洽（服务端重算签名要能对上）
2. **不依赖 DOM**：注入逻辑不碰任何页面结构，天然免疫哈希类名失效
3. **失败即放行**：任何异常路径都必须继续原请求，绝不能中断采集
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.services.search_filter_injection import (  # noqa: E402
    PUBLISH_DAYS_LABELS,
    SEARCH_API_FRAGMENT,
    build_route_handler,
    extract_data_payload,
    merge_filters,
    resign,
    resolve_publish_days,
)

SEARCH_URL = (
    "https://h5api.m.goofish.com/h5/mtop.taobao.idlemtopsearch.pc.search/1.0/"
    "?jsv=2.7.2&appKey=34839810&t=1700000000000&sign=old_sign&v=1.0"
    "&type=originaljson&accountSite=xianyu"
)
COOKIE = "unb=123456; _m_h5_tk=abc123token_1699999999999; cookie2=deadbeef"
BASE_DATA = {
    "pageNumber": 1,
    "keyword": "macbook",
    "fromFilter": False,
    "rowsPerPage": 30,
    "sortValue": "",
    "sortField": "",
    "customDistance": "",
    "gps": "",
    "propValueStr": {"searchFilter": ""},
    "customGps": "",
    "searchReqFromPage": "pcSearch",
    "extraFilterValue": "{}",
    "userPositionJson": "{}",
}


def _body(data: dict) -> str:
    from urllib.parse import urlencode

    return urlencode({"data": json.dumps(data, separators=(",", ":"))})


def _expected_sign(body_json: str) -> str:
    raw = f"abc123token&1700000000000&34839810&{body_json}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class TestExtractDataPayload:
    def test_extracts_data_field(self):
        payload = extract_data_payload(_body(BASE_DATA))
        assert json.loads(payload)["keyword"] == "macbook"

    @pytest.mark.parametrize("bad", ["", "foo=bar", "data="])
    def test_returns_none_when_absent(self, bad):
        assert extract_data_payload(bad) is None


class TestMergeFilters:
    def test_injects_search_filter(self):
        merged = json.loads(
            merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterPersonal;")
        )
        assert merged["propValueStr"]["searchFilter"] == "quickFilter:filterPersonal;"

    def test_sets_from_filter_true(self):
        merged = json.loads(
            merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterPersonal;")
        )
        assert merged["fromFilter"] is True

    def test_preserves_page_number_and_other_fields(self):
        """翻页由页面驱动，注入不能把页码改回 1。"""
        data = dict(BASE_DATA, pageNumber=5, sortValue="desc", sortField="price")
        merged = json.loads(
            merge_filters(json.dumps(data), search_filter="quickFilter:filterPersonal;")
        )
        assert merged["pageNumber"] == 5
        assert merged["sortValue"] == "desc"
        assert merged["sortField"] == "price"

    def test_injects_region_into_extra_filter_value(self):
        extra = '{"divisionList":[{"city":"杭州市"}],"excludeMultiPlacesSellers":"0","extraDivision":""}'
        merged = json.loads(
            merge_filters(json.dumps(BASE_DATA), extra_filter_value=extra)
        )
        assert json.loads(merged["extraFilterValue"])["divisionList"] == [{"city": "杭州市"}]

    def test_region_alone_sets_from_filter(self):
        extra = '{"divisionList":[{"city":"杭州市"}],"excludeMultiPlacesSellers":"0","extraDivision":""}'
        merged = json.loads(
            merge_filters(json.dumps(BASE_DATA), extra_filter_value=extra)
        )
        assert merged["fromFilter"] is True

    def test_keeps_prop_value_str_dict_shape(self):
        """``propValueStr`` 必须保持嵌套对象，不能被压成字符串。"""
        merged = json.loads(
            merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterNew;")
        )
        assert isinstance(merged["propValueStr"], dict)

    def test_handles_missing_prop_value_str(self):
        data = {k: v for k, v in BASE_DATA.items() if k != "propValueStr"}
        merged = json.loads(
            merge_filters(json.dumps(data), search_filter="quickFilter:filterNew;")
        )
        assert merged["propValueStr"]["searchFilter"] == "quickFilter:filterNew;"

    def test_replaces_existing_filter_rather_than_appending(self):
        data = dict(BASE_DATA, propValueStr={"searchFilter": "publishDays:1;"})
        merged = json.loads(
            merge_filters(json.dumps(data), search_filter="quickFilter:filterPersonal;")
        )
        assert merged["propValueStr"]["searchFilter"] == "quickFilter:filterPersonal;"

    def test_none_means_leave_untouched(self):
        merged = json.loads(merge_filters(json.dumps(BASE_DATA)))
        assert merged["propValueStr"]["searchFilter"] == ""
        assert merged["fromFilter"] is False

    @pytest.mark.parametrize("bad", ["[]", "null", '"string"', "123"])
    def test_non_dict_payload_raises(self, bad):
        with pytest.raises(ValueError):
            merge_filters(bad, search_filter="quickFilter:filterNew;")

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            merge_filters("{not json", search_filter="quickFilter:filterNew;")


class TestResign:
    def test_signature_matches_emitted_body(self):
        merged = merge_filters(
            json.dumps(BASE_DATA), search_filter="quickFilter:filterPersonal;"
        )
        url, body = resign(SEARCH_URL, merged, COOKIE)
        sent = dict(parse_qsl(body))["data"]
        sign = dict(parse_qsl(urlsplit(url).query))["sign"]
        assert sign == _expected_sign(sent)

    def test_keeps_original_t(self):
        """``t`` 必须保持请求原值：签名原文用哪个 t，query 就得发哪个 t。"""
        merged = merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterNew;")
        url, body = resign(SEARCH_URL, merged, COOKIE)
        query = dict(parse_qsl(urlsplit(url).query))
        assert query["t"] == "1700000000000"
        # 签名必须基于 body 与「原 t」重算，而不是当前时间
        sent = dict(parse_qsl(body))["data"]
        assert query["sign"] == _expected_sign(sent)

    def test_preserves_other_query_params(self):
        merged = merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterNew;")
        url, _ = resign(SEARCH_URL, merged, COOKIE)
        query = dict(parse_qsl(urlsplit(url).query))
        assert query["appKey"] == "34839810"
        assert query["accountSite"] == "xianyu"
        assert query["jsv"] == "2.7.2"

    def test_body_is_form_encoded(self):
        merged = merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterNew;")
        _url, body = resign(SEARCH_URL, merged, COOKIE)
        assert body.startswith("data=")
        assert dict(parse_qsl(body))["data"]

    def test_preserves_risk_tokens_in_original_body(self):
        """原 body 的风控字段（真机抓包实测为 bx-ua/bx-umidtoken/bx_et）必须保留。

        初版用 ``urlencode({"data": ...})`` 整体重建 body，把这些一并丢掉。
        它们由平台自己的 JS 注入，属请求的组成部分，丢了就是「发了一个
        与浏览器不同的请求」，而这类差异正是风控关注的东西。
        """
        original = "bx-ua=UA_TOKEN&bx-umidtoken=UMID_TOKEN&data=OLD&bx_et=ET_TOKEN"
        merged = merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterNew;")
        _url, body = resign(SEARCH_URL, merged, COOKIE, original_body=original)
        sent = dict(parse_qsl(body, keep_blank_values=True))
        assert sent["bx-ua"] == "UA_TOKEN"
        assert sent["bx-umidtoken"] == "UMID_TOKEN"
        assert sent["bx_et"] == "ET_TOKEN"
        assert sent["data"] != "OLD"  # data 被换成了注入后的载荷

    def test_preserves_original_field_order(self):
        """字段顺序保持原样：只替换 data 的值，不重排其它字段。"""
        original = "bx-ua=A&data=OLD&bx_et=B"
        merged = merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterNew;")
        _url, body = resign(SEARCH_URL, merged, COOKIE, original_body=original)
        assert [k for k, _ in parse_qsl(body)] == ["bx-ua", "data", "bx_et"]

    def test_falls_back_when_original_body_has_no_data(self):
        """原 body 无 data 字段时退化为只发 data（不擅自发明字段）。"""
        merged = merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterNew;")
        _url, body = resign(SEARCH_URL, merged, COOKIE, original_body="whatever=1")
        assert [k for k, _ in parse_qsl(body)] == ["data"]

    def test_duplicate_data_keeps_only_first(self):
        """重复 data 属异常形态，只替换第一处，不放大可注入面。"""
        original = "data=OLD1&data=OLD2&bx_et=E"
        merged = merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterNew;")
        _url, body = resign(SEARCH_URL, merged, COOKIE, original_body=original)
        keys = [k for k, _ in parse_qsl(body)]
        assert keys.count("data") == 1

    def test_signature_still_matches_with_risk_tokens(self):
        """保留风控字段不影响签名自洽：签名只覆盖 data 的值。"""
        original = "bx-ua=A&data=OLD&bx_et=B"
        merged = merge_filters(json.dumps(BASE_DATA), search_filter="quickFilter:filterNew;")
        url, body = resign(SEARCH_URL, merged, COOKIE, original_body=original)
        sent = dict(parse_qsl(body))["data"]
        assert dict(parse_qsl(urlsplit(url).query))["sign"] == _expected_sign(sent)

    def test_missing_token_raises(self):
        with pytest.raises(ValueError, match="_m_h5_tk"):
            resign(SEARCH_URL, "{}", "unb=1; cookie2=x")

    def test_empty_token_raises(self):
        with pytest.raises(ValueError):
            resign(SEARCH_URL, "{}", "_m_h5_tk=_1699999999999")

    def test_missing_t_raises(self):
        url = SEARCH_URL.replace("t=1700000000000&", "")
        with pytest.raises(ValueError, match="t / appKey"):
            resign(url, "{}", COOKIE)

    def test_non_numeric_t_raises(self):
        url = SEARCH_URL.replace("t=1700000000000", "t=abc")
        with pytest.raises(ValueError, match="毫秒时间戳"):
            resign(url, "{}", COOKIE)

    def test_chinese_keyword_signature_is_consistent(self):
        """中文转义差异曾导致签名错位；这里钉住端到端一致性。"""
        data = dict(BASE_DATA, keyword="苹果手机")
        merged = merge_filters(json.dumps(data), search_filter="quickFilter:filterNew;")
        url, body = resign(SEARCH_URL, merged, COOKIE)
        sent = dict(parse_qsl(body))["data"]
        sign = dict(parse_qsl(urlsplit(url).query))["sign"]
        assert "\\u82f9\\u679c" in sent
        assert sign == _expected_sign(sent)


class _FakeRoute:
    def __init__(self, outcome_recorder, fail_continue=False):
        self.recorder = outcome_recorder
        self.fail_continue = fail_continue

    async def continue_(self, **kwargs):
        if self.fail_continue and not kwargs:
            raise RuntimeError("route already gone")
        self.recorder.append(kwargs)


class _FakeRequest:
    def __init__(self, url, method="POST", data=None, headers=None, raise_on_headers=False):
        self.url = url
        self.method = method
        self.post_data = data
        self._headers = headers or {}
        self.raise_on_headers = raise_on_headers

    @property
    def headers(self):
        if self.raise_on_headers:
            raise RuntimeError("headers unavailable")
        return self._headers


class TestRouteHandler:
    """handler 的核心约束：失败即放行原请求。

    本仓库未启用 ``asyncio_mode``，既有约定是用 ``asyncio.run`` 在同步用例里驱动
    协程（见 ``tests/unit/test_ai_budget_integration.py``）。这里沿用同一约定，
    避免引入第二种异步测试风格。
    """

    @staticmethod
    def _run(handler, route, request):
        asyncio.run(handler(route, request))

    def test_injects_and_resigns(self):
        rec = []
        handler = build_route_handler(personal_only=True)
        req = _FakeRequest(
            SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": COOKIE}
        )
        self._run(handler, _FakeRoute(rec), req)
        assert len(rec) == 1
        assert "url" in rec[0] and "post_data" in rec[0]
        sent = dict(parse_qsl(rec[0]["post_data"]))["data"]
        assert json.loads(sent)["propValueStr"]["searchFilter"] == "quickFilter:filterPersonal;"
        sign = dict(parse_qsl(urlsplit(rec[0]["url"]).query))["sign"]
        assert sign == _expected_sign(sent)

    def test_non_search_url_passes_through(self):
        rec = []
        handler = build_route_handler(personal_only=True)
        self._run(handler, _FakeRoute(rec), _FakeRequest("https://www.goofish.com/", "GET"))
        assert rec == [{}]

    def test_get_method_passes_through(self):
        rec = []
        handler = build_route_handler(personal_only=True)
        self._run(handler, _FakeRoute(rec), _FakeRequest(SEARCH_URL, "GET"))
        assert rec == [{}]

    def test_no_filters_configured_passes_through_unchanged(self):
        """没配筛选时 handler 仍安装，但只做放行。"""
        rec = []
        handler = build_route_handler()
        req = _FakeRequest(SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": COOKIE})
        self._run(handler, _FakeRoute(rec), req)
        assert rec == [{}]

    def test_missing_token_passes_through_original_request(self):
        """拿不到 token 时不能中断，必须原样放行。"""
        rec = []
        handler = build_route_handler(personal_only=True)
        req = _FakeRequest(
            SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": "unb=1; cookie2=x"}
        )
        self._run(handler, _FakeRoute(rec), req)
        assert rec == [{}], "应放行原请求而不是往 route 里塞修改后的参数"

    def test_corrupt_payload_passes_through(self):
        rec = []
        handler = build_route_handler(personal_only=True)
        from urllib.parse import urlencode

        req = _FakeRequest(
            SEARCH_URL, data=urlencode({"data": "{broken"}), headers={"cookie": COOKIE}
        )
        self._run(handler, _FakeRoute(rec), req)
        assert rec == [{}]

    def test_missing_data_field_passes_through(self):
        rec = []
        handler = build_route_handler(personal_only=True)
        req = _FakeRequest(SEARCH_URL, data="foo=bar", headers={"cookie": COOKIE})
        self._run(handler, _FakeRoute(rec), req)
        assert rec == [{}]

    def test_headers_unavailable_passes_through(self):
        rec = []
        handler = build_route_handler(personal_only=True)
        req = _FakeRequest(
            SEARCH_URL, data=_body(BASE_DATA), raise_on_headers=True
        )
        self._run(handler, _FakeRoute(rec), req)
        assert rec == [{}]

    def test_region_injection(self):
        """区域合成**一个**元素（省/市平级字段），不是一层一个元素。"""
        rec = []
        handler = build_route_handler(region="浙江省/杭州市")
        req = _FakeRequest(SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": COOKIE})
        self._run(handler, _FakeRoute(rec), req)
        sent = json.loads(dict(parse_qsl(rec[0]["post_data"]))["data"])
        extra = json.loads(sent["extraFilterValue"])
        assert extra["divisionList"] == [{"province": "浙江省", "city": "杭州市"}]

    def test_region_injection_with_district(self):
        rec = []
        handler = build_route_handler(region="浙江省/杭州市/西湖区")
        req = _FakeRequest(SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": COOKIE})
        self._run(handler, _FakeRoute(rec), req)
        sent = json.loads(dict(parse_qsl(rec[0]["post_data"]))["data"])
        extra = json.loads(sent["extraFilterValue"])
        assert extra["divisionList"] == [
            {"province": "浙江省", "city": "杭州市", "area": "西湖区"}
        ]

    def test_all_five_switches(self):
        rec = []
        handler = build_route_handler(
            personal_only=True,
            free_shipping=True,
            inspection_service=True,
            super_shop=True,
            brand_new=True,
        )
        req = _FakeRequest(SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": COOKIE})
        self._run(handler, _FakeRoute(rec), req)
        sent = json.loads(dict(parse_qsl(rec[0]["post_data"]))["data"])
        codes = sent["propValueStr"]["searchFilter"]
        for expected in (
            "filterPersonal",
            "filterFreePostage",
            "filterAppraise",  # 验货宝；不是 inspectedPhone（那是「严选」）
            "filterNew",
            "filterHighLevelYxpSeller",
        ):
            assert expected in codes

    def test_price_range_injection(self):
        rec = []
        handler = build_route_handler(min_price="100", max_price="2000")
        req = _FakeRequest(SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": COOKIE})
        self._run(handler, _FakeRoute(rec), req)
        sent = json.loads(dict(parse_qsl(rec[0]["post_data"]))["data"])
        assert sent["propValueStr"]["searchFilter"] == "priceRange:100,2000;"

    def test_logger_receives_failure_reason(self):
        messages = []
        handler = build_route_handler(personal_only=True, logger=messages.append)
        req = _FakeRequest(SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": "unb=1"})
        self._run(handler, _FakeRoute([]), req)
        assert any("失败" in m and "放行" in m for m in messages)

    def test_second_continue_failure_is_swallowed(self):
        """route 已失效时不能把异常抛回 Playwright。"""
        handler = build_route_handler(personal_only=True)
        req = _FakeRequest(SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": "unb=1"})
        self._run(handler, _FakeRoute([], fail_continue=True), req)


class TestPublishDaysMapping:
    """前端文案 -> ``publishDays`` 取值。

    文案来源：``web-ui/src/components/tasks/TaskForm.vue:423-427``。
    取值范围与 ``goofish-client`` 的 ``PublishDays`` 枚举一致。
    """

    @pytest.mark.parametrize(
        ("label", "expected"),
        [("1天内", "1"), ("3天内", "3"), ("7天内", "7"), ("14天内", "14")],
    )
    def test_known_labels(self, label, expected):
        assert resolve_publish_days(label) == expected

    def test_latest_is_not_a_time_window(self):
        """「最新」是排序语义，绝不能翻成「1 天内」——那会静默丢掉更早的商品。"""
        assert resolve_publish_days("最新") is None
        assert "最新" not in PUBLISH_DAYS_LABELS

    @pytest.mark.parametrize("bad", [None, "", "   ", "__none__", "乱写", "30天内"])
    def test_unknown_returns_none(self, bad):
        assert resolve_publish_days(bad) is None

    def test_all_mapped_values_are_in_authoritative_enum(self):
        assert set(PUBLISH_DAYS_LABELS.values()) == {"1", "3", "7", "14"}


class TestRouteHandlerPublishDays:
    def test_publish_days_injected_from_label(self):
        rec = []
        handler = build_route_handler(new_publish_option="3天内")
        req = _FakeRequest(SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": COOKIE})
        TestRouteHandler._run(handler, _FakeRoute(rec), req)
        sent = json.loads(dict(parse_qsl(rec[0]["post_data"]))["data"])
        assert sent["propValueStr"]["searchFilter"] == "publishDays:3;"

    def test_latest_maps_to_sort_not_publish_days(self):
        """「最新」必须注入**排序**而不是时间窗，也不能放行。

        平台自己的构造器里这是两条互斥分支::

            case"新发布":
              case"create": t.sortValue="desc", t.sortField="create"   <- 「最新」
              case"1"/"3"/"7"/"14": ...publishDays...                  <- 时间窗

        初版把「最新」判定为「无法映射、交给点击兜底」，结果是：注入层放行、
        点击层去点「新发布」下拉里的排序项——最终下发一个**不带任何排序**的请求，
        用户的「最新」静默失效。这里把它钉死。
        """
        rec = []
        handler = build_route_handler(new_publish_option="最新")
        req = _FakeRequest(SEARCH_URL, data=_body(BASE_DATA), headers={"cookie": COOKIE})
        TestRouteHandler._run(handler, _FakeRoute(rec), req)
        assert rec != [{}], "「最新」不能放行——那等于排序失效"
        sent = json.loads(dict(parse_qsl(rec[0]["post_data"]))["data"])
        assert sent["sortValue"] == "desc"
        assert sent["sortField"] == "create"
        # 排序分支**不得**混入 publishDays
        assert "publishDays" not in sent["propValueStr"]["searchFilter"]
        # 平台在排序分支同样置 fromFilter=true
        assert sent["fromFilter"] is True

    def test_sort_and_publish_days_are_mutually_exclusive(self):
        """同一文案不会同时落进排序与时间窗两条分支。"""
        from src.services.search_filter_injection import (
            PUBLISH_DAYS_LABELS,
            SORT_LABELS,
        )

        assert not (set(PUBLISH_DAYS_LABELS) & set(SORT_LABELS))


class TestNoDomDependency:
    """注入路径不得再引用任何 CSS 选择器或页面元素。

    这里用 AST 检查**代码**，不检查注释与文档字符串——本模块的模块级 docstring
    刻意引用了旧的点击式选择器（``page.click("text=个人闲置")``、
    ``.areaWrap--FaZHsn8E``）来说明「为什么不再那样做」。
    用纯文本 grep 会把这类说明误判成依赖，所以必须区分为可执行代码。
    """

    @staticmethod
    def _code_strings_and_attrs(source: str) -> tuple[list[str], set[str]]:
        import ast

        tree = ast.parse(source)
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = getattr(node, "body", None) or []
                if body and isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant
                ) and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))

        strings: list[str] = []
        attrs: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docstrings:
                    strings.append(node.value)
            elif isinstance(node, ast.Attribute):
                attrs.add(node.attr)
        return strings, attrs

    def test_no_playwright_import(self):
        source = (
            repo_root / "src" / "services" / "search_filter_injection.py"
        ).read_text(encoding="utf-8")
        _strings, _attrs = self._code_strings_and_attrs(source)
        assert "playwright" not in source.split('"""')[0]
        assert "from playwright" not in source and "import playwright" not in source

    def test_no_css_selectors_in_code(self):
        source = (
            repo_root / "src" / "services" / "search_filter_injection.py"
        ).read_text(encoding="utf-8")
        strings, attrs = self._code_strings_and_attrs(source)
        for banned in ("provItem", "areaWrap", "searchBtn--"):
            assert not any(banned in s for s in strings), f"代码里出现哈希类名：{banned}"
        for banned_attr in ("locator", "wait_for_selector"):
            assert banned_attr not in attrs, f"代码里出现 DOM 定位调用：{banned_attr}"



class TestFragmentMatchesSearchPagination:
    """URL 片段必须与分页模块识别到的接口一致，否则两个模块会看不同的东西。"""

    def test_fragment_equals_pagination_fragment_without_scheme(self):
        from src.services.search_pagination import SEARCH_RESULTS_API_FRAGMENT

        assert SEARCH_API_FRAGMENT == SEARCH_RESULTS_API_FRAGMENT
