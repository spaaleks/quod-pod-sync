#!/usr/bin/env bash
# Remove QuodPodSync. Your profiles, settings and database backups are kept unless you pass --purge.
set -euo pipefail
DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"

systemctl --user disable --now quodpodsync-autosync.path 2>/dev/null || true
rm -f "$CONF/systemd/user/quodpodsync-autosync."{path,service}
systemctl --user daemon-reload || true
rm -rf "$DATA/quodpodsync/app"
rm -f "$HOME/.local/bin/quod-pod-sync" "$HOME/.local/bin/quodpodsync" "$DATA/applications/quodpodsync.desktop" "$DATA/icons/hicolor/scalable/apps/quodpodsync.svg"

/usr/bin/python3 - <<'PY'
import json, os
path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "quodlibet", "lists", "customcommands.json")
try:
    with open(path, encoding="utf-8") as fh:
        coms = json.load(fh)
except (FileNotFoundError, json.JSONDecodeError):
    raise SystemExit
for name in ("Send to iPod", "Sync playlist to iPod"):
    coms.pop(name, None)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(coms, fh, indent=4)
PY

if [[ "${1:-}" == "--purge" ]]; then
    rm -rf "$CONF/quodpodsync" "${XDG_CACHE_HOME:-$HOME/.cache}/quodpodsync" "$DATA/quodpodsync"
    echo "QuodPodSync removed, including profiles, caches and backups."
else
    echo "QuodPodSync removed. Profiles in $CONF/quodpodsync and backups in $DATA/quodpodsync/backups were kept."
fi
