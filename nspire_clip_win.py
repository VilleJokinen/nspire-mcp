"""Windows clipboard backend for nspire_bridge (pywin32).

The platform primitives nspire_bridge needs, with the same names in
nspire_clip_mac: clip_get, write_text, seq, clip_snapshot, clip_restore,
ClipSnapshot and ProcessLock. Traffic is kept out of Win+V history and cloud
clipboard sync; the cross-process lock is a named mutex.
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass, field

import pywintypes
import win32clipboard as wcb
import win32event

log = logging.getLogger("nspire")

SNAPSHOT_MAX_BYTES = 128 * 2**20


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


def seq() -> int:
    """The clipboard's change counter (needs no OpenClipboard)."""
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


def write_text(text: str, transient: bool) -> int:
    """Replace the clipboard with text. Returns the clipboard sequence number after the write.

    transient: bridge traffic, kept out of clipboard history and cloud sync.
    """
    data = (text + "\0").encode("utf-16-le", errors="surrogatepass")

    def write():
        wcb.EmptyClipboard()
        _set_data(wcb.CF_UNICODETEXT, data)
        if transient:
            _keep_out_of_history()
    _with_clipboard(write)
    return seq()


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
        snap.items.append((f, bytes(data)))  # pyright: ignore[reportArgumentType]
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


# ---------------------------------------------------------------- cross-process lock

class ProcessLock:
    """A named mutex: one bridge request at a time across processes (MCP server, hotkey).

    Owned by the thread that acquired it, which must release it. If its owner
    dies, Windows hands it to the next waiter (WAIT_ABANDONED).
    """

    def __init__(self, name: str) -> None:
        self._mutex = win32event.CreateMutex(None, False, name)  # pyright: ignore[reportArgumentType]

    def acquire(self, timeout_s: float) -> bool:
        """Wait at most timeout_s; True if this thread now holds the lock."""
        got = win32event.WaitForSingleObject(self._mutex, max(0, int(timeout_s * 1000)))  # pyright: ignore[reportArgumentType]
        return got in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED)

    def release(self) -> None:
        win32event.ReleaseMutex(self._mutex)  # pyright: ignore[reportArgumentType]
