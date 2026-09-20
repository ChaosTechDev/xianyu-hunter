"""数据保留策略的纯逻辑计算。

本模块只回答「算什么该删」，**不执行任何删除动作、不连接数据库、不读写文件**：

* 数据库清理：输出每张表的 ``cutoff`` 与 ``should_prune`` 决策，由调用方用
  ``DELETE FROM <表> WHERE <时间列> < cutoff`` 执行；
* 文件清理：输入是调用方已经用 ``os.scandir`` 收集好的文件元信息，输出待删清单与可释放字节数。

这样拆分的原因是「决策」可以用固定 ``now`` 做到完全确定性的单元测试，而「执行」留在调用方手里，
避免一处配置笔误直接删掉长期运行累积的生产数据。

保留天数的一处关键取舍：**任何非法保留天数（0 / 负数 / None / 非数值 / NaN / inf）都回落到
该字段的默认值，绝不解释成「删除全部」**。理由：保留天数来自配置，写错一个 ``0`` 会让
``cutoff == now``，若按字面执行就是清空整库；把 ``0`` 当成「全删」是对语义的危险扩展，
所以本模块把 ``0`` 与负数同等视为非法配置并回落到默认值。真要做「全删」，请在业务代码里
显式写出删除语句，不要指望用保留天数表达。
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

__all__ = [
    "RetentionPolicy",
    "RETENTION_TARGETS",
    "DEFAULT_FILE_RETENTION_DAYS",
    "PROTECTED_FILE_NAMES",
    "PROTECTED_DIRECTORY_NAMES",
    "PROTECTED_SUFFIXES",
    "build_cleanup_plan",
    "build_file_cleanup_plan",
    "summarize_usage",
    "format_bytes",
    "resolve_retention_days",
]


@dataclass(frozen=True)
class RetentionPolicy:
    """各表的保留天数策略。

    单位是天，``cutoff = now - timedelta(days=天数)``。字段值为非法时回落到这里的默认值，
    默认值本身即「该字段的合理默认」，因此不需要额外兜底常量。
    """

    result_items_days: int = 90
    price_snapshots_days: int = 180
    watch_events_days: int = 180
    logs_days: int = 30
    ai_usage_days: int = 365


#: 清理目标 -> ``RetentionPolicy`` 字段名。顺序即决策结果的键顺序，便于调用方稳定展示。
#:
#: 键名与库表/业务域的对应关系：
#: * ``result_items``   -> ``result_items.crawl_time``（采集结果明细）
#: * ``price_snapshots``-> ``price_snapshots.snapshot_time``（价格快照，支撑趋势图，保留期最长）
#: * ``watch_events``   -> ``watch_events.created_at``（关注事件，含未读标记，需给用户足够回看时间）
#: * ``logs``           -> 运行日志。**注意：实际 schema 中没有 ``app_logs`` 表**，
#:   日志落在磁盘 ``logs/`` 目录，由 :func:`build_file_cleanup_plan` 处理。
#:   这里保留 ``logs`` 这个键是为了给「日志保留天数」一个统一的配置出口，
#:   执行层（``retention_runner``）会把它的数据库列映射为 ``None`` 并跳过 SQL。
#: * ``ai_usage_stats`` -> ``ai_usage_stats.created_at``（AI 用量与成本核算，按年审计）
RETENTION_TARGETS: dict[str, str] = {
    "result_items": "result_items_days",
    "price_snapshots": "price_snapshots_days",
    "watch_events": "watch_events_days",
    "logs": "logs_days",
    "ai_usage_stats": "ai_usage_days",
}

#: 文件清理保留天数非法时的回落值（天）。
DEFAULT_FILE_RETENTION_DAYS = 30

#: 受保护的文件名（配置与持久状态）。误删会导致应用无法启动或丢失任务/登录态定义。
PROTECTED_FILE_NAMES = frozenset({".env", "config.json", "state.json", "state.sample.json"})

#: 受保护的目录名。``data/`` 存放数据库本体与密钥文件，整体视为禁区。
PROTECTED_DIRECTORY_NAMES = frozenset({"data"})

#: 受保护的后缀。数据库本体与其 WAL/SHM/journal 伴随文件都不能靠删文件清理：
#: WAL 模式下 ``-wal`` 里可能还有尚未 checkpoint 的已提交事务，删掉它们等于丢数据；
#: 数据库瘦身必须走 ``DELETE`` + ``VACUUM``，不是文件系统层面的操作。
PROTECTED_SUFFIXES = (
    ".db",
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".sqlite",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".sqlite3",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".sqlite3-journal",
)

#: 时间字符串的规范输出格式，与库内 ``crawl_time`` / ``created_at`` 的格式保持一致。
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: 单表决策的字段顺序，保持所有返回值形状一致。
_TABLE_KEYS = ("cutoff", "should_prune", "reason")

_DEFAULT_RETENTION_DAYS: dict[str, int] = {
    field.name: int(field.default) for field in dataclasses.fields(RetentionPolicy)
}


# --------------------------------------------------------------------------------------
# 内部工具：数值与时间归一化
# --------------------------------------------------------------------------------------


def _coerce_days(value: object, default: int) -> int:
    """把保留天数归一化成 >= 1 的整数，非法值回落到 ``default``。

    ``0`` / 负数 / ``None`` / 非数值 / ``NaN`` / ``inf`` 全部回落到默认值——见模块 docstring
    里关于「0 天不等于全删」的取舍说明。``bool`` 也按非法处理：``True`` 出现在这里一定是调用方 bug。
    """
    number: float | None = None
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return default
    else:
        return default
    if number is None or not math.isfinite(number) or number < 1:
        return default
    return int(number)


def resolve_retention_days(value: object, *, default: int = DEFAULT_FILE_RETENTION_DAYS) -> int:
    """返回实际生效的保留天数，供调用方在回落发生时知道真实阈值。

    非法输入（含 ``0`` 与负数）一律回落到 ``default``，永远不会返回 ``0``。
    """
    fallback = _coerce_days(default, DEFAULT_FILE_RETENTION_DAYS)
    return _coerce_days(value, fallback)


def _coerce_size(value: object) -> int:
    """把字节数归一化成 >= 0 的整数；非法/负数/NaN/inf 一律记 0。"""
    if isinstance(value, bool) or value is None:
        return 0
    number: float | None = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except (TypeError, ValueError):
            return 0
    else:
        return 0
    if number is None or not math.isfinite(number) or number < 0:
        return 0
    return int(number)


def _coerce_count(value: object) -> int:
    """把文件计数归一化成 >= 0 的整数；非法值记 0。"""
    return _coerce_size(value)


def _parse_datetime(value: object) -> datetime | None:
    """解析库内时间字符串，失败返回 ``None``。

    接受的形式：``"2026-03-19 12:00:00"``、ISO 8601（含 ``T`` 分隔与微秒）、纯日期，
    以及 ``datetime`` 本身与 ``int``/``float`` 时间戳。解析不出结果时返回 ``None``，
    由调用方决定保守策略（本模块一律选择「不清理」）。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)):
            return None
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        # Python 3.11+ 的 fromisoformat 已覆盖 "YYYY-MM-DD HH:MM:SS" 与带 T / 时区 / 微秒的写法
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _coerce_now(now: object) -> datetime:
    """把 ``now`` 归一化成 ``datetime``；无法解析时抛出 ``ValueError``。"""
    if isinstance(now, datetime):
        return now
    parsed = _parse_datetime(now)
    if parsed is None:
        raise ValueError(f"now 必须是 datetime 或可解析的时间字符串，收到：{now!r}")
    return parsed


