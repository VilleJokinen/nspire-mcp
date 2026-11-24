# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = [
#     "mcp>=1.30,<2",
#     "pywin32>=306; sys_platform == 'win32'",
#     "pyobjc-framework-Cocoa>=10.3; sys_platform == 'darwin'",
#     "sympy>=1.13",
#     "antlr4-python3-runtime==4.11.*",
#     "matplotlib>=3.8",
# ]
# ///
"""MCP server: evaluate expressions in the TI-Nspire CX (CAS) desktop app.

Talks to bridge.lua (running on a page of nspire_bridge.tns) through the
clipboard (Windows or macOS). Run with:  uv run --script server.py
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib
import logging
import os
import re
import sys
import threading
from collections.abc import Collection
from typing import Annotated, Any, Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import Context, FastMCP  # noqa: E402
from mcp.types import CallToolResult, ContentBlock, ImageContent, TextContent, ToolAnnotations  # noqa: E402
from pydantic import Field  # noqa: E402
from typing_extensions import TypedDict  # noqa: E402  (pydantic needs this one before Python 3.12)

from nspire_bridge import (  # noqa: E402
    VALUE_MAX_CHARS,
    VALUE_TYPES,
    Bridge,
    BridgeTimeout,
    mode_override_refusal,
    mode_setting,
    parse_var_info,
    readable,
)

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="nspire: %(message)s")
log = logging.getLogger("nspire")

INSTRUCTIONS = """\
Tools for the TI-Nspire CX (CAS) desktop software on the user's computer, used as an exact CAS.
- TI-Nspire must be open with nspire_bridge.tns on its bridge page ("NSPIRE BRIDGE tick N"). If a call
  times out, ask the user to check that; nspire_status pings the bridge.
- Requests travel through the clipboard: each call briefly replaces it and puts it back.
- Variables and functions stored in the open document (a:=5, Define f(x)=...) persist and are
  substituted into every answer. If an answer looks oddly specific, check nspire_vars.
- For scratch work (checking a step, trying a definition, testing values) pass isolated=True to
  nspire_eval, nspire_eval_batch or nspire_latex: whatever the call defines or deletes (a:=5,
  Define f(x)=..., DelVar a) is put back afterwards, so the user's document isn't left with it.
  Leave it off when the user wants the definition kept.
- angle ("radian"/"degree"/"gradian") and exact_mode ("auto"/"exact"/"approx") on nspire_eval and
  nspire_latex evaluate in that mode for that one call; the document's settings aren't changed.
  They can't be used with input that stores or defines (a:=5, Define, f(x)=...).
- nspire_clear deletes stored variables and can't be undone: ask the user first.
- nspire_notes (Windows only) types into the TI-Nspire window: use it only when the user wants a Notes page, and
  tell them not to type or click while it runs.
- nspire_plot graphs functions as an image; the CAS computes every point. Describe the graph from
  its summary.
- Units are TI-Nspire's: 5_m, 9.81_m/_s^2, 2_mA*4.7_kΩ; convert with @> (5_km@>_m). Their names
  ignore case, so some aren't what they look like: kelvin is _°K (_K is Boltzmann's constant),
  coulomb _coul (_C is c), henry _henry (_H is h); TI-Nspire has no _MW, _kN, _mT, _MJ. Constants:
  _g, _c, _h, _k, _q, _Na, _Rc, _Gc, _ε0, _μ0, _Me, _Mp, _Mn. In LaTeX, units go right after a number
  (5\\,\\mathrm{m}) and \\to \\mathrm{km/h} converts; constants=["g", ...] reads symbols as constants.
