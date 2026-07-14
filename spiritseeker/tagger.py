"""Tag downloaded files with MusicBrainz metadata + Cover Art Archive artwork.

MusicBrainz's API is free and keyless; it only asks for a descriptive
User-Agent and max 1 request/second. If MusicBrainz has no match we fall
back to the Spotify playlist metadata so files are never left untagged.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TRCK
from mutagen.mp4 import MP4, MP4Cover
from mutagen.oggopus import OggOpus
from mutagen.oggvorbis import OggVorbis

from . import USER_AGENT
from .spotify import Track

MB_URL = "https://musicbrainz.org/ws/2/recording"
CAA_URL = "https://coverartarchive.org/release/{release_id}/front-500"

_rate_lock = threading.Lock()
_last_request = 0.0


def _rate_limited_get(url: str, **kwargs) -> requests.Response:
    """MusicBrainz allows 1 req/sec; be a good citizen."""
    global _last_request
    with _rate_lock:
        wait = 1.1 - (time.monotonic() - _last_request)
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
    kwargs.setdefault("timeout", 15)
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)
    return requests.get(url, headers=headers, **kwargs)


@dataclass
class TagData:
    title: str
    artist: str
    album: str = ""
    year: str = ""
    track_number: str = ""
    release_mbid: str = ""
    source: str = "spotify"     # "musicbrainz" or "spotify"


def _lucene_escape(text: str) -> str:
    return re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r"\\\1", text)


def guess_artist_title(remote_path: str) -> tuple[str, str]:
    """Best-effort (artist, title) from a Soulseek path/filename.

    Handles the two common shapes:
      ...\\Artist\\Album\\03 - Title.flac   -> ("Artist", "Title")
      Artist - Title.mp3                    -> ("Artist", "Title")
    Returns ("", stem) when no artist can be inferred.
    """
    parts = re.split(r"[\\/]", remote_path)
    fname = parts[-1] if parts else remote_path
    stem = fname.rsplit(".", 1)[0]
    # Drop a leading track number ("03 ", "03. ", "03 - ")
    stem = re.sub(r"^\s*\d{1,3}\s*[-.\)]?\s*", "", stem).strip()

    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip(), title.strip()
    # Fall back to the parent folders: .../Artist/Album/Title
    if len(parts) >= 3:
        artist = parts[-3].strip()
        # Skip obvious non-artist roots like drive shares "@@..." or "Music"
        if artist and not artist.startswith("@") and artist.lower() != "music":
            return artist, stem
    return "", stem


def lookup(track: Track) -> TagData:
    """Find the best MusicBrainz match for a track; fall back to Spotify data."""
    fallback = TagData(title=track.title, artist=track.artist,
                       album=track.album, source="spotify")

    primary_artist = re.split(r"[,;]", track.artist)[0].strip()
    query = (f'recording:"{_lucene_escape(track.title)}" '
             f'AND artist:"{_lucene_escape(primary_artist)}"')
    try:
        resp = _rate_limited_get(
            MB_URL, params={"query": query, "fmt": "json", "limit": "10"})
        resp.raise_for_status()
        recordings = resp.json().get("recordings", [])
    except (requests.RequestException, ValueError):
        return fallback

    # Prefer official album releases over singles/compilations/bootlegs
    def release_rank(rel):
        group = rel.get("release-group", {})
        primary = (group.get("primary-type") or "").lower()
        secondary = group.get("secondary-types") or []
        rank = 0
        if primary == "album":
            rank -= 2
        elif primary == "single":
            rank -= 1
        if secondary:                                  # live/compilation/remix...
            rank += 2
        if (rel.get("status") or "").lower() != "official":
            rank += 3
        title = (rel.get("title") or "").lower()
        if any(w in title for w in ("remix", "megamix", "karaoke", "tribute")):
            rank += 3
        return (rank, rel.get("date") or "9999")

    scored = []
    for rec in recordings:
        if int(rec.get("score", 0)) < 85:
            continue
        # Duration mismatch beyond 20s means a different version of the song
        if track.duration_ms and rec.get("length"):
            dur_pen = abs(int(rec["length"]) - track.duration_ms)
            if dur_pen > 20_000:
                continue
        else:
            dur_pen = 15_000        # unknown duration: worse than a close match
        # Rank every (recording, release) pair: duration identifies the right
        # version of the song (10s buckets), release quality picks the best
        # album to credit within a bucket.
        for rel in rec.get("releases") or [None]:
            rel_rank = release_rank(rel) if rel else (99, "9999")
            scored.append(((dur_pen // 10_000, rel_rank), rec, rel))
    if not scored:
        return fallback
    scored.sort(key=lambda item: item[0])
    best, rel = scored[0][1], scored[0][2]

    artist = ", ".join(
        credit.get("name", "") for credit in best.get("artist-credit", [])
        if isinstance(credit, dict)) or track.artist
    title = best.get("title") or track.title

    album, year, track_number, release_mbid = track.album, "", "", ""
    if rel:
        album = rel.get("title") or album
        year = (rel.get("date") or "")[:4]
        release_mbid = rel.get("id") or ""
        for m in rel.get("media") or []:
            tracks_info = m.get("track") or []
            if tracks_info:
                track_number = tracks_info[0].get("number") or ""
                break

    return TagData(title=title, artist=artist, album=album, year=year,
                   track_number=track_number, release_mbid=release_mbid,
                   source="musicbrainz")


def fetch_cover_art(release_mbid: str) -> Optional[bytes]:
    if not release_mbid:
        return None
    try:
        resp = _rate_limited_get(CAA_URL.format(release_id=release_mbid),
                                 allow_redirects=True, timeout=20)
        if resp.status_code == 200 and resp.content[:4] in (b"\xff\xd8\xff\xe0",
                                                            b"\xff\xd8\xff\xe1",
                                                            b"\x89PNG"):
            return resp.content
        if resp.status_code == 200 and len(resp.content) > 1000:
            return resp.content
    except requests.RequestException:
        pass
    return None


def write_tags(path: str, tags: TagData, cover: Optional[bytes] = None):
    ext = path.rsplit(".", 1)[-1].lower()
    mime = "image/png" if cover and cover.startswith(b"\x89PNG") else "image/jpeg"

    if ext == "mp3":
        try:
            id3 = ID3(path)
        except Exception:
            id3 = ID3()
        id3.setall("TIT2", [TIT2(encoding=3, text=tags.title)])
        id3.setall("TPE1", [TPE1(encoding=3, text=tags.artist)])
        if tags.album:
            id3.setall("TALB", [TALB(encoding=3, text=tags.album)])
        if tags.year:
            id3.setall("TDRC", [TDRC(encoding=3, text=tags.year)])
        if tags.track_number:
            id3.setall("TRCK", [TRCK(encoding=3, text=tags.track_number)])
        if cover:
            id3.setall("APIC", [APIC(encoding=3, mime=mime, type=3,
                                     desc="Cover", data=cover)])
        id3.save(path)
        return

    if ext == "flac":
        audio = FLAC(path)
        audio["title"] = tags.title
        audio["artist"] = tags.artist
        if tags.album:
            audio["album"] = tags.album
        if tags.year:
            audio["date"] = tags.year
        if tags.track_number:
            audio["tracknumber"] = tags.track_number
        if cover:
            pic = Picture()
            pic.type = 3
            pic.mime = mime
            pic.desc = "Cover"
            pic.data = cover
            audio.clear_pictures()
            audio.add_picture(pic)
        audio.save()
        return

    if ext in ("m4a", "mp4", "aac"):
        audio = MP4(path)
        audio["\xa9nam"] = [tags.title]
        audio["\xa9ART"] = [tags.artist]
        if tags.album:
            audio["\xa9alb"] = [tags.album]
        if tags.year:
            audio["\xa9day"] = [tags.year]
        if cover:
            fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
            audio["covr"] = [MP4Cover(cover, imageformat=fmt)]
        audio.save()
        return

    if ext in ("ogg", "opus"):
        audio = OggOpus(path) if ext == "opus" else OggVorbis(path)
        audio["title"] = tags.title
        audio["artist"] = tags.artist
        if tags.album:
            audio["album"] = tags.album
        if tags.year:
            audio["date"] = tags.year
        audio.save()
        return
    # Other formats (wav/ape/aiff): leave untagged rather than risk corruption
