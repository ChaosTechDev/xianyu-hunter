"""
结果黑名单规则解析与匹配。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from src.keyword_rule_engine import build_search_text, normalize_text


_ASCII_TOKEN_KEYWORD_PATTERN = re.compile(r"^[a-z0-9 ]+$")
_ASCII_TOKEN_BOUNDARY = r"[a-z0-9]"
_KEYWORD_SPLIT_PATTERN = re.compile(r"[\n,，]+")
_REGEX_PREFIX = "re:"


def normalize_blacklist_keywords(values: Iterable[str] | str | None) -> list[str]:
    raw_values: list[str] = []
    if isinstance(values, str):
        raw_values.extend(_KEYWORD_SPLIT_PATTERN.split(values))
    else:
        for value in values or []:
            raw_values.extend(_KEYWORD_SPLIT_PATTERN.split(str(value)))

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        source_text = str(raw).strip()
        if not source_text:
            continue
        if source_text[: len(_REGEX_PREFIX)].lower() == _REGEX_PREFIX:
            pattern = source_text[len(_REGEX_PREFIX):].strip()
            if not pattern:
                continue
            text = f"{_REGEX_PREFIX}{pattern}"
        else:
            text = normalize_text(source_text)
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _is_regex_keyword(keyword: str) -> bool:
    return keyword[: len(_REGEX_PREFIX)].lower() == _REGEX_PREFIX


def _uses_ascii_token_match(keyword: str) -> bool:
    return bool(keyword) and _ASCII_TOKEN_KEYWORD_PATTERN.fullmatch(keyword) is not None


def _keyword_matches(keyword: str, normalized_text: str) -> bool:
    if _is_regex_keyword(keyword):
        pattern = keyword[len(_REGEX_PREFIX):]
        try:
            return re.search(pattern, normalized_text, flags=re.IGNORECASE) is not None
        except re.error:
            return False
    if not _uses_ascii_token_match(keyword):
        return keyword in normalized_text
    pattern = rf"(?<!{_ASCII_TOKEN_BOUNDARY}){re.escape(keyword)}(?!{_ASCII_TOKEN_BOUNDARY})"
    return re.search(pattern, normalized_text) is not None


def match_blacklist_keywords(record: dict[str, Any], keywords: Iterable[str] | str | None) -> list[str]:
    normalized_keywords = normalize_blacklist_keywords(keywords)
    if not normalized_keywords:
        return []

    search_text = normalize_text(build_search_text(record))
    if not search_text:
        return []

    return [keyword for keyword in normalized_keywords if _keyword_matches(keyword, search_text)]
