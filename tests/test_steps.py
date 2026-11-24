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
    assert st.commands(spec) == ["simplify(((x+1)^2-1)-(x^2+2x+1-1))", "simplify((x^2+2x+1-1)-(x^2+2x))"]


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
    assert st.commands(spec)[0] == "simplify(((x + 1)^2)-(x^2 + 2*x + 1))"


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
