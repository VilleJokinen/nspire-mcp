"""LaTeX -> TI-Nspire command, offline (no bridge, no Windows)."""
import pytest

from nspire_chars import readable
from nspire_latex import LatexError, _decimal_commas, plan, unit_command


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
    (r"\begin{pmatrix}1\\2\\3\end{pmatrix}\cdot\begin{pmatrix}4\\5\\6\end{pmatrix}", "auto",
     "dotP([[1][2][3]],[[4][5][6]])"),
    (r"\begin{pmatrix}1&0&0\end{pmatrix}\times\begin{pmatrix}0&1&0\end{pmatrix}", "auto", "crossP([[1,0,0]],[[0,1,0]])"),
    (r"\frac{1\,\mathrm{km}}{1\,\mathrm{m}}", "auto", "_km/_m"),
    (r"\frac{2\,\mathrm{kg}}{500\,\mathrm{g}}", "auto", "2*_kg/(500*_gm)"),
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


# Subscripted names: letters and digits only, each written subscript its own name.
SUBSCRIPTS = [
    (r"a_1", "a1"),
    (r"x_{12}", "x12"),
    (r"x_{n1}", "xn1"),
    (r"x_n", "xn"),
    (r"x_{ab}", "xab"),
    (r"x_{ba}", "xba"),          # not reordered to xab by the parser
    (r"\alpha_1", "α1"),
    (r"\alpha_{12}", "α12"),
    (r"x_{\alpha}", "xalpha"),
    (r"\mathbf{x}_1", "x1"),
    (r"x_12", "x1*2"),           # as LaTeX renders it: x₁2
    (r"x_1^2", "x1^2"),
    (r"a_1(x+1)", "a1*(x + 1)"),  # a product, like x(x+1)
    (r"a_1 + a_2 = 3", "solve(a1 + a2=3,a1)"),
    # subscripts of \int, \sum, \lim, \log and evaluation bars are limits, not names
    (r"\int_0^1 x\,dx", "∫(x,x,0,1)"),
    (r"\log_2 8", "log(8,2)"),
    (r"\left.x^2\right|_{x=1,5}", "(x^2|x=1.5)"),
]


@pytest.mark.parametrize("latex,want", SUBSCRIPTS)
def test_subscripts(latex, want):
    assert cmd(latex) == want


@pytest.mark.parametrize("latex,what", [
    (r"x_{n+1} - x_{n-1}", r"'\+'"),  # was xn1 - xn11, and x_{n+1} collided with x_{n1}
    (r"x_{n-1}", "'-'"),
    (r"x_{1,2}", "a comma"),           # was x120000000000000 (decimal comma 1.2, then cleaned)
    (r"x_{2k}^2 + x_{k/2}", "'/'"),
    (r"a_{\infty}", r"'\\infty'"),
    (r"x_{}", "empty"),
])
def test_subscripts_refused(latex, what):
    with pytest.raises(LatexError, match=what):
        plan(latex)


# A script without braces is one character, as LaTeX renders it.
SCRIPTS = [
    (r"\sum_{k=1}^5 2k", "Σ(2*k,k,1,5)"),     # was Σ(k,k,1,52)
    (r"\int_0^1 2\,dx", "∫(2,x,0,1)"),        # was ∫(1,x,0,12)
    (r"x^12", "x^1*2"),
    (r"x^{12}", "x^12"),
    (r"10^8", "10^8"),
]


@pytest.mark.parametrize("latex,want", SCRIPTS)
def test_one_character_scripts(latex, want):
    assert cmd(latex) == want


