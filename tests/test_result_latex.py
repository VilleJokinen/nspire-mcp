"""CAS result text -> LaTeX, offline."""
import pytest

from nspire_chars import NS_C, NS_E, NS_EXP, NS_I, to_cas_chars
from nspire_latex import result_to_latex

CASES = [
    # the CAS's form is kept, not re-simplified by SymPy
    ("2*(x+1)", r"2 \left(x + 1\right)"),
    ("−(x−1)", r"- \left(x - 1\right)"),
    ("(x−3)*(x−2)*(x−1)", r"\left(x - 3\right) \left(x - 2\right) \left(x - 1\right)"),
    ("−x+1", "- x + 1"),
    ("x^2+2*x+1", r"x^{2} + 2 x + 1"),
    ("a−(b−c)", r"a - \left(b - c\right)"),
    ("−(x+1)/2", r"- \frac{x + 1}{2}"),
    ("√(8)", r"\sqrt{8}"),
    # unevaluated CAS calls are shown, not recomputed
    ("limit(sin(1/x),x,0)", r"\operatorname{limit}{\left(\sin{\left(\frac{1}{x} \right)},x,0 \right)}"),
    ("derivative(f(x),x)", r"\operatorname{derivative}{\left(f{\left(x \right)},x \right)}"),
    ("nCr(n,2)", r"{\binom{n}{2}}"),
    ("max(1,5)", r"\max\left(1, 5\right)"),
    # plain results
    ("x=2 or x=3", r"x = 2 \quad\text{or}\quad x = 3"),
    ("x=2 and y=1", r"x = 2,\quad y = 1"),
    ("−2<x<4", "-2 < x < 4"),
    ("x≤3", r"x \leq 3"),
    ("x^3/3", r"\frac{x^{3}}{3}"),
    ("1/3", r"\frac{1}{3}"),
    ("−1/3", r"- \frac{1}{3}"),
    ("4/3", r"\frac{4}{3}"),
    ("x+1/2", r"x + \frac{1}{2}"),
    ("2*x*cos(x^2)", r"2 x \cos{\left(x^{2} \right)}"),
    ("n*(n+1)/2", r"\frac{n \left(n + 1\right)}{2}"),
    ("√(2)/2", r"\frac{\sqrt{2}}{2}"),
    ("−√(2)/2", r"- \frac{\sqrt{2}}{2}"),
    ("π/4", r"\frac{\pi}{4}"),
    ("2*π/3", r"\frac{2 \pi}{3}"),
    ("−2*x", "- 2 x"),
    ("x*(−1)", "- x"),
    ("3*x^2*ln(x)+x^2", r"3 x^{2} \ln{\left(x \right)} + x^{2}"),
    ("root(x,3)", r"\sqrt[3]{x}"),
    ("tan⁻¹(x)", r"\arctan{\left(x \right)}"),
    ("abs(x)", r"\left|{x}\right|"),
    ("5!", "5!"),
    ("{1,2,3}", r"\left\{1, 2, 3\right\}"),
    ("[[1,2][3,4]]", r"\left[\begin{matrix}1 & 2\\3 & 4\end{matrix}\right]"),
    ("1.41421356237", "1.41421356237"),
    # TI-Nspire's private-use characters
    (f"1.23{NS_EXP}−5", r"1.23 \cdot 10^{-5}"),
    (f"{NS_E}^x", "e^{x}"),
    (f"x={NS_I} or x=−{NS_I}", r"x = i \quad\text{or}\quad x = - i"),
    (f"y={NS_C}1*{NS_E}^(2*x)", r"y = c_{1} e^{2 x}"),
    (to_cas_chars("y=c1*ℯ^(2*x)"), r"y = c_{1} e^{2 x}"),  # readable text converts back
    # words
    ("true", r"\text{true (holds for all values)}"),
    ("false", r"\text{no solution}"),
    ("undef", r"\text{undefined}"),
]


@pytest.mark.parametrize("text,want", CASES)
def test_result_to_latex(text, want):
    assert result_to_latex(text) == want


@pytest.mark.parametrize("text", ["", "Done", "piecewise(x,x>0,-x,x<=0)", "∫(x,x)"])
def test_not_converted(text):
    assert result_to_latex(text) is None


@pytest.mark.parametrize("text", [
    "__import__('os').system('echo hi')",
    "x.__class__",
    "().__class__.__bases__",
    "Symbol('x')",
    "x.conjugate()",
])
def test_unsafe_input_is_not_evaluated(text):
    assert result_to_latex(text) is None
