#!/usr/bin/env bash
# Install this statusline into ~/.claude/settings.json.
# ponytail: naive JSON patch via python -c; swap for jq if settings.json grows nested statusLine conflicts.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETTINGS="$HOME/.claude/settings.json"

read -r -p "Also set prompt cache TTL to 1h (longer cache, costs more per write)? [y/N] " SET_TTL
SET_TTL="${SET_TTL:-n}"

PY="$(command -v python3)"
"$PY" - "$SETTINGS" "$DIR" "$SET_TTL" <<'EOF'
import json, os, sys
settings_path, pkg_dir, set_ttl = sys.argv[1], sys.argv[2], sys.argv[3]
data = {}
if os.path.exists(settings_path):
    with open(settings_path) as f:
        data = json.load(f)
data["statusLine"] = {
    "type": "command",
    "command": f"{sys.executable} {pkg_dir}/statusline.py",
    "refreshInterval": 30,
}
if set_ttl.lower().startswith("y"):
    data["promptCacheTtl"] = "1h"
    print("promptCacheTtl -> 1h")
os.makedirs(os.path.dirname(settings_path), exist_ok=True)
with open(settings_path, "w") as f:
    json.dump(data, f, indent=2)
print(f"Installed statusLine -> {pkg_dir}/statusline.py")
EOF