def test_greek_letter_calls():
    # \Gamma(5) was Γ*5 and \Phi(x) Φ*x: a function to the reader, a product to the parser
    for latex in (r"\Gamma(5)", r"\Phi(x)", r"\phi(x)", r"\phi_1(x)", r"\zeta(2)", r"\Gamma(x,2)",
                  r"\alpha (x+1)", r"\alpha\left(x+1\right)"):
        with pytest.raises(LatexError, match=r"\\cdot"):
            plan(latex)
    assert cmd(r"\phi(x)=x^2") == "Define φ(x)=x^2"   # a definition still works
    assert cmd(r"\psi(t) := 3t") == "Define ψ(t)=3*t"
    assert cmd(r"\alpha\cdot(x+1)") == "α*(x + 1)"
    assert cmd(r"\Gamma x") == "Γ*x"
    assert cmd(r"2\pi(r+1)") == "2*π*(r + 1)"           # π is a constant: a product (was pi*(r + 1))


VECTORS = [
    (r"\vec{u}\cdot\vec{v}", "dotP(u,v)"),       # was u*v
    (r"\vec{u}\times\vec{v}", "crossP(u,v)"),    # was u*v
    (r"\vec u\cdot\vec v", "dotP(u,v)"),
    (r"\vec{\omega}\times\vec{r}", "crossP(ω,r)"),
    (r"\begin{pmatrix}1\\2\end{pmatrix}\cdot\begin{pmatrix}3\\4\end{pmatrix}", "dotP([[1][2]],[[3][4]])"),
    (r"\begin{pmatrix}1\\2\\3\end{pmatrix}\times\begin{pmatrix}3\\4\\5\end{pmatrix}",
     "crossP([[1][2][3]],[[3][4][5]])"),
    (r"\begin{pmatrix}1&2&3\end{pmatrix}\cdot\begin{pmatrix}4&5&6\end{pmatrix}", "dotP([[1,2,3]],[[4,5,6]])"),
    (r"\vec{u}\cdot\begin{pmatrix}1\\2\end{pmatrix}", "dotP(u,[[1][2]])"),
    (r"\vec{u}\cdot(\vec{v}\times\vec{w})", "dotP(u,crossP(v,w))"),
    (r"(\vec{u}\times\vec{v})\cdot\vec{w}", "dotP(crossP(u,v),w)"),
    (r"2\vec{u}\cdot\vec{v}", "2*dotP(u,v)"),
    (r"-\vec{u}\cdot\vec{v} + 1", "-dotP(u,v) + 1"),
    (r"2\vec{u}+\vec{v}", "2*u + v"),
    (r"\lambda\vec{u}", "λ*u"),
    (r"\frac{\vec{u}}{2}", "u/2"),
    # a matrix times a vector stays a matrix product
    (r"\begin{pmatrix}1&2\\3&4\end{pmatrix}\cdot\begin{pmatrix}3\\4\end{pmatrix}", "[[1,2][3,4]]*[[3][4]]"),
    (r"\begin{pmatrix}1&2\\3&4\end{pmatrix}\begin{pmatrix}5\\6\end{pmatrix}", "[[1,2][3,4]]*[[5][6]]"),
]


@pytest.mark.parametrize("latex,want", VECTORS)
def test_vectors(latex, want):
    assert cmd(latex) == want


@pytest.mark.parametrize("latex,what", [
    (r"\vec{u}\cdot\vec{v}\times\vec{w}", "parentheses"),   # which product first changes the answer
    (r"\vec{u}\cdot 2\vec{v}", r"\\cdot or \\times"),
    (r"\vec{u}\vec{v}", r"\\cdot or \\times"),
    (r"\vec{u}\cdot(\vec{v}+\vec{w})", r"\\cdot or \\times"),
    (r"\vec{u}^2", "power"),
    (r"\vec{u}\cdot\vec{v}^2", "exponent"),
    (r"\vec{v}\cdot\vec{v}=v^2", "same variable"),
    (r"\vec{AB}", "one letter"),
    (r"\begin{pmatrix}1&2\end{pmatrix}\cdot\begin{pmatrix}3\\4\end{pmatrix}", "row"),
    (r"\begin{pmatrix}1\\2\end{pmatrix}\cdot\begin{pmatrix}3\\4\\5\end{pmatrix}", "different lengths"),
    (r"\begin{pmatrix}1\\2\\3\\4\end{pmatrix}\times\begin{pmatrix}3\\4\\5\\6\end{pmatrix}", "length 2 or 3"),
])
def test_vectors_refused(latex, what):
    with pytest.raises(LatexError, match=what):
        plan(latex)


