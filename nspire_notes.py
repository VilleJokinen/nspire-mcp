"""Build a TI-Nspire Notes page that shows a worked solution.

The Lua bridge can only evaluate, so the page is typed into the TI-Nspire window
with keystrokes: a new document gets a Notes page, then each step becomes a line
of text followed by a math box that TI-Nspire evaluates itself (live, editable).

Optionally a Graphs page follows, with functions typed into its entry line, then a
Lists & Spreadsheet page with named columns of data, and the new document is saved
through TI-Nspire's Save dialog.

Every math expression (and every function to graph) is first checked through the
bridge, because a math box that errors opens a modal popup in TI-Nspire; if any
step fails, nothing is typed. The window is found once, by the exe of its process
(never by its title), and that handle is used for the whole run. Before every key
and every typed character it checks that TI-Nspire is still the foreground window
and that the run wasn't cancelled, so it stops instead of typing into another program.
"""
from __future__ import annotations

import contextlib
import logging
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from nspire_bridge import TIMEOUT_MAX_S, TIMEOUT_MIN_S, Bridge, clip_set, readable, to_cas_chars

# Typing needs Windows (SendInput); scan_names and the step checks work anywhere
# (server.py's nspire_latex warnings use scan_names on macOS too).
if sys.platform == "win32":
    from nspire_keys import (
        FILE_DIALOG_CLASS,
        chord,
        control_text,
        describe_window,
        find_nspire_windows,
        focus_nspire,
        focused_control,
        foreground_is,
        foreground_window,
        is_file_name_box,
        menu_area,
        menu_changed,
        new_dialogs,
        process_windows,
        type_text,
        wait_for_popup,
        window_class,
        window_open,
        window_title,
    )
    UNSUPPORTED = None
else:
    UNSUPPORTED = ("nspire_notes works on Windows only: it types into the TI-Nspire window with Windows "
                   "keystrokes. Show the steps with nspire_eval or nspire_latex instead.")

log = logging.getLogger("nspire")

# Menu positions (CX CAS Student/Teacher 6.x). Menus are reached by position, so any UI language works.
NOTES_MENU_KEY = "6"   # "Add Notes" in the add-page menu of a handheld document
GRAPHS_MENU_KEY = "2"  # "Add Graphs" in the same menu
FILE_MENU, NEW_COMPUTER_ITEM = 1, 2  # File > New computer document
# File > Settings > Document Settings. File: New handheld, New computer, Open, Close, Save, Save As,
# Save to Handheld, Convert to, Print, Settings; Settings: Change Language, Handheld Type, Document Settings
SETTINGS_ITEM, DOCUMENT_SETTINGS_ITEM = 10, 3
# Display Digits, the dialog's first (focused) field, lists Float, Float1 ... Float12, Fix0 ... Fix12
DIGITS_TOP_KEYS = 20  # Up presses that reach Float from anywhere (the list doesn't wrap)
MIN_DIGITS, MAX_DIGITS = 1, 12
LISTS_MENU_KEY = "4"  # "Add Lists & Spreadsheet" in the add-page menu (not checked on the real app yet)
INSERT_MENU = 4  # Insert: Problem, Page, Calculator, Graphs, Geometry, Lists & Spreadsheet, Data & Statistics, Notes
INSERT_PAGE_ITEM, INSERT_GRAPHS_ITEM, INSERT_LISTS_ITEM, INSERT_NOTES_ITEM = 2, 4, 6, 8
GRAPHS_ENTRY_KEY = "ctrl+g"  # Show Entry Line: Enter graphs a function and closes the entry line
SAVE_KEY = "ctrl+s"  # File > Save Document; a document that was never saved asks for a name

GRAPH_CHECK_NAME = "mcpgraphchk"  # temporary function that checks a graph in the bridge document (then deleted)
MAX_GRAPHS = 99  # f1 ... f99
SHEET_CHECK_NAME = "mcpsheetchk"  # temporary list that checks an unnamed column in the bridge document
SHEET_CHUNK = 200  # values per request when checking a column on the bridge (as nspire_stats stores lists)
MAX_SHEET_CELLS = 2600  # values typed into the spreadsheet (about a minute of typing at most)
MAX_SHEET_NAME = 16  # characters in a column (list) name

Progress = Callable[[int, int, str], None]  # (done, total, message), called before each unit of work


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
_UNIT_RE = re.compile(r"_(?:°[CFK]|[^\W\d_]\w*)")  # _m, _°C, _g: units and constants, not variables
_IDENT_RE = re.compile(_NAME)
_VARINFO_ROW = re.compile(r'\[([^\[\]",]+),"([A-Za-z]+)"')  # one row of getVarInfo(): [name,"NUM","",0]


@dataclass(frozen=True)
class Names:
    defines: tuple[str, ...]  # variables/functions the command stores, in order
    uses: frozenset[str]      # other names it mentions (lower case), minus function parameters


def scan_names(cmd: str) -> Names:
    s = _UNIT_RE.sub(lambda m: " " * len(m.group()), _STRING_RE.sub('""', cmd))
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

