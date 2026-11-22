"""Offline tests for the nspire_notes extras: progress, the Graphs page and saving.

Nothing real is touched: keys go to a recorder (nspire_keys._send is faked), and the
TI-Nspire window, its Save dialog and the clipboard are simulated.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("win32api")  # nspire_keys needs pywin32

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_notes_safety import FakeBridge, KeyRecorder, NotesGuard  # noqa: E402

import nspire_keys as keys  # noqa: E402
import nspire_notes as notes  # noqa: E402

WIN = NotesGuard.WIN
HWND, DIALOG, BOX, PROMPT = WIN.hwnd, 900, 901, 950


# ------------------------------------------------------------ plan_graphs (no bridge)

def exprs(graphs):
    return [g["expr"] for g in notes.plan_graphs(graphs)]


def test_plan_graphs_tinspire_syntax():
    assert exprs(["x^2-1", "y=2*x", "f(x)=sin(x)", " f1(x) = abs(x) ", "x^2|x>0"]) == \
        ["x^2-1", "2*x", "sin(x)", "abs(x)", "x^2|x>0"]
    g = notes.plan_graphs(["x^2"])[0]
    assert g["cmd"] == f"Define {notes.GRAPH_CHECK_NAME}(x)=x^2" and g["label"] == "graph 1"
    assert notes.plan_graphs(None) == [] and notes.plan_graphs([]) == []


def test_plan_graphs_latex():
    pytest.importorskip("sympy")
    assert exprs([r"$\frac{1}{x}$", r"$y=x^2-1$", r"$$|x|$$"]) == ["1/x", "x^2 - 1", "abs(x)"]


@pytest.mark.parametrize("graphs", [
    "x^2", [5], [""], ["  "], ["x^2\n1"], ["y=x^2=1"], ["x^2=4"], ["x>1"], ["a:=x^2"], ["Define g(x)=x"],
    ["x^2→a"], ["x"] * (notes.MAX_GRAPHS + 1),
])
def test_plan_graphs_rejects(graphs):
    with pytest.raises(ValueError):
        notes.plan_graphs(graphs)


def test_plan_graphs_error_names_the_graph():
    with pytest.raises(ValueError, match="graph 2"):
        notes.plan_graphs(["x", "x=1"])


def test_comparisons_inside_brackets_are_allowed():
    assert exprs(["when(x<0,-x,x)", "piecewise(x,x>0,0,x<=0)"]) == ["when(x<0,-x,x)", "piecewise(x,x>0,0,x<=0)"]


# ------------------------------------------------------------ check_save (no bridge)

def test_check_save_accepts(tmp_path):
    assert notes.check_save(None) is None
    p = tmp_path / "area.tns"
    assert notes.check_save(str(p)) == os.path.normpath(str(p))
    assert notes.check_save(str(tmp_path / "Area.TNS")).endswith("Area.TNS")
    p.write_bytes(b"x")
    assert notes.check_save(str(p), overwrite=True) == os.path.normpath(str(p))


@pytest.mark.parametrize("make", [
    lambda d: "area.tns",                          # not a full path
    lambda d: str(d / "area.txt"),                 # not .tns
    lambda d: str(d / ".tns"),
    lambda d: str(d / "missing" / "area.tns"),     # folder doesn't exist
    lambda d: str(d / "a?b.tns"),
    lambda d: str(d / "a\nb.tns"),
    lambda d: "",
    lambda d: 5,
])
def test_check_save_rejects(tmp_path, make):
    with pytest.raises(ValueError):
        notes.check_save(make(tmp_path))


def test_check_save_refuses_existing_file_and_folder(tmp_path):
    p = tmp_path / "area.tns"
    p.write_bytes(b"x")
    with pytest.raises(ValueError, match="already exists"):
        notes.check_save(str(p))
    (tmp_path / "dir.tns").mkdir()
    with pytest.raises(ValueError, match="folder"):
        notes.check_save(str(tmp_path / "dir.tns"), overwrite=True)
    with pytest.raises(ValueError, match="only applies"):
        notes.check_save(None, overwrite=True)
    with pytest.raises(ValueError):
        notes.check_save(str(tmp_path / "b.tns"), overwrite="yes")


# ------------------------------------------------------------ checking graphs on the bridge

class FailingOn(FakeBridge):
    def __init__(self, bad, **kw):
        super().__init__(**kw)
        self.bad = bad

    def eval_one(self, expr, timeout_s, raw=False):
        if self.bad in expr and expr.startswith("Define"):
            self.calls.append(expr)
            raise ValueError("TI-Nspire error: error 910 (syntax error)")
        return super().eval_one(expr, timeout_s, raw)


def test_graph_check_function_is_deleted_afterwards():
    b = FakeBridge()
    its = notes.plan_steps([{"math": "k:=2"}]) + notes.plan_graphs(["k*x^2", "x+1"])
    assert notes._prepare(b, its, 5) == []
    assert b.vars == {}  # k and the check function both removed
    assert f"Define {notes.GRAPH_CHECK_NAME}(x)=k*x^2" in b.calls


def test_failing_graph_names_it_and_cleans_up():
    b = FailingOn("x+", stored={"a": ("NUM", "5")})
    its = notes.plan_steps([{"math": "a:=1"}]) + notes.plan_graphs(["x^2", "x+"])
    with pytest.raises(ValueError, match=r"graph 2 \(x\+\)"):
        notes._prepare(b, its, 5)
    assert b.vars == {"a": ("NUM", "5")}


def test_graph_using_a_bridge_variable_warns():
    b = FakeBridge({"k": ("NUM", "9")})
    w = notes._prepare(b, notes.plan_graphs(["k*x"]), 5)
    assert len(w) == 1 and w[0].startswith("Graph 1 uses k")


# ------------------------------------------------------------ the whole run, faked

class Sim:
    """TI-Nspire's window and Save dialog as seen from outside, driven by the keys sent.

    ctrl+s opens the dialog (unless dialog=None) with the focus in its file name box;
    typed characters go into the box; Enter closes it and writes the file."""

    def __init__(self, path=None, dialog_cls=keys.FILE_DIALOG_CLASS, opens=True, focus_box=True,
                 garble=False, prompt=False, writes=True, steal_focus_on=None):
        self.path, self.dialog_cls, self.opens, self.focus_box = path, dialog_cls, opens, focus_box
        self.garble, self.prompt, self.writes, self.steal_focus_on = garble, prompt, writes, steal_focus_on
        self.dialog_open = self.prompt_open = False
        self.text, self.selected = "", False
        self.focused = True
        self.pasted: list[str] = []

    def after(self, ev):
        if ev == self.steal_focus_on:
            self.focused = False
        if ev == "ctrl+s" and self.opens:
            self.dialog_open, self.text = True, "Document1.tns"
        elif self.dialog_open and ev == "shift+home":
            self.selected = True
        elif self.dialog_open and ev == "enter":
            if self.prompt:
                self.prompt_open = True
                return
            self.dialog_open = False
            if self.writes:
                Path(self.path).write_bytes(b"TI-Nspire document")
        elif self.dialog_open and len(ev) == 1:
            self.text = (ev if self.selected else self.text + ev)
            self.selected = False
            if self.garble and self.text.endswith(".tns"):
                self.text += "x"

    def dialogs(self, hwnd, before):
        out = [DIALOG] if self.dialog_open else []
        return out + ([PROMPT] if self.prompt_open else [])

    def patches(self):
        return [
            mock.patch("time.sleep"),
            mock.patch.object(notes, "find_nspire_windows", return_value=[WIN]),
            mock.patch.object(notes, "focus_nspire", side_effect=lambda h: h),
            mock.patch.object(notes, "foreground_is", side_effect=lambda h: self.focused),
            mock.patch.object(notes, "clip_set", side_effect=self.pasted.append),
            mock.patch.object(notes, "describe_window",
                              return_value={"hwnd": HWND, "title": "Document1 - TI-Nspire", "process": "x.exe"}),
            mock.patch.object(notes, "window_title", return_value="Document1 - TI-Nspire"),
            mock.patch.object(notes, "process_windows", return_value=set()),
            mock.patch.object(notes, "new_dialogs", side_effect=self.dialogs),
            mock.patch.object(notes, "window_open",
                              side_effect=lambda h: (h == DIALOG and self.dialog_open)
                              or (h == PROMPT and self.prompt_open)),
            mock.patch.object(notes, "window_class",
                              side_effect=lambda h: self.dialog_cls if h == DIALOG else "#32770"),
            mock.patch.object(notes, "foreground_window",
                              side_effect=lambda: (PROMPT if self.prompt_open else DIALOG if self.dialog_open
                                                   else HWND) if self.focused else 1),
            mock.patch.object(notes, "focused_control", side_effect=lambda h: BOX if self.focus_box else 0),
            mock.patch.object(notes, "is_file_name_box", side_effect=lambda d, c: d == DIALOG and c == BOX),
            mock.patch.object(notes, "control_text", side_effect=lambda h: self.text),
        ]


def build(sim, steps=({"text": "Area:", "math": "1+1"},), graphs=None, save_as=None, overwrite=False,
          progress=None, cancel=None, bridge=None, page_size="computer"):
    rec = KeyRecorder(sim.after)
    menus = []
    with mock.patch.object(keys, "_send", rec), \
            mock.patch.object(notes, "_new_computer_notes", return_value="Document1"), \
            mock.patch.object(notes, "_new_handheld_notes", return_value="Document1"), \
            mock.patch.object(notes, "_menu", side_effect=lambda g, m, i, what: g() or menus.append((m, i))):
        for p in sim.patches():
            p.start()
        try:
            out = notes.build_notes(bridge or FakeBridge(), list(steps), None, page_size, 5, cancel=cancel,
                                    graphs=graphs, save_as=save_as, overwrite=overwrite, progress=progress)
            return rec.events, out, menus
        except notes.NotesAborted as e:
            return rec.events, e, menus
        finally:
            mock.patch.stopall()


def test_progress_is_reported_per_check_page_step_graph_and_save(tmp_path):
    path = str(tmp_path / "area.tns")
    seen = []
    sim = Sim(path)
    _, out, _ = build(sim, steps=[{"text": "Area:", "math": "1+1"}, {"text": "done"}], graphs=["x^2", "2*x"],
                      save_as=path, progress=lambda d, t, m: seen.append((d, t, m)))
    assert out["save"]["saved"] is True
    totals = {t for _, t, _ in seen}
    assert totals == {3 + 1 + 2 + 1 + 2 + 1}  # checks (1 step + 2 graphs), page, 2 steps, graphs page, 2 graphs, save
    dones = [d for d, _, _ in seen]
    assert dones == sorted(dones) and dones[0] == 0 and dones[-1] == seen[-1][1]
    messages = [m for _, _, m in seen]
    assert messages[0].startswith("Checking step 1") and any(m.startswith("Checking graph 2") for m in messages)
    assert "Typing step 2 of 2" in messages and "Graphing f2(x)=2*x" in messages
    assert any(m.startswith("Saving as") for m in messages) and messages[-1] == "Saved"


def test_progress_callback_errors_never_stop_the_run():
    def broken(*a):
        raise RuntimeError("client gone")
    _, out, _ = build(Sim(), progress=broken)
    assert out["steps"][0]["result"] == "ok"


def test_graphs_page_keys_and_menus():
    sim = Sim()
    events, out, menus = build(sim, steps=[{"text": "hi"}], graphs=["x^2-1", "y=2*x"])
    assert menus == [(notes.INSERT_MENU, notes.INSERT_PAGE_ITEM), (notes.INSERT_MENU, notes.INSERT_GRAPHS_ITEM)]
    assert events == ["h", "i", "enter", "ctrl+v", "enter", "ctrl+g", "ctrl+v", "enter"]
    assert sim.pasted == ["x^2-1", "2*x"]
    assert out["graphs"] == ["f1(x)=x^2-1", "f2(x)=2*x"] and "save" not in out


def test_graphs_page_on_a_handheld_document_uses_the_add_page_menu():
    with mock.patch.object(notes, "wait_for_popup", return_value=True):
        events, out, menus = build(Sim(), steps=[{"text": "hi"}], graphs=["x"], page_size="handheld")
    assert menus == [(notes.INSERT_MENU, notes.INSERT_PAGE_ITEM)]
    assert events == ["h", "i", "enter", notes.GRAPHS_MENU_KEY, "ctrl+v", "enter"]


def test_bad_graph_means_nothing_is_typed():
    sim = Sim()
    b = FailingOn("bad")
    with pytest.raises(ValueError, match="graph 2"):
        build(sim, graphs=["x", "bad+x"], bridge=b)
    assert sim.pasted == [] and b.vars == {}


def test_focus_lost_while_graphing_stops():
    sim = Sim(steal_focus_on="ctrl+g")
    events, err, _ = build(sim, steps=[{"text": "a"}], graphs=["x", "x^2"])
    assert isinstance(err, notes.NotesAborted)
    assert events[-1] == "ctrl+g" and sim.pasted == ["x"]


def test_cancel_while_graphing_stops():
    cancel = threading.Event()
    sim = Sim()
    orig = sim.after
    sim.after = lambda ev: (orig(ev), ev == "ctrl+g" and cancel.set())
    events, err, _ = build(sim, steps=[{"text": "a"}], graphs=["x", "x^2"], cancel=cancel)
    assert isinstance(err, notes.NotesAborted) and "Cancelled" in str(err)
    assert events[-1] == "ctrl+g"


# ------------------------------------------------------------ saving

def save_keys(path):
    return ["ctrl+s", "end", "shift+home", *path, "enter"]


def test_save_types_the_path_and_checks_the_file(tmp_path):
    path = str(tmp_path / "area.tns")
    sim = Sim(path)
    events, out, _ = build(sim, steps=[{"text": "a"}], save_as=path)
    assert events == ["a", "enter", *save_keys(path)]
    assert out["save"] == {"path": path, "saved": True, "message": f"Saved as {path}."}
    assert out["warnings"] == [] and "saved as" in out["note"]
    assert Path(path).read_bytes() == b"TI-Nspire document"


def test_overwrite_an_existing_file(tmp_path):
    path = tmp_path / "area.tns"
    path.write_bytes(b"old")
    os.utime(path, (1, 1))
    _, out, _ = build(Sim(str(path)), steps=[{"text": "a"}], save_as=str(path), overwrite=True)
    assert out["save"]["saved"] is True and path.read_bytes() == b"TI-Nspire document"


def test_existing_file_is_refused_before_anything_happens(tmp_path):
    path = tmp_path / "area.tns"
    path.write_bytes(b"old")
    b = FakeBridge()
    with pytest.raises(ValueError, match="already exists"):
        build(Sim(str(path)), save_as=str(path), bridge=b)
    assert b.calls == [] and path.read_bytes() == b"old"


def unsaved(out, *needles):
    assert out["save"]["saved"] is False
    for n in needles:
        assert n in out["save"]["message"]
    assert out["save"]["message"] in out["warnings"] and "unsaved" in out["note"]


def test_dialog_that_never_opens(tmp_path):
    path = str(tmp_path / "a.tns")
    events, out, _ = build(Sim(path, opens=False), steps=[{"text": "a"}], save_as=path)
    assert events == ["a", "enter", "ctrl+s"]
    unsaved(out, "didn't open its Save dialog")


def test_java_dialog_gets_no_keys(tmp_path):
    path = str(tmp_path / "a.tns")
    events, out, _ = build(Sim(path, dialog_cls="SunAwtDialog"), steps=[{"text": "a"}], save_as=path)
    assert events == ["a", "enter", "ctrl+s"]
    unsaved(out, "isn't a standard Windows file dialog", path)


def test_file_name_box_without_focus_gets_no_keys(tmp_path):
    path = str(tmp_path / "a.tns")
    events, out, _ = build(Sim(path, focus_box=False), steps=[{"text": "a"}], save_as=path)
    assert events == ["a", "enter", "ctrl+s"]
    unsaved(out, "doesn't have the keyboard focus")


def test_wrong_text_in_the_box_means_no_enter(tmp_path):
    path = str(tmp_path / "a.tns")
    events, out, _ = build(Sim(path, garble=True), steps=[{"text": "a"}], save_as=path)
    assert events[-1] != "enter" and events[:3] == ["a", "enter", "ctrl+s"]
    unsaved(out, "Save wasn't pressed")
    assert not Path(path).exists()


def test_question_after_save_is_left_to_the_user(tmp_path):
    path = str(tmp_path / "a.tns")
    events, out, _ = build(Sim(path, prompt=True), steps=[{"text": "a"}], save_as=path)
    assert events == ["a", "enter", *save_keys(path)]  # nothing pressed in the question
    unsaved(out, "showed a message")


def test_dialog_closed_but_no_file(tmp_path):
    path = str(tmp_path / "a.tns")
    with mock.patch.object(notes.time, "monotonic", side_effect=iter(range(0, 10_000, 1)).__next__):
        events, out, _ = build(Sim(path, writes=False), steps=[{"text": "a"}], save_as=path)
    unsaved(out, "didn't appear")


def test_focus_lost_while_typing_the_path_stops_typing(tmp_path):
    path = str(tmp_path / "a.tns")
    sim = Sim(path, steal_focus_on="shift+home")
    events, out, _ = build(sim, steps=[{"text": "a"}], save_as=path)
    assert events[-1] == "shift+home"  # not one character of the path went elsewhere
    unsaved(out, "stopped being the active window")


def test_file_appearing_meanwhile_is_not_overwritten(tmp_path):
    path = tmp_path / "a.tns"
    sim = Sim(str(path))
    orig = sim.after
    sim.after = lambda ev: (orig(ev), ev == "a" and path.write_bytes(b"theirs"))
    events, out, _ = build(sim, steps=[{"text": "a"}], save_as=str(path))
    assert "ctrl+s" not in events and path.read_bytes() == b"theirs"
    unsaved(out, "appeared while")


# ------------------------------------------------------------ server: progress and parameters

@pytest.fixture
def server(monkeypatch):
    pytest.importorskip("mcp")
    import nspire_bridge as nb
    nb.MUTEX_NAME = "Local\\nspire_mcp_bridge_offline_test"
    import server as srv

    class Bridge:
        def session(self, work, wait_s=60):
            return work()
    monkeypatch.setattr(srv, "bridge", Bridge())
    return srv


class FakeCtx:
    def __init__(self):
        self.reports = []

    async def report_progress(self, progress, total=None, message=None):
        self.reports.append((progress, total, message))


def fake_notes(monkeypatch, build_notes):
    import types
    mod = types.ModuleType("nspire_notes")
    mod.build_notes = build_notes
    monkeypatch.setitem(sys.modules, "nspire_notes", mod)


def test_server_forwards_progress_and_new_parameters(server, monkeypatch):
    got = {}

    def build_notes(bridge, steps, title, page_size, timeout_s, cancel=None, **kw):
        got.update(kw, thread=threading.current_thread())
        for k in range(3):
            kw["progress"](k, 3, f"step {k}")
        return {"note": "ok"}
    fake_notes(monkeypatch, build_notes)
    ctx = FakeCtx()
    out = asyncio.run(server.nspire_notes([{"text": "hi"}], graphs=["x^2"], save_as=r"C:\x\a.tns",
                                          overwrite=True, ctx=ctx))
    assert out == {"note": "ok"}
    assert ctx.reports == [(0, 3, "step 0"), (1, 3, "step 1"), (2, 3, "step 2")]
    assert got["graphs"] == ["x^2"] and got["save_as"] == r"C:\x\a.tns" and got["overwrite"] is True
    assert got["thread"] is not threading.main_thread()  # built in a worker thread


def test_server_without_context_still_runs(server, monkeypatch):
    def build_notes(bridge, steps, title, page_size, timeout_s, cancel=None, **kw):
        kw["progress"](0, 1, "x")
        return {"note": "ok"}
    fake_notes(monkeypatch, build_notes)
    assert asyncio.run(server.nspire_notes([{"text": "hi"}])) == {"note": "ok"}


def test_server_cancel_still_stops_the_worker_and_progress(server, monkeypatch):
    started, seen = threading.Event(), {}
    ctx = FakeCtx()

    def build_notes(bridge, steps, title, page_size, timeout_s, cancel=None, **kw):
        kw["progress"](0, 2, "before")
        started.set()
        seen["stopped"] = cancel.wait(10)
        kw["progress"](1, 2, "after")  # dropped: the call was cancelled
        return {}
    fake_notes(monkeypatch, build_notes)

    async def scenario():
        task = asyncio.create_task(server.nspire_notes([{"text": "hi"}], ctx=ctx))
        assert await asyncio.to_thread(started.wait, 5)
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(scenario())
    assert seen["stopped"] is True
    assert ctx.reports == [(0, 2, "before")]


def test_server_schema_has_the_new_parameters_and_hides_context(server):
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools["nspire_notes"].inputSchema["properties"]
    assert {"graphs", "save_as", "overwrite"} <= set(props) and "ctx" not in props
    assert props["overwrite"]["default"] is False


# ------------------------------------------------------------ which box is the file name box

def test_is_file_name_box():
    # dialog 1 > FloatNotifySink 2 > ComboBox 3 > Edit 4 (file name);
    # dialog 1 > Address Band Root 5 > ComboBoxEx32 6 > ComboBox 7 > Edit 8 (address bar)
    cls = {1: "#32770", 2: "FloatNotifySink", 3: "ComboBox", 4: "Edit", 5: "Address Band Root",
           6: "ComboBoxEx32", 7: "ComboBox", 8: "Edit", 9: "SunAwtDialog", 10: "Edit"}
    parent = {4: 3, 3: 2, 2: 1, 8: 7, 7: 6, 6: 5, 5: 1, 10: 9}

    def is_child(d, c):
        while c in parent:
            c = parent[c]
            if c == d:
                return True
        return False
    with mock.patch.object(keys, "window_class", side_effect=lambda h: cls.get(h, "")), \
            mock.patch.object(keys.win32gui, "IsChild", side_effect=is_child), \
            mock.patch.object(keys.win32gui, "GetParent", side_effect=lambda h: parent.get(h, 0)):
        assert keys.is_file_name_box(1, 4)
        assert not keys.is_file_name_box(1, 8)   # address bar
        assert not keys.is_file_name_box(1, 3)   # not an Edit
        assert not keys.is_file_name_box(1, 0)
        assert not keys.is_file_name_box(9, 10)  # a Java dialog's box can't be checked
        assert not keys.is_file_name_box(1, 10)  # in another window
