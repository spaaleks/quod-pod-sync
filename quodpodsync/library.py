"""The music library on disk.

Reads the tags of every song once and caches them, so later scans only touch changed files.
"""
import json
import os

import mutagen

from . import ratings
from .matching import AUDIO_EXTS, read_ids
from .paths import CACHE_DIR, LOCAL_CACHE
from .settings import Settings


def _num(value):
    """'3/12' to (3, 12)."""
    try:
        first, _, total = str(value or "").partition("/")
        return int(first or 0), int(total or 0)
    except ValueError:
        return 0, 0


def song_meta(path):
    """Everything about one song that a sync needs."""
    audio = mutagen.File(path)
    easy = mutagen.File(path, easy=True)
    tags = (easy.tags if easy is not None else None) or {}
    get = lambda key: "/".join(str(v) for v in tags.get(key, [])).strip()
    track, tracks = _num(get("tracknumber"))
    disc, discs = _num(get("discnumber"))
    info = getattr(audio, "info", None)
    meta = {
        "size": os.path.getsize(path),
        "mtime": os.path.getmtime(path),
        "title": get("title") or os.path.splitext(os.path.basename(path))[0],
        "artist": get("artist"),
        "albumartist": get("albumartist"),
        "album": get("album"),
        "genre": get("genre"),
        "composer": get("composer"),
        "year": _num((get("date") or get("originaldate"))[:4])[0],
        "track": track,
        "tracks": tracks,
        "disc": disc,
        "discs": discs,
        "length_ms": int((getattr(info, "length", 0) or 0) * 1000),
        "bitrate": int((getattr(info, "bitrate", 0) or 0) / 1000),
        "samplerate": int(getattr(info, "sample_rate", 0) or 0),
        "compilation": 1 if get("compilation") in ("1", "true", "True") else 0,
    }
    meta.update(read_ids(audio))
    meta["popm_rating"], meta["popm_count"] = ratings.of_file(audio)
    meta["group_artist"] = meta["albumartist"] or meta["artist"] or "Unknown Artist"
    meta["group_album"] = meta["album"] or "Unknown Album"
    return meta


class Library:
    """Every song in the configured folders, keyed by its path."""

    def __init__(self, settings=None, scan=True):
        self.settings = settings or Settings()
        self.songs = {}
        if scan:
            self.scan()

    def __contains__(self, path):
        return path in self.songs

    def __len__(self):
        return len(self.songs)

    def __getitem__(self, path):
        return self.songs[path]

    def items(self):
        return self.songs.items()

    def scan(self):
        """Read the library, using the cache for files that did not change."""
        try:
            with open(LOCAL_CACHE, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}
        songs, dirty = {}, False
        for folder in self.settings.folders:
            for root, dirs, files in os.walk(folder):
                dirs.sort()
                for name in sorted(files):
                    if os.path.splitext(name)[1].lower() not in AUDIO_EXTS or name.startswith("._"):
                        continue
                    path = os.path.join(root, name)
                    if path in songs:
                        continue
                    stat = os.stat(path)
                    hit = cache.get(path)
                    if not hit or hit["size"] != stat.st_size or hit["mtime"] != stat.st_mtime \
                            or "popm_rating" not in hit:
                        try:
                            hit = song_meta(path)
                        except Exception as e:
                            print(f"WARN  cannot read {path}: {e}")
                            continue
                        dirty = True
                    songs[path] = hit
        self.songs = songs
        if dirty or len(songs) != len(cache):
            self._save_cache()
        return self

    def _save_cache(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(LOCAL_CACHE + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(self.songs, fh, ensure_ascii=False)
        os.replace(LOCAL_CACHE + ".tmp", LOCAL_CACHE)

    def size_of(self, paths):
        return sum(self.songs[p]["size"] for p in paths if p in self.songs)

    def by_artist(self):
        """{artist: {album: [count, bytes]}} for the tree in the window."""
        out = {}
        for meta in self.songs.values():
            album = out.setdefault(meta["group_artist"], {}).setdefault(meta["group_album"], [0, 0])
            album[0] += 1
            album[1] += meta["size"]
        return out