def _align_tz(moment: datetime, reference: datetime) -> datetime:
    """把 ``moment`` 的时区状态对齐到 ``reference``，避免 naive/aware 混用导致比较报错。"""
    if reference.tzinfo is None and moment.tzinfo is not None:
        return moment.replace(tzinfo=None)
    if reference.tzinfo is not None and moment.tzinfo is None:
        return moment.replace(tzinfo=reference.tzinfo)
    return moment


def _format_dt(moment: datetime) -> str:
    return moment.strftime(_TIME_FORMAT)


def _table_decision(cutoff: datetime, should_prune: bool, reason: str) -> dict:
    decision = {
        "cutoff": _format_dt(cutoff),
        "should_prune": should_prune,
        "reason": reason,
    }
    assert tuple(decision) == _TABLE_KEYS  # 保证返回形状稳定
    return decision


# --------------------------------------------------------------------------------------
# 交付 1.1：数据库清理计划
# --------------------------------------------------------------------------------------


def build_cleanup_plan(
    policy: RetentionPolicy,
    *,
    now: datetime,
    oldest_dates: dict[str, str | None],
) -> dict:
    """计算每张表是否需要清理，返回 ``{表名: {"cutoff", "should_prune", "reason"}}``。

    :param policy: 保留策略；字段值非法时按字段回落到 :class:`RetentionPolicy` 的默认值
    :param now: 参考时间点，决定 cutoff；生产调用传 ``datetime.now()``，测试传固定值
    :param oldest_dates: 各表当前最早一条记录的时间字符串，``None`` 或空白表示表为空；
        解析失败的表一律 ``should_prune=False``（拿不准就不删）

    判定规则（判据与调用方的 SQL 谓词一致，都是严格早于）：

    * 表为空 -> ``should_prune=False``，无需清理
    * 最早记录 ``< cutoff`` -> ``should_prune=True``，存在超出保留期的历史数据
    * 最早记录 ``>= cutoff`` -> ``should_prune=False``，全部数据都在保留期内
    * 时间字符串解析失败 -> ``should_prune=False``，理由写进 ``reason``

    ``reason`` 始终是中文说明，并带上实际生效的保留天数与 cutoff，
    这样即使发生配置回落，调用方与界面也能看到真实阈值。
    """
    reference = _coerce_now(now)
    source = oldest_dates if isinstance(oldest_dates, dict) else {}

    plan: dict[str, dict] = {}
    for target, field_name in RETENTION_TARGETS.items():
        default_days = _DEFAULT_RETENTION_DAYS[field_name]
        days = _coerce_days(getattr(policy, field_name, None), default_days)
        cutoff = reference - timedelta(days=days)
        cutoff_text = _format_dt(cutoff)

        raw = source.get(target)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            plan[target] = _table_decision(
                cutoff,
                False,
                f"{target} 当前没有记录（最早记录时间为空），没有可清理的数据；"
                f"保留界限为 {cutoff_text}（保留 {days} 天）。",
            )
            continue

        oldest = _parse_datetime(raw)
        if oldest is None:
            plan[target] = _table_decision(
                cutoff,
                False,
                f"{target} 的最早记录时间 {raw!r} 无法解析，拿不准就不清理，"
                f"以免误删仍在保留期内的数据；保留界限为 {cutoff_text}（保留 {days} 天）。",
            )
            continue

        oldest_text = _format_dt(_align_tz(oldest, reference))
        if _align_tz(oldest, reference) < cutoff:
            plan[target] = _table_decision(
                cutoff,
                True,
                f"{target} 最早记录 {oldest_text} 早于保留界限 {cutoff_text}"
                f"（保留 {days} 天），存在超出保留期的历史数据，可执行删除。",
            )
        else:
            plan[target] = _table_decision(
                cutoff,
                False,
                f"{target} 最早记录 {oldest_text} 不早于保留界限 {cutoff_text}"
                f"（保留 {days} 天），全部数据都在保留期内，无需清理。",
            )
    return plan


