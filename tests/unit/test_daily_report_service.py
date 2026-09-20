"""日报服务的配置归一与统计计算测试。

``_normalize_hour`` 的输入来自 Web UI 表单与历史存量数据（早期版本存的是
int 小时），格式极不统一；一旦归一失败，日报会在错误的时间推送甚至不发。
因此这里穷举格式与越界输入，而不是只测 happy path。
"""
from __future__ import annotations

import json

import pytest

from src.services import daily_report_service as drs
from src.services.daily_report_service import (
    _fmt_price,
    _normalize_hour,
    _pct,
    get_report_config,
    save_report_config,
)


@pytest.fixture(autouse=True)
def _ensure_schema():
    """init_schema 就绪后再跑本模块用例。

    save_report_config 直接写 app_metadata 且自身不调用 bootstrap（见
    test_save_report_config_requires_schema_bootstrap_first），所以测试需要
    显式初始化 schema，与应用启动路径（lifespan 的 bootstrap）保持一致。
    """
    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage

    bootstrap_sqlite_storage()


# --- _normalize_hour 格式归一 ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9:00", "9:00"),
        ("9", "9:00"),
        (9, "9:00"),
        ("7", "7:00"),
        (" 9:00 ", "9:00"),
        ("0", "0:00"),
        ("23", "23:00"),
    ],
)
def test_normalize_hour_accepts_common_forms(raw, expected):
    assert _normalize_hour(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [None, "", "abc", "9.5", [], {}, "  ", "N/A", "晚上九点", "9::00"],
)
def test_normalize_hour_falls_back_to_default_on_invalid(raw):
    """非法值必须回落到默认 9:00，而不是抛异常或产生非法时间。"""
    assert _normalize_hour(raw) == "9:00"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("25:00", "23:00"),
        ("-5", "0:00"),
        ("9:60", "9:59"),
        ("-1:-1", "0:00"),
        ("100:100", "23:59"),
    ],
)
def test_normalize_hour_clamps_out_of_range_values(raw, expected):
    """时分越界必须被夹取到合法区间。"""
    assert _normalize_hour(raw) == expected


def test_normalize_hour_pads_minutes_to_two_digits():
    assert _normalize_hour("9:5") == "9:05"
    assert _normalize_hour("9:05") == "9:05"


def test_normalize_hour_result_is_always_parseable():
    """护栏：任何输入的结果都必须能被 int() 解析为合法时分。"""
    for raw in [None, "", "abc", 9, "25:00", "9:60", -3, 3.7, True]:
        result = _normalize_hour(raw)
        hour, _, minute = result.partition(":")
        assert 0 <= int(hour) <= 23
        assert 0 <= int(minute) <= 59


# --- 默认配置与保存/读取 ---


def test_default_report_config_shape():
    cfg = drs.DEFAULT_REPORT_CONFIG
    assert cfg["enabled"] is True
    assert cfg["hour"] == "9:00"
    assert cfg["keywords"] == []
    assert set(cfg["include"]) == {"avg", "min", "max", "change", "rec"}


def test_get_report_config_returns_defaults_when_nothing_saved():
    assert get_report_config() == drs.DEFAULT_REPORT_CONFIG


def test_save_report_config_normalizes_and_persists():
    saved = save_report_config({"hour": "7", "keywords": ["  a  ", "", "b"]})
    assert saved["hour"] == "7:00"
    assert saved["keywords"] == ["a", "b"]
    assert get_report_config()["hour"] == "7:00"


def test_save_report_config_invalid_hour_never_reaches_storage():
    saved = save_report_config({"hour": "not-a-time"})
    assert saved["hour"] == "9:00"
    assert get_report_config()["hour"] == "9:00"


def test_save_report_config_coerces_include_flags_to_bool():
    saved = save_report_config({"include": {"avg": 0, "min": "yes", "max": None}})
    assert saved["include"]["avg"] is False
    assert saved["include"]["min"] is True
    assert saved["include"]["max"] is False
    # 未提供的键回落到默认 True
    assert saved["include"]["change"] is True
    assert saved["include"]["rec"] is True


def test_save_report_config_drops_unexpected_include_keys():
    """include 只保留已知键，避免脏配置渗入。"""
    saved = save_report_config({"include": {"avg": True, "bogus": True}})
    assert "bogus" not in saved["include"]


def test_save_report_config_enabled_flag_is_boolean():
    assert save_report_config({"enabled": 0})["enabled"] is False
    assert save_report_config({"enabled": True})["enabled"] is True


def test_get_report_config_survives_corrupted_stored_json():
    """库里存了坏 JSON 时必须回落默认值而不是让日报端点 500。"""
    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection

    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
            (drs.REPORT_CONFIG_KEY, "{not valid json"),
        )
        conn.commit()

    assert get_report_config() == drs.DEFAULT_REPORT_CONFIG


def test_get_report_config_ignores_non_dict_payload():
    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection

    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_metadata(key, value) VALUES (?, ?)",
            (drs.REPORT_CONFIG_KEY, json.dumps(["not", "a", "dict"])),
        )
        conn.commit()

    assert get_report_config() == drs.DEFAULT_REPORT_CONFIG


