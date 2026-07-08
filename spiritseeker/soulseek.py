"""Soulseek search and download via aioslsk.

No API keys: the Soulseek network auto-creates an account the first time a
new username/password logs in.
"""
from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from aioslsk.client import SoulSeekClient
from aioslsk.exceptions import AioSlskException, AuthenticationError
from aioslsk.protocol.primitives import AttributeKey
from aioslsk.settings import (CredentialsSettings, Settings,
                              SharedDirectorySettingEntry)
from aioslsk.transfer.state import TransferState

from .spotify import Track

LOSSLESS_EXTS = {"flac", "wav", "ape", "aiff"}
ACCEPTED_EXTS = LOSSLESS_EXTS | {"mp3", "m4a", "ogg", "opus"}

# Words in a filename that usually mean "not the studio track you wanted",
# unless the requested title itself contains them.
SUSPECT_WORDS = ("remix", "live", "cover", "instrumental", "karaoke",
                 "acoustic", "acapella", "slowed", "reverb", "nightcore",
                 "sped up", "8d audio", "edit)")


class SoulseekError(Exception):
    pass


@dataclass
class Candidate:
    username: str
    remote_path: str
    filesize: int
    extension: str
    bitrate: int = 0            # from search-result attributes; 0 = unknown
    duration: int = 0           # seconds; 0 = unknown
    vbr: bool = False
    has_free_slots: bool = False
    avg_speed: int = 0
    queue_size: int = 0
    score: float = field(default=0.0, compare=False)

    @property
    def is_lossless(self) -> bool:
        return self.extension in LOSSLESS_EXTS

    @property
    def basename(self) -> str:
        return self.remote_path.replace("\\", "/").rsplit("/", 1)[-1]

    def describe(self) -> str:
        qual = "FLAC" if self.extension == "flac" else self.extension.upper()
        if self.bitrate and not self.is_lossless:
            qual += f" {self.bitrate}kbps"
        mb = self.filesize / (1024 * 1024)
        return f"{qual}, {mb:.1f}MB from {self.username}"


def _tokenize(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


def _strip_extras(title: str) -> str:
    """Remove featuring credits and bracketed suffixes for matching."""
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title)
    t = re.sub(r"\s*(feat\.?|ft\.?|with)\s.*$", "", t, flags=re.IGNORECASE)
    return t.strip() or title


def build_queries(track: Track) -> list[str]:
    """Search query variants, most specific first."""
    primary_artist = re.split(r"[,;]| & | x | X ", track.artist)[0].strip()
    clean_title = _strip_extras(track.title)
    queries = [f"{primary_artist} {clean_title}"]
    if clean_title != track.title:
        queries.append(f"{primary_artist} {track.title}")
    # Last resort: title only (helps when artist folder names diverge)
    if len(_tokenize(clean_title)) >= 2:
        queries.append(clean_title)
    # Dedupe, preserve order
    seen, out = set(), []
    for q in queries:
        ql = q.lower()
        if ql not in seen:
            seen.add(ql)
            out.append(q)
    return out


def rank_candidates(track: Track, candidates: list[Candidate],
                    require_320: bool) -> list[Candidate]:
    """Filter to plausible matches and sort best-first."""
    title_tokens = _tokenize(_strip_extras(track.title))
    artist_tokens = _tokenize(re.split(r"[,;]", track.artist)[0])
    all_artist_tokens = _tokenize(track.artist)
    wanted_tokens = _tokenize(track.title) | all_artist_tokens

    ranked = []
    for c in candidates:
        if c.extension not in ACCEPTED_EXTS:
            continue
        path_tokens = _tokenize(c.remote_path)

        # Every significant word of the title must appear somewhere in the path
        if title_tokens and not title_tokens.issubset(path_tokens):
            continue

        # ...and so must at least one credited artist (folder or filename).
        # Without this, a title-only hit can be a different song entirely.
        if all_artist_tokens and not (all_artist_tokens & path_tokens):
            continue

        base = c.basename.lower()
        if any(w in base and w not in track.title.lower() for w in SUSPECT_WORDS):
            continue

        # Strict quality gate: lossless, stated >=320, or unstated bitrate
        # (many FLAC shares omit attributes; unknowns get verified after
        # download anyway - but for MP3s an unknown bitrate is usually a
        # red flag, so only allow unknowns for lossless formats)
        if require_320 and not c.is_lossless and c.bitrate and c.bitrate < 320:
            continue
        if require_320 and not c.is_lossless and not c.bitrate:
            continue

        # Duration sanity check when both sides know it
        if c.duration and track.duration_sec:
            if abs(c.duration - track.duration_sec) > 15:
                continue

        score = 0.0
        score += 400 if c.extension == "flac" else 0
        score += 250 if c.extension in LOSSLESS_EXTS - {"flac"} else 0
        if not c.is_lossless:
            score += min(c.bitrate, 320)
        score += 150 if artist_tokens & path_tokens == artist_tokens else 0
        score += 100 if c.has_free_slots else 0
        score += min(c.avg_speed / 1024, 50)      # reward fast peers a little
        score -= min(c.queue_size * 5, 100)
        extra_tokens = len(_tokenize(base) - wanted_tokens)
        score -= extra_tokens * 3                  # penalize noisy filenames
        c.score = score
        ranked.append(c)

    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked


