#!/usr/bin/env python3
"""Desktop window for the Pocket Rogues save tools.

Double-click "Pocket Rogues Saves.cmd" (or run this file) to open it. Everything the
command-line tools do is here: back up, watch and auto-revive, revive, restore any
backup, and convert saves between Android XML, Windows .reg, and .prs/.json.

The window only drives save_manager.py and prefs_convert.py; the rules live there.
"""
import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import prefs_convert  # noqa: E402
import save_manager as sm  # noqa: E402

INDEX_FILE = os.path.join(sm.BACKUP_DIR, ".index.json")   # cached per-backup details
LOG_FILE = os.path.join(HERE, "save_manager_ui.log")      # everything the Activity pane shows, plus errors
STATUS_EVERY_MS = 3000
LOG_LINES = 400
INDEX_VERSION = 7          # bump when describe() in save_manager gains fields; old caches are rebuilt

# ---------------------------------------------------------------- backup details

def load_index() -> dict:
    try:
        with open(INDEX_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("_version") != INDEX_VERSION:
        return {}
    return {k: v for k, v in data.items() if k != "_version"}


def save_index(index: dict):
    os.makedirs(sm.BACKUP_DIR, exist_ok=True)
    tmp = INDEX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"_version": INDEX_VERSION, **index}, fh)
    os.replace(tmp, INDEX_FILE)


def hours(minutes) -> str:
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        return "?"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def describe_lines(info: dict, live: dict = None):
    """Rows of (label, value, change-vs-live) for the Details panel."""
    def delta(key, sub=None):
        if not live:
            return ""
        a = info.get(key, {}).get(sub) if sub else info.get(key)
        b = live.get(key, {}).get(sub) if sub else live.get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or a == b:
            return ""
        d = a - b
        return f"{'+' if d > 0 else ''}{d} vs live"

    tier = info.get("kills_by_tier", {})
    cause = info.get("deaths_by_cause", {})
    areas = info.get("best_floor_by_area", {})
    reached = ", ".join(f"{a} {f}" for a, f in areas.items() if f) or "none yet"
    where = scene_name(info.get("scene")) or "?"
    if info.get("load_point"):
        where += "  (load point: the game starts here)"
    elif info.get("resumable"):
        where += "  (checkpoint)"
    return [
        ("Hero", None, None),
        ("Class", info.get("hero_name", f"hero {info.get('hero', '?')}"), ""),
        ("Level", info.get("level", "?"), delta("level")),
        ("Where", where, ""),
        ("Floor", f"{info.get('depth', '?')}" + ("  (endless)" if info.get("endless") else ""), delta("depth")),
        ("Gold", f"{info.get('gold', 0):,}   (gems {info.get('gems', 0)})", delta("gold")),
        ("Coins collected this life", f"{info.get('run_coins', 0):,}", delta("run_coins")),
        ("Kills this life", f"{info.get('run_kills', 0):,}", delta("run_kills")),
        ("Floors cleared this life", info.get("run_floors", 0), delta("run_floors")),
        ("Time this life", hours(info.get("run_minutes")), ""),
        ("Unspent skill points", info.get("skill_points", 0), delta("skill_points")),
        ("Lifetime, this class", None, None),
        ("Guild level", info.get("guild_level", "?"), delta("guild_level")),
        ("Raids", f"{info.get('all_raids', 0)}  ({info.get('raids_no_death', 0)} without dying)", delta("all_raids")),
        ("Heroes lost", info.get("all_deaths", 0), delta("all_deaths")),
        ("    by cause", f"monsters {cause.get('monster', 0)}, bosses {cause.get('bosses', 0)}, traps {cause.get('traps', 0)}, "
                       f"effects {cause.get('effects', 0)}, left behind {cause.get('leaved', 0)}", ""),
        ("Kills", f"{info.get('all_kills', 0):,}", delta("all_kills")),
        ("    by tier", f"normal {tier.get('normal', 0):,}, upper {tier.get('upper', 0)}, elite {tier.get('elite', 0)}, "
                      f"champions {tier.get('champions', 0)}, bosses {tier.get('bosses', 0)}", ""),
        ("Gold earned", f"{info.get('all_coins', 0):,}", delta("all_coins")),
        ("Floors cleared", info.get("all_floors", 0), delta("all_floors")),
        ("Deepest floor", info.get("best_floor", 0), delta("best_floor")),
        ("Best score", f"{info.get('best_score', 0):,}", delta("best_score")),
        ("Deepest by area", reached, ""),
        ("Secrets found", info.get("secrets", 0), delta("secrets")),
        ("Quests done", info.get("quests", 0), delta("quests")),
        ("Play time", hours(info.get("all_minutes")), ""),
        ("All classes", None, None),
        ("Total heroes lost", info.get("deaths", 0), delta("deaths")),
        ("Levels", ", ".join(f"{c} {lv}" for c, lv in info.get("level_by_class", {}).items() if lv) or "none yet", ""),
        ("Skill points", ", ".join(f"{c} {pt}" for c, pt in info.get("skill_points_by_class", {}).items() if pt) or "none", ""),
    ]


