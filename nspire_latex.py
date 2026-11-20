"""LaTeX -> TI-Nspire command, and TI-Nspire result -> LaTeX.

parse:   LaTeX --(preprocess, sympy antlr parser, cleanup)--> sympy trees
plan:    sympy trees + action --> one TI-Nspire command string
print:   NspirePrinter writes sympy trees in TI-Nspire syntax
back:    result_to_latex() parses CAS output with sympy and prints LaTeX
run:     run_plan() evaluates a plan inside bridge.session(); format_answer()
         converts the results to LaTeX afterwards

Trees are kept unevaluated in both directions: SymPy only reads and writes
the math, the CAS does all of it. 0.1+0.2 is sent as written, not as
0.30000000000000004, and a factored CAS result stays factored in LaTeX.
"""
from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field

import sympy as sp
from mpmath.libmp import prec_to_dps, to_str as mpf_to_str
from sympy.core.function import AppliedUndef
from sympy.core.relational import Relational
from sympy.parsing.latex import parse_latex
from sympy.parsing.sympy_parser import auto_number, auto_symbol, factorial_notation, parse_expr
from sympy.printing.latex import LatexPrinter
from sympy.printing.str import StrPrinter

from nspire_chars import NS_C, NS_E, NS_EXP, NS_I, NS_N, readable


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


_NUM_BEFORE = re.compile(r"(?<![\d.,])\d+$")
_NUM_AFTER = re.compile(r"\d+(?![\d,]|\.\d)")


def _decimal_commas(s: str) -> str:
    """Finnish/European decimal comma: 1,5 -> 1.5.

    Only where a comma can't separate arguments: at top level and directly
    inside a {...} group (\\frac{1,5}{2}, x^{0,5}). Inside (...), [...] and
    \\{...\\} it separates function arguments, coordinates, interval ends or
    list items, so f(1,5), \\gcd(12,18), (1,5] and \\{1,2\\} stay as they are.
    Runs such as 1,2,3 are lists, not decimals.
    """
    out: list[str] = []
    stack: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            pair = s[i:i + 2]
            if pair == r"\{":
                stack.append(pair)
            elif pair == r"\}" and stack:
                stack.pop()
            out.append(pair)  # also keeps \\ (row break) and \, in one piece
            i += 2
            continue
        if c in "([{":
            stack.append(c)
        elif c in ")]}" and stack:
            stack.pop()
        elif c == "," and (not stack or stack[-1] == "{") \
                and _NUM_BEFORE.search(s, 0, i) and _NUM_AFTER.match(s, i + 1):
            c = "."
        out.append(c)
        i += 1
    return "".join(out)


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
        s = _decimal_commas(s)  # before matrices, so cells such as 1,5 are converted too
        s = self._matrices(s)
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


_antlr_imported = False


def _parse_one(src: str) -> sp.Basic:
    global _antlr_imported
    if not _antlr_imported:
        # Import the parser with evaluation on: importing it builds
        # sympy.physics.units, which fails inside evaluate(False).
        try:
            importlib.import_module("sympy.parsing.latex._parse_latex_antlr")
        except Exception:
            pass  # parse_latex below reports what's missing
        _antlr_imported = True
    try:
        # evaluate(False) keeps what the parser builds as written: x=x stays an
        # equation, -(x-1) isn't distributed, binomial(5,2) isn't computed
        with sp.evaluate(False):
            return parse_latex(src, backend="antlr")
    except Exception as e:
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise LatexError(f"Couldn't parse LaTeX {src!r}: {first}") from None


def _flat(e: sp.Basic) -> list:
    """Arguments of an Add/Mul with nested ones of the same kind spliced in."""
    out = []
    for a in e.args:
        out.extend(_flat(a) if a.func is e.func else [a])
    return out


def _flatten(func, args) -> list:
    out = []
    for a in args:
        out.extend(_flat(a) if a.func is func else [a])
    return out


