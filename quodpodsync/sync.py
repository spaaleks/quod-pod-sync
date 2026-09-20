"""Working out what a sync would change, and doing it.

A plan compares one iPod with the selection in its profile. Applying it copies and removes songs,
carries ratings and play counts both ways and writes the playlists.
"""
import fcntl
import os
import subprocess
import tempfile
import time
from collections import defaultdict

import mutagen

from . import ratings
from .matching import split_artists, title_key
from .paths import FILETYPES, IPOD_DB, LOCK_FILE, POPM_BY_STARS, RESERVE_BYTES
from .playlists import Playlists


def match_tracks(songs, tracks, prefer=()):
    """{path: dbid}, strongest evidence first. Songs in `prefer` get first pick, so a song that exists
    twice in the library matches the copy that is selected."""
    prefer = set(prefer)
    songs = dict(sorted(songs.items(), key=lambda kv: kv[0] not in prefer))
    assigned, used = {}, set()
    for key in ("spotify", "isrc", "youtube"):
        index = defaultdict(list)
        for dbid, track in tracks.items():
            if track.get(key):
                index[track[key]].append(dbid)
        for path, meta in songs.items():
            if path in assigned or not meta.get(key):
                continue
            for dbid in index.get(meta[key], []):
                if dbid not in used:
                    assigned[path] = dbid
                    used.add(dbid)
                    break
    by_title = defaultdict(list)
    for dbid, track in tracks.items():
        if dbid not in used:
            by_title[title_key(track["title"])].append(dbid)
    for path, meta in songs.items():
        if path in assigned:
            continue
        artists = split_artists(meta["artist"], meta["albumartist"])
        for dbid in by_title.get(title_key(meta["title"]), []):
            if dbid not in used and artists & split_artists(tracks[dbid]["artist"], tracks[dbid]["albumartist"]):
                assigned[path] = dbid
                used.add(dbid)
                break
    return assigned


def cover_of(path, destination):
    """Write the embedded cover to a file, for libgpod to pick up. Returns the path or ''."""
    try:
        audio = mutagen.File(path)
    except Exception:
        return ""
    data = None
    tags = getattr(audio, "tags", None)
    if tags is not None:
        if hasattr(tags, "getall"):
            pictures = tags.getall("APIC")
            data = pictures[0].data if pictures else None
        elif "covr" in tags:
            data = bytes(tags["covr"][0])
    if data is None and getattr(audio, "pictures", None):
        data = audio.pictures[0].data
    if not data:
        return ""
    with open(destination, "wb") as fh:
        fh.write(data)
    return destination


def _clean(value):
    return str(value if value is not None else "").replace("\t", " ").replace("\n", " ")