def check_options(title: str | None, page_size: str, timeout_s: float, digits: int | None = None) -> None:
    if digits is not None and (isinstance(digits, bool) or not isinstance(digits, int)
                               or not MIN_DIGITS <= digits <= MAX_DIGITS):
        raise ValueError(f"digits must be a whole number of significant figures from {MIN_DIGITS} to {MAX_DIGITS}")
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
        for key in ("text", "math", "latex", "action", "var", "to"):
            if st.get(key) is not None and not isinstance(st[key], str):
                raise ValueError(f"step {i}: {key} must be a string")
        if st.get("approx") is not None and not isinstance(st["approx"], bool):
            raise ValueError(f"step {i}: approx must be true or false")
        consts = st.get("constants")
        if consts is not None and not (isinstance(consts, bool) or (
                isinstance(consts, list) and all(isinstance(c, str) for c in consts))):
            raise ValueError(f'step {i}: constants must be a list of symbols such as ["g", "c"], or true')
        text = (st.get("text") or "").replace("\r", "")
        if "\n" in text:
            raise ValueError(f"step {i}: text must be one line; split it into several steps")
        cmd = None
        convertible = True  # a quantity to evaluate, so "to" may convert it
        if st.get("latex"):
            import nspire_latex
            try:
                p = nspire_latex.plan(st["latex"], st.get("action") or "auto", st.get("var"), constants=consts or False)
            except nspire_latex.LatexError as e:
                raise ValueError(f"step {i}: {e}") from None
            cmd = p.command
            convertible = p.action in ("eval", "simplify", "approx") and not p.parsed.convert
        elif st.get("math"):
            if consts:
                raise ValueError(f"step {i}: constants is for latex steps; in math write TI-Nspire's names "
                                 "(_g, _c, _h, _k, _Na, _Rc, _Gc, _ε0, _μ0, _Me, _Mp, _q)")
            cmd = to_cas_chars(st["math"].strip())
            convertible = "@>" not in cmd
        if cmd is not None and ("\n" in cmd or "\r" in cmd):
            raise ValueError(f"step {i}: math must be a single line")
        if (st.get("approx") or st.get("to")) and not cmd:
            raise ValueError(f"step {i}: approx and to need math or latex")
        if cmd and st.get("approx"):
            cmd = f"approx({cmd})"
        if cmd and st.get("to"):
            import nspire_units
            if not convertible:
                raise ValueError(f"step {i}: to converts a quantity to evaluate; this step already converts or "
                                 "doesn't evaluate a quantity (solve, define ...)")
            try:
                unit = nspire_units.unit_command(st["to"])
            except nspire_units.UnitError as e:
                raise ValueError(f"step {i}: to: {e}") from None
            cmd = (f"({cmd})" if "|" in cmd else cmd) + "@>" + unit
        if not cmd and not text:
            raise ValueError(f"step {i}: needs text, math or latex")
        out.append({"text": text, "cmd": cmd, "label": f"step {i}", "result": None, "seconds": 0.0})
    return out


# "y=" or "f(x)=" in front of a function is dropped; nothing else that holds an = is a function
_GRAPH_LHS = re.compile(r"^\s*(?:y|[fgh]\d{0,2}\s*\(\s*x\s*\))\s*=(?![=<>])")
_LATEX_GRAPH = re.compile(r"\s*\$+(.*?)\$+\s*", re.DOTALL)
_COMPARISONS = set("=<>≤≥≠")


def _top_level_comparison(expr: str) -> bool:
    """True if expr compares at its top level (an equation or inequality), ignoring a
    condition after "|" (x^2|x>0) and anything inside brackets."""
    depth = 0
    for ch in expr:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch == "|":
            return False
        elif depth == 0 and ch in _COMPARISONS:
            return True
    return False


def plan_graphs(graphs: list[str] | None) -> list[dict]:
    """Check the functions for a Graphs page and translate them, without touching the bridge.

    Each is a function of x in TI-Nspire syntax ("x^2-1"), or LaTeX between $...$
    ("$\\frac{1}{x}$"); a leading y= or f(x)= is dropped. Returns check items for
    _prepare: cmd defines a temporary function of x (like the entry line's f1(x)=...),
    expr is what gets typed.
    """
    if graphs is None:
        return []
    if not isinstance(graphs, list):
        raise ValueError('graphs must be a list of functions of x, e.g. ["x^2-1", "$\\\\frac{1}{x}$"]')
    if len(graphs) > MAX_GRAPHS:
        raise ValueError(f"graphs: at most {MAX_GRAPHS} functions (f1 to f{MAX_GRAPHS})")
    out = []
    for i, g in enumerate(graphs, 1):
        if not isinstance(g, str) or not g.strip():
            raise ValueError(f"graph {i}: must be a non-empty string")
        if "\n" in g or "\r" in g:
            raise ValueError(f"graph {i}: must be a single line")
        m = _LATEX_GRAPH.fullmatch(g)
        if m:
            import nspire_latex
            try:
                expr = nspire_latex.plan(_GRAPH_LHS.sub("", m.group(1), count=1), "eval").command
            except nspire_latex.LatexError as e:
                raise ValueError(f"graph {i}: {e}") from None
        else:
            expr = to_cas_chars(_GRAPH_LHS.sub("", g, count=1))
        expr = expr.strip()
        if not expr or "\n" in expr or "\r" in expr:
            raise ValueError(f"graph {i}: needs a function of x, e.g. x^2-1")
        if scan_names(expr).defines:
            raise ValueError(f"graph {i} ({g.strip()}): a function to graph can't store or define anything")
        if _top_level_comparison(expr):
            raise ValueError(f"graph {i} ({g.strip()}): give a function of x, without =, < or > "
                             f"(for y=x^2-1 write x^2-1)")
        out.append({"text": "", "cmd": f"Define {GRAPH_CHECK_NAME}(x)={expr}", "expr": expr,
                    "label": f"graph {i}", "result": None, "seconds": 0.0})
    return out