def _mul(args) -> sp.Basic:
    """Unevaluated product with the parser's artifacts removed: subtraction's
    -1*5*x becomes -5*x and the 1 in 1*k goes. Nothing else is multiplied."""
    fs = _flatten(sp.Mul, args)
    while len(fs) > 1 and fs[0] == sp.S.NegativeOne and fs[1].is_Number:
        fs = [-fs[1]] + fs[2:]
    fs = [f for f in fs if f != sp.S.One] or [sp.S.One]
    return fs[0] if len(fs) == 1 else sp.Mul(*fs, evaluate=False)


def _matrix_op(func, args) -> sp.Basic:
    """Matrix arithmetic stays symbolic too (MatAdd/MatMul/MatPow), for the CAS to do."""
    try:
        if func is sp.Add:
            return sp.MatAdd(*_flatten(sp.MatAdd, args))
        if func is sp.Mul:
            fs = [f for f in _flatten(sp.MatMul, args) if f != sp.S.One]
            return fs[0] if len(fs) == 1 else sp.MatMul(*fs)
        return sp.MatPow(*args)
    except Exception as e:
        raise LatexError(f"Couldn't combine the matrices: {e}") from None


def _is_matrix(e) -> bool:
    return isinstance(e, (sp.MatrixBase, sp.MatrixExpr))


def _bound_symbols(e: sp.Basic) -> set[str]:
    names = set()
    for node in sp.preorder_traversal(e):
        if isinstance(node, (sp.Integral, sp.Sum, sp.Product)):
            names |= {str(lim[0]) for lim in node.limits}
        elif isinstance(node, sp.Limit):
            names.add(str(node.args[1]))
    return names


def _ode_indep(raw, dep: str, all_syms: set[str]) -> str:
    """Independent variable of an ODE whose LaTeX doesn't name it (no dy/dx)."""
    dep_fn = re.compile(re.escape(dep) + "'*")
    for e in raw:  # written as an argument: y'(t) = y(t)
        for node in sp.preorder_traversal(e):
            if isinstance(node, AppliedUndef) and dep_fn.fullmatch(node.func.__name__) \
                    and len(node.args) == 1 and node.args[0].is_Symbol and node.args[0].name != dep:
                return node.args[0].name
    others = {s for s in all_syms if s.rstrip("'") != dep}
    if "x" in others:
        return "x"
    if "t" in others:
        return "t"
    return "t" if dep == "x" else "x"  # x' = 3 is x(t), never deSolve(...,x,x)


