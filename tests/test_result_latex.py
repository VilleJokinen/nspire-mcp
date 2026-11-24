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
    # unevaluated CAS calls are shown as math, not recomputed
    ("limit(sin(1/x),x,0)", r"\lim_{x \to 0} \sin{\left(\frac{1}{x} \right)}"),
    ("limit(1/x,x,0,1)", r"\lim_{x \to 0^+}\left(\frac{1}{x}\right)"),
    ("limit(1/x,x,0,-1)", r"\lim_{x \to 0^-}\left(\frac{1}{x}\right)"),
    ("limit(f(x),x,∞)", r"\lim_{x \to \infty} f{\left(x \right)}"),
    ("derivative(f(x),x)", r"\frac{d}{d x} f{\left(x \right)}"),
    ("derivative(f(x),x,2)", r"\frac{d^{2}}{d x^{2}} f{\left(x \right)}"),
    ("∫(x,x)", r"\int x\, dx"),
    ("∫(x^2,x,0,1)", r"\int\limits_{0}^{1} x^{2}\, dx"),
    ("Σ(1/k^2,k,1,∞)", r"\sum_{k=1}^{\infty} \frac{1}{k^{2}}"),
    ("Π(k,k,1,n)", r"\prod_{k=1}^{n} k"),
    # piecewise(expr1,cond1,expr2,cond2,...[,otherwise])
    ("piecewise(x,x>0,−x,x≤0)", r"\begin{cases} x & x > 0 \\ - x & x \leq 0 \end{cases}"),
    ("piecewise(1,0<x and x<1,0)", r"\begin{cases} 1 & 0 < x \text{ and } x < 1 \\ 0 & \text{otherwise} \end{cases}"),
    ("y=2*piecewise(x,x>0,0)", r"y = 2 \begin{cases} x & x > 0 \\ 0 & \text{otherwise} \end{cases}"),
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
    ("[[1][2]]", r"\left[\begin{matrix}1\\2\end{matrix}\right]"),
    ("[1,2,3]", r"\left[\begin{matrix}1 & 2 & 3\end{matrix}\right]"),  # a row vector is a 1×n matrix
    ("{[1,2],[3,4]}",
     r"\left\{\left[\begin{matrix}1 & 2\end{matrix}\right], \left[\begin{matrix}3 & 4\end{matrix}\right]\right\}"),
    # units
    ("5.03*_m", r"5.03 \mathrm{m}"),
    ("10*_m/_s^2", r"\frac{10 \mathrm{m}}{\mathrm{s}^{2}}"),
    # units whose names aren't ASCII or aren't the symbol, and constants
    ("9.4*_V", r"9.4 \mathrm{V}"),
    ("1000.*_µA", r"1000.0 \mu\mathrm{A}"),
    ("4.7*_kΩ", r"4.7 \mathrm{k}\Omega"),
    ("50*_Ω", r"50 \Omega"),
    ("298.15*_°K", r"298.15 \mathrm{K}"),
    ("−272.15*_°C", r"- 272.15 {}^{\circ}\mathrm{C}"),
    ("54.*(_km/_hr)", r"\frac{54.0 \mathrm{km}}{\mathrm{h}}"),
    (f"1.602176634{NS_EXP}−19*_coul", r"1.602176634 \cdot 10^{-19} \mathrm{C}"),
    ("2*_henry", r"2 \mathrm{H}"),
    ("10000000000.*_Å", r"10000000000.0 \mathring{\mathrm{A}}"),
    ("m*_g", r"m g"),
    ("_q^2/(4*π*_ε0*r^2)", r"\frac{e^{2}}{4 \pi \varepsilon_{0} r^{2}}"),
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


@pytest.mark.parametrize("text", ["", "Done", "∫", "x∫y", "piecewise(", "piecewise()", "piecewise(x,x>0"])
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