_SHEET_KEYS = {"csv", "columns", "names", "header"}
_SHEET_NAME_RE = re.compile(r"[^\W\d_]\w*")  # a letter first (an underscore starts a unit, _m)
_CELL_REF_RE = re.compile(r"[A-Za-z]{1,2}\d+")  # a1, B12: a spreadsheet cell, not a column name


@dataclass
class Sheet:
    """A Lists & Spreadsheet page to fill: columns A, B, ... with these values."""
    names: list[str | None]      # one per column; None: the column has no name (defines no list)
    columns: list[list[str]]     # TI-Nspire number literals, column by column (all the same length)
    checks: list[dict]           # items for _prepare: each column stored once on the bridge, then undone

    @property
    def rows(self) -> int:
        return len(self.columns[0]) if self.columns else 0


def _column_letter(k: int) -> str:
    return chr(ord("A") + k)


def plan_spreadsheet(sheet: dict | None, items: Sequence[dict] = (),
                     graph_items: Sequence[dict] = ()) -> Sheet | None:
    """Check the data for a Lists & Spreadsheet page, without touching the bridge.

    sheet: {"csv": text} or {"columns": [[...], ...]}, with optional "names" (one per column,
    "" for a column without a name; with csv they replace the header row) and "header" (csv:
    whether the first row is a header; detected if left out). Read by nspire_data.parse_table.
    A column name becomes a list variable of the new document, so it must be a valid TI-Nspire
    name that no step or graph defines or uses (the Notes page is typed before the data exists).
    """
    if sheet is None:
        return None
    if not isinstance(sheet, dict):
        raise ValueError('spreadsheet must be an object like {"csv": "t,d\\n0,0\\n1,4.9"} or '
                         '{"columns": [[0,1,2],[0,4.9,19.6]], "names": ["t", "d"]}')
    extra = set(sheet) - _SHEET_KEYS
    if extra:
        raise ValueError(f"spreadsheet: unknown key(s) {', '.join(sorted(extra))} (use csv or columns, and "
                         f"optionally names and header)")
    if sheet.get("header") is not None and not isinstance(sheet["header"], bool):
        raise ValueError("spreadsheet: header must be true or false")
    import nspire_data
    try:
        table = nspire_data.parse_table(sheet.get("csv"), sheet.get("columns"), sheet.get("names"),
                                        sheet.get("header"))
    except ValueError as e:
        raise ValueError(f"spreadsheet: {e}") from None
    cells = table.rows * len(table.columns)
    if cells > MAX_SHEET_CELLS:
        raise ValueError(f"spreadsheet: at most {MAX_SHEET_CELLS} values are typed (got {table.rows} rows x "
                         f"{len(table.columns)} columns = {cells}); each one is a few keystrokes")

    taken: dict[str, str] = {}  # lower-case name -> what already has it
    for it in list(items) + list(graph_items):
        if it.get("cmd"):
            nm = scan_names(it["cmd"])
            for n in (*nm.defines, *nm.uses):
                taken.setdefault(n.lower(), it.get("label") or "a step")
    for k in range(1, len(graph_items) + 1):
        taken.setdefault(f"f{k}", f"graph {k} (f{k})")
    for n in (GRAPH_CHECK_NAME, SHEET_CHECK_NAME):
        taken.setdefault(n, "this tool")

    names: list[str | None] = []
    seen: dict[str, int] = {}
    for k in range(len(table.columns)):
        name = table.header(k, "").strip() or None
        where = f"spreadsheet column {k + 1} ({_column_letter(k)})"
        if name is not None:
            if not _SHEET_NAME_RE.fullmatch(name) or len(name) > MAX_SHEET_NAME:
                raise ValueError(f"{where}: {name!r} can't be a column name: it becomes a list variable, so it "
                                 f"must be a letter followed by letters, digits or _ (at most {MAX_SHEET_NAME} "
                                 f"characters, no spaces or units), e.g. height or t1. Pass names to rename it, "
                                 f"or \"\" to leave the column unnamed.")
            low = name.lower()
            if low in seen:
                raise ValueError(f"{where}: the name {name!r} is already column {seen[low]}'s (names ignore case)")
            if low in taken:
                raise ValueError(f"{where}: the name {name!r} is also used by {taken[low]}. Each column name "
                                 f"becomes a list variable of the new document, and the Notes page is typed before "
                                 f"the data, so give the column another name or leave it out of the steps.")
            if _CELL_REF_RE.fullmatch(name):
                raise ValueError(f"{where}: {name!r} looks like a spreadsheet cell (column letters and a row "
                                 f"number), which TI-Nspire doesn't accept as a column name; pick another name.")
            seen[low] = k + 1
        names.append(name)

    checks = []
    for k, col in enumerate(table.columns):
        name = names[k]
        var = name or SHEET_CHECK_NAME
        label = f"spreadsheet column {k + 1} ({_column_letter(k)}{': ' + name if name else ''})"
        preview = ",".join(col[:3]) + (",..." if len(col) > 3 else "")
        for j, chunk in enumerate(nspire_data.list_chunks(col, SHEET_CHUNK)):
            lit = to_cas_chars(nspire_data.list_literal(chunk))
            cmd = f"{var}:={lit}" if j == 0 else f"{var}:=augment({var},{lit})"
            checks.append({"text": "", "cmd": cmd, "expr": f"{{{preview}}}", "label": label,
                           "result": None, "seconds": 0.0})
    return Sheet(names, table.columns, checks)