BOLD_VECTORS = [
    (r"\mathbf{u}\cdot\mathbf{v}", "dotP(u,v)"),            # was u*v
    (r"\mathbf{u}\times\mathbf{v}", "crossP(u,v)"),
    (r"\boldsymbol{u}\cdot\bm{v}", "dotP(u,v)"),
    (r"\mathbf u\cdot\mathbf v", "dotP(u,v)"),
    (r"\mathbf{u}\cdot\vec{v}", "dotP(u,v)"),
    (r"\mathbf{u}\cdot(\mathbf{v}\times\mathbf{w})", "dotP(u,crossP(v,w))"),
    (r"2\mathbf{u}\cdot\mathbf{v}", "2*dotP(u,v)"),
    (r"\mathbf{u}\cdot\begin{pmatrix}1\\2\end{pmatrix}", "dotP(u,[[1][2]])"),
    # bold that isn't next to \cdot or \times stays styling
    (r"\mathbf{F}=m\mathbf{a}", "solve(F=m*a,a)"),
    (r"\mathbf{x}+1", "x + 1"),
]


@pytest.mark.parametrize("latex,want", BOLD_VECTORS)
def test_bold_vectors(latex, want):
    assert cmd(latex) == want


@pytest.mark.parametrize("latex,what", [
    (r"\mathbf{v}\cdot\mathbf{v}=v^2", "same variable"),
    (r"\mathbf{u}\cdot\mathbf{v}+v", "same variable"),
    (r"\mathbf{u}\cdot\mathbf{v}\mathbf{u}\mathbf{v}", r"\\cdot or \\times"),
    (r"\mathbf{u}\cdot\mathbf{v}^2", "exponent"),
    (r"\mathbf{v}_1\cdot\mathbf{v}_2", "index or exponent"),
])
def test_bold_vectors_refused(latex, what):
    with pytest.raises(LatexError, match=what):
        plan(latex)


NORMS = [
    (r"|\vec u|", "norm(u)"),                       # was abs(u), which acts on each element
    (r"|\vec{u}|", "norm(u)"),
    (r"\left|\vec u\right|", "norm(u)"),
    (r"\|\vec u\|", "norm(u)"),
    (r"\left\|\vec u\right\|", "norm(u)"),
    (r"\lVert u\rVert", "norm(u)"),
    (r"\left\lVert \vec{u} \right\rVert", "norm(u)"),
    (r"\Vert u\Vert", "norm(u)"),
    (r"\|x+y\|", "norm(x + y)"),
    (r"|\mathbf{u}|\cdot|\mathbf{v}|", "abs(u)*abs(v)"),   # bold only counts next to \cdot or \times
    (r"|\vec u|^2", "norm(u)^2"),
    (r"\frac{\vec u}{|\vec u|}", "u/norm(u)"),
    (r"|\vec u\times\vec v|", "norm(crossP(u,v))"),
    (r"|\begin{pmatrix}3\\4\end{pmatrix}|", "norm([[3][4]])"),
    (r"|\begin{pmatrix}3&4&5\end{pmatrix}|", "norm([[3,4,5]])"),
    (r"\|\begin{pmatrix}3\\4\end{pmatrix}\|", "norm([[3][4]])"),
    (r"|x|", "abs(x)"),                              # scalars keep abs
    (r"|x|+|\vec u|+|y|", "abs(x) + norm(u) + abs(y)"),
    (r"|x-1|\cdot|\vec u|", "abs(x - 1)*norm(u)"),
]


@pytest.mark.parametrize("latex,want", NORMS)
def test_norms(latex, want):
    assert cmd(latex) == want


