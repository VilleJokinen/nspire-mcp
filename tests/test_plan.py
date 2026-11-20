"""LaTeX -> TI-Nspire command, offline (no bridge, no Windows)."""
import pytest

from nspire_chars import readable
from nspire_latex import LatexError, _decimal_commas, plan


def cmd(latex: str, action: str = "auto", **kw) -> str:
    # readable() shows the CAS's private-use e and i as ℯ and 𝑖
    return readable(plan(latex, action, **kw).command)


# The translations behind selftest.py's LATEX_CASES (those also run on the real CAS).
SELFTEST = [
    (r"x^2-5x+6=0", "auto", "solve(x^2 - 5*x + 6=0,x)"),
    (r"\int_0^1 x^2\,dx", "auto", "∫(x^2,x,0,1)"),
    (r"\lim_{x\to 0}\frac{\sin x}{x}", "auto", "limit(sin(x)/x,x,0)"),
    (r"\sum_{k=1}^{n} k", "auto", "Σ(k,k,1,n)"),
    (r"\frac{d}{dx}\sin(x^2)", "auto", "derivative(sin(x^2),x)"),
    (r"\begin{cases} x+y=3 \\ x-y=1 \end{cases}", "auto", "solve(x + y=3 and x - y=1,{x,y})"),
    (r"\begin{vmatrix}1&2\\3&4\end{vmatrix}", "auto", "det([[1,2][3,4]])"),
    (r"y''+y=0,\ y(0)=1,\ y'(0)=0", "auto", "deSolve(y'' + y=0 and y(0)=1 and y'(0)=0,x,y)"),
    (r"|x-1| < 3", "auto", "solve(abs(x - 1)<3,x)"),
    (r"x^3-6x^2+11x-6", "factor", "factor(x^3 - 6*x^2 + 11*x - 6)"),
]


@pytest.mark.parametrize("latex,action,want", SELFTEST)
def test_selftest_translations(latex, action, want):
    assert cmd(latex, action) == want


# SymPy must not do the arithmetic: the CAS gets what was written.
UNEVALUATED = [
    (r"0.1+0.2", "0.1 + 0.2"),
    (r"\frac{1}{3}+0.5", "1/3 + 0.5"),
    (r"e^{i\pi}", "ℯ^(𝑖*π)"),
    (r"\sqrt{8}", "√(8)"),
    (r"2x+3x", "2*x + 3*x"),
    (r"2\cdot 3\cdot x", "2*3*x"),
    (r"\frac{2}{4}", "2/4"),
    (r"2^{10}", "2^10"),
    (r"x^2\cdot x^3", "x^2*x^3"),
    (r"\sqrt{2}\sqrt{2}", "√(2)*√(2)"),
    (r"-(x-1)", "-(x - 1)"),
    (r"2(x+1)", "2*(x + 1)"),
    (r"\left(\frac{1}{2}\right)^2", "(1/2)^2"),
    (r"30^\circ", "30*π/180"),
    (r"\binom{5}{2}", "nCr(5,2)"),
    (r"\ln(e^2)", "ln(ℯ^(2))"),
    (r"x=x", "solve(x=x,x)"),  # not folded to True
]


@pytest.mark.parametrize("latex,want", UNEVALUATED)
def test_no_arithmetic_in_sympy(latex, want):
    assert cmd(latex) == want


FLOATS = [
    (r"0.000001", "0.000001"),
    (r"3.14159 x", "3.14159*x"),
    (r"3.14159265358979323846", "3.14159265358979323846"),
    (r"1.50", "1.5"),
    (r"-0.5x", "-0.5*x"),
    (r"x-0.2", "x - 0.2"),
]


@pytest.mark.parametrize("latex,want", FLOATS)
def test_floats_as_written(latex, want):
    assert cmd(latex) == want


