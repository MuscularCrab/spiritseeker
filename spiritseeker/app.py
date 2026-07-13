"""SpiritSeeker GUI - paste a Spotify playlist link, get verified music."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__
from .config import Config
from .connection import ConnectionManager
from .spotify import Playlist, SpotifyError, fetch_playlist, import_csv
from .workflow import Status, Worker

STATUS_COLORS = {
    Status.PENDING: ("#888888", "#9a9a9a"),
    Status.SEARCHING: ("#b58900", "#d9b53a"),
    Status.DOWNLOADING: ("#268bd2", "#58aeea"),
    Status.VERIFYING: ("#6c71c4", "#9d97e8"),
    Status.TAGGING: ("#2aa198", "#3fc7bc"),
    Status.DONE: ("#1a8a2a", "#53c95f"),
    Status.SKIPPED: ("#7a7a7a", "#8f8f8f"),
    Status.FAILED: ("#dc322f", "#ff6b5e"),
}

DARK = {
    "bg": "#1f1f1f",
    "surface": "#2b2b2b",
    "field": "#333333",
    "fg": "#e6e6e6",
    "subtle": "#9a9a9a",
    "select": "#0a4a77",
    "accent": "#58aeea",
}
LIGHT = {
    "bg": "SystemButtonFace",
    "surface": "SystemButtonFace",
    "field": "white",
    "fg": "SystemWindowText",
    "subtle": "#666666",
    "select": "SystemHighlight",
    "accent": "#268bd2",
}


def palette(dark: bool) -> dict:
    return DARK if dark else LIGHT


def show_toast(window: tk.Misc, title: str, message: str):
    """Windows tray balloon/toast via Shell_NotifyIcon (pure ctypes)."""
    try:
        import ctypes
        from ctypes import wintypes

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", ctypes.c_wchar * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", ctypes.c_wchar * 256),
                ("uVersion", wintypes.UINT),
                ("szInfoTitle", ctypes.c_wchar * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        NIM_ADD, NIM_DELETE = 0x0, 0x2
        NIF_ICON, NIF_TIP, NIF_INFO = 0x2, 0x4, 0x10
        NIIF_INFO = 0x1

        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = 0x5EEC
        data.uFlags = NIF_ICON | NIF_TIP | NIF_INFO
        data.hIcon = ctypes.windll.user32.LoadIconW(None, 32512)  # IDI_APPLICATION
        data.szTip = APP_NAME
        data.szInfo = message[:255]
        data.szInfoTitle = title[:63]
        data.dwInfoFlags = NIIF_INFO
        ctypes.windll.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))

        def cleanup():
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(
                    NIM_DELETE, ctypes.byref(data))
            except Exception:
                pass
        window.after(10000, cleanup)
    except Exception:
        pass


def flash_taskbar(window: tk.Misc):
    """Flash the taskbar button until the window gets focus."""
    try:
        import ctypes
        from ctypes import wintypes

        class FLASHWINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT),
                        ("hwnd", wintypes.HWND),
                        ("dwFlags", wintypes.DWORD),
                        ("uCount", wintypes.UINT),
                        ("dwTimeout", wintypes.DWORD)]

        FLASHW_ALL, FLASHW_TIMERNOFG = 0x3, 0xC
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd,
                          FLASHW_ALL | FLASHW_TIMERNOFG, 0, 0)
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(info))
    except Exception:
        pass


def set_titlebar_dark(window: tk.Misc, dark: bool):
    """Ask DWM for a dark title bar (Windows 10 1809+; silently no-ops)."""
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(value), ctypes.sizeof(value))
        # SWP_FRAMECHANGED nudge so the bar repaints without a focus change
        SWP = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP)
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = Config()
        self.playlist: Playlist | None = None
        self.worker: Worker | None = None
        self.events: queue.Queue = queue.Queue()
        self.track_paths: dict[int, str] = {}
        self.skip_requests: set[int] = set()
        self.worker_index_map: dict[int, int] | None = None
        self.conn = ConnectionManager(self.config,
                                      lambda *event: self.events.put(event))
        # Chat state lives here so the window can close without losing it
        self.chat_window = None
        self.pm_history: dict[str, list[tuple[str, str, str]]] = {}
        self.room_history: dict[str, list[tuple[str, str, str]]] = {}
        self.joined_rooms: set[str] = set()
        self.unread_chats = 0

        root.title(f"{APP_NAME} v{__version__}")
        root.geometry("980x680")
        root.minsize(760, 520)

        self._build_ui()
        self._apply_theme()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_events)

    # ------------------------------------------------------------- UI setup

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- playlist input row ---
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Spotify link (playlist or song):").pack(side="left")
        self.url_var = tk.StringVar()
        url_entry = ttk.Entry(top, textvariable=self.url_var)
        url_entry.pack(side="left", fill="x", expand=True, padx=6)
        url_entry.bind("<Return>", lambda e: self.load_playlist())
        self.load_btn = ttk.Button(top, text="Load playlist",
                                   command=self.load_playlist)
        self.load_btn.pack(side="left")
        ttk.Button(top, text="Import CSV...",
                   command=self.load_csv).pack(side="left", padx=(6, 0))

        # --- options row ---
        opts = ttk.Frame(self.root)
        opts.pack(fill="x", **pad)
        ttk.Label(opts, text="Save to:").pack(side="left")
        self.dir_var = tk.StringVar(value=self.config["output_dir"])
        ttk.Entry(opts, textvariable=self.dir_var).pack(
            side="left", fill="x", expand=True, padx=6)
        ttk.Button(opts, text="Browse...",
                   command=self.pick_folder).pack(side="left")

        opts2 = ttk.Frame(self.root)
        opts2.pack(fill="x", **pad)
        self.allow_lower_var = tk.BooleanVar(
            value=self.config["allow_lower_quality"])
        ttk.Checkbutton(
            opts2, variable=self.allow_lower_var,
            text="Allow lower quality when no 320kbps+/lossless copy exists",
        ).pack(side="left")
        self.spectral_var = tk.BooleanVar(value=self.config["spectral_check"])
        ttk.Checkbutton(
            opts2, variable=self.spectral_var,
            text="Spectral verification (detect fake 320s, like Spek)",
        ).pack(side="left", padx=(16, 0))

        # --- track table ---
        table_frame = ttk.Frame(self.root)
        table_frame.pack(fill="both", expand=True, **pad)
        columns = ("num", "title", "artist", "file", "status", "detail")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings",
                                 selectmode="extended")
        for col, text, width, stretch in (
                ("num", "#", 36, False),
                ("title", "Title", 190, True),
                ("artist", "Artist", 140, True),
                ("file", "Source file", 260, True),
                ("status", "Status", 95, False),
                ("detail", "Details", 300, True)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch,
                             anchor="w" if col != "num" else "e")
        scroll = ttk.Scrollbar(table_frame, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Double-1>", lambda e: self._play_selected())

        # --- action row ---
        actions = ttk.Frame(self.root)
        actions.pack(fill="x", **pad)
        self.start_btn = ttk.Button(actions, text="Download all",
                                    command=self.start_downloads,
                                    state="disabled")
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Stop",
                                   command=self.stop_downloads,
                                   state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        self.progress = ttk.Progressbar(actions, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=10)
        self.summary_var = tk.StringVar(
            value="Paste a playlist or song link to begin")
        ttk.Label(actions, textvariable=self.summary_var).pack(side="right")

        # --- log pane ---
        log_frame = ttk.LabelFrame(self.root, text="Log")
        log_frame.pack(fill="x", **pad)
        self.log_text = tk.Text(log_frame, height=6, state="disabled",
                                wrap="word", font=("Consolas", 9))
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical",
                                   command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        # --- status bar ---
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=8, pady=(0, 6))
        self.identity_var = tk.StringVar()
        self._refresh_identity_label()
        ttk.Label(bar, textvariable=self.identity_var,
                  style="Subtle.TLabel").pack(side="left")
        ttk.Button(bar, text="Account & sharing...",
                   command=self.open_account_dialog).pack(side="right")
        ttk.Button(bar, text="New identity",
                   command=self.new_identity).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="Settings...",
                   command=self.open_settings_dialog).pack(side="right",
                                                           padx=(0, 6))
        self.chat_btn = ttk.Button(bar, text="Chat...",
                                   command=self.open_chat)
        self.chat_btn.pack(side="right", padx=(0, 6))
        self.dark_var = tk.BooleanVar(value=self.config["dark_mode"])

    # ------------------------------------------------------------- shutdown

    def _on_close(self):
        """Deterministic exit: background threads (connection loop, verify/
        tag executor threads, wedged library internals) must never keep the
        process alive after the window is gone."""
        import os
        try:
            if self.worker:
                self.worker.cancel()
            future = self.conn.submit(self.conn.reset())
            try:
                future.result(timeout=3)   # best-effort clean disconnect
            except Exception:
                pass
            if self.conn.loop:
                self.conn.loop.call_soon_threadsafe(self.conn.loop.stop)
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        os._exit(0)

    # ---------------------------------------------------------------- theme

    def open_settings_dialog(self):
        SettingsDialog(self.root, self.config,
                       on_saved=self._on_settings_saved)

    def _on_settings_saved(self):
        self.dark_var.set(bool(self.config["dark_mode"]))
        self._apply_theme()
        self.log("Settings saved.")

    def _apply_theme(self):
        dark = bool(self.dark_var.get())
        p = palette(dark)
        style = ttk.Style(self.root)
        if dark:
            # clam is the only built-in theme that respects color options
            style.theme_use("clam")
            style.configure(".", background=p["bg"], foreground=p["fg"],
                            fieldbackground=p["field"],
                            troughcolor=p["surface"], bordercolor="#454545",
                            lightcolor=p["surface"], darkcolor=p["bg"],
                            insertcolor=p["fg"])
            style.configure("TButton", background=p["surface"],
                            foreground=p["fg"])
            style.map("TButton", background=[("active", "#3d3d3d")])
            for w in ("TCheckbutton", "TRadiobutton"):
                style.configure(w, background=p["bg"], foreground=p["fg"])
                style.map(w, background=[("active", p["bg"])],
                          foreground=[("disabled", p["subtle"])])
            style.configure("TEntry", fieldbackground=p["field"],
                            foreground=p["fg"])
            style.map("TEntry",
                      fieldbackground=[("disabled", p["surface"])],
                      foreground=[("disabled", p["subtle"])])
            style.configure("TLabelframe", background=p["bg"])
            style.configure("TLabelframe.Label", background=p["bg"],
                            foreground=p["subtle"])
            style.configure("Treeview", background=p["field"],
                            fieldbackground=p["field"], foreground=p["fg"])
            style.configure("Treeview.Heading", background=p["surface"],
                            foreground=p["fg"])
            style.map("Treeview",
                      background=[("selected", p["select"])],
                      foreground=[("selected", "#ffffff")])
            style.configure("Horizontal.TProgressbar",
                            background=p["accent"], troughcolor=p["field"])
        else:
            style.theme_use("vista" if "vista" in style.theme_names()
                            else "clam")
        style.configure("Subtle.TLabel", background=p["bg"],
                        foreground=p["subtle"])
        self.root.configure(bg=p["bg"])
        set_titlebar_dark(self.root, dark)
        self.log_text.configure(bg=p["field"], fg=p["fg"],
                                insertbackground=p["fg"])
        for status, colors in STATUS_COLORS.items():
            self.tree.tag_configure(status.name,
                                    foreground=colors[1 if dark else 0])

    # --------------------------------------------------------------- actions

    def log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def pick_folder(self):
        folder = filedialog.askdirectory(initialdir=self.dir_var.get() or None)
        if folder:
            self.dir_var.set(folder)

    def _refresh_identity_label(self):
        username, _, is_custom = self.config.effective_credentials()
        kind = "your account" if is_custom else "auto-generated"
        shared = len([p for p in self.config["shared_folders"]
                      if os.path.isdir(p)])
        text = f"Soulseek: {username} ({kind})"
        if shared:
            text += f" | sharing {shared} folder(s)"
        self.identity_var.set(text)

    def new_identity(self):
        if self.worker:
            messagebox.showinfo(APP_NAME, "Stop downloads first.")
            return
        self.config.regenerate_credentials()
        if self.config["use_custom_login"]:
            self.log("Generated a new rotating identity (note: you're "
                     "currently logging in with your own account).")
        else:
            self.log("Generated a new Soulseek identity.")
        self._refresh_identity_label()
        self.conn.submit(self.conn.reset())

    def open_account_dialog(self):
        if self.worker:
            messagebox.showinfo(APP_NAME, "Stop downloads first.")
            return
        AccountDialog(self.root, self.config, on_saved=self._on_account_saved)

    def _on_account_saved(self):
        self._refresh_identity_label()
        username, _, is_custom = self.config.effective_credentials()
        self.log(f"Soulseek settings saved - logging in as {username} "
                 f"({'your account' if is_custom else 'rotating identity'}).")
        self.conn.submit(self.conn.reset())

    # ----------------------------------------------------------------- chat

    def open_chat(self):
        from .chat import ChatWindow
        if self.chat_window is None or not self.chat_window.winfo_exists():
            self.chat_window = ChatWindow(self)
        else:
            self.chat_window.lift()
        self.unread_chats = 0
        self._update_chat_button()

    def _update_chat_button(self):
        label = "Chat..." if not self.unread_chats \
            else f"Chat ({self.unread_chats})..."
        self.chat_btn.configure(text=label)

    def load_playlist(self):
        url = self.url_var.get().strip()
        if not url:
            return
        self.load_btn.configure(state="disabled")
        self.summary_var.set("Fetching playlist...")

        def fetch():
            try:
                playlist = fetch_playlist(url)
                self.events.put(("playlist", playlist))
            except SpotifyError as exc:
                self.events.put(("playlist_error", str(exc)))

        threading.Thread(target=fetch, daemon=True).start()

    def load_csv(self):
        path = filedialog.askopenfilename(
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            self._set_playlist(import_csv(path))
        except SpotifyError as exc:
            messagebox.showerror(APP_NAME, str(exc))

    def _set_playlist(self, playlist: Playlist):
        self.playlist = playlist
        self.track_paths.clear()
        self.skip_requests.clear()
        self.tree.delete(*self.tree.get_children())
        for i, track in enumerate(playlist.tracks):
            self.tree.insert("", "end", iid=str(i), values=(
                i + 1, track.title, track.artist, "",
                Status.PENDING.value, ""),
                tags=(Status.PENDING.name,))
        self.summary_var.set(
            f"{playlist.name} - {len(playlist.tracks)} tracks")
        self.start_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=max(len(playlist.tracks), 1))
        self.log(f"Loaded '{playlist.name}' ({len(playlist.tracks)} tracks)")
        if playlist.maybe_truncated:
            self.log("Note: Spotify's public page only exposes the first 100 "
                     "tracks. For longer playlists use Import CSV (export via "
                     "Exportify or chosic.com).")

    def start_downloads(self):
        if not self.playlist or self.worker:
            return
        self.config["output_dir"] = self.dir_var.get().strip()
        self.config["allow_lower_quality"] = self.allow_lower_var.get()
        self.config["spectral_check"] = self.spectral_var.get()
        self.config.save()

        self.start_btn.configure(state="disabled")
        self.load_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(
            value=0, maximum=max(len(self.playlist.tracks), 1))

        self.worker_index_map = None    # identity: GUI index == worker index
        self.worker = Worker(self.playlist, self.config,
                             lambda *event: self.events.put(event),
                             manager=self.conn,
                             should_skip=lambda i: i in self.skip_requests)
        self.worker.start()

    def stop_downloads(self):
        if self.worker:
            self.stop_btn.configure(state="disabled")
            self.log("Stopping...")
            self.worker.cancel()

    # ------------------------------------------------------ context menu

    def _selected_index(self) -> int | None:
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _selected_indices(self) -> list[int]:
        return [int(i) for i in self.tree.selection()]

    def _row_status(self, index: int) -> str:
        values = self.tree.item(str(index))["values"]
        return str(values[4]) if len(values) > 4 else ""

    def _selected_file(self) -> str | None:
        index = self._selected_index()
        path = self.track_paths.get(index) if index is not None else None
        return path if path and os.path.exists(path) else None

    def _skippable_now(self, index: int) -> bool:
        status = self._row_status(index)
        if status == Status.PENDING.value and index not in self.skip_requests:
            return True
        return self.worker is not None and status in (
            Status.SEARCHING.value, Status.DOWNLOADING.value,
            Status.VERIFYING.value, Status.TAGGING.value)

    def _bulk_skip(self, indices: list[int]):
        for index in indices:
            status = self._row_status(index)
            if status == Status.PENDING.value:
                self._set_skip(index, True)
            elif self.worker and status in (
                    Status.SEARCHING.value, Status.DOWNLOADING.value,
                    Status.VERIFYING.value, Status.TAGGING.value):
                self._skip_active(index)

    def _bulk_include(self, indices: list[int]):
        for index in indices:
            if index in self.skip_requests:
                self._set_skip(index, False)

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        # Right-clicking inside an existing multi-selection keeps it;
        # right-clicking elsewhere selects just that row
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        selected = self._selected_indices()
        if len(selected) > 1:
            self._show_bulk_menu(event, selected)
            return
        index = int(row)
        track = self.playlist.tracks[index]
        has_file = self._selected_file() is not None
        values = self.tree.item(row)["values"]
        status = str(values[4])
        detail = str(values[5]) if len(values) > 5 else ""
        can_retry = self.worker is None and status in (
            Status.FAILED.value, Status.DONE.value, Status.SKIPPED.value)
        is_duplicate = (status == Status.SKIPPED.value
                        and "already in folder" in detail)

        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(label="Play", command=self._play_selected,
                         state="normal" if has_file else "disabled")
        menu.add_command(label="Open folder location",
                         command=self._open_folder_location)
        menu.add_separator()
        menu.add_command(label="Copy file path", command=self._copy_path,
                         state="normal" if has_file else "disabled")
        menu.add_command(label=f"Copy \"{track.artist} - {track.title}\""[:60],
                         command=self._copy_title)
        menu.add_separator()
        retry_label = ("Retry download" if status == Status.FAILED.value
                       else "Download again")
        menu.add_command(label=retry_label, command=self._retry_selected,
                         state="normal" if can_retry else "disabled")
        if is_duplicate:
            menu.add_command(
                label="Overwrite duplicate (re-download)",
                command=self._retry_selected,
                state="normal" if self.worker is None else "disabled")
        active = self.worker is not None and status in (
            Status.SEARCHING.value, Status.DOWNLOADING.value,
            Status.VERIFYING.value, Status.TAGGING.value)
        if active:
            menu.add_command(label="Skip download",
                             command=lambda: self._skip_active(index))
        elif status == Status.PENDING.value or index in self.skip_requests:
            if index in self.skip_requests:
                menu.add_command(label="Include in downloads",
                                 command=lambda: self._set_skip(index, False))
            else:
                menu.add_command(label="Skip download",
                                 command=lambda: self._set_skip(index, True))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _show_bulk_menu(self, event, selected: list[int]):
        skippable = [i for i in selected if self._skippable_now(i)]
        includable = [i for i in selected if i in self.skip_requests]
        with_files = [i for i in selected
                      if self.track_paths.get(i)
                      and os.path.exists(self.track_paths[i])]

        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(
            label=f"Skip download ({len(skippable)} tracks)",
            command=lambda: self._bulk_skip(skippable),
            state="normal" if skippable else "disabled")
        menu.add_command(
            label=f"Include in downloads ({len(includable)} tracks)",
            command=lambda: self._bulk_include(includable),
            state="normal" if includable else "disabled")
        menu.add_separator()
        menu.add_command(
            label=f"Copy file paths ({len(with_files)})",
            command=lambda: self._copy_paths(with_files),
            state="normal" if with_files else "disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_paths(self, indices: list[int]):
        paths = [self.track_paths[i] for i in indices
                 if self.track_paths.get(i)]
        if paths:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(paths))

    def _play_selected(self):
        path = self._selected_file()
        if path:
            os.startfile(path)

    def _open_folder_location(self):
        path = self._selected_file()
        if path:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return
        # No file yet for this track: open the save folder itself
        folder = self.dir_var.get().strip() or self.config["output_dir"]
        if os.path.isdir(folder):
            os.startfile(folder)
        else:
            messagebox.showinfo(APP_NAME, "Nothing downloaded here yet.")

    def _copy_path(self):
        path = self._selected_file()
        if path:
            self.root.clipboard_clear()
            self.root.clipboard_append(path)

    def _copy_title(self):
        index = self._selected_index()
        if index is not None:
            track = self.playlist.tracks[index]
            self.root.clipboard_clear()
            self.root.clipboard_append(f"{track.artist} - {track.title}")

    def _skip_active(self, index: int):
        """Skip a track that is already searching/downloading."""
        self.skip_requests.add(index)
        if not self.worker:
            return
        local = index
        if self.worker_index_map is not None:
            if index not in self.worker_index_map:
                return
            local = self.worker_index_map[index]
        self.worker.skip_track(local)

    def _set_skip(self, index: int, skip: bool):
        track = self.playlist.tracks[index]
        current = self.tree.item(str(index))["values"]
        file_cell = current[3] if len(current) > 3 else ""
        if skip:
            self.skip_requests.add(index)
            self.tree.item(str(index), values=(
                index + 1, track.title, track.artist, file_cell,
                Status.SKIPPED.value, "skipped by you"),
                tags=(Status.SKIPPED.name,))
        else:
            self.skip_requests.discard(index)
            self.tree.item(str(index), values=(
                index + 1, track.title, track.artist, file_cell,
                Status.PENDING.value, ""), tags=(Status.PENDING.name,))

    def _retry_selected(self):
        index = self._selected_index()
        if index is None or self.worker:
            return
        self.skip_requests.discard(index)
        track = self.playlist.tracks[index]
        # Downloading again shouldn't hit the already-in-folder skip
        old = self.track_paths.pop(index, None)
        if old and os.path.exists(old):
            try:
                os.remove(old)
            except OSError as exc:
                messagebox.showerror(
                    APP_NAME, f"Could not remove the old file:\n{exc}")
                return

        self.config["output_dir"] = self.dir_var.get().strip()
        self.config["allow_lower_quality"] = self.allow_lower_var.get()
        self.config["spectral_check"] = self.spectral_var.get()
        self.config.save()

        self.start_btn.configure(state="disabled")
        self.load_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.progress.configure(value=0, maximum=1)

        single = Playlist(name=str(track), tracks=[track])

        def remapped(*event):
            if event[0] in ("track", "track_path", "track_file", "progress"):
                event = (event[0], index) + event[2:]
            self.events.put(event)

        self.log(f"Retrying: {track.artist} - {track.title}")
        self.worker_index_map = {index: 0}
        self.worker = Worker(single, self.config, remapped, manager=self.conn)
        self.worker.start()

    # ------------------------------------------------------- event pump

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                self._handle_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _handle_event(self, event):
        kind = event[0]
        if kind == "playlist":
            self.load_btn.configure(state="normal")
            self._set_playlist(event[1])
        elif kind == "playlist_error":
            self.load_btn.configure(state="normal")
            self.summary_var.set("Could not load playlist")
            messagebox.showerror(APP_NAME, event[1])
        elif kind == "track":
            _, index, status, detail = event
            track = self.playlist.tracks[index]
            current = self.tree.item(str(index))["values"]
            file_cell = current[3] if len(current) > 3 else ""
            self.tree.item(str(index), values=(
                index + 1, track.title, track.artist, file_cell,
                status.value, detail),
                tags=(status.name,))
            self.tree.see(str(index))
            if status in (Status.DONE, Status.SKIPPED, Status.FAILED):
                self.progress.step(1)
        elif kind == "track_file":
            _, index, filename = event
            current = self.tree.item(str(index))["values"]
            if len(current) >= 6:
                self.tree.item(str(index), values=(
                    *current[:3], filename, *current[4:]))
        elif kind == "progress":
            _, index, done, total, rate = event
            if total:
                pct = min(done * 100 // total, 100)
                blocks = pct // 10
                bar = "█" * blocks + "░" * (10 - blocks)
                if rate >= 1024 * 1024:
                    rate_s = f"  {rate / (1024 * 1024):.1f} MB/s"
                elif rate > 0:
                    rate_s = f"  {rate / 1024:.0f} KB/s"
                else:
                    rate_s = ""
                current = self.tree.item(str(index))["values"]
                self.tree.item(str(index), values=(
                    *current[:4], Status.DOWNLOADING.value,
                    f"{bar} {pct}% of {total / (1024 * 1024):.1f}MB{rate_s}"))
        elif kind == "track_path":
            self.track_paths[event[1]] = event[2]
        elif kind == "log":
            self.log(event[1])
        elif kind == "chat_connected":
            self._refresh_identity_label()
        elif kind == "pm":
            _, username, message, _direct = event
            import time as _time
            stamp = _time.strftime("%H:%M")
            self.pm_history.setdefault(username, []).append(
                (stamp, username, message))
            if self.chat_window and self.chat_window.winfo_exists():
                self.chat_window.refresh_pms()
            else:
                self.unread_chats += 1
                self._update_chat_button()
                self.log(f"[PM] {username}: {message}")
        elif kind == "room_msg":
            _, room, username, message = event
            import time as _time
            stamp = _time.strftime("%H:%M")
            self.room_history.setdefault(room, []).append(
                (stamp, username, message))
            if self.chat_window and self.chat_window.winfo_exists():
                self.chat_window.refresh_rooms()
        elif kind == "room_list":
            if self.chat_window and self.chat_window.winfo_exists():
                self.chat_window.set_room_list(event[1])
        elif kind == "room_joined":
            _, room, username = event
            me = self.config.effective_credentials()[0]
            import time as _time
            stamp = _time.strftime("%H:%M")
            if username is None or username == me:
                self.joined_rooms.add(room)
                self.room_history.setdefault(room, []).append(
                    (stamp, "*", "you joined the room"))
            else:
                self.room_history.setdefault(room, []).append(
                    (stamp, "*", f"{username} joined"))
            if self.chat_window and self.chat_window.winfo_exists():
                self.chat_window.refresh_rooms(select=room)
        elif kind == "room_left":
            self.joined_rooms.discard(event[1])
            if self.chat_window and self.chat_window.winfo_exists():
                self.chat_window.refresh_rooms()
        elif kind == "finished":
            _, ok, failed = event
            self.worker = None
            self.start_btn.configure(state="normal")
            self.load_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if ok >= 0:
                self.summary_var.set(f"Finished: {ok} ok, {failed} failed")
                self.log(f"Finished. {ok} tracks ok, {failed} failed.")
                if self.config["notify_on_finish"]:
                    try:
                        import winsound
                        winsound.MessageBeep(winsound.MB_ICONASTERISK)
                    except Exception:
                        pass
                    show_toast(self.root, f"{APP_NAME} finished",
                               f"{ok} tracks ok, {failed} failed")
                    flash_taskbar(self.root)
            else:
                self.summary_var.set("Stopped")
        elif kind == "fatal":
            self.worker = None
            self.start_btn.configure(state="normal")
            self.load_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.summary_var.set("Error")
            messagebox.showerror(APP_NAME, event[1])


class SettingsDialog(tk.Toplevel):
    """General app settings."""

    def __init__(self, parent: tk.Tk, config, on_saved):
        super().__init__(parent)
        self.config_obj = config
        self.on_saved = on_saved
        self.title("Settings")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        p = palette(bool(config["dark_mode"]))
        self.configure(bg=p["bg"])
        set_titlebar_dark(self, bool(config["dark_mode"]))

        pad = {"padx": 10, "pady": 4}

        # --- appearance ---
        looks = ttk.LabelFrame(self, text="Appearance")
        looks.pack(fill="x", **pad)
        self.dark_var = tk.BooleanVar(value=bool(config["dark_mode"]))
        ttk.Checkbutton(looks, text="Dark mode",
                        variable=self.dark_var).pack(anchor="w", padx=8,
                                                     pady=6)

        # --- downloads ---
        dls = ttk.LabelFrame(self, text="Downloads")
        dls.pack(fill="x", **pad)
        grid = ttk.Frame(dls)
        grid.pack(fill="x", padx=8, pady=6)

        def spin_row(row, label, from_, to, value):
            ttk.Label(grid, text=label).grid(row=row, column=0, sticky="w",
                                             pady=2)
            var = tk.StringVar(value=str(value))
            ttk.Spinbox(grid, textvariable=var, from_=from_, to=to,
                        width=6).grid(row=row, column=1, sticky="w",
                                      padx=8, pady=2)
            return var

        self.timeout_var = spin_row(
            0, "Give up on a track after (minutes):", 1, 120,
            config["track_timeout_min"])
        self.stall_var = spin_row(
            1, "Cancel a download after no data for (seconds):", 10, 600,
            config["stall_timeout_sec"])
        self.attempts_var = spin_row(
            2, "Sources to try per track:", 1, 10, config["max_attempts"])
        self.concurrent_var = spin_row(
            3, "Simultaneous downloads:", 1, 4,
            config["concurrent_downloads"])
        ttk.Label(
            dls, style="Subtle.TLabel", wraplength=430, justify="left",
            text="The track timeout only counts searching and waiting in "
                 "peers' queues - a download that is still receiving data is "
                 "never cut off, however slow.",
        ).pack(anchor="w", padx=8, pady=(0, 4))
        self.retry_var = tk.BooleanVar(value=bool(config["auto_retry_failed"]))
        ttk.Checkbutton(
            dls, text="Retry failed tracks once at the end of a run",
            variable=self.retry_var).pack(anchor="w", padx=8, pady=(0, 2))
        self.overwrite_var = tk.BooleanVar(
            value=bool(config["overwrite_duplicates"]))
        ttk.Checkbutton(
            dls, text="Overwrite songs already in the save folder "
                      "(re-download instead of skipping)",
            variable=self.overwrite_var).pack(anchor="w", padx=8, pady=(0, 2))
        self.notify_var = tk.BooleanVar(value=bool(config["notify_on_finish"]))
        ttk.Checkbutton(
            dls, text="Notification and sound when a run finishes",
            variable=self.notify_var).pack(anchor="w", padx=8, pady=(0, 8))

        # --- buttons ---
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="Save", command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel",
                   command=self.destroy).pack(side="right", padx=(0, 6))

    def _save(self):
        def clamped(var, lo, hi, name):
            try:
                value = int(var.get().strip())
            except ValueError:
                raise ValueError(f"{name} must be a whole number.")
            if not lo <= value <= hi:
                raise ValueError(f"{name} must be between {lo} and {hi}.")
            return value

        try:
            timeout = clamped(self.timeout_var, 1, 120, "Track timeout")
            stall = clamped(self.stall_var, 10, 600, "Stall timeout")
            attempts = clamped(self.attempts_var, 1, 10, "Sources to try")
            concurrent = clamped(self.concurrent_var, 1, 4,
                                 "Simultaneous downloads")
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc), parent=self)
            return
        cfg = self.config_obj
        cfg["dark_mode"] = bool(self.dark_var.get())
        cfg["track_timeout_min"] = timeout
        cfg["stall_timeout_sec"] = stall
        cfg["max_attempts"] = attempts
        cfg["concurrent_downloads"] = concurrent
        cfg["auto_retry_failed"] = bool(self.retry_var.get())
        cfg["notify_on_finish"] = bool(self.notify_var.get())
        cfg["overwrite_duplicates"] = bool(self.overwrite_var.get())
        cfg.save()
        self.destroy()
        self.on_saved()


class AccountDialog(tk.Toplevel):
    """Soulseek login (rotating identity or the user's own account) and
    shared upload folders."""

    def __init__(self, parent: tk.Tk, config, on_saved):
        super().__init__(parent)
        self.config_obj = config
        self.on_saved = on_saved
        self.title("Soulseek account & sharing")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        p = palette(bool(config["dark_mode"]))
        self.configure(bg=p["bg"])
        set_titlebar_dark(self, bool(config["dark_mode"]))

        pad = {"padx": 10, "pady": 4}

        # --- login section ---
        login = ttk.LabelFrame(self, text="Login")
        login.pack(fill="x", **pad)
        self.mode_var = tk.StringVar(
            value="custom" if config["use_custom_login"] else "auto")
        ttk.Radiobutton(
            login, text="Rotating identity (auto-generated, anonymous)",
            variable=self.mode_var, value="auto",
            command=self._sync_state).pack(anchor="w", padx=8, pady=(6, 0))
        self.auto_label = ttk.Label(
            login, text=f"    current: {config['soulseek_username']}",
            style="Subtle.TLabel")
        self.auto_label.pack(anchor="w", padx=8)
        ttk.Radiobutton(
            login, text="My Soulseek account",
            variable=self.mode_var, value="custom",
            command=self._sync_state).pack(anchor="w", padx=8, pady=(8, 0))

        form = ttk.Frame(login)
        form.pack(fill="x", padx=24, pady=(2, 8))
        ttk.Label(form, text="Username:").grid(row=0, column=0, sticky="w")
        self.user_var = tk.StringVar(value=config["custom_username"])
        self.user_entry = ttk.Entry(form, textvariable=self.user_var, width=28)
        self.user_entry.grid(row=0, column=1, sticky="w", padx=6, pady=2)
        ttk.Label(form, text="Password:").grid(row=1, column=0, sticky="w")
        self.pass_var = tk.StringVar(value=config["custom_password"])
        self.pass_entry = ttk.Entry(form, textvariable=self.pass_var,
                                    width=28, show="•")
        self.pass_entry.grid(row=1, column=1, sticky="w", padx=6, pady=2)

        # --- sharing section ---
        share = ttk.LabelFrame(self, text="Shared folders (uploads)")
        share.pack(fill="both", expand=True, **pad)
        ttk.Label(
            share, style="Subtle.TLabel", wraplength=420, justify="left",
            text="These folders are browsable and downloadable by other "
                 "Soulseek users while SpiritSeeker is running. Sharing "
                 "improves your standing with peers that require it.",
        ).pack(anchor="w", padx=8, pady=(6, 4))
        list_frame = ttk.Frame(share)
        list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        self.share_list = tk.Listbox(
            list_frame, height=5, activestyle="none", bg=p["field"],
            fg=p["fg"], selectbackground=p["select"],
            highlightthickness=0)
        share_scroll = ttk.Scrollbar(list_frame, orient="vertical",
                                     command=self.share_list.yview)
        self.share_list.configure(yscrollcommand=share_scroll.set)
        self.share_list.pack(side="left", fill="both", expand=True)
        share_scroll.pack(side="right", fill="y")
        for folder in config["shared_folders"]:
            self.share_list.insert("end", folder)
        share_btns = ttk.Frame(share)
        share_btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(share_btns, text="Add folder...",
                   command=self._add_folder).pack(side="left")
        ttk.Button(share_btns, text="Remove selected",
                   command=self._remove_folder).pack(side="left", padx=(6, 0))

        # --- connection section ---
        conn = ttk.LabelFrame(self, text="Connection")
        conn.pack(fill="x", **pad)
        row = ttk.Frame(conn)
        row.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(row, text="Listening port:").pack(side="left")
        self.port_var = tk.StringVar(value=str(config["listening_port"]))
        ttk.Entry(row, textvariable=self.port_var,
                  width=8).pack(side="left", padx=6)
        ttk.Label(
            conn, style="Subtle.TLabel", wraplength=420, justify="left",
            text="Uses this port (and the next one up) for incoming peer "
                 "connections. If your VPN forwards a port (e.g. PIA's port "
                 "forwarding), enter that port here for the best "
                 "connectivity. When the port is busy, SpiritSeeker "
                 "automatically falls back to a nearby free one.",
        ).pack(anchor="w", padx=8, pady=(0, 8))

        # --- buttons ---
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="Save", command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel",
                   command=self.destroy).pack(side="right", padx=(0, 6))

        self._sync_state()

    def _sync_state(self):
        state = "normal" if self.mode_var.get() == "custom" else "disabled"
        self.user_entry.configure(state=state)
        self.pass_entry.configure(state=state)

    def _add_folder(self):
        folder = filedialog.askdirectory(parent=self)
        if folder and folder not in self.share_list.get(0, "end"):
            self.share_list.insert("end", folder)

    def _remove_folder(self):
        for i in reversed(self.share_list.curselection()):
            self.share_list.delete(i)

    def _save(self):
        use_custom = self.mode_var.get() == "custom"
        username = self.user_var.get().strip()
        password = self.pass_var.get()
        if use_custom and not (username and password):
            messagebox.showerror(
                APP_NAME, "Enter your Soulseek username and password, or "
                "switch back to the rotating identity.", parent=self)
            return
        try:
            port = int(self.port_var.get().strip())
            if not 1024 < port < 65534:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                APP_NAME, "Listening port must be a number between 1025 "
                "and 65533.", parent=self)
            return
        cfg = self.config_obj
        cfg["listening_port"] = port
        cfg["use_custom_login"] = use_custom
        cfg["custom_username"] = username
        cfg["custom_password"] = password
        cfg["shared_folders"] = [os.path.normpath(p) for p in
                                 self.share_list.get(0, "end")]
        cfg.save()
        self.destroy()
        self.on_saved()


def _icon_path() -> str | None:
    import os
    import sys
    base = getattr(sys, "_MEIPASS", None)  # PyInstaller bundle dir
    if base is None:
        base = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
    path = os.path.join(base, "assets", "icon.ico")
    return path if os.path.exists(path) else None


def main():
    root = tk.Tk()
    try:
        root.call("tk", "scaling", 1.25)
    except tk.TclError:
        pass
    icon = _icon_path()
    if icon:
        try:
            root.iconbitmap(icon)
        except tk.TclError:
            pass
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
