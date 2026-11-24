"""hotkey.py without TI-Nspire: hotkey parsing, history, tray menu, action picker,
math rendering, and the app's event handling against a fake bridge and a fake
clipboard (the real clipboard is never touched)."""
import queue
import struct
import sys
import types

import pytest

pytest.importorskip("tkinter")
if sys.platform == "win32":
    pytest.importorskip("win32api")

import tkinter as tk  # noqa: E402

import hotkey as hk  # noqa: E402
from nspire_latex import ACTIONS, LatexError, plan  # noqa: E402

SPECS = {hk.SOLVE: "ctrl+alt+shift+l", hk.APPROX: "ctrl+alt+shift+k", hk.PICK: "ctrl+alt+shift+j",
         hk.QUIT: "ctrl+alt+shift+q", hk.TO_LATEX: "ctrl+alt+shift+t"}


# ---------------------------------------------------------------- hotkeys

def test_parse_hotkey():
    assert hk.parse_hotkey("ctrl+alt+shift+j") == (hk.MOD_NOREPEAT | 0x1 | 0x2 | 0x4, ord("J"))
    assert hk.parse_hotkey(" Control + F9 ") == (hk.MOD_NOREPEAT | 0x2, 0x78)
    assert hk.parse_hotkey("win+5") == (hk.MOD_NOREPEAT | 0x8, ord("5"))
    assert hk.parse_hotkey("F24") == (hk.MOD_NOREPEAT, 0x87)


@pytest.mark.parametrize("spec", ["hyper+x", "ctrl+space", "ctrl+f25", "ctrl+f0", "ctrl+"])
def test_parse_hotkey_rejects(spec):
    with pytest.raises(SystemExit):
        hk.parse_hotkey(spec)


def test_default_hotkeys_differ():
    keys = [hk.parse_hotkey(s) for s in SPECS.values()]
    assert len(set(keys)) == len(keys)


def test_pretty_hotkey():
    assert hk.pretty_hotkey("ctrl+alt+shift+l") == "Ctrl+Alt+Shift+L"
    assert hk.pretty_hotkey("ctrl + f9") == "Ctrl+F9"


# ---------------------------------------------------------------- history and tray menu

def answer(n: int = 1, latex: str | None = r"x = 1") -> hk.Answer:
    return hk.Answer(f"x={n}", "solve for x", f"x={n}", latex)


def test_history_newest_first_and_capped():
    h = hk.History(size=3)
    for n in range(5):
        h.add(answer(n))
    assert [a.latex for a in h.items()] == ["x=4", "x=3", "x=2"]
    assert len(h) == 3


def test_history_repeat_moves_to_top():
    h = hk.History()
    h.add(answer(1))
    h.add(answer(2))
    h.add(answer(1))
    assert [a.latex for a in h.items()] == ["x=1", "x=2"]
    h.clear()
    assert h.items() == []


def test_history_default_size():
    h = hk.History()
    for n in range(25):
        h.add(answer(n))
    assert len(h) == hk.HISTORY_SIZE == 10


def test_copy_text_follows_copy_option():
    a = hk.Answer(r"x^2=4", "solve for x", "x=−2 or x=2", r"x = -2 \quad\text{or}\quad x = 2")
    assert a.copy_text("latex") == a.result_latex
    assert a.copy_text("text") == "x=−2 or x=2"
    assert a.copy_text("none") == a.result_latex  # a click in the menu is an explicit copy
    assert answer(latex=None).copy_text("latex") == "x=1"  # no LaTeX: the text


def test_menu_label():
    a = hk.Answer(r"\begin{matrix}1 & 2\end{matrix}", "det", "", None)
    label = a.menu_label()
    assert label.startswith("(empty)\t")
    assert "&&" in label and "& " not in label.replace("&&", "")
    long = hk.Answer("y" * 200, "eval", "z" * 200, None).menu_label()
    assert len(long) < 90


