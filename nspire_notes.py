"""Build a TI-Nspire Notes page that shows a worked solution.

The Lua bridge can only evaluate, so the page is typed into the TI-Nspire window
with keystrokes: a new document gets a Notes page, then each step becomes a line
of text followed by a math box that TI-Nspire evaluates itself (live, editable).

Every math expression is first checked through the bridge, because a math box
that errors opens a modal popup in TI-Nspire; if any step fails, nothing is typed.
The window is found once, by the exe of its process (never by its title), and that
handle is used for the whole run. Before every key and every typed character it
checks that TI-Nspire is still the foreground window and that the run wasn't
cancelled, so it stops instead of typing into another program.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

from nspire_bridge import TIMEOUT_MAX_S, TIMEOUT_MIN_S, Bridge, clip_set, readable, to_cas_chars
from nspire_keys import (chord, describe_window, find_nspire_windows, focus_nspire, foreground_is, menu_area,
                         menu_changed, process_windows, type_text, wait_for_popup, window_title)

log = logging.getLogger("nspire")

NOTES_MENU_KEY = "6"  # "Add Notes" in the add-page menu (CX CAS Student/Teacher 6.x)


class NotesAborted(RuntimeError):
    pass


# ------------------------------------------------------------ names in a command
#
# Checking a step runs it in the bridge document, which keeps whatever the step
# defines (a:=5, Define f(x)=..., 5→a). To undo that, and to warn about steps that
# read variables the new Notes document won't have, we need the names a command
# defines and the names it mentions. Over-detecting is harmless: a name that turns
# out not to exist is simply never touched.

_NAME = r"[^\W\d]\w*"  # a letter (Greek included) or underscore, then word characters
_DEFINE_RE = re.compile(rf"\bDefine\s+(?:Lib(?:Pub|Priv)\s+)?({_NAME})\s*(\([^()]*\))?", re.IGNORECASE)
_ASSIGN_RE = re.compile(rf"(?<![\w.])({_NAME})\s*(\([^()]*\))?\s*:=")  # a:=5   f(x):=x^2
_STORE_RE = re.compile(rf"(?:→|=:)\s*({_NAME})")  # 5→a   5=:a
_STRING_RE = re.compile(r'"[^"]*"')
_IDENT_RE = re.compile(_NAME)
_VARINFO_ROW = re.compile(r'\[([^\[\]",]+),"([A-Za-z]+)"')  # one row of getVarInfo(): [name,"NUM","",0]


@dataclass(frozen=True)
class Names:
    defines: tuple[str, ...]  # variables/functions the command stores, in order
    uses: frozenset[str]      # other names it mentions (lower case), minus function parameters


def scan_names(cmd: str) -> Names:
    s = _STRING_RE.sub('""', cmd)
    defines: list[str] = []
    skip: list[tuple[int, int]] = []
    params: set[str] = set()
    for rx in (_DEFINE_RE, _ASSIGN_RE, _STORE_RE):
        for m in rx.finditer(s):
            if m.group(1) not in defines:
                defines.append(m.group(1))
            args = m.group(2) if rx is not _STORE_RE else None
            # Skip the head (the Define keyword, the name and its parameter list) but not
            # what follows: in a:=a+1 the second a is a use.
            skip.append((m.start(), m.end(2) if args else m.end(1)))
            if args:
                params.update(p.lower() for p in _IDENT_RE.findall(args))
    uses = {m.group(0).lower() for m in _IDENT_RE.finditer(s)
            if not any(a <= m.start() < b for a, b in skip)}
    return Names(tuple(defines), frozenset(uses - params))


# ------------------------------------------------------------ argument checks (offline)

def check_options(title: str | None, page_size: str, timeout_s: float) -> None:
    if page_size not in ("computer", "handheld"):
        raise ValueError("page_size must be 'computer' or 'handheld'")
    if title is not None and (not isinstance(title, str) or "\n" in title or "\r" in title):
        raise ValueError("title must be a single line of text")
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) \
            or not TIMEOUT_MIN_S <= timeout_s <= TIMEOUT_MAX_S:
        raise ValueError(f"timeout_s must be a number of seconds between {TIMEOUT_MIN_S:g} and {TIMEOUT_MAX_S:g}")


def plan_steps(steps: list[dict]) -> list[dict]:
    """Check every step and translate it to a TI-Nspire command, without touching the bridge.

    Returns [{text, cmd, result, seconds}]; result and seconds are filled in by _prepare.
    """
    if not isinstance(steps, list) or not steps:
        raise ValueError('steps must be a non-empty list of objects like {"text": ..., "math": ...}')
    out = []
    for i, st in enumerate(steps, 1):
        if not isinstance(st, dict):
            raise ValueError(f'step {i}: must be an object like {{"text": ..., "math": ...}}')
        for key in ("text", "math", "latex", "action", "var"):
            if st.get(key) is not None and not isinstance(st[key], str):
                raise ValueError(f"step {i}: {key} must be a string")
        text = (st.get("text") or "").replace("\r", "")
        if "\n" in text:
            raise ValueError(f"step {i}: text must be one line; split it into several steps")
        cmd = None
        if st.get("latex"):
            import nspire_latex
            try:
                cmd = nspire_latex.plan(st["latex"], st.get("action") or "auto", st.get("var")).command
            except nspire_latex.LatexError as e:
                raise ValueError(f"step {i}: {e}") from None
        elif st.get("math"):
            cmd = to_cas_chars(st["math"].strip())
        if cmd is not None and ("\n" in cmd or "\r" in cmd):
            raise ValueError(f"step {i}: math must be a single line")
        if not cmd and not text:
            raise ValueError(f"step {i}: needs text, math or latex")
        out.append({"text": text, "cmd": cmd, "result": None, "seconds": 0.0})
    return out


# ------------------------------------------------------------ checking on the bridge

@dataclass
class _Saved:
    """What a name held in the bridge document before checking the steps touched it."""
    name: str
    old_type: str       # "NONE" if it didn't exist
    value: str | None   # CAS text that restores it; None if it didn't exist or can't be read back
    evaluated: bool = False  # value is the evaluated form, which may differ from what was stored


def _type_of(bridge: Bridge, name: str, timeout_s: float) -> str:
    try:
        return bridge.eval_one(f"getType({name})", timeout_s).strip('"')
    except ValueError:
        return "NONE"  # built-in names (solve, sin, ...) make getType fail; they aren't variables


def _bridge_vars(bridge: Bridge, timeout_s: float) -> dict[str, str] | None:
    """Variables stored in the bridge document: {lower-case name: type}; None if it can't be listed."""
    try:
        text = bridge.eval_one("getVarInfo()", timeout_s, raw=True).strip()
    except ValueError:
        return None
    if text.strip('"') == "NONE":
        return {}
    rows = _VARINFO_ROW.findall(text)
    return {n.lower(): t for n, t in rows} if rows else None


