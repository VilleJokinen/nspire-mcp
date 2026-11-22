"""Offline tests of the macOS pasteboard backend (nspire_clip_mac) with a fake
NSPasteboard, on any OS. Also: the bridge end to end over that backend (needs
lupa), the file lock (needs fcntl: macOS/Linux), and that server.py starts on
macOS without importing Windows modules.

With NSPIRE_REAL_PASTEBOARD=1 on a Mac with pyobjc, one test also round-trips
through the real pasteboard (CI only: it replaces your clipboard for a moment).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import nspire_clip_mac as mac  # noqa: E402

TEXT = mac.TEXT_TYPE


class FakeItem:
    """NSPasteboardItem: types -> bytes."""

    def __init__(self):
        self.data: dict[str, bytes] = {}

    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self

    def setString_forType_(self, s, t):
        self.data[t] = s.encode("utf-8")
        return True

    def setData_forType_(self, d, t):
        self.data[t] = bytes(d)
        return True

    def types(self):
        return list(self.data)

    def dataForType_(self, t):
        d = self.data.get(t)
        if isinstance(d, Exception):
            raise d
        return d


class FakePasteboard:
    """NSPasteboard.generalPasteboard(): the calls nspire_clip_mac makes."""

    def __init__(self):
        self.lock = threading.RLock()
        self.items: list[FakeItem] = []
        self.count = 1
        self.options: list[int] = []  # prepareForNewContentsWithOptions_ arguments, in order

    def changeCount(self):
        return self.count

    def clearContents(self):
        with self.lock:
            self.items = []
            self.count += 1
            self.options.append(0)
            return self.count

    def prepareForNewContentsWithOptions_(self, opt):
        with self.lock:
            self.items = []
            self.count += 1
            self.options.append(opt)
            return self.count

    def writeObjects_(self, items):
        with self.lock:
            self.items = list(items)
            return True

    def pasteboardItems(self):
        with self.lock:
            return list(self.items)

    def stringForType_(self, t):
        with self.lock:
            for it in self.items:
                if t in it.data:
                    return it.data[t].decode("utf-8")
            return None

    # as another program would write
    def user_copy(self, *items: dict[str, bytes]):
        with self.lock:
            self.clearContents()
            self.options.pop()
            fakes = []
            for types_ in items:
                f = FakeItem()
                f.data.update(types_)
                fakes.append(f)
            self.items = fakes

    def flat(self):
        with self.lock:
            return [dict(it.data) for it in self.items]


@pytest.fixture
def pb(monkeypatch):
    pb = FakePasteboard()
    fake = types.SimpleNamespace(pb=pb, Item=FakeItem, data=lambda b: bytes(b), pool=lambda: _Null())
    monkeypatch.setattr(mac, "_cocoa_ns", fake)
    return pb


class _Null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ------------------------------------------------------------ backend primitives

def test_write_text_transient_and_plain(pb):
    n = mac.write_text("NSPIRE_IN:1:2+2", transient=True)
    assert n == pb.count == mac.seq()
    assert mac.clip_get() == "NSPIRE_IN:1:2+2"
    item = pb.flat()[0]
    assert set(mac.TRAFFIC_MARKS) <= set(item)  # clipboard managers skip it
    assert pb.options[-1] == mac.CURRENT_HOST_ONLY  # not sent to the user's other devices
    n2 = mac.write_text("result for the user", transient=False)
    assert n2 == n + 1
    assert pb.flat() == [{TEXT: b"result for the user"}]  # an ordinary copy: no marks
    assert pb.options[-1] == 0


def test_unicode_round_trip(pb):
    s = "x=−2 or x=2 ∫ ℯ^(𝑖π) "
    mac.write_text(s, transient=True)
    assert mac.clip_get() == s
    assert mac.clip_snapshot().text() == s


def test_clip_get_none_without_text(pb):
    assert mac.clip_get() is None
    pb.user_copy({"public.png": b"PNG"})
    assert mac.clip_get() is None


def test_snapshot_and_restore_every_item_and_type(pb):
    pb.user_copy({"public.file-url": b"file:///a", TEXT: b"a"}, {"public.file-url": b"file:///b"},
                 {"public.png": b"PNG", "public.html": b"<b>"})
    snap = mac.clip_snapshot()
    assert snap.text() == "a" and len(snap.items) == 3 and not snap.skipped
    mac.write_text("NSPIRE_OUT:1:OK:4", transient=True)
    before = pb.count
    mac.clip_restore(snap)
    assert pb.count == before + 1
    got = pb.flat()
    assert got[0].pop(mac.TRANSIENT_TYPE) == b""  # the restore isn't recorded again
    assert got == [{"public.file-url": b"file:///a", TEXT: b"a"}, {"public.file-url": b"file:///b"},
                   {"public.png": b"PNG", "public.html": b"<b>"}]
    assert pb.options[-1] == mac.CURRENT_HOST_ONLY


def test_restore_keeps_the_copys_own_marks(pb):
    pb.user_copy({TEXT: b"secret", mac.CONCEALED_TYPE: b"", mac.TRANSIENT_TYPE: b"x"})
    mac.clip_restore(mac.clip_snapshot())
    assert pb.flat() == [{TEXT: b"secret", mac.CONCEALED_TYPE: b"", mac.TRANSIENT_TYPE: b"x"}]


def test_restore_none_empties(pb):
    pb.user_copy({TEXT: b"a"})
    mac.clip_restore(None)
    assert pb.flat() == []
    mac.clip_restore(mac.ClipSnapshot())
    assert pb.flat() == []


def test_snapshot_skips_unreadable_and_oversized(pb, monkeypatch):
    pb.user_copy({TEXT: b"a", "com.example.promise": None, "com.example.broken": RuntimeError("gone")},
                 {"public.tiff": b"x" * 100})
    monkeypatch.setattr(mac, "SNAPSHOT_MAX_BYTES", 50)
    snap = mac.clip_snapshot()
    assert snap.items == [{TEXT: b"a"}]
    assert set(snap.skipped) == {"com.example.promise", "com.example.broken", "public.tiff"}


def test_snapshot_text_utf16_fallback():
    snap = mac.ClipSnapshot(items=[{"public.png": b"P"}, {mac.UTF16_TYPE: "hä".encode("utf-16-le")}])
    assert snap.text() == "hä"
    assert mac.ClipSnapshot().text() is None


def test_settle_waits_for_a_writer(pb):
    pb.clearContents()  # another program cleared the pasteboard and is about to write
    t = threading.Timer(0.02, lambda: pb.writeObjects_([_item({TEXT: b"late"})]))
    t.start()
    assert mac.clip_get() == "late"
    t.join()


def _item(data):
    f = FakeItem()
    f.data.update(data)
    return f


def test_unsupported_platform_message(monkeypatch):
    monkeypatch.setattr(mac, "_cocoa_ns", None)
    monkeypatch.setattr(mac.sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="Windows and macOS only"):
        mac.seq()


# ------------------------------------------------------------ cross-process lock

def test_process_lock(tmp_path):
    pytest.importorskip("fcntl")
    a = mac.ProcessLock("Local\\nspire test", directory=str(tmp_path))
    b = mac.ProcessLock("Local\\nspire test", directory=str(tmp_path))
    assert a.path == str(tmp_path / "Local_nspire_test.lock")
    assert a.acquire(0)
    t0 = time.monotonic()
    assert not b.acquire(0.2)  # another lock object (as in another process) waits, then gives up
    assert 0.15 < time.monotonic() - t0 < 2
    got = []
    t = threading.Thread(target=lambda: got.append(b.acquire(5)))
    t.start()
    time.sleep(0.1)
    a.release()
    t.join(5)
    assert got == [True]
    b.release()
    b.release()  # harmless
    assert a.acquire(0)
    a.release()


def test_process_lock_freed_when_holder_dies(tmp_path):
    pytest.importorskip("fcntl")
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); import nspire_clip_mac as m; "
            "assert m.ProcessLock('x', directory=sys.argv[2]).acquire(0); print('held', flush=True); "
            "time.sleep(30)")
    p = subprocess.Popen([sys.executable, "-c", code, str(REPO), str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        lock = mac.ProcessLock("x", directory=str(tmp_path))
        assert not lock.acquire(0)
        p.kill()
        p.wait(5)
        assert lock.acquire(2)
        lock.release()
    finally:
        p.kill()


# ------------------------------------------------------------ the bridge over the mac backend

class _ThreadLock:
    """Stands in for ProcessLock where fcntl is missing (Windows)."""

    def __init__(self, name):
        self._l = threading.Lock()

    def acquire(self, timeout_s):
        return self._l.acquire(timeout=max(timeout_s, 0))

    def release(self):
        self._l.release()


@pytest.fixture
def mac_bridge(pb, monkeypatch, tmp_path):
    lupa = pytest.importorskip("lupa")
    import nspire_bridge as nb
    try:
        import fcntl  # noqa: F401
        lock = lambda name: mac.ProcessLock(name, directory=str(tmp_path))  # noqa: E731
    except ImportError:
        lock = _ThreadLock
    backend = types.SimpleNamespace(**{n: getattr(mac, n) for n in (
        "clip_get", "write_text", "seq", "clip_snapshot", "clip_restore", "ClipSnapshot")}, ProcessLock=lock)
    monkeypatch.setattr(nb, "_clip", backend)

    # bridge.lua in lupa; TI-Nspire's clipboard reads and writes the fake pasteboard as Java would
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    g = lua.globals()
    g.platform = lua.table(apiLevel="2.7", window=lua.table(invalidate=lambda self_: None))
    g.timer = lua.table(start=lambda p: None)
    g.on = lua.table()
    written = []

    def add_text(t):
        written.append(t)
        pb.clearContents()
        pb.options.pop()
        pb.writeObjects_([_item({TEXT: t.encode("utf-8")})])
    g.clipboard = lua.table(getText=lambda: pb.stringForType_(TEXT), addText=add_text)
    answers = {"2+2": "4", "expand((brgcasp+brgcasq)^2)": "brgcasp^2+2*brgcasp*brgcasq+brgcasq^2"}
    g.math = lua.table(evalStr=lambda e: (answers[e], None) if e in answers else (None, 910))
    lua.execute((REPO / "bridge.lua").read_text(encoding="utf-8"))
    on_timer, stop = g.on.timer, threading.Event()

    def run():
        while not stop.is_set():
            on_timer()
            time.sleep(0.05)
    th = threading.Thread(target=run, daemon=True)
    th.start()
    b = nb.Bridge()
    yield b, written
    stop.set()
    th.join(2)
    if b._watcher:
        b._watcher.halt.set()
        b._watcher.join(2)


def test_bridge_over_mac_backend(pb, mac_bridge):
    b, written = mac_bridge
    pb.user_copy({"public.png": b"PNG", "public.html": b"<img>"}, {TEXT: b"second item"})
    user = pb.flat()
    st = b.session(b.ping)
    assert st["protocol"] == 2 and st["cas"] is True
    assert b.session(lambda: b.eval_one("2+2", 3)) == "4"
    res = b.session(lambda: b.eval_batch(["2+2", "bad"], 3))
    assert [r["ok"] for r in res] == [True, False]
    got = pb.flat()
    assert got[0].pop(mac.TRANSIENT_TYPE) == b""
    assert got == user  # the user's two items came back, every type
    assert len(written) == 4  # ping, eval, ping (once per session before a batch), batch


def test_bridge_requests_marked_transient(pb, mac_bridge):
    b, _ = mac_bridge
    seen = []

    def work():
        b._post("NSPIRE_IN:probe:2+2")
        seen.append(pb.flat()[0])
        return b.eval_one("2+2", 3)
    pb.user_copy({TEXT: b"user text"})
    b.session(work)
    assert set(mac.TRAFFIC_MARKS) <= set(seen[0])
    assert pb.stringForType_(TEXT) == "user text"


# ------------------------------------------------------------ startup on macOS

def test_mac_startup_imports_no_windows_modules():
    # A fresh interpreter that believes it is on macOS, with pywin32 blocked.
    # The standard library (and mcp, if present) is imported first, as it would
    # be on a real Mac; only this project's modules see the patched platform.
    code = r"""
