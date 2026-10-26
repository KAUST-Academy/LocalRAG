"""LocalRAG: the Day 2 RAG lab as a Windows desktop app, in KAUST Academy colours.

Build a vector DB from a folder of documents (or open one you built before) with one of several embedding models,
load a language model (Qwen3.5-4B by default; one at a time), and ask questions answered from your documents, with
citations. Every step is visible (retrieved context, the exact prompt, timings) and written to a session log.

Run from source:  python local_rag.py
Build the .exe:   .\\build.ps1
Self-test:        LocalRAG.exe --selftest --docs <folder> [--llm Qwen/Qwen3.5-0.8B] [--out result.txt]
"""
import os
import sys

# Before huggingface_hub / transformers are imported: no progress bars (a windowed .exe has no console to draw them
# on), no symlink warning on Windows, no chromadb telemetry. Plain HTTP downloads instead of Xet: they grow the file
# in the cache as they go, which is what the app's progress bar watches.
for _key, _value in {"HF_HUB_DISABLE_PROGRESS_BARS": "1", "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
                     "HF_HUB_DISABLE_XET": "1", "TOKENIZERS_PARALLELISM": "false",
                     "TRANSFORMERS_NO_ADVISORY_WARNINGS": "1", "ANONYMIZED_TELEMETRY": "False"}.items():
    os.environ.setdefault(_key, _value)

import argparse
import json
import logging
import logging.handlers
import multiprocessing
import queue
import re
import shutil
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import rag_engine as rag
from session_log import SessionLog

log = logging.getLogger("localrag")
SETTINGS = rag.data_dir() / "settings.json"
MEAL_QUESTION = "What is the daily limit for meals while traveling for work?"

# KAUST Academy colours: the KAUST digital palette and the Academy presentation template
NAVY, NAVY_DEEP, NAVY_SOFT = "#003B75", "#002C58", "#0E4C8C"
GOLD, ORANGE, TEAL, LIME = "#F0B500", "#F18F00", "#00A6AA", "#CDCE00"
INK, QUIET, LINE, PAPER, LINK = "#1F2933", "#5B6470", "#D9DCDF", "#F3F4F5", "#0B5CAD"
ON_NAVY_QUIET, OK_GREEN, WARN_ORANGE, ERROR_RED = "#B7C7DB", "#13795B", "#A85700", "#B3261E"
FONT_FILES = ("Raleway-Bold.ttf", "Raleway-SemiBold.ttf", "OpenSans-Regular.ttf", "OpenSans-SemiBold.ttf",
              "OpenSans-Bold.ttf")


def setup_logging() -> Path:
    """Log everything to %LOCALAPPDATA%\\LocalRAG\\logs. In a windowed .exe sys.stdout and sys.stderr are None, and
    any library that prints (warnings, progress bars) would crash on them, so they are pointed at a file as well."""
    log_dir = rag.data_dir() / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(log_dir / "localrag.log", maxBytes=5_000_000, backupCount=3,
                                                   encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.captureWarnings(True)                   # Python warnings raised by the libraries go to the log too
    if sys.stdout is None or sys.stderr is None:
        console = open(log_dir / "console.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or console
        sys.stderr = sys.stderr or console
    return log_dir


def resource(*parts) -> Path:
    """A file bundled inside the app: PyInstaller unpacks those to sys._MEIPASS, not next to the .exe."""
    return Path(getattr(sys, "_MEIPASS", rag.app_dir())).joinpath(*parts)


def seconds_since_start() -> float:
    """Time since Windows started this process (the bootloader included): what a user waits for."""
    import psutil
    return time.time() - psutil.Process().create_time()


def open_target(target):
    """Open a web page in the browser, a file in its default program (Notepad when there is none)."""
    target = str(target)
    if target.startswith(("http://", "https://")):
        webbrowser.open(target)
    elif Path(target).exists():
        try:
            os.startfile(target)
        except OSError:
            import subprocess
            subprocess.Popen(["notepad.exe", target])


def load_brand_fonts() -> int:
    """Make the bundled Raleway and Open Sans usable by this process only: nothing is installed, no admin rights."""
    if sys.platform != "win32":
        return 0
    import ctypes
    added = 0
    for name in FONT_FILES:
        path = resource("assets", "fonts", name)
        if path.exists():
            added += ctypes.windll.gdi32.AddFontResourceExW(str(path), 0x10, 0)      # 0x10 = FR_PRIVATE
    return added


class Fonts:
    """The brand's Raleway (headings) and Open Sans (text) when they loaded; Segoe UI otherwise."""

    def __init__(self, root):
        from tkinter import font as tkfont
        have = set(tkfont.families(root))
        body = "Open Sans" if "Open Sans" in have else "Segoe UI"
        semi = "Open Sans SemiBold" if "Open Sans SemiBold" in have else "Segoe UI Semibold"
        head = "Raleway" if "Raleway" in have else "Segoe UI"
        self.brand = head == "Raleway" and body == "Open Sans"

        def font(family, size, weight="normal"):
            return tkfont.Font(root=root, family=family, size=size, weight=weight)

        self.body, self.small, self.tiny = font(body, 10), font(body, 9), font(body, 8)
        self.semi, self.semi_small = font(semi, 10), font(semi, 9)
        self.text, self.text_semi = font(body, 11), font(semi, 11)
        self.step_num, self.step_title, self.app_title = font(head, 18, "bold"), font(head, 11, "bold"), font(head, 18, "bold")
        self.heading = font(head, 10, "bold")
        self.mono = font("Cascadia Mono" if "Cascadia Mono" in have else "Consolas", 9)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family=body, size=10)


def apply_style(root, f: Fonts, scale: float):
    """ttk's clam theme is the built-in theme whose colours can all be set: navy, gold and teal throughout."""
    from tkinter import ttk
    px = lambda n: max(1, round(n * scale))  # noqa: E731
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", font=f.body, background="white", foreground=INK, bordercolor=LINE, lightcolor="white",
                darkcolor="white", troughcolor=PAPER, focuscolor=TEAL, selectbackground=NAVY,
                selectforeground="white", insertcolor=INK)
    for name, bg in (("TFrame", "white"), ("Side.TFrame", NAVY), ("Status.TFrame", PAPER)):
        s.configure(name, background=bg)
    labels = {"TLabel": ("white", INK, f.body), "Side.TLabel": (NAVY, "white", f.body),
              "SideQuiet.TLabel": (NAVY, ON_NAVY_QUIET, f.small), "SideOk.TLabel": (NAVY, "#8FE3CB", f.small),
              "SideBad.TLabel": (NAVY, "#FFC27A", f.small), "StepNum.TLabel": (NAVY, GOLD, f.step_num),
              "StepTitle.TLabel": (NAVY, "white", f.step_title), "StepHint.TLabel": (NAVY, ON_NAVY_QUIET, f.small),
              "AppTitle.TLabel": ("white", NAVY, f.app_title), "AppSub.TLabel": ("white", QUIET, f.body),
              "Chip.TLabel": (PAPER, QUIET, f.small), "Status.TLabel": (PAPER, QUIET, f.small),
              "Pane.TLabel": ("white", QUIET, f.small)}
    for name, (bg, fg, font) in labels.items():
        s.configure(name, background=bg, foreground=fg, font=font)
    s.configure("Chip.TLabel", padding=(px(10), px(4)))
    s.configure("Status.TLabel", padding=(px(14), px(4)))
    for kind in ("TRadiobutton", "TCheckbutton"):
        s.configure(f"Side.{kind}", background=NAVY, foreground="white", font=f.body, indicatorbackground=NAVY,
                    indicatorforeground=GOLD, upperbordercolor="white", lowerbordercolor="white", focuscolor=NAVY,
                    indicatorsize=px(14), indicatormargin=(0, 0, px(7), 0))
        s.map(f"Side.{kind}", background=[("active", NAVY)], foreground=[("disabled", ON_NAVY_QUIET)],
              indicatorbackground=[("pressed", NAVY_SOFT)],
              upperbordercolor=[("disabled", ON_NAVY_QUIET)], lowerbordercolor=[("disabled", ON_NAVY_QUIET)])
    s.configure("TEntry", fieldbackground="white", padding=(px(6), px(4)), bordercolor=LINE, lightcolor="white")
    s.map("TEntry", fieldbackground=[("disabled", "#D5DDE7")], foreground=[("disabled", "#6B7785")])
    s.configure("TCombobox", fieldbackground="white", background="white", arrowcolor=NAVY, padding=(px(6), px(4)),
                arrowsize=px(13), bordercolor=LINE, lightcolor="white")
    s.map("TCombobox", fieldbackground=[("readonly", "white"), ("disabled", "#D5DDE7")],
          foreground=[("disabled", "#6B7785")], selectbackground=[("readonly", "white")],
          selectforeground=[("readonly", INK)], background=[("disabled", "#D5DDE7")])
    s.configure("TSpinbox", fieldbackground="white", background="white", arrowcolor=NAVY, arrowsize=px(11),
                padding=(px(6), px(2)), bordercolor=LINE, lightcolor="white")
    buttons = {"Gold.TButton": (GOLD, NAVY_DEEP, "#FFC933", "#D9A300", "#4B6A91", "#A9BBD0"),
               "Ghost.TButton": (NAVY_SOFT, "white", "#1A5DA3", "#0A3F77", "#0A3F77", "#7F98B8"),
               "Navy.TButton": (NAVY, "white", NAVY_SOFT, NAVY_DEEP, "#9AA8B8", "#E9EDF2"),
               "Plain.TButton": (PAPER, NAVY, "#E6E9EC", "#D9DDE1", PAPER, "#A3ABB4")}
    for name, (bg, fg, active, pressed, off_bg, off_fg) in buttons.items():
        s.configure(name, background=bg, foreground=fg, font=f.semi, padding=(px(14), px(5)), borderwidth=0,
                    relief="flat", focusthickness=0, lightcolor=bg, darkcolor=bg, bordercolor=bg)
        s.map(name, background=[("disabled", off_bg), ("pressed", pressed), ("active", active)],
              foreground=[("disabled", off_fg)], lightcolor=[("active", active)], darkcolor=[("active", active)],
              bordercolor=[("active", active)])
    s.configure("Side.Horizontal.TProgressbar", troughcolor=NAVY_DEEP, background=TEAL, bordercolor=NAVY_DEEP,
                lightcolor=TEAL, darkcolor=TEAL, thickness=px(5))
    s.configure("TNotebook", background="white", bordercolor=LINE, tabmargins=(0, 0, 0, 0))
    s.configure("TNotebook.Tab", background=PAPER, foreground=QUIET, padding=(px(16), px(7)), font=f.semi,
                bordercolor=LINE, lightcolor=PAPER)
    s.map("TNotebook.Tab", background=[("selected", "white")], foreground=[("selected", NAVY)],
          lightcolor=[("selected", "white")])
    for name, bg, trough, arrow in (("Vertical.TScrollbar", "#E2E5E8", "white", QUIET),
                                    ("Side.Vertical.TScrollbar", NAVY_SOFT, NAVY, ON_NAVY_QUIET)):
        s.configure(name, background=bg, troughcolor=trough, bordercolor=trough, arrowcolor=arrow, gripcount=0,
                    arrowsize=px(12), lightcolor=bg, darkcolor=bg)
    root.option_add("*TCombobox*Listbox.font", f.body)
    root.option_add("*TCombobox*Listbox.selectBackground", NAVY)
    root.option_add("*TCombobox*Listbox.selectForeground", "white")


