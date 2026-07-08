"""SpiritSeeker GUI - paste a Spotify playlist link, get verified music."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import APP_NAME, __version__
from .config import Config
from .spotify import Playlist, SpotifyError, fetch_playlist, import_csv
from .workflow import Status, Worker

STATUS_COLORS = {
    Status.PENDING: "#888888",
    Status.SEARCHING: "#b58900",
    Status.DOWNLOADING: "#268bd2",
    Status.VERIFYING: "#6c71c4",
    Status.TAGGING: "#2aa198",
    Status.DONE: "#1a8a2a",
    Status.SKIPPED: "#7a7a7a",
    Status.FAILED: "#dc322f",
}


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.config = Config()
        self.playlist: Playlist | None = None
        self.worker: Worker | None = None
        self.events: queue.Queue = queue.Queue()

        root.title(f"{APP_NAME} v{__version__}")
        root.geometry("980x680")
        root.minsize(760, 520)

        self._build_ui()
        self.root.after(100, self._poll_events)

    # ------------------------------------------------------------- UI setup

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- playlist input row ---
        top = ttk.Frame(self.root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Spotify playlist:").pack(side="left")
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
        for status, color in STATUS_COLORS.items():
            self.tree.tag_configure(status.name, foreground=color)

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
        self.summary_var = tk.StringVar(value="Load a playlist to begin")
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
        self.identity_var = tk.StringVar(
            value=f"Soulseek identity: {self.config['soulseek_username']}")
        ttk.Label(bar, textvariable=self.identity_var,
                  foreground="#666666").pack(side="left")
        ttk.Button(bar, text="New identity",
                   command=self.new_identity).pack(side="right")

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

    def new_identity(self):
        if self.worker:
            messagebox.showinfo(APP_NAME, "Stop downloads first.")
            return
        self.config.regenerate_credentials()
        self.identity_var.set(
            f"Soulseek identity: {self.config['soulseek_username']}")
        self.log("Generated a new Soulseek identity.")

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
        self.progress.configure(value=0)

        self.worker = Worker(self.playlist, self.config,
                             lambda *event: self.events.put(event))
        self.worker.start()

    def stop_downloads(self):
        if self.worker:
            self.stop_btn.configure(state="disabled")
            self.log("Stopping...")
            self.worker.cancel()

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
