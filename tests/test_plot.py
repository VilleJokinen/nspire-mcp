"""Offline tests of nspire_plot.py's pure parts: argument checks, the seq() commands,
parsing the CAS's lists, and where lines are broken. No bridge and no pywin32, so these
run on every OS. The sampling loop and the tool itself are tested in test_server.py.
"""
from __future__ import annotations

import math
import re
from decimal import Decimal

import pytest

import nspire_plot as pl
from nspire_chars import NS_EXP, NS_I


def spec(*cmds: str, x_min=-5, x_max=5, samples=200, **kw) -> pl.PlotSpec:
    return pl.prepare([pl.Func(c, c) for c in cmds], "x", x_min, x_max, samples, **kw)


# ------------------------------------------------------------ arguments

def test_prepare_checks_arguments():
    with pytest.raises(ValueError, match="at least one"):
        pl.prepare([], "x", -1, 1, 100)
    with pytest.raises(ValueError, match="At most 8"):
        spec(*["x"] * 9)
    with pytest.raises(ValueError, match="less than x_max"):
        spec("x", x_min=2, x_max=1)
    with pytest.raises(ValueError, match="finite"):
        spec("x", x_max=math.inf)
    with pytest.raises(ValueError, match="finite"):
        spec("x", x_min=True)
    with pytest.raises(ValueError, match="variable name"):
        pl.prepare([pl.Func("x", "x")], "2x", -1, 1, 100)
    with pytest.raises(ValueError, match="y_min must be less"):
        spec("x", y_min=3, y_max=3)
    with pytest.raises(ValueError, match="single line"):
        spec("x+\n1")


@pytest.mark.parametrize("cmd", ["a:=5", "Define f(x)=x", "DelVar a", "5→a", "x=:b", "setMode(2,2)"])
def test_prepare_refuses_commands_that_store(cmd):
    with pytest.raises(ValueError, match="only evaluates"):
        spec(cmd)


def test_samples_clamped_with_a_note():
    s = spec("x", samples=5000)
    assert s.grid.n == pl.SAMPLES_MAX and "samples set to 1000" in s.notes[0]
    s = spec("x", samples=3)
    assert s.grid.n == pl.SAMPLES_MIN
    assert spec("x", samples=200).notes == []
    with pytest.raises(ValueError, match="whole number"):
        spec("x", samples=math.nan)


def test_strip_lhs():
    assert pl.strip_lhs("y=x^2", "x") == "x^2"
    assert pl.strip_lhs(" f(x) = sin(x)", "x") == "sin(x)"
    assert pl.strip_lhs("g(t)=t^2", "t") == "t^2"
    assert pl.strip_lhs("x^2=4", "x") == "x^2=4"        # an equation, not a function: left alone
    assert pl.strip_lhs("f(2)=x", "x") == "f(2)=x"
    assert pl.strip_lhs("y=x", "y") == "y=x"            # y is the variable here
    assert pl.strip_lhs("y<=x", "x") == "y<=x"


# ------------------------------------------------------------ seq commands

def _ti_seq_count(start: str, stop: str, step: str) -> int:
    """How many terms seq(...,x,start,stop,step) has: start + k*step <= stop."""
    a, b, h = Decimal(start), Decimal(stop), Decimal(step)
    return int((b - a) / h) + 1


@pytest.mark.parametrize("x_min,x_max,n", [(-10, 10, 200), (-5, 5, 201), (0, 1, 1000), (-1e-3, 2e-3, 10),
                                           (1e14, 1e15, 997), (-math.pi, math.pi, 333)])
