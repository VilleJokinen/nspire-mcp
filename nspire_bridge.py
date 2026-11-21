"""Clipboard transport to bridge.lua running in the TI-Nspire desktop app.

Shared by server.py (MCP) and hotkey.py. A named Windows mutex serialises
requests across processes, so both can run at once.

Each Bridge.session() saves every format on the user's clipboard that can be
copied as bytes, and puts it back at the end if the clipboard still holds the
bridge's traffic. Traffic is kept out of clipboard history and cloud sync.
"""
from __future__ import annotations

import ctypes
import logging
import re
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass, field

import pywintypes
import win32clipboard as wcb
import win32event

log = logging.getLogger("nspire")

PREFIX_IN = "NSPIRE_IN:"
PREFIX_OUT = "NSPIRE_OUT:"
POLL_S = 0.03
TIMEOUT_HINT = ("Is TI-Nspire open with nspire_bridge.tns on the bridge page? If the page's tick count "
                "rises but 'served' doesn't, TI-Nspire has stopped seeing clipboard changes: close and "
                "reopen the document (or restart the app).")
MUTEX_NAME = "Local\\nspire_mcp_bridge"
TIMEOUT_MIN_S, TIMEOUT_MAX_S = 0.5, 120.0  # every timeout_s is clamped to this
BATCH_WAIT_MAX_S = 300.0     # a whole batch request waits at most this long
BATCH_MARK = "]BATCH:"       # starts with ']' so an older bridge.lua fails it as a syntax error
BATCH_MAX_ITEMS = 25
BATCH_MAX_CHARS = 4000
MAX_REPOSTS = 5              # times one request is posted again after something overwrote it
LATE_WINDOW_S = 120.0        # how long to watch for a reply to a request that timed out
SNAPSHOT_MAX_BYTES = 128 * 2**20


class BridgeTimeout(Exception):
    pass


# The CAS's private-use characters live in nspire_chars (no pywin32 there, so
# the LaTeX translator imports on any platform). Re-exported for callers.
from nspire_chars import NS_C, NS_E, NS_EXP, NS_I, NS_N, readable, to_cas_chars  # noqa: E402,F401


# ---------------------------------------------------------------- clipboard

def _with_clipboard(fn, retries: int = 40):
    """Open the clipboard, run fn, close it. Retries while another app holds it."""
    for _ in range(retries):
        try:
            wcb.OpenClipboard()
        except pywintypes.error:
            time.sleep(0.025)
            continue
        try:
            return fn()
        finally:
            wcb.CloseClipboard()
    raise RuntimeError("Windows clipboard stayed locked by another program; try again.")


def clip_get() -> str | None:
    def read():
        if not wcb.IsClipboardFormatAvailable(wcb.CF_UNICODETEXT):
            return None
        return wcb.GetClipboardData(wcb.CF_UNICODETEXT)
    return _with_clipboard(read)


# Registered formats that keep the current clipboard out of Win+V history and
# cloud clipboard sync when set to DWORD 0. (Not ExcludeClipboardContentFrom-
# MonitorProcessing: TI-Nspire watches the clipboard and must see requests.)
_CF_HISTORY = wcb.RegisterClipboardFormat("CanIncludeInClipboardHistory")
_CF_CLOUD = wcb.RegisterClipboardFormat("CanUploadToCloudClipboard")
_DWORD_0 = b"\0\0\0\0"

_tls = threading.local()      # .in_session: this thread is inside Bridge.session()
_ours_seq: int | None = None  # clipboard sequence number when it last held this process's traffic


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_GMEM_MOVEABLE = 0x0002


def _seq() -> int:
    return wcb.GetClipboardSequenceNumber()


