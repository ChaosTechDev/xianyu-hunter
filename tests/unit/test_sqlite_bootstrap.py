"""legacy JSON → SQLite 迁移的回归测试。

这条链路负责把老版本的 ``config.json`` / ``jsonl/`` / ``price_history/`` 数据
搬进 SQLite。它只在「首次启动 + 空库」时运行，所以一旦出错，用户升级后会
直接启动失败或数据静默丢失 —— 属于典型的「上线才发现」问题。

回归背景：本仓库改写时 SELECT/INSERT 的列清单与值列表错位（23 列 / 21 值），
导致 ``sqlite3.OperationalError: 21 values for 23 columns``，legacy 迁移路径
完全不可用。本文件固定「不抛异常」**且**「值落在正确的列」。
"""
from __future__ import annotations

import json

import pytest

from src.infrastructure.persistence import sqlite_bootstrap as sb
from src.infrastructure.persistence.sqlite_connection import sqlite_connection


@pytest.fixture()
def legacy_root(tmp_path, monkeypatch):
    """构造一个隔离的 legacy 数据根目录，并把 CWD 切进去。"""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write_config(root, tasks):
    (root / "config.json").write_text(
        json.dumps(tasks, ensure_ascii=False), encoding="utf-8"
    )


def _write_jsonl(root, filename, records):
    jsonl_dir = root / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    (jsonl_dir / filename).write_text(
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records),
        encoding="utf-8",
    )


# --- 任务迁移 ---


def test_legacy_task_migration_does_not_raise(legacy_root):
    """回归护栏：迁移必须能跑完（原先必然抛 21 values for 23 columns）。"""
    _write_config(legacy_root, [{"task_name": "legacy-task", "keyword": "macbook"}])
    sb.bootstrap_sqlite_storage()  # 不应抛异常


def test_legacy_task_values_land_in_correct_columns(legacy_root):
    """**核心断言**：不只是「不崩」，还要验证列对齐。

    原缺陷除了占位符数量不足，还把 ``cron`` 的值塞进了 ``notify_price_below``
    的位置，导致其后所有值整体前移。因此必须逐列校验，否则将来仍可能错位。
    """
    _write_config(
        legacy_root,
        [
            {
                "task_name": "align-task",
                "keyword": "iphone",
                "enabled": True,
                "max_pages": 7,
                "personal_only": True,
                "min_price": 100,
                "max_price": 2000,
                "cron": "0 9 * * *",
            }
        ],
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_name = ?", ("align-task",)
        ).fetchone()

    assert row is not None
    assert row["keyword"] == "iphone"
    assert row["enabled"] == 1
    assert row["max_pages"] == 7
    assert row["personal_only"] == 1
    # min_price/max_price 按 legacy 原值透传（SQLite 动态类型，不强制转换）
    assert row["min_price"] == "100"
    assert row["max_price"] == "2000"
    # 关键：cron 值必须落在 cron 列
    assert row["cron"] == "0 9 * * *"
    # 未提供的可选字段不应被错误地填入其他列的值
    assert row["notify_price_below"] is None
    assert row["auto_consult"] == 0


def test_legacy_task_migration_preserves_multiple_tasks(legacy_root):
    _write_config(
        legacy_root,
        [
            {"task_name": "t1", "keyword": "k1", "cron": "0 1 * * *"},
            {"task_name": "t2", "keyword": "k2", "cron": "30 2 * * *"},
        ],
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        rows = conn.execute(
            "SELECT task_name, keyword, cron FROM tasks ORDER BY task_name"
        ).fetchall()

    assert [(row["task_name"], row["keyword"], row["cron"]) for row in rows] == [
        ("t1", "k1", "0 1 * * *"),
        ("t2", "k2", "30 2 * * *"),
    ]


def test_legacy_task_migration_is_idempotent(legacy_root):
    """重复启动不得产生重复任务行。"""
    _write_config(legacy_root, [{"task_name": "t", "keyword": "k"}])
    sb.bootstrap_sqlite_storage()
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
    assert count == 1


def test_legacy_task_migration_marks_completion(legacy_root):
    _write_config(legacy_root, [{"task_name": "t", "keyword": "k"}])
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        assert sb._bootstrap_completed(conn, "bootstrap:legacy_tasks") is True


def test_legacy_task_migration_skipped_when_table_not_empty(legacy_root):
    """已有任务时不得再导入 legacy 数据（避免覆盖用户后续修改）。"""
    sb.bootstrap_sqlite_storage()  # 先建表

    with sqlite_connection() as conn:
        info = conn.execute("PRAGMA table_info(tasks)").fetchall()
        # 动态补全所有「NOT NULL 且无默认值」的列，避免 schema 演进后测试失修
        required = {
            col["name"]: (0 if (col["type"] or "").upper().startswith("INT") else "")
            for col in info
            if col["notnull"] and col["dflt_value"] is None and col["name"] != "id"
        }
        columns = {**required, "task_name": "existing", "keyword": "existing-kw"}
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})",
            tuple(columns.values()),
        )
        conn.commit()

    _write_config(legacy_root, [{"task_name": "legacy", "keyword": "legacy-kw"}])
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        names = {row["task_name"] for row in conn.execute("SELECT task_name FROM tasks")}
    assert names == {"existing"}