_BAD_NAME_CHARS = set('<>:"/\\|?*')


def check_save(save_as: str | None, overwrite: bool = False) -> str | None:
    """The full path to save the new document to, or None. Refuses what can't be saved there."""
    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be true or false")
    if save_as is None:
        if overwrite:
            raise ValueError("overwrite only applies together with save_as")
        return None
    if not isinstance(save_as, str) or not save_as.strip():
        raise ValueError("save_as must be the full path of a .tns file")
    p = os.path.expanduser(save_as.strip())
    if any(ord(c) < 32 for c in p):
        raise ValueError("save_as must be a single line")
    if not os.path.isabs(p) or not os.path.splitdrive(p)[0]:
        raise ValueError(f"save_as must be a full path, e.g. C:\\Users\\you\\Documents\\area.tns (got {save_as})")
    p = os.path.normpath(p)
    folder, name = os.path.split(p)
    if not name.lower().endswith(".tns") or name.lower() == ".tns":
        raise ValueError(f"save_as must name a .tns file (got {name})")
    if _BAD_NAME_CHARS & set(name):
        raise ValueError(f"save_as: a file name can't contain any of {''.join(sorted(_BAD_NAME_CHARS))}")
    if len(p) >= 260:
        raise ValueError("save_as: the path is too long (260 characters or more)")
    if not os.path.isdir(folder):
        raise ValueError(f"save_as: the folder {folder} doesn't exist")
    if os.path.isdir(p):
        raise ValueError(f"save_as: {p} is a folder")
    if os.path.exists(p) and not overwrite:
        raise ValueError(f"save_as: {p} already exists; pass overwrite=true to replace it (TI-Nspire or Windows "
                         f"may still ask to confirm, and that's left to the user)")
    return p


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
        with contextlib.suppress(ValueError):
            value = bridge.eval_one(name, timeout_s, raw=True)
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
             cancel: threading.Event | None = None,
             on_check: Callable[[dict], None] | None = None) -> list[str]:
    """Evaluate every math step on the bridge (fills result and seconds), then undo what that changed there.

    Returns warnings. Raises ValueError naming the first step that fails, or NotesAborted
    if cancel is set between steps (the undo runs either way). on_check(item) runs before
    each item that has math is checked (for progress).
    """
    saved: dict[str, _Saved] = {}
    try:
        warnings = _run_steps(bridge, items, timeout_s, saved, cancel, on_check)
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
               cancel: threading.Event | None = None,
               on_check: Callable[[dict], None] | None = None) -> list[str]:
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
        if on_check is not None:
            on_check(it)
        label = it.get("label") or f"step {i}"
        names = scan_names(cmd)
        stale = sorted(n for n in names.uses if n not in defined and type_of(n) != "NONE")
        if stale:
            warnings.append(f"{label[:1].upper()}{label[1:]} uses {', '.join(stale)}, stored in the bridge document "
                            f"but not in the new Notes document, so the page may show a different result than the "
                            f"one returned here. Define {'it' if len(stale) == 1 else 'them'} in an earlier step "
                            f"or write the value in.")
        for name in names.defines:
            if name.lower() not in saved:
                saved[name.lower()] = _snapshot(bridge, name, type_of(name), timeout_s, others_stored(name))
        defined.update(n.lower() for n in names.defines)
        t0 = time.monotonic()
        try:
            it["result"] = bridge.eval_one(cmd, timeout_s)
        except ValueError as e:
            raise ValueError(f"{label} ({it.get('expr') or cmd}): {e}") from None
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


# Sampled pixels that clearly change when a drop-down opens, compared with the window just before
# its Down key: File ~1360-1470, Insert ~950-1350 (measured). Alt alone ~120-245. Wallpaper drift
# through a translucent window theme: ~100 within MENU_OPEN_WAIT_S, up to ~600 within a second.
MENU_OPEN_PIXELS = 700
MENU_OPEN_WAIT_S = 0.6  # a menu shows within ~0.4 s of its Down key; later changes are drift, so try again


def _settled_menu_area(hwnd: int, timeout: float = 5.0, quiet: int = 3) -> bytes:
    """menu_area once the window has stopped redrawing (e.g. a document still opening):
    quiet samples in a row, 0.2 s apart, with (almost) nothing changed. One quiet sample
    isn't enough: a new document can pause before it paints its page."""
    prev = menu_area(hwnd)
    still = 0
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.2)
        cur = menu_area(hwnd)
        still = still + 1 if menu_changed(prev, cur) < 50 else 0
        if still >= quiet:
            return cur
        prev = cur
    return prev


