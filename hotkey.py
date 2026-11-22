# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["pywin32>=306; sys_platform == 'win32'", "sympy>=1.13", "antlr4-python3-runtime==4.11.*",
#                 "matplotlib>=3.8"]
# ///
"""Global hotkey: copy LaTeX, press the hotkey, get the TI-Nspire answer.

    uv run --script hotkey.py                      # with a console window
    uv run --gui-script hotkey.py                  # no console (quit from the tray icon)

Ctrl+Alt+Shift+L   solve/evaluate the LaTeX on the clipboard (action "auto")
Ctrl+Alt+Shift+K   same, but the decimal value becomes the answer
Ctrl+Alt+Shift+J   pick an action (factor, expand, solve, integrate, ...) for it
Ctrl+Alt+Shift+Q   quit

The answer replaces the clipboard (as LaTeX, see --copy) and shows in a popup
in the bottom-right corner, drawn as math when matplotlib can draw it; click
the popup to close it. The tray icon's menu lists the last answers: click one
to copy it again. Needs TI-Nspire open on the bridge page, like the MCP server.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import ctypes
import io
import math
import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Started without a console (--gui-script / uvw), stdout and stderr are None.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")  # noqa: SIM115

from nspire_latex import ACTIONS, LatexError, format_answer, plan, run_plan  # noqa: E402

# The Windows parts (hotkeys, tray icon, clipboard) load only on Windows, so the
# rest of this module (history, action list, math rendering) imports anywhere
# for the offline tests.
if sys.platform == "win32":
    import ctypes.wintypes as wt

    import win32api
    import win32con
    import win32event
    import win32gui
    import winerror

    user32 = ctypes.windll.user32

WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000

# hotkey ids
SOLVE, APPROX, PICK, QUIT = 1, 2, 3, 4
# tray menu ids; history entries are MENU_HISTORY + index
MENU_SOLVE, MENU_PICK, MENU_CLEAR, MENU_QUIT, MENU_HISTORY = 1, 2, 3, 4, 100
HISTORY_SIZE = 10


def parse_hotkey(spec: str) -> tuple[int, int]:
    *mods, key = [p.strip().lower() for p in spec.split("+")]
    m = MOD_NOREPEAT
    for x in mods:
        if x not in MODS:
            raise SystemExit(f"Unknown modifier {x!r} in {spec!r}")
        m |= MODS[x]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    else:
        raise SystemExit(f"Unsupported key {key!r} in {spec!r} (use a letter, digit or F1-F24)")
    return m, vk


def pretty_hotkey(spec: str) -> str:
    """'ctrl+alt+shift+l' -> 'Ctrl+Alt+Shift+L'."""
    return "+".join(p.strip().capitalize() for p in spec.split("+"))


def short(s: str, n: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- history

@dataclass(frozen=True)
class Answer:
    latex: str                # what was solved
    action: str               # e.g. "solve for x"
    result: str               # TI-Nspire's answer as text
    result_latex: str | None  # the same as LaTeX, if it converted

    def copy_text(self, mode: str) -> str:
        """What clicking this answer in the tray menu copies.

        LaTeX, like the hotkey, unless --copy text (the TI-Nspire text). With
        --copy none the hotkey leaves the clipboard alone, but a click in the
        menu is an explicit request, so it copies the LaTeX.
        """
        if mode == "text" or not self.result_latex:
            return self.result
        return self.result_latex

    def menu_label(self) -> str:
        # Tab right-aligns the input in its own column; & would underline the next letter.
        label = f"{short(self.result or '(empty)', 48)}\t{short(self.latex, 28)}"
        return label.replace("&", "&&")


class History:
    """The last answers, newest first. Thread-safe: the tray thread reads it."""

    def __init__(self, size: int = HISTORY_SIZE):
        self._items: deque[Answer] = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, answer: Answer) -> None:
        with self._lock:
            with contextlib.suppress(ValueError):
                self._items.remove(answer)  # the same answer again moves to the top
            self._items.appendleft(answer)

    def items(self) -> list[Answer]:
        with self._lock:
            return list(self._items)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


MenuItem = tuple[int | None, str, bool, object]  # (id or None for a separator, label, enabled, data)


def tray_menu(history: History, hotkeys: dict[int, str]) -> list[MenuItem]:
    """The tray menu as built when it opens: recent answers, then commands.

    Each answer item carries its Answer, so a result that arrives while the
    menu is open can't shift which answer a click copies.
    """
    answers = history.items()
    items: list[MenuItem] = []
    if answers:
        items += [(MENU_HISTORY + i, a.menu_label(), True, a) for i, a in enumerate(answers)]
    else:
        items.append((0, "No answers yet", False, None))
    items += [(None, "", False, None),
              (MENU_SOLVE, f"Solve clipboard\t{pretty_hotkey(hotkeys[SOLVE])}", True, None),
              (MENU_PICK, f"Pick an action…\t{pretty_hotkey(hotkeys[PICK])}", True, None)]
    if answers:
        items.append((MENU_CLEAR, "Clear history", True, None))
    items += [(None, "", False, None),
              (MENU_QUIT, f"Quit\t{pretty_hotkey(hotkeys[QUIT])}", True, None)]
    return items


# ---------------------------------------------------------------- action picker data

# What the picker offers, most used first (names from nspire_latex.ACTIONS).
# Left out: eval (the solve hotkey does it), desolve (picked automatically for
# y' or dy/dx), define (f(x)=... is stored automatically) and czeros.
PICKER_ACTIONS = ["factor", "expand", "simplify", "solve", "diff", "integrate", "limit", "approx",
                  "taylor", "zeros", "nsolve", "csolve", "cfactor", "propfrac", "comdenom",
                  "texpand", "tcollect", "det", "inverse", "rref", "eigenvalues", "eigenvectors"]
# Input some actions take besides the variable: action -> (label, default, plan() keyword)
PICKER_EXTRA = {"limit": ("towards", "0", "point"), "taylor": ("order", "5", "order")}
_SOLVE_LIKE = ("solve", "csolve", "nsolve")  # these take a list of variables for a system


def needs_var(action: str) -> bool:
    return ACTIONS[action][1]


def action_label(action: str) -> str:
    desc = ACTIONS[action][2]
    return action if desc == action else f"{action}  ·  {desc}"


def filter_actions(query: str, actions: list[str] | None = None) -> list[str]:
    """Picker actions matching what was typed: name prefixes first, then any name or description match."""
    actions = PICKER_ACTIONS if actions is None else actions
    q = query.strip().lower()
    if not q:
        return list(actions)
    first = [a for a in actions if a.startswith(q)]
    rest = [a for a in actions if a not in first and (q in a or q in ACTIONS[a][2].lower())]
    return first + rest


def picker_defaults(latex: str) -> dict[str, str]:
    """The variable(s) each picker action that needs one would use, as plan() picks them.

    '' where plan() finds none. Two plans cover them all: solve-like actions
    take every unknown of a system, the others the first free name.
    """
    def variables(action: str) -> str:
        try:
            return ", ".join(plan(latex, action).variables)
        except LatexError:
            return ""
    many, one = variables("solve"), variables("diff")
    return {a: (many if a in _SOLVE_LIKE else one) for a in PICKER_ACTIONS if needs_var(a)}


def parse_extra(action: str, text: str) -> dict:
    """plan() keyword for the picker's extra field (limit point, Taylor order). Raises LatexError."""
    if action not in PICKER_EXTRA:
        return {}
    label, default, key = PICKER_EXTRA[action]
    text = text.strip() or default
    if key == "order":
        if not text.isdigit() or not 0 < int(text) <= 50:
            raise LatexError(f"The Taylor order must be a whole number from 1 to 50, not {text!r}.")
        return {"order": int(text)}
    return {key: text}


