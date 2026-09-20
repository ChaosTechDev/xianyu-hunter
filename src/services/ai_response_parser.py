"""
AI 响应解析工具
"""
import json
from typing import Any


class EmptyAIResponseError(ValueError):
    """AI 返回了空内容。"""


def extract_ai_response_content(response: Any) -> str:
    """从不同形态的 AI 响应中提取文本内容。"""
    if response is None:
        raise EmptyAIResponseError("AI响应对象为空。")

    if isinstance(response, (bytes, bytearray)):
        text = response.decode("utf-8", errors="replace")
        return _normalize_text_content(text)

    if isinstance(response, str):
        return _normalize_text_content(response)

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str):
        return _normalize_text_content(output_text)

    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is None:
            raise EmptyAIResponseError("AI响应缺少 message。")
        content = getattr(message, "content", None)

        try:
            return _normalize_text_content(_coerce_content_parts(content))
        except EmptyAIResponseError:
            # content 为空时才考虑 reasoning_content 回退，但**必须先确认不是
            # 被长度截断**。
            #
            # 这个回退原本是为「智谱等网关把输出放在 reasoning_content」准备的
            # （思考即正文）。但推理模型（DeepSeek v4.1-flash 等）是「思考与
            # 正文分离」：思考计入 max_tokens，思考写满后正文还没开始，
            # 此时 content 为空而 reasoning_content 是**半截思考过程**。
            # 实测让它回退，写进 prompts/*_criteria.txt 的会是
            # 「我们需要回答用户。用户要求：……」这类思维独白，AI 分析随后就
            # 照这段垃圾执行，且全程不报错——静默失效，最难排查。
            #
            # finish_reason == "length" 表示「被预算截断」，这种空 content
            # 是预算不足而非网关特性，必须报出来触发重试/上抛，
            # 绝不能拿思考当答案。
            finish_reason = getattr(choices[0], "finish_reason", None)
            if finish_reason == "length":
                raise EmptyAIResponseError(
                    "AI 输出被 max_tokens 截断（finish_reason=length），正文为空。"
                    "推理模型会先消耗 token 进行思考，需提高 max_output_tokens。"
                ) from None

            reasoning_content = getattr(message, "reasoning_content", None)
            if reasoning_content:
                return _normalize_text_content(_coerce_content_parts(reasoning_content))
            raise

    raise ValueError(f"无法识别的AI响应类型: {type(response).__name__}")


def parse_ai_response_json(content: str) -> dict:
    """解析 AI 文本响应中的 JSON。"""
    cleaned = _strip_code_fences(content)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return _extract_first_json_value(cleaned, exc)


def _coerce_content_parts(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (bytes, bytearray)):
        return content.decode("utf-8", errors="replace")
    if not isinstance(content, list):
        raise ValueError(f"AI响应内容类型不受支持: {type(content).__name__}")

    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
            continue
        text = getattr(item, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def _normalize_text_content(content: str) -> str:
    text = str(content).strip()
    if not text:
        raise EmptyAIResponseError("AI响应内容为空。")
    return text


def _strip_code_fences(content: str) -> str:
    cleaned = content.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


def _extract_first_json_value(
    content: str,
    fallback_error: json.JSONDecodeError,
):
    decoder = json.JSONDecoder()
    last_error: json.JSONDecodeError | None = None

    for start_index, char in enumerate(content):
        if char not in "{[":
            continue
        try:
            parsed, _ = decoder.raw_decode(content[start_index:])
            return parsed
        except json.JSONDecodeError as exc:
            last_error = exc

    if last_error is not None:
        raise last_error
    raise fallback_error
