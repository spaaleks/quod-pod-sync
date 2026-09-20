"""Where QuodPodSync keeps its files, and the values that do not change.

Config and profiles under ~/.config, caches under ~/.cache, database backups under ~/.local/share.
"""
import os

def _xdg(var, default):
    return os.path.join(os.environ.get(var) or os.path.expanduser(default), "quodpodsync")


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", "~/.config")
CACHE_DIR = _xdg("XDG_CACHE_HOME", "~/.cache")
DATA_DIR = _xdg("XDG_DATA_HOME", "~/.local/share")
PROFILE_DIR = os.path.join(CONFIG_DIR, "profiles")
SETTINGS = os.path.join(CONFIG_DIR, "settings.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
LOCAL_CACHE = os.path.join(CACHE_DIR, "library.json")
LOCK_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or CACHE_DIR, "quodpodsync.lock")
IPOD_DB = os.environ.get("QUODPODSYNC_IPOD_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipod-db")
QL_PLAYLISTS = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "quodlibet", "playlists")
XSPF_NS = "http://xspf.org/ns/0/"
HASH58_MODELS = {
    0x1261: {80: "B147", 120: "B562", 160: "B145"},  # iPod Classic 6G / 6.5G / 7G
    0x1262: {4: "A978", 8: "A980"},  # iPod nano 3G
    0x1263: {4: "B480", 8: "B598", 16: "B903"},  # iPod nano 4G
}
POPM_EMAIL = os.environ.get("EMAIL") or "quodlibet@lists.sacredchao.net"
POPM_BY_STARS = {0: 0, 1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
RESERVE_BYTES = 200 * 1024**2
KEEP_BACKUPS = 10
FILETYPES = {".mp3": "MPEG audio file", ".m4a": "AAC audio file", ".aac": "AAC audio file", ".wav": "WAV audio file"}
PACKAGES = {
    "pacman": ["libgpod"],
    "apt": ["libgpod4"],
    "dnf": ["libgpod"],
    "zypper": ["libgpod"],
}


def profile_path(serial):
    return os.path.join(PROFILE_DIR, f"{serial}.json")


def tracks_cache_path(serial):
    return os.path.join(CACHE_DIR, f"{serial}.tracks.json")
