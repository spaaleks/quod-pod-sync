"""Where playlists come from and go back to.

Either a folder of .m3u files, or Quod Libet's own XSPF playlists.
"""
import glob
import os
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse

from .paths import XSPF_NS
from .settings import Settings


class Playlists:
    """Reads and writes playlists in whatever format the source uses."""

    def __init__(self, settings=None):
        self.kind, self.folder = (settings or Settings()).playlist_source

    def available(self):
        """{name: [song paths]}."""
        return self._read_m3u() if self.kind == "m3u" else self._read_xspf()

    def write(self, name, paths):
        """Create a playlist. Returns False if one of that name exists already."""
        return self._write_m3u(name, paths) if self.kind == "m3u" else self._write_xspf(name, paths)

    def _read_m3u(self):
        out = {}
        for path in sorted(glob.glob(os.path.join(self.folder, "*.m3u"))):
            with open(path, encoding="utf-8", errors="ignore") as fh:
                songs = [line.strip() for line in fh if line.strip() and not line.startswith("#")]
            out[os.path.splitext(os.path.basename(path))[0]] = songs
        return out

    def _read_xspf(self):
        out = {}
        for path in sorted(glob.glob(os.path.join(self.folder, "*.xspf"))):
            try:
                root = ET.parse(path).getroot()
            except ET.ParseError:
                continue
            namespace = {"x": XSPF_NS} if root.tag.startswith("{") else {"x": ""}
            find = (lambda node, tag: node.find(f"x:{tag}", namespace)) if namespace["x"] \
                else (lambda node, tag: node.find(tag))
            title = find(root, "title")
            name = title.text if title is not None and title.text \
                else unquote(os.path.splitext(os.path.basename(path))[0])
            songs = []
            tracks = root.iter(f"{{{XSPF_NS}}}track") if namespace["x"] else root.iter("track")
            for track in tracks:
                location = find(track, "location")
                if location is not None and location.text and location.text.strip().startswith("file://"):
                    songs.append(unquote(urlparse(location.text.strip()).path))
            out[name] = songs
        return out

    def _write_m3u(self, name, paths):
        os.makedirs(self.folder, exist_ok=True)
        target = os.path.join(self.folder, name.replace("/", "-").replace(":", "-") + ".m3u")
        if os.path.exists(target) or name in self.available():
            return False
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("#EXTM3U\n" + "".join(f"{p}\n" for p in paths))
        return True

    def _write_xspf(self, name, paths):
        os.makedirs(self.folder, exist_ok=True)
        target = os.path.join(self.folder, name.replace("/", "%2F").replace("\0", "%00") + ".xspf")
        if os.path.exists(target) or name in self.available():
            return False
        ET.register_namespace("", XSPF_NS)
        root = ET.Element(f"{{{XSPF_NS}}}playlist", {"version": "1"})
        ET.SubElement(root, f"{{{XSPF_NS}}}title").text = name
        tracks = ET.SubElement(root, f"{{{XSPF_NS}}}trackList")
        for path in paths:
            track = ET.SubElement(tracks, f"{{{XSPF_NS}}}track")
            ET.SubElement(track, f"{{{XSPF_NS}}}location").text = "file://" + quote(path)
        ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)
        return True
