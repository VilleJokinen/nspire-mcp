"""Offline tests for nspire_notes' Lists & Spreadsheet page: planning the data, checking it on
the bridge, and the keys typed (nspire_keys._send is faked, as in test_notes_features)."""
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest

pytest.importorskip("win32api")  # nspire_keys needs pywin32

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_notes_features import Sim, build, fake_notes, server  # noqa: E402,F401
from test_notes_safety import FakeBridge  # noqa: E402

import nspire_notes as notes  # noqa: E402
from nspire_chars import NS_EXP  # noqa: E402


def plan(sheet, steps=(), graphs=None):
    items = notes.plan_steps(list(steps)) if steps else []
    return notes.plan_spreadsheet(sheet, items, notes.plan_graphs(graphs))


# ------------------------------------------------------------ plan_spreadsheet (no bridge)

def test_csv_with_header_names_the_columns():
    s = plan({"csv": "t,d\n0,0\n1,4.9\n2,19.6"})
    assert s.names == ["t", "d"] and s.columns == [["0", "1", "2"], ["0", "4.9", "19.6"]] and s.rows == 3
    assert [c["cmd"] for c in s.checks] == ["t:={0,1,2}", "d:={0,4.9,19.6}"]
    assert s.checks[1]["label"] == "spreadsheet column 2 (B: d)"


def test_columns_with_names_and_an_unnamed_column():
    s = plan({"columns": [[1, 2], [-3, 1.5e-30]], "names": ["xs", ""]})
    assert s.names == ["xs", None]
    assert s.checks[1]["cmd"] == f"{notes.SHEET_CHECK_NAME}:={{-3,1.5{NS_EXP}-30}}"
    assert s.checks[1]["label"] == "spreadsheet column 2 (B)"


def test_no_header_means_no_names():
    s = plan({"csv": "1 2\n3 4"})
    assert s.names == [None, None] and s.columns == [["1", "3"], ["2", "4"]]


def test_long_columns_are_checked_in_chunks():
    s = plan({"columns": [list(range(notes.SHEET_CHUNK + 5))], "names": ["big"]})
    cmds = [c["cmd"] for c in s.checks]
    assert len(cmds) == 2 and cmds[0].startswith("big:={0,1,") and cmds[1].startswith("big:=augment(big,{200,")
    assert s.checks[0]["expr"] == "{0,1,2,...}"


def test_no_spreadsheet():
    assert notes.plan_spreadsheet(None) is None


@pytest.mark.parametrize("sheet, what", [
    ("1,2", "must be an object"),
    ({"csv": "a\n1", "rows": 3}, "unknown key"),
    ({"csv": "a\n1", "columns": [[1]]}, "not both"),
    ({}, "not both"),
    ({"csv": "a\nx"}, "not a number"),
    ({"csv": "a\n1", "header": "yes"}, "header must be"),
    ({"csv": "time (s),d\n1,2"}, "can't be a column name"),
    ({"csv": "_m\n1"}, "can't be a column name"),
    ({"csv": "2x\n1"}, "can't be a column name"),
    ({"columns": [[1]], "names": ["a" * 17]}, "can't be a column name"),
    ({"csv": "a1\n1"}, "looks like a spreadsheet cell"),
    ({"csv": "AB12\n1"}, "looks like a spreadsheet cell"),
    ({"csv": "t,T\n1,2"}, "already column 1"),
    ({"csv": f"{notes.SHEET_CHECK_NAME}\n1"}, "also used by this tool"),
    ({"columns": [[1]] * 3, "names": ["a", "b"]}, "one name per column"),
])
def test_refused(sheet, what):
    with pytest.raises(ValueError, match=what):
        plan(sheet)


def test_too_many_values():
    with pytest.raises(ValueError, match="at most"):
        plan({"columns": [list(range(1000))] * 3})


def test_names_clashing_with_steps_and_graphs_are_refused():
    with pytest.raises(ValueError, match=r"also used by step 1"):
        plan({"csv": "a\n1"}, steps=[{"math": "a:=5"}])
    with pytest.raises(ValueError, match=r"also used by step 2"):
        plan({"csv": "h\n1"}, steps=[{"text": "x"}, {"math": "mean(h)"}])
    with pytest.raises(ValueError, match=r"graph 2 \(f2\)"):
        plan({"csv": "f2\n1"}, graphs=["x", "x^2"])
    with pytest.raises(ValueError, match=r"also used by graph 1"):
        plan({"csv": "k\n1"}, graphs=["k*x"])
    assert plan({"csv": "fx\n1"}, graphs=["x", "x^2"]).names == ["fx"]


# ------------------------------------------------------------ checking on the bridge

def test_columns_are_checked_on_the_bridge_and_undone():
    b = FakeBridge({"d": ("LIST", "{7}")})
    s = plan({"csv": "t,d\n0,0\n1,4.9"})
    assert notes._prepare(b, s.checks, 5) == []
    assert "t:={0,1}" in b.calls and "d:={0,4.9}" in b.calls
    assert set(b.vars) == {"d"} and b.vars["d"][1] == "{7}"  # t removed, d put back


class Refuses(FakeBridge):
    def eval_one(self, expr, timeout_s, raw=False):
        if expr.startswith("d:="):
            self.calls.append(expr)
            raise ValueError("TI-Nspire error: error 910 (syntax error)")
        return super().eval_one(expr, timeout_s, raw)


def test_bad_column_names_it_and_nothing_is_typed():
    sim = Sim()
    b = Refuses()
    with pytest.raises(ValueError, match=r"spreadsheet column 2 \(B: d\) \(\{0,4\.9\}\)"):
        build(sim, steps=[{"text": "hi"}], spreadsheet={"csv": "t,d\n0,0\n1,4.9"}, bridge=b)
    assert sim.pasted == [] and b.vars == {}