def parse(latex: str) -> Parsed:
    pre = _Pre()
    pieces = pre.run(latex)
    if not pieces:
        raise LatexError("The LaTeX is empty.")
    raw = [_parse_one(p) for p in pieces]

    # matrix placeholders: parse their cells (cleaned up by walk() below)
    ph_values: dict[str, tuple[str, sp.ImmutableMatrix]] = {}
    for name, (kind, payload) in pre.placeholders.items():
        if kind in ("matrix", "det"):
            ph_values[name] = (kind, sp.ImmutableMatrix([[_parse_one(c) for c in row] for row in payload]))

    prime_names = set()
    for e in raw:
        for node in sp.preorder_traversal(e):
            nm = getattr(node, "name", None) or (node.func.__name__ if isinstance(node, AppliedUndef) else None)
            if nm and re.fullmatch(r"[A-Za-z]'+", nm):
                prime_names.add(nm)
    # primes make an ODE only alongside an equation; a bare f'(x) is a derivative
    has_rel = any(isinstance(e, Relational) for e in raw)
    ode_dep = sorted(prime_names)[0][0] if prime_names and has_rel else None
    all_syms = {s.name for e in raw for s in e.free_symbols}

    def fix(e):
        bound = _bound_symbols(e)

        def walk(node):
            if isinstance(node, sp.Symbol):
                n = node.name
                if n in ph_values:
                    kind, m = ph_values[n]
                    m = m.applyfunc(walk)
                    return sp.Function("det")(m) if kind == "det" else m
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
                    return _mul([sym, args[0]])
                return sp.Function(n)(*args)
            if not node.args:
                return node
            new = [walk(a) for a in node.args]
            if node.func in (sp.Add, sp.Mul, sp.Pow):
                # Rebuilt unevaluated: the CAS does the arithmetic, not SymPy
                # (0.1+0.2, 1/3+0.5, e^(i*pi) and √(8) reach it as written)
                if any(_is_matrix(a) for a in new):
                    return _matrix_op(node.func, new)
                if node.func is sp.Mul:
                    return _mul(new)
                if node.func is sp.Add:
                    return sp.Add(*_flatten(sp.Add, new), evaluate=False)
                return sp.Pow(*new, evaluate=False)
            try:
                return node.func(*new, evaluate=False)
            except TypeError:
                return node.func(*new)
        return walk(e)

    exprs = [fix(e) for e in raw]
    indep = pre.ode_indep
    if ode_dep and not indep:
        indep = _ode_indep(raw, ode_dep, all_syms)
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
        # The digits as written (0.1, not 0.1000000000000000055...), in positional
        # notation: LaTeX decimals are always written that way, and TI-Nspire's
        # own exponent mark isn't an ASCII e.
        dps = max(1, prec_to_dps(e._prec))
        return mpf_to_str(e._mpf_, dps, strip_zeros=True, min_fixed=-10**6, max_fixed=10**6)

    def _print_Rational(self, e):
        return f"{e.p}/{e.q}" if e.q != 1 else str(e.p)

    # operators. Trees are unevaluated (see parse), so these print args in the
    # order written and add only the parentheses TI-Nspire needs. Precedence,
    # loosest first: relation, + -, * /, negation, ^, atoms.
    ATOM, POW, NEG, MUL, ADD, REL = 100, 30, 15, 20, 10, 1

    @staticmethod
    def _inverse_int(x) -> int | None:
        """n if the exponent x is 1/n (as a Rational or the parser's unevaluated 3^-1)."""
        if x.is_Rational and x.p == 1 and x.q > 1:
            return int(x.q)
        if isinstance(x, sp.Pow) and x.exp == sp.S.NegativeOne and x.base.is_Integer and x.base > 1:
            return int(x.base)
        return None

    def _prec(self, e) -> int:
        if isinstance(e, Relational):
            return self.REL
        if isinstance(e, (sp.Add, sp.MatAdd)):
            return self.ADD
        if isinstance(e, (sp.Mul, sp.MatMul)):
            return self.MUL
        if isinstance(e, sp.Number):
            if e.is_extended_negative:
                return self.NEG
            return self.MUL if e.is_Rational and not e.is_Integer else self.ATOM
        if isinstance(e, sp.Pow):
            if self._inverse_int(e.exp):
                return self.ATOM  # √(b), root(b,n)
            if e.exp in (sp.S.NegativeOne, -sp.S.Half):
                return self.MUL   # 1/b
            return self.POW
        if isinstance(e, sp.MatPow):
            return self.POW
        if isinstance(e, sp.factorial):
            return self.POW + 1
        return self.ATOM

    def _paren(self, e, need: int) -> str:
        s = self._print(e)
        return f"({s})" if self._prec(e) < need else s

    def _print_Add(self, e, order=None):
        out = ""
        for i, t in enumerate(_flat(e)):
            s = self._paren(t, self.ADD)
            if i == 0:
                out = s
            elif s.startswith("-"):
                out += " - " + s[1:]
            else:
                out += " + " + s
        return out

    def _print_Mul(self, e):
        matrix = isinstance(e, sp.MatMul)  # keep factor order, no denominators
        neg, num, den = False, [], []
        for f in _flat(e):
            if f == sp.S.NegativeOne:
                neg = not neg
                continue
            if f == sp.S.One:
                continue
            if not num and not den and isinstance(f, sp.Number) and f.is_extended_negative:
                neg, f = not neg, -f
            if not matrix and isinstance(f, sp.Rational) and not f.is_Integer:
                if f.p != 1:
                    num.append(sp.Integer(f.p))
                den.append(sp.Integer(f.q))
                continue
            if not matrix and isinstance(f, sp.Pow) and f.exp.is_Number and f.exp.is_extended_negative:
                k = -f.exp
                den.append(f.base if k == 1 else sp.Pow(f.base, k, evaluate=False))
                continue
            num.append(f)

        def factors(fs):
            out = []
            for f in fs:
                s = self._print(f)
                if self._prec(f) < self.MUL or (out and s.startswith("-")):
                    s = f"({s})"
                out.append(s)
            return "*".join(out)

        s = factors(num) or "1"
        if len(den) == 1:
            s += "/" + self._paren(den[0], self.POW)
        elif den:
            s += f"/({factors(den)})"
        return ("-" if neg else "") + s

    _print_MatAdd = _print_Add
    _print_MatMul = _print_Mul

    def _print_Pow(self, e, rational=False):
        b, x = e.args
        if _is_matrix(b):
            return f"{self._paren(b, self.ATOM)}^({self._print(x)})"
        n = self._inverse_int(x)
        if n == 2:
            return f"√({self._print(b)})"
        if n:
            return f"root({self._print(b)},{n})"
        if x == -sp.S.Half:
            return f"1/√({self._print(b)})"
        if x == sp.S.NegativeOne:
            return f"1/{self._paren(b, self.POW)}"
        if b == sp.E:
            return f"{NS_E}^({self._print(x)})"
        exp = self._print(x) if (x.is_Symbol or (x.is_Integer and x >= 0)) else f"({self._print(x)})"
        return f"{self._paren(b, self.ATOM)}^{exp}"

    _print_MatPow = _print_Pow

    def _print_Relational(self, e):
        op = {"==": "=", "!=": "≠", "<=": "≤", ">=": "≥", "<": "<", ">": ">"}[e.rel_op]
        return f"{self._print(e.lhs)}{op}{self._print(e.rhs)}"

    def _print_factorial(self, e):
        return f"{self._paren(e.args[0], self.ATOM)}!"

    def _print_log(self, e):
        if len(e.args) == 1 or e.args[1] == sp.E:
            return f"ln({self._print(e.args[0])})"
        if e.args[1] == 10:
            return f"log({self._print(e.args[0])})"
        return f"log({self._print(e.args[0])},{self._print(e.args[1])})"

    def _print_Function(self, e):
        n = e.func.__name__
        if isinstance(e, AppliedUndef) and self.ode_dep and len(e.args) == 1 \
                and str(e.args[0]) == self.ode_indep and re.fullmatch(re.escape(self.ode_dep) + "'*", n):
            # y(x) and y'(x) are written y and y' in deSolve; y'(0)=1 keeps its point
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
    rels = [e for e in exprs if isinstance(e, Relational)]
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
        body = " and ".join(to_nspire(e if isinstance(e, Relational) else sp.Eq(e, 0, evaluate=False), p)
                            for e in exprs)
        vtxt = "{" + ",".join(vs) + "}"
        return Plan(tmpl.format(e=body, v=vtxt), action, f"{desc} for {', '.join(vs)}", vs, p)

    if len(exprs) > 1:
        body = "{" + ",".join(to_nspire(e, p) for e in exprs) + "}"
    else:
        e = exprs[0]
        if action in ("solve", "csolve", "nsolve") and not isinstance(e, Relational):
            e = sp.Eq(e, 0, evaluate=False)
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

