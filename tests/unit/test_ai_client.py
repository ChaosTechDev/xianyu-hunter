import asyncio
import os
from types import SimpleNamespace

import pytest

from src.infrastructure.external.ai_client import AIClient, _sanitize_no_proxy_env
from src.services.ai_request_compat import build_responses_input


def _build_fake_client(responses_create_impl, chat_create_impl=None):
    responses = SimpleNamespace(create=responses_create_impl)
    chat = SimpleNamespace(
        completions=SimpleNamespace(create=chat_create_impl or responses_create_impl)
    )
    return SimpleNamespace(responses=responses, chat=chat)


def test_build_messages_without_images_uses_text_only_content():
    """无图片时应产出纯文本 content。

    契约说明：本项目的 ``_build_messages`` 返回 ``[system, user]`` 两条消息
    （上游为单条 user）。断言因此取最后一条（承载商品数据的 user 消息），
    测试意图不变：无图片时 content 必须是字符串而非多模态列表。
    """
    client = AIClient.__new__(AIClient)

    messages = client._build_messages(
        {"商品信息": {"商品标题": "MacBook Pro M2"}, "卖家信息": {"卖家信用等级": "优秀"}},
        [],
        "只分析文字描述和卖家资质。",
    )

    assert [m["role"] for m in messages] == ["system", "user"]
    content = messages[-1]["content"]
    assert isinstance(content, str)
    assert "MacBook Pro M2" in content
    assert "未提供商品图片" in content


def test_build_messages_with_images_uses_multimodal_content(monkeypatch):
    """有图片时应产出多模态 content（契约同上，取 user 消息）。"""
    client = AIClient.__new__(AIClient)
    # 显式开启图片模式，避免 auto 模式按模型名降级为纯文本
    client.settings = SimpleNamespace(image_mode="on", base_url="", model_name="")
    monkeypatch.setattr(AIClient, "encode_image", staticmethod(lambda _path: "ZmFrZQ=="))

    messages = client._build_messages(
        {"商品信息": {"商品标题": "MacBook Pro M2"}},
        ["fake-image.jpg"],
        "结合图片和文字综合判断。",
    )

    content = messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "image_url"
    assert content[-1]["type"] == "text"


def test_build_responses_input_converts_multimodal_messages():
    result = build_responses_input(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,ZmFrZQ=="}},
                    {"type": "text", "text": "hello"},
                ],
            }
        ]
    )

    assert result == [
        {
            "role": "user",
            "content": [
                {
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64,ZmFrZQ==",
                    "detail": "auto",
                },
                {"type": "input_text", "text": "hello"},
            ],
        }
    ]


def test_call_ai_retries_without_structured_output_when_model_rejects_it():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=True,
        enable_thinking=False,
    )
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            raise Exception(
                "Error code: 400 - {'error': {'code': 'InvalidParameter', "
                "'message': 'The parameter `response_format.type` specified in "
                "the request are not valid: `json_object` is not supported by "
                "this model.', 'param': 'response_format.type'}}"
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok":true}')
                )
            ]
        )

    client.client = _build_fake_client(fake_create)

    response = asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert response == '{"ok":true}'
    assert request_history[0]["messages"][0]["content"] == "hi"
    assert request_history[0]["response_format"]["type"] == "json_object"
    assert "response_format" not in request_history[1]


def test_call_ai_falls_back_to_responses_when_chat_completions_api_is_missing():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=True,
        enable_thinking=False,
    )
    request_history = []

    async def fake_chat_create(**kwargs):
        request_history.append(("chat", kwargs))
        raise Exception("Error code: 404 - page not found")

    async def fake_responses_create(**kwargs):
        request_history.append(("responses", kwargs))
        if len([item for item in request_history if item[0] == "responses"]) == 1:
            raise Exception(
                "Error code: 400 - {'error': {'code': 'InvalidParameter', "
                "'message': 'The parameter `text.format.type` specified in "
                "the request are not valid: `json_object` is not supported by "
                "this model.', 'param': 'text.format.type'}}"
            )
        return SimpleNamespace(output_text='{"ok":true}')

    client.client = _build_fake_client(fake_responses_create, fake_chat_create)

    response = asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert response == '{"ok":true}'
    assert request_history[0][0] == "chat"
    assert request_history[1][0] == "responses"
    assert request_history[1][1]["text"]["format"]["type"] == "json_object"
    assert request_history[2][0] == "responses"
    assert "text" not in request_history[2][1]


