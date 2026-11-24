"""Offline tests of server.py: tool schemas and annotations, the nspire_latex
document warnings, nspire_plot's sampling, and nspire_notes cancellation. A fake bridge answers instead
of TI-Nspire; the real clipboard is never touched.

Skipped by tests/conftest.py where pywin32 or mcp is missing.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import math
import os
import re
import subprocess
import sys
import threading
import types
from pathlib import Path
from typing import Any, get_args, get_type_hints

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import nspire_bridge as nb  # noqa: E402

nb.MUTEX_NAME = f"Local\\nspire_mcp_bridge_offline_test_{os.getpid()}"  # not the real bridge's, nor other runs'
from mcp import types as mt  # noqa: E402

import nspire_errors as ne  # noqa: E402
import server  # noqa: E402


class FakeBridge:
    """Answers from a dict {expr: (ok, text)}; anything else is a syntax error."""

    def __init__(self, replies: dict[str, tuple[bool, str]]):
        self.replies = replies
        self.calls: list[str] = []
        self.batches: list[list[str]] = []
        self.sessions = 0
        self.last_timeout: str | None = None

    def session(self, work, wait_s: float = 60):
        self.sessions += 1
        return work()

    # the real mode override, on top of this fake's eval_one/eval_batch
    eval_in_modes = nb.Bridge.eval_in_modes

    def _reply(self, expr: str) -> tuple[bool, str]:
        self.calls.append(expr)
        return self.replies.get(expr, (False, "error 910 (syntax error)"))

    def eval_one(self, expr, timeout_s, raw=False):
        ok, text = self._reply(expr)
        if not ok:
            raise ValueError(f"TI-Nspire error: {text}")
        return text if raw else nb.readable(text)

    def eval_batch(self, exprs, timeout_s=5.0, raw=False):
        self.batches.append(list(exprs))
        out = []
        for e in exprs:
            ok, text = self._reply(e)
            out.append({"expr": e, "ok": True, "result": text} if ok else {"expr": e, "ok": False, "error": text})
        return out


def run(coro):
    return asyncio.run(coro)


def tools() -> dict:
    return {t.name: t for t in run(server.mcp.list_tools())}


# ------------------------------------------------------------ schemas and metadata

def test_tool_list_and_annotations():
    ts = tools()
    assert set(ts) == {"nspire_status", "nspire_eval", "nspire_eval_batch", "nspire_vars", "nspire_clear",
                       "nspire_latex", "nspire_notes", "nspire_plot", "nspire_check_steps", "nspire_stats"}
    for t in ts.values():
        assert t.annotations is not None and t.annotations.title
        assert t.annotations.openWorldHint is False
    for name in ("nspire_status", "nspire_vars", "nspire_check_steps"):
        assert ts[name].annotations.readOnlyHint is True and ts[name].annotations.idempotentHint is True
    assert ts["nspire_clear"].annotations.destructiveHint is True
    for name in ("nspire_eval", "nspire_eval_batch", "nspire_latex", "nspire_notes", "nspire_clear", "nspire_plot"):
        assert not ts[name].annotations.readOnlyHint  # they can define or delete things, or evaluate in the document
    assert ts["nspire_plot"].annotations.destructiveHint is False
    # it writes TI-Nspire's stat.* variables, so not read-only; the data lists are put back
    assert ts["nspire_stats"].annotations.destructiveHint is False and not ts["nspire_stats"].annotations.readOnlyHint


def test_action_enum_matches_translator():
    import nspire_latex
    assert set(get_args(server.LatexAction)) == {"auto", *nspire_latex.ACTIONS}
    schema = tools()["nspire_latex"].inputSchema
    action = schema["properties"]["action"]
    assert action["enum"] == list(get_args(server.LatexAction)) and action["default"] == "auto"


def test_notes_schema():
    schema = tools()["nspire_notes"].inputSchema
    props = schema["properties"]
    assert props["page_size"]["enum"] == ["computer", "handheld"]
    ref = props["steps"]["items"]["$ref"].rsplit("/", 1)[1]
    step = schema["$defs"][ref]
    assert set(step["properties"]) == {"text", "math", "latex", "action", "var", "to", "approx", "constants"}
    assert not step.get("required")
    assert all(p.get("description") for p in step["properties"].values())
    assert step["properties"]["action"]["enum"] == list(get_args(server.LatexAction))


def test_instructions():
    text = server.mcp.instructions
    for needle in ("nspire_bridge.tns", "clipboard", "nspire_vars", "nspire_clear", "nspire_notes",
                   "isolated=True", "exact_mode", "nspire_plot", "nspire_check_steps", "nspire_stats"):
        assert needle in text


def test_bad_action_rejected_before_the_bridge(monkeypatch):
    fake = FakeBridge({})
    monkeypatch.setattr(server, "bridge", fake)
    with pytest.raises(Exception, match="action"):
        run(server.mcp.call_tool("nspire_latex", {"latex": "x^2=4", "action": "bogus"}))
    assert fake.calls == []


def test_startup_imports_stay_light():
    # The client times out a server that's slow to answer initialize: no SymPy, no
    # matplotlib, no keystroke module at import. A fresh interpreter, since other tests import them.
    code = ("import sys; sys.path.insert(0, '.'); import server; "
            "print(sorted(m for m in ('sympy', 'nspire_latex', 'nspire_notes', 'nspire_keys', 'matplotlib', "
            "'nspire_plot', 'nspire_steps', 'nspire_stats') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]"


# ------------------------------------------------------------ nspire_status

def test_status_reports_modes(monkeypatch):
    class B(FakeBridge):
        def ping(self, timeout_s=3.0):
            return {"alive": True, "api_level": "2.7", "cas": True, "protocol": 2}

        def modes(self, timeout_s=5.0):
            return {"angle": "Degree"}
    monkeypatch.setattr(server, "bridge", B({}))
    st = run(server.nspire_status())
    assert st["alive"] and st["modes"] == {"angle": "Degree"}


def test_status_dead_bridge(monkeypatch):
    class B(FakeBridge):
        def ping(self, timeout_s=3.0):
            raise nb.BridgeTimeout("No reply from the TI-Nspire bridge within 3 s.")
    monkeypatch.setattr(server, "bridge", B({}))
    st = run(server.nspire_status())
    assert st["alive"] is False and "No reply" in st["error"] and "modes" not in st


# ------------------------------------------------------------ nspire_latex warnings

RADIAN, DEGREE = (True, "1"), (True, "2")
NOTHING = (True, '"NONE"')


def latex_call(monkeypatch, latex: str, replies: dict, action: str = "auto"):
    import nspire_latex
    cmd = nspire_latex.plan(latex, action).command
    fake = FakeBridge({k.replace("CMD", cmd): v for k, v in replies.items()})
    monkeypatch.setattr(server, "bridge", fake)
    return run(server.nspire_latex(latex, action)), fake, cmd


def test_latex_warns_about_substituted_variable(monkeypatch):
    out, fake, cmd = latex_call(monkeypatch, "x^2=4", {
        "getVarInfo()": (True, '[["x","NUM","none",0]["q","NUM","none",0]]'),
        "getMode(2)": RADIAN, "x": (True, "3"), "CMD": (True, "false")})
    assert out["result"] == "false"
    assert len(out["warnings"]) == 1
    assert out["warnings"][0].startswith("x is stored in the document (x = 3)")
    assert fake.sessions == 1                          # one session: clipboard saved and restored once
    assert fake.batches == [["getVarInfo()", "getMode(2)"], ["x"]]  # q isn't used, so it isn't read
    assert fake.calls.index(cmd) > fake.calls.index("x")  # checked before the command runs


def test_latex_no_warnings_key_when_clean(monkeypatch):
    out, fake, cmd = latex_call(monkeypatch, "x^2=4", {
        "getVarInfo()": NOTHING, "getMode(2)": DEGREE, "CMD": (True, "x=−2 or x=2")})
    assert "warnings" not in out  # degree mode doesn't matter without trig
    assert fake.batches == [["getVarInfo()", "getMode(2)"]]


def test_latex_warns_about_degree_mode(monkeypatch):
    out, fake, cmd = latex_call(monkeypatch, r"\sin(x)", {
        "getVarInfo()": NOTHING, "getMode(2)": DEGREE, "CMD": (True, "sin(x)")})
    assert len(out["warnings"]) == 1 and "angle mode is Degree" in out["warnings"][0]
    out, _, _ = latex_call(monkeypatch, r"\sin(x)", {
        "getVarInfo()": NOTHING, "getMode(2)": RADIAN, "CMD": (True, "sin(x)")})
    assert "warnings" not in out
    out, _, _ = latex_call(monkeypatch, r"\sinh(x)", {
        "getVarInfo()": NOTHING, "getMode(2)": DEGREE, "CMD": (True, "sinh(x)")})
    assert "warnings" not in out  # hyperbolic functions don't use the angle mode


def test_latex_warns_about_functions(monkeypatch):
    stored_f = {"getVarInfo()": (True, '[["f","FUNC","none",0]]'), "getMode(2)": RADIAN}
    out, fake, _ = latex_call(monkeypatch, "f(x)=x^2", {**stored_f, "CMD": (True, "Done")})
    assert out["warnings"] == ["f was already stored in the document (FUNC); this replaced it."]
    out, fake, _ = latex_call(monkeypatch, "f(2)", {**stored_f, "CMD": (True, "4")})
    assert out["warnings"] == ["f is defined in the document (FUNC); its stored definition was used."]
    assert fake.batches == [["getVarInfo()", "getMode(2)"]]  # a function's value isn't read


def test_latex_checks_never_fail_the_tool(monkeypatch):
    # getVarInfo and getMode both fail: no warnings, the answer still comes back
    out, _, _ = latex_call(monkeypatch, "x^2=4", {"CMD": (True, "x=−2 or x=2")})
    assert out["result"] == "x=−2 or x=2" and "warnings" not in out
    # reading the value fails: no value, still a warning
    out, _, _ = latex_call(monkeypatch, "x^2=4", {
        "getVarInfo()": (True, '[["x","NUM","none",0]]'), "getMode(2)": (True, "weird"),
        "CMD": (True, "false")})
    assert out["warnings"] == ["x is stored in the document (NUM) and was substituted; see nspire_vars."]

    # scan_names blows up: no stored-variable warnings, the angle check still runs
    class Broken:
        @staticmethod
        def scan_names(cmd):
            raise RuntimeError("boom")
    fake = FakeBridge({"getVarInfo()": (True, '[["x","NUM","none",0]]'), "getMode(2)": DEGREE})
    w = server.doc_warnings(fake, "sin(x)", 5, Broken)
    assert len(w) == 1 and "angle mode is Degree" in w[0]


def test_latex_dead_bridge_fails_once(monkeypatch):
    class Dead(FakeBridge):
        def eval_batch(self, exprs, timeout_s=5.0, raw=False):
            self.last_timeout = "No reply from the TI-Nspire bridge within 5 s."
            return [{"expr": e, "ok": False, "error": self.last_timeout} for e in exprs]
    fake = Dead({})
    monkeypatch.setattr(server, "bridge", fake)
    with pytest.raises(nb.BridgeTimeout, match="No reply"):
        run(server.nspire_latex("x^2=4"))
    assert fake.calls == []  # the command isn't sent to a bridge that doesn't answer


def test_value_shortened():
    fake = FakeBridge({"getVarInfo()": (True, '[["m","LIST","none",0]]'), "getMode(2)": RADIAN,
                       "m": (True, "{" + ",".join(["1"] * 400) + "}")})
    import nspire_notes
    w = server.doc_warnings(fake, "sum(m)", 5, nspire_notes)
    assert len(w) == 1 and "…)" in w[0]


# ------------------------------------------------------------ nspire_notes

def test_build_notes_accepts_cancel():
    import nspire_notes
    params = inspect.signature(nspire_notes.build_notes).parameters
    assert "cancel" in params and params["cancel"].default is None


def fake_notes_module(monkeypatch, build_notes):
    mod = types.ModuleType("nspire_notes")
    mod.build_notes = build_notes
    monkeypatch.setitem(sys.modules, "nspire_notes", mod)


def test_notes_steps_reach_build_notes_as_dicts(monkeypatch):
    got = {}

    def build_notes(bridge, steps, title, page_size, timeout_s, cancel=None, **kw):
        got.update(steps=steps, page_size=page_size, cancel=cancel)
        return {"document": "Document 2", "steps": [], "warnings": [], "note": ""}
    fake_notes_module(monkeypatch, build_notes)
    monkeypatch.setattr(server, "bridge", FakeBridge({}))
    run(server.mcp.call_tool("nspire_notes", {"steps": [{"text": "Area:", "latex": r"\int_0^1 x\,dx"},
                                                        {"math": "1+1"}], "page_size": "handheld"}))
    assert got["steps"] == [{"text": "Area:", "latex": r"\int_0^1 x\,dx"}, {"math": "1+1"}]
    assert all(type(s) is dict for s in got["steps"])
    assert got["page_size"] == "handheld" and isinstance(got["cancel"], threading.Event)
    assert not got["cancel"].is_set()


def test_notes_cancel_sets_event(monkeypatch):
    started, seen = threading.Event(), {}

    def build_notes(bridge, steps, title, page_size, timeout_s, cancel=None, **kw):
        started.set()
        seen["stopped"] = cancel.wait(10)  # a real build_notes checks this between keystrokes
        return {}
    fake_notes_module(monkeypatch, build_notes)
    monkeypatch.setattr(server, "bridge", FakeBridge({}))

    async def scenario():
        task = asyncio.create_task(server.nspire_notes([{"text": "hi"}]))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    run(scenario())  # asyncio.run waits for the worker thread to finish
    assert seen["stopped"] is True


# ------------------------------------------------------------ output schemas

def call(name: str, args: dict):
    """A tools/call through the MCP server's own handler, which checks the
    result against the tool's outputSchema (as a client would see it)."""
    handler = server.mcp._mcp_server.request_handlers[mt.CallToolRequest]
    req = mt.CallToolRequest(method="tools/call", params=mt.CallToolRequestParams(name=name, arguments=args))
    return run(handler(req)).root


