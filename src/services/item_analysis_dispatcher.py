"""
商品分析分发器
将卖家资料采集、图片下载、AI 分析和结果保存移出主抓取链路。
"""
import asyncio
import copy
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from src.keyword_rule_engine import build_search_text, evaluate_keyword_rules
from src.services.price_history_service import build_price_history_insights


#: 进程级共享的跨任务通知去重器。
#:
#: 必须是进程级共享而非每任务一个：去重的意义就在于「任务之间互相同步」，
#: 若每个任务持有一份独立的记录，去重会退化成单任务级别，等于没做。
_SHARED_DEDUPER = None


def _shared_deduper():
    """惰性构造跨任务通知去重器。

    窗口取 ``NOTIFY_DEDUP_WINDOW_SECONDS``（默认 3600 秒）。惰性构造而非模块级
    实例化：本模块在多个入口被导入，惰性可以避免导入期就固化配置。
    """
    global _SHARED_DEDUPER
    if _SHARED_DEDUPER is None:
        from src.services.notification_dedup_service import CrossTaskNotificationDeduper

        try:
            window = float(os.getenv("NOTIFY_DEDUP_WINDOW_SECONDS", "3600"))
        except (TypeError, ValueError):
            window = 3600.0
        _SHARED_DEDUPER = CrossTaskNotificationDeduper(window_seconds=window)
    return _SHARED_DEDUPER


SellerLoader = Callable[[str], Awaitable[dict]]
ImageDownloader = Callable[[str, list[str], str], Awaitable[list[str]]]
AIAnalyzer = Callable[[dict, list[str], str], Awaitable[Optional[dict]]]
Notifier = Callable[[dict, str], Awaitable[None]]
Saver = Callable[[dict, str], Awaitable[bool]]


@dataclass(frozen=True)
class ItemAnalysisJob:
    keyword: str
    task_name: str
    decision_mode: str
    analyze_images: bool
    prompt_text: str
    keyword_rules: tuple[str, ...]
    final_record: dict
    seller_id: Optional[str]
    zhima_credit_text: Optional[str]
    registration_duration_text: str
    notify_mode: str = "keyword"
    notify_price_below: Optional[float] = None