SCENE_NAMES = {"Base": "Fortress", "_shop": "Camp", "floor_prison": "Abandoned Prison", "floor_dirtDung": "Dirt Dungeon",
               "locMini_beastLair": "Predatory Lair", "_miniBoss_SUN_MONK": "Monk of the Sun Cult"}


def scene_name(scene: str) -> str:
    """Readable name for a scene id ('_treasury6' -> 'Treasury', 'floor_prison' -> 'Abandoned Prison')."""
    if not scene:
        return ""
    if scene in SCENE_NAMES:
        return SCENE_NAMES[scene]
    name = re.sub(r"^(floor_|locMini_|_miniBoss_|_)", "", scene)
    name = re.sub(r"\d+$", "", name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ")
    return name.title()


def when_text(stamp: str) -> str:
    """Today's backups show just the time; older ones show 'Sep 12 12:08'."""
    try:
        t = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return stamp
    if t.date() == datetime.now().date():
        return t.strftime("%H:%M:%S")
    return t.strftime("%b %d %H:%M")


def row_kind(info: dict, label: str) -> str:
    """Type column: load points by content, everything else by the label the watcher or a command gave the file."""
    if info.get("load_point"):
        return "load point"
    if label == "checkpoint":
        return "checkpoint"
    if label.startswith("pre-"):
        return label.replace("pre-", "before ")
    if label.startswith(("revive", "rewind", "before-death", "death")):
        return label.split("-")[0] if not label.startswith("before") else "before death"
    return "" if label == "auto" else label


HOW_SAVING_WORKS = (
    "Pocket Rogues keeps its save in the Windows registry and writes it in two layers.\n\n"
    "Gold, gems, kills, XP and skill points are written within a second of every change. "
    "They are always in the newest backup.\n\n"
    "Your position, the floor layout, the monsters alive and your bag are written only when the game "
    "saves the dungeon: on entering or leaving a floor or side room, and whenever you use Save and Exit "
    "in the pause menu. Items found since the last of those are in no backup.\n\n"
    "So: use Save and Exit after finding anything you care about. It makes a load point, shown in blue: the game starts straight back into that floor at that spot with everything you carried. A revive or rewind from this manager makes one too.\n\n"
    "Watching snapshots every change. If a hero dies and you quit within 3 minutes, Revive runs by itself: "
    "the last dungeon save, the gold and XP from a second before the death, and the gear you wore when you died."
)


MODE_NAMES = {"xml2reg": "Android XML to Windows .reg", "reg2xml": "Windows .reg to Android XML",
              "prs2json": "PRogues.prs to editable JSON", "json2prs": "JSON back to PRogues.prs"}
MODE_IDS = {v: k for k, v in MODE_NAMES.items()}


def parse_name(path: str):
    """Backup file name -> ('YYYY-MM-DD HH:MM:SS', label). Millisecond suffixes are dropped."""
    m = sm.STAMP_RE.match(os.path.basename(path))
    if not m:
        return os.path.basename(path)[:-4], ""
    return f"{m.group(1)} {m.group(2).replace('-', ':')}", m.group(3) or ""


# ---------------------------------------------------------------- dialogs

class Dialog(tk.Toplevel):
    """A small modal drawn by the app itself, centred on the main window."""

    def __init__(self, parent, title, text, buttons, danger=None):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        self.transient(parent)
        self.result = None
        body = ttk.Frame(self, padding=(20, 16))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=text, wraplength=520, justify="left").pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=(16, 0))
        for i, name in enumerate(buttons):
            style = "Danger.TButton" if name == danger else "TButton"
            b = ttk.Button(row, text=name, style=style, command=lambda n=name: self._pick(n))
            b.pack(side="right", padx=(8, 0))
            if i == 0:
                b.focus_set()
        self.bind("<Escape>", lambda e: self._pick(buttons[-1]))
        self.bind("<Return>", lambda e: self._pick(buttons[0]))
        self.protocol("WM_DELETE_WINDOW", lambda: self._pick(buttons[-1]))
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.grab_set()
        self.wait_window()

    def _pick(self, name):
        self.result = name
        self.destroy()


def confirm(parent, title, text, yes="Yes", no="Cancel", danger=None) -> bool:
    return Dialog(parent, title, text, [yes, no], danger=danger).result == yes


def notice(parent, title, text):
    Dialog(parent, title, text, ["OK"])


