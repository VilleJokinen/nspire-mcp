"""Build a TI-Nspire Notes page that shows a worked solution.

The Lua bridge can only evaluate, so the page is typed into the TI-Nspire window
with keystrokes: a new document gets a Notes page, then each step becomes a line
of text followed by a math box that TI-Nspire evaluates itself (live, editable).

Every math expression is first checked through the bridge, because a math box
that errors opens a modal popup in TI-Nspire; if any step fails, nothing is typed.
While typing, it stops as soon as TI-Nspire isn't the foreground window, so it
never types into another program.
"""
from __future__ import annotations

import re
import time

from nspire_bridge import Bridge, clip_set, readable, to_cas_chars
from nspire_keys import (chord, focus_nspire, foreground_is, menu_area, menu_changed, process_windows,
                         type_text, wait_for_popup, window_title)

NOTES_MENU_KEY = "6"  # "Add Notes" in the add-page menu (CX CAS Student/Teacher 6.x)


class NotesAborted(RuntimeError):
    pass


_DEFINES = re.compile(r"^\s*Define\s+(?:LibPub\s+|LibPriv\s+)?([A-Za-zα-ωΑ-Ω]\w*)|^\s*([A-Za-zα-ωΑ-Ω]\w*)\s*:=",
                      re.IGNORECASE)


def _prepare(bridge: Bridge, steps: list[dict], timeout_s: float) -> list[dict]:
    """Check every math step on the bridge, then undo definitions the check made there."""
    created: list[str] = []
    try:
        return _prepare_inner(bridge, steps, timeout_s, created)
    finally:
        # The check runs in the bridge document; don't leave new variables behind
        # (names that already existed there are left alone).
        for name in created:
            try:
                bridge.eval_one(f"DelVar {name}", timeout_s)
            except Exception:
                pass


def _prepare_inner(bridge: Bridge, steps: list[dict], timeout_s: float, created: list[str]) -> list[dict]:
    """Turn steps into {text, cmd, result, seconds}, evaluating every math step on the bridge."""
    out = []
    for i, st in enumerate(steps, 1):
        text = (st.get("text") or "").replace("\r", "")
        if "\n" in text:
            raise ValueError(f"step {i}: text must be one line; split it into several steps")
        cmd = None
        if st.get("latex"):
            import nspire_latex
            cmd = nspire_latex.plan(st["latex"], st.get("action", "auto"), st.get("var")).command
        elif st.get("math"):
            cmd = to_cas_chars(st["math"].strip())
        item = {"text": text, "cmd": cmd, "result": None, "seconds": 0.0}
        if cmd:
            m = _DEFINES.match(cmd)
            name = m and (m.group(1) or m.group(2))
            if name and name not in created and bridge.eval_one(f"getType({name})", timeout_s).strip('"') == "NONE":
                created.append(name)
            t0 = time.monotonic()
            try:
                item["result"] = bridge.eval_one(cmd, timeout_s)
            except ValueError as e:
                raise ValueError(f"step {i} ({cmd}): {e}") from None
            item["seconds"] = time.monotonic() - t0
        elif not text:
            raise ValueError(f"step {i}: needs text, math or latex")
        out.append(item)
    return out


def _guard(hwnd: int) -> None:
    if not foreground_is(hwnd):
        raise NotesAborted("TI-Nspire stopped being the active window, so typing stopped. "
                           "The page is partly built; nothing was typed elsewhere.")


def _paste(hwnd: int, text: str) -> None:
    clip_set(text)
    time.sleep(0.12)
    _guard(hwnd)
    chord("ctrl+v", pause=0.35)


def _doc_name() -> str:
    return window_title().split(" - ")[0]


