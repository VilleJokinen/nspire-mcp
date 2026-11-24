"""Text copied from TI-Nspire -> LaTeX (the reverse hotkey and nspire_to_latex), offline.

The inputs are TI-Nspire's own text: what the CAS returns through the bridge
(private-use characters kept), the readable look-alikes the tools show, and
the display characters a copy from a math box may carry (·, ►, ², ...).
"""
import pytest

from nspire_chars import NS_C, NS_E, NS_EXP, NS_I, NS_N
from nspire_latex import LatexError, nspire_to_latex

CASES = [
    # roots, fractions, powers
    ("√(2)/2", r"\frac{\sqrt{2}}{2}"),
    ("−√(3)", r"- \sqrt{3}"),
    ("root(x,3)", r"\sqrt[3]{x}"),
    ("x^3/3+c", r"\frac{x^{3}}{3} + c"),
    ("x²+y³", r"x^{2} + y^{3}"),
    # the CAS's form is kept, never simplified
    ("2*(x+1)", r"2 \left(x + 1\right)"),
    ("2·x+1", "2 x + 1"),
    ("6×7", r"6 \cdot 7"),
    ("8÷2", r"\frac{8}{2}"),
    ("(x+1)*(x−1)", r"\left(x + 1\right) \left(x - 1\right)"),
    # solutions and relations
    ("x=−2 or x=2", r"x = -2 \quad\text{or}\quad x = 2"),
    ("x=1 and y=2", r"x = 1,\quad y = 2"),
    ("x≠2", r"x \neq 2"),
    ("−1≤x<3", r"-1 \leq x < 3"),
    ("true", r"\text{true}"),
    ("false", r"\text{false}"),
    ("undef", r"\text{undef}"),
    # private-use characters, and the readable look-alikes the tools print
    (f"{NS_E}^({NS_I}*π)", r"e^{i \pi}"),
    ("ℯ^(2*x)", "e^{2 x}"),
    ("3+4*𝑖", "3 + 4 i"),
    (f"1.23{NS_EXP}−5", r"1.23 \cdot 10^{-5}"),
    ("1.23ᴇ−5", r"1.23 \cdot 10^{-5}"),
    (f"y={NS_C}1*{NS_E}^(2*x)", r"y = c_{1} e^{2 x}"),
    (f"x=π*{NS_N}1", r"x = \pi n_{1}"),
    ("sin⁻¹(x)", r"\arcsin{\left(x \right)}"),
    # units and ► conversions
    ("5.*_km", r"5.0 \mathrm{km}"),
    ("9.81*_m/_s^2", r"\frac{9.81 \mathrm{m}}{\mathrm{s}^{2}}"),
    ("4.7*_kΩ", r"4.7 \mathrm{k}\Omega"),
    ("25*_°C", r"25 {}^{\circ}\mathrm{C}"),
    ("5.*_km►_m", r"5.0 \mathrm{km} \blacktriangleright \mathrm{m}"),
    ("90*_km/_hr▸_m/_s", r"\frac{90 \mathrm{km}}{\mathrm{h}} \blacktriangleright \frac{\mathrm{m}}{\mathrm{s}}"),
    ("5*_km@>_m", r"5 \mathrm{km} \blacktriangleright \mathrm{m}"),
    # degrees, stores, definitions, the | operator
    ("30°", r"30 {}^{\circ}"),
    ("sin(30°)", r"\sin{\left(30 {}^{\circ} \right)}"),
    ("5→a", r"5 \rightarrow a"),
    ("f(x):=x^2", r"f{\left(x \right)} := x^{2}"),
    ("x^2+1|x=2", r"x^{2} + 1 \mid x = 2"),
    ("x^2|x>0 and x<3", r"x^{2} \mid x > 0,\quad x < 3"),
    # lists and matrices
    ("{1,2,3}", r"\left\{1, 2, 3\right\}"),
    ("{x=1,y=2}", r"\left\{x = 1, y = 2\right\}"),
    ("[[1,2][3,4]]", r"\left[\begin{matrix}1 & 2\\3 & 4\end{matrix}\right]"),
    ("[1,2,3]", r"\left[\begin{matrix}1 & 2 & 3\end{matrix}\right]"),
    ("[[1,2][3,4]]⁻¹", r"\left[\begin{matrix}1 & 2\\3 & 4\end{matrix}\right]^{-1}"),
    # calculus the CAS hands back unevaluated
    ("∫(x^2,x,0,1)", r"\int\limits_{0}^{1} x^{2}\, dx"),
    ("∫(ℯ^(x^2),x)", r"\int e^{x^{2}}\, dx"),
    ("Σ(1/k^2,k,1,∞)", r"\sum_{k=1}^{\infty} \frac{1}{k^{2}}"),
    ("limit(sin(x)/x,x,0)", r"\lim_{x \to 0}\left(\frac{\sin{\left(x \right)}}{x}\right)"),
    ("derivative(f(x),x)", r"\frac{d}{d x} f{\left(x \right)}"),
    ("piecewise(x,x>0,−x,x≤0)", r"\begin{cases} x & x > 0 \\ - x & x \leq 0 \end{cases}"),
    # whitespace and several lines
    ("  x+1 \r\n", "x + 1"),
    ("x+1\n\n2*y", "x + 1\n2 y"),
    ("3 * x", "3 x"),
]


@pytest.mark.parametrize("text,want", CASES)
def test_nspire_to_latex(text, want):
    assert nspire_to_latex(text) == want


@pytest.mark.parametrize("text, match", [
    ("", "no text"),
    (" \n ", "no text"),
    ("x∫y", "Couldn't convert"),
    ("solve(x^2=4,x)", "Couldn't convert"),  # an = inside a call: not a result's form
    ("a►", "both sides"),
    ("→a", "both sides"),
    ("Define f(x)=x^2", "Define"),
    ("x" * 6000, "too long"),
    ("__import__('os')", "Couldn't convert"),
])
def test_refused(text, match):
    with pytest.raises(LatexError, match=match):
        nspire_to_latex(text)


def test_operators_inside_brackets_stay_put():
    # a | or → inside a call or list isn't split at
    assert nspire_to_latex("{1,2}") == r"\left\{1, 2\right\}"
    with pytest.raises(LatexError):
        nspire_to_latex("f(x|x=2)")
