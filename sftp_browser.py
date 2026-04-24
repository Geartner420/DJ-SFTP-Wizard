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
import posixpath
import queue
import stat
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Literal, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import paramiko
except ImportError:
    paramiko = None


TransferDirection = Literal["download", "upload"]


@dataclass(frozen=True)
class BrowserEntry:
    name: str
    path: str
    is_dir: bool
    size: int


class SftpWizardApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title("DJ SFTP Wizard")
        self.geometry("1180x760")
        self.minsize(980, 620)

        self.client: Optional["paramiko.SSHClient"] = None
        self.sftp: Optional["paramiko.SFTPClient"] = None
        self.transfer_thread: Optional[threading.Thread] = None
        self.stop_requested = threading.Event()
        self.worker_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()

        self.local_path_var = tk.StringVar(value=str(Path.home() / "Downloads"))
        self.remote_path_var = tk.StringVar(value="/")
        self.status_var = tk.StringVar(value="Nicht verbunden.")
        self.progress_var = tk.DoubleVar(value=0.0)

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

    def _build_connection_area(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Verbindung")
        frame.pack(fill=tk.X)

        self.host_var = tk.StringVar()
        self.port_var = tk.StringVar(value="22")
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.key_var = tk.StringVar(value=str(Path.home() / ".ssh" / "id_ed25519"))
        self.key_passphrase_var = tk.StringVar()
        self.auth_mode_var = tk.StringVar(value="key")

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

        columns = ("name", "type", "size")
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="extended")
        tree.heading("name", text="Name")
        tree.heading("type", text="Typ")
        tree.heading("size", text="Groesse")
        tree.column("name", width=360, anchor="w")
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

        ttk.Button(frame, text="Download <-", command=self.download_selection).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(frame, text="Upload ->", command=self.upload_selection).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(frame, text="Abbrechen", command=self.request_stop).pack(side=tk.LEFT, padx=(0, 16))

        self.progress = ttk.Progressbar(frame, variable=self.progress_var, maximum=100)
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8))

        ttk.Label(parent, textvariable=self.status_var, anchor="w").pack(fill=tk.X, pady=(8, 0))

    def _build_log_area(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Log")
        frame.pack(fill=tk.BOTH, expand=False, pady=(8, 0))

        self.log_text = tk.Text(frame, height=8, wrap="word")
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scroll = ttk.Scrollbar(frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

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
                elif kind == "refresh_remote":
                    self.refresh_remote()
                elif kind == "refresh_local":
                    self.refresh_local()
                elif kind == "error":
                    messagebox.showerror("Fehler", str(payload))
                    self.log("FEHLER: " + str(payload))
                elif kind == "done":
                    self.progress_var.set(0.0)
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

    # Connection -----------------------------------------------------------

    def choose_key(self) -> None:
        filename = filedialog.askopenfilename(title="SSH-Key waehlen", initialdir=str(Path.home() / ".ssh"))
        if filename:
            self.key_var.set(filename)

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

        config = {
            "host": self.host_var.get().strip(),
            "port": self.port_var.get().strip() or "22",
            "user": self.user_var.get().strip(),
            "auth_mode": self.auth_mode_var.get(),
            "password": self.password_var.get(),
            "key_path": self.key_var.get().strip(),
            "key_passphrase": self.key_passphrase_var.get(),
        }
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

            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_args: dict[str, object] = {
                "hostname": host,
                "port": port,
                "username": user,
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
            self.client = client
            self.sftp = client.open_sftp()

            self.qlog(f"Verbunden mit {host}:{port}.")
            self.qstatus("Verbunden.")
            self.worker_queue.put(("refresh_remote", None))

        except Exception as exc:
            self.worker_queue.put(("error", f"Verbindung fehlgeschlagen:\n{exc}\n\n{traceback.format_exc()}"))
            self.qstatus("Nicht verbunden.")

    def disconnect(self, log: bool = True) -> None:
        self.stop_requested.set()

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

    def go_local_path(self) -> None:
        self.refresh_local()

    def local_up(self) -> None:
        path = Path(self.local_path_var.get()).expanduser()
        parent = path.parent
        if parent != path:
            self.local_path_var.set(str(parent))
            self.refresh_local()

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
        else:
            self.upload_paths([str(path)])

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

    def remote_up(self) -> None:
        path = self.normalize_remote_path(self.remote_path_var.get())
        if path == "/":
            return
        parent = posixpath.dirname(path.rstrip("/")) or "/"
        self.remote_path_var.set(parent)
        self.refresh_remote()

    def refresh_remote(self) -> None:
        if self.sftp is None:
            return

        path = self.normalize_remote_path(self.remote_path_var.get())
        try:
            entries = self.list_remote(path)
            self.remote_path_var.set(path)
            self.populate_tree(self.remote_tree, entries)
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
            else:
                self.download_paths([path])
        except Exception as exc:
            messagebox.showerror("Fehler", str(exc))

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
        self.transfer_thread = threading.Thread(
            target=self._transfer_worker,
            args=(direction, sources, target_base),
            daemon=True,
        )
        self.transfer_thread.start()

    def request_stop(self) -> None:
        self.stop_requested.set()
        self.log("Abbruch angefordert...")

    def _transfer_worker(self, direction: TransferDirection, sources: list[str], target_base: str) -> None:
        try:
            if direction == "download":
                files = self.collect_download_files(sources, Path(target_base))
                self.copy_files(files, direction, self.require_sftp().get)
                self.worker_queue.put(("refresh_local", None))
                self.worker_queue.put(("done", "Download fertig."))
            else:
                files = self.collect_upload_files(sources, target_base)
                self.copy_files(files, direction, self.require_sftp().put)
                self.worker_queue.put(("refresh_remote", None))
                self.worker_queue.put(("done", "Upload fertig."))
        except Exception as exc:
            self.worker_queue.put(("error", f"Transfer fehlgeschlagen:\n{exc}\n\n{traceback.format_exc()}"))
            self.qstatus("Transfer fehlgeschlagen.")

    def collect_download_files(self, remote_paths: Iterable[str], local_base: Path) -> list[tuple[str, str]]:
        sftp = self.require_sftp()
        files: list[tuple[str, str]] = []

        for remote_path in remote_paths:
            remote_path = self.normalize_remote_path(remote_path)
            basename = posixpath.basename(remote_path.rstrip("/")) or "download"
            local_path = local_base / basename
            attr = sftp.stat(remote_path)
            if stat.S_ISDIR(attr.st_mode):
                self.walk_remote(remote_path, local_path, files)
            else:
                files.append((remote_path, str(local_path)))

        return files

    def walk_remote(self, remote_dir: str, local_dir: Path, out: list[tuple[str, str]]) -> None:
        sftp = self.require_sftp()
        local_dir.mkdir(parents=True, exist_ok=True)

        for attr in sftp.listdir_attr(remote_dir):
            if self.stop_requested.is_set():
                return
            if attr.filename in (".", ".."):
                continue

            remote_path = posixpath.join(remote_dir.rstrip("/"), attr.filename)
            local_path = local_dir / attr.filename
            if stat.S_ISDIR(attr.st_mode):
                self.walk_remote(remote_path, local_path, out)
            else:
                out.append((remote_path, str(local_path)))

    def collect_upload_files(self, local_paths: Iterable[str], remote_base: str) -> list[tuple[str, str]]:
        files: list[tuple[str, str]] = []

        for local_source in local_paths:
            path = Path(local_source)
            remote_target = self.join_remote(remote_base, path.name)
            if path.is_dir():
                self.ensure_remote_dir(remote_target)
                self.walk_local(path, remote_target, files)
            else:
                files.append((str(path), remote_target))

        return files

    def walk_local(self, local_dir: Path, remote_dir: str, out: list[tuple[str, str]]) -> None:
        for child in local_dir.iterdir():
            if self.stop_requested.is_set():
                return
            remote_path = self.join_remote(remote_dir, child.name)
            if child.is_dir():
                self.ensure_remote_dir(remote_path)
                self.walk_local(child, remote_path, out)
            else:
                out.append((str(child), remote_path))

    def copy_files(
        self,
        files: list[tuple[str, str]],
        direction: TransferDirection,
        copy_func: Callable[[str, str], None],
    ) -> None:
        total = max(len(files), 1)
        self.qlog(f"{len(files)} Dateien vorgemerkt.")

        for index, (source, target) in enumerate(files, start=1):
            if self.stop_requested.is_set():
                self.worker_queue.put(("done", "Transfer abgebrochen."))
                return

            if direction == "download":
                Path(target).parent.mkdir(parents=True, exist_ok=True)
            else:
                self.ensure_remote_dir(posixpath.dirname(target))

            self.qstatus(f"{direction}: {index}/{total}")
            self.qlog(f"{direction}: {source} -> {target}")

            try:
                copy_func(source, target)
            except OSError as exc:
                self.qlog(f"FEHLER, ueberspringe: {source} | {exc}")
                continue

            self.qprogress(index / total * 100.0)

    def ensure_remote_dir(self, remote_dir: str) -> None:
        if not remote_dir or remote_dir == "/":
            return

        sftp = self.require_sftp()
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

    def populate_tree(self, tree: ttk.Treeview, entries: list[BrowserEntry]) -> None:
        for item in tree.get_children():
            tree.delete(item)

        for entry in entries:
            tree.insert(
                "",
                tk.END,
                iid=entry.path,
                values=(
                    entry.name,
                    "Ordner" if entry.is_dir else "Datei",
                    "" if entry.is_dir else self.format_size(entry.size),
                ),
            )

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
        self.disconnect(log=False)
        self.destroy()


def main() -> None:
    app = SftpWizardApp()
    app.mainloop()


if __name__ == "__main__":
    main()
