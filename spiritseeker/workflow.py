"""The download pipeline: search -> download -> verify -> tag -> file away.

Runs on a background thread with its own asyncio loop; reports progress to
the GUI through a thread-safe callback.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import threading
from enum import Enum
from typing import Callable, Optional

from . import tagger, verify
from .config import Config
from .soulseek import (Candidate, SoulseekError, SoulseekSession,
                       build_queries, pick_listening_port, rank_candidates)
from .spotify import Playlist, Track

MAX_CANDIDATE_ATTEMPTS = 5


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
    """Downloads a playlist on a dedicated thread.

    ``notify`` is called from the worker thread with:
        ("track", index, Status, detail_str)
        ("track_path", index, local_file_path)
        ("progress", index, done_bytes, total_bytes)
        ("log", message)
        ("finished", ok_count, fail_count)
        ("fatal", message)
    """

    def __init__(self, playlist: Playlist, config: Config,
                 notify: Callable[..., None]):
        self.playlist = playlist
        self.config = config
        self.notify = notify
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._main_task: Optional[asyncio.Task] = None

    def start(self):
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def cancel(self):
        if self._loop and self._main_task:
            self._loop.call_soon_threadsafe(self._main_task.cancel)

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._runner())
        finally:
            self._loop.close()

    async def _runner(self):
        self._main_task = asyncio.current_task()
        try:
            await self._run()
        except asyncio.CancelledError:
            self.notify("log", "Cancelled.")
            self.notify("finished", -1, -1)
        except Exception as exc:  # surface anything unexpected in the UI
            self.notify("fatal", f"{type(exc).__name__}: {exc}")

    async def _run(self):
        output_dir = self.config["output_dir"]
        os.makedirs(output_dir, exist_ok=True)
        tmp_dir = os.path.join(output_dir, ".spiritseeker-tmp")
        os.makedirs(tmp_dir, exist_ok=True)

        username, password, is_custom = self.config.effective_credentials()
        shared = [p for p in self.config["shared_folders"] if os.path.isdir(p)]
        self.notify("log", f"Connecting to Soulseek as {username}"
                    + (" (your account)" if is_custom else "") + "...")
        if shared:
            self.notify("log", f"Sharing {len(shared)} folder(s) with the "
                        "network while downloading.")
        preferred = int(self.config["listening_port"])
        port, got_preferred = pick_listening_port(preferred)
        if not got_preferred:
            self.notify("log",
                        f"Listening port {preferred} is in use (another "
                        f"SpiritSeeker or Soulseek client?) - using {port} "
                        "instead. If you rely on VPN port forwarding, close "
                        "the other client so the forwarded port is free.")
        session = SoulseekSession(
            username=username,
            password=password,
            download_dir=tmp_dir,
            listening_port=port,
            shared_folders=shared,
            log=lambda msg: self.notify("log", msg),
        )
        try:
            await session.start()
        except SoulseekError as exc:
            self.notify("fatal", str(exc))
            return

        track_timeout = max(1, int(self.config["track_timeout_min"])) * 60

        ok = fail = 0
        try:
            for index, track in enumerate(self.playlist.tracks):
                try:
                    done = await asyncio.wait_for(
                        self._process_track(session, index, track,
                                            output_dir, tmp_dir),
                        timeout=track_timeout)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    self.notify("track", index, Status.FAILED,
                                f"gave up after {track_timeout // 60} min "
                                "(not found or peers too slow)")
                    done = False
                except (SoulseekError, verify.VerificationError, OSError) as exc:
                    self.notify("track", index, Status.FAILED, str(exc)[:120])
                    done = False
                if done:
                    ok += 1
                else:
                    fail += 1
        finally:
            await session.stop()
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.notify("finished", ok, fail)

    async def _process_track(self, session: SoulseekSession, index: int,
                             track: Track, output_dir: str, tmp_dir: str) -> bool:
        existing = already_downloaded(track, output_dir)
        if existing:
            self.notify("track_path", index, existing)
            self.notify("track", index, Status.SKIPPED, "already in folder")
            return True

        strict = not self.config["allow_lower_quality"]
        spectral = bool(self.config["spectral_check"])

        # --- search ---
        self.notify("track", index, Status.SEARCHING, "")
        candidates: list[Candidate] = []
        for query in build_queries(track):
            found = await session.search(query)
            candidates.extend(found)
            ranked = rank_candidates(track, candidates, require_320=strict)
            if len(ranked) >= 3:
                break
        else:
            ranked = rank_candidates(track, candidates, require_320=strict)

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
        attempt = 0
        for cand in ranked:
            if attempt >= MAX_CANDIDATE_ATTEMPTS:
                break
            if cand.username in stuck_users:
                continue    # their queue already timed out on us once
            attempt += 1
            self.notify("track", index, Status.DOWNLOADING,
                        f"[{attempt}/{min(len(ranked), MAX_CANDIDATE_ATTEMPTS)}] "
                        f"{cand.describe()}")
            try:
                path = await session.download(
                    cand,
                    progress=lambda done, total: self.notify(
                        "progress", index, done, total))
            except SoulseekError as exc:
                if "queue" in str(exc).lower():
                    stuck_users.add(cand.username)
                self.notify("log", f"{track}: {cand.username} failed ({exc})")
                continue

            self.notify("track", index, Status.VERIFYING, cand.describe())
            try:
                rep = await asyncio.to_thread(verify.verify_file, path, spectral)
            except verify.VerificationError as exc:
                self.notify("log", f"{track}: unreadable file from "
                            f"{cand.username} ({exc})")
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
        try:
            tags = await asyncio.to_thread(tagger.lookup, track)
            cover = await asyncio.to_thread(tagger.fetch_cover_art,
                                            tags.release_mbid)
            await asyncio.to_thread(tagger.write_tags, local_path, tags, cover)
            tag_note = f"tagged via {tags.source}"
        except Exception as exc:
            tag_note = f"tagging failed ({type(exc).__name__})"
            self.notify("log", f"{track}: {tag_note}: {exc}")

        # --- move into place ---
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
