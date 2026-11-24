"""Offline tests of nspire_steps: reading the lines, the CAS commands, and the verdicts
read from (fake) CAS replies. No bridge, no clipboard."""
from __future__ import annotations

import pytest

import nspire_steps as st


def ok(text: str) -> dict:
    return {"ok": True, "result": text}


def err(text: str) -> dict:
    return {"ok": False, "error": text}


def verdicts(spec, replies: dict) -> list[str]:
    cmds = st.commands(spec)
    assert set(replies) == set(cmds), (sorted(replies), cmds)
    return [s.verdict for s in st.judge(spec, replies)]


# ------------------------------------------------------------ reading lines

def test_raw_expression_chain():
    spec = st.prepare(["(x+1)^2-1", "= x^2+2x+1-1", "=x^2+2x"], latex=False)
    assert [ln.kind for ln in spec.lines] == ["expression"] * 3
    assert spec.var is None
    assert st.commands(spec) == ["expand(((x+1)^2-1)-(x^2+2x+1-1))", "expand((x^2+2x+1-1)-(x^2+2x))"]


def test_raw_equations_solved_once_each():
    spec = st.prepare(["2x+3=7", "⇔ 2x=4", "x=2"], latex=False)
    assert spec.var == "x"
    assert st.commands(spec) == ["solve(2x+3=7,x)", "solve(2x=4,x)", "solve(x=2,x)"]


def test_raw_var_choice_and_systems():
    assert st.prepare(["a*t=b", "t=b/a"], latex=False).var == "t"  # t before a and b
    spec = st.prepare(["x+y=3 and x-y=1", "x=2 and y=1"], latex=False, var="x, y")
    assert spec.lines[0].nspire == "solve(x+y=3 and x-y=1,{x,y})"


def test_raw_relations_not_fooled_by_brackets_or_arrows():
    assert st._top_level_relation("x^2=4")
    assert st._top_level_relation("x<=3")
    assert not st._top_level_relation("f(x=1)")       # inside brackets
    assert not st._top_level_relation("5_km@>_m")      # conversion, not a relation
    assert not st._top_level_relation('"a=b"&x')


def test_latex_lines():
    spec = st.prepare([r"$x^2-5x+6=0$", r"\iff (x-2)(x-3)=0", r"x=2 \lor x=3", r"&\Rightarrow x=2,\ x=3."])
    assert spec.var == "x"
    assert [ln.nspire for ln in spec.lines] == [
        "solve(x^2 - 5*x + 6=0,x)", "solve((x - 2)*(x - 3)=0,x)", "solve(x=2 or x=3,x)", "solve(x=2 or x=3,x)"]
    assert st.commands(spec) == ["solve(x^2 - 5*x + 6=0,x)", "solve((x - 2)*(x - 3)=0,x)", "solve(x=2 or x=3,x)"]


def test_latex_expression_chain_and_align():
    spec = st.prepare([r"(x+1)^2", r"&= x^2+2x+1 \\", r"= \frac{x^3+2x^2+x}{x}"])
    assert [ln.nspire for ln in spec.lines] == ["(x + 1)^2", "x^2 + 2*x + 1", "(x^3 + 2*x^2 + x)/x"]
    assert st.commands(spec)[0] == "expand(((x + 1)^2)-(x^2 + 2*x + 1))"


def test_latex_system_keeps_both_variables():
    spec = st.prepare([r"\begin{cases}x+y=3\\x-y=1\end{cases}", r"x=2,\ y=1"])
    assert spec.var == "{x,y}"
    assert [ln.nspire for ln in spec.lines] == ["solve(x + y=3 and x - y=1,{x,y})", "solve(x=2 and y=1,{x,y})"]


def test_latex_text_or():
    spec = st.prepare([r"x^2=4", r"x=2 \text{ or } x=-2"])
    assert spec.lines[1].nspire == "solve(x=2 or x=-2,x)"


