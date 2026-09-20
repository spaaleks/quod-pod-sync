"""Settings and the per iPod profile.

Settings say where the library and the playlists are, a profile says what belongs on one iPod.
"""
import glob
import json
import os
import subprocess

from .paths import CONFIG_DIR, PROFILE_DIR, QL_PLAYLISTS, SETTINGS, profile_path


def default_music_dir():
    try:
        out = subprocess.run(["xdg-user-dir", "MUSIC"], capture_output=True, text=True, timeout=5).stdout.strip()
        if out and os.path.isdir(out):
            return out
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.path.expanduser("~/Music")


class Settings:
    """Library folders and where playlists come from."""

    def __init__(self, data=None):
        self.data = {"folders": [default_music_dir()]}
        if data is None:
            try:
                with open(SETTINGS, encoding="utf-8") as fh:
                    self.data.update(json.load(fh))
            except (FileNotFoundError, json.JSONDecodeError):
                pass
        else:
            self.data.update(data)

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(SETTINGS + ".tmp", SETTINGS)

    @property
    def folders(self):
        return [os.path.abspath(os.path.expanduser(f)) for f in self.data["folders"]]

    @folders.setter
    def folders(self, value):
        self.data["folders"] = list(value)

    def contains(self, path):
        path = os.path.abspath(path)
        return any(path == f or path.startswith(f + os.sep) for f in self.folders)

    @property
    def playlist_source(self):
        """("m3u", folder) or ("quodlibet", folder)."""
        folder = self.data.get("playlists")
        if folder:
            return "m3u", os.path.abspath(os.path.expanduser(folder))
        for candidate in [os.path.join(os.path.dirname(f), "920_Playlists") for f in self.folders]:
            if os.path.isdir(candidate):
                return "m3u", candidate
        return "quodlibet", QL_PLAYLISTS


class Profile:
    """What belongs on one iPod, and what the last sync left there."""

    FIELDS = {
        "serial": "", "name": "iPod", "everything": False,
        "artists": [], "albums": [], "files": [], "playlists": [], "all_playlists": False,
        "managed_playlists": [], "known_ipod_playlists": [],
        "autosync": True, "capacity": None, "last_sync": None,
    }

    def __init__(self, serial, label="iPod", data=None):
        self.data = {**{k: (v.copy() if isinstance(v, list) else v) for k, v in self.FIELDS.items()},
                     "serial": serial, "name": label}
        if data is None:
            try:
                with open(profile_path(serial), encoding="utf-8") as fh:
                    self.data.update(json.load(fh))
            except FileNotFoundError:
                pass
        else:
            self.data.update(data)

    def __getitem__(self, key):
        return self.data[key]

    def __setitem__(self, key, value):
        self.data[key] = value

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def serial(self):
        return self.data["serial"]

    @property
    def name(self):
        return self.data.get("name") or self.serial

    @property
    def exists(self):
        return os.path.exists(profile_path(self.serial))

    def save(self):
        os.makedirs(PROFILE_DIR, exist_ok=True)
        target = profile_path(self.serial)
        with open(target + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(target + ".tmp", target)

    def wanted_playlists(self, playlists):
        """Names of the playlists this iPod syncs."""
        if self.data.get("all_playlists"):
            return sorted(playlists)
        return [n for n in self.data.get("playlists", []) if n in playlists]

    def selection(self, songs, playlists):
        """Set of song paths that belong on this iPod."""
        if self.data.get("everything"):
            return set(songs)
        artists = set(self.data.get("artists", []))
        albums = {tuple(a) for a in self.data.get("albums", [])}
        chosen = {p for p, m in songs.items()
                  if m["group_artist"] in artists or (m["group_artist"], m["group_album"]) in albums}
        chosen |= {p for p in self.data.get("files", []) if p in songs}
        for name in self.wanted_playlists(playlists):
            chosen |= {p for p in playlists.get(name, []) if p in songs}
        return chosen

    @classmethod
    def all_known(cls):
        """Every saved profile, also for iPods that are not connected."""
        out = []
        for path in sorted(glob.glob(os.path.join(PROFILE_DIR, "*.json"))):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            out.append(cls(data.get("serial") or os.path.splitext(os.path.basename(path))[0], data=data))
        return out
