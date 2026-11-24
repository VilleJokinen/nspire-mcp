"""Offline tests of nspire_library: reading definitions, the library file, and save/load against a
fake CAS that stores definitions. No bridge, no clipboard."""
from __future__ import annotations

import json
import re

import pytest

import nspire_library as lib
from nspire_chars import NS_E, to_cas_chars


class FakeCAS:
    """A tiny stand-in for the document: Define/:= store, getType, DelVar, calls substitute the
    arguments into a one-expression body (or a Func's Return), variables give their value."""

    def __init__(self, stored: dict[str, str] | None = None, locked: set[str] | None = None,
                 symbolic_error: set[str] | None = None):
        self.defs: dict[str, lib.Parsed] = {}
        self.locked = {n.lower() for n in (locked or set())}
        self.symbolic_error = {n.lower() for n in (symbolic_error or set())}
        self.batches: list[list[str]] = []
        for cmd in (stored or {}).values():
            p = lib.parse_command(cmd)
            self.defs[p.name.lower()] = p

    def session(self, work, wait_s=60):
        return work()

    def list_vars(self, values=True, timeout_s=5.0):
        types = {"function": "FUNC", "program": "PRGM"}
        return [{"name": p.name, "type": types.get(p.kind) or ("STR" if p.body.startswith('"') else "NUM"),
                 "library": None, "locked": p.name.lower() in self.locked} for p in self.defs.values()]

    def eval_batch(self, exprs, timeout_s=5.0, raw=False):
        self.batches.append(list(exprs))
        out = []
        for e in exprs:
            try:
                out.append({"expr": e, "ok": True, "result": self.eval(e)})
            except ValueError as x:
                out.append({"expr": e, "ok": False, "error": str(x)})
        return out

    def eval(self, e: str) -> str:
        if "BAD" in e:
            raise ValueError("error 910 (syntax error)")
        m = re.fullmatch(r"getType\((\w+)\)", e)
        if m:
            p = self.defs.get(m.group(1).lower())
            if p is None:
                return '"NONE"'
            return {"function": '"FUNC"', "program": '"PRGM"'}.get(p.kind, '"NUM"')
        m = re.fullmatch(r"DelVar (\w+)", e)
        if m:
            self.defs.pop(m.group(1).lower(), None)
            return ""
        if e.startswith("Define ") or re.match(r"\w+(\([^)]*\))?:=", e):
            p = lib.parse_command(e, internal=True)
            if p.name.lower() in self.locked:
                raise ValueError("error 600 (variable is locked)")
            self.defs[p.name.lower()] = p
            return p.body if p.kind == "variable" else ""
        m = re.fullmatch(r"(\w+)\(([^()]*)\)", e)
        if m and m.group(1).lower() in self.defs:
            p = self.defs[m.group(1).lower()]
            args = [a for a in m.group(2).split(",") if a]
            if len(args) < len(p.params):
                raise ValueError("error 930 (too few arguments)")
            if len(args) > len(p.params):
                raise ValueError("error 940 (too many arguments)")
            symbolic = any(lib.SYM_PREFIX in a for a in args)
            if symbolic and (p.name.lower() in self.symbolic_error or "If " in p.body):
                raise ValueError("error 20 (a test did not resolve to true or false)")
            body = p.body
            if p.kind == "function" and body.lower().startswith("func"):
                body = re.search(r"Return ([^:]*)", body).group(1)  # type: ignore[union-attr]
            for par, arg in zip(p.params, args, strict=True):
                body = re.sub(rf"(?<![\w.]){par}(?!\w)", arg, body)
            return body
        if e.lower() in self.defs and self.defs[e.lower()].kind == "variable":
            return self.defs[e.lower()].body
        if re.fullmatch(r"\w+\(.*\)", e):
            raise ValueError("error 140 (argument must be a variable name)" if "(" in e else "error 910")
        raise ValueError("error 40 (argument error)")

    def stores(self) -> list[str]:
        """Commands of the batches that weren't checks or cleanups (none of their items uses a scratch name)."""
        return [e for b in self.batches if not any(lib.TEMP_PREFIX in e or lib.SYM_PREFIX in e for e in b)
                for e in b]


