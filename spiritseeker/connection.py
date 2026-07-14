"""Persistent Soulseek connection shared by downloads and chat.

The Soulseek server kicks the previous session when the same account logs in
twice, so the app keeps exactly one connection alive on a background asyncio
thread. Download workers and the chat window both submit coroutines here.
"""
from __future__ import annotations

import asyncio
import threading
from concurrent.futures import Future
from typing import Callable, Optional

from aioslsk.events import (PrivateMessageEvent, RoomJoinedEvent,
                            RoomLeftEvent, RoomListEvent, RoomMessageEvent)

from .config import Config, config_dir
from .soulseek import SoulseekError, SoulseekSession, pick_listening_port


class ConnectionManager:
    """Owns the background event loop and the single SoulseekSession.

    ``notify`` is called (from the background thread) with GUI events:
        ("log", message)
        ("chat_connected", username)
        ("pm", username, message, is_direct)
        ("room_msg", room_name, username, message)
        ("room_list", [(room_name, user_count), ...])
        ("room_joined", room_name, username_or_None)
        ("room_left", room_name)
    """

    def __init__(self, config: Config, notify: Callable[..., None]):
        self.config = config
        self.notify = notify
        self.session: Optional[SoulseekSession] = None
        self._session_key: Optional[tuple] = None
        self._lock: Optional[asyncio.Lock] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        started = threading.Event()

        def thread_main():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self._lock = asyncio.Lock()
            started.set()
            self.loop.run_forever()

        self._thread = threading.Thread(target=thread_main, daemon=True)
        self._thread.start()
        started.wait()

    def submit(self, coro) -> Future:
        """Run a coroutine on the connection thread."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def _current_key(self) -> tuple:
        username, password, _ = self.config.effective_credentials()
        shared = tuple(sorted(self.config["shared_folders"]))
        return (username, password, int(self.config["listening_port"]), shared)

    async def ensure_session(self) -> SoulseekSession:
        """Return a connected session, (re)connecting if the account,
        port, or shares changed since the last connection."""
        async with self._lock:
            key = self._current_key()
            if self.session and self._session_key == key:
                # Reuse only if the underlying client is still logged in;
                # the server can silently drop us (e.g. after a huge search)
                if getattr(self.session.client, "session", None) is not None:
                    return self.session
                self.notify("log", "Soulseek connection dropped - "
                            "reconnecting...")
                try:
                    await self.session.stop()
                except Exception:
                    pass
                self.session = None
            elif self.session:
                self.notify("log", "Reconnecting to Soulseek "
                            "(settings changed)...")
                await self.session.stop()
                self.session = None

            username, password, is_custom = self.config.effective_credentials()
            import os
            shared = [p for p in self.config["shared_folders"]
                      if os.path.isdir(p)]
            preferred = int(self.config["listening_port"])
            port, got_preferred = pick_listening_port(preferred)
            if not got_preferred:
                self.notify("log",
                            f"Listening port {preferred} is in use (another "
                            f"SpiritSeeker or Soulseek client?) - using "
                            f"{port} instead.")
            self.notify("log", f"Connecting to Soulseek as {username}"
                        + (" (your account)" if is_custom else "") + "...")
            if shared:
                self.notify("log", f"Sharing {len(shared)} folder(s) with "
                            "the network.")

            incoming = str(config_dir() / "incoming")
            session = SoulseekSession(
                username=username,
                password=password,
                download_dir=incoming,
                listening_port=port,
                shared_folders=shared,
                cache_dir=str(config_dir() / "shares-cache"),
                log=lambda msg: self.notify("log", msg),
            )
            await session.start()
            self._register_chat_events(session)
            self.session = session
            self._session_key = key
            self.notify("chat_connected", username)
            return session

    async def reset(self):
        """Disconnect; the next ensure_session() reconnects fresh."""
        async with self._lock:
            if self.session:
                await self.session.stop()
                self.session = None
                self._session_key = None
                self.notify("log", "Disconnected from Soulseek.")

    # ------------------------------------------------------------- chat

    def _register_chat_events(self, session: SoulseekSession):
        events = session.client.events

        async def on_pm(event: PrivateMessageEvent):
            msg = event.message
            self.notify("pm", msg.user.name, msg.message,
                        bool(getattr(msg, "is_direct", False)))

        async def on_room_msg(event: RoomMessageEvent):
            msg = event.message
            self.notify("room_msg", msg.room.name, msg.user.name, msg.message)

        async def on_room_list(event: RoomListEvent):
            rooms = sorted(event.rooms, key=lambda r: -r.user_count)
            self.notify("room_list",
                        [(r.name, r.user_count) for r in rooms[:200]])

        async def on_room_joined(event: RoomJoinedEvent):
            room = getattr(event, "room", None)
            user = getattr(event, "user", None)
            self.notify("room_joined",
                        room.name if room else "?",
                        user.name if user else None)

        async def on_room_left(event: RoomLeftEvent):
            room = getattr(event, "room", None)
            user = getattr(event, "user", None)
            if user is None or user.name == self.config.effective_credentials()[0]:
                self.notify("room_left", room.name if room else "?")

        # Hold references so the bus's weakrefs stay alive
        self._chat_handlers = (on_pm, on_room_msg, on_room_list,
                               on_room_joined, on_room_left)
        events.register(PrivateMessageEvent, on_pm)
        events.register(RoomMessageEvent, on_room_msg)
        events.register(RoomListEvent, on_room_list)
        events.register(RoomJoinedEvent, on_room_joined)
        events.register(RoomLeftEvent, on_room_left)

    # ---------------------------------------------------- chat actions
    # All of these are fire-and-forget from the GUI thread; errors are
    # reported through the log.

    def _chat_call(self, coro_factory, description: str):
        async def run():
            try:
                session = await self.ensure_session()
                await coro_factory(session)
            except (SoulseekError, Exception) as exc:  # noqa: BLE001
                self.notify("log", f"{description} failed: {exc}")
        self.submit(run())

    def send_private_message(self, username: str, message: str):
        from aioslsk.commands import PrivateMessageCommand
        self._chat_call(
            lambda s: s.client(PrivateMessageCommand(username, message)),
            f"Message to {username}")

    def send_room_message(self, room: str, message: str):
        from aioslsk.commands import RoomMessageCommand
        self._chat_call(
            lambda s: s.client(RoomMessageCommand(room, message)),
            f"Message to room {room}")

    def join_room(self, room: str):
        from aioslsk.commands import JoinRoomCommand
        self._chat_call(lambda s: s.client(JoinRoomCommand(room)),
                        f"Joining {room}")

    def leave_room(self, room: str):
        from aioslsk.commands import LeaveRoomCommand
        self._chat_call(lambda s: s.client(LeaveRoomCommand(room)),
                        f"Leaving {room}")

    def request_room_list(self):
        from aioslsk.commands import GetRoomListCommand
        self._chat_call(lambda s: s.client(GetRoomListCommand()),
                        "Fetching room list")

    def connect_now(self):
        """Just bring the connection up (used when the chat window opens)."""
        self._chat_call(lambda s: asyncio.sleep(0), "Connecting")

    def browse_user(self, username: str):
        """Fetch a user's shared files. Results arrive as a
        ("browse_result", username, dirs | None, error) GUI event —
        never via direct widget calls, which are not thread-safe."""
        from aioslsk.commands import PeerGetSharesCommand

        async def run():
            try:
                session = await self.ensure_session()
                # PeerGetSharesCommand returns (directories, locked_directories)
                directories, locked = await session.client(
                    PeerGetSharesCommand(username), response=True, timeout=60)
                dirs = list(directories or []) + list(locked or [])
                self.notify("browse_result", username, dirs, None)
            except Exception as exc:  # noqa: BLE001
                self.notify("browse_result", username, None, str(exc))
        self.submit(run())

    def search_files(self, query: str, token: int):
        """Free-form file search. Results arrive as a
        ("search_result", token, query, candidates, error) GUI event."""
        async def run():
            try:
                session = await self.ensure_session()
                results = await session.search(query)
                self.notify("search_result", token, query, results, None)
            except Exception as exc:  # noqa: BLE001
                self.notify("search_result", token, query, [], str(exc))
        self.submit(run())

    def download_remote_file(self, username: str, remote_path: str,
                             filesize: int, dest_dir: str):
        """Download a single browsed file straight into dest_dir."""
        import os
        import shutil

        from .soulseek import Candidate

        ext = remote_path.rsplit(".", 1)[-1].lower() if "." in remote_path else ""
        cand = Candidate(username=username, remote_path=remote_path,
                         filesize=filesize, extension=ext)
        basename = remote_path.replace("\\", "/").rsplit("/", 1)[-1]

        async def run():
            try:
                session = await self.ensure_session()
                self.notify("log", f"Downloading {basename} "
                            f"from {username}...")
                path = await session.download(cand)
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, basename)
                n = 2
                while os.path.exists(dest):
                    stem, dot, e = basename.rpartition(".")
                    dest = os.path.join(
                        dest_dir, f"{stem or e} ({n}){dot}{e if stem else ''}")
                    n += 1
                shutil.move(path, dest)
                self.notify("log", f"Downloaded {basename} -> {dest}")
            except Exception as exc:  # noqa: BLE001
                self.notify("log", f"Download of {basename} failed: {exc}")
        self.submit(run())