def _set_data(fmt: int, data: bytes) -> None:
    """SetClipboardData with memory allocated here, exactly len(data) bytes.

    Never hand pywin32 a str or bytes: its str conversion fails on some short
    strings such as "11" (error 6, invalid handle) and corrupts the heap, so
    the next write crashes Python; and it adds a byte to text formats, which
    would grow the user's clipboard on every restore. Clipboard must be open.
    """
    h = _kernel32.GlobalAlloc(_GMEM_MOVEABLE, max(len(data), 1))
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    p = _kernel32.GlobalLock(h)
    if not p:
        _kernel32.GlobalFree(h)
        raise ctypes.WinError(ctypes.get_last_error())
    ctypes.memmove(p, data, len(data))
    _kernel32.GlobalUnlock(h)
    try:
        wcb.SetClipboardData(fmt, h)  # an int is passed through as the handle; the clipboard owns it now
    except Exception:
        _kernel32.GlobalFree(h)
        raise


def _keep_out_of_history() -> None:
    """Mark what is on the clipboard as not for history or cloud sync. Clipboard must be open."""
    for fmt in (_CF_HISTORY, _CF_CLOUD):
        try:
            _set_data(fmt, _DWORD_0)
        except Exception as e:
            log.debug("could not set clipboard format %d: %s", fmt, e)


def _write_text(text: str, transient: bool) -> int:
    """Replace the clipboard with text. Returns the clipboard sequence number after the write."""
    global _ours_seq
    data = (text + "\0").encode("utf-16-le", errors="surrogatepass")

    def write():
        wcb.EmptyClipboard()
        _set_data(wcb.CF_UNICODETEXT, data)
        if transient:
            _keep_out_of_history()
    _with_clipboard(write)
    seq = _seq()
    if transient:
        _ours_seq = seq
    return seq


def clip_set(text: str) -> None:
    """Put text on the clipboard.

    Inside Bridge.session() the text is temporary (e.g. math pasted into
    TI-Nspire): it is kept out of clipboard history, and the user's clipboard
    replaces it when the session ends. Outside a session it is an ordinary copy.
    """
    _write_text(text, getattr(_tls, "in_session", False))


def is_bridge_traffic(text: str | None) -> bool:
    return text is not None and text.startswith((PREFIX_IN, PREFIX_OUT))


def _reply_id(text: str | None) -> str | None:
    if text is not None and text.startswith(PREFIX_OUT):
        return text[len(PREFIX_OUT):].split(":", 1)[0]
    return None


# ---------------------------------------------------------------- snapshots

# Formats whose data is a GDI object or an owner-private handle rather than
# memory, so it can't be copied as bytes. Windows rebuilds bitmaps and palettes
# from CF_DIB/CF_DIBV5 and old-style metafiles from CF_ENHMETAFILE, which are kept.
_CF_DIB, _CF_ENHMETAFILE, _CF_DIBV5 = 8, 14, 17
_HANDLE_FORMATS = {
    2,     # CF_BITMAP
    3,     # CF_METAFILEPICT
    9,     # CF_PALETTE
    0x80,  # CF_OWNERDISPLAY
    0x82,  # CF_DSPBITMAP
    0x83,  # CF_DSPMETAFILEPICT
    0x8E,  # CF_DSPENHMETAFILE
}
# OLE's links back to the program that copied. They go stale once that program
# no longer owns the clipboard; without them OLE reads the ordinary formats.
_OLE_LINK_FORMATS = {"DataObject", "Ole Private Data"}

_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_gdi32.GetEnhMetaFileBits.argtypes = [wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p]
_gdi32.GetEnhMetaFileBits.restype = wintypes.UINT
_gdi32.SetEnhMetaFileBits.argtypes = [wintypes.UINT, ctypes.c_char_p]
_gdi32.SetEnhMetaFileBits.restype = wintypes.HANDLE
_gdi32.DeleteEnhMetaFile.argtypes = [wintypes.HANDLE]
_gdi32.DeleteEnhMetaFile.restype = wintypes.BOOL


@dataclass
class ClipSnapshot:
    """A copy of the clipboard: (format, bytes) pairs, CF_ENHMETAFILE as metafile bits."""
    items: list[tuple[int, bytes]] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)

    def text(self) -> str | None:
        for fmt, data in self.items:
            if fmt == wcb.CF_UNICODETEXT:
                return data.decode("utf-16-le", errors="replace").split("\0", 1)[0]
        return None


def _format_name(fmt: int) -> str | None:
    if fmt < 0xC000:  # only registered formats have names
        return None
    try:
        return wcb.GetClipboardFormatName(fmt)
    except pywintypes.error:
        return None