def _log(x, b=10, **kw):
    if b == sp.E:
        return sp.log(x, **kw)
    return sp.Function(f"log_{{{sp.latex(b)}}}")(x)


def _root(x, n, **kw):
    k = sp.Rational(1, n) if n.is_Integer and n != 0 else sp.Pow(n, -1, evaluate=False)
    return sp.Pow(x, k, evaluate=False)  # shown as \sqrt[n]{x}


# Functions the CAS prints, as SymPy classes that only display them (parsing
# runs with evaluation off). Any other called name, such as limit or
# derivative in an unevaluated result, becomes an undefined function and is
# shown, not recomputed.
_OUT_FN = {"sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "cot": sp.cot, "sec": sp.sec, "csc": sp.csc,
           "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh, "coth": sp.coth, "sech": sp.sech,
           "csch": sp.csch, "arcsin": sp.asin, "arccos": sp.acos, "arctan": sp.atan, "arccot": sp.acot,
           "arcsec": sp.asec, "arccsc": sp.acsc, "arcsinh": sp.asinh, "arccosh": sp.acosh,
           "arctanh": sp.atanh, "ln": sp.log, "log": _log, "exp": sp.exp, "sqrt": sp.sqrt, "root": _root,
           "abs": sp.Abs, "floor": sp.floor, "ceiling": sp.ceiling, "sign": sp.sign, "nCr": sp.binomial,
           "real": sp.re, "imag": sp.im, "conj": sp.conjugate, "max": sp.Max, "min": sp.Min,
           "factorial": sp.factorial, "Matrix": sp.ImmutableMatrix}
