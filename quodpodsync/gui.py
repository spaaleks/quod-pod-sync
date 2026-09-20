"""The Quod Pod Sync window.

Shows artists, albums and playlists with checkboxes per iPod, plus the space it would take.
"""
import os
import threading
from collections import defaultdict

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, GObject, Gtk, Pango  # noqa: E402

from .deps import Dependencies  # noqa: E402
from .ipod import IPod  # noqa: E402
from .library import Library  # noqa: E402
from .paths import RESERVE_BYTES  # noqa: E402
from .playlists import Playlists  # noqa: E402
from .settings import Profile, Settings, default_music_dir  # noqa: E402
from .sync import SyncPlan, import_ipod_playlists  # noqa: E402

COL_ON, COL_MIXED, COL_NAME, COL_INFO, COL_KIND, COL_ARTIST, COL_ALBUM, COL_BYTES = range(8)


def songs_text(n):
    return f"{n} song" if n == 1 else f"{n} songs"


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit == "GB" else f"{n:.0f} {unit}"
        n /= 1024


class Manager(Gtk.Window):
    def __init__(self):
        super().__init__(title="Quod Pod Sync")
        self.set_default_size(760, 720)
        self.set_icon_name("quodpodsync")
        self.library = Library()
        self.songs = self.library.songs
        self.playlists = Playlists(self.library.settings).available()
        self.entries = []  # [(serial, label, Device or None)]
        self.dev = None
        self.prof = None
        self.ipod_music = None  # bytes of music currently on the selected iPod
        self.busy = False

        self.group_albums()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=10)
        self.add(box)

        # device row
        row = Gtk.Box(spacing=8)
        row.pack_start(Gtk.Label(label="iPod:"), False, False, 0)
        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.connect("changed", self.on_device_changed)
        row.pack_start(self.device_combo, True, True, 0)
        refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        refresh.set_tooltip_text("Look for connected iPods")
        refresh.connect("clicked", lambda *_: self.load_devices())
        row.pack_start(refresh, False, False, 0)
        self.eject_button = Gtk.Button.new_from_icon_name("media-eject-symbolic", Gtk.IconSize.BUTTON)
        self.eject_button.set_tooltip_text("Unmount and power off the iPod, then it is safe to unplug")
        self.eject_button.connect("clicked", self.on_eject)
        row.pack_start(self.eject_button, False, False, 0)
        self.export_button = Gtk.Button.new_from_icon_name("document-save-symbolic", Gtk.IconSize.BUTTON)
        self.export_button.set_tooltip_text("Copy songs from the iPod into a folder")
        self.export_button.connect("clicked", self.on_export)
        row.pack_start(self.export_button, False, False, 0)
        self.restore_button = Gtk.Button.new_from_icon_name("document-revert-symbolic", Gtk.IconSize.BUTTON)
        self.restore_button.set_tooltip_text("Put an earlier iPod database back (e.g. if the iPod shows no songs)")
        self.restore_button.connect("clicked", self.on_restore)
        row.pack_start(self.restore_button, False, False, 0)
        settings = Gtk.Button.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.BUTTON)
        settings.set_tooltip_text("Library folders")
        settings.connect("clicked", self.on_settings)
        row.pack_start(settings, False, False, 0)
        box.pack_start(row, False, False, 0)
        self.device_info = Gtk.Label(xalign=0)
        self.device_info.get_style_context().add_class("dim-label")
        box.pack_start(self.device_info, False, False, 0)

        self.everything = Gtk.CheckButton(label="Sync everything in the library folders")
        self.everything.connect("toggled", self.on_everything)
        box.pack_start(self.everything, False, False, 0)

        notebook = Gtk.Notebook()
        box.pack_start(notebook, True, True, 0)
        notebook.append_page(self.build_tree(), Gtk.Label(label="Artists & Albums"))
        notebook.append_page(self.build_playlists(), Gtk.Label(label="Playlists"))

        self.extra_files = Gtk.Label(xalign=0)
        box.pack_start(self.extra_files, False, False, 0)

        # storage bar
        self.level = Gtk.LevelBar(min_value=0, max_value=1)
        self.level.add_offset_value("full", 1.0)
        box.pack_start(self.level, False, False, 0)
        self.space_label = Gtk.Label(xalign=0)
        box.pack_start(self.space_label, False, False, 0)

        # progress + buttons
        self.progress = Gtk.ProgressBar(show_text=True, no_show_all=True)
        box.pack_start(self.progress, False, False, 0)
        bottom = Gtk.Box(spacing=8)
        self.autosync = Gtk.CheckButton(label="Sync automatically when plugged in")
        self.autosync.connect("toggled", lambda *_: self.mark_dirty())
        bottom.pack_start(self.autosync, False, False, 0)
        self.sync_button = Gtk.Button(label="Sync now")
        self.sync_button.get_style_context().add_class("suggested-action")
        self.sync_button.connect("clicked", self.on_sync)
        bottom.pack_end(self.sync_button, False, False, 0)
        self.save_button = Gtk.Button(label="Save")
        self.save_button.connect("clicked", lambda *_: self.save())
        bottom.pack_end(self.save_button, False, False, 0)
        box.pack_start(bottom, False, False, 0)

        self.connect("delete-event", self.on_close)
        self.load_devices()
        GLib.idle_add(self.check_dependencies)

    # ------------------------------------------------------------ widgets

    def group_albums(self):
        self.albums = self.library.by_artist()

    def build_tree(self):
        self.store = Gtk.TreeStore(bool, bool, str, str, str, str, str, GObject.TYPE_INT64)
        self.fill_tree()
        self.filter_text = ""
        self.filtered = self.store.filter_new()
        self.filtered.set_visible_func(self.visible)
        self.view = Gtk.TreeView(model=self.filtered, headers_visible=False, enable_search=False)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.on_toggle)
        self.view.append_column(Gtk.TreeViewColumn("", toggle, active=COL_ON, inconsistent=COL_MIXED))
        name = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        col = Gtk.TreeViewColumn("Name", name, text=COL_NAME)
        col.set_expand(True)
        self.view.append_column(col)
        info = Gtk.CellRendererText(xalign=1)
        info.set_property("foreground", "gray")
        self.view.append_column(Gtk.TreeViewColumn("Info", info, text=COL_INFO))

        search = Gtk.SearchEntry(placeholder_text="Filter artists and albums")
        search.connect("search-changed", self.on_search)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(self.view)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)
        page.pack_start(search, False, False, 0)
        page.pack_start(scroll, True, True, 0)
        self.tree_page = page
        return page

    def fill_tree(self):
        self.store.clear()
        for artist in sorted(self.albums, key=str.casefold):
            count = sum(c for c, _ in self.albums[artist].values())
            size = sum(b for _, b in self.albums[artist].values())
            it = self.store.append(None, [False, False, artist, f"{songs_text(count)} · {human(size)}", "artist", artist, "", size])
            for album in sorted(self.albums[artist], key=str.casefold):
                c, b = self.albums[artist][album]
                self.store.append(it, [False, False, album, f"{songs_text(c)} · {human(b)}", "album", artist, album, b])

    def fill_playlists(self):
        chosen = set(self.prof.get("playlists", [])) if self.prof else set()
        self.pl_store.clear()
        for name, paths in sorted(self.playlists.items(), key=lambda x: x[0].casefold()):
            present = [p for p in paths if p in self.songs]
            size = sum(self.songs[p]["size"] for p in present)
            info = f"{songs_text(len(present))} · {human(size)}"
            if len(present) < len(paths):
                info += f" · {len(paths) - len(present)} outside library"
            self.pl_store.append([name in chosen, name, info])

    def build_playlists(self):
        self.pl_store = Gtk.ListStore(bool, str, str)
        self.fill_playlists()
        view = Gtk.TreeView(model=self.pl_store, headers_visible=False)
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self.on_pl_toggle)
        view.append_column(Gtk.TreeViewColumn("", toggle, active=0))
        col = Gtk.TreeViewColumn("Name", Gtk.CellRendererText(), text=1)
        col.set_expand(True)
        view.append_column(col)
        info = Gtk.CellRendererText(xalign=1)
        info.set_property("foreground", "gray")
        view.append_column(Gtk.TreeViewColumn("Info", info, text=2))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(view)
        self.pl_view = view
        self.all_playlists = Gtk.CheckButton(label="All playlists (including ones created later)")
        self.all_playlists.connect("toggled", self.on_all_playlists)
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=6)
        page.pack_start(self.all_playlists, False, False, 0)
        hint = Gtk.Label(xalign=0, wrap=True)
        hint.set_markup(f"<small>Quod Libet playlists from {GLib.markup_escape_text(Playlists(self.library.settings).folder)}. "
                        "Ticked playlists are created on the iPod, their songs are synced too. "
                        "Playlists you save on the iPod (On-The-Go) are imported here on the next sync.</small>")
        page.pack_start(hint, False, False, 0)
        page.pack_start(scroll, True, True, 0)
        self.import_button = Gtk.Button(label="Import playlists from this iPod into Quod Libet…")
        self.import_button.connect("clicked", self.on_import)
        page.pack_start(self.import_button, False, False, 0)
        return page

    def check_dependencies(self, retry=False):
        """ipod-db needs libgpod; offer to install it if it was removed (e.g. with another program)."""
        problem = Dependencies.missing()
        if not problem:
            return False
        cmd = Dependencies.install_command()
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.ERROR,
                                   text="Quod Pod Sync cannot talk to iPods")
        dialog.format_secondary_text(problem + ("\n\nInstall it now? You will be asked for your password."
                                                if cmd else ""))
        if cmd:
            dialog.add_buttons("Not now", Gtk.ResponseType.CANCEL, "Install", Gtk.ResponseType.OK)
        else:
            dialog.add_buttons("OK", Gtk.ResponseType.CANCEL)
        install = dialog.run() == Gtk.ResponseType.OK
        dialog.destroy()
        if not install:
            return False
        self.show_progress("Installing " + " ".join(cmd[-1:]) + " ...", 0)
        while Gtk.events_pending():
            Gtk.main_iteration()
        ok, output = Dependencies.install()
        self.progress.hide()
        if ok:
            self.finish("Installed, iPod sync works again.")
        elif not retry:
            self.finish(f"Installation failed:\n{output[-500:]}", True)
        return not ok

    # ------------------------------------------------------------ devices / profiles

    def load_devices(self):
        devs = {d.serial: d for d in IPod.connected()}
        entries = [(s, d.label, d) for s, d in devs.items()]
        for p in Profile.all_known():
            if p["serial"] not in devs:
                entries.append((p["serial"], f"{p.get('name') or p['serial']} (not connected)", None))
        current = self.prof["serial"] if self.prof else None
        self.entries = entries
        self.device_combo.remove_all()
        for _, label, _ in entries:
            self.device_combo.append_text(label)
        if not entries:
            self.device_info.set_text("No iPod connected and no saved profiles. Plug in an iPod and press refresh.")
            self.set_sensitive_all(False)
            return
        idx = next((i for i, e in enumerate(entries) if e[0] == current), 0)
        self.device_combo.set_active(idx)

    def set_sensitive_all(self, on):
        for w in (self.everything, self.tree_page, self.save_button, self.autosync, self.import_button):
            w.set_sensitive(on)
        self.sync_button.set_sensitive(on and self.dev is not None)
        self.eject_button.set_sensitive(self.dev is not None and not self.busy)
        self.restore_button.set_sensitive(self.dev is not None and not self.busy)
        self.export_button.set_sensitive(self.dev is not None and not self.busy)

    def on_device_changed(self, combo):
        i = combo.get_active()
        if i < 0 or i >= len(self.entries):
            return
        serial, label, dev = self.entries[i]
        self.dev = dev
        self.prof = Profile(serial, label.replace(" (not connected)", ""))
        self.set_sensitive_all(True)
        self.import_button.set_sensitive(dev is not None)
        self.apply_profile_to_ui()
        self.ipod_music = None
        if dev:
            self.device_info.set_text(f"Serial {serial} · {dev.mount} · {human(dev.total)} · last sync: {self.prof.get('last_sync') or 'never'}")
            threading.Thread(target=self.load_ipod_usage, args=(dev,), daemon=True).start()
        else:
            self.device_info.set_text(f"Serial {serial} · not connected, changes are used at the next sync")
        self.update_space()
        self.dirty = False

    def load_ipod_usage(self, dev):
        try:
            tracks, _ = dev.read(lambda msg, frac: GLib.idle_add(self.show_progress, msg, frac))
            music = sum(t["size"] for t in tracks.values())
        except Exception as e:
            GLib.idle_add(self.device_info.set_text, f"Cannot read iPod: {e}")
            return
        GLib.idle_add(self.set_ipod_usage, dev, music)

    def set_ipod_usage(self, dev, music):
        if dev is self.dev:
            self.ipod_music = music
            self.prof["capacity"] = dev.free + music - RESERVE_BYTES
            self.progress.hide()
            self.update_space()

    def apply_profile_to_ui(self):
        p = self.prof
        artists = set(p.get("artists", []))
        albums = {tuple(a) for a in p.get("albums", [])}
        self.loading = True
        it = self.store.get_iter_first()
        while it:
            artist = self.store[it][COL_ARTIST]
            child = self.store.iter_children(it)
            while child:
                self.store[child][COL_ON] = artist in artists or (artist, self.store[child][COL_ALBUM]) in albums
                child = self.store.iter_next(child)
            self.update_parent(it)
            it = self.store.iter_next(it)
        for row in self.pl_store:
            row[0] = row[1] in p.get("playlists", [])
        self.all_playlists.set_active(bool(p.get("all_playlists")))
        self.pl_view.set_sensitive(not p.get("all_playlists"))
        if p.get("all_playlists"):
            for row in self.pl_store:
                row[0] = True
        self.everything.set_active(bool(p.get("everything")))
        self.autosync.set_active(bool(p.get("autosync")))
        self.tree_page.set_sensitive(not p.get("everything"))
        files = [f for f in p.get("files", []) if f in self.songs]
        self.extra_files.set_visible(bool(files))
        self.extra_files.set_markup(f"<small>+ {len(files)} single songs added from Quod Libet</small>" if files else "")
        self.loading = False

    def ui_to_profile(self):
        p = self.prof
        artists, albums = [], []
        for row in self.store:
            kids = list(row.iterchildren())
            if row[COL_ON] and not row[COL_MIXED]:
                artists.append(row[COL_ARTIST])
            else:
                albums += [[row[COL_ARTIST], k[COL_ALBUM]] for k in kids if k[COL_ON]]
        p["artists"], p["albums"] = artists, albums
        p["all_playlists"] = self.all_playlists.get_active()
        if not p["all_playlists"]:  # keep the individual ticks for when "all" is switched off again
            p["playlists"] = [row[1] for row in self.pl_store if row[0]]
        p["everything"] = self.everything.get_active()
        p["autosync"] = self.autosync.get_active()
        return p

    def save(self):
        self.ui_to_profile().save()
        self.dirty = False
        self.space_label.set_text(self.space_label.get_text().split(" · saved")[0] + " · saved")

    def mark_dirty(self):
        if not getattr(self, "loading", False):
            self.dirty = True
            self.update_space()

    # ------------------------------------------------------------ selection

    def visible(self, model, it, _data):
        if not self.filter_text:
            return True
        if self.filter_text in model[it][COL_NAME].casefold():
            return True
        if model[it][COL_KIND] == "album":
            return self.filter_text in model[it][COL_ARTIST].casefold()
        child = model.iter_children(it)
        while child:
            if self.filter_text in model[child][COL_NAME].casefold():
                return True
            child = model.iter_next(child)
        return False

    def on_search(self, entry):
        self.filter_text = entry.get_text().strip().casefold()
        self.filtered.refilter()
        if self.filter_text:
            self.view.expand_all()

    def on_toggle(self, _renderer, path):
        it = self.filtered.convert_iter_to_child_iter(self.filtered.get_iter(path))
        row = self.store[it]
        state = not row[COL_ON] or row[COL_MIXED]
        if row[COL_KIND] == "artist":
            row[COL_ON], row[COL_MIXED] = state, False
            child = self.store.iter_children(it)
            while child:
                self.store[child][COL_ON] = state
                child = self.store.iter_next(child)
        else:
            row[COL_ON] = state
            self.update_parent(self.store.iter_parent(it))
        self.mark_dirty()

    def update_parent(self, it):
        states = []
        child = self.store.iter_children(it)
        while child:
            states.append(self.store[child][COL_ON])
            child = self.store.iter_next(child)
        self.store[it][COL_ON] = bool(states) and all(states)
        self.store[it][COL_MIXED] = any(states) and not all(states)

    def on_pl_toggle(self, _renderer, path):
        if self.all_playlists.get_active():
            return
        self.pl_store[path][0] = not self.pl_store[path][0]
        self.mark_dirty()

    def on_all_playlists(self, button):
        on = button.get_active()
        self.pl_view.set_sensitive(not on)
        if on:
            for row in self.pl_store:
                row[0] = True
        elif not getattr(self, "loading", False):
            chosen = set(self.prof.get("playlists", []))
            for row in self.pl_store:
                row[0] = row[1] in chosen
        self.mark_dirty()

    def on_everything(self, button):
        self.tree_page.set_sensitive(not button.get_active())
        self.mark_dirty()

    def show_playlist_songs(self, prof):
        """Mark artists/albums whose songs are on the iPod because of a ticked playlist."""
        via = defaultdict(lambda: defaultdict(int))  # artist -> album -> songs coming from playlists
        if not prof.get("everything"):
            for name in prof.wanted_playlists(self.playlists):
                for p in set(self.playlists.get(name, [])):
                    m = self.songs.get(p)
                    if m:
                        via[m["group_artist"]][m["group_album"]] += 1
        for row in self.store:
            artist = row[COL_ARTIST]
            count = sum(c for c, _ in self.albums[artist].values())
            size = sum(b for _, b in self.albums[artist].values())
            n = sum(via[artist].values()) if artist in via else 0
            row[COL_INFO] = f"{songs_text(count)} · {human(size)}" + (f" · {n} via playlist" if n and not row[COL_ON] else "")
            for kid in row.iterchildren():
                c, b = self.albums[artist][kid[COL_ALBUM]]
                k = via[artist][kid[COL_ALBUM]] if artist in via else 0
                kid[COL_INFO] = f"{songs_text(c)} · {human(b)}" + (f" · {k} via playlist" if k and not kid[COL_ON] else "")

    def update_space(self):
        if not self.prof:
            return
        prof = self.ui_to_profile() if not getattr(self, "loading", False) else self.prof
        sel = prof.selection(self.songs, self.playlists)
        size = self.library.size_of(sel)
        self.show_playlist_songs(prof)
        if self.dev and self.ipod_music is not None:
            budget = self.dev.free + self.ipod_music - RESERVE_BYTES
        else:
            budget = self.prof.get("capacity") or (self.dev.total - RESERVE_BYTES if self.dev else 0)
        frac = size / budget if budget else 0
        self.level.set_value(min(frac, 1))
        if budget:
            text = f"Selected: {songs_text(len(sel))} · {human(size)} of {human(budget)} usable"
            text += "  ⚠ TOO MUCH" if frac > 1 else f"  ({frac:.0%})"
        else:
            text = f"Selected: {songs_text(len(sel))} · {human(size)} (capacity known after first connect)"
        if self.dev and self.ipod_music is None:
            text += " · reading iPod…"
        self.space_label.set_text(text)
        self.sync_button.set_sensitive(self.dev is not None and not self.busy and frac <= 1)

    # ------------------------------------------------------------ sync

    def show_progress(self, msg, frac):
        self.progress.show()
        self.progress.set_text(msg)
        self.progress.set_fraction(max(0, min(frac, 1)))

    def on_sync(self, _button):
        if self.check_dependencies():
            return
        self.save()
        self.busy = True
        self.sync_button.set_sensitive(False)
        self.show_progress("Comparing library and iPod…", 0)
        threading.Thread(target=self.plan_thread, args=(self.dev, self.prof), daemon=True).start()

    def plan_thread(self, dev, prof):
        try:
            dev.refresh_space()
            plan = SyncPlan.make(dev, prof, self.library, lambda m, f: GLib.idle_add(self.show_progress, m, f))
        except Exception as e:
            GLib.idle_add(self.finish, f"Sync failed: {e}", True)
            return
        GLib.idle_add(self.confirm, plan)

    def confirm(self, plan):
        if plan.empty:
            self.finish("Already in sync.")
            return
        dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   buttons=Gtk.ButtonsType.OK_CANCEL, text="Sync this iPod?")
        dialog.format_secondary_text(plan.summary() + "\n\nRemoved songs are only removed from the iPod, never from your library.")
        ok = dialog.run() == Gtk.ResponseType.OK and plan.fits
        dialog.destroy()
        if not ok:
            self.finish("Cancelled." if plan.fits else "Selection does not fit.", not plan.fits)
            return
        threading.Thread(target=self.apply_thread, args=(plan,), daemon=True).start()

    def apply_thread(self, plan):
        try:
            result = plan.apply(lambda m, f: GLib.idle_add(self.show_progress, m, f))
        except Exception as e:
            GLib.idle_add(self.finish, f"Sync failed: {e}", True)
            return
        GLib.idle_add(self.finish, f"Synced: {result}. Safe to eject.")

    def finish(self, msg, error=False):
        self.busy = False
        self.set_sensitive_all(True)
        self.progress.hide()
        if self.prof:
            self.prof = Profile(self.prof["serial"], self.prof.get("name"))
            self.playlists = Playlists(self.library.settings).available()
            self.fill_playlists()
            self.apply_profile_to_ui()
            self.dirty = False
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.OK, text=msg)
        dialog.run()
        dialog.destroy()
        if self.dev:
            self.dev.refresh_space()
            self.ipod_music = None
            threading.Thread(target=self.load_ipod_usage, args=(self.dev,), daemon=True).start()
        self.update_space()

    def on_import(self, _button):
        if not self.dev:
            return
        self.show_progress("Importing playlists…", 0)

        def work(dev):
            try:
                res = import_ipod_playlists(dev, self.library, lambda m, f: GLib.idle_add(self.show_progress, m, f))
                lines = [f"{n}: {found} songs" + (f", {miss} not in library" if miss else "") + ("" if made else " (exists, skipped)")
                         for n, found, miss, made in res]
                msg = "Imported into Quod Libet (restart it to see them):\n" + "\n".join(lines)
            except Exception as e:
                GLib.idle_add(self.finish, f"Import failed: {e}", True)
                return
            GLib.idle_add(self.after_import, msg)

        threading.Thread(target=work, args=(self.dev,), daemon=True).start()

    def after_import(self, msg):
        self.playlists = Playlists(self.library.settings).available()
        self.fill_playlists()
        self.finish(msg)

    # ------------------------------------------------------------ library folders

    def on_settings(self, _button):
        dialog = Gtk.Dialog(title="Library folders", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        dialog.set_default_size(560, 320)
        area = dialog.get_content_area()
        area.set_spacing(6)
        area.set_border_width(10)
        area.pack_start(Gtk.Label(label="Songs from these folders (and their subfolders) can be synced:", xalign=0), False, False, 0)
        store = Gtk.ListStore(str)
        for f in self.library.settings.folders:
            store.append([f])
        view = Gtk.TreeView(model=store, headers_visible=False)
        view.append_column(Gtk.TreeViewColumn("Folder", Gtk.CellRendererText(), text=0))
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(view)
        area.pack_start(scroll, True, True, 0)
        buttons = Gtk.Box(spacing=6)
        add = Gtk.Button(label="Add folder…")
        remove = Gtk.Button(label="Remove")
        buttons.pack_start(add, False, False, 0)
        buttons.pack_start(remove, False, False, 0)
        area.pack_start(buttons, False, False, 0)

        def on_add(_b):
            chooser = Gtk.FileChooserDialog(title="Add library folder", transient_for=dialog,
                                            action=Gtk.FileChooserAction.SELECT_FOLDER)
            chooser.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Add", Gtk.ResponseType.OK)
            chooser.set_current_folder(default_music_dir())
            if chooser.run() == Gtk.ResponseType.OK:
                folder = chooser.get_filename()
                if folder and folder not in [r[0] for r in store]:
                    store.append([folder])
            chooser.destroy()

        def on_remove(_b):
            model, it = view.get_selection().get_selected()
            if it:
                model.remove(it)

        add.connect("clicked", on_add)
        remove.connect("clicked", on_remove)
        dialog.show_all()
        folders = [r[0] for r in store] if dialog.run() == Gtk.ResponseType.OK else None
        dialog.destroy()
        if folders is None or folders == self.library.settings.folders:
            return
        settings = Settings()
        settings.folders = folders
        settings.save()
        self.show_progress("Scanning library folders…", 0)
        while Gtk.events_pending():
            Gtk.main_iteration()
        self.reload_library()
        self.progress.hide()

    def reload_library(self):
        if self.prof:
            self.ui_to_profile()
        self.library = Library()
        self.songs = self.library.songs
        self.playlists = Playlists(self.library.settings).available()
        self.group_albums()
        self.fill_tree()
        self.fill_playlists()
        if self.prof:
            self.apply_profile_to_ui()
        self.update_space()

    def on_eject(self, _button):
        if not self.dev or self.busy:
            return
        if getattr(self, "dirty", False):
            self.save()
        try:
            msg = self.dev.eject()
            error = False
        except RuntimeError as e:
            msg, error = f"Eject failed: {e}", True
        dialog = Gtk.MessageDialog(transient_for=self, modal=True,
                                   message_type=Gtk.MessageType.ERROR if error else Gtk.MessageType.INFO,
                                   buttons=Gtk.ButtonsType.OK, text=msg)
        dialog.run()
        dialog.destroy()
        if not error:
            self.load_devices()

    def on_export(self, _button):
        """Copy songs off the iPod, for example ones that exist only there."""
        if not self.dev or self.busy:
            return
        dialog = Gtk.Dialog(title="Copy from iPod", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Copy", Gtk.ResponseType.OK)
        area = dialog.get_content_area()
        area.set_spacing(8)
        area.set_border_width(10)
        area.pack_start(Gtk.Label(xalign=0, wrap=True,
                                  label="Songs are copied in the usual Artist/Album layout, using their own tags. "
                                        "Nothing on the iPod is changed."), False, False, 0)
        chooser = Gtk.FileChooserButton(title="Target folder", action=Gtk.FileChooserAction.SELECT_FOLDER)
        chooser.set_current_folder(os.path.expanduser("~"))
        area.pack_start(chooser, False, False, 0)
        only_missing = Gtk.CheckButton(label="Only songs that are not in the library", active=True)
        area.pack_start(only_missing, False, False, 0)
        dialog.show_all()
        ok = dialog.run() == Gtk.ResponseType.OK
        target, missing_only = chooser.get_filename(), only_missing.get_active()
        dialog.destroy()
        if not (ok and target):
            return
        self.busy = True
        self.set_sensitive_all(False)
        self.show_progress("Reading the iPod ...", 0)
        threading.Thread(target=self.export_thread, args=(self.dev, target, missing_only), daemon=True).start()

    def export_thread(self, dev, target, missing_only):
        try:
            copied, skipped, failed = dev.export(
                target, library=self.library, only_missing=missing_only,
                progress=lambda m, f: GLib.idle_add(self.show_progress, m, f))
        except Exception as e:
            GLib.idle_add(self.finish, f"Copying failed: {e}", True)
            return
        message = f"{copied} songs copied to {target}"
        if skipped:
            message += f", {skipped} were already there"
        if failed:
            message += f"\n{len(failed)} failed:\n  " + "\n  ".join(failed[:5])
        GLib.idle_add(self.finish, message, bool(failed))

    def on_restore(self, _button):
        if not self.dev or self.busy:
            return
        backups = self.dev.backups()
        if not backups:
            self.finish("There are no database backups for this iPod yet.")
            return
        dialog = Gtk.Dialog(title="Restore iPod database", transient_for=self, modal=True)
        dialog.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Restore", Gtk.ResponseType.OK)
        dialog.set_default_size(460, 300)
        area = dialog.get_content_area()
        area.set_spacing(6)
        area.set_border_width(10)
        label = Gtk.Label(xalign=0, wrap=True)
        label.set_markup("A backup is made before every sync. Restoring is useful if the iPod shows no songs "
                         "or you want its old play counts back.\n<small>Only the database is restored, not deleted "
                         "songs. The current database is saved first.</small>")
        area.pack_start(label, False, False, 0)
        store = Gtk.ListStore(str, str)
        for folder, stamp, size, signed in backups:
            store.append([f"{stamp}   {size / 1024**2:.1f} MB" + ("" if signed else "   (unsigned!)"), folder])
        view = Gtk.TreeView(model=store, headers_visible=False)
        view.append_column(Gtk.TreeViewColumn("Backup", Gtk.CellRendererText(), text=0))
        view.get_selection().select_path(0)
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.add(view)
        area.pack_start(scroll, True, True, 0)
        dialog.show_all()
        ok = dialog.run() == Gtk.ResponseType.OK
        model, it = view.get_selection().get_selected()
        folder = model[it][1] if it else None
        dialog.destroy()
        if not (ok and folder):
            return
        try:
            self.finish(self.dev.restore(folder))
        except (RuntimeError, OSError) as e:
            self.finish(f"Restore failed: {e}", True)

    def on_close(self, *_):
        if getattr(self, "dirty", False):
            dialog = Gtk.MessageDialog(transient_for=self, modal=True, message_type=Gtk.MessageType.QUESTION,
                                       buttons=Gtk.ButtonsType.YES_NO, text="Save changes to this iPod's selection?")
            if dialog.run() == Gtk.ResponseType.YES:
                self.save()
            dialog.destroy()
        return False


APP_ID = "io.github.quodpodsync.QuodPodSync"


def main():
    """Single instance: starting it again just brings the open window to the front."""
    GLib.set_prgname("quodpodsync")
    GLib.set_application_name("Quod Pod Sync")
    app = Gtk.Application(application_id=APP_ID)

    def activate(app):
        if app.get_windows():
            app.get_windows()[0].present()
            return
        win = Manager()
        app.add_window(win)
        win.show_all()
        win.progress.hide()

    app.connect("activate", activate)
    return app.run([])

