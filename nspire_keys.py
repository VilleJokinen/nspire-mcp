"""Keystroke automation for the TI-Nspire desktop window (Windows SendInput).

Used to build Notes pages: the bridge can only evaluate, it can't add pages,
so we type into the app like a person would.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time

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


def find_nspire_window() -> int:
    found: list[int] = []

    def cb(h, _):
        if win32gui.IsWindowVisible(h) and "TI-Nspire" in win32gui.GetWindowText(h):
            found.append(h)
    win32gui.EnumWindows(cb, None)
    if not found:
        raise RuntimeError("TI-Nspire window not found. Is the TI-Nspire app open?")
    return found[0]


def focus_nspire() -> int:
    h = find_nspire_window()
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


def foreground_is(hwnd: int) -> bool:
    """True if the foreground window belongs to hwnd's process (its menus and popups count)."""
    fg = win32gui.GetForegroundWindow()
    if not fg:
        return False
    return win32process.GetWindowThreadProcessId(fg)[1] == win32process.GetWindowThreadProcessId(hwnd)[1]


def window_title() -> str:
    return win32gui.GetWindowText(find_nspire_window())