def _menu(guard: _Guard, menu_index: int, item_index: int, what: str, sub_item: int | None = None) -> None:
    """Pick a menu-bar item by position (1-based), so any UI language works.

    Right after a new document opens TI-Nspire ignores the Alt. Its menus are drawn
    by Java inside the window, invisible to Windows, so whether one opened is told
    from the pixels where it appears; if it didn't, try again.
    """
    hwnd = guard.hwnd
    for attempt in range(5):
        guard()
        _settled_menu_area(hwnd)
        chord("alt", pause=0.15, guard=guard)
        for _ in range(menu_index - 1):
            chord("right", pause=0.05, guard=guard)
        # Compare with the window just before the menu opens: with a translucent window theme an
        # animated wallpaper drifts through it, and over seconds that adds up to hundreds of pixels.
        base = menu_area(hwnd)
        chord("down", pause=0.05, guard=guard)  # opens the menu with its first item highlighted
        deadline = time.monotonic() + MENU_OPEN_WAIT_S
        while time.monotonic() < deadline:
            if menu_changed(base, menu_area(hwnd)) > MENU_OPEN_PIXELS:
                time.sleep(0.1)
                for _ in range(item_index - 1):
                    chord("down", pause=0.05, guard=guard)
                if sub_item is not None:  # the item opens a submenu, with its first item highlighted
                    chord("right", pause=0.15, guard=guard)
                    for _ in range(sub_item - 1):
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
    _menu(guard, FILE_MENU, NEW_COMPUTER_ITEM, "File > New computer document")
    name = _wait_doc_change(guard.hwnd, before, 8)
    if not name:
        raise NotesAborted("TI-Nspire didn't open a new document (is a dialog open in it?).")
    time.sleep(0.5)
    _menu(guard, INSERT_MENU, INSERT_NOTES_ITEM, "Insert > Notes")
    time.sleep(0.8)
    return name


# ------------------------------------------------------------ Display Digits

def _set_digits(guard: _Guard, digits: int) -> None:
    """File > Settings > Document Settings, Display Digits = Float<digits>, then OK.

    Only the new document's settings change (the dialog belongs to the active document).
    The dialog is a Java one (SunAwtDialog): its fields can't be read from outside, but it
    opens with Display Digits focused; Down opens its list, where Up/Down move through Float, Float1 ...
    Float12, Fix0 ... Enter picks the value, a second Enter is OK. Each key is sent only
    while that dialog is the foreground window; if it doesn't open or close as expected,
    the run stops (no Esc: if the focus had moved, it would land in another program).
    """
    hwnd = guard.hwnd
    before = process_windows(hwnd)
    _menu(guard, FILE_MENU, SETTINGS_ITEM, "File > Settings > Document Settings", DOCUMENT_SETTINGS_ITEM)
    dialog = _wait_dialog(hwnd, before, 6)
    if dialog is None or window_class(dialog) != "SunAwtDialog":
        raise NotesAborted("TI-Nspire's Document Settings dialog didn't open, so Display Digits wasn't set and "
                           "nothing was typed. Check TI-Nspire for an open menu or dialog.")
    dguard = _DialogGuard(guard, dialog, "Document Settings")
    dguard()
    # Down opens the list (and moves one item down); Up does nothing while it's closed, and
    # keys sent while it opens are dropped, so open it, wait, then move.
    chord("down", pause=0.6, guard=dguard)
    for _ in range(DIGITS_TOP_KEYS):
        chord("up", pause=0.06, guard=dguard)
    for _ in range(digits):
        chord("down", pause=0.08, guard=dguard)
    time.sleep(0.2)
    chord("enter", pause=0.3, guard=dguard)  # picks Float<digits> and closes the list
    chord("enter", pause=0.3, guard=dguard)  # OK
    deadline = time.monotonic() + 4
    while window_open(dialog) and time.monotonic() < deadline:
        time.sleep(0.1)
    if window_open(dialog):
        raise NotesAborted("TI-Nspire's Document Settings dialog didn't close after OK, so the page wasn't typed. "
                           "Check Display Digits in the dialog and close it in TI-Nspire.")
    time.sleep(0.4)


# ------------------------------------------------------------ Graphs page

def _new_graphs_page(guard: _Guard, page_size: str) -> None:
    """A new page after the current one, holding the Graphs app.

    Computer documents: Insert > Page, then Insert > Graphs fills the new, empty page.
    Handheld documents: Insert > Page shows the add-page menu, where GRAPHS_MENU_KEY
    picks Graphs (as Ctrl+N's menu does for Notes).
    """
    if page_size == "computer":
        _menu(guard, INSERT_MENU, INSERT_PAGE_ITEM, "Insert > Page")
        time.sleep(0.5)
        _menu(guard, INSERT_MENU, INSERT_GRAPHS_ITEM, "Insert > Graphs")
    else:
        windows = process_windows(guard.hwnd)
        _menu(guard, INSERT_MENU, INSERT_PAGE_ITEM, "Insert > Page")
        if not wait_for_popup(guard.hwnd, windows):
            time.sleep(1.5)
        time.sleep(0.3)
        chord(GRAPHS_MENU_KEY, pause=0.9, guard=guard)
    time.sleep(1.2)  # the Graphs app opens with its entry line (f1(x)=) focused


