#!/usr/bin/env python3
"""
gateway_spend.py - Predict/now-cast daily AI Gateway spend against a budget.

Computes exact spend from local Claude Code transcripts (~/.claude/projects/*/*.jsonl)
aligned to a midnight-to-midnight UTC reset cycle, using a configurable pricing
catalog (base model rates + flat gateway markup), including 1-hour prompt cache TTL rates.
"""

import math
import os
import glob
import json
import argparse
import datetime
from typing import Dict, Any, Optional, Tuple, List

# AI Gateway Pricing Model
# Uses standard base rates + clean 10% gateway markup, with uniform cache write/read across TTLs.
# Rates in USD per 1,000,000 tokens
GATEWAY_MARKUP = 1.10  # 10% gateway fee

BASE_RATES = {
    "opus": {
        "input": 6.00,
        "output": 30.00,
        "cache_read": 0.60,
        "cache_write_5m": 7.50,
        "cache_write_1h": 7.50,
    },
    "sonnet": {
        "input": 3.00,
        "output": 15.00,
        "cache_read": 0.30,
        "cache_write_5m": 3.75,
        "cache_write_1h": 3.75,
    },
    "haiku": {
        "input": 0.80,
        "output": 4.00,
        "cache_read": 0.08,
        "cache_write_5m": 1.00,
        "cache_write_1h": 1.00,
    },
}

# Effective catalog rates with 10% gateway surcharge applied
CATALOG_RATES = {
    tier: {k: v * GATEWAY_MARKUP for k, v in rates.items()}
    for tier, rates in BASE_RATES.items()
}

DEFAULT_BUDGET_LIMIT = 50.00


def get_model_tier(model_name: str) -> str:
    """Classify model string into pricing tier ('opus', 'sonnet', or 'haiku')."""
    m = (model_name or "").lower()
    if "opus" in m:
        return "opus"
    if "haiku" in m:
        return "haiku"
    return "sonnet"


def get_rates_for_model(model_name: str) -> Dict[str, float]:
    """Retrieve robust rates for a model tier."""
    tier = get_model_tier(model_name)
    return CATALOG_RATES.get(tier, CATALOG_RATES["sonnet"])


def get_default_cache_ttl(settings_path: Optional[str] = None) -> str:
    """Inspect ~/.claude/settings.json for configured promptCacheTtl (default: '1h')."""
    if settings_path is None:
        settings_path = os.path.expanduser("~/.claude/settings.json")
    if os.path.exists(settings_path):
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("promptCacheTtl", "1h")
        except Exception:
            pass
    return "1h"


def compute_turn_cost(usage: Dict[str, Any], model_name: str, default_ttl: str = "1h") -> Tuple[float, Dict[str, int]]:
    """Compute USD cost and token breakdown for a single API turn."""
    rates = get_rates_for_model(model_name)

    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    c_read = usage.get("cache_read_input_tokens", 0)

    cc = usage.get("cache_creation") or {}
    c_1h = cc.get("ephemeral_1h_input_tokens", 0)
    c_5m = cc.get("ephemeral_5m_input_tokens", 0)

    # Fallback when ephemeral tiers are not explicitly split in payload
    if not c_1h and not c_5m:
        raw_cc = usage.get("cache_creation_input_tokens", 0)
        if default_ttl == "1h":
            c_1h = raw_cc
        else:
            c_5m = raw_cc

    cost = (
        (inp * rates["input"])
        + (out * rates["output"])
        + (c_read * rates["cache_read"])
        + (c_5m * rates["cache_write_5m"])
        + (c_1h * rates["cache_write_1h"])
    ) / 1_000_000.0

    tokens = {
        "input": inp,
        "output": out,
        "cache_read": c_read,
        "cache_write_5m": c_5m,
        "cache_write_1h": c_1h,
    }
    return cost, tokens