@pytest.fixture(autouse=True)
def libdir(tmp_path, monkeypatch):
    monkeypatch.setenv("NSPIRE_LIBRARY_DIR", str(tmp_path / "lib"))
    return tmp_path / "lib"


# ------------------------------------------------------------ reading definitions

def test_parse_simple_forms():
    p = lib.parse_command("Define f(x)=x^2+1")
    assert (p.name, p.kind, p.params, p.command) == ("f", "function", ("x",), "Define f(x)=x^2+1")
    assert lib.parse_command("g(x, y):=x*y").command == "Define g(x,y)=x*y"
    assert lib.parse_command("Define a=5").command == "a:=5"
    v = lib.parse_command("k:=9.81_m/_s^2")
    assert (v.kind, v.command) == ("variable", "k:=9.81_m/_s^2")
    assert lib.parse_command("Define LibPub h(t)=t/2").command == "Define LibPub h(t)=t/2"
    assert lib.parse_command('s:="a:b"').kind == "variable"  # a ':' inside a string is fine


def test_parse_blocks_and_newlines():
    src = "Define g(x,y)=Func\n  Local t\n  t:=x+y\n  If t>0 Then\n    Return t^2\n  Else\n    Return -t\n  EndIf\nEndFunc"
    p = lib.parse_command(src)
    assert p.kind == "function" and p.params == ("x", "y")
    assert p.command == "Define g(x,y)=Func:Local t:t:=x+y:If t>0 Then:Return t^2:Else:Return -t:EndIf:EndFunc"
    q = lib.parse_command('Define p()=Prgm:Disp "hi":EndPrgm')
    assert (q.kind, q.params) == ("program", ())


def test_parse_tricky_characters_round_trip():
    p = lib.parse_command("Define f(x)=piecewise(x^2,x<0,√(x),x≥0)")
    assert p.command == "Define f(x)=piecewise(x^2,x<0,√(x),x≥0)"
    e = lib.parse_command(f"Define q(x)={NS_E}^x+2{NS_E}-3")  # private-use e from the CAS
    assert "ℯ" in e.command and NS_E not in e.command          # stored readable
    assert to_cas_chars(e.command) == f"Define q(x)={NS_E}^x+2{NS_E}-3"
    assert lib.parse_command("Define w(v)=v@>_km/_hr").body == "v@>_km/_hr"


@pytest.mark.parametrize("bad", [
    "x^2+1", "Define f(x)=x:DelVar a", "Define f(x)=Func:Return x", "f(x):=Func:Return x:EndFunc",
    "Define f(x)=Func:Return x:EndPrgm", "Define f(x+1)=2", "Define f(x,x)=x", "Define mcplibk1(x)=x",
    "Define f(x)=", "a:=3→b", "", "Define f(x)=x\n+1", "Define LibPub a=3", "Define p=Prgm:Disp 1:EndPrgm",
])
def test_parse_refuses(bad):
    with pytest.raises(lib.LibraryError):
        lib.parse_command(bad)


def test_rebuild_params_avoids_names_the_body_uses():
    assert lib.rebuild_params("mcplibp1^2+1", 1) == (("x",), "x^2+1")
    assert lib.rebuild_params("mcplibp1*x+mcplibp2", 2) == (("a", "b"), "a*x+b")
    assert lib.rebuild_params("3.*mcplibp1*(_m/_s)", 1) == (("x",), "3.*x*(_m/_s)")  # _m isn't a name
    params, body = lib.rebuild_params("+".join(f"mcplibp{i}" for i in range(1, 11)), 10)
    assert params[9] == "x10" and body.endswith("+x9+x10")  # mcplibp10 isn't read as mcplibp1 then 0