@pytest.mark.parametrize("latex,what", [
    (r"\|\begin{pmatrix}1&2\\3&4\end{pmatrix}\|", "ambiguous"),
    (r"\| \|", "Nothing between"),
    (r"\lVert x", "pair up"),
    (r"\lVert x \|", "pair up"),
    (r"\|\vec u\|\vec u\|", "pair up"),
])
def test_norms_refused(latex, what):
    with pytest.raises(LatexError, match=what):
        plan(latex)


@pytest.mark.parametrize("latex,want", [
    (r"x=1 \text{ and } y=2", "solve(x=1 and y=2,{x,y})"),
    (r"x=1 \text{ ja } y=2", "solve(x=1 and y=2,{x,y})"),
    (r"x=1 \text{ } y", "solve(x=y,x)"),
    (r"\text{}x", "x"),
    (r"x=1 \text{ . } y", "solve(x=y,x)"),
    (r"x^2=2 \text{ for } x>0", "solve(x^2=2,x)|x>0"),
    (r"x^2=2 \text{ where } x>0", "solve(x^2=2,x)|x>0"),
    (r"\sqrt{x^2} \text{ kun } x<0", "√(x^2)|x<0"),
    (r"5\text{ m}", "5*_m"),
])
def test_text_layout_kept(latex, want):
    assert cmd(latex) == want


@pytest.mark.parametrize("latex", [
    r"x^2 \text{ meters squared}",
    r"\text{area} = 5",
    r"x+1 \text{ and so on}",
    r"\textbf{x}+1",
    r"x \textit{approximately} 3",
    r"x=y \text{ andrew } z",
    r"\mbox{speed} = 3",
])
def test_unknown_text_refused(latex):
    with pytest.raises(LatexError, match=r"text|isn't a unit"):
        plan(latex)


UNITS = [
    (r"5\,\mathrm{m} + 3\,\mathrm{cm}", "5*_m + 3*_cm"),   # was 5*m + 3*cm
    (r"5\mathrm{m}", "5*_m"),
    (r"5\text{ m}", "5*_m"),                               # was 5: the text was dropped
    (r"5\,\text{kg}\cdot 9.81\,\mathrm{m}", "5*_kg*9.81*_m"),
    (r"9.81\,\mathrm{m/s^2}", "9.81*_m/_s^2"),
    (r"9,81\,\mathrm{m}/\mathrm{s}^2", "9.81*_m/_s^2"),
    (r"3\cdot10^8\,\mathrm{m/s}", "3*10^8*_m/_s"),
    (r"100\,\mathrm{km/h}", "100*_km/_hr"),
    (r"2\,\mathrm{kg\cdot m^2}", "2*_kg*_m^2"),
    (r"5\,\mathrm{m}\,\mathrm{s}^{-1}", "5*_m/_s"),
    (r"5\,\mathrm{m}\,(1+2)", "5*_m*(1 + 2)"),
    (r"\frac{300\,\mathrm{cm}}{1\,\mathrm{m}}", "300*_cm/_m"),
    (r"5\,\mathrm{g}", "5*_gm"),
    (r"2\,\mathrm{l}", "2*_l"),
    (r"5\,\mathrm{min}", "5*_min"),
    (r"x = 5\,\mathrm{m}", "solve(x=5*_m,x)"),   # the unit isn't a variable to solve for
    # not units
    (r"2\,\mathrm{min}(a,b)", "2*min(a,b)"),
    (r"\int x\,\mathrm{d}x", "∫(x,x)"),
    (r"\int_0^1 2\,\mathrm{d}x", "∫(2,x,0,1)"),
    (r"2\mathrm{e}^x", "2*ℯ^(x)"),
    (r"3+2\mathrm{i}", "3 + 2*𝑖"),
    (r"x^2=2 \text{ for } x>0", "solve(x^2=2,x)|x>0"),
    (r"\mathrm{m}\cdot a", "m*a"),   # not after a number: a variable
]


@pytest.mark.parametrize("latex,want", UNITS)
def test_units(latex, want):
    assert cmd(latex) == want


