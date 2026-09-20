"""``data_retention_service`` 的单元测试。

重点覆盖三类容易出事的边界：非法保留天数只能回落、不能放大删除范围；空表/新数据/解析失败都
必须判为「不清理」；文件清理的保留白名单（配置与数据库本体）必须逐项拦住。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.services.data_retention_service import (
    DEFAULT_FILE_RETENTION_DAYS,
    PROTECTED_DIRECTORY_NAMES,
    PROTECTED_FILE_NAMES,
    PROTECTED_SUFFIXES,
    RETENTION_TARGETS,
    RetentionPolicy,
    build_cleanup_plan,
    build_file_cleanup_plan,
    format_bytes,
    resolve_retention_days,
    summarize_usage,
)

NOW = datetime(2026, 3, 19, 12, 0, 0)


def _format(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _entry(name: str, *, mtime: object = None, size: int = 0, is_dir: bool = False) -> dict:
    return {"name": name, "mtime": mtime, "size": size, "is_dir": is_dir}


# ======================================================================================
# 交付 1.1：build_cleanup_plan
# ======================================================================================


def test_cleanup_plan_uses_policy_days_for_each_table():
    """每张表的 cutoff 必须等于 now - 各自保留天数，且决策键完整。"""
    plan = build_cleanup_plan(
        RetentionPolicy(),
        now=NOW,
        oldest_dates={
            "result_items": "2025-01-01 10:00:00",
            "price_snapshots": "2025-01-01 10:00:00",
            "watch_events": "2025-01-01 10:00:00",
            "logs": "2025-01-01 10:00:00",
            "ai_usage_stats": "2025-01-01 10:00:00",
        },
    )

    assert set(plan) == set(RETENTION_TARGETS)
    assert plan["result_items"]["cutoff"] == "2025-12-19 12:00:00"
    assert plan["price_snapshots"]["cutoff"] == "2025-09-20 12:00:00"
    assert plan["watch_events"]["cutoff"] == "2025-09-20 12:00:00"
    assert plan["logs"]["cutoff"] == "2026-02-17 12:00:00"
    assert plan["ai_usage_stats"]["cutoff"] == "2025-03-19 12:00:00"
    assert all(decision["should_prune"] is True for decision in plan.values())
    assert all(set(decision) == {"cutoff", "should_prune", "reason"} for decision in plan.values())


def test_cleanup_plan_marks_only_tables_older_than_cutoff():
    """真实场景：只有 result_items 有超期数据，其余表的最早记录都在保留期内。"""
    plan = build_cleanup_plan(
        RetentionPolicy(),
        now=NOW,
        oldest_dates={
            "result_items": "2024-01-01 10:00:00",
            "price_snapshots": "2026-01-01 10:00:00",
            "watch_events": "2025-10-01 10:00:00",
            "logs": "2026-03-01 10:00:00",
            "ai_usage_stats": "2025-06-01 10:00:00",
        },
    )

    assert {name for name, decision in plan.items() if decision["should_prune"]} == {"result_items"}
    for name in ("price_snapshots", "watch_events", "logs", "ai_usage_stats"):
        assert plan[name]["should_prune"] is False
        assert "无需清理" in plan[name]["reason"]


def test_cleanup_plan_keeps_empty_tables():
    """表为空时必须不清理，并且 reason 明确说明「没有记录」。"""
    plan = build_cleanup_plan(
        RetentionPolicy(),
        now=NOW,
        oldest_dates={name: None for name in RETENTION_TARGETS},
    )

    for name, decision in plan.items():
        assert decision["should_prune"] is False
        assert "没有记录" in decision["reason"]
        # cutoff 仍然要算出来，界面需要展示阈值
        assert decision["cutoff"] == _format(
            NOW - timedelta(days=getattr(RetentionPolicy(), RETENTION_TARGETS[name]))
        )


def test_cleanup_plan_keeps_whitespace_only_oldest_date_as_empty():
    """空白字符串等价于空表，不能当成可解析的时间。"""
    plan = build_cleanup_plan(
        RetentionPolicy(),
        now=NOW,
        oldest_dates={"result_items": "   "},
    )

    assert plan["result_items"]["should_prune"] is False
    assert "没有记录" in plan["result_items"]["reason"]


def test_cleanup_plan_keeps_table_when_oldest_date_unparseable():
    """时间解析失败必须保守不清理，且原因要写明解析失败。"""
    plan = build_cleanup_plan(
        RetentionPolicy(),
        now=NOW,
        oldest_dates={"result_items": "not-a-date", "logs": "2026-02-30 25:00:00"},
    )

    assert plan["result_items"]["should_prune"] is False
    assert plan["logs"]["should_prune"] is False
    assert "无法解析" in plan["result_items"]["reason"]
    assert "拿不准就不清理" in plan["logs"]["reason"]


def test_cleanup_plan_boundary_equal_to_cutoff_is_kept():
    """最早记录恰好等于 cutoff 时不清理：判据是严格早于，与 SQL 的 ``<`` 保持一致。"""
    plan = build_cleanup_plan(
        RetentionPolicy(result_items_days=90),
        now=NOW,
        oldest_dates={"result_items": "2025-12-19 12:00:00"},
    )

    assert plan["result_items"]["should_prune"] is False
    assert plan["result_items"]["cutoff"] == "2025-12-19 12:00:00"


def test_cleanup_plan_one_second_before_cutoff_prunes():
    """比 cutoff 早一秒就必须判可清理，避免边界判定整体偏移。"""
    plan = build_cleanup_plan(
        RetentionPolicy(result_items_days=90),
        now=NOW,
        oldest_dates={"result_items": "2025-12-19 11:59:59"},
    )

    assert plan["result_items"]["should_prune"] is True


def test_cleanup_plan_keeps_when_all_data_is_newer():
    """全部数据都比保留期新 -> 不清理。"""
    plan = build_cleanup_plan(
        RetentionPolicy(price_snapshots_days=180),
        now=NOW,
        oldest_dates={"price_snapshots": "2026-03-01 00:00:00"},
    )

    assert plan["price_snapshots"]["should_prune"] is False
    assert "全部数据都在保留期内" in plan["price_snapshots"]["reason"]


@pytest.mark.parametrize("bad_days", [0, -1, -365, None, "abc", "", True, float("nan"), float("inf")])
def test_cleanup_plan_falls_back_to_default_for_invalid_days(bad_days):
    """非法保留天数（尤其是 0）必须回落到该字段默认值，绝不解释成「全删」。"""
    policy = RetentionPolicy(result_items_days=bad_days)  # type: ignore[arg-type]
    plan = build_cleanup_plan(
        policy,
        now=NOW,
        # 数据刚刚写入，任何合法保留天数下都不该清理
        oldest_dates={"result_items": _format(NOW)},
    )

    assert plan["result_items"]["cutoff"] == "2025-12-19 12:00:00"
    assert plan["result_items"]["should_prune"] is False
    assert "保留 90 天" in plan["result_items"]["reason"]


def test_cleanup_plan_zero_days_never_wipes_table():
    """0 天的显式回归用例：若 0 被当「全删」，cutoff 会等于 now，刚写入的数据也会被判可删。

    这里让所有表的最早记录都只比 now 早一小时，断言全体不清理——
    证明 0 天被识别为非法配置并回落到默认保留天数。
    """
    fresh = _format(NOW - timedelta(hours=1))
    plan = build_cleanup_plan(
        RetentionPolicy(
            result_items_days=0,
            price_snapshots_days=0,
            watch_events_days=0,
            logs_days=0,
            ai_usage_days=0,
        ),
        now=NOW,
        oldest_dates={name: fresh for name in RETENTION_TARGETS},
    )

    assert all(decision["should_prune"] is False for decision in plan.values())
    assert all("保留 " in decision["reason"] for decision in plan.values())
    # 每张表的 cutoff 都必须落在各自默认保留期之上，而不是等于 now
    assert plan["result_items"]["cutoff"] == "2025-12-19 12:00:00"
    assert plan["logs"]["cutoff"] == "2026-02-17 12:00:00"
    assert plan["ai_usage_stats"]["cutoff"] == "2025-03-19 12:00:00"


def test_cleanup_plan_accepts_datetime_and_iso_formats():
    """最早记录支持 datetime、ISO 8601 与纯日期三种写法。"""
    plan = build_cleanup_plan(
        RetentionPolicy(result_items_days=30),
        now=NOW,
        oldest_dates={
            "result_items": datetime(2026, 1, 1, 0, 0, 0),
            "price_snapshots": "2025-09-20T12:00:00",
            "watch_events": "2025-01-01",
        },
    )

    assert plan["result_items"]["should_prune"] is True
    assert plan["price_snapshots"]["should_prune"] is False  # 恰好等于 cutoff
    assert plan["watch_events"]["should_prune"] is True


def test_cleanup_plan_rejects_non_datetime_now():
    """now 不可解析时抛 ValueError，而不是悄悄用当前时间兜底。"""
    with pytest.raises(ValueError):
        build_cleanup_plan(RetentionPolicy(), now="这不是时间", oldest_dates={})  # type: ignore[arg-type]


def test_cleanup_plan_tolerates_missing_tables_in_oldest_dates():
    """oldest_dates 缺表时按空表处理，不抛 KeyError。"""
    plan = build_cleanup_plan(RetentionPolicy(), now=NOW, oldest_dates={})

    assert set(plan) == set(RETENTION_TARGETS)
    assert all(decision["should_prune"] is False for decision in plan.values())


# ======================================================================================
# 交付 1.2：build_file_cleanup_plan
# ======================================================================================


def test_file_cleanup_plan_deletes_only_old_regular_files():
    old = datetime(2020, 1, 1, 0, 0, 0)
    recent = datetime(2026, 3, 18, 0, 0, 0)
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="logs",
        retention_days=30,
        entries=[
            _entry("old.jsonl", mtime=old, size=100),
            _entry("recent.jsonl", mtime=recent, size=200),
        ],
    )

    assert [item["name"] for item in plan["to_delete"]] == ["old.jsonl"]
    assert [item["name"] for item in plan["kept"]] == ["recent.jsonl"]
    assert plan["skipped"] == []
    assert plan["freed_bytes"] == 100


def test_file_cleanup_plan_zero_retention_falls_back_and_deletes_nothing_recent():
    """retention_days=0 必须回落到 30 天：昨天写的文件绝不能被判为可删。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="logs",
        retention_days=0,
        entries=[_entry("today.log", mtime=NOW - timedelta(hours=1), size=50)],
    )

    assert plan["to_delete"] == []
    assert [item["name"] for item in plan["kept"]] == ["today.log"]
    assert plan["freed_bytes"] == 0
    assert "保留 30 天" in plan["kept"][0]["reason"]


