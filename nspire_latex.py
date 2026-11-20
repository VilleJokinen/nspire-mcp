"""LaTeX -> TI-Nspire command, and TI-Nspire result -> LaTeX.

parse:   LaTeX --(preprocess, sympy antlr parser, cleanup)--> sympy trees
plan:    sympy trees + action --> one TI-Nspire command string
print:   NspirePrinter writes sympy trees in TI-Nspire syntax
back:    result_to_latex() parses CAS output with sympy and prints LaTeX
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sympy as sp
from sympy.core.function import AppliedUndef
from sympy.parsing.latex import parse_latex
from sympy.parsing.sympy_parser import parse_expr, standard_transformations
from sympy.printing.str import StrPrinter

from nspire_bridge import NS_C, NS_E, NS_EXP, NS_I, NS_N


class LatexError(ValueError):
    pass


# ================================================================ preprocess

_PH = "Omega"  # placeholder symbols are \Omega_{9NN}; restored after parsing

_DROP = [r"\displaystyle", r"\textstyle", r"\limits", r"\nolimits", r"\middle",
         r"\bigl", r"\bigr", r"\Bigl", r"\Bigr", r"\biggl", r"\biggr", r"\big", r"\Big", r"\bigg", r"\Bigg"]
_UNICODE = {"−": "-", "·": r"\cdot ", "×": r"\times ", "÷": r"\div ", "≤": r"\leq ", "≥": r"\geq ",
            "≠": r"\neq ", "π": r"\pi ", "∞": r"\infty ", "√": r"\sqrt", "°": r"^{\circ}",
            "θ": r"\theta ", "α": r"\alpha ", "β": r"\beta ", "λ": r"\lambda ", "ω": r"\omega "}
_STYLE_WRAPPERS = ["mathbf", "boldsymbol", "mathit", "mathsf", "mathtt", "vec", "hat", "bar",
                   "tilde", "overline", "underline", "mathbb", "mathcal", "bm"]
_MATRIX_ENVS = "pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|matrix|smallmatrix"
# \operatorname / \mathrm names sympy's parser knows as commands
_KNOWN_CMDS = {"sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh", "arcsin", "arccos",
               "arctan", "ln", "log", "exp", "max", "min", "gcd", "lcm", "det"}
_RELATION = re.compile(r"(?<![<>!\\])=|<|>|\\leq?\b|\\geq?\b|\\neq\b|\\ne\b|\\leqslant|\\geqslant")
_GREEK = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
          "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ",
          "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "rho": "ρ", "sigma": "σ", "tau": "τ",
          "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
          "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Sigma": "Σ",
          "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω"}


@dataclass
class Parsed:
    exprs: list            # sympy trees, one per equation/expression
    ode_dep: str | None = None    # dependent variable of an ODE, e.g. "y"
    ode_indep: str | None = None  # independent variable, e.g. "x"
    placeholders: dict = field(default_factory=dict)


def _match_brace(s: str, i: int) -> int:
    """s[i] == '{'; return index of the matching '}'."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    raise LatexError("Unbalanced braces in the LaTeX.")


def _unwrap_cmd(s: str, cmd: str, fn) -> str:
    """Replace every \\cmd{arg} with fn(arg)."""
    pat = re.compile(r"\\" + cmd + r"\s*\{")
    while (m := pat.search(s)):
        end = _match_brace(s, m.end() - 1)
        s = s[:m.start()] + fn(s[m.end():end]) + s[end + 1:]
    return s


def _split_top(s: str, seps: tuple[str, ...]) -> list[str]:
    """Split s at any of seps occurring outside braces/brackets/parentheses."""
    out, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c in "{([":
            depth += 1
        elif c in "})]":
            depth -= 1
        if depth == 0:
            hit = next((sep for sep in seps if s.startswith(sep, i)), None)
            if hit:
                out.append("".join(cur))
                cur = []
                i += len(hit)
                continue
        cur.append(c)
        i += 1
    out.append("".join(cur))
    return [p for p in (x.strip() for x in out) if p]


