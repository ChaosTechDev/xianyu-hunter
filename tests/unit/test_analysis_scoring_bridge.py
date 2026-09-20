"""AI 定性结论 -> 定量评分 桥接层的测试。

覆盖三件最容易出错的事：
1. ``NEEDS_MANUAL_CHECK``（信息缺失）不能被误当成 ``FAIL``（明确不符合）。
   项目提示词明确规定信息缺失不直接否决，误判会大量误杀好商品。
2. 一票否决必须在**最终融合分**上生效，不能被其他维度的满分平均回来。
   这是实际踩到的缺陷：只在 AI 维度压分时，关键词与价格满分会把被否决的
   商品拉回及格线（实测 25 分的 AI 维度被拉到 56 分）。
3. 拿不到 ``criteria_analysis`` 时返回 ``None`` 走降级路径，不编造分数。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.services.analysis_scoring_bridge import (
    FAIL_CEILING,
    STATUS_FAIL,
    STATUS_PASS,
    STATUS_UNKNOWN,
    VETO_SCORE_CAP,
    compute_analysis_score,
    derive_ai_score,
)


def _criteria(**statuses: str) -> dict:
    """构造一份 criteria_analysis。"""
    return {"criteria_analysis": {name: {"status": v} for name, v in statuses.items()}}


class TestStatusClassification:
    def test_all_pass_gives_full_score_and_full_confidence(self):
        result = derive_ai_score(
            _criteria(model_chip="PASS", battery_health="PASS", history="PASS")
        )
        assert result["ai_score"] == 100.0
        assert result["ai_confidence"] == 1.0
        assert result["failures"] == []
        assert result["unknowns"] == []

    def test_needs_manual_check_is_not_treated_as_failure(self):
        """信息缺失必须与明确否决区分开——这是提示词的核心规则。"""
        result = derive_ai_score(
            _criteria(model_chip="PASS", battery_health="NEEDS_MANUAL_CHECK")
        )
        assert result["failures"] == [], "NEEDS_MANUAL_CHECK 不能算否决"
        assert result["unknowns"] == ["battery_health"]
        # 50 分而非 0 分：不能因为"卖家没写电池健康"就惩罚商品
        assert result["ai_score"] == pytest.approx(75.0)
        # 置信度应因存在未确认项而下降
        assert result["ai_confidence"] == pytest.approx(0.5)

    def test_failure_lowers_score_to_ceiling(self):
        result = derive_ai_score(
            _criteria(model_chip="FAIL", condition="PASS", history="PASS")
        )
        assert result["failures"] == ["model_chip"]
        assert result["ai_score"] == FAIL_CEILING

    @pytest.mark.parametrize(
        "raw_status",
        ["FAIL", "fail", "Failed", "REJECT", "not_pass", "not pass", "FAIL_NEEDS_REVIEW"],
    )
    def test_failure_token_variants_are_recognized(self, raw_status):
        """模型输出大小写/分隔符不稳定，这些写法都必须识别为否决。"""
        result = derive_ai_score(_criteria(model_chip=raw_status, condition="PASS"))
        assert result["failures"] == ["model_chip"], f"{raw_status!r} 应识别为否决"

    @pytest.mark.parametrize(
        "raw_status",
        ["NEEDS_MANUAL_CHECK", "Needs Manual Check", "needs-manual-check",
         "UNKNOWN", "uncertain", "MISSING", "not_mentioned"],
    )
    def test_unknown_token_variants_are_recognized(self, raw_status):
        result = derive_ai_score(_criteria(battery_health=raw_status, condition="PASS"))
        assert result["unknowns"] == ["battery_health"], f"{raw_status!r} 应识别为待查"
        assert result["failures"] == []

    def test_mixed_fail_and_unknown_prioritizes_fail(self):
        """混写时否决优先——宁可从严，不能把明确否决当成"待查"。"""
        result = derive_ai_score(
            _criteria(model_chip="FAIL_NEEDS_MANUAL_CHECK", condition="PASS")
        )
        assert result["failures"] == ["model_chip"]
        assert result["unknowns"] == []

    def test_boolean_status_is_tolerated(self):
        """模型偶尔把 status 直接写成布尔。"""
        result = derive_ai_score(_criteria(a=True, b=False))
        assert result["statuses"]["a"] == STATUS_PASS
        assert result["statuses"]["b"] == STATUS_FAIL
        assert result["failures"] == ["b"]

    def test_nested_analysis_details_are_not_treated_as_criteria(self):
        """seller_type 下的嵌套结构是论证过程，不应被当成独立评估项。"""
        analysis = {
            "criteria_analysis": {
                "seller_type": {
                    "status": "PASS",
                    "analysis_details": {
                        "temporal_analysis": {"comment": "x", "evidence": "y"},
                        "buying_behavior": {"comment": "x", "evidence": "y"},
                    },
                }
            }
        }
        result = derive_ai_score(analysis)
        assert result["ai_score"] == 100.0
        assert result["ai_confidence"] == 1.0, "嵌套项不应拉低置信度分母"
        assert result["statuses"] == {"seller_type": STATUS_PASS}


class TestDeriveEdgeCases:
    @pytest.mark.parametrize(
        "bad_input",
        [None, {}, "string", 123, [], {"criteria_analysis": None},
         {"criteria_analysis": {}}, {"criteria_analysis": "not a dict"},
         {"reason": "no criteria key"}],
    )
    def test_missing_criteria_returns_none_not_fabricated_score(self, bad_input):
        """拿不到评估项时必须返回 None（走降级），不能编造一个分数。"""
        result = derive_ai_score(bad_input)
        assert result["ai_score"] is None
        assert result["ai_confidence"] is None

    def test_statuses_without_status_key_are_ignored(self):
        result = derive_ai_score({"criteria_analysis": {"a": {"comment": "no status"}}})
        assert result["ai_score"] is None

    def test_confidence_has_floor_but_is_not_zero(self):
        """全部待查时置信度有下限，但绝不为 0——否则与"完全没分析"无法区分。"""
        result = derive_ai_score(_criteria(a="NEEDS_MANUAL_CHECK", b="UNKNOWN"))
        assert result["ai_confidence"] > 0.0
        assert result["ai_score"] == 50.0


class TestFusedScoring:
    def test_veto_caps_final_score_despite_full_other_dimensions(self):
        """核心回归护栏：一票否决必须封顶最终分。

        这是实际缺陷——只在 AI 维度压分时，关键词满分 + 价格满分会把
        被否决的商品拉回 56 分（及格线以上），让"硬性否决"在排序里失效。
        """
        vetoed = compute_analysis_score(
            ai_analysis=_criteria(model_chip="FAIL", condition="PASS"),
            keyword_hit_count=3,
            reference_price=5000,
            current_price=3500,
        )
        assert vetoed["vetoed"] is True
        assert vetoed["ai_failures"] == ["model_chip"]
        assert vetoed["score"] <= VETO_SCORE_CAP

        passing = compute_analysis_score(
            ai_analysis=_criteria(model_chip="PASS", condition="PASS"),
            keyword_hit_count=3,
            reference_price=5000,
            current_price=3500,
        )
        assert passing["vetoed"] is False
        assert passing["score"] > VETO_SCORE_CAP
        assert passing["score"] > vetoed["score"], "通过者必须排在否决者之前"

    def test_veto_applies_even_without_price_reference(self):
        """降级权重下否决同样必须封顶。"""
        result = compute_analysis_score(
            ai_analysis=_criteria(shipping="FAIL"),
            keyword_hit_count=3,
        )
        assert result["vetoed"] is True
        assert result["score"] <= VETO_SCORE_CAP
        assert result["degraded"] is True

    def test_risk_tags_are_taken_from_ai_result(self):
        """未显式传 risk_labels 时应回退到提示词定义的 risk_tags 字段。"""
        analysis = _criteria(model_chip="PASS")
        analysis["risk_tags"] = ["拆修", "拆修", "进水"]  # 故意重复
        result = compute_analysis_score(ai_analysis=analysis, keyword_hit_count=3)
        # 去重后 2 个标签 -> 扣 10 分
        assert result["risk_penalty"] == 10.0

    def test_explicit_risk_labels_override_ai_tags(self):
        analysis = _criteria(model_chip="PASS")
        analysis["risk_tags"] = ["来自AI"]
        result = compute_analysis_score(
            ai_analysis=analysis, keyword_hit_count=3, risk_labels=["来自调用方"]
        )
        assert result["risk_penalty"] == 5.0

    def test_price_dimension_needs_both_prices(self):
        only_current = compute_analysis_score(
            ai_analysis=_criteria(model_chip="PASS"),
            keyword_hit_count=3,
            current_price=3500,
        )
        assert only_current["degraded"] is True
        assert "price" not in only_current["available_dimensions"]

        both = compute_analysis_score(
            ai_analysis=_criteria(model_chip="PASS"),
            keyword_hit_count=3,
            reference_price=5000,
            current_price=3500,
        )
        assert both["degraded"] is False
        assert "price" in both["available_dimensions"]

    def test_zero_reference_price_does_not_divide_by_zero(self):
        result = compute_analysis_score(
            ai_analysis=_criteria(model_chip="PASS"),
            keyword_hit_count=3,
            reference_price=0,
            current_price=3500,
        )
        assert result["degraded"] is True
        assert result["score"] == result["score"], "分数不能是 NaN"

    def test_dirty_prices_fall_back_to_degraded(self):
        result = compute_analysis_score(
            ai_analysis=_criteria(model_chip="PASS"),
            keyword_hit_count=3,
            reference_price="abc",
            current_price="def",
        )
        assert result["degraded"] is True

    def test_no_ai_data_still_scores_keyword_and_marks_ai_absent(self):
        """AI 完全失败时仍应给出可用的关键词/价格评分，而不是整体失效。"""
        result = compute_analysis_score(
            ai_analysis=None,
            keyword_hit_count=3,
            reference_price=5000,
            current_price=3500,
        )
        assert "ai" not in result["available_dimensions"]
        assert "keyword" in result["available_dimensions"]
        assert result["score"] > 0.0
        assert result["vetoed"] is False, "无 AI 数据不等于被否决"

    def test_loss_for_sale_criteria_interpretation(self):
        """对照：两项全 PASS 且价格明显低于参考价时应接近满分。"""
        result = compute_analysis_score(
            ai_analysis=_criteria(model_chip="PASS", battery_health="PASS",
                                  condition="PASS", history="PASS"),
            keyword_hit_count=3,
            reference_price=5000,
            current_price=3000,  # ratio 0.6 < 0.7 -> 价格维度满分
        )
        assert result["score"] == pytest.approx(100.0)
        assert result["vetoed"] is False