# --------------------------------------------------------------------------------------
# 交付 1.2：文件清理计划
# --------------------------------------------------------------------------------------


def _protection_reason(name: str) -> str | None:
    """判断条目是否落在保留白名单里；命中则返回中文原因，否则返回 ``None``。

    逐个路径段检查（而不是只看 basename），这样 ``data/app.sqlite3``、``nested/.env``
    这类带目录的写法同样会被拦住。
    """
    normalized = str(name).replace("\\", "/").strip()
    if not normalized:
        return "条目名称为空，无法判断类型，保守跳过。"
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    for part in parts:
        lowered = part.lower()
        if part.startswith("."):
            return f"受保护项 {part!r}：点号开头的名字属于隐藏配置/状态文件，跳过清理。"
        if lowered in PROTECTED_DIRECTORY_NAMES:
            return f"受保护目录 {part!r}：该目录存放数据库本体与持久状态，跳过清理。"
        if lowered in PROTECTED_FILE_NAMES:
            return f"受保护项 {part!r}：属于配置/状态文件，误删会让应用无法恢复，跳过清理。"
        if lowered.endswith(PROTECTED_SUFFIXES):
            return (
                f"受保护项 {part!r}：属于数据库文件（含 WAL/SHM 伴随文件），"
                "数据库清理必须走 SQL DELETE，不能靠删文件，跳过清理。"
            )
    return None