class _Pre:
    def __init__(self) -> None:
        self.placeholders: dict[str, tuple[str, object]] = {}  # name -> (kind, payload)
        self.ode_indep: str | None = None

    def ph(self, kind: str, payload) -> str:
        key = f"9{len(self.placeholders):02d}"
        self.placeholders[f"{_PH}_{{{key}}}"] = (kind, payload)
        return rf" \{_PH}_{{{key}}} "

    def run(self, latex: str) -> list[str]:
        s = latex.strip()
        for a, b in _UNICODE.items():
            s = s.replace(a, b)
        # math delimiters and display environments
        s = re.sub(r"^\$\$?|\$\$?$", "", s.strip())
        s = re.sub(r"^\\\[|\\\]$|^\\\(|\\\)$", "", s.strip())
        s = re.sub(r"\\(begin|end)\{(equation|align|gather|multline|eqnarray|split)\*?\}", "", s)
        s = re.sub(r"\\(label|tag)\{[^}]*\}", "", s)
        s = s.replace("&", " ") if not re.search(r"\\begin\{(" + _MATRIX_ENVS + r"|array|cases)\}", s) else s
        for d in _DROP:
            s = s.replace(d, " ")
        s = re.sub(r"\\left\s*\.|\\right\s*\.", " ", s)
        s = re.sub(r"\\(left|right)\b\s*", "", s)  # keep the delimiter that follows
        s = s.replace(r"\lbrace", r"\{").replace(r"\rbrace", r"\}")
        s = re.sub(r"\\q?quad\b", " ", s)
        s = re.sub(r"(?<!\\)\\[,;:! ]", " ", s)  # not the second half of a \\ row break
        s = s.replace("~", " ")
        s = s.replace(r"\dfrac", r"\frac").replace(r"\tfrac", r"\frac").replace(r"\dbinom", r"\binom")
        s = s.replace(r"\iiint", r"\int\int\int").replace(r"\iint", r"\int\int")
        s = s.replace(r"\leqslant", r"\leq").replace(r"\geqslant", r"\geq")
        s = re.sub(r"\\(land|wedge)\b", r" \\\\ ", s)
        if r"\pm" in s or r"\mp" in s:
            raise LatexError("± isn't supported; write the two cases separately or ask to solve the equation.")
        for w in _STYLE_WRAPPERS:
            s = _unwrap_cmd(s, w, lambda a: "{" + a + "}")
        s = _unwrap_cmd(s, "text", self._text)
        s = _unwrap_cmd(s, r"mathrm", self._name)
        s = _unwrap_cmd(s, r"operatorname\*?", self._name)
        # degrees: 30^\circ -> 30*pi/180
        s = re.sub(r"\^\s*\{?\s*\\circ\s*\}?", r"\\cdot\\frac{\\pi}{180}", s)
        # log conventions: \log x is base 10 (as on the calculator), \lg too
        s = re.sub(r"\\lg\b", r"\\log_{10}", s)
        s = re.sub(r"\\log\b(?!\s*_)", r"\\log_{10}", s)
        s = self._derivatives(s)
        s = self._matrices(s)
        # Finnish/European decimal comma between digits at top level: 1,5 -> 1.5
        s = re.sub(r"(?<=\d),(?=\d)", ".", s) if not re.search(r"[({\[]", s) else s
        rows = self._cases(s)
        pieces: list[str] = []
        for r in rows:
            parts = _split_top(r, (";",))
            for p in parts:
                comma = _split_top(p, (",",))
                if len(comma) > 1 and all(_RELATION.search(c) for c in comma):
                    pieces.extend(comma)
                elif len(comma) > 1 and not any(_RELATION.search(c) for c in comma):
                    pieces.extend(comma)
                else:
                    pieces.append(p)
        pieces = [p.rstrip(".,; ") for p in pieces]
        return [p for p in pieces if p and not re.fullmatch(r"\s*", p)]

    def _text(self, arg: str) -> str:
        w = arg.strip().lower()
        if w in ("and", "ja", ",", "&", "") or w.startswith("and") or w.startswith("for") or w.startswith("kun"):
            return r" \\ " if w in ("and", "ja", ",", "&") else " "
        if w in ("or", "tai"):
            raise LatexError("'or' between equations isn't supported; send each case separately.")
        return " "

    def _name(self, arg: str) -> str:
        a = arg.strip()
        if a in ("d",):
            return " d"
        if a in ("e", "i"):
            return f" {a} "
        if len(a) == 1:
            return a
        if a in _KNOWN_CMDS:
            return f"\\{a} "
        return self.ph("name", a)

    def _derivatives(self, s: str) -> str:
        # \frac{d^n y}{d x^n} / \frac{dy}{dx}  ->  y'' (record x)
        pat = re.compile(r"\\frac\s*\{\s*d\s*(?:\^\s*\{?\s*(\d)\s*\}?)?\s*([A-Za-z])\s*\}"
                         r"\s*\{\s*d\s*([A-Za-z])\s*(?:\^\s*\{?\s*\d\s*\}?)?\s*\}")

        def rep(m):
            n = int(m.group(1) or 1)
            self.ode_indep = self.ode_indep or m.group(3)
            return f"{m.group(2)}{chr(39) * n}"
        s = pat.sub(rep, s)
        # y^{\prime\prime}, y^{(3)}, \dot y, \ddot y
        s = re.sub(r"\^\s*\{\s*((?:\\prime\s*)+)\}", lambda m: "'" * m.group(1).count("prime"), s)
        s = re.sub(r"\^\s*\\prime", "'", s)
        s = re.sub(r"\^\s*\{\s*\((\d)\)\s*\}", lambda m: "'" * int(m.group(1)), s)

        def dot(m):
            self.ode_indep = self.ode_indep or "t"
            return m.group(2) + "'" * (2 if m.group(1) == "ddot" else 1)
        s = re.sub(r"\\(ddot|dot)\s*\{?\s*([A-Za-z])\s*\}?", dot, s)
        return s

    def _matrices(self, s: str) -> str:
        pat = re.compile(r"\\begin\{(" + _MATRIX_ENVS + r")\}(.*?)\\end\{\1\}", re.S)

        def rep(m):
            rows = [r for r in re.split(r"\\\\", m.group(2)) if r.strip()]
            cells = [[c.strip() for c in r.split("&")] for r in rows]
            if len({len(r) for r in cells}) != 1:
                raise LatexError("Matrix rows have different lengths.")
            kind = "det" if m.group(1) in ("vmatrix", "Vmatrix") else "matrix"
            return self.ph(kind, cells)
        return pat.sub(rep, s)

    def _cases(self, s: str) -> list[str]:
        m = re.search(r"\\begin\{(cases|array)\}(?:\{[^}]*\})?(.*?)\\end\{\1\}", s, re.S)
        if m:
            body = m.group(2)
            rows = [r.split("&")[0] for r in re.split(r"\\\\", body)]
            rest = (s[:m.start()] + s[m.end():]).replace(r"\{", "").replace(r"\}", "").strip()
            return [r for r in rows if r.strip()] + ([rest] if rest else [])
        return [r for r in re.split(r"\\\\", s) if r.strip()]


