"""
AI 客户端封装
提供统一的 AI 调用接口
"""
import ipaddress
import os
import json
import time
import base64
import hashlib
from typing import Dict, List, Optional
from datetime import datetime
from dotenv import load_dotenv
from openai import AsyncOpenAI
from src.ai_message_builder import (
    build_analysis_text_prompt,
    build_user_message_content,
    is_text_only_model,
)
from src.infrastructure.config.settings import AISettings
from src.infrastructure.config.env_manager import env_manager
from src.services.ai_request_compat import (
    CHAT_COMPLETIONS_API_MODE,
    RESPONSES_API_MODE,
    build_ai_request_params,
    create_ai_response_async,
    is_chat_completions_api_unsupported_error,
    is_json_output_unsupported_error,
    is_responses_api_unsupported_error,
    is_temperature_unsupported_error,
    remove_temperature_param,
)
from src.services.ai_response_parser import (
    EmptyAIResponseError,
    extract_ai_response_content,
    parse_ai_response_json,
)
from src.services.ai_usage_service import record_ai_usage


def _shared_ai_gate():
    """按配置构造跨任务共享的并发闸。

    默认上限取自 ``AI_MAX_CONCURRENCY``（默认 4）。多个采集任务会各自构造
    ``AIClient``，但它们共享同一个闸对象，因此上限是**全局**的——这正是
    「限制对上游 API 的并发压力」所需的语义。
    """
    from src.services.ai_governance_service import AIRequestGate

    try:
        limit = int(os.getenv("AI_MAX_CONCURRENCY", "4"))
    except (TypeError, ValueError):
        limit = 4
    return AIRequestGate(limit)


def _shared_ai_cache():
    """按配置构造跨任务共享的结果缓存。

    默认 TTL ``AI_CACHE_TTL_SECONDS``（900 秒），条目上限 ``AI_CACHE_MAX_ENTRIES``（500）。
    设为 ``0`` 天/TTL 可以关掉缓存语义（此时条目立即过期，等价于不缓存）。

    与并发闸一样是**全局共享**的：同一个商品被多个任务命中时，第二个任务直接拿缓存，
    不必再付一次 token 费用。
    """
    from src.services.ai_governance_service import AIResultCache

    try:
        ttl = float(os.getenv("AI_CACHE_TTL_SECONDS", "900"))
    except (TypeError, ValueError):
        ttl = 900.0
    try:
        max_entries = int(os.getenv("AI_CACHE_MAX_ENTRIES", "500"))
    except (TypeError, ValueError):
        max_entries = 500
    return AIResultCache(ttl_seconds=ttl, max_entries=max_entries)


_SHARED_AI_GATE = _shared_ai_gate()
_SHARED_AI_CACHE = _shared_ai_cache()

#: 缓存键里代表「本次不发送 temperature 参数」的哨兵值。
#: 不能复用 0.0，因为「不发参数」与「发 0.0」是两种不同的请求。
_TEMPERATURE_UNSUPPORTED = "unsupported"


class AIBudgetExceededError(RuntimeError):
    """AI 预算已超限，本次调用被拦下。

    单独定义异常类型（而不是复用 RuntimeError）让调用方能区分「预算拦下」与
    「上游技术故障」：前者需要人工调整预算或等待次日，后者重试可能有用。
    """

#: 预算已花费金额的缓存，避免每次 AI 调用都查一次聚合 SQL。
#:
#: ``at`` 用 ``None`` 表示「尚未取过」而不是 ``0.0``：这里的时间戳来自
#: ``time.monotonic()``，其起点是进程启动（Linux 上还是系统启动），启动早期
#: 的读数可能小于 TTL。用 ``0.0`` 当哨兵会让新进程误判缓存仍然新鲜，从而
#: 永远不查花费、预算闸门形同虚设。
_budget_spent_cache: dict[str, object] = {"value": 0.0, "at": None}