def test_output_schemas():
    ts = tools()
    assert ts["nspire_notes"].outputSchema is None  # not typed (yet)

    def props(schema, ref=None):
        sch = schema["$defs"][ref] if ref else schema
        return set(sch["properties"]), set(sch.get("required", []))
    assert props(ts["nspire_status"].outputSchema) == (
        {"alive", "api_level", "cas", "protocol", "modes", "error"}, {"alive"})
    assert props(ts["nspire_eval"].outputSchema) == ({"result", "warnings"}, {"result"})
    batch = ts["nspire_eval_batch"].outputSchema
    assert batch["properties"]["result"]["type"] == "array"
    assert props(batch, "BatchItem") == ({"expr", "ok", "result", "error", "warnings"}, {"expr", "ok"})
    assert props(ts["nspire_vars"].outputSchema, "VarEntry") == (
        {"name", "type", "library", "locked", "value"}, {"name", "type", "library", "locked"})
    clear = ts["nspire_clear"].outputSchema
    assert props(clear) == ({"deleted", "errors"}, {"deleted", "errors"})
    assert props(clear, "ClearError") == ({"name", "error"}, {"name", "error"})
    check = ts["nspire_check_steps"].outputSchema
    assert props(check) == ({"all_ok", "first_broken", "summary", "steps", "lines", "var", "warnings"},
                            {"all_ok", "first_broken", "summary", "steps", "lines"})
    assert props(check, "CheckStep") == (
        {"step", "from_line", "to_line", "kind", "verdict", "check", "cas", "reason", "residual", "spot_check"},
        {"step", "from_line", "to_line", "kind", "verdict", "check", "cas", "reason"})
    assert props(check, "SpotCheck") == ({"tried", "usable", "agreed", "counterexample"},) * 2
    assert props(check, "SpotCounterexample") == ({"at", "command", "before", "after", "difference"},) * 2
    assert props(check, "CheckLine") == ({"line", "source", "nspire", "kind"},) * 2
    assert props(ts["nspire_latex"].outputSchema) == (
        {"action", "nspire", "result", "result_latex", "approx", "approx_latex", "warnings"},
        {"action", "nspire", "result", "result_latex"})
    plot = ts["nspire_plot"].outputSchema
    assert props(plot) == ({"kind", "var", "range", "samples", "summary", "curves", "marks", "warnings"},
                           {"kind", "var", "range", "samples", "summary", "curves"})
    assert props(plot, "PlotCurve") == ({"label", "nspire", "points", "total", "x_range", "y_range"},) * 2
    assert props(plot, "PlotMark") == ({"type", "functions", "x", "y", "x_approx", "y_approx", "method"},) * 2


def test_latex_result_declares_every_key():
    # structuredContent drops keys LatexResult doesn't declare
    import nspire_latex as lx
    p = lx.plan(r"\sqrt{8}")
    run_ = lx.CasRun(p, "2*√(2)")
    run_.approx_raw = "2.82843"
    assert set(lx.format_answer(run_)) | {"warnings"} == set(get_type_hints(server.LatexResult))