def _parse_mtime(value: object, reference: datetime) -> datetime | None:
    """解析文件修改时间；无法判断时返回 ``None``（调用方按「不删」处理）。"""
    moment = _parse_datetime(value)
    if moment is None:
        return None
    return _align_tz(moment, reference)


def build_file_cleanup_plan(
    *,
    now: datetime,
    directory: str,
    entries: list[dict],
    retention_days: int,
) -> dict:
    """根据已收集的目录项元信息，计算该删哪些文件。

    :param now: 参考时间点，决定 cutoff
    :param directory: 目录名，只用于在 ``reason`` 里说明来源，不做任何 IO
    :param entries: 调用方用 ``os.scandir`` 收集好的条目：
        ``[{"name": "x.jsonl", "mtime": datetime, "size": int, "is_dir": bool}, ...]``
        （``mtime`` 也接受时间戳或时间字符串）
    :param retention_days: 保留天数；``0`` / 负数 / ``None`` / 非数值一律回落到
        :data:`DEFAULT_FILE_RETENTION_DAYS`，**绝不**解释成「删全部」

    :return: ``{"to_delete": [...], "kept": [...], "skipped": [...], "freed_bytes": int}``，
        每个条目是 ``{"name", "size", "reason"}``。实际生效的保留天数与 cutoff 写在每条 ``reason`` 中，
        调用方若需单独取值可用 :func:`resolve_retention_days`。

    安全护栏（按命中顺序）：

    1. 保留天数非法 -> 回落默认 30 天，不放大删除范围；
    2. 命中保留白名单（``.env`` / ``config.json`` / ``state.json`` / 点号开头的名字 /
       ``data/`` 目录 / ``*.db``、``*.sqlite3`` 及其 ``-wal``/``-shm``/``-journal`` 伴随文件）
       -> 一律 ``skipped``；
    3. 目录项（``is_dir=True``）-> 一律 ``skipped``，本模块只处理文件；
    4. 修改时间无法解析 -> ``skipped``（拿不准就不删）；
    5. ``freed_bytes`` 只累计真正进入 ``to_delete`` 的文件大小。
    """
    reference = _coerce_now(now)
    days = _coerce_days(retention_days, DEFAULT_FILE_RETENTION_DAYS)
    cutoff = reference - timedelta(days=days)
    cutoff_text = _format_dt(cutoff)
    source = entries if isinstance(entries, list) else []

    to_delete: list[dict] = []
    kept: list[dict] = []
    skipped: list[dict] = []
    freed_bytes = 0

    for entry in source:
        if not isinstance(entry, dict):
            skipped.append(
                {
                    "name": "",
                    "size": 0,
                    "reason": "条目结构非法（不是字典），无法判断类型，保守跳过。",
                }
            )
            continue

        name = str(entry.get("name") or "")
        size = _coerce_size(entry.get("size"))
        base = {"name": name, "size": size}

        if bool(entry.get("is_dir")):
            skipped.append(
                {
                    **base,
                    "reason": f"目录项 {name!r} 一律不删除：目录清理需要递归判断，"
                    "由调用方单独处理，本模块只输出文件级决策。",
                }
            )
            continue

        guard = _protection_reason(name)
        if guard is not None:
            skipped.append({**base, "reason": f"{directory} 中的 {guard}"})
            continue

        mtime = _parse_mtime(entry.get("mtime"), reference)
        if mtime is None:
            skipped.append(
                {
                    **base,
                    "reason": f"{name!r} 的修改时间无法解析，拿不准就不删；"
                    f"保留界限为 {cutoff_text}（保留 {days} 天）。",
                }
            )
            continue

        mtime_text = _format_dt(mtime)
        if mtime < cutoff:
            to_delete.append(
                {
                    **base,
                    "reason": f"修改时间 {mtime_text} 早于保留界限 {cutoff_text}"
                    f"（保留 {days} 天），可删除并释放 {format_bytes(size)}。",
                }
            )
            freed_bytes += size
        else:
            kept.append(
                {
                    **base,
                    "reason": f"修改时间 {mtime_text} 在保留期内"
                    f"（保留 {days} 天，界限 {cutoff_text}），保留。",
                }
            )

    return {
        "to_delete": to_delete,
        "kept": kept,
        "skipped": skipped,
        "freed_bytes": freed_bytes,
    }


