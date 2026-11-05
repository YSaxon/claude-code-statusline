#!/usr/bin/env python3
import sys
import os
import glob
import json
import datetime

# Read Claude Code hook payload from stdin
try:
    raw_in = sys.stdin.read()
    payload = json.loads(raw_in) if raw_in.strip() else {}
except Exception:
    payload = {}

# 1. Model display name with version string (e.g. Opus 4.8, Sonnet 5, Haiku 5.5)
model_info = payload.get("model") or {}
model_raw = model_info.get("id") or model_info.get("display_name") or "Claude"

def format_model_name(m):
    m = m.strip()
    if m.startswith("claude-"):
        m = m[7:]
    parts = m.split("-")
    tier = parts[0].capitalize()
    if len(parts) == 2:
        return f"{tier} {parts[1]}"
    elif len(parts) >= 3:
        version = ".".join(parts[1:])
        return f"{tier} {version}"
    return tier

model_name = format_model_name(model_raw)

# 2. Session cost (today only), context tokens, and last turn timestamp from transcript
now = datetime.datetime.now(datetime.timezone.utc)
today_prefix = now.strftime("%Y-%m-%d")

transcript_path = payload.get("transcript_path", "")
session_cost = 0.0
context_tokens = 0
last_turn_ts = None
seen_ids = set()

claude_dir = os.path.expanduser("~/.claude")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from gateway_spend import calculate_gateway_spend, compute_turn_cost
except ImportError:
    compute_turn_cost = None
    calculate_gateway_spend = None

if transcript_path and os.path.exists(transcript_path) and compute_turn_cost:
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                if '"assistant"' not in line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("type") == "assistant":
                        mid = (r.get("message") or {}).get("id") or r.get("requestId")
                        if mid and mid not in seen_ids:
                            seen_ids.add(mid)
                            u = (r.get("message") or {}).get("usage") or {}
                            m = (r.get("message") or {}).get("model", "")
                            ts = r.get("timestamp")
                            cost, _ = compute_turn_cost(u, m)
                            session_cost += cost
                            if ts and (last_turn_ts is None or ts > last_turn_ts):
                                last_turn_ts = ts
                            context_tokens = (
                                u.get("input_tokens", 0)
                                + u.get("cache_read_input_tokens", 0)
                                + u.get("cache_creation_input_tokens", 0)
                            )
                except Exception:
                    continue
    except Exception:
        pass

# 3. Today cost and budget percentage from gateway_spend
today_cost = 0.0
budget_limit = 50.0
if calculate_gateway_spend:
    try:
        gw = calculate_gateway_spend()
        today_cost = gw.get("total_cost_usd", 0.0)
        budget_limit = gw.get("budget_limit_usd", 50.0)
    except Exception:
        pass

pct_used = round((today_cost / budget_limit) * 100.0) if budget_limit > 0 else 0

# 4. Rolling 5m spend and 1h burn rate across recent project/job files
now = datetime.datetime.now(datetime.timezone.utc)
now_ts = now.timestamp()
cutoff_5m = now_ts - 300
cutoff_1h = now_ts - 3600

spend_5m = 0.0
spend_1h = 0.0
sess_5m = 0.0
sess_1h = 0.0
sid = payload.get("session_id") or "\0"
r_seen = set()

if compute_turn_cost:
    try:
        # Fast scan: only open files modified within the last hour
        search_dirs = [os.path.join(claude_dir, "projects"), os.path.join(claude_dir, "jobs")]
        recent_files = []
        for sdir in search_dirs:
            if os.path.exists(sdir):
                for root, _, files in os.walk(sdir):
                    for file in files:
                        if file.endswith(".jsonl"):
                            fpath = os.path.join(root, file)
                            try:
                                if os.path.getmtime(fpath) >= cutoff_1h:
                                    recent_files.append(fpath)
                            except Exception:
                                pass

        for f in recent_files:
            try:
                with open(f, "r", encoding="utf-8", errors="ignore") as fp:
                    for line in fp:
                        if '"assistant"' not in line:
                            continue
                        try:
                            r = json.loads(line)
                            if r.get("type") == "assistant":
                                ts_str = r.get("timestamp")
                                if ts_str:
                                    ts_dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                    t_epoch = ts_dt.timestamp()
                                    if t_epoch >= cutoff_1h:
                                        mid = (r.get("message") or {}).get("id") or r.get("requestId")
                                        if mid and mid not in r_seen:
                                            r_seen.add(mid)
                                            u = (r.get("message") or {}).get("usage") or {}
                                            m = (r.get("message") or {}).get("model", "")
                                            cost, _ = compute_turn_cost(u, m)
                                            in_sess = f == transcript_path or sid in f
                                            spend_1h += cost
                                            if in_sess:
                                                sess_1h += cost
                                            if t_epoch >= cutoff_5m:
                                                spend_5m += cost
                                                if in_sess:
                                                    sess_5m += cost
                                            if last_turn_ts is None or ts_str > last_turn_ts:
                                                last_turn_ts = ts_str
                        except Exception:
                            continue
            except Exception:
                continue
    except Exception:
        pass