def test_structured_results_match_schemas(monkeypatch):
    class B(FakeBridge):
        def ping(self, timeout_s=3.0):
            return {"alive": True, "api_level": "2.7", "cas": True, "protocol": 2}

        def modes(self, timeout_s=5.0):
            return {"angle": "Radian", "display_digits": "Float6", "mode_9": 4}

        def list_vars(self, values=True, timeout_s=5.0):
            return [{"name": "a", "type": "NUM", "library": None, "locked": False, "value": "5"},
                    {"name": "f", "type": "FUNC", "library": None, "locked": None}]

        def clear_vars(self, names=None, timeout_s=5.0):
            return {"deleted": ["a"], "errors": [{"name": "f", "error": "locked"}]}
    fake = B({"2+2": (True, "4"), "getVarInfo()": NOTHING, "getMode(2)": RADIAN, r"√(8)": (True, "2*√(2)"),
              "approx(√(8))": (True, "2.82843")})
    monkeypatch.setattr(server, "bridge", fake)
    r = call("nspire_status", {})
    assert not r.isError and r.structuredContent["alive"] is True and r.structuredContent["modes"]["mode_9"] == 4
    r = call("nspire_eval", {"expr": "2+2"})
    assert not r.isError and [c.text for c in r.content] == ["4"] and r.structuredContent == {"result": "4"}
    r = call("nspire_eval_batch", {"exprs": ["2+2", "bad"]})
    assert not r.isError and r.structuredContent["result"] == [
        {"expr": "2+2", "ok": True, "result": "4"},
        {"expr": "bad", "ok": False, "error": "error 910 (syntax error)",
         "warnings": [ne.explain("error 910 (syntax error)")]}]
    r = call("nspire_vars", {})
    assert not r.isError and r.structuredContent["result"][1] == {
        "name": "f", "type": "FUNC", "library": None, "locked": None}
    r = call("nspire_clear", {"names": ["a", "f"]})
    assert not r.isError and r.structuredContent == {"deleted": ["a"], "errors": [{"name": "f", "error": "locked"}]}
    r = call("nspire_latex", {"latex": r"\sqrt{8}"})
    assert not r.isError, r.content
    assert r.structuredContent["result"] == "2*√(2)" and r.structuredContent["approx"] == "2.82843"

    class Dead(FakeBridge):
        def ping(self, timeout_s=3.0):
            raise nb.BridgeTimeout("No reply")
    monkeypatch.setattr(server, "bridge", Dead({}))
    r = call("nspire_status", {})
    assert not r.isError and r.structuredContent["alive"] is False and r.structuredContent["error"] == "No reply"


# ------------------------------------------------------------ isolated evaluation and mode overrides

class CasBridge(FakeBridge):
    """Just enough of a CAS for isolation and modes: getVarInfo, getType, DelVar,
    Define, :=, reading a variable, getMode, and the mode override's temporary function
    (setMode works only inside it, as on the real CAS, and the modes come back when it
    returns). self.modes_seen records the angle and auto/approx settings each other
    expression ran under."""

    def __init__(self, stored=None, modes=None, set_mode="works", undeletable=()):
        super().__init__({})
        self.vars = {k: tuple(v) for k, v in (stored or {}).items()}  # lower name -> (type, value)
        self.modes = dict(modes or {2: 1, 5: 1})
        self.set_mode = set_mode
        self.undeletable = set(undeletable)
        self.modes_seen: dict[str, tuple[int, int]] = {}
        self.mode_func: tuple[list[tuple[str, str]], str] | None = None  # (setMode args, returned expr)

    def _reply(self, expr):
        self.calls.append(expr)
        if expr == "getVarInfo()":
            if not self.vars:
                return True, '"NONE"'
            return True, "[" + "".join(f'[{n},"{t}","none",0]' for n, (t, _) in self.vars.items()) + "]"
        if m := re.fullmatch(r"getType\((\w+)\)", expr):
            return True, f'"{self.vars.get(m.group(1).lower(), ("NONE", None))[0]}"'
        if m := re.fullmatch(r"getMode\((\d+)\)", expr):
            return True, str(self.modes.get(int(m.group(1)), 1))
        if m := re.fullmatch(r"Define brgmodeeval\(\)=Func:Local brgmodeold((?::brgmodeold:=setMode\(\d+,\d+\))*)"
                             r":Return (.*):EndFunc", expr):
            self.mode_func = (re.findall(r"setMode\((\d+),(\d+)\)", m.group(1)), m.group(2))
            return True, ""
        if expr == "brgmodeeval()":
            if self.mode_func is None:
                return False, "error 1090 (function is not defined)"
            if self.set_mode == "refused":
                return False, "error 450 (invalid in a function or current expression)"
            sets, inner = self.mode_func
            saved = dict(self.modes)
            for mode, setting in sets:
                self.modes[int(mode)] = int(setting)
            try:
                return self._reply(inner)
            finally:
                self.modes = saved
        if expr == "DelVar brgmodeeval":
            self.mode_func = None
            return True, ""
        if re.fullmatch(r"setMode\((\d+),(\d+)\)", expr):
            return False, "error 550 (invalid outside function or program)"
        self.modes_seen[expr] = (self.modes.get(2, 1), self.modes.get(5, 1))
        if m := re.fullmatch(r"DelVar (\w+)", expr):
            if m.group(1).lower() in self.undeletable:
                return False, "error 980 (variable is protected)"
            self.vars.pop(m.group(1).lower(), None)
            return True, "Done"
        if m := re.fullmatch(r"Define (\w+)\(.*\)=.*", expr):
            self.vars[m.group(1).lower()] = ("FUNC", None)
            return True, "Done"
        if m := re.fullmatch(r"(\w+):=(.*)", expr):
            value = self.vars.get(m.group(1).lower(), (None, None))[1] if m.group(2) == "a+1" else m.group(2)
            value = str(int(value) + 1) if m.group(2) == "a+1" else value
            self.vars[m.group(1).lower()] = ("NUM", value)
            return True, value
        if expr == "bad":
            return False, "error 910 (syntax error)"
        if re.fullmatch(r"\w+", expr):
            t, v = self.vars.get(expr.lower(), ("NONE", None))
            return (False, "error 40 (argument error)") if t == "FUNC" else (True, v if t != "NONE" else expr)
        return True, f"<{expr}>"


def use(monkeypatch, fake):
    monkeypatch.setattr(server, "bridge", fake)
    return fake


def test_changed_names():
    import nspire_notes
    assert server.changed_names(nspire_notes, ["a:=5", "Define f(x)=x^2", "3→b", "DelVar c, d", "solve(x^2=4,x)",
                                               "delvar e: f(2)"]) == [
        ["a"], ["f"], ["b"], ["c", "d"], [], ["e"]]


def test_eval_isolated_removes_new_names(monkeypatch):
    fake = use(monkeypatch, CasBridge())
    r = call("nspire_eval", {"expr": "a:=5", "isolated": True})
    assert not r.isError and [c.text for c in r.content] == ["5"] and r.structuredContent == {"result": "5"}
    assert fake.vars == {} and fake.calls.index("a:=5") < fake.calls.index("DelVar a")
    assert fake.sessions == 1


def test_eval_isolated_restores_and_deletes(monkeypatch):
    fake = use(monkeypatch, CasBridge({"a": ("NUM", "3")}))
    assert run(server.nspire_eval("a:=a+1", isolated=True)).structuredContent == {"result": "4"}
    assert fake.vars == {"a": ("NUM", "3")}
    run(server.nspire_eval("DelVar a", isolated=True))
    assert fake.vars == {"a": ("NUM", "3")}
    # without isolated the change stays
    run(server.nspire_eval("a:=a+1"))
    assert fake.vars == {"a": ("NUM", "4")}


def test_isolated_refuses_to_replace_a_function(monkeypatch):
    fake = use(monkeypatch, CasBridge({"f": ("FUNC", None)}))
    with pytest.raises(ValueError, match="f is a function stored in the document"):
        run(server.nspire_eval("Define f(x)=x^2", isolated=True))
    assert "Define f(x)=x^2" not in fake.calls and fake.vars == {"f": ("FUNC", None)}
    r = call("nspire_eval_batch", {"exprs": ["2+2", "Define f(x)=x^3"], "isolated": True})
    assert r.isError and "can't be saved and put back" in r.content[0].text and "2+2" not in fake.calls


def test_eval_warning_when_not_put_back(monkeypatch):
    fake = use(monkeypatch, CasBridge(undeletable={"a"}))
    r = run(server.nspire_eval("a:=5", isolated=True))
    assert [c.text for c in r.content][0] == "5"
    assert len(r.content) == 2 and r.content[1].text.startswith("Warnings: Couldn't remove a")
    assert "after the isolated call" in r.structuredContent["warnings"][0]
    assert fake.vars == {"a": ("NUM", "5")}


def test_batch_isolated(monkeypatch):
    fake = use(monkeypatch, CasBridge({"b": ("NUM", "7")}, undeletable={"g"}))
    res = run(server.nspire_eval_batch(["2+2", "Define g(x)=x^2", "g(3)", "b:=2", "b"], isolated=True))
    assert [r["ok"] for r in res] == [True] * 5 and res[4]["result"] == "2"  # later ones see earlier definitions
    assert fake.vars == {"b": ("NUM", "7"), "g": ("FUNC", None)}
    assert "warnings" not in res[0] and "warnings" not in res[3]
    assert len(res[1]["warnings"]) == 1 and res[1]["warnings"][0].startswith("Couldn't remove g")
    # nothing to put back: no extra round trips
    fake = use(monkeypatch, CasBridge())
    run(server.nspire_eval_batch(["2+2", "3+3"], isolated=True))
    assert fake.calls == ["2+2", "3+3"]


def test_latex_isolated(monkeypatch):
    fake = use(monkeypatch, CasBridge({"a": ("NUM", "3")}))
    out = run(server.nspire_latex("f(x)=x^2", isolated=True))
    assert out["result"] == "Done" and "f" not in fake.vars and "warnings" not in out
    out = run(server.nspire_latex("a:=5", isolated=True))
    assert fake.vars["a"] == ("NUM", "3")
    assert out["warnings"] == ["a was already stored in the document (NUM); this call replaced it until it was "
                               "put back (isolated)."]


