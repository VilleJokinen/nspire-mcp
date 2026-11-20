"""Offline transport tests: nspire_bridge.py against bridge.lua run in lupa,
with a fake clipboard. Never touches the real Windows clipboard.

    uv run --no-project --with pywin32 --with lupa --with pytest python -m pytest tests/test_transport.py -q

Each bridge test runs against the current bridge.lua and the protocol-1 script
from before batch support (tests/fixtures/bridge_protocol1.lua, or OLD_LUA).
Skipped by tests/conftest.py where pywin32 or lupa is missing.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(os.environ.get("NSPIRE_REPO", Path(__file__).resolve().parent.parent)).resolve()
sys.path.insert(0, str(REPO))
import nspire_bridge as nb  # noqa: E402

nb.MUTEX_NAME = "Local\\nspire_mcp_bridge_offline_test"  # don't block a real bridge
CF_TEXT = 13
IMG = 8


class FakeClipboard:
    def __init__(self):
        self.lock = threading.RLock()
        self.formats: dict[int, bytes] = {}
        self.seq = 1
        self.history_excluded = False
        self.log: list[str] = []

    def _set(self, formats, excluded):
        with self.lock:
            self.formats = formats
            self.seq += 1
            self.history_excluded = excluded

    def text(self):
        with self.lock:
            b = self.formats.get(CF_TEXT)
            return None if b is None else b.decode("utf-16-le").split("\0", 1)[0]

    def set_text(self, text, excluded=False):
        self._set({CF_TEXT: (text + "\0").encode("utf-16-le")}, excluded)

    def user_copy_image(self, data=b"PICTURE"):
        self._set({IMG: data, 0xC123: b"<html>"}, False)


clip = FakeClipboard()


def _write_text(text, transient):
    clip.set_text(text, excluded=transient)
    seq = clip.seq
    if transient:
        nb._ours_seq = seq
    return seq


def _snapshot():
    with clip.lock:
        return nb.ClipSnapshot(items=list(clip.formats.items()))


def _restore(snap):
    clip._set(dict(snap.items) if snap else {}, excluded=bool(snap and snap.items))


nb.clip_get = clip.text
nb._write_text = _write_text
nb.clip_snapshot = _snapshot
nb.clip_restore = _restore
nb._seq = lambda: clip.seq


class FakeNspire:
    """Runs bridge.lua in lupa; math.evalStr is a tiny fake CAS."""

    def __init__(self, lua_path: Path):
        import lupa
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        self.vars: dict[str, str] = {}
        self.evals: list[str] = []
        g = self.lua.globals()
        g.platform = self.lua.table(apiLevel="2.7", window=self.lua.table(invalidate=lambda self_: None))
        g.timer = self.lua.table(start=lambda p: None)
        g.on = self.lua.table()
        self.written: list[str] = []
        g.clipboard = self.lua.table(getText=clip.text, addText=self.add_text)
        g.math = self.lua.table(evalStr=self.eval_str)
        self.lua.execute(lua_path.read_text(encoding="utf-8"))
        self.on_timer = g.on.timer
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def add_text(self, t):
        self.written.append(t)
        clip.set_text(t)

    def _run(self):
        while not self.stop.is_set():
            self.on_timer()
            time.sleep(0.05)

    def eval_str(self, expr):
        self.evals.append(expr)
        if expr == "copyduring":
            clip.set_text("copied while evaluating")
            return "cd", None
        if expr.startswith("slow"):
            time.sleep(float(expr[4:]))
            return "slowdone", None
        if expr == "2+2":
            return "4", None
        if expr == "weird":
            return 'a:b;c%25d\r\ne"', None
        if expr == "bad" or expr.startswith("]"):
            return None, 910
        if expr == "expand((brgcasp+brgcasq)^2)":
            return "brgcasp^2+2*brgcasp*brgcasq+brgcasq^2", None
        if ":=" in expr:
            k, v = expr.split(":=")
            self.vars[k] = v
            return v, None
        if expr == "getVarInfo()":
            if not self.vars:
                return '"NONE"', None
            rows = "".join(f'["{k}","NUM","none",0]' for k in self.vars)
            return f"[{rows}]", None
        if expr.startswith("DelVar "):
            for n in expr[7:].split(","):
                self.vars.pop(n.strip(), None)
            return "Done", None
        if expr in self.vars:
            return self.vars[expr], None
        return f"<{expr}>", None

    def close(self):
        self.stop.set()
        self.thread.join(2)


NEW_LUA = REPO / "bridge.lua"
OLD_LUA = Path(os.environ.get("OLD_LUA") or Path(__file__).resolve().parent / "fixtures" / "bridge_protocol1.lua")


@pytest.fixture(params=["new", "old"])
def env(request):
    if request.param == "old" and OLD_LUA is None:
        pytest.skip("OLD_LUA not set")
    clip.set_text("user text")
    ns = FakeNspire(NEW_LUA if request.param == "new" else OLD_LUA)
    b = nb.Bridge()
    yield request.param, ns, b
    ns.close()
    w = b._watcher
    if w:
        w.halt.set()
        w.join(2)


def test_pure_helpers():
    s = 'x:y;z%41\r\n'
    assert nb._unesc(nb._esc(s)) == s
    assert nb.parse_batch_reply("BATCH:0:") == []
    assert nb.parse_batch_reply("BATCH:2:O4;Eerror 910 (syntax error)") == [(True, "4"), (False, "error 910 (syntax error)")]
    assert nb.parse_batch_reply("BATCH:2:O4") is None
    assert nb.parse_batch_reply("4") is None
    assert nb._clamp_timeout(0) == 0.5 and nb._clamp_timeout(1e9) == 120 and nb._clamp_timeout("x") == 5
    assert nb._clamp_timeout(float("nan")) == 5
    assert nb.parse_var_info('"NONE"') == [] and nb.parse_var_info("NONE") == []
    assert nb.parse_var_info('[["a","NUM","none",0]["f","FUNC","LibPub ",1]]') == [
        ["a", "NUM", "none", "0"], ["f", "FUNC", "LibPub", "1"]]
    assert nb.parse_var_info('[["a" "NUM" "none" 0]]') == [["a", "NUM", "none", "0"]]
    assert nb.parse_var_info('["a","NUM","none",0]') == [["a", "NUM", "none", "0"]]
    assert list(nb._chunks(list(range(60)), ["1"] * 60)) == [list(range(0, 25)), list(range(25, 50)), list(range(50, 60))]
    assert nb._NAME_RE.match("a1") and nb._NAME_RE.match("θ") and nb._NAME_RE.match("stat.results")
    assert not nb._NAME_RE.match("f(x)") and not nb._NAME_RE.match("a:=1") and not nb._NAME_RE.match("1a")


def test_ping_eval_restore(env):
    kind, ns, b = env
    st = b.session(b.ping)
    assert st["cas"] is True and st["protocol"] == (2 if kind == "new" else 1)
    assert b.session(lambda: b.eval_one("2+2", 3)) == "4"
    assert clip.text() == "user text"
    assert clip.history_excluded  # the restore isn't added to history again


def test_traffic_excluded_from_history(env):
    kind, ns, b = env
    seen = []

    def work():
        b._post("NSPIRE_IN:probe:2+2")
        seen.append(clip.history_excluded)
        return b.eval_one("2+2", 3)
    b.session(work)
    assert seen == [True]


def test_image_restored(env):
    kind, ns, b = env
    clip.user_copy_image()
    assert b.session(lambda: b.eval_one("2+2", 3)) == "4"
    assert clip.formats == {IMG: b"PICTURE", 0xC123: b"<html>"}


def test_batch(env):
    kind, ns, b = env
    ns.evals.clear()
    res = b.session(lambda: b.eval_batch(["2+2", "weird", "bad", "", "a:=5", "a"], 3))
    assert [r["ok"] for r in res] == [True, True, False, False, True, True]
    assert res[1]["result"] == 'a:b;c%25d\r\ne"'
    assert "syntax error" in res[2]["error"]
    assert res[3]["error"] == "Empty expression."
    assert res[5]["result"] == "5"
    assert [r["expr"] for r in res] == ["2+2", "weird", "bad", "", "a:=5", "a"]
    if kind == "new":
        assert len(ns.written) == 2  # ping + one batch reply
        assert any(e == "weird" for e in ns.evals)
        assert not any(e.startswith("]") for e in ns.evals)  # batch handled by Lua, not evalStr
    else:
        assert any(e.startswith("]") for e in ns.evals) is False  # old: protocol 1 known from ping, no batch sent
    assert clip.text() == "user text"


def test_user_copy_during_request_survives(env):
    kind, ns, b = env
    t = threading.Timer(0.4, clip.user_copy_image)
    t.start()
    assert b.session(lambda: b.eval_one("slow1", 5)) == "slowdone"
    t.join()
    assert clip.formats.get(IMG) == b"PICTURE"


def test_timeout_then_late_reply(env):
    kind, ns, b = env
    with pytest.raises(nb.BridgeTimeout):
        b.session(lambda: b.eval_one("slow2", 0.5))
    assert clip.text() == "user text"
    time.sleep(2.5)  # Lua finishes; old script writes the late reply, watcher puts it back
    assert clip.text() == "user text"
    late = [w for w in ns.written if w.endswith(":OK:slowdone")]
    assert len(late) == (1 if kind == "old" else 0)  # new script holds it
    # the next request works
    assert b.session(lambda: b.eval_one("2+2", 3)) == "4"
    assert clip.text() == "user text"


def test_request_while_lua_busy(env):
    kind, ns, b = env
    with pytest.raises(nb.BridgeTimeout):
        b.session(lambda: b.eval_one("slow1.5", 0.5))
    # still evaluating: this request is overwritten by the late reply (old) or waits (new)
    assert b.session(lambda: b.eval_one("2+2", 5)) == "4"
    assert clip.text() == "user text"


def test_user_copy_after_timeout_survives_late_reply(env):
    kind, ns, b = env
    with pytest.raises(nb.BridgeTimeout):
        b.session(lambda: b.eval_one("slow1.5", 0.5))
    time.sleep(0.3)
    clip.set_text("newer copy")
    time.sleep(2)
    assert clip.text() == "newer copy"


def test_stale_traffic_at_start_is_not_saved(env):
    kind, ns, b = env
    clip.set_text("NSPIRE_OUT:deadbeef:OK:42")
    assert b.session(lambda: b.eval_one("2+2", 3)) == "4"
    assert clip.formats == {}  # emptied, not left as traffic


def test_transient_clip_set_in_session_restored(env):
    kind, ns, b = env

    def work():
        b.eval_one("2+2", 3)
        nb.clip_set("factor(x^2-1)")  # as nspire_notes pastes math
        assert clip.history_excluded
    b.session(work)
    assert clip.text() == "user text"
    nb.clip_set("result for the user")  # outside a session: an ordinary copy
    assert not clip.history_excluded


def test_vars_and_clear(env):
    kind, ns, b = env
    ns.vars.clear()
    assert b.session(b.list_vars) == []
    b.session(lambda: b.eval_batch(["a:=5", "b:=7"], 3))
    v = b.session(b.list_vars)
    assert [(x["name"], x["type"], x.get("value"), x["locked"], x["library"]) for x in v] == [
        ("a", "NUM", "5", False, None), ("b", "NUM", "7", False, None)]
    r = b.session(lambda: b.clear_vars(["a"]))
    assert r == {"deleted": ["a"], "errors": []}
    with pytest.raises(ValueError):
        b.session(lambda: b.clear_vars(["f(x)"]))
    r = b.session(lambda: b.clear_vars())
    assert r["deleted"] == ["b"] and ns.vars == {}
    assert clip.text() == "user text"


def test_dead_bridge_batch_skips(env):
    kind, ns, b = env
    ns.close()
    res = b.session(lambda: b.eval_batch(["2+2", "3+3", "4+4"], 0.5))
    assert not res[0]["ok"] and "No reply" in res[0]["error"]
    assert all(r["error"].startswith("skipped: ") for r in res[1:])
    assert clip.text() == "user text"


def test_lua_held_reply_and_resend():
    """bridge.lua alone: a reply is held when the clipboard changed during the
    evaluation, and posting the request again resends it without re-evaluating."""
    clip.set_text("user text")
    ns = FakeNspire(NEW_LUA)
    ns.close()  # drive the timer by hand
    clip.set_text("NSPIRE_IN:r1:copyduring")
    ns.on_timer()
    assert ns.written == [] and clip.text() == "copied while evaluating"
    ns.on_timer()
    assert ns.written == []
    clip.set_text("NSPIRE_IN:r1:copyduring")
    ns.on_timer()
    assert ns.written == ["NSPIRE_OUT:r1:OK:cd"] and ns.evals.count("copyduring") == 1
    ns.on_timer()
    assert len(ns.written) == 1
    # an overwritten reply is resent when asked again, even straight after delivery
    clip.set_text("NSPIRE_IN:r1:copyduring")
    ns.on_timer()
    assert ns.written[-1] == "NSPIRE_OUT:r1:OK:cd" and len(ns.written) == 2
    # batch with an empty body and with special characters
    clip.set_text("NSPIRE_IN:r2:]BATCH:")
    ns.on_timer()
    assert ns.written[-1] == "NSPIRE_OUT:r2:OK:BATCH:0:"
    clip.set_text("NSPIRE_IN:r3:]BATCH:weird;2%2B2;bad")
    ns.on_timer()
    assert ns.evals[-3:] == ["weird", "2+2", "bad"]
    assert ns.written[-1] == "NSPIRE_OUT:r3:OK:BATCH:3:Oa:b%3Bc%2525d%0D%0Ae\";O4;Eerror 910 (syntax error)"
    assert nb.parse_batch_reply(ns.written[-1].split(":OK:", 1)[1])[0] == (True, 'a:b;c%25d' + chr(13) + chr(10) + 'e"')
    clip.set_text("NSPIRE_IN:r4:__PING__")
    ns.on_timer()
    assert ns.written[-1] == "NSPIRE_OUT:r4:OK:PONG:2.7:cas:2"