def calculate_gateway_spend(
    target_date: Optional[str] = None,
    until_timestamp: Optional[str] = None,
    claude_dir: Optional[str] = None,
    budget_limit: float = DEFAULT_BUDGET_LIMIT,
) -> Dict[str, Any]:
    """
    Now-cast gateway spend for target_date (YYYY-MM-DD in UTC, defaults to today).

    Reads ~/.claude/projects/*/*.jsonl and jobs/*/*.jsonl, deduplicates assistant
    records by message/request ID, applies catalog rates, and computes spend.
    """
    if claude_dir is None:
        claude_dir = os.path.expanduser("~/.claude")

    if target_date is None:
        if until_timestamp:
            target_date = until_timestamp[:10]
        else:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            target_date = now_utc.strftime("%Y-%m-%d")

    date_prefix = target_date
    default_ttl = get_default_cache_ttl(os.path.join(claude_dir, "settings.json"))

    projects_dir = os.path.join(claude_dir, "projects")
    jobs_dir = os.path.join(claude_dir, "jobs")
    jsonl_files = (
        glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True)
        + glob.glob(os.path.join(jobs_dir, "**", "*.jsonl"), recursive=True)
    )

    seen_ids = set()
    models_summary: Dict[str, Dict[str, Any]] = {}
    totals = {
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
    }
    total_cost = 0.0
    total_requests = 0

    # A file untouched since the target day began can't hold records from it.
    day_start = datetime.datetime.strptime(date_prefix, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc).timestamp()

    for file_path in jsonl_files:
        try:
            if os.path.getmtime(file_path) < day_start:
                continue
        except OSError:
            continue
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as fp:
                for line in fp:
                    if '"assistant"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except Exception:
                        continue

                    if record.get("type") != "assistant":
                        continue

                    ts = record.get("timestamp", "")
                    if not ts.startswith(date_prefix):
                        continue
                    if until_timestamp and ts > until_timestamp:
                        continue

                    msg = record.get("message") or {}
                    mid = msg.get("id") or record.get("requestId")
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)

                    model = msg.get("model", "unknown")
                    usage = msg.get("usage") or {}

                    cost, tokens = compute_turn_cost(usage, model, default_ttl=default_ttl)

                    total_cost += cost
                    total_requests += 1
                    for k in totals:
                        totals[k] += tokens[k]

                    if model not in models_summary:
                        models_summary[model] = {
                            "tier": get_model_tier(model),
                            "requests": 0,
                            "cost": 0.0,
                            "tokens": {
                                "input": 0,
                                "output": 0,
                                "cache_read": 0,
                                "cache_write_5m": 0,
                                "cache_write_1h": 0,
                            },
                        }
                    entry = models_summary[model]
                    entry["requests"] += 1
                    entry["cost"] += cost
                    for k in entry["tokens"]:
                        entry["tokens"][k] += tokens[k]

        except Exception:
            continue

    # Gateway dashboard UIs often ceil spend for card display (e.g. $5.38 -> $6)
    display_rounded = math.ceil(total_cost) if total_cost > 0 else 0
    pct_used = (total_cost / budget_limit * 100.0) if budget_limit > 0 else 0.0
    remaining = max(0.0, budget_limit - total_cost)

    return {
        "date_utc": target_date,
        "total_cost_usd": round(total_cost, 4),
        "display_rounded_usd": display_rounded,
        "budget_limit_usd": budget_limit,
        "budget_used_pct": round(pct_used, 1),
        "budget_remaining_usd": round(remaining, 2),
        "total_requests": total_requests,
        "totals": totals,
        "models": models_summary,
    }


def format_table(data: Dict[str, Any]) -> str:
    """Format now-cast results into a human-readable table."""
    lines = []
    lines.append(f"Gateway Now-Cast (Date: {data['date_utc']} UTC)")
    lines.append(
        f"Spend: ${data['total_cost_usd']:.2f} (~${data['display_rounded_usd']} on dashboard) "
        f"/ ${data['budget_limit_usd']:.2f} limit ({data['budget_used_pct']}% used, "
        f"${data['budget_remaining_usd']:.2f} remaining)"
    )
    lines.append(f"Delivered Requests: {data['total_requests']}")
    lines.append("")
    lines.append(
        f"{'Model':<20} {'Reqs':>5} {'Input':>10} {'Output':>10} {'Cache Read':>12} {'1h Write':>10} {'Cost (USD)':>11}"
    )
    lines.append("-" * 83)

    for model, info in sorted(data["models"].items()):
        tok = info["tokens"]
        lines.append(
            f"{model:<20} {info['requests']:>5d} {tok['input']:>10,d} {tok['output']:>10,d} "
            f"{tok['cache_read']:>12,d} {tok['cache_write_1h']:>10,d} ${info['cost']:>10.2f}"
        )

    lines.append("-" * 83)
    tot = data["totals"]
    lines.append(
        f"{'TOTAL':<20} {data['total_requests']:>5d} {tot['input']:>10,d} {tot['output']:>10,d} "
        f"{tot['cache_read']:>12,d} {tot['cache_write_1h']:>10,d} ${data['total_cost_usd']:>10.2f}"
    )
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Predict/now-cast daily AI Gateway spend up to right now."
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Date to evaluate in YYYY-MM-DD UTC (default: today UTC)",
    )
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="End timestamp cutoff in ISO format (e.g. 2026-11-02T21:05:00Z)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=DEFAULT_BUDGET_LIMIT,
        help="Daily budget limit in USD (default: 50.00)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--statusline",
        action="store_true",
        help="Output single compact string for statusLine hooks (e.g. '$6 / $50 (12%%)')",
    )

    args = parser.parse_args()
    data = calculate_gateway_spend(
        target_date=args.date,
        until_timestamp=args.until,
        budget_limit=args.budget,
    )

    if args.json:
        print(json.dumps(data, indent=2))
    elif args.statusline:
        print(f"${data['total_cost_usd']:.2f} / ${data['budget_limit_usd']:.0f} ({data['budget_used_pct']:.0f}%)")
    else:
        print(format_table(data))


if __name__ == "__main__":
    main()
