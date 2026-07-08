"""Fetch public Spotify playlists without any API keys.

Strategy: Spotify's embed player page (open.spotify.com/embed/playlist/<id>)
ships the playlist as JSON inside a __NEXT_DATA__ script tag. No auth needed
for public playlists. The embed exposes at most ~100 tracks, so for longer
playlists we support importing a CSV export (Exportify / chosic.com format).
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field

import requests

EMBED_URL = "https://open.spotify.com/embed/playlist/{playlist_id}"
EMBED_TRACK_LIMIT = 100

_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


class SpotifyError(Exception):
    pass


@dataclass
class Track:
    title: str
    artist: str
    duration_ms: int = 0
    album: str = ""

    @property
    def duration_sec(self) -> float:
        return self.duration_ms / 1000.0

    def __str__(self):
        return f"{self.artist} - {self.title}"


@dataclass
class Playlist:
    name: str
    tracks: list[Track] = field(default_factory=list)
    maybe_truncated: bool = False


def parse_playlist_id(url_or_id: str) -> str:
    """Accepts a full playlist URL, a spotify: URI, or a bare playlist id."""
    text = url_or_id.strip()
    m = re.search(r"playlist[/:]([A-Za-z0-9]{16,})", text)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{16,}", text):
        return text
    raise SpotifyError(
        "That doesn't look like a Spotify playlist link.\n"
        "Expected something like https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
    )


def fetch_playlist(url_or_id: str, timeout: int = 20) -> Playlist:
    playlist_id = parse_playlist_id(url_or_id)
    url = EMBED_URL.format(playlist_id=playlist_id)
    try:
        resp = requests.get(url, headers={"User-Agent": _BROWSER_UA}, timeout=timeout)
    except requests.RequestException as exc:
        raise SpotifyError(f"Could not reach Spotify: {exc}") from exc
    if resp.status_code != 200:
        raise SpotifyError(
            f"Spotify returned HTTP {resp.status_code} - is the playlist public?")

    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        resp.text, re.DOTALL)
    if not m:
        raise SpotifyError(
            "Could not find playlist data in the Spotify page. Spotify may have "
            "changed their site - try the CSV import instead.")

    try:
        data = json.loads(m.group(1))
        entity = data["props"]["pageProps"]["state"]["data"]["entity"]
        name = entity.get("name") or "Spotify Playlist"
        track_list = entity.get("trackList") or []
    except (ValueError, KeyError, TypeError) as exc:
        raise SpotifyError(
            f"Spotify page layout changed ({exc}). Try the CSV import instead.") from exc

    if not track_list:
        raise SpotifyError("The playlist appears to be empty or private.")

    tracks = []
    for item in track_list:
        title = (item.get("title") or "").strip()
        # subtitle is a comma-separated artist list (with non-breaking spaces)
        artist = (item.get("subtitle") or "").replace(chr(0xa0), " ").strip()
        if not title:
            continue
        tracks.append(Track(
            title=title,
            artist=artist,
            duration_ms=int(item.get("duration") or 0),
        ))

    return Playlist(
        name=name,
        tracks=tracks,
        maybe_truncated=len(tracks) >= EMBED_TRACK_LIMIT,
    )


def import_csv(path: str) -> Playlist:
    """Import a playlist CSV exported by Exportify or chosic.com.

    Both formats include 'Track Name' / 'Artist Name(s)' style headers; we
    match column names loosely so minor variations still work.
    """
    def find_col(headers: list[str], *needles: str) -> int | None:
        for i, h in enumerate(headers):
            hl = h.lower()
            if any(n in hl for n in needles):
                return i
        return None

    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if any(cell.strip() for cell in r)]

    if len(rows) < 2:
        raise SpotifyError("CSV file appears to be empty.")

    headers = rows[0]
    title_i = find_col(headers, "track name", "song", "title")
    artist_i = find_col(headers, "artist")
    dur_i = find_col(headers, "duration")
    album_i = find_col(headers, "album name", "album")
    if title_i is None or artist_i is None:
        raise SpotifyError(
            "Could not find track/artist columns in the CSV. Use an export from "
            "Exportify or chosic.com's Spotify Playlist Exporter.")

    tracks = []
    for row in rows[1:]:
        if len(row) <= max(title_i, artist_i):
            continue
        title = row[title_i].strip()
        artist = row[artist_i].strip()
        if not title:
            continue
        duration_ms = 0
        if dur_i is not None and len(row) > dur_i:
            raw = row[dur_i].strip()
            if raw.isdigit():
                duration_ms = int(raw)
            elif re.fullmatch(r"\d+:\d{2}", raw):
                mins, secs = raw.split(":")
                duration_ms = (int(mins) * 60 + int(secs)) * 1000
        album = row[album_i].strip() if album_i is not None and len(row) > album_i else ""
        tracks.append(Track(title=title, artist=artist,
                            duration_ms=duration_ms, album=album))

    if not tracks:
        raise SpotifyError("No tracks found in the CSV.")

    import os
    name = os.path.splitext(os.path.basename(path))[0]
    return Playlist(name=name, tracks=tracks)