@pytest.mark.parametrize("latex,what", [
    (r"5\text{ apples}", "'apples'"),
    (r"5\,\mathrm{m}/\mathrm{foo}", "'foo'"),
    (r"5\,\mathrm{m/}", "unit"),
    # TI-Nspire's names ignore case: _MW would be a milliwatt-like unknown, _nA Avogadro's
    # number, _mT unknown. Refused, not passed on.
    (r"4\,\mathrm{MW}", "'MW'"),
    (r"4\,\mathrm{mW}", "'mW'"),
    (r"2\,\mathrm{kN}", "'kN'"),
    (r"3\,\mathrm{nA}", "'nA'"),
    (r"3\,\mathrm{mT}", "'mT'"),
    (r"3\,\mathrm{\mu C}", "'µC'"),
    (r"3\,\mathrm{MJ}", "'MJ'"),
    (r"T^\circ\mathrm{C}", "number in front"),
])
def test_units_refused(latex, what):
    with pytest.raises(LatexError, match=what):
        plan(latex)


PHYSICS_UNITS = [
    (r"5\,\mathrm{K}", "5*_°K"),            # _K is Boltzmann's constant in TI-Nspire
    (r"2\,\mathrm{mol}\cdot 300\,\mathrm{K}", "2*_mol*300*_°K"),
    (r"5\,\mathrm{C}", "5*_coul"),          # _C is the speed of light
    (r"5\,\mathrm{H}", "5*_henry"),         # _H is Planck's constant
    (r"5\,\mathrm{V}", "5*_V"),
    (r"5\,\Omega", "5*_Ω"),
    (r"5\,\mathrm{\Omega}", "5*_Ω"),
    (r"4.7\,\mathrm{k}\Omega", "4.7*_kΩ"),
    (r"4.7\,\mathrm{k\Omega}", "4.7*_kΩ"),
    (r"3\,\mu\mathrm{A}", "3*_µA"),
    (r"3\,\mathrm{\mu A}", "3*_µA"),
    (r"3\,\mathrm{µA}", "3*_µA"),
    (r"3\,\mathrm{μA}", "3*_µA"),
    (r"2\,\mathrm{mA}\cdot 4.7\,\mathrm{k\Omega}", "2*_mA*4.7*_kΩ"),
    (r"25^\circ\mathrm{C}", "25*_°C"),
    (r"25^{\circ}C", "25*_°C"),
    (r"25°C", "25*_°C"),
    (r"100\,\mathrm{MHz}", "100*_MHz"),
    (r"2\,\mathrm{nF}", "2*_nF"),
    (r"1.5\,\mathrm{T}", "1.5*_T"),
    (r"1\,\mathrm{t}", "_mton"),             # metric ton; _t would be a tesla
    (r"3\,\mathrm{eV}", "3*_eV"),
    (r"1013\,\mathrm{mbar}", "1013*_mbar"),
    (r"2\,\mathrm{(m/s)^2}", "2*(_m/_s)^2"),
    (r"30^\circ", "30*π/180"),               # a bare degree sign is still an angle
]


@pytest.mark.parametrize("latex,want", PHYSICS_UNITS)
def test_physics_units(latex, want):
    assert cmd(latex) == want


CONVERSIONS = [
    (r"90\,\mathrm{km/h} \to \mathrm{m/s}", "90*_km/_hr@>_m/_s"),
    (r"90\,\mathrm{km/h} \rightarrow \mathrm{m}/\mathrm{s}", "90*_km/_hr@>_m/_s"),
    (r"90\,\mathrm{km/h}\text{ in }\mathrm{m/s}", "90*_km/_hr@>_m/_s"),
    (r"2\,\mathrm{mA}\cdot 4.7\,\mathrm{k\Omega} \to \mathrm{V}", "2*_mA*4.7*_kΩ@>_V"),
    (r"20\,\mathrm{mA}\cdot 50\,\Omega \to \mathrm{mV}", "20*_mA*50*_Ω@>_mV"),
    (r"3600\,\mathrm{J} \to \mathrm{kWh}", "3600*_J@>_kWh"),
    (r"25^\circ\mathrm{C} \to \mathrm{K}", "25*_°C@>_°K"),
    (r"x \text{ for } x=5\,\mathrm{km} \to \mathrm{m}", "(x|x=5*_km)@>_m"),
    # not conversions
    (r"\lim_{x\to 0} \frac{\sin x}{x}", "limit(sin(x)/x,x,0)"),
]


