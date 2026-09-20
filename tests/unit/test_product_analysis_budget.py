"""商品分析的输出预算必须给推理模型留足空间。

推理模型的思考 token 与正文共享 ``max_output_tokens``。原值 4000 对
``global:deepseek-v4.1-flash`` 不够用，真机统计：189 次商品分析里 91 次
``output_tokens >= 3999``（撞顶率 48.1%），JSON 被截断，契约校验报
「响应缺少必需字段 'prompt_version'」，只能靠重试硬扛。

这些测试锁住"预算不得低于推理模型所需下限"这一约束，防止有人把它改回去。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from src import ai_handler

REPO_ROOT = Path(__file__).resolve().parents[2]
HANDLER_PATH = REPO_ROOT / "src" / "ai_handler.py"

#: 实测得出的推理模型下限。8192 可以覆盖思考 + 完整 JSON 正文。
MIN_SAFE_BUDGET = 8192


class TestProductAnalysisBudget:
    def test_budget_is_safe_for_reasoning_models(self):
        assert ai_handler.PRODUCT_ANALYSIS_MAX_TOKENS >= MIN_SAFE_BUDGET

    def test_budget_constant_is_documented(self):
        """这个值有实测依据，必须留下注释说明来源，不能被随手改掉。"""
        source = inspect.getsource(ai_handler)
        idx = source.find("PRODUCT_ANALYSIS_MAX_TOKENS")
        assert idx > 0
        preceding = source[max(0, idx - 900):idx]
        assert "撞顶" in preceding or "截断" in preceding


class TestNoHardcodedBudgetInRequestCall:
    """请求参数里不能再出现硬编码的小预算。"""

    def test_build_request_uses_the_constant(self):
        source = HANDLER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)

        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "build_ai_request_params"
        ]
        assert calls, "未找到 build_ai_request_params 调用点"

        budgets = []
        for call in calls:
            for kw in call.keywords:
                if kw.arg == "max_output_tokens":
                    budgets.append(kw.value)

        assert budgets, "调用点未设置 max_output_tokens"

        for value in budgets:
            # 必须是常量引用（Name），不能是字面量数字
            assert isinstance(value, ast.Name), (
                "max_output_tokens 不应使用字面量，应引用 "
                "PRODUCT_ANALYSIS_MAX_TOKENS"
            )
            assert value.id == "PRODUCT_ANALYSIS_MAX_TOKENS"

    def test_no_small_literal_budget_survives(self):
        """4000 这类旧值不得以字面量形式留在请求构造里。"""
        source = HANDLER_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "max_output_tokens":
                    assert not isinstance(kw.value, ast.Constant), (
                        "发现硬编码的 max_output_tokens 字面量"
                    )


class TestTruncationGuardStillPresent:
    """解析侧的截断护栏不能被绕过。"""

    def test_parser_rejects_length_truncation(self):
        from src.services import ai_response_parser

        source = inspect.getsource(ai_response_parser)
        assert "finish_reason" in source
        assert "length" in source

    def test_guard_raises_on_truncated_payload(self):
        from types import SimpleNamespace

        from src.services.ai_response_parser import (
            EmptyAIResponseError,
            extract_ai_response_content,
        )

        choice = SimpleNamespace(
            finish_reason="length",
            message=SimpleNamespace(content="", reasoning_content="思考中"),
        )
        with pytest.raises(EmptyAIResponseError):
            extract_ai_response_content(SimpleNamespace(choices=[choice]))
