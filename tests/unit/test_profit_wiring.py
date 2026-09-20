"""利润估算接线测试。

这个文件存在的理由：``profit_service.estimate_profit`` 曾经写了完整的实现
（含中位数、样本量下限、安全边际），但**生产代码里零调用**——只有单元测试在调它。
测试全绿，功能从没跑过。这是「看起来做了、实际没用上」的典型形态，
所以这里专门验证「分析完一件商品后，record 里真的出现利润字段」。

覆盖两个关键正确性点：
1. 参考样本必须排除当前商品自身，否则转卖价恒等于买入价；
2. 样本不足时必须 ``estimated=False``，不能给出一个看着精确的假数字。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

import pytest

from src.services.item_analysis_dispatcher import (  # noqa: E402
    ItemAnalysisDispatcher,
    ItemAnalysisJob,
)


def _run_job(monkeypatch, job: ItemAnalysisJob, snapshots: list[dict]) -> dict:
    """跑一件商品，返回落盘后的 record。"""
    saved: list[dict] = []

    monkeypatch.setattr(
        "src.services.price_history_service.load_price_snapshots",
        lambda keyword: list(snapshots),
    )

    async def seller_loader(user_id: str):
        return {}

    async def image_downloader(product_id, image_urls, task_name):
        return []

    async def ai_analyzer(record, image_paths, prompt_text):
        return {
            "analysis_source": "ai",
            "is_recommended": True,
            "reason": "ok",
            "keyword_hit_count": 0,
        }

    async def notifier(item_data, reason):
        return None

    async def saver(record: dict, keyword: str):
        saved.append(record)
        return True

    async def run():
        dispatcher = ItemAnalysisDispatcher(
            concurrency=1,
            skip_ai_analysis=False,
            seller_loader=seller_loader,
            image_downloader=image_downloader,
            ai_analyzer=ai_analyzer,
            notifier=notifier,
            saver=saver,
        )
        dispatcher.submit(job)
        await dispatcher.join()

    asyncio.run(run())
    assert len(saved) == 1
    return saved[0]


def _snapshot(item_id: str, price: float) -> dict:
    return {
        "snapshot_time": "2026-05-01T00:00:00",
        "snapshot_day": "2026-05-01",
        "run_id": "r1",
        "task_name": "T",
        "keyword": "demo",
        "item_id": item_id,
        "title": f"item {item_id}",
        "price": price,
    }


def _job(item_id: str = "self-1", price: str = "1000") -> ItemAnalysisJob:
    return ItemAnalysisJob(
        keyword="demo",
        task_name="Demo",
        decision_mode="ai",
        analyze_images=False,
        prompt_text="prompt",
        keyword_rules=(),
        final_record={
            "商品信息": {"商品ID": item_id, "当前售价": price},
        },
        seller_id="seller-1",
        zhima_credit_text="极好",
        registration_duration_text="3年",
    )


class TestProfitIsActuallyWired:
    def test_record_gets_profit_field(self, monkeypatch):
        """核心断言：分析完的商品 record 里必须出现利润估算。"""
        record = _run_job(
            monkeypatch,
            _job(),
            [_snapshot("a", 1500), _snapshot("b", 1600), _snapshot("c", 1700)],
        )
        assert "利润估算" in record, "利润估算没有接线——又是死代码"

    def test_estimated_true_with_enough_samples(self, monkeypatch):
        record = _run_job(
            monkeypatch,
            _job(price="1000"),
            [_snapshot("a", 2000), _snapshot("b", 2100), _snapshot("c", 2200)],
        )
        profit = record["利润估算"]
        assert profit["estimated"] is True
        assert profit["resale_price"] > 1000
        assert profit["net_profit"] > 0


class TestSelfIsExcludedFromReference:
    def test_self_snapshot_is_not_used_as_reference(self, monkeypatch):
        """当前商品已在快照表里，必须排除，否则转卖价恒等于买入价。

        构造：自己 1000 元，另有 3 个同类样本 2000/2100/2200。
        若不自排除，中位数会被自己拉低，利润被系统性低估。

        注意 ``resale_price`` 是**中位数乘安全边际**（默认 0.9）后的保守值，
        不是裸中位数：2100 × 0.9 = 1890。
        """
        record = _run_job(
            monkeypatch,
            _job(item_id="self-1", price="1000"),
            [
                _snapshot("self-1", 1000),  # 自己
                _snapshot("a", 2000),
                _snapshot("b", 2100),
                _snapshot("c", 2200),
            ],
        )
        profit = record["利润估算"]
        assert profit["estimated"] is True
        # 中位数 2100，安全边际 0.9 -> 1890
        assert profit["resale_price"] == pytest.approx(2100 * 0.9)

    def test_exclusion_is_provable_via_sample_shortfall(self, monkeypatch):
        """用「样本量」反证自排除确实生效。

        构造：自己 1000 + 两个同类 2000/2200，共 3 条快照。
        默认样本下限是 3。若**不**排除自己，样本数正好 3，会算出「可估算」；
        排除自己后只剩 2 条，低于下限，必须报「样本不足」。
        因此 ``estimated=False`` 正是自排除生效的可观测证据。
        """
        record = _run_job(
            monkeypatch,
            _job(item_id="self-1", price="1000"),
            [_snapshot("self-1", 1000), _snapshot("a", 2000), _snapshot("c", 2200)],
        )
        profit = record["利润估算"]
        assert profit["estimated"] is False
        assert "样本不足" in profit["reason"]

    def test_three_real_references_estimate_successfully(self, monkeypatch):
        """补上一条对照组：把样本补到 3 条真实参考，就应该能估算。"""
        record = _run_job(
            monkeypatch,
            _job(item_id="self-1", price="1000"),
            [
                _snapshot("self-1", 1000),
                _snapshot("a", 2000),
                _snapshot("b", 2100),
                _snapshot("c", 2200),
            ],
        )
        assert record["利润估算"]["estimated"] is True

    def test_all_self_snapshots_yield_not_estimated(self, monkeypatch):
        """只有自己的快照时，没有参考样本，必须明确不可估算。"""
        record = _run_job(
            monkeypatch,
            _job(item_id="self-1", price="1000"),
            [_snapshot("self-1", 1000), _snapshot("self-1", 900)],
        )
        profit = record["利润估算"]
        assert profit["estimated"] is False
        assert profit["net_profit"] is None


class TestSamplesAreDedupedByItemId:
    def test_repeated_snapshots_of_same_item_count_once(self, monkeypatch):
        """同一商品多轮采集会留多条快照，只应算一个样本。

        否则一件热门商品的多次快照会淹没真实的市场分布。
        """
        record = _run_job(
            monkeypatch,
            _job(item_id="self-1", price="1000"),
            [
                _snapshot("a", 2000),
                _snapshot("a", 2000),  # 同一件货的第二轮
                _snapshot("a", 2000),
                _snapshot("b", 3000),
                _snapshot("c", 4000),
            ],
        )
        # 去重后是 2000/3000/4000，中位数 3000；若没去重则中位数是 2000。
        # 返回的是中位数乘安全边际：3000 × 0.9 = 2700。
        assert record["利润估算"]["resale_price"] == pytest.approx(3000 * 0.9)


class TestDegradesGracefully:
    def test_missing_price_skips_profit(self, monkeypatch):
        """当前售价解析不出来时跳过，不写字段也不报错。"""
        job = _job()
        job.final_record["商品信息"]["当前售价"] = "面议"
        record = _run_job(monkeypatch, job, [_snapshot("a", 2000)])
        assert "利润估算" not in record

    def test_snapshot_loader_failure_does_not_break_save(self, monkeypatch):
        """快照读取失败绝不能影响落盘——利润只是附加信息。"""
        saved: list[dict] = []

        def boom(keyword):
            raise RuntimeError("db down")

        monkeypatch.setattr(
            "src.services.price_history_service.load_price_snapshots", boom
        )

        async def seller_loader(user_id):
            return {}

        async def image_downloader(product_id, image_urls, task_name):
            return []

        async def ai_analyzer(record, image_paths, prompt_text):
            return {"analysis_source": "ai", "is_recommended": True, "reason": "ok",
                    "keyword_hit_count": 0}

        async def notifier(item_data, reason):
            return None

        async def saver(record, keyword):
            saved.append(record)
            return True

        async def run():
            dispatcher = ItemAnalysisDispatcher(
                concurrency=1,
                skip_ai_analysis=False,
                seller_loader=seller_loader,
                image_downloader=image_downloader,
                ai_analyzer=ai_analyzer,
                notifier=notifier,
                saver=saver,
            )
            dispatcher.submit(_job())
            await dispatcher.join()

        asyncio.run(run())
        assert len(saved) == 1, "快照失败导致结果没落盘"
        assert "利润估算" not in saved[0]