def test_latex_constants_reported():
    spec = st.prepare([r"m g h", r"g h m"], constants=["g"])
    assert spec.constants == ["g"] and "_g" in spec.lines[0].nspire


@pytest.mark.parametrize(("lines", "latex", "match"), [
    (["x"], True, "at least two"),
    (["x", "  "], False, "Line 2 is empty"),
    (["a:=5", "a"], False, "stores or deletes"),
    (["Define f(x)=x^2", "f(2)"], False, "stores or deletes"),
    ([r"a:=5", r"a"], True, "Line 1"),
    ([r"y'=y", r"y=e^x"], True, "differential equation"),
    ([r"x^2=4 \\ x+1", r"x=2"], True, "mixes equations and expressions"),
    ([r"x+1 \lor x", r"x"], True, "'or' joins equations"),
    ([r"\foo x", r"x"], True, "Line 1"),
    (["x", "x"], False, None),
])
def test_bad_input_refused_naming_the_line(lines, latex, match):
    if match is None:
        st.prepare(lines, latex=latex)
        return
    with pytest.raises(ValueError, match=match):
        st.prepare(lines, latex=latex)


def test_bad_var_and_too_many_lines():
    with pytest.raises(ValueError, match="var"):
        st.prepare(["x=1", "x=1"], latex=False, var="x)")
    with pytest.raises(ValueError, match="At most"):
        st.prepare(["x"] * (st.MAX_LINES + 1), latex=False)


def test_equation_without_variable_needs_var():
    with pytest.raises(ValueError, match="give var"):
        st.prepare(["2=2", "1=1"], latex=False)


# ------------------------------------------------------------ verdicts

def test_expression_verdicts():
    spec = st.prepare(["(x+1)^2", "x^2+2x+1", "x^2+1", "sin(x)^2+cos(x)^2+x^2", "x^2+1.0000001"], latex=False)
    c = st.commands(spec)
    steps = st.judge(spec, {c[0]: ok("0"), c[1]: ok("2*x"), c[2]: ok("sin(x)^2+cos(x)^2−1"),
                            c[3]: ok("−1.ᴇ−7")})
    assert [s.verdict for s in steps] == ["ok", "broken", "undecided", "undecided"]
    assert steps[1].residual == "2*x" and "2*x" in steps[1].reason and steps[1].cas == ["2*x"]
    assert steps[1].check == [c[1]] and (steps[1].from_line, steps[1].to_line) == (2, 3)
    assert steps[2].variables == ["x"]  # for a later numeric spot check


@pytest.mark.parametrize(("residual", "decided"), [
    ("2*x", True), ("−3", True), ("1/(x*(x+1))", True), ("x^2−y^(−2)", True), ("π", True), ("2*_m", True),
    ("√(2)−1", False), ("ln(x)", False), ("ℯ^x", False), ("x^(1/2)", False), ("2^x", False),
    ("|x|−x", False), ("0.5", False), ("undef", False), ("x!", False),
])
def test_residual_is_decided(residual, decided):
    assert st.residual_is_decided(residual) is decided


def test_equation_verdicts():
    spec = st.prepare(["x^2=x", "x=1", "x=1", "x=1"], latex=False)
    replies = {"solve(x^2=x,x)": ok("x=0 or x=1"), "solve(x=1,x)": ok("x=1")}
    steps = st.judge(spec, replies)
    assert [s.verdict for s in steps] == ["broken", "ok", "ok"]
    assert "loses x=0" in steps[0].reason and steps[0].cas == ["x=0 or x=1", "x=1"]


def test_equation_order_and_spacing_dont_matter():
    spec = st.prepare(["x^2=4", "x=2 or x=-2"], latex=False)
    assert verdicts(spec, {"solve(x^2=4,x)": ok("x=−2 or x=2"),
                           "solve(x=2 or x=-2,x)": ok("x=2 or x = −2")}) == ["ok"]


def test_equation_gains_a_solution():
    spec = st.prepare(["√(x)=−1", "x=1"], latex=False)
    steps = st.judge(spec, {"solve(√(x)=−1,x)": ok("false"), "solve(x=1,x)": ok("x=1")})
    assert steps[0].verdict == "broken" and "gains x=1" in steps[0].reason


