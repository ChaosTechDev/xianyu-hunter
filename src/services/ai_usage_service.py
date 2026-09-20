from __future__ import annotations

import os
from datetime import datetime, timedelta

from src.infrastructure.persistence.sqlite_connection import sqlite_connection
from src.infrastructure.persistence.sqlite_bootstrap import bootstrap_sqlite_storage


def _usage_value(usage, name: str):
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(name)
    return getattr(usage, name, None)


def record_ai_usage(response, *, model: str, request_type: str) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    input_tokens = int(_usage_value(usage, "prompt_tokens") or _usage_value(usage, "input_tokens") or 0)
    output_tokens = int(_usage_value(usage, "completion_tokens") or _usage_value(usage, "output_tokens") or 0)
    cache_hit = _usage_value(usage, "prompt_cache_hit_tokens")
    cache_miss = _usage_value(usage, "prompt_cache_miss_tokens")
    if cache_hit is None:
        details = _usage_value(usage, "prompt_tokens_details")
        cache_hit = _usage_value(details, "cached_tokens")
    cache_hit = int(cache_hit) if cache_hit is not None else None
    cache_miss = int(cache_miss) if cache_miss is not None else None

    estimated_cost = None
    if "deepseek" in str(model).lower():
        hit_rate = float(os.getenv("DEEPSEEK_CACHE_HIT_COST_PER_MILLION", "0.028"))
        miss_rate = float(os.getenv("DEEPSEEK_CACHE_MISS_COST_PER_MILLION", "0.28"))
        output_rate = float(os.getenv("DEEPSEEK_OUTPUT_COST_PER_MILLION", "0.42"))
        hit = cache_hit or 0
        miss = cache_miss if cache_miss is not None else max(0, input_tokens - hit)
        estimated_cost = round((hit * hit_rate + miss * miss_rate + output_tokens * output_rate) / 1_000_000, 8)

    bootstrap_sqlite_storage()
    with sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO ai_usage_stats(
                model, request_type, input_tokens, output_tokens,
                cache_hit_tokens, cache_miss_tokens, estimated_cost, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(model or "unknown"), request_type, input_tokens, output_tokens,
                cache_hit, cache_miss, estimated_cost, datetime.now().isoformat(),
            ),
        )
        conn.commit()


def get_ai_usage_summary(days: int = 30) -> dict:
    bootstrap_sqlite_storage()
    since = (datetime.now() - timedelta(days=max(1, days))).isoformat()
    with sqlite_connection() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) requests, COALESCE(SUM(input_tokens), 0) input_tokens,
                   COALESCE(SUM(output_tokens), 0) output_tokens,
                   SUM(cache_hit_tokens) cache_hit_tokens,
                   SUM(cache_miss_tokens) cache_miss_tokens,
                   SUM(estimated_cost) estimated_cost
            FROM ai_usage_stats WHERE created_at >= ?
            """,
            (since,),
        ).fetchone()
        daily = conn.execute(
            """
            SELECT substr(created_at, 1, 10) day, model, COUNT(*) requests,
                   SUM(input_tokens) input_tokens, SUM(output_tokens) output_tokens,
                   SUM(cache_hit_tokens) cache_hit_tokens,
                   SUM(cache_miss_tokens) cache_miss_tokens,
                   SUM(estimated_cost) estimated_cost
            FROM ai_usage_stats WHERE created_at >= ?
            GROUP BY day, model ORDER BY day DESC, model
            """,
            (since,),
        ).fetchall()
    result = dict(totals)
    hit = result.get("cache_hit_tokens")
    miss = result.get("cache_miss_tokens")
    result["cache_supported"] = hit is not None or miss is not None
    denominator = (hit or 0) + (miss or 0)
    result["cache_hit_rate"] = round((hit or 0) / denominator * 100, 2) if denominator else None
    result["daily"] = [dict(row) for row in daily]
    return result
