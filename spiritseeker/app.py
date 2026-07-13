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

        root.title(f"{APP_NAME} v{__version__}")
        root.geometry("980x680")
        root.minsize(760, 520)

        self._build_ui()
        self._apply_theme()
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
        columns = ("num", "title", "artist", "status", "detail")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings",
                                 selectmode="browse")
        for col, text, width, stretch in (
                ("num", "#", 36, False),
                ("title", "Title", 240, True),
                ("artist", "Artist", 180, True),
                ("status", "Status", 100, False),
                ("detail", "Details", 320, True)):
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
        self.dark_var = tk.BooleanVar(value=self.config["dark_mode"])
        ttk.Checkbutton(bar, text="Dark mode", variable=self.dark_var,
                        command=self._toggle_dark).pack(side="right",
                                                        padx=(0, 12))

    # ---------------------------------------------------------------- theme

    def _toggle_dark(self):
        self.config["dark_mode"] = bool(self.dark_var.get())
        self.config.save()
        self._apply_theme()

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
        self.tree.delete(*self.tree.get_children())
        for i, track in enumerate(playlist.tracks):
            self.tree.insert("", "end", iid=str(i), values=(
                i + 1, track.title, track.artist, Status.PENDING.value, ""),
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

        self.worker = Worker(self.playlist, self.config,
                             lambda *event: self.events.put(event))
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

    def _selected_file(self) -> str | None:
        index = self._selected_index()
        path = self.track_paths.get(index) if index is not None else None
        return path if path and os.path.exists(path) else None

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        self.tree.selection_set(row)
        index = int(row)
        track = self.playlist.tracks[index]
        has_file = self._selected_file() is not None
        status = str(self.tree.item(row)["values"][3])
        can_retry = self.worker is None and status in (
            Status.FAILED.value, Status.DONE.value, Status.SKIPPED.value)

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
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

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

    def _retry_selected(self):
        index = self._selected_index()
        if index is None or self.worker:
            return
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
            if event[0] in ("track", "track_path", "progress"):
                event = (event[0], index) + event[2:]
            self.events.put(event)

        self.log(f"Retrying: {track.artist} - {track.title}")
        self.worker = Worker(single, self.config, remapped)
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
            self.tree.item(str(index), values=(
                index + 1, track.title, track.artist, status.value, detail),
                tags=(status.name,))
            self.tree.see(str(index))
            if status in (Status.DONE, Status.SKIPPED, Status.FAILED):
                self.progress.step(1)
        elif kind == "progress":
            _, index, done, total = event
            if total:
                pct = done * 100 // total
                current = self.tree.item(str(index))["values"]
                self.tree.item(str(index), values=(
                    *current[:3], Status.DOWNLOADING.value,
                    f"{pct}% of {total / (1024 * 1024):.1f}MB"))
        elif kind == "track_path":
            self.track_paths[event[1]] = event[2]
        elif kind == "log":
            self.log(event[1])
        elif kind == "finished":
            _, ok, failed = event
            self.worker = None
            self.start_btn.configure(state="normal")
            self.load_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if ok >= 0:
                self.summary_var.set(f"Finished: {ok} ok, {failed} failed")
                self.log(f"Finished. {ok} tracks ok, {failed} failed.")
            else:
                self.summary_var.set("Stopped")
        elif kind == "fatal":
            self.worker = None
            self.start_btn.configure(state="normal")
            self.load_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.summary_var.set("Error")
            messagebox.showerror(APP_NAME, event[1])


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
        cfg["shared_folders"] = list(self.share_list.get(0, "end"))
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
