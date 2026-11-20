# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["pywin32>=306", "sympy>=1.13", "antlr4-python3-runtime==4.11.*"]
# ///
"""Global hotkey: copy LaTeX, press the hotkey, get the TI-Nspire answer.

    uv run --script hotkey.py                      # with a console window
    uv run --gui-script hotkey.py                  # no console (quit with the quit hotkey)

Ctrl+Alt+Shift+L   solve/evaluate the LaTeX on the clipboard (action "auto")
Ctrl+Alt+Shift+K   same, but the decimal value becomes the answer
Ctrl+Alt+Shift+Q   quit

The answer replaces the clipboard (as LaTeX, see --copy) and shows in a popup
in the bottom-right corner; click the popup to close it.
Needs TI-Nspire open on the bridge page, like the MCP server.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import os
import queue
import sys
import threading
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Started without a console (--gui-script / uvw), stdout and stderr are None.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import win32event  # noqa: E402
import winerror  # noqa: E402
import win32api  # noqa: E402

from nspire_bridge import Bridge, clip_get, clip_set, is_bridge_traffic  # noqa: E402
from nspire_latex import LatexError, format_answer, plan, run_plan  # noqa: E402

user32 = ctypes.windll.user32
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012
MODS = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000


def parse_hotkey(spec: str) -> tuple[int, int]:
    *mods, key = [p.strip().lower() for p in spec.split("+")]
    m = MOD_NOREPEAT
    for x in mods:
        if x not in MODS:
            raise SystemExit(f"Unknown modifier {x!r} in {spec!r}")
        m |= MODS[x]
    if len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x6F + int(key[1:])
    else:
        raise SystemExit(f"Unsupported key {key!r} in {spec!r} (use a letter, digit or F1-F24)")
    return m, vk


# ---------------------------------------------------------------- hotkey thread

class HotkeyThread(threading.Thread):
    def __init__(self, bindings: dict[int, tuple[int, int, str]], events: queue.Queue):
        super().__init__(daemon=True)
        self.bindings, self.events = bindings, events
        self.thread_id = None
        self.failed: list[str] = []
        self.ready = threading.Event()

    def run(self):
        self.thread_id = win32api.GetCurrentThreadId()
        for hid, (mods, vk, label) in self.bindings.items():
            if not user32.RegisterHotKey(None, hid, mods, vk):
                self.failed.append(label)
        self.ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                self.events.put(("hotkey", msg.wParam))
        for hid in self.bindings:
            user32.UnregisterHotKey(None, hid)

    def stop(self):
        if self.thread_id:
            user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


# ---------------------------------------------------------------- popup

BG, FG, DIM, ERR, ACC = "#1f2328", "#f0f3f6", "#9198a1", "#ff7b72", "#7ee787"


class Popup:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.win: tk.Toplevel | None = None
        self.after_id = None

    def show(self, title: str, body: str, detail: str = "", footer: str = "", error: bool = False,
             seconds: float = 12):
        self.close()
        w = tk.Toplevel(self.root)
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        w.configure(bg=BG, padx=14, pady=10)
        wrap = 520
        tk.Label(w, text=title, bg=BG, fg=ERR if error else DIM, font=("Segoe UI", 9),
                 anchor="w", justify="left", wraplength=wrap).pack(fill="x")
        tk.Label(w, text=body, bg=BG, fg=FG, font=("Cambria Math", 15), anchor="w",
                 justify="left", wraplength=wrap).pack(fill="x", pady=(2, 2))
        if detail:
            tk.Label(w, text=detail, bg=BG, fg=FG, font=("Cambria Math", 11), anchor="w",
                     justify="left", wraplength=wrap).pack(fill="x")
        if footer:
            tk.Label(w, text=footer, bg=BG, fg=ACC if not error else DIM, font=("Segoe UI", 8),
                     anchor="w", justify="left", wraplength=wrap).pack(fill="x", pady=(4, 0))
        for widget in [w, *w.winfo_children()]:
            widget.bind("<Button-1>", lambda e: self.close())
        w.update_idletasks()
        x = w.winfo_screenwidth() - w.winfo_reqwidth() - 24
        y = w.winfo_screenheight() - w.winfo_reqheight() - 72
        w.geometry(f"+{x}+{y}")
        self.win = w
        if seconds:
            self.after_id = self.root.after(int(seconds * 1000), self.close)

    def close(self):
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if self.win is not None:
            self.win.destroy()
            self.win = None


def short(s: str, n: int = 160) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n] + "…"


# ---------------------------------------------------------------- app

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hotkey", default="ctrl+alt+shift+l", help="solve hotkey (default %(default)s)")
    ap.add_argument("--approx-hotkey", default="ctrl+alt+shift+k", help="numeric hotkey (default %(default)s)")
    ap.add_argument("--quit-hotkey", default="ctrl+alt+shift+q", help="quit hotkey (default %(default)s)")
    ap.add_argument("--copy", choices=["latex", "text", "none"], default="latex",
                    help="what to put on the clipboard after solving (default %(default)s)")
    ap.add_argument("--timeout", type=float, default=15, help="CAS timeout in seconds")
    args = ap.parse_args()

    single = win32event.CreateMutex(None, False, "Local\\nspire_hotkey_single_instance")
    if win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS:
        print("hotkey.py is already running.", file=sys.stderr)
        return
    _ = single  # keep the handle alive

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    SOLVE, APPROX, QUIT = 1, 2, 3
    bindings = {SOLVE: (*parse_hotkey(args.hotkey), args.hotkey),
                APPROX: (*parse_hotkey(args.approx_hotkey), args.approx_hotkey),
                QUIT: (*parse_hotkey(args.quit_hotkey), args.quit_hotkey)}
    events: queue.Queue = queue.Queue()
    hk = HotkeyThread(bindings, events)
    hk.start()
    hk.ready.wait(5)

    root = tk.Tk()
    root.withdraw()
    popup = Popup(root)
    bridge = Bridge()
    busy = threading.Event()

    if hk.failed:
        print("Couldn't register: " + ", ".join(hk.failed), file=sys.stderr)
        popup.show("Hotkey unavailable", "Couldn't register " + ", ".join(hk.failed),
                   footer="Another program uses it; pick another with --hotkey.", error=True, seconds=10)
    else:
        popup.show("TI-Nspire LaTeX hotkey ready", f"{args.hotkey.upper()}",
                   detail=f"numeric: {args.approx_hotkey.upper()}    quit: {args.quit_hotkey.upper()}",
                   footer="Copy LaTeX, then press the hotkey.", seconds=5)
    print(f"Ready. {args.hotkey} solves, {args.approx_hotkey} approximates, {args.quit_hotkey} quits.",
          file=sys.stderr)

    def solve(numeric: bool):
        latex = ""
        try:
            latex = (clip_get() or "").strip()
            if not latex or is_bridge_traffic(latex):
                events.put(("error", "Clipboard has no text", "Copy some LaTeX first.", ""))
                return
            p = plan(latex, "auto")  # bad LaTeX fails here, before the clipboard is used
            run = bridge.session(lambda: run_plan(bridge, p, args.timeout))
            r = format_answer(run)   # after the session: the user's clipboard is back meanwhile
            if numeric and r.get("approx"):
                # numeric hotkey: the decimal answer becomes the main one
                r = {**r, "exact": r["result"], "result": r["approx"], "result_latex": r.get("approx_latex"),
                     "approx": None}
            copied = ""
            if args.copy == "latex" and r.get("result_latex"):
                clip_set(r["result_latex"])
                copied = "LaTeX copied to clipboard"
            elif args.copy != "none" and r.get("result"):
                clip_set(r["result"])
                copied = "Result text copied to clipboard"
            events.put(("result", latex, r, copied))
        except LatexError as e:
            events.put(("error", "Couldn't read the LaTeX", str(e), latex))
        except Exception as e:
            events.put(("error", "TI-Nspire error", str(e), latex))
        finally:
            busy.clear()

    def show_error(title: str, msg: str, latex: str = ""):
        try:
            popup.show(title, short(msg, 300), footer=short(latex, 120), error=True, seconds=10)
        except Exception as e:  # the popup itself failed; don't take the event loop down
            print(f"{title}: {msg} (popup failed: {e})", file=sys.stderr)

    def start_solve(numeric: bool):
        busy.set()
        try:
            try:
                preview = short(clip_get() or "", 120)
            except Exception:  # clipboard locked by another program; solve() retries
                preview = ""
            popup.show("Solving…", preview, seconds=0)
            threading.Thread(target=solve, args=(numeric,), daemon=True).start()
        except BaseException:
            busy.clear()  # the worker never started, so it can't clear busy itself
            raise

    def handle(ev) -> bool:
        """Handle one event; False once the app should quit."""
        if ev[0] == "hotkey":
            if ev[1] == QUIT:
                popup.close()
                hk.stop()
                root.quit()
                return False
            if not busy.is_set():
                start_solve(ev[1] == APPROX)
        elif ev[0] == "result":
            _, latex, r, copied = ev
            detail = (f"≈ {r['approx']}" if r.get("approx")
                      else f"exact: {r['exact']}" if r.get("exact") else "")
            popup.show(f"{r['action']}:  {short(r['nspire'], 140)}", r["result"] or "(empty)",
                       detail=detail, footer=copied, seconds=15)
        elif ev[0] == "error":
            _, title, msg, latex = ev
            show_error(title, msg, latex)
        return True

    def pump():
        running = True
        try:
            while running:
                try:
                    ev = events.get_nowait()
                except queue.Empty:
                    break
                try:
                    running = handle(ev)
                except Exception as e:
                    show_error("Hotkey error", f"{type(e).__name__}: {e}")
        finally:
            # always reschedule, or one failed event would silently stop the hotkey
            if running:
                root.after(50, pump)

    root.after(50, pump)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    hk.stop()


if __name__ == "__main__":
    main()