def test_eval_angle_override(monkeypatch):
    fake = use(monkeypatch, CasBridge(modes={2: 1, 5: 1}))
    r = run(server.nspire_eval("sin(30)", angle="degree", exact_mode="approx"))
    assert r.structuredContent == {"result": "<sin(30)>"}
    assert fake.modes_seen["sin(30)"] == (2, 2) and fake.modes == {2: 1, 5: 1}
    # one temporary function, defined, called and deleted again; no top-level setMode
    assert fake.batches[-1] == [
        "Define brgmodeeval()=Func:Local brgmodeold:brgmodeold:=setMode(2,2):brgmodeold:=setMode(5,2)"
        ":Return sin(30):EndFunc", "brgmodeeval()", "DelVar brgmodeeval"]
    assert fake.mode_func is None
    before = len(fake.calls)
    run(server.nspire_eval("sin(31)"))
    assert fake.calls[before:] == ["sin(31)"] and fake.modes_seen["sin(31)"] == (1, 1)
    # the expression's own error, and the function is deleted all the same
    with pytest.raises(ValueError, match="syntax error"):
        run(server.nspire_eval("bad", angle="gradian"))
    assert fake.modes == {2: 1, 5: 1} and fake.mode_func is None


def test_mode_override_refused(monkeypatch):
    fake = use(monkeypatch, CasBridge(set_mode="refused"))
    r = call("nspire_eval", {"expr": "sin(30)", "angle": "degree"})
    assert r.isError and "runs inside a temporary TI-Nspire function" in r.content[0].text
    assert "sin(30)" not in fake.calls and fake.mode_func is None
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError, match="angle"):
        call_args = {"expr": "1", "angle": "turns"}
        run(server.mcp.call_tool("nspire_eval", call_args))


@pytest.mark.parametrize("expr", ["a:=7", "Define f(x)=x^2", "DelVar a", "7→a", "7=:a", "a:=7:a+1"])
def test_mode_override_refuses_stores(monkeypatch, expr):
    # a store can't run inside the temporary function: refused before anything is sent
    fake = use(monkeypatch, CasBridge({"a": ("NUM", "3")}))
    with pytest.raises(ValueError, match="stores, defines or deletes"):
        run(server.nspire_eval(expr, isolated=True, exact_mode="approx"))
    assert fake.calls == [] and fake.vars == {"a": ("NUM", "3")}


def test_latex_mode_override_refuses_definitions(monkeypatch):
    fake = use(monkeypatch, CasBridge())
    for latex in ("f(x)=x^2", "a:=5"):
        with pytest.raises(ValueError, match="stores, defines or deletes"):
            run(server.nspire_latex(latex, angle="degree"))
    assert fake.calls == []


def test_latex_angle_override_skips_angle_warning(monkeypatch):
    fake = use(monkeypatch, CasBridge(modes={2: 2, 5: 1}))
    out = run(server.nspire_latex(r"\sin(x)"))
    assert "angle mode is Degree" in out["warnings"][0]
    out = run(server.nspire_latex(r"\sin(x)", angle="radian"))
    assert "warnings" not in out and fake.modes == {2: 2, 5: 1}
    assert fake.modes_seen["sin(x)"] == (1, 1) and fake.mode_func is None


def test_latex_mode_override_covers_the_approx(monkeypatch):
    # run_plan's approx() of an exact answer runs in the same modes
    class B(CasBridge):
        def _reply(self, expr):
            if expr in ("π/4", "approx(π/4)"):
                self.calls.append(expr)
                self.modes_seen[expr] = (self.modes.get(2, 1), self.modes.get(5, 1))
                return True, "π/4" if expr == "π/4" else "0.785398"
            return super()._reply(expr)
    fake = use(monkeypatch, B(modes={2: 1, 5: 1}))
    out = run(server.nspire_latex(r"\frac{\pi}{4}", angle="degree"))
    assert out["result"] == "π/4" and out["approx"] == "0.785398"
    assert fake.modes_seen == {"π/4": (2, 1), "approx(π/4)": (2, 1)} and fake.modes == {2: 1, 5: 1}


# ------------------------------------------------------------ nspire_check_steps

def test_check_steps_through_the_handler(monkeypatch):
    # checked against the outputSchema, as a client would see it
    fake = use(monkeypatch, FakeBridge({
        "getVarInfo()": NOTHING, "getMode(2)": RADIAN,
        "expand(((x+1)^2)-(x^2+2x+1))": (True, "0"),
        "expand((x^2+2x+1)-(x^2+1))": (True, "2*x"),
    }))
    r = call("nspire_check_steps", {"lines": ["(x+1)^2", "=x^2+2x+1", "=x^2+1"], "latex": False})
    assert not r.isError, r.content
    out = r.structuredContent
    assert out["all_ok"] is False and out["first_broken"] == 2 and out["var"] is None
    assert [s["verdict"] for s in out["steps"]] == ["ok", "broken"]
    assert out["steps"][1]["residual"] == "2*x" and out["steps"][1]["cas"] == ["2*x"]
    assert out["summary"].startswith("Step 2 (line 2 to line 3) is broken")
    assert out["lines"][1] == {"line": 2, "source": "=x^2+2x+1", "nspire": "x^2+2x+1", "kind": "expression"}
    assert fake.sessions == 1
    assert fake.batches[-1] == ["expand(((x+1)^2)-(x^2+2x+1))", "expand((x^2+2x+1)-(x^2+1))"]


def test_check_steps_latex_equations_and_warnings(monkeypatch):
    fake = use(monkeypatch, FakeBridge({
        "getVarInfo()": (True, '[["a","NUM","none",0]]'), "getMode(2)": RADIAN, "a": (True, "3"),
        "solve(2*x + a=7,x)": (True, "x=2"), "solve(2*x=4,x)": (True, "x=2"), "solve(x=2,x)": (True, "x=2"),
    }))
    out = run(server.nspire_check_steps([r"2x+a=7", r"\Rightarrow 2x=4", r"x=2"]))
    assert out["all_ok"] is True and out["first_broken"] is None and out["var"] == "x"
    assert out["summary"] == "All 2 step(s) check out on the CAS."
    assert any("a is stored in the document (a = 3)" in w for w in out["warnings"])
    assert fake.batches[-1] == ["solve(2*x + a=7,x)", "solve(2*x=4,x)", "solve(x=2,x)"]


def test_check_steps_spot_check_through_the_handler(monkeypatch):
    # an undecided step settled numerically, checked against the outputSchema
    import nspire_steps as st
    a, b = "√(x^2)", "x"
    replies = {"getVarInfo()": NOTHING, "getMode(2)": RADIAN, f"expand(({a})-({b}))": (True, "|x|−x")}
    for p in st.spot_points(["x"], f"{a}\n{b}"):
        x = float(p["x"])
        replies[st.spot_command(a, b, p)] = (True, f"{{{abs(x):g},{x:g},{abs(x) - x:g}}}".replace("-", "−"))
    fake = use(monkeypatch, FakeBridge(replies))
    r = call("nspire_check_steps", {"lines": [a, b], "latex": False})
    assert not r.isError, r.content
    out = r.structuredContent
    s = out["steps"][0]
    assert s["verdict"] == "broken" and out["first_broken"] == 1 and out["all_ok"] is False
    assert s["spot_check"]["counterexample"]["at"].startswith("x=-")  # the first negative point
    assert s["spot_check"]["usable"] == 12 and fake.sessions == 1
    assert len(fake.batches[-1]) == 12 and all(e.startswith("approx(") for e in fake.batches[-1])

    replies = {"getVarInfo()": NOTHING, "getMode(2)": RADIAN, f"expand(({a})-({b}))": (True, "|x|−x")}
    for p in st.spot_points(["x"], f"{a}\n{b}"):
        x = abs(float(p["x"]))
        replies[st.spot_command(a, b, p)] = (True, f"{{{x:g},{x:g},0.}}")
    use(monkeypatch, FakeBridge(replies))
    out = run(server.nspire_check_steps([a, b], latex=False))
    assert out["steps"][0]["verdict"] == "probably_ok" and out["all_ok"] is False and out["first_broken"] is None
    assert out["summary"].startswith("No step is wrong. Probably ok") and "not proven" in out["summary"]
    use(monkeypatch, FakeBridge(replies))
    out = run(server.nspire_check_steps([a, b], latex=False, spot_check=False))
    assert out["steps"][0]["verdict"] == "undecided" and "spot_check" not in out["steps"][0]


def test_check_steps_bad_input_never_reaches_the_bridge(monkeypatch):
    fake = use(monkeypatch, FakeBridge({}))
    for lines in (["a:=5", "a"], ["x"]):
        with pytest.raises(ValueError):
            run(server.nspire_check_steps(lines, latex=False))
    assert fake.calls == [] and fake.sessions == 0


