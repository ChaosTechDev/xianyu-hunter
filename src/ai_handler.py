import asyncio
import base64
import copy
import hashlib
import json
import os
import re
import sys
import shutil
import traceback
from datetime import datetime, timedelta
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import requests

# 设置标准输出编码为UTF-8，解决Windows控制台编码问题
if sys.platform.startswith("win"):
    # Preserve capture/supervisor streams; native console streams can be
    # reconfigured in place without detaching their underlying buffers.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")

from src.config import (
    AI_DEBUG_MODE,
    IMAGE_DOWNLOAD_HEADERS,
    IMAGE_SAVE_DIR,
    TASK_IMAGE_DIR_PREFIX,
    MODEL_NAME,
    BASE_URL,
    ENABLE_RESPONSE_FORMAT,
    client,
)
from src.ai_message_builder import (
    build_analysis_text_prompt,
    build_user_message_content,
    is_text_only_model,
)
from src.services.ai_response_parser import (
    EmptyAIResponseError,
    extract_ai_response_content,
    parse_ai_response_json,
)
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
from src.services.notification_service import build_notification_service
from src.services.ai_usage_service import record_ai_usage
from src.utils import convert_goofish_link, retry_on_failure


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


DEFAULT_IMAGE_DOWNLOAD_CONCURRENCY = max(
    1,
    _positive_int(os.getenv("IMAGE_DOWNLOAD_CONCURRENCY", "3"), 3),
)


def safe_print(text):
    """安全的打印函数，处理编码错误"""
    try:
        print(text)
    except UnicodeEncodeError:
        # 如果遇到编码错误，尝试用ASCII编码并忽略无法编码的字符
        try:
            print(text.encode('ascii', errors='ignore').decode('ascii'))
        except:
            # 如果还是失败，打印一个简化的消息
            print("[输出包含无法显示的字符]")


def _build_debug_request_summary(api_mode: str, request_params: dict) -> dict:
    summary = {
        "api_mode": api_mode,
        "model": request_params.get("model"),
    }
    if "temperature" in request_params:
        summary["temperature"] = request_params["temperature"]
    if "max_output_tokens" in request_params:
        summary["max_output_tokens"] = request_params["max_output_tokens"]
    if "max_tokens" in request_params:
        summary["max_tokens"] = request_params["max_tokens"]
    if "text" in request_params:
        summary["text"] = request_params["text"]
    if "response_format" in request_params:
        summary["response_format"] = request_params["response_format"]
    if "input" in request_params:
        summary["input_content_types"] = [
            [item.get("type") for item in message.get("content", [])]
            for message in request_params["input"]
        ]
    if "messages" in request_params:
        summary["message_content_types"] = [
            _extract_message_content_types(message)
            for message in request_params["messages"]
        ]
    return summary


def _extract_message_content_types(message: dict) -> list[str]:
    content = message.get("content")
    if isinstance(content, str):
        return ["text"]
    if not isinstance(content, list):
        return [type(content).__name__]
    return [str(item.get("type")) for item in content if isinstance(item, dict)]


@retry_on_failure(retries=2, delay=3)
async def _download_single_image(url, save_path):
    """一个带重试的内部函数，用于异步下载单个图片。"""
    loop = asyncio.get_running_loop()
    # 使用 run_in_executor 运行同步的 requests 代码，避免阻塞事件循环
    response = await loop.run_in_executor(
        None,
        lambda: requests.get(url, headers=IMAGE_DOWNLOAD_HEADERS, timeout=20, stream=True)
    )
    response.raise_for_status()
    with open(save_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)
    return save_path


def _build_image_save_path(
    product_id: str,
    index: int,
    url: str,
    task_image_dir: str,
) -> str:
    clean_url = url.split('.heic')[0] if '.heic' in url else url
    file_name_base = os.path.basename(clean_url).split('?')[0]
    file_name = f"product_{product_id}_{index}_{file_name_base}"
    file_name = re.sub(r'[\\/*?:"<>|]', "", file_name)
    if not os.path.splitext(file_name)[1]:
        file_name += ".jpg"
    return os.path.join(task_image_dir, file_name)