def test_tray_menu_empty():
    items = hk.tray_menu(hk.History(), SPECS)
    first = items[0]
    assert first[1] == "No answers yet" and first[2] is False
    ids = [i[0] for i in items]
    assert hk.MENU_CLEAR not in ids
    assert ids[-1] == hk.MENU_QUIT
    labels = {i[0]: i[1] for i in items}
    assert labels[hk.MENU_SOLVE].endswith("\tCtrl+Alt+Shift+L")
    assert labels[hk.MENU_PICK].endswith("\tCtrl+Alt+Shift+J")
    assert labels[hk.MENU_TO_LATEX].endswith("\tCtrl+Alt+Shift+T")
    assert labels[hk.MENU_QUIT].endswith("\tCtrl+Alt+Shift+Q")


def test_tray_menu_lists_answers_with_their_data():
    h = hk.History()
    h.add(answer(1))
    h.add(answer(2))
    items = hk.tray_menu(h, SPECS)
    assert [(i[0], i[3].latex) for i in items[:2]] == [(hk.MENU_HISTORY, "x=2"), (hk.MENU_HISTORY + 1, "x=1")]
    assert hk.MENU_CLEAR in [i[0] for i in items]
    assert max(hk.MENU_SOLVE, hk.MENU_PICK, hk.MENU_CLEAR, hk.MENU_QUIT, hk.MENU_TO_LATEX) < hk.MENU_HISTORY
    seps = [i for i in items if i[0] is None]
    assert len(seps) == 2