# ================================================================ parse

_FUNC_NAMES = {"f", "g", "h", "F", "G", "H"}
_RESERVED = {"e", "i", "pi"}


def _parse_one(src: str) -> sp.Basic:
    try:
        return parse_latex(src, backend="antlr")
    except Exception as e:
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise LatexError(f"Couldn't parse LaTeX {src!r}: {first}") from None


def _bound_symbols(e: sp.Basic) -> set[str]:
    names = set()
    for node in sp.preorder_traversal(e):
        if isinstance(node, (sp.Integral, sp.Sum, sp.Product)):
            names |= {str(lim[0]) for lim in node.limits}
        elif isinstance(node, sp.Limit):
            names.add(str(node.args[1]))
    return names


def parse(latex: str) -> Parsed:
    pre = _Pre()
    pieces = pre.run(latex)
    if not pieces:
        raise LatexError("The LaTeX is empty.")
    raw = [_parse_one(p) for p in pieces]

    # matrix placeholders: parse their cells
    ph_values: dict[str, sp.Basic] = {}
    for name, (kind, payload) in pre.placeholders.items():
        if kind in ("matrix", "det"):
            m = sp.Matrix([[_parse_one(c) for c in row] for row in payload])
            ph_values[name] = sp.Function("det")(m) if kind == "det" else m

    prime_names = set()
    for e in raw:
        for node in sp.preorder_traversal(e):
            nm = getattr(node, "name", None) or (node.func.__name__ if isinstance(node, AppliedUndef) else None)
            if nm and re.fullmatch(r"[A-Za-z]'+", nm):
                prime_names.add(nm)
    # primes make an ODE only alongside an equation; a bare f'(x) is a derivative
    has_rel = any(isinstance(e, sp.core.relational.Relational) for e in raw)
    ode_dep = sorted(prime_names)[0][0] if prime_names and has_rel else None
    all_syms = {s.name for e in raw for s in e.free_symbols}

    def fix(e):
        bound = _bound_symbols(e)

        def walk(node):
            if isinstance(node, sp.Symbol):
                n = node.name
                if n in ph_values:
                    return ph_values[n]
                if n in pre.placeholders:
                    return sp.Symbol(pre.placeholders[n][1])
                if n not in bound:
                    if n == "e":
                        return sp.E
                    if n == "i":
                        return sp.I
                if n == "pi":
                    return sp.pi
                return node
            if isinstance(node, AppliedUndef):
                n = node.func.__name__
                args = [walk(a) for a in node.args]
                if n in pre.placeholders:
                    return sp.Function(pre.placeholders[n][1])(*args)
                pm = re.fullmatch(r"([A-Za-z])('+)", n)
                if pm and not ode_dep and len(args) == 1:
                    # f'(x) -> derivative(f(x),x); f''(2) -> derivative(f(t),t,2) at t=2
                    f, k, a = sp.Function(pm.group(1)), len(pm.group(2)), args[0]
                    v = a if a.is_Symbol else sp.Symbol("x")
                    d = sp.Derivative(f(v), (v, k))
                    return d if a.is_Symbol else sp.Subs(d, v, a)
                is_func = (n in _FUNC_NAMES or re.fullmatch(r"[A-Za-z]'+", n) or n == ode_dep
                           or n in ("gcd", "lcm", "max", "min", "det"))
                if not is_func and len(args) == 1 and n not in ("e",):
                    # x(x+1) is a product, not a call
                    sym = {"e": sp.E, "i": sp.I}.get(n, sp.Symbol(n))
                    return sp.Mul(sym, args[0], evaluate=False)
                return sp.Function(n)(*args)
            if not node.args:
                return node
            new = [walk(a) for a in node.args]
            if node.func in (sp.Add, sp.Mul, sp.Pow) and not any(isinstance(a, sp.MatrixBase) for a in new):
                # arithmetic only: folds 1*5*x and flattens (a+b)+c; integrals,
                # limits, sums etc. below stay unevaluated for the CAS
                return node.func(*new)
            try:
                return node.func(*new, evaluate=False)
            except TypeError:
                return node.func(*new)
        return walk(e)

    exprs = [fix(e) for e in raw]
    indep = pre.ode_indep
    if ode_dep and not indep:
        indep = "t" if "t" in all_syms and "x" not in all_syms else "x"
    return Parsed(exprs=exprs, ode_dep=ode_dep, ode_indep=indep if ode_dep else None,
                  placeholders=pre.placeholders)