- Report results as the CAS gives them; explain the steps yourself."""

bridge = Bridge()
mcp = FastMCP("nspire", instructions=INSTRUCTIONS)

# "auto" plus the keys of nspire_latex.ACTIONS, listed here so SymPy isn't
# imported at startup (tests/test_server.py checks that they match).
LatexAction = Literal[
    "auto", "eval", "simplify", "approx", "solve", "csolve", "nsolve", "zeros", "czeros", "factor",
    "cfactor", "expand", "propfrac", "comdenom", "texpand", "tcollect", "diff", "integrate", "taylor",
    "limit", "det", "inverse", "rref", "eigenvalues", "eigenvectors", "desolve", "define",
]


class NotesStep(TypedDict, total=False):
    """One line of a Notes page: text, then an optional math box (math or latex)."""
    text: Annotated[str, Field(description="One line of text, shown before the math box.")]
    math: Annotated[str, Field(description="TI-Nspire expression for a live math box, e.g. factor(x^2-5*x+6).")]
    latex: Annotated[str, Field(description="LaTeX instead of math, translated like nspire_latex.")]
    action: Annotated[LatexAction, Field(description="With latex: what to do with it, as in nspire_latex.")]
    var: Annotated[str, Field(description="With latex: the variable(s), as in nspire_latex.")]
    to: Annotated[str, Field(description="Unit to show the result in, e.g. km/h, m/s^2, kΩ or _km/_hr "
                                         "(TI-Nspire's ► convert, typed @>).")]
    approx: Annotated[bool, Field(description="Show the result as a decimal: the math box becomes approx(...).")]
    constants: Annotated[list[str] | bool, Field(
        description='With latex: symbols to read as physical constants, e.g. ["g", "c", "k_B"] '
                    "(true: all of g, c, h, k_B, N_A, R, G, \\varepsilon_0, \\mu_0, m_e, m_p, m_n, q_e).")]


AngleMode = Literal["radian", "degree", "gradian"]
ExactMode = Literal["auto", "exact", "approx"]
# setMode numbers, as in nspire_bridge.MODES: mode 2 is the angle, mode 5 auto/approximate/exact.
_ANGLE_SETTINGS = {"radian": 1, "degree": 2, "gradian": 3}
_EXACT_SETTINGS = {"auto": 1, "approx": 2, "exact": 3}


def _mode_settings(angle: str | None, exact_mode: str | None) -> dict[int, int]:
    out = {}
    if angle is not None:
        out[2] = _ANGLE_SETTINGS[angle]
    if exact_mode is not None:
        out[5] = _EXACT_SETTINGS[exact_mode]
    return out


# ---------------------------------------------------------------- result types
#
# Return types are TypedDicts so FastMCP publishes an outputSchema. It turns a
# top-level TypedDict into a model whose missing optional keys come out as null in
# structuredContent (and checks that against the schema), so those keys allow None.
# TypedDicts inside a list are validated as they are: a missing key stays missing.
# Keys a TypedDict doesn't declare are dropped from structuredContent: declare them all.
# Optional keys go in a total=False subclass: NotRequired[...] isn't seen in the
# string annotations that `from __future__ import annotations` makes.

class _BridgeStatus(TypedDict):
    alive: bool


class BridgeStatus(_BridgeStatus, total=False):
    api_level: str | None
    cas: bool | None
    protocol: int | None
    modes: dict[str, str | int] | None
    error: str | None


class _EvalResult(TypedDict):
    result: str


class EvalResult(_EvalResult, total=False):
    warnings: list[str] | None


class _BatchItem(TypedDict):
    expr: str
    ok: bool


class BatchItem(_BatchItem, total=False):
    result: str
    error: str
    warnings: list[str]


class _VarEntry(TypedDict):
    name: str
    type: str
    library: str | None
    locked: bool | None


class VarEntry(_VarEntry, total=False):
    value: str


class ClearError(TypedDict):
    name: str
    error: str


class ClearResult(TypedDict):
    deleted: list[str]
    errors: list[ClearError]


class _LatexResult(TypedDict):
    action: str
    nspire: str
    result: str
    result_latex: str | None


class LatexResult(_LatexResult, total=False):
    approx: str | None
    approx_latex: str | None
    warnings: list[str] | None


# ---------------------------------------------------------------- isolated evaluation

_DELVAR_RE = re.compile(r"\bDelVar\s+([^:]*)", re.IGNORECASE)
_VAR_NAME_RE = re.compile(r"[^\W\d]\w*(?:\.\w+)?")


def changed_names(notes_mod, cmds: list[str]) -> list[list[str]]:
    """Per command, the names it stores (a:=5, Define f(x)=..., 5→a) or deletes (DelVar a).
    notes_mod is nspire_notes (scan_names). Over-detecting only costs a round trip."""
    out = []
    for c in cmds:
        names = list(notes_mod.scan_names(c).defines)
        for m in _DELVAR_RE.finditer(c):
            names += [n for n in _VAR_NAME_RE.findall(m.group(1)) if n not in names]
        out.append(names)
    return out


class _FailFast:
    """Bridge stand-in for the undo: after one request times out, the rest fail at once
    instead of each waiting out its own timeout."""

    def __init__(self, bridge):
        self._bridge = bridge
        self.dead: str | None = None

    def eval_one(self, expr: str, timeout_s: float, raw: bool = False) -> str:
        if self.dead:
            raise BridgeTimeout(self.dead)
        try:
            return self._bridge.eval_one(expr, timeout_s, raw)
        except BridgeTimeout as e:
            self.dead = str(e)
            raise


# nspire_notes words its undo warnings for checking Notes steps.
_UNDO_WORDING = (("Checking the steps redefined", "The isolated call redefined"),
                 ("after checking the steps", "after the isolated call"))


class Isolation:
    """What an isolated call changes in the document, saved before it runs."""

    def __init__(self, names: list[list[str]]):
        self.names = names                         # per command, as changed_names() gives them
        self.saved: dict[str, Any] = {}            # lower-case name -> nspire_notes._Saved
        self.warnings: dict[str, list[str]] = {}   # lower-case name -> what couldn't be put back

    def all_warnings(self) -> list[str]:
        return [w for ws in self.warnings.values() for w in ws]

    def first_use(self, key: str) -> int:
        """Index of the first command that changes the name key (lower case)."""
        return next(i for i, names in enumerate(self.names) if key in (n.lower() for n in names))


def _undo_all(bridge, notes_mod, iso: Isolation, timeout_s: float) -> None:
    ff = _FailFast(bridge)
    for key in reversed(list(iso.saved)):
        for w in notes_mod._undo(ff, {key: iso.saved[key]}, timeout_s):
            for old, new in _UNDO_WORDING:
                w = w.replace(old, new)
            iso.warnings.setdefault(key, []).append(w)


@contextlib.contextmanager
def isolated_names(bridge, notes_mod, cmds: list[str], timeout_s: float):
    """Save every name cmds store or delete, run the with block, then put them back.

    notes_mod is nspire_notes (scan_names, _bridge_vars, _type_of, _snapshot, _undo), or
    None for no isolation. Call inside bridge.session(), before changing modes (values are
    read and written back in the document's own mode). Raises ValueError before anything
    runs if a name holds something that can't be put back: a function, a program, or a
    value that can't be read. Yields an Isolation whose warnings are filled in after the
    block; if the block raised ValueError or BridgeTimeout, they're added to its message.
    """
    iso = Isolation(changed_names(notes_mod, cmds) if notes_mod is not None else [[] for _ in cmds])
    if not any(iso.names):
        yield iso
        return
    assert notes_mod is not None  # names are only found with it
    t = min(float(timeout_s), 5.0)
    existing = notes_mod._bridge_vars(bridge, t)  # None: couldn't list them, so ask getType name by name
    for name in dict.fromkeys(n for names in iso.names for n in names):
        key = name.lower()
        if key in iso.saved:
            continue
        typ = existing.get(key, "NONE") if existing is not None else notes_mod._type_of(bridge, name, t)
        others = existing is None or any(k != key for k in existing)
        kind = {"FUNC": "a function", "PRGM": "a program"}.get(typ.upper())
        sv = None if kind else notes_mod._snapshot(bridge, name, typ, t, others)
        if kind or (typ.upper() != "NONE" and sv is not None and sv.value is None):
            what = kind or f"a variable ({typ}) whose value couldn't be read"
            raise ValueError(f"isolated=True: {name} is {what} stored in the document, which can't be saved and "
                             f"put back, so this call would change it for good. Use another name, or ask the "
                             f"user before running it without isolated.")
        iso.saved[key] = sv
    undone = False
    try:
        yield iso
    except Exception as e:
        _undo_all(bridge, notes_mod, iso, t)
        undone = True
        if iso.warnings and type(e) in (ValueError, BridgeTimeout):
            raise type(e)(f"{e} ({' '.join(iso.all_warnings())})") from None
        raise
    finally:
        if not undone:
            _undo_all(bridge, notes_mod, iso, t)


def _eval_result(result: str, warnings: list[str]) -> CallToolResult:
    """nspire_eval's reply: the result as the only text block, as before isolated existed,
    plus a second block when there are warnings. structuredContent is EvalResult."""
    content: list[ContentBlock] = [TextContent(type="text", text=result)]
    structured: dict = {"result": result}
    if warnings:
        content.append(TextContent(type="text", text="Warnings: " + " ".join(warnings)))
        structured["warnings"] = warnings
    return CallToolResult(content=content, structuredContent=structured)



# Every tool talks only to the local TI-Nspire app.
def _hints(title: str, **kw) -> ToolAnnotations:
    return ToolAnnotations(title=title, openWorldHint=False, **kw)


@mcp.tool(annotations=_hints("TI-Nspire bridge status", readOnlyHint=True, idempotentHint=True))
async def nspire_status() -> BridgeStatus:
    """Ping the TI-Nspire bridge.

    Returns alive, the Lua API level, whether the CAS is available, the bridge script's
    protocol (2 = batch support; 1 = an older nspire_bridge.tns), and the document's mode
    settings: modes = {angle, auto_or_approx, real_or_complex, display_digits,
    exponential_format, vector_format, base, unit_system}, or null if they couldn't be read.
    Angle mode Degree changes trig answers; Approximate mode gives decimals instead of
    exact results. If the bridge doesn't answer: {alive: false, error}.
    """
    def work():
        try:
            st = bridge.ping()
        except BridgeTimeout as e:
            return {"alive": False, "error": str(e)}
        st["modes"] = bridge.modes()
        return st
    return await asyncio.to_thread(bridge.session, work)


_ISOLATED_DOC = Field(description="Scratch work: put back afterwards whatever the call stores or deletes "
                                  "(a:=5, Define f(x)=..., DelVar a), so the user's document is left as it was.")
_ANGLE_DOC = Field(description="Angle mode for this call only; the document's setting isn't changed. Not for "
                               "expressions that store or define. Leave out to use the document's.")
_CONSTANTS_DOC = Field(description='Symbols to read as physical constants (TI-Nspire\'s _g, _c, ...), e.g. '
                                    '["g", "c", "k_B"]; true for all of g, c, h, k_B, N_A, R, G, \\varepsilon_0, '
                                    '\\mu_0, m_e, m_p, m_n, q_e. Leave out to keep every symbol a variable.')
_EXACT_DOC = Field(description="Auto/Exact/Approximate mode for this call only; the document's setting isn't "
                               "changed. Not for expressions that store or define. Leave out to use the document's.")


class _InModes:
    """Stands in for the bridge in nspire_latex.run_plan: every evaluation runs with the
    call's mode settings (Bridge.eval_in_modes), and its warnings are collected."""

    def __init__(self, bridge: Bridge, settings: dict[int, int]):
        self.bridge, self.settings = bridge, settings
        self.warnings: list[str] = []

    def eval_one(self, expr: str, timeout_s: float, raw: bool = False) -> str:
        result, warnings = self.bridge.eval_in_modes(expr, self.settings, timeout_s, raw)
        self.warnings += [w for w in warnings if w not in self.warnings]
        return result


def _check_mode_override(settings: dict[int, int], command: str) -> None:
    """Refuse angle/exact_mode for a command that can't run inside a function, before any bridge use."""
    refusal = mode_override_refusal(command) if settings else None
    if refusal:
        raise ValueError(refusal)


async def _isolation_module(isolated: bool):
    """nspire_notes (scan_names, _snapshot, _undo) when isolated, imported outside the session."""
    if not isolated:
        return None
    return await asyncio.to_thread(importlib.import_module, "nspire_notes")


@mcp.tool(annotations=_hints("Evaluate a TI-Nspire expression"))
async def nspire_eval(expr: str, timeout_s: float = 5,
                      isolated: Annotated[bool, _ISOLATED_DOC] = False,
                      angle: Annotated[AngleMode | None, _ANGLE_DOC] = None,
                      exact_mode: Annotated[ExactMode | None, _EXACT_DOC] = None,
                      ) -> Annotated[CallToolResult, EvalResult]:
    """Evaluate one expression in TI-Nspire (CAS if available) and return the result as text.

    Use TI-Nspire syntax, e.g. solve(x^2=4,x), factor(x^2-5x+6), ∫(x^2,x), approx(π).
    Results come back as the CAS writes them; negatives use the Unicode minus "−".
    The constants ℯ and 𝑖 and the exponent mark ᴇ appear as those characters and can be
    sent back as input. Arbitrary constants show as c1, c2 (deSolve) and n1, n2 (integers).
    Definitions (Define f(x)=..., a:=5) persist in the open document's problem,
    so later calls can use them. Undefine with DelVar a.

    isolated=True is for scratch work (checking a value, trying a definition): whatever the
    expression stores or deletes is put back afterwards, so the user's document isn't left
    with it. It refuses to run, changing nothing, if that would replace a stored function or
    program (those can't be put back). angle / exact_mode evaluate in that mode for this
    call only, without changing the document's settings (TI-Nspire runs the expression
    inside a temporary function, so it can't store or define anything: a:=5 or Define with
    them is refused). The result text is the first content block, as without these options;
    anything that couldn't be put back comes as a second block starting "Warnings:" (and in
    structured output as {result, warnings}): tell the user.
    """
    settings = _mode_settings(angle, exact_mode)
    _check_mode_override(settings, expr)
    nm = await _isolation_module(isolated)

    def work():
        with isolated_names(bridge, nm, [expr], timeout_s) as iso:
            result, mode_warnings = bridge.eval_in_modes(expr, settings, timeout_s)
        return result, mode_warnings + iso.all_warnings()
    log.info("eval %r%s", expr, " (isolated)" if isolated else "")
    result, warnings = await asyncio.to_thread(bridge.session, work)
    return _eval_result(result, warnings)


@mcp.tool(annotations=_hints("Evaluate several TI-Nspire expressions"))
async def nspire_eval_batch(exprs: list[str], timeout_s: float = 5,
                            isolated: Annotated[bool, _ISOLATED_DOC] = False) -> list[BatchItem]:
    """Evaluate expressions in order. Returns one {expr, ok, result | error} per input.

    Definitions made by earlier expressions are visible to later ones and persist
    in the open document's problem. timeout_s is per expression. If the bridge stops
    answering, the remaining expressions are skipped. Much faster than separate
    nspire_eval calls: with a current nspire_bridge.tns the whole list is one request.

    isolated=True is for scratch work, e.g. checking a student's steps with helper
    definitions: later expressions still see earlier definitions, but every name the list
    stores or deletes is put back afterwards, so the user's document isn't left with them.
    It refuses to run, changing nothing, if that would replace a stored function or program.
    If a name couldn't be put back, the item that first changed it gets warnings: [str].
    """
    nm = await _isolation_module(isolated)

    def work():
        with isolated_names(bridge, nm, exprs, timeout_s) as iso:
            res = bridge.eval_batch(exprs, timeout_s)
        for key, ws in iso.warnings.items():
            res[iso.first_use(key)].setdefault("warnings", []).extend(ws)
        return res
    log.info("batch of %d%s", len(exprs), " (isolated)" if isolated else "")
    return await asyncio.to_thread(bridge.session, work)


@mcp.tool(annotations=_hints("List stored TI-Nspire variables", readOnlyHint=True, idempotentHint=True))
async def nspire_vars(values: bool = True, timeout_s: float = 5) -> list[VarEntry]:
    """List the variables and functions stored in the open document's problem (getVarInfo()).

    Returns one {name, type, library, locked, value?} per variable. type is TI-Nspire's,
    e.g. NUM, EXPR, LIST, MAT, STR, FUNC, PRGM. With values=True, value is the stored value
    of NUM/EXPR/LIST/MAT/STR variables, cut to 300 characters. Stored variables are
    substituted into every result (x:=3 turns solve(x^2=4,x) into false), so check here
    when an answer looks oddly specific. Empty list: nothing stored.
    """
    def work():
        return bridge.list_vars(values, timeout_s)
    log.info("vars")
    return await asyncio.to_thread(bridge.session, work)


@mcp.tool(annotations=_hints("Delete stored TI-Nspire variables", destructiveHint=True, idempotentHint=True))
async def nspire_clear(names: list[str] | None = None, timeout_s: float = 5) -> ClearResult:
    """DESTRUCTIVE: delete stored variables and functions from the open document's problem.

    Runs DelVar on each name, e.g. names=["a", "f"] (bare names: "f", not "f(x)").
    With no names, EVERY variable and function stored in the problem is deleted,
    including ones the user defined by hand. This can't be undone (only by closing the
    document without saving, which loses everything since the last save). Prefer
    deleting specific names; ask the user before clearing everything or deleting
    variables you didn't create. Returns {deleted: [names], errors: [{name, error}]};
    locked variables can't be deleted and show up in errors.
    """
    def work():
        return bridge.clear_vars(names, timeout_s)
    log.info("clear %s", names if names else "all")
    return await asyncio.to_thread(bridge.session, work)


# ---------------------------------------------------------------- document checks for nspire_latex

_TRIG_RE = re.compile(r"(?<![\w.])(?:arc)?(?:sin|cos|tan|cot|sec|csc)(?:⁻¹)?\s*\(")


def _import_optional(name: str):
    try:
        return importlib.import_module(name)
    except Exception as e:  # fewer warnings rather than a failed tool
        log.info("couldn't import %s: %s", name, e)
        return None


def _short(v: str) -> str:
    return v if len(v) <= VALUE_MAX_CHARS else v[:VALUE_MAX_CHARS] + "…"


def doc_warnings(bridge: Bridge, command: str, timeout_s: float, notes_mod=None,
                 isolated: bool = False, angle_override: bool = False,
                 ignore: Collection[str] = ()) -> list[str]:
    """Warnings about the open document for command: stored variables or functions it
    uses or replaces, and an angle mode other than radian when it uses trig functions.
    isolated: what command replaces is put back afterwards. angle_override: the call sets
    its own angle mode, so the document's doesn't matter.

    Call inside bridge.session(), before running command. One round trip, plus one to read
    values when a stored variable is used. Never fails the tool: anything unexpected just
    gives fewer warnings. Raises BridgeTimeout only if the bridge doesn't answer at all, in
    which case command would time out too. notes_mod is nspire_notes (for scan_names).
    ignore: names (lower case) that command binds itself, such as seq's variable.
    """
    t = min(float(timeout_s), 5.0)
    res = bridge.eval_batch(["getVarInfo()", "getMode(2)"], t)
    if bridge.last_timeout:
        raise BridgeTimeout(bridge.last_timeout)
    warnings: list[str] = []
    try:
        if notes_mod is not None and res[0]["ok"]:
            names = notes_mod.scan_names(command)
            stored = {r[0].lower(): (r[0], r[1].upper() if len(r) > 1 else "")
                      for r in parse_var_info(res[0]["result"]) if r}
            used = [stored[n] for n in sorted(names.uses) if n in stored and n not in ignore]
            replaced = [stored[n.lower()] for n in names.defines
                        if n.lower() in stored and n.lower() not in names.uses]
            with_value = [name for name, typ in used if typ in VALUE_TYPES]
            values: dict[str, str] = {}
            if with_value:
                for name, r in zip(with_value, bridge.eval_batch(with_value, t), strict=True):
                    if r["ok"]:
                        values[name] = _short(r["result"])
            for name, typ in used:
                if name in values:
                    warnings.append(f"{name} is stored in the document ({name} = {values[name]}), so its value "
                                    f"was substituted. If that isn't intended, ask the user before deleting it "
                                    f"(nspire_clear), or use another variable name.")
                elif typ in ("FUNC", "PRGM"):
                    warnings.append(f"{name} is defined in the document ({typ}); its stored definition was used.")
                else:
                    warnings.append(f"{name} is stored in the document ({typ or 'unknown type'}) and was "
                                    f"substituted; see nspire_vars.")
            for name, typ in replaced:
                warnings.append(f"{name} was already stored in the document ({typ or 'unknown type'}); "
                                + ("this call replaced it until it was put back (isolated)." if isolated
                                   else "this replaced it."))
    except Exception as e:
        log.info("stored-variable check failed: %s", e)
    try:
        if not angle_override and res[1]["ok"] and _TRIG_RE.search(readable(command)):
            angle = mode_setting(2, res[1]["result"])
            if angle is not None and angle != "Radian":
                warnings.append(f"The document's angle mode is {angle}, so trig functions here work in "
                                f"{angle} rather than radians. The user can change it in TI-Nspire's "
                                f"document settings.")
    except Exception as e:
        log.info("angle-mode check failed: %s", e)
    return warnings


@mcp.tool(annotations=_hints("Solve LaTeX on the TI-Nspire CAS"))
async def nspire_latex(latex: str, action: LatexAction = "auto", var: str | None = None,
                       order: int | None = None, point: str | None = None,
                       timeout_s: float = 10,
                       isolated: Annotated[bool, _ISOLATED_DOC] = False,
                       angle: Annotated[AngleMode | None, _ANGLE_DOC] = None,
                       exact_mode: Annotated[ExactMode | None, _EXACT_DOC] = None,
                       constants: Annotated[list[str] | bool, _CONSTANTS_DOC] = False) -> LatexResult:
    """Solve or evaluate a LaTeX problem on the TI-Nspire CAS; returns the answer as text and LaTeX.

    Accepts $…$ / \\[…\\] delimiters, \\frac, \\sqrt[n]{}, \\int (with limits, \\,dx or \\mathrm{d}x),
    \\iint, \\sum, \\prod, \\lim (x\\to a, a^+), \\frac{d}{dx}, \\frac{d^n}{dx^n}, \\partial, derivatives
    y', y'', \\frac{dy}{dx}, \\dot y, evaluation bars (\\left. ... \\right|_{x=2}, |_0^1), matrices
    (pmatrix/bmatrix; vmatrix = determinant; \\det A), lists \\{1,2,3\\}, cases/align systems, several
    equations separated by \\\\ , commas or ;, conditions (\\text{for } x>0 -> |x>0), degrees
    (30^\\circ), |x|, n!, \\binom, \\arg, \\Re, \\Im, sgn, a \\bmod b, := (a:=5 stores, f(x):=... defines).
    Any other backslash command, or input that can't be read to the end, is an error, not a guess.
    Accents are separate names (\\bar x -> xbar, x^* -> xstar); T(t)=... with t on the right defines T.
    Subscripts of letters/digits are names (x_{n1} -> xn1; x_{n+1} is refused); \\vec{u}\\cdot\\vec{v} ->
    dotP(u,v) (also \\mathbf), \\times -> crossP (also row/column matrices), |\\vec u| and \\lVert u\\rVert ->
    norm(u); units after a number (5\\,\\mathrm{m}, 9.81\\,\\mathrm{m/s^2}, 4.7\\,\\mathrm{k}\\Omega,
    3\\,\\mu\\mathrm{A}, 25^\\circ\\mathrm{C}) -> 5*_m, 9.81*_m/_s^2, 4.7*_kΩ, 3*_µA, 25*_°C (SI units and the
    prefixed ones TI-Nspire has; others such as MW or kN are refused); a unit after a final \\to,
    \\rightarrow or \\text{ in } converts the result (90\\,\\mathrm{km/h} \\to \\mathrm{m/s} -> 90*_km/_hr@>_m/_s). \\Gamma(5), other Greek calls and unknown \\text{...} are refused.
    \\log without a base is base 10 and \\ln is natural; e and i are the constants unless they
    are summation/integration variables. "1,5" between digits is a decimal comma, also inside
    {} groups such as \\frac{1,5}{2}, but not inside (), [] or \\{\\} (f(1,5), \\gcd(12,18), lists).
    The expression reaches the CAS as written (0.1+0.2, \\sqrt{8}); the CAS does all arithmetic.

    action="auto" picks:
      f(x)=…            -> Define (f stays defined for later calls)
      equation(s) with y', y'' or dy/dx -> deSolve (initial conditions like y(0)=1 allowed)
      equation / inequality / system    -> solve for x (or the variables present)
      anything else     -> evaluate (integrals, limits, sums, derivatives, matrices, simplification)
    Or force one: eval, approx, solve, csolve, nsolve, zeros, czeros, factor, cfactor, expand,
    propfrac, comdenom, texpand, tcollect, diff, integrate, taylor (order, point), limit (point),
    det, inverse, rref, eigenvalues, eigenvectors, desolve, define.
    var: the variable(s) to solve/differentiate for, e.g. "t" or "x,y".

    Returns {action, nspire (the command run), result, result_latex, approx?, approx_latex?,
    warnings?}. result_latex is null when the result couldn't be converted; result is always the
    raw CAS text. warnings is there when variables stored in the open document (e.g. a:=5) were
    substituted, a stored function was used or replaced, or the angle mode isn't radian for a
    trig function: tell the user. If the LaTeX can't be parsed, the error says so; then
    translate it to TI-Nspire syntax yourself and use nspire_eval.

    isolated=True is for scratch work: whatever the command stores (f(x)=... defines f, a:=5)
    is put back afterwards, so the user's document isn't left with it; it refuses to run,
    changing nothing, if that would replace a stored function. Anything that couldn't be put
    back is in warnings. angle ("radian"/"degree"/"gradian") and exact_mode
    ("auto"/"exact"/"approx") evaluate in that mode for this call only, e.g. angle="degree"
    for \\sin(30), without changing the document's settings; not for input that stores or
    defines (f(x)=..., a:=5).
    """
    # Imported here, not at startup: sympy takes seconds to load and the MCP
    # client times out a server that's slow to answer initialize.
    lx = await asyncio.to_thread(importlib.import_module, "nspire_latex")
    log.info("latex %r action=%s", latex, action)
    try:
        # translate first: bad LaTeX fails without touching the clipboard
        p = await asyncio.to_thread(lx.plan, latex, action, var, order, point, constants)
    except lx.LatexError as e:
        raise ValueError(str(e)) from None
    settings = _mode_settings(angle, exact_mode)
    _check_mode_override(settings, p.command)
    # scan_names (and the undo, when isolated), imported outside the session
    notes_mod = await _isolation_module(True) if isolated else await asyncio.to_thread(_import_optional, "nspire_notes")

    def work():
        with isolated_names(bridge, notes_mod if isolated else None, [p.command], timeout_s) as iso:
            warnings = doc_warnings(bridge, p.command, timeout_s, notes_mod, isolated, angle is not None)
            in_modes = _InModes(bridge, settings) if settings else None
            run = lx.run_plan(in_modes or bridge, p, timeout_s)
        return run, warnings + (in_modes.warnings if in_modes else []) + iso.all_warnings()
    run, warnings = await asyncio.to_thread(bridge.session, work)
    if p.parsed.constants:
        warnings.insert(0, "Read as physical constants (TI-Nspire's values): " + ", ".join(p.parsed.constants) + ".")
    # LaTeX conversion after the session, with the user's clipboard already restored
    out = await asyncio.to_thread(lx.format_answer, run)
    if warnings:
        out["warnings"] = warnings
    return out


@mcp.tool(annotations=_hints("Plot functions with the TI-Nspire CAS", destructiveHint=False, idempotentHint=True))
async def nspire_plot(exprs: list[str] | None = None, latex: list[str] | None = None, var: str = "x",
                      x_min: float = -10, x_max: float = 10, y_min: float | None = None,
                      y_max: float | None = None, samples: int = 200, timeout_s: float = 20) -> CallToolResult:
    """Graph functions of one variable: returns a PNG image and a short text summary.

    exprs: functions in TI-Nspire syntax, e.g. ["sin(x)", "x^2-2", "f(x)"] ("y=..." is fine too).
    latex: or LaTeX, e.g. ["\\frac{1}{x}"], translated like nspire_latex. Up to 8 functions in
    all, one colour each. var: the variable (default x). x_min, x_max: the range. y_min, y_max:
    the y range to show (automatic if left out; spikes next to asymptotes are then cut off).
    samples: points per function, 10 to 1000.

    Every value comes from the CAS: one seq(approx(f),x,...) request per function; the image is
    only drawn from them. Points where a function is undefined or non-real are gaps, and lines
    aren't drawn across asymptotes. The summary gives each function's y range, gaps and jumps:
    describe the graph from it. Variables and functions stored in the open document are
    substituted (a*x^2 uses a stored a; the summary warns), and trig functions follow the
    document's angle mode (a warning if it isn't radian). Other names without a value are an
    error. Nothing is stored or changed: define functions with nspire_eval first.
    timeout_s: per request (one request holds every function's points, or a piece of them).
    """
    pl = await asyncio.to_thread(importlib.import_module, "nspire_plot")
    funcs = []
    for e in exprs or []:
        if not isinstance(e, str):
            raise ValueError("exprs must be strings")
        funcs.append(pl.Func(e, e))
    if latex:
        # SymPy only for LaTeX input
        lx = await asyncio.to_thread(importlib.import_module, "nspire_latex")
        for src in latex:
            try:
                p = await asyncio.to_thread(lx.plan, src, "eval")
            except lx.LatexError as err:
                raise ValueError(f"{src!r}: {err}") from None
            funcs.append(pl.Func(src, p.command))
    spec = pl.prepare(funcs, var, x_min, x_max, samples, y_min, y_max)  # bad arguments fail before the bridge
    notes_mod = await asyncio.to_thread(_import_optional, "nspire_notes")

    def work():
        both = ",".join(f.command for f in spec.funcs)
        warnings = doc_warnings(bridge, both, timeout_s, notes_mod, ignore={spec.var.lower()})
        return pl.sample(bridge, spec, timeout_s), warnings
    log.info("plot %d function(s), %d samples", len(spec.funcs), spec.grid.n)
    got, warnings = await asyncio.to_thread(bridge.session, work)
    text = pl.summary(spec, got, warnings)
    if not any(v is not None for ys in got.ys for v in ys):
        raise ValueError(text)
    # drawn after the session, with the user's clipboard already restored
    png = await asyncio.to_thread(pl.render_png, spec, got)
    return CallToolResult(content=[
        TextContent(type="text", text=text),
        ImageContent(type="image", data=base64.b64encode(png).decode("ascii"), mimeType="image/png"),
    ])


@mcp.tool(annotations=_hints("Write a worked solution on a TI-Nspire Notes page"))
async def nspire_notes(steps: list[NotesStep], title: str | None = None,
                       page_size: Literal["computer", "handheld"] = "computer",
                       graphs: list[str] | None = None, save_as: str | None = None, overwrite: bool = False,
                       timeout_s: float = 10, digits: int | None = None, ctx: Context | None = None) -> dict:
    """Write a worked solution onto a new TI-Nspire Notes page, with live math boxes.

    Use this when the user wants the solution shown *in TI-Nspire* (e.g. "write this up
    in a notes page"). It opens a new document in the TI-Nspire app, adds a Notes page and
    types it out: each step is one line of text followed by an optional math box, which
    TI-Nspire evaluates and shows as  input ▸ result.

    steps: list of objects, each one line:
      {"text": "Factor the left side:", "math": "factor(x^2-5*x+6)"}
      {"text": "so the solutions are"}                       (text only)
      {"text": "Area:", "latex": "\\int_0^1 x^2\\,dx"}         (LaTeX, translated like nspire_latex;
                                                              optional "action"/"var" as there)
      {"text": "Speed:", "math": "90_km/_hr", "to": "m/s"}     (physics: units, result in m/s)
      {"text": "Weight:", "latex": "5\\,\\mathrm{kg}\\cdot g", "constants": ["g"], "approx": true}
    Physics: write each quantity with its unit (inline numbers: 12_m/_s*3_s, or LaTeX
    12\\,\\mathrm{m/s}); "to" shows the result in that unit (km/h, kΩ, m/s^2, °C), "approx": true
    shows a decimal, "constants" (latex steps) reads g, c, k_B ... as TI-Nspire's constants
    (in math write _g, _c, _k). Kelvin is _°K and coulomb _coul in math: TI-Nspire's unit names ignore case.
    math uses TI-Nspire syntax (as in nspire_eval). Put Define f(x)=... in an early step to
    use f later. Keep text to one line per step; plan the steps from the actual math, and
    check results with nspire_eval first if unsure.

    Arguments are checked first, then every math step is evaluated through the bridge; if one
    errors, nothing is typed and the error names the step. The bridge document must be open (it
    stays on its page). Definitions the check makes there (a:=5, Define f(x)=...) are undone
    afterwards and changed values restored (an overwritten function can't be: that's a warning).
    The new document does NOT have the variables stored in the bridge document, so a step that
    uses one gets a warning (its page result may differ from the returned one): define it in an
    earlier step instead. It takes about a second per step and types into the TI-Nspire window
    (found by its process, never by title; if several are open the one in front is used), which
    comes to the front: the user shouldn't type or click meanwhile. If TI-Nspire loses focus, or
    the call is cancelled, it stops without typing anywhere else. Returns {document,
    window:{hwnd, title, process}, steps:[{text, math, result}], warnings:[str], note,
    graphs?:[str], save?:{path, saved, message}}. Progress is reported per step while it runs.

    page_size: "computer" (default: large page, fills the TI-Nspire window) or "handheld"
    (the calculator's small screen, e.g. to transfer the document to a handheld).
    graphs: functions of x to draw on a Graphs page after the Notes page, e.g. ["x^2", "2*x"]
    (TI-Nspire syntax) or LaTeX between dollar signs ("$\\frac{1}{x}$"); a leading y= or f(x)= is
    dropped. Each is checked on the bridge first, like math steps, and becomes f1, f2, ...
    save_as: full path of a .tns file (its folder must exist) to save the new document to, through
    TI-Nspire's Save dialog. An existing file is refused unless overwrite=true. Each step of the
    save is checked (dialog open, file name box focused and holding the path, file written); if
    one can't be, it stops, leaves the dialog for the user and says so in save.message and
    warnings. Without save_as the new document is unsaved.
    digits: significant figures (1-12) the new document shows, e.g. 3 for physics answers: its
    Display Digits is set to Float<digits> in File > Settings > Document Settings before typing.
    Values stay exact inside TI-Nspire; only their display is rounded. Without it, the default
    (Float6) is kept.
    """
    nb = await asyncio.to_thread(importlib.import_module, "nspire_notes")
    cancel = threading.Event()  # set when the client cancels the call: the worker stops typing
    loop = asyncio.get_running_loop()
    sent: list = []  # progress notifications scheduled from the worker thread

    def progress(done: int, total: int, message: str) -> None:
        if ctx is None or cancel.is_set():
            return
        fut = asyncio.run_coroutine_threadsafe(ctx.report_progress(done, total, message), loop)
        fut.add_done_callback(lambda f: f.cancelled() or f.exception() is None
                              or log.info("progress not sent: %s", f.exception()))
        sent.append(fut)

    def work():
        return nb.build_notes(bridge, steps, title, page_size, timeout_s, cancel=cancel,
                              graphs=graphs, save_as=save_as, overwrite=overwrite, progress=progress,
                              digits=digits)
    log.info("notes: %d steps, %d graphs%s", len(steps), len(graphs or ()), ", save" if save_as else "")
    try:
        out = await asyncio.to_thread(bridge.session, work)
    except asyncio.CancelledError:
        cancel.set()
        log.info("notes: cancelled")
        raise
    if sent:  # let the last notifications go out before the result
        await asyncio.wait([asyncio.wrap_future(f) for f in sent], timeout=2)
    return out


# ---------------------------------------------------------------- prompts

CHECK_SOLUTION = """\
Check this worked solution step by step on the TI-Nspire CAS (the nspire tools).

Problem:
{problem}

Worked solution:
{solution}

How to check it:
1. Split the solution into numbered steps, one per line or transformation, quoting each step.
2. For each step, write one or more TI-Nspire expressions that test whether it follows from
   the step before, for example:
   - equal expressions: simplify((before)-(after)) should give 0, or expand both sides;
   - equations: solve(before,x) and solve(after,x) should give the same solutions
     (a step that loses or gains solutions is an error even if the algebra looks right);
   - derivatives and integrals: derivative(f,x) or ∫(f,x), compared with the student's;
   - numbers: evaluate the student's arithmetic as written.
   Helper definitions (Define f(x)=..., a:=...) are fine: use names that look unlikely to be
   stored in the document.
3. Send all checks in ONE nspire_eval_batch call with isolated=True, so the helper definitions
   aren't left in the user's document. If it refuses because a name is already stored as a
   function, pick another name. Read every item: ok, result or error, and warnings.
4. Check the final answer on its own: solve or evaluate the original problem (nspire_eval or
   nspire_latex, isolated=True) and compare. If the problem is in degrees, pass angle="degree".
5. Report, for each step: correct, or wrong with what the CAS shows (results as the CAS gives
   them) and a short explanation. Name the first wrong step; an error that later steps carry
   forward counts once. Then say whether the final answer is right. Don't rewrite the whole
   solution unless asked.

If an answer looks oddly specific, a variable stored in the document may have been substituted:
check nspire_vars, and ask the user before deleting anything."""


@mcp.prompt(title="Check a worked solution")
def check_solution(problem: str, solution: str) -> str:
    """Check a student's worked solution step by step on the TI-Nspire CAS, with
    nspire_eval_batch(isolated=True) so the checks leave the user's document as it was."""
    return CHECK_SOLUTION.format(problem=problem.strip(), solution=solution.strip())


if __name__ == "__main__":
    mcp.run()  # stdio
