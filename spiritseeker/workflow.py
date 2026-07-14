"""The download pipeline: search -> download -> verify -> tag -> file away.

Runs on a background thread with its own asyncio loop; reports progress to
the GUI through a thread-safe callback.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from concurrent.futures import Future
from enum import Enum
from typing import TYPE_CHECKING, Callable, Optional

from . import tagger, verify
from .config import Config
from .soulseek import (Candidate, SoulseekError, SoulseekSession,
                       build_queries, rank_candidates)
from .spotify import Playlist, Track

if TYPE_CHECKING:
    from .connection import ConnectionManager

# Consecutive tracks with zero raw search results before we assume the
# server is rate-limiting us and cool down
EMPTY_STREAK_LIMIT = 3
RATE_LIMIT_COOLDOWN_SEC = 240
MAX_COOLDOWNS = 5


class SearchRateLimited(Exception):
    """Searches are coming back empty in a way that smells like server-side
    rate limiting rather than genuinely unfindable tracks."""


class TrackTimeout(Exception):
    """The track's time budget ran out while searching or queue-waiting.
    Never raised while a download is actually receiving data."""


class Status(str, Enum):
    PENDING = "Pending"
    SEARCHING = "Searching"
    DOWNLOADING = "Downloading"
    VERIFYING = "Verifying"
    TAGGING = "Tagging"
    DONE = "Done"
    SKIPPED = "Skipped"
    FAILED = "Failed"


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:180] or "track"


def final_path_for(track: Track, ext: str, output_dir: str) -> str:
    base = sanitize_filename(f"{track.artist} - {track.title}")
    path = os.path.join(output_dir, f"{base}.{ext}")
    n = 2
    while os.path.exists(path):
        path = os.path.join(output_dir, f"{base} ({n}).{ext}")
        n += 1
    return path


def already_downloaded(track: Track, output_dir: str) -> Optional[str]:
    base = sanitize_filename(f"{track.artist} - {track.title}").lower()
    try:
        for fname in os.listdir(output_dir):
            stem = fname.rsplit(".", 1)[0].lower()
            if stem == base:
                return os.path.join(output_dir, fname)
    except OSError:
        pass
    return None


class Worker:
    """Downloads a playlist on the shared connection thread.

    ``notify`` is called from the connection thread with:
        ("track", index, Status, detail_str)
        ("track_path", index, local_file_path)
        ("track_file", index, source_filename)
        ("progress", index, done_bytes, total_bytes, rate_bytes_per_sec)
        ("log", message)
        ("finished", ok_count, fail_count)
        ("fatal", message)
    """

    def __init__(self, playlist: Playlist, config: Config,
                 notify: Callable[..., None],
                 manager: "ConnectionManager",
                 should_skip: Optional[Callable[[int], bool]] = None):
        self.playlist = playlist
        self.config = config
        self.notify = notify
        self.manager = manager
        self.should_skip = should_skip
        self._future: Optional[Future] = None
        self._empty_streak = 0
        self._cooldowns_used = 0
        self._track_tasks: dict[int, asyncio.Task] = {}
        self._user_skipped: set[int] = set()
        self._extra_tasks: list[asyncio.Task] = []
        self._attempt = None            # set while a run is active
        self._accepting = False

    def start(self):
        self._future = self.manager.submit(self._runner())

    def cancel(self):
        if self._future:
            self._future.cancel()

    def add_track(self, index: int, track: Track):
        """Append one more track to a RUNNING worker (thread-safe). The
        track shares the same concurrency slots and counts in the totals."""
        def do_add():
            if not self._accepting or self._attempt is None:
                return
            task = asyncio.ensure_future(self._attempt(index, track))
            self._track_tasks[index] = task
            self._extra_tasks.append(task)
        if self.manager.loop:
            self.manager.loop.call_soon_threadsafe(do_add)

    def skip_track(self, index: int):
        """Cancel one in-flight track (thread-safe); it's marked skipped
        rather than failed, and the rest of the run continues."""
        def do_cancel():
            self._user_skipped.add(index)
            task = self._track_tasks.get(index)
            if task and not task.done():
                task.cancel()
        if self.manager.loop:
            self.manager.loop.call_soon_threadsafe(do_cancel)

    async def _runner(self):
        try:
            await self._run()
        except asyncio.CancelledError:
            self.notify("log", "Cancelled.")
            self.notify("finished", -1, -1)
            raise
        except Exception as exc:  # surface anything unexpected in the UI
            self.notify("fatal", f"{type(exc).__name__}: {exc}")

    async def _run(self):
        output_dir = self.config["output_dir"]
        os.makedirs(output_dir, exist_ok=True)
        tmp_dir = os.path.join(output_dir, ".spiritseeker-tmp")
        os.makedirs(tmp_dir, exist_ok=True)

        try:
            session = await self.manager.ensure_session()
        except SoulseekError as exc:
            self.notify("fatal", str(exc))
            return
        # Incoming files for this run land in the tmp dir next to the output
        session.client.settings.shares.download = tmp_dir

        track_timeout = max(1, int(self.config["track_timeout_min"])) * 60
        concurrency = max(1, min(4, int(self.config["concurrent_downloads"])))
        sem = asyncio.Semaphore(concurrency)

        async def attempt(index: int, track: Track) -> Optional[bool]:
            """True/False = ok/failed; None = skipped by the user."""
            if self.should_skip and self.should_skip(index):
                self.notify("track", index, Status.SKIPPED, "skipped by you")
                return None
            try:
                async with sem:
                    if self.should_skip and self.should_skip(index):
                        self.notify("track", index, Status.SKIPPED,
                                    "skipped by you")
                        return None
                    return await self._run_one(session, index, track,
                                               output_dir, tmp_dir,
                                               track_timeout)
            except asyncio.CancelledError:
                # A single-track skip; a full Stop cancels the whole runner
                # and the index won't be in _user_skipped
                if index in self._user_skipped:
                    self.notify("track", index, Status.SKIPPED,
                                "skipped by you")
                    return None
                raise

        def spawn_all(indices) -> dict[int, asyncio.Task]:
            self._track_tasks = {
                i: asyncio.create_task(attempt(i, self.playlist.tracks[i]))
                for i in indices}
            return self._track_tasks

        self._attempt = attempt
        self._accepting = True
        try:
            results = await asyncio.gather(
                *spawn_all(range(len(self.playlist.tracks))).values())
            ok = sum(1 for r in results if r is True)
            failed_idx = [i for i, r in enumerate(results) if r is False]

            if failed_idx and self.config["auto_retry_failed"]:
                self.notify("log", f"Retrying {len(failed_idx)} failed "
                            "track(s) once more...")
                retried = await asyncio.gather(
                    *spawn_all(failed_idx).values())
                ok += sum(1 for r in retried if r is True)
                failed_idx = [i for i, r in zip(failed_idx, retried)
                              if r is False]
            fail = len(failed_idx)

            # Tracks appended mid-run via add_track (manual searches)
            while self._extra_tasks:
                batch, self._extra_tasks = self._extra_tasks, []
                extras = await asyncio.gather(*batch)
                ok += sum(1 for r in extras if r is True)
                fail += sum(1 for r in extras if r is False)
        finally:
            self._accepting = False
            self._attempt = None
            # The session stays connected for chat and the next run
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.notify("finished", ok, fail)

    async def _run_one(self, session: SoulseekSession, index: int,
                       track: Track, output_dir: str, tmp_dir: str,
                       track_timeout: int, allow_cooldown: bool = True) -> bool:
        try:
            return await self._process_track(session, index, track,
                                             output_dir, tmp_dir,
                                             timeout_sec=track_timeout)
        except asyncio.CancelledError:
            raise
        except SearchRateLimited:
            if allow_cooldown and self._cooldowns_used < MAX_COOLDOWNS:
                self._cooldowns_used += 1
                minutes = RATE_LIMIT_COOLDOWN_SEC // 60
                self.notify("log",
                            f"{EMPTY_STREAK_LIMIT} searches in a row came "
                            "back empty - the Soulseek server is likely "
                            "rate-limiting us. Cooling down for "
                            f"{minutes} minutes, then continuing...")
                self.notify("track", index, Status.SEARCHING,
                            f"cooling down {minutes} min (server rate limit)")
                await asyncio.sleep(RATE_LIMIT_COOLDOWN_SEC)
                return await self._run_one(session, index, track, output_dir,
                                           tmp_dir, track_timeout,
                                           allow_cooldown=False)
            self.notify("track", index, Status.FAILED, "no sources found")
            return False
        except TrackTimeout:
            self.notify("track", index, Status.FAILED,
                        f"gave up after {track_timeout // 60} min "
                        "(not found or peers too slow)")
            return False
        except (SoulseekError, verify.VerificationError, OSError) as exc:
            self.notify("track", index, Status.FAILED, str(exc)[:120])
            return False

    async def _process_track(self, session: SoulseekSession, index: int,
                             track: Track, output_dir: str, tmp_dir: str,
                             timeout_sec: int) -> bool:
        existing = already_downloaded(track, output_dir)
        overwrite = bool(self.config["overwrite_duplicates"])
        if existing and not overwrite:
            self.notify("track_path", index, existing)
            self.notify("track_file", index, os.path.basename(existing))
            self.notify("track", index, Status.SKIPPED, "already in folder")
            return True
        if existing:
            self.notify("log", f"{track}: will replace existing "
                        f"{os.path.basename(existing)} (overwrite is on)")

        strict = not self.config["allow_lower_quality"]
        spectral = bool(self.config["spectral_check"])

        start = time.monotonic()
        paced_at_start = session.total_paced_sec

        def out_of_time() -> bool:
            # Global search pacing isn't this track's fault - exclude it.
            # Checked only between steps, so an in-flight download that is
            # still receiving data always runs to completion.
            paced = session.total_paced_sec - paced_at_start
            return time.monotonic() - start - paced > timeout_sec

        # --- search ---
        self.notify("track", index, Status.SEARCHING, "")
        candidates: list[Candidate] = []
        for query in build_queries(track):
            if out_of_time():
                raise TrackTimeout()
            found = await session.search(query)
            candidates.extend(found)
            ranked = rank_candidates(track, candidates, require_320=strict)
            if len(ranked) >= 3:
                break
        else:
            ranked = rank_candidates(track, candidates, require_320=strict)

        # Zero RAW results (not merely filtered out) for several tracks in a
        # row means the server has stopped relaying our searches
        if not candidates:
            self._empty_streak += 1
            if self._empty_streak >= EMPTY_STREAK_LIMIT:
                self._empty_streak = 0
                raise SearchRateLimited()
        else:
            self._empty_streak = 0

        if not ranked and strict:
            self.notify("track", index, Status.FAILED,
                        "no 320kbps+/lossless sources found")
            return False
        if not ranked:
            self.notify("track", index, Status.FAILED, "no sources found")
            return False

        # --- download + verify, walking down the candidate list ---
        local_path = None
        report = None
        rejected_note = ""
        stuck_users: set[str] = set()
        max_attempts = max(1, int(self.config["max_attempts"]))
        stall_timeout = max(10, int(self.config["stall_timeout_sec"]))
        attempt = 0
        for cand in ranked:
            if attempt >= max_attempts:
                break
            if attempt and out_of_time():
                raise TrackTimeout()
            if cand.username in stuck_users:
                continue    # their queue already timed out on us once
            attempt += 1
            self.notify("track_file", index, cand.basename)
            tag = f"[{attempt}/{min(len(ranked), max_attempts)}]"
            empty_bar = "░" * 10
            self.notify("track", index, Status.DOWNLOADING,
                        f"{empty_bar} connecting... {tag} {cand.describe()}")

            def waiting(state_name: str, secs: float,
                        _tag=tag, _cand=cand):
                label = {"QUEUED": "in peer's queue",
                         "VIRGIN": "requesting",
                         "INITIALIZING": "connecting"}.get(
                    state_name, state_name.lower())
                self.notify("track", index, Status.DOWNLOADING,
                            f"{empty_bar} {label} {int(secs)}s... "
                            f"{_tag} {_cand.describe()}")

            try:
                path = await session.download(
                    cand,
                    progress=lambda done, total, rate: self.notify(
                        "progress", index, done, total, rate),
                    stall_timeout=stall_timeout,
                    on_wait=waiting)
            except SoulseekError as exc:
                if "queue" in str(exc).lower():
                    stuck_users.add(cand.username)
                self.notify("log", f"{track}: {cand.username} failed ({exc})")
                continue

            self.notify("track", index, Status.VERIFYING, cand.describe())
            try:
                rep = await asyncio.wait_for(
                    asyncio.to_thread(verify.verify_file, path, spectral),
                    timeout=300)
            except (verify.VerificationError, TimeoutError) as exc:
                self.notify("log", f"{track}: unreadable file from "
                            f"{cand.username} ({exc or 'verification timed out'})")
                self._discard(path)
                continue

            if strict and not rep.passes_320:
                self.notify("log", f"{track}: rejected {cand.username}'s copy - "
                            f"{rep.notes or rep.summary()}")
                rejected_note = rep.notes or rep.summary()
                self._discard(path)
                continue

            local_path, report = path, rep
            break

        if local_path is None:
            detail = "all candidates failed verification" if rejected_note \
                else "no candidate could be downloaded"
            self.notify("track", index, Status.FAILED, detail)
            return False

        # --- tag ---
        self.notify("track", index, Status.TAGGING, "MusicBrainz lookup...")

        async def _tag():
            tags = await asyncio.to_thread(tagger.lookup, track)
            cover = await asyncio.to_thread(tagger.fetch_cover_art,
                                            tags.release_mbid)
            await asyncio.to_thread(tagger.write_tags, local_path, tags, cover)
            return tags

        try:
            tags = await asyncio.wait_for(_tag(), timeout=120)
            tag_note = f"tagged via {tags.source}"
        except Exception as exc:
            tag_note = f"tagging failed ({type(exc).__name__})"
            self.notify("log", f"{track}: {tag_note}: {exc}")

        # --- move into place ---
        # Only now that the new copy is verified is the old one replaced
        if existing and overwrite:
            try:
                os.remove(existing)
            except OSError:
                pass
        ext = local_path.rsplit(".", 1)[-1].lower()
        dest = final_path_for(track, ext, output_dir)
        shutil.move(local_path, dest)

        self.notify("track_path", index, dest)
        self.notify("track", index, Status.DONE,
                    f"{report.summary()} | {tag_note}")
        return True

    @staticmethod
    def _discard(path: str):
        try:
            os.remove(path)
        except OSError:
            pass