# ---------------------------------------------------------------- math rendering

def _display_fracs(s: str) -> str:
    r"""\frac -> \dfrac outside superscripts and subscripts.

    mathtext draws \frac in text style, which is small next to the 15 pt answer;
    exponents keep \frac so they stay small.
    """
    out: list[str] = []
    depth = 0
    scripts: list[int] = []  # brace depths at which a ^{ or _{ group opened
    pending = False          # just after ^ or _
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            j = i + 1
            while j < n and s[j].isalpha():
                j += 1
            if j == i + 1:
                j = min(i + 2, n)  # \{ \} \, \\ and the like: one character
            cmd = s[i:j]
            if cmd == r"\frac" and not scripts and not pending:
                cmd = r"\dfrac"
            out.append(cmd)
            pending = False
            i = j
            continue
        if c in "^_":
            pending = True
        elif c == "{":
            depth += 1
            if pending:
                scripts.append(depth)
                pending = False
        elif c == "}":
            if scripts and scripts[-1] == depth:
                scripts.pop()
            depth -= 1
        elif not c.isspace():
            pending = False  # x^2: a one-character exponent
        out.append(c)
        i += 1
    return "".join(out)


def mathtext_source(latex: str | None) -> str | None:
    """result LaTeX rewritten for matplotlib's mathtext, or None if mathtext can't draw it."""
    if not latex or not latex.strip() or "$" in latex or "\\begin" in latex or "\n" in latex:
        return None  # matrices (\begin{matrix}) aren't in mathtext
    s = latex.replace(r"\left|", "|").replace(r"\right|", "|")  # mathtext's \left| is drawn badly
    s = s.replace(r"\nolimits", "").replace(r"\limits", "")  # unevaluated ∫ results; mathtext has no \limits
    return "$" + _display_fracs(s) + "$"