def _emf_bits(handle: int) -> bytes:
    n = _gdi32.GetEnhMetaFileBits(handle, 0, None)
    buf = ctypes.create_string_buffer(n)
    if not n or _gdi32.GetEnhMetaFileBits(handle, n, buf) != n:
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.raw


def _read_all() -> ClipSnapshot:
    """Copy every format that can be copied as bytes. Clipboard must be open."""
    fmts, f = [], 0
    while True:
        f = wcb.EnumClipboardFormats(f)
        if not f:
            break
        fmts.append(f)
    snap, total, have_dib = ClipSnapshot(), 0, False
    for f in fmts:
        if f in _HANDLE_FORMATS or 0x200 <= f <= 0x3FF or _format_name(f) in _OLE_LINK_FORMATS:
            snap.skipped.append(f)
            continue
        if f in (_CF_DIB, _CF_DIBV5) and have_dib:
            continue  # Windows makes either one from the other: keep one copy of the image
        try:
            # This asks the program that copied to render formats it offered
            # without data yet ("delayed rendering").
            h = wcb.GetClipboardDataHandle(f)
            data = _emf_bits(h) if f == _CF_ENHMETAFILE else wcb.GetGlobalMemory(h)
        except Exception:  # rendering failed, stream-only OLE data, ...
            snap.skipped.append(f)
            continue
        if total + len(data) > SNAPSHOT_MAX_BYTES:
            snap.skipped.append(f)
            continue
        total += len(data)
        have_dib = have_dib or f in (_CF_DIB, _CF_DIBV5)
        snap.items.append((f, bytes(data)))
    if snap.skipped:
        log.debug("clipboard snapshot skipped formats %s", snap.skipped)
    return snap


def _write_all(snap: ClipSnapshot | None) -> None:
    """Replace the clipboard with snap; empty it if snap is None. Clipboard must be open."""
    wcb.EmptyClipboard()
    items = [(f, d) for f, d in (snap.items if snap else []) if f not in (_CF_HISTORY, _CF_CLOUD)]
    if not items:
        return
    for f, data in items:
        try:
            if f == _CF_ENHMETAFILE:
                h = _gdi32.SetEnhMetaFileBits(len(data), data)
                if not h:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    wcb.SetClipboardData(f, h)  # the clipboard owns h from here
                except Exception:
                    _gdi32.DeleteEnhMetaFile(h)
                    raise
            else:
                _set_data(f, data)
        except Exception as e:
            log.warning("could not restore clipboard format %d: %s", f, e)
    # The copy went into history when the user made it; don't add it twice.
    _keep_out_of_history()


def clip_snapshot() -> ClipSnapshot:
    """Copy everything on the clipboard that can be copied as bytes."""
    return _with_clipboard(_read_all)


def clip_restore(snap: ClipSnapshot | None) -> None:
    """Put a snapshot back (kept out of clipboard history). None empties the clipboard."""
    _with_clipboard(lambda: _write_all(snap))


# ---------------------------------------------------------------- protocol helpers

def _clamp_timeout(timeout_s, default: float = 5.0) -> float:
    try:
        t = float(timeout_s)
    except (TypeError, ValueError):
        t = default
    if t != t:  # NaN
        t = default
    return min(max(t, TIMEOUT_MIN_S), TIMEOUT_MAX_S)


def _check_expr(expr: str) -> None:
    if not expr.strip():
        raise ValueError("Empty expression.")
    if "\n" in expr or "\r" in expr:
        raise ValueError("Expression must be a single line.")


def _esc(s: str) -> str:
    return re.sub(r"[%;\r\n]", lambda m: "%%%02X" % ord(m.group()), s)