@pytest.mark.parametrize("bad_days", [0, -1, None, "abc", True, float("inf")])
def test_file_cleanup_plan_invalid_retention_uses_default_boundary(bad_days):
    """所有非法保留天数都回落到 30 天，cutoff 必须等于 now - 30 天。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="logs",
        retention_days=bad_days,  # type: ignore[arg-type]
        entries=[_entry("very-old.log", mtime=datetime(2000, 1, 1), size=10)],
    )

    assert plan["freed_bytes"] == 10
    assert "2026-02-17 12:00:00" in plan["to_delete"][0]["reason"]
    assert "保留 30 天" in plan["to_delete"][0]["reason"]


@pytest.mark.parametrize(
    "protected_name",
    [
        ".env",
        ".env.local",
        ".gitignore",
        "config.json",
        "state.json",
        "app.sqlite3",
        "app.sqlite3-wal",
        "app.sqlite3-shm",
        "app.db",
        "app.sqlite",
        "cache/app.sqlite3-wal",
    ],
)
def test_file_cleanup_plan_protects_config_and_database_files(protected_name):
    """白名单里的文件即使时间极旧也必须进 skipped，且 freed_bytes 不累计。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="data",
        retention_days=30,
        entries=[_entry(protected_name, mtime=datetime(2000, 1, 1), size=4096)],
    )

    assert plan["to_delete"] == []
    assert plan["kept"] == []
    assert plan["freed_bytes"] == 0
    assert [item["name"] for item in plan["skipped"]] == [protected_name]
    assert "受保护" in plan["skipped"][0]["reason"]