# Parser artifacts are cleaned up, and only the parentheses TI-Nspire needs are added.
LAYOUT = [
    (r"x-5x", "x - 5*x"),               # not x - 1*5*x
    (r"\int x e^x dx", "∫(x*ℯ^(x),x)"),  # not ∫(1*x*...)
    (r"(a+b)+c", "a + b + c"),
    (r"a-b-c", "a - b - c"),
    (r"x-(y+z)", "x - (y + z)"),
    (r"x-y^2", "x - y^2"),
    (r"2-3i", "2 - 3*𝑖"),
    (r"-x^2", "-x^2"),
    (r"(-2)^2", "(-2)^2"),
    (r"(-x)^2", "(-x)^2"),
    (r"(x+1)^2", "(x + 1)^2"),
    (r"2^{x+1}", "2^(x + 1)"),
    (r"x^{y^z}", "x^(y^z)"),
    (r"e^{-x^2}", "ℯ^(-x^2)"),
    (r"x\cdot(-3)", "x*(-3)"),
    (r"a/b/c", "a/(b*c)"),
    (r"\frac{a}{bc}", "a/(b*c)"),
    (r"\frac{2x}{3y}", "2*x/(3*y)"),
    (r"1/(2x)", "1/(2*x)"),
    (r"\frac{x^2-1}{x-1}", "(x^2 - 1)/(x - 1)"),
    (r"\frac{1}{x+1}", "1/(x + 1)"),
    (r"\frac{1}{x^2}", "1/x^2"),
    (r"\frac{3}{-2}", "3/(-2)"),
    (r"-\frac{1}{2}", "-1/2"),
    (r"\frac{1}{2}x", "x/2"),
    (r"x^{-1}", "1/x"),
    (r"\frac{1}{\sqrt{x}}", "1/√(x)"),
    (r"\sqrt[3]{x}", "root(x,3)"),
    (r"x^{1/3}", "root(x,3)"),
    (r"\sin^2 x+\cos^2 x", "sin(x)^2 + cos(x)^2"),
    (r"x(x+1)", "x*(x + 1)"),
    (r"5!", "5!"),
    (r"\log x", "log(x)"),
    (r"\lim_{x\to 0^+} \ln x", "limit(ln(x),x,0,1)"),
    (r"\int_{0}^{\infty} e^{-x} dx", "∫(ℯ^(-x),x,0,∞)"),
    (r"\prod_{k=1}^{5} k", "Π(k,k,1,5)"),
    (r"e", "ℯ"),
    (r"\exp(x)", "exp(x)"),
]


@pytest.mark.parametrize("latex,want", LAYOUT)
def test_layout(latex, want):
    assert cmd(latex) == want


MATRICES = [
    (r"\begin{pmatrix}1&2\\3&4\end{pmatrix}^{-1}", "[[1,2][3,4]]^(-1)"),
    (r"\begin{pmatrix}1&2\\3&4\end{pmatrix}\begin{pmatrix}5\\6\end{pmatrix}", "[[1,2][3,4]]*[[5][6]]"),
    (r"2\begin{pmatrix}1&2\\3&4\end{pmatrix}", "2*[[1,2][3,4]]"),
    (r"\begin{pmatrix}1&2\\3&4\end{pmatrix}-\begin{pmatrix}1&0\\0&1\end{pmatrix}", "[[1,2][3,4]] - [[1,0][0,1]]"),
    (r"\begin{pmatrix}e&-x\\x-1&0\end{pmatrix}", "[[ℯ,-x][x - 1,0]]"),
]


@pytest.mark.parametrize("latex,want", MATRICES)
def test_matrices_stay_symbolic(latex, want):
    assert cmd(latex) == want


