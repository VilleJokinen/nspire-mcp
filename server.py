# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["mcp>=1.2,<2", "pywin32>=306", "sympy>=1.13", "antlr4-python3-runtime==4.11.*"]
# ///
"""MCP server: evaluate expressions in the TI-Nspire CX (CAS) desktop app.

Talks to bridge.lua (running on a page of nspire_bridge.tns) through the
Windows clipboard. Run with:  uv run --script server.py
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from nspire_bridge import Bridge, BridgeTimeout  # noqa: E402


logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="nspire: %(message)s")
log = logging.getLogger("nspire")

bridge = Bridge()
mcp = FastMCP("nspire")


@mcp.tool()
async def nspire_status() -> dict:
    """Ping the TI-Nspire bridge.

    Returns alive, the Lua API level, whether the CAS is available, and the bridge
    script's protocol (2 = batch support; 1 = an older nspire_bridge.tns).
    """
    def work():
        try:
            return bridge.ping()
        except BridgeTimeout as e:
            return {"alive": False, "error": str(e)}
    return await asyncio.to_thread(bridge.session, work)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
async def nspire_latex(latex: str, action: str = "auto", var: str | None = None,
                       order: int | None = None, point: str | None = None,
                       timeout_s: float = 10) -> dict:
    """Solve or evaluate a LaTeX problem on the TI-Nspire CAS; returns the answer as text and LaTeX.

    Accepts $…$ / \\[…\\] delimiters, \\frac, \\sqrt[n]{}, \\int (with limits, \\,dx or \\mathrm{d}x),
    \\iint, \\sum, \\prod, \\lim (x\\to a, a^+), \\frac{d}{dx}, derivatives y', y'', \\frac{dy}{dx},
    \\dot y, matrices (pmatrix/bmatrix; vmatrix = determinant), cases/align systems, several
    equations separated by \\\\ , commas or ;, degrees (30^\\circ), |x|, n!, \\binom.
    \\log without a base is base 10 and \\ln is natural; e and i are the constants unless they
    are summation/integration variables. "1,5" between digits is a decimal comma.

    action="auto" picks:
      f(x)=…            -> Define (f stays defined for later calls)
      equation(s) with y', y'' or dy/dx -> deSolve (initial conditions like y(0)=1 allowed)
      equation / inequality / system    -> solve for x (or the variables present)
      anything else     -> evaluate (integrals, limits, sums, derivatives, matrices, simplification)
    Or force one: eval, approx, solve, csolve, nsolve, zeros, czeros, factor, cfactor, expand,
    propfrac, comdenom, texpand, tcollect, diff, integrate, taylor (order, point), limit (point),
    det, inverse, rref, eigenvalues, eigenvectors, desolve, define.
    var: the variable(s) to solve/differentiate for, e.g. "t" or "x,y".

    Returns {action, nspire (the command run), result, result_latex, approx?, approx_latex?}.
    result_latex is null when the result couldn't be converted; result is always the raw CAS text.
    Variables stored in the open document (e.g. a:=5) are substituted. If the LaTeX can't be
    parsed, the error says so; then translate it to TI-Nspire syntax yourself and use nspire_eval.
    """
    # Imported here, not at startup: sympy takes seconds to load and the MCP
    # client times out a server that's slow to answer initialize.
    lx = await asyncio.to_thread(importlib.import_module, "nspire_latex")

    def work():
        return lx.solve_latex(bridge, latex, action, var, order, point, timeout_s)
    log.info("latex %r action=%s", latex, action)
    try:
        lx.plan(latex, action, var, order, point)  # fail fast on bad LaTeX, no clipboard use
    except lx.LatexError as e:
        raise ValueError(str(e)) from None
    return await asyncio.to_thread(bridge.session, work)


@mcp.tool()
async def nspire_notes(steps: list[dict], title: str | None = None, page_size: str = "computer",
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

    Every math step is evaluated through the bridge first; if one errors, nothing is typed
    and the error names the step. The bridge document must be open (it stays on its page).
    It takes about a second per step and types into the TI-Nspire window, which comes to the
    front: the user shouldn't type or click meanwhile. If TI-Nspire loses focus it stops
    without typing anywhere else. Returns {document, steps:[{text, math, result}], note}.
    The new document is unsaved.

    page_size: "computer" (default: large page, fills the TI-Nspire window) or "handheld"
    (the calculator's small screen, e.g. to transfer the document to a handheld).
    """
    nb = await asyncio.to_thread(importlib.import_module, "nspire_notes")

    def work():
        return nb.build_notes(bridge, steps, title, page_size, timeout_s)
    log.info("notes: %d steps", len(steps))
    return await asyncio.to_thread(bridge.session, work)


if __name__ == "__main__":
    mcp.run()  # stdio