class MathRenderer:
    """Draws result LaTeX as a PNG with matplotlib's mathtext (no TeX install needed).

    matplotlib takes a second or two to import, so warm_up() imports it on a
    background thread at startup; render() runs on the solve worker, never on
    the Tk thread. Without matplotlib, or for LaTeX mathtext can't draw,
    render() returns None and the popup shows text as before.
    """

    PAD = 3  # points around the formula

    def __init__(self, dpi: float = 96, max_width: int = 520, max_height: int = 400, size: float = 15,
                 fg: str = "#f0f3f6", bg: str = "#1f2328", min_size: float = 9):
        self.dpi, self.max_width, self.max_height = dpi, max_width, max_height
        self.size, self.min_size, self.fg, self.bg = size, min_size, fg, bg
        self._lock = threading.Lock()
        self._loaded = threading.Event()
        self._warming = False
        self._mpl: tuple | None = None
        self.error: str | None = None

    def load(self) -> bool:
        with self._lock:
            if self._mpl is None and self.error is None:
                try:
                    import matplotlib
                    from matplotlib.backends.backend_agg import FigureCanvasAgg
                    from matplotlib.figure import Figure
                    from matplotlib.font_manager import FontProperties
                    from matplotlib.mathtext import MathTextParser
                    self._mpl = (matplotlib, MathTextParser("path"), FontProperties, Figure, FigureCanvasAgg)
                except Exception as e:  # not installed, or a broken install: draw text instead
                    self.error = f"{type(e).__name__}: {e}"
            self._loaded.set()
        return self._mpl is not None

    def warm_up(self) -> None:
        self._warming = True
        threading.Thread(target=self.load, daemon=True, name="matplotlib-import").start()

    def render(self, latex: str | None, wait_s: float = 2) -> bytes | None:
        """PNG bytes, or None to show text. Waits at most wait_s for a warm-up import."""
        src = mathtext_source(latex)
        if src is None:
            return None
        if self._warming:
            self._loaded.wait(wait_s)
        else:
            self.load()
        if self._mpl is None:
            return None
        with self._lock:
            try:
                return self._draw(src)
            except Exception:  # mathtext can't parse it: text instead
                return None

    def _draw(self, src: str) -> bytes | None:
        matplotlib, parser, FontProperties, Figure, FigureCanvasAgg = self._mpl  # type: ignore[misc]
        px = self.dpi / 72
        with matplotlib.rc_context({"mathtext.fontset": "cm"}):
            size = self.size
            while True:
                prop = FontProperties(size=size)
                w, h, d, _, _ = parser.parse(src, dpi=72, prop=prop)
                width = (w + 2 * self.PAD) * px
                if width <= self.max_width or size <= self.min_size:
                    break
                size = max(self.min_size, math.floor(size * self.max_width / width * 10) / 10)
            if width > self.max_width or (h + 2 * self.PAD) * px > self.max_height:
                return None  # too big even small: the text label wraps instead
            fig = Figure(figsize=((w + 2 * self.PAD) / 72, (h + 2 * self.PAD) / 72), facecolor=self.bg)
            FigureCanvasAgg(fig)
            fig.text(self.PAD / (w + 2 * self.PAD), (d + self.PAD) / (h + 2 * self.PAD), src,
                     fontproperties=prop, color=self.fg)
            buf = io.BytesIO()
            fig.savefig(buf, dpi=self.dpi, format="png", facecolor=self.bg)
            return buf.getvalue()


# ---------------------------------------------------------------- tray icon

def _icon_rgba(size: int) -> list[tuple[int, int, int, int]]:
    """Pixels, top row first, of a green square-root sign on a dark rounded square."""
    bg, fg = (0x1F, 0x23, 0x28), (0x7E, 0xE7, 0x87)
    margin, radius, half_stroke = 0.03, 0.2, 0.055
    pts = [(0.16, 0.57), (0.29, 0.50), (0.45, 0.80), (0.63, 0.19), (0.86, 0.19)]
    segs = list(zip(pts, pts[1:], strict=False))
    ss = 4  # supersampling per axis

    def in_square(u: float, v: float) -> bool:
        lo, hi = margin + radius, 1 - margin - radius
        dx, dy = max(lo - u, 0.0, u - hi), max(lo - v, 0.0, v - hi)
        return dx * dx + dy * dy <= radius * radius and margin <= u <= 1 - margin and margin <= v <= 1 - margin

    def on_stroke(u: float, v: float) -> bool:
        for (x1, y1), (x2, y2) in segs:
            vx, vy = x2 - x1, y2 - y1
            t = max(0.0, min(1.0, ((u - x1) * vx + (v - y1) * vy) / (vx * vx + vy * vy)))
            if (u - x1 - t * vx) ** 2 + (v - y1 - t * vy) ** 2 <= half_stroke * half_stroke:
                return True
        return False

    out = []
    for y in range(size):
        for x in range(size):
            inside = stroke = 0
            for sy in range(ss):
                for sx in range(ss):
                    u, v = (x + (sx + 0.5) / ss) / size, (y + (sy + 0.5) / ss) / size
                    if in_square(u, v):
                        inside += 1
                        stroke += on_stroke(u, v)
            if not inside:
                out.append((0, 0, 0, 0))
                continue
            f = stroke / inside
            rgb = tuple(round(b + (g - b) * f) for b, g in zip(bg, fg, strict=True))
            out.append((*rgb, round(255 * inside / ss / ss)))
    return out