def _budget_spent_ttl() -> float:
    """``_budget_spent_cache`` 的有效期（秒），默认 60。"""
    try:
        return max(0.0, float(os.getenv("AI_BUDGET_CACHE_SECONDS", "60")))
    except (TypeError, ValueError):
        return 60.0


def _budget_cache_is_fresh(now: float, ttl: float) -> bool:
    at = _budget_spent_cache["at"]
    if at is None:
        return False
    try:
        return (now - float(at)) <= ttl
    except (TypeError, ValueError):
        return False


def _current_budget_state() -> dict:
    """读取配置并判定当前预算状态；**任何异常都放行**。

    这里刻意「失败即放行」而不是「失败即拦截」：预算统计依赖数据库查询，
    若因库损坏/表缺失而读不到花费，正确反应是继续干活并在日志里留痕，
    而不是让整个采集链路因为一个统计查询失败而全线停摆。

    未配置 ``AI_BUDGET_LIMIT`` 时 ``limit`` 为 ``None``，``check_budget`` 直接
    返回 ``unlimited``，因此本函数在默认配置下是零行为的。
    """
    from src.services.ai_governance_service import check_budget

    raw_limit = (os.getenv("AI_BUDGET_LIMIT") or "").strip()
    if not raw_limit:
        return {"allow": True, "level": "unlimited", "message": None}

    try:
        limit = float(raw_limit)
    except (TypeError, ValueError):
        return {
            "allow": True,
            "level": "unlimited",
            "message": f"AI_BUDGET_LIMIT={raw_limit!r} 不是有效数值，忽略预算限制",
        }

    now = time.monotonic()
    ttl = _budget_spent_ttl()
    if not _budget_cache_is_fresh(now, ttl):
        try:
            from src.services.ai_usage_service import get_ai_usage_summary

            summary = get_ai_usage_summary(days=1) or {}
            spent = summary.get("estimated_cost") or 0.0
            _budget_spent_cache["value"] = float(spent)
            _budget_spent_cache["at"] = now
        except Exception as exc:  # noqa: BLE001 - 统计失败必须放行
            print(f"AI 预算统计读取失败（本次不做预算拦截）: {exc}")
            return {"allow": True, "level": "unlimited", "message": None}

    try:
        ratio = float(os.getenv("AI_BUDGET_WARN_RATIO", "0.8"))
    except (TypeError, ValueError):
        ratio = 0.8

    try:
        spent_now = float(_budget_spent_cache["value"] or 0.0)
    except (TypeError, ValueError):
        spent_now = 0.0

    return check_budget(spent=spent_now, limit=limit, warn_ratio=ratio)


def _sanitize_no_proxy_env() -> None:
    """Strip CIDR prefix lengths from IPv6 entries in NO_PROXY / no_proxy.

    httpx <= 0.28.1 wraps NO_PROXY IPv6 entries in brackets *including* the
    CIDR mask (e.g. ``[::1/128]``), which the URL parser rejects as an invalid
    port.  Stripping the ``/prefix`` part is safe because httpx doesn't
    support CIDR range matching anyway — it only does exact-host comparison.

    See https://github.com/encode/httpx/pull/3741
    """
    for key in ("NO_PROXY", "no_proxy"):
        value = os.environ.get(key)
        if not value:
            continue
        parts = [h.strip() for h in value.split(",")]
        cleaned: list[str] = []
        changed = False
        for part in parts:
            if "/" in part:
                host, _, prefix = part.partition("/")
                try:
                    ipaddress.IPv6Address(host)
                    cleaned.append(host)
                    changed = True
                    continue
                except ValueError:
                    pass
            cleaned.append(part)
        if changed:
            os.environ[key] = ",".join(cleaned)


