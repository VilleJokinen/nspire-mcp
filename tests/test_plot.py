"""Offline tests of nspire_plot.py's pure parts: argument checks, the seq() commands,
parsing the CAS's lists, and where lines are broken. No bridge and no pywin32, so these
run on every OS. The sampling loop and the tool itself are tested in test_server.py.
"""
from __future__ import annotations

import math
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