def test_check_steps_timeout_becomes_a_warning(monkeypatch):
    class B(FakeBridge):
        def eval_batch(self, exprs, timeout_s=5.0, raw=False):
            if exprs[0].startswith("solve"):
                self.last_timeout = "No reply from TI-Nspire."
                return [{"expr": e, "ok": False, "error": "skipped: No reply from TI-Nspire."} for e in exprs]
            self.last_timeout = None
            return super().eval_batch(exprs, timeout_s, raw)
    use(monkeypatch, B({"getVarInfo()": NOTHING, "getMode(2)": RADIAN}))
    out = run(server.nspire_check_steps(["x^2=1", "x=1"], latex=False))
    assert [s["verdict"] for s in out["steps"]] == ["undecided"]
    assert any("stopped answering" in w for w in out["warnings"])


# ------------------------------------------------------------ prompts

def test_check_solution_prompt():
    prompts = {p.name: p for p in run(server.mcp.list_prompts())}
    p = prompts["check_solution"]
    assert {a.name: a.required for a in p.arguments} == {"problem": True, "solution": True}
    got = run(server.mcp.get_prompt("check_solution", {"problem": "Solve x^2=4 {real}",
                                                       "solution": "x=2"}))
    text = got.messages[0].content.text
    assert "Solve x^2=4 {real}" in text and "x=2" in text
    assert "nspire_eval_batch" in text and "isolated=True" in text and "nspire_check_steps" in text
# ------------------------------------------------------------ nspire_plot

_SEQ_RE = re.compile(r"^seq\(approx\((.*)\),(\w+),([^,]+),([^,]+),([^,]+)\)$")


def _cas_number(v: float) -> str:
    return f"{v:.6g}".replace("e", nb.NS_EXP).replace("-", "−")


class SeqBridge(FakeBridge):
    """Answers seq(approx(f),x,a,b,h) like the CAS: funcs maps f to a Python function. A
    point where it raises fails the whole seq with error 800 (as a non-real result does in
    Real mode); None is undef, a str is returned as written. More than max_len points is
    error 672. The first dead_rounds seq batches time out."""

    def __init__(self, funcs: dict, replies: dict | None = None, max_len: int = 10_000, dead_rounds: int = 0):
        super().__init__(replies or {})
        self.funcs, self.max_len, self.dead_rounds, self.rounds = funcs, max_len, dead_rounds, 0

    def eval_batch(self, exprs, timeout_s=5.0, raw=False):
        if not any(_SEQ_RE.match(e) for e in exprs):
            return super().eval_batch(exprs, timeout_s, raw)
        self.batches.append(list(exprs))
        self.rounds += 1
        if self.rounds <= self.dead_rounds:
            self.last_timeout = dead = f"No reply from the TI-Nspire bridge within {timeout_s:g} s."
            return [{"expr": e, "ok": False, "error": dead if k == 0 else "skipped: " + dead}
                    for k, e in enumerate(exprs)]
        self.last_timeout = None
        return [self._seq(e) for e in exprs]

    def _seq(self, expr: str) -> dict:
        self.calls.append(expr)
        f, _var, a, b, h = _SEQ_RE.match(expr).groups()
        if f not in self.funcs:
            return {"expr": expr, "ok": False, "error": "error 910 (syntax error)"}
        a, b, h = float(a), float(b), float(h)
        xs = [a + k * h for k in range(int((b - a) / h + 1e-9) + 1)]
        if len(xs) > self.max_len:
            return {"expr": expr, "ok": False, "error": "error 672 (resource exhaustion)"}
        out = []
        for x in xs:
            try:
                v = self.funcs[f](x)
            except (ValueError, ZeroDivisionError):
                return {"expr": expr, "ok": False, "error": "error 800 (non-real result)"}
            out.append("undef" if v is None else v if isinstance(v, str) else _cas_number(v))
        return {"expr": expr, "ok": True, "result": "{" + ",".join(out) + "}"}


def plot(monkeypatch, fake, **kw):
    monkeypatch.setattr(server, "bridge", fake)
    return run(server.nspire_plot(**kw))


def text_of(res) -> str:
    return res.content[0].text


def test_plot_schema():
    schema = tools()["nspire_plot"].inputSchema
    props = schema["properties"]
    assert set(props) == {"exprs", "latex", "var", "x_min", "x_max", "y_min", "y_max", "samples", "timeout_s",
                          "kind", "t_min", "t_max", "annotate"}
    # defaults depend on the kind (function: x, -10..10, 200 samples), so they are None here
    assert props["var"]["default"] is None and props["samples"]["default"] is None
    assert props["x_min"]["default"] is None and props["kind"]["default"] == "function"
    assert set(props["kind"]["enum"]) == {"function", "parametric", "polar", "implicit"}
    assert not schema.get("required")


