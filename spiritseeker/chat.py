"""Chat window: Soulseek private messages, chat rooms, and user browsing."""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox, ttk
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import App


def _stamp() -> str:
    return time.strftime("%H:%M")


class ChatWindow(tk.Toplevel):
    """Private messages + rooms. History lives on the App so it survives
    closing and reopening the window."""

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title("Soulseek chat")
        self.geometry("760x520")
        self.minsize(560, 380)
        from .app import palette, set_titlebar_dark
        p = palette(bool(app.config["dark_mode"]))
        self.p = p
        self.configure(bg=p["bg"])
        set_titlebar_dark(self, bool(app.config["dark_mode"]))

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self._build_pm_tab()
        self._build_rooms_tab()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        app.conn.connect_now()
        self.refresh_pms()
        self.refresh_rooms()

    def _on_close(self):
        self.app.chat_window = None
        self.destroy()

    def _text_widget(self, parent) -> tk.Text:
        text = tk.Text(parent, state="disabled", wrap="word",
                       font=("Segoe UI", 10), bg=self.p["field"],
                       fg=self.p["fg"], insertbackground=self.p["fg"],
                       highlightthickness=0)
        return text

    def _listbox(self, parent, **kw) -> tk.Listbox:
        return tk.Listbox(parent, activestyle="none", bg=self.p["field"],
                          fg=self.p["fg"],
                          selectbackground=self.p["select"],
                          highlightthickness=0, exportselection=False, **kw)

    # ------------------------------------------------------------ PM tab

    def _build_pm_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Private messages")

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="To:").pack(side="left")
        self.pm_to_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.pm_to_var, width=24)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: self._start_conversation())
        ttk.Button(top, text="Start chat",
                   command=self._start_conversation).pack(side="left")
        ttk.Button(top, text="Browse user's files",
                   command=self._browse_user).pack(side="left", padx=(12, 0))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.pm_list = self._listbox(body, width=24)
        self.pm_list.pack(side="left", fill="y")
        self.pm_list.bind("<<ListboxSelect>>", lambda e: self.refresh_pms())

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.pm_text = self._text_widget(right)
        self.pm_text.pack(fill="both", expand=True)
        send_row = ttk.Frame(right)
        send_row.pack(fill="x", pady=(6, 0))
        self.pm_msg_var = tk.StringVar()
        pm_entry = ttk.Entry(send_row, textvariable=self.pm_msg_var)
        pm_entry.pack(side="left", fill="x", expand=True)
        pm_entry.bind("<Return>", lambda e: self._send_pm())
        ttk.Button(send_row, text="Send",
                   command=self._send_pm).pack(side="left", padx=(6, 0))

    def _selected_pm_user(self) -> str | None:
        sel = self.pm_list.curselection()
        if sel:
            return self.pm_list.get(sel[0])
        return None

    def _start_conversation(self):
        user = self.pm_to_var.get().strip()
        if not user:
            return
        self.app.pm_history.setdefault(user, [])
        self.refresh_pms(select=user)

    def _send_pm(self):
        user = self._selected_pm_user() or self.pm_to_var.get().strip()
        text = self.pm_msg_var.get().strip()
        if not user or not text:
            return
        self.app.conn.send_private_message(user, text)
        me = self.app.config.effective_credentials()[0]
        self.app.pm_history.setdefault(user, []).append(
            (_stamp(), me, text))
        self.app._save_chat_history()
        self.pm_msg_var.set("")
        self.refresh_pms(select=user)

    def refresh_pms(self, select: str | None = None):
        users = list(self.app.pm_history.keys())
        current = select or self._selected_pm_user()
        self.pm_list.delete(0, "end")
        for u in users:
            self.pm_list.insert("end", u)
        if current in users:
            i = users.index(current)
            self.pm_list.selection_clear(0, "end")
            self.pm_list.selection_set(i)
        shown = current if current in users else (users[0] if users else None)
        self.pm_text.configure(state="normal")
        self.pm_text.delete("1.0", "end")
        if shown:
            for ts, sender, msg in self.app.pm_history.get(shown, []):
                self.pm_text.insert("end", f"[{ts}] {sender}: {msg}\n")
        self.pm_text.configure(state="disabled")
        self.pm_text.see("end")

    def _browse_user(self):
        user = self._selected_pm_user() or self.pm_to_var.get().strip()
        if not user:
            messagebox.showinfo("Browse", "Select a conversation or type a "
                                "username first.", parent=self)
            return
        BrowseWindow(self.app, user)

    # ---------------------------------------------------------- rooms tab

    def _build_rooms_tab(self):
        tab = ttk.Frame(self.notebook)
        self.notebook.add(tab, text="Rooms")

        top = ttk.Frame(tab)
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(top, text="Room:").pack(side="left")
        self.room_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.room_var, width=28)
        entry.pack(side="left", padx=6)
        entry.bind("<Return>", lambda e: self._join_room())
        ttk.Button(top, text="Join",
                   command=self._join_room).pack(side="left")
        ttk.Button(top, text="Leave",
                   command=self._leave_room).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="Refresh room list",
                   command=self.app.conn.request_room_list).pack(
            side="left", padx=(12, 0))

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        left = ttk.Frame(body)
        left.pack(side="left", fill="y")
        ttk.Label(left, text="Joined").pack(anchor="w")
        self.joined_list = self._listbox(left, width=26, height=6)
        self.joined_list.pack(fill="y", expand=False)
        self.joined_list.bind("<<ListboxSelect>>",
                              lambda e: self.refresh_rooms())
        ttk.Label(left, text="All rooms (double-click to join)").pack(
            anchor="w", pady=(8, 0))
        self.all_rooms_list = self._listbox(left, width=26)
        self.all_rooms_list.pack(fill="both", expand=True)
        self.all_rooms_list.bind("<Double-1>", self._join_from_list)

        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(6, 0))
        self.room_text = self._text_widget(right)
        self.room_text.pack(fill="both", expand=True)
        send_row = ttk.Frame(right)
        send_row.pack(fill="x", pady=(6, 0))
        self.room_msg_var = tk.StringVar()
        room_entry = ttk.Entry(send_row, textvariable=self.room_msg_var)
        room_entry.pack(side="left", fill="x", expand=True)
        room_entry.bind("<Return>", lambda e: self._send_room_msg())
        ttk.Button(send_row, text="Send",
                   command=self._send_room_msg).pack(side="left", padx=(6, 0))

    def _selected_room(self) -> str | None:
        sel = self.joined_list.curselection()
        if sel:
            return self.joined_list.get(sel[0])
        return None

    def _join_room(self):
        room = self.room_var.get().strip()
        if room:
            self.app.conn.join_room(room)

    def _join_from_list(self, event):
        sel = self.all_rooms_list.curselection()
        if sel:
            name = self.all_rooms_list.get(sel[0]).rsplit("  (", 1)[0]
            self.app.conn.join_room(name)

    def _leave_room(self):
        room = self._selected_room()
        if room:
            self.app.conn.leave_room(room)

    def _send_room_msg(self):
        room = self._selected_room()
        text = self.room_msg_var.get().strip()
        if not room or not text:
            return
        self.app.conn.send_room_message(room, text)
        self.room_msg_var.set("")

    def refresh_rooms(self, select: str | None = None):
        joined = sorted(self.app.joined_rooms)
        current = select or self._selected_room()
        self.joined_list.delete(0, "end")
        for r in joined:
            self.joined_list.insert("end", r)
        if current in joined:
            self.joined_list.selection_clear(0, "end")
            self.joined_list.selection_set(joined.index(current))
        shown = current if current in joined else (joined[0] if joined else None)
        self.room_text.configure(state="normal")
        self.room_text.delete("1.0", "end")
        if shown:
            for ts, sender, msg in self.app.room_history.get(shown, []):
                self.room_text.insert("end", f"[{ts}] {sender}: {msg}\n")
        self.room_text.configure(state="disabled")
        self.room_text.see("end")

    def set_room_list(self, rooms: list[tuple[str, int]]):
        self.all_rooms_list.delete(0, "end")
        for name, count in rooms:
            self.all_rooms_list.insert("end", f"{name}  ({count})")