@pytest.mark.parametrize("latex,want", CONVERSIONS)
def test_conversions(latex, want):
    assert cmd(latex) == want


@pytest.mark.parametrize("latex,what", [
    (r"5\,\mathrm{km} \to \mathrm{foo}", "'foo'"),
    (r"x^2=4 \to \mathrm{m}", "convert the result in a step of its own"),
    (r"\to \mathrm{m}", "Nothing to convert"),
])
def test_conversions_refused(latex, what):
    with pytest.raises(LatexError, match=what):
        plan(latex)


@pytest.mark.parametrize("text,want", [
    ("km/h", "_km/_hr"), ("m/s^2", "_m/_s^2"), (r"\mathrm{m}/\mathrm{s}^{-1}", "_m/_s^(-1)"),
    (r"k\Omega", "_kΩ"), ("µA", "_µA"), (r"\mu A", "_µA"), ("_km/_hr", "_km/_hr"), (" _m / _s^2 ", "_m/_s^2"),
    (r"^\circ C", "_°C"), ("K", "_°K"), ("kg*m^2", "_kg*_m^2"),
])
def test_unit_command(text, want):
    assert unit_command(text) == want


@pytest.mark.parametrize("text", ["", "MW", "m/", "_km/hr", "km+h", "_m; DelVar a"])
def test_unit_command_refused(text):
    with pytest.raises(LatexError):
        unit_command(text)


CONSTANTS = [
    (r"m g h", ["g"], "m*_g*h", ["g"]),       # only the constants asked for: h stays a height
    (r"m g h", True, "m*_g*_h", ["g", "h"]),
    (r"k_B T", ["k_B"], "_k*T", ["kB"]),
    (r"m_e c^2", ["m_e", "c"], "_Me*_c^2", ["c", "me"]),
    (r"\frac{q_e^2}{4\pi\varepsilon_0 r^2}", ["q_e", r"\varepsilon_0"], "_q^2/(4*π*_ε0*r^2)", ["qe", "ε0"]),
    (r"N_A k_B", ["N_A", "k_B"], "_Na*_k", ["NA", "kB"]),
    (r"\sum_{g=1}^{3} g", ["g"], "Σ(g,g,1,3)", []),   # a bound g is the summation index
    (r"5\,\mathrm{kg}\cdot g \to \mathrm{N}", ["g"], "5*_kg*_g@>_N", ["g"]),
    (r"m g", [], "m*g", []),
]


@pytest.mark.parametrize("latex,consts,want,used", CONSTANTS)
def test_constants(latex, consts, want, used):
    p = plan(latex, constants=consts)
    assert p.command == want
    assert p.parsed.constants == used


def test_constants_unknown_refused():
    with pytest.raises(LatexError, match="isn't a physical constant"):
        plan("m g", constants=["gee"])


# ---- decimal comma: 3{,}5 always, plain 3,5 by mode (None default, True = decimal-comma mode, False = off)

@pytest.mark.parametrize("latex,want", [
    (r"3{,}5+1", "3.5 + 1"),
    (r"-0{,}25", "-0.25"),
    (r"1{,}5\cdot10^{3}", "1.5*10^3"),
    (r"f(1{,}5, 2)", "f(1.5,2)"),
    (r"\{1{,}5, 2\}", "{1.5,2}"),
    (r"\frac{1{,}5}{2}", "1.5/2"),
    (r"x=2{,}5", "solve(x=2.5,x)"),
])
@pytest.mark.parametrize("mode", [None, False, True])
def test_braced_comma_is_a_decimal_in_every_mode(latex, want, mode):
    assert cmd(latex, decimal_comma=mode) == want