async def download_all_images(product_id, image_urls, task_name="default", concurrency=None):
    """异步下载一个商品的所有图片。如果图片已存在则跳过。支持任务隔离。"""
    if not image_urls:
        return []

    # 为每个任务创建独立的图片目录
    task_image_dir = os.path.join(IMAGE_SAVE_DIR, f"{TASK_IMAGE_DIR_PREFIX}{task_name}")
    os.makedirs(task_image_dir, exist_ok=True)

    urls = [url.strip() for url in image_urls if url.strip().startswith('http')]
    if not urls:
        return []

    max_concurrency = _positive_int(concurrency, DEFAULT_IMAGE_DOWNLOAD_CONCURRENCY)
    semaphore = asyncio.Semaphore(max_concurrency)
    total_images = len(urls)

    async def _download_one(index: int, url: str):
        save_path = _build_image_save_path(product_id, index, url, task_image_dir)
        if os.path.exists(save_path):
            safe_print(
                f"   [图片] 图片 {index}/{total_images} 已存在，跳过下载: {os.path.basename(save_path)}"
            )
            return save_path
        async with semaphore:
            safe_print(f"   [图片] 正在下载图片 {index}/{total_images}: {url}")
            if await _download_single_image(url, save_path):
                safe_print(
                    f"   [图片] 图片 {index}/{total_images} 已成功下载到: {os.path.basename(save_path)}"
                )
                return save_path
        return None

    tasks = [
        asyncio.create_task(_download_one(index, url))
        for index, url in enumerate(urls, start=1)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    saved_paths = []
    for url, result in zip(urls, results):
        try:
            if isinstance(result, Exception):
                raise result
            if result:
                saved_paths.append(result)
        except Exception as e:
            safe_print(f"   [图片] 处理图片 {url} 时发生错误，已跳过此图: {e}")

    return saved_paths


def cleanup_task_images(task_name):
    """清理指定任务的图片目录"""
    task_image_dir = os.path.join(IMAGE_SAVE_DIR, f"{TASK_IMAGE_DIR_PREFIX}{task_name}")
    if os.path.exists(task_image_dir):
        try:
            shutil.rmtree(task_image_dir)
            safe_print(f"   [清理] 已删除任务 '{task_name}' 的临时图片目录: {task_image_dir}")
        except Exception as e:
            safe_print(f"   [清理] 删除任务 '{task_name}' 的临时图片目录时出错: {e}")
    else:
        safe_print(f"   [清理] 任务 '{task_name}' 的临时图片目录不存在: {task_image_dir}")


def cleanup_ai_logs(logs_dir: str, keep_days: int = 1) -> None:
    try:
        cutoff = datetime.now() - timedelta(days=keep_days)
        for filename in os.listdir(logs_dir):
            if not filename.endswith(".log"):
                continue
            try:
                timestamp = datetime.strptime(filename[:15], "%Y%m%d_%H%M%S")
            except ValueError:
                continue
            if timestamp < cutoff:
                os.remove(os.path.join(logs_dir, filename))
    except Exception as e:
        safe_print(f"   [日志] 清理AI日志时出错: {e}")


def encode_image_to_base64(image_path):
    """将本地图片文件编码为 Base64 字符串。"""
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        with open(image_path, "rb") as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        safe_print(f"编码图片时出错: {e}")
        return None


def validate_ai_response_format(parsed_response):
    """验证AI响应的格式是否符合预期结构"""
    required_fields = [
        "prompt_version",
        "is_recommended",
        "reason",
        "risk_tags",
        "criteria_analysis"
    ]

    # 检查顶层字段
    for field in required_fields:
        if field not in parsed_response:
            safe_print(f"   [AI分析] 警告：响应缺少必需字段 '{field}'")
            return False

    # 检查criteria_analysis是否为字典且不为空
    criteria_analysis = parsed_response.get("criteria_analysis", {})
    if not isinstance(criteria_analysis, dict) or not criteria_analysis:
        safe_print("   [AI分析] 警告：criteria_analysis必须是非空字典")
        return False

    # 检查seller_type字段（所有商品都需要）
    if "seller_type" not in criteria_analysis:
        safe_print("   [AI分析] 警告：criteria_analysis缺少必需字段 'seller_type'")
        return False

    # 检查数据类型
    if not isinstance(parsed_response.get("is_recommended"), bool):
        safe_print("   [AI分析] 警告：is_recommended字段不是布尔类型")
        return False

    if not isinstance(parsed_response.get("risk_tags"), list):
        safe_print("   [AI分析] 警告：risk_tags字段不是列表类型")
        return False

    return True


@retry_on_failure(retries=3, delay=5)
async def send_ntfy_notification(product_data, reason):
    """兼容旧调用名，内部统一走 NotificationService。

    同一任务批次内的连续通知会在短暂窗口内聚合，
    合并为一条汇总推送，避免逐条刷屏。
    """
    global _aggregate_flush_task
    service = build_notification_service()
    if not service.clients:
        safe_print("警告：未在 .env 文件中配置任何通知服务，跳过通知。")
        return {}

    # 通知价格上限过滤：任务配置 notify_price_below，高于该价的商品不推送（仍正常入库/统计）
    try:
        from src.infrastructure.persistence.sqlite_task_repository import find_task_by_name_sync
        from src.services.price_history_service import parse_price_value

        _task_name = product_data.get("任务名称")
        if _task_name:
            _task = find_task_by_name_sync(_task_name)
            if _task and _task.notify_price_below is not None:
                _info = product_data.get("商品信息") or product_data
                _price = parse_price_value(_info.get("当前售价")) or parse_price_value(
                    product_data.get("当前售价")
                )
                if _price is not None and _price > float(_task.notify_price_below):
                    safe_print(
                        f"   -> 价格 ¥{_price:,.0f} 超过通知上限 ¥{float(_task.notify_price_below):,.0f}，跳过推送（已入库）"
                    )
                    return {}
    except Exception:
        pass
    # 任务级自动咨询：任务开启 auto_consult 时，后台私聊卖家（不阻塞采集）
    try:
        from src.services.consultation_service import maybe_send_task_consultation

        _consult_task_name = product_data.get("任务名称")
        if _consult_task_name:
            _consult_task = find_task_by_name_sync(_consult_task_name)
            if _consult_task and getattr(_consult_task, "auto_consult", False):
                asyncio.create_task(maybe_send_task_consultation(product_data))
    except Exception:
        pass
    async with _pending_lock:
        _pending_notifications.append((product_data, reason))

    if _aggregate_flush_task and not _aggregate_flush_task.done():
        _aggregate_flush_task.cancel()
    _aggregate_flush_task = asyncio.create_task(_delayed_flush())
    return {}


async def _delayed_flush():
    """聚合窗口结束后统一发送。"""
    await asyncio.sleep(AGGREGATE_WINDOW_SECONDS)
    await _flush_notifications()


async def _flush_notifications():
    global _pending_notifications
    async with _pending_lock:
        items = _pending_notifications
        _pending_notifications = []
    if not items:
        return

    try:
        service = build_notification_service()
        if len(items) == 1:
            product_data, reason = items[0]
            results = await service.send_notification(product_data, reason)
        else:
            product_data, reason = _build_summary(items)
            results = await service.send_notification(product_data, reason)
    except Exception as exc:
        safe_print(f"   -> 通知发送失败: {exc}")
        return {}

    for channel, result in results.items():
        if result["success"]:
            safe_print(f"   -> {channel} 通知发送成功。")
        else:
            safe_print(f"   -> {channel} 通知发送失败: {result['message']}")
    return results


def _build_summary(items):
    """把多条商品通知合并为一条汇总推送。"""
    first_payload, _ = items[0]
    keyword = (
        first_payload.get("搜索关键字")
        or first_payload.get("任务名称")
        or first_payload.get("商品信息", {}).get("商品标题", "")
    )
    first_link = first_payload.get("商品链接") or first_payload.get("商品信息", {}).get("商品链接", "")

    lines = []
    seen = set()
    for payload, _ in items:
        info = payload.get("商品信息", {})
        title = info.get("商品标题", "未知商品")
        price = info.get("当前售价", "")
        link = payload.get("商品链接") or info.get("商品链接", "")
        key = (link or title).split("&")[0]
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"{price} {title}")

    total = len(seen)
    content_lines = lines[:SUMMARY_MAX_ITEMS]
    remainder = total - len(content_lines)
    if remainder > 0:
        content_lines.append(f"… 等共 {total} 件，点击查看详情")

    summary_payload = {
        "商品标题": str(keyword)[:40] or "闲鱼好物",
        "当前售价": f"{total} 件",
        "商品链接": first_link,
        "通知标题": f"🎯 新发现 {total} 件好物",
    }
    return summary_payload, "\n".join(content_lines)