def test_equation_undecided_when_cas_cant_solve_or_errors():
    spec = st.prepare(["cos(x)=x", "x=0.739085"], latex=False)
    assert verdicts(spec, {"solve(cos(x)=x,x)": ok("solve(cos(x)=x,x)"),
                           "solve(x=0.739085,x)": ok("x=0.739085")}) == ["undecided"]
    steps = st.judge(spec, {"solve(cos(x)=x,x)": err("skipped: No reply"), "solve(x=0.739085,x)": ok("x=0.739085")})
    assert steps[0].verdict == "undecided" and "skipped" in steps[0].reason


def test_or_inside_brackets_not_split():
    assert st._split_or("x=2 or x=3") == ["x=2", "x=3"]
    assert st._split_or("x=when(a or b,1,2)") == ["x=when(aorb,1,2)"]


def test_mixed_step_is_undecided_without_a_request():
    spec = st.prepare(["x^2-4", "x^2-4=0", "x=2 or x=-2"], latex=False)
    assert st.commands(spec) == ["solve(x^2-4=0,x)", "solve(x=2 or x=-2,x)"]
    steps = st.judge(spec, {"solve(x^2-4=0,x)": ok("x=−2 or x=2"), "solve(x=2 or x=-2,x)": ok("x=−2 or x=2")})
    assert [s.kind for s in steps] == ["mixed", "equation"]
    assert [s.verdict for s in steps] == ["undecided", "ok"] and steps[0].check == []


def test_expression_error_is_undecided():
    spec = st.prepare(["1/0", "2"], latex=False)
    steps = st.judge(spec, {st.commands(spec)[0]: err("error 260 (Division by zero)")})
    assert steps[0].verdict == "undecided" and "Division by zero" in steps[0].reason


class Batch:
    def __init__(self, replies):
        self.replies, self.batches = replies, []

    def eval_batch(self, exprs, timeout_s=5.0, raw=False):
        self.batches.append(list(exprs))
        return [{"expr": e, **self.replies[e]} for e in exprs]


def test_run_is_one_batch():
    spec = st.prepare(["2x+3=7", "2x=4", "x=2"], latex=False)
    b = Batch({"solve(2x+3=7,x)": ok("x=2"), "solve(2x=4,x)": ok("x=2"), "solve(x=2,x)": ok("x=2")})
    steps = st.run(b, spec, 10)
    assert len(b.batches) == 1 and [s.verdict for s in steps] == ["ok", "ok"]
    assert set(steps[0].as_dict()) == {"step", "from_line", "to_line", "kind", "verdict", "check", "cas", "reason"}


# ------------------------------------------------------------ numeric spot check

class Spot(Batch):
    """Answers the first batch from replies and spot-check commands from fn(before, after, point),
    which returns the CAS's reply text (or an err() dict)."""

    def __init__(self, replies, fn):
        super().__init__(replies)
        self.fn = fn
        self.last_timeout = None

    def eval_batch(self, exprs, timeout_s=5.0, raw=False):
        if not exprs[0].startswith("approx("):
            return super().eval_batch(exprs, timeout_s, raw)
        self.batches.append(list(exprs))
        out = []
        for e in exprs:
            body, _, at = e[len("approx({"):-1].rpartition("}|") if "}|" in e else (e[len("approx({"):-2], "", "")
            pt = dict(p.split("=") for p in at.split(" and ")) if at else {}
            r = self.fn(pt)
            out.append({"expr": e, **(r if isinstance(r, dict) else ok(r))})
        return out


def three(a: float, b: float) -> str:
    return "{" + ",".join(f"{v:.6g}".replace("-", "−") for v in (a, b, a - b)) + "}"