# ================================================================ print (sympy -> TI-Nspire)

_FN_MAP = {"asin": "arcsin", "acos": "arccos", "atan": "arctan", "acot": "arccot", "asec": "arcsec",
           "acsc": "arccsc", "asinh": "arcsinh", "acosh": "arccosh", "atanh": "arctanh",
           "Abs": "abs", "ceiling": "ceiling", "floor": "floor", "sign": "sign", "Max": "max",
           "Min": "min", "re": "real", "im": "imag", "Re": "real", "Im": "imag", "conjugate": "conj", "arg": "angle",
           "binomial": "nCr", "exp": "exp", "gcd": "gcd", "lcm": "lcm"}


def _clean_name(n: str) -> str:
    n = n.replace("{", "").replace("}", "").replace(" ", "")
    base, _, sub = n.partition("_")
    base = _GREEK.get(base, base)
    sub = re.sub(r"[^0-9A-Za-z]", "", sub)
    return base + sub


class NspirePrinter(StrPrinter):
    def __init__(self, ode_dep: str | None = None, ode_indep: str | None = None):
        super().__init__()
        self.ode_dep, self.ode_indep = ode_dep, ode_indep

    # atoms
    def _print_Symbol(self, e):
        return _clean_name(e.name)

    def _print_Exp1(self, e):
        return NS_E

    def _print_ImaginaryUnit(self, e):
        return NS_I

    def _print_Pi(self, e):
        return "π"

    def _print_Infinity(self, e):
        return "∞"

    def _print_NegativeInfinity(self, e):
        return "-∞"

    def _print_ComplexInfinity(self, e):
        return "undef"

    def _print_NaN(self, e):
        return "undef"

    def _print_Float(self, e):
        s = repr(float(e))
        if "e" in s:
            m, x = s.split("e")
            return f"({m}*10^({int(x)}))"
        return s

    def _print_Rational(self, e):
        return f"{e.p}/{e.q}" if e.q != 1 else str(e.p)

    # operators
    def _wrap(self, e, simple_ok=True):
        s = self._print(e)
        if simple_ok and (e.is_Symbol or (e.is_Integer and e >= 0) or e.is_Function
                          or isinstance(e, (sp.Matrix, sp.ImmutableMatrix)) or e in (sp.pi, sp.E, sp.I)):
            return s
        return f"({s})"

    def _print_Pow(self, e, rational=False):
        b, x = e.args
        if isinstance(b, sp.MatrixBase):
            return f"{self._print(b)}^({self._print(x)})"
        if x == sp.S.Half:
            return f"√({self._print(b)})"
        if x == -sp.S.Half:
            return f"1/√({self._print(b)})"
        if x == sp.S.NegativeOne:
            return f"1/{self._wrap(b)}"
        if x.is_Rational and x.p == 1 and x.q > 2:
            return f"root({self._print(b)},{x.q})"
        if b == sp.E:
            return f"{NS_E}^({self._print(x)})"
        exp = self._print(x) if (x.is_Symbol or (x.is_Integer and x >= 0)) else f"({self._print(x)})"
        return f"{self._wrap(b)}^{exp}"

    def _print_Relational(self, e):
        op = {"==": "=", "!=": "≠", "<=": "≤", ">=": "≥", "<": "<", ">": ">"}[e.rel_op]
        return f"{self._print(e.lhs)}{op}{self._print(e.rhs)}"

    def _print_factorial(self, e):
        return f"{self._wrap(e.args[0])}!"

    def _print_log(self, e):
        if len(e.args) == 1 or e.args[1] == sp.E:
            return f"ln({self._print(e.args[0])})"
        if e.args[1] == 10:
            return f"log({self._print(e.args[0])})"
        return f"log({self._print(e.args[0])},{self._print(e.args[1])})"

    def _print_Function(self, e):
        n = e.func.__name__
        if isinstance(e, AppliedUndef) and n == self.ode_dep and len(e.args) == 1 \
                and str(e.args[0]) == self.ode_indep:
            return n
        n = _FN_MAP.get(n, n)
        if isinstance(e, AppliedUndef):
            n = _clean_name(n) if "'" not in n else n
        return f"{n}({','.join(self._print(a) for a in e.args)})"

    def _print_Integral(self, e):
        s = self._print(e.function)
        for lim in e.limits:
            parts = [s] + [self._print(p) for p in lim]
            s = f"∫({','.join(parts)})"
        return s

    def _print_Derivative(self, e):
        s = self._print(e.expr)
        for v, n in e.variable_count:
            s = f"derivative({s},{self._print(v)}" + (f",{n})" if n != 1 else ")")
        return s

    def _print_Subs(self, e):
        expr, vs, pts = e.args
        cond = " and ".join(f"{self._print(v)}={self._print(p)}" for v, p in zip(vs, pts))
        return f"({self._print(expr)}|{cond})"

    def _print_Limit(self, e):
        f, z, z0, d = e.args
        s = f"limit({self._print(f)},{self._print(z)},{self._print(z0)}"
        if z0.is_finite and str(d) in ("+", "-"):
            s += ",1" if str(d) == "+" else ",-1"
        return s + ")"

    def _print_Sum(self, e):
        s = self._print(e.function)
        for v, a, b in e.limits:
            s = f"Σ({s},{self._print(v)},{self._print(a)},{self._print(b)})"
        return s

    def _print_Product(self, e):
        s = self._print(e.function)
        for v, a, b in e.limits:
            s = f"Π({s},{self._print(v)},{self._print(a)},{self._print(b)})"
        return s

    def _print_MatrixBase(self, e):
        return "[" + "".join("[" + ",".join(self._print(c) for c in e.row(i)) + "]"
                             for i in range(e.rows)) + "]"

    _print_ImmutableMatrix = _print_MutableDenseMatrix = _print_ImmutableDenseMatrix = _print_MatrixBase