import sys, asyncio, uuid, tempfile, logging, importlib.util
have_mcp = importlib.util.find_spec("mcp") is not None
if have_mcp:
    import mcp.server.fastmcp, mcp.types, pydantic, typing_extensions
before = set(sys.modules)
for m in ("win32clipboard", "win32event", "win32api", "win32con", "win32gui", "win32process", "pywintypes", "winerror"):
    sys.modules[m] = None
sys.platform = "darwin"
sys.path.insert(0, ".")
import nspire_bridge
assert nspire_bridge._clip.__name__ == "nspire_clip_mac"
if have_mcp:
    import server
import nspire_notes
try:
    nspire_notes.build_notes(None, [{"text": "hi"}])
except RuntimeError as e:
    assert "Windows only" in str(e), e
else:
    raise AssertionError("build_notes ran")
assert nspire_notes.scan_names("a:=5").defines == ("a",)
bad = [m for m in ("nspire_clip_win", "nspire_keys", "AppKit", "objc", "ctypes.wintypes")
       if m in sys.modules and m not in before]
print("ok" if not bad else bad, have_mcp)
"""
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert out.returncode == 0, out.stderr
    assert out.stdout.split()[0] == "ok", out.stdout


def test_hotkey_refuses_on_mac():
    # hotkey imports on any OS (its tests run on Linux CI); main() refuses
    code = "import sys; sys.platform = 'darwin'; sys.path.insert(0, '.'); import hotkey; hotkey.main()"
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert out.returncode == 1 and "Windows only" in out.stderr


# ------------------------------------------------------------ the real pasteboard (CI on macOS)

@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("NSPIRE_REAL_PASTEBOARD") != "1",
                    reason="real pasteboard: macOS with NSPIRE_REAL_PASTEBOARD=1 only")
def test_real_pasteboard_round_trip():
    pytest.importorskip("AppKit")
    mac._cocoa_ns = None
    saved = mac.clip_snapshot()
    try:
        n = mac.seq()
        n2 = mac.write_text("NSPIRE_IN:ci:2+2 −ℯ", transient=True)
        assert n2 > n and mac.seq() == n2
        assert mac.clip_get() == "NSPIRE_IN:ci:2+2 −ℯ"
        snap = mac.clip_snapshot()
        assert snap.text() == "NSPIRE_IN:ci:2+2 −ℯ"
        assert set(mac.TRAFFIC_MARKS) <= set(snap.items[0])
        mac.write_text("plain", transient=False)
        assert mac.clip_get() == "plain"
        mac.clip_restore(snap)
        assert mac.clip_get() == "NSPIRE_IN:ci:2+2 −ℯ"
        mac.clip_restore(None)
        assert mac.clip_get() is None

        def other_thread():  # the MCP server calls in from worker threads
            mac.write_text("from a thread", transient=True)
        t = threading.Thread(target=other_thread)
        t.start()
        t.join(5)
        assert mac.clip_get() == "from a thread"
    finally:
        mac.clip_restore(saved)
