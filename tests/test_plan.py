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
    (r"\left.\frac{d}{dx}x^3\right|_{x=2}", "auto", "(derivative(x^3,x)|x=2)"),
    (r"\frac{d^2}{dx^2}x^4", "auto", "derivative(x^4,x,2)"),
    (r"\det\begin{pmatrix}1&2\\3&4\end{pmatrix}", "auto", "det([[1,2][3,4]])"),
    (r"2\cdot\{1,2,3\}", "auto", "2*{1,2,3}"),
    (r"x^2=2 \text{ for } x>0", "auto", "solve(x^2=2,x)|x>0"),
    (r"12 \bmod 5", "auto", "mod(12,5)"),
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


# Inputs that used to become a wrong command without any error.
WAS_SILENTLY_WRONG = [
    # evaluation bar: applied after differentiating, not substituted into x^2 first
    (r"\left.\frac{d}{dx}x^2\right|_{x=2}", "(derivative(x^2,x)|x=2)"),
    (r"\frac{d}{dx}x^2 \Big|_{x=2}", "(derivative(x^2,x)|x=2)"),
    (r"x^2 |_{x=3}", "(x^2|x=3)"),
    (r"\left. \frac{x^3}{3} \right|_0^1", "(x^3/3|x=1) - (x^3/3|x=0)"),
    (r"\left. x^2 \right|_{x=0}^{2} + 1", "(x^2|x=2) - (x^2|x=0) + 1"),
    # ... but |x|^2 and \left|x\right|^2 stay absolute values
    (r"|x|^2", "abs(x)^2"),
    (r"\left| x \right|^2", "abs(x)^2"),
    (r"\Big| x \Big|", "abs(x)"),
    # higher derivatives in operator form
    (r"\frac{d^2}{dx^2}\sin x", "derivative(sin(x),x,2)"),
    (r"\frac{d^{3}}{dx^{3}} x^5", "derivative(x^5,x,3)"),
    (r"\frac{\partial^2}{\partial x^2} x^3 y", "derivative(x^3*y,x,2)"),
    (r"\frac{\partial^2}{\partial x \partial y} x^2 y", "derivative(derivative(x^2*y,y),x)"),
    (r"\frac{d}{dx}\frac{d}{dx}\sin x", "derivative(sin(x),x,2)"),
    # \det takes the matrix (or letter) after it
    (r"\det\begin{pmatrix}a&b\\c&d\end{pmatrix}", "det([[a,b][c,d]])"),
    (r"\det A", "det(A)"),
    (r"\det(A)", "det(A)"),
    # conditions: TI-Nspire's "with" operator
    (r"x^2=2 \text{ for } x>0", "solve(x^2=2,x)|x>0"),
    (r"x^2=2, \text{ where } x>0", "solve(x^2=2,x)|x>0"),
    (r"x+y=3, x-y=1 \text{ for } x>0, y>0", "solve(x + y=3 and x - y=1,{x,y})|x>0 and y>0"),
    (r"\sqrt{x^2} \text{ kun } x<0", "√(x^2)|x<0"),
    # accents make a variable of their own, never the plain letter
    (r"\bar{x} = x + 1", "solve(xbar=x + 1,x)"),
    (r"\bar x + \hat y + \tilde z", "xbar + yhat + ztilde"),
    (r"\overline{z}", "zbar"),
    (r"\bar{\alpha}", "alphabar"),
    (r"z \cdot z^{*}", "z*zstar"),
    (r"z^*", "zstar"),
    # functions the parser read as variables
    (r"\arg(1+i)", "angle(1 + 𝑖)"),
    (r"\Re(z)", "real(z)"),
    (r"\Im(z) + \Re(z)", "imag(z) + real(z)"),
    (r"\mathrm{Re}(z)", "real(z)"),
    (r"\operatorname{sgn}(x)", "sign(x)"),
    (r"\coth(x)", "coth(x)"),
    (r"\arcsinh(x)", "arcsinh(x)"),
    (r"12 \bmod 5", "mod(12,5)"),
    (r"2^{10} \bmod 7 + 1", "mod(2^10,7) + 1"),
    (r"x \bmod 3 = 1", "solve(mod(x,3)=1,x)"),
    # a named function of a variable that appears on the right is a definition
    (r"T(t) = 20 + 80e^{-0.1t}", "Define T(t)=20 + 80*ℯ^(-0.1*t)"),
    (r"V(r)=\frac{4}{3}\pi r^3", "Define V(r)=4*π*r^3/3"),
    (r"x(x+1)=6", "solve(x*(x + 1)=6,x)"),  # still a product
    # lists
    (r"\{1,2,3\}", "{1,2,3}"),
    (r"2\cdot\{1,2,3\}", "2*{1,2,3}"),
    (r"\{1,5\}", "{1,5}"),
    (r"\{x\}", "x"),  # one item: just braces
    # other spellings
    (r"x \ne 2", "solve(x≠2,x)"),
    (r"\lvert x \rvert", "abs(x)"),
    (r"x + 1 : 3", "x + 1/3"),  # ':' divides, as in Finnish schools
]


@pytest.mark.parametrize("latex,want", WAS_SILENTLY_WRONG)
def test_was_silently_wrong(latex, want):
    assert cmd(latex) == want


def test_assignment():
    assert cmd(r"x := 5") == "x:=5"
    assert cmd(r"a \coloneqq 2+3") == "a:=2 + 3"
    assert cmd(r"f(x) := x^2") == "Define f(x)=x^2"
    assert cmd(r"x(t) := 3t") == "Define x(t)=3*t"
    p = plan(r"x := 5")
    assert p.action == "define" and p.description == "store x"
    with pytest.raises(LatexError):
        plan(r"a := 1, b := 2")
    with pytest.raises(LatexError):
        plan(r"x+1 := 5")
    with pytest.raises(LatexError):
        plan(r"x := 5", "solve")


@pytest.mark.parametrize("latex", [
    r"2x ) + 7",              # the parser stopped at ')' and dropped '+ 7'
    r"x^2 = 4 ] junk",
    r"(x+1",
    r"x \in \mathbb{R}",      # unknown commands: no longer variables named in, implies, ...
    r"x^2 - 4 = 0 \implies x",
    r"\forall x",
    r"\langle x \rangle",
    r"\cdots",
    r"\Re z",                 # function-like commands need parentheses
    r"\mathrm{Re}\, z",
    r"\operatorname{sign} x",
    r"\max x",
    r"\coth x",
    r"\overline{AB}",         # accent over more than one letter
    r"x(t) = 3t",             # would turn the variable x into a function
    r"x^2 \text{ for all } x",  # a condition that isn't a comparison
    r"x^2 \text{ for }",
    r"a \equiv b \pmod{5}",
    r"\bmod 5",
    r"\frac{d^2}{dx\,dy^2} f",  # orders don't add up
])
def test_refused_instead_of_misread(latex):
    with pytest.raises(LatexError):
        plan(latex)


def test_unknown_command_message_names_it():
    with pytest.raises(LatexError, match=r"\\implies"):
        plan(r"a \implies b")


def test_errors():
    with pytest.raises(LatexError):
        plan(r"x \pm 1")
    with pytest.raises(LatexError):
        plan(r"x^2", "nonsense")
    with pytest.raises(LatexError):
        plan(r"\frac{1}{")
    with pytest.raises(LatexError):
        plan(r"\begin{pmatrix}1&2\end{pmatrix}+1")  # matrix + scalar