def _type_graphs(guard: _Guard, graphs: list[dict], tick: _Ticker) -> None:
    """Each function into the entry line, then Enter. Enter graphs it and closes the
    entry line, so the next one first shows it again (GRAPHS_ENTRY_KEY), at f2, f3, ...
    Pasted, not typed: typing would fight the 2D editor's templates (x^2+1 → x^(2+1))."""
    for k, g in enumerate(graphs):
        tick(f"Graphing f{k + 1}(x)={readable(g['expr'])}")
        if k:
            chord(GRAPHS_ENTRY_KEY, pause=0.4, guard=guard)
        _paste(guard, g["expr"])
        chord("enter", pause=0.8, guard=guard)


# ------------------------------------------------------------ Lists & Spreadsheet page

def _new_lists_page(guard: _Guard, page_size: str) -> None:
    """A new page after the current one, holding the Lists & Spreadsheet app, as for Graphs:
    computer documents Insert > Page then Insert > Lists & Spreadsheet, handheld documents
    Insert > Page and LISTS_MENU_KEY in the add-page menu."""
    if page_size == "computer":
        _menu(guard, INSERT_MENU, INSERT_PAGE_ITEM, "Insert > Page")
        time.sleep(0.5)
        _menu(guard, INSERT_MENU, INSERT_LISTS_ITEM, "Insert > Lists & Spreadsheet")
    else:
        windows = process_windows(guard.hwnd)
        _menu(guard, INSERT_MENU, INSERT_PAGE_ITEM, "Insert > Page")
        if not wait_for_popup(guard.hwnd, windows):
            time.sleep(1.5)
        time.sleep(0.3)
        chord(LISTS_MENU_KEY, pause=0.9, guard=guard)
    time.sleep(1.2)  # the spreadsheet opens with cell A1 selected


def _type_sheet(guard: _Guard, sheet: Sheet, tick: _Ticker) -> None:
    """Fill the new spreadsheet, starting from cell A1.

    Names first: two Ups lead from A1 (past the formula row ◆) to column A's name cell. After
    a name's Enter, two Ups go back to the name row wherever Enter left the cursor (Up does
    nothing in the top row), and Right moves to the next column's name; then Left and two
    Downs lead back to A1. Then the values, column by column: each typed and entered (Enter
    moves one cell down), then Right and one Up per row to the next column's first cell.
    Typed, not pasted: plain numbers can't trip the 2D editor's templates, and typing needs
    no clipboard round trip per cell.
    """
    n, last = sheet.rows, len(sheet.columns) - 1
    if any(sheet.names):
        tick("Naming the spreadsheet columns")
        chord("up", pause=0.1, guard=guard)
        chord("up", pause=0.1, guard=guard)
        for k, name in enumerate(sheet.names):
            if name:
                type_text(name, guard=guard)
                chord("enter", pause=0.4, guard=guard)  # the column's list variable is created
                chord("up", pause=0.1, guard=guard)
                chord("up", pause=0.1, guard=guard)
            if k < last:
                chord("right", pause=0.1, guard=guard)
        for _ in range(last):
            chord("left", pause=0.05, guard=guard)
        chord("down", pause=0.1, guard=guard)
        chord("down", pause=0.1, guard=guard)
    for k, col in enumerate(sheet.columns):
        name = sheet.names[k]
        tick(f"Filling column {_column_letter(k)}{' (' + name + ')' if name else ''}: {n} values")
        for v in col:
            type_text(to_cas_chars(v), guard=guard)
            chord("enter", pause=0.12, guard=guard)
        if k < last:
            chord("right", pause=0.1, guard=guard)
            for _ in range(n):
                chord("up", pause=0.03, guard=guard)


# ------------------------------------------------------------ saving

class _DialogGuard:
    """_Guard, and the dialog must be the foreground window (not TI-Nspire's main
    window or another of its popups)."""

    def __init__(self, guard: _Guard, dialog: int, what: str = "Save"):
        self.guard, self.dialog, self.what = guard, dialog, what

    def __call__(self) -> None:
        self.guard()
        if foreground_window() != self.dialog:
            raise NotesAborted(f"The {self.what} dialog stopped being the active window, so typing into it stopped; "
                               "nothing was typed elsewhere. If the dialog is still open, finish or cancel it "
                               "in TI-Nspire.")