# Types whose stored value can hold other variables' names. Reading one back
# evaluates it, so a:=x+1 stored while x was undefined reads back as 3 once x:=2.
_SYMBOLIC_TYPES = {"EXPR", "LIST", "MAT"}


def _snapshot(bridge: Bridge, name: str, old_type: str, timeout_s: float, others_stored: bool = True) -> _Saved:
    """others_stored: other variables exist in the bridge document (or that's unknown).
    Without them nothing can be substituted, so the value read back is exactly what was stored."""
    value = None
    if old_type not in ("NONE", "FUNC", "PRGM"):  # a function's body can't be read back by evaluating it
        try:
            value = bridge.eval_one(name, timeout_s, raw=True)
        except ValueError:
            pass
    evaluated = value is not None and old_type.upper() in _SYMBOLIC_TYPES and others_stored
    return _Saved(name, old_type, value, evaluated)


def _undo(bridge: Bridge, saved: dict[str, _Saved], timeout_s: float) -> list[str]:
    """Put the bridge document back as it was; returns warnings for what couldn't be."""
    warnings = []
    for sv in reversed(list(saved.values())):
        try:
            if sv.old_type == "NONE":
                bridge.eval_one(f"DelVar {sv.name}", timeout_s)
            elif sv.value is not None:
                bridge.eval_one(f"{sv.name}:={sv.value}", timeout_s)
                if sv.evaluated:
                    warnings.append(f"Checking the steps redefined {sv.name} ({sv.old_type}), which already existed "
                                    f"in the bridge document; it was restored as {readable(sv.value)}. If it was "
                                    f"stored as an expression of other variables, that link is lost.")
            else:
                warnings.append(f"Checking the steps redefined {sv.name} ({sv.old_type}), which already existed in "
                                f"the bridge document; its old definition couldn't be restored.")
        except Exception as e:
            what = "remove" if sv.old_type == "NONE" else f"restore (old value {readable(sv.value or '')})"
            warnings.append(f"Couldn't {what} {sv.name} in the bridge document after checking the steps: {e}")
    return warnings


def _prepare(bridge: Bridge, items: list[dict], timeout_s: float,
             cancel: threading.Event | None = None) -> list[str]:
    """Evaluate every math step on the bridge (fills result and seconds), then undo what that changed there.

    Returns warnings. Raises ValueError naming the first step that fails, or NotesAborted
    if cancel is set between steps (the undo runs either way).
    """
    saved: dict[str, _Saved] = {}
    try:
        warnings = _run_steps(bridge, items, timeout_s, saved, cancel)
    except BaseException as e:
        extra = _undo(bridge, saved, timeout_s)
        if extra and isinstance(e, ValueError):
            raise ValueError(f"{e} ({' '.join(extra)})") from None
        for w in extra:
            log.warning(w)
        raise
    return warnings + _undo(bridge, saved, timeout_s)


