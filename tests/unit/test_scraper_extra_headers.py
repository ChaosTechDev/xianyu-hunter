"""增强快照的 HTTP 头下发规则。

背景（真机实测，NAS 容器，同一出口 IP、同一登录态，逐变量对照）：

增强快照是从**某一个具体请求**抓下来的，它记录的请求头带有那次请求的类型
特征。实测 ``state/xy-1.json`` 里是::

    Sec-Fetch-Mode: cors
    Sec-Fetch-Dest: empty
    Sec-Fetch-Site: same-origin

这是 XHR 的值。当初把它当作全局 ``extra_http_headers`` 下发时，**主文档导航**
也被套上了这些值，而导航本应是 navigate/document。服务端据此返回了一个没有任何
内容的 SPA 空壳，表现为：搜索接口一次都不发起，采集干等 60 秒超时，日志里
既不报「非法访问」也没有任何异常 —— 静默失败，最难排查。

真机对照结果：

    不设 extra_http_headers            -> 命中接口，正文 4972 字、html 263 KB
    单独加上述三个中任意一个            -> 未命中，正文 0 字、html 仅 10558 B
    单独加 UA / sec-ch-ua / Accept /
      Referer / Accept-Encoding 等      -> 仍然命中

所以只排除 Sec-Fetch-* 系列，其余头保留（对指纹一致性有用）。
"""
from __future__ import annotations

from src.scraper import _build_extra_headers


class TestFetchMetadataHeadersAreDropped:
    """这三个头一旦下发就会让主文档导航变成空壳。"""

    def test_site_is_dropped(self):
        assert "Sec-Fetch-Site" not in _build_extra_headers(
            {"Sec-Fetch-Site": "same-origin"}
        )

    def test_mode_is_dropped(self):
        assert "Sec-Fetch-Mode" not in _build_extra_headers({"Sec-Fetch-Mode": "cors"})

    def test_dest_is_dropped(self):
        assert "Sec-Fetch-Dest" not in _build_extra_headers({"Sec-Fetch-Dest": "empty"})

    def test_user_is_dropped(self):
        assert "Sec-Fetch-User" not in _build_extra_headers({"Sec-Fetch-User": "?1"})

    def test_matching_is_case_insensitive(self):
        """快照里的头名大小写来自浏览器，不能假设与常量完全一致。"""
        headers = {"sec-fetch-mode": "cors", "SEC-FETCH-DEST": "empty"}
        assert _build_extra_headers(headers) == {}


class TestUsefulHeadersAreKept:
    """其余头对指纹一致性有用，不能被顺手删掉。"""

    def test_user_agent_is_kept(self):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        assert _build_extra_headers(headers) == headers

    def test_accept_language_is_kept(self):
        assert _build_extra_headers({"Accept-Language": "zh-CN,zh;q=0.9"}) == {
            "Accept-Language": "zh-CN,zh;q=0.9"
        }

    def test_referer_and_encoding_are_kept(self):
        headers = {
            "Referer": "https://www.goofish.com/",
            "Accept-Encoding": "gzip, deflate, br, zstd",
        }
        assert _build_extra_headers(headers) == headers

    def test_mixed_input_keeps_only_safe_headers(self):
        headers = {
            "User-Agent": "UA",
            "Sec-Fetch-Mode": "cors",
            "Accept": "*/*",
            "Sec-Fetch-Dest": "empty",
        }
        assert _build_extra_headers(headers) == {"User-Agent": "UA", "Accept": "*/*"}


class TestExistingExclusionsStillHold:
    """原有的排除项不能被这次修改破坏。"""

    def test_cookie_is_dropped(self):
        assert "Cookie" not in _build_extra_headers({"Cookie": "a=1"})

    def test_content_length_is_dropped(self):
        assert "Content-Length" not in _build_extra_headers({"Content-Length": "10"})

    def test_none_values_are_dropped(self):
        assert _build_extra_headers({"X-Test": None}) == {}

    def test_empty_input_returns_empty(self):
        assert _build_extra_headers({}) == {}
        assert _build_extra_headers(None) == {}