ODES = [
    (r"x' = 3", "deSolve(x'=3,t,x)"),  # was deSolve(x'=3,x,x)
    (r"x'(t)=x(t)", "deSolve(x'=x,t,x)"),  # was deSolve(x'(t)=x,t,x)
    (r"x''(t) = -x(t)", "deSolve(x''=-x,t,x)"),
    (r"x'(0)=1, x'' = -x, x(0)=0", "deSolve(x'(0)=1 and x''=-x and x(0)=0,t,x)"),
    (r"x' = t x", "deSolve(x'=t*x,t,x)"),
    (r"y'(x) = y(x)", "deSolve(y'=y,x,y)"),
    (r"y'(t) = y(t),\ y'(0) = 1", "deSolve(y'=y and y'(0)=1,t,y)"),
    (r"y'=2y,\ y(0)=3", "deSolve(y'=2*y and y(0)=3,x,y)"),
    (r"\frac{dy}{dx}=2y, y(0)=3", "deSolve(y'=2*y and y(0)=3,x,y)"),
    (r"\frac{d^2y}{dt^2}=-y", "deSolve(y''=-y,t,y)"),
    (r"\dot x = -x", "deSolve(x'=-x,t,x)"),
    (r"y'' + 4y = 0", "deSolve(y'' + 4*y=0,x,y)"),
    (r"y' = t y", "deSolve(y'=t*y,t,y)"),
]


@pytest.mark.parametrize("latex,want", ODES)
def test_odes(latex, want):
    assert cmd(latex) == want


DECIMAL_COMMA = [
    (r"\frac{1,5}{2}", "1.5/2"),
    (r"1,5+2", "1.5 + 2"),
    (r"0,1+0,2", "0.1 + 0.2"),
    (r"3,14", "3.14"),
    (r"x=1,5", "solve(x=1.5,x)"),
    (r"x^{0,5}", "x^(0.5)"),
    (r"\sqrt{2,25}", "√(2.25)"),
    (r"\frac{1}{2}+1,5", "1/2 + 1.5"),
    (r"\begin{pmatrix}1,5&2\\3&4\end{pmatrix}", "[[1.5,2][3,4]]"),
    # a comma between arguments stays one
    (r"f(1,5)", "f(1,5)"),
    (r"\gcd(12,18)", "gcd(12,18)"),
    (r"\max(1,5)", "max(1,5)"),
    (r"\frac{\max(1,5)}{2}", "max(1,5)/2"),
    (r"1,2,3", "{1,2,3}"),
]


@pytest.mark.parametrize("latex,want", DECIMAL_COMMA)
def test_decimal_comma(latex, want):
    assert cmd(latex) == want


@pytest.mark.parametrize("src,want", [
    ("(1,5)", "(1,5)"),
    ("[1,5]", "[1,5]"),
    ("(1,5]", "(1,5]"),
    (r"\{1,5\}", r"\{1,5\}"),
    (r"\frac{(1,5)}{2}", r"\frac{(1,5)}{2}"),
    (r"\frac{1,5}{2}", r"\frac{1.5}{2}"),
    ("1,500,000", "1,500,000"),
    ("1.2,5", "1.2,5"),
    (r"a \\ 1,5", r"a \\ 1.5"),
])
def test_decimal_comma_contexts(src, want):
    assert _decimal_commas(src) == want


def test_define_and_derivatives():
    assert cmd(r"f(x)=x^2") == "Define f(x)=x^2"
    assert cmd(r"f'(x)") == "derivative(f(x),x)"
    assert cmd(r"f''(2)") == "(derivative(f(x),x,2)|x=2)"


def test_actions():
    assert cmd(r"x^2 - 2 = 0", "nsolve") == "nSolve(x^2 - 2=0,x)"
    assert cmd(r"x^2-4", "solve") == "solve(x^2 - 4=0,x)"
    assert cmd(r"x+y=3, x-y=1") == "solve(x + y=3 and x - y=1,{x,y})"
    assert cmd(r"\sin x", "taylor", order=5) == "taylor(sin(x),x,5)"
    assert cmd(r"t^2", "diff") == "derivative(t^2,t)"


def test_errors():
    with pytest.raises(LatexError):
        plan(r"x \pm 1")
    with pytest.raises(LatexError):
        plan(r"x^2", "nonsense")
    with pytest.raises(LatexError):
        plan(r"\frac{1}{")
    with pytest.raises(LatexError):
        plan(r"\begin{pmatrix}1&2\end{pmatrix}+1")  # matrix + scalar
