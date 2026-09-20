"""Song identity across systems.

Normalizes titles and artists and reads the Spotify, ISRC and YouTube ids from the tags.
"""
import re
import unicodedata

AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aiff"}

# Title qualifiers that don't make it a different recording.
IGNORED_QUALIFIERS = re.compile(
    r"^(feat\.?|ft\.?|featuring|with|prod\.?)\b|remaster|explicit|clean|single version|album version|original mix|bonus track"
)


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def title_key(title):
    """'Praise You - Maribou State Remix' and 'Praise You (Maribou State Remix)' -> same key."""
    title = (title or "").replace("’", "'")
    quals = re.findall(r"[(\[]([^)\]]*)[)\]]", title)
    base = re.sub(r"[(\[][^)\]]*[)\]]", " ", title)
    base, *dash = re.split(r"\s+-\s+", base)
    quals += dash
    kept = sorted(norm(q) for q in quals if q.strip() and not IGNORED_QUALIFIERS.search(q.strip().lower()))
    return " | ".join([norm(base)] + [q for q in kept if q])


def split_artists(*values):
    out = set()
    for v in values:
        for a in re.split(r",|&|/|;|\bfeat\.?|\bft\.?|\bx\b|\band\b", v or "", flags=re.I):
            if norm(a):
                out.add(norm(a))
    return out


def spotify_id(url):
    m = re.search(r"track[/:]([A-Za-z0-9]{22})", url or "")
    return m.group(1) if m else None


def youtube_id(url):
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", url or "")
    return m.group(1) if m else None


def _tag_values(audio):
    for key in audio.tags.keys():
        val = audio.tags[key]
        vals = val if isinstance(val, list) else [getattr(val, "url", None) or getattr(val, "text", None) or val]
        for v in vals:
            if isinstance(v, list):
                v = v[0] if v else ""
            if isinstance(v, bytes):
                v = v.decode(errors="ignore")
            yield key.lower(), str(v)


def read_ids(audio):
    """{'spotify', 'isrc', 'youtube'} IDs from a mutagen file object (values may be None)."""
    ids = {"spotify": None, "isrc": None, "youtube": None}
    if audio is None or audio.tags is None:
        return ids
    for key, val in _tag_values(audio):
        if "woas" in key:
            ids["spotify"] = ids["spotify"] or spotify_id(val)
        elif key in ("tsrc", "isrc") or key.endswith(":isrc"):
            ids["isrc"] = ids["isrc"] or (val.strip().upper() or None)
        elif "comm" in key or "comment" in key or "purl" in key or key == "\xa9cmt":
            ids["youtube"] = ids["youtube"] or youtube_id(val)
    return ids