_OUT_CONST = {"E": sp.E, "I": sp.I, "pi": sp.pi, "oo": sp.oo}
# What parse_expr's generated code may call. No builtins: parse_expr evals.
_PARSE_GLOBALS = {"__builtins__": {}, "Symbol": sp.Symbol, "Function": sp.Function, "Integer": sp.Integer,
                  "Float": sp.Float, "Rational": sp.Rational, "Add": sp.Add, "Mul": sp.Mul, "Pow": sp.Pow,
                  "factorial": sp.factorial}
_PARSE_TRANSFORMS = (auto_symbol, auto_number, factorial_notation)
_UNSAFE = re.compile(r"__|['\"\\;:@`]|[A-Za-z_)\]]\s*\.\s*[A-Za-z_]")  # attribute access, strings, ...
_REL_TEX = {"<=": r"\leq", ">=": r"\geq", "!=": r"\neq", "=": "=", "<": "<", ">": ">"}
_INV_TRIG = re.compile(r"\b((?:sin|cos|tan|cot|sec|csc)h?)⁻¹\(")


def _norm_result(text: str) -> str:
    t = text.replace("−", "-").replace("ℯ", NS_E).replace("𝑖", NS_I).replace("ᴇ", NS_EXP)
    t = re.sub(NS_C + r"(\d+)", r"c_\1", t)
    t = re.sub(NS_N + r"(\d+)", r"n_\1", t)
    t = t.replace(NS_E, "E").replace(NS_I, "I").replace(NS_EXP, "e")
    t = _INV_TRIG.sub(r"arc\1(", t)  # tan⁻¹(x) -> arctan(x)
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


class _ResultLatex(LatexPrinter):
    """LaTeX for unevaluated CAS results: the CAS's term order and form, with
    the signs of the parser's -1*x factors written as subtraction."""

    def __init__(self):
        super().__init__({"ln_notation": True, "order": "none", "inv_trig_style": "full"})

    @staticmethod
    def _split_sign(e) -> tuple[bool, sp.Basic]:
        """(negative?, e without its sign), dropping 1 and -1 factors."""
        if isinstance(e, sp.Number) and e.is_extended_negative:
            return True, -e
        if not isinstance(e, sp.Mul):
            return False, e
        neg, rest = False, []
        for f in _flat(e):
            if f == sp.S.NegativeOne:
                neg = not neg
            elif f != sp.S.One:
                rest.append(f)
        if rest and isinstance(rest[0], sp.Number) and rest[0].is_extended_negative:
            neg, rest[0] = not neg, -rest[0]
        if not rest:
            return neg, sp.S.One
        return neg, rest[0] if len(rest) == 1 else sp.Mul(*rest, evaluate=False)

    def _signed(self, e) -> tuple[bool, str]:
        neg, rest = self._split_sign(e)
        if isinstance(rest, sp.Mul):
            s = super()._print_Mul(rest)
        else:
            s = self._print(rest)
            if isinstance(rest, sp.Add):
                s = r"\left(" + s + r"\right)"
        return neg, s

    def _print_Add(self, expr, order=None):
        tex = ""
        for i, term in enumerate(_flat(expr)):
            neg, s = self._signed(term)
            if i == 0:
                tex = ("- " if neg else "") + s
            else:
                tex += (" - " if neg else " + ") + s
        return tex

    def _print_Mul(self, expr):
        if not isinstance(expr, sp.Mul):  # LatexPrinter._print_Pow sends 1/x here
            return super()._print_Mul(expr)
        neg, s = self._signed(expr)
        return ("- " if neg else "") + s