def test_plot_one_request_for_all_functions(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = SeqBridge({"sin(x)": math.sin, "x^2-2": lambda x: x * x - 2},
                     {"getVarInfo()": NOTHING, "getMode(2)": RADIAN})
    res = plot(monkeypatch, fake, exprs=["sin(x)", "y=x^2-2"], x_min=-3, x_max=3, samples=50)
    assert fake.sessions == 1  # clipboard saved and restored once
    assert fake.batches[0] == ["getVarInfo()", "getMode(2)"]
    assert len(fake.batches) == 2 and len(fake.batches[1]) == 2  # both functions in one batch
    assert fake.batches[1][1].startswith("seq(approx(x^2-2),x,-3.0,")
    text = text_of(res)
    assert "Plotted 2 functions for x from −3 to 3: 50 points each" in text
    assert "2. y=x^2-2  (TI-Nspire: x^2-2): y from −1.99625 to 7." in text
    assert "Warnings" not in text
    img = res.content[1]
    assert img.type == "image" and img.mimeType == "image/png"
    assert base64.b64decode(img.data).startswith(b"\x89PNG")


def test_plot_via_call_tool(monkeypatch):
    pytest.importorskip("matplotlib")
    monkeypatch.setattr(server, "bridge", SeqBridge({"x": lambda x: x}))
    out = run(server.mcp.call_tool("nspire_plot", {"exprs": ["x"], "samples": 20}))
    blocks = out.content if hasattr(out, "content") else out[0] if isinstance(out, tuple) else out
    assert [b.type for b in blocks] == ["text", "image"]


def test_plot_long_seq_falls_back_to_pieces():
    import nspire_plot as pl
    fake = SeqBridge({"x^3": lambda x: x ** 3}, max_len=60)
    spec = pl.prepare([pl.Func("x^3", "x^3")], "x", -2, 2, 200)
    got = pl.sample(fake, spec, 5)
    assert got.requests == 2 and len(fake.batches[1]) == pl.SPLIT
    assert all(v is not None for v in got.ys[0])
    for i in (0, 57, 199):
        assert got.ys[0][i] == pytest.approx(spec.grid.x(i) ** 3, rel=1e-5, abs=1e-5)
    assert not got.errors[0]


def test_plot_domain_errors_become_gaps():
    import nspire_plot as pl
    fake = SeqBridge({"√(x)": math.sqrt})
    spec = pl.prepare([pl.Func("√(x)", "√(x)")], "x", -5, 5, 200)
    got = pl.sample(fake, spec, 5)
    neg = [i for i in range(200) if spec.grid.x(i) < 0]
    assert all(got.ys[0][i] is None for i in neg)
    assert all(got.ys[0][i] is not None for i in range(200) if i not in neg)
    assert got.errors[0] == {"error 800 (non-real result)": len(neg)}
    assert got.requests <= 6  # split down to single points in a few rounds, not one request per point


def test_plot_undef_points_are_gaps(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = SeqBridge({"1/x": lambda x: None if abs(x) < 1e-12 else 1 / x})
    text = text_of(plot(monkeypatch, fake, exprs=["1/x"], x_min=-1, x_max=1, samples=21))
    assert "undefined or non-real at 1 of 21 points (x≈0)" in text


def test_plot_timeout_retried_once_in_pieces():
    import nspire_plot as pl
    spec = pl.prepare([pl.Func("x", "x")], "x", 0, 1, 40)
    fake = SeqBridge({"x": lambda x: x}, dead_rounds=1)
    got = pl.sample(fake, spec, 5)
    assert got.requests == 2 and len(fake.batches[1]) == pl.SPLIT
    assert all(v is not None for v in got.ys[0])
    fake = SeqBridge({"x": lambda x: x}, dead_rounds=2)
    with pytest.raises(nb.BridgeTimeout, match="fewer samples"):
        pl.sample(fake, spec, 5)


def test_plot_syntax_error_fails_at_once(monkeypatch):
    fake = SeqBridge({})
    with pytest.raises(ValueError, match="syntax error"):
        plot(monkeypatch, fake, exprs=["sin(x"])
    assert len(fake.batches) == 2  # the warnings check, then one seq: no splitting


def test_plot_free_variable_is_an_error(monkeypatch):
    fake = SeqBridge({"a*x": lambda x: f"{_cas_number(x)}*a"})
    with pytest.raises(ValueError, match="needs a value: store it first"):
        plot(monkeypatch, fake, exprs=["a*x"])


def test_plot_nothing_real_is_an_error(monkeypatch):
    fake = SeqBridge({"ln(x)": math.log})
    with pytest.raises(ValueError, match="no real value anywhere"):
        plot(monkeypatch, fake, exprs=["ln(x)"], x_min=-5, x_max=-1, samples=10)


def test_plot_bad_arguments_never_reach_the_bridge(monkeypatch):
    for kw in ({}, {"exprs": ["x"], "x_min": 1, "x_max": 1}, {"exprs": ["a:=2"]},
               {"exprs": ["x"] * 9}, {"exprs": ["x"], "var": "1x"}):
        fake = SeqBridge({})
        with pytest.raises(ValueError):
            plot(monkeypatch, fake, **kw)
        assert fake.calls == [] and fake.sessions == 0


def test_plot_warns_about_stored_variable_but_not_the_plot_variable(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = SeqBridge({"a*x^2": lambda x: 2 * x * x},
                     {"getVarInfo()": (True, '[["a","NUM","none",0]["x","NUM","none",0]]'),
                      "getMode(2)": DEGREE, "a": (True, "2")})
    text = text_of(plot(monkeypatch, fake, exprs=["a*x^2"], samples=20))
    assert "- a is stored in the document (a = 2)" in text
    assert "x is stored" not in text  # seq binds x itself
    assert "angle mode" not in text   # no trig
    assert fake.batches[1] == ["a"]


def test_plot_warns_about_degree_mode(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = SeqBridge({"sin(x)": lambda x: math.sin(math.radians(x))},
                     {"getVarInfo()": NOTHING, "getMode(2)": DEGREE})
    text = text_of(plot(monkeypatch, fake, exprs=["sin(x)"], x_min=0, x_max=360, samples=37))
    assert "angle mode is Degree" in text


def test_plot_annotation_names_match():
    import nspire_plot as pl
    assert list(get_args(server.PlotAnnotation)) == list(pl.ANNOTATIONS)
    items = tools()["nspire_plot"].inputSchema["properties"]["annotate"]["anyOf"][0]["items"]
    assert items["enum"] == list(pl.ANNOTATIONS)


_CUBIC_ANNOT = {
    "zeros((x^3-3*x),x)|x>=-3.0 and x<=3.0": (True, "{−√(3),0,√(3)}"),
    "zeros(derivative((x^3-3*x),x),x)|x>=-3.0 and x<=3.0": (True, "{−1,1}"),
    "{(x^3-3*x),approx(x),approx((x^3-3*x))}|x=(−√(3))": (True, "{0,−1.7320508075689,0.}"),
    "{(x^3-3*x),approx(x),approx((x^3-3*x))}|x=(0)": (True, "{0,0.,0.}"),
    "{(x^3-3*x),approx(x),approx((x^3-3*x))}|x=(√(3))": (True, "{0,1.7320508075689,0.}"),
    "{(x^3-3*x),approx(x),approx((x^3-3*x)),approx(derivative((x^3-3*x),x,2))}|x=(−1)": (True, "{2,−1.,2.,−6.}"),
    "{(x^3-3*x),approx(x),approx((x^3-3*x)),approx(derivative((x^3-3*x),x,2))}|x=(1)": (True, "{−2,1.,−2.,6.}"),
    "approx(derivative((x^3-3*x),x)|x=(−1)-0.025)": (True, "0.3"),
    "approx(derivative((x^3-3*x),x)|x=(−1)+0.025)": (True, "−0.3"),
    "approx(derivative((x^3-3*x),x)|x=(1)-0.025)": (True, "−0.3"),
    "approx(derivative((x^3-3*x),x)|x=(1)+0.025)": (True, "0.3"),
}


def test_plot_annotate_zeros_and_extrema(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = SeqBridge({"x^3-3*x": lambda x: x ** 3 - 3 * x},
                     {"getVarInfo()": NOTHING, "getMode(2)": RADIAN, **_CUBIC_ANNOT})
    res = plot(monkeypatch, fake, exprs=["x^3-3*x"], x_min=-3, x_max=3, samples=61,
               annotate=["zeros", "extrema"])
    assert fake.sessions == 1
    # warnings check, sampling, then zeros() for both, then every point's values in one batch
    assert len(fake.batches) == 4 and len(fake.batches[2]) == 2 and len(fake.batches[3]) == 9
    text = text_of(res)
    assert "- zeros of 1: x = −√(3) ≈ −1.73205; x = 0; x = √(3) ≈ 1.73205" in text
    assert "- maximum of 1: (−1, 2)" in text and "- minimum of 1: (1, −2)" in text
    assert "Warnings" not in text
    marks = res.structuredContent["marks"]
    assert [m["type"] for m in marks] == ["zero", "zero", "zero", "max", "min"]
    assert marks[0] == {"type": "zero", "functions": [1], "x": "−√(3)", "y": "0",
                        "x_approx": pytest.approx(-1.7320508075689), "y_approx": 0.0, "method": "exact"}
    assert base64.b64decode(res.content[1].data).startswith(b"\x89PNG")


def test_plot_without_annotate_has_no_marks(monkeypatch):
    pytest.importorskip("matplotlib")
    res = plot(monkeypatch, SeqBridge({"x": lambda x: x}), exprs=["x"], samples=20)
    assert res.structuredContent["marks"] is None


def test_plot_annotate_errors_are_explained_and_the_plot_kept(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = SeqBridge({"x^2+1": lambda x: x * x + 1},
                     {"zeros((x^2+1),x)|x>=-2.0 and x<=2.0": (False, "error 800 (Non-real result)")})
    res = plot(monkeypatch, fake, exprs=["x^2+1"], x_min=-2, x_max=2, samples=20, annotate=["zeros"])
    text = text_of(res)
    assert "zeros of 1 (x^2+1): error 800 (Non-real result); none marked." in text
    assert "800" in " ".join(res.structuredContent["warnings"]) and res.structuredContent["marks"] == []
    assert res.content[1].type == "image"

    class Broken(SeqBridge):
        def eval_batch(self, exprs, timeout_s=5.0, raw=False):
            if exprs[0].startswith("zeros("):
                raise RuntimeError("bridge fell over")
            return super().eval_batch(exprs, timeout_s, raw)
    res = plot(monkeypatch, Broken({"x": lambda x: x}), exprs=["x"], samples=20, annotate=["zeros"])
    assert "couldn't be found (bridge fell over)" in text_of(res)


def test_plot_annotate_refused_for_curves_before_the_bridge(monkeypatch):
    fake = SeqBridge({})
    with pytest.raises(ValueError, match="function plots"):
        plot(monkeypatch, fake, kind="polar", exprs=["1+cos(θ)"], annotate=["zeros"])
    assert fake.calls == [] and fake.sessions == 0


def test_plot_latex_input(monkeypatch):
    pytest.importorskip("matplotlib")
    import nspire_latex
    cmd = nspire_latex.plan(r"\frac{1}{x^2+1}", "eval").command
    fake = SeqBridge({cmd: lambda x: 1 / (x * x + 1)})
    text = text_of(plot(monkeypatch, fake, latex=[r"\frac{1}{x^2+1}"], samples=21))
    assert f"1. \\frac{{1}}{{x^2+1}}  (TI-Nspire: {cmd}): y from 0.00990099 to 1" in text
    with pytest.raises(ValueError, match="frac"):
        plot(monkeypatch, SeqBridge({}), latex=[r"\frac{1}"])


_HELPER_CALL_RE = re.compile(r"^(mcpplotp\d+)\(([^,]+),([^,]+),([^,]+)\)$")
_POLAR_DEF_RE = re.compile(r"^Define (mcpplotp\d+)\(mcpa,mcpb,mcph\)=Func:Local mcpm,(\w+):mcpm:=setMode\(2,1\)"
                           r":Return seq\(approx\(\((.*)\)\*(cos|sin)\(\w+\)\),\w+,mcpa,mcpb,mcph\):EndFunc$")
_IMPLICIT_DEF_RE = re.compile(r"^Define (mcpplotf\d+)\(x,y\)=(.*)$")
_IMPLICIT_CALL_RE = re.compile(r"^(mcpplotf\d+)\(x,([^,]+)\)$")


class CurveBridge(SeqBridge):
    """SeqBridge plus the temporary functions: polar maps r (as written) to r(θ), implicit
    maps F (as defined, e.g. '(x^2+y^2)-(25)') to F(x, y). Defines and DelVars are recorded."""

    def __init__(self, funcs: dict | None = None, polar: dict | None = None, implicit: dict | None = None,
                 replies: dict | None = None, **kw):
        super().__init__(dict(funcs or {}), replies, **kw)
        self.polar, self.implicit = polar or {}, implicit or {}
        self.defined: dict[str, Any] = {}
        self.deleted: list[str] = []

    def eval_batch(self, exprs, timeout_s=5.0, raw=False):
        if not any(e.startswith(("Define mcpplot", "DelVar mcpplot")) or _HELPER_CALL_RE.match(e) for e in exprs):
            return super().eval_batch(exprs, timeout_s, raw)
        self.batches.append(list(exprs))
        self.last_timeout = None
        return [self._item(e) for e in exprs]

    def _item(self, e: str) -> dict:
        self.calls.append(e)
        if e.startswith("DelVar "):
            self.deleted += e[len("DelVar "):].split(",")
            return {"expr": e, "ok": True, "result": "Done"}
        m = _POLAR_DEF_RE.match(e)
        if m:
            name, _var, r, trig = m.groups()
            if r not in self.polar:
                return {"expr": e, "ok": False, "error": "error 910 (syntax error)"}
            f, g = self.polar[r], math.cos if trig == "cos" else math.sin
            self.defined[name] = lambda t, f=f, g=g: f(t) * g(t)
            return {"expr": e, "ok": True, "result": "Done"}
        m = _IMPLICIT_DEF_RE.match(e)
        if m:
            name, F = m.groups()
            if F not in self.implicit:
                return {"expr": e, "ok": False, "error": "error 910 (syntax error)"}
            self.defined[name] = self.implicit[F]
            # rows become ordinary seq channels: mcpplotf1(x,Y) -> F(x, Y)
            return {"expr": e, "ok": True, "result": "Done"}
        m = _HELPER_CALL_RE.match(e)
        if m:
            name, a, b, h = m.groups()
            self.funcs[e] = self.defined[name]  # reuse SeqBridge's seq answer over θ
            return self._seq(f"seq(approx({e}),θ,{a},{b},{h})")
        return {"expr": e, "ok": False, "error": "error 910 (syntax error)"}

    def _seq(self, expr: str) -> dict:
        m = _SEQ_RE.match(expr)
        if m:
            call = _IMPLICIT_CALL_RE.match(m.group(1))
            if call and call.group(1) in self.defined and m.group(1) not in self.funcs:
                F, y = self.defined[call.group(1)], float(call.group(2))
                self.funcs[m.group(1)] = lambda x, F=F, y=y: F(x, y)
        return super()._seq(expr)


def test_plot_parametric_samples_x_and_y_in_one_batch(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = CurveBridge({"cos(t)": math.cos, "sin(2*t)": lambda t: math.sin(2 * t)},
                       replies={"getVarInfo()": NOTHING, "getMode(2)": RADIAN})
    res = plot(monkeypatch, fake, kind="parametric", exprs=["x=cos(t), y=sin(2*t)"], samples=50)
    assert fake.sessions == 1
    assert len(fake.batches) == 2 and len(fake.batches[1]) == 2  # x and y in one batch
    assert fake.batches[1][0].startswith("seq(approx(cos(t)),t,0.0,")
    text = text_of(res)
    assert "Plotted 1 parametric curve for t from 0 to 6.28319: 50 points each" in text
    assert "x from −0.997945 to 1, y from −0.999486 to 0.999486." in text
    out = res.structuredContent
    assert out["kind"] == "parametric" and out["var"] == "t" and out["samples"] == 50
    assert out["curves"][0]["nspire"] == ["cos(t)", "sin(2*t)"] and out["curves"][0]["points"] == 50
    assert out["warnings"] is None
    assert base64.b64decode(res.content[1].data).startswith(b"\x89PNG")


def test_plot_parametric_split_and_gaps(monkeypatch):
    pytest.importorskip("matplotlib")
    # √ fails for t<0 (error 800 for the whole seq): split down to single points, which become gaps
    fake = CurveBridge({"t": lambda t: t, "√(t)": math.sqrt}, max_len=30)
    text = text_of(plot(monkeypatch, fake, kind="parametric", exprs=["t, √(t)"], t_min=-1, t_max=1, samples=41))
    assert "undefined or non-real at 20 of 41 points (t in [−1, −0.05]" in text
    assert len(fake.batches) > 3  # split


def test_plot_polar_runs_in_radian_helpers_and_deletes_them(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = CurveBridge(polar={"1+cos(θ)": lambda t: 1 + math.cos(t)},
                       replies={"getVarInfo()": NOTHING, "getMode(2)": DEGREE})
    res = plot(monkeypatch, fake, kind="polar", exprs=["r=1+cos(θ)"], samples=100)
    assert fake.batches[1][0].startswith("Define mcpplotp1(mcpa,mcpb,mcph)=Func:Local mcpm,θ:mcpm:=setMode(2,1)")
    assert fake.batches[2] == [b for b in fake.batches[2] if b.startswith("mcpplotp")] and len(fake.batches[2]) == 2
    assert fake.batches[-1] == ["DelVar mcpplotp1,mcpplotp2"] and fake.deleted == ["mcpplotp1", "mcpplotp2"]
    text = text_of(res)
    assert "angle mode" not in text  # degree document, but polar is always radians
    assert "θ is in radians" in text
    assert "x from −0.25 to 2, y from −1.29" in text  # the cardioid
    assert res.structuredContent["var"] == "θ"


def test_plot_polar_error_still_deletes_helpers(monkeypatch):
    fake = CurveBridge(polar={}, replies={"getVarInfo()": NOTHING, "getMode(2)": RADIAN})
    with pytest.raises(ValueError, match="syntax error"):
        plot(monkeypatch, fake, kind="polar", exprs=["sin(θ"])
    assert fake.deleted == ["mcpplotp1", "mcpplotp2"]


def test_plot_implicit_grid_rows(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = CurveBridge(implicit={"(x^2+y^2)-(25)": lambda x, y: x * x + y * y - 25},
                       replies={"getVarInfo()": (True, '[["y","NUM","none",0]]'), "getMode(2)": RADIAN})
    res = plot(monkeypatch, fake, kind="implicit", exprs=["x^2+y^2=25"], samples=21)
    assert fake.batches[1] == ["Define mcpplotf1(x,y)=(x^2+y^2)-(25)"]
    rows = fake.batches[2]
    assert len(rows) == 21 and rows[0].startswith("seq(approx(mcpplotf1(x,-10.0)),x,-10.0,")
    assert fake.deleted == ["mcpplotf1"]
    text = text_of(res)
    assert "on a 21×21 grid" in text and "passes through" in text
    assert "y is stored" not in text  # x and y are the helper's own parameters
    out = res.structuredContent
    assert out["var"] == "x,y" and out["curves"][0]["total"] == 441
    assert out["curves"][0]["x_range"] == [-6.0, 6.0]


def test_plot_implicit_latex(monkeypatch):
    pytest.importorskip("matplotlib")
    import nspire_latex
    cmd = nspire_latex.plan("y^2=x^3-x", "eval").command
    F = f"({cmd.split('=')[0]})-({cmd.split('=')[1]})"
    fake = CurveBridge(implicit={F: lambda x, y: y * y - x ** 3 + x})
    text = text_of(plot(monkeypatch, fake, kind="implicit", latex=["y^2=x^3-x"], samples=15,
                        x_min=-2, x_max=3, y_min=-3, y_max=3))
    assert "1. y^2=x^3-x: passes through" in text  # same as the TI-Nspire command, so not repeated


def test_plot_parametric_latex(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = CurveBridge({"cos(t)": math.cos, "sin(t)": math.sin})
    text = text_of(plot(monkeypatch, fake, kind="parametric", latex=[r"\left(\cos t, \sin t\right)"], samples=20))
    assert "parametric curve" in text and "(TI-Nspire: (cos(t), sin(t)))" in text


def test_plot_kinds_through_the_handler(monkeypatch):
    pytest.importorskip("matplotlib")
    monkeypatch.setattr(server, "bridge", CurveBridge({"x": lambda x: x}, polar={"2": lambda t: 2.0}))
    for args in ({"exprs": ["x"], "samples": 20}, {"kind": "polar", "exprs": ["2"], "samples": 20}):
        r = call("nspire_plot", args)
        assert not r.isError, r.content
        assert [b.type for b in r.content] == ["text", "image"] and r.structuredContent["curves"]


# ------------------------------------------------------------ error explanations

def test_eval_error_gets_an_explanation(monkeypatch):
    use(monkeypatch, FakeBridge({"√(−4)": (False, "error 800 (non-real result)")}))
    with pytest.raises(ValueError) as e:
        run(server.nspire_eval("√(−4)"))
    msg = str(e.value)
    assert msg.startswith("TI-Nspire error: error 800 (non-real result) Error 800")
    assert msg.count("Error 800") == 1
    use(monkeypatch, FakeBridge({}))
    with pytest.raises(ValueError, match="Error 910 .*syntax"):
        run(server.nspire_eval("2+"))


def test_eval_error_without_a_known_code_is_unchanged(monkeypatch):
    use(monkeypatch, FakeBridge({"x": (False, "error 99999 (odd)")}))
    with pytest.raises(ValueError) as e:
        run(server.nspire_eval("x"))
    assert str(e.value) == "TI-Nspire error: error 99999 (odd)"


def test_batch_explains_only_failed_items(monkeypatch):
    use(monkeypatch, FakeBridge({"1": (True, "1"), "a": (False, "error 800 (non-real result)")}))
    r = run(server.nspire_eval_batch(["1", "a", "b"]))
    assert "warnings" not in r[0]
    assert r[1]["warnings"] == [ne.explain(800)]
    assert r[2]["warnings"] == [ne.explain(910)]


def test_latex_error_gets_an_explanation(monkeypatch):
    fake = FakeBridge({"getVarInfo()": NOTHING, "getMode(2)": RADIAN})
    use(monkeypatch, fake)
    import nspire_latex
    cmd = nspire_latex.plan(r"\sqrt{-4}", "eval").command
    fake.replies[cmd] = (False, "error 800 (non-real result)")
    with pytest.raises(ValueError, match="Error 800 .*non-real"):
        run(server.nspire_latex(r"\sqrt{-4}", action="eval"))


def test_check_steps_explains_cas_errors(monkeypatch):
    use(monkeypatch, FakeBridge({
        "getVarInfo()": NOTHING, "getMode(2)": RADIAN,
        "expand((√(−4))-(2*𝑖))": (False, "error 800 (non-real result)"),
    }))
    out = run(server.nspire_check_steps(["√(−4)", "2*𝑖"], latex=False, spot_check=False))
    assert out["steps"][0]["verdict"] == "undecided"
    assert [w for w in out["warnings"] if w.startswith("Error 800")] == [ne.explain(800)]


def test_plot_fatal_error_gets_an_explanation(monkeypatch):
    with pytest.raises(ValueError, match="Error 910 .*syntax"):
        plot(monkeypatch, SeqBridge({}), exprs=["sin(x"])


def test_plot_all_gaps_summary_explains_the_cause(monkeypatch):
    fake = SeqBridge({"ln(x)": math.log})
    with pytest.raises(ValueError) as e:
        plot(monkeypatch, fake, exprs=["ln(x)"], x_min=-5, x_max=-1, samples=10)
    assert ne.explain(800) in str(e.value)


def test_instructions_mention_explanations():
    assert "nspire_errors" in server.mcp.instructions


# ------------------------------------------------------------ nspire_stats

QUAD_RESULTS = ('[["Title","Quadratic Regression"]["RegEqn","a*x^2+b*x+c"]["a",1.25]["b",−0.95]["c",0.75]'
                '["R²",0.99965928449744]["Resid","{...}"]]')
LIN_RESULTS = ('[["Title","Linear Regression (mx+b)"]["RegEqn","m*x+b"]["m",1.9]["b",0.]["r²",0.96266666666667]'
               '["r",0.98115578103921]["Resid","{...}"]]')
_LINE_SEQ_RE = re.compile(r"seq\(approx\(1\.9\*x\+0\.\),x,([^,]+),([^,]+),([^,]+)\)")


class StatsBridge(CasBridge):
    """CasBridge (stores, isolation) plus the statistics commands: commands maps a command to
    (stat.results, stat.RegEqn(mcpstatv)), or to an error text; the command checks its lists
    are stored. seq() of the fitted line 1.9*x+0. is answered like the CAS would."""

    def __init__(self, commands: dict, **kw):
        super().__init__(**kw)
        self.commands = commands
        self.current: str | None = None

    def _reply(self, expr):
        if expr in self.commands:
            self.calls.append(expr)
            if isinstance(self.commands[expr], str):  # an error
                return False, self.commands[expr]
            lists = expr.split(" ", 1)[1].split(",")
            if any(n not in self.vars for n in lists):
                return False, "error 960 (variable is not defined)"
            self.current = expr
            return True, ""
        if expr == "stat.results" and self.current:
            self.calls.append(expr)
            return True, self.commands[self.current][0]
        if expr == "stat.RegEqn(mcpstatv)" and self.current:
            self.calls.append(expr)
            return True, self.commands[self.current][1]
        if m := _LINE_SEQ_RE.fullmatch(expr):
            self.calls.append(expr)
            a, b, h = map(float, m.groups())
            xs = [a + k * h for k in range(int((b - a) / h + 1e-9) + 1)]
            return True, "{" + ",".join(_cas_number(1.9 * x) for x in xs) + "}"
        return super()._reply(expr)


def test_stats_schema():
    import nspire_stats
    assert list(get_args(server.StatCommand)) == list(nspire_stats.COMMANDS)
    t = tools()["nspire_stats"]
    props = t.inputSchema["properties"]
    assert props["command"]["enum"] == list(nspire_stats.COMMANDS)
    assert t.inputSchema["required"] == ["command"]
    assert {"csv", "columns", "headers", "x_column", "y_column", "freq_column", "plot", "timeout_s"} <= set(props)
    out = t.outputSchema["properties"]
    assert {"command", "nspire", "title", "n", "columns", "results", "equation", "r2", "r", "warnings"} <= set(out)


def test_stats_regression_through_the_handler(monkeypatch):
    fake = use(monkeypatch, StatsBridge({"QuadReg mcpstatx,mcpstaty": (QUAD_RESULTS,
                                                                       "1.25*mcpstatv^2-0.95*mcpstatv+0.75")}))
    r = call("nspire_stats", {"command": "QuadReg", "csv": "t;h\n1;1\n2;4\n3;9\n4;17\n"})
    assert not r.isError, r.content
    out = r.structuredContent
    assert out["equation"] == "1.25*x^2-0.95*x+0.75" and out["r2"] == "0.99965928449744" and out["r"] is None
    assert out["columns"] == ["t", "h"] and out["n"] == 4 and out["warnings"] is None
    assert {"name": "b", "value": "−0.95"} in out["results"]
    assert r.content[0].text.startswith("Quadratic Regression (QuadReg mcpstatx,mcpstaty): 4 rows of t and h")
    assert fake.sessions == 1
    assert fake.batches[-1] == ["mcpstatx:={1,2,3,4}", "mcpstaty:={1,4,9,17}", "QuadReg mcpstatx,mcpstaty",
                                "stat.results", "stat.RegEqn(mcpstatv)"]
    assert fake.vars == {}  # the lists were deleted again


def test_stats_puts_back_a_stored_list_name_and_explains_errors(monkeypatch):
    fake = use(monkeypatch, StatsBridge({"CubicReg mcpstatx,mcpstaty": "error 235 (dimension error: not enough "
                                         "elements in the lists)"}, stored={"mcpstatx": ("NUM", "7")}))
    with pytest.raises(ValueError, match="error 235") as e:
        run(server.nspire_stats("CubicReg", columns=[[1, 2, 3], [1, 2, 3]]))
    assert "too few data points" in str(e.value)
    assert fake.vars == {"mcpstatx": ("NUM", "7")}


def test_stats_onevar_with_frequencies(monkeypatch):
    onevar = '[["Title","One-Variable Statistics"]["",2.25]["n",4.]]'
    fake = use(monkeypatch, StatsBridge({"OneVar mcpstatx,mcpstatf": (onevar, "")}))
    out = run(server.nspire_stats("OneVar", columns=[[1, 2, 3], [1, 2, 1]], headers=["score", "count"],
                                  freq_column="count")).structuredContent
    assert out["results"] == [{"name": "x̄", "value": "2.25"}, {"name": "n", "value": "4."}]
    assert out["frequencies"] == "count" and "equation" not in out
    assert "stat.RegEqn(mcpstatv)" not in fake.calls


def test_stats_bad_data_never_reaches_the_bridge(monkeypatch):
    fake = use(monkeypatch, StatsBridge({}))
    for command, kw in (("LinRegMx", {"csv": "1,2\n3"}), ("LinRegMx", {"columns": [[1, 2], [3]]}),
                        ("LinRegMx", {"csv": "1,a"}), ("LinRegMx", {}), ("OneVar", {"csv": "1\n2", "plot": True}),
                        ("LinRegMx", {"columns": [[1, 2]]})):
        with pytest.raises(ValueError):
            run(server.nspire_stats(command, **kw))
    assert fake.calls == [] and fake.sessions == 0


def test_stats_plot(monkeypatch):
    pytest.importorskip("matplotlib")
    fake = use(monkeypatch, StatsBridge({"LinRegMx mcpstatx,mcpstaty": (LIN_RESULTS, "1.9*mcpstatv+0.")}))
    r = call("nspire_stats", {"command": "LinRegMx", "columns": [[1, 2, 3, 4], [2, 4, 5, 8]], "plot": True})
    assert not r.isError, r.content
    out = r.structuredContent
    assert out["equation"] == "1.9*x+0." and out["r"] == "0.98115578103921" and out["r2"] == "0.96266666666667"
    assert out["plot"].startswith("1. y = 1.9*x+0.  (TI-Nspire: 1.9*x+0.): y from")
    assert fake.sessions == 1 and fake.batches[-1][0].startswith("seq(approx(1.9*x+0.),x,0.85,")
    img = r.content[1]
    assert img.type == "image" and base64.b64decode(img.data).startswith(b"\x89PNG")
    assert "Plot: 1. y = 1.9*x+0." in r.content[0].text


def test_stats_twovar_plot_shows_only_the_data(monkeypatch):
    pytest.importorskip("matplotlib")
    twovar = '[["Title","Two-Variable Statistics"]["r",0.9]]'
    fake = use(monkeypatch, StatsBridge({"TwoVar mcpstatx,mcpstaty": (twovar, "")}))
    r = call("nspire_stats", {"command": "TwoVar", "csv": "1 2\n2 3\n3 5", "plot": True})
    assert not r.isError, r.content
    assert r.structuredContent["plot"].startswith("The data points only")
    assert len(r.content) == 2 and not any(c.startswith("seq(") for c in fake.calls)