def test_save_report_config_bootstraps_schema_itself(monkeypatch):
    """save_report_config 必须自举 schema，不能依赖调用顺序。

    该函数会被真实 API ``PUT /api/settings/report``（src/api/routes/settings.py:429）
    直接调用。历史上它只写 app_metadata 而不建表，于是 fresh 数据库上会
    ``no such table: app_metadata``；若数据库文件被删除/重建，即使应用已启动过，
    保存日报配置也会失败。现已修复：函数内部先调 ``bootstrap_sqlite_storage()``。

    这里用一个全新空库验证它自包含。
    """
    import tempfile

    fresh = tempfile.mkdtemp()
    monkeypatch.setenv("APP_DATABASE_FILE", f"{fresh}/fresh.sqlite3")

    # 空库、无任何 schema，必须能直接保存成功
    assert save_report_config({"hour": "8"})["hour"] == "8:00"
    assert get_report_config()["hour"] == "8:00"


# --- 价格格式化 ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, "¥0"),
        (999, "¥999"),
        (1000, "¥1,000"),
        (9999, "¥9,999"),
        (10000, "1.00万"),
        (12345.6, "1.23万"),
    ],
)
def test_fmt_price_formatting(raw, expected):
    assert _fmt_price(raw) == expected


def test_fmt_price_invalid_input_returns_dash():
    assert _fmt_price(None) == "-"
    assert _fmt_price("abc") == "-"


# --- 环比涨跌 ---


@pytest.mark.parametrize(
    ("current", "previous"),
    [(100, 0), (100, -1), (100, None)],
)
def test_pct_returns_empty_without_valid_baseline(current, previous):
    """没有有效基期时不得输出涨跌幅（避免出现除零或无穷百分比）。"""
    assert _pct(current, previous) == ""


def test_pct_flat_is_marked_as_flat():
    assert _pct(100, 100) == "（持平）"


def test_pct_rise_and_fall_direction():
    assert _pct(110, 100) == "（↑10.0%）"
    assert _pct(90, 100) == "（↓10.0%）"


def test_pct_tiny_change_is_treated_as_flat():
    """涨幅 <0.01% 视为持平，避免抖动的噪声数字。"""
    assert _pct(1000.05, 1000) == "（持平）"


def test_pct_drop_to_zero_is_reported():
    assert _pct(0, 100) == "（↓100.0%）"


# --- 关键词统计 ---


def _insert_result_item(conn, *, keyword: str, crawl_time: str, price, is_recommended: int = 0):
    conn.execute(
        """
        INSERT INTO result_items (
            result_filename, keyword, task_name, crawl_time, price, price_display,
            link_unique_key, is_recommended, keyword_hit_count, raw_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"{keyword}_full_data.jsonl",
            keyword,
            "T",
            crawl_time,
            price,
            str(price),
            f"k-{keyword}-{crawl_time}-{price}",
            is_recommended,
            0,
            "{}",
        ),
    )


def test_build_keyword_stats_aggregates_within_window():
    import datetime as dt_module

    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage
    from src.infrastructure.persistence.sqlite_connection import sqlite_connection

    bootstrap_sqlite_storage()
    since = (dt_module.datetime.now() - dt_module.timedelta(days=1)).isoformat()
    recent = dt_module.datetime.now().isoformat()
    old = (dt_module.datetime.now() - dt_module.timedelta(days=5)).isoformat()

    with sqlite_connection() as conn:
        _insert_result_item(conn, keyword="kw", crawl_time=recent, price=100, is_recommended=1)
        _insert_result_item(conn, keyword="kw", crawl_time=recent, price=200)
        _insert_result_item(conn, keyword="kw", crawl_time=old, price=99999)
        conn.commit()

    stats = drs._build_keyword_stats("kw", since)
    assert stats["cnt"] == 2
    assert stats["avg"] == 150.0
    assert stats["min"] == 100.0
    assert stats["max"] == 200.0
    assert stats["rec"] == 1


def test_build_keyword_stats_returns_none_when_no_samples():
    import datetime as dt_module

    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage

    bootstrap_sqlite_storage()
    since = (dt_module.datetime.now() - dt_module.timedelta(days=1)).isoformat()
    assert drs._build_keyword_stats("nonexistent-keyword", since) is None


def test_build_keyword_stats_unknown_keyword_returns_none():
    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage

    bootstrap_sqlite_storage()
    assert drs._build_keyword_stats("never-seen", "1970-01-01T00:00:00") is None


# --- 上期报告读写 ---


def test_prev_report_round_trip():
    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage

    bootstrap_sqlite_storage()
    drs._save_prev_report({"kw": {"avg": 123.4, "cnt": 5}})
    assert drs._get_prev_report()["kw"]["avg"] == 123.4


def test_prev_report_empty_when_absent():
    from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage

    bootstrap_sqlite_storage()
    assert drs._get_prev_report() == {}