def test_icon_dib():
    size = 16
    d = hk.icon_dib(size)
    assert len(d) == 40 + size * size * 4 + size * 4
    assert struct.unpack("<IiiHH", d[:16]) == (40, size, 2 * size, 1, 32)

    def pixel(x, y):  # rows are stored bottom-up, BGRA
        off = 40 + ((size - 1 - y) * size + x) * 4
        return tuple(d[off:off + 4])

    assert pixel(0, 0)[3] == 0  # rounded corner: transparent
    assert pixel(size // 2, size // 2 + 4)[3] == 255
    greens = [pixel(x, y) for x in range(size) for y in range(size) if pixel(x, y)[1] > 180]
    assert greens  # the square-root stroke


# ---------------------------------------------------------------- action picker data

def test_picker_actions_exist():
    assert len(set(hk.PICKER_ACTIONS)) == len(hk.PICKER_ACTIONS)
    assert set(hk.PICKER_ACTIONS) <= set(ACTIONS)
    for left_out in ("eval", "define", "desolve"):
        assert left_out not in hk.PICKER_ACTIONS
    for a in ("factor", "expand", "solve", "integrate", "diff", "approx", "limit"):
        assert a in hk.PICKER_ACTIONS
    for a in hk.PICKER_EXTRA:
        assert a in hk.PICKER_ACTIONS and hk.needs_var(a)


def test_filter_actions():
    assert hk.filter_actions("") == hk.PICKER_ACTIONS
    assert hk.filter_actions("fac") == ["factor", "cfactor"]
    assert hk.filter_actions(" SOL ") == ["solve", "nsolve", "csolve"]
    assert hk.filter_actions("partial") == ["propfrac"]  # matches the description
    assert hk.filter_actions("zzz") == []


def test_action_label():
    assert hk.action_label("factor") == "factor"
    assert hk.action_label("diff") == "diff  ·  differentiate"


def test_picker_defaults():
    d = hk.picker_defaults(r"x+y=3, x-y=1")
    assert set(d) == {a for a in hk.PICKER_ACTIONS if hk.needs_var(a)}
    assert d["solve"] == d["csolve"] == d["nsolve"] == "x, y"
    assert d["diff"] == d["integrate"] == d["limit"] == "x"
    assert set(hk.picker_defaults("2+3").values()) == {""}
    assert hk.picker_defaults(r"\sin(t)")["diff"] == "t"


def test_parse_extra():
    assert hk.parse_extra("factor", "anything") == {}
    assert hk.parse_extra("taylor", "") == {"order": 5}
    assert hk.parse_extra("taylor", " 7 ") == {"order": 7}
    assert hk.parse_extra("limit", "") == {"point": "0"}
    assert hk.parse_extra("limit", "∞") == {"point": "∞"}
    for bad in ("x", "0", "-1", "2.5", "100"):
        with pytest.raises(LatexError):
            hk.parse_extra("taylor", bad)


def test_extra_reaches_plan():
    p = plan(r"\frac{\sin x}{x}", "limit", "x", **hk.parse_extra("limit", ""))
    assert p.command == "limit(sin(x)/x,x,0)"
    p = plan(r"x^2", "taylor", "x", **hk.parse_extra("taylor", "3"))
    assert p.command == "taylor(x^2,x,3)"


# ---------------------------------------------------------------- math rendering

@pytest.mark.parametrize("src, want", [
    (r"\frac{1}{2}", r"$\dfrac{1}{2}$"),
    (r"e^{\frac{1}{2}} + \frac{3}{4}", r"$e^{\frac{1}{2}} + \dfrac{3}{4}$"),
    (r"x_{\frac{1}{2}}", r"$x_{\frac{1}{2}}$"),
    (r"x^\frac{1}{2}", r"$x^\frac{1}{2}$"),
    (r"x^2 \frac{1}{2}", r"$x^2 \dfrac{1}{2}$"),  # one-character exponent, then a fraction
    (r"\left\{\frac{1}{2}\right\}", r"$\left\{\dfrac{1}{2}\right\}$"),  # \{ isn't a group
    (r"e^{x \frac{1}{2}} \frac{1}{3}", r"$e^{x \frac{1}{2}} \dfrac{1}{3}$"),
    (r"\left|{x}\right|", r"$|{x}|$"),
])
def test_mathtext_source(src, want):
    assert hk.mathtext_source(src) == want


@pytest.mark.parametrize("src", [None, "", "  ", r"\left[\begin{matrix}1 & 2\end{matrix}\right]", "$x$", "a\nb"])
def test_mathtext_source_refuses(src):
    assert hk.mathtext_source(src) is None


def png_size(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", png[16:24])


def test_render_result_latex_cases():
    pytest.importorskip("matplotlib")
    from test_result_latex import CASES
    r = hk.MathRenderer(dpi=96, max_width=10_000)
    for _, latex in CASES:
        png = r.render(latex)
        if r"\begin{" in latex:  # matrices and cases (piecewise) aren't in mathtext
            assert png is None
        else:
            assert png is not None, latex
            png_size(png)


def test_render_shrinks_or_gives_up():
    pytest.importorskip("matplotlib")
    wide = " + ".join([r"\frac{x^{2}}{3}"] * 6)
    big = hk.MathRenderer(dpi=96, max_width=10_000).render(wide)
    w_big, _ = png_size(big)
    small = hk.MathRenderer(dpi=96, max_width=w_big * 3 // 4).render(wide)
    assert small is not None
    assert png_size(small)[0] <= w_big * 3 // 4
    assert hk.MathRenderer(dpi=96, max_width=60).render(wide) is None  # too wide even at the smallest size


def test_render_without_matplotlib():
    r = hk.MathRenderer()
    r.error = "ModuleNotFoundError: No module named 'matplotlib'"  # load() then doesn't try
    assert r.render(r"\frac{1}{2}") is None


def test_render_after_warm_up():
    pytest.importorskip("matplotlib")
    r = hk.MathRenderer()
    r.warm_up()
    assert r.render(r"x^{2}", wait_s=60) is not None


# ---------------------------------------------------------------- app, with fakes

class FakePopup:
    def __init__(self):
        self.shown = []
        self.closed = 0

    def show(self, title, body, detail="", footer="", error=False, seconds=12, image=None):
        self.shown.append(types.SimpleNamespace(title=title, body=body, detail=detail, footer=footer,
                                                error=error, seconds=seconds, image=image))

    def close(self):
        self.closed += 1

    @property
    def last(self):
        return self.shown[-1]


class FakeRoot:
    def __init__(self):
        self.quitted = False

    def quit(self):
        self.quitted = True

    def after(self, ms, fn):
        return None


class FakeBridge:
    def __init__(self, replies):
        self.replies = replies
        self.commands = []
        self.sessions = 0

    def session(self, work, wait_s=60):
        self.sessions += 1
        return work()

    def eval_one(self, expr, timeout_s, raw=False):
        self.commands.append(expr)
        return self.replies[expr]


class FakeClip:
    def __init__(self, text):
        self.text = text
        self.copies = []

    def set(self, text):
        self.copies.append(text)
        self.text = text

    def clipboard(self):
        return hk.Clipboard(lambda: self.text, self.set,
                            lambda t: t is not None and t.startswith(("NSPIRE_IN:", "NSPIRE_OUT:")))


SOLVE_REPLIES = {"solve(x^2 - 4=0,x)": "x=−2 or x=2"}


def make_app(clip_text="x^2-4=0", replies=None, copy="latex", root=None):
    clip = FakeClip(clip_text)
    bridge = FakeBridge(SOLVE_REPLIES if replies is None else replies)
    app = hk.App(types.SimpleNamespace(copy=copy, timeout=5), root or FakeRoot(), FakePopup(), bridge,
                 queue.Queue(), clip.clipboard())
    return app, clip, bridge


def run_job(app, job=None):
    app.busy.set()  # as start() does
    app.solve(job or hk.Job())
    assert not app.busy.is_set()
    return app.events.get_nowait()


def test_solve_copies_latex_and_records_history():
    app, clip, bridge = make_app()
    ev = run_job(app)
    assert ev[0] == "result"
    _, latex, r, copied, png = ev
    assert latex == "x^2-4=0" and png is None
    assert clip.copies == [r"x = -2 \quad\text{or}\quad x = 2"]
    assert copied == "LaTeX copied to clipboard"
    assert app.handle(ev) is True
    assert app.history.items()[0].result == "x=−2 or x=2"
    assert app.popup.last.title.startswith("solve for x:")
    assert app.popup.last.body == "x=−2 or x=2"


@pytest.mark.parametrize("copy, want", [("text", ["x=−2 or x=2"]), ("none", [])])
def test_solve_copy_option(copy, want):
    app, clip, _ = make_app(copy=copy)
    run_job(app)
    assert clip.copies == want


def test_numeric_job_makes_the_decimal_the_answer():
    replies = {"√(2)": "√(2)", "approx(√(2))": "1.41421356237"}
    app, clip, _ = make_app(clip_text=r"\sqrt{2}", replies=replies)
    _, _, r, _, _ = run_job(app, hk.Job(numeric=True))
    assert r["result"] == "1.41421356237" and r["exact"] == "√(2)"
    assert clip.copies == ["1.41421356237"]


def test_action_job_uses_its_latex_and_variable():
    replies = {"limit(sin(x)/x,x,0)": "1"}
    app, clip, bridge = make_app(clip_text="not this", replies=replies)
    job = hk.Job(action="limit", latex=r"\frac{\sin x}{x}", var="x", extra={"point": "0"})
    ev = run_job(app, job)
    assert ev[0] == "result"
    assert bridge.commands[0] == "limit(sin(x)/x,x,0)"


@pytest.mark.parametrize("text", [None, "", "NSPIRE_OUT:abc"])
def test_solve_without_latex_on_the_clipboard(text):
    app, _, bridge = make_app(clip_text=text)
    ev = run_job(app)
    assert ev[:2] == ("error", "Clipboard has no text")
    assert bridge.sessions == 0


def test_solve_bad_latex_never_uses_the_bridge():
    app, _, bridge = make_app(clip_text=r"\frac{1")
    ev = run_job(app)
    assert ev[:2] == ("error", "Couldn't read the LaTeX")
    assert bridge.sessions == 0


def test_cas_error_is_reported():
    app, _, _ = make_app(replies={})  # FakeBridge raises KeyError
    ev = run_job(app)
    assert ev[:2] == ("error", "TI-Nspire error")
    app.handle(ev)
    assert app.popup.last.error


@pytest.mark.parametrize("ev", [("hotkey", hk.SOLVE), ("hotkey", hk.APPROX), ("hotkey", hk.PICK),
                                ("hotkey", hk.TO_LATEX), ("menu", hk.MENU_TO_LATEX, None),
                                ("menu", hk.MENU_SOLVE, None), ("menu", hk.MENU_PICK, None),
                                ("menu", hk.MENU_HISTORY, answer())])
def test_busy_shows_still_solving(ev):
    app, clip, bridge = make_app()
    app.busy.set()
    app.preview = "x^2-4=0"
    assert app.handle(ev) is True
    assert app.popup.last.title == "Still solving…"
    assert app.popup.last.body == "x^2-4=0"
    assert app.popup.last.seconds == 0  # stays until the answer replaces it
    assert bridge.sessions == 0 and clip.copies == []
    assert app.events.empty()


def test_hotkey_starts_a_solve():
    app, clip, _ = make_app()
    app.handle(("hotkey", hk.SOLVE))
    assert app.popup.last.title == "Solving…"
    ev = app.events.get(timeout=30)
    assert ev[0] == "result"
    assert clip.copies


@pytest.mark.parametrize("copy, want", [("latex", r"x = 1"), ("text", "x=1"), ("none", r"x = 1")])
def test_menu_copies_an_answer(copy, want):
    app, clip, _ = make_app(copy=copy)
    app.handle(("menu", hk.MENU_HISTORY + 3, answer()))
    ev = app.events.get(timeout=10)
    assert ev == ("copied", want)
    assert clip.copies == [want]
    app.handle(ev)
    assert app.popup.last.title == "Copied to the clipboard"


def test_menu_clear_and_quit():
    app, _, _ = make_app()
    app.history.add(answer())
    app.handle(("menu", hk.MENU_CLEAR, None))
    assert len(app.history) == 0
    assert app.handle(("menu", hk.MENU_QUIT, None)) is False
    assert app.root.quitted


def test_quit_hotkey():
    app, _, _ = make_app()
    assert app.handle(("hotkey", hk.QUIT)) is False
    assert app.root.quitted


# ---------------------------------------------------------------- TI-Nspire text -> LaTeX

def run_to_latex(app):
    app.busy.set()  # as request_to_latex() does
    app.to_latex()
    assert not app.busy.is_set()
    return app.events.get_nowait()


def test_to_latex_copies_latex_without_the_bridge():
    app, clip, bridge = make_app(clip_text="√(2)/2")
    ev = run_to_latex(app)
    assert ev == ("converted", "√(2)/2", r"\frac{\sqrt{2}}{2}", None)
    assert clip.copies == [r"\frac{\sqrt{2}}{2}"]
    assert bridge.sessions == 0
    assert app.handle(ev) is True
    assert app.popup.last.title.startswith("TI-Nspire text to LaTeX:")
    assert app.popup.last.body == r"\frac{\sqrt{2}}{2}" and not app.popup.last.error
    assert app.history.items()[0].copy_text("latex") == r"\frac{\sqrt{2}}{2}"


def test_to_latex_draws_the_result():
    class Renderer:
        def render(self, latex):
            self.latex = latex
            return b"png"
    app, _, _ = make_app(clip_text="x^2")
    app.renderer = Renderer()
    assert run_to_latex(app)[3] == b"png"
    assert app.renderer.latex == "x^{2}"


@pytest.mark.parametrize("text, title", [(None, "Clipboard has no text"), ("  ", "Clipboard has no text"),
                                         ("NSPIRE_OUT:abc", "Clipboard has no text"),
                                         ("x∫y", "Couldn't convert to LaTeX"),
                                         ("Define f(x)=x", "Couldn't convert to LaTeX")])
def test_to_latex_failure_leaves_the_clipboard(text, title):
    app, clip, _ = make_app(clip_text=text)
    ev = run_to_latex(app)
    assert ev[:2] == ("error", title)
    assert clip.copies == []
    app.handle(ev)
    assert app.popup.last.error


def test_to_latex_hotkey_and_menu_start_a_conversion():
    for ev in [("hotkey", hk.TO_LATEX), ("menu", hk.MENU_TO_LATEX, None)]:
        app, clip, _ = make_app(clip_text="5.*_km►_m")
        app.handle(ev)
        got = app.events.get(timeout=30)
        assert got[0] == "converted"
        assert clip.copies == [r"5.0 \mathrm{km} \blacktriangleright \mathrm{m}"]


def test_prepare_picker():
    app, _, bridge = make_app(clip_text=r"x+y=3, x-y=1")
    app._prepare_picker()
    kind, latex, defaults = app.events.get_nowait()
    assert (kind, latex) == ("picker", r"x+y=3, x-y=1")
    assert defaults["solve"] == "x, y"
    assert bridge.sessions == 0


@pytest.mark.parametrize("text, title", [(r"\frac{1", "Couldn't read the LaTeX"), ("", "Clipboard has no text")])
def test_prepare_picker_errors(text, title):
    app, _, _ = make_app(clip_text=text)
    app._prepare_picker()
    assert app.events.get_nowait()[:2] == ("error", title)


def test_pump_survives_a_failing_event():
    app, _, _ = make_app()
    app.events.put(("result",))  # malformed: handle() raises
    app.events.put(("hotkey", hk.QUIT))
    app.pump()
    assert app.popup.shown[0].title == "Hotkey error"
    assert app.root.quitted


# ---------------------------------------------------------------- picker window (needs a display)

@pytest.fixture(scope="module")
def root():
    # One Tk for all: creating a second Tk after destroying one sometimes fails
    # to find its Tcl library (uv's Python on Windows).
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    r.destroy()


def make_picker(root, latex=r"x^2-1", **kw):
    picked, closed = [], []
    p = hk.ActionPicker(root, latex, hk.picker_defaults(latex), lambda *a: picked.append(a),
                        on_close=lambda: closed.append(True), show=False, **kw)
    return p, picked, closed


def test_picker_filter_and_pick(root):
    p, picked, closed = make_picker(root)
    assert p.shown == hk.PICKER_ACTIONS
    p.query.set("fac")
    assert p.shown == ["factor", "cfactor"]
    assert p.list.size() == 2
    p._move(1)
    assert p.selected() == "cfactor"
    p._move(-1)
    p._enter()
    assert picked == [("factor", None, {})]
    assert closed == [True] and p.win is None


def test_picker_asks_for_the_variable(root):
    p, picked, _ = make_picker(root, latex=r"\sin(t)")
    p.choose("diff")
    assert p.action == "diff"
    assert p.var_entry.get() == "t"
    p.var_entry.delete(0, "end")
    p.var_entry.insert(0, "u")
    p._enter()
    assert picked == [("diff", "u", {})]


def test_picker_extra_field(root):
    p, picked, _ = make_picker(root)
    p.choose("taylor")
    p.extra_entry.delete(0, "end")
    p.extra_entry.insert(0, "abc")
    p.submit()
    assert picked == [] and "whole number" in p.hint.cget("text")
    p.extra_entry.delete(0, "end")
    p.extra_entry.insert(0, "3")
    p.submit()
    assert picked == [("taylor", "x", {"order": 3})]


def test_picker_escape_goes_back_then_closes(root):
    p, picked, closed = make_picker(root)
    p.choose("limit")
    p._escape()
    assert p.action is None and p.win is not None
    p._escape()
    assert p.win is None and closed == [True] and picked == []


def test_app_runs_the_picked_action(root, monkeypatch):
    real = hk.ActionPicker
    monkeypatch.setattr(hk, "ActionPicker", lambda *a, **kw: real(*a, **{**kw, "show": False}))  # keep focus
    replies = {"factor(x^2 - 1)": "(x−1)*(x+1)"}
    app, clip, bridge = make_app(clip_text="x^2-1", replies=replies, root=root)
    app.show_picker("x^2-1", hk.picker_defaults("x^2-1"))
    assert app.picker is not None
    app.picker.choose("factor")
    assert app.picker is None
    assert app.popup.last.title == "factor…"
    ev = app.events.get(timeout=30)
    assert ev[0] == "result"
    assert bridge.commands == ["factor(x^2 - 1)"]
    assert clip.copies == [ev[2]["result_latex"]]