def to_nspire(e, parsed: Parsed | None = None) -> str:
    p = NspirePrinter(parsed.ode_dep if parsed else None, parsed.ode_indep if parsed else None)
    return p.doprint(e)


# ================================================================ plan

ACTIONS = {
    # name: (template, needs a variable, description)
    "eval": ("{e}", False, "evaluate"),
    "simplify": ("{e}", False, "simplify"),
    "approx": ("approx({e})", False, "decimal approximation"),
    "solve": ("solve({e},{v})", True, "solve"),
    "csolve": ("cSolve({e},{v})", True, "solve over ℂ"),
    "nsolve": ("nSolve({e},{v})", True, "solve numerically"),
    "zeros": ("zeros({e},{v})", True, "zeros"),
    "czeros": ("cZeros({e},{v})", True, "complex zeros"),
    "factor": ("factor({e})", False, "factor"),
    "cfactor": ("cFactor({e})", False, "factor over ℂ"),
    "expand": ("expand({e})", False, "expand"),
    "propfrac": ("propFrac({e})", False, "partial fractions"),
    "comdenom": ("comDenom({e})", False, "common denominator"),
    "texpand": ("tExpand({e})", False, "expand trig"),
    "tcollect": ("tCollect({e})", False, "collect trig"),
    "diff": ("derivative({e},{v})", True, "differentiate"),
    "integrate": ("∫({e},{v})", True, "integrate"),
    "taylor": ("taylor({e},{v},{order})", True, "Taylor polynomial"),
    "limit": ("limit({e},{v},{point})", True, "limit"),
    "det": ("det({e})", False, "determinant"),
    "inverse": ("({e})^(-1)", False, "inverse"),
    "rref": ("rref({e})", False, "reduced row echelon form"),
    "eigenvalues": ("eigVl({e})", False, "eigenvalues"),
    "eigenvectors": ("eigVc({e})", False, "eigenvectors"),
    "desolve": ("deSolve({e},{x},{y})", False, "solve the differential equation"),
    "define": ("Define {e}", False, "define a function"),
}
_VAR_PREF = ["x", "y", "z", "t", "u", "v", "w", "s", "r", "n", "k", "a", "b", "c"]


