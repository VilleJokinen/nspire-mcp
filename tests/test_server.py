"""Offline tests of server.py: tool schemas and annotations, the nspire_latex
document warnings, and nspire_notes cancellation. A fake bridge answers instead
of TI-Nspire; the real clipboard is never touched.

Skipped by tests/conftest.py where pywin32 or mcp is missing.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import subprocess
import sys
import threading
import types
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import nspire_bridge as nb  # noqa: E402

nb.MUTEX_NAME = f"Local\\nspire_mcp_bridge_offline_test_{os.getpid()}"  # not the real bridge's, nor other runs'
import server  # noqa: E402
from mcp import types as mt  # noqa: E402


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

    # the real mode override, on top of this fake's eval_batch
    temp_modes = nb.Bridge.temp_modes
    _apply_modes = nb.Bridge._apply_modes
    _restore_modes = nb.Bridge._restore_modes

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
                       "nspire_latex", "nspire_notes"}
    for t in ts.values():
        assert t.annotations is not None and t.annotations.title
        assert t.annotations.openWorldHint is False
    for name in ("nspire_status", "nspire_vars"):
        assert ts[name].annotations.readOnlyHint is True and ts[name].annotations.idempotentHint is True
    assert ts["nspire_clear"].annotations.destructiveHint is True
    for name in ("nspire_eval", "nspire_eval_batch", "nspire_latex", "nspire_notes", "nspire_clear"):
        assert not ts[name].annotations.readOnlyHint  # they can define or delete things


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
    assert set(step["properties"]) == {"text", "math", "latex", "action", "var"}
    assert not step.get("required")
    assert all(p.get("description") for p in step["properties"].values())
    assert step["properties"]["action"]["enum"] == list(get_args(server.LatexAction))


def test_instructions():
    text = server.mcp.instructions
    for needle in ("nspire_bridge.tns", "clipboard", "nspire_vars", "nspire_clear", "nspire_notes",
                   "isolated=True", "exact_mode"):
        assert needle in text


def test_bad_action_rejected_before_the_bridge(monkeypatch):
    fake = FakeBridge({})
    monkeypatch.setattr(server, "bridge", fake)
    with pytest.raises(Exception):
        run(server.mcp.call_tool("nspire_latex", {"latex": "x^2=4", "action": "bogus"}))
    assert fake.calls == []


def test_startup_imports_stay_light():
    # The client times out a server that's slow to answer initialize: no SymPy, no
    # keystroke module at import. A fresh interpreter, since other tests import them.
    code = ("import sys; sys.path.insert(0, '.'); import server; "
            "print(sorted(m for m in ('sympy', 'nspire_latex', 'nspire_notes', 'nspire_keys') if m in sys.modules))")
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

    def build_notes(bridge, steps, title, page_size, timeout_s, cancel=None):
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

    def build_notes(bridge, steps, title, page_size, timeout_s, cancel=None):
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
    assert props(ts["nspire_latex"].outputSchema) == (
        {"action", "nspire", "result", "result_latex", "approx", "approx_latex", "warnings"},
        {"action", "nspire", "result", "result_latex"})


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
        {"expr": "2+2", "ok": True, "result": "4"}, {"expr": "bad", "ok": False, "error": "error 910 (syntax error)"}]
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
    Define, :=, reading a variable, getMode/setMode. self.modes_seen records the
    angle and auto/approx settings each other expression ran under."""

    def __init__(self, stored=None, modes=None, set_mode="works", undeletable=()):
        super().__init__({})
        self.vars = {k: tuple(v) for k, v in (stored or {}).items()}  # lower name -> (type, value)
        self.modes = dict(modes or {2: 1, 5: 1})
        self.set_mode = set_mode
        self.undeletable = set(undeletable)
        self.modes_seen: dict[str, tuple[int, int]] = {}

    def _reply(self, expr):
        self.calls.append(expr)
        if expr == "getVarInfo()":
            if not self.vars:
                return True, '"NONE"'
            return True, "[" + "".join(f'[{n},"{t}","none",0]' for n, (t, _) in self.vars.items()) + "]"
        if m := re.fullmatch(r"getType\((\w+)\)", expr):
            return True, '"%s"' % self.vars.get(m.group(1).lower(), ("NONE", None))[0]
        if m := re.fullmatch(r"getMode\((\d+)\)", expr):
            return True, str(self.modes.get(int(m.group(1)), 1))
        if m := re.fullmatch(r"setMode\((\d+),(\d+)\)", expr):
            if self.set_mode == "refused":
                return False, "error 430 (invalid for the current mode settings)"
            old = self.modes.get(int(m.group(1)), 1)
            self.modes[int(m.group(1))] = int(m.group(2))
            return True, str(old)
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
    run(server.nspire_eval("sin(30)", angle="degree", exact_mode="approx"))
    assert fake.modes_seen["sin(30)"] == (2, 2) and fake.modes == {2: 1, 5: 1}
    before = len(fake.calls)
    run(server.nspire_eval("sin(31)"))
    assert fake.calls[before:] == ["sin(31)"] and fake.modes_seen["sin(31)"] == (1, 1)
    # put back when the expression fails
    with pytest.raises(ValueError, match="syntax error"):
        run(server.nspire_eval("bad", angle="gradian"))
    assert fake.modes == {2: 1, 5: 1}


def test_mode_override_refused(monkeypatch):
    fake = use(monkeypatch, CasBridge(set_mode="refused"))
    r = call("nspire_eval", {"expr": "sin(30)", "angle": "degree"})
    assert r.isError and "didn't change the mode setting with setMode" in r.content[0].text
    assert "sin(30)" not in fake.calls
    with pytest.raises(Exception):
        call_args = {"expr": "1", "angle": "turns"}
        run(server.mcp.call_tool("nspire_eval", call_args))


def test_isolation_outside_the_mode_change(monkeypatch):
    # values are saved and written back in the document's own modes
    fake = use(monkeypatch, CasBridge({"a": ("NUM", "3")}, modes={2: 1, 5: 1}))
    run(server.nspire_eval("a:=7", isolated=True, exact_mode="approx"))
    order = [c for c in fake.calls if c in ("a", "setMode(5,2)", "a:=7", "setMode(5,1)", "a:=3")]
    assert order == ["a", "setMode(5,2)", "a:=7", "setMode(5,1)", "a:=3"]
    assert fake.modes_seen["a:=3"] == (1, 1) and fake.vars["a"] == ("NUM", "3")


def test_latex_angle_override_skips_angle_warning(monkeypatch):
    fake = use(monkeypatch, CasBridge(modes={2: 2, 5: 1}))
    out = run(server.nspire_latex(r"\sin(x)"))
    assert "angle mode is Degree" in out["warnings"][0]
    out = run(server.nspire_latex(r"\sin(x)", angle="radian"))
    assert "warnings" not in out and fake.modes == {2: 2, 5: 1}
    assert fake.modes_seen["sin(x)"] == (1, 1)


# ------------------------------------------------------------ prompts

def test_check_solution_prompt():
    prompts = {p.name: p for p in run(server.mcp.list_prompts())}
    p = prompts["check_solution"]
    assert {a.name: a.required for a in p.arguments} == {"problem": True, "solution": True}
    got = run(server.mcp.get_prompt("check_solution", {"problem": "Solve x^2=4 {real}",
                                                       "solution": "x=2"}))
    text = got.messages[0].content.text
    assert "Solve x^2=4 {real}" in text and "x=2" in text
    assert "nspire_eval_batch" in text and "isolated=True" in text