def test_spot_points_reproducible_and_varied():
    p1 = st.spot_points(["x", "y"], "sin(x)\ny")
    assert p1 == st.spot_points(["x", "y"], "sin(x)\ny") and len(p1) == st.SPOT_POINTS
    assert p1 != st.spot_points(["x", "y"], "cos(x)\ny")
    xs = [float(p["x"]) for p in p1]
    assert all(0.1 <= x <= 3 for x in xs[:6]) and all(x < 0 for x in xs[6:9])
    assert all(p["x"].isdigit() for p in p1[9:11]) and xs[11] > 3
    assert {p["x"] for p in p1} != {p["y"] for p in p1}  # each variable gets its own values
    assert all(abs(x) not in (0, 1) for x in xs)


def test_spot_command():
    assert st.spot_command("ln(x*y)", "ln(x)+ln(y)", {"x": "0.5", "y": "-1.2"}) == \
        "approx({ln(x*y),ln(x)+ln(y),(ln(x*y))-(ln(x)+ln(y))}|x=0.5 and y=-1.2)"
    assert st.spot_command("√(2)", "1.4142", {}) == "approx({√(2),1.4142,(√(2))-(1.4142)})"


@pytest.mark.parametrize(("reply", "want"), [
    ("{1.23457,1.23457,1.ᴇ−13}", [1.23457, 1.23457, 1e-13]),
    ("{−2.,3,−5.}", [-2.0, 3.0, -5.0]),
    ("{undef,1.,undef}", None), ("{1.+2.*𝑖,1.,2.*𝑖}", None), ("{∞,1,∞}", None), ("{a,1,a−1}", None),
    ("{1,2}", None), ("1.5", None),
])
def test_spot_reply_parsing(reply, want):
    got = st._three(reply)
    assert (got[1] if got else None) == (pytest.approx(want) if want else None)


def test_agree_is_relative():
    assert st.agree(1e6, 1e6, 1e-5) and not st.agree(1e6, 1e6 + 1, -1)
    assert st.agree(0, 0, 0) and not st.agree(1e-3, 2e-3, -1e-3)


def test_spot_check_probably_ok():
    import math
    spec = st.prepare(["sin(x)^2+cos(x)^2", "1"], latex=False)
    b = Spot({st.commands(spec)[0]: ok("sin(x)^2+cos(x)^2−1")},
             lambda pt: three(math.sin(float(pt["x"])) ** 2 + math.cos(float(pt["x"])) ** 2, 1))
    steps = st.run(b, spec, 10)
    assert len(b.batches) == 2 and len(b.batches[1]) == st.SPOT_POINTS
    s = steps[0]
    assert s.verdict == "probably_ok" and "probably equal" in s.reason and "isn't a proof" in s.reason
    assert s.spot_check == {"tried": 12, "usable": 12, "agreed": 12, "counterexample": None}
    assert s.as_dict()["spot_check"] == s.spot_check and s.as_dict()["residual"] == "sin(x)^2+cos(x)^2−1"


def test_spot_check_counterexample():
    spec = st.prepare(["√(x^2)", "x"], latex=False)
    b = Spot({st.commands(spec)[0]: ok("|x|−x")}, lambda pt: three(abs(float(pt["x"])), float(pt["x"])))
    s = st.run(b, spec, 10)[0]
    assert s.verdict == "broken"
    ce = s.spot_check["counterexample"]
    first_negative = st.spot_points(["x"], "√(x^2)\nx")[6]["x"]
    assert ce["at"] == f"x={first_negative}" and ce["command"].endswith(f"|x={first_negative})")
    assert ce["before"] == f"{abs(float(first_negative)):.6g}" and ce["difference"] == f"{2 * abs(float(first_negative)):.6g}"
    assert f"at x={first_negative}" in s.reason and "smaller domain" in s.reason
    assert s.spot_check["agreed"] == 9 and s.spot_check["usable"] == 12


