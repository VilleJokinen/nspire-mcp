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
    assert len(cuts) == 4 and all(abs(c - w) < s.grid.step for c, w in zip(cuts, want))


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
