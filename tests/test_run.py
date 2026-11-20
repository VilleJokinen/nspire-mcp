"""run_plan / format_answer / solve_latex with a fake bridge."""
import nspire_latex
from nspire_chars import NS_E, readable, to_cas_chars
from nspire_latex import format_answer, plan, run_plan, solve_latex


class FakeBridge:
    """Answers eval_one from a dict, like Bridge with raw=True; records session use."""

    def __init__(self, answers: dict[str, str]):
        self.answers = answers
        self.calls: list[str] = []
        self.in_session = False

    def session(self, work):
        assert not self.in_session, "nested session"
        self.in_session = True
        try:
            return work()
        finally:
            self.in_session = False

    def eval_one(self, expr, timeout_s, raw=False):
        assert self.in_session, "bridge used outside a session"
        self.calls.append(readable(expr))
        return self.answers[readable(expr)]


def test_run_then_format():
    b = FakeBridge({"√(8)": "2*√(2)", "approx(√(8))": "2.82842712475"})
    p = plan(r"\sqrt{8}")
    run = b.session(lambda: run_plan(b, p))
    assert b.calls == ["√(8)", "approx(√(8))"]
    assert run.raw == "2*√(2)" and run.approx_raw == "2.82842712475"
    out = format_answer(run)
    assert out == {"action": "evaluate", "nspire": "√(8)", "result": "2*√(2)",
                   "result_latex": r"2 \sqrt{2}", "approx": "2.82842712475",
                   "approx_latex": "2.82842712475"}


def test_no_latex_conversion_inside_the_session(monkeypatch):
    b = FakeBridge({"solve(x^2 - 4=0,x)": "x=−2 or x=2"})
    real = nspire_latex.result_to_latex

    def checked(text):
        assert not b.in_session, "result_to_latex ran while the clipboard was held"
        return real(text)
    monkeypatch.setattr(nspire_latex, "result_to_latex", checked)
    out = solve_latex(b, r"x^2-4=0")
    assert out["result_latex"] == r"x = -2 \quad\text{or}\quad x = 2"
    assert "approx" not in out


def test_no_approx_for_plain_numbers_and_defines():
    b = FakeBridge({"2 + 2": "4", "Define f(x)=x^2": ""})
    out = solve_latex(b, "2+2")
    assert out["result"] == "4" and "approx" not in out
    out = solve_latex(b, "f(x)=x^2")
    assert out["result"] == "Done" and out["result_latex"] is None
    assert b.calls == ["2 + 2", "Define f(x)=x^2"]


def test_private_use_characters_reach_the_cas():
    raw_cmd = to_cas_chars("ℯ^(𝑖*π)")
    b = FakeBridge({readable(raw_cmd): "−1"})
    p = plan(r"e^{i\pi}")
    assert p.command == raw_cmd and NS_E in p.command
    assert format_answer(b.session(lambda: run_plan(b, p)))["result"] == "−1"