@dataclass
class Plan:
    command: str
    action: str
    description: str
    variables: list[str]
    parsed: Parsed


def _free_names(exprs) -> list[str]:
    names = set()
    for e in exprs:
        bound = _bound_symbols(e)
        names |= {_clean_name(s.name) for s in e.free_symbols if s.name not in bound}
        names -= {"e", "i", "pi"}
    names = {n for n in names if "'" not in n}
    return sorted(names, key=lambda n: (_VAR_PREF.index(n) if n in _VAR_PREF else 99, n))


def plan(latex: str, action: str = "auto", var: str | None = None, order: int | None = None,
         point: str | None = None) -> Plan:
    action = (action or "auto").lower().replace(" ", "").replace("_", "")
    if action != "auto" and action not in ACTIONS:
        raise LatexError(f"Unknown action {action!r}. Use auto or one of: {', '.join(ACTIONS)}.")
    p = parse(latex)
    exprs = p.exprs
    rels = [e for e in exprs if isinstance(e, sp.core.relational.Relational)]
    user_vars = [v.strip() for v in re.split(r"[,\s]+", var) if v.strip()] if var else []

    if action in ("auto", "define") and len(exprs) == 1 and isinstance(exprs[0], sp.Eq) \
            and isinstance(exprs[0].lhs, AppliedUndef) and all(a.is_Symbol for a in exprs[0].lhs.args) \
            and not p.ode_dep:
        # f(x)=x^2 defines f for later calls
        cmd = "Define " + to_nspire(exprs[0], p)
        return Plan(cmd, "define", f"define {exprs[0].lhs.func.__name__}", [], p)
    if action == "define":
        raise LatexError("define needs the form f(x)=expression.")

    if action == "auto":
        if p.ode_dep:
            action = "desolve"
        elif rels:
            action = "solve"
        else:
            action = "eval"

    if action == "desolve":
        if not p.ode_dep:
            raise LatexError("desolve needs a derivative such as y' or \\frac{dy}{dx}.")
        body = " and ".join(to_nspire(e, p) for e in exprs)
        cmd = ACTIONS["desolve"][0].format(e=body, x=p.ode_indep, y=p.ode_dep)
        return Plan(cmd, action, ACTIONS[action][2], [p.ode_dep], p)

    tmpl, needs_var, desc = ACTIONS[action]
    free = _free_names(exprs)
    if action in ("solve", "csolve", "nsolve") and len(exprs) > 1:
        n = len(exprs)
        vs = user_vars or (free if len(free) <= n else free[:n])
        body = " and ".join(to_nspire(e if isinstance(e, sp.core.relational.Relational) else sp.Eq(e, 0), p)
                            for e in exprs)
        vtxt = "{" + ",".join(vs) + "}"
        return Plan(tmpl.format(e=body, v=vtxt), action, f"{desc} for {', '.join(vs)}", vs, p)

    if len(exprs) > 1:
        body = "{" + ",".join(to_nspire(e, p) for e in exprs) + "}"
    else:
        e = exprs[0]
        if action in ("solve", "csolve", "nsolve") and not isinstance(e, sp.core.relational.Relational):
            e = sp.Eq(e, 0)
        body = to_nspire(e, p)
    vs: list[str] = []
    if needs_var:
        vs = user_vars[:1] or free[:1]
        if not vs:
            raise LatexError(f"{action} needs a variable and the expression has none.")
    cmd = tmpl.format(e=body, v=vs[0] if vs else "", order=order or 5, point=point or "0")
    if action == "taylor" and point:
        cmd = cmd[:-1] + f",{point})"
    return Plan(cmd, action, desc + (f" for {vs[0]}" if vs and action in ("solve", "csolve", "nsolve", "zeros", "czeros") else ""),
                vs, p)