@pytest.mark.parametrize("suffix", PROTECTED_SUFFIXES)
def test_file_cleanup_plan_protects_every_database_suffix(suffix):
    """逐一验证受保护后缀表里的每一项都被拦住（含 WAL/SHM/journal 伴随文件）。"""
    name = f"snapshot{suffix}"
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="data",
        retention_days=30,
        entries=[_entry(name, mtime=datetime(2000, 1, 1), size=1024)],
    )

    assert plan["to_delete"] == []
    assert plan["freed_bytes"] == 0
    assert [item["name"] for item in plan["skipped"]] == [name]


def test_file_cleanup_plan_suffix_check_is_case_insensitive():
    """后缀判定忽略大小写，避免 ``APP.SQLITE3-WAL`` 这类写法绕过护栏。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="data",
        retention_days=30,
        entries=[_entry("APP.SQLITE3-WAL", mtime=datetime(2000, 1, 1), size=99)],
    )

    assert plan["to_delete"] == []
    assert plan["freed_bytes"] == 0
    assert len(plan["skipped"]) == 1


def test_file_cleanup_plan_protects_nested_database_and_config_paths():
    """带目录前缀的写法同样要拦住，不能只看 basename 之外的部分。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory=".",
        retention_days=30,
        entries=[
            _entry("data/app.sqlite3", mtime=datetime(2000, 1, 1), size=100),
            _entry(r"data\app.sqlite3-wal", mtime=datetime(2000, 1, 1), size=100),
            _entry("nested/.env", mtime=datetime(2000, 1, 1), size=100),
            _entry("sub/config.json", mtime=datetime(2000, 1, 1), size=100),
        ],
    )

    assert plan["to_delete"] == []
    assert plan["freed_bytes"] == 0
    assert len(plan["skipped"]) == 4