class ItemAnalysisDispatcher:
    """用受控并发处理商品分析和落盘。"""

    def __init__(
        self,
        *,
        concurrency: int,
        skip_ai_analysis: bool,
        seller_loader: SellerLoader,
        image_downloader: ImageDownloader,
        ai_analyzer: AIAnalyzer,
        notifier: Notifier,
        saver: Saver,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._skip_ai_analysis = skip_ai_analysis
        self._seller_loader = seller_loader
        self._image_downloader = image_downloader
        self._ai_analyzer = ai_analyzer
        self._notifier = notifier
        self._saver = saver
        self._tasks: set[asyncio.Task] = set()
        self.completed_count = 0

    def submit(self, job: ItemAnalysisJob) -> None:
        task = asyncio.create_task(self._process_with_limit(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def join(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks))

    async def _process_with_limit(self, job: ItemAnalysisJob) -> None:
        async with self._semaphore:
            await self._process_job(job)

    async def _process_job(self, job: ItemAnalysisJob) -> None:
        record = copy.deepcopy(job.final_record)
        item_data = record.get("商品信息", {}) or {}
        record["卖家信息"] = await self._load_seller_info(job)
        record["ai_analysis"] = await self._build_analysis_result(job, record)
        # 卖家信用评分：把散落的卖家指标确定性合成一个分数与风险信号。
        # 提示词里「信用等级必须极好」这条硬性条件此前只由模型判断，
        # 这里做一遍可复现的独立判定，供前端展示与上游交叉验证。
        credit = self._build_seller_credit(record["卖家信息"])
        if credit is not None:
            record["卖家信用评分"] = credit
        # 融合评分：把 AI 的定性结论、关键词命中、价格优势与风险标签压成单一分数，
        # 便于排序与解释。评分失败绝不影响主流程，因此整体包在 try 里。
        score = self._build_score(job, item_data, record["ai_analysis"])
        if score is not None:
            record["评分"] = score
        # 利润估算：同样的「历史中位数 vs 当前价」输入，换一个问法——
        # 不是「便宜吗」，而是「买进来还能赚吗」。样本不足时明确报告不可估算
        # （estimated=False）而不是给一个看着精确的数字。
        profit = self._build_profit(job, item_data)
        if profit is not None:
            record["利润估算"] = profit
        if await self._saver(record, job.keyword):
            self.completed_count += 1
        # 通知负载：商品信息 + 任务上下文（价格过滤/自动咨询需要任务名）
        notify_payload = copy.deepcopy(item_data)
        notify_payload["任务名称"] = job.task_name
        notify_payload["任务关键词"] = job.keyword
        notify_payload["商品信息"] = item_data
        await self._notify_if_recommended(job, notify_payload, record["ai_analysis"])

    def _build_seller_credit(self, seller_info: dict) -> dict | None:
        """计算卖家信用评分；异常只放弃该项，不影响落盘。"""
        try:
            from src.services.seller_credit_service import score_seller

            return score_seller(seller_info)
        except Exception as exc:
            print(f"   [信用] 计算卖家信用评分失败，已跳过: {exc}")
            return None

    def _build_score(self, job: ItemAnalysisJob, item_data: dict, analysis: dict) -> dict | None:
        """计算融合评分；任何异常都只放弃评分，不影响已完成的 AI 分析。"""
        try:
            from src.services.analysis_scoring_bridge import compute_analysis_score
            from src.services.price_history_service import parse_price_value

            reference_price = None
            try:
                insights = build_price_history_insights(job.keyword)
                # 用中位数而非均价（median_price 是 build_price_history_insights 的
                # 实际字段名）：历史样本里常混着配件残件与标错价的诱饵帖，
                # 均价会被离群值拉偏，导致"超值"误判。
                reference_price = (insights.get("market_summary") or {}).get("median_price")
            except Exception:
                reference_price = None

            return compute_analysis_score(
                ai_analysis=analysis,
                keyword_hit_count=int(analysis.get("keyword_hit_count") or 0),
                reference_price=reference_price,
                current_price=parse_price_value(item_data.get("当前售价")),
            )
        except Exception as exc:
            print(f"   [评分] 计算融合评分失败，已跳过: {exc}")
            return None

    def _build_profit(self, job: ItemAnalysisJob, item_data: dict) -> dict | None:
        """估算「低价买入再转卖」的利润；异常只放弃该项，不影响落盘。

        **参考样本必须排除当前商品自身。** 当前商品已经写进 price_snapshots，
        若把它算进参考价，就成了「拿自己跟自己比」——转卖价恒等于买入价，
        利润永远为 0 或负手续费，估算彻底失去意义。
        对比维度用 ``item_id`` 而不是价格：同一商品在多轮采集里价格会变，
        按价格去重会把同一件货的不同快照当成多个样本。
        """
        try:
            from src.services.price_history_service import (
                load_price_snapshots,
                parse_price_value,
            )
            from src.services.profit_service import estimate_profit

            buy_price = parse_price_value(item_data.get("当前售价"))
            if buy_price is None:
                return None

            current_item_id = str(
                item_data.get("商品ID") or (item_data.get("商品信息") or {}).get("商品ID") or ""
            )
            reference_prices: list[float] = []
            seen_ids: set[str] = set()
            for snapshot in load_price_snapshots(job.keyword):
                item_id = str(snapshot.get("item_id") or "")
                if item_id and item_id == current_item_id:
                    continue
                # 同一商品多轮快照只取一条，避免热门商品重复计入
                if item_id:
                    if item_id in seen_ids:
                        continue
                    seen_ids.add(item_id)
                price = parse_price_value(snapshot.get("price"))
                if price is not None and price > 0:
                    reference_prices.append(float(price))

            return estimate_profit(
                buy_price=buy_price,
                reference_prices=reference_prices,
            )
        except Exception as exc:
            print(f"   [利润] 估算转卖利润失败，已跳过: {exc}")
            return None

    async def _load_seller_info(self, job: ItemAnalysisJob) -> dict:
        seller_info = {}
        if job.seller_id:
            try:
                seller_info = await self._seller_loader(job.seller_id)
            except Exception as exc:
                print(f"   [卖家] 采集卖家 {job.seller_id} 信息失败: {exc}")
        merged = copy.deepcopy(seller_info or {})
        merged["卖家芝麻信用"] = job.zhima_credit_text
        merged["卖家注册时长"] = job.registration_duration_text
        return merged

    async def _build_analysis_result(self, job: ItemAnalysisJob, record: dict) -> dict:
        if job.decision_mode == "keyword":
            return self._build_keyword_result(job, record)
        if self._skip_ai_analysis:
            return self._build_skip_ai_result()
        return await self._run_ai_analysis(job, record)

    def _build_keyword_result(self, job: ItemAnalysisJob, record: dict) -> dict:
        search_text = build_search_text(record)
        return evaluate_keyword_rules(list(job.keyword_rules), search_text)

    def _build_skip_ai_result(self) -> dict:
        return {
            "analysis_source": "ai",
            "is_recommended": True,
            "reason": "商品已跳过AI分析，直接通知",
            "keyword_hit_count": 0,
        }

    def _build_ai_error_result(self, reason: str, *, error: str = "") -> dict:
        payload = {
            "analysis_source": "ai",
            "is_recommended": False,
            "reason": reason,
            "keyword_hit_count": 0,
        }
        if error:
            payload["error"] = error
        return payload

    async def _run_ai_analysis(self, job: ItemAnalysisJob, record: dict) -> dict:
        image_paths: list[str] = []
        try:
            image_paths = await self._download_images(job, record)
            if not job.prompt_text:
                return self._build_ai_error_result("任务未配置AI prompt，跳过分析。")
            ai_result = await self._ai_analyzer(record, image_paths, job.prompt_text)
            if not ai_result:
                return self._build_ai_error_result(
                    "AI analysis returned None after retries.",
                    error="AI analysis returned None after retries.",
                )
            ai_result.setdefault("analysis_source", "ai")
            ai_result.setdefault("keyword_hit_count", 0)
            return ai_result
        except Exception as exc:
            return self._build_ai_error_result(
                f"AI分析异常: {exc}",
                error=str(exc),
            )
        finally:
            self._cleanup_images(image_paths)

    async def _download_images(self, job: ItemAnalysisJob, record: dict) -> list[str]:
        if not job.analyze_images:
            return []
        item_data = record.get("商品信息", {}) or {}
        image_urls = item_data.get("商品图片列表", [])
        if not image_urls:
            return []
        return await self._image_downloader(
            item_data["商品ID"],
            image_urls,
            job.task_name,
        )

    def _cleanup_images(self, image_paths: list[str]) -> None:
        for img_path in image_paths:
            try:
                if os.path.exists(img_path):
                    os.remove(img_path)
            except Exception as exc:
                print(f"   [图片] 删除图片文件时出错: {exc}")

    async def _notify_if_recommended(self, job, item_data: dict, analysis_result: dict) -> None:
        is_rec = bool(analysis_result.get("is_recommended"))
        mode = getattr(job, "notify_mode", "keyword")
        should_notify = is_rec
        price = None
        try:
            from src.services.price_history_service import parse_price_value
            info = item_data.get("商品信息") or item_data
            price = parse_price_value(info.get("当前售价")) or parse_price_value(item_data.get("当前售价"))
        except Exception:
            price = None
        limit = getattr(job, "notify_price_below", None)
        if mode == "price":
            should_notify = price is not None and limit is not None and price <= float(limit)
        elif mode == "both":
            should_notify = is_rec or (price is not None and limit is not None and price <= float(limit))
        if not should_notify:
            return

        # 跨任务去重：多个任务的关键词常重叠（「索尼 A7M4」与「索尼微单」会命中
        # 同一件商品），任务之间互不知情，于是同一商品在几分钟内被推 N 次。
        # 这里按商品 ID 做进程级去重，窗口内只推一次。
        # 去重表查询失败绝不能影响通知本身，因此整段包 try。
        item_id = str(item_data.get("商品ID") or (item_data.get("商品信息") or {}).get("商品ID") or "")
        deduper = _shared_deduper()
        try:
            if not deduper.should_notify(item_id, task_name=job.task_name, event_type="recommend"):
                print(f"   [通知] 商品 {item_id or '(无ID)'} 已在去重窗口内推送过，跳过本次通知")
                return
        except Exception as exc:
            print(f"   [通知] 去重检查失败，按未去重处理: {exc}")

        reason = analysis_result.get("reason", "无")
        if mode == "price" and not is_rec:
            reason = f"价格 ¥{price:,.0f} 低于提醒价 ¥{float(limit):,.0f}"
        try:
            await self._notifier(item_data, reason)
        except Exception as exc:
            print(f"   [通知] 发送推荐通知失败: {exc}")
            return
        # 推送成功后才登记，这样推送失败的条目下一轮还能补推
        try:
            deduper.mark_notified(item_id, task_name=job.task_name, event_type="recommend")
        except Exception as exc:
            print(f"   [通知] 去重登记失败（可能导致下一轮重复推送）: {exc}")