# 通知聚合相关状态（模块级）
_pending_notifications = []
_pending_lock = asyncio.Lock()
_aggregate_flush_task = None
AGGREGATE_WINDOW_SECONDS = 25
SUMMARY_MAX_ITEMS = 5

#: 商品分析的单次输出上限。
#:
#: 这里必须给推理模型留足"思考 + 正文"两份预算 —— 思考 token 也计入
#: max_output_tokens。原值 4000 是从纯对话模型时代沿用下来的，对
#: ``global:deepseek-v4.1-flash`` 这类模型不够：真机统计 189 次商品分析中，
#: 有 91 次 ``output_tokens >= 3999``（撞顶率 48.1%），JSON 被截断在中间，
#: 契约校验报「响应缺少必需字段 'prompt_version'」，只能靠降温度重试 3~4 次
#: 硬扛 —— 每次都白烧一次 token，且重试仍可能失败。
#:
#: 实测同一提示词下 4000 与 8192 都能 ``finish_reason=stop``（短描述时），
#: 但真实商品描述更长，4000 会被思考吃满。8192 是实测安全的预算。
PRODUCT_ANALYSIS_MAX_TOKENS = 8192


#: 进程内唯一的分析结果缓存句柄。
#:
#: 必须缓存这个**实例**本身。``_shared_ai_cache()`` 是工厂函数，每次调用都返回
#: 一个全新的空缓存 —— 若每次都去调它，写入与读取落在两个不同对象上，缓存永远
#: 不命中（实测表现为缓存条目数恒为 0、每个商品都重新付费分析）。
_ANALYSIS_CACHE = None