def _expr_latex(s: str, ld: dict) -> str:
    with sp.evaluate(False):
        e = parse_expr(s, local_dict=dict(ld), global_dict=dict(_PARSE_GLOBALS),
                       transformations=_PARSE_TRANSFORMS, evaluate=False)
    return _ResultLatex().doprint(e)


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
        if _UNSAFE.search(norm) or any(ord(c) > 127 and not c.isalpha() for c in norm):
            return None  # e.g. an unevaluated ∫(...) the CAS hands back
        ld: dict = {}
        for m in re.finditer(r"[A-Za-z_][A-Za-z_0-9]*", norm):
            n = m.group()
            if n in ld:
                continue
            called = norm[m.end():].lstrip().startswith("(")
            if called:
                ld[n] = _OUT_FN.get(n) or sp.Function(n)  # sin(x), or f(x) / limit(...) as written
            else:
                ld[n] = _OUT_CONST.get(n) or sp.Symbol(n)
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

@dataclass
class CasRun:
    plan: Plan
    raw: str                       # CAS reply, private-use characters kept
    approx_raw: str | None = None  # approx() of an exact numeric answer, if it differs


def run_plan(bridge, p: Plan, timeout_s: float = 10) -> CasRun:
    """Evaluate a plan on the CAS, plus approx() of an exact numeric answer.

    Call inside bridge.session(). Only CAS round trips happen here, so the
    user's clipboard is held no longer than needed; convert with
    format_answer() after the session.
    """
    raw = bridge.eval_one(p.command, timeout_s, raw=True)
    run = CasRun(p, raw)
    if p.action not in ("approx", "nsolve", "define") and not raw.startswith("[") and is_exact_numeric(readable(raw)):
        try:
            ar = bridge.eval_one(f"approx({p.command})", timeout_s, raw=True)
            if ar != raw:
                run.approx_raw = ar
        except Exception:
            pass
    return run


def format_answer(run: CasRun) -> dict:
    """{action, nspire, result, result_latex, approx?, approx_latex?} for a CasRun. No bridge use."""
    p, raw = run.plan, run.raw
    out = {
        "action": p.description,
        "nspire": readable(p.command),
        "result": readable(raw) or ("Done" if p.action == "define" else ""),
        "result_latex": result_to_latex(raw),
    }
    if run.approx_raw is not None:
        out["approx"] = readable(run.approx_raw)
        out["approx_latex"] = result_to_latex(run.approx_raw)
    return out


def solve_latex(bridge, latex: str, action: str = "auto", var: str | None = None,
                order: int | None = None, point: str | None = None, timeout_s: float = 10) -> dict:
    """plan(), then run_plan() in a bridge.session() of its own, then format_answer().

    Opens the session itself: don't call it inside bridge.session() (the
    session lock isn't reentrant). Inside one, use run_plan() instead.
    """
    p = plan(latex, action, var, order, point)
    run = bridge.session(lambda: run_plan(bridge, p, timeout_s))
    return format_answer(run)
