# Claude Code statusline (AI gateway spend tracker)

Native Python statusline hook: session/today spend, 5m/1h burn rate, prompt
cache TTL countdown, context usage. Reads `~/.claude/projects/**/*.jsonl`
directly — no `ccusage`, no node.

Pricing in `gateway_spend.py` defaults to base model rates + a flat 10%
gateway markup. If your org's AI gateway charges differently, edit
`BASE_RATES` / `GATEWAY_MARKUP` there.

## Install

```bash
./install.sh
```

Writes `statusLine` into `~/.claude/settings.json` pointing at this
directory. Restart Claude Code to pick it up.

## Manual check

```bash
python3 gateway_spend.py                # today's spend table
echo '{}' | python3 statusline.py        # render one statusline row
```

## Files

- `statusline.py` — the hook Claude Code calls each render.
- `gateway_spend.py` — pricing/cost engine, importable and standalone-runnable.
