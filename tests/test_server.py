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
from typing import get_args

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import nspire_bridge as nb  # noqa: E402

nb.MUTEX_NAME = "Local\\nspire_mcp_bridge_offline_test"  # don't share the real bridge's mutex
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
                       "nspire_latex", "nspire_notes", "nspire_plot"}
    for t in ts.values():
        assert t.annotations is not None and t.annotations.title
        assert t.annotations.openWorldHint is False
    for name in ("nspire_status", "nspire_vars"):
        assert ts[name].annotations.readOnlyHint is True and ts[name].annotations.idempotentHint is True
    assert ts["nspire_clear"].annotations.destructiveHint is True
    for name in ("nspire_eval", "nspire_eval_batch", "nspire_latex", "nspire_notes", "nspire_clear", "nspire_plot"):
        assert not ts[name].annotations.readOnlyHint  # they can define or delete things, or evaluate in the document
    assert ts["nspire_plot"].annotations.destructiveHint is False


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
    for needle in ("nspire_bridge.tns", "clipboard", "nspire_vars", "nspire_clear", "nspire_notes", "nspire_plot"):
        assert needle in text


def test_bad_action_rejected_before_the_bridge(monkeypatch):
    fake = FakeBridge({})
    monkeypatch.setattr(server, "bridge", fake)
    with pytest.raises(Exception):
        run(server.mcp.call_tool("nspire_latex", {"latex": "x^2=4", "action": "bogus"}))
    assert fake.calls == []


def test_startup_imports_stay_light():
    # The client times out a server that's slow to answer initialize: no SymPy, no
    # matplotlib, no keystroke module at import. A fresh interpreter, since other tests import them.
    code = ("import sys; sys.path.insert(0, '.'); import server; "
            "print(sorted(m for m in ('sympy', 'nspire_latex', 'nspire_notes', 'nspire_keys', 'matplotlib', "
            "'nspire_plot') if m in sys.modules))")
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
    assert set(props) == {"exprs", "latex", "var", "x_min", "x_max", "y_min", "y_max", "samples", "timeout_s"}
    assert props["var"]["default"] == "x" and props["samples"]["default"] == 200
    assert props["x_min"]["default"] == -10 and props["x_max"]["default"] == 10
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


def test_plot_latex_input(monkeypatch):
    pytest.importorskip("matplotlib")
    import nspire_latex
    cmd = nspire_latex.plan(r"\frac{1}{x^2+1}", "eval").command
    fake = SeqBridge({cmd: lambda x: 1 / (x * x + 1)})
    text = text_of(plot(monkeypatch, fake, latex=[r"\frac{1}{x^2+1}"], samples=21))
    assert f"1. \\frac{{1}}{{x^2+1}}  (TI-Nspire: {cmd}): y from 0.00990099 to 1" in text
    with pytest.raises(ValueError, match="frac"):
        plot(monkeypatch, SeqBridge({}), latex=[r"\frac{1}"])