def test_file_cleanup_plan_skips_protected_name_sets_are_consistent():
    """白名单常量本身要覆盖需求点名的配置文件名。"""
    assert {".env", "config.json", "state.json"} <= set(PROTECTED_FILE_NAMES)
    assert "data" in PROTECTED_DIRECTORY_NAMES
    assert {".db", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm"} <= set(PROTECTED_SUFFIXES)


def test_file_cleanup_plan_skips_directories():
    """目录项一律 skipped，不进 to_delete、不计入 freed_bytes。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="logs",
        retention_days=30,
        entries=[_entry("archive", mtime=datetime(2000, 1, 1), size=9999, is_dir=True)],
    )

    assert plan["to_delete"] == []
    assert plan["kept"] == []
    assert plan["freed_bytes"] == 0
    assert [item["name"] for item in plan["skipped"]] == ["archive"]
    assert "目录项" in plan["skipped"][0]["reason"]


def test_file_cleanup_plan_skips_unparseable_mtime():
    """修改时间解析失败时保守跳过，不删除。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="logs",
        retention_days=30,
        entries=[
            _entry("broken.log", mtime="not-a-date", size=77),
            _entry("missing-mtime.log", mtime=None, size=88),
        ],
    )

    assert plan["to_delete"] == []
    assert plan["freed_bytes"] == 0
    assert {item["name"] for item in plan["skipped"]} == {"broken.log", "missing-mtime.log"}
    assert all("无法解析" in item["reason"] for item in plan["skipped"])


def test_file_cleanup_plan_freed_bytes_counts_only_deleted_files():
    """freed_bytes 只累计真正进入 to_delete 的大小，跳过与保留项都不算。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="logs",
        retention_days=30,
        entries=[
            _entry("old-a.log", mtime=datetime(2000, 1, 1), size=1000),
            _entry("old-b.log", mtime=datetime(2001, 1, 1), size=2500),
            _entry("recent.log", mtime=NOW - timedelta(hours=2), size=4000),
            _entry(".env", mtime=datetime(2000, 1, 1), size=5000),
            _entry("subdir", mtime=datetime(2000, 1, 1), size=6000, is_dir=True),
            _entry("bad.log", mtime="?", size=7000),
        ],
    )

    assert sorted(item["name"] for item in plan["to_delete"]) == ["old-a.log", "old-b.log"]
    assert plan["freed_bytes"] == 3500
    assert [item["name"] for item in plan["kept"]] == ["recent.log"]
    assert sorted(item["name"] for item in plan["skipped"]) == [".env", "bad.log", "subdir"]


def test_file_cleanup_plan_handles_negative_and_dirty_sizes():
    """负数/非数值 size 记为 0，不影响判定也不污染 freed_bytes。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="logs",
        retention_days=30,
        entries=[
            _entry("neg.log", mtime=datetime(2000, 1, 1), size=-100),
            _entry("text.log", mtime=datetime(2000, 1, 1), size="abc"),
        ],
    )

    assert sorted(item["name"] for item in plan["to_delete"]) == ["neg.log", "text.log"]
    assert plan["freed_bytes"] == 0