# ---------------------------------------------------------------- main window

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Pocket Rogues Saves")
        self.minsize(900, 600)
        self.geometry("1180x760")
        self._style()

        self.events = queue.Queue()          # (kind, payload) from worker threads
        self.watch_stop = None               # threading.Event while the watcher runs
        self.watch_thread = None
        self.busy = False                    # a backup/restore/revive is running
        self.index = load_index()
        self.rows = {}                       # tree item id -> backup path
        self.detail_thread = None

        self._build()
        self.report_callback_exception = self._callback_error
        self._refresh_list()
        self.after(100, self._pump)
        self.after(200, self._refresh_status)
        self.protocol("WM_DELETE_WINDOW", self._close)

    # ---- look

    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista")
        except tk.TclError:
            pass
        base = ("Segoe UI", 10)
        self.option_add("*Font", base)
        st.configure("TButton", padding=(12, 6))
        st.configure("Big.TButton", font=("Segoe UI", 11, "bold"), padding=(16, 10))
        st.configure("Danger.TButton", foreground="#a11")
        st.configure("Status.TLabel", font=("Segoe UI", 10))
        st.configure("Strong.TLabel", font=("Segoe UI", 10, "bold"))
        st.configure("Treeview", rowheight=24)

    # ---- layout

    def _build(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=10, pady=10)
        saves = ttk.Frame(nb, padding=10)
        timeline = ttk.Frame(nb, padding=10)
        conv = ttk.Frame(nb, padding=10)
        nb.add(saves, text="  Saves  ")
        nb.add(timeline, text="  Timeline  ")
        nb.add(conv, text="  Convert  ")
        self._build_saves(saves)
        self._build_timeline(timeline)
        self._build_convert(conv)
        nb.bind("<<NotebookTabChanged>>", lambda e: self._draw_timeline() if nb.index("current") == 1 else None)
        self.nb = nb

    def _build_saves(self, root):
        # status strip
        strip = ttk.Frame(root)
        strip.pack(fill="x")
        self.var_game = tk.StringVar(value="Game: checking...")
        self.var_deaths = tk.StringVar(value="Deaths: ?")
        self.var_newest = tk.StringVar(value="Newest backup: ?")
        self.var_watch = tk.StringVar(value="Watcher: off")
        for var in (self.var_game, self.var_deaths, self.var_newest, self.var_watch):
            ttk.Label(strip, textvariable=var, style="Status.TLabel").pack(side="left", padx=(0, 24))
        ttk.Button(strip, text="How saving works", command=lambda: notice(self, "How saving works", HOW_SAVING_WORKS)
                   ).pack(side="right")
        ttk.Button(strip, text="Open Folder", command=self.open_folder).pack(side="right", padx=(0, 6))
        ttk.Button(strip, text="Refresh", command=self._refresh_list).pack(side="right", padx=(0, 6))

        # action bar
        acts = ttk.Frame(root)
        acts.pack(fill="x", pady=(10, 10))
        self.btn_backup = ttk.Button(acts, text="Backup Now", style="Big.TButton", command=self.do_backup)
        self.btn_backup.pack(side="left")
        ttk.Label(acts, text="Label").pack(side="left", padx=(10, 4))
        self.var_label = tk.StringVar()
        ttk.Entry(acts, textvariable=self.var_label, width=16).pack(side="left")
        ttk.Separator(acts, orient="vertical").pack(side="left", fill="y", padx=16)
        self.btn_watch = ttk.Button(acts, text="Start Watching", style="Big.TButton", command=self.toggle_watch)
        self.btn_watch.pack(side="left")
        self.var_revive = tk.BooleanVar(value=True)
        ttk.Checkbutton(acts, text="auto-revive after a death", variable=self.var_revive).pack(side="left", padx=(10, 0))
        ttk.Separator(acts, orient="vertical").pack(side="left", fill="y", padx=16)
        self.btn_revive = ttk.Button(acts, text="Revive", style="Big.TButton", command=self.do_revive)
        self.btn_revive.pack(side="left")

        # list + details side by side, activity underneath
        vpane = ttk.PanedWindow(root, orient="vertical")
        vpane.pack(fill="both", expand=True)
        upper = ttk.Frame(vpane)
        lower = ttk.Frame(vpane)
        vpane.add(upper, weight=6)
        vpane.add(lower, weight=1)
        hpane = ttk.PanedWindow(upper, orient="horizontal")
        hpane.pack(fill="both", expand=True)
        left = ttk.Frame(hpane)
        right = ttk.Frame(hpane)
        hpane.add(left, weight=3)
        hpane.add(right, weight=2)
        self.hpane = hpane
        hpane.bind("<B1-Motion>", lambda e: self.after_idle(self._clamp_sash), add="+")
        hpane.bind("<ButtonRelease-1>", lambda e: self.after_idle(self._clamp_sash), add="+")
        hpane.bind("<Configure>", lambda e: self.after_idle(self._clamp_sash), add="+")

        # toolbar over the list
        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(0, 6))
        self.btn_restore = ttk.Button(bar, text="Restore", command=self.do_restore)
        self.btn_restore.pack(side="left")
        self.btn_rewind = ttk.Button(bar, text="Rewind here, keep progress", command=self.do_rewind)
        self.btn_rewind.pack(side="left", padx=(6, 0))
        self.btn_delete = ttk.Button(bar, text="Delete", style="Danger.TButton", command=self.do_delete)
        self.btn_delete.pack(side="left", padx=(6, 0))

        cols = ("when", "kind", "hero", "scene", "depth", "level", "gold", "deaths")
        grid = ttk.Frame(left)
        self.tree = ttk.Treeview(grid, columns=cols, show="headings", selectmode="browse")
        heads = {"when": ("When", 100, "w"), "kind": ("Type", 100, "w"), "hero": ("Class", 90, "w"),
                 "scene": ("Where", 150, "w"), "depth": ("Floor", 48, "center"), "level": ("Lvl", 44, "center"),
                 "gold": ("Gold", 70, "e"), "deaths": ("Deaths", 56, "center")}
        for c in cols:
            text, width, anchor = heads[c]
            self.tree.heading(c, text=text, command=lambda c=c: self._sort_by(c))
            self.tree.column(c, width=width, minwidth=width, anchor=anchor, stretch=(c == "scene"))
        grid.pack(fill="both", expand=True)
        grid.rowconfigure(0, weight=1)
        grid.columnconfigure(0, weight=1)
        sb = ttk.Scrollbar(grid, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(grid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.tree.tag_configure("checkpoint", foreground="#0a5")
        self.tree.tag_configure("marker", foreground="#a60")
        self.tree.tag_configure("saveexit", foreground="#06c")
        self.tree.bind("<Double-1>", lambda e: self.do_restore())
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.sort_col, self.sort_desc = "when", True
        self.col_titles = {c: heads[c][0] for c in cols}

        # details
        self.var_detail_title = tk.StringVar(value="Live save")
        ttk.Label(right, textvariable=self.var_detail_title, style="Strong.TLabel").pack(anchor="w", padx=(10, 0), pady=(8, 8))
        dgrid = ttk.Frame(right)
        dgrid.pack(fill="both", expand=True, padx=(10, 0))
        dgrid.rowconfigure(0, weight=1)
        dgrid.columnconfigure(0, weight=1)
        self.detail = ttk.Treeview(dgrid, columns=("k", "v", "d"), show="headings", selectmode="none")
        for c, text, width, anchor in (("k", "Field", 180, "w"), ("v", "Value", 420, "w"), ("d", "Change vs live", 120, "e")):
            self.detail.heading(c, text=text, anchor=anchor)
            self.detail.column(c, width=width, minwidth=60, anchor=anchor, stretch=(c == "v"))
        self.detail.tag_configure("head", background="#e9eef5", foreground="#3b4a5c")
        self.detail.tag_configure("up", foreground="#0a5")
        self.detail.tag_configure("down", foreground="#a11")
        dsb = ttk.Scrollbar(dgrid, orient="vertical", command=self.detail.yview)
        dhsb = ttk.Scrollbar(dgrid, orient="horizontal", command=self.detail.xview)
        self.detail.configure(yscrollcommand=dsb.set, xscrollcommand=dhsb.set)
        self.detail.grid(row=0, column=0, sticky="nsew")
        dsb.grid(row=0, column=1, sticky="ns")
        dhsb.grid(row=1, column=0, sticky="ew")
        self.live_info = None

        # activity
        ttk.Label(lower, text="Activity", style="Strong.TLabel").pack(anchor="w", pady=(10, 4))
        self.log_box = tk.Text(lower, height=4, wrap="word", state="disabled", font=("Consolas", 10),
                               relief="flat", background="#f3f4f6", padx=8, pady=6)
        lsb = ttk.Scrollbar(lower, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y")
        self.log_box.pack(side="left", fill="both", expand=True)

    LIST_MIN = 560   # the list pane never gets narrower than its toolbar
    DETAIL_MIN = 300

    def _clamp_sash(self):
        try:
            total = self.hpane.winfo_width()
            pos = self.hpane.sashpos(0)
        except tk.TclError:
            return
        lo, hi = self.LIST_MIN, max(self.LIST_MIN, total - self.DETAIL_MIN)
        if pos < lo:
            self.hpane.sashpos(0, lo)
        elif pos > hi:
            self.hpane.sashpos(0, hi)

    def _build_timeline(self, root):
        ttk.Label(root, text="Your last backups, oldest on the left", style="Strong.TLabel").pack(anchor="w")
        ttk.Label(root, foreground="#555", wraplength=880, justify="left", text=(
            "Bars: dungeon floor at each backup. Line: gold carried by the hero. Red marks: a hero died "
            "between that backup and the one before it. Hover a bar to see the backup it came from.")).pack(anchor="w", pady=(4, 8))
        self.canvas = tk.Canvas(root, background="white", highlightthickness=1, highlightbackground="#ccc")
        self.canvas.pack(fill="both", expand=True)
        self.var_hover = tk.StringVar()
        ttk.Label(root, textvariable=self.var_hover).pack(anchor="w", pady=(6, 0))
        self.canvas.bind("<Configure>", lambda e: self._draw_timeline())
        self.canvas.bind("<Motion>", self._timeline_hover)
        self.timeline_points = []

    def _draw_timeline(self):
        c = self.canvas
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        files = [p for p in reversed(sm.backups()) if os.path.basename(p) in self.index
                 and "error" not in self.index[os.path.basename(p)]]
        files = files[-200:]
        if len(files) < 2:
            c.create_text(w // 2, h // 2, text="Not enough backups read yet", fill="#888")
            return
        infos = [self.index[os.path.basename(p)] for p in files]
        left, right, top, bottom = 50, w - 60, 20, h - 30
        n = len(files)
        step = (right - left) / n
        max_floor = max(1, max(i.get("depth", 0) for i in infos))
        max_gold = max(1, max(i.get("gold", 0) for i in infos))
        self.timeline_points = []
        # axes
        c.create_line(left, bottom, right, bottom, fill="#888")
        c.create_line(left, top, left, bottom, fill="#888")
        c.create_line(right, top, right, bottom, fill="#888")
        c.create_text(left - 6, top, text=f"floor {max_floor}", anchor="e", fill="#36c", font=("Segoe UI", 8))
        c.create_text(right + 6, top, text=f"{max_gold:,} gold", anchor="w", fill="#c80", font=("Segoe UI", 8))
        c.create_text(left - 6, bottom, text="0", anchor="e", fill="#888", font=("Segoe UI", 8))
        prev = None
        for i, (path, info) in enumerate(zip(files, infos)):
            x0 = left + i * step
            x1 = x0 + max(step - 1, 1)
            fh = (bottom - top) * info.get("depth", 0) / max_floor
            colour = "#3a7" if info.get("resumable") else "#9bd"
            c.create_rectangle(x0, bottom - fh, x1, bottom, fill=colour, outline="")
            gy = bottom - (bottom - top) * info.get("gold", 0) / max_gold
            gx = (x0 + x1) / 2
            if prev:
                c.create_line(prev[0], prev[1], gx, gy, fill="#c80", width=2)
            prev = (gx, gy)
            if i and info.get("deaths", 0) > infos[i - 1].get("deaths", 0):
                c.create_line(gx, top, gx, bottom, fill="#e33", width=2, dash=(3, 3))
                c.create_text(gx + 3, top + 2, text="died", fill="#e33", anchor="nw", font=("Segoe UI", 8))
            self.timeline_points.append((x0, x1, path, info))
        c.create_text(left, bottom + 14, text=when_text(parse_name(files[0])[0]), anchor="w", fill="#666", font=("Segoe UI", 8))
        c.create_text(right, bottom + 14, text=when_text(parse_name(files[-1])[0]), anchor="e", fill="#666", font=("Segoe UI", 8))

    def _timeline_hover(self, event):
        for x0, x1, path, info in self.timeline_points:
            if x0 <= event.x <= x1:
                when, label = parse_name(path)
                self.var_hover.set(f"{when_text(when)}  {label or ''}   {info.get('hero_name', '')} floor {info.get('depth', '?')}, "
                                   f"level {info.get('level', '?')}, gold {info.get('gold', 0):,}, "
                                   f"{scene_name(info.get('scene', ''))}")
                return
        self.var_hover.set("")

    def _build_convert(self, root):
        ttk.Label(root, text="Convert a save file", style="Strong.TLabel").pack(anchor="w")
        ttk.Label(root, wraplength=880, foreground="#555", justify="left", text=(
            "Pick the file to convert; the direction and output name fill in from its extension. "
            "For .xml and .reg you can also give a base file: entries the input does not mention are "
            "kept from it, so Windows-only settings survive a conversion.")).pack(anchor="w", pady=(4, 14))

        grid = ttk.Frame(root)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self.var_src = tk.StringVar()
        self.var_dst = tk.StringVar()
        self.var_base = tk.StringVar()
        self.var_mode = tk.StringVar()
        self.var_src.trace_add("write", lambda *a: self._guess_output())

        def row(r, text, var, browse):
            ttk.Label(grid, text=text).grid(row=r, column=0, sticky="w", pady=6, padx=(0, 10))
            ttk.Entry(grid, textvariable=var).grid(row=r, column=1, sticky="ew")
            ttk.Button(grid, text="Browse...", command=browse).grid(row=r, column=2, padx=(8, 0))

        row(0, "Input file", self.var_src, lambda: self._browse(self.var_src, False))
        ttk.Label(grid, text="Direction").grid(row=1, column=0, sticky="w", pady=6)
        self.mode_box = ttk.Combobox(grid, textvariable=self.var_mode, values=list(MODE_NAMES.values()),
                                     state="readonly", width=30)
        self.mode_box.grid(row=1, column=1, sticky="w")
        row(2, "Output file", self.var_dst, lambda: self._browse(self.var_dst, True))
        row(3, "Base file (optional)", self.var_base, lambda: self._browse(self.var_base, False))

        ttk.Button(root, text="Convert", style="Big.TButton", command=self.do_convert).pack(anchor="w", pady=(16, 8))
        self.var_conv_result = tk.StringVar()
        ttk.Label(root, textvariable=self.var_conv_result, wraplength=880, justify="left").pack(anchor="w")

    # ---- helpers

    def log(self, text):
        stamp = datetime.now().strftime("%H:%M:%S")
        lines = str(text).splitlines() or [""]
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                for line in lines:
                    fh.write(f"{datetime.now():%Y-%m-%d} {stamp}  {line}\n")
        except OSError:
            pass
        self.log_box.configure(state="normal")
        for line in lines:
            self.log_box.insert("end", f"{stamp}  {line}\n")
        extra = int(self.log_box.index("end-1c").split(".")[0]) - LOG_LINES
        if extra > 0:
            self.log_box.delete("1.0", f"{extra + 1}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _callback_error(self, exc_type, exc, tb):
        """Tk swallows exceptions raised inside button handlers and timers; show them instead."""
        import traceback
        self.log("ERROR: " + "".join(traceback.format_exception(exc_type, exc, tb)).rstrip())

    def _pump(self):
        """Deliver worker-thread events on the UI thread."""
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self.log(payload)
                elif kind == "detail":
                    path, info = payload
                    self.index[os.path.basename(path)] = info
                    self._fill_row(path)
                elif kind == "details-done":
                    save_index(self.index)
                    self._apply_sort()
                    if any(os.path.basename(p) not in self.index for p in self.rows.values()):
                        self._refresh_list()   # files that arrived while the reader was busy
                elif kind == "done":
                    self.busy = False
                    self._set_buttons()
                    self._refresh_list()
                    if payload:
                        notice(self, *payload)
                elif kind == "watch-ended":
                    self.watch_stop = self.watch_thread = None
                    self.btn_watch.configure(text="Start Watching")
                    self.var_watch.set("Watcher: off")
                    self._set_buttons()
                    self._refresh_list()
        except queue.Empty:
            pass
        self.after(100, self._pump)

    def _refresh_status(self):
        try:
            self._read_status()
        except Exception:  # noqa: BLE001 - the poll must survive anything
            self._callback_error(*sys.exc_info())
        finally:
            self.after(STATUS_EVERY_MS, self._refresh_status)

    def _read_status(self):
        try:
            running = sm.game_running()
            live = sm.export_key()
            self.var_game.set("Game: RUNNING" if running else "Game: not running")
            self.var_deaths.set(f"Deaths: {sm.deaths(live)}")
            self.live_info = sm.describe(prefs_convert.read_reg_bytes(live))
            if not self._selected():
                self._show_details()
        except (sm.SaveError, OSError) as exc:
            self.var_game.set("Game: ?")
            self.var_deaths.set(f"Deaths: ? ({exc})")
        files = sm.backups()
        self.var_newest.set(f"Newest backup: {when_text(parse_name(files[0])[0]) if files else 'none'}")
        if len(files) != len(self.rows):
            self._refresh_list()
        self._set_buttons()

    def _set_buttons(self):
        idle = "normal" if not self.busy else "disabled"
        self.btn_backup.configure(state=idle)
        self.btn_watch.configure(state=idle)
        self.btn_revive.configure(state=idle)
        has_pick = bool(self.tree.selection())
        self.btn_restore.configure(state="normal" if not self.busy and has_pick else "disabled")
        self.btn_delete.configure(state="normal" if not self.busy and has_pick else "disabled")
        pick = self._selected()
        info = self.index.get(os.path.basename(pick)) if pick else None
        can_rewind = bool(info) and info.get("has_layout") and not self.busy
        self.btn_rewind.configure(state="normal" if can_rewind else "disabled")

    def _run_bg(self, fn, *args):
        """Run a save_manager command off the UI thread; log its output; report when done."""
        self.busy = True
        self._set_buttons()

        def work():
            note = None
            try:
                fn(*args, log=lambda t: self.events.put(("log", t)))
            except sm.SaveError as exc:
                note = ("Could not do that", str(exc))
                self.events.put(("log", f"ERROR: {exc}"))
            except Exception as exc:  # noqa: BLE001 - surface anything, never die silently
                note = ("Unexpected error", f"{type(exc).__name__}: {exc}")
                self.events.put(("log", f"ERROR: {type(exc).__name__}: {exc}"))
            self.events.put(("done", note))

        threading.Thread(target=work, daemon=True).start()

    def _selected(self):
        sel = self.tree.selection()
        return self.rows.get(sel[0]) if sel else None

    def _on_select(self, event=None):
        self._set_buttons()
        self._show_details()

    def _show_details(self):
        path = self._selected()
        if path:
            info = self.index.get(os.path.basename(path))
            when, label = parse_name(path)
            self.var_detail_title.set(f"Backup {when_text(when)} {label}".rstrip() + "   (change vs live)")
        else:
            info = self.live_info
            self.var_detail_title.set("Live save (select a backup to compare)")
        self.detail.delete(*self.detail.get_children())
        if not info or "error" in info:
            self.detail.insert("", "end", values=("", "still reading..." if info is None else "unreadable", ""))
            return
        for label, value, change in describe_lines(info, self.live_info if path else None):
            if value is None:
                self.detail.insert("", "end", values=(label, "", ""), tags=("head",))
                continue
            tag = ("up",) if change.startswith("+") else ("down",) if change.startswith("-") else ()
            self.detail.insert("", "end", values=(label, value, change), tags=tag)

    # ---- backup list

    NUMERIC = {"depth", "level", "gold", "deaths"}

    def _sort_by(self, col):
        if col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, col in self.NUMERIC or col == "when"
        self._apply_sort()

    def _apply_sort(self):
        col = self.sort_col
        idx = list(self.tree["columns"]).index(col)

        def key(iid):
            raw = self.tree.item(iid)["values"][idx]
            if col == "when":
                return (0, self.rows[iid])             # the file name carries the full date and time
            if col in self.NUMERIC:
                try:
                    return (0, float(str(raw).replace(",", "")))
                except ValueError:
                    return (1, 0.0)                    # still reading: sorts last
            return (0, str(raw).lower())

        items = sorted(self.tree.get_children(), key=key, reverse=self.sort_desc)
        for pos, iid in enumerate(items):
            self.tree.move(iid, "", pos)
        for c, title in self.col_titles.items():
            arrow = ("  ▾" if self.sort_desc else "  ▴") if c == col else ""
            self.tree.heading(c, text=title + arrow)

    def _refresh_list(self):
        pick = self._selected()
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        files = sm.backups()
        missing = []
        for path in files:
            iid = self.tree.insert("", "end", values=("", "", "", "", "", ""))
            self.rows[iid] = path
            if os.path.basename(path) in self.index:
                self._fill_row(path)
            else:
                self._fill_row(path, pending=True)
                missing.append(path)
            if path == pick:
                self.tree.selection_set(iid)
        self._set_buttons()
        self._show_details()
        self._apply_sort()
        if missing and (self.detail_thread is None or not self.detail_thread.is_alive()):
            self.detail_thread = threading.Thread(target=self._compute_details, args=(missing,), daemon=True)
            self.detail_thread.start()

    def _compute_details(self, paths):
        for path in paths:
            try:
                info = sm.snapshot_info(path)
            except Exception as exc:  # noqa: BLE001 - one bad file must not stop the list
                info = {"error": str(exc)}
            self.events.put(("detail", (path, info)))
        self.events.put(("details-done", None))

    def _fill_row(self, path, pending=False):
        iid = next((i for i, p in self.rows.items() if p == path), None)
        if iid is None:
            return
        when, label = parse_name(path)
        when = when_text(when)
        info = self.index.get(os.path.basename(path))
        if pending or info is None:
            self.tree.item(iid, values=(when, label, "", "reading...", "", "", "", ""))
            return
        if "error" in info:
            self.tree.item(iid, values=(when, label, "", "unreadable", "", "", "", ""))
            return
        kind = row_kind(info, label)
        tags = ()
        if kind == "load point":
            tags = ("saveexit",)
        elif kind == "checkpoint":
            tags = ("checkpoint",)
        elif kind.startswith("before"):
            tags = ("marker",)
        self.tree.item(iid, values=(when, kind, info.get("hero_name", ""), scene_name(info.get("scene", "")),
                                    info.get("depth", ""), info.get("level", ""), f"{info.get('gold', 0):,}",
                                    info.get("deaths", "")), tags=tags)
        if self._selected() == path:
            self._show_details()

    # ---- actions

    def do_backup(self):
        label = self.var_label.get().strip()
        self.var_label.set("")
        self.log(f"backing up{' as ' + label if label else ''}...")
        self._run_bg(sm.cmd_backup, label, False)

    def do_restore(self):
        path = self._selected()
        if not path or self.busy:
            return
        name = os.path.basename(path)
        if sm.game_running():
            notice(self, "Game is running", "Close Pocket Rogues first. The game rewrites its save when it exits "
                                            "and would wipe the restore.")
            return
        if not confirm(self, "Restore this backup?",
                       f"Replace the live save with\n{name}\n\nThe current save is kept as a pre-restore backup first.",
                       yes="Restore", danger="Restore"):
            return
        self._with_watcher_paused(lambda: self._run_bg(sm.cmd_restore, path, False))

    def do_revive(self):
        if self.busy:
            return
        if sm.game_running():
            notice(self, "Game is running", "Close Pocket Rogues first, then revive.")
            return
        if not confirm(self, "Revive?",
                       "Find the newest death in the backups and restore the last safe point before it.\n\n"
                       "The current save is kept as a pre-revive backup first.", yes="Revive", danger="Revive"):
            return
        self._with_watcher_paused(lambda: self._run_bg(sm.cmd_revive, False))

    def do_rewind(self):
        path = self._selected()
        if not path or self.busy:
            return
        if sm.game_running():
            notice(self, "Game is running", "Close Pocket Rogues first, then rewind.")
            return
        when, label = parse_name(path)
        info = self.index.get(os.path.basename(path), {})
        newest = sm.backups()[0]
        if not confirm(self, "Rewind?",
                       f"Load the floor and position from {when_text(when)} {label}\n({scene_name(info.get('scene', '?'))}, floor {info.get('depth', '?')}) "
                       f"but keep gold, gear, XP and counters from the newest backup\n{parse_name(newest)[0]}.\n\n"
                       "Monsters alive on that floor at that time come back. The current save is kept as a pre-rewind backup.",
                       yes="Rewind", danger="Rewind"):
            return
        self._with_watcher_paused(lambda: self._run_bg(sm.cmd_rewind, path, newest, False))

    def do_delete(self):
        path = self._selected()
        if not path or self.busy:
            return
        name = os.path.basename(path)
        if not confirm(self, "Delete this backup?", f"Delete {name} for good?", yes="Delete", danger="Delete"):
            return
        try:
            os.remove(path)
            self.index.pop(name, None)
            self.log(f"deleted {name}")
        except OSError as exc:
            notice(self, "Could not delete", str(exc))
        self._refresh_list()

    def open_folder(self):
        os.makedirs(sm.BACKUP_DIR, exist_ok=True)
        subprocess.Popen(["explorer", sm.BACKUP_DIR])

    # ---- watcher

    def toggle_watch(self):
        if self.watch_stop is None:
            self._start_watch()
        else:
            self._stop_watch()

    def _start_watch(self):
        revive = self.var_revive.get()
        self.watch_stop = threading.Event()

        def work(stop=self.watch_stop):
            try:
                sm.cmd_watch(revive=revive, log=lambda t: self.events.put(("log", t)), stop=stop)
            except Exception as exc:  # noqa: BLE001
                self.events.put(("log", f"WATCHER STOPPED: {type(exc).__name__}: {exc}"))
            self.events.put(("watch-ended", None))

        self.watch_thread = threading.Thread(target=work, daemon=True)
        self.watch_thread.start()
        self.btn_watch.configure(text="Stop Watching")
        self.var_watch.set("Watcher: ON" + ("" if revive else " (no auto-revive)"))

    def _stop_watch(self):
        if self.watch_stop is not None:
            self.watch_stop.set()

    def _with_watcher_paused(self, action):
        """Stop the watcher (so it never exports mid-restore), run the action, restart it after."""
        if self.watch_stop is None:
            action()
            return
        self.log("pausing the watcher for the restore")
        self._stop_watch()
        thread = self.watch_thread

        def wait():
            thread.join()
            self.after(0, lambda: (action(), self._resume_after_done()))

        threading.Thread(target=wait, daemon=True).start()

    def _resume_after_done(self):
        if self.busy:
            self.after(200, self._resume_after_done)
        else:
            self._start_watch()

    # ---- convert

    def _browse(self, var, saving):
        types = [("Save files", "*.xml *.reg *.prs *.json"), ("All files", "*.*")]
        path = (filedialog.asksaveasfilename(parent=self, filetypes=types) if saving
                else filedialog.askopenfilename(parent=self, filetypes=types, initialdir=HERE))
        if path:
            var.set(path)

    def _guess_output(self):
        src = self.var_src.get().strip()
        mode = prefs_convert.mode_for(src) if src else None
        if mode:
            self.var_mode.set(MODE_NAMES[mode])
            out_ext = {"xml2reg": ".reg", "reg2xml": ".xml", "prs2json": ".json", "json2prs": ".prs"}[mode]
            root, _ = os.path.splitext(src)
            self.var_dst.set(root + "_converted" + out_ext)

    def do_convert(self):
        src, dst, base, mode = (self.var_src.get().strip(), self.var_dst.get().strip(),
                                self.var_base.get().strip(), MODE_IDS.get(self.var_mode.get(), ""))
        if not src or not dst:
            notice(self, "Missing paths", "Pick an input file and an output file first.")
            return
        if not mode:
            notice(self, "Direction unknown", "Choose the direction of the conversion.")
            return
        if base and mode not in ("xml2reg", "reg2xml"):
            notice(self, "Base file not used", "A base file only applies to .xml and .reg conversions.")
            return
        if os.path.exists(dst) and not confirm(self, "Overwrite?", f"{dst} exists. Overwrite it?", yes="Overwrite",
                                               danger="Overwrite"):
            return
        try:
            count = prefs_convert.convert(mode, src, dst, base or None)
        except (prefs_convert.ConvertError, OSError, ValueError) as exc:
            self.var_conv_result.set(f"Failed: {exc}")
            self.log(f"convert failed: {exc}")
            return
        self.var_conv_result.set(f"Done: wrote {count} entries to {dst}")
        self.log(f"{MODE_NAMES[mode]}: wrote {count} entries to {dst}")

    # ---- shutdown

    def _close(self):
        if self.busy:
            notice(self, "Still working", "A backup or restore is in progress. Wait for it to finish, then close.")
            return
        if self.watch_stop is not None:
            if not confirm(self, "Stop watching?", "The watcher is on. Closing this window stops it.",
                           yes="Close anyway"):
                return
            self._stop_watch()
        save_index(self.index)
        self.destroy()


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("this tool talks to the Windows registry; run it on Windows")
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # crisp text on scaled displays
    except (AttributeError, OSError):
        pass
    try:
        App().mainloop()
    except Exception:  # noqa: BLE001 - pythonw has no console, so write it down
        import traceback
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  CRASH:\n{traceback.format_exc()}")
        raise