def _check_cancel(cancel: threading.Event | None, typing: bool = False) -> None:
    if cancel is not None and cancel.is_set():
        if typing:
            raise NotesAborted("Cancelled, so typing stopped. The new, unsaved document in TI-Nspire may hold a "
                               "partly built page; nothing was typed elsewhere.")
        raise NotesAborted("Cancelled before anything was typed into TI-Nspire.")


def _run_steps(bridge: Bridge, items: list[dict], timeout_s: float, saved: dict[str, _Saved],
               cancel: threading.Event | None = None) -> list[str]:
    _check_cancel(cancel)
    existing = _bridge_vars(bridge, timeout_s)  # None: couldn't list them, so ask getType name by name
    types: dict[str, str] = {}

    def type_of(name: str) -> str:
        key = name.lower()
        if key not in types:
            types[key] = existing.get(key, "NONE") if existing is not None else _type_of(bridge, name, timeout_s)
        return types[key]

    def others_stored(name: str) -> bool:
        return existing is None or any(k != name.lower() for k in existing)

    warnings: list[str] = []
    defined: set[str] = set()  # names earlier steps define: the new document gets those too
    for i, it in enumerate(items, 1):
        _check_cancel(cancel)
        cmd = it["cmd"]
        if not cmd:
            continue
        names = scan_names(cmd)
        stale = sorted(n for n in names.uses if n not in defined and type_of(n) != "NONE")
        if stale:
            warnings.append(f"Step {i} uses {', '.join(stale)}, stored in the bridge document but not in the new "
                            f"Notes document, so the page may show a different result than the one returned here. "
                            f"Define {'it' if len(stale) == 1 else 'them'} in an earlier step or write the value in.")
        for name in names.defines:
            if name.lower() not in saved:
                saved[name.lower()] = _snapshot(bridge, name, type_of(name), timeout_s, others_stored(name))
        defined.update(n.lower() for n in names.defines)
        t0 = time.monotonic()
        try:
            it["result"] = bridge.eval_one(cmd, timeout_s)
        except ValueError as e:
            raise ValueError(f"step {i} ({cmd}): {e}") from None
        it["seconds"] = time.monotonic() - t0
    return warnings


# ------------------------------------------------------------ typing into TI-Nspire

class _Guard:
    """Runs before every key and typed character: stops the run if it was cancelled
    or if TI-Nspire (hwnd's process) is no longer the foreground window."""

    def __init__(self, hwnd: int, cancel: threading.Event | None = None):
        self.hwnd = hwnd
        self.cancel = cancel

    def __call__(self) -> None:
        _check_cancel(self.cancel, typing=True)
        if not foreground_is(self.hwnd):
            raise NotesAborted("TI-Nspire stopped being the active window, so typing stopped; nothing was typed "
                               "elsewhere. The new, unsaved document in TI-Nspire may hold a partly built page.")


def _paste(guard: _Guard, text: str) -> None:
    guard()
    clip_set(text)
    time.sleep(0.12)
    chord("ctrl+v", pause=0.35, guard=guard)


def _doc_name(hwnd: int) -> str:
    """Document name from the window title ("<document> - TI-Nspire ... Software")."""
    return window_title(hwnd).rsplit(" - ", 1)[0]


def _wait_doc_change(hwnd: int, old: str, timeout: float) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        name = _doc_name(hwnd)
        if name != old:
            return name
        time.sleep(0.1)
    return None


def _new_handheld_notes(guard: _Guard) -> str:
    """Ctrl+N (handheld-size document), then "Add Notes" from the add-page menu."""
    hwnd = guard.hwnd
    before = _doc_name(hwnd)
    chord("ctrl+n", pause=0.2, guard=guard)
    name = _wait_doc_change(hwnd, before, 8)
    if not name:
        raise NotesAborted("TI-Nspire didn't open a new document (is a dialog open in it?).")
    # Snapshot only now (the document exists), so the next new window is the add-page menu.
    windows = process_windows(hwnd)
    if not wait_for_popup(hwnd, windows):
        time.sleep(1.5)
    time.sleep(0.3)
    chord(NOTES_MENU_KEY, pause=0.9, guard=guard)  # on handheld documents the number picks the item at once
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