# --------------------------------------------------------------------------------------
# 交付 1.3：磁盘用量报告
# --------------------------------------------------------------------------------------

def format_bytes(n: int) -> str:
    """把字节数格式化成人类可读字符串，进率为 1024，保留 1 位小数。

    非法输入（负数 / ``None`` / 非数值字符串 / ``NaN`` / ``inf`` / ``bool``）一律返回 ``"0 B"``。
    正常路径各档位统一保留 1 位小数（``0 -> "0.0 B"``、``512 -> "512.0 B"``），
    超出 GB 的量级继续用 GB 表示（如 ``"2048.0 GB"``），不引入 TB 档以免与界面单位约定冲突。
    """
    if isinstance(n, bool) or n is None:
        return "0 B"
    number: float | None = None
    if isinstance(n, (int, float)):
        number = float(n)
    elif isinstance(n, str):
        try:
            number = float(n.strip())
        except (TypeError, ValueError):
            return "0 B"
    else:
        return "0 B"
    if number is None or not math.isfinite(number) or number < 0:
        return "0 B"
    if number < 1024:
        return f"{number:.1f} B"
    if number < 1024**2:
        return f"{number / 1024:.1f} KB"
    if number < 1024**3:
        return f"{number / 1024**2:.1f} MB"
    return f"{number / 1024**3:.1f} GB"


def summarize_usage(*, directories: dict[str, dict]) -> dict:
    """汇总各目录磁盘用量，返回带总量、占比与人类可读大小的报告。

    :param directories: ``{"logs": {"bytes": 123, "file_count": 4}, ...}``；
        ``bytes`` 为负数/非数值按 0 计，``file_count`` 同理

    :return: 键为 ``directories`` / ``total_bytes`` / ``total_human`` / ``total_files`` /
        ``largest_directory``。``directories`` 内的每个子项含 ``bytes`` / ``file_count`` /
        ``human`` / ``share_percent``（占总量百分比，保留 1 位小数；总量为 0 时全部记 0.0）。
        子项按目录名排序输出，保证渲染顺序稳定；``largest_directory`` 在并列时取名称靠前者，
        全空输入时为 ``None``。
    """
    data = directories if isinstance(directories, dict) else {}

    per_directory: dict[str, dict] = {}
    total_bytes = 0
    total_files = 0
    for name in sorted(data, key=lambda key: str(key)):
        item = data[name] if isinstance(data[name], dict) else {}
        size = _coerce_size(item.get("bytes"))
        count = _coerce_count(item.get("file_count"))
        per_directory[str(name)] = {
            "bytes": size,
            "file_count": count,
            "human": format_bytes(size),
            "share_percent": 0.0,
        }
        total_bytes += size
        total_files += count

    if total_bytes > 0:
        for item in per_directory.values():
            item["share_percent"] = round(item["bytes"] / total_bytes * 100, 1)

    largest = max(per_directory, key=lambda key: per_directory[key]["bytes"]) if per_directory else None

    return {
        "directories": per_directory,
        "total_bytes": total_bytes,
        "total_human": format_bytes(total_bytes),
        "total_files": total_files,
        "largest_directory": largest,
    }