def test_file_cleanup_plan_boundary_and_custom_retention():
    """自定义保留天数生效：恰好等于 cutoff 的文件保留，早一秒的删除。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="images",
        retention_days=7,
        entries=[
            _entry("edge.json", mtime=NOW - timedelta(days=7), size=10),
            _entry("old.json", mtime=NOW - timedelta(days=7, seconds=1), size=20),
        ],
    )

    assert [item["name"] for item in plan["to_delete"]] == ["old.json"]
    assert [item["name"] for item in plan["kept"]] == ["edge.json"]
    assert plan["freed_bytes"] == 20
    assert "2026-03-12 12:00:00" in plan["kept"][0]["reason"]


def test_file_cleanup_plan_accepts_timestamp_and_iso_mtime():
    """mtime 支持时间戳与 ISO 字符串（os.scandir 之外的上游可能给这两种形态）。"""
    old_ts = datetime(2000, 1, 1).timestamp()
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="logs",
        retention_days=30,
        entries=[
            _entry("ts.log", mtime=old_ts, size=1),
            _entry("iso.log", mtime="2000-01-01T00:00:00", size=2),
        ],
    )

    assert sorted(item["name"] for item in plan["to_delete"]) == ["iso.log", "ts.log"]
    assert plan["freed_bytes"] == 3


def test_file_cleanup_plan_tolerates_garbage_entries():
    """entries 里混入非字典项时跳过而不是抛异常。"""
    plan = build_file_cleanup_plan(
        now=NOW,
        directory="logs",
        retention_days=30,
        entries=["not-a-dict", _entry("ok.log", mtime=datetime(2000, 1, 1), size=5)],  # type: ignore[list-item]
    )

    assert [item["name"] for item in plan["to_delete"]] == ["ok.log"]
    assert plan["freed_bytes"] == 5
    assert len(plan["skipped"]) == 1


def test_resolve_retention_days_normalizes_invalid_input():
    """resolve_retention_days 永远不返回 0。"""
    assert resolve_retention_days(0) == DEFAULT_FILE_RETENTION_DAYS
    assert resolve_retention_days(-10) == DEFAULT_FILE_RETENTION_DAYS
    assert resolve_retention_days(None) == DEFAULT_FILE_RETENTION_DAYS
    assert resolve_retention_days("abc") == DEFAULT_FILE_RETENTION_DAYS
    assert resolve_retention_days(True) == DEFAULT_FILE_RETENTION_DAYS
    assert resolve_retention_days(7) == 7
    assert resolve_retention_days("14") == 14
    assert resolve_retention_days(0, default=90) == 90


# ======================================================================================
# 交付 1.3：format_bytes / summarize_usage
# ======================================================================================


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0.0 B"),
        (1, "1.0 B"),
        (512, "512.0 B"),
        (1023, "1023.0 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (1024**2, "1.0 MB"),
        (1024**2 * 2 + 1024 * 512, "2.5 MB"),
        (1024**3, "1.0 GB"),
        (1024**3 * 3, "3.0 GB"),
        (1024**4, "1024.0 GB"),
    ],
)
def test_format_bytes_scales(value, expected):
    assert format_bytes(value) == expected


@pytest.mark.parametrize(
    "dirty",
    [-1, -1024, None, "abc", "", True, False, float("nan"), float("inf"), float("-inf"), [], {}],
)
def test_format_bytes_dirty_input_returns_zero(dirty):
    """负数、非数值、bool、容器一律返回 "0 B"，不抛异常。"""
    assert format_bytes(dirty) == "0 B"  # type: ignore[arg-type]


def test_summarize_usage_totals_shares_and_human_sizes():
    report = summarize_usage(
        directories={
            "logs": {"bytes": 1000, "file_count": 2},
            "images": {"bytes": 3000, "file_count": 1},
        }
    )

    assert report["total_bytes"] == 4000
    assert report["total_files"] == 3
    assert report["total_human"] == "3.9 KB"
    assert report["largest_directory"] == "images"
    assert report["directories"] == {
        "images": {"bytes": 3000, "file_count": 1, "human": "2.9 KB", "share_percent": 75.0},
        "logs": {"bytes": 1000, "file_count": 2, "human": "1000.0 B", "share_percent": 25.0},
    }


def test_summarize_usage_zero_totals_use_zero_share():
    report = summarize_usage(directories={"jsonl": {"bytes": 0, "file_count": 0}})

    assert report["total_bytes"] == 0
    assert report["total_human"] == "0.0 B"
    assert report["largest_directory"] == "jsonl"
    assert report["directories"]["jsonl"]["share_percent"] == 0.0


def test_summarize_usage_tolerates_dirty_entries():
    """非字典条目、负数、非数值一律归一化，不影响其他目录的统计。"""
    report = summarize_usage(
        directories={
            "logs": {"bytes": -5, "file_count": "x"},
            "images": "not-a-dict",
            "price_history": {"bytes": 2048},
        }
    )

    assert report["total_bytes"] == 2048
    assert report["total_files"] == 0
    assert report["total_human"] == "2.0 KB"
    assert report["largest_directory"] == "price_history"
    assert report["directories"]["logs"] == {
        "bytes": 0,
        "file_count": 0,
        "human": "0.0 B",
        "share_percent": 0.0,
    }
    assert report["directories"]["images"]["bytes"] == 0
    assert report["directories"]["price_history"]["share_percent"] == 100.0


def test_summarize_usage_empty_input():
    report = summarize_usage(directories={})

    assert report == {
        "directories": {},
        "total_bytes": 0,
        "total_human": "0.0 B",
        "total_files": 0,
        "largest_directory": None,
    }