def _menu(guard: _Guard, menu_index: int, item_index: int, what: str) -> None:
    """Pick a menu-bar item by position (1-based), so any UI language works.

    Right after a new document opens TI-Nspire ignores the Alt. Its menus are drawn
    by Java inside the window, invisible to Windows, so whether one opened is told
    from the pixels where it appears; if it didn't, try again.
    """
    hwnd = guard.hwnd
    for attempt in range(5):
        guard()
        base = _settled_menu_area(hwnd)
        chord("alt", pause=0.15, guard=guard)
        for _ in range(menu_index - 1):
            chord("right", pause=0.05, guard=guard)
        chord("down", pause=0.05, guard=guard)  # opens the menu with its first item highlighted
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if menu_changed(base, menu_area(hwnd)) > MENU_OPEN_PIXELS:
                time.sleep(0.1)
                for _ in range(item_index - 1):
                    chord("down", pause=0.05, guard=guard)
                chord("enter", pause=0.2, guard=guard)
                return
            time.sleep(0.05)
        # No Esc to back out: if focus had moved to another program, Esc would land
        # there (in Claude it cancels the running command). Usually the Alt was
        # simply ignored, so there's nothing to undo.
        time.sleep(0.4 + 0.3 * attempt)
    raise NotesAborted(f"Couldn't open TI-Nspire's menu for {what}; nothing was typed.")


def _new_computer_notes(guard: _Guard) -> str:
    """File > New computer document, then Insert > Notes.

    A new computer-size document shows no add-page menu, just an empty page
    ("click here to add an application"); inserting an app fills that page.
    File items: New handheld, New computer, ...; Insert (4th menu) items: Problem,
    Page, Calculator, Graphs, Geometry, Lists & Spreadsheet, Data & Statistics, Notes.
    """
    before = _doc_name(guard.hwnd)
    _menu(guard, 1, 2, "File > New computer document")
    name = _wait_doc_change(guard.hwnd, before, 8)
    if not name:
        raise NotesAborted("TI-Nspire didn't open a new document (is a dialog open in it?).")
    time.sleep(0.5)
    _menu(guard, 4, 8, "Insert > Notes")
    time.sleep(0.8)
    return name


def build_notes(bridge: Bridge, steps: list[dict], title: str | None = None, page_size: str = "computer",
                timeout_s: float = 10, cancel: threading.Event | None = None) -> dict:
    """Validate the steps, then type them into a new document's Notes page.

    Call inside bridge.session() (it uses the clipboard to paste math).
    Order: check the arguments, find the TI-Nspire window (by its process), check
    the math on the bridge, and only then bring the window forward and type.
    Every key goes through a guard that stops (NotesAborted) if TI-Nspire isn't the
    foreground window or cancel is set; set cancel from another thread to stop a run.
    """
    check_options(title, page_size, timeout_s)
    items = plan_steps(steps)

    windows = find_nspire_windows()  # raises before any clipboard use if TI-Nspire isn't running
    hwnd = windows[0].hwnd           # this window, and no other, for the whole run
    warnings: list[str] = []
    if len(windows) > 1:
        warnings.append(f"{len(windows)} TI-Nspire windows are open; used '{windows[0].title}' "
                        f"(handle {hwnd}), the one in front or else the lowest handle.")

    warnings += _prepare(bridge, items, timeout_s, cancel)

    _check_cancel(cancel)  # last chance to stop before TI-Nspire is touched
    focus_nspire(hwnd)
    guard = _Guard(hwnd, cancel)
    if page_size == "computer":
        _new_computer_notes(guard)
    else:
        _new_handheld_notes(guard)

    if title:
        type_text(title, guard=guard)
        chord("enter", pause=0.15, guard=guard)

    for it in items:
        guard()
        if it["text"]:
            type_text(it["text"] + (" " if it["cmd"] else ""), guard=guard)
        if it["cmd"]:
            chord("ctrl+m", pause=0.35, guard=guard)
            _paste(guard, it["cmd"])
            chord("enter", pause=0.6 + 1.5 * it["seconds"], guard=guard)  # TI-Nspire evaluates the box
            chord("backspace", pause=0.2, guard=guard)  # Enter leaves an empty math box on the next line
        else:
            chord("enter", pause=0.15, guard=guard)

    try:
        window = describe_window(hwnd)
        document = window["title"].rsplit(" - ", 1)[0]
    except Exception:  # closed right at the end: the page is built, don't turn that into a failure
        window, document = {"hwnd": hwnd}, None
    note = "Built in a new, unsaved document; save it from TI-Nspire if you want to keep it."
    if warnings:
        note += " Warning: " + " ".join(warnings)
    return {
        "document": document,
        "window": window,
        "steps": [{"text": it["text"], "math": it["cmd"] and readable(it["cmd"]), "result": it["result"]}
                  for it in items],
        "warnings": warnings,
        "note": note,
    }
