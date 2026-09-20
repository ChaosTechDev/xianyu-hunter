"""商品分析分发器的落地接入测试：融合评分 + 跨任务通知去重。

两个模块（``analysis_scoring_bridge``、``notification_dedup_service``）单独测过，
但**单独测过不等于接上了**。本项目已有先例：``xy_protocol`` 三个模块写了 75 个
测试却从未被任何生产路径调用。本文件专门守住「真的接进主流程」这件事：

1. 每条落盘记录都应带 ``评分`` 字段，且一票否决能在真实链路里生效。
2. 同一商品被多个任务命中时只推送一次；不同商品互不干扰。
3. 评分/去重自身出问题时**绝不能**拖垮 AI 分析与落盘主流程。
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

# 分发器落盘与评分会触碰 SQLite，指向临时库以免污染项目 data/
os.environ.setdefault("APP_DATABASE_FILE", os.path.join(tempfile.mkdtemp(), "dispatcher_test.sqlite3"))

from src.services.item_analysis_dispatcher import (  # noqa: E402
    ItemAnalysisDispatcher,
    ItemAnalysisJob,
)


def _criteria(**statuses: str) -> dict:
    return {"criteria_analysis": {k: {"status": v} for k, v in statuses.items()}}


def _job(*, task_name: str = "任务A", item_id: str = "ITEM_1", ai_analysis: dict | None = None,
         seller_id: str | None = "SELLER_1"):
    return ItemAnalysisJob(
        keyword="测试关键词",
        task_name=task_name,
        decision_mode="ai",
        analyze_images=False,
        prompt_text="prompt",
        keyword_rules=(),
        final_record={"商品信息": {"商品ID": item_id, "商品标题": "测试商品", "当前售价": "3500"}},
        seller_id=seller_id,
        zhima_credit_text=None,
        registration_duration_text="",
    ), ai_analysis


class _Harness:
    """收集落盘记录与通知调用，其余依赖全部桩掉。"""

    def __init__(self, ai_analysis: dict | None = None, *, notify_exc: Exception | None = None):
        self.saved: list[dict] = []
        self.notified: list[str] = []
        self.ai_analysis = ai_analysis or _criteria(model_chip="PASS")
        self.notify_exc = notify_exc
        # 默认给一份可用的卖家资料，让卖家信用评分有数据可算
        self.seller_info: dict = {"卖家信用等级": "卖家信用极好"}

    async def saver(self, record: dict, keyword: str) -> bool:
        self.saved.append(record)
        return True

    async def notifier(self, payload: dict, reason: str) -> None:
        if self.notify_exc is not None:
            raise self.notify_exc
        self.notified.append(str(payload.get("商品ID") or ""))

    async def seller_loader(self, seller_id: str) -> dict:
        return dict(self.seller_info)

    async def image_downloader(self, item_id: str, urls: list, task_name: str) -> list:
        return []

    async def analyzer(self, record: dict, paths: list, prompt: str) -> dict | None:
        return dict(self.ai_analysis, is_recommended=True, reason="ok", keyword_hit_count=3)

    def run(self, jobs: list[ItemAnalysisJob]) -> None:
        async def main() -> None:
            dispatcher = ItemAnalysisDispatcher(
                concurrency=1,
                skip_ai_analysis=False,
                seller_loader=self.seller_loader,
                image_downloader=self.image_downloader,
                ai_analyzer=self.analyzer,
                notifier=self.notifier,
                saver=self.saver,
            )
            for job in jobs:
                dispatcher.submit(job)
            await dispatcher.join()

        asyncio.run(main())


@pytest.fixture(autouse=True)
def _fresh_deduper(monkeypatch):
    """每个用例拿一个全新的去重器，避免用例之间互相干扰。"""
    import src.services.item_analysis_dispatcher as mod
    from src.services.notification_dedup_service import CrossTaskNotificationDeduper

    monkeypatch.setattr(mod, "_SHARED_DEDUPER", CrossTaskNotificationDeduper(window_seconds=3600))


class TestScoringIsWired:
    def test_record_carries_score(self):
        harness = _Harness()
        harness.run([_job()[0]])
        assert len(harness.saved) == 1
        record = harness.saved[0]
        assert "评分" in record, "落盘记录必须带评分，否则评分模块等于没接"
        score = record["评分"]
        assert set(score) >= {"score", "components", "degraded", "vetoed"}
        assert 0.0 <= score["score"] <= 100.0

    def test_veto_takes_effect_through_real_pipeline(self):
        """一票否决必须在真实链路里封顶，而不是只在单元测试里成立。"""
        harness = _Harness(_criteria(model_chip="FAIL", condition="PASS"))
        harness.run([_job()[0]])
        score = harness.saved[0]["评分"]
        assert score["vetoed"] is True
        assert score["ai_failures"] == ["model_chip"]
        assert score["score"] <= 30.0, "被否决的商品不能被其他维度拉回及格线"

    def test_passing_item_scores_higher_than_vetoed(self):
        passing = _Harness(_criteria(model_chip="PASS", condition="PASS"))
        passing.run([_job(item_id="PASS_ITEM")[0]])
        vetoed = _Harness(_criteria(model_chip="FAIL", condition="PASS"))
        vetoed.run([_job(item_id="FAIL_ITEM")[0]])
        assert passing.saved[0]["评分"]["score"] > vetoed.saved[0]["评分"]["score"]

    def test_scoring_failure_does_not_break_saving(self, monkeypatch):
        """评分模块抛异常时必须只放弃评分，落盘与通知照常。"""
        import src.services.analysis_scoring_bridge as bridge

        def boom(*args, **kwargs):
            raise RuntimeError("scoring exploded")

        monkeypatch.setattr(bridge, "compute_analysis_score", boom)
        harness = _Harness()
        harness.run([_job()[0]])
        assert len(harness.saved) == 1, "评分失败不能阻止落盘"
        assert "评分" not in harness.saved[0]
        assert harness.notified == ["ITEM_1"], "评分失败不能阻止通知"


class TestCrossTaskDedupIsWired:
    def test_same_item_across_two_tasks_notifies_once(self):
        harness = _Harness()
        harness.run([_job(task_name="任务A", item_id="SAME_ITEM")[0]])
        harness.run([_job(task_name="任务B", item_id="SAME_ITEM")[0]])
        assert harness.notified == ["SAME_ITEM"], "同一商品被两个任务命中应只推一次"
        assert len(harness.saved) == 2, "去重只影响通知，落盘不应被跳过"

    def test_different_items_both_notify(self):
        harness = _Harness()
        harness.run([
            _job(task_name="任务A", item_id="ITEM_AAA")[0],
            _job(task_name="任务B", item_id="ITEM_BBB")[0],
        ])
        assert sorted(harness.notified) == ["ITEM_AAA", "ITEM_BBB"]

    def test_failed_notification_is_not_marked_so_it_can_retry(self):
        """推送失败时不登记去重，下一轮还能补推（这是刻意选择的语义）。"""
        failing = _Harness(notify_exc=RuntimeError("webhook 超时"))
        failing.run([_job(item_id="RETRY_ITEM")[0]])
        assert failing.notified == []

        healthy = _Harness()
        healthy.run([_job(item_id="RETRY_ITEM")[0]])
        assert healthy.notified == ["RETRY_ITEM"], "上次推送失败不应导致本次被去重吞掉"

    def test_item_without_id_uses_fallback_key_and_does_not_block_others(self):
        """缺 ID 的商品不能把其他缺 ID 商品一起误杀。"""
        harness = _Harness()
        job_no_id, _ = _job(task_name="任务A", item_id="")
        job_other, _ = _job(task_name="任务B", item_id="")
        harness.run([job_no_id, job_other])
        assert harness.notified == ["", ""], "不同任务的缺 ID 商品应各自推送"

    def test_deduper_failure_does_not_block_notification(self, monkeypatch):
        """去重器自身异常时按未去重处理，宁可多推也不能漏推。"""
        import src.services.item_analysis_dispatcher as mod

        class Boom:
            def should_notify(self, *a, **k):
                raise RuntimeError("deduper exploded")

            def mark_notified(self, *a, **k):
                raise RuntimeError("deduper exploded")

        monkeypatch.setattr(mod, "_SHARED_DEDUPER", Boom())
        harness = _Harness()
        harness.run([_job(item_id="BOOM_ITEM")[0]])
        assert harness.notified == ["BOOM_ITEM"]


class TestSellerCreditIsWired:
    """卖家信用评分必须真的落到记录里，而不只是模块存在。"""

    def test_record_carries_seller_credit(self):
        harness = _Harness()
        harness.seller_info = {
            "卖家信用等级": "卖家信用极好",
            "作为卖家的好评率": "99.8%",
            "卖家收到的评价总数": "1,234",
            "卖家注册时长": "来闲鱼 7 年",
            "卖家在售/已售商品数": "15",
        }
        harness.run([_job()[0]])
        record = harness.saved[0]
        assert "卖家信用评分" in record, "卖家信用评分未接入落盘记录"
        credit = record["卖家信用评分"]
        assert credit["score"] == pytest.approx(100.0)
        assert credit["level"] == "excellent"
        assert credit["vetoed"] is False

    def test_low_credit_seller_is_vetoed_in_record(self):
        harness = _Harness()
        harness.seller_info = {
            "卖家信用等级": "卖家信用一般",
            "作为卖家的好评率": "90%",
            "卖家在售/已售商品数": "900",
        }
        harness.run([_job()[0]])
        credit = harness.saved[0]["卖家信用评分"]
        assert credit["vetoed"] is True
        assert any("商家" in flag for flag in credit["risk_flags"])

    def test_missing_seller_data_does_not_fabricate_zero_credit(self):
        """卖家字段全缺时 score 必须是 None，不能写成 0 分。"""
        harness = _Harness()
        harness.seller_info = {}
        harness.run([_job()[0]])
        credit = harness.saved[0]["卖家信用评分"]
        assert credit["score"] is None
        assert credit["vetoed"] is False

    def test_seller_credit_failure_does_not_break_saving(self, monkeypatch):
        import src.services.seller_credit_service as mod

        def boom(*args, **kwargs):
            raise RuntimeError("credit exploded")

        monkeypatch.setattr(mod, "score_seller", boom)
        harness = _Harness()
        harness.run([_job()[0]])
        assert len(harness.saved) == 1, "信用评分失败不能阻止落盘"
        assert "卖家信用评分" not in harness.saved[0]
        assert harness.notified == ["ITEM_1"], "信用评分失败不能阻止通知"


class TestPriceModeUnaffected:
    def test_price_mode_still_notifies_without_ai_recommendation(self):
        """去重接入不能破坏原有的价格模式通知逻辑。"""
        harness = _Harness()
        job, _ = _job(item_id="PRICE_ITEM")
        job = ItemAnalysisJob(
            **{**job.__dict__, "notify_mode": "price", "notify_price_below": 5000.0}
        )
        harness.ai_analysis = _criteria(model_chip="FAIL")  # AI 不推荐
        harness.run([job])
        assert harness.notified == ["PRICE_ITEM"], "价格低于提醒价时应仍然通知"