# ================================================================ result -> LaTeX

def _log(x, b=None):
    if b is None or b == sp.E:
        return sp.log(x)
    return sp.Function(f"log_{{{sp.latex(b)}}}")(x)


_OUT_FN = {"arcsin": sp.asin, "arccos": sp.acos, "arctan": sp.atan, "arccot": sp.acot,
           "arcsec": sp.asec, "arccsc": sp.acsc, "arcsinh": sp.asinh, "arccosh": sp.acosh,
           "arctanh": sp.atanh, "ln": sp.log, "abs": sp.Abs, "floor": sp.floor, "ceiling": sp.ceiling,
           "sign": sp.sign, "nCr": sp.binomial, "real": sp.re, "imag": sp.im, "conj": sp.conjugate,
           "sqrt": sp.sqrt, "exp": sp.exp, "E": sp.E, "I": sp.I, "pi": sp.pi, "oo": sp.oo,
           "log": lambda x, b=10: _log(x, b), "root": lambda x, n: sp.root(x, n),
           "max": sp.Max, "min": sp.Min, "Matrix": sp.Matrix}
_REL_TEX = {"<=": r"\leq", ">=": r"\geq", "!=": r"\neq", "=": "=", "<": "<", ">": ">"}


def _norm_result(text: str) -> str:
    t = text.replace("−", "-").replace("ℯ", NS_E).replace("𝑖", NS_I).replace("ᴇ", NS_EXP)
    t = re.sub(NS_C + r"(\d+)", r"c_\1", t)
    t = re.sub(NS_N + r"(\d+)", r"n_\1", t)
    t = t.replace(NS_E, "E").replace(NS_I, "I").replace(NS_EXP, "e")
    t = t.replace("√(", "sqrt(").replace("π", "pi").replace("∞", "oo").replace("^", "**")
    t = t.replace("≤", "<=").replace("≥", ">=").replace("≠", "!=")
    for g, r in [("θ", "theta"), ("α", "alpha"), ("β", "beta"), ("λ", "lamda"), ("ω", "omega")]:
        t = t.replace(g, r)
    t = re.sub(r"\[\[(.*?)\]\]", lambda m: "Matrix([[" + m.group(1).replace("][", "],[") + "]])", t)
    return t


