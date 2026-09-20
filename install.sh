#!/usr/bin/env bash
# Install QuodPodSync for the current user (no root needed).
#   ./install.sh                  install / update
#   ./install.sh --no-autosync    don't enable the plug-in autosync service
#   ./install.sh --no-quodlibet   don't add the Quod Libet "Custom Commands" entries
set -euo pipefail
cd "$(dirname "$0")"

AUTOSYNC=1 QUODLIBET=1
for arg in "$@"; do
    case "$arg" in
        --no-autosync) AUTOSYNC=0 ;;
        --no-quodlibet) QUODLIBET=0 ;;
        *) echo "unknown option: $arg" >&2; exit 64 ;;
    esac
done

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
APP="$DATA/quodpodsync/app"
BIN="$HOME/.local/bin"
UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

echo "Checking dependencies ..."
missing=()
/usr/bin/python3 -c 'import mutagen' 2>/dev/null || missing+=(python-mutagen)
/usr/bin/python3 -c 'import gi; gi.require_version("Gtk", "3.0")' 2>/dev/null || missing+=(python-gobject gtk3)
pkg-config --exists libgpod-1.0 2>/dev/null || missing+=(libgpod)
command -v cc >/dev/null || missing+=(gcc)
command -v pkg-config >/dev/null || missing+=(pkgconf)
if ((${#missing[@]})); then
    echo "Missing: ${missing[*]}"
    echo "Install with: sudo pacman -S --needed ${missing[*]}"
    exit 1
fi

echo "Building ipod-db ..."
make -s -C native

echo "Installing to $APP ..."
rm -rf "$APP"
mkdir -p "$APP" "$BIN"
cp -r quodpodsync "$APP/"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
install -m 755 native/ipod-db "$APP/quodpodsync/ipod-db"
cat > "$BIN/quod-pod-sync" <<LAUNCHER
#!/bin/sh
PYTHONPATH="$APP" exec /usr/bin/python3 -m quodpodsync "\$@"
LAUNCHER
chmod 755 "$BIN/quod-pod-sync"

install -Dm 644 data/quodpodsync.desktop "$DATA/applications/quodpodsync.desktop"
install -Dm 644 data/quodpodsync.svg "$DATA/icons/hicolor/scalable/apps/quodpodsync.svg"
command -v update-desktop-database >/dev/null && update-desktop-database -q "$DATA/applications" || true
command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA/icons/hicolor" 2>/dev/null || true

if ((AUTOSYNC)); then
    echo "Enabling autosync (systemd user service) ..."
    mkdir -p "$UNITS"
    install -m 644 data/systemd/quodpodsync-autosync.{path,service} "$UNITS/"
    systemctl --user daemon-reload
    systemctl --user enable --now quodpodsync-autosync.path >/dev/null
fi

if ((QUODLIBET)); then
    echo "Adding Quod Libet custom commands ..."
    /usr/bin/python3 - "$BIN/quod-pod-sync" <<'PY'
import json, os, sys
exe = sys.argv[1]
path = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "quodlibet", "lists", "customcommands.json")
try:
    with open(path, encoding="utf-8") as fh:
        coms = json.load(fh)
except (FileNotFoundError, json.JSONDecodeError):
    coms = {}
base = {"parameter": "", "unique": True, "reverse": False, "warn_threshold": 1000}
coms["Send to iPod"] = {**base, "name": "Send to iPod", "command": f"{exe} add", "pattern": "<~filename>", "max_args": 10000}
coms["Sync playlist to iPod"] = {**base, "name": "Sync playlist to iPod", "command": f"{exe} playlist", "pattern": "<~playlistname>", "max_args": 1}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, "w", encoding="utf-8") as fh:
    json.dump(coms, fh, indent=4)
PY
    echo "  -> in Quod Libet enable the 'Custom Commands' plugin (File > Plugins) if it isn't active."
fi

echo
echo "Done. Start Quod Pod Sync from your app launcher or run: quod-pod-sync"