# 5. Prompt Cache TTL countdown with exact end time
ttl_setting = "1h"
try:
    with open(os.path.join(claude_dir, "settings.json")) as fp:
        ttl_setting = json.load(fp).get("promptCacheTtl", "1h")
except Exception:
    pass
ttl_seconds = 3600 if ttl_setting == "1h" else 300

cache_status = "cache \033[3;33mcold\033[0m"
pc_exp = (payload.get("prompt_cache") or {}).get("expires_at")
if pc_exp or last_turn_ts:
    try:
        if pc_exp:
            # authoritative: Claude Code's own expiry (main conversation, real TTL)
            expiry_dt = datetime.datetime.fromtimestamp(pc_exp, datetime.timezone.utc)
        else:
            # fallback for older versions / no cache tokens reported
            turn_dt = datetime.datetime.fromisoformat(last_turn_ts.replace("Z", "+00:00"))
            expiry_dt = turn_dt + datetime.timedelta(seconds=ttl_seconds)
        remaining = (expiry_dt - now).total_seconds()
        local_expiry = expiry_dt.astimezone()
        time_str = local_expiry.strftime("%I:%M%p").lstrip("0").lower()
        if remaining > 0:
            mins = int(remaining // 60)
            left = f"{mins}m" if mins else f"{int(remaining)}s"
            cache_status = f"cache {left} (to {time_str})"
        else:
            cache_status = f"cache \033[3;33mcold\033[0m (since {time_str})"
    except Exception:
        pass

# 6. Currency and context formatting
def fmt_usd(val):
    if abs(val) < 0.005:
        return "$0"
    return f"${val:.2f}"

def fmt_tokens(n):
    if n >= 1000000:
        return f"{n/1000000:.1f}".rstrip("0").rstrip(".") + "M"
    return f"{round(n/1000)}k"

cw = payload.get("context_window") or {}
ctx_size = cw.get("context_window_size") or (1000000 if "1m" in model_raw.lower() else 200000)
ctx_str = f"{fmt_tokens(context_tokens)}/{fmt_tokens(ctx_size)}" if context_tokens > 0 else "N/A"

# Runway: time until daily cap at the trailing-1h burn rate; only shown if it hits before UTC reset
runway = ""
if spend_1h > 0.01 and budget_limit > 0:
    secs_to_cap = max(0.0, budget_limit - today_cost) / spend_1h * 3600
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
    if secs_to_cap < (midnight - now).total_seconds():
        h, m = divmod(int(secs_to_cap // 60), 60)
        runway = f"  cap in ~{h}h{m:02d}m" if h else f"  cap in ~{m}m"

B, D, R = "\033[1m", "\033[38;5;249m", "\033[0m"  # D = light gray, readable unlike faint
TC = "\033[1;31m" if pct_used >= 96 else "\033[1;33m" if pct_used >= 80 else B  # budget: yellow 80%, red 96%
ctx_pct = context_tokens / ctx_size if ctx_size else 0
if ctx_pct >= 0.8:
    ctx_str = f"\033[33m{ctx_str}{R}"

# One row: model · cache · context │ session spend +5m │ today spend +5m +1h
print(
    f"{B}{model_name}{R} {D}·{R} {cache_status} {D}·{R} {ctx_str}"
    f"  {D}│ session{R} {B}{fmt_usd(session_cost)}{R} {D}{fmt_usd(sess_5m)}/5m{R}"
    f"  {D}│ today{R} {TC}{fmt_usd(today_cost)}{R} {D}{fmt_usd(spend_5m)}/5m  {fmt_usd(spend_1h)}/hr{runway}{R}"
)
