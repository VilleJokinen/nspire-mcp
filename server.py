# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["mcp>=1.2,<2", "pywin32>=306"]
# ///
"""MCP server: evaluate expressions in the TI-Nspire CX (CAS) desktop app.

Talks to bridge.lua (running on a page of nspire_bridge.tns) through the
Windows clipboard. Run with:  uv run --script server.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import uuid

import pywintypes
import win32clipboard as wcb
from mcp.server.fastmcp import FastMCP

logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="nspire: %(message)s")
log = logging.getLogger("nspire")

PREFIX_IN = "NSPIRE_IN:"
PREFIX_OUT = "NSPIRE_OUT:"
POLL_S = 0.03
TIMEOUT_HINT = "Is TI-Nspire open with nspire_bridge.tns on the bridge page?"


class BridgeTimeout(Exception):
    pass


# ---------------------------------------------------------------- clipboard

def _with_clipboard(fn, retries: int = 40):
    """Open the clipboard, run fn, close it. Retries while another app holds it."""
    for attempt in range(retries):
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
    def write():
        wcb.EmptyClipboard()
        wcb.SetClipboardData(wcb.CF_UNICODETEXT, text)
    _with_clipboard(write)


# ---------------------------------------------------------------- bridge

class Bridge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.cas: bool | None = None

    def _roundtrip(self, payload: str, timeout_s: float) -> tuple[bool, str]:
        """Send one request and wait for its reply. Caller holds the lock."""
        rid = uuid.uuid4().hex[:12]
        clip_set(f"{PREFIX_IN}{rid}:{payload}")
        want = f"{PREFIX_OUT}{rid}:"
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(POLL_S)
            clip = clip_get()
            if clip and clip.startswith(want):
                status, _, body = clip[len(want):].partition(":")
                return status == "OK", body
        raise BridgeTimeout(f"No reply from the TI-Nspire bridge within {timeout_s:g} s. {TIMEOUT_HINT}")

    def session(self, work):
        """Hold the lock, save the user's clipboard text, run work, restore it."""
        with self._lock:
            saved = clip_get()
            try:
                return work()
            finally:
                try:
                    current = clip_get()
                    # Restore only if the clipboard still holds our traffic,
                    # so a copy the user made meanwhile isn't overwritten.
                    ours = current is None or current.startswith((PREFIX_IN, PREFIX_OUT))
                    if saved is not None and ours:
                        clip_set(saved)
                except Exception as e:  # never mask the real result
                    log.warning("clipboard restore failed: %s", e)

    def ping(self, timeout_s: float = 3.0) -> dict:
        ok, body = self._roundtrip("__PING__", timeout_s)
        parts = body.split(":")
        if not ok or len(parts) != 3 or parts[0] != "PONG":
            raise RuntimeError(f"Unexpected ping reply: {body!r}")
        self.cas = parts[2] == "cas"
        return {"alive": True, "api_level": parts[1], "cas": self.cas}

    def eval_one(self, expr: str, timeout_s: float) -> str:
        if not expr.strip():
            raise ValueError("Empty expression.")
        if "\n" in expr or "\r" in expr:
            raise ValueError("Expression must be a single line.")
        ok, body = self._roundtrip(expr, timeout_s)
        if ok:
            return body
        msg = f"TI-Nspire error: {body}"
        if self.cas is None:
            try:
                self.ping(timeout_s)
            except Exception:
                pass
        if self.cas is False:
            msg += " (this is the non-CAS edition: symbolic input like solve/factor/∫ with free variables isn't supported; use numeric expressions)"
        raise ValueError(msg)


bridge = Bridge()
mcp = FastMCP("nspire")


@mcp.tool()
async def nspire_status() -> dict:
    """Ping the TI-Nspire bridge. Returns alive, the Lua API level, and whether the CAS is available."""
    def work():
        try:
            return bridge.ping()
        except BridgeTimeout as e:
            return {"alive": False, "error": str(e)}
    return await asyncio.to_thread(bridge.session, work)


@mcp.tool()
async def nspire_eval(expr: str, timeout_s: float = 5) -> str:
    """Evaluate one expression in TI-Nspire (CAS if available) and return the result as text.

    Use TI-Nspire syntax, e.g. solve(x^2=4,x), factor(x^2-5x+6), ∫(x^2,x), approx(π).
    Results come back as the CAS writes them; negatives use the Unicode minus "−".
    Definitions (Define f(x)=..., a:=5) persist in the open document's problem,
    so later calls can use them. Undefine with DelVar a.
    """
    def work():
        return bridge.eval_one(expr, timeout_s)
    log.info("eval %r", expr)
    return await asyncio.to_thread(bridge.session, work)


@mcp.tool()
async def nspire_eval_batch(exprs: list[str], timeout_s: float = 5) -> list[dict]:
    """Evaluate expressions in order. Returns one {expr, ok, result | error} per input.

    Definitions made by earlier expressions are visible to later ones and persist
    in the open document's problem. If the bridge stops answering, the remaining
    expressions are skipped.
    """
    def work():
        out: list[dict] = []
        dead: str | None = None
        for e in exprs:
            if dead:
                out.append({"expr": e, "ok": False, "error": "skipped: " + dead})
                continue
            try:
                out.append({"expr": e, "ok": True, "result": bridge.eval_one(e, timeout_s)})
            except BridgeTimeout as t:
                dead = str(t)
                out.append({"expr": e, "ok": False, "error": dead})
            except Exception as x:
                out.append({"expr": e, "ok": False, "error": str(x)})
        return out
    log.info("batch of %d", len(exprs))
    return await asyncio.to_thread(bridge.session, work)


if __name__ == "__main__":
    mcp.run()  # stdio