def test_spot_check_skips_points_off_the_domain():
    import math
    spec = st.prepare(["ln(x*y)", "ln(x)+ln(y)"], latex=False)

    def fn(pt):
        x, y = float(pt["x"]), float(pt["y"])
        if x <= 0 or y <= 0:
            return err("error 800 (Non-real result)")
        return three(math.log(x * y), math.log(x) + math.log(y))
    s = st.run(Spot({st.commands(spec)[0]: ok("ln(x*y)−ln(x)−ln(y)")}, fn), spec, 10)[0]
    assert s.variables == ["x", "y"]
    assert s.verdict == "probably_ok" and s.spot_check["usable"] == s.spot_check["agreed"] == 9


def test_spot_check_too_few_points_stays_undecided():
    spec = st.prepare(["x!", "Γ(x+1)"], latex=False)
    s = st.run(Spot({st.commands(spec)[0]: ok("x!−Γ(x+1)")},
                    lambda pt: three(6, 6) if pt["x"].isdigit() else ok("{undef,undef,undef}")), spec, 10)[0]
    assert s.verdict == "undecided" and "too few" in s.reason
    assert s.spot_check["usable"] == 2 and s.spot_check["counterexample"] is None


def test_spot_check_without_variables():
    spec = st.prepare(["√(2)", "1.41421356237"], latex=False)
    b = Spot({st.commands(spec)[0]: ok("1.41421356237−√(2)")}, lambda pt: "{1.41421,1.41421,−3.1ᴇ−13}")
    s = st.run(b, spec, 10)[0]
    assert b.batches[1] == ["approx({√(2),1.41421356237,(√(2))-(1.41421356237)})"]
    assert s.verdict == "probably_ok" and s.spot_check["tried"] == 1
    spec = st.prepare(["√(2)", "1.4142"], latex=False)
    s = st.run(Spot({st.commands(spec)[0]: ok("1.4142−√(2)")}, lambda pt: "{1.41421,1.4142,1.35624ᴇ−5}"), spec, 10)[0]
    assert s.verdict == "broken" and "numerically line 1 is 1.41421" in s.reason


def test_spot_check_only_for_undecided_expression_steps():
    spec = st.prepare(["(x+1)^2", "x^2+2x+1", "x^2+1", "ln(ℯ^x)", "x=1", "x=1"], latex=False)
    c = st.commands(spec)
    replies = {c[0]: ok("x=1"), "expand(((x+1)^2)-(x^2+2x+1))": ok("0"),
               "expand((x^2+2x+1)-(x^2+1))": ok("2*x"), "expand((x^2+1)-(ln(ℯ^x)))": ok("x^2−ln(ℯ^x)+1")}
    b = Spot(replies, lambda pt: three(1, 2))
    steps = st.run(b, spec, 10)
    assert [s.verdict for s in steps] == ["ok", "broken", "broken", "undecided", "ok"]
    assert len(b.batches) == 2 and all("ln(ℯ^x)" in e for e in b.batches[1])
    assert [s.spot_check is not None for s in steps] == [False, False, True, False, False]


def test_spot_check_off_or_after_a_timeout():
    spec = st.prepare(["sin(2x)", "2sin(x)cos(x)"], latex=False)
    replies = {st.commands(spec)[0]: ok("sin(2*x)−2*sin(x)*cos(x)")}
    b = Spot(replies, lambda pt: three(1, 1))
    assert st.run(b, spec, 10, spot_check=False)[0].verdict == "undecided" and len(b.batches) == 1
    b = Spot(replies, lambda pt: three(1, 1))
    b.last_timeout = "No reply"  # what eval_batch leaves after a timeout
    s = st.run(b, spec, 10)[0]
    assert s.verdict == "undecided" and s.spot_check is None and len(b.batches) == 1


def test_latex_decimal_comma_mode():
    spec = st.prepare(["1,5x+3=6", r"1{,}5x=3", "x=2"], decimal_comma=True)
    assert [ln.nspire for ln in spec.lines][:2] == ["solve(1.5*x + 3=6,x)", "solve(1.5*x=3,x)"]
    with pytest.raises(ValueError, match="decimal-comma mode"):
        st.prepare(["1,2,3", "x"], decimal_comma=True)
