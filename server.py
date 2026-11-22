# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["mcp>=1.30,<2", "pywin32>=306", "sympy>=1.13", "antlr4-python3-runtime==4.11.*", "matplotlib>=3.8"]
# ///
"""MCP server: evaluate expressions in the TI-Nspire CX (CAS) desktop app.

Talks to bridge.lua (running on a page of nspire_bridge.tns) through the
Windows clipboard. Run with:  uv run --script server.py
"""
from __future__ import annotations

import asyncio
import base64
import importlib
import logging
import os
import re
import sys
import threading
from collections.abc import Collection
from typing import Annotated, Literal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP  # noqa: E402
from mcp.types import CallToolResult, ImageContent, TextContent, ToolAnnotations  # noqa: E402
from pydantic import Field  # noqa: E402
from typing_extensions import TypedDict  # noqa: E402  (pydantic needs this one before Python 3.12)

from nspire_bridge import (VALUE_MAX_CHARS, VALUE_TYPES, Bridge, BridgeTimeout, mode_setting,  # noqa: E402
                           parse_var_info, readable)


logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="nspire: %(message)s")
log = logging.getLogger("nspire")

INSTRUCTIONS = """\
Tools for the TI-Nspire CX (CAS) desktop software on the user's Windows PC, used as an exact CAS.
- TI-Nspire must be open with nspire_bridge.tns on its bridge page ("NSPIRE BRIDGE tick N"). If a call
  times out, ask the user to check that; nspire_status pings the bridge.
- Requests travel through the Windows clipboard: each call briefly replaces it and puts it back.
- Variables and functions stored in the open document (a:=5, Define f(x)=...) persist and are
  substituted into every answer. If an answer looks oddly specific, check nspire_vars.
- nspire_clear deletes stored variables and can't be undone: ask the user first.
- nspire_notes types into the TI-Nspire window: use it only when the user wants a Notes page, and
  tell them not to type or click while it runs.
- nspire_plot graphs functions as an image; the CAS computes every point. Describe the graph from
  its summary.
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


# Every tool talks only to the local TI-Nspire app.
def _hints(title: str, **kw) -> ToolAnnotations:
    return ToolAnnotations(title=title, openWorldHint=False, **kw)


@mcp.tool(annotations=_hints("TI-Nspire bridge status", readOnlyHint=True, idempotentHint=True))
async def nspire_status() -> dict:
    """Ping the TI-Nspire bridge.

    Returns alive, the Lua API level, whether the CAS is available, the bridge script's
    protocol (2 = batch support; 1 = an older nspire_bridge.tns), and the document's mode
    settings: modes = {angle, auto_or_approx, real_or_complex, display_digits,
    exponential_format, vector_format, base, unit_system}, or null if they couldn't be read.
    Angle mode Degree changes trig answers; Approximate mode gives decimals instead of
    exact results.
    """
    def work():
        try:
            st = bridge.ping()
        except BridgeTimeout as e:
            return {"alive": False, "error": str(e)}
        st["modes"] = bridge.modes()
        return st
    return await asyncio.to_thread(bridge.session, work)


@mcp.tool(annotations=_hints("Evaluate a TI-Nspire expression"))
async def nspire_eval(expr: str, timeout_s: float = 5) -> str:
    """Evaluate one expression in TI-Nspire (CAS if available) and return the result as text.

    Use TI-Nspire syntax, e.g. solve(x^2=4,x), factor(x^2-5x+6), ∫(x^2,x), approx(π).
    Results come back as the CAS writes them; negatives use the Unicode minus "−".
    The constants ℯ and 𝑖 and the exponent mark ᴇ appear as those characters and can be
    sent back as input. Arbitrary constants show as c1, c2 (deSolve) and n1, n2 (integers).
    Definitions (Define f(x)=..., a:=5) persist in the open document's problem,
    so later calls can use them. Undefine with DelVar a.
    """
    def work():
        return bridge.eval_one(expr, timeout_s)
    log.info("eval %r", expr)
    return await asyncio.to_thread(bridge.session, work)


@mcp.tool(annotations=_hints("Evaluate several TI-Nspire expressions"))
async def nspire_eval_batch(exprs: list[str], timeout_s: float = 5) -> list[dict]:
    """Evaluate expressions in order. Returns one {expr, ok, result | error} per input.

    Definitions made by earlier expressions are visible to later ones and persist
    in the open document's problem. timeout_s is per expression. If the bridge stops
    answering, the remaining expressions are skipped. Much faster than separate
    nspire_eval calls: with a current nspire_bridge.tns the whole list is one request.
    """
    def work():
        return bridge.eval_batch(exprs, timeout_s)
    log.info("batch of %d", len(exprs))
    return await asyncio.to_thread(bridge.session, work)


@mcp.tool(annotations=_hints("List stored TI-Nspire variables", readOnlyHint=True, idempotentHint=True))
async def nspire_vars(values: bool = True, timeout_s: float = 5) -> list[dict]:
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
async def nspire_clear(names: list[str] | None = None, timeout_s: float = 5) -> dict:
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
                 ignore: Collection[str] = ()) -> list[str]:
    """Warnings about the open document for command: stored variables or functions it
    uses or replaces, and an angle mode other than radian when it uses trig functions.

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
                for name, r in zip(with_value, bridge.eval_batch(with_value, t)):
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
                                f"this replaced it.")
    except Exception as e:
        log.info("stored-variable check failed: %s", e)
    try:
        if res[1]["ok"] and _TRIG_RE.search(readable(command)):
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
                       timeout_s: float = 10) -> dict:
    """Solve or evaluate a LaTeX problem on the TI-Nspire CAS; returns the answer as text and LaTeX.

    Accepts $…$ / \\[…\\] delimiters, \\frac, \\sqrt[n]{}, \\int (with limits, \\,dx or \\mathrm{d}x),
    \\iint, \\sum, \\prod, \\lim (x\\to a, a^+), \\frac{d}{dx}, \\frac{d^n}{dx^n}, \\partial, derivatives
    y', y'', \\frac{dy}{dx}, \\dot y, evaluation bars (\\left. ... \\right|_{x=2}, |_0^1), matrices
    (pmatrix/bmatrix; vmatrix = determinant; \\det A), lists \\{1,2,3\\}, cases/align systems, several
    equations separated by \\\\ , commas or ;, conditions (\\text{for } x>0 -> |x>0), degrees
    (30^\\circ), |x|, n!, \\binom, \\arg, \\Re, \\Im, sgn, a \\bmod b, := (a:=5 stores, f(x):=... defines).
    Any other backslash command, or input that can't be read to the end, is an error, not a guess.
    Accents are separate names (\\bar x -> xbar, x^* -> xstar); T(t)=... with t on the right defines T.
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
    """
    # Imported here, not at startup: sympy takes seconds to load and the MCP
    # client times out a server that's slow to answer initialize.
    lx = await asyncio.to_thread(importlib.import_module, "nspire_latex")
    log.info("latex %r action=%s", latex, action)
    try:
        # translate first: bad LaTeX fails without touching the clipboard
        p = await asyncio.to_thread(lx.plan, latex, action, var, order, point)
    except lx.LatexError as e:
        raise ValueError(str(e)) from None
    notes_mod = await asyncio.to_thread(_import_optional, "nspire_notes")  # scan_names, imported outside the session

    def work():
        warnings = doc_warnings(bridge, p.command, timeout_s, notes_mod)
        return lx.run_plan(bridge, p, timeout_s), warnings
    run, warnings = await asyncio.to_thread(bridge.session, work)
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
                       timeout_s: float = 10) -> dict:
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
    window:{hwnd, title, process}, steps:[{text, math, result}], warnings:[str], note}.
    The new document is unsaved.

    page_size: "computer" (default: large page, fills the TI-Nspire window) or "handheld"
    (the calculator's small screen, e.g. to transfer the document to a handheld).
    """
    nb = await asyncio.to_thread(importlib.import_module, "nspire_notes")
    cancel = threading.Event()  # set when the client cancels the call: the worker stops typing

    def work():
        return nb.build_notes(bridge, steps, title, page_size, timeout_s, cancel=cancel)
    log.info("notes: %d steps", len(steps))
    try:
        return await asyncio.to_thread(bridge.session, work)
    except asyncio.CancelledError:
        cancel.set()
        log.info("notes: cancelled")
        raise


if __name__ == "__main__":
    mcp.run()  # stdio
