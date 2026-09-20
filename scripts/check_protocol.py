#!/usr/bin/env python
"""闲鱼协议模块离线自检脚本。

**纯离线**：不发任何真实网络请求，不读 cookie 文件，不连数据库。
所有断言针对 :mod:`src.services.xy_protocol` 下的纯函数。

用法::

    python scripts/check_protocol.py

全部通过退出码 0，任一用例失败退出码 1。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from unittest.mock import patch

# 脚本位于 <项目根>/scripts/，把项目根加入 sys.path 才能 import src.*
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from src.services.xy_protocol import (  # noqa: E402
    APP_KEY,
    DEAD_MARKERS,
    ItemStatus,
    build_sign,
    classify_error,
    compact_json,
    extract_ret,
    extract_token,
    is_retryable,
    judge_status,
    now_ms,
    sign_params,
)

# ---------------------------------------------------------------- 测试框架

_PASSED = 0
_FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    global _PASSED
    if condition:
        _PASSED += 1
        print(f"  [PASS] {name}")
    else:
        _FAILED.append(name if not detail else f"{name} :: {detail}")
        print(f"  [FAIL] {name}" + (f" :: {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 1. 签名验证

#: 手工独立计算的基准值（用 .NET MD5 与 hashlib 两条路径交叉验证过）
GOLDEN_TOKEN = "testtoken"
GOLDEN_T_MS = 1700000000000
GOLDEN_DATA = '{"itemId":"123456"}'
GOLDEN_SIGN = "9c30fc5fddb2fc1ab4dc21edab69c286"


def test_sign_golden() -> None:
    section("1. 签名基准值验证")

    raw = f"{GOLDEN_TOKEN}&{GOLDEN_T_MS}&{APP_KEY}&{GOLDEN_DATA}"
    print(f"  原文: {raw}")

    actual = build_sign(GOLDEN_TOKEN, GOLDEN_T_MS, GOLDEN_DATA)
    print(f"  实际签名: {actual}")
    print(f"  期望签名: {GOLDEN_SIGN}")

    check("build_sign 输出等于硬编码基准值", actual == GOLDEN_SIGN, f"got {actual}")

    # 用第三条独立路径（手工拼接 + 直接 hashlib）再算一遍，杜绝基准值本身写错
    manual = hashlib.md5(raw.encode("utf-8")).hexdigest()
    check("手工 hashlib 复算一致", manual == GOLDEN_SIGN, f"got {manual}")

    check("签名是 32 位小写 hex", len(actual) == 32 and all(c in "0123456789abcdef" for c in actual))

    # 分隔符格式断言：token&t&appKey&data
    check("APP_KEY 常量为 34839810", APP_KEY == "34839810", f"got {APP_KEY}")


def test_extract_token() -> None:
    section("1b. token 提取")

    check("正常 cookie 取值", extract_token("abc123_1699999999999") == "abc123")
    check("无下划线时原样返回", extract_token("abc123") == "abc123")
    check("空字符串返回空", extract_token("") == "")
    check("None 返回空", extract_token(None) == "")  # type: ignore[arg-type]
    check("多个下划线只取第一段", extract_token("a_b_c_123") == "a")


def test_compact_json() -> None:
    section("1c. 紧凑序列化")

    params = {"itemId": "123456", "page": 1}
    compact = compact_json(params)
    print(f"  compact_json -> {compact}")
    print(f"  json.dumps   -> {json.dumps(params)}")

    check("无空格", " " not in compact, f"got {compact}")
    check("紧凑结果与手工拼接一致", compact == '{"itemId":"123456","page":1}', f"got {compact}")
    check("compact_json 与 json.dumps 默认输出不同", compact != json.dumps(params))

    # 嵌套结构也要紧凑
    nested = compact_json({"a": {"b": [1, 2]}})
    check("嵌套结构无空格", nested == '{"a":{"b":[1,2]}}', f"got {nested}")


def test_sign_params() -> None:
    section("1d. sign_params 组装")

    token = "testtoken"
    t_ms = 1700000000000
    params = {"itemId": "123456"}
    signed = sign_params(params, token, t_ms)

    print(f"  sign_params -> {signed}")

    check("包含 sign", "sign" in signed)
    check("包含 t", signed.get("t") == t_ms, f"got {signed.get('t')}")
    check("包含 appKey", signed.get("appKey") == APP_KEY)
    check("保留原始业务参数", signed.get("itemId") == "123456")
    check(
        "sign 与 build_sign(紧凑 data) 一致",
        signed.get("sign") == build_sign(token, t_ms, compact_json(params)),
        f"got {signed.get('sign')}",
    )
    check("不修改入参（纯函数）", params == {"itemId": "123456"}, f"got {params}")
    check("返回新对象", signed is not params)


# ---------------------------------------------------------------- 2. 两个坑

def test_pitfall_timestamp() -> None:
    section("2a. 坑一：int(time.time()) * 1000 会让末三位恒为 0")

    # 冻结时钟到一个带小数的时刻，模拟真实情况（time.time() 几乎总是带小数）
    frozen = 1700000000.123456

    with patch("time.time", return_value=frozen):
        wrong_t = int(time.time()) * 1000
        right_t = int(time.time() * 1000)

    print(f"  冻结时钟          : {frozen}")
    print(f"  错误 int(t)*1000  : {wrong_t}  (末三位={wrong_t % 1000:03d})")
    print(f"  正确 int(t*1000)  : {right_t}  (末三位={right_t % 1000:03d})")

    check("两种写法产生不同时间戳", wrong_t != right_t, f"{wrong_t} vs {right_t}")
    check("错误写法的末三位恒为 0", wrong_t % 1000 == 0, f"got {wrong_t % 1000}")
    check("正确写法保留了毫秒精度", right_t % 1000 == 123, f"got {right_t % 1000}")

    wrong_sign = build_sign(GOLDEN_TOKEN, wrong_t, GOLDEN_DATA)
    right_sign = build_sign(GOLDEN_TOKEN, right_t, GOLDEN_DATA)
    print(f"  错误写法签名: {wrong_sign}")
    print(f"  正确写法签名: {right_sign}")
    check("两种时间戳导致签名不同", wrong_sign != right_sign)

    # 多组小数样本，证明错误写法恒定吞掉毫秒
    all_zero = True
    for frac in (0.000001, 0.25, 0.5, 0.75, 0.999):
        sample = 1700000000 + frac
        with patch("time.time", return_value=sample):
            all_zero = all_zero and (int(time.time()) * 1000) % 1000 == 0
    check("多种小数输入下错误写法末三位恒为 0", all_zero)

    # now_ms 必须走正确路径
    with patch("time.time", return_value=frozen):
        observed = now_ms()
    check("now_ms 保留毫秒精度", observed == right_t, f"got {observed}")


def test_pitfall_json_spacing() -> None:
    section("2b. 坑二：json.dumps 默认带空格会签错")

    params = {"itemId": "123456", "keyword": "test"}
    spaced = json.dumps(params)
    compact = compact_json(params)

    print(f"  带空格 data: {spaced}")
    print(f"  紧凑   data: {compact}")

    check("默认 json.dumps 确实含空格", " " in spaced, f"got {spaced}")
    check("两者不相等", spaced != compact)

    t_ms = 1700000000000
    spaced_sign = build_sign(GOLDEN_TOKEN, t_ms, spaced)
    compact_sign = build_sign(GOLDEN_TOKEN, t_ms, compact)
    print(f"  带空格签名: {spaced_sign}")
    print(f"  紧凑  签名: {compact_sign}")
    check("带空格与不带空格产生不同签名", spaced_sign != compact_sign)

    # 证明 sign_params 走的是紧凑路径
    signed = sign_params(params, GOLDEN_TOKEN, t_ms)
    check(
        "sign_params 使用紧凑序列化",
        signed["sign"] == compact_sign,
        f"got {signed['sign']}",
    )
    check("sign_params 的 sign 不等于带空格版本", signed["sign"] != spaced_sign)


# ---------------------------------------------------------------- 3. 状态判定

def test_judge_status() -> None:
    section("3. 商品状态判定（保守原则）")

    # 3.1 明确存活
    alive_payload = {"api": "mtop.taobao.idle.pc.detail", "ret": ["SUCCESS::调用成功"]}
    st = judge_status("111", alive_payload)
    print(f"  SUCCESS       -> alive={st.alive} reason={st.reason}")
    check("SUCCESS 判活", st.alive is True)
    check("SUCCESS 记录 raw_ret", st.raw_ret == ["SUCCESS::调用成功"])
    check("SUCCESS 不填 error", st.error is None, f"got {st.error}")

    # 3.2 明确死亡
    dead_ret = "FAIL_BIZ_ITEM_DEL_NOT_FOUND::您要看的宝贝不存在或已被删除啦!"
    dead_payload = {"ret": [dead_ret]}
    st = judge_status("222", dead_payload)
    print(f"  删除标记      -> alive={st.alive} reason={st.reason}")
    check("死亡标记判死", st.alive is False)
    check("死亡原因文案正确", st.reason == "已删除或不存在", f"got {st.reason}")
    check("死亡也保留 raw_ret", st.raw_ret == [dead_ret])

    # 3.3 风控 -> 保守判活 + error 有值
    risk_payload = {"ret": ["RGV587_ERROR::请稍后再试"]}
    st = judge_status("333", risk_payload)
    print(f"  RGV587        -> alive={st.alive} error={st.error}")
    check("风控保守判活", st.alive is True)
    check("风控写入 error", bool(st.error), f"got {st.error!r}")
    check("风控 error 含原始 ret", st.error is not None and "RGV587_ERROR" in st.error)

    # 3.4 异常 + payload None -> 保守判活
    exc = TimeoutError("connection timed out")
    st = judge_status("444", None, error=exc)
    print(f"  异常+None     -> alive={st.alive} error={st.error}")
    check("异常保守判活", st.alive is True)
    check("异常写入 error", st.error is not None and "TimeoutError" in st.error)
    check("无 payload 时 raw_ret 为空", st.raw_ret == [])

    # 3.5 空 dict -> 保守判活
    st = judge_status("555", {})
    print(f"  空 dict       -> alive={st.alive} reason={st.reason}")
    check("空 dict 保守判活", st.alive is True)
    check("空 dict 记录原因", "ret" in st.reason)
    check("空 dict error 提示缺 ret", st.error is not None and "ret" in st.error)

    # 3.6 非法结构 -> 保守判活
    st = judge_status("666", {"foo": "bar"})
    print(f"  非法结构      -> alive={st.alive} reason={st.reason}")
    check("非法结构保守判活", st.alive is True)
    check("非法结构有 error", bool(st.error))

    # 3.7 payload 不是 dict（JSON 解析失败的典型表现）
    st = judge_status("667", "not a json")  # type: ignore[arg-type]
    check("payload 为字符串保守判活", st.alive is True)
    check("payload 类型异常写入 error", st.error is not None and "str" in st.error)

    # 3.8 ret 为空列表
    st = judge_status("777", {"ret": []})
    check("ret 为空列表保守判活", st.alive is True)

    # 3.9 ret 为字符串（对端异常形态）
    st = judge_status("888", {"ret": "SUCCESS::调用成功"})
    check("ret 为字符串也能识别存活", st.alive is True and st.raw_ret == ["SUCCESS::调用成功"])

    # 3.10 死亡标记优先于 SUCCESS（多 ret 混合时采信死亡）
    st = judge_status("999", {"ret": [dead_ret, "SUCCESS::调用成功"]})
    check("死亡标记优先级高于 SUCCESS", st.alive is False)

    # 3.11 辅助字段提取（不参与判定）
    rich = {
        "ret": ["SUCCESS::调用成功"],
        "data": {
            "itemDO": {
                "itemStatus": 1,
                "itemStatusStr": "在卖",
                "soldCnt": 3,
                "quantity": 1,
                "browseCnt": 120,
                "collectCnt": 8,
                "wantCnt": 5,
            }
        },
    }
    st = judge_status("1010", rich)
    check("提取 item_status_code", st.item_status_code == 1, f"got {st.item_status_code}")
    check("提取 item_status_str", st.item_status_str == "在卖", f"got {st.item_status_str}")

    # itemStatus 语义存疑：无论取值如何都不影响 alive
    for code in (0, 1, 2, 99, -1):
        st_zero = judge_status(
            "1011",
            {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {"itemStatus": code}}},
        )
        if st_zero.alive is not True:
            check(f"itemStatus={code} 不影响判定", False, "alive 被改变了")
            break
    else:
        check("任意 itemStatus 取值都不影响 alive（语义不确定的隔离）", True)

    # 极端：itemStatus 缺失 / 类型异常也不崩
    st = judge_status("1012", {"ret": ["SUCCESS::调用成功"], "data": {"itemDO": {"itemStatus": "abc"}}})
    check("itemStatus 非法类型降级为 None", st.item_status_code is None and st.alive is True)

    st = judge_status("1013", {"ret": ["SUCCESS::调用成功"], "data": "oops"})
    check("data 非 dict 不崩", st.alive is True and st.item_status_code is None)

    # 3.12 异常与 payload 同时给出时异常优先
    st = judge_status("1014", alive_payload, error=ValueError("boom"))
    check("异常优先于 payload", st.alive is True and st.error is not None and "ValueError" in st.error)

    # 3.13 DEAD_MARKERS 常量可扩展且非空
    check("DEAD_MARKERS 是列表且非空", isinstance(DEAD_MARKERS, list) and len(DEAD_MARKERS) > 0)
    check("DEAD_MARKERS 含已知删除标记", "FAIL_BIZ_ITEM_DEL_NOT_FOUND" in DEAD_MARKERS)

    # 3.14 ItemStatus 是 dataclass，字段齐全
    st = judge_status("1015", alive_payload)
    for field_name in (
        "item_id",
        "alive",
        "reason",
        "raw_ret",
        "item_status_code",
        "item_status_str",
        "error",
    ):
        if not hasattr(st, field_name):
            check(f"ItemStatus 含字段 {field_name}", False, "字段缺失")
            break
    else:
        check("ItemStatus 字段齐全", True)
    check("ItemStatus.item_id 回传正确", st.item_id == "1015")


# ---------------------------------------------------------------- 4. 错误分类

def test_classify_error() -> None:
    section("4. 错误分类")

    cases: list[tuple[str, object, str]] = [
        ("FAIL_SYS_TOKEN_EMPTY", ["FAIL_SYS_TOKEN_EMPTY::token为空"], "recoverable"),
        ("FAIL_SYS_TOKEN_ILLEGAL", ["FAIL_SYS_TOKEN_ILLEGAL::token非法"], "recoverable"),
        ("FAIL_SYS_SESSION_EXPIRED", ["FAIL_SYS_SESSION_EXPIRED::session过期"], "recoverable"),
        # 闲鱼真实返回的少 P 拼写
        ("FAIL_SYS_TOKEN_EXOIRED(少P)", ["FAIL_SYS_TOKEN_EXOIRED::token过期"], "recoverable"),
        # 拼写正确的版本也要兼容
        ("FAIL_SYS_TOKEN_EXPIRED(正确)", ["FAIL_SYS_TOKEN_EXPIRED::token过期"], "recoverable"),
        ("RGV587_ERROR", ["RGV587_ERROR::请稍后再试"], "risk_control"),
        ("FAIL_SYS_USER_VALIDATE", ["FAIL_SYS_USER_VALIDATE::需要验证"], "risk_control"),
        ("/punish 风控页", ["FAIL_SYS_ILLEGAL_ACCESS::/punish"], "fatal"),
        ("FAIL_SYS_ILLEGAL_ACCESS", ["FAIL_SYS_ILLEGAL_ACCESS::非法访问"], "fatal"),
        ("SUCCESS 无分类", ["SUCCESS::调用成功"], "unknown"),
        ("完全未知码", ["SOME_UNKNOWN_CODE::???"], "unknown"),
    ]

    for label, ret, expected in cases:
        got = classify_error(ret)  # type: ignore[arg-type]
        print(f"  {label:<30} -> {got}")
        check(f"{label} -> {expected}", got == expected, f"got {got}")

    # 字符串入参
    check("字符串入参 RGV587", classify_error("RGV587_ERROR::x") == "risk_control")
    check(
        "字符串入参 TOKEN_EXOIRED",
        classify_error("FAIL_SYS_TOKEN_EXOIRED::x") == "recoverable",
    )

    # 空值
    check("空列表 -> unknown", classify_error([]) == "unknown")
    check("空字符串 -> unknown", classify_error("") == "unknown")

    # 多 ret 时 fatal 优先
    check(
        "fatal 优先级最高",
        classify_error(["FAIL_SYS_TOKEN_EMPTY::x", "FAIL_SYS_ILLEGAL_ACCESS::y"]) == "fatal",
    )
    check(
        "risk_control 优先于 recoverable",
        classify_error(["FAIL_SYS_TOKEN_EMPTY::x", "RGV587_ERROR::y"]) == "risk_control",
    )

    # is_retryable 辅助
    check("recoverable 可重试", is_retryable(["FAIL_SYS_TOKEN_EMPTY::x"]) is True)
    check("fatal 不可重试", is_retryable(["FAIL_SYS_ILLEGAL_ACCESS::x"]) is False)
    check("risk_control 视为可重试（需退避）", is_retryable(["RGV587_ERROR::x"]) is True)

    # extract_ret 归一化
    check("extract_ret 取列表", extract_ret({"ret": ["A", "B"]}) == ["A", "B"])
    check("extract_ret 字符串转列表", extract_ret({"ret": "A"}) == ["A"])
    check("extract_ret 缺字段返回空", extract_ret({}) == [])
    check("extract_ret 非 dict 返回空", extract_ret(None) == [])
    check("extract_ret 元素转字符串", extract_ret({"ret": [1, 2]}) == ["1", "2"])


# ---------------------------------------------------------------- 5. 纯函数性

def test_purity() -> None:
    section("5. 纯函数性与确定性")

    # 相同输入必须永远得到相同输出
    first = build_sign(GOLDEN_TOKEN, GOLDEN_T_MS, GOLDEN_DATA)
    for _ in range(50):
        if build_sign(GOLDEN_TOKEN, GOLDEN_T_MS, GOLDEN_DATA) != first:
            check("build_sign 幂等", False, "输出不稳定")
            break
    else:
        check("build_sign 幂等（50 次重复一致）", True)

    payload = {"ret": ["SUCCESS::调用成功"]}
    snapshot = json.dumps(payload, sort_keys=True)
    judge_status("id", payload)
    check("judge_status 不修改 payload", json.dumps(payload, sort_keys=True) == snapshot)

    # sign_params 不写入全局状态：连续调用只随 t_ms 变化
    a = sign_params({"x": 1}, "tk", 1000)
    b = sign_params({"x": 1}, "tk", 1000)
    check("sign_params 同参同结果", a == b)

    # 模块内不发网络请求：扫描本包源码，确认没有任何网络库 import
    pkg_dir = os.path.join(_PROJECT_ROOT, "src", "services", "xy_protocol")
    forbidden = ("httpx", "requests", "urllib", "socket", "aiohttp")
    import_re = re.compile(
        r"^\s*(?:import|from)\s+(" + "|".join(forbidden) + r")\b", re.MULTILINE
    )
    offenders: list[str] = []
    for filename in sorted(os.listdir(pkg_dir)):
        if not filename.endswith(".py"):
            continue
        with open(os.path.join(pkg_dir, filename), "r", encoding="utf-8") as handle:
            source = handle.read()
        # 先剥掉文档字符串与注释行，避免文档里提到的库名造成误报
        code_only = "\n".join(
            line for line in source.splitlines() if not line.lstrip().startswith("#")
        )
        code_only = re.sub(r'""".*?"""', "", code_only, flags=re.DOTALL)
        code_only = re.sub(r"'''.*?'''", "", code_only, flags=re.DOTALL)
        for match in import_re.finditer(code_only):
            offenders.append(f"{filename}:{match.group(1)}")
    check("xy_protocol 包内无任何网络库 import", not offenders, f"发现 {offenders}")


# ---------------------------------------------------------------- main

def main() -> int:
    print("闲鱼协议模块离线自检（不发任何网络请求）")
    print(f"Python {sys.version.split()[0]}  |  项目根 {_PROJECT_ROOT}")

    test_sign_golden()
    test_extract_token()
    test_compact_json()
    test_sign_params()
    test_pitfall_timestamp()
    test_pitfall_json_spacing()
    test_judge_status()
    test_classify_error()
    test_purity()

    total = _PASSED + len(_FAILED)
    print("\n" + "=" * 56)
    print(f"通过 {_PASSED}/{total}")
    if _FAILED:
        print(f"失败 {len(_FAILED)} 项：")
        for name in _FAILED:
            print(f"  - {name}")
        print("结果: FAILED")
        return 1

    print("结果: ALL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