@pytest.mark.parametrize("latex", [r"{,}5", r"3{,}x", r"3{,}", r"x{,}2"])
def test_braced_comma_needs_digits_on_both_sides(latex):
    with pytest.raises(LatexError, match=r"\{,\}"):
        plan(latex)


@pytest.mark.parametrize("latex,want", [
    (r"1,5+2", "1.5 + 2"),
    (r"f(1,5)", "f(1.5)"),                    # a decimal, not two arguments
    (r"f(1;5)", "f(1,5)"),                    # ';' separates
    (r"\gcd(12;18)", "gcd(12,18)"),
    (r"\max(1,5; 2)", "max(1.5,2)"),
    (r"\{1;2;3\}", "{1,2,3}"),
    (r"\{1,5; 2,5\}", "{1.5,2.5}"),
    (r"x=2, x=3", "solve(x=2 and x=3,{x})"),  # comma followed by a space: a separator
    (r"x=1,5; x=2", "solve(x=1.5 and x=2,{x})"),
    (r"x=1,5", "solve(x=1.5,x)"),
    (r"-0,25", "-0.25"),
    (r"1,5\cdot10^{3}", "1.5*10^3"),
    (r"\begin{pmatrix}1,5&2\\3&4\end{pmatrix}", "[[1.5,2][3,4]]"),
    (r"\sqrt{2,25}+\frac{1,5}{2}", "√(2.25) + 1.5/2"),
])
def test_decimal_comma_mode(latex, want):
    assert cmd(latex, decimal_comma=True) == want


@pytest.mark.parametrize("latex", [r"1,2,3", r"1,500,000", r"1.5,2", r"f(1,2,3)", r"\{1,2,3\}"])
def test_decimal_comma_mode_refuses_ambiguous_runs(latex):
    with pytest.raises(LatexError, match="decimal-comma mode"):
        plan(latex, decimal_comma=True)


def test_decimal_comma_off_never_reads_plain_commas_as_decimals():
    assert cmd(r"\{1,2,3\}", decimal_comma=False) == "{1,2,3}"
    assert cmd(r"1,2,3", decimal_comma=False) == "{1,2,3}"
    assert cmd(r"f(1,5)", decimal_comma=False) == "f(1,5)"
    assert cmd(r"\max(a,b)", decimal_comma=True) == "max(a,b)"
    assert cmd(r"\binom{5}{2}", decimal_comma=True) == "nCr(5,2)"


def test_default_mode_is_unchanged():
    assert cmd(r"1,5+2") == "1.5 + 2"
    assert cmd(r"f(1,5)") == "f(1,5)"


def test_to_decimal_comma():
    from nspire_latex import to_decimal_comma as t
    assert t("1.5") == "1,5"
    assert t("{1.5,2}") == "{1,5;2}"
    assert t("x=1.5 or x=2.25") == "x=1,5 or x=2,25"
    assert t("f(x)=x.") == "f(x)=x."
    assert t("3.ᴇ-4") == "3.ᴇ-4"
    assert t(r"x=1.5,\quad y=2.5", latex=True) == r"x=1{,}5,\quad y=2{,}5"
    assert t(r"\left\{1.5, 2\right\}", latex=True) == r"\left\{1{,}5; 2\right\}"
    assert t(r"\frac{1}{2}", latex=True) == r"\frac{1}{2}"


def test_format_answer_decimal_comma():
    from nspire_latex import CasRun, format_answer
    p = plan("1.5+1")
    run = CasRun(p, "2.5", "3.5")
    out = format_answer(run, True)
    assert out["result"] == "2,5" and out["approx"] == "3,5"
    assert out["result_latex"] == "2{,}5" and out["approx_latex"] == "3{,}5"
    assert out["nspire"] == readable(p.command)       # the command keeps decimal points
    assert format_answer(run)["result"] == "2.5"
