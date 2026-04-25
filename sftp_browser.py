#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
DJ SFTP Wizard
==============

Grafisches SFTP-Tool fuer macOS.

Features:
- Verbindung per Passwort oder SSH-Key
- Lokaler Dateibrowser
- Remote-Dateibrowser
- Dateien und Ordner herunterladen
- Dateien und Ordner hochladen
- Rekursive Transfers mit Fortschritt und Log

Start:
    python3 -m pip install -r requirements.txt
    python3 sftp_browser.py

macOS-App bauen:
    python3 -m pip install pyinstaller
    pyinstaller --windowed --name "DJ SFTP Wizard" sftp_browser.py
"""

from __future__ import annotations

import os
import json
import hashlib
import posixpath
import queue
import shutil
import stat
import subprocess
import tempfile
import threading
import traceback
import signal
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import paramiko
except ImportError:
    paramiko = None

try:
    from mutagen import File as MutagenFile
    from mutagen.flac import FLAC
    from mutagen.id3 import ID3NoHeaderError
    from mutagen.mp4 import MP4Cover
except ImportError:
    MutagenFile = None
    FLAC = None
    ID3NoHeaderError = Exception
    MP4Cover = None


TransferDirection = Literal["download", "upload"]
APP_NAME = "DJ SFTP Wizard"
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
SETTINGS_PATH = APP_SUPPORT_DIR / "settings.json"
KEYCHAIN_SERVICE = "DJ SFTP Wizard"
AUDIO_THUMB_DIR = APP_SUPPORT_DIR / "thumbnails"
AUDIO_THUMB_SIZE = 40
REMOTE_THUMBNAIL_MAX_BYTES = 100 * 1024 * 1024
AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".aif",
    ".aiff",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
}


@dataclass(frozen=True)
class BrowserEntry:
    name: str
    path: str
    is_dir: bool
    size: int


class SftpWizardApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(980, 620)

        self.settings = self.load_settings()

        self.client: Optional["paramiko.SSHClient"] = None
        self.sftp: Optional["paramiko.SFTPClient"] = None
        self.transfer_thread: Optional[threading.Thread] = None
        self.stop_requested = threading.Event()
        self.worker_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker_clients: list["paramiko.SSHClient"] = []
        self.worker_clients_lock = threading.Lock()

        self.local_path_var = tk.StringVar(value=self.settings.get("local_path", str(Path.home() / "Downloads")))
        self.remote_path_var = tk.StringVar(value=self.settings.get("remote_path", "/"))
        self.status_var = tk.StringVar(value="Nicht verbunden.")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_text_var = tk.StringVar(value="Bereit.")
        self.drag_source: Optional[str] = None
        self.preview_process: Optional[subprocess.Popen] = None
        self.preview_temp_path: Optional[Path] = None
        self.player_track_var = tk.StringVar(value="Kein Track geladen.")
        self.player_state_var = tk.StringVar(value="Stop")
        self.player_playlist: list[str] = []
        self.player_index = -1
        self.player_source: Optional[str] = None
        self.preview_paused = False
        self.cover_enabled_var = tk.BooleanVar(value=self.settings.get("cover_enabled", "1") != "0")
        self.local_tree_images: dict[str, tk.PhotoImage] = {}
        self.remote_tree_images: dict[str, tk.PhotoImage] = {}
        self.thumbnail_token = 0
        self.thumbnail_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="cover")
        self.closing = False

        self._build_ui()
        self.refresh_local()
        self._poll_worker_queue()

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # UI ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        self._build_connection_area(root)
        self._build_browser_area(root)
        self._build_action_area(root)
        self._build_log_area(root)
        self._build_player_area(root)

    def _build_connection_area(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Verbindung")
        frame.pack(fill=tk.X)

        self.host_var = tk.StringVar(value=self.settings.get("host", ""))
        self.port_var = tk.StringVar(value=self.settings.get("port", "22"))
        self.user_var = tk.StringVar(value=self.settings.get("user", ""))
        self.password_var = tk.StringVar(value=self.load_secret("password"))
        self.key_var = tk.StringVar(value=self.settings.get("key_path", str(Path.home() / ".ssh" / "id_ed25519")))
        self.key_passphrase_var = tk.StringVar(value=self.load_secret("key_passphrase"))
        self.auth_mode_var = tk.StringVar(value=self.settings.get("auth_mode", "key"))

        self._grid_label_entry(frame, "Host", self.host_var, 0, 0, width=22)
        self._grid_label_entry(frame, "Port", self.port_var, 0, 2, width=8)
        self._grid_label_entry(frame, "User", self.user_var, 0, 4, width=16)

        ttk.Label(frame, text="Login").grid(row=1, column=0, sticky="w", padx=(8, 4), pady=4)
        auth_frame = ttk.Frame(frame)
        auth_frame.grid(row=1, column=1, sticky="w", padx=4, pady=4)
        ttk.Radiobutton(auth_frame, text="SSH-Key", value="key", variable=self.auth_mode_var).pack(side=tk.LEFT)
        ttk.Radiobutton(auth_frame, text="Passwort", value="password", variable=self.auth_mode_var).pack(side=tk.LEFT, padx=(12, 0))

        ttk.Label(frame, text="Passwort").grid(row=1, column=2, sticky="w", padx=(8, 4), pady=4)
        ttk.Entry(frame, textvariable=self.password_var, show="*").grid(row=1, column=3, sticky="ew", padx=4, pady=4)

        ttk.Button(frame, text="Verbinden", command=self.connect_clicked).grid(row=1, column=4, sticky="ew", padx=4, pady=4)
        ttk.Button(frame, text="Trennen", command=self.disconnect).grid(row=1, column=5, sticky="ew", padx=4, pady=4)

        ttk.Label(frame, text="Key").grid(row=2, column=0, sticky="w", padx=(8, 4), pady=4)
        ttk.Entry(frame, textvariable=self.key_var).grid(row=2, column=1, columnspan=2, sticky="ew", padx=4, pady=4)
        ttk.Button(frame, text="Key waehlen", command=self.choose_key).grid(row=2, column=3, sticky="ew", padx=4, pady=4)

        ttk.Label(frame, text="Key-Passphrase").grid(row=2, column=4, sticky="w", padx=(8, 4), pady=4)
        ttk.Entry(frame, textvariable=self.key_passphrase_var, show="*").grid(row=2, column=5, sticky="ew", padx=4, pady=4)

        ttk.Checkbutton(
            frame,
            text="Cover laden",
            variable=self.cover_enabled_var,
            command=self.on_cover_toggle,
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=(8, 4), pady=(4, 8))

        for col in range(6):
            frame.columnconfigure(col, weight=1)

    def _build_browser_area(self, parent: ttk.Frame) -> None:
        pane = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        pane.pack(fill=tk.BOTH, expand=True, pady=(10, 0))

        local_frame = ttk.LabelFrame(pane, text="Lokal")
        remote_frame = ttk.LabelFrame(pane, text="Remote")
        pane.add(local_frame, weight=1)
        pane.add(remote_frame, weight=1)

        self.local_tree = self._build_file_panel(
            local_frame,
            self.local_path_var,
            self.choose_local_path,
            self.go_local_path,
            self.local_up,
            self.refresh_local,
        )
        self.remote_tree = self._build_file_panel(
            remote_frame,
            self.remote_path_var,
            None,
            self.go_remote_path,
            self.remote_up,
            self.refresh_remote,
        )

        self.local_tree.bind("<Double-1>", self.on_local_double_click)
        self.remote_tree.bind("<Double-1>", self.on_remote_double_click)
        self.local_tree.bind("<ButtonPress-1>", lambda event: self.start_drag(event, "local"))
        self.remote_tree.bind("<ButtonPress-1>", lambda event: self.start_drag(event, "remote"))
        self.local_tree.bind("<ButtonRelease-1>", lambda event: self.finish_drag(event, "local"))
        self.remote_tree.bind("<ButtonRelease-1>", lambda event: self.finish_drag(event, "remote"))
        self.local_tree.bind("<Button-2>", lambda event: self.show_local_context_menu(event))
        self.local_tree.bind("<Button-3>", lambda event: self.show_local_context_menu(event))
        self.local_tree.bind("<Control-1>", lambda event: self.show_local_context_menu(event))
        self.remote_tree.bind("<Button-2>", lambda event: self.show_remote_context_menu(event))
        self.remote_tree.bind("<Button-3>", lambda event: self.show_remote_context_menu(event))
        self.remote_tree.bind("<Control-1>", lambda event: self.show_remote_context_menu(event))

        self.local_menu = tk.Menu(self, tearoff=0)
        self.local_menu.add_command(label="Oeffnen", command=self.local_context_open)
        self.local_menu.add_command(label="Audio abspielen", command=self.preview_local_selection)
        self.local_menu.add_command(label="Upload", command=self.upload_selection)
        self.local_menu.add_separator()
        self.local_menu.add_command(label="Neuer Ordner", command=self.create_local_folder)
        self.local_menu.add_command(label="Umbenennen", command=self.rename_local_selection)
        self.local_menu.add_separator()
        self.local_menu.add_command(label="Loeschen", command=self.delete_local_selection)
        self.local_menu.add_separator()
        self.local_menu.add_command(label="Refresh", command=self.refresh_local)

        self.remote_menu = tk.Menu(self, tearoff=0)
        self.remote_menu.add_command(label="Oeffnen", command=self.remote_context_open)
        self.remote_menu.add_command(label="Audio abspielen", command=self.preview_remote_selection)
        self.remote_menu.add_command(label="Download", command=self.download_selection)
        self.remote_menu.add_separator()
        self.remote_menu.add_command(label="Neuer Ordner", command=self.create_remote_folder)
        self.remote_menu.add_command(label="Umbenennen", command=self.rename_remote_selection)
        self.remote_menu.add_separator()
        self.remote_menu.add_command(label="Loeschen", command=self.delete_remote_selection)
        self.remote_menu.add_separator()
        self.remote_menu.add_command(label="Refresh", command=self.refresh_remote)

    def _build_file_panel(
        self,
        parent: ttk.LabelFrame,
        path_var: tk.StringVar,
        choose_command: Optional[Callable[[], None]],
        open_command: Callable[[], None],
        up_command: Callable[[], None],
        refresh_command: Callable[[], None],
    ) -> ttk.Treeview:
        path_bar = ttk.Frame(parent)
        path_bar.pack(fill=tk.X, padx=8, pady=(8, 4))

        entry = ttk.Entry(path_bar, textvariable=path_var)
        entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        entry.bind("<Return>", lambda _event: open_command())

        if choose_command:
            ttk.Button(path_bar, text="Waehlen", command=choose_command).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(path_bar, text="Oeffnen", command=open_command).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(path_bar, text="Hoch", command=up_command).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(path_bar, text="Refresh", command=refresh_command).pack(side=tk.LEFT, padx=(6, 0))

        columns = ("type", "size")
        tree = ttk.Treeview(parent, columns=columns, show="tree headings", selectmode="extended")
        tree.heading("#0", text="Name")
        tree.heading("type", text="Typ")
        tree.heading("size", text="Groesse")
        tree.column("#0", width=360, anchor="w")
        tree.column("type", width=80, anchor="w")
        tree.column("size", width=90, anchor="e")

        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=(4, 8))
        scroll.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 8), pady=(4, 8))

        return tree

    def _build_action_area(self, parent: ttk.Frame) -> None:
        frame = ttk.Frame(parent)
        frame.pack(fill=tk.X, pady=(10, 0))

        self.progress = ttk.Progressbar(frame, variable=self.progress_var, maximum=100)
        self.progress.pack(fill=tk.X, expand=True)

        ttk.Label(parent, textvariable=self.progress_text_var, anchor="w").pack(fill=tk.X, pady=(8, 0))
        ttk.Label(parent, textvariable=self.status_var, anchor="w").pack(fill=tk.X, pady=(4, 0))

    def _build_log_area(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Log")
        frame.pack(fill=tk.BOTH, expand=False, pady=(8, 0))

        self.log_text = tk.Text(frame, height=8, wrap="word")
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

    def _build_player_area(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Player")
        frame.pack(fill=tk.X, expand=False, pady=(8, 0))

        controls = ttk.Frame(frame)
        controls.pack(fill=tk.X, padx=8, pady=8)

        ttk.Button(controls, text="Play/Pause", command=self.toggle_play_pause).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="Next", command=self.play_next_track).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(controls, text="Stop", command=self.stop_preview).pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(controls, textvariable=self.player_state_var, width=8, anchor="w").pack(side=tk.LEFT, padx=(0, 8))
        ttk.Label(controls, textvariable=self.player_track_var, anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True)

    def _grid_label_entry(
        self,
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.StringVar,
        row: int,
        col: int,
        width: int = 16,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", padx=(8, 4), pady=4)
        ttk.Entry(parent, textvariable=variable, width=width).grid(row=row, column=col + 1, sticky="ew", padx=4, pady=4)

    # Queue / Log ----------------------------------------------------------

    def _poll_worker_queue(self) -> None:
        try:
            while True:
                kind, payload = self.worker_queue.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "progress":
                    self.progress_var.set(float(payload))
                elif kind == "progress_text":
                    self.progress_text_var.set(str(payload))
                elif kind == "start_preview":
                    temp_path, label = payload
                    self.start_preview(Path(str(temp_path)), str(label), is_temp=True)
                elif kind == "thumbnail":
                    tree_name, item_path, image_path, token = payload
                    self.apply_thumbnail(str(tree_name), str(item_path), str(image_path), int(token))
                elif kind == "refresh_remote":
                    self.refresh_remote()
                elif kind == "refresh_local":
                    self.refresh_local()
                elif kind == "error":
                    messagebox.showerror("Fehler", str(payload))
                    self.log("FEHLER: " + str(payload))
                elif kind == "done":
                    self.progress_var.set(0.0)
                    self.progress_text_var.set("Bereit.")
                    self.status_var.set(str(payload))
                    self.log(str(payload))
        except queue.Empty:
            pass

        self.after(150, self._poll_worker_queue)

    def log(self, message: str) -> None:
        self.log_text.insert(tk.END, message.rstrip() + "\n")
        self.log_text.see(tk.END)

    def qlog(self, message: str) -> None:
        self.worker_queue.put(("log", message))

    def qstatus(self, message: str) -> None:
        self.worker_queue.put(("status", message))

    def qprogress(self, value: float) -> None:
        self.worker_queue.put(("progress", value))

    def qprogress_text(self, message: str) -> None:
        self.worker_queue.put(("progress_text", message))

    def on_cover_toggle(self) -> None:
        self.save_settings()
        self.refresh_local()
        if self.sftp is not None:
            self.refresh_remote()

    # Connection -----------------------------------------------------------

    def choose_key(self) -> None:
        filename = filedialog.askopenfilename(title="SSH-Key waehlen", initialdir=str(Path.home() / ".ssh"))
        if filename:
            self.key_var.set(filename)
            self.save_settings()

    def connect_clicked(self) -> None:
        if paramiko is None:
            messagebox.showerror(
                "Paramiko fehlt",
                "Bitte zuerst installieren:\n\npython3 -m pip install -r requirements.txt",
            )
            return

        if self.transfer_thread and self.transfer_thread.is_alive():
            messagebox.showwarning("Transfer laeuft", "Bitte warte, bis der aktuelle Transfer fertig ist.")
            return

        config = self.get_connection_config()
        self.save_settings()
        threading.Thread(target=self._connect_worker, args=(config,), daemon=True).start()

    def _connect_worker(self, config: dict[str, str]) -> None:
        try:
            self.qstatus("Verbinde...")
            host = config["host"]
            user = config["user"]
            port = int(config["port"])

            if not host or not user:
                raise ValueError("Host und User duerfen nicht leer sein.")

            self.disconnect(log=False)

            client = self.connect_ssh_client(config)
            self.client = client
            self.sftp = client.open_sftp()

            self.qlog(f"Verbunden mit {host}:{port}.")
            self.qstatus("Verbunden.")
            self.worker_queue.put(("refresh_remote", None))

        except Exception as exc:
            self.worker_queue.put(("error", f"Verbindung fehlgeschlagen:\n{exc}\n\n{traceback.format_exc()}"))
            self.qstatus("Nicht verbunden.")

    def get_connection_config(self) -> dict[str, str]:
        return {
            "host": self.host_var.get().strip(),
            "port": self.port_var.get().strip() or "22",
            "user": self.user_var.get().strip(),
            "auth_mode": self.auth_mode_var.get(),
            "password": self.password_var.get(),
            "key_path": self.key_var.get().strip(),
            "key_passphrase": self.key_passphrase_var.get(),
        }

    def connect_ssh_client(self, config: dict[str, str]) -> "paramiko.SSHClient":
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_args: dict[str, object] = {
            "hostname": config["host"],
            "port": int(config["port"]),
            "username": config["user"],
            "timeout": 20,
            "banner_timeout": 20,
            "auth_timeout": 20,
        }

        if config["auth_mode"] == "password":
            password = config["password"]
            if not password:
                raise ValueError("Passwort fehlt.")
            connect_args["password"] = password
            connect_args["look_for_keys"] = False
            connect_args["allow_agent"] = False
        else:
            key_path = Path(os.path.expanduser(config["key_path"]))
            if not key_path.exists():
                raise FileNotFoundError(f"SSH-Key nicht gefunden: {key_path}")
            connect_args["key_filename"] = str(key_path)
            connect_args["passphrase"] = config["key_passphrase"] or None
            connect_args["look_for_keys"] = False
            connect_args["allow_agent"] = False

        client.connect(**connect_args)
        return client

    def register_worker_client(self, client: "paramiko.SSHClient") -> None:
        with self.worker_clients_lock:
            self.worker_clients.append(client)

    def unregister_worker_client(self, client: "paramiko.SSHClient") -> None:
        with self.worker_clients_lock:
            if client in self.worker_clients:
                self.worker_clients.remove(client)

    def close_worker_clients(self) -> None:
        with self.worker_clients_lock:
            clients = list(self.worker_clients)
            self.worker_clients.clear()

        for client in clients:
            try:
                client.close()
            except Exception:
                pass

    def disconnect(self, log: bool = True) -> None:
        self.stop_requested.set()
        self.close_worker_clients()

        try:
            if self.sftp:
                self.sftp.close()
        except Exception:
            pass

        try:
            if self.client:
                self.client.close()
        except Exception:
            pass

        self.sftp = None
        self.client = None

        if log:
            self.status_var.set("Getrennt.")
            self.log("Verbindung getrennt.")

    def require_sftp(self) -> "paramiko.SFTPClient":
        if self.sftp is None:
            raise RuntimeError("Nicht verbunden.")
        return self.sftp

    # Local browser --------------------------------------------------------

    def choose_local_path(self) -> None:
        directory = filedialog.askdirectory(title="Lokalen Ordner waehlen", initialdir=self.local_path_var.get())
        if directory:
            self.local_path_var.set(directory)
            self.refresh_local()
            self.save_settings()

    def go_local_path(self) -> None:
        self.refresh_local()
        self.save_settings()

    def local_up(self) -> None:
        path = Path(self.local_path_var.get()).expanduser()
        parent = path.parent
        if parent != path:
            self.local_path_var.set(str(parent))
            self.refresh_local()
            self.save_settings()

    def refresh_local(self) -> None:
        path = Path(self.local_path_var.get()).expanduser()
        try:
            if not path.exists() or not path.is_dir():
                raise NotADirectoryError(path)

            entries = []
            for child in path.iterdir():
                try:
                    entries.append(BrowserEntry(child.name, str(child), child.is_dir(), child.stat().st_size))
                except OSError:
                    continue

            entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))
            self.local_path_var.set(str(path))
            self.populate_tree(self.local_tree, entries)
            self.request_audio_thumbnails("local", entries)
        except Exception as exc:
            messagebox.showerror("Lokaler Pfad nicht lesbar", str(exc))

    def on_local_double_click(self, _event: tk.Event) -> None:
        selection = self.local_tree.selection()
        if not selection:
            return

        path = Path(selection[0])
        if path.is_dir():
            self.local_path_var.set(str(path))
            self.refresh_local()
        elif self.is_audio_file(path.name):
            self.prepare_playlist("local", str(path))
            self.start_preview(path, path.name, is_temp=False)
        else:
            self.upload_paths([str(path)])

    def show_local_context_menu(self, event: tk.Event) -> str:
        self.select_tree_item_at_event(self.local_tree, event)
        self.local_menu.tk_popup(event.x_root, event.y_root)
        self.local_menu.grab_release()
        return "break"

    def local_context_open(self) -> None:
        selection = self.local_tree.selection()
        if not selection:
            return
        path = Path(selection[0])
        if path.is_dir():
            self.local_path_var.set(str(path))
            self.refresh_local()
            self.save_settings()
        else:
            self.upload_paths([str(path)])

    def preview_local_selection(self) -> None:
        selection = list(self.local_tree.selection())
        if len(selection) != 1:
            messagebox.showinfo("Auswahl", "Bitte genau eine lokale Audiodatei auswaehlen.")
            return

        path = Path(selection[0])
        if path.is_dir() or not self.is_audio_file(path.name):
            messagebox.showinfo("Audio-Vorschau", "Bitte eine lokale Audiodatei auswaehlen.")
            return

        self.prepare_playlist("local", str(path))
        self.start_preview(path, path.name, is_temp=False)

    def start_drag(self, event: tk.Event, source: str) -> None:
        tree = self.local_tree if source == "local" else self.remote_tree
        item = tree.identify_row(event.y)
        if item:
            if item not in tree.selection():
                tree.selection_set(item)
            tree.focus(item)
            self.drag_source = source
        else:
            self.drag_source = None

    def finish_drag(self, event: tk.Event, target: str) -> None:
        if not self.drag_source or self.drag_source == target:
            self.drag_source = None
            return

        widget = self.winfo_containing(event.x_root, event.y_root)
        if target == "remote" and widget not in {self.remote_tree}:
            self.drag_source = None
            return
        if target == "local" and widget not in {self.local_tree}:
            self.drag_source = None
            return

        if self.drag_source == "local" and target == "remote":
            selection = list(self.local_tree.selection())
            if selection:
                self.upload_paths(selection)
        elif self.drag_source == "remote" and target == "local":
            selection = list(self.remote_tree.selection())
            if selection:
                self.download_paths(selection)

        self.drag_source = None

    def create_local_folder(self) -> None:
        folder_name = simpledialog.askstring("Neuer lokaler Ordner", "Ordnername:")
        folder_name = self.clean_entry_name(folder_name)
        if not folder_name:
            return

        target = Path(self.local_path_var.get()).expanduser() / folder_name
        try:
            target.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            messagebox.showerror("Ordner anlegen fehlgeschlagen", str(exc))
            return

        self.refresh_local()
        self.log(f"Lokal angelegt: {target}")

    def rename_local_selection(self) -> None:
        selection = list(self.local_tree.selection())
        if len(selection) != 1:
            messagebox.showinfo("Auswahl", "Bitte genau einen lokalen Eintrag zum Umbenennen auswaehlen.")
            return

        path = Path(selection[0])
        new_name = simpledialog.askstring("Lokal umbenennen", "Neuer Name:", initialvalue=path.name)
        new_name = self.clean_entry_name(new_name)
        if not new_name or new_name == path.name:
            return

        target = path.with_name(new_name)
        try:
            path.rename(target)
        except OSError as exc:
            messagebox.showerror("Umbenennen fehlgeschlagen", str(exc))
            return

        self.refresh_local()
        self.log(f"Lokal umbenannt: {path.name} -> {target.name}")

    # Remote browser -------------------------------------------------------

    def normalize_remote_path(self, path: str) -> str:
        path = path.strip().replace("\\", "/")
        if not path:
            return "/"
        if len(path) >= 2 and path[1] == ":":
            rest = path[2:]
            if not rest.startswith("/"):
                rest = "/" + rest
            return f"/{path[0].upper()}:{rest}"
        if not path.startswith("/"):
            return "/" + path
        return path

    def go_remote_path(self) -> None:
        self.remote_path_var.set(self.normalize_remote_path(self.remote_path_var.get()))
        self.refresh_remote()
        self.save_settings()

    def remote_up(self) -> None:
        path = self.normalize_remote_path(self.remote_path_var.get())
        if path == "/":
            return
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        self.remote_path_var.set(parent)
        self.refresh_remote()
        self.save_settings()

    def refresh_remote(self) -> None:
        if self.sftp is None:
            return

        path = self.normalize_remote_path(self.remote_path_var.get())
        try:
            entries = self.list_remote(path)
            self.remote_path_var.set(path)
            self.populate_tree(self.remote_tree, entries)
            self.request_audio_thumbnails("remote", entries)
            self.status_var.set(f"Remote: {path} | {len(entries)} Eintraege")
        except Exception as exc:
            messagebox.showerror("Remote-Pfad nicht lesbar", str(exc))
            self.log(f"Remote-Pfad nicht lesbar: {path} | {exc}")

    def list_remote(self, path: str) -> list[BrowserEntry]:
        sftp = self.require_sftp()
        entries = []

        for attr in sftp.listdir_attr(path):
            if attr.filename in (".", ".."):
                continue
            full_path = self.join_remote(path, attr.filename)
            entries.append(
                BrowserEntry(
                    name=attr.filename,
                    path=full_path,
                    is_dir=stat.S_ISDIR(attr.st_mode),
                    size=int(attr.st_size or 0),
                )
            )

        entries.sort(key=lambda entry: (not entry.is_dir, entry.name.lower()))
        return entries

    def on_remote_double_click(self, _event: tk.Event) -> None:
        selection = self.remote_tree.selection()
        if not selection:
            return

        path = selection[0]
        try:
            attr = self.require_sftp().stat(path)
            if stat.S_ISDIR(attr.st_mode):
                self.remote_path_var.set(path)
                self.refresh_remote()
            elif self.is_audio_file(path):
                self.prepare_playlist("remote", path)
                self.preview_remote_path(path)
            else:
                self.download_paths([path])
        except Exception as exc:
            messagebox.showerror("Fehler", str(exc))

    def show_remote_context_menu(self, event: tk.Event) -> str:
        self.select_tree_item_at_event(self.remote_tree, event)
        self.remote_menu.tk_popup(event.x_root, event.y_root)
        self.remote_menu.grab_release()
        return "break"

    def remote_context_open(self) -> None:
        selection = self.remote_tree.selection()
        if not selection:
            return
        path = selection[0]
        try:
            attr = self.require_sftp().stat(path)
            if stat.S_ISDIR(attr.st_mode):
                self.remote_path_var.set(path)
                self.refresh_remote()
                self.save_settings()
            else:
                self.download_paths([path])
        except Exception as exc:
            messagebox.showerror("Fehler", str(exc))

    def preview_remote_selection(self) -> None:
        if self.sftp is None:
            messagebox.showerror("Nicht verbunden", "Bitte zuerst verbinden.")
            return

        selection = list(self.remote_tree.selection())
        if len(selection) != 1:
            messagebox.showinfo("Auswahl", "Bitte genau eine Remote-Audiodatei auswaehlen.")
            return

        remote_path = selection[0]
        self.prepare_playlist("remote", remote_path)
        self.preview_remote_path(remote_path)

    def preview_remote_path(self, remote_path: str) -> None:
        if not self.is_audio_file(remote_path):
            messagebox.showinfo("Audio-Vorschau", "Bitte eine Remote-Audiodatei auswaehlen.")
            return

        config = self.get_connection_config()
        threading.Thread(target=self._preview_remote_worker, args=(remote_path, config), daemon=True).start()

    def _preview_remote_worker(self, remote_path: str, config: dict[str, str]) -> None:
        temp_client = None
        temp_sftp = None
        temp_path: Optional[Path] = None
        try:
            self.qstatus("Lade Audio-Vorschau...")
            suffix = Path(remote_path).suffix or ".tmp"
            with tempfile.NamedTemporaryFile(prefix="dj_sftp_preview_", suffix=suffix, delete=False) as temp_file:
                temp_path = Path(temp_file.name)
            temp_client = self.connect_ssh_client(config)
            self.register_worker_client(temp_client)
            temp_sftp = temp_client.open_sftp()
            temp_sftp.get(remote_path, str(temp_path))
            if self.closing:
                self.cleanup_preview_temp(temp_path)
                return
            self.worker_queue.put(("start_preview", (str(temp_path), posixpath.basename(remote_path))))
        except Exception as exc:
            if temp_path is not None:
                self.cleanup_preview_temp(temp_path)
            self.worker_queue.put(("error", f"Audio-Vorschau fehlgeschlagen:\n{exc}\n\n{traceback.format_exc()}"))
            self.qstatus("Audio-Vorschau fehlgeschlagen.")
        finally:
            try:
                if temp_sftp:
                    temp_sftp.close()
            except Exception:
                pass
            try:
                if temp_client:
                    self.unregister_worker_client(temp_client)
                    temp_client.close()
            except Exception:
                pass

    def start_preview(self, path: Path, label: str, is_temp: bool) -> None:
        self.stop_preview()

        if shutil.which("afplay") is None:
            messagebox.showerror("afplay fehlt", "Auf diesem Mac wurde kein 'afplay' gefunden.")
            if is_temp:
                self.cleanup_preview_temp(path)
            return

        try:
            self.preview_process = subprocess.Popen(["afplay", str(path)], start_new_session=True)
            self.preview_temp_path = path if is_temp else None
            self.preview_paused = False
            self.status_var.set(f"Vorschau: {label}")
            self.progress_text_var.set(f"Spielt: {label}")
            self.player_state_var.set("Play")
            self.player_track_var.set(label)
            self.log(f"Audio-Vorschau: {label}")
            self.after(300, self.poll_preview_process)
        except OSError as exc:
            if is_temp:
                self.cleanup_preview_temp(path)
            messagebox.showerror("Audio-Vorschau fehlgeschlagen", str(exc))

    def stop_preview(self) -> None:
        if self.preview_process and self.preview_process.poll() is None:
            try:
                os.killpg(self.preview_process.pid, signal.SIGTERM)
            except OSError:
                self.preview_process.terminate()
            try:
                self.preview_process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.preview_process.pid, signal.SIGKILL)
                except OSError:
                    self.preview_process.kill()
        self.preview_process = None
        self.preview_paused = False
        if self.preview_temp_path:
            self.cleanup_preview_temp(self.preview_temp_path)
            self.preview_temp_path = None
        self.player_state_var.set("Stop")

    def poll_preview_process(self) -> None:
        if self.preview_process is None:
            return
        if self.preview_process.poll() is None:
            self.after(300, self.poll_preview_process)
            return

        self.preview_process = None
        if self.preview_temp_path:
            self.cleanup_preview_temp(self.preview_temp_path)
            self.preview_temp_path = None
        self.progress_text_var.set("Bereit.")
        self.status_var.set("Audio-Vorschau beendet.")
        self.player_state_var.set("Stop")

    def toggle_play_pause(self) -> None:
        if self.preview_process and self.preview_process.poll() is None:
            if self.preview_paused:
                os.kill(self.preview_process.pid, signal.SIGCONT)
                self.preview_paused = False
                self.player_state_var.set("Play")
                self.progress_text_var.set(f"Spielt: {self.player_track_var.get()}")
            else:
                os.kill(self.preview_process.pid, signal.SIGSTOP)
                self.preview_paused = True
                self.player_state_var.set("Pause")
                self.progress_text_var.set(f"Pausiert: {self.player_track_var.get()}")
            return

        if self.player_playlist and 0 <= self.player_index < len(self.player_playlist):
            self.play_playlist_item(self.player_index)

    def play_next_track(self) -> None:
        if not self.player_playlist:
            return
        next_index = self.player_index + 1 if self.player_index >= 0 else 0
        if next_index >= len(self.player_playlist):
            next_index = 0
        self.play_playlist_item(next_index)

    def prepare_playlist(self, source: str, current_item: str) -> None:
        tree = self.local_tree if source == "local" else self.remote_tree
        entries = [item for item in tree.get_children() if self.is_audio_file(str(item))]
        if not entries:
            entries = [current_item]
        self.player_playlist = entries
        self.player_index = entries.index(current_item) if current_item in entries else 0
        self.player_source = source
        self.player_track_var.set(Path(current_item).name if source == "local" else posixpath.basename(current_item))

    def play_playlist_item(self, index: int) -> None:
        if not self.player_playlist or self.player_source is None:
            return
        if index < 0 or index >= len(self.player_playlist):
            return
        self.player_index = index
        item = self.player_playlist[index]
        if self.player_source == "local":
            self.start_preview(Path(item), Path(item).name, is_temp=False)
        else:
            self.preview_remote_path(item)

    @staticmethod
    def is_audio_file(path: str) -> bool:
        return Path(path).suffix.lower() in AUDIO_EXTENSIONS

    @staticmethod
    def cleanup_preview_temp(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    def create_remote_folder(self) -> None:
        if self.sftp is None:
            messagebox.showerror("Nicht verbunden", "Bitte zuerst verbinden.")
            return

        folder_name = simpledialog.askstring("Neuer Remote-Ordner", "Ordnername:")
        folder_name = self.clean_entry_name(folder_name)
        if not folder_name:
            return

        target = self.join_remote(self.normalize_remote_path(self.remote_path_var.get()), folder_name)
        try:
            self.require_sftp().mkdir(target)
        except OSError as exc:
            messagebox.showerror("Ordner anlegen fehlgeschlagen", str(exc))
            return

        self.refresh_remote()
        self.log(f"Remote angelegt: {target}")

    def rename_remote_selection(self) -> None:
        if self.sftp is None:
            messagebox.showerror("Nicht verbunden", "Bitte zuerst verbinden.")
            return

        selection = list(self.remote_tree.selection())
        if len(selection) != 1:
            messagebox.showinfo("Auswahl", "Bitte genau einen Remote-Eintrag zum Umbenennen auswaehlen.")
            return

        path = selection[0]
        current_name = posixpath.basename(path.rstrip("/")) or path
        new_name = simpledialog.askstring("Remote umbenennen", "Neuer Name:", initialvalue=current_name)
        new_name = self.clean_entry_name(new_name)
        if not new_name or new_name == current_name:
            return

        target = self.join_remote(posixpath.dirname(path.rstrip("/")) or "/", new_name)
        try:
            self.require_sftp().rename(path, target)
        except OSError as exc:
            messagebox.showerror("Umbenennen fehlgeschlagen", str(exc))
            return

        self.refresh_remote()
        self.log(f"Remote umbenannt: {current_name} -> {posixpath.basename(target)}")

    # Transfers ------------------------------------------------------------

    def download_selection(self) -> None:
        selection = list(self.remote_tree.selection())
        if not selection:
            messagebox.showinfo("Keine Auswahl", "Bitte remote Dateien oder Ordner auswaehlen.")
            return
        self.download_paths(selection)

    def upload_selection(self) -> None:
        selection = list(self.local_tree.selection())
        if not selection:
            messagebox.showinfo("Keine Auswahl", "Bitte lokale Dateien oder Ordner auswaehlen.")
            return
        self.upload_paths(selection)

    def download_paths(self, remote_paths: list[str]) -> None:
        local_base = Path(self.local_path_var.get()).expanduser()
        local_base.mkdir(parents=True, exist_ok=True)
        self.start_transfer("download", remote_paths, str(local_base))

    def upload_paths(self, local_paths: list[str]) -> None:
        remote_base = self.normalize_remote_path(self.remote_path_var.get())
        self.start_transfer("upload", local_paths, remote_base)

    def start_transfer(self, direction: TransferDirection, sources: list[str], target_base: str) -> None:
        if self.sftp is None:
            messagebox.showerror("Nicht verbunden", "Bitte zuerst verbinden.")
            return

        if self.transfer_thread and self.transfer_thread.is_alive():
            messagebox.showwarning("Transfer laeuft", "Es laeuft bereits ein Transfer.")
            return

        self.stop_requested.clear()
        self.progress_var.set(0.0)
        config = self.get_connection_config()
        self.transfer_thread = threading.Thread(
            target=self._transfer_worker,
            args=(direction, sources, target_base, config),
            daemon=True,
        )
        self.transfer_thread.start()

    def request_stop(self) -> None:
        self.stop_requested.set()
        self.log("Abbruch angefordert...")

    def _transfer_worker(
        self,
        direction: TransferDirection,
        sources: list[str],
        target_base: str,
        config: dict[str, str],
    ) -> None:
        client = None
        sftp = None
        try:
            client = self.connect_ssh_client(config)
            self.register_worker_client(client)
            sftp = client.open_sftp()

            if direction == "download":
                files = self.collect_download_files(sftp, sources, Path(target_base))
                self.copy_files(sftp, files, direction, self.copy_with_progress)
                self.worker_queue.put(("refresh_local", None))
                self.worker_queue.put(("done", "Download fertig."))
            else:
                files = self.collect_upload_files(sftp, sources, target_base)
                self.copy_files(sftp, files, direction, self.copy_with_progress)
                self.worker_queue.put(("refresh_remote", None))
                self.worker_queue.put(("done", "Upload fertig."))
        except Exception as exc:
            self.worker_queue.put(("error", f"Transfer fehlgeschlagen:\n{exc}\n\n{traceback.format_exc()}"))
            self.qstatus("Transfer fehlgeschlagen.")
        finally:
            try:
                if sftp:
                    sftp.close()
            except Exception:
                pass
            if client:
                self.unregister_worker_client(client)
                try:
                    client.close()
                except Exception:
                    pass

    def collect_download_files(
        self,
        sftp: "paramiko.SFTPClient",
        remote_paths: Iterable[str],
        local_base: Path,
    ) -> list[tuple[str, str]]:
        files: list[tuple[str, str]] = []

        for remote_path in remote_paths:
            remote_path = self.normalize_remote_path(remote_path)
            basename = posixpath.basename(remote_path.rstrip("/")) or "download"
            local_path = local_base / basename
            attr = sftp.stat(remote_path)
            if stat.S_ISDIR(attr.st_mode):
                self.walk_remote(sftp, remote_path, local_path, files)
            else:
                files.append((remote_path, str(local_path)))

        return files

    def walk_remote(
        self,
        sftp: "paramiko.SFTPClient",
        remote_dir: str,
        local_dir: Path,
        out: list[tuple[str, str]],
    ) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)

        for attr in sftp.listdir_attr(remote_dir):
            if self.stop_requested.is_set():
                return
            if attr.filename in (".", ".."):
                continue

            remote_path = posixpath.join(remote_dir.rstrip("/"), attr.filename)
            local_path = local_dir / attr.filename
            if stat.S_ISDIR(attr.st_mode):
                self.walk_remote(sftp, remote_path, local_path, out)
            else:
                out.append((remote_path, str(local_path)))

    def collect_upload_files(
        self,
        sftp: "paramiko.SFTPClient",
        local_paths: Iterable[str],
        remote_base: str,
    ) -> list[tuple[str, str]]:
        files: list[tuple[str, str]] = []

        for local_source in local_paths:
            path = Path(local_source)
            remote_target = self.join_remote(remote_base, path.name)
            if path.is_dir():
                self.ensure_remote_dir(sftp, remote_target)
                self.walk_local(sftp, path, remote_target, files)
            else:
                files.append((str(path), remote_target))

        return files

    def walk_local(
        self,
        sftp: "paramiko.SFTPClient",
        local_dir: Path,
        remote_dir: str,
        out: list[tuple[str, str]],
    ) -> None:
        for child in local_dir.iterdir():
            if self.stop_requested.is_set():
                return
            remote_path = self.join_remote(remote_dir, child.name)
            if child.is_dir():
                self.ensure_remote_dir(sftp, remote_path)
                self.walk_local(sftp, child, remote_path, out)
            else:
                out.append((str(child), remote_path))

    def copy_files(
        self,
        sftp: "paramiko.SFTPClient",
        files: list[tuple[str, str]],
        direction: TransferDirection,
        copy_func: Callable[["paramiko.SFTPClient", str, str, TransferDirection, int, int, int], None],
    ) -> None:
        total = max(len(files), 1)
        total_bytes = max(self.total_transfer_size(sftp, files, direction), 1)
        bytes_done = 0
        self.qlog(f"{len(files)} Dateien vorgemerkt.")
        self.qprogress_text(f"{direction.title()} vorbereitet: {len(files)} Dateien")

        for index, (source, target) in enumerate(files, start=1):
            if self.stop_requested.is_set():
                self.worker_queue.put(("done", "Transfer abgebrochen."))
                return

            if direction == "download":
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            else:
                self.ensure_remote_dir(sftp, posixpath.dirname(target))

            self.qstatus(f"{direction}: {index}/{total}")
            self.qlog(f"{direction}: {source} -> {target}")

            try:
                file_size = self.get_transfer_size(sftp, source, target, direction)
                copy_func(sftp, source, target, direction, index, total, bytes_done)
            except OSError as exc:
                self.qlog(f"FEHLER, ueberspringe: {source} | {exc}")
                continue

            bytes_done += file_size
            self.qprogress(bytes_done / total_bytes * 100.0)
            self.qprogress_text(f"{direction.title()}: {index}/{total} Dateien abgeschlossen")

    def copy_with_progress(
        self,
        sftp: "paramiko.SFTPClient",
        source: str,
        target: str,
        direction: TransferDirection,
        index: int,
        total: int,
        bytes_done_before: int,
    ) -> None:
        total_bytes = max(self.get_transfer_size(sftp, source, target, direction), 1)
        label = Path(target if direction == "download" else source).name

        def callback(transferred: int, _total: int) -> None:
            overall = bytes_done_before + transferred
            percent = min(100.0, overall / max(self.total_transfer_bytes_current, 1) * 100.0)
            current_percent = min(100, int(transferred / total_bytes * 100))
            self.qprogress(percent)
            self.qprogress_text(f"{direction.title()} {index}/{total}: {label} ({current_percent}%)")

        if direction == "download":
            sftp.get(source, target, callback=callback)
        else:
            sftp.put(source, target, callback=callback)

    def get_transfer_size(
        self,
        sftp: "paramiko.SFTPClient",
        source: str,
        target: str,
        direction: TransferDirection,
    ) -> int:
        if direction == "download":
            return int(sftp.stat(source).st_size or 0)
        return int(Path(source).stat().st_size)

    def total_transfer_size(
        self,
        sftp: "paramiko.SFTPClient",
        files: list[tuple[str, str]],
        direction: TransferDirection,
    ) -> int:
        size = 0
        for source, target in files:
            try:
                size += self.get_transfer_size(sftp, source, target, direction)
            except OSError:
                continue
        self.total_transfer_bytes_current = max(size, 1)
        return size

    def delete_local_selection(self) -> None:
        selection = list(self.local_tree.selection())
        if not selection:
            messagebox.showinfo("Keine Auswahl", "Bitte lokale Dateien oder Ordner auswaehlen.")
            return

        if not messagebox.askyesno("Lokal loeschen", self.build_delete_prompt(selection, local=True)):
            return

        deleted = 0
        for item in selection:
            path = Path(item)
            try:
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                deleted += 1
            except OSError as exc:
                messagebox.showerror("Loeschen fehlgeschlagen", f"{path}\n\n{exc}")
                return

        self.refresh_local()
        self.status_var.set(f"Lokal geloescht: {deleted} Eintraege")
        self.log(f"Lokal geloescht: {deleted} Eintraege")

    def delete_remote_selection(self) -> None:
        selection = list(self.remote_tree.selection())
        if not selection:
            messagebox.showinfo("Keine Auswahl", "Bitte remote Dateien oder Ordner auswaehlen.")
            return
        if self.sftp is None:
            messagebox.showerror("Nicht verbunden", "Bitte zuerst verbinden.")
            return
        if self.transfer_thread and self.transfer_thread.is_alive():
            messagebox.showwarning("Transfer laeuft", "Bitte erst den laufenden Transfer beenden.")
            return
        if not messagebox.askyesno("Remote loeschen", self.build_delete_prompt(selection, local=False)):
            return

        config = self.get_connection_config()
        self.transfer_thread = threading.Thread(
            target=self._delete_remote_worker,
            args=(selection, config),
            daemon=True,
        )
        self.transfer_thread.start()

    def build_delete_prompt(self, selection: list[str], local: bool) -> str:
        scope = "lokal" if local else "remote"
        if len(selection) == 1:
            name = Path(selection[0]).name or selection[0]
            return f"Soll '{name}' wirklich {scope} geloescht werden?"
        return f"Sollen {len(selection)} Eintraege wirklich {scope} geloescht werden?"

    def _delete_remote_worker(self, remote_paths: list[str], config: dict[str, str]) -> None:
        client = None
        sftp = None
        try:
            client = self.connect_ssh_client(config)
            self.register_worker_client(client)
            sftp = client.open_sftp()
            self.qstatus("Remote-Dateien werden geloescht...")
            self.qprogress_text("Loeschen gestartet...")
            for index, remote_path in enumerate(remote_paths, start=1):
                if self.stop_requested.is_set():
                    self.worker_queue.put(("done", "Remote loeschen abgebrochen."))
                    return
                self.remove_remote_path(sftp, remote_path)
                self.qprogress(index / max(len(remote_paths), 1) * 100.0)
                self.qprogress_text(f"Remote loeschen: {index}/{len(remote_paths)}")
                self.qlog(f"remote delete: {remote_path}")

            self.worker_queue.put(("refresh_remote", None))
            self.worker_queue.put(("done", "Remote loeschen fertig."))
        except Exception as exc:
            self.worker_queue.put(("error", f"Remote loeschen fehlgeschlagen:\n{exc}\n\n{traceback.format_exc()}"))
            self.qstatus("Remote loeschen fehlgeschlagen.")
        finally:
            try:
                if sftp:
                    sftp.close()
            except Exception:
                pass
            if client:
                self.unregister_worker_client(client)
                try:
                    client.close()
                except Exception:
                    pass

    def remove_remote_path(self, sftp: "paramiko.SFTPClient", remote_path: str) -> None:
        attr = sftp.stat(remote_path)
        if stat.S_ISDIR(attr.st_mode):
            for child in sftp.listdir_attr(remote_path):
                if self.stop_requested.is_set():
                    return
                if child.filename in (".", ".."):
                    continue
                self.remove_remote_path(sftp, self.join_remote(remote_path, child.filename))
            sftp.rmdir(remote_path)
        else:
            sftp.remove(remote_path)

    def ensure_remote_dir(self, sftp: "paramiko.SFTPClient", remote_dir: str) -> None:
        if not remote_dir or remote_dir == "/":
            return

        parts = [part for part in remote_dir.strip("/").split("/") if part]
        current = ""

        for part in parts:
            current = "/" + part if not current else posixpath.join(current, part)
            try:
                attr = sftp.stat(current)
                if not stat.S_ISDIR(attr.st_mode):
                    raise NotADirectoryError(current)
            except FileNotFoundError:
                sftp.mkdir(current)
            except OSError:
                try:
                    sftp.mkdir(current)
                except OSError:
                    pass

    # Helpers --------------------------------------------------------------

    @staticmethod
    def select_tree_item_at_event(tree: ttk.Treeview, event: tk.Event) -> None:
        item = tree.identify_row(event.y)
        if not item:
            return
        if item not in tree.selection():
            tree.selection_set(item)
        tree.focus(item)

    @staticmethod
    def clean_entry_name(name: Optional[str]) -> str:
        if name is None:
            return ""
        cleaned = name.strip()
        if cleaned in {"", ".", ".."} or "/" in cleaned or "\\" in cleaned:
            return ""
        return cleaned

    def populate_tree(self, tree: ttk.Treeview, entries: list[BrowserEntry]) -> None:
        if tree is self.local_tree:
            self.local_tree_images = {}
        elif tree is self.remote_tree:
            self.remote_tree_images = {}

        for item in tree.get_children():
            tree.delete(item)

        for entry in entries:
            tree.insert(
                "",
                tk.END,
                iid=entry.path,
                text=entry.name,
                values=("Ordner" if entry.is_dir else "Datei", "" if entry.is_dir else self.format_size(entry.size)),
            )

    def request_audio_thumbnails(self, tree_name: str, entries: list[BrowserEntry]) -> None:
        self.thumbnail_token += 1
        token = self.thumbnail_token

        if self.closing or not self.cover_enabled_var.get():
            return

        config = self.get_connection_config() if tree_name == "remote" else {}
        for entry in entries:
            if entry.is_dir or not self.is_audio_file(entry.name):
                continue
            if tree_name == "remote" and entry.size > REMOTE_THUMBNAIL_MAX_BYTES:
                continue
            self.thumbnail_executor.submit(self._thumbnail_worker, tree_name, entry, token, config)

    def _thumbnail_worker(
        self,
        tree_name: str,
        entry: BrowserEntry,
        token: int,
        config: dict[str, str],
    ) -> None:
        if self.closing or token != self.thumbnail_token:
            return

        try:
            image_path = self.generate_audio_thumbnail(tree_name, entry, config)
        except Exception:
            return

        if image_path and not self.closing and token == self.thumbnail_token:
            self.worker_queue.put(("thumbnail", (tree_name, entry.path, str(image_path), token)))

    def generate_audio_thumbnail(
        self,
        tree_name: str,
        entry: BrowserEntry,
        config: dict[str, str],
    ) -> Optional[Path]:
        AUDIO_THUMB_DIR.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha1(f"art-v2:{tree_name}:{entry.path}:{entry.size}".encode("utf-8")).hexdigest()
        cached = AUDIO_THUMB_DIR / f"{cache_key}.png"
        missing = AUDIO_THUMB_DIR / f"{cache_key}.missing"
        if cached.exists():
            return cached
        if missing.exists():
            return None

        if tree_name == "local":
            result = self.build_thumbnail_from_local_file(Path(entry.path), cached)
        else:
            result = self.build_thumbnail_from_remote_file(entry.path, cached, config)

        if result is None:
            missing.write_text("no-cover", encoding="utf-8")
        return result

    def build_thumbnail_from_local_file(self, source: Path, cached: Path) -> Optional[Path]:
        if self.extract_embedded_artwork_to_png(source, cached):
            return cached
        return None

    def build_thumbnail_from_remote_file(
        self,
        remote_path: str,
        cached: Path,
        config: dict[str, str],
    ) -> Optional[Path]:
        temp_client = None
        temp_sftp = None
        with tempfile.TemporaryDirectory(prefix="dj_sftp_thumb_remote_") as tmpdir:
            local_audio = Path(tmpdir) / Path(remote_path).name
            try:
                if self.closing:
                    return None
                temp_client = self.connect_ssh_client(config)
                self.register_worker_client(temp_client)
                temp_sftp = temp_client.open_sftp()
                temp_sftp.get(remote_path, str(local_audio))
                if self.closing:
                    return None
                return self.build_thumbnail_from_local_file(local_audio, cached)
            finally:
                try:
                    if temp_sftp:
                        temp_sftp.close()
                except Exception:
                    pass
                try:
                    if temp_client:
                        self.unregister_worker_client(temp_client)
                        temp_client.close()
                except Exception:
                    pass

    def extract_embedded_artwork_to_png(self, source: Path, cached: Path) -> bool:
        artwork = self.read_embedded_artwork(source)
        if artwork is None:
            return False

        image_bytes, suffix = artwork
        with tempfile.TemporaryDirectory(prefix="dj_sftp_art_") as tmpdir:
            image_path = Path(tmpdir) / f"cover{suffix}"
            image_path.write_bytes(image_bytes)

            if suffix.lower() == ".png":
                png_source = image_path
            else:
                png_source = Path(tmpdir) / "cover.png"
                if shutil.which("sips") is None:
                    return False
                result = subprocess.run(
                    ["sips", "-s", "format", "png", str(image_path), "--out", str(png_source)],
                    capture_output=True,
                    check=False,
                )
                if result.returncode != 0 or not png_source.exists():
                    return False

            if shutil.which("sips") is None:
                shutil.copyfile(png_source, cached)
                return True

            result = subprocess.run(
                [
                    "sips",
                    "-z",
                    str(AUDIO_THUMB_SIZE),
                    str(AUDIO_THUMB_SIZE),
                    str(png_source),
                    "--out",
                    str(cached),
                ],
                capture_output=True,
                check=False,
            )
            return result.returncode == 0 and cached.exists()

    def read_embedded_artwork(self, source: Path) -> Optional[tuple[bytes, str]]:
        if MutagenFile is None:
            return None

        try:
            audio = MutagenFile(str(source))
        except Exception:
            return None

        if audio is None:
            return None

        tags = getattr(audio, "tags", None)
        if tags is not None:
            try:
                apic_frames = tags.getall("APIC")  # type: ignore[attr-defined]
            except (AttributeError, ID3NoHeaderError):
                apic_frames = []
            if apic_frames:
                mime = getattr(apic_frames[0], "mime", "") or ""
                return bytes(apic_frames[0].data), self.image_suffix_from_mime(mime)

            covr = tags.get("covr") if hasattr(tags, "get") else None
            if covr:
                cover = covr[0]
                if MP4Cover is not None and isinstance(cover, MP4Cover):
                    imageformat = getattr(cover, "imageformat", None)
                    suffix = ".png" if imageformat == MP4Cover.FORMAT_PNG else ".jpg"
                else:
                    suffix = ".jpg"
                return bytes(cover), suffix

            flac_pictures = getattr(audio, "pictures", None)
            if flac_pictures:
                mime = getattr(flac_pictures[0], "mime", "") or ""
                return bytes(flac_pictures[0].data), self.image_suffix_from_mime(mime)

        if FLAC is not None:
            try:
                flac_audio = FLAC(str(source))
                if flac_audio.pictures:
                    mime = getattr(flac_audio.pictures[0], "mime", "") or ""
                    return bytes(flac_audio.pictures[0].data), self.image_suffix_from_mime(mime)
            except Exception:
                pass

        return None

    @staticmethod
    def image_suffix_from_mime(mime: str) -> str:
        mime = mime.lower()
        if "png" in mime:
            return ".png"
        if "gif" in mime:
            return ".gif"
        return ".jpg"

    def apply_thumbnail(self, tree_name: str, item_path: str, image_path: str, token: int) -> None:
        if token != self.thumbnail_token:
            return

        tree = self.local_tree if tree_name == "local" else self.remote_tree
        images = self.local_tree_images if tree_name == "local" else self.remote_tree_images

        if not tree.exists(item_path):
            return

        try:
            image = tk.PhotoImage(file=image_path)
        except tk.TclError:
            return

        images[item_path] = image
        tree.item(item_path, image=image)

    @staticmethod
    def join_remote(parent: str, name: str) -> str:
        if not parent or parent == "/":
            return "/" + name
        return posixpath.join(parent.rstrip("/"), name)

    @staticmethod
    def format_size(size: int) -> str:
        value = float(size)
        units = ["B", "KB", "MB", "GB", "TB"]

        for unit in units:
            if value < 1024.0 or unit == units[-1]:
                return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024.0

        return f"{size} B"

    def on_close(self) -> None:
        if self.transfer_thread and self.transfer_thread.is_alive():
            if not messagebox.askyesno(
                "Transfer laeuft",
                "Es laeuft noch ein Transfer oder Remote-Job. Trotzdem beenden?",
            ):
                return

        self.closing = True
        self.thumbnail_token += 1
        self.stop_requested.set()
        self.stop_preview()
        self.close_worker_clients()
        if self.transfer_thread and self.transfer_thread.is_alive():
            self.transfer_thread.join(timeout=2)
        self.thumbnail_executor.shutdown(wait=False, cancel_futures=True)
        self.save_settings()
        self.disconnect(log=False)
        self.destroy()

    # Persistence ---------------------------------------------------------

    def load_settings(self) -> dict[str, str]:
        try:
            if SETTINGS_PATH.exists():
                with SETTINGS_PATH.open("r", encoding="utf-8") as file:
                    data = json.load(file)
                if isinstance(data, dict):
                    return {str(key): str(value) for key, value in data.items()}
        except Exception:
            pass
        return {}

    def save_settings(self) -> None:
        APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)

        data = {
            "host": self.host_var.get().strip(),
            "port": self.port_var.get().strip() or "22",
            "user": self.user_var.get().strip(),
            "auth_mode": self.auth_mode_var.get().strip() or "key",
            "key_path": self.key_var.get().strip(),
            "local_path": self.local_path_var.get().strip(),
            "remote_path": self.normalize_remote_path(self.remote_path_var.get()),
            "cover_enabled": "1" if self.cover_enabled_var.get() else "0",
        }

        with SETTINGS_PATH.open("w", encoding="utf-8") as file:
            json.dump(data, file, indent=2, ensure_ascii=True)

        self.save_secret("password", self.password_var.get())
        self.save_secret("key_passphrase", self.key_passphrase_var.get())

    def load_secret(self, name: str) -> str:
        try:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-a",
                    name,
                    "-s",
                    KEYCHAIN_SERVICE,
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return ""

        return result.stdout.strip() if result.returncode == 0 else ""

    def save_secret(self, name: str, value: str) -> None:
        try:
            if value:
                subprocess.run(
                    [
                        "security",
                        "add-generic-password",
                        "-U",
                        "-a",
                        name,
                        "-s",
                        KEYCHAIN_SERVICE,
                        "-w",
                        value,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            else:
                subprocess.run(
                    [
                        "security",
                        "delete-generic-password",
                        "-a",
                        name,
                        "-s",
                        KEYCHAIN_SERVICE,
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
        except FileNotFoundError:
            pass


def main() -> None:
    app = SftpWizardApp()
    app.mainloop()


if __name__ == "__main__":
    main()