class AIClient:
    """AI 客户端封装"""

    def __init__(self):
        self.settings: Optional[AISettings] = None
        self.client: Optional[AsyncOpenAI] = None
        # 全局并发闸：跨任务限制同时在飞的 AI 请求。作为类属性共享，
        # 这样多个 AIClient 实例（每个采集任务一个）合起来也受同一个上限约束，
        # 否则"限制并发"在多任务下会退化成"每任务各自限制"。
        self._ai_gate = _SHARED_AI_GATE
        self.refresh()

    def _load_settings(self) -> None:
        load_dotenv(dotenv_path=env_manager.env_file, override=True)
        self.settings = AISettings()

    def refresh(self) -> None:
        self._load_settings()
        self.client = self._initialize_client()

    def _initialize_client(self) -> Optional[AsyncOpenAI]:
        """初始化 OpenAI 客户端"""
        if not self.settings or not self.settings.is_configured():
            print("警告：AI 配置不完整，AI 功能将不可用")
            return None

        try:
            if self.settings.proxy_url:
                print(f"正在为 AI 请求使用代理: {self.settings.proxy_url}")
                os.environ['HTTP_PROXY'] = self.settings.proxy_url
                os.environ['HTTPS_PROXY'] = self.settings.proxy_url

            _sanitize_no_proxy_env()

            return AsyncOpenAI(
                api_key=self.settings.api_key,
                base_url=self.settings.base_url
            )
        except Exception as e:
            print(f"初始化 AI 客户端失败: {e}")
            return None

    def is_available(self) -> bool:
        """检查 AI 客户端是否可用"""
        return self.client is not None

    async def close(self) -> None:
        """关闭底层异步客户端，避免事件循环结束后再触发清理。"""
        client = self.client
        self.client = None
        if client is None:
            return

        close = getattr(client, "close", None)
        if close is None:
            return
        await close()

    @staticmethod
    def encode_image(image_path: str) -> Optional[str]:
        """将图片编码为 Base64"""
        if not image_path or not os.path.exists(image_path):
            return None
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            print(f"编码图片失败: {e}")
            return None

    async def analyze(
        self,
        product_data: Dict,
        image_paths: List[str],
        prompt_text: str
    ) -> Optional[Dict]:
        """
        分析商品数据

        Args:
            product_data: 商品数据
            image_paths: 图片路径列表
            prompt_text: 分析提示词

        Returns:
            分析结果
        """
        if not self.is_available():
            print("AI 客户端不可用")
            return None

        try:
            messages = self._build_messages(product_data, image_paths, prompt_text)
            response = await self._call_ai(messages)
            return self._parse_response(response)
        except Exception as e:
            print(f"AI 分析失败: {e}")
            return None

    async def generate_json(self, prompt_text: str) -> Optional[Dict]:
        """使用与商品分析相同的兼容链生成结构化 JSON。"""
        if not self.is_available():
            return None
        messages = [{"role": "user", "content": prompt_text}]
        response = await self._call_ai(
            messages,
            temperature=0.2,
            max_output_tokens=1200,
            enable_json_output=True,
        )
        return self._parse_response(response)

    def _build_messages(self, product_data: Dict, image_paths: List[str], prompt_text: str) -> List[Dict]:
        """构建 AI 消息。

        返回 ``[system, user]`` 两条消息：system 承载分析要求（prompt），
        user 承载商品动态数据。与上游的「单条 user 消息」不同，这样能让
        system prompt 在多轮/重试间保持稳定，也便于接入只认 system 角色的模型。

        ``settings`` 通过 ``getattr`` 兜底读取：本方法可能在未经 ``__init__``
        初始化的实例上被调用（例如单元测试直接构造），此时按默认值处理，
        不应抛出 AttributeError。
        """
        product_json = json.dumps(product_data, ensure_ascii=False, indent=2)
        image_data_urls: List[str] = []
        for path in image_paths:
            base64_img = self.encode_image(path)
            if base64_img:
                image_data_urls.append(f"data:image/jpeg;base64,{base64_img}")

        settings = getattr(self, "settings", None)
        image_mode = str(getattr(settings, "image_mode", "auto") or "auto").strip().lower()
        if image_mode not in {"auto", "on", "off"}:
            image_mode = "auto"
        allow_images = image_mode == "on"
        if image_mode == "auto":
            allow_images = not is_text_only_model(
                getattr(settings, "base_url", ""),
                getattr(settings, "model_name", ""),
            )

        # Text-only models must not receive OpenAI Vision image_url blocks.
        # 用统一的构造器而非手拼字符串：它会带上「本次未提供商品图片」的补充说明，
        # 避免模型把「没有图」误当成信息缺失而去臆测图片内容。
        text_prompt = build_analysis_text_prompt(
            product_json,
            prompt_text,
            include_images=bool(image_data_urls) and allow_images,
        )
        user_content = build_user_message_content(
            text_prompt,
            image_data_urls,
            allow_images=allow_images,
        )
        return [
            {"role": "system", "content": prompt_text.strip()},
            {"role": "user", "content": user_content},
        ]

    def _gate(self):
        """惰性取得并发闸。

        本方法用 ``getattr`` 兜底：``AIClient`` 可能在未经 ``__init__`` 初始化的
        实例上被调用（例如单元测试直接构造），此时按需补建一个共享闸，
        不应抛出 AttributeError（与 ``_build_messages`` 对 ``settings`` 的处理一致）。
        """
        gate = getattr(self, "_ai_gate", None)
        if gate is None:
            gate = _SHARED_AI_GATE
            self._ai_gate = gate
        return gate

    def _cache(self):
        """惰性取得结果缓存（与 :meth:`_gate` 同样的兜底理由）。"""
        cache = getattr(self, "_ai_cache", None)
        if cache is None:
            cache = _SHARED_AI_CACHE
            self._ai_cache = cache
        return cache

    def _effective_temperature(self, temperature: float, use_temperature: bool) -> float | str:
        """返回「实际会发给上游」的 temperature 标识，用于构造缓存键。

        模型不支持 temperature 时返回 :data:`_TEMPERATURE_UNSUPPORTED` 这个哨兵，
        而不是 ``0.0``——因为「不发这个参数」与「发 0.0」是两种不同的请求，
        结果未必相同，用同一个键会互相污染。
        """
        return temperature if use_temperature else _TEMPERATURE_UNSUPPORTED

    def _build_cache_key(
        self,
        messages: List[Dict],
        *,
        temperature: float,
        max_output_tokens: int,
        api_mode: str,
    ) -> str:
        """由**最终发出的消息与请求参数**推导缓存键。

        刻意对整个 ``messages`` 做哈希，而不是只用商品 ID：分析结果取决于
        商品数据 + 提示词 + 图片内容 + 模型 + 采样参数，其中任何一项变化都必须
        产生不同的键。只按 ID 缓存会出现「换了提示词却拿到旧结论」这类静默错误，
        而那种错误极难排查。

        图片以 base64 data URL 的形式已内联在 ``messages`` 里，因此图片变化
        也会自然反映到键上。
        """
        from src.services.ai_governance_service import build_cache_key

        try:
            payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            # 无法序列化时退化为「不缓存」，而不是抛错中断分析
            return ""
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        settings = getattr(self, "settings", None)
        return build_cache_key(
            getattr(settings, "model_name", None) or "unknown-model",
            getattr(settings, "base_url", None) or "unknown-base",
            api_mode,
            temperature,
            max_output_tokens,
            digest,
        )

    async def _call_ai(
        self,
        messages: List[Dict],
        *,
        temperature: float = 0.1,
        max_output_tokens: int = 4000,
        enable_json_output: Optional[bool] = None,
    ) -> str:
        """调用 AI API"""
        api_mode = CHAT_COMPLETIONS_API_MODE
        use_response_format = (
            self.settings.enable_response_format
            if enable_json_output is None
            else enable_json_output
        )
        use_temperature = not getattr(self, "_temperature_unsupported", False)
        max_attempts = 4

        cache = self._cache()
        # 键用「本次实际会使用的参数」计算，而不是调用方传入的原始参数。
        # 否则在「模型不支持 temperature」的模型上，第一次调用存进的是
        # key(无 temperature)、第二次却用 key(原始 temperature) 去查，永远不命中。
        cache_key = self._build_cache_key(
            messages,
            temperature=self._effective_temperature(temperature, use_temperature),
            max_output_tokens=max_output_tokens,
            api_mode=api_mode,
        )
        if cache_key:
            cached = cache.get(cache_key)
            if cached is not None:
                return cached

        # 预算闸门放在缓存之后：命中缓存不产生新费用，不该被预算拦下；
        # 只有真的要发请求时才需要判预算。
        budget = _current_budget_state()
        if not budget.get("allow", True):
            raise AIBudgetExceededError(budget.get("message") or "AI 预算已超限")
        if budget.get("level") == "warning" and budget.get("message"):
            print(f"[AI 预算提醒] {budget['message']}")

        for attempt in range(max_attempts):
            request_params = build_ai_request_params(
                api_mode,
                model=self.settings.model_name,
                messages=messages,
                temperature=temperature,
                max_output_tokens=max_output_tokens,
                enable_json_output=use_response_format,
            )
            if not use_temperature:
                request_params = remove_temperature_param(request_params)

            if self.settings.enable_thinking:
                request_params["extra_body"] = {"enable_thinking": False}

            try:
                # 全局并发闸：多个采集任务并行时限制同时在飞的 AI 请求数，
                # 避免把上游 API 打到限流（限流会连带整批商品分析失败）。
                async with self._gate():
                    response = await create_ai_response_async(
                        self.client,
                        api_mode,
                        request_params,
                    )
                record_ai_usage(response, model=self.settings.model_name, request_type="ai_client")
                content = extract_ai_response_content(response)
                # 用**实际生效**的参数重算键再写入：重试可能切换了 api_mode 或
                # 移除了 temperature，若仍按进入循环时的键写入，会把「另一套参数
                # 得到的结果」记到「原始参数的键」上，后续命中就是错配。
                final_key = self._build_cache_key(
                    messages,
                    temperature=self._effective_temperature(temperature, use_temperature),
                    max_output_tokens=max_output_tokens,
                    api_mode=api_mode,
                )
                if final_key:
                    cache.put(final_key, content)
                return content
            except EmptyAIResponseError as exc:
                if attempt < max_attempts - 1:
                    print(
                        f"AI响应为空，正在自动重试 ({attempt + 2}/{max_attempts})"
                    )
                    continue
                raise exc
            except Exception as exc:
                changed = False
                if (
                    api_mode == CHAT_COMPLETIONS_API_MODE
                    and is_chat_completions_api_unsupported_error(exc)
                ):
                    api_mode = RESPONSES_API_MODE
                    changed = True
                    print("当前服务未实现 Chat Completions API，正在自动回退到 Responses API")
                elif (
                    api_mode == RESPONSES_API_MODE
                    and is_responses_api_unsupported_error(exc)
                ):
                    api_mode = CHAT_COMPLETIONS_API_MODE
                    changed = True
                    print("当前服务未实现 Responses API，正在自动回退到 Chat Completions API")
                if use_response_format and is_json_output_unsupported_error(exc):
                    use_response_format = False
                    changed = True
                    print("当前模型不支持结构化 JSON 输出，正在自动重试并移除该参数")
                if use_temperature and is_temperature_unsupported_error(exc):
                    use_temperature = False
                    # 记成粘性能力位：同一模型后续调用直接用一致参数，
                    # 既少一轮失败重试，也让缓存键保持稳定（否则永远不命中）。
                    self._temperature_unsupported = True
                    changed = True
                    print("当前模型不支持 temperature 参数，正在自动重试并移除该参数")
                if changed and attempt < max_attempts - 1:
                    continue
                raise

        raise RuntimeError("AI 调用在兼容性重试后仍未返回结果")

    def _parse_response(self, response_text: str) -> Optional[Dict]:
        """解析 AI 响应"""
        try:
            return parse_ai_response_json(response_text)
        except json.JSONDecodeError:
            print(f"无法解析 AI 响应: {response_text[:100]}")
            return None
