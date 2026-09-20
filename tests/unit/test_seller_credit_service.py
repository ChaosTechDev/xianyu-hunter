"""卖家信用评分的测试。

重点守住三类容易翻车的判断：

1. **「一无所知」不等于「信用为零」**。所有字段都缺失时 ``score`` 必须是
   ``None`` 而不是 ``0.0``——把缺数据当成低信用会把缺字段的卖家全部误杀。
2. **没见过的等级文案不能当成低信用**。平台改文案是常态，若把无法识别的
   文本判为差评，一次文案调整就会误杀全部卖家。
3. **硬性否决只在有证据时生效**。拿不到等级数据时不否决。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from src.services.seller_credit_service import (
    DEALER_LISTING_THRESHOLD,
    LEVEL_EXCELLENT,
    LEVEL_FAIR,
    LEVEL_GOOD,
    LEVEL_POOR,
    LEVEL_UNKNOWN,
    parse_count,
    parse_positive_rate,
    score_seller,
)


class TestParseCount:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("1.2万", 12000.0),
            ("2万", 20000.0),
            ("350", 350.0),
            ("1,234", 1234.0),
            ("１２３", 123.0),  # 全角数字
            ("500+", 500.0),
            ("2千", 2000.0),
            (42, 42.0),
            (42.5, 42.5),
        ],
    )
    def test_parses_real_formats(self, raw, expected):
        assert parse_count(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["", "   ", None, "abc", "万", True, False, "nan", "inf"])
    def test_returns_none_for_unparseable(self, raw):
        """解析失败必须返回 None 而不是 0——0 与「未知」在信用评估里含义相反。"""
        assert parse_count(raw) is None


class TestParsePositiveRate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("99.5%", 0.995),
            ("0.995", 0.995),
            (99.5, 0.995),
            ("95%", 0.95),
            ("100%", 1.0),
            ("0", 0.0),
        ],
    )
    def test_normalizes_to_unit_interval(self, raw, expected):
        assert parse_positive_rate(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", [None, "abc", "", True])
    def test_returns_none_for_garbage(self, raw):
        assert parse_positive_rate(raw) is None


class TestMissingDataIsNotZeroCredit:
    def test_all_fields_missing_gives_none_score_not_zero(self):
        result = score_seller({})
        assert result["score"] is None, "一无所知不能等于 0 分"
        assert result["level"] == LEVEL_UNKNOWN
        assert result["vetoed"] is False
        assert len(result["unknown_factors"]) == 4

    def test_none_input_is_tolerated(self):
        result = score_seller(None)
        assert result["score"] is None
        assert result["vetoed"] is False

    def test_non_dict_input_is_tolerated(self):
        result = score_seller("not a dict")
        assert result["score"] is None

    def test_partial_data_reweights_remaining_factors(self):
        """只有等级数据时，该因子权重归一化到 1.0，总分等于该因子分。"""
        result = score_seller({"卖家信用等级": "卖家信用极好"})
        assert result["score"] == pytest.approx(100.0)
        assert result["available_factors"] == ["credit_level"]
        assert result["factors"]["credit_level"]["normalized_weight"] == pytest.approx(1.0)
        assert result["factors"]["rating_volume"]["weighted"] is None


class TestCreditLevelClassification:
    @pytest.mark.parametrize(
        "text,expected_level",
        [
            ("卖家信用极好", LEVEL_EXCELLENT),
            ("卖家信用很好", LEVEL_GOOD),
            ("卖家信用良好", LEVEL_FAIR),
            ("卖家信用一般", LEVEL_POOR),
        ],
    )
    def test_known_levels_map_correctly(self, text, expected_level):
        assert score_seller({"卖家信用等级": text})["level"] == expected_level

    @pytest.mark.parametrize("text", ["暂无", "未知", "", None, "-"])
    def test_placeholder_text_is_treated_as_unknown_not_poor(self, text):
        result = score_seller({"卖家信用等级": text})
        assert result["level"] == LEVEL_UNKNOWN
        assert result["vetoed"] is False

    def test_unrecognized_text_is_not_treated_as_low_credit(self):
        """核心护栏：平台改文案时不能误杀全部卖家。"""
        result = score_seller({"卖家信用等级": "卖家信用AAA级"})
        assert result["level"] == LEVEL_UNKNOWN, "没见过的写法不能标成低信用"
        assert result["vetoed"] is False, "没见过的写法不能触发否决"
        assert result["score"] is not None, "仍应给出中性分而非 None"


class TestHardVeto:
    def test_poor_level_triggers_veto_with_reason(self):
        result = score_seller({"卖家信用等级": "卖家信用一般"})
        assert result["vetoed"] is True
        assert "卖家信用一般" in result["veto_reason"]

    def test_good_level_does_not_trigger_veto(self):
        """「很好」不否决——实测大量真实个人卖家是这个档位，否决过于激进。"""
        result = score_seller({"卖家信用等级": "卖家信用很好"})
        assert result["vetoed"] is False

    def test_excellent_level_does_not_trigger_veto(self):
        assert score_seller({"卖家信用等级": "卖家信用极好"})["vetoed"] is False

    def test_missing_level_never_triggers_veto(self):
        """缺证据不等于有罪：没拿到等级数据时不能否决。"""
        result = score_seller({"作为卖家的好评率": "99.9%"})
        assert result["vetoed"] is False


class TestRiskFlags:
    def test_high_listing_count_flags_dealer_suspicion(self):
        result = score_seller({"卖家在售/已售商品数": str(DEALER_LISTING_THRESHOLD + 500)})
        assert any("商家" in f for f in result["risk_flags"])

    def test_low_listing_count_does_not_flag(self):
        result = score_seller({"卖家在售/已售商品数": "12"})
        assert not any("商家" in f for f in result["risk_flags"])

    def test_low_positive_rate_flags_risk(self):
        result = score_seller({"作为卖家的好评率": "88%"})
        assert any("好评率" in f for f in result["risk_flags"])

    def test_good_rate_does_not_flag(self):
        result = score_seller({"作为卖家的好评率": "99.5%"})
        assert not any("好评率" in f for f in result["risk_flags"])

    def test_missing_listing_count_does_not_flag(self):
        """缺数据不能产生风险信号。"""
        assert score_seller({})["risk_flags"] == []


class TestScoringBehavior:
    def test_excellent_seller_scores_high(self):
        result = score_seller(
            {
                "卖家信用等级": "卖家信用极好",
                "作为卖家的好评率": "99.8%",
                "卖家收到的评价总数": "1,234",
                "卖家注册时长": "来闲鱼 7 年",
                "卖家在售/已售商品数": "15",
            }
        )
        assert result["score"] == pytest.approx(100.0)
        assert result["risk_flags"] == []

    def test_dealer_suspicion_lowers_score(self):
        """商品数异常多 + 账号新 + 好评率低于阈值，总分应明显低于优质个人卖家。"""
        good = score_seller(
            {
                "卖家信用等级": "卖家信用极好",
                "作为卖家的好评率": "99.8%",
                "卖家收到的评价总数": "1,234",
                "卖家注册时长": "来闲鱼 7 年",
            }
        )
        sketchy = score_seller(
            {
                "卖家信用等级": "卖家信用极好",
                # 必须低于 LOW_POSITIVE_RATE_THRESHOLD(95%) 才会触发风险信号；
                # 96% 虽然比 99.8% 低，但仍属正常范围，不应报风险。
                "作为卖家的好评率": "90%",
                "卖家收到的评价总数": "2.3万",
                "卖家注册时长": "来闲鱼 1 年",
                "卖家在售/已售商品数": str(DEALER_LISTING_THRESHOLD + 700),
            }
        )
        assert sketchy["score"] < good["score"]
        assert any("好评率" in f for f in sketchy["risk_flags"])
        assert any("商家" in f for f in sketchy["risk_flags"])

    def test_slightly_lower_rate_within_normal_range_is_not_flagged(self):
        """96% 仍属正常范围，不能因为「比满分低」就报风险。"""
        result = score_seller({"作为卖家的好评率": "96%"})
        assert not any("好评率" in f for f in result["risk_flags"])

    def test_score_always_within_bounds(self):
        for info in (
            {"卖家信用等级": "卖家信用极好", "作为卖家的好评率": "100%",
             "卖家收到的评价总数": "99999", "卖家注册时长": "来闲鱼 20 年"},
            {"卖家信用等级": "卖家信用一般", "作为卖家的好评率": "0%",
             "卖家收到的评价总数": "0", "卖家注册时长": "0"},
        ):
            score = score_seller(info)["score"]
            assert score is not None
            assert 0.0 <= score <= 100.0

    def test_dirty_values_do_not_crash_or_produce_nan(self):
        result = score_seller(
            {
                "卖家信用等级": 123,
                "作为卖家的好评率": float("nan"),
                "卖家收到的评价总数": float("inf"),
                "卖家注册时长": object(),
                "卖家在售/已售商品数": {"unexpected": "dict"},
            }
        )
        assert result["score"] is None or result["score"] == result["score"]

    def test_empty_string_rate_is_not_treated_as_zero_percent(self):
        """空字符串不能变成 0% 好评率——那是把「没写」当成「差评」。"""
        result = score_seller({"作为卖家的好评率": "", "卖家信用等级": "卖家信用极好"})
        assert "positive_rate" in result["unknown_factors"]
        assert not any("好评率" in f for f in result["risk_flags"])