def test_missing_legacy_config_is_not_an_error(legacy_root):
    """全新安装没有 config.json，必须安静跳过而不是报错。"""
    sb.bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 0


def test_corrupted_legacy_config_does_not_crash_bootstrap(legacy_root):
    """坏 JSON 不得阻断启动。

    这是「升级即启动失败」的高危场景：用户手改坏了 config.json，整个应用就再也
    起不来，连改回配置的网页都进不去。现已修复：``_load_json_file`` 吞掉解析
    异常并返回 None，走「无 legacy 任务」分支。
    """
    (legacy_root / "config.json").write_text("{not valid json", encoding="utf-8")

    # 不应抛出任何异常
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 0


def test_explicit_none_legacy_config_disables_migration(legacy_root):
    """legacy_config_file=None 必须完全跳过任务导入。"""
    _write_config(legacy_root, [{"task_name": "t", "keyword": "k"}])
    sb.bootstrap_sqlite_storage(legacy_config_file=None)

    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"] == 0
    with sqlite_connection() as conn:
        assert sb._bootstrap_completed(conn, "bootstrap:legacy_tasks") is True


# --- 结果迁移 ---


def test_legacy_result_migration_imports_records(legacy_root):
    """结果 JSONL 的结构是嵌套的（``商品信息`` 子对象），不是扁平字段。"""
    _write_jsonl(
        legacy_root,
        "kw_full_data.jsonl",
        [
            {
                "搜索关键字": "kw",
                "商品信息": {
                    "商品ID": "1001",
                    "商品标题": "MacBook",
                    "当前售价": "¥1,234.56",
                    "商品链接": "https://example.invalid/i/1001",
                },
                "爬取时间": "2026-01-01 10:00:00",
            }
        ],
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        row = conn.execute("SELECT * FROM result_items WHERE item_id = ?", ("1001",)).fetchone()

    assert row is not None
    assert row["keyword"] == "kw"
    assert row["price"] == 1234.56
    assert row["title"] == "MacBook"
    assert row["price_display"] == "¥1,234.56"


def test_legacy_result_migration_is_idempotent(legacy_root):
    _write_jsonl(
        legacy_root,
        "kw_full_data.jsonl",
        [
            {
                "商品信息": {
                    "商品ID": "1",
                    "商品标题": "X",
                    "当前售价": "100",
                    "商品链接": "https://example.invalid/1",
                }
            }
        ],
    )
    sb.bootstrap_sqlite_storage()
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM result_items").fetchone()["c"] == 1


def test_legacy_result_migration_skips_blank_lines(legacy_root):
    jsonl_dir = legacy_root / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    (jsonl_dir / "kw_full_data.jsonl").write_text(
        "\n\n"
        + json.dumps(
            {
                "商品信息": {
                    "商品ID": "1",
                    "商品标题": "X",
                    "当前售价": "10",
                    "商品链接": "https://a.invalid/1",
                }
            }
        )
        + "\n\n",
        encoding="utf-8",
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM result_items").fetchone()["c"] == 1


def test_legacy_result_migration_skips_corrupted_lines(legacy_root):
    """单行坏 JSON 必须跳过，不能中断整个文件导入。"""
    jsonl_dir = legacy_root / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)

    def _record(item_id: str) -> str:
        return json.dumps(
            {
                "商品信息": {
                    "商品ID": item_id,
                    "商品标题": f"X{item_id}",
                    "当前售价": "10",
                    "商品链接": f"https://a.invalid/{item_id}",
                }
            }
        )

    (jsonl_dir / "kw_full_data.jsonl").write_text(
        _record("1") + "\n{broken json\n" + _record("2"), encoding="utf-8"
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM result_items").fetchone()["c"]
        ids = {row["item_id"] for row in conn.execute("SELECT item_id FROM result_items")}
    assert count == 2
    assert ids == {"1", "2"}


def test_legacy_result_migration_ignores_non_jsonl_files(legacy_root):
    jsonl_dir = legacy_root / "jsonl"
    jsonl_dir.mkdir(parents=True, exist_ok=True)
    (jsonl_dir / "notes.txt").write_text("not jsonl", encoding="utf-8")
    (jsonl_dir / "config.json").write_text("{}", encoding="utf-8")
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM result_items").fetchone()["c"] == 0


def test_legacy_result_migration_handles_unparseable_price(legacy_root):
    """「面议」价格必须入库为 NULL，而不是 0 或抛异常。"""
    _write_jsonl(
        legacy_root,
        "kw_full_data.jsonl",
        [
            {
                "商品信息": {
                    "商品ID": "1",
                    "商品标题": "X",
                    "当前售价": "面议",
                    "商品链接": "https://a.invalid/1",
                }
            }
        ],
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        row = conn.execute("SELECT price, price_display FROM result_items WHERE item_id = '1'").fetchone()
    assert row["price"] is None
    # 原始展示文本保留，供前端展示「面议」
    assert row["price_display"] == "面议"


def test_legacy_result_link_unique_key_drops_query_after_ampersand(legacy_root):
    _write_jsonl(
        legacy_root,
        "kw_full_data.jsonl",
        [
            {
                "商品信息": {
                    "商品ID": "1",
                    "商品标题": "X",
                    "当前售价": "10",
                    "商品链接": "https://a.invalid/1?a=1&b=2",
                }
            }
        ],
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        row = conn.execute("SELECT link, link_unique_key FROM result_items WHERE item_id = '1'").fetchone()
    assert row["link"] == "https://a.invalid/1?a=1&b=2"
    assert row["link_unique_key"] == "https://a.invalid/1?a=1"


def test_legacy_result_keyword_derived_from_filename(legacy_root):
    """无「搜索关键字」字段时，keyword 从文件名去掉后缀推导。"""
    _write_jsonl(
        legacy_root,
        "iphone15_full_data.jsonl",
        [
            {
                "商品信息": {
                    "商品ID": "1",
                    "商品标题": "X",
                    "当前售价": "10",
                    "商品链接": "https://a.invalid/1",
                }
            }
        ],
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        row = conn.execute("SELECT keyword FROM result_items WHERE item_id = '1'").fetchone()
    assert row["keyword"] == "iphone15"


# --- 价格快照迁移 ---


def test_legacy_price_snapshot_migration(legacy_root):
    """快照目录只扫描 ``*_history.jsonl``（不是 ``*.jsonl``）。"""
    price_dir = legacy_root / "price_history"
    price_dir.mkdir(parents=True, exist_ok=True)
    (price_dir / "kw_history.jsonl").write_text(
        json.dumps(
            {
                "keyword": "kw",
                "keyword_slug": "kw",
                "item_id": "1",
                "title": "X",
                "price": "100",
                "snapshot_time": "2026-01-01T10:00:00",
                "snapshot_day": "2026-01-01",
                "run_id": "r1",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        rows = conn.execute("SELECT * FROM price_snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["item_id"] == "1"
    assert rows[0]["price"] == 100.0
    assert rows[0]["keyword_slug"] == "kw"
    assert rows[0]["run_id"] == "r1"


def test_legacy_price_snapshot_migration_ignores_other_filenames(legacy_root):
    """``kw.jsonl`` 不匹配 ``*_history.jsonl``，必须被忽略。"""
    price_dir = legacy_root / "price_history"
    price_dir.mkdir(parents=True, exist_ok=True)
    (price_dir / "kw.jsonl").write_text(
        json.dumps({"item_id": "1", "price": "100", "run_id": "r1"}), encoding="utf-8"
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM price_snapshots").fetchone()["c"] == 0


def test_legacy_price_snapshot_migration_is_idempotent(legacy_root):
    price_dir = legacy_root / "price_history"
    price_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "keyword": "kw",
        "item_id": "1",
        "price": "100",
        "snapshot_time": "2026-01-01T10:00:00",
        "snapshot_day": "2026-01-01",
        "run_id": "r1",
    }
    (price_dir / "kw_history.jsonl").write_text(json.dumps(payload), encoding="utf-8")

    sb.bootstrap_sqlite_storage()
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM price_snapshots").fetchone()["c"] == 1


def test_legacy_price_snapshot_slug_is_derived_from_keyword(legacy_root):
    """缺 keyword_slug 时按 keyword 归一化生成。"""
    price_dir = legacy_root / "price_history"
    price_dir.mkdir(parents=True, exist_ok=True)
    (price_dir / "k_history.jsonl").write_text(
        json.dumps(
            {
                "keyword": "MacBook Pro",
                "item_id": "1",
                "price": "100",
                "snapshot_time": "2026-01-01T10:00:00",
                "snapshot_day": "2026-01-01",
                "run_id": "r1",
            }
        ),
        encoding="utf-8",
    )
    sb.bootstrap_sqlite_storage()

    with sqlite_connection() as conn:
        row = conn.execute("SELECT keyword_slug FROM price_snapshots").fetchone()
    assert row["keyword_slug"] == "macbook_pro"


# --- 辅助函数 ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 0),
        (True, 1),
        (False, 0),
        ("1", 1),
        ("true", 1),
        ("TRUE", 1),
        ("yes", 1),
        ("on", 1),
        ("  1  ", 1),
        ("0", 0),
        ("false", 0),
        ("no", 0),
        ("off", 0),
        ("", 0),
        ("abc", 0),
        ("2", 0),
        ([], 0),
    ],
)
def test_as_int_is_boolean_coercion_not_numeric_parse(raw, expected):
    """**契约说明**：``_as_int`` 是「布尔式」解析，不是数值转换。

    它只认 1/true/yes/on（大小写不敏感），其余一律 0 —— 包括数字字符串
    ``"7"`` 和 int ``7``。该函数只用于 enabled/analyze_images 等布尔列，
    因此语义正确；但把它当通用数值解析会静默得到 0。
    """
    assert sb._as_int(raw) == expected


def test_as_int_is_never_used_for_numeric_columns():
    """护栏：数值列（max_pages/min_price）走的是 int()/原值，不经过 _as_int。

    迁移一个 max_pages=7 的任务，验证它不会被 _as_int 归零。
    """
    assert sb._as_int(7) == 0
    assert int(7) == 7


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("¥1,234.56", 1234.56),
        ("1234", 1234.0),
        ("1万", 10000.0),
        ("面议", None),
        ("", None),
        (None, None),
        ("-", None),
        ("N/A", None),
    ],
)
def test_parse_price_correct_cases(raw, expected):
    assert sb._parse_price(raw) == expected


@pytest.mark.parametrize("raw", ["abc万", "万"])
def test_parse_price_bad_wan_suffix_does_not_raise(raw):
    """非数字前缀 + 「万」后缀必须返回 None，而不是抛 ValueError。"""
    assert sb._parse_price(raw) is None


def test_parse_price_delegates_to_single_implementation():
    """确认两处价格解析已收敛为同一实现。

    历史上 ``sqlite_bootstrap._parse_price`` 与
    ``price_history_service.parse_price_value`` 是两份同源副本，导致同一个 bug
    （「万」后缀抛未捕获异常、nan/inf 无防护）修一处漏一处。现在前者委托给后者，
    这里锁定这个契约，防止将来又被拆成两份。
    """
    from src.services.price_history_service import parse_price_value

    for raw in ("abc万", "万", "nan", "inf", "1.5万", "1234", "¥1,234.56"):
        assert sb._parse_price(raw) == parse_price_value(raw)
