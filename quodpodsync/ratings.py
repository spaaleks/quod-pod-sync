"""Ratings and play counts in the song file.

Stored in the POPM tag under the address Quod Libet reads, so ratings from an iPod show up there too.
"""
import mutagen

from .paths import POPM_BY_STARS, POPM_EMAIL


def stars(ipod_rating):
    """iPod rating (0-100) to stars."""
    return max(0, min(5, round(ipod_rating / 20)))


def stars_of_popm(rating255):
    """POPM rating to stars. The steps are not linear: 2 stars is 64, not 102."""
    return min(POPM_BY_STARS, key=lambda s: abs(POPM_BY_STARS[s] - rating255))


def of_file(audio):
    """(rating 0-255, play count) of our own POPM entry, or (0, 0). Entries of other programs are ignored."""
    try:
        frames = audio.tags.getall("POPM") if audio is not None and audio.tags is not None else []
    except Exception:
        return 0, 0
    frame = next((f for f in frames if f.email == POPM_EMAIL), None)
    return (frame.rating, getattr(frame, "count", 0)) if frame else (0, 0)


def write(path, rating255, count):
    """Set our POPM entry, leaving entries of other programs alone."""
    from mutagen.id3 import ID3, POPM

    tags = ID3(path)
    for frame in tags.getall("POPM"):
        if frame.email == POPM_EMAIL:
            tags.delall(f"POPM:{POPM_EMAIL}")
            break
    tags.add(POPM(email=POPM_EMAIL, rating=rating255, count=count))
    tags.save()


def read(path):
    """(rating 0-255, play count) of a file on disk."""
    try:
        return of_file(mutagen.File(path))
    except Exception:
        return 0, 0