class SoulseekSession:
    """Owns the aioslsk client lifecycle inside an asyncio loop."""

    def __init__(self, username: str, password: str, download_dir: str,
                 listening_port: int = 61000,
                 shared_folders: Optional[list[str]] = None,
                 log: Optional[Callable[[str], None]] = None):
        os.makedirs(download_dir, exist_ok=True)
        self._log = log or (lambda msg: None)
        settings = Settings(
            credentials=CredentialsSettings(username=username, password=password),
        )
        settings.shares.download = download_dir
        shares = [SharedDirectorySettingEntry(path=p)
                  for p in (shared_folders or []) if os.path.isdir(p)]
        settings.shares.directories = shares
        settings.shares.scan_on_start = bool(shares)
        settings.network.listening.port = listening_port
        settings.network.listening.obfuscated_port = listening_port + 1
        settings.network.server.reconnect.auto = True
        settings.searches.receive.max_results = 150
        self.client = SoulSeekClient(settings)

    async def start(self):
        try:
            await self.client.start()
            await self.client.login()
        except AuthenticationError as exc:
            raise SoulseekError(
                f"Soulseek login failed: {exc}. The username may be taken - "
                "use 'New identity' in settings.") from exc
        except (AioSlskException, OSError) as exc:
            raise SoulseekError(f"Could not connect to Soulseek: {exc}") from exc
        self._log("Connected to Soulseek")

    async def stop(self):
        try:
            await self.client.stop()
        except Exception:
            pass

    async def search(self, query: str, wait_sec: float = 9.0) -> list[Candidate]:
        request = await self.client.searches.search(query)
        await asyncio.sleep(wait_sec)

        candidates: list[Candidate] = []
        for result in request.results:
            for item in result.shared_items:
                attrs = {a.key: a.value for a in item.attributes}
                ext = (item.extension or
                       item.filename.rsplit(".", 1)[-1]).lower().strip(". ")
                candidates.append(Candidate(
                    username=result.username,
                    remote_path=item.filename,
                    filesize=item.filesize,
                    extension=ext,
                    bitrate=attrs.get(AttributeKey.BITRATE.value, 0),
                    duration=attrs.get(AttributeKey.DURATION.value, 0),
                    vbr=bool(attrs.get(AttributeKey.VBR.value, 0)),
                    has_free_slots=result.has_free_slots,
                    avg_speed=result.avg_speed,
                    queue_size=result.queue_size,
                ))
        return candidates

    async def download(self, candidate: Candidate,
                       progress: Optional[Callable[[int, int], None]] = None,
                       queue_timeout: float = 180.0,
                       stall_timeout: float = 60.0,
                       total_timeout: float = 900.0) -> str:
        """Download a search result. Returns the local file path.

        Raises SoulseekError on failure/timeout; the partial file is removed.
        """
        transfer = await self.client.transfers.download(
            candidate.username, candidate.remote_path)

        elapsed = 0.0
        stalled = 0.0
        queued = 0.0
        last_bytes = -1
        poll = 0.5
        try:
            while True:
                await asyncio.sleep(poll)
                elapsed += poll
                state = transfer.state.VALUE

                if state == TransferState.COMPLETE:
                    if not transfer.local_path or not os.path.exists(transfer.local_path):
                        raise SoulseekError("Transfer completed but file is missing")
                    return transfer.local_path
                if state == TransferState.FAILED:
                    raise SoulseekError(
                        f"Peer failed the transfer ({transfer.fail_reason or 'unknown reason'})")
                if state == TransferState.ABORTED:
                    raise SoulseekError("Transfer was aborted")

                if state == TransferState.DOWNLOADING:
                    queued = 0.0
                    if transfer.bytes_transfered == last_bytes:
                        stalled += poll
                    else:
                        stalled = 0.0
                        last_bytes = transfer.bytes_transfered
                        if progress:
                            progress(transfer.bytes_transfered,
                                     transfer.filesize or candidate.filesize)
                    if stalled >= stall_timeout:
                        raise SoulseekError("Transfer stalled")
                else:
                    queued += poll
                    if queued >= queue_timeout:
                        raise SoulseekError("Stuck in peer's queue")

                if elapsed >= total_timeout:
                    raise SoulseekError("Transfer timed out")
        except (SoulseekError, asyncio.CancelledError):
            try:
                await self.client.transfers.abort(transfer)
            except Exception:
                pass
            raise