def _unesc(s: str) -> str:
    return re.sub(r"%([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)


def encode_batch(exprs: list[str]) -> str:
    """Request body for bridge.lua protocol 2: ]BATCH:<e1>;<e2>;... ('%', ';', CR, LF escaped as %XX)."""
    return BATCH_MARK + ";".join(_esc(e) for e in exprs)


def parse_batch_reply(body: str) -> list[tuple[bool, str]] | None:
    """BATCH:<n>:<s1><r1>;<s2><r2>;... -> [(ok, text), ...]; None if body isn't a batch reply."""
    if not body.startswith("BATCH:"):
        return None
    count, sep, rest = body[len("BATCH:"):].partition(":")
    if not sep or not count.isdigit():
        return None
    items = rest.split(";") if int(count) else []
    if len(items) != int(count) or any(not it or it[0] not in "OE" for it in items):
        return None
    return [(it[0] == "O", _unesc(it[1:])) for it in items]


def _chunks(todo: list[int], exprs: list[str]):
    chunk: list[int] = []
    size = 0
    for i in todo:
        n = len(_esc(exprs[i])) + 1
        if chunk and (len(chunk) >= BATCH_MAX_ITEMS or size + n > BATCH_MAX_CHARS):
            yield chunk
            chunk, size = [], 0
        chunk.append(i)
        size += n
    if chunk:
        yield chunk


_NAME_RE = re.compile(r"^[^\W\d_]\w*(?:\.\w+)?$")
VALUE_TYPES = {"NUM", "EXPR", "LIST", "MAT", "STR"}
VALUE_MAX_CHARS = 300

# getMode() numbers: mode -> (key, {setting: name}), as listed under setMode in
# the TI-Nspire Reference Guide. Unknown numbers are passed through as numbers.
_DIGITS = {1: "Float", **{k: f"Float{k - 1}" for k in range(2, 14)}, **{k: f"Fix{k - 14}" for k in range(14, 27)}}
MODES = {
    1: ("display_digits", _DIGITS),
    2: ("angle", {1: "Radian", 2: "Degree", 3: "Gradian"}),
    3: ("exponential_format", {1: "Normal", 2: "Scientific", 3: "Engineering"}),
    4: ("real_or_complex", {1: "Real", 2: "Rectangular", 3: "Polar"}),
    5: ("auto_or_approx", {1: "Auto", 2: "Approximate", 3: "Exact"}),
    6: ("vector_format", {1: "Rectangular", 2: "Cylindrical", 3: "Spherical"}),
    7: ("base", {1: "Decimal", 2: "Hex", 3: "Binary"}),
    8: ("unit_system", {1: "SI", 2: "Eng/US"}),
}


def _mode_ints(text: str) -> list[int] | None:
    """'{1,7,2,1}' or '2' -> [1, 7, 2, 1] / [2]; None if it isn't integers."""
    t = text.strip()
    if t.startswith("{") and t.endswith("}"):
        t = t[1:-1]
    items = [i.strip().strip('"') for i in t.split(",")] if t.strip() else []
    if not items or not all(i.isdigit() for i in items):
        return None
    return [int(i) for i in items]


def mode_setting(mode: int, text: str) -> str | int | None:
    """Name of one mode's setting from getMode(mode)'s reply, e.g. (2, '2') -> 'Degree'."""
    nums = _mode_ints(text)
    if not nums or len(nums) != 1:
        return None
    return MODES.get(mode, ("", {}))[1].get(nums[0], nums[0])


def parse_modes(text: str) -> dict | None:
    """getMode(0)'s list of (mode, setting) pairs -> {"angle": "Radian", ...}; None if unreadable."""
    nums = _mode_ints(text)
    if not nums or len(nums) % 2:
        return None
    out: dict = {}
    for mode, setting in zip(nums[::2], nums[1::2]):
        key, names = MODES.get(mode, (f"mode_{mode}", {}))
        out[key] = names.get(setting, setting)
    return out


def parse_var_info(text: str) -> list[list[str]]:
    """Rows of getVarInfo()'s matrix, e.g. [["a","NUM","none",0]["f","FUNC","none",0]].

    Returns [] for "NONE" (no variables) or anything that isn't a matrix.
    """
    stack: list[list] = [[]]
    cell: str | None = None
    quoted = False
    for ch in text.strip():
        if quoted:
            if ch == '"':
                quoted = False
            else:
                cell += ch
        elif ch == '"':
            quoted = True
            cell = cell or ""
        elif ch == "[":
            stack.append([])
        elif ch in "], ":
            if cell is not None:
                stack[-1].append(cell.strip())
                cell = None
            if ch == "]" and len(stack) > 1:
                done = stack.pop()
                stack[-1].append(done)
        else:
            cell = (cell or "") + ch
    top = stack[0]
    if len(stack) != 1 or len(top) != 1 or not isinstance(top[0], list):
        return []
    m = top[0]
    if m and all(isinstance(r, list) for r in m):
        return [[c for c in r if isinstance(c, str)] for r in m]
    if m and not any(isinstance(c, list) for c in m):
        return [m]  # a single row written as a list
    return []


# ---------------------------------------------------------------- late replies

class _LateReplyWatcher(threading.Thread):
    """After a request timed out, watch for its late reply and put the user's
    clipboard back over it.

    Needed for bridge.lua protocol 1 (the .tns before batch support), which
    writes a reply whenever its evaluation finishes, even after Python gave up
    and restored the user's clipboard. Protocol 2 writes a reply only while the
    clipboard still holds its request, so this then just waits and ends.
    Watches the change counter only; reads the clipboard when it changes.
    """

    def __init__(self, bridge: Bridge, pending: dict[str, float], snap: ClipSnapshot | None):
        super().__init__(name="nspire-late-reply", daemon=True)
        self.bridge = bridge
        self.pending = pending  # request id -> time.monotonic() until which a reply may come
        self.snap = snap        # the user's clipboard, to put back over a late reply
        self.halt = threading.Event()

    def run(self) -> None:
        seq = _seq()
        while not self.halt.wait(0.1):
            now = time.monotonic()
            for rid in [r for r, t in self.pending.items() if t < now]:
                del self.pending[rid]
            if not self.pending:
                break
            cur = _seq()
            if cur == seq:
                continue
            seq = cur
            time.sleep(0.02)  # let the writer finish
            try:
                seq = self._changed(seq)
            except Exception as e:
                log.debug("late-reply watcher: %s", e)

    def _changed(self, seq: int) -> int:
        text = clip_get()
        rid = _reply_id(text)
        if rid in self.pending:
            del self.pending[rid]  # answered: no further reply will come for it
            if self._put_back(text):
                return _seq()
        elif not is_bridge_traffic(text) and seq != _ours_seq:
            # The user copied something: that is what to put back now.
            snap = clip_snapshot()
            if not is_bridge_traffic(snap.text()):
                self.snap = snap
        return seq

    def _put_back(self, late_reply: str) -> bool:
        b = self.bridge
        # If a session is running (here or in another process), it handles the
        # reply itself: it asks again and restores its own saved clipboard.
        if not b._lock.acquire(blocking=False):
            return False
        try:
            got = win32event.WaitForSingleObject(b._mutex, 0)
            if got not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                return False
            try:
                if clip_get() != late_reply:
                    return False
                clip_restore(self.snap)
                log.info("put the clipboard back over a late bridge reply")
                return True
            finally:
                win32event.ReleaseMutex(b._mutex)
        finally:
            b._lock.release()


# ---------------------------------------------------------------- bridge

class Bridge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
        self.cas: bool | None = None
        self.protocol: int | None = None  # bridge.lua protocol from the last ping
        self._saved: ClipSnapshot | None = None  # the user's clipboard, during a session
        self._seen_seq: int | None = None        # sequence number the session last accounted for
        self._pending: dict[str, float] = {}     # timed-out request ids -> until when a reply may come
        self._fallback: ClipSnapshot | None = None  # user's clipboard while late replies may come
        self._watcher: _LateReplyWatcher | None = None
        self._batch_checked = False
        self.last_timeout: str | None = None  # timeout message if the last eval_batch hit one

    # -- session

    def session(self, work, wait_s: float = 60):
        """Hold the locks, save the user's clipboard, run work, put the clipboard back.

        The clipboard is put back only if it still holds bridge traffic (or
        something else written during the session), so a copy the user made
        after the last request survives. A copy made during a request is kept
        and put back instead of the older one. Must run start to finish on one
        thread (the mutex is thread-owned). Waits at most wait_s in all for
        another request (in this process or another) to finish.
        """
        deadline = time.monotonic() + wait_s
        if not self._lock.acquire(timeout=max(wait_s, 0)):
            raise BridgeTimeout(f"Another request kept the TI-Nspire bridge busy for {wait_s:g} s.")
        try:
            left_ms = max(0, int((deadline - time.monotonic()) * 1000))
            got = win32event.WaitForSingleObject(self._mutex, left_ms)
            if got not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                raise BridgeTimeout(f"Another program kept the TI-Nspire bridge busy for {wait_s:g} s.")
            try:
                self._begin()
                _tls.in_session = True
                try:
                    return work()
                finally:
                    _tls.in_session = False
                    try:
                        self._end()
                    except Exception as e:  # never mask the real result
                        log.warning("clipboard restore failed: %s", e)
            finally:
                win32event.ReleaseMutex(self._mutex)
        finally:
            self._lock.release()

    def _begin(self) -> None:
        w, self._watcher = self._watcher, None
        if w is not None:
            w.halt.set()
            w.join(2)
            self._pending, self._fallback = dict(w.pending), w.snap
        now = time.monotonic()
        self._pending = {r: t for r, t in self._pending.items() if t > now}
        if not self._pending:
            self._fallback = None
        self._batch_checked = False
        snap = clip_snapshot()
        text = snap.text()
        self._pending.pop(_reply_id(text), None)
        if is_bridge_traffic(text):
            # A late reply to a request that timed out, or traffic left by a
            # process that died mid-request: not the user's. Use what this
            # process last put back, if it still has it; otherwise the
            # clipboard is emptied at the end rather than left like this.
            log.info("clipboard held bridge traffic at session start")
            snap = self._fallback
        self._saved = snap
        self._seen_seq = _seq()

    def _end(self) -> None:
        saved, self._saved = self._saved, None
        text = clip_get()
        ours = _seq() == _ours_seq or is_bridge_traffic(text)
        if ours:
            self._pending.pop(_reply_id(text), None)
            clip_restore(saved)
        if self._pending:
            # A timed-out request may still be answered by an old bridge.lua.
            snap = saved if ours else clip_snapshot()
            self._fallback = snap
            self._watcher = _LateReplyWatcher(self, self._pending, snap)
            self._pending = {}
            self._watcher.start()
        else:
            self._fallback = None

    def _keep_user_copy(self) -> None:
        """If the user copied something since the session last touched the
        clipboard, make that the clipboard to put back at the end."""
        cur = _seq()
        if cur in (self._seen_seq, _ours_seq):
            return
        snap = clip_snapshot()
        text = snap.text()
        if is_bridge_traffic(text):
            self._pending.pop(_reply_id(text), None)
        else:
            self._saved = snap
        self._seen_seq = cur

    def _post(self, text: str) -> int:
        self._seen_seq = _write_text(text, transient=True)
        return self._seen_seq

    # -- requests

    def _roundtrip(self, payload: str, timeout_s: float) -> tuple[bool, str]:
        """Send one request and wait for its reply. Call inside session()."""
        global _ours_seq
        rid = uuid.uuid4().hex[:12]
        request = f"{PREFIX_IN}{rid}:{payload}"
        want = f"{PREFIX_OUT}{rid}:"
        self._keep_user_copy()
        seq = self._post(request)
        reposts = 0
        # Watch the clipboard's change counter, which needs no OpenClipboard,
        # and open it only after something changed. Opening it constantly can
        # collide with TI-Nspire writing its reply; a failed write there can
        # leave TI-Nspire's clipboard out of sync with Windows until the
        # document is reopened.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(POLL_S)
            now = _seq()
            if now == seq:
                continue
            seq = now
            time.sleep(0.02)  # let the writer finish and close the clipboard
            clip = clip_get()
            if clip is not None and clip.startswith(want):
                _ours_seq = self._seen_seq = seq
                status, _, body = clip[len(want):].partition(":")
                return status == "OK", body
            if clip is not None and clip.startswith(PREFIX_IN):
                continue  # still a request
            if clip is not None and clip.startswith(PREFIX_OUT):
                # A late reply to an earlier request that timed out replaced
                # ours (an old bridge.lua writes whatever it finishes). The
                # script is free again: ask again.
                self._pending.pop(_reply_id(clip), None)
            else:
                # The user copied something meanwhile. Keep it to put back at
                # the end, then ask again.
                snap = clip_snapshot()
                if is_bridge_traffic(snap.text()):
                    seq = -1  # changed again while reading: look again
                    continue
                self._saved = snap
            if reposts < MAX_REPOSTS:
                reposts += 1
                seq = self._post(request)
        self._pending[rid] = time.monotonic() + LATE_WINDOW_S
        raise BridgeTimeout(f"No reply from the TI-Nspire bridge within {timeout_s:g} s. {TIMEOUT_HINT}")

    def ping(self, timeout_s: float = 3.0) -> dict:
        """Returns {alive, api_level, cas, protocol}. protocol 2 = bridge.lua with batch support."""
        ok, body = self._roundtrip("__PING__", _clamp_timeout(timeout_s))
        parts = body.split(":")
        if not ok or len(parts) < 3 or parts[0] != "PONG":
            raise RuntimeError(f"Unexpected ping reply: {body!r}")
        self.cas = parts[2] == "cas"
        self.protocol = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1
        return {"alive": True, "api_level": parts[1], "cas": self.cas, "protocol": self.protocol}

    def _error_message(self, body: str, timeout_s: float) -> str:
        msg = f"TI-Nspire error: {body}"
        if self.cas is None:
            try:
                self.ping(timeout_s)
            except Exception:
                pass
        if self.cas is False:
            msg += (" (this is the non-CAS edition: symbolic input like solve/factor/∫ with free"
                    " variables isn't supported; use numeric expressions)")
        return msg

    def eval_one(self, expr: str, timeout_s: float, raw: bool = False) -> str:
        """Evaluate expr. Returns readable() text unless raw=True (CAS code points)."""
        _check_expr(expr)
        timeout_s = _clamp_timeout(timeout_s)
        ok, body = self._roundtrip(to_cas_chars(expr), timeout_s)
        if ok:
            return body if raw else readable(body)
        raise ValueError(self._error_message(body, timeout_s))

    def eval_batch(self, exprs: list[str], timeout_s: float = 5.0, raw: bool = False) -> list[dict]:
        """Evaluate exprs in order. Returns one {expr, ok, result | error} per input.

        With bridge.lua protocol 2 each chunk of expressions is one clipboard
        round trip; with an older nspire_bridge.tns they go one at a time.
        timeout_s is per expression. Definitions made by earlier expressions
        are visible to later ones. If the bridge stops answering, the rest are
        skipped and last_timeout holds the timeout message (None otherwise).
        Call inside session().
        """
        self.last_timeout = None
        per = _clamp_timeout(timeout_s)
        exprs = list(exprs)
        out: list[dict | None] = [None] * len(exprs)
        todo: list[int] = []
        for i, e in enumerate(exprs):
            try:
                _check_expr(e)
                todo.append(i)
            except ValueError as x:
                out[i] = {"expr": e, "ok": False, "error": str(x)}

        dead: str | None = None
        if len(todo) > 1:
            try:
                if self._batch_supported(per):
                    dead = self._eval_chunks(exprs, todo, out, per, raw)
            except BridgeTimeout as t:
                dead = str(t)
                out[todo[0]] = {"expr": exprs[todo[0]], "ok": False, "error": dead}

        for i in todo:  # one at a time: old bridge, or whatever the batch left
            if out[i] is not None:
                continue
            e = exprs[i]
            if dead:
                out[i] = {"expr": e, "ok": False, "error": "skipped: " + dead}
                continue
            try:
                out[i] = {"expr": e, "ok": True, "result": self.eval_one(e, per, raw)}
            except BridgeTimeout as t:
                dead = str(t)
                out[i] = {"expr": e, "ok": False, "error": dead}
            except Exception as x:
                out[i] = {"expr": e, "ok": False, "error": str(x)}
        self.last_timeout = dead
        return out  # type: ignore[return-value]

    def _batch_supported(self, timeout_s: float) -> bool:
        if not self._batch_checked:  # once per session: the document could change between sessions
            try:
                self.ping(min(timeout_s, 5.0))
            except RuntimeError:
                self.protocol = 1
            self._batch_checked = True
        return (self.protocol or 1) >= 2

    def _eval_chunks(self, exprs: list[str], todo: list[int], out: list, per: float, raw: bool) -> str | None:
        """Batch requests for todo, filling out. Returns the timeout message if the bridge stopped answering."""
        for chunk in _chunks(todo, exprs):
            payload = encode_batch([to_cas_chars(exprs[i]) for i in chunk])
            try:
                ok, body = self._roundtrip(payload, min(per * len(chunk), BATCH_WAIT_MAX_S))
            except BridgeTimeout as t:
                for i in chunk:
                    out[i] = {"expr": exprs[i], "ok": False, "error": str(t)}
                return str(t)
            results = parse_batch_reply(body) if ok else None
            if results is None or len(results) != len(chunk):
                # Not a batch reply: the document was swapped for an older one
                # (which rejects the request as a syntax error, running nothing).
                log.warning("not a batch reply (%r); evaluating one at a time", body[:80])
                self.protocol = 1
                return None
            for i, (item_ok, text) in zip(chunk, results):
                if item_ok:
                    out[i] = {"expr": exprs[i], "ok": True, "result": text if raw else readable(text)}
                else:
                    out[i] = {"expr": exprs[i], "ok": False, "error": self._error_message(text, per)}
        return None

    # -- document settings

    def modes(self, timeout_s: float = 5.0) -> dict | None:
        """The document's mode settings from getMode(0), e.g. {"angle": "Radian",
        "auto_or_approx": "Auto", ...}; None if they can't be read. Never raises.
        Call inside session()."""
        try:
            return parse_modes(self.eval_one("getMode(0)", timeout_s))
        except Exception as e:
            log.info("couldn't read the mode settings: %s", e)
            return None

    # -- variables

    def list_vars(self, values: bool = True, timeout_s: float = 5.0) -> list[dict]:
        """Variables stored in the open problem, from getVarInfo(). Call inside session().

        Returns [{name, type, library, locked, value?}]; value only for NUM, EXPR,
        LIST, MAT and STR variables, cut to VALUE_MAX_CHARS.
        """
        rows = parse_var_info(self.eval_one("getVarInfo()", timeout_s))
        out = []
        for r in rows:
            if not r:
                continue
            lib = r[2] if len(r) > 2 else ""
            out.append({
                "name": r[0],
                "type": r[1] if len(r) > 1 else "",
                "library": None if lib.lower() in ("", "none") else lib,
                "locked": {"0": False, "1": True}.get(r[3]) if len(r) > 3 else None,
            })
        if values:
            idx = [k for k, v in enumerate(out) if v["type"].upper() in VALUE_TYPES]
            for k, res in zip(idx, self.eval_batch([out[k]["name"] for k in idx], timeout_s)):
                if res["ok"]:
                    v = res["result"]
                    out[k]["value"] = v if len(v) <= VALUE_MAX_CHARS else v[:VALUE_MAX_CHARS] + "…"
        return out

    def clear_vars(self, names: list[str] | None = None, timeout_s: float = 5.0) -> dict:
        """DelVar the given names, or every variable getVarInfo() lists. Call inside session().

        Returns {deleted: [names], errors: [{name, error}]}.
        """
        if names:
            clean = [n.strip() if isinstance(n, str) else n for n in names]
            bad = [n for n in clean if not isinstance(n, str) or not _NAME_RE.match(n)]
            if bad:
                raise ValueError(f"Not variable names: {bad!r}. Give bare names, e.g. 'f', not 'f(x)'.")
            names = list(dict.fromkeys(clean))
        else:
            names = [v["name"] for v in self.list_vars(values=False, timeout_s=timeout_s)]
        if not names:
            return {"deleted": [], "errors": []}
        results = self.eval_batch([f"DelVar {n}" for n in names], timeout_s)
        return {
            "deleted": [n for n, r in zip(names, results) if r["ok"]],
            "errors": [{"name": n, "error": r["error"]} for n, r in zip(names, results) if not r["ok"]],
        }