def _split_rel(s: str) -> tuple[list[str], list[str]]:
    """'-2<x<=4' -> (['-2','x','4'], ['<','<=']), at bracket depth 0 only."""
    parts, ops, depth, cur, i = [], [], 0, "", 0
    while i < len(s):
        c = s[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if depth == 0:
            two = s[i:i + 2]
            if two in ("<=", ">=", "!="):
                parts.append(cur); ops.append(two); cur = ""; i += 2
                continue
            if c in "=<>":
                parts.append(cur); ops.append(c); cur = ""; i += 1
                continue
        cur += c
        i += 1
    parts.append(cur)
    return parts, ops


def _expr_latex(s: str, ld: dict) -> str:
    try:
        e = parse_expr(s, local_dict=ld, transformations=standard_transformations)
    except Exception:
        e = parse_expr(s, local_dict=ld, transformations=standard_transformations, evaluate=False)
    return sp.latex(e, ln_notation=True)


def _atom_latex(s: str, ld: dict) -> str:
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        items = _split_top(s[1:-1], (",",))
        return r"\left\{" + ", ".join(_atom_latex(i, ld) for i in items) + r"\right\}"
    parts, ops = _split_rel(s)
    out = _expr_latex(parts[0], ld)
    for op, side in zip(ops, parts[1:]):
        out += f" {_REL_TEX[op]} " + _expr_latex(side, ld)
    return out


def result_to_latex(text: str) -> str | None:
    """CAS result text -> LaTeX, or None if it couldn't be converted."""
    t = text.strip()
    low = t.lower()
    if low == "false":
        return r"\text{no solution}"
    if low == "true":
        return r"\text{true (holds for all values)}"
    if low == "undef":
        return r"\text{undefined}"
    if not t or low == "done" or "piecewise(" in t:
        return None
    try:
        norm = _norm_result(t)
        ld = dict(_OUT_FN)
        for m in re.finditer(r"[A-Za-z_][A-Za-z_0-9]*", norm):
            n = m.group()
            if n in ld:
                continue
            called = norm[m.end():m.end() + 1] == "("
            if called and n.islower() and callable(getattr(sp, n, None)):
                ld[n] = getattr(sp, n)           # sin, cos, tanh, ...
            elif called:
                ld[n] = sp.Function(n)           # user function f(x)
            else:
                ld[n] = sp.Symbol(n)
        alts = []
        for alt in _split_top(norm, (" or ",)):
            alts.append(r",\quad ".join(_atom_latex(a, ld) for a in _split_top(alt, (" and ",))))
        return r" \quad\text{or}\quad ".join(alts)
    except Exception:
        return None


def is_exact_numeric(text: str) -> bool:
    """True if an approx() of this result would add information (e.g. √2, π/4, ln 3)."""
    t = text.replace("−", "-")
    if re.fullmatch(r"-?\d+(\.\d*)?", t) or t in ("true", "false", "undef"):
        return False
    if not re.search(r"[√πℯ/]|ln|log|sin|cos|tan|arc|\^", t):
        return False
    # drop "x=" style left-hand sides of solutions, then function names and connectives
    stripped = re.sub(r"(^|\bor\b|\band\b)\s*[A-Za-z]\w*\s*[=<>≤≥]", " ", t)
    stripped = re.sub(r"\b(ln|log|sin|cos|tan|arcsin|arccos|arctan|sinh|cosh|tanh|root|abs|or|and)\b",
                      "", stripped)
    return not re.search(r"[A-Za-zθαβλ]", stripped)


# ================================================================ run

def solve_latex(bridge, latex: str, action: str = "auto", var: str | None = None,
                order: int | None = None, point: str | None = None, timeout_s: float = 10) -> dict:
    """Translate, evaluate on the CAS and convert back. Call inside bridge.session()."""
    from nspire_bridge import readable
    p = plan(latex, action, var, order, point)
    raw = bridge.eval_one(p.command, timeout_s, raw=True)
    out = {
        "action": p.description,
        "nspire": readable(p.command),
        "result": readable(raw) or ("Done" if p.action == "define" else ""),
        "result_latex": result_to_latex(raw),
    }
    if p.action not in ("approx", "nsolve", "define") and not raw.startswith("[") and is_exact_numeric(readable(raw)):
        try:
            ar = bridge.eval_one(f"approx({p.command})", timeout_s, raw=True)
            if ar != raw:
                out["approx"] = readable(ar)
                out["approx_latex"] = result_to_latex(ar)
        except Exception:
            pass
    return out
