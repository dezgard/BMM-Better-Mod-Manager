#!/usr/bin/env python3
"""Desktop UI for BMM.

This is a small Tkinter/ttk wrapper around the existing bmm.py core. The UI is
deliberately conservative: a mod table, a detail pane, action buttons, profiles,
and an output log.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import queue
import re
import sys
import tempfile
import threading
import traceback
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any, Callable

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
from tkinter import ttk

import bmm


APP_TITLE = "BMM - Better Mod Manager"
GITHUB_MODS_KEY = "github_repo_mods"
GITHUB_STATUS_KEY = "github_repo_status"
EXTERNAL_LINKS_KEY = "external_repo_links"
EXTERNAL_MOD_PREFIX = "external."
MOD_INDEX_DIR_NAME = "Mod_index"
MOD_INDEX_FILE_NAME = "bmm-index.json"
MOD_INDEX_BACKUP_FILE_NAME = "mod-index.backup.json"
GITHUB_REPOS_FILE_NAME = "github-repos.json"
DATA_MOD_ZIPS_FILE_NAME = "data-mod-zips.json"
COLOR_BG = "#202423"
COLOR_PANEL = "#2c302f"
COLOR_PANEL_2 = "#383d3a"
COLOR_FIELD = "#252b2a"
COLOR_BORDER = "#6a756f"
COLOR_TEXT = "#f0eadb"
COLOR_MUTED = "#c8bdab"
COLOR_CYAN = "#78f4ed"
COLOR_CYAN_DIM = "#2f6f6b"
COLOR_ORANGE = "#eba146"
COLOR_WARN = "#ffb15c"
COLOR_DANGER = "#ff6658"
COLOR_OK = "#9aea91"
COLOR_SELECT = "#347b76"
APP_MIN_WIDTH = 1260
APP_MIN_HEIGHT = 880
LEFT_PANE_MIN_WIDTH = 640
RIGHT_PANE_MIN_WIDTH = 380
RIGHT_STACK_MIN_HEIGHT = 600
LOG_PANE_MIN_HEIGHT = 80
LOG_PANE_HEIGHT = 96
DETAIL_PANEL_MIN_HEIGHT = 250
DETAIL_MIN_WRAP = 120
DETAIL_FULL_WRAP_PAD = 16
DETAIL_VALUE_WRAP_PAD = 150
PROFILE_PANEL_HEIGHT = 46
PROFILE_COMBO_WIDTH = 12
PROFILE_BUTTON_WIDTH = 12
TOP_ENTRY_WIDTH = 84
TOP_BUTTON_WIDTH = 14
TOP_WIDE_BUTTON_WIDTH = 16
PLUGIN_ROW_PREFIX = "plugin::"
DATA_ROW_PREFIX = "data::"
DATA_FOLDER_ROW_PREFIX = "data-folder::"
CORE_ROW_ID = "data::core"


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as tmp:
            json.dump(data, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def read_backup_source(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "invalid_json": True,
            "error": str(exc),
            "text": text,
        }


class BmmGui:
    def __init__(self, root: tk.Tk, data_dir: str | None = None, index: str | None = None, game_dir: str | None = None) -> None:
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1280x900")
        self.root.minsize(APP_MIN_WIDTH, APP_MIN_HEIGHT)

        self.rt = bmm.make_runtime(data_dir)
        self.index_override = index
        self.game_dir_override = game_dir
        self.config: dict[str, Any] = {}
        self.state: dict[str, Any] = {}
        self.index: dict[str, Any] = {}
        self.mods: list[dict[str, Any]] = []
        self.external_mods: list[dict[str, Any]] = []
        self.mods_by_id: dict[str, dict[str, Any]] = {}
        self.row_mod_ids: dict[str, str] = {}
        self.row_data_folders: dict[str, str] = {}
        self.active_tree: ttk.Treeview | None = None
        self.data_load_order_dirty = False
        self.mod_index_dir = self.application_dir() / MOD_INDEX_DIR_NAME
        self.runtime_index_path = self.mod_index_dir / MOD_INDEX_FILE_NAME
        self.mod_index_backup_path = self.mod_index_dir / MOD_INDEX_BACKUP_FILE_NAME
        self.github_repos_path = self.mod_index_dir / GITHUB_REPOS_FILE_NAME
        self.data_mod_zips_path = self.mod_index_dir / DATA_MOD_ZIPS_FILE_NAME
        self.startup_update_done = False
        self.busy = False
        self.queue: queue.Queue[tuple[str, Any]] = queue.Queue()

        self.search_var = tk.StringVar()
        self.game_dir_var = tk.StringVar()
        self.github_repo_var = tk.StringVar()
        self.data_zip_var = tk.StringVar()
        self.bepinex_status_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Ready")
        self.profile_var = tk.StringVar()

        self.action_buttons: list[ttk.Button] = []

        self._configure_style()
        self._build_ui()
        self.root.after(100, self._process_queue)
        self.reload_data()
        self.root.after(700, self.update_github_on_startup)

    def _configure_style(self) -> None:
        self.root.configure(background=COLOR_BG)
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=COLOR_BG, foreground=COLOR_TEXT, font=("Segoe UI", 9))
        style.configure("TFrame", background=COLOR_BG)
        style.configure("Panel.TFrame", background=COLOR_PANEL, relief="solid", borderwidth=1)
        style.configure("DetailBody.TFrame", background=COLOR_PANEL, relief="flat", borderwidth=0)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_TEXT)
        style.configure("Header.TLabel", background=COLOR_BG, foreground=COLOR_CYAN, font=("Segoe UI", 10, "bold"))
        style.configure("DetailKey.TLabel", background=COLOR_PANEL, foreground=COLOR_ORANGE, font=("Segoe UI", 9, "bold"))
        style.configure("DetailValue.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT)
        style.configure("DetailHeader.TLabel", background=COLOR_PANEL, foreground=COLOR_TEXT, font=("Segoe UI", 11, "bold"))
        style.configure("Status.TLabel", background=COLOR_PANEL_2, foreground=COLOR_MUTED, padding=(8, 4))
        style.configure("Warning.TLabel", background=COLOR_BG, foreground=COLOR_WARN, padding=(0, 2), font=("Segoe UI", 9, "bold"))
        style.configure("Ok.TLabel", background=COLOR_BG, foreground=COLOR_OK, padding=(0, 2), font=("Segoe UI", 9, "bold"))
        style.configure("TEntry", fieldbackground=COLOR_FIELD, background=COLOR_FIELD, foreground=COLOR_TEXT, insertcolor=COLOR_CYAN)
        style.configure("TCombobox", fieldbackground=COLOR_FIELD, background=COLOR_PANEL_2, foreground=COLOR_TEXT, arrowcolor=COLOR_CYAN)
        style.configure(
            "TButton",
            background=COLOR_PANEL_2,
            foreground=COLOR_TEXT,
            bordercolor=COLOR_BORDER,
            focusthickness=1,
            focuscolor=COLOR_CYAN_DIM,
            padding=(8, 5),
        )
        style.map(
            "TButton",
            background=[("active", COLOR_CYAN_DIM), ("pressed", COLOR_CYAN_DIM), ("disabled", "#303432")],
            foreground=[("active", COLOR_CYAN), ("pressed", COLOR_CYAN), ("disabled", "#898278")],
            bordercolor=[("active", COLOR_CYAN), ("pressed", COLOR_CYAN)],
        )
        style.configure("TLabelframe", background=COLOR_BG, foreground=COLOR_TEXT, bordercolor=COLOR_BORDER)
        style.configure("TLabelframe.Label", background=COLOR_BG, foreground=COLOR_ORANGE, font=("Segoe UI", 9, "bold"))
        style.configure("TPanedwindow", background=COLOR_BG)
        style.configure("Vertical.TScrollbar", background=COLOR_PANEL_2, troughcolor=COLOR_FIELD, arrowcolor=COLOR_CYAN)
        style.configure(
            "Treeview",
            background=COLOR_FIELD,
            foreground=COLOR_TEXT,
            fieldbackground=COLOR_FIELD,
            bordercolor=COLOR_BORDER,
            rowheight=25,
        )
        style.configure(
            "Treeview.Heading",
            background=COLOR_PANEL_2,
            foreground=COLOR_CYAN,
            bordercolor=COLOR_BORDER,
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", COLOR_SELECT)],
            foreground=[("selected", COLOR_TEXT)],
        )

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self.root, padding=(10, 8, 10, 6))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(4, weight=1)

        ttk.Label(toolbar, text="Game").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Entry(toolbar, textvariable=self.game_dir_var, width=TOP_ENTRY_WIDTH).grid(
            row=0, column=1, sticky="w", padx=(0, 6)
        )
        ttk.Button(toolbar, text="Browse", width=TOP_BUTTON_WIDTH, command=self.choose_game_dir).grid(
            row=0, column=2, sticky="ew", padx=(0, 6)
        )
        ttk.Button(toolbar, text="Reload", width=TOP_BUTTON_WIDTH, command=self.reload_data).grid(
            row=0, column=3, sticky="ew"
        )
        self.bepinex_status_label = ttk.Label(toolbar, textvariable=self.bepinex_status_var, style="Warning.TLabel")
        self.bepinex_status_label.grid(row=1, column=1, columnspan=4, sticky="w", pady=(2, 0))

        ttk.Label(toolbar, text="GitHub").grid(row=2, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(toolbar, textvariable=self.github_repo_var, width=TOP_ENTRY_WIDTH).grid(
            row=2, column=1, sticky="w", padx=(0, 6), pady=(6, 0)
        )
        add_repo = ttk.Button(toolbar, text="Add Repo", width=TOP_BUTTON_WIDTH, command=self.add_github_repo)
        add_repo.grid(row=2, column=2, sticky="ew", padx=(0, 6), pady=(6, 0))
        update_repo = ttk.Button(toolbar, text="Update GitHub", width=TOP_BUTTON_WIDTH, command=self.update_github_repos)
        update_repo.grid(row=2, column=3, sticky="ew", pady=(6, 0))
        self.action_buttons.extend([add_repo, update_repo])

        ttk.Label(toolbar, text="Data ZIP").grid(row=3, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
        ttk.Entry(toolbar, textvariable=self.data_zip_var, width=TOP_ENTRY_WIDTH).grid(
            row=3, column=1, sticky="w", padx=(0, 6), pady=(6, 0)
        )
        browse_zip = ttk.Button(toolbar, text="Browse ZIP", width=TOP_BUTTON_WIDTH, command=self.choose_data_zip)
        browse_zip.grid(row=3, column=2, sticky="ew", padx=(0, 6), pady=(6, 0))
        add_data_zip = ttk.Button(toolbar, text="Add Data Mod", width=TOP_WIDE_BUTTON_WIDTH, command=self.add_data_mod_zip)
        add_data_zip.grid(row=3, column=3, sticky="ew", pady=(6, 0))
        self.action_buttons.extend([browse_zip, add_data_zip])

        main_pane = tk.PanedWindow(
            self.root,
            orient=tk.VERTICAL,
            background=COLOR_BG,
            borderwidth=0,
            sashwidth=6,
            showhandle=False,
            opaqueresize=True,
        )
        self.main_pane = main_pane
        main_pane.grid(row=1, column=0, sticky="nsew")

        content = tk.PanedWindow(
            main_pane,
            orient=tk.HORIZONTAL,
            background=COLOR_BG,
            borderwidth=0,
            sashwidth=6,
            showhandle=False,
            opaqueresize=True,
        )
        self.content_pane = content
        main_pane.add(content, minsize=RIGHT_STACK_MIN_HEIGHT, stretch="always")

        left = ttk.Frame(content, padding=(10, 4, 6, 6))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(2, weight=1)
        left.rowconfigure(5, weight=1)
        content.add(left, minsize=LEFT_PANE_MIN_WIDTH, stretch="always")

        filter_bar = ttk.Frame(left)
        filter_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        filter_bar.columnconfigure(2, weight=1)
        ttk.Label(filter_bar, text="Search").grid(row=0, column=0, sticky="w", padx=(0, 6))
        search = ttk.Entry(filter_bar, textvariable=self.search_var, width=TOP_ENTRY_WIDTH)
        search.grid(row=0, column=1, sticky="w")
        self.search_var.trace_add("write", lambda *_: self.on_search_changed())

        ttk.Label(left, text="BepInEx / Plugin Mods", style="Header.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 4))
        plugin_frame = ttk.Frame(left)
        plugin_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        plugin_frame.columnconfigure(0, weight=1)
        plugin_frame.rowconfigure(0, weight=1)
        plugin_columns = ("status", "latest", "id", "name", "category")
        self.plugin_tree = self.create_mod_tree(plugin_frame, plugin_columns)
        self.plugin_tree.heading("status", text="Status")
        self.plugin_tree.heading("latest", text="Latest")
        self.plugin_tree.heading("id", text="ID")
        self.plugin_tree.heading("name", text="Name")
        self.plugin_tree.heading("category", text="Category")
        self.plugin_tree.column("status", width=150, anchor="w", stretch=False)
        self.plugin_tree.column("latest", width=80, anchor="w", stretch=False)
        self.plugin_tree.column("id", width=185, anchor="w", stretch=False)
        self.plugin_tree.column("name", width=260, anchor="w", stretch=True)
        self.plugin_tree.column("category", width=155, anchor="w", stretch=False)
        self.plugin_tree.grid(row=0, column=0, sticky="nsew")
        self.plugin_tree.bind("<<TreeviewSelect>>", lambda _event: self.on_tree_selected(self.plugin_tree))
        plugin_scroll = ttk.Scrollbar(plugin_frame, orient=tk.VERTICAL, command=self.plugin_tree.yview)
        plugin_scroll.grid(row=0, column=1, sticky="ns")
        self.plugin_tree.configure(yscrollcommand=plugin_scroll.set)

        ttk.Label(left, text="Data Mods / Load Order", style="Header.TLabel").grid(row=3, column=0, sticky="w", pady=(0, 4))
        data_frame = ttk.Frame(left)
        data_frame.grid(row=5, column=0, sticky="nsew")
        data_frame.columnconfigure(0, weight=1)
        data_frame.rowconfigure(0, weight=1)
        data_columns = ("load", "status", "latest", "folder", "name", "category")
        self.data_tree = self.create_mod_tree(data_frame, data_columns)
        self.data_tree.heading("load", text="Load #")
        self.data_tree.heading("status", text="Status")
        self.data_tree.heading("latest", text="Latest")
        self.data_tree.heading("folder", text="Folder")
        self.data_tree.heading("name", text="Name")
        self.data_tree.heading("category", text="Category")
        self.data_tree.column("load", width=70, anchor="center", stretch=False)
        self.data_tree.column("status", width=150, anchor="w", stretch=False)
        self.data_tree.column("latest", width=80, anchor="w", stretch=False)
        self.data_tree.column("folder", width=190, anchor="w", stretch=False)
        self.data_tree.column("name", width=250, anchor="w", stretch=True)
        self.data_tree.column("category", width=145, anchor="w", stretch=False)
        self.data_tree.grid(row=0, column=0, sticky="nsew")
        self.data_tree.bind("<<TreeviewSelect>>", lambda _event: self.on_tree_selected(self.data_tree))
        data_scroll = ttk.Scrollbar(data_frame, orient=tk.VERTICAL, command=self.data_tree.yview)
        data_scroll.grid(row=0, column=1, sticky="ns")
        self.data_tree.configure(yscrollcommand=data_scroll.set)

        load_controls = ttk.Frame(left)
        load_controls.grid(row=6, column=0, sticky="ew", pady=(6, 0))
        for col in range(4):
            load_controls.columnconfigure(col, weight=1)
        move_up = ttk.Button(load_controls, text="Move Up", command=lambda: self.move_data_load_order(-1))
        move_down = ttk.Button(load_controls, text="Move Down", command=lambda: self.move_data_load_order(1))
        save_order = ttk.Button(load_controls, text="Save Load Order", command=self.save_data_load_order)
        move_up.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        move_down.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        save_order.grid(row=0, column=2, columnspan=2, sticky="ew")
        self.action_buttons.extend([move_up, move_down, save_order])

        right = ttk.Frame(content, padding=(6, 4, 10, 6))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1, minsize=DETAIL_PANEL_MIN_HEIGHT)
        content.add(right, minsize=RIGHT_PANE_MIN_WIDTH, stretch="always")
        self.right_panel = right

        ttk.Label(right, text="Mod Details", style="Header.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        self.detail_panel = ttk.Frame(right, style="Panel.TFrame", padding=(8, 8), height=DETAIL_PANEL_MIN_HEIGHT)
        self.detail_panel.grid(row=1, column=0, sticky="nsew")
        self.detail_panel.grid_propagate(False)
        self.detail_panel.columnconfigure(0, weight=1)
        self.detail_panel.rowconfigure(0, weight=1)

        self.detail_canvas = tk.Canvas(
            self.detail_panel,
            background=COLOR_PANEL,
            borderwidth=0,
            highlightthickness=0,
            yscrollincrement=24,
        )
        self.detail_canvas.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(self.detail_panel, orient=tk.VERTICAL, command=self.detail_canvas.yview)
        detail_scroll.grid(row=0, column=1, sticky="ns")
        self.detail_canvas.configure(yscrollcommand=detail_scroll.set)

        self.detail_frame = ttk.Frame(self.detail_canvas, style="DetailBody.TFrame")
        self.detail_frame.columnconfigure(1, weight=1)
        self.detail_window = self.detail_canvas.create_window((0, 0), window=self.detail_frame, anchor="nw")
        self.detail_frame.bind("<Configure>", self._sync_detail_scrollregion)
        self.detail_canvas.bind("<Configure>", self._sync_detail_width)
        self.detail_widgets: list[tk.Widget] = []
        self.detail_wrap_widgets: list[tuple[tk.Widget, str]] = []

        ttk.Label(right, text="Actions", style="Header.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 6))
        actions = ttk.Frame(right, style="Panel.TFrame", padding=(8, 8))
        actions.grid(row=3, column=0, sticky="ew")
        self.actions_frame = actions
        for col in range(2):
            actions.columnconfigure(col, weight=1)
        self._add_action(actions, "Disable", self.disable_selected, 0, 0)
        self._add_action(actions, "Enable", self.enable_selected, 0, 1)
        self._add_action(actions, "Install", self.install_selected, 1, 0)
        self._add_action(actions, "Uninstall", self.uninstall_selected, 1, 1)
        self._add_action(actions, "Update", self.update_selected_or_all, 2, 0)
        self._add_action(actions, "Remove", self.remove_selected, 2, 1)
        self._add_action(actions, "Connect Repo", self.connect_repo_selected, 3, 0, columnspan=2)

        ttk.Label(right, text="Profiles", style="Header.TLabel").grid(row=4, column=0, sticky="w", pady=(8, 6))
        profiles = ttk.Frame(right, style="Panel.TFrame", padding=(8, 8), height=PROFILE_PANEL_HEIGHT)
        profiles.grid(row=5, column=0, sticky="ew")
        profiles.grid_propagate(False)
        self.profiles_frame = profiles
        profiles.columnconfigure(0, weight=0)
        profiles.columnconfigure(1, weight=1)
        profiles.columnconfigure(2, weight=0)
        profiles.columnconfigure(3, weight=0)
        profiles.rowconfigure(0, weight=1)
        self.profile_combo = ttk.Combobox(
            profiles,
            textvariable=self.profile_var,
            state="readonly",
            width=PROFILE_COMBO_WIDTH,
        )
        self.profile_combo.grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Button(
            profiles,
            text="Save Current",
            width=PROFILE_BUTTON_WIDTH,
            command=self.save_profile,
        ).grid(row=0, column=2, sticky="e", padx=(0, 6))
        ttk.Button(
            profiles,
            text="Apply",
            width=PROFILE_BUTTON_WIDTH,
            command=self.apply_profile,
        ).grid(row=0, column=3, sticky="e")

        log_frame = ttk.Frame(main_pane, padding=(10, 0, 10, 6))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        ttk.Label(log_frame, text="Output", style="Header.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.log_text = tk.Text(
            log_frame,
            height=9,
            wrap="word",
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=6,
            background=COLOR_FIELD,
            foreground=COLOR_TEXT,
            insertbackground=COLOR_CYAN,
            selectbackground=COLOR_SELECT,
            selectforeground=COLOR_TEXT,
            highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_CYAN,
        )
        self.log_text.grid(row=1, column=0, sticky="nsew")
        self.log_text.configure(state="disabled")
        main_pane.add(log_frame, minsize=LOG_PANE_MIN_HEIGHT, height=LOG_PANE_HEIGHT, stretch="never")

        status = ttk.Label(self.root, textvariable=self.status_var, style="Status.TLabel", anchor="w")
        status.grid(row=2, column=0, sticky="ew")

    def _add_action(
        self,
        parent: ttk.Frame,
        text: str,
        command: Callable[[], None],
        row: int,
        col: int,
        columnspan: int = 1,
    ) -> None:
        button = ttk.Button(parent, text=text, command=command)
        button.grid(row=row, column=col, columnspan=columnspan, sticky="ew", padx=3, pady=3)
        self.action_buttons.append(button)

    def create_mod_tree(self, parent: ttk.Frame, columns: tuple[str, ...]) -> ttk.Treeview:
        tree = ttk.Treeview(parent, columns=columns, show="headings", selectmode="browse")
        tree.tag_configure("installed", background="#18844f", foreground="#f0fff5")
        tree.tag_configure("disabled", background="#8b2822", foreground="#ffe2dc")
        tree.tag_configure("external", background="#5a4930", foreground="#fff0d2")
        tree.tag_configure("external_linked", background="#345b58", foreground="#effffc")
        tree.tag_configure("missing", background=COLOR_FIELD, foreground=COLOR_TEXT)
        tree.tag_configure("core", background=COLOR_PANEL_2, foreground=COLOR_CYAN)
        tree.tag_configure("unknown", background="#473a2a", foreground="#ffe9c4")
        return tree

    def on_tree_selected(self, tree: ttk.Treeview) -> None:
        if not tree.selection():
            return
        self.active_tree = tree
        other = self.data_tree if tree is self.plugin_tree else self.plugin_tree
        other.selection_remove(other.selection())
        self.show_selected_details()

    def _sync_detail_scrollregion(self, _event: tk.Event | None = None) -> None:
        self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all"))

    def _sync_detail_width(self, event: tk.Event) -> None:
        current_width = int(float(self.detail_canvas.itemcget(self.detail_window, "width") or 0))
        if current_width != event.width:
            self.detail_canvas.itemconfigure(self.detail_window, width=event.width)
        self._update_detail_wraps(event.width)

    def _detail_wraplength(self, role: str, width: int | None = None) -> int:
        canvas_width = width if width is not None else self.detail_canvas.winfo_width()
        if canvas_width <= 1:
            canvas_width = 560
        pad = DETAIL_VALUE_WRAP_PAD if role == "value" else DETAIL_FULL_WRAP_PAD
        return max(DETAIL_MIN_WRAP, canvas_width - pad)

    def _track_detail_wrap(self, widget: tk.Widget, role: str) -> None:
        self.detail_wrap_widgets.append((widget, role))
        widget.configure(wraplength=self._detail_wraplength(role))

    def _update_detail_wraps(self, width: int | None = None) -> None:
        for widget, role in self.detail_wrap_widgets:
            if widget.winfo_exists():
                widget.configure(wraplength=self._detail_wraplength(role, width))

    def make_args(self, **values: Any) -> argparse.Namespace:
        game_dir = self.game_dir_var.get().strip() or self.game_dir_override
        base = {
            "data_dir": str(self.rt.data_dir),
            "index": str(self.runtime_index_path),
            "game_dir": game_dir,
            "yes": True,
            "version": None,
            "mod_id": None,
            "target": None,
            "name": None,
            "disable_extra": False,
        }
        base.update(values)
        return argparse.Namespace(**base)

    def log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        if self.log_text.index("end-1c") != "1.0":
            self.log_text.insert("end", "\n")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_busy(self, busy: bool, message: str | None = None) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        for button in self.action_buttons:
            button.configure(state=state)
        if busy:
            self.status_var.set(message or "Working...")
        elif message:
            self.status_var.set(message)
        else:
            self.status_var.set("Ready")

    def _process_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self.log(str(payload))
                elif kind == "done":
                    if len(payload) == 2:
                        message, refresh = payload
                        after_refresh = None
                    else:
                        message, refresh, after_refresh = payload
                    self.set_busy(False, message)
                    if refresh:
                        self.reload_data(log_errors=False)
                    if after_refresh:
                        after_refresh()
                elif kind == "error":
                    self.set_busy(False, "Error")
                    self.log(str(payload))
                    messagebox.showerror(APP_TITLE, str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._process_queue)

    def run_task(
        self,
        label: str,
        func: Callable[[], Any],
        refresh: bool = True,
        after_refresh: Callable[[], None] | None = None,
    ) -> None:
        if self.busy:
            return
        self.set_busy(True, label)
        self.log(label)

        def worker() -> None:
            out = io.StringIO()
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    result = func()
                text = out.getvalue().strip()
                error_text = err.getvalue().strip()
                if text:
                    self.queue.put(("log", text))
                if error_text:
                    self.queue.put(("log", error_text))
                if isinstance(result, int) and result != 0:
                    self.queue.put(("done", (f"Finished with exit code {result}", refresh, after_refresh)))
                else:
                    self.queue.put(("done", ("Ready", refresh, after_refresh)))
            except Exception as exc:  # noqa: BLE001 - GUI boundary should report all failures.
                details = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                self.queue.put(("error", details))

        threading.Thread(target=worker, daemon=True).start()

    def application_dir(self) -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).resolve().parent
        return Path(__file__).resolve().parent

    def empty_index(self) -> dict[str, Any]:
        return {
            "schema": bmm.INDEX_SCHEMA,
            "game": {
                "id": "ostranauts",
                "name": "Ostranauts",
            },
            "updated_utc": bmm.stamp(),
            "mods": [],
        }

    def write_mod_index_json(self, path: Path, data: Any) -> None:
        self.mod_index_dir.mkdir(parents=True, exist_ok=True)
        target = path.resolve()
        mod_index_root = self.mod_index_dir.resolve()
        try:
            target.relative_to(mod_index_root)
        except ValueError as exc:
            raise bmm.BmmError(f"Refusing to write outside {self.mod_index_dir}: {path}") from exc

        backup: dict[str, Any]
        if self.mod_index_backup_path.exists():
            loaded = bmm.read_json(self.mod_index_backup_path, default={})
            backup = loaded if isinstance(loaded, dict) else {}
        else:
            backup = {}
        files = backup.get("files")
        if not isinstance(files, dict):
            files = {}
        if path.exists():
            files[path.name] = {
                "backed_up_utc": bmm.stamp(),
                "content": read_backup_source(path),
            }
        backup["schema"] = "bmm-mod-index-backup-v1"
        backup["updated_utc"] = bmm.stamp()
        backup["files"] = files
        write_json_atomic(self.mod_index_backup_path, backup)
        write_json_atomic(path, data)

    def load_tracked_repos(self) -> list[str]:
        raw = bmm.read_json(self.github_repos_path, default=[])
        if isinstance(raw, dict):
            raw = raw.get("repos", [])
        if not isinstance(raw, list):
            return []
        repos = []
        seen = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            repo = item.strip()
            if repo and repo not in seen:
                repos.append(repo)
                seen.add(repo)
        return repos

    def save_tracked_repos(self, repos: list[str]) -> None:
        clean = []
        seen = set()
        for repo in repos:
            if not isinstance(repo, str):
                continue
            value = repo.strip()
            if value and value not in seen:
                clean.append(value)
                seen.add(value)
        self.mod_index_dir.mkdir(parents=True, exist_ok=True)
        self.write_mod_index_json(self.github_repos_path, clean)

    def load_tracked_data_zips(self) -> list[str]:
        raw = bmm.read_json(self.data_mod_zips_path, default=[])
        if isinstance(raw, dict):
            raw = raw.get("zips", [])
        if not isinstance(raw, list):
            return []
        paths = []
        seen = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            value = item.strip()
            if value and value.lower() not in seen:
                paths.append(value)
                seen.add(value.lower())
        return paths

    def save_tracked_data_zips(self, paths: list[str]) -> None:
        clean = []
        seen = set()
        for path in paths:
            if not isinstance(path, str):
                continue
            value = path.strip()
            if value and value.lower() not in seen:
                clean.append(value)
                seen.add(value.lower())
        self.mod_index_dir.mkdir(parents=True, exist_ok=True)
        self.write_mod_index_json(self.data_mod_zips_path, clean)

    def data_zip_mod_id(self, zip_path: Path, folder: str) -> str:
        stem = folder or zip_path.stem
        safe = re.sub(r"[^a-z0-9_.-]+", "-", stem.lower()).strip(".-")
        return "localdata." + (safe or "data-mod")

    def data_zip_mod_entry(self, path_value: str) -> dict[str, Any] | None:
        zip_path = Path(path_value).expanduser()
        if not zip_path.exists() or not zip_path.is_file():
            return None
        try:
            data_mod = bmm.detect_data_mod_archive(zip_path)
            if not data_mod:
                return None
        except bmm.BmmError:
            return None
        metadata = data_mod.get("metadata", {}) if isinstance(data_mod, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        folder = str(data_mod.get("folder") or zip_path.stem)
        version = str(metadata.get("strModVersion") or "local").strip() or "local"
        author = str(metadata.get("strAuthor") or "unknown").strip() or "unknown"
        name = str(metadata.get("strName") or folder).strip() or folder
        summary = bmm.mod_info_to_summary(metadata)
        return {
            "id": self.data_zip_mod_id(zip_path, folder),
            "type": "data",
            "name": name,
            "summary": summary,
            "authors": [author],
            "categories": ["data", "local"],
            "website": str(metadata.get("strModURL") or ""),
            "notes": [
                f"Local data ZIP: {zip_path}",
                f"Data mod folder: {folder}",
                f"Target game version: {metadata.get('strGameVersion') or 'unknown'}",
            ],
            "relationships": {
                "depends": [],
                "recommends": [],
                "suggests": [],
                "conflicts": [],
                "provides": [self.data_zip_mod_id(zip_path, folder)],
            },
            "data_mod_folder": folder,
            "versions": [
                {
                    "version": version,
                    "download": {
                        "type": "local",
                        "path": str(zip_path),
                        "source_label": str(zip_path),
                    },
                    "game_versions": [str(metadata.get("strGameVersion") or "")],
                    "relationships": {
                        "depends": [],
                        "recommends": [],
                        "suggests": [],
                        "conflicts": [],
                    },
                }
            ],
        }

    def build_runtime_index(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime_index = self.empty_index()
        github_mods = self.state.get(GITHUB_MODS_KEY, {}) if isinstance(self.state, dict) else {}
        if state is not None:
            github_mods = state.get(GITHUB_MODS_KEY, {}) if isinstance(state, dict) else {}
        merged_mods = []
        seen_ids: set[str] = set()
        for repo in self.load_tracked_repos():
            stored = github_mods.get(repo) if isinstance(github_mods, dict) else None
            if isinstance(stored, dict):
                repo_mods = [stored]
            elif isinstance(stored, list):
                repo_mods = [item for item in stored if isinstance(item, dict)]
            else:
                repo_mods = []
            for mod in repo_mods:
                mod_id = str(mod.get("id", ""))
                if not mod_id or mod_id in seen_ids:
                    continue
                merged_mods.append(mod)
                seen_ids.add(mod_id)
        for path_value in self.load_tracked_data_zips():
            mod = self.data_zip_mod_entry(path_value)
            if not isinstance(mod, dict):
                continue
            mod_id = str(mod.get("id", ""))
            if not mod_id or mod_id in seen_ids:
                continue
            merged_mods.append(mod)
            seen_ids.add(mod_id)
        runtime_index["mods"] = merged_mods
        return runtime_index

    def save_runtime_index(self) -> None:
        self.runtime_index_path.parent.mkdir(parents=True, exist_ok=True)
        self.write_mod_index_json(self.runtime_index_path, self.index)

    def write_mod_index_from_state(self, state: dict[str, Any]) -> None:
        index = self.build_runtime_index(state)
        self.mod_index_dir.mkdir(parents=True, exist_ok=True)
        self.write_mod_index_json(self.runtime_index_path, index)

    def ensure_mod_index_file(self) -> None:
        self.mod_index_dir.mkdir(parents=True, exist_ok=True)
        if not self.github_repos_path.exists():
            self.save_tracked_repos([])
        if not self.data_mod_zips_path.exists():
            self.save_tracked_data_zips([])
        if not self.runtime_index_path.exists():
            self.index = self.empty_index()
            self.save_runtime_index()

    def check_bepinex_status(self, game_dir: str | Path | None = None) -> tuple[bool, str]:
        raw = str(game_dir or self.game_dir_var.get() or "").strip()
        if not raw:
            return False, "Select the Ostranauts game folder."
        root = Path(raw).expanduser()
        if not root.exists():
            return False, f"Game folder not found: {root}"
        bepinex = root / "BepInEx"
        plugins = bepinex / "plugins"
        if not bepinex.exists():
            return False, "BepInEx is not installed for this game folder."
        if not plugins.exists():
            return False, "BepInEx is present, but BepInEx\\plugins is missing."
        warnings = bmm.loading_order_warnings(root)
        if warnings:
            return False, "BepInEx detected. " + warnings[0]
        load_order = bmm.display_game_path(root, bmm.loading_order_path(root))
        return True, f"BepInEx detected. Data load order: {load_order}"

    def update_bepinex_status(self) -> bool:
        ok, message = self.check_bepinex_status()
        self.bepinex_status_var.set(message)
        self.bepinex_status_label.configure(style="Ok.TLabel" if ok else "Warning.TLabel")
        return ok

    def current_game_dir_path(self) -> Path | None:
        raw = str(self.game_dir_override or self.game_dir_var.get() or self.config.get("game_dir") or "").strip()
        if not raw:
            return None
        return Path(raw).expanduser()

    def record_data_folder(self, record: dict[str, Any]) -> str:
        data_folder = str(record.get("data_mod_folder") or "").strip()
        if data_folder:
            return data_folder
        for file_record in record.get("files", []):
            if not isinstance(file_record, dict) or file_record.get("root") != bmm.DATA_MOD_ROOT:
                continue
            rel = str(file_record.get("path") or "").replace("\\", "/").strip("/")
            if rel:
                return rel.split("/", 1)[0]
        return ""

    def record_data_load_active(self, record: dict[str, Any]) -> bool | None:
        data_folder = self.record_data_folder(record)
        if not data_folder:
            return None
        game_dir = self.current_game_dir_path()
        if not game_dir or not game_dir.exists():
            return None
        return bmm.data_mod_enabled(game_dir, data_folder)

    def external_base_path(self, relative: str) -> str:
        normalized = relative.replace("\\", "/").strip("/")
        lower = normalized.lower()
        marker = ".dll.disabled"
        marker_index = lower.find(marker)
        if marker_index >= 0:
            after_marker = lower[marker_index + len(marker):]
            if not after_marker or after_marker.startswith("-"):
                return normalized[: marker_index + 4]
        return normalized

    def external_mod_id_for_path(self, relative: str) -> str:
        base = self.external_base_path(relative).lower()
        safe = re.sub(r"[^a-z0-9_.-]+", "-", base.replace("/", ".")).strip(".-")
        return EXTERNAL_MOD_PREFIX + (safe or "plugin")

    def is_external_mod_id(self, mod_id: str | None) -> bool:
        return isinstance(mod_id, str) and mod_id.startswith(EXTERNAL_MOD_PREFIX)

    def is_external_mod(self, mod: dict[str, Any] | None) -> bool:
        return isinstance(mod, dict) and bool(mod.get("_external"))

    def external_display_name(self, relative: str) -> str:
        name = Path(self.external_base_path(relative)).name
        lower = name.lower()
        if lower.endswith(".dll"):
            return name[:-4]
        return Path(relative).stem or relative

    def managed_plugin_paths(self) -> set[str]:
        managed: set[str] = set()
        installed = self.state.get("installed", {}) if isinstance(self.state, dict) else {}
        if not isinstance(installed, dict):
            return managed
        for record in installed.values():
            if not isinstance(record, dict):
                continue
            for file_record in record.get("files", []):
                if not isinstance(file_record, dict) or file_record.get("root") != "bepinex_plugins":
                    continue
                for key in ("path", "disabled_path"):
                    value = file_record.get(key)
                    if isinstance(value, str) and value.strip():
                        managed.add(value.replace("\\", "/").strip("/").lower())
        return managed

    def managed_data_mod_folders(self) -> set[str]:
        managed: set[str] = set()
        installed = self.state.get("installed", {}) if isinstance(self.state, dict) else {}
        if not isinstance(installed, dict):
            return managed
        for record in installed.values():
            if not isinstance(record, dict):
                continue
            folder = str(record.get("data_mod_folder") or "").strip()
            if folder:
                managed.add(folder.lower())
            for file_record in record.get("files", []):
                if not isinstance(file_record, dict) or file_record.get("root") != bmm.DATA_MOD_ROOT:
                    continue
                rel = str(file_record.get("path") or "").replace("\\", "/").strip("/")
                if rel:
                    managed.add(rel.split("/", 1)[0].lower())
        return managed

    def external_data_mod_id_for_folder(self, folder: str) -> str:
        safe = re.sub(r"[^a-z0-9_.-]+", "-", folder.lower()).strip(".-")
        return EXTERNAL_MOD_PREFIX + "data." + (safe or "mod")

    def read_data_mod_info(self, folder: Path) -> dict[str, Any]:
        raw = bmm.read_json(folder / "mod_info.json", default=[])
        if isinstance(raw, list) and raw and isinstance(raw[0], dict):
            return raw[0]
        if isinstance(raw, dict):
            return raw
        return {}

    def scan_external_data_mods(self, game_dir: Path, seen_ids: set[str]) -> list[dict[str, Any]]:
        mods_root = bmm.data_mods_dir(game_dir)
        if not mods_root.exists() or not mods_root.is_dir():
            return []
        managed = self.managed_data_mod_folders()
        links = self.state.get(EXTERNAL_LINKS_KEY, {}) if isinstance(self.state, dict) else {}
        if not isinstance(links, dict):
            links = {}
        try:
            _load_path, load_order = bmm.load_loading_order(game_dir)
            enabled_names = set(load_order[0].get("aLoadOrder", [])) if isinstance(load_order[0].get("aLoadOrder"), list) else set()
        except bmm.BmmError:
            enabled_names = set()

        result: list[dict[str, Any]] = []
        for folder in sorted((item for item in mods_root.iterdir() if item.is_dir()), key=lambda item: item.name.lower()):
            if folder.name.lower() in managed:
                continue
            if not (folder / "mod_info.json").exists():
                continue
            try:
                info = self.read_data_mod_info(folder)
            except bmm.BmmError as exc:
                info = {
                    "strName": folder.name,
                    "strNotes": f"Invalid mod_info.json: {exc}",
                }
            mod_id = self.external_data_mod_id_for_folder(folder.name)
            if mod_id in seen_ids:
                mod_id = f"{mod_id}.{len(seen_ids) + 1}"
            seen_ids.add(mod_id)
            link = links.get(mod_id)
            if not isinstance(link, dict):
                link = {}
            repo = str(link.get("repo") or "").strip()
            release = str(link.get("release") or "").strip()
            website = str(link.get("website") or info.get("strModURL") or (f"https://github.com/{repo}" if repo else ""))
            summary = str(info.get("strNotes") or "Existing Ostranauts data mod not installed by BMM.")
            result.append(
                {
                    "id": mod_id,
                    "name": str(info.get("strName") or folder.name),
                    "summary": summary,
                    "authors": [str(info.get("strAuthor") or "")],
                    "categories": ["external", "data", "not BMM"],
                    "website": website,
                    "notes": ["BMM is only listing this data mod. It will not modify it unless an explicit adopt flow is added later."],
                    "_external": True,
                    "external": {
                        "kind": "data",
                        "root": bmm.DATA_MOD_ROOT,
                        "path": folder.name,
                        "enabled": folder.name in enabled_names,
                        "repo": repo,
                        "release": release,
                        "asset": str(link.get("asset") or ""),
                        "updated_at": str(link.get("updated_at") or ""),
                        "message": str(link.get("message") or ""),
                        "author": str(info.get("strAuthor") or ""),
                        "game_version": str(info.get("strGameVersion") or ""),
                        "mod_version": str(info.get("strModVersion") or ""),
                    },
                    "versions": [],
                }
            )
        return result

    def scan_external_mods(self) -> list[dict[str, Any]]:
        try:
            config = bmm.load_config(self.rt)
            game_dir = bmm.game_dir_from_config(config, self.game_dir_var.get().strip() or self.game_dir_override)
            plugins = game_dir / "BepInEx" / "plugins"
        except Exception:
            return []

        managed = self.managed_plugin_paths()
        links = self.state.get(EXTERNAL_LINKS_KEY, {}) if isinstance(self.state, dict) else {}
        if not isinstance(links, dict):
            links = {}

        external_mods: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        if plugins.exists() and plugins.is_dir():
            for path in sorted(plugins.rglob("*"), key=lambda item: str(item).lower()):
                if not path.is_file():
                    continue
                name_lower = path.name.lower()
                if not (name_lower.endswith(".dll") or ".dll.disabled" in name_lower):
                    continue
                rel = path.relative_to(plugins).as_posix()
                if rel.lower() in managed:
                    continue

                mod_id = self.external_mod_id_for_path(rel)
                if mod_id in seen_ids:
                    mod_id = f"{mod_id}.{len(seen_ids) + 1}"
                seen_ids.add(mod_id)

                link = links.get(mod_id)
                if not isinstance(link, dict):
                    link = {}
                repo = str(link.get("repo") or "").strip()
                release = str(link.get("release") or "").strip()
                website = str(link.get("website") or (f"https://github.com/{repo}" if repo else ""))
                enabled = name_lower.endswith(".dll")
                external_mods.append(
                    {
                        "id": mod_id,
                        "name": self.external_display_name(rel),
                        "summary": "Existing BepInEx plugin not installed by BMM.",
                        "categories": ["external", "bepinex", "not BMM"],
                        "website": website,
                        "notes": ["BMM is only listing this file. It will not modify it unless an explicit adopt flow is added later."],
                        "_external": True,
                        "external": {
                            "kind": "bepinex",
                            "root": "bepinex_plugins",
                            "path": rel,
                            "enabled": enabled,
                            "repo": repo,
                            "release": release,
                            "asset": str(link.get("asset") or ""),
                            "updated_at": str(link.get("updated_at") or ""),
                            "message": str(link.get("message") or ""),
                        },
                        "versions": [],
                    }
                )
        external_mods.extend(self.scan_external_data_mods(game_dir, seen_ids))
        return external_mods

    def reload_data(self, log_errors: bool = True) -> None:
        try:
            self.config = bmm.load_config(self.rt)
            self.state = bmm.load_state(self.rt)
            game_dir = self.game_dir_override or str(self.config.get("game_dir") or "").strip()
            self.game_dir_var.set(game_dir)
            self.update_bepinex_status()
            self.ensure_mod_index_file()
            self.index = self.build_runtime_index()
            self.save_runtime_index()
            indexed_mods = bmm.get_mods(self.index)
            self.external_mods = self.scan_external_mods()
            self.mods = indexed_mods + self.external_mods
            self.mods_by_id = {str(mod.get("id")): mod for mod in self.mods}
            self.populate_mod_table()
            self.populate_profiles()
            suffix = f", {len(self.external_mods)} external" if self.external_mods else ""
            self.status_var.set(f"Loaded {len(indexed_mods)} BMM mods{suffix}")
        except Exception as exc:  # noqa: BLE001 - show load errors in the UI.
            if log_errors:
                self.log(f"Load failed: {exc}")
                messagebox.showerror(APP_TITLE, str(exc))

    def on_search_changed(self) -> None:
        if self.data_load_order_dirty:
            self.data_load_order_dirty = False
            self.status_var.set("Unsaved load-order edits were discarded by the search filter.")
        self.populate_mod_table()

    def mod_matches_filter(self, mod: dict[str, Any], filter_text: str, extra: str = "") -> bool:
        if not filter_text:
            return True
        mod_id = str(mod.get("id", ""))
        haystack = " ".join(
            [
                mod_id,
                str(mod.get("name", "")),
                str(mod.get("summary", "")),
                extra,
                " ".join(str(c) for c in mod.get("categories", []) if isinstance(c, str)),
            ]
        ).lower()
        return filter_text in haystack

    def mod_type_value(self, mod: dict[str, Any], record: dict[str, Any] | None = None) -> str:
        if isinstance(record, dict) and record.get("type"):
            return str(record.get("type") or "").lower()
        return str(mod.get("type") or "").lower()

    def mod_data_folder(self, mod: dict[str, Any], record: dict[str, Any] | None = None) -> str:
        if self.is_external_mod(mod):
            external = mod.get("external") if isinstance(mod.get("external"), dict) else {}
            if str(external.get("kind") or "") == "data":
                return str(external.get("path") or "").strip()
        if isinstance(record, dict):
            data_folder = self.record_data_folder(record)
            if data_folder:
                return data_folder
        return str(mod.get("data_mod_folder") or "").strip()

    def mod_has_plugin_side(self, mod: dict[str, Any], record: dict[str, Any] | None = None) -> bool:
        if self.is_external_mod(mod):
            external = mod.get("external") if isinstance(mod.get("external"), dict) else {}
            return str(external.get("kind") or "bepinex") == "bepinex"
        type_value = self.mod_type_value(mod, record)
        if type_value in ("bepinex", "plugin", "hybrid"):
            return True
        if type_value in ("data", "data_mod"):
            return False
        if isinstance(record, dict):
            for file_record in record.get("files", []):
                if isinstance(file_record, dict) and str(file_record.get("root") or "bepinex_plugins") != bmm.DATA_MOD_ROOT:
                    return True
        plugin = mod.get("plugin", {}) if isinstance(mod.get("plugin"), dict) else {}
        return bool(plugin.get("dll") or plugin.get("guid"))

    def mod_has_data_side(self, mod: dict[str, Any], record: dict[str, Any] | None = None) -> bool:
        if self.is_external_mod(mod):
            external = mod.get("external") if isinstance(mod.get("external"), dict) else {}
            return str(external.get("kind") or "") == "data"
        type_value = self.mod_type_value(mod, record)
        if type_value in ("data", "data_mod", "hybrid"):
            return True
        return bool(self.mod_data_folder(mod, record))

    def mod_latest_label(self, mod: dict[str, Any]) -> str:
        external = mod.get("external") if self.is_external_mod(mod) else None
        if isinstance(external, dict):
            return str(external.get("release") or "linked") if external.get("repo") else "external"
        declared = bmm.latest_declared_version(mod)
        return str(declared.get("version")) if declared else "github"

    def mod_status_tag(self, mod: dict[str, Any], section: str) -> tuple[str, str]:
        if self.is_external_mod(mod):
            external = mod.get("external") if isinstance(mod.get("external"), dict) else {}
            enabled = bool(external.get("enabled", True))
            if section == "data":
                status = "external active" if enabled else "external disabled"
            else:
                status = "external" if enabled else "external disabled"
            if not enabled:
                return status, "disabled"
            return status, "external_linked" if external.get("repo") else "external"

        mod_id = str(mod.get("id", ""))
        installed = self.state.get("installed", {}) if isinstance(self.state, dict) else {}
        record = installed.get(mod_id) if isinstance(installed, dict) else None
        if not isinstance(record, dict):
            return "not installed", "missing"

        status = f"installed {record.get('version', '?')}"
        tag = "installed"
        if section == "data":
            load_active = self.record_data_load_active(record)
            if load_active is False:
                status += " disabled"
                if record.get("enabled", True):
                    status += " (load order)"
                return status, "disabled"
            if load_active is True:
                status += " active"
        if not record.get("enabled", True):
            status += " disabled"
            tag = "disabled"
        return status, tag

    def current_data_load_order(self) -> list[str]:
        game_dir = self.current_game_dir_path()
        if not game_dir or not game_dir.exists():
            return ["core"]
        try:
            path, order = bmm.load_loading_order(game_dir)
            if not path.exists() and bmm.inactive_data_loading_order_path(game_dir).exists():
                bmm.merge_inactive_data_loading_order(game_dir, order)
        except bmm.BmmError:
            return ["core"]
        values = bmm.loading_order_values(order, "aLoadOrder")
        return values or ["core"]

    def data_tree_iid_for_mod(self, mod_id: str) -> str:
        return DATA_ROW_PREFIX + mod_id

    def plugin_tree_iid_for_mod(self, mod_id: str) -> str:
        return PLUGIN_ROW_PREFIX + mod_id

    def data_tree_iid_for_folder(self, folder: str) -> str:
        return DATA_FOLDER_ROW_PREFIX + folder.lower()

    def insert_plugin_row(self, mod: dict[str, Any]) -> None:
        mod_id = str(mod.get("id", ""))
        iid = self.plugin_tree_iid_for_mod(mod_id)
        status, tag = self.mod_status_tag(mod, "plugin")
        categories = ", ".join(str(c) for c in mod.get("categories", []) if isinstance(c, str))
        self.row_mod_ids[iid] = mod_id
        self.plugin_tree.insert(
            "",
            "end",
            iid=iid,
            values=(status, self.mod_latest_label(mod), mod_id, mod.get("name", ""), categories),
            tags=(tag,),
        )

    def insert_data_mod_row(self, mod: dict[str, Any], folder: str, load_number: str = "") -> None:
        mod_id = str(mod.get("id", ""))
        iid = self.data_tree_iid_for_mod(mod_id)
        status, tag = self.mod_status_tag(mod, "data")
        categories = ", ".join(str(c) for c in mod.get("categories", []) if isinstance(c, str))
        self.row_mod_ids[iid] = mod_id
        self.row_data_folders[iid] = folder
        self.data_tree.insert(
            "",
            "end",
            iid=iid,
            values=(load_number, status, self.mod_latest_label(mod), folder, mod.get("name", ""), categories),
            tags=(tag,),
        )

    def insert_data_folder_row(self, folder: str, load_number: str) -> None:
        if folder == "core":
            self.row_data_folders[CORE_ROW_ID] = "core"
            self.data_tree.insert(
                "",
                "end",
                iid=CORE_ROW_ID,
                values=("locked", "game core", "", "core", "Ostranauts Core Data", "base game"),
                tags=("core",),
            )
            return
        iid = self.data_tree_iid_for_folder(folder)
        self.row_data_folders[iid] = folder
        self.data_tree.insert(
            "",
            "end",
            iid=iid,
            values=(load_number, "load-order only", "", folder, "Unknown data mod folder", "not indexed"),
            tags=("unknown",),
        )

    def populate_mod_table(self) -> None:
        selected_mod = self.selected_mod_id()
        selected_folder = self.selected_data_folder()
        selected_was_data = self.active_tree is self.data_tree
        for tree in (self.plugin_tree, self.data_tree):
            for item in tree.get_children():
                tree.delete(item)
        self.row_mod_ids = {}
        self.row_data_folders = {}

        filter_text = self.search_var.get().strip().lower()
        installed = self.state.get("installed", {}) if isinstance(self.state, dict) else {}
        if not isinstance(installed, dict):
            installed = {}

        all_data_mods: list[tuple[dict[str, Any], str]] = []
        data_by_folder: dict[str, tuple[dict[str, Any], str]] = {}
        for mod in self.mods:
            mod_id = str(mod.get("id", ""))
            record = installed.get(mod_id)
            if not isinstance(record, dict):
                record = None
            if self.mod_has_data_side(mod, record):
                folder = self.mod_data_folder(mod, record)
                if folder:
                    all_data_mods.append((mod, folder))
                    data_by_folder.setdefault(folder.lower(), (mod, folder))

        for mod in sorted(self.mods, key=lambda item: str(item.get("name", "")).lower()):
            mod_id = str(mod.get("id", ""))
            record = installed.get(mod_id)
            if not isinstance(record, dict):
                record = None
            if self.mod_has_plugin_side(mod, record) and self.mod_matches_filter(mod, filter_text):
                self.insert_plugin_row(mod)

        load_order = self.current_data_load_order()
        shown_data_ids: set[str] = set()
        data_position = 0
        for folder in load_order:
            if folder == "core":
                if not filter_text or "core".find(filter_text) >= 0 or "ostranauts core data".find(filter_text) >= 0:
                    self.insert_data_folder_row("core", "locked")
                continue
            data_position += 1
            match = data_by_folder.get(folder.lower())
            if match:
                mod, real_folder = match
                if not self.mod_matches_filter(mod, filter_text, real_folder):
                    continue
                self.insert_data_mod_row(mod, real_folder, str(data_position))
                shown_data_ids.add(str(mod.get("id", "")))
                continue
            if filter_text and filter_text not in folder.lower():
                continue
            self.insert_data_folder_row(folder, str(data_position))

        for mod, folder in sorted(all_data_mods, key=lambda item: (item[1].lower(), str(item[0].get("name", "")).lower())):
            mod_id = str(mod.get("id", ""))
            if mod_id in shown_data_ids:
                continue
            if not self.mod_matches_filter(mod, filter_text, folder):
                continue
            self.insert_data_mod_row(mod, folder, "")

        restored = False
        if selected_mod:
            preferred = self.data_tree_iid_for_mod(selected_mod) if selected_was_data else self.plugin_tree_iid_for_mod(selected_mod)
            fallback = self.plugin_tree_iid_for_mod(selected_mod) if selected_was_data else self.data_tree_iid_for_mod(selected_mod)
            for tree, iid in ((self.data_tree if selected_was_data else self.plugin_tree, preferred), (self.plugin_tree if selected_was_data else self.data_tree, fallback)):
                if tree.exists(iid):
                    self.active_tree = tree
                    tree.selection_set(iid)
                    tree.see(iid)
                    restored = True
                    break
        elif selected_folder:
            for iid, folder in self.row_data_folders.items():
                if folder == selected_folder and self.data_tree.exists(iid):
                    self.active_tree = self.data_tree
                    self.data_tree.selection_set(iid)
                    self.data_tree.see(iid)
                    restored = True
                    break
        if not restored:
            self.active_tree = None
        self.show_selected_details()

    def populate_profiles(self) -> None:
        profiles = self.state.get("profiles", {}) if isinstance(self.state, dict) else {}
        names = sorted(str(name) for name in profiles)
        self.profile_combo["values"] = names
        if names and self.profile_var.get() not in names:
            self.profile_var.set(names[0])
        elif not names:
            self.profile_var.set("")

    def selected_mod_id(self) -> str | None:
        row_id = self.selected_row_id()
        if not row_id:
            return None
        return self.row_mod_ids.get(row_id)

    def selected_row_id(self) -> str | None:
        trees = []
        if self.active_tree is not None:
            trees.append(self.active_tree)
        for tree in (getattr(self, "plugin_tree", None), getattr(self, "data_tree", None)):
            if tree is not None and tree not in trees:
                trees.append(tree)
        for tree in trees:
            selection = tree.selection()
            if selection:
                self.active_tree = tree
                return str(selection[0])
        return None

    def selected_data_folder(self) -> str | None:
        row_id = self.selected_row_id()
        if not row_id:
            return None
        return self.row_data_folders.get(row_id)

    def selected_mod(self) -> dict[str, Any] | None:
        mod_id = self.selected_mod_id()
        if not mod_id:
            return None
        return self.mods_by_id.get(mod_id)

    def select_mod_row(self, mod_id: str) -> bool:
        targets = [
            (self.plugin_tree, self.plugin_tree_iid_for_mod(mod_id), "BepInEx / Plugin Mods"),
            (self.data_tree, self.data_tree_iid_for_mod(mod_id), "Data Mods / Load Order"),
        ]
        for tree, iid, section in targets:
            if not tree.exists(iid):
                continue
            other = self.data_tree if tree is self.plugin_tree else self.plugin_tree
            other.selection_remove(other.selection())
            tree.selection_set(iid)
            tree.focus(iid)
            tree.see(iid)
            self.active_tree = tree
            self.show_selected_details()
            mod = self.mods_by_id.get(mod_id, {})
            self.status_var.set(f"Showing {mod.get('name') or mod_id} in {section}")
            return True
        return False

    def reveal_updated_github_mods(self, mod_ids: list[str]) -> None:
        clean_ids = []
        for mod_id in mod_ids:
            value = str(mod_id or "").strip()
            if value and value not in clean_ids:
                clean_ids.append(value)
        if not clean_ids:
            return
        for mod_id in clean_ids:
            if self.select_mod_row(mod_id):
                return
        if self.search_var.get().strip():
            self.search_var.set("")
            self.populate_mod_table()
            self.log("Cleared search filter to show the updated GitHub mod.")
            for mod_id in clean_ids:
                if self.select_mod_row(mod_id):
                    return
        names = ", ".join(clean_ids)
        self.status_var.set(f"GitHub update completed, but updated mod row is not visible: {names}")

    def repo_for_mod(self, mod_id: str, mod: dict[str, Any] | None = None, state: dict[str, Any] | None = None) -> str | None:
        if isinstance(mod, dict):
            release = mod.get("release")
            if isinstance(release, dict) and release.get("provider") == "github":
                repo = release.get("repo")
                if isinstance(repo, str) and repo.strip():
                    return repo.strip()

        lookup_state = state if isinstance(state, dict) else self.state
        cached_mods = lookup_state.get(GITHUB_MODS_KEY, {}) if isinstance(lookup_state, dict) else {}
        if isinstance(cached_mods, dict):
            for repo, cached in cached_mods.items():
                if isinstance(repo, str) and isinstance(cached, dict) and str(cached.get("id", "")) == mod_id:
                    return repo
                if isinstance(repo, str) and isinstance(cached, list):
                    for cached_mod in cached:
                        if isinstance(cached_mod, dict) and str(cached_mod.get("id", "")) == mod_id:
                            return repo

        for repo in self.load_tracked_repos():
            if self.github_mod_id(repo) == mod_id:
                return repo
        return None

    def data_zip_for_mod(self, mod_id: str, mod: dict[str, Any] | None = None) -> str | None:
        if not isinstance(mod, dict) or str(mod.get("type") or "").lower() != "data":
            return None
        for version in mod.get("versions") or []:
            if not isinstance(version, dict):
                continue
            download = version.get("download")
            if not isinstance(download, dict) or download.get("type") != "local":
                continue
            path = str(download.get("path") or "").strip()
            if path and self.data_zip_mod_entry(path) and str(self.data_zip_mod_entry(path).get("id")) == mod_id:
                return path
        return None

    def cached_download_paths_for_mod(
        self,
        mod: dict[str, Any],
        repo: str | None,
        state: dict[str, Any],
    ) -> list[Path]:
        names: set[str] = set()
        for version in mod.get("versions") or []:
            if not isinstance(version, dict):
                continue
            download = version.get("download")
            if not isinstance(download, dict) or download.get("type") != "url":
                continue
            url = str(download.get("url") or "")
            name = Path(urllib.parse.urlparse(url).path).name
            if name:
                names.add(name)

        statuses = state.get(GITHUB_STATUS_KEY, {}) if isinstance(state, dict) else {}
        if repo and isinstance(statuses, dict):
            status = statuses.get(repo)
            if isinstance(status, dict):
                asset = str(status.get("asset") or "").strip()
                if asset:
                    names.add(asset)

        cache_root = self.rt.cache_dir.resolve()
        paths = []
        for name in names:
            if not name or "/" in name or "\\" in name:
                continue
            path = (self.rt.cache_dir / name).resolve()
            with contextlib.suppress(ValueError):
                path.relative_to(cache_root)
                paths.append(path)
        return paths

    def clear_details(self) -> None:
        for widget in self.detail_widgets:
            widget.destroy()
        self.detail_widgets = []
        self.detail_wrap_widgets = []
        if hasattr(self, "detail_canvas"):
            self.detail_canvas.yview_moveto(0)

    def add_detail_heading(self, text: str, row: int) -> int:
        label = ttk.Label(self.detail_frame, text=text, style="DetailHeader.TLabel", justify="left")
        label.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self._track_detail_wrap(label, "full")
        self.detail_widgets.append(label)
        return row + 1

    def add_detail_row(self, row: int, label_text: str, value: Any) -> int:
        label = ttk.Label(self.detail_frame, text=label_text, width=16, anchor="w", style="DetailKey.TLabel")
        label.grid(row=row, column=0, sticky="nw", padx=(0, 8), pady=2)
        value_label = ttk.Label(
            self.detail_frame,
            text=str(value or ""),
            justify="left",
            anchor="w",
            style="DetailValue.TLabel",
        )
        value_label.grid(row=row, column=1, sticky="ew", pady=2)
        self._track_detail_wrap(value_label, "value")
        self.detail_widgets.extend([label, value_label])
        return row + 1

    def add_detail_text(self, row: int, text: str) -> int:
        value_label = ttk.Label(
            self.detail_frame,
            text=text,
            justify="left",
            anchor="w",
            style="DetailValue.TLabel",
        )
        value_label.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 2))
        self._track_detail_wrap(value_label, "full")
        self.detail_widgets.append(value_label)
        return row + 1

    def show_selected_details(self) -> None:
        row_id = self.selected_row_id()
        mod = self.selected_mod()
        self.clear_details()
        if row_id == CORE_ROW_ID:
            row = 0
            row = self.add_detail_heading("Ostranauts Core Data", row)
            row = self.add_detail_row(row, "Load Order", "locked")
            row = self.add_detail_row(row, "Folder", "core")
            self.add_detail_text(row, "Core is the base game data entry. BMM keeps it first and does not move or remove it.")
            return
        if not mod and row_id and row_id in self.row_data_folders:
            folder = self.row_data_folders[row_id]
            row = 0
            row = self.add_detail_heading("Load-order folder", row)
            row = self.add_detail_row(row, "Folder", folder)
            row = self.add_detail_row(row, "BMM Managed", "no")
            game_dir = self.current_game_dir_path()
            if game_dir:
                row = self.add_detail_row(row, "Load Order", bmm.display_game_path(game_dir, bmm.loading_order_path(game_dir)))
            self.add_detail_text(
                row,
                "This folder is present in the game's data load order, but BMM could not match it to an installed or indexed data mod.",
            )
            return
        if not mod:
            self.add_detail_text(0, "Select a mod to see details.")
            return
        mod_id = str(mod.get("id", ""))
        if self.is_external_mod(mod):
            external = mod.get("external") if isinstance(mod.get("external"), dict) else {}
            kind = str(external.get("kind") or "bepinex")
            row = 0
            row = self.add_detail_heading(str(mod.get("name", mod_id)), row)
            row = self.add_detail_row(row, "Source", "External data mod" if kind == "data" else "External BepInEx plugin")
            row = self.add_detail_row(row, "BMM Managed", "no")
            row = self.add_detail_row(row, "Path", external.get("path", ""))
            row = self.add_detail_row(row, "Enabled", external.get("enabled", True))
            if kind == "data":
                game_dir = self.current_game_dir_path()
                if game_dir:
                    row = self.add_detail_row(row, "Load Order", bmm.display_game_path(game_dir, bmm.loading_order_path(game_dir)))
                row = self.add_detail_row(row, "Author", external.get("author", ""))
                row = self.add_detail_row(row, "Game Version", external.get("game_version", ""))
                row = self.add_detail_row(row, "Mod Version", external.get("mod_version", ""))
            repo = str(external.get("repo") or "").strip()
            row = self.add_detail_row(row, "Linked Repo", repo or "not connected")
            if repo:
                row = self.add_detail_row(row, "Latest", external.get("release") or "not checked")
                row = self.add_detail_row(row, "Asset", external.get("asset") or "")
                row = self.add_detail_row(row, "Last Check", external.get("updated_at") or "")
                row = self.add_detail_row(row, "Website", mod.get("website", ""))
            message = str(external.get("message") or "").strip()
            if message:
                row = self.add_detail_text(row, f"GitHub status: {message}")
            self.add_detail_text(
                row,
                "This mod was found in the game folder but was not installed by BMM. "
                "BMM will not install, uninstall, enable, disable, update, or remove its files.",
            )
            return
        declared = bmm.latest_declared_version(mod)
        record = self.state.get("installed", {}).get(mod_id) if isinstance(self.state, dict) else None
        plugin = mod.get("plugin", {}) if isinstance(mod.get("plugin"), dict) else {}
        rel = bmm.merged_relationships(mod, declared)
        row = 0
        row = self.add_detail_heading(str(mod.get("name", mod_id)), row)
        row = self.add_detail_row(row, "ID", mod_id)
        row = self.add_detail_row(row, "Latest", declared.get("version") if declared else "GitHub latest")
        row = self.add_detail_row(row, "Installed", record.get("version", "?") if isinstance(record, dict) else "no")
        row = self.add_detail_row(row, "Enabled", record.get("enabled", True) if isinstance(record, dict) else "n/a")
        type_value = str((record or {}).get("type") if isinstance(record, dict) else mod.get("type") or "").lower()
        is_data_mod = type_value in ("data", "data_mod", "hybrid")
        if is_data_mod:
            type_label = "Hybrid BepInEx + data mod" if type_value == "hybrid" else "Ostranauts data mod"
            data_folder = self.record_data_folder(record) if isinstance(record, dict) else str(mod.get("data_mod_folder", "") or "")
            row = self.add_detail_row(row, "Type", type_label)
            row = self.add_detail_row(row, "Data Folder", data_folder)
            game_dir = self.current_game_dir_path()
            if game_dir:
                row = self.add_detail_row(row, "Load Order", bmm.display_game_path(game_dir, bmm.loading_order_path(game_dir)))
            if isinstance(record, dict):
                load_active = self.record_data_load_active(record)
                if load_active is not None:
                    row = self.add_detail_row(row, "Load Active", load_active)
                    if record.get("enabled", True) and not load_active:
                        row = self.add_detail_text(
                            row,
                            "BMM state says this data mod is enabled, but the game load order does not include its folder.",
                        )
        plugin_guid = str(plugin.get("guid", "")).strip()
        plugin_dll = str(plugin.get("dll", "")).strip()
        if plugin_guid and plugin_guid != mod_id:
            row = self.add_detail_row(row, "Plugin GUID", plugin_guid)
        if plugin_dll:
            row = self.add_detail_row(row, "Plugin DLL", plugin_dll)
        row = self.add_detail_row(row, "Website", mod.get("website", ""))
        summary = str(mod.get("summary", "")).strip()
        if summary and not summary.startswith("Manual GitHub repo:"):
            row = self.add_detail_text(row, summary)
        for key in bmm.RELATIONSHIP_KEYS:
            if key == "provides":
                continue
            values = rel.get(key, [])
            if values:
                labels = ", ".join(bmm.relationship_label(value) for value in values)
                row = self.add_detail_row(row, key, labels)
        notes = mod.get("notes", [])
        if isinstance(notes, list) and notes:
            visible_notes = [
                str(note)
                for note in notes
                if isinstance(note, str)
                and not note.startswith("Manual GitHub entry")
                and not note.startswith("Install uses ZIP auto-detection")
                and not note.startswith("Selected release asset:")
            ]
        else:
            visible_notes = []
        if visible_notes:
            row = self.add_detail_text(row, "Notes:")
            for note in visible_notes:
                row = self.add_detail_text(row, f"- {note}")

    def choose_game_dir(self) -> None:
        initial = self.game_dir_var.get().strip()
        chosen = filedialog.askdirectory(title="Select Ostranauts game folder", initialdir=initial if Path(initial).exists() else None)
        if not chosen:
            return
        self.config = bmm.load_config(self.rt)
        self.config["game"] = "ostranauts"
        self.config["game_dir"] = chosen
        bmm.write_json_with_backup(self.rt.config_path, self.config)
        self.log(f"Game folder set to {chosen}")
        self.game_dir_var.set(chosen)
        self.update_bepinex_status()
        self.reload_data()

    def choose_data_zip(self) -> None:
        initial = self.data_zip_var.get().strip()
        initial_dir = str(Path(initial).expanduser().parent) if initial and Path(initial).expanduser().parent.exists() else None
        chosen = filedialog.askopenfilename(
            title="Select Ostranauts data mod ZIP",
            initialdir=initial_dir,
            filetypes=[("Zip archives", "*.zip"), ("All files", "*.*")],
        )
        if chosen:
            self.data_zip_var.set(chosen)

    def normalize_data_zip_path(self, value: str) -> Path:
        text = value.strip().strip('"')
        if not text:
            raise bmm.BmmError("Paste or browse to a data mod ZIP first.")
        path = Path(text).expanduser()
        if not path.exists() or not path.is_file():
            raise bmm.BmmError(f"Data mod ZIP not found: {path}")
        if path.suffix.lower() != ".zip":
            raise bmm.BmmError(f"Data mod file must be a ZIP: {path}")
        return path.resolve()

    def add_data_mod_zip(self) -> None:
        try:
            zip_path = self.normalize_data_zip_path(self.data_zip_var.get())
            entry = self.data_zip_mod_entry(str(zip_path))
            if not entry:
                raise bmm.BmmError("ZIP must contain one top-level data mod folder with mod_info.json.")
            data_folder = str(entry.get("data_mod_folder") or "")
            bmm.validate_data_mod_archive_json(zip_path, data_folder)
        except bmm.BmmError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.ensure_mod_index_file()
        paths = self.load_tracked_data_zips()
        value = str(zip_path)
        if value.lower() not in {item.lower() for item in paths}:
            paths.append(value)
            self.save_tracked_data_zips(paths)
            self.log(f"Added data mod ZIP: {value}")
        else:
            self.log(f"Data mod ZIP already tracked: {value}")
        self.data_zip_var.set("")
        self.index = self.build_runtime_index()
        self.save_runtime_index()
        self.reload_data(log_errors=False)

    def normalize_github_repo(self, value: str) -> str:
        text = value.strip()
        if not text:
            raise bmm.BmmError("Paste a GitHub repo URL or owner/repo value first.")
        if text.startswith("http://") or text.startswith("https://"):
            parsed = urllib.parse.urlparse(text)
            if parsed.netloc.lower() not in ("github.com", "www.github.com"):
                raise bmm.BmmError("Only github.com repository URLs are supported.")
            parts = [part for part in parsed.path.strip("/").split("/") if part]
            if len(parts) < 2:
                raise bmm.BmmError("GitHub URL must include owner and repo.")
            owner, repo = parts[0], parts[1]
        else:
            parts = [part for part in text.strip("/").split("/") if part]
            if len(parts) != 2:
                raise bmm.BmmError("Use a GitHub URL or owner/repo.")
            owner, repo = parts
        repo = repo[:-4] if repo.lower().endswith(".git") else repo
        if not re.match(r"^[A-Za-z0-9_.-]+$", owner) or not re.match(r"^[A-Za-z0-9_.-]+$", repo):
            raise bmm.BmmError("GitHub owner/repo contains unsupported characters.")
        return f"{owner}/{repo}"

    def github_mod_id(self, repo: str) -> str:
        return "github." + re.sub(r"[^a-z0-9_.-]+", "-", repo.replace("/", ".").lower()).strip(".-")

    def choose_release_asset(self, release: dict[str, Any] | None) -> dict[str, Any] | None:
        if not release:
            return None
        assets = release.get("assets") or []
        zip_assets = [
            asset
            for asset in assets
            if isinstance(asset, dict) and str(asset.get("name", "")).lower().endswith(".zip")
        ]
        if not zip_assets:
            return None
        return sorted(zip_assets, key=lambda asset: str(asset.get("name", "")).lower())[0]

    def fetch_github_fallback_mod_entry(
        self,
        repo: str,
        repo_data: dict[str, Any],
        release: dict[str, Any] | None,
        release_error: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        owner, repo_name = repo.split("/", 1)
        asset = self.choose_release_asset(release)
        mod_id = self.github_mod_id(repo)
        name = str(repo_data.get("name") or repo_name)
        summary = str(repo_data.get("description") or f"Manual GitHub repo: {repo}")
        html_url = str(repo_data.get("html_url") or f"https://github.com/{repo}")
        tag = str((release or {}).get("tag_name") or "").lstrip("v")
        notes = [
            "Manual GitHub entry generated by BMM.",
            "Install uses ZIP auto-detection unless a full BMM manifest is added later.",
        ]
        if release:
            notes.append(f"Latest GitHub release: {release.get('tag_name')}")
        if release_error:
            notes.append(f"Release check failed: {release_error}")
        if asset:
            notes.append(f"Selected release asset: {asset.get('name')}")
        else:
            notes.append("No ZIP release asset was selected; install/inspect may need manual metadata.")

        entry: dict[str, Any] = {
            "id": mod_id,
            "name": name,
            "summary": summary,
            "authors": [owner],
            "categories": ["github"],
            "website": html_url,
            "notes": notes,
            "plugin": {
                "guid": mod_id,
                "name": name,
                "dll": "",
            },
            "relationships": {
                "depends": [],
                "recommends": [],
                "suggests": [],
                "conflicts": [],
                "provides": [mod_id],
            },
            "release": {
                "provider": "github",
                "repo": repo,
                "include_prereleases": False,
            },
            "versions": [],
        }
        if asset:
            entry["release"]["asset_pattern"] = str(asset.get("name", ""))
        if release and asset and tag and asset.get("browser_download_url"):
            download: dict[str, Any] = {
                "type": "url",
                "url": str(asset["browser_download_url"]),
                "source_label": f"{repo} {release.get('tag_name')} {asset.get('name')}",
            }
            digest = str(asset.get("digest") or "")
            if digest.startswith("sha256:"):
                download["sha256"] = digest.replace("sha256:", "", 1)
            entry["versions"] = [
                {
                    "version": tag,
                    "download": download,
                    "game_versions": [],
                    "bepinex": "",
                    "relationships": {
                        "depends": [],
                        "recommends": [],
                        "suggests": [],
                        "conflicts": [],
                    },
                }
            ]

        status = {
            "repo": repo,
            "ok": not release_error,
            "updated_at": bmm.stamp(),
            "release": str((release or {}).get("tag_name") or ""),
            "asset": str((asset or {}).get("name") or ""),
            "message": release_error or "OK",
        }
        return entry, status

    def fetch_github_mod_entries(self, repo: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        repo_data = bmm.http_get_json(f"{bmm.GITHUB_API_ROOT}/repos/{repo}")
        if not isinstance(repo_data, dict):
            raise bmm.BmmError(f"Unexpected GitHub repo response for {repo}")

        release = None
        release_error = ""
        try:
            release = bmm.github_latest_release(repo)
        except bmm.BmmError as exc:
            release_error = str(exc)

        default_branch = str(repo_data.get("default_branch") or "")
        nest = bmm.github_fetch_nest(repo, default_branch or None)
        if nest:
            entries = bmm.nest_to_index_mods(nest, repo=repo, repo_data=repo_data, release=release)
            asset_names = []
            for entry in entries:
                release_spec = entry.get("release") if isinstance(entry.get("release"), dict) else {}
                pattern = str(release_spec.get("asset_pattern") or "").strip()
                if release and pattern:
                    try:
                        asset_names.append(str(bmm.find_release_asset(release, pattern).get("name") or pattern))
                    except bmm.BmmError:
                        asset_names.append(pattern)
            status = {
                "repo": repo,
                "ok": not release_error,
                "updated_at": bmm.stamp(),
                "release": str((release or {}).get("tag_name") or ""),
                "asset": ", ".join(asset_names),
                "message": release_error or f"OK: {bmm.NEST_FILE_NAME} found with {len(entries)} mod(s)",
                "nest": bmm.NEST_FILE_NAME,
                "mods": [str(entry.get("id") or "") for entry in entries],
            }
            return entries, status

        entry, status = self.fetch_github_fallback_mod_entry(repo, repo_data, release, release_error)
        return [entry], status

    def add_github_repo(self) -> None:
        try:
            repo = self.normalize_github_repo(self.github_repo_var.get())
        except bmm.BmmError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        self.ensure_mod_index_file()
        repos = self.load_tracked_repos()
        if repo not in repos:
            repos.append(repo)
            self.save_tracked_repos(repos)
            self.log(f"Added GitHub repo: {repo}")
        else:
            self.log(f"GitHub repo already tracked: {repo}")
        self.github_repo_var.set("")
        self.update_github_repos([repo], focus=True)

    def load_order_edit_blocked(self) -> bool:
        if self.search_var.get().strip():
            messagebox.showinfo(APP_TITLE, "Clear the search box before editing load order so hidden rows are not skipped.")
            return True
        game_dir = self.current_game_dir_path()
        if not game_dir or not game_dir.exists():
            messagebox.showinfo(APP_TITLE, "Select the Ostranauts game folder before editing data load order.")
            return True
        return False

    def selected_data_row_for_edit(self) -> str | None:
        if self.active_tree is not self.data_tree:
            messagebox.showinfo(APP_TITLE, "Select a data mod row in the Data Mods / Load Order table.")
            return None
        selection = self.data_tree.selection()
        if not selection:
            messagebox.showinfo(APP_TITLE, "Select a data mod row first.")
            return None
        iid = str(selection[0])
        folder = self.row_data_folders.get(iid)
        if not folder:
            messagebox.showinfo(APP_TITLE, "Select a load-order row first.")
            return None
        if folder == "core":
            messagebox.showinfo(APP_TITLE, "Core is locked at the top of the data load order.")
            return None
        if not str(self.data_tree.set(iid, "load")).strip():
            messagebox.showinfo(APP_TITLE, "This data mod is not in the active load order. Install or enable it before moving it.")
            return None
        return iid

    def ordered_data_load_iids(self) -> list[str]:
        result = []
        for iid in self.data_tree.get_children():
            folder = self.row_data_folders.get(str(iid))
            if not folder or folder == "core":
                continue
            if str(self.data_tree.set(iid, "load")).strip():
                result.append(str(iid))
        return result

    def renumber_data_load_rows(self) -> None:
        number = 1
        for iid in self.data_tree.get_children():
            folder = self.row_data_folders.get(str(iid))
            if folder == "core":
                self.data_tree.set(iid, "load", "locked")
                continue
            if not folder:
                continue
            if str(self.data_tree.set(iid, "load")).strip():
                self.data_tree.set(iid, "load", str(number))
                number += 1

    def move_data_load_order(self, delta: int) -> None:
        if self.load_order_edit_blocked():
            return
        iid = self.selected_data_row_for_edit()
        if not iid:
            return
        movable = self.ordered_data_load_iids()
        try:
            index = movable.index(iid)
        except ValueError:
            return
        next_index = index + delta
        if next_index < 0 or next_index >= len(movable):
            return
        target = movable[next_index]
        target_tree_index = self.data_tree.index(target)
        if delta < 0:
            self.data_tree.move(iid, "", target_tree_index)
        else:
            self.data_tree.move(iid, "", target_tree_index + 1)
        self.data_tree.selection_set(iid)
        self.data_tree.see(iid)
        self.renumber_data_load_rows()
        self.data_load_order_dirty = True
        self.status_var.set("Data load order changed. Use Save Load Order to write loading_order.json.")

    def current_data_tree_load_order(self) -> list[str]:
        folders = []
        for iid in self.ordered_data_load_iids():
            folder = self.row_data_folders.get(iid)
            if folder and folder != "core":
                folders.append(folder)
        return folders

    def save_data_load_order(self) -> None:
        if self.load_order_edit_blocked():
            return
        folders = self.current_data_tree_load_order()
        game_dir = self.current_game_dir_path()
        if not game_dir:
            return
        load_order_path = bmm.loading_order_path(game_dir)
        preview = "\n".join(f"{idx}. {folder}" for idx, folder in enumerate(folders, start=1))
        if not preview:
            preview = "No data mods after core."
        message = (
            "Save this data mod load order to the game?\n\n"
            f"{preview}\n\n"
            f"File: {load_order_path}\n\n"
            "Changing load order can change which data mod wins when two mods edit the same records."
        )
        if not messagebox.askyesno(APP_TITLE, message):
            return

        def task() -> int:
            path, changed = bmm.save_data_mod_load_order(game_dir, folders)
            print(f"Data load order {'saved' if changed else 'already current'}: {path}")
            for idx, folder in enumerate(folders, start=1):
                print(f"  {idx}. {folder}")
            return 0

        self.data_load_order_dirty = False
        self.run_task("Save data load order", task, refresh=True)

    def external_action_blocked(self, action: str) -> bool:
        mod_id = self.selected_mod_id()
        if not self.is_external_mod_id(mod_id):
            return False
        messagebox.showinfo(
            APP_TITLE,
            f"{action} is blocked for external mods.\n\n"
            "This mod was not installed by BMM, so BMM is only listing it and can connect it to a repo.",
        )
        return True

    def confirm_external_toggle(self, mod: dict[str, Any], enable: bool) -> bool:
        external = mod.get("external") if isinstance(mod.get("external"), dict) else {}
        name = str(mod.get("name") or mod.get("id") or "external mod")
        action = "Enable" if enable else "Disable"
        kind = str(external.get("kind") or "bepinex")
        if kind == "data":
            target = f"data load-order folder: {external.get('path', '')}"
        else:
            target = f"BepInEx plugin file: {external.get('path', '')}"
        return messagebox.askyesno(
            APP_TITLE,
            f"{action} external mod {name}?\n\n"
            "This mod was not installed by BMM, so BMM does not own its files and cannot update or remove it safely.\n\n"
            f"BMM will only change the enabled state for:\n{target}",
        )

    def external_plugin_toggle_plan(self, game_dir: Path, external: dict[str, Any], enable: bool) -> tuple[Path, Path, str]:
        rel = str(external.get("path") or "").replace("\\", "/").strip("/")
        if not rel:
            raise bmm.BmmError("External plugin path is missing.")
        root_name = str(external.get("root") or "bepinex_plugins")
        root = bmm.install_root_path(game_dir, root_name)
        current = bmm.safe_target(root, rel, root_name)

        if enable:
            base_rel = self.external_base_path(rel)
            target = bmm.safe_target(root, base_rel, root_name)
            if target.exists():
                return current, target, base_rel
            if not current.exists():
                disabled = bmm.safe_target(root, base_rel + ".disabled", root_name)
                if disabled.exists():
                    return disabled, target, base_rel
            if not str(current).lower().endswith(".dll.disabled"):
                raise bmm.BmmError(f"External plugin is not disabled: {current}")
            return current, target, base_rel

        base_rel = self.external_base_path(rel)
        source = bmm.safe_target(root, base_rel, root_name)
        if not source.exists():
            source = current
        if not source.exists():
            raise bmm.BmmError(f"External plugin file not found: {source}")
        disabled_rel = base_rel + ".disabled"
        target = bmm.safe_target(root, disabled_rel, root_name)
        if target.exists():
            target = bmm.safe_target(root, disabled_rel + "-" + bmm.stamp(), root_name)
            disabled_rel = str(target.relative_to(root)).replace("\\", "/")
        return source, target, disabled_rel

    def toggle_external_selected(self, enable: bool) -> bool:
        mod = self.selected_mod()
        if not self.is_external_mod(mod):
            return False
        if not self.confirm_external_toggle(mod, enable):
            return True

        external = mod.get("external") if isinstance(mod.get("external"), dict) else {}
        kind = str(external.get("kind") or "bepinex")
        name = str(mod.get("name") or mod.get("id") or "external mod")
        game_dir = self.current_game_dir_path()
        if not game_dir or not game_dir.exists():
            messagebox.showerror(APP_TITLE, "Select the Ostranauts game folder first.")
            return True

        def task() -> int:
            if kind == "data":
                folder = str(external.get("path") or "").strip()
                if not folder:
                    raise bmm.BmmError("External data mod folder is missing.")
                path, changed = bmm.set_data_mod_load_order(game_dir, folder, enable)
                print(f"{'Enabled' if enable else 'Disabled'} external data mod {name}")
                print(f"  folder: {folder}")
                print(f"  load order: {path} ({'changed' if changed else 'already current'})")
                return 0

            source, target, stored_rel = self.external_plugin_toggle_plan(game_dir, external, enable)
            if source.resolve() != target.resolve():
                if enable and target.exists():
                    raise bmm.BmmError(f"Cannot enable because target already exists: {target}")
                source.rename(target)
            print(f"{'Enabled' if enable else 'Disabled'} external BepInEx plugin {name}")
            print(f"  {source} -> {target}")
            print(f"  path: {stored_rel}")
            return 0

        self.run_task(f"{'Enable' if enable else 'Disable'} external {name}", task, refresh=True)
        return True

    def connect_repo_selected(self) -> None:
        mod_id = self.require_selected_mod_id()
        if not mod_id:
            return
        mod = self.mods_by_id.get(mod_id)
        if not self.is_external_mod(mod):
            messagebox.showinfo(APP_TITLE, "Select an external mod, paste a GitHub repo, then use Connect Repo.")
            return
        try:
            repo = self.normalize_github_repo(self.github_repo_var.get())
        except bmm.BmmError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        name = str(mod.get("name") or mod_id)
        if not messagebox.askyesno(
            APP_TITLE,
            f"Connect {name} to GitHub repo {repo}?\n\n"
            "This only stores a BMM metadata link. It does not install, update, or overwrite the external mod.",
        ):
            return
        self.github_repo_var.set("")
        self.update_external_repo_link(mod_id, repo, "Connect repo")

    def update_external_repo_link(self, mod_id: str, repo: str, label: str = "Update external repo") -> None:
        mod = self.mods_by_id.get(mod_id, {})
        external = mod.get("external") if isinstance(mod.get("external"), dict) else {}
        name = str(mod.get("name") or mod_id)
        path = str(external.get("path") or "")
        enabled = bool(external.get("enabled", True))
        kind = str(external.get("kind") or "bepinex")
        root_name = str(external.get("root") or "bepinex_plugins")

        def task() -> int:
            entries, status = self.fetch_github_mod_entries(repo)
            entry = entries[0] if entries else {}
            state = bmm.load_state(self.rt)
            links = state.setdefault(EXTERNAL_LINKS_KEY, {})
            if not isinstance(links, dict):
                links = {}
                state[EXTERNAL_LINKS_KEY] = links
            links[mod_id] = {
                "id": mod_id,
                "name": name,
                "kind": kind,
                "root": root_name,
                "path": path,
                "enabled": enabled,
                "repo": repo,
                "website": str(entry.get("website") or f"https://github.com/{repo}"),
                "release": str(status.get("release") or ""),
                "asset": str(status.get("asset") or ""),
                "updated_at": str(status.get("updated_at") or bmm.stamp()),
                "message": str(status.get("message") or "OK"),
                "ok": bool(status.get("ok", False)),
            }
            bmm.save_state(self.rt, state)
            print(f"Connected external mod {name} to {repo}")
            if status.get("release") or status.get("asset"):
                print(f"  latest: {status.get('release') or 'no release'} {status.get('asset') or 'no zip asset'}")
            if status.get("message") and status.get("message") != "OK":
                print(f"  status: {status.get('message')}")
            return 0

        self.run_task(f"{label} {mod_id}", task, refresh=True)

    def update_github_on_startup(self) -> None:
        if self.startup_update_done:
            return
        self.startup_update_done = True
        repos = self.load_tracked_repos()
        if repos:
            self.update_github_repos(list(repos), startup=True)

    def update_github_repos(self, repos: list[str] | None = None, startup: bool = False, focus: bool = False) -> None:
        self.config = bmm.load_config(self.rt)
        self.ensure_mod_index_file()
        tracked = self.load_tracked_repos()
        selected_repos = repos or list(tracked)
        if not selected_repos:
            self.log("No GitHub repos are tracked yet.")
            self.index = self.empty_index()
            self.save_runtime_index()
            self.reload_data(log_errors=False)
            return

        updated_mod_ids: list[str] = []

        def task() -> int:
            state = bmm.load_state(self.rt)
            cached_mods = state.setdefault(GITHUB_MODS_KEY, {})
            statuses = state.setdefault(GITHUB_STATUS_KEY, {})
            if not isinstance(cached_mods, dict):
                cached_mods = {}
                state[GITHUB_MODS_KEY] = cached_mods
            if not isinstance(statuses, dict):
                statuses = {}
                state[GITHUB_STATUS_KEY] = statuses

            had_error = False
            for repo in selected_repos:
                print(f"Updating GitHub repo: {repo}")
                try:
                    entries, status = self.fetch_github_mod_entries(str(repo))
                    cached_mods[str(repo)] = entries
                    statuses[str(repo)] = status
                    print(
                        f"  {len(entries)} mod(s) {status.get('release') or 'no release'} "
                        f"{status.get('asset') or 'no zip asset'}"
                    )
                    for entry in entries:
                        print(f"    {entry.get('id')}: {entry.get('name')}")
                        entry_id = str(entry.get("id") or "").strip()
                        if entry_id and entry_id not in updated_mod_ids:
                            updated_mod_ids.append(entry_id)
                except bmm.BmmError as exc:
                    had_error = True
                    statuses[str(repo)] = {
                        "repo": str(repo),
                        "ok": False,
                        "updated_at": bmm.stamp(),
                        "release": "",
                        "asset": "",
                        "message": str(exc),
                    }
                    print(f"  failed: {exc}")

            active = {str(repo) for repo in tracked if isinstance(repo, str)}
            for repo in list(cached_mods):
                if repo not in active:
                    cached_mods.pop(repo, None)
            bmm.save_state(self.rt, state)
            self.write_mod_index_from_state(state)
            return 1 if had_error else 0

        label = "Update GitHub repos on startup" if startup else "Update GitHub repos"
        after_refresh = (lambda: self.reveal_updated_github_mods(updated_mod_ids)) if focus else None
        self.run_task(label, task, refresh=True, after_refresh=after_refresh)

    def validate_index(self) -> None:
        def task() -> int:
            return bmm.command_validate_index(self.make_args())

        self.run_task("Validate index", task, refresh=False)

    def check_selected(self) -> None:
        mod_id = self.require_selected_mod_id()
        if not mod_id:
            return

        def task() -> int:
            return bmm.command_check(self.make_args(mod_id=mod_id))

        self.run_task(f"Check {mod_id}", task, refresh=False)

    def check_all(self) -> None:
        def task() -> int:
            return bmm.command_check(self.make_args(mod_id=None))

        self.run_task("Check all indexed mods", task, refresh=False)

    def update_selected_or_all(self) -> None:
        mod = self.selected_mod()
        if self.is_external_mod(mod):
            external = mod.get("external") if isinstance(mod.get("external"), dict) else {}
            repo = str(external.get("repo") or "").strip()
            if not repo:
                messagebox.showinfo(APP_TITLE, "Paste a GitHub repo and use Connect Repo before updating this external mod.")
                return
            self.update_external_repo_link(str(mod.get("id", "")), repo, "Update external repo")
            return
        if mod and self.data_zip_for_mod(str(mod.get("id", "")), mod):
            self.index = self.build_runtime_index()
            self.save_runtime_index()
            self.reload_data(log_errors=False)
            self.log(f"Reloaded local data ZIP entry: {mod.get('name', mod.get('id', ''))}")
            return
        repo = self.repo_for_mod(str(mod.get("id", "")), mod) if mod else None
        if isinstance(repo, str) and repo:
            self.update_github_repos([repo], focus=True)
        else:
            self.update_github_repos()

    def inspect_selected(self) -> None:
        mod_id = self.require_selected_mod_id()
        if not mod_id:
            return

        def task() -> int:
            return bmm.command_inspect(self.make_args(target=mod_id))

        self.run_task(f"Inspect {mod_id}", task, refresh=False)

    def existing_install_targets(self, mod: dict[str, Any]) -> list[str]:
        version = bmm.resolve_version(mod, None)
        archive, _source_label = bmm.resolve_archive(mod, version, self.rt)
        config = bmm.load_config(self.rt)
        game_dir = bmm.game_dir_from_config(config, self.game_dir_var.get().strip() or self.game_dir_override)
        entries = bmm.install_entries_for_archive(mod, version, archive)
        plan = bmm.expand_install_plan(archive, entries)
        roots = {str(item["root"]) for item in plan}
        bmm.ensure_game_dir(game_dir)
        if roots - {bmm.DATA_MOD_ROOT}:
            bmm.ensure_bepinex_dir(game_dir)
        existing = []
        for item in plan:
            root_name = str(item["root"])
            root = bmm.install_root_path(game_dir, root_name)
            target = bmm.safe_target(root, item["target"], root_name)
            if target.exists():
                existing.append(f"[{root_name}] {item['target']}")
        return existing

    def install_selected(self) -> None:
        mod_id = self.require_selected_mod_id()
        if not mod_id:
            return
        if self.external_action_blocked("Install"):
            return
        mod = self.mods_by_id.get(mod_id, {})
        self.status_var.set("Checking install targets...")
        self.root.update_idletasks()
        try:
            existing = self.existing_install_targets(mod)
        except bmm.BmmError as exc:
            self.status_var.set("Install check failed")
            messagebox.showerror(APP_TITLE, str(exc))
            return
        finally:
            if not self.busy:
                self.status_var.set("Ready")

        if existing:
            shown = "\n".join(existing[:20])
            extra = "" if len(existing) <= 20 else f"\n...and {len(existing) - 20} more"
            message = (
                "These files already exist and will be backed up before overwrite:\n\n"
                f"{shown}{extra}\n\nContinue?"
            )
            if not messagebox.askyesno(APP_TITLE, message):
                self.log(f"Install cancelled before overwrite: {mod_id}")
                return
        elif not messagebox.askyesno(APP_TITLE, f"Install {mod.get('name', mod_id)} into the configured game mod roots?"):
            return

        def task() -> int:
            return bmm.command_install(self.make_args(mod_id=mod_id, yes=True))

        self.run_task(f"Install {mod_id}", task, refresh=True)

    def uninstall_selected(self) -> None:
        mod_id = self.require_selected_mod_id()
        if not mod_id:
            return
        if self.external_action_blocked("Uninstall"):
            return
        if not messagebox.askyesno(APP_TITLE, f"Uninstall {mod_id} and move BMM-managed files to backups?"):
            return

        def task() -> int:
            return bmm.command_uninstall(self.make_args(mod_id=mod_id, yes=True))

        self.run_task(f"Uninstall {mod_id}", task, refresh=True)

    def remove_selected(self) -> None:
        mod_id = self.require_selected_mod_id()
        if not mod_id:
            return
        if self.external_action_blocked("Remove"):
            return
        mod = self.mods_by_id.get(mod_id, {})
        repo = self.repo_for_mod(mod_id, mod)
        data_zip = self.data_zip_for_mod(mod_id, mod)
        installed = self.state.get("installed", {}) if isinstance(self.state, dict) else {}
        is_installed = isinstance(installed, dict) and mod_id in installed
        name = str(mod.get("name") or mod_id)
        message = (
            f"Remove {name} from BMM?\n\n"
            "This removes the tracked source and cached BMM metadata so the mod disappears from the list.\n"
        )
        if is_installed:
            message += "Installed BMM-managed game files will be moved to backups first.\n"
        message += "BMM will also delete any cached GitHub ZIP for this mod when it can identify it."
        if not messagebox.askyesno(APP_TITLE, message):
            return

        mod_snapshot = dict(mod) if isinstance(mod, dict) else {}
        repo_snapshot = repo
        data_zip_snapshot = data_zip

        def task() -> int:
            state = bmm.load_state(self.rt)
            installed_state = state.get("installed", {})
            was_installed = isinstance(installed_state, dict) and mod_id in installed_state
            if was_installed:
                bmm.command_uninstall(self.make_args(mod_id=mod_id, yes=True))
                state = bmm.load_state(self.rt)
            else:
                print(f"No installed BMM files were recorded for {mod_id}.")

            cache_paths = self.cached_download_paths_for_mod(mod_snapshot, repo_snapshot, state)
            removed_repos: set[str] = set()
            if repo_snapshot:
                repos = self.load_tracked_repos()
                next_repos = [item for item in repos if item != repo_snapshot]
                if len(next_repos) != len(repos):
                    self.save_tracked_repos(next_repos)
                    removed_repos.add(repo_snapshot)
                    print(f"Removed GitHub repo from Mod_index: {repo_snapshot}")
            if data_zip_snapshot:
                paths = self.load_tracked_data_zips()
                next_paths = [item for item in paths if item.lower() != data_zip_snapshot.lower()]
                if len(next_paths) != len(paths):
                    self.save_tracked_data_zips(next_paths)
                    print(f"Removed data mod ZIP from Mod_index: {data_zip_snapshot}")

            cached_mods = state.get(GITHUB_MODS_KEY, {})
            if not isinstance(cached_mods, dict):
                cached_mods = {}
                state[GITHUB_MODS_KEY] = cached_mods
            for cached_repo, cached_mod in list(cached_mods.items()):
                if not isinstance(cached_repo, str):
                    continue
                same_repo = repo_snapshot and cached_repo == repo_snapshot
                same_mod = isinstance(cached_mod, dict) and str(cached_mod.get("id", "")) == mod_id
                if isinstance(cached_mod, list):
                    same_mod = any(isinstance(item, dict) and str(item.get("id", "")) == mod_id for item in cached_mod)
                if same_repo or same_mod:
                    cached_mods.pop(cached_repo, None)
                    removed_repos.add(cached_repo)

            statuses = state.get(GITHUB_STATUS_KEY, {})
            if not isinstance(statuses, dict):
                statuses = {}
                state[GITHUB_STATUS_KEY] = statuses
            for removed_repo in removed_repos:
                statuses.pop(removed_repo, None)

            bmm.save_state(self.rt, state)
            self.write_mod_index_from_state(state)

            removed_cache = []
            for path in cache_paths:
                if path.exists() and path.is_file():
                    path.unlink()
                    removed_cache.append(path)
            if removed_cache:
                print("Deleted cached download files:")
                for path in removed_cache:
                    print(f"  {path}")
            print(f"Removed {mod_id} from BMM.")
            return 0

        self.run_task(f"Remove {mod_id}", task, refresh=True)

    def enable_selected(self) -> None:
        mod_id = self.require_selected_mod_id()
        if not mod_id:
            return
        if self.toggle_external_selected(True):
            return

        def task() -> int:
            return bmm.command_enable(self.make_args(mod_id=mod_id))

        self.run_task(f"Enable {mod_id}", task, refresh=True)

    def disable_selected(self) -> None:
        mod_id = self.require_selected_mod_id()
        if not mod_id:
            return
        if self.toggle_external_selected(False):
            return

        def task() -> int:
            return bmm.command_disable(self.make_args(mod_id=mod_id))

        self.run_task(f"Disable {mod_id}", task, refresh=True)

    def open_selected_website(self) -> None:
        mod = self.selected_mod()
        if not mod:
            messagebox.showinfo(APP_TITLE, "Select a mod first.")
            return
        url = str(mod.get("website", "")).strip()
        if not url:
            messagebox.showinfo(APP_TITLE, "This mod has no website URL in the index.")
            return
        webbrowser.open(url)

    def save_profile(self) -> None:
        name = simpledialog.askstring(APP_TITLE, "Profile name:", parent=self.root)
        if not name:
            return
        name = name.strip()
        if not name:
            return

        def task() -> int:
            return bmm.command_profile_save(self.make_args(name=name))

        self.run_task(f"Save profile {name}", task, refresh=True)

    def apply_profile(self) -> None:
        name = self.profile_var.get().strip()
        if not name:
            messagebox.showinfo(APP_TITLE, "No profile selected.")
            return
        if not messagebox.askyesno(APP_TITLE, f"Apply profile {name}? This renames BMM-managed DLLs to match the saved enabled state."):
            return

        def task() -> int:
            return bmm.command_profile_apply(self.make_args(name=name, yes=True, disable_extra=False))

        self.run_task(f"Apply profile {name}", task, refresh=True)

    def require_selected_mod_id(self) -> str | None:
        mod_id = self.selected_mod_id()
        if not mod_id:
            messagebox.showinfo(APP_TITLE, "Select a mod first.")
            return None
        return mod_id


def smoke_test(index: str | None = None, data_dir: str | None = None) -> int:
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    index_path = Path(index).expanduser() if index else app_dir / MOD_INDEX_DIR_NAME / MOD_INDEX_FILE_NAME
    if not index_path.exists():
        index_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            index_path,
            {
                "schema": bmm.INDEX_SCHEMA,
                "game": {
                    "id": "ostranauts",
                    "name": "Ostranauts",
                },
                "updated_utc": bmm.stamp(),
                "mods": [],
            },
        )
    loaded = bmm.load_index(str(index_path), {})
    errors = bmm.validate_index(loaded)
    if errors:
        print("GUI smoke test failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"GUI smoke test OK: {len(bmm.get_mods(loaded))} mods")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="BMM desktop UI")
    parser.add_argument("--data-dir", help="BMM data directory.")
    parser.add_argument("--game-dir", help="Ostranauts game directory override.")
    parser.add_argument("--smoke-test", action="store_true", help="Load the Mod_index index and exit without opening the UI.")
    args = parser.parse_args(argv)
    if args.smoke_test:
        return smoke_test(data_dir=args.data_dir)
    root = tk.Tk()
    BmmGui(root, data_dir=args.data_dir, game_dir=args.game_dir)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
