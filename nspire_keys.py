"""Keystroke automation for the TI-Nspire desktop window (Windows SendInput).

Used to build Notes pages: the bridge can only evaluate, it can't add pages,
so we type into the app like a person would.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import ntpath
import re
import time
from dataclasses import dataclass

import win32con
import win32gui
import win32process

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _U(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _send(inputs: list[INPUT]) -> None:
    arr = (INPUT * len(inputs))(*inputs)
    if user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
        raise OSError(ctypes.get_last_error(), "SendInput was blocked (is an elevated window in front?)")


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    i = INPUT(type=INPUT_KEYBOARD)
    i.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
    return i


VK = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
      "esc": 0x1B, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "end": 0x23,
      "home": 0x24, "backspace": 0x08, "delete": 0x2E, "space": 0x20}


def chord(spec: str, pause: float = 0.05) -> None:
    """Press e.g. 'ctrl+m', 'enter', 'right'."""
    parts = spec.lower().split("+")
    vks = [VK[p] if p in VK else ord(p.upper()) for p in parts]
    _send([_key(v) for v in vks] + [_key(v, flags=KEYEVENTF_KEYUP) for v in reversed(vks)])
    time.sleep(pause)


def type_text(text: str, per_char: float = 0.004) -> None:
    """Type any Unicode text (no layout dependence). Newlines are not special: use chord('enter')."""
    for ch in text:
        units = ch.encode("utf-16-le")
        codes = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
        _send([_key(scan=c, flags=KEYEVENTF_UNICODE) for c in codes]
              + [_key(scan=c, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP) for c in codes])
        time.sleep(per_char)


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# TI-Nspire CX (CAS) Student / Teacher Software: "TI-Nspire CX CAS Student Software.exe",
# normally under Program Files\TI Education. Matched by the exe's name, not its folder, so a
# custom install location works. "TI-Nspire Computer Link" and similar don't match.
_NSPIRE_EXE = re.compile(r"^TI-Nspire\b.*\b(?:Student|Teacher)\s+Software\.exe$", re.IGNORECASE)
MAIN_WINDOW_CLASS = "SunAwtFrame"  # TI-Nspire is a Java (AWT/Swing) app; its main window is a Frame


@dataclass(frozen=True)
class WinInfo:
    hwnd: int
    title: str
    cls: str
    pid: int
    image: str | None  # full path of the owning process's exe, None if it couldn't be read


def is_nspire_image(path: str | None) -> bool:
    """True if path is the exe of the TI-Nspire desktop software."""
    return bool(path) and bool(_NSPIRE_EXE.match(ntpath.basename(path)))


def process_image(pid: int) -> str | None:
    """Full path of the exe a process runs, or None (process gone, or access denied)."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = wt.DWORD(len(buf))
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return None
    finally:
        kernel32.CloseHandle(h)


def list_windows() -> list[WinInfo]:
    """Visible top-level windows with a title, front to back."""
    out: list[WinInfo] = []
    images: dict[int, str | None] = {}

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        title = win32gui.GetWindowText(h)
        if not title:
            return
        pid = win32process.GetWindowThreadProcessId(h)[1]
        if pid not in images:
            images[pid] = process_image(pid)
        out.append(WinInfo(h, title, win32gui.GetClassName(h), pid, images[pid]))
    win32gui.EnumWindows(cb, None)
    return out


def pick_nspire_window(windows: list[WinInfo], foreground: int = 0) -> tuple[WinInfo, list[WinInfo]]:
    """Choose the TI-Nspire main window among windows. Returns (chosen, all candidates).

    Only windows of a TI-Nspire process count; the title is never trusted (a browser tab
    or editor can say "TI-Nspire" too). Main frames (SunAwtFrame) beat its dialogs and
    popups. With several candidates (Student and Teacher both open, say) the one in front
    wins, otherwise the lowest handle, so the choice is the same every time.
    """
    mine = [w for w in windows if is_nspire_image(w.image)]
    frames = [w for w in mine if w.cls == MAIN_WINDOW_CLASS]
    cands = frames or [w for w in mine if "TI-Nspire" in w.title]
    if not cands:
        others = sorted({ntpath.basename(w.image or "?") for w in windows if "TI-Nspire" in w.title})
        hint = (f" (Ignored window(s) of other programs with 'TI-Nspire' in the title: {', '.join(others)}.)"
                if others else "")
        raise RuntimeError("TI-Nspire window not found. Is the TI-Nspire CX (CAS) Student or Teacher "
                           "Software open?" + hint)
    cands = sorted(cands, key=lambda w: (w.hwnd != foreground, w.hwnd))
    return cands[0], cands