class BrowseWindow(tk.Toplevel):
    """Tree view of one user's shared files; double-click downloads."""

    def __init__(self, app: "App", username: str):
        super().__init__(app.root)
        self.app = app
        self.username = username
        self.title(f"Files shared by {username}")
        self.geometry("720x480")
        from .app import palette, set_titlebar_dark
        p = palette(bool(app.config["dark_mode"]))
        self.configure(bg=p["bg"])
        set_titlebar_dark(self, bool(app.config["dark_mode"]))

        self.status_var = tk.StringVar(value=f"Fetching {username}'s "
                                       "file list...")
        ttk.Label(self, textvariable=self.status_var,
                  style="Subtle.TLabel").pack(anchor="w", padx=8, pady=6)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tree = ttk.Treeview(frame, columns=("size",), show="tree headings")
        self.tree.heading("#0", text="File")
        self.tree.heading("size", text="Size")
        self.tree.column("size", width=90, stretch=False, anchor="e")
        scroll = ttk.Scrollbar(frame, orient="vertical",
                               command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._download_selected)

        self._files: dict[str, tuple[str, int]] = {}   # item id -> (path, size)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        app.browse_windows[username] = self
        app.conn.browse_user(username)

    def _on_close(self):
        self.app.browse_windows.pop(self.username, None)
        self.destroy()

    def deliver(self, dirs, error):
        """Called by App on the GUI thread when browse_result arrives."""
        self._populate(dirs, error)

    def _populate(self, dirs, error):
        if error:
            self.status_var.set(f"Could not browse {self.username}: {error}")
            return
        total = 0
        for d in dirs:
            dir_name = getattr(d, "name", "")
            files = getattr(d, "files", None) or []
            if not files:
                continue
            node = self.tree.insert("", "end", text=dir_name, open=False)
            for f in files:
                # filename is the FULL remote path the peer serves
                remote_path = getattr(f, "filename", "")
                display = remote_path.replace("/", "\\").rsplit("\\", 1)[-1]
                fsize = int(getattr(f, "filesize", 0))
                mb = f"{fsize / (1024 * 1024):.1f}MB" if fsize else ""
                item = self.tree.insert(node, "end", text=display,
                                        values=(mb,))
                self._files[item] = (remote_path, fsize)
                total += 1
        self.status_var.set(
            f"{total} files in {len(self.tree.get_children())} folders - "
            "double-click a file to download it to your save folder")

    def _download_selected(self, event):
        item = self.tree.focus()
        if item not in self._files:
            return
        remote_path, size = self._files[item]
        dest = self.app.dir_var.get().strip() or self.app.config["output_dir"]
        self.app.conn.download_remote_file(self.username, remote_path,
                                           size, dest)
        self.status_var.set(f"Queued: {remote_path.rsplit(chr(92), 1)[-1]} "
                            "(watch the main window log)")


