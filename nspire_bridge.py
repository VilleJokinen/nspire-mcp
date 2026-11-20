"""Clipboard transport to bridge.lua running in the TI-Nspire desktop app.

Shared by server.py (MCP) and hotkey.py. A named Windows mutex serialises
requests across processes, so both can run at once.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

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


def clip_set(text: str) -> None:
    # Pass NUL-terminated UTF-16 bytes, not a str: pywin32's own str conversion
    # in SetClipboardData fails on some short strings such as "11" (error 6,
    # invalid handle) and corrupts the heap, so the next write crashes Python.
    data = (text + "\0").encode("utf-16-le")

    def write():
        wcb.EmptyClipboard()
        wcb.SetClipboardData(wcb.CF_UNICODETEXT, data)
    _with_clipboard(write)


def is_bridge_traffic(text: str | None) -> bool:
    return text is not None and text.startswith((PREFIX_IN, PREFIX_OUT))


# ---------------------------------------------------------------- bridge

class Bridge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
        self.cas: bool | None = None

    def _roundtrip(self, payload: str, timeout_s: float) -> tuple[bool, str]:
        """Send one request and wait for its reply. Caller holds the lock."""
        rid = uuid.uuid4().hex[:12]
        clip_set(f"{PREFIX_IN}{rid}:{payload}")
        want = f"{PREFIX_OUT}{rid}:"
        # Watch the clipboard's change counter, which needs no OpenClipboard,
        # and open it only after something changed. Opening it constantly can
        # collide with TI-Nspire writing its reply; a failed write there can
        # leave TI-Nspire's clipboard out of sync with Windows until the
        # document is reopened.
        seq = wcb.GetClipboardSequenceNumber()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(POLL_S)
            now = wcb.GetClipboardSequenceNumber()
            if now == seq:
                continue
            seq = now
            time.sleep(0.02)  # let the writer finish and close the clipboard
            clip = clip_get()
            if clip and clip.startswith(want):
                status, _, body = clip[len(want):].partition(":")
                return status == "OK", body
        raise BridgeTimeout(f"No reply from the TI-Nspire bridge within {timeout_s:g} s. {TIMEOUT_HINT}")

    def session(self, work, wait_s: float = 60):
        """Hold the locks, save the user's clipboard text, run work, restore it.

        Must run start to finish on one thread (the mutex is thread-owned).
        """
        with self._lock:
            got = win32event.WaitForSingleObject(self._mutex, int(wait_s * 1000))
            if got not in (win32event.WAIT_OBJECT_0, win32event.WAIT_ABANDONED):
                raise BridgeTimeout(f"Another program kept the TI-Nspire bridge busy for {wait_s:g} s.")
            try:
                saved = clip_get()
                try:
                    return work()
                finally:
                    try:
                        # Restore only if the clipboard still holds our traffic,
                        # so a copy the user made meanwhile isn't overwritten.
                        current = clip_get()
                        if saved is not None and (current is None or is_bridge_traffic(current)):
                            clip_set(saved)
                    except Exception as e:  # never mask the real result
                        log.warning("clipboard restore failed: %s", e)
            finally:
                win32event.ReleaseMutex(self._mutex)

    def ping(self, timeout_s: float = 3.0) -> dict:
        ok, body = self._roundtrip("__PING__", timeout_s)
        parts = body.split(":")
        if not ok or len(parts) < 3 or parts[0] != "PONG":
            raise RuntimeError(f"Unexpected ping reply: {body!r}")
        self.cas = parts[2] == "cas"
        return {"alive": True, "api_level": parts[1], "cas": self.cas}

    def eval_one(self, expr: str, timeout_s: float, raw: bool = False) -> str:
        """Evaluate expr. Returns readable() text unless raw=True (CAS code points)."""
        if not expr.strip():
            raise ValueError("Empty expression.")
        if "\n" in expr or "\r" in expr:
            raise ValueError("Expression must be a single line.")
        ok, body = self._roundtrip(to_cas_chars(expr), timeout_s)
        if ok:
            return body if raw else readable(body)
        msg = f"TI-Nspire error: {body}"
        if self.cas is None:
            try:
                self.ping(timeout_s)
            except Exception:
                pass
        if self.cas is False:
            msg += (" (this is the non-CAS edition: symbolic input like solve/factor/∫ with free"
                    " variables isn't supported; use numeric expressions)")
        raise ValueError(msg)