# ------------------------------------------------------------ the keys typed

def name_keys(names, last):
    out = ["up", "up"]
    for k, n in enumerate(names):
        if n:
            out += [*n, "enter", "up", "up"]
        if k < last:
            out.append("right")
    return out + ["left"] * last + ["down", "down"]


def test_spreadsheet_page_keys_and_menus():
    sim = Sim()
    events, out, menus = build(sim, steps=[{"text": "hi"}], spreadsheet={"csv": "t,d\n0,0\n1,-4.9"})
    assert menus == [(notes.INSERT_MENU, notes.INSERT_PAGE_ITEM), (notes.INSERT_MENU, notes.INSERT_LISTS_ITEM)]
    assert events == (["h", "i", "enter"] + name_keys(["t", "d"], 1)
                      + ["0", "enter", "1", "enter", "right", "up", "up"]
                      + ["0", "enter", "-", "4", ".", "9", "enter"])
    assert sim.pasted == []  # typed, not pasted
    assert out["spreadsheet"] == {"rows": 2, "columns": [{"column": "A", "name": "t", "first": ["0", "1"]},
                                                         {"column": "B", "name": "d", "first": ["0", "-4.9"]}]}
    assert "list variables of the new document (t, d)" in out["note"]


def test_unnamed_columns_skip_the_name_row():
    events, out, _ = build(Sim(), steps=[{"text": "a"}], spreadsheet={"columns": [[1], [2]]})
    assert events == ["a", "enter", "1", "enter", "right", "up", "2", "enter"]
    assert "list variables" not in out["note"]


def test_partly_named_columns():
    events, _, _ = build(Sim(), steps=[{"text": "a"}], spreadsheet={"columns": [[1], [2], [3]],
                                                                    "names": ["", "y", ""]})
    assert events[2:] == (name_keys([None, "y", None], 2)
                          + ["1", "enter", "right", "up", "2", "enter", "right", "up", "3", "enter"])


def test_exponent_is_typed_as_tinspires_character():
    events, _, _ = build(Sim(), steps=[{"text": "a"}], spreadsheet={"columns": [[2.5e-30]]})
    assert events[2:] == ["2", ".", "5", NS_EXP, "-", "3", "0", "enter"]


def test_spreadsheet_after_graphs_on_a_handheld_document():
    with mock.patch.object(notes, "wait_for_popup", return_value=True):
        events, out, menus = build(Sim(), steps=[{"text": "a"}], graphs=["x"], page_size="handheld",
                                   spreadsheet={"columns": [[5]]})
    assert menus == [(notes.INSERT_MENU, notes.INSERT_PAGE_ITEM)] * 2
    assert events == ["a", "enter", notes.GRAPHS_MENU_KEY, "ctrl+v", "enter", notes.LISTS_MENU_KEY, "5", "enter"]
    assert out["graphs"] == ["f1(x)=x"]


def test_progress_counts_the_spreadsheet():
    seen = []
    build(Sim(), steps=[{"text": "a"}], spreadsheet={"csv": "t,d\n0,0"},
          progress=lambda d, t, m: seen.append((d, t, m)))
    totals = {t for _, t, _ in seen}
    assert totals == {2 + 1 + 1 + 1 + 1 + 2}  # 2 column checks, page, step, sheet page, naming, 2 columns
    messages = [m for _, _, m in seen]
    assert "Adding a Lists & Spreadsheet page" in messages and "Naming the spreadsheet columns" in messages
    assert "Filling column B (d): 1 values" in messages
    assert any(m.startswith("Checking spreadsheet column 1") for m in messages)


def test_focus_lost_while_filling_stops():
    sim = Sim(steal_focus_on="right")
    events, err, _ = build(sim, steps=[{"text": "a"}], spreadsheet={"columns": [[1, 2], [3, 4]]})
    assert isinstance(err, notes.NotesAborted)
    assert events[-1] == "right" and "3" not in events


def test_cancel_while_filling_stops():
    cancel = threading.Event()
    sim = Sim()
    orig = sim.after
    sim.after = lambda ev: (orig(ev), ev == "2" and cancel.set())
    events, err, _ = build(sim, steps=[{"text": "a"}], spreadsheet={"columns": [[1, 2, 3]]}, cancel=cancel)
    assert isinstance(err, notes.NotesAborted) and "Cancelled" in str(err)
    assert events[-1] == "2"


# ------------------------------------------------------------ server

def test_server_passes_the_spreadsheet_as_a_dict(server, monkeypatch):  # noqa: F811
    got = {}

    def build_notes(bridge, steps, title, page_size, timeout_s, cancel=None, **kw):
        got.update(kw)
        return {"note": "ok"}
    fake_notes(monkeypatch, build_notes)
    asyncio.run(server.mcp.call_tool("nspire_notes", {"steps": [{"text": "hi"}],
                                                      "spreadsheet": {"csv": "t\n1", "header": True}}))
    assert got["spreadsheet"] == {"csv": "t\n1", "header": True} and type(got["spreadsheet"]) is dict
    asyncio.run(server.nspire_notes([{"text": "hi"}]))
    assert got["spreadsheet"] is None


def test_server_schema_has_the_spreadsheet(server):  # noqa: F811
    tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}
    props = tools["nspire_notes"].inputSchema["properties"]
    assert "spreadsheet" in props
    assert "nspire_notes" in server.INSTRUCTIONS and "Lists &" in server.INSTRUCTIONS