def find_nspire_window() -> int:
    return find_nspire_windows()[0].hwnd


def find_nspire_windows() -> list[WinInfo]:
    """Candidate TI-Nspire main windows, the one to use first."""
    return pick_nspire_window(list_windows(), win32gui.GetForegroundWindow())[1]


def describe_window(hwnd: int) -> dict:
    """Which window is being driven, for the tool result."""
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    image = process_image(pid)
    return {"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd),
            "process": ntpath.basename(image) if image else None}


def focus_nspire(hwnd: int | None = None) -> int:
    """Bring a TI-Nspire window to the front. Pass the hwnd chosen at the start of a run."""
    h = hwnd or find_nspire_window()
    if not win32gui.IsWindow(h):
        raise RuntimeError("The TI-Nspire window was closed.")
    if not is_nspire_image(process_image(win32process.GetWindowThreadProcessId(h)[1])):
        raise RuntimeError("Refusing to type into a window that doesn't belong to the TI-Nspire software.")
    if win32gui.IsIconic(h):
        win32gui.ShowWindow(h, win32con.SW_RESTORE)
    if win32gui.GetForegroundWindow() == h:
        return h
    # Windows only lets the foreground thread hand focus over, so borrow its
    # input state. (An Alt tap would also work but opens TI-Nspire's menu bar.)
    fg = win32gui.GetForegroundWindow()
    fg_thread = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
    me = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = bool(fg_thread) and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True)
    try:
        win32gui.BringWindowToTop(h)
        win32gui.SetForegroundWindow(h)
    except Exception:
        pass
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)
    time.sleep(0.3)
    if win32gui.GetForegroundWindow() != h:
        raise RuntimeError("Couldn't bring TI-Nspire to the front; click its window and try again.")
    return h


def process_windows(hwnd: int) -> set[int]:
    """Visible top-level windows of the process that owns hwnd (menus and popups included)."""
    pid = win32process.GetWindowThreadProcessId(hwnd)[1]
    found: set[int] = set()

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and win32process.GetWindowThreadProcessId(h)[1] == pid:
            found.add(h)
    win32gui.EnumWindows(cb, None)
    return found


def wait_for_popup(hwnd: int, before: set[int], timeout: float = 6.0) -> bool:
    """Wait until the process shows a top-level window that wasn't in `before`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_windows(hwnd) - before:
            time.sleep(0.25)  # let it finish appearing and take keyboard focus
            return True
        time.sleep(0.05)
    return False


def menu_area(hwnd: int, step: int = 6) -> bytes:
    """Sampled pixels of the window's top-left area, where its drop-down menus open.

    TI-Nspire is a Java (Swing) app: its menus are drawn inside the main window,
    so Windows reports neither a menu window nor menu mode. The pixels are the
    only outside sign that a menu opened (compare with menu_changed).
    """
    import win32ui
    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    w, h = max(1, int((right - left) * 0.6)), max(1, int((bottom - top) * 0.6))
    hdc = win32gui.GetWindowDC(0)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(src, w, h)
        mem.SelectObject(bmp)
        mem.BitBlt((0, 0), (w, h), src, (left, top), win32con.SRCCOPY)
        bits = bmp.GetBitmapBits(True)  # 4 bytes per pixel, rows of w pixels
    finally:
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(0, hdc)
        win32gui.DeleteObject(bmp.GetHandle())
    return b"".join(bits[(y * w + x) * 4:(y * w + x) * 4 + 3] for y in range(0, h, step) for x in range(0, w, step))


def menu_changed(before: bytes, after: bytes) -> int:
    """How many sampled pixels differ (a caret blink is a few, an open menu hundreds)."""
    n = min(len(before), len(after)) // 3
    return sum(before[i * 3:i * 3 + 3] != after[i * 3:i * 3 + 3] for i in range(n))


def foreground_is(hwnd: int) -> bool:
    """True if the foreground window belongs to hwnd's process (its menus and popups count)."""
    fg = win32gui.GetForegroundWindow()
    if not fg or not win32gui.IsWindow(hwnd):
        return False
    return win32process.GetWindowThreadProcessId(fg)[1] == win32process.GetWindowThreadProcessId(hwnd)[1]


def window_title(hwnd: int) -> str:
    """Title of the given window (the one chosen at the start; never searches again)."""
    if not win32gui.IsWindow(hwnd):
        raise RuntimeError("The TI-Nspire window was closed.")
    return win32gui.GetWindowText(hwnd)
