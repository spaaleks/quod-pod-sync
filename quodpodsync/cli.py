"""Command line for Quod Pod Sync.

Without arguments it opens the window.
"""
import argparse
import os
import subprocess
import sys
import time

from .deps import Dependencies
from .ipod import IPod
from .library import Library
from .playlists import Playlists
from .settings import Profile, Settings
from .sync import SyncPlan, import_ipod_playlists

# autosync refuses to remove more than this share of the iPod without confirmation in the GUI
AUTOSYNC_MAX_REMOVE = 0.25


USAGE = """
examples:
  quod-pod-sync                  open the window
  quod-pod-sync status           what a sync would do
  quod-pod-sync sync -n          dry run
  quod-pod-sync add FILE|DIR     add songs to the selection and sync
  quod-pod-sync playlist NAME    add a playlist and sync
  quod-pod-sync eject            unmount and power off
  quod-pod-sync install-deps     install missing system packages
  quod-pod-sync restore [N]      put an earlier iPod database back
  quod-pod-sync export FOLDER    copy songs off the iPod into that folder
"""


def notify(title, body, urgent=False):
    print(f"{title}: {body}")
    try:
        subprocess.run(["notify-send", "-a", "Quod Pod Sync", "-i", "quodpodsync", "-u", "critical" if urgent else "normal", title, body], timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def progress(msg, frac):
    sys.stdout.write(f"\r\033[K{msg}")
    sys.stdout.flush()
    if frac >= 1:
        sys.stdout.write("\n")


def fail(msg):
    notify("Quod Pod Sync", msg, urgent=True)
    sys.exit(1)


def pick_devices(serial=None):
    devs = IPod.connected()
    if serial:
        devs = [d for d in devs if d.serial == serial]
    if not devs:
        fail("No iPod connected (looked for /run/media/*/*/iPod_Control).")
    return devs


def configured_device(serial=None):
    """First connected iPod that already has a profile (used by the Quod Libet commands)."""
    for dev in pick_devices(serial):
        profile = dev.profile()
        if profile.exists:
            return dev, profile
    fail("Set up this iPod in Quod Pod Sync first (choose what to sync, then Save).")


def cmd_status(args):
    check_deps()
    songs = Library()
    for dev in pick_devices(args.serial):
        prof = dev.profile()
        plan = SyncPlan.make(dev, prof, songs, progress)
        sel = songs.size_of(plan.selected)
        print(f"\n{dev.label}  serial {dev.serial}  mounted at {dev.mount}")
        print(f"  on iPod: {len(plan.tracks)} songs, {plan.music_bytes / 1024**3:.2f} GB   free: {dev.free / 1024**3:.2f} GB")
        print(f"  selected: {len(plan.selected)} songs, {sel / 1024**3:.2f} GB of {plan.budget / 1024**3:.2f} GB usable")
        print(f"  last sync: {prof.get('last_sync') or 'never'}   autosync: {'on' if prof.get('autosync') else 'off'}")
        print("  pending: " + ("nothing" if plan.empty else plan.summary().replace("\n", "\n           ")))


def sync_device(dev, prof, songs, dry_run=False, yes=False, auto=False):
    plan = SyncPlan.make(dev, prof, songs, None if auto else progress)
    name = prof.get("name") or dev.label
    if plan.empty:
        print(f"{name}: already in sync")
        return True
    print(f"{name}:\n  " + plan.summary().replace("\n", "\n  "))
    if not plan.fits:
        notify(f"{name}: selection too big", plan.summary(), urgent=True)
        return False
    if dry_run:
        return True
    if auto and len(plan.removes) > 50 and len(plan.removes) > AUTOSYNC_MAX_REMOVE * max(len(plan.tracks), 1):
        notify(f"{name}: not synced", f"Would remove {len(plan.removes)} songs. Open Quod Pod Sync to confirm.", urgent=True)
        return False
    if not (yes or auto):
        if input("Continue? [y/N] ").strip().lower() != "y":
            return False
    t0 = time.time()
    try:
        result = plan.apply(None if auto else progress)
    except Exception as e:
        notify(f"{name}: sync failed", str(e), urgent=True)
        return False
    notify(f"{name} synced", f"{result} ({time.time() - t0:.0f}s). Safe to eject.")
    return True


def cmd_sync(args):
    check_deps()
    songs = Library()
    ok = True
    for dev in pick_devices(args.serial):
        prof = dev.profile()
        ok &= sync_device(dev, prof, songs, args.dry_run, args.yes)
    return 0 if ok else 1


def cmd_add(args):
    dev, prof = configured_device(args.serial)
    files = set(prof.get("files", []))
    skipped = []
    for p in map(os.path.abspath, args.paths):
        if not Settings().contains(p):
            skipped.append(p)
        elif os.path.isdir(p):
            for root, _, names in os.walk(p):
                files.update(os.path.join(root, f) for f in names)
        else:
            files.add(p)
    if skipped:
        notify("Quod Pod Sync", f"{len(skipped)} songs skipped, they are not in a library folder (see settings)")
    prof["files"] = sorted(files)
    prof.save()
    return 0 if sync_device(dev, prof, Library(), auto=True) else 1


def cmd_playlist(args):
    dev, prof = configured_device(args.serial)
    if args.name not in Playlists(songs.settings).available():
        fail(f"No Quod Libet playlist named {args.name!r} in {Playlists().folder}")
    if args.name not in prof["playlists"]:
        prof["playlists"].append(args.name)
        prof.save()
    return 0 if sync_device(dev, prof, Library(), auto=True) else 1


def cmd_import_playlists(args):
    for dev in pick_devices(args.serial):
        for name, found, missing, created in import_ipod_playlists(dev, Library(), progress=progress):
            state = "created" if created else "exists already, skipped"
            print(f"  {name}: {found} songs" + (f", {missing} not found in the library" if missing else "") + f"  [{state}]")
    print(f"\nPlaylists are in {Playlists().folder}. Restart Quod Libet to see them.")


def cmd_folders(args):
    settings = Settings()
    folders = settings.folders
    for f in args.add or []:
        f = os.path.abspath(os.path.expanduser(f))
        if not os.path.isdir(f):
            sys.exit(f"not a folder: {f}")
        if f not in folders:
            folders.append(f)
    for f in args.remove or []:
        f = os.path.abspath(os.path.expanduser(f))
        folders = [x for x in folders if x != f]
    if args.add or args.remove:
        settings.folders = folders
        settings.save()
    print("Library folders:")
    for f in folders:
        print(f"  {f}")


def cmd_restore(args):
    dev = pick_devices(args.serial)[0]
    backups = dev.backups()
    if not backups:
        print(f"No database backups for {dev.label}")
        return 1
    if args.number is None:
        print(f"Database backups for {dev.label} (restore with: quod-pod-sync restore N):")
        for i, (_, stamp, size, signed) in enumerate(backups, 1):
            print(f"  {i}: {stamp}  {size / 1024**2:.1f} MB" + ("" if signed else "  (unsigned!)"))
        return 0
    if not 1 <= args.number <= len(backups):
        sys.exit(f"There is no backup {args.number} (1..{len(backups)})")
    folder, stamp, _, _ = backups[args.number - 1]
    print(f"Restoring the database from {stamp} onto {dev.label}")
    print(dev.restore(folder))
    return 0


def cmd_export(args):
    dev = pick_devices(args.serial)[0]
    songs = Library()
    copied, skipped, failed = dev.export(args.folder, library=songs, only_missing=not args.all, progress=progress)
    print(f"{copied} songs copied to {args.folder}" + (f", {skipped} already there" if skipped else ""))
    for line in failed[:10]:
        print(f"  failed: {line}")
    return 1 if failed else 0


def cmd_install_deps(args):
    problem = Dependencies.missing()
    if not problem:
        print("Everything is installed.")
        return 0
    print(problem)
    cmd = Dependencies.install_command()
    if not cmd:
        return 1
    print("Installing: " + " ".join(cmd))
    ok, output = Dependencies.install(graphical=not sys.stdin.isatty())
    print(output)
    return 0 if ok else 1


def check_deps(auto=False):
    """Stop early with a clear message if ipod-db cannot run."""
    problem = Dependencies.missing()
    if not problem:
        return
    cmd = Dependencies.install_command()
    hint = f"Fix it with: quod-pod-sync install-deps  (installs: {' '.join(cmd[-1:])})" if cmd else ""
    if auto:
        notify("Quod Pod Sync: sync not possible", f"{problem} {hint}", urgent=True)
        sys.exit(1)
    fail(f"{problem}\n{hint}")


def cmd_eject(args):
    ok = True
    for dev in pick_devices(args.serial):
        try:
            print(f"{dev.label}: {dev.eject()}")
        except RuntimeError as e:
            print(f"{dev.label}: {e}")
            ok = False
    return 0 if ok else 1


def cmd_autosync(args):
    check_deps(auto=True)
    # the trigger fires when the mount point appears; wait until the iPod is actually readable
    deadline = time.time() + args.wait
    time.sleep(args.delay)
    devs = IPod.connected()
    while not devs and time.time() < deadline:
        time.sleep(2)
        devs = IPod.connected()
    songs = None
    for dev in devs:
        prof = dev.profile()
        if not prof.exists:
            notify("New iPod connected", f"{dev.label}: open Quod Pod Sync to choose what to sync.")
            continue
        if not prof.get("autosync"):
            continue
        songs = Library() if songs is None else songs
        try:
            sync_device(dev, prof, songs, auto=True)
        except Exception as e:  # e.g. the iPod database could not be read
            notify(f"{prof.get('name') or dev.label}: sync failed", str(e), urgent=True)


def main():
    ap = argparse.ArgumentParser(prog="quod-pod-sync", description=__doc__, epilog=USAGE,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--serial", help="only this iPod (serial number)")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("gui", help="open the window (default)")
    sub.add_parser("status")
    p = sub.add_parser("sync")
    p.add_argument("-n", "--dry-run", action="store_true")
    p.add_argument("-y", "--yes", action="store_true")
    p = sub.add_parser("add")
    p.add_argument("paths", nargs="+")
    p = sub.add_parser("playlist")
    p.add_argument("name")
    sub.add_parser("import-playlists")
    p = sub.add_parser("folders")
    p.add_argument("--add", action="append")
    p.add_argument("--remove", action="append")
    sub.add_parser("eject")
    sub.add_parser("install-deps")
    p = sub.add_parser("export")
    p.add_argument("folder")
    p.add_argument("--all", action="store_true", help="also songs that are already in the library")
    p = sub.add_parser("restore")
    p.add_argument("number", nargs="?", type=int)
    p = sub.add_parser("autosync")
    p.add_argument("--delay", type=float, default=3)
    p.add_argument("--wait", type=float, default=20, help="seconds to wait for an iPod to appear")
    args = ap.parse_args()
    if args.cmd in (None, "gui"):
        from .gui import main as gui_main

        return gui_main()
    handler = {"status": cmd_status, "sync": cmd_sync, "add": cmd_add, "playlist": cmd_playlist,
               "import-playlists": cmd_import_playlists, "folders": cmd_folders, "eject": cmd_eject, "install-deps": cmd_install_deps, "restore": cmd_restore, "export": cmd_export, "autosync": cmd_autosync}[args.cmd]
    sys.exit(handler(args) or 0)