def test_call_ai_retries_without_temperature_when_gateway_rejects_it():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=False,
        enable_thinking=False,
    )
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) == 1:
            raise Exception("temperature is not supported by this gateway")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"ok":true}')
                )
            ]
        )

    client.client = _build_fake_client(fake_create)

    response = asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert response == '{"ok":true}'
    assert request_history[0]["temperature"] == 0.1
    assert "temperature" not in request_history[1]


def test_call_ai_retries_when_response_content_is_empty():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=False,
        enable_thinking=False,
    )
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        if len(request_history) < 4:
            return SimpleNamespace(output_text="")
        return SimpleNamespace(output_text='{"ok":true}')

    client.client = _build_fake_client(fake_create)

    response = asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert response == '{"ok":true}'
    assert len(request_history) == 4


def test_call_ai_raises_after_all_empty_response_retries_are_exhausted():
    client = AIClient.__new__(AIClient)
    client.settings = SimpleNamespace(
        model_name="fake-model",
        enable_response_format=False,
        enable_thinking=False,
    )
    request_history = []

    async def fake_create(**kwargs):
        request_history.append(kwargs)
        return SimpleNamespace(output_text="")

    client.client = _build_fake_client(fake_create)

    with pytest.raises(ValueError, match="AI响应内容为空"):
        asyncio.run(client._call_ai([{"role": "user", "content": "hi"}]))

    assert len(request_history) == 4


def test_close_closes_underlying_async_client_and_clears_reference():
    client = AIClient.__new__(AIClient)
    close_state = {"closed": False}

    async def fake_close():
        close_state["closed"] = True

    client.client = SimpleNamespace(close=fake_close)

    asyncio.run(client.close())

    assert close_state["closed"] is True
    assert client.client is None


def test_parse_response_uses_first_json_object_when_response_contains_multiple_objects():
    client = AIClient.__new__(AIClient)

    result = client._parse_response("""```json
{"ok": true, "reason": "first"}
{"ok": false, "reason": "second"}
```""")

    assert result == {"ok": True, "reason": "first"}


# -- _sanitize_no_proxy_env tests --


def test_sanitize_no_proxy_strips_ipv6_cidr(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.0/8,::1/128")
    _sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.0/8,::1"


def test_sanitize_no_proxy_strips_lowercase_variant(monkeypatch):
    monkeypatch.setenv("no_proxy", "localhost,::1/128,fe80::1/64")
    _sanitize_no_proxy_env()
    assert os.environ["no_proxy"] == "localhost,::1,fe80::1"


def test_sanitize_no_proxy_preserves_ipv4_cidr(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "10.0.0.0/8,192.168.0.0/16")
    _sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "10.0.0.0/8,192.168.0.0/16"


def test_sanitize_no_proxy_noop_without_env(monkeypatch):
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    _sanitize_no_proxy_env()


def test_sanitize_no_proxy_handles_both_keys(monkeypatch):
    """同时设置大小写两个键时应都能被清理。

    平台说明：Windows 的环境变量名不区分大小写，``NO_PROXY`` 与 ``no_proxy``
    在 ``os.environ`` 中是同一个变量（后写覆盖先写），因此无法在本平台构造
    「两个键各自独立取值」的场景。此测试在非 Windows 平台保留完整断言，
    在 Windows 上退化为验证「清理逻辑对当前生效值有效」。
    """
    if os.name == "nt":
        # Windows：两个键实为同一个，验证清理逻辑本身生效即可
        monkeypatch.setenv("NO_PROXY", "::1/128")
        _sanitize_no_proxy_env()
        assert os.environ["NO_PROXY"] == "::1"
        return

    monkeypatch.setenv("NO_PROXY", "::1/128")
    monkeypatch.setenv("no_proxy", "fe80::1/10")
    _sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "::1"
    assert os.environ["no_proxy"] == "fe80::1"
