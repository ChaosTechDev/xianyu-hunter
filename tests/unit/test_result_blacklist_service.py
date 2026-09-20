from src.services.result_blacklist_service import match_blacklist_keywords


def _build_record(title: str) -> dict:
    return {
        "商品信息": {
            "商品标题": title,
        },
        "卖家信息": {},
    }


def test_regex_blacklist_rule_matches_aliases_case_insensitively():
    keywords = [r"re:\b(pm|pro[\s-]?max)\b"]

    assert match_blacklist_keywords(_build_record("iPhone 15 Pm 256G"), keywords) == keywords
    assert match_blacklist_keywords(_build_record("iPhone 15 Pro Max 256G"), keywords) == keywords
    assert match_blacklist_keywords(_build_record("iPhone 15 promax 256G"), keywords) == keywords
    assert match_blacklist_keywords(_build_record("iPhone 15 pro-max 256G"), keywords) == keywords


def test_regex_blacklist_rule_does_not_hide_plain_pro_models():
    keywords = [r"re:\b(pm|pro[\s-]?max)\b"]

    assert match_blacklist_keywords(_build_record("iPhone 15 Pro 256G"), keywords) == []
