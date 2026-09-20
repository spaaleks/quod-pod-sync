"""One connected iPod: the device, its database, its backups.

Everything that touches the device goes through here. The database itself is read and written by the
ipod-db helper, so libgpod stays out of the Python side.
"""
import fcntl
import glob
import json
import os
import re
import shutil
import subprocess
import time

import mutagen

from . import ratings
from .deps import Dependencies
from .matching import read_ids, split_artists, title_key
from .paths import BACKUP_DIR, CACHE_DIR, HASH58_MODELS, IPOD_DB, KEEP_BACKUPS, LOCK_FILE, RESERVE_BYTES, \
    profile_path, tracks_cache_path
from .settings import Profile


def sync_running():
    """True while a sync holds the lock."""
    os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


class IPod:
    """A mounted iPod, identified by its serial number or the filesystem UUID."""

    def __init__(self, mount):
        self.mount = mount
        self.info = self._read_sysinfo()
        self.usb_pid = 0
        self.source = self.uuid = ""
        self._find_device_node()
        self._ensure_device_info()
        self.serial = self.info.get("pszSerialNumber") or (f"UUID-{self.uuid}" if self.uuid
                                                           else os.path.basename(mount))
        self.needs_rewrite = os.path.exists(self._rewrite_marker()) or not self.signature_ok()
        self._adopt_label_profile()
        self.refresh_space()

    @classmethod
    def connected(cls):
        """Every mounted iPod."""
        mounts = sorted({os.path.dirname(p) for p in glob.glob("/run/media/*/*/iPod_Control")
                         + glob.glob("/media/*/*/iPod_Control")})
        return [cls(m) for m in mounts if os.path.exists(os.path.join(m, "iPod_Control", "iTunes", "iTunesDB"))]

    @property
    def label(self):
        model = " ".join(x for x in (self.info.get("ModelFamily"), self.info.get("Capacity")) if x)
        return f"{model or 'iPod'} ({os.path.basename(self.mount)})"

    def profile(self):
        return Profile(self.serial, self.label)

    # ------------------------------------------------------------ device

    def _read_sysinfo(self):
        info = {}
        try:
            with open(os.path.join(self.mount, "iPod_Control", "Device", "SysInfo"), errors="ignore") as fh:
                for line in fh:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        info[key.strip()] = value.strip()
        except OSError:
            pass
        return info

    def _find_device_node(self):
        try:
            out = subprocess.run(["findmnt", "-no", "SOURCE,UUID", "--target", self.mount],
                                 capture_output=True, text=True, timeout=5).stdout.split()
            self.source, self.uuid = (out + ["", ""])[:2]
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _udev(self):
        disk = subprocess.run(["lsblk", "-no", "PKNAME", self.source], capture_output=True, text=True).stdout.strip()
        properties = subprocess.run(["udevadm", "info", "-q", "property", "-n", f"/dev/{disk}" if disk else self.source],
                                    capture_output=True, text=True).stdout
        return dict(line.split("=", 1) for line in properties.splitlines() if "=" in line)

    def _ensure_device_info(self):
        """Fill in GUID and model when SysInfo is empty, otherwise libgpod cannot sign the database.
        The GUID is the USB serial, the model follows from USB product id and capacity."""
        if not self.source:
            return
        properties = self._udev()
        try:
            self.usb_pid = int(properties.get("ID_USB_MODEL_ID") or properties.get("ID_MODEL_ID") or "0", 16)
        except ValueError:
            self.usb_pid = 0
        if os.path.exists(os.path.join(self.mount, "iPod_Control", "Device", "SysInfoExtended")):
            return
        missing = {}
        serial = properties.get("ID_SERIAL_SHORT", "")
        if not self.info.get("FirewireGuid") and re.fullmatch(r"[0-9A-Fa-f]{16}", serial):
            missing["FirewireGuid"] = f"0x{serial.upper()}"
        models = HASH58_MODELS.get(self.usb_pid)
        if not self.info.get("ModelNumStr") and models:
            gb = min(models, key=lambda c: abs(c * 1e9 - self.total_bytes()))
            missing["ModelNumStr"] = f"x{models[gb]}"
        if not missing:
            return
        with open(os.path.join(self.mount, "iPod_Control", "Device", "SysInfo"), "a", encoding="ascii") as fh:
            fh.writelines(f"{k}: {v}\n" for k, v in missing.items())
        self.info.update(missing)
        os.makedirs(CACHE_DIR, exist_ok=True)
        open(self._rewrite_marker(), "w").close()

    def _rewrite_marker(self):
        return os.path.join(CACHE_DIR, f"rewrite-{self.info.get('FirewireGuid') or self.uuid or 'unknown'}")

    def _adopt_label_profile(self):
        """Rename a profile that was named after the drive label."""
        old_id = os.path.basename(self.mount)
        if old_id == self.serial or os.path.exists(profile_path(self.serial)) \
                or not os.path.exists(profile_path(old_id)):
            return
        with open(profile_path(old_id), encoding="utf-8") as fh:
            data = json.load(fh)
        data["serial"] = self.serial
        Profile(self.serial, data=data).save()
        os.remove(profile_path(old_id))
        if os.path.exists(tracks_cache_path(old_id)):
            os.replace(tracks_cache_path(old_id), tracks_cache_path(self.serial))
        old_backups = os.path.join(BACKUP_DIR, old_id)
        if os.path.isdir(old_backups) and not os.path.exists(os.path.join(BACKUP_DIR, self.serial)):
            os.replace(old_backups, os.path.join(BACKUP_DIR, self.serial))

    def total_bytes(self):
        stat = os.statvfs(self.mount)
        return stat.f_blocks * stat.f_frsize

    def refresh_space(self):
        stat = os.statvfs(self.mount)
        self.total = stat.f_blocks * stat.f_frsize
        self.free = stat.f_bavail * stat.f_frsize

    @property
    def needs_signature(self):
        return self.usb_pid in HASH58_MODELS or "Classic" in self.info.get("ModelFamily", "")

    def signature_ok(self):
        """False if this iPod needs a signed database and the current one is not signed."""
        if not self.needs_signature:
            return True
        try:
            with open(os.path.join(self.mount, "iPod_Control", "iTunes", "iTunesDB"), "rb") as fh:
                header = fh.read(0x6C)
        except OSError:
            return True
        return len(header) == 0x6C and any(header[0x58:0x6C])

    def eject(self):
        """Flush, unmount and power off. Raises RuntimeError on failure."""
        if sync_running():
            raise RuntimeError("A sync is running, eject when it is finished")
        if not self.source:
            raise RuntimeError(f"Cannot find the device for {self.mount}")
        os.sync()
        result = subprocess.run(["udisksctl", "unmount", "-b", self.source, "--no-user-interaction"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout).strip()
                               or "unmount failed (is a file on the iPod still open?)")
        disk = subprocess.run(["lsblk", "-no", "PKNAME", self.source], capture_output=True, text=True).stdout.strip()
        target = f"/dev/{disk}" if disk else self.source
        result = subprocess.run(["udisksctl", "power-off", "-b", target, "--no-user-interaction"],
                                capture_output=True, text=True)
        if result.returncode != 0:
            return f"Unmounted, safe to unplug ({(result.stderr or result.stdout).strip()})"
        return "Ejected, safe to unplug"

    # ------------------------------------------------------------ database

    def run_helper(self, *args, stdin=None):
        problem = Dependencies.missing()
        if problem:
            raise RuntimeError(problem)
        return subprocess.run([IPOD_DB, self.mount, *args], input=stdin, capture_output=True, text=True)

    def read(self, progress=None):
        """(tracks {dbid: dict}, playlists {name: [dbid]}). Song ids are cached per iPod."""
        result = self.run_helper("list")
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "ipod-db list failed")
        tracks, playlists = {}, {}
        for line in result.stdout.splitlines():
            f = line.split("\t")
            if f[0] == "T" and len(f) >= 15:
                tracks[f[1]] = {
                    "dbid": f[1], "size": int(f[2]), "length_ms": int(f[3]), "disc": int(f[4]), "track": int(f[5]),
                    "rating": int(f[6]), "app_rating": int(f[7]), "playcount": int(f[8]),
                    "recent_playcount": int(f[9]), "ipod_path": f[10], "title": f[11], "artist": f[12],
                    "albumartist": f[13], "album": f[14],
                }
            elif f[0] == "P" and len(f) >= 3:
                playlists[f[1]] = [x for x in f[2].split(",") if x]
        self._fill_ids(tracks, progress)
        return tracks, playlists

    def _fill_ids(self, tracks, progress=None):
        """Read the identifiers out of the files on the iPod, once per file."""
        cache_file = tracks_cache_path(self.serial)
        try:
            with open(cache_file, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}
        todo = [t for t in tracks.values() if cache.get(t["dbid"], {}).get("size") != t["size"]]
        for i, track in enumerate(todo, 1):
            if progress:
                progress(f"Reading tags on the iPod ({i}/{len(todo)}, only needed once)", i / len(todo))
            try:
                ids = read_ids(mutagen.File(self.file_of(track)))
            except Exception:
                ids = {"spotify": None, "isrc": None, "youtube": None}
            cache[track["dbid"]] = {"size": track["size"], **ids}
        stale = [k for k in cache if k not in tracks]
        for key in stale:
            del cache[key]
        if todo or stale:
            self._save_track_cache(cache)
        for dbid, track in tracks.items():
            track.update({k: cache[dbid][k] for k in ("spotify", "isrc", "youtube")})

    def _save_track_cache(self, cache):
        os.makedirs(CACHE_DIR, exist_ok=True)
        target = tracks_cache_path(self.serial)
        with open(target + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(target + ".tmp", target)

    def remember_ids(self, new_ids, songs):
        """Note the identifiers of freshly copied songs, so they are not read from the device again."""
        cache_file = tracks_cache_path(self.serial)
        try:
            with open(cache_file, encoding="utf-8") as fh:
                cache = json.load(fh)
        except (FileNotFoundError, json.JSONDecodeError):
            cache = {}
        for path, dbid in new_ids.items():
            meta = songs[path]
            cache[dbid] = {"size": meta["size"], "spotify": meta["spotify"], "isrc": meta["isrc"],
                           "youtube": meta["youtube"]}
        self._save_track_cache(cache)

    def file_of(self, track):
        return os.path.join(self.mount, *track["ipod_path"].strip(":").split(":"))

    # ------------------------------------------------------------ backups

    def backup(self):
        """Copy the database aside before writing. Keeps the last KEEP_BACKUPS."""
        target = os.path.join(BACKUP_DIR, self.serial, time.strftime("%Y-%m-%d_%H-%M-%S"))
        os.makedirs(target, exist_ok=True)
        control = os.path.join(self.mount, "iPod_Control")
        for name in ("iTunes/iTunesDB", "iTunes/iTunesCDB", "iTunes/Play Counts", "iTunes/iTunesPrefs",
                     "Artwork/ArtworkDB"):
            source = os.path.join(control, name)
            if os.path.exists(source):
                shutil.copy2(source, os.path.join(target, os.path.basename(name)))
        for old in sorted(glob.glob(os.path.join(BACKUP_DIR, self.serial, "*")))[:-KEEP_BACKUPS]:
            shutil.rmtree(old, ignore_errors=True)
        return target

    def backups(self):
        """[(folder, "2026-09-19 22:31", bytes, signed)], newest first."""
        out = []
        for folder in sorted(glob.glob(os.path.join(BACKUP_DIR, self.serial, "*")), reverse=True):
            database = os.path.join(folder, "iTunesDB")
            if not os.path.isfile(database):
                continue
            name = os.path.basename(folder)
            with open(database, "rb") as fh:
                header = fh.read(0x6C)
            signed = len(header) == 0x6C and any(header[0x58:0x6C])
            out.append((folder, f"{name[:10]} {name[11:].replace('-', ':')}", os.path.getsize(database), signed))
        return out

    def restore(self, folder):
        """Put a database backup back. The current one is saved first."""
        if sync_running():
            raise RuntimeError("A sync is running, restore when it is finished")
        files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
        if "iTunesDB" not in files:
            raise RuntimeError(f"{folder} contains no iTunesDB")
        self.backup()
        targets = {"iTunesDB": "iTunes", "iTunesCDB": "iTunes", "Play Counts": "iTunes", "iTunesPrefs": "iTunes",
                   "ArtworkDB": "Artwork"}
        restored = []
        for name in files:
            if name not in targets:
                continue
            destination = os.path.join(self.mount, "iPod_Control", targets[name], name)
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(os.path.join(folder, name), destination)
            restored.append(name)
        os.sync()
        if not self.signature_ok():
            open(self._rewrite_marker(), "w").close()
            self.needs_rewrite = True
        return f"Restored: {', '.join(restored)}. The iPod now shows the state from that time."

    def clear_rewrite_marker(self):
        if os.path.exists(self._rewrite_marker()):
            os.remove(self._rewrite_marker())
        self.needs_rewrite = False

    # ------------------------------------------------------------ copying songs off

    def export_name(self, track):
        """Where a song from the iPod belongs: Artist/Album/01 Title.ext, from its own tags."""
        source = self.file_of(track)
        artist = track["albumartist"] or track["artist"]
        album, title = track["album"], track["title"]
        disc, number = track["disc"], track["track"]
        try:
            tags = (mutagen.File(source, easy=True) or {}).tags or {}
            get = lambda key: "/".join(tags.get(key, [])).strip()
            artist = get("albumartist") or get("artist") or artist
            album = get("album") or album
            title = get("title") or title
            disc = int((get("discnumber") or "0").partition("/")[0] or 0) or disc
            number = int((get("tracknumber") or "0").partition("/")[0] or 0) or number
        except Exception:
            pass
        clean = lambda value, fallback: re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", (value or "").strip()) or fallback
        name = f"{number:02d} {title}" if number else (title or os.path.basename(source))
        if disc and number:
            name = f"{disc}-{name}"
        return os.path.join(clean(artist, "Unknown Artist"), clean(album, "Unknown Album"),
                            clean(name, os.path.basename(source)) + os.path.splitext(source)[1].lower())

    def export(self, target, tracks=None, library=None, only_missing=True, progress=None):
        """Copy songs off the iPod into `target`. Returns (copied, skipped, failed)."""
        from .sync import match_tracks

        tracks = self.read(progress)[0] if tracks is None else tracks
        keep = set(match_tracks(library.songs, tracks).values()) if (only_missing and library) else set()
        todo = [t for t in tracks.values() if t["dbid"] not in keep]
        copied, skipped, failed = 0, 0, []
        for i, track in enumerate(todo, 1):
            source = self.file_of(track)
            destination = os.path.join(target, self.export_name(track))
            if progress:
                progress(f"Copying {i}/{len(todo)}: {track['artist']} - {track['title']}", i / max(len(todo), 1))
            try:
                if os.path.exists(destination) and os.path.getsize(destination) == os.path.getsize(source):
                    skipped += 1
                    continue
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.copy2(source, destination)
                copied += 1
            except OSError as e:
                failed.append(f"{track['title']}: {e}")
        return copied, skipped, failed