def test_seq_has_exactly_the_requested_points(x_min, x_max, n):
    s = pl.prepare([pl.Func("x", "x")], "x", x_min, x_max, n)
    for i0, i1 in [(0, n), (0, 1), (n - 1, n), (n // 3, n // 3 + 7)]:
        cmd = pl.seq_command("x", "x", s.grid, i0, i1)
        assert cmd.startswith("seq(approx(x),x,")
        start, stop, step = cmd[len("seq(approx(x),x,"):-1].split(",")
        assert "e" not in (start + stop + step).lower()     # plain decimals only
        assert _ti_seq_count(start, stop, step) == i1 - i0
        assert float(start) == pytest.approx(s.grid.x(i0), abs=1e-9 * max(1, abs(x_max)))


def test_grid_ends_exactly():
    s = spec("x", x_min=-1, x_max=1, samples=13)
    assert s.grid.x(0) == -1 and s.grid.x(12) == 1


# ------------------------------------------------------------ parsing

def test_parse_values():
    text = "{0.,0.841471,undef,1.2" + NS_EXP + "−5,−2.5,∞,−∞,1.+2.*" + NS_I + ",3}"
    assert pl.parse_values(text) == [0.0, 0.841471, None, 1.2e-05, -2.5, None, None, None, 3.0]
    assert pl.parse_values("{}") == []
    assert pl.parse_values(" { 1.5 , −.5 } ") == [1.5, -0.5]


def test_parse_values_rejects_symbols():
    with pytest.raises(pl.NotNumeric) as e:
        pl.parse_values("{0.,0.5*a,1.}")
    assert e.value.index == 1 and e.value.item == "0.5*a"
    with pytest.raises(pl.NotNumeric):
        pl.parse_values("{f(1.)}")
    with pytest.raises(ValueError, match="expected a list"):
        pl.parse_values("1.5")


def test_parse_values_nested_commas():
    # a list or matrix result per point isn't a number: refused, not split wrongly
    with pytest.raises(pl.NotNumeric) as e:
        pl.parse_values("{{1,2},3.}")
    assert e.value.item == "{1,2}"


# ------------------------------------------------------------ gaps and jumps

def _ys(f, s: pl.PlotSpec):
    out = []
    for i in range(s.grid.n):
        try:
            v = f(s.grid.x(i))
            out.append(v if math.isfinite(v) else None)
        except (ValueError, ZeroDivisionError):
            out.append(None)
    return out


def test_tan_is_broken_at_each_asymptote():
    s = spec("tan(x)")
    ys = _ys(math.tan, s)
    lo, hi, clipped = pl.view_limits([ys])
    assert clipped  # the spikes next to the asymptotes don't set the y range
    cuts = [s.grid.x(i) + s.grid.step / 2 for i in pl.breaks(ys, lo, hi)]
    want = [-3 * math.pi / 2, -math.pi / 2, math.pi / 2, 3 * math.pi / 2]
    assert len(cuts) == 4 and all(abs(c - w) < s.grid.step for c, w in zip(cuts, want, strict=True))


def test_reciprocal_is_broken_at_zero():
    s = spec("1/x", samples=200)
    ys = _ys(lambda x: 1 / x, s)
    lo, hi, _ = pl.view_limits([ys])
    assert pl.breaks(ys, lo, hi) == [99]


@pytest.mark.parametrize("f", [math.sin, math.exp, lambda x: x ** 2, lambda x: x ** 3 - 30 * x,
                               lambda x: 1 if x > 0 else -1])
def test_continuous_curves_are_not_broken(f):
    # steep or one-sided steps are drawn; only jumps against the curve's direction are cut
    s = spec("f(x)")
    ys = _ys(f, s)
    lo, hi, _ = pl.view_limits([ys])
    assert pl.breaks(ys, lo, hi) == []


def test_runs_of_gaps():
    assert pl.runs([None, None, 1.0, 2.0, None, 3.0, None]) == [(0, 1), (4, 4), (6, 6)]
    assert pl.runs([1.0, 2.0]) == []


def test_view_limits():
    assert pl.view_limits([[None, None]]) == (-1.0, 1.0, False)
    lo, hi, clipped = pl.view_limits([[2.0, 2.0]])
    assert lo < 2 < hi and not clipped
    lo, hi, _ = pl.view_limits([[0.0, 10.0]])
    assert lo == pytest.approx(-0.5) and hi == pytest.approx(10.5)
    # the user's limits win, also over clipping
    ys = [float(i) for i in range(100)] + [1e9]
    assert pl.view_limits([ys])[2] is True
    assert pl.view_limits([ys], -1, 5) == (-1, 5, False)
    lo, hi, _ = pl.view_limits([[0.0, 10.0]], y_min=20)
    assert lo == 20 and hi > 20


# ------------------------------------------------------------ summary

def test_summary_names_gaps_jumps_and_warnings():
    from collections import Counter
    s = pl.prepare([pl.Func(r"\sqrt{x}", "√(x)"), pl.Func("1/x", "1/x")], "x", -5, 5, 200)
    ys = [_ys(lambda x: math.sqrt(x) if x >= 0 else math.nan, s), _ys(lambda x: 1 / x, s)]
    got = pl.Sampled(ys, [Counter({"error 800 (non-real result)": 100}), Counter()], 2)
    text = pl.summary(s, got, ["a is stored in the document"])
    assert "Plotted 2 functions for x from −5 to 5: 200 points each" in text
    assert r"1. \sqrt{x}  (TI-Nspire: √(x)): y from 0.158511 to 2.23607" in text
    assert "undefined or non-real at 100 of 200 points (x in [−5, −0.0251256]; TI-Nspire: error 800" in text
    assert "not connected across jumps (asymptotes) near x≈0." in text  # not 4.5e−16
    assert text.endswith("Warnings:\n- a is stored in the document")


def test_summary_no_real_values():
    from collections import Counter
    s = spec("√(x)", x_min=-5, x_max=-1, samples=10)
    got = pl.Sampled([[None] * 10], [Counter({"error 800 (non-real result)": 10})], 3)
    assert "no real value anywhere in this range (error 800 (non-real result))" in pl.summary(s, got)


def test_render_png():
    pytest.importorskip("matplotlib")
    from collections import Counter
    s = pl.prepare([pl.Func("tan(x)", "tan(x)"), pl.Func(r"\frac{1}{x}", "1/x"), pl.Func("$_a", "$_a")],
                   "x", -5, 5, 100)
    ys = [_ys(math.tan, s), _ys(lambda x: 1 / x, s), [None] * 100]
    png = pl.render_png(s, pl.Sampled(ys, [Counter()] * 3, 1))
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 5000
    one = pl.prepare([pl.Func("x^2", "x^2")], "x", 0, 1, 10)
    assert pl.render_png(one, pl.Sampled([[v / 10 for v in range(10)]], [Counter()], 1)).startswith(b"\x89PNG")


# ------------------------------------------------------------ parametric, polar, implicit

def test_split_pair():
    assert pl.split_pair("cos(t), sin(t)") == ("cos(t)", "sin(t)")
    assert pl.split_pair("(cos(t), sin(2*t))") == ("cos(t)", "sin(2*t)")
    assert pl.split_pair("{t^2,t^3}") == ("t^2", "t^3")
    assert pl.split_pair(r"\left(\cos t, \sin t\right)") == (r"\cos t", r"\sin t")
    assert pl.split_pair("x=max(t,1), y=t") == ("x=max(t,1)", "y=t")  # commas inside brackets stay
    for bad in ("cos(t)", "a,b,c", "(t), ", "(cos(t))+(sin(t))"):
        with pytest.raises(ValueError, match="parametric curve"):
            pl.split_pair(bad)


def test_implicit_function():
    assert pl.implicit_function("x^2+y^2=25", "c") == "(x^2+y^2)-(25)"
    assert pl.implicit_function("x*y-1", "c") == "x*y-1"
    assert pl.implicit_function("f(x=1,y)=y", "c") == "(f(x=1,y))-(y)"  # '=' inside brackets isn't the split
    with pytest.raises(ValueError, match="inequality"):
        pl.implicit_function("x^2+y^2<=1", "c")
    with pytest.raises(ValueError, match="more than one"):
        pl.implicit_function("x=y=1", "c")
    with pytest.raises(ValueError, match="empty"):
        pl.implicit_function("x^2=", "c")


def test_prepare_parametric():
    s = pl.prepare_kind("parametric", [("lissajous", "x=cos(t), y=sin(2*t)")])
    assert s.kind == "parametric" and s.var == "t" and s.grid.n == 400
    assert (s.grid.x_min, s.grid.x_max) == (0.0, 2 * math.pi)
    assert [f.command for f in s.funcs] == ["cos(t)", "sin(2*t)"] and s.funcs[0].call is None
    assert s.curves[0].parts == ["cos(t)", "sin(2*t)"] and s.curves[0].channels == [0, 1]
    assert s.helpers == [] and s.x_min is None and s.y_min is None
    s = pl.prepare_kind("parametric", [("a", "t, t^2"), ("b", "2*t, t")], t_min=-1, t_max=1, samples=50,
                        x_min=-2, x_max=2)
    assert len(s.funcs) == 4 and s.curves[1].channels == [2, 3] and s.x_min == -2
    with pytest.raises(ValueError, match="only evaluates"):
        pl.prepare_kind("parametric", [("a", "t, b:=t")])
    with pytest.raises(ValueError, match="t_min must be less"):
        pl.prepare_kind("parametric", [("a", "t, t")], t_min=1, t_max=0)
    with pytest.raises(ValueError, match="At most 4"):
        pl.prepare_kind("parametric", [("a", "t, t")] * 5)


def test_prepare_polar_uses_radian_helpers():
    s = pl.prepare_kind("polar", [("cardioid", "r=1+cos(θ)")], samples=100)
    assert s.var == "θ" and s.grid.n == 100 and s.curves[0].parts == ["1+cos(θ)"]
    assert s.helper_names == ["mcpplotp1", "mcpplotp2"]
    assert s.helpers[0] == ("cardioid", "Define mcpplotp1(mcpa,mcpb,mcph)=Func:Local mcpm,θ:mcpm:=setMode(2,1)"
                                        ":Return seq(approx((1+cos(θ))*cos(θ)),θ,mcpa,mcpb,mcph):EndFunc")
    assert "*sin(θ)),θ," in s.helpers[1][1]
    assert [f.call for f in s.funcs] == ["mcpplotp1", "mcpplotp2"]
    assert pl.seq_command(s.funcs[0].command, "θ", s.grid, 0, 100, s.funcs[0].call).startswith("mcpplotp1(0.0,")
    assert any("radians" in n for n in s.notes)
    with pytest.raises(ValueError, match="temporary function"):
        pl.prepare_kind("polar", [("x", "1:2")])
    with pytest.raises(ValueError, match="variable name"):
        pl.prepare_kind("polar", [("x", "1")], var="mcpa")


def test_prepare_implicit_grid():
    s = pl.prepare_kind("implicit", [("circle", "x^2+y^2=25")], samples=20, x_min=-6, x_max=6)
    assert s.kind == "implicit" and s.grid.n == 20 and s.ygrid is not None and s.ygrid.n == 20
    assert (s.ygrid.x_min, s.ygrid.x_max) == (-10.0, 10.0)
    assert s.helpers == [("circle", "Define mcpplotf1(x,y)=(x^2+y^2)-(25)")]
    assert len(s.funcs) == 20
    assert s.funcs[0].command == "mcpplotf1(x,-10.0)" and s.funcs[-1].command == "mcpplotf1(x,10.0)"
    assert pl.seq_command(s.funcs[0].command, "x", s.grid, 0, 20).startswith(
        "seq(approx(mcpplotf1(x,-10.0)),x,-6.0,")
    s = pl.prepare_kind("implicit", [("a", "x=y"), ("b", "x=-y")])
    assert s.grid.n == pl.GRID_DEFAULT and len(s.funcs) == 2 * pl.GRID_DEFAULT
    assert s.curves[1].channels[0] == pl.GRID_DEFAULT and s.helper_names == ["mcpplotf1", "mcpplotf2"]
    s = pl.prepare_kind("implicit", [("a", "x=y")], samples=500)
    assert s.grid.n == pl.GRID_MAX and "per axis" in s.notes[0]
    with pytest.raises(ValueError, match="leave var out"):
        pl.prepare_kind("implicit", [("a", "x=y")], var="t")
    with pytest.raises(ValueError, match="At most 4"):
        pl.prepare_kind("implicit", [("a", "x=y")] * 5)
    with pytest.raises(ValueError, match="t_min"):
        pl.prepare_kind("implicit", [("a", "x=y")], t_min=0)


def test_prepare_kind_function_is_the_old_prepare():
    s = pl.prepare_kind("function", [("y=x^2", "y=x^2")])
    assert s.kind == "function" and (s.grid.x_min, s.grid.x_max, s.grid.n) == (-10.0, 10.0, 200)
    assert s.funcs[0].command == "x^2" and s.curves == []
    with pytest.raises(ValueError, match="kind must be"):
        pl.prepare_kind("spiral", [("a", "x")])
    with pytest.raises(ValueError, match="t_min"):
        pl.prepare_kind("function", [("a", "x")], t_max=3)


def _grid_values(s: pl.PlotSpec, F) -> list[list[float | None]]:
    assert s.ygrid is not None
    rows = []
    for j in range(s.ygrid.n):
        row: list[float | None] = []
        for i in range(s.grid.n):
            try:
                v = F(s.grid.x(i), s.ygrid.x(j))
                row.append(v if math.isfinite(v) else None)
            except (ValueError, ZeroDivisionError):
                row.append(None)
        rows.append(row)
    return rows


def test_circle_crossings_and_no_poles():
    s = pl.prepare_kind("implicit", [("c", "x^2+y^2=25")], samples=41)
    rows = _grid_values(s, lambda x, y: x * x + y * y - 25)
    assert pl.pole_points(rows) == set()
    ext = pl.cell_extent(s, pl.crossing_cells(rows, set()))
    assert ext is not None
    assert ext[0][0] == pytest.approx(-5.5) and ext[0][1] == pytest.approx(5.5)


def test_pole_is_not_a_crossing():
    # y = 1/x written as y-1/x: F jumps from -inf to +inf at x=0, which isn't part of the curve
    s = pl.prepare_kind("implicit", [("h", "y=1/x")], samples=20)
    rows = _grid_values(s, lambda x, y: y - 1 / x)
    poles = pl.pole_points(rows)
    assert poles and all(abs(s.grid.x(i)) < s.grid.step for _, i in poles)
    cells = pl.crossing_cells(rows, poles)
    assert cells and all(i != 9 for _, i in cells)  # no cell spans x=0 (between x(9) and x(10))


def _curve_got(s: pl.PlotSpec, fx, fy) -> pl.Sampled:
    from collections import Counter
    ys: list[list[float | None]] = []
    for _ in s.curves:
        ys += [_ys(fx, s), _ys(fy, s)]
    return pl.Sampled(ys, [Counter() for _ in ys], 1)


def test_curve_summary_and_render():
    pytest.importorskip("matplotlib")
    s = pl.prepare_kind("parametric", [("circle", "cos(t), sin(t)")], samples=100)
    got = _curve_got(s, math.cos, math.sin)
    text = pl.summary(s, got)
    assert "Plotted 1 parametric curve for t from 0 to 6.28319: 100 points each" in text
    assert "1. circle  (TI-Nspire: (cos(t), sin(t))): x from −0.999497 to 1, y from −0.999874 to 0.999874." in text
    assert pl.render_png(s, got).startswith(b"\x89PNG")
    info = pl.curve_info(s, got)
    assert info[0]["points"] == 100 and info[0]["x_range"] == [pytest.approx(-0.999497), 1.0]


def test_parametric_gap_and_jump():
    # x = t, y = 1/(t-3): undefined at t=3 is a gap; the jump around it isn't drawn
    s = pl.prepare_kind("parametric", [("h", "t, 1/(t-3)")], t_min=0, t_max=6, samples=61)
    got = _curve_got(s, lambda t: t, lambda t: 1 / (t - 3) if abs(t - 3) > 1e-9 else math.nan)
    assert "undefined or non-real at 1 of 61 points (t≈3)" in pl.summary(s, got)
    s2 = pl.prepare_kind("parametric", [("h", "t, 1/(t-3)")], t_min=0, t_max=6, samples=60)
    got2 = _curve_got(s2, lambda t: t, lambda t: 1 / (t - 3))
    assert "not connected across jumps (asymptotes) near t≈3" in pl.summary(s2, got2)


def test_polar_summary_says_radians():
    s = pl.prepare_kind("polar", [("rose", "sin(3*θ)")], samples=200)
    got = _curve_got(s, lambda t: math.sin(3 * t) * math.cos(t), lambda t: math.sin(3 * t) * math.sin(t))
    text = pl.summary(s, got, ["w"])
    assert "Plotted 1 polar curve r(θ) for θ from 0 to 6.28319" in text
    assert "θ is in radians" in text and text.endswith("Warnings:\n- w")
    pytest.importorskip("matplotlib")
    assert pl.render_png(s, got).startswith(b"\x89PNG")


def test_implicit_summary_and_render():
    from collections import Counter
    s = pl.prepare_kind("implicit", [("circle", "x^2+y^2=25"), ("h", "y=1/x"), ("never", "x^2+y^2+1")],
                        samples=30)
    fs = (lambda x, y: x * x + y * y - 25, lambda x, y: y - 1 / x, lambda x, y: x * x + y * y + 1)
    ys = [row for F in fs for row in _grid_values(s, F)]
    got = pl.Sampled(ys, [Counter() for _ in ys], 1)
    text = pl.summary(s, got)
    assert "Plotted 3 implicit curves F(x,y)=0 for x from −10 to 10 and y from −10 to 10" in text
    assert "on a 30×30 grid" in text
    assert "1. circle  (TI-Nspire: x^2+y^2=25): passes through" in text
    assert "where F jumps through ±∞" in text
    assert "3. never  (TI-Nspire: x^2+y^2+1 = 0): F doesn't change sign" in text
    info = pl.curve_info(s, got)
    assert info[2]["x_range"] is None and info[0]["total"] == 900
    pytest.importorskip("matplotlib")
    assert pl.render_png(s, got).startswith(b"\x89PNG")


# ------------------------------------------------------------ annotations

_ZEROS_RE = re.compile(r"^zeros\((.*),x\)\|x>=(\S+) and x<=(\S+)$")
_NSOLVE_RE = re.compile(r"^nSolve\((.*)=0,x,([^,]+),([^,]+)\)$")
_POINT_RE = re.compile(r"^\{(\(.*?\)),approx\(x\),approx\(\1\)(,approx\(derivative\(\1,x,2\)\))?\}\|x=\((.*)\)$")
_SLOPE_RE = re.compile(r"^approx\(derivative\((\(.*\)),x\)\|x=\((.*)\)([+-])(.+)\)$")


def _cas(v: float) -> str:
    v = 0.0 if abs(v) < 1e-12 else v  # the CAS's exact 0 where floats leave noise
    return f"{v + 0.0:.14g}".replace("e", NS_EXP).replace("-", "−")


class AnnotBridge:
    """Answers annotate()'s requests like the CAS would. zeros: {expr: reply text, or
    (False, error)}; funcs: {"(f)": (f, f', f'')} as Python functions; exact: {"−√(2)": value}
    for the exact forms the zeros replies use. nSolve bisects f, f' or f−g in its bracket.
    From batch dead_at + 1 on, every batch times out."""

    def __init__(self, zeros: dict, funcs: dict, exact: dict | None = None, dead_at: int | None = None):
        self.zeros, self.funcs, self.exact = zeros, funcs, exact or {}
        self.batches: list[list[str]] = []
        self.last_timeout: str | None = None
        self.dead_at = dead_at

    def _x(self, s: str) -> float:
        if s in self.exact:
            return self.exact[s]
        return float(s.replace("−", "-").replace(NS_EXP, "e"))

    def _target(self, expr: str):
        if expr in self.funcs:
            return self.funcs[expr][0]
        m = re.fullmatch(r"derivative\((\(.*\)),x\)", expr)
        if m:
            return self.funcs[m.group(1)][1]
        a, b = expr.split(")-(")
        f, g = self.funcs[a + ")"][0], self.funcs["(" + b][0]
        return lambda x: f(x) - g(x)

    def _one(self, e: str) -> dict:
        m = _ZEROS_RE.match(e)
        if m:
            r = self.zeros.get(m.group(1), (False, "error 910 (syntax error)"))
            if isinstance(r, str):
                return {"expr": e, "ok": True, "result": r}
            return {"expr": e, "ok": False, "error": r[1]}
        m = _NSOLVE_RE.match(e)
        if m:
            f, lo, hi = self._target(m.group(1)), float(m.group(2)), float(m.group(3))
            if f(lo) * f(hi) > 0:
                return {"expr": e, "ok": True, "result": '"No solution found"'}
            for _ in range(80):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if f(lo) * f(mid) > 0 else (lo, mid)
            return {"expr": e, "ok": True, "result": _cas((lo + hi) / 2)}
        m = _POINT_RE.match(e)
        if m:
            f, _, f2 = self.funcs[m.group(1)]
            x = self._x(m.group(3))
            y = f(x)
            if y is None:
                return {"expr": e, "ok": True, "result": "{undef,undef,undef}"}
            items = [_cas(y), _cas(x), _cas(y)] + ([_cas(f2(x))] if m.group(2) else [])
            return {"expr": e, "ok": True, "result": "{" + ",".join(items) + "}"}
        m = _SLOPE_RE.match(e)
        if m:
            x = self._x(m.group(2)) + (1 if m.group(3) == "+" else -1) * float(m.group(4))
            return {"expr": e, "ok": True, "result": _cas(self.funcs[m.group(1)][1](x))}
        return {"expr": e, "ok": False, "error": "error 910 (syntax error)"}

    def eval_batch(self, exprs, timeout_s=5.0, raw=False):
        self.batches.append(list(exprs))
        if self.dead_at is not None and len(self.batches) > self.dead_at:
            self.last_timeout = dead = "No reply from the TI-Nspire bridge within 5 s."
            return [{"expr": e, "ok": False, "error": dead if k == 0 else "skipped: " + dead}
                    for k, e in enumerate(exprs)]
        return [self._one(e) for e in exprs]


def _sampled(s: pl.PlotSpec, *fs) -> pl.Sampled:
    from collections import Counter
    return pl.Sampled([_ys(f, s) for f in fs], [Counter() for _ in fs], 1)


CUBIC = ("(x^3-3*x)", (lambda x: x ** 3 - 3 * x, lambda x: 3 * x * x - 3, lambda x: 6 * x))
LINE = ("(x)", (lambda x: x, lambda x: 1.0, lambda x: 0.0))


def test_prepare_annotations():
    s = spec("x^2", "x")
    assert pl.prepare_annotations(s, None) == [] and pl.prepare_annotations(s, []) == []
    assert pl.prepare_annotations(s, ["zeros", "extrema", "zeros"]) == ["zeros", "extrema"]
    assert pl.prepare_annotations(s, "intersections") == ["intersections"]
    with pytest.raises(ValueError, match="isn't one of zeros, extrema, intersections"):
        pl.prepare_annotations(s, ["roots"])
    one = spec("x^2")
    assert pl.prepare_annotations(one, ["zeros", "intersections"]) == ["zeros"]
    assert "Intersections need two or more functions" in one.notes[0]
    curve = pl.prepare_kind("parametric", [("c", "cos(t), sin(t)")])
    with pytest.raises(ValueError, match="function plots"):
        pl.prepare_annotations(curve, ["zeros"])
    assert pl.prepare_annotations(curve, None) == []


def test_annotation_commands():
    s = spec("x^3-3*x", "x", x_min=-2.5, x_max=3)
    tasks = pl.annotation_tasks(s, ["zeros", "extrema", "intersections"])
    assert [(t.kind, t.funcs) for t in tasks] == [("zeros", (0,)), ("extrema", (0,)), ("zeros", (1,)),
                                                  ("extrema", (1,)), ("intersections", (0, 1))]
    assert pl.exact_command(s, tasks[0]) == "zeros((x^3-3*x),x)|x>=-2.5 and x<=3.0"
    # derivative(), not d(): the bridge leaves d() unevaluated
    assert pl.exact_command(s, tasks[1]) == "zeros(derivative((x^3-3*x),x),x)|x>=-2.5 and x<=3.0"
    assert pl.exact_command(s, tasks[4]) == "zeros((x^3-3*x)-(x),x)|x>=-2.5 and x<=3.0"
    assert pl.numeric_command(s, tasks[0], 1.5, 1.75) == "nSolve((x^3-3*x)=0,x,1.5,1.75)"
    assert pl.point_command(s, tasks[0], "−√(3)") == "{(x^3-3*x),approx(x),approx((x^3-3*x))}|x=(−√(3))"
    assert pl.point_command(s, tasks[1], "1") == \
        "{(x^3-3*x),approx(x),approx((x^3-3*x)),approx(derivative((x^3-3*x),x,2))}|x=(1)"
    assert pl.slope_command(s, tasks[1], "0", -0.25) == "approx(derivative((x^3-3*x),x)|x=(0)-0.25)"
    st = pl.prepare([pl.Func("t^2", "t^2")], "t", -1, 1, 20)
    assert pl.exact_command(st, pl.annotation_tasks(st, ["zeros"])[0]) == "zeros((t^2),t)|t>=-1.0 and t<=1.0"


def test_exact_points():
    def ok(text):
        return {"ok": True, "result": text}
    assert pl.exact_points(ok("{−√(2),√(2)}")) == ["−√(2)", "√(2)"]
    assert pl.exact_points(ok("{}")) == []
    assert pl.exact_points(ok("{0.73908513321517}")) == ["0.73908513321517"]
    assert pl.exact_points(ok("{−11*π/4,π/4}")) == ["−11*π/4", "π/4"]
    assert pl.exact_points(ok("{n6*π}")) is None           # every multiple: not points
    assert pl.exact_points(ok("x=1 or x=2")) is None
    assert pl.exact_points(ok("{x>0 and x<1}")) is None
    assert pl.exact_points({"ok": False, "error": "error 800"}) is None


def test_shown_inverse_functions():
    # the real CAS: zeros(derivative((cos(x)-x/5),x),x)|... -> {sin(1/5)-π,−sin(1/5),...}
    assert pl.shown("−sin(1/5) ") == "−sin⁻¹(1/5)"
    assert pl.shown(f"2.5{NS_EXP}3") == "2.5ᴇ3"


def test_number():
    assert pl.number("−1.4142135623731") == pytest.approx(-1.4142135623731)
    assert pl.number(f"3.{NS_EXP}−4") == pytest.approx(3e-4)
    assert pl.number('"No solution found"') is None and pl.number("undef") is None and pl.number("π") is None


def test_brackets():
    s = spec("x^3-3*x", "1/x", x_min=-3, x_max=3, samples=61)
    got = _sampled(s, CUBIC[1][0], lambda x: 1 / x)
    view = pl.plot_view(s, got)
    t = pl.annotation_tasks(s, ["zeros", "extrema"])
    zero_br = pl.brackets(s, got, t[0], view)
    # x = −√3, √3 between samples, and x = 0 on a sample
    assert len(zero_br) == 3 and any(lo < 0 < hi for lo, hi in zero_br)
    assert any(lo < -math.sqrt(3) < hi for lo, hi in zero_br)
    assert [(round(lo, 6), round(hi, 6)) for lo, hi in pl.brackets(s, got, t[1], view)] == [(-1.1, -0.9), (0.9, 1.1)]
    # 1/x changes sign at 0 through ±∞ (and 0 itself is a gap): no bracket there
    assert pl.brackets(s, got, t[2], view) == []


def test_classify():
    assert pl._classify(-6.0, []) == "max" and pl._classify(6.0, []) == "min"
    assert pl._classify(0.0, [-1e-4, 1e-4]) == "min"    # x^4 at 0
    assert pl._classify(None, [2.0, -2.0]) == "max"
    assert pl._classify(0.0, [3e-4, 3e-4]) == "stationary"   # x^3 at 0
    assert pl._classify(0.0, [None, None]) == "stationary"


def test_annotate_exact():
    s = spec("x^3-3*x", "x", x_min=-3, x_max=3, samples=61)
    got = _sampled(s, CUBIC[1][0], LINE[1][0])
    b = AnnotBridge({"(x^3-3*x)": "{−√(3),0,√(3)}", "derivative((x^3-3*x),x)": "{−1,1}", "(x)": "{0}",
                     "derivative((x),x)": "{}", "(x^3-3*x)-(x)": "{−2,0,2}"},
                    dict([CUBIC, LINE]), {"−√(3)": -math.sqrt(3), "√(3)": math.sqrt(3)})
    ann = pl.annotate(b, s, got, ["zeros", "extrema", "intersections"], 5)
    assert ann.requests == 2 and len(b.batches) == 2    # no nSolve round
    assert len(b.batches[0]) == 5
    by = [(m.type, m.funcs, m.x, m.y) for m in ann.marks]
    assert by == [("zero", [0], "−√(3)", "0"), ("zero", [0], "0", "0"), ("zero", [0], "√(3)", "0"),
                  ("zero", [1], "0", "0"), ("max", [0], "−1", "2"), ("min", [0], "1", "−2"),
                  ("intersection", [0, 1], "−2", "−2"), ("intersection", [0, 1], "0", "0"),
                  ("intersection", [0, 1], "2", "2")]
    assert ann.marks[0].x_approx == pytest.approx(-1.7320508) and ann.marks[0].method == "exact"
    assert ann.warnings == [] and ann.notes == []
    d = ann.marks[4].as_dict()
    assert d == {"type": "max", "functions": [1], "x": "−1", "y": "2", "x_approx": -1.0, "y_approx": 2.0,
                 "method": "exact"}
    text = pl.summary(s, got, None, ann)
    assert "Marked points (every coordinate computed by the TI-Nspire CAS):" in text
    assert "- zeros of 1: x = −√(3) ≈ −1.73205; x = 0; x = √(3) ≈ 1.73205" in text
    assert "- maximum of 1: (−1, 2)" in text and "- minimum of 1: (1, −2)" in text
    assert "- intersections of 1 and 2: (−2, −2); (0, 0); (2, 2)" in text


def test_annotate_stationary_and_flat_minimum():
    s = spec("x^3", "x^4", x_min=-1, x_max=1, samples=21)
    cube = ("(x^3)", (lambda x: x ** 3, lambda x: 3 * x * x, lambda x: 6 * x))
    quart = ("(x^4)", (lambda x: x ** 4, lambda x: 4 * x ** 3, lambda x: 12 * x * x))
    got = _sampled(s, cube[1][0], quart[1][0])
    b = AnnotBridge({"derivative((x^3),x)": "{0}", "derivative((x^4),x)": "{0}"}, dict([cube, quart]))
    ann = pl.annotate(b, s, got, ["extrema"], 5)
    assert [(m.type, m.funcs) for m in ann.marks] == [("min", [1]), ("stationary", [0])]
    assert len(b.batches[1]) == 6  # each point with its two slopes
    assert "- stationary point (not an extremum) of 1: (0, 0)" in pl.summary(s, got, None, ann)


def test_annotate_falls_back_to_nsolve():
    # say zeros() failed for cos(x)=x/5, and the extrema list came back empty although the samples turn
    cosx = ("(cos(x)-x/5)", (lambda x: math.cos(x) - x / 5, lambda x: -math.sin(x) - 0.2, lambda x: -math.cos(x)))
    s = spec("cos(x)-x/5", x_min=-10, x_max=10, samples=200)
    got = _sampled(s, cosx[1][0])
    b = AnnotBridge({"(cos(x)-x/5)": (False, "error 1010 (Questionable accuracy)"),
                     "derivative((cos(x)-x/5),x)": "{}"}, dict([cosx]))
    ann = pl.annotate(b, s, got, ["zeros", "extrema"], 5)
    assert len(b.batches) == 3 and all(e.startswith("nSolve(") for e in b.batches[1])
    zeros = [m for m in ann.marks if m.type == "zero"]
    assert [round(m.x_approx, 5) for m in zeros] == [-3.83747, -1.97738, 1.30644]
    assert all(m.method == "numeric" for m in ann.marks)
    assert {m.type for m in ann.marks} == {"zero", "max", "min"}
    assert ann.errors == ["error 1010 (Questionable accuracy)"]
    assert any("looked for them numerically" in n for n in ann.notes)
    assert "- zeros of 1 (found numerically): x = −3.83747; x = −1.97738; x = 1.30644" in \
        pl.summary(s, got, None, ann)


def test_annotate_unusable_reply_without_sign_changes_is_a_warning():
    s = spec("x^2+1", x_min=-2, x_max=2, samples=20)
    sq = ("(x^2+1)", (lambda x: x * x + 1, lambda x: 2 * x, lambda x: 2.0))
    got = _sampled(s, sq[1][0])
    ann = pl.annotate(AnnotBridge({"(x^2+1)": "{n1*π}"}, dict([sq])), s, got, ["zeros"], 5)
    assert ann.marks == [] and "zeros() gave {n1*π}; none marked" in ann.warnings[0]


def test_annotate_timeout_is_a_warning():
    s = spec("x^3-3*x", x_min=-3, x_max=3, samples=61)
    got = _sampled(s, CUBIC[1][0])
    b = AnnotBridge({"(x^3-3*x)": "{0}"}, dict([CUBIC]), dead_at=1)  # the points' batch times out
    ann = pl.annotate(b, s, got, ["zeros"], 5)
    assert ann.marks == [] and "didn't finish the annotations (values at the points)" in ann.warnings[0]


def test_annotate_caps_points():
    s = spec("sin(x)", x_min=-100, x_max=100, samples=1000)
    sinx = ("(sin(x))", (math.sin, math.cos, lambda x: -math.sin(x)))
    got = _sampled(s, sinx[1][0])
    reply = "{" + ",".join(f"{k}*π" for k in range(-31, 32)) + "}"
    b = AnnotBridge({"(sin(x))": reply}, dict([sinx]), {f"{k}*π": k * math.pi for k in range(-31, 32)})
    ann = pl.annotate(b, s, got, ["zeros"], 5)
    assert len(ann.marks) == pl.MAX_PER_SET
    assert "the CAS found 63; the first 12 are marked" in ann.notes[0]


def test_render_png_with_marks():
    pytest.importorskip("matplotlib")
    s = spec("x^3-3*x", "x", x_min=-3, x_max=3, samples=61)
    got = _sampled(s, CUBIC[1][0], LINE[1][0])
    marks = [pl.Mark("zero", [0], "0", "0", 0.0, 0.0, "exact"), pl.Mark("max", [0], "−1", "2", -1.0, 2.0, "exact"),
             pl.Mark("min", [0], "1", "−2", 1.0, -2.0, "exact"),
             pl.Mark("stationary", [1], "0", "0", 0.0, 0.0, "exact"),
             pl.Mark("intersection", [0, 1], "2", "2", 2.0, 2.0, "exact"),
             pl.Mark("zero", [0], "50", "0", 50.0, 0.0, "exact")]   # outside the view: not drawn
    assert [m.x for m in pl.visible_marks(marks, pl.plot_view(s, got))] == ["0", "−1", "1", "0", "2"]
    plain = pl.render_png(s, got)
    marked = pl.render_png(s, got, marks=marks)
    assert marked.startswith(b"\x89PNG") and marked != plain
    many = [pl.Mark("zero", [0], str(i), "0", -3 + i * 0.1, 0.0, "exact") for i in range(30)]
    assert pl.render_png(s, got, marks=many).startswith(b"\x89PNG")  # too many for labels
