"""``extract_ai_response_content`` 对推理模型的截断防护。

背景（真机实测，DeepSeek v4.1-flash）：

同一条提示词下，预算不足时 content 为空、``finish_reason=length``，
而 ``reasoning_content`` 里躺着**半截思考**。原先的实现在 content 为空时
无条件回退到 ``reasoning_content``（那是为智谱「思考即正文」设计的），
结果是这份思考被当作正文写进 ``prompts/*_criteria.txt``：

    "我们需要回答用户。用户要求：作为世界级 AI 提示词工程大师……"

AI 分析随后就照这段思维独白执行，且全程不报错。这类「静默写错内容」
比直接失败危险得多，因此在这里把边界钉死。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.services.ai_response_parser import (
    EmptyAIResponseError,
    extract_ai_response_content,
)


def _response(*, content, reasoning=None, finish_reason="stop"):
    message = SimpleNamespace(content=content)
    if reasoning is not None:
        message.reasoning_content = reasoning
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)]
    )


class TestTruncatedResponseIsRejected:
    """截断不得被当成「网关把答案放在 reasoning 里」。"""

    def test_length_truncation_with_reasoning_is_an_error(self):
        resp = _response(content="", reasoning="我们需要回答用户。用户要求：……",
                         finish_reason="length")
        with pytest.raises(EmptyAIResponseError) as exc:
            extract_ai_response_content(resp)
        assert "截断" in str(exc.value)

    def test_length_truncation_without_reasoning_is_an_error(self):
        resp = _response(content="", reasoning=None, finish_reason="length")
        with pytest.raises(EmptyAIResponseError):
            extract_ai_response_content(resp)

    def test_whitespace_content_counts_as_empty(self):
        resp = _response(content="   ", reasoning="思考", finish_reason="length")
        with pytest.raises(EmptyAIResponseError):
            extract_ai_response_content(resp)

    def test_error_message_mentions_the_remedy(self):
        """报错要说清怎么办，否则排查者只会看到「响应为空」。"""
        resp = _response(content="", reasoning="x", finish_reason="length")
        with pytest.raises(EmptyAIResponseError) as exc:
            extract_ai_response_content(resp)
        assert "max_output_tokens" in str(exc.value)


class TestGatewayFallbackStillWorks:
    """智谱那类「输出放 reasoning_content」的网关不能被这次修复打破。"""

    def test_falls_back_when_not_truncated(self):
        resp = _response(content="", reasoning="网关放在 reasoning 里的答案",
                         finish_reason="stop")
        assert extract_ai_response_content(resp) == "网关放在 reasoning 里的答案"

    def test_falls_back_when_finish_reason_absent(self):
        resp = _response(content="", reasoning="没有 finish_reason 的答案",
                         finish_reason=None)
        assert extract_ai_response_content(resp) == "没有 finish_reason 的答案"


class TestNormalPathUnaffected:
    def test_content_is_returned_directly(self):
        resp = _response(content="正常正文", finish_reason="stop")
        assert extract_ai_response_content(resp) == "正常正文"

    def test_content_wins_over_reasoning(self):
        """两者都有时以 content 为准——思考不是答案。"""
        resp = _response(content="正文", reasoning="思考过程", finish_reason="stop")
        assert extract_ai_response_content(resp) == "正文"