class SyncPlan:
    """What one sync would change on one iPod."""

    def __init__(self, ipod, profile, library, tracks, ipod_playlists, playlists):
        self.ipod, self.profile, self.library = ipod, profile, library
        self.tracks, self.ipod_playlists = tracks, ipod_playlists
        self.songs = library.songs
        self.selected = profile.selection(self.songs, playlists)
        self.matched = match_tracks(self.songs, tracks, prefer=self.selected)

        keep = {self.matched[p] for p in self.selected if p in self.matched}
        self.adds = sorted(p for p in self.selected if p not in self.matched and self._playable(p))
        self.unsupported = sorted(p for p in self.selected if p not in self.matched and not self._playable(p))
        self.removes = sorted(d for d in tracks if d not in keep)

        wanted = profile.wanted_playlists(playlists)
        self.playlists = {n: [p for p in playlists[n] if p in self.songs] for n in wanted}
        self.changed_playlists = [
            n for n, paths in self.playlists.items()
            if any(p not in self.matched for p in paths)
            or ipod_playlists.get(n) != [self.matched[p] for p in paths]
        ]
        self.drop_playlists = [n for n in profile.get("managed_playlists", [])
                               if n not in wanted and n in ipod_playlists]
        known = set(profile.get("managed_playlists", [])) | set(profile.get("known_ipod_playlists", [])) \
            | set(playlists)
        path_of = {d: p for p, d in self.matched.items()}
        self.backsync = {n: [path_of[d] for d in ids if d in path_of]
                         for n, ids in ipod_playlists.items() if n not in known}
        self.ipod_playlist_names = sorted(ipod_playlists)

        self.ratings_from_ipod, self.ratings_to_ipod = self._ratings()
        self.add_bytes = sum(self.songs[p]["size"] for p in self.adds)
        self.remove_bytes = sum(tracks[d]["size"] for d in self.removes)
        self.music_bytes = sum(t["size"] for t in tracks.values())
        self.budget = ipod.free + self.music_bytes - RESERVE_BYTES

    @classmethod
    def make(cls, ipod, profile, library, progress=None):
        tracks, ipod_playlists = ipod.read(progress)
        return cls(ipod, profile, library, tracks, ipod_playlists, Playlists(library.settings).available())

    def _playable(self, path):
        return os.path.splitext(path)[1].lower() in FILETYPES

    def _ratings(self):
        """What to carry over: (into the library, onto the iPod).
        A rating that differs from the one written at the last sync was set on the device and wins."""
        from_ipod, to_ipod = [], []
        for path, dbid in self.matched.items():
            if path in self.adds:
                continue
            song, track = self.songs[path], self.tracks[dbid]
            library_rating, library_count = song["popm_rating"], song["popm_count"]
            ipod_rating = POPM_BY_STARS[ratings.stars(track["rating"])]
            changed_on_ipod = track["rating"] != track["app_rating"]

            rating = ipod_rating if (changed_on_ipod or not library_rating) else library_rating
            count = max(library_count, track["playcount"]) + track["recent_playcount"]

            if (rating, count) != (library_rating, library_count):
                from_ipod.append((path, rating, count, dbid))
            if (ratings.stars_of_popm(rating) * 20, count) != (track["rating"], track["playcount"]) \
                    or changed_on_ipod:
                to_ipod.append((dbid, ratings.stars_of_popm(rating) * 20, count))
        return from_ipod, to_ipod

    @property
    def fits(self):
        return self.add_bytes - self.remove_bytes <= self.ipod.free - RESERVE_BYTES

    @property
    def empty(self):
        return not (self.adds or self.removes or self.drop_playlists or self.changed_playlists or self.backsync
                    or self.ratings_from_ipod or self.ratings_to_ipod or self.ipod.needs_rewrite)

    def summary(self):
        gb = lambda value: f"{value / 1024**3:.2f} GB"
        lines = [f"+{len(self.adds)} songs ({gb(self.add_bytes)}), "
                 f"-{len(self.removes)} songs ({gb(self.remove_bytes)})"]
        if self.changed_playlists:
            lines.append(f"update playlists: {', '.join(self.changed_playlists)}")
        if self.drop_playlists:
            lines.append(f"remove playlists: {', '.join(self.drop_playlists)}")
        if self.backsync:
            lines.append(f"new playlists on the iPod: {', '.join(self.backsync)}")
        if self.ratings_from_ipod:
            lines.append(f"ratings/play counts from the iPod into the library: {len(self.ratings_from_ipod)} songs")
        if self.ratings_to_ipod:
            lines.append(f"ratings/play counts from the library onto the iPod: {len(self.ratings_to_ipod)} songs")
        if self.ipod.needs_rewrite:
            lines.append("rewrite the iPod database (its device id was missing, songs may not have shown up)")
        if self.unsupported:
            lines.append(f"{len(self.unsupported)} songs skipped (format not playable on iPod)")
        if not self.fits:
            over = self.add_bytes - self.remove_bytes - (self.ipod.free - RESERVE_BYTES)
            lines.append(f"DOES NOT FIT: {gb(over)} too much")
        return "\n".join(lines)

    # ------------------------------------------------------------ applying

    def apply(self, progress=None):
        """Carry out the plan. Returns a summary line, raises if the database could not be written."""
        if not self.fits:
            raise RuntimeError("Selection does not fit on the iPod:\n" + self.summary())
        os.makedirs(os.path.dirname(LOCK_FILE), exist_ok=True)
        with open(LOCK_FILE, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another sync is already running")
            if progress:
                progress("Backing up iPod database", 0)
            self.ipod.backup()
            new_ids, errors = self._copy_and_remove(progress)
            self.ipod.remember_ids(new_ids, self.songs)
            imported, rating_errors = self._playlists_and_ratings(new_ids, progress)
            errors += "".join(f"{line}\n" for line in rating_errors)
            os.sync()

        if not self.ipod.signature_ok():
            raise RuntimeError(
                "The songs were copied, but the iPod database could not be signed, so the iPod will show 0 songs. "
                "libgpod does not know this model. Fix: sudo ipod-read-sysinfo-extended "
                f"{self.ipod.source.rstrip('0123456789')} {self.ipod.mount}  and sync again.")
        self.ipod.clear_rewrite_marker()
        self._update_profile()

        # libgpod warns about smart playlist rules it does not know; harmless
        failed = [l for l in errors.splitlines() if l.strip() and "itdb_splr_validate" not in l]
        message = f"+{len(new_ids)} / -{len(self.removes)} songs"
        if self.changed_playlists:
            message += f", {len(self.changed_playlists)} playlists updated"
        if self.ratings_from_ipod:
            message += f", {len(self.ratings_from_ipod)} ratings/play counts from the iPod"
        if self.ratings_to_ipod:
            message += f", {len(self.ratings_to_ipod)} onto the iPod"
        if imported:
            message += f", imported: {', '.join(imported)}"
        if failed:
            message += f", {len(failed)} problems:\n  " + "\n  ".join(failed[:10])
        return message

    def _copy_and_remove(self, progress):
        """Run the removals and additions through the helper. Returns ({path: dbid}, helper errors)."""
        with tempfile.TemporaryDirectory() as tmp:
            commands = [f"remove\t{dbid}" for dbid in self.removes]
            for i, path in enumerate(self.adds):
                meta = self.songs[path]
                cover = cover_of(path, os.path.join(tmp, f"{i}.jpg"))
                fields = ["add", path, cover, FILETYPES[os.path.splitext(path)[1].lower()], meta["title"],
                          meta["artist"], meta["albumartist"] or meta["artist"], meta["album"], meta["genre"],
                          meta["composer"], meta["year"], meta["track"], meta["tracks"], meta["disc"],
                          meta["discs"], meta["length_ms"], meta["bitrate"], meta["samplerate"],
                          meta["compilation"]]
                commands.append("\t".join(_clean(f) for f in fields))

            process = subprocess.Popen([IPOD_DB, self.ipod.mount, "apply"], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            process.stdin.write("\n".join(commands) + "\n")
            process.stdin.close()
            new_ids, done = {}, 0
            for line in process.stdout:
                parts = line.rstrip("\n").split("\t")
                if parts[0] == "added":
                    done += 1
                    new_ids[parts[2]] = parts[1]
                    if progress:
                        progress(f"Copying {done}/{len(self.adds)}: {os.path.basename(parts[2])}",
                                 done / max(len(self.adds), 1))
                elif parts[0] == "written" and progress:
                    progress("Writing iPod database", 1)
            errors = process.stderr.read()
            if process.wait() == 2:
                raise RuntimeError(errors.strip())
            return new_ids, errors

    def _playlists_and_ratings(self, new_ids, progress):
        """Second helper run: ratings, playlists, and playlists made on the iPod. Returns (imported, errors)."""
        commands, rating_errors = [], []
        for path, rating255, count, dbid in self.ratings_from_ipod:
            try:
                ratings.write(path, rating255, count)
            except Exception as e:
                rating_errors.append(f"cannot write rating to {os.path.basename(path)}: {e}")
                continue
            commands.append(f"rated\t{dbid}\t{ratings.stars_of_popm(rating255) * 20}\t{count}")
        for dbid, rating100, count in self.ratings_to_ipod:
            commands.append(f"rated\t{dbid}\t{rating100}\t{count}")

        imported = []
        store = Playlists(self.library.settings)
        for name, paths in self.backsync.items():
            if store.write(name, paths):
                imported.append(name)
                if name not in self.profile["playlists"]:
                    self.profile["playlists"].append(name)
                self.playlists[name] = [p for p in paths if p in self.songs]

        removed = set(self.removes)
        ids = {**{p: d for p, d in self.matched.items() if d not in removed}, **new_ids}
        commands += [f"delplaylist\t{_clean(name)}" for name in self.drop_playlists]
        for name, paths in self.playlists.items():
            if name in self.changed_playlists or name in imported or any(p in new_ids for p in paths):
                commands.append(f"playlist\t{_clean(name)}\t{','.join(ids[p] for p in paths if p in ids)}")
        if commands:
            if progress:
                progress("Writing playlists and ratings", 1)
            result = self.ipod.run_helper("apply", stdin="\n".join(commands) + "\n")
            if result.returncode == 2:
                raise RuntimeError(result.stderr.strip())
            rating_errors += [l for l in result.stderr.splitlines() if l.strip()]
        return imported, rating_errors

    def _update_profile(self):
        self.profile["managed_playlists"] = sorted(self.playlists)
        self.profile["known_ipod_playlists"] = sorted(
            (set(self.ipod_playlist_names) - set(self.drop_playlists)) | set(self.playlists))
        self.profile["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.profile["capacity"] = self.budget
        self.profile.save()


def import_ipod_playlists(ipod, library, progress=None):
    """Copy the iPod's own playlists into the playlist source. Returns [(name, found, missing, created)]."""
    tracks, ipod_playlists = ipod.read(progress)
    path_of = {d: p for p, d in match_tracks(library.songs, tracks).items()}
    store = Playlists(library.settings)
    out = []
    for name, dbids in ipod_playlists.items():
        paths = [path_of[d] for d in dbids if d in path_of]
        out.append((name, len(paths), len(dbids) - len(paths), store.write(name, paths)))
    return out