def _wait_doc_change(old: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        name = _doc_name()
        if name != old:
            return name
        time.sleep(0.1)
    return None


def _new_handheld_notes(hwnd: int) -> str:
    """Ctrl+N (handheld-size document), then "Add Notes" from the add-page menu."""
    before = _doc_name()
    chord("ctrl+n", pause=0.2)
    name = _wait_doc_change(before, 8)
    if not name:
        raise NotesAborted("TI-Nspire didn't open a new document (is a dialog open in it?).")
    # Snapshot only now (the document exists), so the next new window is the add-page menu.
    windows = process_windows(hwnd)
    if not wait_for_popup(hwnd, windows):
        time.sleep(1.5)
    time.sleep(0.3)
    _guard(hwnd)
    chord(NOTES_MENU_KEY, pause=0.9)  # on handheld documents the number picks the item at once
    return name


MENU_OPEN_PIXELS = 1000  # sampled pixels that change when a drop-down opens (measured 3000-5600; idle 0)


def _settled_menu_area(hwnd: int, timeout: float = 4.0) -> bytes:
    """menu_area once the window has stopped redrawing (e.g. a document still opening)."""
    prev = menu_area(hwnd)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.2)
        cur = menu_area(hwnd)
        if menu_changed(prev, cur) < 50:
            return cur
        prev = cur
    return prev


def _menu(hwnd: int, menu_index: int, item_index: int, what: str) -> None:
    """Pick a menu-bar item by position (1-based), so any UI language works.

    Right after a new document opens TI-Nspire ignores the Alt. Its menus are drawn
    by Java inside the window, invisible to Windows, so whether one opened is told
    from the pixels where it appears; if it didn't, try again.
    """
    for attempt in range(5):
        _guard(hwnd)
        base = _settled_menu_area(hwnd)
        chord("alt", pause=0.15)
        for _ in range(menu_index - 1):
            chord("right", pause=0.05)
        chord("down", pause=0.05)  # opens the menu with its first item highlighted
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if menu_changed(base, menu_area(hwnd)) > MENU_OPEN_PIXELS:
                time.sleep(0.1)
                for _ in range(item_index - 1):
                    chord("down", pause=0.05)
                _guard(hwnd)
                chord("enter", pause=0.2)
                return
            time.sleep(0.05)
        # No Esc to back out: if focus had moved to another program, Esc would land
        # there (in Claude it cancels the running command). Usually the Alt was
        # simply ignored, so there's nothing to undo.
        time.sleep(0.4 + 0.3 * attempt)
    raise NotesAborted(f"Couldn't open TI-Nspire's menu for {what}; nothing was typed.")


def _new_computer_notes(hwnd: int) -> str:
    """File > New computer document, then Insert > Notes.

    A new computer-size document shows no add-page menu, just an empty page
    ("click here to add an application"); inserting an app fills that page.
    File items: New handheld, New computer, ...; Insert (4th menu) items: Problem,
    Page, Calculator, Graphs, Geometry, Lists & Spreadsheet, Data & Statistics, Notes.
    """
    before = _doc_name()
    _menu(hwnd, 1, 2, "File > New computer document")
    name = _wait_doc_change(before, 8)
    if not name:
        raise NotesAborted("TI-Nspire didn't open a new document (is a dialog open in it?).")
    time.sleep(0.5)
    _menu(hwnd, 4, 8, "Insert > Notes")
    time.sleep(0.8)
    return name


def build_notes(bridge: Bridge, steps: list[dict], title: str | None = None, page_size: str = "computer",
                timeout_s: float = 10) -> dict:
    """Validate the steps, then type them into a new document's Notes page.

    Call inside bridge.session() (it uses the clipboard to paste math).
    """
    items = _prepare(bridge, steps, timeout_s)

    if page_size not in ("computer", "handheld"):
        raise ValueError("page_size must be 'computer' or 'handheld'")
    hwnd = focus_nspire()
    if page_size == "computer":
        _new_computer_notes(hwnd)
    else:
        _new_handheld_notes(hwnd)

    if title:
        _guard(hwnd)
        type_text(title)
        chord("enter", pause=0.15)

    for it in items:
        _guard(hwnd)
        if it["text"]:
            type_text(it["text"] + (" " if it["cmd"] else ""))
        if it["cmd"]:
            chord("ctrl+m", pause=0.35)
            _paste(hwnd, it["cmd"])
            _guard(hwnd)
            chord("enter", pause=0.6 + 1.5 * it["seconds"])  # TI-Nspire evaluates the box
            _guard(hwnd)
            chord("backspace", pause=0.2)  # Enter leaves an empty math box on the next line
        else:
            chord("enter", pause=0.15)

    return {
        "document": window_title().split(" - ")[0],
        "steps": [{"text": it["text"], "math": it["cmd"] and readable(it["cmd"]), "result": it["result"]}
                  for it in items],
        "note": "Built in a new, unsaved document; save it from TI-Nspire if you want to keep it.",
    }