class UILogHandler(logging.Handler):
    """Every log record (the app's and the libraries') also goes to the Log tab, through the event queue."""

    def __init__(self, post):
        super().__init__(logging.INFO)
        self.post = post
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.post("log", "line", (self.format(record), record.levelname))
        except Exception:
            pass


class Scrollable:
    """The navy sidebar: it scrolls when the window is shorter than its content (small or high-DPI screens)."""

    def __init__(self, parent, width, scale):
        import tkinter as tk
        from tkinter import ttk
        self.canvas = tk.Canvas(parent, width=width, background=NAVY, highlightthickness=0, borderwidth=0)
        self.bar = ttk.Scrollbar(parent, orient="vertical", command=self.canvas.yview, style="Side.Vertical.TScrollbar")
        self.inner = ttk.Frame(self.canvas, style="Side.TFrame",
                               padding=(round(18 * scale), round(14 * scale), round(18 * scale), round(10 * scale)))
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.inner.bind("<Configure>", lambda e: self.update())
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(self.window, width=e.width), self.update()))
        self.canvas.bind_all("<MouseWheel>", self.wheel, add="+")

    def update(self):
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()))
        if self.inner.winfo_reqheight() > self.canvas.winfo_height() > 1:
            self.bar.grid()
        else:
            self.bar.grid_remove()
            self.canvas.yview_moveto(0)

    def wheel(self, event):
        if str(event.widget).startswith(str(self.canvas)) and self.bar.winfo_ismapped():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")


# ----------------------------------------------------------------------------------------------- the window