def icon_dib(size: int = 32) -> bytes:
    """The icon as CreateIconFromResourceEx wants it: BITMAPINFOHEADER (height
    doubled for the mask), 32-bit BGRA rows bottom-up, then an all-zero AND
    mask (the alpha channel decides transparency)."""
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    px = _icon_rgba(size)
    rows = [b"".join(struct.pack("BBBB", b, g, r, a) for r, g, b, a in px[y * size:(y + 1) * size])
            for y in reversed(range(size))]
    mask = bytes((size + 31) // 32 * 4 * size)
    return header + b"".join(rows) + mask


class TrayIcon(threading.Thread):
    """Notification-area icon on its own thread, with pywin32's win32gui.

    pywin32 is already needed for the hotkeys and clipboard, so the icon costs
    no extra dependency (pystray + Pillow would). The menu comes from menu()
    each time it opens; a chosen item goes to the event queue as
    ("menu", id, data) for the Tk thread.
    """

    CALLBACK = 0x0400 + 20  # WM_USER + 20

    def __init__(self, tip: str, menu: Callable[[], list[MenuItem]], events: queue.Queue):
        super().__init__(daemon=True, name="tray")
        self.tip, self.menu, self.events = tip, menu, events
        self.hwnd = None
        self.hicon = None
        self.ready = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        try:
            self._create()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            self.ready.set()
            return
        self.ready.set()
        win32gui.PumpMessages()  # until WM_DESTROY posts WM_QUIT
        if self.hicon:
            with contextlib.suppress(Exception):
                win32gui.DestroyIcon(self.hicon)

    def _create(self) -> None:
        hinst = win32api.GetModuleHandle(None)
        taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        wc = win32gui.WNDCLASS()
        # pywin32's stubs call these read-only; pywin32 itself sets them (see its win32gui_taskbar demo).
        wc.hInstance = hinst  # pyright: ignore[reportAttributeAccessIssue]
        wc.lpszClassName = f"NspireHotkeyTray{id(self)}"  # pyright: ignore[reportAttributeAccessIssue]
        wc.lpfnWndProc = {  # pyright: ignore[reportAttributeAccessIssue]
            self.CALLBACK: self._on_notify,
            taskbar_created: self._on_taskbar_created,  # Explorer restarted
            win32con.WM_DESTROY: self._on_destroy}
        atom = win32gui.RegisterClass(wc)
        # A hidden top-level window rather than a message-only one: only
        # top-level windows get the TaskbarCreated broadcast.
        self.hwnd = win32gui.CreateWindow(atom, "nspire hotkey", 0, 0, 0, 0, 0, 0, 0, hinst, None)
        self.hicon = _make_hicon(win32api.GetSystemMetrics(win32con.SM_CXSMICON))
        self._add()

    def _add(self) -> bool:
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        try:
            win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (self.hwnd, 0, flags, self.CALLBACK, self.hicon, self.tip))
            return True
        except Exception as e:  # taskbar not up yet (start at login): TaskbarCreated adds it later
            print(f"tray icon not added yet: {e}", file=sys.stderr)
            return False

    def _on_taskbar_created(self, hwnd, msg, wparam, lparam):
        self._add()
        return 0

    def _on_notify(self, hwnd, msg, wparam, lparam):
        if lparam in (win32con.WM_LBUTTONUP, win32con.WM_RBUTTONUP):
            try:
                self._show_menu()
            except Exception as e:
                print(f"tray menu failed: {e}", file=sys.stderr)
        return 0

    def _show_menu(self) -> None:
        items = self.menu()
        data = {}
        m = win32gui.CreatePopupMenu()
        try:
            for cmd, label, enabled, payload in items:
                if cmd is None:
                    win32gui.AppendMenu(m, win32con.MF_SEPARATOR, 0, "")  # pyright: ignore[reportCallIssue]
                else:
                    flags = win32con.MF_STRING | (0 if enabled else win32con.MF_GRAYED)
                    win32gui.AppendMenu(m, flags, cmd, label)  # pyright: ignore[reportCallIssue]
                    data[cmd] = payload
            x, y = win32gui.GetCursorPos()
            # Without this the menu doesn't close when you click elsewhere.
            with contextlib.suppress(Exception):
                win32gui.SetForegroundWindow(self.hwnd)
            flags = win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY | win32con.TPM_RIGHTBUTTON
            cmd = win32gui.TrackPopupMenu(m, flags, x, y, 0, self.hwnd, None)
            win32gui.PostMessage(self.hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(m)  # pyright: ignore[reportCallIssue]
        if cmd:
            self.events.put(("menu", cmd, data.get(cmd)))

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        with contextlib.suppress(Exception):
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
        win32gui.PostQuitMessage(0)
        return 0

    def stop(self, wait_s: float = 2) -> None:
        """Remove the icon (a killed thread would leave it until the mouse passes over it)."""
        if self.hwnd:
            with contextlib.suppress(Exception):
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
            self.join(wait_s)


def _make_hicon(size: int) -> int:
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.CreateIconFromResourceEx.argtypes = [ctypes.c_char_p, wt.DWORD, wt.BOOL, wt.DWORD,
                                             ctypes.c_int, ctypes.c_int, wt.UINT]
    u32.CreateIconFromResourceEx.restype = wt.HICON
    data = icon_dib(size)
    h = u32.CreateIconFromResourceEx(data, len(data), True, 0x00030000, size, size, 0)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


# ---------------------------------------------------------------- hotkey thread

class HotkeyThread(threading.Thread):
    def __init__(self, bindings: dict[int, tuple[int, int, str]], events: queue.Queue):
        super().__init__(daemon=True)
        self.bindings, self.events = bindings, events
        self.thread_id = None
        self.failed: list[str] = []
        self.ready = threading.Event()

    def run(self):
        self.thread_id = win32api.GetCurrentThreadId()
        for hid, (mods, vk, label) in self.bindings.items():
            if not user32.RegisterHotKey(None, hid, mods, vk):
                self.failed.append(label)
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.events.put(("hotkey", msg.wParam))
        for hid in self.bindings:
            user32.UnregisterHotKey(None, hid)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


# ---------------------------------------------------------------- popup

BG, BG2, FG, DIM, ERR, ACC = "#1f2328", "#2d333b", "#f0f3f6", "#9198a1", "#ff7b72", "#7ee787"


class Popup:
    def __init__(self, root: tk.Tk, wrap: int = 520):
        self.root = root
        self.wrap = wrap
        self.win: tk.Toplevel | None = None
        self.after_id = None
        self.image: tk.PhotoImage | None = None  # Tk drops an image nothing in Python refers to

    def show(self, title: str, body: str, detail: str = "", footer: str = "", error: bool = False,
             seconds: float = 12, image: tk.PhotoImage | None = None):
        """image, if given, is drawn instead of the body text."""
        self.close()
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=BG, padx=14, pady=10)
        wrap = self.wrap
        tk.Label(w, text=title, bg=BG, fg=ERR if error else DIM, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=wrap).pack(fill="x")
        if image is not None:
            tk.Label(w, image=image, bg=BG, anchor="w").pack(fill="x", pady=(4, 4))
        else:
            tk.Label(w, text=body, bg=BG, fg=FG, font=("Cambria Math", 15), anchor="w",
                     justify="left", wraplength=wrap).pack(fill="x", pady=(2, 2))
        self.image = image
        if detail:
            tk.Label(w, text=detail, bg=BG, fg=FG, font=("Cambria Math", 11), anchor="w",
                     justify="left", wraplength=wrap).pack(fill="x")
        if footer:
            tk.Label(w, text=footer, bg=BG, fg=ACC if not error else DIM, font=("Segoe UI", 8),
                     anchor="w", justify="left", wraplength=wrap).pack(fill="x", pady=(4, 0))
        for widget in [w, *w.winfo_children()]:
            widget.bind("<Button-1>", lambda e: self.close())
        w.update_idletasks()
        x = w.winfo_screenwidth() - w.winfo_reqwidth() - 24
        y = w.winfo_screenheight() - w.winfo_reqheight() - 72
        w.geometry(f"+{x}+{y}")
        self.win = w
        if seconds:
            self.after_id = self.root.after(int(seconds * 1000), self.close)

    def close(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if self.win is not None:
            self.win.destroy()
            self.win = None
        self.image = None


# ---------------------------------------------------------------- action picker

class ActionPicker:
    """Small window listing PICKER_ACTIONS for the LaTeX that was on the clipboard.

    Type to filter, Up/Down to move, Enter or a click to pick, Esc to close.
    An action that needs a variable then asks for it (prefilled with the one
    plan() would pick), and limit/taylor for their point/order.
    on_pick(action, var, extra) gets the choice; extra holds plan() keywords.
    """

    def __init__(self, root: tk.Misc, latex: str, defaults: dict[str, str],
                 on_pick: Callable[[str, str | None, dict], None], on_close: Callable[[], None] | None = None,
                 show: bool = True):
        self.root, self.latex, self.defaults = root, latex, defaults
        self.on_pick, self.on_close = on_pick, on_close
        self.action: str | None = None  # set once an action needing input is picked
        self.shown: list[str] = []
        self.win: tk.Toplevel | None = tk.Toplevel(root)
        w = self.win
        if not show:
            w.withdraw()
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=BG, padx=12, pady=10, highlightthickness=1, highlightbackground=BG2)

        self.title = tk.Label(w, text="Action for  " + short(latex, 70), bg=BG, fg=DIM, font=("Segoe UI", 9),
                              anchor="w", justify="left", wraplength=420)
        self.title.pack(fill="x")
        self.query = tk.StringVar(master=w)
        entry_opts = dict(bg=BG2, fg=FG, insertbackground=FG, relief="flat", font=("Segoe UI", 11),
                          highlightthickness=1, highlightbackground=BG2, highlightcolor=ACC)
        self.entry = tk.Entry(w, textvariable=self.query, **entry_opts)
        self.entry.pack(fill="x", pady=(6, 4))
        self.list = tk.Listbox(w, height=min(12, len(PICKER_ACTIONS)), bg=BG, fg=FG, font=("Segoe UI", 11),
                               selectbackground=BG2, selectforeground=ACC, activestyle="none", relief="flat",
                               highlightthickness=0, borderwidth=0, exportselection=False, width=34)
        self.list.pack(fill="both")

        # second step: the variable (and the limit point or Taylor order)
        self.form = tk.Frame(w, bg=BG)
        self.var_label = tk.Label(self.form, text="Variable", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.var_entry = tk.Entry(self.form, **entry_opts, width=16)
        self.extra_label = tk.Label(self.form, text="", bg=BG, fg=DIM, font=("Segoe UI", 9))
        self.extra_entry = tk.Entry(self.form, **entry_opts, width=16)
        self.var_label.grid(row=0, column=0, sticky="w", padx=(0, 8), pady=2)
        self.var_entry.grid(row=0, column=1, sticky="we", pady=2)
        self.form.columnconfigure(1, weight=1)

        self.hint = tk.Label(w, text="Type to filter · ↑↓ · Enter picks · Esc closes", bg=BG, fg=DIM,
                             font=("Segoe UI", 8), anchor="w")
        self.hint.pack(fill="x", pady=(4, 0))

        self.query.trace_add("write", lambda *_: self._refilter())
        for widget in (self.entry, self.var_entry, self.extra_entry):
            widget.bind("<Return>", lambda e: self._enter())
            widget.bind("<Escape>", lambda e: self._escape())
        self.entry.bind("<Down>", lambda e: self._move(1))
        self.entry.bind("<Up>", lambda e: self._move(-1))
        self.list.bind("<ButtonRelease-1>", self._clicked)
        w.bind("<FocusOut>", lambda e: self.root.after(200, self._focus_check))
        self._timeout = self.root.after(120_000, self.close)  # never left on screen for good
        self._refilter()
        if show:
            self._place()
            self._focus()

    # -- layout and focus

    def _place(self) -> None:
        w = self.win
        if w is None:
            return
        w.update_idletasks()
        px, py = w.winfo_pointerxy()
        ww, wh = w.winfo_reqwidth(), w.winfo_reqheight()
        x = max(8, min(px - ww // 2, w.winfo_screenwidth() - ww - 8))
        y = max(8, min(py - 20, w.winfo_screenheight() - wh - 56))
        w.geometry(f"+{x}+{y}")

    def _focus(self) -> None:
        w = self.win
        if w is None:
            return
        w.lift()
        w.focus_force()
        (self.var_entry if self.action else self.entry).focus_set()
        if sys.platform == "win32":
            # The hotkey made this process the last one to get input, so Windows
            # lets it take the foreground; Tk alone may only flash the taskbar.
            with contextlib.suppress(Exception):
                user32.SetForegroundWindow(int(w.wm_frame(), 16))

    def _focus_check(self) -> None:
        if self.win is None:
            return
        try:
            focus = self.win.focus_get()
        except (KeyError, tk.TclError):
            focus = None
        if focus is None:  # focus went to another program: close like a menu
            self.close()

    # -- list step

    def _refilter(self) -> None:
        self.shown = filter_actions(self.query.get())
        self.list.configure(height=max(1, min(12, len(self.shown))))
        self.list.delete(0, "end")
        for a in self.shown:
            self.list.insert("end", action_label(a))
        if self.shown:
            self.list.selection_set(0)
            self.list.see(0)

    def _move(self, step: int) -> str:
        if self.shown:
            cur = self.list.curselection()
            i = max(0, min(len(self.shown) - 1, (cur[0] if cur else -1) + step))
            self.list.selection_clear(0, "end")
            self.list.selection_set(i)
            self.list.see(i)
        return "break"

    def selected(self) -> str | None:
        cur = self.list.curselection()
        return self.shown[cur[0]] if cur and self.shown else None

    def _clicked(self, event) -> None:
        if self.action is None and self.shown:
            i = self.list.nearest(event.y)
            self.list.selection_clear(0, "end")
            self.list.selection_set(i)
            self.choose(self.shown[i])

    def choose(self, action: str) -> None:
        """Pick an action: run it, or ask for its variable first."""
        if not needs_var(action):
            self._done(action, None, {})
            return
        self.action = action
        self.list.pack_forget()
        self.entry.pack_forget()
        self.title.configure(text=f"{action_label(action)}  for  {short(self.latex, 50)}")
        self.var_label.configure(text="Variables" if action in _SOLVE_LIKE else "Variable")
        self.var_entry.delete(0, "end")
        self.var_entry.insert(0, self.defaults.get(action, ""))
        self.var_entry.select_range(0, "end")
        if action in PICKER_EXTRA:
            label, default, _ = PICKER_EXTRA[action]
            self.extra_label.configure(text=label)
            self.extra_entry.delete(0, "end")
            self.extra_entry.insert(0, default)
            self.extra_label.grid(row=1, column=0, sticky="w", padx=(0, 8), pady=2)
            self.extra_entry.grid(row=1, column=1, sticky="we", pady=2)
        self.form.pack(fill="x", pady=(6, 0), before=self.hint)
        self.hint.configure(text="Enter runs · Esc goes back")
        self.var_entry.focus_set()

    # -- form step

    def submit(self) -> None:
        if self.action is None:
            return
        extra_text = self.extra_entry.get() if self.action in PICKER_EXTRA else ""
        try:
            extra = parse_extra(self.action, extra_text)
        except LatexError as e:
            self.hint.configure(text=str(e), fg=ERR)
            return
        var = self.var_entry.get().strip() or None
        self._done(self.action, var, extra)

    def back(self) -> None:
        self.action = None
        self.form.pack_forget()
        self.extra_label.grid_forget()
        self.extra_entry.grid_forget()
        self.title.configure(text="Action for  " + short(self.latex, 70))
        self.entry.pack(fill="x", pady=(6, 4), before=self.hint)
        self.list.pack(fill="both", before=self.hint)
        self.hint.configure(text="Type to filter · ↑↓ · Enter picks · Esc closes", fg=DIM)
        self.entry.focus_set()

    def _enter(self) -> str:
        if self.action is not None:
            self.submit()
        elif (a := self.selected()) is not None:
            self.choose(a)
        return "break"

    def _escape(self) -> str:
        if self.action is not None:
            self.back()
        else:
            self.close()
        return "break"

    def _done(self, action: str, var: str | None, extra: dict) -> None:
        self.close()
        self.on_pick(action, var, extra)

    def close(self) -> None:
        if self.win is None:
            return
        with contextlib.suppress(tk.TclError):
            self.root.after_cancel(self._timeout)
        win, self.win = self.win, None
        win.destroy()
        if self.on_close:
            self.on_close()


# ---------------------------------------------------------------- app

@dataclass
class Job:
    action: str = "auto"
    numeric: bool = False      # the decimal answer becomes the main one
    latex: str | None = None   # None: read the clipboard when the job starts
    var: str | None = None
    extra: dict | None = None  # plan() keywords: order, point


@dataclass
class Clipboard:
    get: Callable[[], str | None]
    set: Callable[[str], None]
    is_traffic: Callable[[str | None], bool]


class App:
    """Event handling on the Tk thread; CAS work on worker threads.

    Workers and the hotkey/tray threads talk to the Tk thread only through
    events: ("hotkey", id), ("menu", id, data), ("result", latex, r, copied,
    png), ("error", title, message, latex), ("picker", latex, defaults),
    ("copied", text).
    """

    def __init__(self, args, root, popup, bridge, events: queue.Queue, clipboard: Clipboard,
                 renderer: MathRenderer | None = None):
        self.args, self.root, self.popup, self.bridge = args, root, popup, bridge
        self.events, self.clip, self.renderer = events, clipboard, renderer
        self.history = History()
        self.busy = threading.Event()
        self.started = 0.0
        self.preview = ""
        self.picker: ActionPicker | None = None

    # -- events

    def handle(self, ev) -> bool:
        """Handle one event; False once the app should quit."""
        kind = ev[0]
        if kind == "hotkey":
            if ev[1] == QUIT:
                return self.quit()
            if ev[1] == PICK:
                self.open_picker()
            else:
                self.request(Job(numeric=ev[1] == APPROX))
        elif kind == "menu":
            _, cmd, data = ev
            if cmd == MENU_QUIT:
                return self.quit()
            if cmd == MENU_SOLVE:
                self.request(Job())
            elif cmd == MENU_PICK:
                self.open_picker()
            elif cmd == MENU_CLEAR:
                self.history.clear()
            elif isinstance(data, Answer):
                self.copy_answer(data)
        elif kind == "result":
            _, latex, r, copied, png = ev
            self.history.add(Answer(latex, r["action"], r["result"], r.get("result_latex")))
            detail = (f"≈ {r['approx']}" if r.get("approx")
                      else f"exact: {r['exact']}" if r.get("exact") else "")
            self.popup.show(f"{r['action']}:  {short(r['nspire'], 140)}", r["result"] or "(empty)",
                            detail=detail, footer=copied, seconds=15, image=self._photo(png))
        elif kind == "error":
            _, title, msg, latex = ev
            self.show_error(title, msg, latex)
        elif kind == "picker":
            _, latex, defaults = ev
            self.show_picker(latex, defaults)
        elif kind == "copied":
            self.popup.show("Copied to the clipboard", short(ev[1], 300), seconds=4)
        return True

    def quit(self) -> bool:
        self.close_picker()
        self.popup.close()
        self.root.quit()
        return False

    def show_error(self, title: str, msg: str, latex: str = "") -> None:
        try:
            self.popup.show(title, short(msg, 300), footer=short(latex, 120), error=True, seconds=10)
        except Exception as e:  # the popup itself failed; don't take the event loop down
            print(f"{title}: {msg} (popup failed: {e})", file=sys.stderr)

    def show_busy(self) -> None:
        secs = time.monotonic() - self.started
        self.popup.show("Still solving…", self.preview or "(the clipboard)",
                        footer=f"{secs:.0f} s so far. The hotkey works again once the answer is in.",
                        seconds=0)

    def _photo(self, png: bytes | None):
        if not png:
            return None
        try:
            return tk.PhotoImage(master=self.root, data=base64.b64encode(png).decode("ascii"))
        except Exception:
            return None

    # -- solving

    def request(self, job: Job) -> None:
        if self.busy.is_set():
            self.show_busy()
        else:
            self.start(job)

    def start(self, job: Job) -> None:
        self.busy.set()
        try:
            self.started = time.monotonic()
            if job.latex is not None:
                preview = job.latex
            else:
                try:
                    preview = self.clip.get() or ""
                except Exception:  # clipboard locked by another program; solve() retries
                    preview = ""
            self.preview = short(preview, 120)
            title = "Solving…" if job.action == "auto" else f"{action_label(job.action)}…"
            self.popup.show(title, self.preview, seconds=0)
            threading.Thread(target=self.solve, args=(job,), daemon=True).start()
        except BaseException:
            self.busy.clear()  # the worker never started, so it can't clear busy itself
            raise

    def solve(self, job: Job) -> None:
        """Worker thread: plan, run on the CAS, copy, render; the result goes back as an event."""
        latex = ""
        try:
            latex = job.latex if job.latex is not None else (self.clip.get() or "").strip()
            if not latex or self.clip.is_traffic(latex):
                self.events.put(("error", "Clipboard has no text", "Copy some LaTeX first.", ""))
                return
            p = plan(latex, job.action, job.var, **(job.extra or {}))  # bad LaTeX fails here
            run = self.bridge.session(lambda: run_plan(self.bridge, p, self.args.timeout))
            r = format_answer(run)  # after the session: the user's clipboard is back meanwhile
            if job.numeric and r.get("approx"):
                r = {**r, "exact": r["result"], "result": r["approx"], "result_latex": r.get("approx_latex"),
                     "approx": None}
            copied = ""
            if self.args.copy == "latex" and r.get("result_latex"):
                self.clip.set(r["result_latex"])
                copied = "LaTeX copied to clipboard"
            elif self.args.copy != "none" and r.get("result"):
                self.clip.set(r["result"])
                copied = "Result text copied to clipboard"
            png = None
            if self.renderer is not None and r.get("result_latex"):
                png = self.renderer.render(r["result_latex"])
            self.events.put(("result", latex, r, copied, png))
        except LatexError as e:
            self.events.put(("error", "Couldn't read the LaTeX", str(e), latex))
        except Exception as e:
            self.events.put(("error", "TI-Nspire error", str(e), latex))
        finally:
            self.busy.clear()

    # -- tray history

    def copy_answer(self, answer: Answer) -> None:
        if self.busy.is_set():
            # A copy now would land in the middle of a bridge request.
            self.show_busy()
            return
        text = answer.copy_text(self.args.copy)
        threading.Thread(target=self._copy, args=(text,), daemon=True).start()

    def _copy(self, text: str) -> None:
        try:
            self.clip.set(text)
            self.events.put(("copied", text))
        except Exception as e:
            self.events.put(("error", "Couldn't copy", str(e), ""))

    # -- action picker

    def open_picker(self) -> None:
        if self.busy.is_set():
            self.show_busy()
            return
        self.close_picker()
        threading.Thread(target=self._prepare_picker, daemon=True).start()

    def _prepare_picker(self) -> None:
        """Worker thread: read the clipboard and check the LaTeX before the picker opens."""
        latex = ""
        try:
            latex = (self.clip.get() or "").strip()
            if not latex or self.clip.is_traffic(latex):
                self.events.put(("error", "Clipboard has no text", "Copy some LaTeX first.", ""))
                return
            plan(latex, "auto")  # unreadable LaTeX: say so now, not after an action is picked
            self.events.put(("picker", latex, picker_defaults(latex)))
        except LatexError as e:
            self.events.put(("error", "Couldn't read the LaTeX", str(e), latex))
        except Exception as e:
            self.events.put(("error", "Hotkey error", f"{type(e).__name__}: {e}", latex))

    def show_picker(self, latex: str, defaults: dict[str, str]) -> None:
        self.close_picker()
        self.popup.close()

        def picked(action: str, var: str | None, extra: dict) -> None:
            self.request(Job(action=action, latex=latex, var=var, extra=extra))

        self.picker = ActionPicker(self.root, latex, defaults, picked, on_close=self._picker_closed)

    def _picker_closed(self) -> None:
        self.picker = None

    def close_picker(self) -> None:
        if self.picker is not None:
            self.picker.close()

    # -- loop

    def pump(self) -> None:
        running = True
        try:
            while running:
                try:
                    ev = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    running = self.handle(ev)
                except Exception as e:
                    self.show_error("Hotkey error", f"{type(e).__name__}: {e}")
        finally:
            # always reschedule, or one failed event would silently stop the hotkey
            if running:
                self.root.after(50, self.pump)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hotkey", default="ctrl+alt+shift+l", help="solve hotkey (default %(default)s)")
    ap.add_argument("--approx-hotkey", default="ctrl+alt+shift+k", help="numeric hotkey (default %(default)s)")
    ap.add_argument("--actions-hotkey", default="ctrl+alt+shift+j",
                    help="action picker hotkey (default %(default)s)")
    ap.add_argument("--quit-hotkey", default="ctrl+alt+shift+q", help="quit hotkey (default %(default)s)")
    ap.add_argument("--copy", choices=["latex", "text", "none"], default="latex",
                    help="what to put on the clipboard after solving (default %(default)s)")
    ap.add_argument("--timeout", type=float, default=15, help="CAS timeout in seconds")
    ap.add_argument("--no-tray", action="store_true", help="no notification-area icon")
    ap.add_argument("--plain", action="store_true", help="show answers as text, not drawn as math")
    args = ap.parse_args()

    if sys.platform != "win32":
        sys.exit("hotkey.py works on Windows only (it uses Windows global hotkeys).")

    single = win32event.CreateMutex(None, False, "Local\\nspire_hotkey_single_instance")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        print("hotkey.py is already running.", file=sys.stderr)
        return
    _ = single  # keep the handle alive

    from nspire_bridge import Bridge, clip_get, clip_set, is_bridge_traffic

    with contextlib.suppress(Exception):
        ctypes.windll.shcore.SetProcessDpiAwareness(2)

    specs = {SOLVE: args.hotkey, APPROX: args.approx_hotkey, PICK: args.actions_hotkey, QUIT: args.quit_hotkey}
    bindings = {hid: (*parse_hotkey(spec), spec) for hid, spec in specs.items()}
    events: queue.Queue = queue.Queue()
    hk = HotkeyThread(bindings, events)
    hk.start()
    hk.ready.wait(5)

    root = tk.Tk()
    root.withdraw()
    scale = root.winfo_fpixels("1i") / 96  # Tk follows the screen's DPI (the process is DPI aware)
    wrap = int(520 * scale)
    popup = Popup(root, wrap)
    renderer = None
    if not args.plain:
        renderer = MathRenderer(dpi=root.winfo_fpixels("1i"), max_width=wrap, max_height=int(400 * scale))
        renderer.warm_up()
    app = App(args, root, popup, Bridge(), events, Clipboard(clip_get, clip_set, is_bridge_traffic), renderer)

    tray = None
    if not args.no_tray:
        tray = TrayIcon("TI-Nspire LaTeX hotkey", lambda: tray_menu(app.history, specs), events)
        tray.start()
        tray.ready.wait(5)
        if tray.error:
            print(f"No tray icon: {tray.error}", file=sys.stderr)
            tray = None

    if hk.failed:
        print("Couldn't register: " + ", ".join(hk.failed), file=sys.stderr)
        popup.show("Hotkey unavailable", "Couldn't register " + ", ".join(hk.failed),
                   footer="Another program uses it; pick another with --hotkey, --actions-hotkey, ….",
                   error=True, seconds=10)
    else:
        popup.show("TI-Nspire LaTeX hotkey ready", pretty_hotkey(args.hotkey),
                   detail=f"numeric: {pretty_hotkey(args.approx_hotkey)}    "
                          f"actions: {pretty_hotkey(args.actions_hotkey)}    quit: {pretty_hotkey(args.quit_hotkey)}",
                   footer="Copy LaTeX, then press the hotkey."
                          + ("  Recent answers: the tray icon." if tray else ""), seconds=6)
    print(f"Ready. {args.hotkey} solves, {args.approx_hotkey} approximates, {args.actions_hotkey} picks "
          f"an action, {args.quit_hotkey} quits.", file=sys.stderr)

    root.after(50, app.pump)
    with contextlib.suppress(KeyboardInterrupt):
        root.mainloop()
    hk.stop()
    if tray is not None:
        tray.stop()


if __name__ == "__main__":
    main()