def test_library_dir_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("NSPIRE_LIBRARY_DIR")
    monkeypatch.setattr(lib.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert lib.library_dir() == tmp_path / "Roaming" / "nspire-mcp" / "library"
    monkeypatch.setattr(lib.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert lib.library_dir() == tmp_path / "data" / "nspire-mcp" / "library"
    with pytest.raises(lib.LibraryError):
        lib.library_file("../evil")


# ------------------------------------------------------------ save

TRICKY = [
    "Define g(x,y)=Func:Local t:t:=x+y:If t>0 Then:Return t^2:Else:Return -t:EndIf:EndFunc",
    "Define f(x)=piecewise(x^2,x<0,√(x),x≥0)",
    "Define speed(t)=t*3_m/_s",
    "Define q(x)=ℯ^x+𝑖",
    'Define hello()=Prgm:Disp "hi":EndPrgm',
    "k:=9.81_m/_s^2",
]


def test_save_definitions_writes_file_and_document_untouched(libdir):
    cas = FakeCAS()
    out = lib.save(cas, "physics", None, TRICKY, source_document="hw3.tns", note="week 3")
    assert out["saved"] == ["g", "f", "speed", "q", "hello", "k"] and not out["errors"] and not out["conflicts"]
    assert cas.defs == {}  # every temporary name deleted again
    data = json.loads((libdir / "physics.json").read_text(encoding="utf-8"))
    assert data["format"] == "nspire-mcp-library" and data["library"] == "physics"
    e = data["entries"][0]
    assert e["command"] == TRICKY[0] and e["kind"] == "function" and e["params"] == ["x", "y"]
    assert e["exact"] is True and e["source_document"] == "hw3.tns" and e["note"] == "week 3" and e["saved_at"]
    assert [x["kind"] for x in data["entries"]][-2:] == ["program", "variable"]
    # the checks went out as one batch, with CAS characters, then one cleanup batch
    assert len(cas.batches) == 2 and all(lib.TEMP_PREFIX in x for x in cas.batches[0][:6])
    assert any(NS_E in x for x in cas.batches[0])


def test_save_again_unchanged_conflict_overwrite(libdir):
    lib.save(FakeCAS(), None, None, ["Define f(x)=x^2"])
    again = lib.save(FakeCAS(), None, None, ["Define f(x)=x^2"])
    assert again["unchanged"] == ["f"] and not again["saved"]
    clash = lib.save(FakeCAS(), None, None, ["Define f(x)=x^3", "Define h(x)=1"])
    assert [c["name"] for c in clash["conflicts"]] == ["f"] and not clash["saved"]
    assert [e["name"] for e in lib.read_library(None)] == ["f"]  # nothing written, h not added
    assert lib.read_library(None)[0]["command"] == "Define f(x)=x^2"
    over = lib.save(FakeCAS(), None, None, ["Define f(x)=x^3"], overwrite=True)
    assert over["replaced"] == ["f"] and lib.read_library(None)[0]["command"] == "Define f(x)=x^3"


def test_save_refuses_a_definition_the_cas_rejects(libdir):
    out = lib.save(FakeCAS(), None, None, ["Define f(x)=x^2", "Define h(x)=BAD(x)"])
    assert [e["name"] for e in out["errors"]] == ["h"] and not out["saved"]
    assert not (libdir / "default.json").exists()


def test_save_names_rebuilds_functions_from_the_document():
    cas = FakeCAS({"f": "Define f(x)=x^2+1", "a": "a:=5", "p": "Define p()=Prgm:Disp 1:EndPrgm"})
    out = lib.save(cas, None, ["f", "a"], None)
    assert out["saved"] == ["f", "a"] and not out["errors"]
    f, a = lib.read_library(None)
    assert f["command"] == "Define f(x)=x^2+1" and f["exact"] is False
    assert a["command"] == "a:=5" and a["exact"] is True  # NUM: the value is the value
    assert any("rebuilt" in w for w in out["warnings"])
    # one batch tried every arity of f and read a; the user's names are all still there
    assert "f()" in cas.batches[0] and "f(mcplibp1)" in cas.batches[0] and "a" in cas.batches[0]
    assert set(cas.defs) == {"f", "a", "p"}


def test_save_names_refuses_programs_and_unreadable_functions():
    cas = FakeCAS({"p": "Define p()=Prgm:Disp 1:EndPrgm", "g": "Define g(x)=x"}, symbolic_error={"g"})
    out = lib.save(cas, None, ["p", "g"], None)
    assert {e["name"] for e in out["errors"]} == {"p", "g"} and not out["saved"]
    assert "definitions" in out["errors"][0]["error"]
    with pytest.raises(lib.LibraryError, match="Not stored"):
        lib.save(cas, None, ["nothere"], None)


def test_scratch_names_already_used_refuse():
    cas = FakeCAS()
    cas.defs["mcplibk1"] = lib.Parsed("mcplibk1", "variable", (), "mcplibk1:=1", "1")
    with pytest.raises(lib.LibraryError, match="scratch"):
        lib.save(cas, None, None, ["Define f(x)=x"])
    assert "mcplibk1" in cas.defs  # never deleted


# ------------------------------------------------------------ load

def test_round_trip_into_an_empty_document():
    lib.save(FakeCAS(), "t", None, TRICKY)
    doc = FakeCAS()
    out = lib.load(doc, "t")
    assert out["loaded"] == ["g", "f", "speed", "q", "hello", "k"] and not out["errors"] and not out["conflicts"]
    assert doc.stores() == [to_cas_chars(c) for c in TRICKY]  # exactly the saved commands, one batch
    assert sum(1 for b in doc.batches if b == doc.stores()) == 1
    assert {p.command for p in doc.defs.values()} == set(TRICKY)
    assert not any(n.startswith(lib.TEMP_PREFIX) for n in doc.defs)


def test_load_same_definition_is_unchanged_and_stores_nothing():
    lib.save(FakeCAS(), None, None, ["Define f(x)=x^2"])
    doc = FakeCAS({"f": "Define f(t)=t^2"})  # same function, other parameter name
    out = lib.load(doc, None)
    assert out["unchanged"] == ["f"] and not out["loaded"] and doc.stores() == []


def test_load_compares_an_if_func_at_numeric_points():
    g = "Define g(x,y)=Func:Local t:t:=x+y:If t>0 Then:Return t^2:Else:Return -t:EndIf:EndFunc"
    lib.save(FakeCAS(), None, None, [g])
    doc = FakeCAS({"g": g})
    out = lib.load(doc, None)
    assert out["unchanged"] == ["g"] and doc.stores() == []
    check = next(b for b in doc.batches if any(x.startswith("getType") for x in b))
    assert "g(mcplibp1,mcplibp2)" in check and "g(0.37,-1.21)" in check and "mcplibk1(2.53,1.7)" in check
    other = FakeCAS({"g": g.replace("t^2", "t^3")})
    assert [c["name"] for c in lib.load(other, None)["conflicts"]] == ["g"]


def test_load_conflict_stores_nothing_then_overwrite():
    lib.save(FakeCAS(), None, None, ["Define f(x)=x^2", "Define h(x)=x+1"])
    doc = FakeCAS({"f": "Define f(x)=x^3"})
    out = lib.load(doc, None)
    assert [c["name"] for c in out["conflicts"]] == ["f"] and out["conflicts"][0]["document_type"] == "FUNC"
    assert not out["loaded"] and doc.stores() == [] and "h" not in doc.defs
    assert doc.defs["f"].body == "x^3"
    out = lib.load(doc, None, overwrite=True)
    assert out["replaced"] == ["f"] and out["loaded"] == ["h"] and doc.defs["f"].body == "x^2"


def test_load_locked_and_programs_conflict():
    lib.save(FakeCAS(), None, None, ["Define f(x)=x^2", "Define p()=Prgm:Disp 1:EndPrgm"])
    doc = FakeCAS({"f": "Define f(x)=x^3", "p": "Define p()=Prgm:Disp 1:EndPrgm"}, locked={"f"})
    out = lib.load(doc, None, overwrite=True)
    assert [c["name"] for c in out["conflicts"]] == ["f"] and "locked" in out["conflicts"][0]["reason"]
    assert doc.stores() == []
    out = lib.load(doc, None, names=["p"])
    assert out["conflicts"][0]["reason"].startswith("a program")


def test_load_variable_of_other_type_conflicts():
    lib.save(FakeCAS(), None, None, ["Define f(x)=x^2"])
    out = lib.load(FakeCAS({"f": "f:=3"}), None)
    assert out["conflicts"] and "NUM" in out["conflicts"][0]["reason"]


def test_load_names_and_errors():
    lib.save(FakeCAS(), None, None, ["Define f(x)=x^2", "Define h(x)=x+1"])
    doc = FakeCAS()
    assert lib.load(doc, None, names=["H"])["loaded"] == ["h"]
    with pytest.raises(lib.LibraryError, match="Not in library"):
        lib.load(doc, None, names=["zz"])
    with pytest.raises(lib.LibraryError, match="empty"):
        lib.load(doc, "nothing")


def test_load_entry_failing_in_this_document_stores_nothing(libdir):
    lib.save(FakeCAS(), None, None, ["Define f(x)=x^2"])
    path = libdir / "default.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["entries"].append({"name": "h", "kind": "function", "params": ["x"], "command": "Define h(x)=BAD(x)"})
    path.write_text(json.dumps(data), encoding="utf-8")
    doc = FakeCAS()
    out = lib.load(doc, None)
    assert [e["name"] for e in out["errors"]] == ["h"] and doc.stores() == [] and doc.defs == {}
    assert any(b and b[0].startswith("DelVar mcplibk") for b in doc.batches)  # cleanup ran


def test_hand_edited_entry_must_match_its_command(libdir):
    lib.write_library(None, [{"name": "f", "kind": "function", "params": [], "command": "Define g(x)=x"}])
    with pytest.raises(lib.LibraryError, match="doesn't match"):
        lib.load(FakeCAS(), None)


# ------------------------------------------------------------ the file

def test_corrupt_file_is_never_overwritten(libdir):
    libdir.mkdir(parents=True)
    path = libdir / "default.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(lib.LibraryError, match="valid JSON"):
        lib.save(FakeCAS(), None, None, ["Define f(x)=x"])
    assert path.read_text(encoding="utf-8") == "{not json"
    listed = lib.list_libraries()
    assert listed["libraries"][0]["error"] and listed["libraries"][0]["entries"] == []


def test_list_and_remove(libdir):
    lib.save(FakeCAS(), "a", None, ["Define f(x)=x", "Define h(x)=2x"])
    lib.save(FakeCAS(), "b", None, ["k:=1"])
    listed = lib.list_libraries()
    assert listed["folder"] == str(libdir)
    assert [(x["library"], len(x["entries"])) for x in listed["libraries"]] == [("a", 2), ("b", 1)]
    assert lib.list_libraries("nope")["libraries"][0]["exists"] is False
    out = lib.remove_entries("a", ["F", "zz"])
    assert out["removed"] == ["f"] and out["missing"] == ["zz"]
    assert [e["name"] for e in lib.read_library("a")] == ["h"]


def test_summary():
    text = lib.summary({"library": "d", "file": "x.json", "loaded": ["f"], "conflicts": [
        {"name": "g", "reason": "different"}], "errors": []}, "load")
    assert "loaded: f" in text and "g (different)" in text