class App:
    def __init__(self, root, settings: dict, log_dir: Path, fonts: Fonts, scale: float):
        self.root, self.log_dir, self.f, self.scale = root, log_dir, fonts, scale
        self.events = queue.Queue()        # worker threads -> the Tk thread; Tk widgets are touched by the Tk thread only
        self.busy = set()                  # lanes with a job running: "kb", "llm", "ask"
        self.started = {}                  # lane -> when its job started, for the session log
        self.kb = None
        self.llm = rag.ChatModel()
        self.cancel = threading.Event()    # Cancel button while building a DB
        self.stop = threading.Event()      # Stop button while an answer streams
        self.links = 0
        self.session = SessionLog(rag.data_dir() / "sessions", {
            "version": rag.APP_VERSION, "python": sys.version.split()[0], "frozen": getattr(sys, "frozen", False),
            "app_dir": str(rag.app_dir()), "md_path": None})
        logging.getLogger().addHandler(UILogHandler(self.post))
        self.build_ui(settings)
        self.on_mode()
        self.show_path_ends()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(40, self.poll)
        log.info("session log: %s", self.session.md_path)
        threading.Thread(target=self.warm_up, daemon=True).start()

    def px(self, n):
        return max(1, round(n * self.scale))

    # ---- layout
    def build_ui(self, s):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        root, f, px = self.root, self.f, self.px
        root.title(f"LocalRAG {rag.APP_VERSION}: ask your documents")
        root.configure(background="white")
        try:
            root.iconbitmap(default=str(resource("assets", "icon.ico")))
        except tk.TclError:
            pass
        root.geometry(f"{min(px(1360), int(root.winfo_screenwidth() * 0.94))}x"
                      f"{min(px(940), int(root.winfo_screenheight() * 0.9))}")
        root.minsize(px(1000), px(600))
        self.build_menu()

        # header: the KAUST Academy logo, the app's name, the hardware it found
        header = ttk.Frame(root, padding=(px(18), px(6), px(18), px(6)))
        header.pack(side="top", fill="x")
        height = min((40, 50, 60, 70, 80, 100), key=lambda h: abs(h - 70 * self.scale))
        try:
            self.logo = tk.PhotoImage(file=str(resource("assets", f"logo_h{height}.png")))
            ttk.Label(header, image=self.logo).pack(side="left")
            ttk.Separator(header, orient="vertical").pack(side="left", fill="y", padx=px(16), pady=px(4))
        except tk.TclError:
            self.logo = None
        titles = ttk.Frame(header)
        titles.pack(side="left", fill="y")
        ttk.Label(titles, text="LocalRAG", style="AppTitle.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Ask your documents: the Day 2 RAG lab, running on this PC",
                  style="AppSub.TLabel").pack(anchor="w")
        self.hw = tk.StringVar(value="Loading the AI libraries...")
        ttk.Label(header, textvariable=self.hw, style="Chip.TLabel").pack(side="right", anchor="center")
        ttk.Separator(root).pack(side="top", fill="x")

        # status bar: where this session is being logged
        bar = ttk.Frame(root, style="Status.TFrame")
        bar.pack(side="bottom", fill="x")
        self.status = tk.StringVar(value=f"Session log: {self.session.md_path}")
        ttk.Label(bar, textvariable=self.status, style="Status.TLabel").pack(side="left")

        body = ttk.Frame(root)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(2, weight=1)
        self.side = Scrollable(body, px(440), self.scale)
        self.side.canvas.grid(row=0, column=0, sticky="ns")
        self.side.bar.grid(row=0, column=1, sticky="ns")
        self.build_sidebar(self.side.inner, s)
        self.side.inner.update_idletasks()               # the sidebar is exactly as wide as its widest row
        self.side.canvas.configure(width=max(px(440), self.side.inner.winfo_reqwidth()))
        main = ttk.Frame(body, padding=(px(16), px(12), px(16), px(12)))
        main.grid(row=0, column=2, sticky="nsew")
        self.build_main(main)

    def build_menu(self):
        tk = self.tk
        menu = tk.Menu(self.root)
        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="Open this session's log", command=lambda: open_target(self.session.md_path))
        file_menu.add_command(label="Save this session's log as...", command=self.save_session_log)
        file_menu.add_command(label="Open the sessions folder", command=lambda: open_target(self.session.md_path.parent))
        file_menu.add_command(label="Open the logs folder", command=lambda: open_target(self.log_dir))
        file_menu.add_command(label="Open the program folder", command=lambda: open_target(rag.app_dir()))
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.on_close)
        menu.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menu, tearoff=False)
        help_menu.add_command(label="About LocalRAG", command=self.about)
        menu.add_cascade(label="Help", menu=help_menu)
        self.root.config(menu=menu)

    def step(self, parent, number, title, hint=""):
        """A step header in the template's style: a gold number, a white upper-case title."""
        from tkinter import ttk
        px = self.px
        head = ttk.Frame(parent, style="Side.TFrame")
        head.pack(fill="x", pady=(px(2), px(6)))
        ttk.Label(head, text=number, style="StepNum.TLabel").pack(side="left", anchor="s")
        ttk.Label(head, text=title.upper(), style="StepTitle.TLabel").pack(side="left", anchor="s",
                                                                          padx=(px(10), 0), pady=(0, px(4)))
        if hint:
            ttk.Label(head, text=hint, style="StepHint.TLabel").pack(side="left", anchor="s", padx=(px(8), 0),
                                                                     pady=(0, px(5)))
        box = ttk.Frame(parent, style="Side.TFrame")
        box.pack(fill="x", pady=(0, px(12)))
        box.columnconfigure(1, weight=1)
        return box

    def path_row(self, parent, r, label, var, command):
        from tkinter import ttk
        px = self.px
        ttk.Label(parent, text=label, style="Side.TLabel").grid(row=r, column=0, sticky="w", padx=(0, px(10)), pady=px(3))
        entry = ttk.Entry(parent, textvariable=var, width=26)
        entry.grid(row=r, column=1, sticky="ew", pady=px(3))
        button = ttk.Button(parent, text="Browse", style="Ghost.TButton", command=command)
        button.grid(row=r, column=2, sticky="e", padx=(px(6), 0), pady=px(3))
        self.path_entries.append(entry)
        return [entry, button]

    def build_sidebar(self, sb, s):
        tk = self.tk
        from tkinter import ttk
        px, wrap = self.px, self.px(390)
        self.path_entries = []

        # ---- 01 knowledge base
        box = self.step(sb, "01", "Knowledge base")
        self.mode = tk.StringVar(value=s.get("mode", "new"))
        modes = ttk.Frame(box, style="Side.TFrame")
        modes.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, px(8)))
        ttk.Radiobutton(modes, text="New, from a folder", variable=self.mode, value="new", style="Side.TRadiobutton",
                        command=self.on_mode).pack(side="left")
        ttk.Radiobutton(modes, text="Open one built before", variable=self.mode, value="old",
                        style="Side.TRadiobutton", command=self.on_mode).pack(side="left", padx=(px(18), 0))
        sample = rag.app_dir() / "sample_docs" / "gitlab_handbook"
        self.docs = tk.StringVar(value=s.get("docs") or (str(sample) if sample.is_dir() else ""))
        self.db_new = tk.StringVar(value=s.get("db_new") or self.default_db(self.docs.get()))
        self.db_old = tk.StringVar(value=s.get("db_old", ""))

        self.new_box = ttk.Frame(box, style="Side.TFrame")
        self.new_box.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.new_box.columnconfigure(1, weight=1)
        self.new_widgets = self.path_row(self.new_box, 0, "Documents", self.docs, self.pick_docs)
        self.new_widgets += self.path_row(self.new_box, 1, "Save DB in", self.db_new, self.pick_db_new)
        self.embed_var = tk.StringVar(value=s.get("embedding", rag.EMBEDDINGS[0].label))
        if self.embed_var.get() not in [c.label for c in rag.EMBEDDINGS]:
            self.embed_var.set(rag.EMBEDDINGS[0].label)
        ttk.Label(self.new_box, text="Embedding", style="Side.TLabel").grid(row=2, column=0, sticky="w", pady=(px(8), 0))
        combo = ttk.Combobox(self.new_box, textvariable=self.embed_var, values=[c.label for c in rag.EMBEDDINGS],
                             state="readonly")
        combo.grid(row=2, column=1, columnspan=2, sticky="ew", pady=(px(8), 0))
        combo.bind("<<ComboboxSelected>>", lambda e: self.embed_note.set(self.embedding().note))
        self.embed_note = tk.StringVar(value=self.embedding().note)
        ttk.Label(self.new_box, textvariable=self.embed_note, style="SideQuiet.TLabel", wraplength=px(330)).grid(
            row=3, column=1, columnspan=2, sticky="w", pady=(px(2), 0))
        chunking = s.get("chunking") if s.get("chunking") in rag.CHUNKERS else "sections"
        self.chunk_var = tk.StringVar(value=rag.CHUNKERS[chunking][0])
        ttk.Label(self.new_box, text="Chunking", style="Side.TLabel").grid(row=4, column=0, sticky="w", pady=(px(6), 0))
        ccombo = ttk.Combobox(self.new_box, textvariable=self.chunk_var, values=[v[0] for v in rag.CHUNKERS.values()],
                              state="readonly")
        ccombo.grid(row=4, column=1, columnspan=2, sticky="ew", pady=(px(6), 0))
        self.new_widgets += [combo, ccombo]
        buttons = ttk.Frame(self.new_box, style="Side.TFrame")
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(px(10), 0))
        self.build_btn = ttk.Button(buttons, text="Build DB", style="Gold.TButton", command=self.build_db)
        self.build_btn.pack(side="right")
        self.cancel_btn = ttk.Button(buttons, text="Cancel", style="Ghost.TButton", command=self.cancel.set)
        self.cancel_btn.pack(side="right", padx=(0, px(8)))

        self.old_box = ttk.Frame(box, style="Side.TFrame")
        self.old_box.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.old_box.columnconfigure(1, weight=1)
        self.old_widgets = self.path_row(self.old_box, 0, "DB folder", self.db_old, self.pick_db_old)
        ttk.Label(self.old_box, text="The folder you saved a DB to: it holds localrag.json. The DB is searched with "
                                     "the embedding model it was built with.",
                  style="SideQuiet.TLabel", wraplength=wrap).grid(row=1, column=0, columnspan=3, sticky="w", pady=(px(4), 0))
        self.open_btn = ttk.Button(self.old_box, text="Open DB", style="Gold.TButton", command=self.open_db)
        self.open_btn.grid(row=2, column=0, columnspan=3, sticky="e", pady=(px(10), 0))

        self.kb_bar = ttk.Progressbar(box, maximum=1.0, style="Side.Horizontal.TProgressbar")
        self.kb_bar.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(px(10), px(3)))
        self.kb_msg = tk.StringVar(value="No knowledge base yet.")
        self.kb_label = ttk.Label(box, textvariable=self.kb_msg, wraplength=wrap, style="SideQuiet.TLabel")
        self.kb_label.grid(row=3, column=0, columnspan=3, sticky="w")

        # ---- 02 language model
        box = self.step(sb, "02", "Language model", "one at a time")
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=0)
        self.llm_var = tk.StringVar(value=s.get("llm", rag.LLMS[0].label))
        self.llm_combo = ttk.Combobox(box, textvariable=self.llm_var, values=[c.label for c in rag.LLMS])
        self.llm_combo.grid(row=0, column=0, sticky="ew")
        self.llm_combo.bind("<<ComboboxSelected>>", lambda e: self.llm_note.set(self.llm_choice_note()))
        self.llm_combo.bind("<KeyRelease>", lambda e: self.llm_note.set(self.llm_choice_note()))
        self.folder_btn = ttk.Button(box, text="Folder", style="Ghost.TButton", command=self.pick_llm_folder)
        self.folder_btn.grid(row=0, column=1, padx=(px(6), 0))
        self.llm_note = tk.StringVar(value=self.llm_choice_note())
        ttk.Label(box, textvariable=self.llm_note, style="SideQuiet.TLabel", wraplength=wrap).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(px(3), 0))
        buttons = ttk.Frame(box, style="Side.TFrame")
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", pady=(px(10), 0))
        self.load_btn = ttk.Button(buttons, text="Load model", style="Gold.TButton", command=self.load_llm)
        self.load_btn.pack(side="right")
        self.unload_btn = ttk.Button(buttons, text="Unload", style="Ghost.TButton", command=self.unload_llm)
        self.unload_btn.pack(side="right", padx=(0, px(8)))
        self.llm_bar = ttk.Progressbar(box, maximum=1.0, style="Side.Horizontal.TProgressbar")
        self.llm_bar.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(px(10), px(3)))
        self.llm_msg = tk.StringVar(value="No model loaded. You can still search: questions then show the passages.")
        self.llm_label = ttk.Label(box, textvariable=self.llm_msg, wraplength=wrap, style="SideQuiet.TLabel")
        self.llm_label.grid(row=4, column=0, columnspan=2, sticky="w")

        # ---- 03 retrieval
        box = self.step(sb, "03", "Retrieval")
        self.k = tk.StringVar(value=str(s.get("k", 3)))
        self.threshold = tk.StringVar(value=f"{float(s.get('threshold', self.embedding().threshold)):.2f}")
        self.show_passages = tk.BooleanVar(value=s.get("show_passages", True))
        row = ttk.Frame(box, style="Side.TFrame")
        row.grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(row, text="Passages (k)", style="Side.TLabel").pack(side="left")
        ttk.Spinbox(row, from_=1, to=10, textvariable=self.k, width=4).pack(side="left", padx=(px(8), px(22)))
        ttk.Label(row, text="Similarity gate", style="Side.TLabel").pack(side="left")
        ttk.Spinbox(row, from_=0.0, to=0.95, increment=0.05, format="%.2f", textvariable=self.threshold,
                    width=5).pack(side="left", padx=(px(8), 0))
        ttk.Label(box, text="Below the gate the app says \"I don't know\" instead of guessing. Opening a DB sets "
                            "the gate measured for its embedding model.",
                  style="SideQuiet.TLabel", wraplength=wrap).grid(row=1, column=0, columnspan=3, sticky="w", pady=(px(5), 0))
        ttk.Checkbutton(box, text="Show the retrieved passages under each answer", variable=self.show_passages,
                        style="Side.TCheckbutton").grid(row=2, column=0, columnspan=3, sticky="w", pady=(px(8), 0))

    def text_view(self, parent, font=None):
        """A read-only text pane with a scrollbar."""
        tk = self.tk
        from tkinter import ttk
        frame = ttk.Frame(parent)
        view = tk.Text(frame, wrap="word", font=font or self.f.text, relief="flat", borderwidth=0,
                       highlightthickness=0, padx=self.px(18), pady=self.px(14), spacing1=2, spacing3=2,
                       state="disabled", cursor="arrow", foreground=INK, background="white",
                       selectbackground="#CFE0F3", selectforeground=INK)
        bar = ttk.Scrollbar(frame, command=view.yview)
        view.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        view.pack(side="left", fill="both", expand=True)
        view.tag_configure("link", foreground=LINK, underline=True)
        view.tag_bind("link", "<Enter>", lambda e: view.configure(cursor="hand2"))
        view.tag_bind("link", "<Leave>", lambda e: view.configure(cursor="arrow"))
        return frame, view

    def build_main(self, main):
        tk = self.tk
        from tkinter import ttk
        f, px = self.f, self.px
        ask = ttk.Frame(main)
        ask.pack(side="bottom", fill="x", pady=(px(10), 0))
        self.question = tk.Text(ask, height=3, wrap="word", font=f.text, relief="flat", borderwidth=0,
                                highlightthickness=1, highlightbackground=LINE, highlightcolor=TEAL,
                                padx=px(10), pady=px(8), foreground=INK, insertbackground=INK)
        self.question.pack(side="left", fill="x", expand=True)
        self.question.bind("<Return>", self.on_enter)
        side = ttk.Frame(ask)
        side.pack(side="left", fill="y", padx=(px(10), 0))
        self.ask_btn = ttk.Button(side, text="Ask", style="Navy.TButton", command=self.ask)
        self.ask_btn.pack(fill="x")
        self.stop_btn = ttk.Button(side, text="Stop", style="Plain.TButton", command=self.stop.set)
        self.stop_btn.pack(fill="x", pady=px(4))
        ttk.Button(side, text="Clear", style="Plain.TButton", command=self.clear).pack(fill="x")

        self.nb = ttk.Notebook(main)
        self.nb.pack(fill="both", expand=True)
        frame, self.out = self.text_view(self.nb)
        self.nb.add(frame, text="Conversation")
        frame, self.ctx = self.text_view(self.nb)
        self.nb.add(frame, text="Retrieved context")
        frame, self.prompt_view = self.text_view(self.nb)
        self.nb.add(frame, text="Prompt")
        log_tab = ttk.Frame(self.nb)
        tools = ttk.Frame(log_tab, padding=(px(12), px(8)))
        tools.pack(side="top", fill="x")
        ttk.Label(tools, text="Everything this session does, live. The same goes to the session log and the logs "
                              "folder.", style="Pane.TLabel").pack(side="left")
        ttk.Button(tools, text="Open the logs folder", style="Plain.TButton",
                   command=lambda: open_target(self.log_dir)).pack(side="right")
        ttk.Button(tools, text="Open this session's log", style="Plain.TButton",
                   command=lambda: open_target(self.session.md_path)).pack(side="right", padx=(0, px(6)))
        frame, self.log_view = self.text_view(log_tab, font=f.mono)
        frame.pack(fill="both", expand=True)
        self.log_view.configure(wrap="word", spacing1=0, spacing3=0)
        indent = f.mono.measure("00:00:00 INFO    ")
        self.log_view.tag_configure("line", lmargin2=indent)
        self.log_view.tag_configure("WARNING", foreground=WARN_ORANGE, lmargin2=indent)
        self.log_view.tag_configure("ERROR", foreground=ERROR_RED, lmargin2=indent)
        self.nb.add(log_tab, text="Log")

        for view in (self.out, self.ctx, self.prompt_view):
            view.tag_configure("h", font=f.heading, foreground=NAVY, spacing1=px(12), spacing3=px(4))
            view.tag_configure("facts", font=f.small, foreground=QUIET)
            view.tag_configure("chunk", font=f.body, foreground=INK, background=PAPER, lmargin1=px(26),
                               lmargin2=px(26), rmargin=px(14), spacing1=px(6), spacing3=px(6))
            view.tag_configure("raw", font=f.mono, foreground=INK, background=PAPER, lmargin1=px(14),
                               lmargin2=px(14), rmargin=px(14), spacing1=0, spacing3=0)
        o = self.out
        o.tag_configure("you", font=f.semi_small, foreground=NAVY, spacing1=px(16))
        o.tag_configure("q", font=f.text_semi, foreground=INK)
        o.tag_configure("bot", font=f.semi_small, foreground=TEAL, spacing1=px(8))
        o.tag_configure("answer", font=f.text, foreground=INK)
        o.tag_configure("answer_bold", font=f.text_semi, foreground=INK)
        o.tag_configure("src", font=f.small, foreground="#3D4652", lmargin1=px(12), lmargin2=px(32))
        o.tag_configure("passage", font=f.small, foreground="#3D4652", background=PAPER, lmargin1=px(26),
                        lmargin2=px(26), rmargin=px(14), spacing1=px(4), spacing3=px(4))
        o.tag_configure("meta", font=f.tiny, foreground="#7A828C", spacing1=px(4))
        o.tag_configure("warn", font=f.text, foreground=WARN_ORANGE)
        c = self.ctx
        c.tag_configure("q", font=f.text_semi, foreground=INK)
        c.tag_configure("pass", font=f.semi, foreground=OK_GREEN, spacing1=px(6))
        c.tag_configure("fail", font=f.semi, foreground=WARN_ORANGE, spacing1=px(6))
        c.tag_configure("rank", font=f.semi, foreground=NAVY, spacing1=px(12))
        c.tag_configure("score", font=f.semi, foreground=INK)
        c.tag_configure("bar", font=f.mono, foreground=TEAL)
        c.tag_configure("barlow", font=f.mono, foreground=ORANGE)
        c.tag_configure("barbg", font=f.mono, foreground="#DCE3EA")
        c.tag_configure("title", font=f.semi, foreground=INK)
        c.tag_configure("where", font=f.tiny, foreground=QUIET, lmargin1=px(26), spacing3=px(4))
        p = self.prompt_view
        p.tag_configure("role", font=f.semi_small, foreground=NAVY, spacing1=px(10))
        p.tag_configure("msg", font=f.body, foreground=INK, lmargin1=px(14), lmargin2=px(14))
        p.tag_configure("answer", font=f.text, foreground=INK)

        self.write("WELCOME\n", "h")
        self.write("1. Build a DB from a folder of documents (.pdf .docx .txt .md .csv .html), or open one you built "
                   "before. The sample folder holds the 38 GitLab handbook pages from the lab.\n"
                   "2. Load a language model. The first time, it downloads (Qwen3.5-4B is 9.3 GB); after that it "
                   "loads from this PC.\n"
                   "3. Ask. The answer cites passages like [1]. The Retrieved context and Prompt tabs show what "
                   "the model was given; the Log tab and the session log record everything.\n", "answer")
        self.write(f"Try: {MEAL_QUESTION}\n", "facts")
        self.fill(self.ctx, [("RETRIEVED CONTEXT\n", "h"), ("Ask a question: the passages the DB returns, their "
                             "similarity and the gate decision appear here.\n", "facts")])
        self.fill(self.prompt_view, [("PROMPT\n", "h"), ("Ask a question: the exact prompt the language model "
                                     "receives, and how long it took to answer, appear here.\n", "facts")])

    # ---- small helpers
    @staticmethod
    def default_db(docs_folder: str) -> str:
        documents = Path.home() / "Documents"
        base = (documents if documents.is_dir() else Path.home()) / "LocalRAG databases"
        return str(base / (Path(docs_folder).name if docs_folder else "my_documents"))

    def embedding(self) -> rag.EmbeddingChoice:
        return next((c for c in rag.EMBEDDINGS if c.label == self.embed_var.get()), rag.EMBEDDINGS[0])

    def chunking(self) -> str:
        return next((key for key, (label, _) in rag.CHUNKERS.items() if label == self.chunk_var.get()), "sections")

    def llm_id(self) -> str:
        text = self.llm_var.get().strip()
        return next((c.id for c in rag.LLMS if c.label == text), text)   # a label, or any Hugging Face id / folder

    def llm_choice_note(self) -> str:
        text = self.llm_var.get().strip()
        choice = next((c for c in rag.LLMS if c.label == text), None)
        if choice:
            return choice.note
        if text and Path(text).is_dir():
            return "A model folder on this PC."
        return "Any Hugging Face chat model id (like org/name), or a model folder: pick a list entry or type one."

    def write(self, text, tag=None, link=None, view=None):
        view = view or self.out
        tags = (tag,) if tag else ()
        if link:
            name = f"link{self.links}"
            self.links += 1
            view.tag_bind(name, "<Button-1>", lambda e, target=link: open_target(target))
            tags += ("link", name)
        view.configure(state="normal")
        view.insert("end", text, tags)
        view.configure(state="disabled")
        if view is not self.log_view or float(view.yview()[1]) > 0.98:
            view.see("end")

    def fill(self, view, parts):
        """Replace a pane's content with [(text, tag) or (text, tag, link), ...]."""
        view.configure(state="normal")
        view.delete("1.0", "end")
        view.configure(state="disabled")
        for part in parts:
            self.write(part[0], part[1], part[2] if len(part) > 2 else None, view=view)
        view.see("1.0")

    def clear(self):
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.configure(state="disabled")

    def show_path_ends(self):
        """Long paths: show their end (the folder name), not their start."""
        for entry in self.path_entries:
            entry.after_idle(entry.xview_moveto, 1.0)

    def on_mode(self):
        new = self.mode.get() == "new"
        (self.new_box if new else self.old_box).grid()
        (self.old_box if new else self.new_box).grid_remove()
        self.refresh()

    def set_progress(self, lane, fraction, text):
        if lane not in ("kb", "llm"):
            self.status.set(text)
            return
        bar, msg, label = self.lane_widgets(lane)
        label.configure(style="SideQuiet.TLabel")
        msg.set(text)
        if fraction is None:                                  # unknown length: a moving bar
            if str(bar.cget("mode")) != "indeterminate":
                bar.configure(mode="indeterminate")
                bar.start(12)
        else:
            if str(bar.cget("mode")) != "determinate":
                bar.stop()
                bar.configure(mode="determinate")
            bar["value"] = fraction

    def end_progress(self, lane, text, ok=True):
        bar, msg, label = self.lane_widgets(lane)
        bar.stop()
        bar.configure(mode="determinate")
        bar["value"] = 1.0 if ok else 0
        msg.set(text)
        label.configure(style="SideOk.TLabel" if ok else "SideBad.TLabel")

    def lane_widgets(self, lane):
        return {"kb": (self.kb_bar, self.kb_msg, self.kb_label), "llm": (self.llm_bar, self.llm_msg, self.llm_label)}[lane]

    def refresh(self):
        """Enable only what makes sense right now."""
        kb_busy, llm_busy, asking = "kb" in self.busy, "llm" in self.busy, "ask" in self.busy

        def able(widget, on):
            widget.state(["!disabled"] if on else ["disabled"])

        for w in self.new_widgets + self.old_widgets:
            able(w, not kb_busy)
        able(self.build_btn, not kb_busy and not asking)
        able(self.open_btn, not kb_busy and not asking)
        able(self.cancel_btn, kb_busy)
        able(self.llm_combo, not llm_busy)
        able(self.folder_btn, not llm_busy)
        able(self.load_btn, not llm_busy and not asking)
        able(self.unload_btn, not llm_busy and not asking and self.llm.ready)
        able(self.ask_btn, not asking and not kb_busy and self.kb is not None)
        able(self.stop_btn, asking)

    # ---- background jobs
    def post(self, lane, kind, payload=None):
        self.events.put((lane, kind, payload))

    def reporter(self, lane):
        return lambda fraction, text: self.post(lane, "progress", (fraction, text))

    def run(self, lane, job, on_done):
        self.busy.add(lane)
        self.started[lane] = time.perf_counter()
        self.refresh()

        def target():
            try:
                result = job()
            except rag.Cancelled:
                self.post(lane, "cancelled")
            except Exception as err:
                log.exception("%s job failed", lane)
                self.post(lane, "error", (err, traceback.format_exc()))
            else:
                self.post(lane, "done", (on_done, result))

        threading.Thread(target=target, daemon=True).start()

    def poll(self):
        try:
            for _ in range(500):                     # bounded, so a fast stream cannot freeze the window
                lane, kind, payload = self.events.get_nowait()
                self.handle(lane, kind, payload)
        except queue.Empty:
            pass
        self.root.after(40, self.poll)

    def handle(self, lane, kind, payload):
        if kind == "progress":
            self.set_progress(lane, *payload)
        elif kind == "write":
            self.write(*payload)
        elif kind == "mark":
            self.out.mark_set("answer_start", "end-1c")
            self.out.mark_gravity("answer_start", "left")
        elif kind == "line":
            self.write(payload[0] + "\n", payload[1] if payload[1] in ("WARNING", "ERROR") else "line",
                       view=self.log_view)
        elif kind == "context":
            self.show_context(payload)
        elif kind == "ready":
            self.hw.set(payload)
            self.session.write("hardware", {"summary": payload})
        elif kind == "done":
            self.busy.discard(lane)
            on_done, result = payload
            on_done(result)
            self.refresh()
        elif kind in ("error", "cancelled"):
            self.busy.discard(lane)
            err, details = payload if kind == "error" else ("Cancelled.", "Cancelled by the user.")
            text = "Cancelled." if kind == "cancelled" else f"{type(err).__name__}: {err}"
            task = {"kb": "knowledge base", "llm": "language model", "ask": "question"}.get(lane, lane)
            self.session.write("error", {"task": task, "error": details})
            if lane in ("kb", "llm"):
                self.end_progress(lane, text, ok=False)
            else:
                self.write(f"\n{text}\n", "warn")
                self.status.set(f"Session log: {self.session.md_path}")
            if kind == "error" and lane != "ask":
                from tkinter import messagebox
                messagebox.showerror("LocalRAG", f"{err}\n\nDetails are in the Log tab and the session log.")
            self.refresh()

    def warm_up(self):
        """Import the heavy libraries while the user looks at the window, then say what hardware we run on."""
        try:
            import chromadb  # noqa: F401
            import sentence_transformers  # noqa: F401
            import transformers
            try:                                      # transformers logs to its own stderr handler: send it to ours
                transformers.utils.logging.disable_default_handler()
                transformers.utils.logging.enable_propagation()
            except Exception:
                pass
            log.info("AI libraries ready %.1f s after the process started", seconds_since_start())
            summary = rag.hardware_summary()
            log.info("hardware: %s", summary)
            self.post("boot", "ready", summary)
        except Exception as err:
            log.exception("warm-up failed")
            self.post("boot", "ready", f"Could not load the AI libraries: {err}")

    # ---- step 1 actions
    def pick_docs(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="Folder with your documents", initialdir=self.docs.get() or None)
        if folder:
            self.docs.set(str(Path(folder)))
            self.db_new.set(self.default_db(folder))
            self.show_path_ends()

    def pick_db_new(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="Where to save the DB (an empty folder)", mustexist=False)
        if folder:
            self.db_new.set(str(Path(folder)))
            self.show_path_ends()

    def pick_db_old(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="A DB folder you built before (it holds localrag.json)",
                                         initialdir=self.db_old.get() or None)
        if folder:
            self.db_old.set(str(Path(folder)))
            self.show_path_ends()

    def build_db(self):
        from tkinter import messagebox
        docs, db = Path(self.docs.get().strip()), Path(self.db_new.get().strip())
        if not docs.is_dir():
            messagebox.showwarning("LocalRAG", "Pick the folder that holds your documents.")
            return
        if not str(db).strip() or str(db) == ".":
            messagebox.showwarning("LocalRAG", "Pick where to save the DB.")
            return
        if (db / rag.MANIFEST).exists():
            if not messagebox.askyesno("LocalRAG", f"{db}\nalready holds a DB. Replace it?"):
                return
        elif db.is_dir() and any(db.iterdir()):
            messagebox.showwarning("LocalRAG", f"{db}\nis not empty. Pick a new or empty folder for the DB.")
            return
        choice, chunking = self.embedding(), self.chunking()
        log.info("build DB: documents=%s, DB=%s, embedding=%s, chunking=%s", docs, db, choice.id, chunking)
        self.kb = None                                         # the old DB closes; the new one replaces it
        self.cancel.clear()
        self.kb_action = "db_built"
        self.run("kb", lambda: rag.KnowledgeBase.build(docs, db, choice, chunking, self.reporter("kb"), self.cancel),
                 self.kb_ready)

    def open_db(self):
        from tkinter import messagebox
        db = Path(self.db_old.get().strip())
        if not db.is_dir():
            messagebox.showwarning("LocalRAG", "Pick the folder of a DB you built before.")
            return
        log.info("open DB: %s", db)
        self.kb = None
        self.kb_action = "db_opened"
        self.run("kb", lambda: rag.KnowledgeBase.open(db, self.reporter("kb")), self.kb_ready)

    def kb_ready(self, kb):
        self.kb = kb
        self.db_old.set(str(kb.db_dir))
        gate = kb.embedder.choice.threshold
        self.threshold.set(f"{gate:.2f}")                  # the gate belongs to the embedding model
        self.show_path_ends()
        m = kb.manifest
        self.end_progress("kb", f"Ready: {m['chunks']:,} chunks from {m['files']:,} files · "
                                f"{kb.embedder.choice.id.split('/')[-1]} · {m['chunking']}\n"
                                f"in ...\\{kb.db_dir.parent.name}\\{kb.db_dir.name}")
        seconds = time.perf_counter() - self.started.get("kb", time.perf_counter())
        log.info("%s in %.1f s: %s", self.kb_action.replace("_", " "), seconds, kb.summary())
        self.session.write(self.kb_action, {
            "db_dir": str(kb.db_dir), "source_folder": m.get("source_folder"),
            "embedding": f"{kb.embedder.choice.label} · {kb.embedder.choice.id}", "chunking": m.get("chunking"),
            "files": m.get("files"), "chunks": m.get("chunks"), "created": m.get("created"), "threshold": f"{gate:.2f}",
            "seconds": seconds, "problems": m.get("problems", [])})
        self.write("\nKNOWLEDGE BASE READY\n", "h")
        self.write(f"{kb.summary()}. Similarity gate set to {gate:.2f}, the value measured for this embedding "
                   f"model.\n", "facts")
        problems = m.get("problems", [])
        if problems:
            self.write(f"{len(problems)} file(s) gave no text:\n" + "\n".join(f"  {p}" for p in problems[:15]) +
                       ("\n  ..." if len(problems) > 15 else "") + "\n", "warn")

    # ---- step 2 actions
    def pick_llm_folder(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(title="A model folder (it holds config.json and the weights)")
        if folder:
            self.llm_var.set(str(Path(folder)))
            self.llm_note.set(self.llm_choice_note())

    def load_llm(self):
        model_id = self.llm_id()
        if not model_id:
            return
        log.info("load language model: %s", model_id)
        self.loading_id = model_id
        self.run("llm", lambda: self.llm.load(model_id, self.reporter("llm")), self.llm_ready)

    def llm_ready(self, _):
        seconds = time.perf_counter() - self.started.get("llm", time.perf_counter())
        self.end_progress("llm", f"Loaded: {self.llm.name} · {self.llm.where}")
        log.info("language model %s loaded in %.1f s on %s", self.llm.name, seconds, self.llm.where)
        self.session.write("model_loaded", {"model": f"{self.llm.name} ({self.loading_id})", "where": self.llm.where,
                                            "seconds": seconds})
        self.write(f"\nLanguage model ready: {self.llm.name} ({self.llm.where})\n", "facts")

    def unload_llm(self):
        name = self.llm.name
        log.info("unload language model: %s", name)

        def done(_):
            self.session.write("model_unloaded", {"model": name})
            self.end_progress("llm", "No model loaded. You can still search: questions then show the passages.")

        self.run("llm", self.llm.unload, done)

    # ---- asking
    def on_enter(self, event):
        if event.state & 0x0001:                                # Shift+Enter: a new line in the question
            return None
        self.ask()
        return "break"

    def ask(self):
        question = self.question.get("1.0", "end").strip()
        if not question or "ask" in self.busy or self.kb is None:
            return
        try:
            k, threshold = max(1, min(int(self.k.get()), 20)), float(self.threshold.get())
        except ValueError:
            k, threshold = 3, self.kb.embedder.choice.threshold
        self.question.delete("1.0", "end")
        self.write("\nYOU\n", "you")
        self.write(question + "\n", "q")
        self.stop.clear()
        use_llm = self.llm.ready and "llm" not in self.busy
        log.info("question (k=%d, gate %.2f, model %s): %s", k, threshold, self.llm.name if use_llm else "none", question)
        self.run("ask", lambda: self.answer(question, k, threshold, use_llm), self.answered)

    def answer(self, question, k, threshold, use_llm):
        """Worker thread: the lab's answer(): retrieve -> gate -> grounded prompt -> streamed reply."""
        r = {"question": question, "k": k, "threshold": threshold, "embedding": self.kb.embedder.choice.label,
             "db_dir": str(self.kb.db_dir), "start": time.perf_counter(), "answer": "", "generation": {}}
        self.post("ask", "progress", (None, "Searching the DB..."))
        t = time.perf_counter()
        r["hits"] = self.kb.search(question, k)
        r["search_ms"] = (time.perf_counter() - t) * 1000
        r["messages"] = rag.prompt_messages(question, r["hits"])
        self.post("ask", "context", dict(r))               # the context tab fills before the model starts writing
        best = r["hits"][0].score if r["hits"] else 0.0
        if best < threshold:
            self.post("ask", "write", ("LOCALRAG\n", "bot"))
            self.post("ask", "write", (f"I don't have that information in these documents. (best similarity "
                                       f"{best:.2f}, gate {threshold:.2f})\n", "warn"))
            r["outcome"] = "refused"
            return r
        if not use_llm:
            self.post("ask", "write", ("LOCALRAG\n", "bot"))
            self.post("ask", "write", ("No language model is loaded, so here are the passages it would read. "
                                       "Load one in step 02 for an answer.\n", "warn"))
            r["outcome"] = "passages"
            return r
        self.post("ask", "write", (self.llm.name.upper() + "\n", "bot"))
        self.post("ask", "mark")
        self.post("ask", "progress", (None, f"{self.llm.name} is reading {len(r['hits'])} passages and writing..."))
        pieces = []
        for piece in self.llm.stream(r["messages"], stop=self.stop):
            pieces.append(piece)
            self.post("ask", "write", (piece, "answer"))
        self.post("ask", "write", ("\n", "answer"))
        r["answer"] = "".join(pieces)
        r["generation"] = dict(self.llm.last)
        r["outcome"] = "stopped" if self.stop.is_set() else "answered"
        return r

    def show_context(self, r):
        """The Retrieved context tab: the question, the gate decision, every passage with its similarity."""
        hits, gate = r["hits"], r["threshold"]
        best = hits[0].score if hits else 0.0
        passed = best >= gate
        parts = [("QUESTION\n", "h"), (r["question"] + "\n", "q"),
                 (f"k = {r['k']} · search {r['search_ms']:.0f} ms · embedding {r['embedding']} · DB {r['db_dir']}\n",
                  "facts"),
                 ((f"Passed the gate: best similarity {best:.2f} ≥ {gate:.2f}, so these passages go to the model.\n"
                   if passed else
                   f"Below the gate: best similarity {best:.2f} < {gate:.2f}, so the app answers \"I don't know\" "
                   f"and asks no model.\n"), "pass" if passed else "fail"),
                 ("RETRIEVED CONTEXT\n", "h")]
        for i, h in enumerate(hits, 1):
            filled = round(max(0.0, min(1.0, h.score)) * 24)
            page = f", page {h.meta['page']}" if h.meta.get("page") else ""
            parts += [(f"[{i}]  ", "rank"), (f"{h.score:.2f}  ", "score"),
                      ("█" * filled, "bar" if h.score >= gate else "barlow"), ("█" * (24 - filled) + "  ", "barbg"),
                      (f"{h.meta['title']}{page}\n", "title"),
                      (h.meta["source"], "where", h.meta["source"]),
                      (f"  ·  chunk {h.id}  ·  file {h.meta.get('file', '')}\n", "where"),
                      (h.text.strip() + "\n", "chunk")]
        parts.append(("\nEvery question, with its passages, is also in the session log (Log tab).\n", "facts"))
        self.fill(self.ctx, parts)

    def show_prompt(self, r):
        """The Prompt tab: the messages, the text the model really read after its chat template, the timings."""
        g = r["generation"]
        parts = [("PROMPT\n", "h")]
        if r["outcome"] == "refused":
            parts.append(("No prompt was sent: the best passage was below the similarity gate, so the app answered "
                          "\"I don't know\" without asking a model. This is what a model would have received:\n",
                          "facts"))
        elif r["outcome"] == "passages":
            parts.append(("No prompt was sent: no language model is loaded. This is what a model would receive:\n",
                          "facts"))
        else:
            parts.append((f"{g.get('model')} · {g.get('where')} · {g.get('decoding')} · at most "
                          f"{g.get('max_new_tokens')} new tokens\n", "facts"))
        parts.append(("MESSAGES THE APP SENT\n", "h"))
        for m in r["messages"]:
            parts += [(m["role"].upper() + "\n", "role"), (m["content"].strip() + "\n", "msg")]
        if g:
            parts += [(f"WHAT THE MODEL READ: its chat template applied, {g.get('prompt_tokens')} tokens\n", "h"),
                      (g.get("prompt", "").rstrip() + "\n", "raw"),
                      (f"OUTPUT: {g.get('new_tokens')} tokens\n", "h"), (r["answer"].strip() + "\n", "answer"),
                      (f"First token after {g.get('first_token_s', 0):.1f} s · {g.get('tokens_per_s', 0):.1f} "
                       f"tokens/s · {g.get('total_s', 0):.1f} s in all\n", "facts")]
        self.fill(self.prompt_view, parts)

    def render_bold(self):
        """Small models answer in Markdown: show **this** in bold instead of with its asterisks."""
        start = self.out.index("answer_start")
        text = self.out.get(start, "end-1c")
        if "**" not in text:
            return
        self.out.configure(state="normal")
        self.out.delete(start, "end-1c")
        for i, part in enumerate(re.split(r"\*\*(.+?)\*\*", text, flags=re.S)):
            self.out.insert("end", part, ("answer_bold" if i % 2 else "answer",))
        self.out.configure(state="disabled")

    def answered(self, r):
        hits, outcome = r["hits"], r["outcome"]
        total = time.perf_counter() - r["start"]
        if outcome in ("answered", "stopped"):
            self.render_bold()
            self.write("SOURCES\n", "h")
            for i, h in enumerate(hits, 1):
                page = f", page {h.meta['page']}" if h.meta.get("page") else ""
                self.write(f"[{i}] {h.meta['title']}{page} · ", "src")
                self.write(h.meta["source"], "src", link=h.meta["source"])
                self.write(f"  ({h.score:.2f})\n", "src")
        if outcome == "passages" or (self.show_passages.get() and hits):
            self.write("RETRIEVED PASSAGES\n", "h")
            for i, h in enumerate(hits, 1):
                text = h.text.strip()
                self.write(f"[{i}] {h.score:.2f} · ", "src")
                self.write(h.meta["title"], "src", link=h.meta["source"])
                self.write("\n" + (text if len(text) <= 600 else text[:600] + " ...") + "\n", "passage")
        g = r["generation"]
        facts = [f"{len(hits)} passages", f"best similarity {hits[0].score if hits else 0:.2f} (gate {r['threshold']:.2f})",
                 f"search {r['search_ms']:.0f} ms"]
        if g:
            facts += [f"{g.get('prompt_tokens')}-token prompt", f"{g.get('new_tokens')} tokens in {g.get('total_s', 0):.1f} s "
                      f"(first after {g.get('first_token_s', 0):.1f} s, {g.get('tokens_per_s', 0):.1f} tokens/s)"]
        facts.append("stopped" if outcome == "stopped" else f"{total:.1f} s in all")
        self.write(" · ".join(facts) + "\n", "meta")
        self.show_prompt(r)
        self.session.write("question", {
            "question": r["question"], "k": r["k"], "threshold": r["threshold"], "embedding": r["embedding"],
            "db_dir": r["db_dir"], "search_ms": r["search_ms"], "outcome": outcome, "answer": r["answer"],
            "messages": r["messages"], "generation": g, "total_s": round(total, 2),
            "passages": [{"rank": i, "score": h.score, "title": h.meta["title"], "source": h.meta["source"],
                          "page": h.meta.get("page", 0), "file": h.meta.get("file", ""), "id": h.id, "text": h.text}
                         for i, h in enumerate(hits, 1)]})
        log.info("answered in %.1f s (%s)", total, outcome)
        self.status.set(f"Session log: {self.session.md_path}")

    # ---- the rest
    def save_session_log(self):
        from tkinter import filedialog
        target = filedialog.asksaveasfilename(title="Save this session's log", defaultextension=".md",
                                              initialfile=self.session.md_path.name,
                                              filetypes=[("Markdown", "*.md"), ("All files", "*.*")])
        if target:
            shutil.copyfile(self.session.md_path, target)
            log.info("session log saved as %s", target)

    def about(self):
        from tkinter import messagebox
        messagebox.showinfo("About LocalRAG",
                            f"LocalRAG {rag.APP_VERSION}\nThe Day 2 RAG lab as a desktop app. KAUST Academy.\n\n"
                            f"Program folder: {rag.app_dir()}\nSettings, logs and session logs: {rag.data_dir()}\n"
                            "Models are cached in %USERPROFILE%\\.cache\\huggingface.\n\n"
                            "Fonts: Raleway and Open Sans (SIL Open Font License).\n"
                            "Sample documents: GitLab Handbook, CC BY-SA 4.0.")

    def settings(self) -> dict:
        try:
            k, threshold = int(self.k.get()), round(float(self.threshold.get()), 2)
        except ValueError:
            k, threshold = 3, 0.40
        return {"mode": self.mode.get(), "docs": self.docs.get(), "db_new": self.db_new.get(),
                "db_old": self.db_old.get(), "embedding": self.embed_var.get(), "chunking": self.chunking(),
                "llm": self.llm_var.get(), "k": k, "threshold": threshold, "show_passages": self.show_passages.get()}

    def on_close(self):
        from tkinter import messagebox
        if self.busy and not messagebox.askyesno("LocalRAG", "Something is still running. Quit anyway?"):
            return
        try:
            SETTINGS.write_text(json.dumps(self.settings(), indent=2), encoding="utf-8")
        except OSError:
            log.warning("could not save settings", exc_info=True)
        log.info("LocalRAG closed")
        self.root.destroy()
        os._exit(0)                     # do not wait for a download or a generation thread to finish


# ----------------------------------------------------------------------------------------------- self-test (no window)

def selftest(args) -> int:
    """Run the whole pipeline without the window: proves a frozen build has every module and data file it needs."""
    import tempfile
    lines = []

    def say(text):
        lines.append(text)
        log.info(text)
        print(text, flush=True)

    code, start = 1, time.perf_counter()
    try:
        say(f"LocalRAG {rag.APP_VERSION} self-test · frozen={getattr(sys, 'frozen', False)} · "
            f"Python {sys.version.split()[0]}")
        say(rag.hardware_summary())
        say(f"brand fonts loaded: {load_brand_fonts()} of {len(FONT_FILES)} · logo bundled: "
            f"{resource('assets', 'logo_h50.png').exists()}")
        db = Path(args.db or tempfile.mkdtemp(prefix="localrag_selftest_"))
        kb = rag.KnowledgeBase.build(args.docs, db, rag.embedding_by_id(args.embed), args.chunking)
        say(f"built in {time.perf_counter() - start:.1f} s: {kb.summary()}")
        kb = rag.KnowledgeBase.open(db)
        say(f"reopened from {db}: {kb.count} chunks")
        hits = kb.search(args.question, 3)
        for h in hits:
            say(f"  {h.score:.2f}  {h.meta['title'][:40]:<40} | {h.text[:90]!r}")
        session = SessionLog(Path(tempfile.mkdtemp(prefix="localrag_selftest_log_")), {"version": rag.APP_VERSION})
        record = {"question": args.question, "k": 3, "threshold": 0.40, "embedding": kb.embedder.choice.label,
                  "db_dir": str(db), "search_ms": 0.0, "outcome": "passages", "answer": "", "messages": [],
                  "generation": {}, "passages": [{"rank": i, "score": h.score, "title": h.meta["title"],
                                                  "source": h.meta["source"], "page": 0, "id": h.id, "text": h.text}
                                                 for i, h in enumerate(hits, 1)]}
        if args.llm:
            llm, t = rag.ChatModel(), time.perf_counter()
            llm.load(args.llm)
            say(f"loaded {llm.name} in {time.perf_counter() - t:.1f} s on {llm.where}")
            reply = "".join(llm.stream(rag.prompt_messages(args.question, hits), max_new_tokens=args.max_tokens))
            g = llm.last
            say(f"answered: {g['prompt_tokens']}-token prompt, {g['new_tokens']} tokens in {g['total_s']:.1f} s "
                f"(first after {g['first_token_s']:.1f} s): {reply.strip()}")
            record.update(outcome="answered", answer=reply, generation=g)
        session.write("question", record)
        say(f"session log written: {session.md_path.stat().st_size:,} bytes")
        say("SELFTEST OK")
        code = 0
    except Exception:
        say(traceback.format_exc())
        say("SELFTEST FAILED")
    if args.out:
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return code


def main():
    multiprocessing.freeze_support()     # a frozen app that starts helper processes would otherwise start itself again
    log_dir = setup_logging()
    parser = argparse.ArgumentParser(description="LocalRAG: ask your documents.")
    parser.add_argument("--selftest", action="store_true", help="run the pipeline without the window, then exit")
    parser.add_argument("--docs", default=str(rag.app_dir() / "sample_docs" / "gitlab_handbook"))
    parser.add_argument("--db", help="where the self-test builds its DB (default: a temporary folder)")
    parser.add_argument("--embed", default=rag.EMBEDDINGS[0].id)
    parser.add_argument("--chunking", default="sections", choices=list(rag.CHUNKERS))
    parser.add_argument("--llm", help="also load this model and answer, e.g. Qwen/Qwen3.5-0.8B")
    parser.add_argument("--question", default=MEAL_QUESTION)
    parser.add_argument("--max-tokens", type=int, default=150)
    parser.add_argument("--out", help="also write the self-test report to this file")
    args = parser.parse_args()
    if args.selftest:
        sys.exit(selftest(args))

    if sys.platform == "win32":          # sharp text on high-DPI screens instead of a blurry, stretched window
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    fonts_added = load_brand_fonts()
    import tkinter as tk
    try:
        settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    root = tk.Tk()
    scale = root.winfo_fpixels("1i") / 96
    fonts = Fonts(root)
    apply_style(root, fonts, scale)
    App(root, settings, log_dir, fonts, scale)
    root.update()
    log.info("LocalRAG %s window shown %.1f s after the process started (frozen=%s, brand fonts %s, %d font files)",
             rag.APP_VERSION, seconds_since_start(), getattr(sys, "frozen", False),
             "loaded" if fonts.brand else "missing", fonts_added)
    root.mainloop()


if __name__ == "__main__":
    main()