def _stat(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _wait_dialog(hwnd: int, before: set[int], timeout: float) -> int | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [h for h in new_dialogs(hwnd, before) if window_open(h)]
        if found:
            time.sleep(0.4)  # let it finish appearing and put the focus in its file name box
            return found[0]
        time.sleep(0.1)
    return None


def _title_of(h: int) -> str:
    try:
        return window_title(h)
    except Exception:
        return ""


SAVE_WAIT_S = 15.0


def _save(guard: _Guard, path: str, overwrite: bool) -> dict:
    """Save the new document as path through TI-Nspire's Save dialog. Returns
    {path, saved, message}; saved is True only once the dialog closed and the file
    appeared (or changed) on disk.

    Each step is checked from outside before the next key: the dialog opened and is a
    standard Windows file dialog, its file name box has the focus, the box holds exactly
    path before Enter. Anything else stops with the dialog left open for the user (no
    Esc: if the focus had moved, it would land in another program). A question after
    Save (replace the file? an error?) is never answered here.
    """
    hwnd = guard.hwnd

    def failed(message: str) -> dict:
        return {"path": path, "saved": False, "message": message}

    old = _stat(path)
    if not os.path.isdir(os.path.dirname(path)):
        return failed(f"Not saved: the folder {os.path.dirname(path)} no longer exists.")
    if old is not None and not overwrite:
        return failed(f"Not saved: {path} appeared while the page was being built, and overwrite wasn't set.")
    before = process_windows(hwnd)
    before_doc = _doc_name(hwnd)
    chord(SAVE_KEY, pause=0.3, guard=guard)
    dialog = _wait_dialog(hwnd, before, 8)
    if dialog is None:
        doc = _doc_name(hwnd)
        if doc != before_doc:
            return failed(f"TI-Nspire saved the document as '{doc}' without showing its Save dialog, so it's "
                          f"where TI-Nspire chose, not at {path}.")
        return failed(f"TI-Nspire didn't open its Save dialog ({SAVE_KEY}), so nothing was saved.")
    if window_class(dialog) != FILE_DIALOG_CLASS:
        return failed(f"TI-Nspire's Save dialog isn't a standard Windows file dialog, so where a typed name would "
                      f"land can't be checked, and nothing was typed into it. It's open in TI-Nspire: type the "
                      f"name there ({path}) and save.")
    dguard = _DialogGuard(guard, dialog)
    dguard()
    box = focused_control(dialog)
    if not is_file_name_box(dialog, box):
        return failed(f"TI-Nspire's Save dialog opened, but its file name box doesn't have the keyboard focus, so "
                      f"nothing was typed into it. It's open in TI-Nspire: type the name there ({path}) and save.")
    chord("end", guard=dguard)
    chord("shift+home", guard=dguard)  # select the suggested name, so the path replaces it
    type_text(path, guard=dguard)
    time.sleep(0.2)
    text = control_text(box)
    if text and text != path and text.startswith(path):  # auto-complete appended a (selected) suggestion
        chord("delete", pause=0.1, guard=dguard)
        text = control_text(box)
    if text != path:
        return failed(f"Save wasn't pressed: the file name box shows {text!r}, not {path!r}. The Save dialog is "
                      f"still open in TI-Nspire: fix the name there and save, or cancel it.")
    if focused_control(dialog) != box:
        return failed("Save wasn't pressed: the focus left the file name box. The Save dialog is still open in "
                      "TI-Nspire: save or cancel it there.")
    chord("enter", pause=0.3, guard=dguard)

    t_enter = time.monotonic()
    pressed_again = False
    while time.monotonic() - t_enter < SAVE_WAIT_S:
        prompts = [h for h in new_dialogs(hwnd, before) if h != dialog and window_open(h)]
        if prompts:
            return failed(f"After Save, TI-Nspire showed a message ('{_title_of(prompts[0])}'), e.g. to confirm "
                          f"replacing {os.path.basename(path)} or to report an error. Nothing was pressed in it: "
                          f"answer it in TI-Nspire.")
        now = _stat(path)
        written = now is not None and now != old and now[1] > 0
        if written and not window_open(dialog):
            time.sleep(0.3)
            return {"path": path, "saved": True, "message": f"Saved as {path}."}
        if (window_open(dialog) and not pressed_again and time.monotonic() - t_enter > 3
                and focused_control(dialog) == box and control_text(box) == path):
            chord("enter", pause=0.3, guard=dguard)  # the first Enter may only have closed the suggestion list
            pressed_again = True
        time.sleep(0.2)
    if window_open(dialog):
        return failed("The Save dialog is still open in TI-Nspire after Enter; save or cancel it there.")
    return failed(f"The Save dialog closed, but {path} didn't appear within {SAVE_WAIT_S:g} s. Check in "
                  f"TI-Nspire whether and where the document was saved.")


# ------------------------------------------------------------ the whole run

class _Ticker:
    """Calls progress(done, total, message) before each unit of work. A failing
    callback is logged, never allowed to stop the run."""

    def __init__(self, progress: Progress | None, total: int):
        self.progress, self.total, self.done = progress, max(total, 1), 0

    def _send(self, message: str) -> None:
        if self.progress is None:
            return
        try:
            self.progress(self.done, self.total, message)
        except Exception as e:
            log.info("progress callback failed: %s", e)

    def __call__(self, message: str) -> None:
        self._send(message)
        self.done = min(self.done + 1, self.total)

    def finish(self, message: str) -> None:
        self.done = self.total
        self._send(message)


def build_notes(bridge: Bridge, steps: list[dict], title: str | None = None, page_size: str = "computer",
                timeout_s: float = 10, cancel: threading.Event | None = None,
                graphs: list[str] | None = None, save_as: str | None = None, overwrite: bool = False,
                progress: Progress | None = None, digits: int | None = None,
                spreadsheet: dict | None = None) -> dict:
    """Validate the steps, then type them into a new document's Notes page.

    Call inside bridge.session() (it uses the clipboard to paste math).
    Order: check the arguments, find the TI-Nspire window (by its process), check
    the math on the bridge, and only then bring the window forward and type.
    Every key goes through a guard that stops (NotesAborted) if TI-Nspire isn't the
    foreground window or cancel is set; set cancel from another thread to stop a run.

    graphs: functions of x for a Graphs page after the Notes page (see plan_graphs).
    save_as: full path of a .tns file to save the new document to (see _save); an
    existing file only with overwrite. progress(done, total, message) is called from
    this thread before each check, page, step, function and the save.
    digits: significant figures the new document shows (Display Digits Float<digits>),
    set through its Document Settings before anything is typed.
    spreadsheet: data for a Lists & Spreadsheet page after the Notes (and Graphs) page (see
    plan_spreadsheet); each column is stored once on the bridge first, then undone.
    """
    if UNSUPPORTED:
        raise RuntimeError(UNSUPPORTED)
    check_options(title, page_size, timeout_s, digits)
    items = plan_steps(steps)
    graph_items = plan_graphs(graphs)
    sheet = plan_spreadsheet(spreadsheet, items, graph_items)
    sheet_items = sheet.checks if sheet else []
    path = check_save(save_as, overwrite)

    windows = find_nspire_windows()  # raises before any clipboard use if TI-Nspire isn't running
    hwnd = windows[0].hwnd           # this window, and no other, for the whole run
    warnings: list[str] = []
    if len(windows) > 1:
        warnings.append(f"{len(windows)} TI-Nspire windows are open; used '{windows[0].title}' "
                        f"(handle {hwnd}), the one in front or else the lowest handle.")

    checks = [it for it in items + graph_items + sheet_items if it["cmd"]]
    sheet_ticks = (1 + (1 if any(sheet.names) else 0) + len(sheet.columns)) if sheet else 0
    tick = _Ticker(progress, len(checks) + 1 + len(items) + (1 + len(graph_items) if graph_items else 0)
                   + sheet_ticks + (1 if path else 0) + (1 if digits else 0))
    order = {id(it): k for k, it in enumerate(checks, 1)}
    warnings += _prepare(bridge, items + graph_items + sheet_items, timeout_s, cancel,
                         on_check=lambda it: tick(f"Checking {it['label']} on the bridge "
                                                  f"({order[id(it)]} of {len(checks)})"))

    _check_cancel(cancel)  # last chance to stop before TI-Nspire is touched
    focus_nspire(hwnd)
    guard = _Guard(hwnd, cancel)
    tick("Opening a new document with a Notes page")
    if page_size == "computer":
        _new_computer_notes(guard)
    else:
        _new_handheld_notes(guard)
    if digits:
        tick(f"Setting Display Digits to Float{digits}")
        _set_digits(guard, digits)

    if title:
        type_text(title, guard=guard)
        chord("enter", pause=0.15, guard=guard)

    for i, it in enumerate(items, 1):
        tick(f"Typing step {i} of {len(items)}")
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

    if graph_items:
        tick("Adding a Graphs page")
        _new_graphs_page(guard, page_size)
        _type_graphs(guard, graph_items, tick)

    if sheet:
        tick("Adding a Lists & Spreadsheet page")
        _new_lists_page(guard, page_size)
        _type_sheet(guard, sheet, tick)

    save = None
    if path:
        tick(f"Saving as {path}")
        try:
            save = _save(guard, path, overwrite)
        except Exception as e:  # the page is built: report the save, don't fail the whole call
            save = {"path": path, "saved": False, "message": f"Not saved: {e}"}
        if not save["saved"]:
            warnings.append(save["message"])
    tick.finish("Saved" if save and save["saved"] else "Done")

    try:
        window = describe_window(hwnd)
        document = window["title"].rsplit(" - ", 1)[0]
    except Exception:  # closed right at the end: the page is built, don't turn that into a failure
        window, document = {"hwnd": hwnd}, None
    if save and save["saved"]:
        note = f"Built in a new document and saved as {path}."
    else:
        note = "Built in a new, unsaved document; save it from TI-Nspire if you want to keep it."
    if sheet and any(sheet.names):
        note += (" The spreadsheet's named columns are list variables of the new document ("
                 + ", ".join(nm for nm in sheet.names if nm) + ").")
    if digits:
        note += (f" The page shows {digits} significant figures (Display Digits Float{digits}); the results "
                 "returned here are the bridge's, with more digits.")
    if warnings:
        note += " Warning: " + " ".join(warnings)
    out = {
        "document": document,
        "window": window,
        "steps": [{"text": it["text"], "math": it["cmd"] and readable(it["cmd"]), "result": it["result"]}
                  for it in items],
        "warnings": warnings,
        "note": note,
    }
    if graph_items:
        out["graphs"] = [f"f{k}(x)={readable(g['expr'])}" for k, g in enumerate(graph_items, 1)]
    if sheet:
        out["spreadsheet"] = {"rows": sheet.rows, "columns": [
            {"column": _column_letter(k), "name": sheet.names[k], "first": [readable(v) for v in col[:3]]}
            for k, col in enumerate(sheet.columns)]}
    if save is not None:
        out["save"] = save
    return out