class SearchWindow(tk.Toplevel):
    """Nicotine+-style free-form file search across the whole network."""

    def __init__(self, app: "App"):
        super().__init__(app.root)
        self.app = app
        self.title("Search Soulseek")
        self.geometry("860x520")
        self.minsize(620, 400)
        from .app import palette, set_titlebar_dark
        p = palette(bool(app.config["dark_mode"]))
        self.p = p
        self.configure(bg=p["bg"])
        set_titlebar_dark(self, bool(app.config["dark_mode"]))
        self._token = 0
        self._results: dict[str, object] = {}   # tree item -> Candidate

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=8)
        ttk.Label(top, text="Search:").pack(side="left")
        self.query_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.query_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda e: self.do_search())
        entry.focus_set()
        ttk.Button(top, text="Search", command=self.do_search).pack(side="left")

        self.status_var = tk.StringVar(value="Type a song or artist and hit "
                                       "Search.")
        ttk.Label(self, textvariable=self.status_var,
                  style="Subtle.TLabel").pack(anchor="w", padx=8)

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        cols = ("file", "size", "bitrate", "user")
        self.tree = ttk.Treeview(frame, columns=cols, show="headings")
        for c, text, w in (("file", "File", 380), ("size", "Size", 80),
                           ("bitrate", "Quality", 90), ("user", "User", 140)):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w,
                             anchor="e" if c == "size" else "w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda e: self._download_selected())

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="Download selected",
                   command=self._download_selected).pack(side="left")
        ttk.Button(btns, text="Browse this user's files",
                   command=self._browse_selected).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text="Add to download queue",
                   command=self._queue_selected).pack(side="left", padx=(6, 0))

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        app.search_windows[id(self)] = self

    def _on_close(self):
        self.app.search_windows.pop(id(self), None)
        self.destroy()

    def do_search(self):
        query = self.query_var.get().strip()
        if not query:
            return
        self.tree.delete(*self.tree.get_children())
        self._results.clear()
        self.app.search_token += 1
        self._token = self.app.search_token
        self.status_var.set(f"Searching for '{query}'...")
        self.app.conn.search_files(query, self._token)

    def deliver(self, token, query, candidates, error):
        """Called by App on the GUI thread when search_result arrives."""
        if token != self._token:
            return
        if error:
            self.status_var.set(f"Search failed: {error}")
            return
        ranked = sorted(candidates,
                        key=lambda c: (c.is_lossless, c.bitrate,
                                       c.has_free_slots), reverse=True)
        for c in ranked[:300]:
            quality = ("FLAC" if c.extension == "flac"
                       else (f"{c.bitrate}k" if c.bitrate else c.extension.upper()))
            mb = f"{c.filesize / (1024 * 1024):.1f}MB" if c.filesize else ""
            item = self.tree.insert("", "end", values=(
                c.basename, mb, quality, c.username))
            self._results[item] = c
        self.status_var.set(
            f"{len(self._results)} results for '{query}'"
            + ("" if self._results else " - nothing found, try different words"))

    def _selected_candidate(self):
        sel = self.tree.selection()
        return self._results.get(sel[0]) if sel else None

    def _download_selected(self):
        c = self._selected_candidate()
        if not c:
            return
        dest = self.app.dir_var.get().strip() or self.app.config["output_dir"]
        self.app.conn.download_remote_file(c.username, c.remote_path,
                                           c.filesize, dest)
        self.status_var.set(f"Downloading {c.basename} to your save folder "
                            "(watch the main window log)")

    def _queue_selected(self):
        c = self._selected_candidate()
        if not c:
            return
        from .spotify import Track
        # Strip extension for a clean title; the exact file is what we found
        name = c.basename.rsplit(".", 1)[0]
        self.app.enqueue_manual_track(Track(title=name, artist=""))
        self.status_var.set(f"Added '{name}' to the main download queue.")

    def _browse_selected(self):
        c = self._selected_candidate()
        if c:
            BrowseWindow(self.app, c.username)