def _analysis_cache():
    """商品分析的结果缓存，与 ``AIClient`` 共用同一份全局缓存实例。

    此前的缺陷：商品分析走的是 ``create_ai_response_async`` 直连，
    **完全绕过** ``AIClient._call_ai`` 里的那层缓存。而 ``_shared_ai_cache()``
    的注释明确写着它的设计意图就是「同一个商品被多个任务命中时，第二个任务
    直接拿缓存」—— 也就是说那份缓存在生产主路径上从来没被用过。

    结果：同一商品被两个任务命中时（例如「3080」和「显卡」两个关键词都能搜到），
    两边各付一次 token 费用。

    复用**同一个实例**而不是新建，是为了让「谁先分析完谁写缓存」在整个进程内
    只有一个真相来源。
    """
    global _ANALYSIS_CACHE
    if _ANALYSIS_CACHE is not None:
        return _ANALYSIS_CACHE
    try:
        from src.infrastructure.external.ai_client import _SHARED_AI_CACHE

        _ANALYSIS_CACHE = _SHARED_AI_CACHE
    except Exception:
        # 缓存不可用只应导致"不缓存"，绝不能中断分析
        return None
    return _ANALYSIS_CACHE


def _build_analysis_cache_key(messages, *, temperature: float, max_output_tokens: int) -> str:
    """由**最终发出的消息与参数**推导缓存键。

    与 ``AIClient._build_cache_key`` 同一策略：对整个 ``messages`` 取哈希，
    而不是只按商品 ID —— 分析结果取决于商品数据 + 提示词 + 图片 + 模型 + 采样
    参数，任何一项变化都必须换键，否则会出现「换了提示词却拿到旧结论」这类
    静默错误。图片以 base64 内联在 messages 里，因此图片变化自然反映到键上。
    """
    from src.services.ai_governance_service import build_cache_key

    try:
        payload = json.dumps(messages, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return ""
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return build_cache_key(MODEL_NAME or "unknown-model", temperature,
                           max_output_tokens, digest)


async def get_ai_analysis(product_data, image_paths=None, prompt_text=""):
    """将完整的商品JSON数据和所有图片发送给 AI 进行分析（异步）。"""
    if not client:
        safe_print("   [AI分析] 错误：AI客户端未初始化，跳过分析。")
        return None

    item_info = product_data.get('商品信息', {})
    product_id = item_info.get('商品ID', 'N/A')

    safe_print(f"\n   [AI分析] 开始分析商品 #{product_id} (含 {len(image_paths or [])} 张图片)...")
    safe_print(f"   [AI分析] 标题: {item_info.get('商品标题', '无')}")

    if not prompt_text:
        safe_print("   [AI分析] 错误：未提供AI分析所需的prompt文本。")
        return None

    product_details_json = json.dumps(product_data, ensure_ascii=False, indent=2)
    system_prompt = prompt_text

    if AI_DEBUG_MODE:
        safe_print("\n--- [AI DEBUG] ---")
        safe_print("--- PRODUCT DATA (JSON) ---")
        safe_print(product_details_json)
        safe_print("--- PROMPT TEXT (完整内容) ---")
        safe_print(prompt_text)
        safe_print("-------------------\n")

    image_data_urls = []
    if image_paths:
        for path in image_paths:
            base64_image = encode_image_to_base64(path)
            if base64_image:
                image_data_urls.append(f"data:image/jpeg;base64,{base64_image}")

    image_mode = os.getenv("AI_IMAGE_MODE", "auto").strip().lower()
    if image_mode not in {"auto", "on", "off"}:
        image_mode = "auto"
    allow_images = image_mode == "on"
    if image_mode == "auto":
        allow_images = not is_text_only_model(BASE_URL, MODEL_NAME)

    combined_text_prompt = "商品动态数据：\n" + product_details_json
    user_content = build_user_message_content(
        combined_text_prompt,
        image_data_urls,
        allow_images=allow_images,
    )
    messages = [
        {"role": "system", "content": system_prompt.strip()},
        {"role": "user", "content": user_content},
    ]

    # 保存最终传输内容到日志文件
    try:
        # 创建logs文件夹
        logs_dir = os.path.join("logs", "ai")
        os.makedirs(logs_dir, exist_ok=True)
        cleanup_ai_logs(logs_dir, keep_days=1)

        # 生成日志文件名（当前时间）
        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_filename = f"{current_time}.log"
        log_filepath = os.path.join(logs_dir, log_filename)

        task_name = product_data.get("任务名称") or product_data.get("任务名") or "unknown"
        log_payload = {
            "timestamp": current_time,
            "task_name": task_name,
            "product_id": product_id,
            "title": item_info.get("商品标题", "无"),
            "image_count": len(image_data_urls),
        }
        log_content = json.dumps(log_payload, ensure_ascii=False)

        # 写入日志文件
        with open(log_filepath, 'w', encoding='utf-8') as f:
            f.write(log_content)

        safe_print(f"   [日志] AI分析请求已保存到: {log_filepath}")

    except Exception as e:
        safe_print(f"   [日志] 保存AI分析日志时出错: {e}")

    # 增强的AI调用，包含更严格的结构化输出控制和重试机制
    max_retries = 4
    api_mode = CHAT_COMPLETIONS_API_MODE
    use_response_format = ENABLE_RESPONSE_FORMAT
    use_temperature = True
    analysis_cache = _analysis_cache()
    for attempt in range(max_retries):
        try:
            # 根据重试次数调整参数
            current_temperature = 0.1 if attempt == 0 else 0.05  # 重试时使用更低的温度

            from src.config import get_ai_request_params

            # 缓存查询放在**发请求之前**，且用与下方请求完全一致的参数算键。
            # 命中直接返回，省掉一次 token 费用；未命中才继续走真实请求。
            cache_key = ""
            if analysis_cache is not None:
                cache_key = _build_analysis_cache_key(
                    messages,
                    temperature=current_temperature,
                    max_output_tokens=PRODUCT_ANALYSIS_MAX_TOKENS,
                )
                if cache_key:
                    cached = analysis_cache.get(cache_key)
                    if cached is not None:
                        safe_print(
                            f"   [AI分析] 命中结果缓存，跳过重复分析 "
                            f"(商品 #{product_id})"
                        )
                        return cached

            request_params = build_ai_request_params(
                api_mode,
                model=MODEL_NAME,
                messages=messages,
                temperature=current_temperature,
                max_output_tokens=PRODUCT_ANALYSIS_MAX_TOKENS,
                enable_json_output=use_response_format,
            )
            if not use_temperature:
                request_params = remove_temperature_param(request_params)

            request_params = get_ai_request_params(**request_params)

            if AI_DEBUG_MODE:
                safe_print(f"\n--- [AI DEBUG] 第{attempt + 1}次尝试 REQUEST ---")
                safe_print(
                    json.dumps(
                        _build_debug_request_summary(api_mode, request_params),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                safe_print("-----------------------------------\n")

            response = await create_ai_response_async(
                client,
                api_mode,
                request_params,
            )
            record_ai_usage(response, model=MODEL_NAME, request_type="product_analysis")
            ai_response_content = extract_ai_response_content(response)

            if AI_DEBUG_MODE:
                safe_print(f"\n--- [AI DEBUG] 第{attempt + 1}次尝试 ---")
                safe_print("--- RAW AI RESPONSE ---")
                safe_print(ai_response_content)
                safe_print("---------------------\n")

            try:
                parsed_response = parse_ai_response_json(ai_response_content)

                # 验证响应格式
                if validate_ai_response_format(parsed_response):
                    safe_print(f"   [AI分析] 第{attempt + 1}次尝试成功，响应格式验证通过")
                    # 只缓存通过契约校验的结果。把失败/半截响应写进缓存会让后续
                    # 同商品直接拿到坏数据，比不缓存更糟。
                    if analysis_cache is not None and cache_key:
                        analysis_cache.put(
                            cache_key, copy.deepcopy(parsed_response)
                        )
                    return parsed_response
                safe_print(f"   [AI分析] 第{attempt + 1}次尝试格式验证失败")
                if attempt < max_retries - 1:
                    safe_print(f"   [AI分析] 准备第{attempt + 2}次重试...")
                    continue
                raise ValueError("AI响应格式缺少必需字段或字段类型不正确。")
            except json.JSONDecodeError as e:
                safe_print(f"   [AI分析] 第{attempt + 1}次尝试JSON解析失败: {e}")
                if attempt < max_retries - 1:
                    safe_print(f"   [AI分析] 准备第{attempt + 2}次重试...")
                    continue
                raise e
            except EmptyAIResponseError as e:
                safe_print(f"   [AI分析] 第{attempt + 1}次尝试返回空响应: {e}")
                if attempt < max_retries - 1:
                    safe_print(f"   [AI分析] 准备第{attempt + 2}次重试...")
                    continue
                raise e

        except Exception as e:
            if (
                api_mode == CHAT_COMPLETIONS_API_MODE
                and is_chat_completions_api_unsupported_error(e)
            ):
                api_mode = RESPONSES_API_MODE
                safe_print(
                    "   [AI分析] 当前服务未实现 Chat Completions API，后续重试将自动回退到 Responses API。"
                )
            elif api_mode == RESPONSES_API_MODE and is_responses_api_unsupported_error(e):
                api_mode = CHAT_COMPLETIONS_API_MODE
                safe_print(
                    "   [AI分析] 当前服务未实现 Responses API，后续重试将自动回退到 Chat Completions API。"
                )
            if use_response_format and is_json_output_unsupported_error(e):
                use_response_format = False
                safe_print(
                    "   [AI分析] 当前模型不支持结构化 JSON 输出，后续重试将自动禁用该参数。"
                )
            if use_temperature and is_temperature_unsupported_error(e):
                use_temperature = False
                safe_print(
                    "   [AI分析] 当前模型不支持 temperature 参数，后续重试将自动禁用该参数。"
                )
            if AI_DEBUG_MODE:
                safe_print(f"\n--- [AI DEBUG] 第{attempt + 1}次尝试 EXCEPTION ---")
                safe_print(repr(e))
                safe_print(traceback.format_exc())
                safe_print("-------------------------------------\n")
            safe_print(f"   [AI分析] 第{attempt + 1}次尝试AI调用失败: {e}")
            if attempt < max_retries - 1:
                safe_print(f"   [AI分析] 准备第{attempt + 2}次重试...")
                continue
            else:
                raise e
