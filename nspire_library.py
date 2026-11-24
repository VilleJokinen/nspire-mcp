"""A function library: save Define'd functions (and programs, variables) to a JSON file and load
them back into any open TI-Nspire document.

Standard library only (no pywin32, no SymPy), so it imports and is tested on any platform. The
bridge is passed in; everything here that talks to it runs inside bridge.session().

What the CAS gives back (checked on the real CAS through math.evalStr): a stored function's
source can't be read. `f` and `string(f)` are error 40 for a FUNC or PRGM, and getVarInfo()
lists only name, type, library and lock. So the library keeps the exact command a definition was
made with (`definitions=`), and for a function saved by name from the document it rebuilds the
command from what the function returns for symbolic arguments: f(mcplibp1) gives mcplibp1^2+1,
and calls with the wrong number of arguments are error 930 (too few) or 940 (too many), which
finds the arity. That copy is marked exact=False: it is the CAS's evaluated, simplified result
(a Func's Local/If structure is gone, stored variables it used are substituted), and a function
that can't be evaluated symbolically, or a program, can't be rebuilt at all.

Every command is checked on the CAS before it is saved or loaded: it is defined under a
temporary name (mcplibk1, ...), its type is read back with getType, and it's deleted again. On
load, a name already in the document is compared with the library's definition by evaluating
both (functions with symbolic arguments and at three numeric points; the CAS's own result strings
compared as text, the same error counting as the same):
the same result is "unchanged", anything else a conflict, and nothing is stored while there are
conflicts unless overwrite=True.

File: <library dir>/<library>.json, human-readable JSON
  {"format": "nspire-mcp-library", "version": 1, "library": name,
   "entries": [{name, kind, params, command, exact, saved_at, source_document, note}]}
The folder is NSPIRE_LIBRARY_DIR, else %APPDATA%\\nspire-mcp\\library on Windows,
~/Library/Application Support/nspire-mcp/library on macOS, $XDG_DATA_HOME (or ~/.local/share)
/nspire-mcp/library elsewhere: never the repo.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from nspire_chars import NS_C, NS_N, readable, to_cas_chars

FORMAT = "nspire-mcp-library"
VERSION = 1
DEFAULT_LIBRARY = "default"
MAX_ARITY = 8            # functions saved by name: arities tried, 0..MAX_ARITY
MAX_COMMAND_CHARS = 3500  # one command must fit in one bridge request (BATCH_MAX_CHARS is 4000)
MAX_ENTRIES = 500
TEMP_PREFIX = "mcplibk"   # temporary names commands are checked under
SYM_PREFIX = "mcplibp"    # symbolic arguments functions are called with
KINDS = ("function", "program", "variable")
VALUE_TYPES = {"NUM", "EXPR", "LIST", "MAT", "STR"}
_KIND_TYPES = {"function": {"FUNC"}, "program": {"PRGM"}, "variable": VALUE_TYPES}
_CODE_RE = re.compile(r"error (\d+)")

_NAME = r"[^\W\d_]\w*"
_NAME_RE = re.compile(rf"^{_NAME}$")
_LIB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DEFINE_RE = re.compile(rf"^\s*Define\s+(?:(LibPub|LibPriv)\s+)?({_NAME})\s*(?:\(([^()]*)\))?\s*=(?!=)(.*)$",
                        re.IGNORECASE | re.DOTALL)
_ASSIGN_RE = re.compile(rf"^\s*({_NAME})\s*(?:\(([^()]*)\))?\s*:=(.*)$", re.DOTALL)
_BLOCK_RE = re.compile(r"^(Func|Prgm)\b(.*)\b(EndFunc|EndPrgm)$", re.IGNORECASE | re.DOTALL)
_STRING_RE = re.compile(r'"[^"]*"')
_PARAM_SETS = (("x", "y", "z", "w"), ("a", "b", "c", "d"), ("t", "u", "v", "s"))


class LibraryError(ValueError):
    pass


# ------------------------------------------------------------ commands (offline)

@dataclass(frozen=True)
class Parsed:
    name: str
    kind: str                 # function, program or variable
    params: tuple[str, ...]
    command: str              # normalised: one line, readable characters
    body: str
    lib: str | None = None    # LibPub / LibPriv


def _join_lines(body: str) -> str:
    """A Func/Prgm body pasted from the program editor, one statement per line -> one line with ':'."""
    lines = [ln.strip() for ln in body.replace("\r", "\n").split("\n")]
    return ":".join(ln for ln in lines if ln)


def _outside_strings(text: str) -> str:
    return _STRING_RE.sub('""', text)


def parse_command(command: str, internal: bool = False) -> Parsed:
    """Read one definition: Define f(x)=..., Define f(x)=Func:...:EndFunc, Define p()=Prgm:...:EndPrgm,
    f(x):=..., or a:=value / Define a=value. Raises LibraryError for anything else."""
    if not isinstance(command, str) or not command.strip():
        raise LibraryError("A definition must be a non-empty string such as 'Define f(x)=x^2+1'.")
    text = readable(command.strip())
    lib = None
    m = _DEFINE_RE.match(text)
    if m:
        lib, name, params_text, body = m.group(1), m.group(2), m.group(3), m.group(4)
    else:
        m = _ASSIGN_RE.match(text)
        if not m:
            raise LibraryError(f"{command!r} isn't a definition. Give it as 'Define f(x)=...', "
                               f"'Define f(x)=Func:...:EndFunc', 'Define p()=Prgm:...:EndPrgm' or 'a:=value'.")
        name, params_text, body = m.group(1), m.group(2), m.group(3)
    params: tuple[str, ...] = ()
    if params_text is not None:
        params = tuple(p.strip() for p in params_text.split(",")) if params_text.strip() else ()
        bad = [p for p in params if not _NAME_RE.match(p)]
        if bad:
            raise LibraryError(f"{command!r}: parameters must be plain names, not {bad!r}.")
        if len({p.lower() for p in params}) != len(params):
            raise LibraryError(f"{command!r}: a parameter name repeats.")
    body = body.strip()
    block = _BLOCK_RE.match(_join_lines(body))
    if block:
        start, end = block.group(1).lower(), block.group(3).lower()
        if (start, end) not in (("func", "endfunc"), ("prgm", "endprgm")):
            raise LibraryError(f"{command!r}: {block.group(1)} must end with End{block.group(1)}.")
        if m.re is _ASSIGN_RE:
            raise LibraryError(f"{command!r}: a Func or Prgm block needs Define, e.g. 'Define f(x)=Func:...:EndFunc'.")
        if params_text is None:
            raise LibraryError(f"{command!r}: a {block.group(1)} needs a parameter list, e.g. {name}().")
        kind = "function" if start == "func" else "program"
        body = _join_lines(body)
    else:
        if "\n" in body or "\r" in body:
            raise LibraryError(f"{command!r}: only a Func or Prgm block may span several lines.")
        plain = _outside_strings(body)
        if not body:
            raise LibraryError(f"{command!r}: nothing after the '='.")
        if ":" in plain or "→" in plain or "=:" in plain:
            raise LibraryError(f"{command!r}: one definition per command (no ':' or '→' outside a Func block).")
        if re.match(r"(?i)\s*(Func|Prgm)\b", plain):
            raise LibraryError(f"{command!r}: a Func or Prgm block must end with EndFunc or EndPrgm.")
        kind = "function" if params_text is not None else "variable"
    if kind == "variable" and lib:
        raise LibraryError(f"{command!r}: {lib} is for functions and programs.")
    if not internal and name.lower().startswith((TEMP_PREFIX, SYM_PREFIX)):
        raise LibraryError(f"{name}: names starting with {TEMP_PREFIX} or {SYM_PREFIX} are used by the library itself.")
    out = build_command(name, kind, params, body, lib)
    if len(out) > MAX_COMMAND_CHARS:
        raise LibraryError(f"{name}: the definition is {len(out)} characters; at most {MAX_COMMAND_CHARS} fit.")
    return Parsed(name, kind, params, out, body, lib)


def build_command(name: str, kind: str, params: tuple[str, ...] | list[str], body: str, lib: str | None = None) -> str:
    if kind == "variable":
        return f"{name}:={body}"
    head = f"Define {lib} " if lib else "Define "
    return f"{head}{name}({','.join(params)})={body}"


def _temp_command(p: Parsed, temp: str) -> str:
    """The same definition under a temporary name (LibPub/LibPriv dropped: only the body is checked)."""
    return to_cas_chars(build_command(temp, p.kind, p.params, p.body))


def _call(name: str, n: int) -> str:
    return f"{name}({','.join(f'{SYM_PREFIX}{i}' for i in range(1, n + 1))})"


# Numbers functions are also called with when they are compared (a Func whose If tests its
# argument can't be called with symbols): odd values, each argument its own.
SAMPLE_VALUES = ("0.37", "-1.21", "2.53", "1.7", "-0.62", "3.14", "-2.4", "0.85", "4.2")
SAMPLE_POINTS = 3


def _probes(name: str, kind: str, n: int) -> list[str]:
    """What is evaluated to compare a definition with another: a variable's value; a function called
    with symbols, then at SAMPLE_POINTS numeric points (none for a function without arguments)."""
    if kind == "variable":
        return [name]
    out = [_call(name, n)]
    if n:
        for j in range(SAMPLE_POINTS):
            args = [SAMPLE_VALUES[(j * n + i) % len(SAMPLE_VALUES)] for i in range(n)]
            out.append(f"{name}({','.join(args)})")
    return out


def _same(pairs: list[tuple[dict, dict]]) -> bool:
    """Both definitions give the same CAS text everywhere (or fail the same way), and at least once a result."""
    def key(r: dict) -> tuple:
        return (True, r["result"]) if r["ok"] else (False, _error_code(r["error"]) or r["error"])
    return any(a["ok"] for a, _ in pairs) and all(key(a) == key(b) for a, b in pairs)


def _error_code(text: str) -> int | None:
    m = _CODE_RE.search(text or "")
    return int(m.group(1)) if m else None


def _mentions(body: str, name: str) -> bool:
    return re.search(rf"(?<![\w.]){re.escape(name)}(?!\w)", body, re.IGNORECASE) is not None


def rebuild_params(body: str, n: int) -> tuple[tuple[str, ...], str]:
    """Rename the symbolic arguments mcplibp1.. in a recovered body to x, y, ... (or a, b, ... / t, u, ...,
    whichever set the body doesn't use; x1, x2, ... otherwise)."""
    syms = [f"{SYM_PREFIX}{i}" for i in range(1, n + 1)]
    rest = body
    for s in syms:
        rest = re.sub(rf"(?<![\w.]){s}(?!\w)", " ", rest)
    choice: tuple[str, ...] | None = None
    for names in _PARAM_SETS:
        if n <= len(names) and not any(_mentions(rest, x) for x in names[:n]):
            choice = names[:n]
            break
    if choice is None:
        for base in ("x", "p", "q"):
            cand = tuple(f"{base}{i}" for i in range(1, n + 1))
            if not any(_mentions(rest, x) for x in cand):
                choice = cand
                break
    if choice is None:  # every candidate is used: keep the symbols, still valid names
        return tuple(syms), body
    out = body
    for s, new in sorted(zip(syms, choice, strict=True), key=lambda t: -len(t[0])):  # mcplibp10 before mcplibp1
        out = re.sub(rf"(?<![\w.]){s}(?!\w)", new, out)
    return choice, out


# ------------------------------------------------------------ the file

def library_dir() -> Path:
    env = os.environ.get("NSPIRE_LIBRARY_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "nspire-mcp" / "library"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "nspire-mcp" / "library"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "nspire-mcp" / "library"


def check_library_name(library: str | None) -> str:
    lib = DEFAULT_LIBRARY if library is None else library
    if not isinstance(lib, str) or not _LIB_NAME_RE.match(lib):
        raise LibraryError(f"Library name {library!r}: use letters, digits, '-' and '_' (up to 64), e.g. 'physics'.")
    return lib


def library_file(library: str | None) -> Path:
    return library_dir() / f"{check_library_name(library)}.json"


def _check_entry(e: object, where: str) -> dict:
    if not isinstance(e, dict):
        raise LibraryError(f"{where}: an entry isn't an object.")
    for key in ("name", "kind", "command"):
        if not isinstance(e.get(key), str):
            raise LibraryError(f"{where}: an entry has no {key}.")
    if e["kind"] not in KINDS:
        raise LibraryError(f"{where}: {e['name']} has an unknown kind {e['kind']!r}.")
    params = e.get("params", [])
    if not isinstance(params, list) or not all(isinstance(p, str) for p in params):
        raise LibraryError(f"{where}: {e['name']} has bad params.")
    return {
        "name": e["name"], "kind": e["kind"], "params": list(params), "command": e["command"],
        "exact": bool(e.get("exact", True)), "saved_at": e.get("saved_at") if isinstance(e.get("saved_at"), str) else None,
        "source_document": e.get("source_document") if isinstance(e.get("source_document"), str) else None,
        "note": e.get("note") if isinstance(e.get("note"), str) else None,
    }


def read_library(library: str | None) -> list[dict]:
    """The entries of a library file; [] if the file doesn't exist. Raises LibraryError if it can't be read."""
    path = library_file(library)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        raise LibraryError(f"Couldn't read {path}: {e}") from None
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise LibraryError(f"{path} isn't valid JSON ({e}); fix or move it, nothing was changed.") from None
    if not isinstance(data, dict) or data.get("format") != FORMAT or not isinstance(data.get("entries"), list):
        raise LibraryError(f"{path} isn't an nspire-mcp library file; nothing was changed.")
    if isinstance(data.get("version"), int) and data["version"] > VERSION:
        raise LibraryError(f"{path} was written by a newer version (format {data['version']}); nothing was changed.")
    return [_check_entry(e, str(path)) for e in data["entries"]]


def write_library(library: str | None, entries: list[dict]) -> Path:
    """Write the whole file (atomically: a temporary file, then a rename)."""
    lib = check_library_name(library)
    path = library_file(lib)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"format": FORMAT, "version": VERSION, "library": lib, "entries": entries}
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _locked(folder: Path):
    import nspire_log
    folder.mkdir(parents=True, exist_ok=True)
    return nspire_log._locked(folder)


def list_libraries(library: str | None = None) -> dict:
    """{folder, libraries: [{library, file, entries}]}: one library, or every *.json in the folder."""
    folder = library_dir()
    if library is not None:
        names = [check_library_name(library)]
    else:
        names = sorted(p.stem for p in folder.glob("*.json") if _LIB_NAME_RE.match(p.stem)) if folder.is_dir() else []
    libs = []
    for name in names:
        path = library_file(name)
        try:
            entries = read_library(name)
            libs.append({"library": name, "file": str(path), "exists": path.exists(), "entries": entries,
                         "error": None})
        except LibraryError as e:
            libs.append({"library": name, "file": str(path), "exists": True, "entries": [], "error": str(e)})
    return {"folder": str(folder), "libraries": libs}


def remove_entries(library: str | None, names: list[str]) -> dict:
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
        raise LibraryError("names must be a non-empty list of entry names, e.g. ['f'].")
    lib = check_library_name(library)
    with _locked(library_dir()):
        entries = read_library(lib)
        want = {n.strip().lower() for n in names}
        keep = [e for e in entries if e["name"].lower() not in want]
        removed = [e["name"] for e in entries if e["name"].lower() in want]
        missing = [n for n in names if n.strip().lower() not in {e["name"].lower() for e in entries}]
        path = write_library(lib, keep) if removed else library_file(lib)
    return {"library": lib, "file": str(path), "removed": removed, "missing": missing}


# ------------------------------------------------------------ checks on the CAS

@dataclass
class Checked:
    parsed: Parsed
    ok: bool = False
    error: str | None = None
    cas_type: str | None = None
    doc_type: str | None = None    # the name's type in the document, None if not stored
    doc_locked: bool = False
    same: bool | None = None       # the document's definition gives the same result (None: not compared)


def _doc_vars(bridge, timeout_s: float) -> dict[str, dict]:
    return {v["name"].lower(): v for v in bridge.list_vars(values=False, timeout_s=timeout_s)}


def _check_free(doc: dict[str, dict], n_temps: int, n_syms: int) -> None:
    used = [f"{TEMP_PREFIX}{i}" for i in range(1, n_temps + 1)] + [f"{SYM_PREFIX}{i}" for i in range(1, n_syms + 1)]
    taken = [u for u in used if u in doc]
    if taken:
        raise LibraryError(f"The document already stores {', '.join(taken)}, which the library uses as scratch "
                           f"names; nothing was changed. Rename them, or ask the user whether they can go.")


def check_on_cas(bridge, items: list[Parsed], doc: dict[str, dict], timeout_s: float) -> tuple[list[Checked], list[str]]:
    """Define each item under a temporary name, read its type, compare it with the document's
    definition of the same name, then delete the temporary names. One batch, plus the cleanup.
    Returns the checks and warnings (a failed cleanup)."""
    n_syms = max((len(p.params) for p in items), default=0)
    _check_free(doc, len(items), n_syms)
    out = [Checked(p) for p in items]
    temps = [f"{TEMP_PREFIX}{i}" for i in range(1, len(items) + 1)]
    exprs = [_temp_command(p, t) for p, t in zip(items, temps, strict=True)]
    exprs += [f"getType({t})" for t in temps]
    compare: list[int] = []
    for k, c in enumerate(out):
        row = doc.get(c.parsed.name.lower())
        if row is not None:
            c.doc_type = row["type"].upper()
            c.doc_locked = bool(row.get("locked"))
            if c.doc_type in _KIND_TYPES[c.parsed.kind] and c.parsed.kind != "program":
                compare.append(k)
    spans: list[tuple[int, int, int]] = []  # (item, start, number of probes)
    for k in compare:
        p = out[k].parsed
        mine = _probes(row_name(doc, p.name), p.kind, len(p.params))
        theirs = _probes(temps[k], p.kind, len(p.params))
        spans.append((k, len(exprs), len(mine)))
        for a, b in zip(mine, theirs, strict=True):
            exprs += [a, b]
    warnings: list[str] = []
    try:
        res = bridge.eval_batch(exprs, timeout_s, raw=True)
    finally:
        warnings += _cleanup(bridge, temps, timeout_s)
    n = len(items)
    for k, c in enumerate(out):
        d, t = res[k], res[n + k]
        if not d["ok"]:
            c.error = readable(d["error"])
            continue
        typ = t["result"].strip().strip('"').upper() if t["ok"] else ""
        c.cas_type = typ or None
        if typ not in _KIND_TYPES[c.parsed.kind]:
            c.error = f"the CAS stored it as {typ or 'nothing'}, not as a {c.parsed.kind}"
            continue
        c.ok = True
    for k, start, count in spans:
        if out[k].ok:
            out[k].same = _same([(res[start + 2 * i], res[start + 2 * i + 1]) for i in range(count)])
    return out, warnings


def row_name(doc: dict[str, dict], name: str) -> str:
    row = doc.get(name.lower())
    return row["name"] if row else name


def _cleanup(bridge, temps: list[str], timeout_s: float) -> list[str]:
    try:
        res = bridge.eval_batch([f"DelVar {t}" for t in temps], timeout_s)
    except Exception as e:
        return [f"Couldn't delete the library's scratch names ({', '.join(temps)}) from the document: {e}"]
    bad = [t for t, r in zip(temps, res, strict=False) if not r["ok"]]
    return [f"Couldn't delete the library's scratch name(s) {', '.join(bad)} from the document."] if bad else []


# ------------------------------------------------------------ save

@dataclass
class Recovered:
    parsed: Parsed | None
    exact: bool
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def recover_from_document(bridge, names: list[str], doc: dict[str, dict], timeout_s: float) -> dict[str, Recovered]:
    """Rebuild definitions for names stored in the document (see the module docstring for what is lost).
    One batch: every arity of every function, and every variable's value."""
    others = len(doc) > len(names)
    out: dict[str, Recovered] = {}
    exprs: list[str] = []
    plan: list[tuple[str, str, int]] = []  # (name, what, arity)
    for name in names:
        row = doc[name.lower()]
        typ = row["type"].upper()
        real = row["name"]
        if typ == "FUNC":
            for n in range(MAX_ARITY + 1):
                exprs.append(_call(real, n))
                plan.append((real, "func", n))
        elif typ in VALUE_TYPES:
            exprs.append(real)
            plan.append((real, typ, 0))
        elif typ == "PRGM":
            out[real] = Recovered(None, False, f"{real} is a program: its text can't be read from the document. "
                                               f"Pass its Define command in definitions.")
        else:
            out[real] = Recovered(None, False, f"{real} is a {typ}, which the library can't save.")
    res = bridge.eval_batch(exprs, timeout_s, raw=True) if exprs else []
    by_name: dict[str, list[tuple[int, dict]]] = {}
    for (real, what, n), r in zip(plan, res, strict=True):
        by_name.setdefault(real, []).append((n, r))
        if what != "func":
            if not r["ok"]:
                out[real] = Recovered(None, False, f"{real}'s value couldn't be read: {readable(r['error'])}")
                continue
            try:
                p = parse_command(f"{real}:={r['result']}")
            except LibraryError as e:
                out[real] = Recovered(None, False, str(e))
                continue
            exact = what in ("NUM", "STR") or not others
            w = [] if exact else [f"{real} was saved as its value, {_short(p.body)}: if it was stored as an "
                                  f"expression of other variables, that link isn't kept."]
            out[real] = Recovered(p, exact, warnings=w)
    for real, tries in by_name.items():
        if real in out:
            continue
        hit = next(((n, r) for n, r in tries if r["ok"] or _error_code(r["error"]) not in (930, 940)), None)
        if hit is None:
            out[real] = Recovered(None, False, f"{real} takes more than {MAX_ARITY} arguments; pass its Define "
                                               f"command in definitions.")
            continue
        n, r = hit
        if not r["ok"]:
            out[real] = Recovered(None, False, f"{real} can't be rebuilt from the document: called with {n} symbolic "
                                               f"argument(s) it gives {readable(r['error'])}. Pass its Define command "
                                               f"in definitions.")
            continue
        body = readable(r["result"])
        if not body:
            out[real] = Recovered(None, False, f"{real} returned nothing for symbolic arguments; pass its Define "
                                               f"command in definitions.")
            continue
        params, body = rebuild_params(body, n)
        try:
            p = parse_command(build_command(real, "function", params, body))
        except LibraryError as e:
            out[real] = Recovered(None, False, f"{real} can't be rebuilt from the document ({e}); pass its Define "
                                               f"command in definitions.")
            continue
        w = [f"{real} was rebuilt from what it returns for symbolic arguments: {p.command}. That is the CAS's "
             f"simplified result, not the text it was defined with (a Func's Local/If structure isn't kept"
             f"{', and values of stored variables it used are written in' if others else ''}). For an exact "
             f"copy, pass its Define command in definitions."]
        if NS_C in r["result"] or NS_N in r["result"]:
            w.append(f"{real}'s result has TI-Nspire's arbitrary constants (c1, n1, ...), which load back as plain names.")
        out[real] = Recovered(p, False, warnings=w)
    order = [doc[n.lower()]["name"] for n in names]
    return {real: out[real] for real in order}


def _short(v: str, n: int = 80) -> str:
    return v if len(v) <= n else v[:n] + "…"


def _now() -> str:
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _entry(p: Parsed, exact: bool, source_document: str | None, note: str | None) -> dict:
    return {"name": p.name, "kind": p.kind, "params": list(p.params), "command": p.command, "exact": exact,
            "saved_at": _now(), "source_document": source_document, "note": note}


def _check_text(value: str | None, what: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 500:
        raise LibraryError(f"{what} must be text of at most 500 characters.")
    return value.strip() or None


def prepare_save(names: list[str] | None, definitions: list[str] | None) -> tuple[list[str], list[Parsed]]:
    """Offline checks for save: returns (names to read from the document, parsed definitions)."""
    names = list(names or [])
    definitions = list(definitions or [])
    if not names and not definitions:
        raise LibraryError("Give names (stored in the document) or definitions (Define commands) to save.")
    if len(names) + len(definitions) > MAX_ENTRIES:
        raise LibraryError(f"At most {MAX_ENTRIES} definitions at once.")
    bad = [n for n in names if not isinstance(n, str) or not _NAME_RE.match(n.strip())]
    if bad:
        raise LibraryError(f"Not names: {bad!r}. Give bare names, e.g. 'f', not 'f(x)'.")
    names = list(dict.fromkeys(n.strip() for n in names))
    parsed = [parse_command(d) for d in definitions]
    seen: dict[str, str] = {}
    for x in [*names, *(p.name for p in parsed)]:
        if x.lower() in seen:
            raise LibraryError(f"{x} is given twice; save each name once.")
        seen[x.lower()] = x
    return names, parsed


def save(bridge, library: str | None, names: list[str] | None, definitions: list[str] | None,
         overwrite: bool = False, source_document: str | None = None, note: str | None = None,
         timeout_s: float = 5.0) -> dict:
    """Inside bridge.session(): check everything on the CAS, then write the library file.
    Nothing is written if any definition fails or (without overwrite) replaces a different saved one."""
    lib = check_library_name(library)
    source_document = _check_text(source_document, "source_document")
    note = _check_text(note, "note")
    want, parsed = prepare_save(names, definitions)
    read_library(lib)  # an unreadable file fails before the bridge is used
    doc = _doc_vars(bridge, timeout_s)
    missing = [n for n in want if n.lower() not in doc]
    if missing:
        raise LibraryError(f"Not stored in the document: {', '.join(missing)} (see nspire_vars). Nothing was saved.")
    warnings: list[str] = []
    errors: list[dict] = []
    rec = recover_from_document(bridge, want, doc, timeout_s) if want else {}
    items: list[tuple[Parsed, bool, str]] = []  # (parsed, exact, origin)
    for real, r in rec.items():
        if r.parsed is None:
            errors.append({"name": real, "error": r.error or "couldn't be read"})
        else:
            items.append((r.parsed, r.exact, "document"))
            warnings += r.warnings
    items += [(p, True, "definition") for p in parsed]
    checks, w = check_on_cas(bridge, [p for p, _, _ in items], doc, timeout_s) if items else ([], [])
    warnings += w
    for (p, _, origin), c in zip(items, checks, strict=True):
        if not c.ok:
            errors.append({"name": p.name, "error": f"{p.command}: {c.error}"})
        elif origin == "document" and c.same is False:
            errors.append({"name": p.name, "error": f"the rebuilt {p.command} doesn't give the same result as the "
                                                    f"document's {p.name}; pass its Define command in definitions."})
        elif origin == "definition" and c.doc_type is not None and (
                c.same is False or c.doc_type not in _KIND_TYPES[p.kind]):
            warnings.append(f"The document's {p.name} ({c.doc_type}) isn't the same as the definition saved; "
                            f"the library keeps the definition as given.")
    result = {"library": lib, "file": str(library_file(lib)), "saved": [], "replaced": [], "unchanged": [],
              "conflicts": [], "errors": errors, "warnings": warnings}
    if errors:
        result["warnings"] = warnings + ["Nothing was saved: fix the errors first."]
        return result
    with _locked(library_dir()):
        entries = read_library(lib)
        index = {e["name"].lower(): i for i, e in enumerate(entries)}
        new: list[tuple[Parsed, bool]] = [(p, exact) for p, exact, _ in items]
        for p, _exact in new:
            i = index.get(p.name.lower())
            if i is None:
                continue
            old = entries[i]
            if old["command"] == p.command:
                result["unchanged"].append(p.name)
            elif not overwrite:
                result["conflicts"].append({"name": p.name, "library_command": old["command"], "command": p.command,
                                            "reason": "the library already has a different definition"})
        if result["conflicts"]:
            result["warnings"].append("Nothing was saved: the library already has different definitions of "
                                      + ", ".join(c["name"] for c in result["conflicts"])
                                      + ". Pass overwrite=True to replace them, or save under another library.")
            result["unchanged"] = []
            return result
        if len(entries) + sum(p.name.lower() not in index for p, _ in new) > MAX_ENTRIES:
            raise LibraryError(f"A library holds at most {MAX_ENTRIES} entries.")
        for p, exact in new:
            i = index.get(p.name.lower())
            if p.name in result["unchanged"]:
                continue
            e = _entry(p, exact, source_document, note)
            if i is None:
                index[p.name.lower()] = len(entries)
                entries.append(e)
                result["saved"].append(p.name)
            else:
                entries[i] = e
                result["replaced"].append(p.name)
        if result["saved"] or result["replaced"]:
            result["file"] = str(write_library(lib, entries))
    return result


# ------------------------------------------------------------ load

def load(bridge, library: str | None, names: list[str] | None = None, overwrite: bool = False,
         timeout_s: float = 5.0) -> dict:
    """Inside bridge.session(): check every entry on the CAS, compare with the document, then store
    them all in one batch. Nothing is stored if an entry fails, or (without overwrite) if the
    document holds a different definition of one of the names."""
    lib = check_library_name(library)
    entries = read_library(lib)
    if not entries:
        raise LibraryError(f"Library {lib!r} is empty or doesn't exist ({library_file(lib)}); see nspire_lib_list.")
    if names:
        if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
            raise LibraryError("names must be a list of entry names.")
        want = {n.strip().lower() for n in names}
        missing = [n for n in names if n.strip().lower() not in {e["name"].lower() for e in entries}]
        if missing:
            raise LibraryError(f"Not in library {lib!r}: {', '.join(missing)}. Nothing was loaded.")
        entries = [e for e in entries if e["name"].lower() in want]
    parsed = []
    for e in entries:
        p = parse_command(e["command"])
        if p.name.lower() != e["name"].lower() or p.kind != e["kind"]:
            raise LibraryError(f"Library {lib!r}: the entry {e['name']} doesn't match its command {e['command']!r}.")
        parsed.append(p)
    result: dict = {"library": lib, "file": str(library_file(lib)), "loaded": [], "replaced": [], "unchanged": [],
                    "conflicts": [], "errors": [], "warnings": []}
    doc = _doc_vars(bridge, timeout_s)
    checks, w = check_on_cas(bridge, parsed, doc, timeout_s)
    result["warnings"] += w
    for c in checks:
        p = c.parsed
        if not c.ok:
            result["errors"].append({"name": p.name, "error": f"{p.command}: {c.error}"})
        elif c.doc_type is None:
            continue
        elif c.same:
            result["unchanged"].append(p.name)
        elif c.doc_locked:
            result["conflicts"].append({"name": p.name, "document_type": c.doc_type,
                                        "reason": "locked in the document (unlock it in TI-Nspire first)"})
        elif not overwrite:
            reason = ("a program, which can't be compared" if c.doc_type == "PRGM" and p.kind == "program"
                      else f"the document's {p.name} ({c.doc_type}) is different")
            result["conflicts"].append({"name": p.name, "document_type": c.doc_type, "reason": reason})
    if result["errors"]:
        result["warnings"].append("Nothing was loaded: some definitions don't work in this document.")
        result["unchanged"] = []
        return result
    if result["conflicts"]:
        locked = any("locked" in c["reason"] for c in result["conflicts"])
        result["warnings"].append(
            "Nothing was loaded: the document already holds "
            + ", ".join(c["name"] for c in result["conflicts"])
            + (". Locked names can't be replaced." if locked and overwrite else
               ". Pass overwrite=True to replace them (ask the user first), or load only other names."))
        result["unchanged"] = []
        return result
    todo = [c for c in checks if c.parsed.name not in result["unchanged"]]
    if not todo:
        return result
    res = bridge.eval_batch([to_cas_chars(c.parsed.command) for c in todo], timeout_s)
    for c, r in zip(todo, res, strict=True):
        name = c.parsed.name
        if not r["ok"]:
            result["errors"].append({"name": name, "error": readable(r["error"])})
        elif c.doc_type is None:
            result["loaded"].append(name)
        else:
            result["replaced"].append(name)
    stored = [e for e in entries if not e.get("exact", True) and e["name"] in result["loaded"] + result["replaced"]]
    if stored:
        result["warnings"].append("Loaded from a rebuilt (not exact) copy: " + ", ".join(e["name"] for e in stored) + ".")
    return result


def summary(result: dict, verb: str) -> str:
    parts = []
    for key, label in (("saved", "saved"), ("loaded", "loaded"), ("replaced", "replaced"),
                       ("unchanged", "already the same")):
        if result.get(key):
            parts.append(f"{label}: {', '.join(result[key])}")
    if result.get("conflicts"):
        parts.append("conflicts: " + "; ".join(f"{c['name']} ({c['reason']})" for c in result["conflicts"]))
    if result.get("errors"):
        parts.append("errors: " + "; ".join(f"{e['name']}: {e['error']}" for e in result["errors"]))
    head = f"Library {result['library']} ({result['file']})"
    return f"{head}, {verb}: " + (" | ".join(parts) if parts else "nothing to do")
