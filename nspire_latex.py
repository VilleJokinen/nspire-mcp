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

import contextlib
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


class TiList(sp.Function):
    """A TI-Nspire list {a,b,...} (from \\{a,b\\}); never evaluated."""


class TiVec(sp.Function):
    """A vector name written \\vec{u}: printed as plain u, but dotP/crossP know it is a vector."""
    nargs = 1


class TiUnit(sp.Symbol):
    """A TI-Nspire unit such as _m (from 5\\,\\mathrm{m}); not a variable to solve for."""


# ================================================================ preprocess

_PH = "Omega"  # placeholder symbols are \Omega_{9NN}; restored after parsing

_DROP = [r"\displaystyle", r"\textstyle", r"\limits", r"\nolimits", r"\middle",
         r"\bigl", r"\bigr", r"\Bigl", r"\Bigr", r"\biggl", r"\biggr", r"\big", r"\Big", r"\bigg", r"\Bigg"]
_UNICODE = {"−": "-", "·": r"\cdot ", "×": r"\times ", "÷": r"\div ", "≤": r"\leq ", "≥": r"\geq ",
            "≠": r"\neq ", "π": r"\pi ", "∞": r"\infty ", "√": r"\sqrt", "°": r"^{\circ}",
            "θ": r"\theta ", "α": r"\alpha ", "β": r"\beta ", "λ": r"\lambda ", "ω": r"\omega "}
# Pure styling, unwrapped. Accents that make a different variable (\bar x, \hat x,
# \tilde x) are not here: they become names of their own (xbar, xhat, xtilde). \vec
# isn't either: \vec{u}\cdot\vec{v} is dotP(u,v), not u*v (see _Pre._vectors).
_STYLE_WRAPPERS = ["mathbf", "boldsymbol", "mathit", "mathsf", "mathtt", "underline",
                   "mathbb", "mathcal", "bm"]
# Units written \mathrm{...} or \text{...} right after a number, and their TI-Nspire
# names (TI-Nspire guide, Appendix A "Conversion Categories and Units"; gram is _gm,
# hour _hr, liter _l). Any other name in that position is refused, not read as a variable.
_UNITS = {
    "m": "_m", "cm": "_cm", "mm": "_mm", "km": "_km", "dm": "_dm", "nm": "_nm",
    "s": "_s", "ms": "_ms", "ns": "_ns", "min": "_min", "h": "_hr", "hr": "_hr", "yr": "_yr",
    "kg": "_kg", "g": "_gm", "mg": "_mg",
    "l": "_l", "L": "_l", "ml": "_ml", "mL": "_ml",
    "N": "_N", "J": "_J", "kJ": "_kJ", "eV": "_eV", "cal": "_cal", "kcal": "_kcal", "kWh": "_kWh",
    "W": "_W", "kW": "_kW",
    "Pa": "_Pa", "kPa": "_kPa", "bar": "_bar", "mbar": "_mbar", "atm": "_atm",
    "K": "_K",
}
_UNIT_WRAPPERS = r"\\(?:mathrm|text|textrm|textnormal|mbox)\s*\{"
_TEXT_WORDS = {"and", "ja", "or", "tai"}  # \text{...} connectives handled by _Pre._text
_ACCENTS = {"bar": "bar", "overline": "bar", "hat": "hat", "widehat": "hat", "tilde": "tilde",
            "widetilde": "tilde"}
_MATRIX_ENVS = "pmatrix|bmatrix|Bmatrix|vmatrix|Vmatrix|matrix|smallmatrix"
# \operatorname / \mathrm names passed on as commands: sympy's parser knows them, or
# they are in _FUNCLIKE below
_KNOWN_CMDS = {"sin", "cos", "tan", "cot", "sec", "csc", "sinh", "cosh", "tanh", "arcsin", "arccos",
               "arctan", "ln", "log", "exp", "max", "min", "gcd", "lcm", "det", "arg", "Re", "Im",
               "sgn", "sign", "coth", "sech", "csch"}
# Commands the parser reads as a plain name: they are functions only when called with
# parentheses (\max(1,2), \Re(z)); without them \Re z would silently become Re*z.
_FUNCLIKE = {"max", "min", "gcd", "lcm", "det", "arg", "Re", "Im", "sgn", "sign", "coth", "sech", "csch"}
# Names that are TI-Nspire or LaTeX functions: as a bare \operatorname{...} they'd be a variable.
_FUNC_WORDS = {n.lower() for n in _FUNCLIKE} | {"mod", "real", "imag", "conj", "angle", "abs", "floor",
                                               "ceiling", "ceil", "exp", "ln", "log"}
_GREEK_CMDS = {"alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta", "theta", "vartheta",
               "iota", "kappa", "varkappa", "lambda", "mu", "nu", "xi", "rho", "varrho", "sigma", "varsigma",
               "tau", "upsilon", "phi", "varphi", "chi", "psi", "omega", "Gamma", "Delta", "Theta", "Lambda",
               "Xi", "Sigma", "Upsilon", "Phi", "Psi", "Omega", "pi", "ell"}
# Every command that may reach SymPy's parser. Any other one is refused: the parser
# would read \foo as a variable named foo and multiply it in.
_PARSER_CMDS = _GREEK_CMDS | _FUNCLIKE | {
    "frac", "binom", "sqrt", "int", "sum", "prod", "lim", "to", "rightarrow", "Rightarrow", "longrightarrow",
    "Longrightarrow", "exp", "log", "ln", "sin", "cos", "tan", "csc", "sec", "cot", "arcsin", "arccos",
    "arctan", "arccsc", "arcsec", "arccot", "sinh", "cosh", "tanh", "arsinh", "arcosh", "artanh", "lfloor",
    "rfloor", "lceil", "rceil", "times", "cdot", "div", "neq", "leq", "le", "leqq", "geq", "ge", "geqq",
    "infty", "partial"}
# \text{for x>0} and the like: what follows is a condition (TI-Nspire's "|")
_CONDITION_WORDS = {"for", "where", "when", "if", "with", "given", "kun", "jos", "missä"}
_RELATION = re.compile(r"(?<![<>!\\])=|<|>|\\leq?\b|\\geq?\b|\\neq\b|\\ne\b|\\leqslant|\\geqslant")
# Markers left in the LaTeX between preprocessing steps (letters only, so they pass as commands)
_WITH = r"\nspireWith"        # a condition follows
_EVBAR = r"\nspireEvBar"      # an evaluation bar, + id letters
_EVFROM = r"\nspireEvFrom"    # where the \left. of that bar was, + the same id
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
    constraints: list = field(default_factory=list)  # conditions from \text{for ...}, joined with "|"
    assign: bool = False          # written with :=, e.g. a:=5


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


_TOKEN = re.compile(r"\\[A-Za-z]+|\\.|.", re.S)
_OPEN, _CLOSE = {"(", "[", "{", r"\{"}, {")", "]", "}", r"\}"}
_REL_TOKENS = {"=", "<", ">", r"\leq", r"\le", r"\geq", r"\ge", r"\neq", r"\ne", r"\leqq", r"\geqq",
               r"\lt", r"\gt", _WITH}
_SEP_TOKENS = {",", ";", "&", "\\\\"}


def _tokens(s: str) -> list[tuple[int, int, str]]:
    return [(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(s)]


def _operand_start(s: str, pos: int, stops: set[str]) -> int:
    """Start of the operand that ends at pos: back to a stop token or an unmatched bracket."""
    depth = 0
    for _a, b, t in reversed([tk for tk in _tokens(s) if tk[1] <= pos]):
        if t in _CLOSE:
            depth += 1
        elif t in _OPEN:
            if depth == 0:
                return b
            depth -= 1
        elif depth == 0 and t in stops:
            return b
    return 0


def _operand_end(s: str, pos: int, stops: set[str]) -> int:
    """End of the operand that starts at pos (a leading sign belongs to it)."""
    depth, first = 0, True
    for a, _b, t in [tk for tk in _tokens(s) if tk[0] >= pos]:
        if t.isspace():
            continue
        if first and t in "+-":
            first = False
            continue
        first = False
        if t in _OPEN:
            depth += 1
        elif t in _CLOSE:
            if depth == 0:
                return a
            depth -= 1
        elif depth == 0 and t in stops:
            return a
    return len(s)


def _id_letters(n: int) -> str:
    out = ""
    while True:
        out = chr(ord("a") + n % 26) + out
        n = n // 26 - 1
        if n < 0:
            return out


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
        self.constraints: list[str] = []  # LaTeX of the conditions after \text{for ...}
        self.assign = False                # the input used :=
        self.subnames: dict[str, str] = {}  # Omega_{8NN} -> subscripted name such as x_n1

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
        s = re.sub(r"\\(begin|end)\{(equation|align|aligned|alignedat|gather|gathered|multline|eqnarray|split)"
                   r"\*?\}(\{\d+\})?", "", s)
        s = re.sub(r"\\(label|tag)\{[^}]*\}", "", s)
        s = s.replace("&", " ") if not re.search(r"\\begin\{(" + _MATRIX_ENVS + r"|array|cases)\}", s) else s
        s = self._assignment(s)
        s = self._mark_eval_bars(s)  # before \left, \right and \Big are dropped
        for d in _DROP:
            s = s.replace(d, " ")
        s = re.sub(r"\\left\s*\.|\\right\s*\.", " ", s)
        s = re.sub(r"\\(left|right)\b\s*", "", s)  # keep the delimiter that follows
        s = s.replace(r"\lbrace", r"\{").replace(r"\rbrace", r"\}").replace(r"\lbrack", "[").replace(r"\rbrack", "]")
        s = re.sub(r"\\[lr]?vert(?![A-Za-z])", "|", s)
        s = re.sub(r"\\q?quad\b", " ", s)
        s = re.sub(r"\\(?:thin|med|thick|negthin|negmed|negthick)space(?![A-Za-z])", " ", s)
        s = re.sub(r"(?<!\\)\\[,;:! ]", " ", s)  # not the second half of a \\ row break
        s = s.replace("~", " ")
        s = s.replace(r"\dfrac", r"\frac").replace(r"\tfrac", r"\frac").replace(r"\dbinom", r"\binom")
        s = s.replace(r"\tbinom", r"\binom")
        s = s.replace(r"\iiint", r"\int\int\int").replace(r"\iint", r"\int\int")
        s = s.replace(r"\leqslant", r"\leq").replace(r"\geqslant", r"\geq")
        s = re.sub(r"\\ne(?![A-Za-z])", r"\\neq ", s)  # the parser knows only \neq
        s = re.sub(r"\\lt(?![A-Za-z])", "<", s)
        s = re.sub(r"\\gt(?![A-Za-z])", ">", s)
        s = re.sub(r"\\arc(sinh|cosh|tanh)(?![A-Za-z])", r"\\ar\1", s)  # the parser's names: \arsinh ...
        s = re.sub(r"\\(land|wedge)\b", r" \\\\ ", s)
        if r"\pm" in s or r"\mp" in s:
            raise LatexError("± isn't supported; write the two cases separately or ask to solve the equation.")
        s = self._accents(s)
        s = self._vectors(s)
        for w in _STYLE_WRAPPERS:
            s = _unwrap_cmd(s, w, lambda a: "{" + a + "}")
        s = self._units(s)  # before \text and \mathrm are unwrapped
        for t in ("text", "textrm", "textit", "textbf", "textnormal", "mbox"):
            s = _unwrap_cmd(s, t, self._text)
        s = _unwrap_cmd(s, r"mathrm", self._name)
        s = _unwrap_cmd(s, r"operatorname\*?", self._name)
        # degrees: 30^\circ -> 30*pi/180
        s = re.sub(r"\^\s*\{?\s*\\circ\s*\}?", r"\\cdot\\frac{\\pi}{180}", s)
        # log conventions: \log x is base 10 (as on the calculator), \lg too
        s = re.sub(r"\\lg\b", r"\\log_{10}", s)
        s = re.sub(r"\\log\b(?!\s*_)", r"\\log_{10}", s)
        s = self._derivatives(s)
        s = self._subscripts(s)  # before decimal commas: x_{1,2} is refused, not x_{1.2}
        # A script without braces is one character, as LaTeX renders it: \sum_{k=1}^5 2k has
        # the upper limit 5 (the parser would read ^52), x^12 is x^1*2
        s = re.sub(r"([\^_])\s*(\d)", r"\1{\2}", s)
        s = _decimal_commas(s)  # before matrices, so cells such as 1,5 are converted too
        s = self._det(s)
        s = self._matrices(s)
        s = self._vector_ops(s)
        s = self._lists(s)
        s = self._bmod(s)
        s = self._eval_bars(s)
        rows = self._cases(s)
        pieces: list[str] = []
        for r in rows:
            parts = _split_top(r, (";",))
            for p in parts:
                comma = _split_top(p, (",",))
                if len(comma) > 1 and (all(_RELATION.search(c) for c in comma)
                                       or not any(_RELATION.search(c) for c in comma)):
                    pieces.extend(comma)
                else:
                    pieces.append(p)
        pieces = self._split_conditions(pieces)
        pieces = [p.rstrip(".,; ") for p in pieces]
        return [p for p in pieces if p and not re.fullmatch(r"\s*", p)]

    # -- :=

    def _assignment(self, s: str) -> str:
        """a := 5 (or \\coloneqq): remembered, then written as = for the parser."""
        pat = re.compile(r"\s*(?::=|\\coloneqq?(?![A-Za-z]))\s*")
        n = len(pat.findall(s))
        if n > 1:
            raise LatexError("Only one ':=' at a time; send each assignment separately.")
        if n:
            self.assign = True
            s = pat.sub(" = ", s)
        return s

    # -- conditions: \text{for } x>0

    def _text(self, arg: str) -> str:
        w = arg.strip().lower()
        words = w.split()
        if w in ("and", "ja", ",", "&"):
            return r" \\ "
        if not words or w.startswith("and"):
            return " "
        if words[0] in ("or", "tai"):
            raise LatexError("'or' between equations isn't supported; send each case separately.")
        if words[0] in _CONDITION_WORDS:
            return f" {_WITH} "
        return " "

    def _split_conditions(self, pieces: list[str]) -> list[str]:
        """Everything after the first \\text{for/where/...} is a condition, not an equation."""
        out: list[str] = []
        found = False
        for p in pieces:
            parts = p.split(_WITH)
            if not found:
                out.append(parts[0])
                parts = parts[1:]
                found = bool(parts)
            self.constraints.extend(parts)
        if found:
            conds = [c.strip().rstrip(".,; ") for c in self.constraints]
            conds = [c for c in conds if c]
            if not conds:
                raise LatexError("Nothing follows the \\text{for ...}: write the condition, e.g. x>0.")
            for c in conds:
                if not _RELATION.search(c):
                    raise LatexError(f"The condition {c.strip()!r} isn't a comparison such as x>0; "
                                     "write it in TI-Nspire syntax with | and use nspire_eval.")
            self.constraints = conds
        return out

    # -- accents: \bar x is a variable of its own

    def _accents(self, s: str) -> str:
        cmds = "|".join(_ACCENTS)
        s = re.sub(r"\\(" + cmds + r")(?![A-Za-z])\s*(\\[A-Za-z]+|[A-Za-z])", r"\\\1{\2}", s)
        for cmd, suffix in _ACCENTS.items():
            s = _unwrap_cmd(s, cmd, lambda a, c=cmd, x=suffix: self._accented(a, c, x))
        # x^* (conjugate, optimum, ...) is a name of its own too
        star = re.compile(r"(\\[A-Za-z]+|[A-Za-z])\s*\^\s*(?:\{\s*(?:\*|\\ast|\\star)\s*\}|\*|\\ast(?![A-Za-z])"
                          r"|\\star(?![A-Za-z]))")
        return star.sub(lambda m: self._accented(m.group(1), "^*", "star"), s)

    def _accented(self, arg: str, cmd: str, suffix: str) -> str:
        a = arg.strip()
        if re.fullmatch(r"[A-Za-z]", a):
            return self.ph("name", a + suffix)
        m = re.fullmatch(r"\\([A-Za-z]+)", a)
        if m and m.group(1) in _GREEK_CMDS and m.group(1) != "pi":
            return self.ph("name", m.group(1) + suffix)
        what = f"{a}^*" if cmd == "^*" else f"\\{cmd}{{{a}}}"
        raise LatexError(f"{what} isn't supported (only over a single letter); write it in TI-Nspire syntax "
                         "and use nspire_eval.")

    # -- evaluation bars: \left. f \right|_{x=a}

    def _mark_eval_bars(self, s: str) -> str:
        """Mark evaluation bars before \\left, \\right and \\big are dropped.

        A bar is one when it closes a \\left. or, outside any open |...|, is
        followed by limits (_ or ^). The parser itself would substitute into
        only the last factor before the bar (d/dx x^2 |_{x=2} -> derivative(2^2,x)).
        """
        bar = r"(?:\||\\[lr]?vert(?![A-Za-z]))"
        pat = re.compile(r"\\(left|right)\s*(\\[A-Za-z]+|\\[{}|]|[.|()\[\]])"
                         r"|\\(?:big|Big|bigg|Bigg)[lrm]?\s*" + bar + r"|(?<!\\)\||\\[lr]?vert(?![A-Za-z])")
        stack: list[tuple[int, int, str]] = []
        edits: list[tuple[int, int, str]] = []
        parity, count = 0, 0
        for m in pat.finditer(s):
            limits = re.match(r"\s*[_^]", s[m.end():]) is not None
            if m.group(1) == "left":
                stack.append((m.start(), m.end(), m.group(2)))
            elif m.group(1) == "right":
                left = stack.pop() if stack else None
                is_bar = re.fullmatch(bar, m.group(2)) is not None
                if left and left[2] == "." and is_bar and limits:
                    bid = _id_letters(count)
                    count += 1
                    edits += [(left[0], left[1], f" {_EVFROM}{bid} "), (m.start(), m.end(), f" {_EVBAR}{bid} ")]
            elif limits and parity == 0:
                edits.append((m.start(), m.end(), f" {_EVBAR}{_id_letters(count)} "))
                count += 1
            else:
                parity ^= 1
        for a, b, rep in sorted(edits, reverse=True):
            s = s[:a] + rep + s[b:]
        return s

    def _eval_bars(self, s: str) -> str:
        pat = re.compile(re.escape(_EVBAR) + r"([a-z]+)")
        while (m := pat.search(s)):
            lo, hi, end = self._bar_limits(s, m.end())
            fm = re.search(re.escape(_EVFROM) + m.group(1) + r"(?![a-z])", s[:m.start()])
            if fm:
                start, body = fm.start(), s[fm.end():m.start()]
            else:
                start = _operand_start(s, m.start(), _REL_TOKENS | _SEP_TOKENS)
                body = s[start:m.start()]
            if not body.strip():
                raise LatexError("Nothing to evaluate before the bar |_{...}.")
            s = s[:start] + self.ph("evalat", (body, lo, hi)) + s[end:]
        return re.sub(re.escape(_EVFROM) + r"[a-z]+", " ", s)

    @staticmethod
    def _bar_limits(s: str, i: int) -> tuple[str | None, str | None, int]:
        lo = hi = None
        while (m := re.compile(r"\s*([_^])\s*").match(s, i)):
            j = m.end()
            if j < len(s) and s[j] == "{":
                k = _match_brace(s, j)
                val, i = s[j + 1:k], k + 1
            else:
                t = re.compile(r"\\[A-Za-z]+|[A-Za-z0-9]").match(s, j)
                if not t:
                    raise LatexError("Couldn't read the limits after the evaluation bar.")
                val, i = t.group(), t.end()
            if m.group(1) == "_":
                if lo is not None:
                    raise LatexError("The evaluation bar has two lower limits.")
                lo = val
            else:
                if hi is not None:
                    raise LatexError("The evaluation bar has two upper limits.")
                hi = val
        return lo, hi, i

    # -- \det, lists, \bmod

    def _det(self, s: str) -> str:
        """\\det before a matrix or a letter takes it as its argument: \\det A -> \\det(A)."""
        s = re.sub(r"\\det\s*(\\begin\{(" + _MATRIX_ENVS + r")\}.*?\\end\{\2\})", r"\\det(\1)", s, flags=re.S)
        return re.sub(r"\\det\s*(?:([A-Za-z])|\{\s*([A-Za-z])\s*\})(?![A-Za-z])",
                      lambda m: rf"\det({m.group(1) or m.group(2)})", s)

    def _lists(self, s: str) -> str:
        """\\{1,2,3\\} -> a TI-Nspire list. A single item in \\{\\} stays a group."""
        i = 0
        while (j := s.find(r"\{", i)) != -1:
            if j > 0 and s[j - 1] == "\\":  # the {...} after a \\ row break
                i = j + 2
                continue
            depth, k = 0, j
            while k < len(s):
                if s.startswith(r"\{", k):
                    depth += 1
                    k += 2
                    continue
                if s.startswith(r"\}", k):
                    depth -= 1
                    if depth == 0:
                        break
                    k += 2
                    continue
                k += 1
            if k >= len(s) or r"\begin{cases}" in s[j:k]:
                i = j + 2
                continue
            items = _split_top(s[j + 2:k], (",",))
            if len(items) < 2:
                i = j + 2
                continue
            rep = self.ph("list", items)
            s = s[:j] + rep + s[k + 2:]
            i = j + len(rep)
        return s

    def _bmod(self, s: str) -> str:
        """a \\bmod b -> mod(a,b), with \\bmod binding like a product."""
        if re.search(r"\\pmod(?![A-Za-z])", s):
            raise LatexError("\\pmod (congruences) isn't supported; write mod(a,n)=mod(b,n) with nspire_eval.")
        pat = re.compile(r"\\b?mod(?![A-Za-z])")
        right_stops = _REL_TOKENS | _SEP_TOKENS | {"+", "-", r"\cdot", r"\times", "*", "/", r"\div",
                                                   r"\bmod", r"\mod"}
        while (m := pat.search(s)):
            start = _operand_start(s, m.start(), _REL_TOKENS | _SEP_TOKENS | {"+", "-"})
            end = _operand_end(s, m.end(), right_stops)
            a, b = s[start:m.start()].strip(), s[m.end():end].strip()
            if not a or not b:
                raise LatexError("\\bmod needs a number on each side, e.g. 12 \\bmod 5.")
            s = s[:start] + self.ph("name", "mod") + f"({a},{b})" + s[end:]
        return s

    def _derivatives(self, s: str) -> str:
        # \frac{d^n y}{d x^n} / \frac{dy}{dx}  ->  y'' (record x)
        pat = re.compile(r"\\frac\s*\{\s*d\s*(?:\^\s*\{?\s*(\d)\s*\}?)?\s*([A-Za-z])\s*\}"
                         r"\s*\{\s*d\s*([A-Za-z])\s*(?:\^\s*\{?\s*\d\s*\}?)?\s*\}")

        def rep(m):
            n = int(m.group(1) or 1)
            self.ode_indep = self.ode_indep or m.group(3)
            return f"{m.group(2)}{chr(39) * n}"
        s = pat.sub(rep, s)
        # operator form \frac{d^2}{dx^2} f, \frac{\partial^2}{\partial x \partial y} f: the
        # parser knows only \frac{d}{dx}, so repeat that
        op = re.compile(r"\\frac\s*\{\s*(d|\\partial)\s*\^\s*\{?\s*(\d+)\s*\}?\s*\}\s*(?=\{)")
        one = r"(?:d|\\partial)\s*\(?\s*(\\[A-Za-z]+|[A-Za-z])\s*\)?\s*(?:\^\s*\{?\s*(\d+)\s*\}?)?"
        while (m := op.search(s)):
            d, n = m.group(1), int(m.group(2))
            end = _match_brace(s, m.end())
            den = s[m.end() + 1:end]
            parts = re.findall(one, den)
            if not parts or re.sub(one, "", den).strip():
                raise LatexError(f"Couldn't read the derivative \\frac{{{d}^{n}}}{{{den}}}.")
            vs = [v for v, k in parts for _ in range(int(k or 1))]
            if len(vs) != n:
                raise LatexError(f"The orders in \\frac{{{d}^{n}}}{{{den}}} don't add up to {n}.")
            s = s[:m.start()] + "".join(rf"\frac{{{d}}}{{{d} {v}}}" for v in vs) + s[end + 1:]
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

    # -- subscripts: x_1, x_{n1}, \alpha_{ij}

    def _subscripts(self, s: str) -> str:
        """A subscripted variable becomes a name of its own (x_{n1} -> xn1).

        Only letters and digits can be in a subscript: TI-Nspire names hold nothing
        else, so x_{n+1} and x_{n-1} would both lose the sign and collide with x_{n1}.
        The parser would also read the subscript as an expression (x_{1,2} -> x_{1.2},
        x_{ba} -> x_{ab}), so each name is replaced by a placeholder here and
        restored after parsing. Subscripts of \\int, \\sum, \\lim, \\log ... stay.
        """
        toks = _tokens(s)
        edits: list[tuple[int, int, str]] = []
        k = 0
        while k < len(toks):
            if toks[k][2] != "_":
                k += 1
                continue
            j = k - 1
            while j >= 0 and toks[j][2].isspace():
                j -= 1
            base, start = self._sub_base(toks, j) if j >= 0 else (None, 0)
            if base is None:
                k += 1
                continue
            sub, raw, k = self._sub_body(s, toks, k + 1)
            if base == "Omega" and re.fullmatch(r"9\d+", raw.strip()) and f"Omega_{{{raw.strip()}}}" in self.placeholders:
                continue  # one of our own placeholders
            key = f"8{len(self.subnames):02d}"
            self.subnames[f"{_PH}_{{{key}}}"] = f"{base}_{sub}"
            edits.append((start, toks[k - 1][1], rf" \{_PH}_{{{key}}} "))
        for a, b, rep in sorted(edits, reverse=True):
            s = s[:a] + rep + s[b:]
        return s

    @staticmethod
    def _sub_base(toks, j: int) -> tuple[str | None, int]:
        """The variable a subscript belongs to: a letter, a Greek letter, or {x} left by \\mathbf{x}."""
        t = toks[j][2]
        if re.fullmatch(r"[A-Za-z]", t):
            return t, toks[j][0]
        if t.startswith("\\") and t[1:] in _GREEK_CMDS:
            return t[1:], toks[j][0]
        if t == "}":
            inner = [tk for tk in toks[:j] if not tk[2].isspace()]
            if len(inner) >= 2 and inner[-2][2] == "{":
                prev = inner[-3][2] if len(inner) >= 3 else ""
                if prev in ("^", "_") or (prev.startswith("\\") and len(prev) > 2):
                    return None, 0  # an exponent, or a command's argument (\frac{a}{x}_1)
                b = inner[-1][2]
                if re.fullmatch(r"[A-Za-z]", b):
                    return b, inner[-2][0]
                if b.startswith("\\") and b[1:] in _GREEK_CMDS:
                    return b[1:], inner[-2][0]
        return None, 0

    @staticmethod
    def _sub_body(s: str, toks, k: int) -> tuple[str, str, int]:
        """Read the subscript starting at token k: (name part, as written, next token index)."""
        while k < len(toks) and toks[k][2].isspace():
            k += 1
        if k >= len(toks):
            raise LatexError("A subscript _ has nothing after it.")
        if toks[k][2] == "{":
            depth, m = 0, k
            while m < len(toks):
                depth += {"{": 1, "}": -1}.get(toks[m][2], 0)
                if depth == 0:
                    break
                m += 1
            if m >= len(toks):
                raise LatexError("Unbalanced braces in the LaTeX.")
            body, nxt = toks[k + 1:m], m + 1
            raw = s[toks[k][1]:toks[m][0]]
        else:
            body, nxt = [toks[k]], k + 1
            raw = toks[k][2]
        parts = []
        for _, _, t in body:
            if t.isspace():
                continue
            if re.fullmatch(r"[A-Za-z0-9]", t):
                parts.append(t)
            elif t.startswith("\\") and t[1:] in _GREEK_CMDS:
                parts.append(t[1:])
            else:
                what = ("a comma" if t == "," else f"'{t}'")
                raise LatexError(
                    f"The subscript _{{{raw.strip()}}} has {what} in it. Only letters and digits work in a "
                    "subscript (x_1, x_{n1}, a_{ij}), because a TI-Nspire name can hold nothing else: "
                    "x_{n+1} would become xn1. Rename it (x_{n1}), or write the problem in TI-Nspire "
                    "syntax, e.g. with a function x(n+1), and use nspire_eval.")
        if not parts:
            raise LatexError("A subscript _{} is empty.")
        return "".join(parts), raw, nxt

    # -- vectors: \vec{u}\cdot\vec{v} -> dotP(u,v), \times -> crossP

    def _vectors(self, s: str) -> str:
        r"""\vec{u} (or \vec u) -> a vector name, printed as u."""
        pat = re.compile(r"\\vec(?![A-Za-z])\s*")
        while (m := pat.search(s)):
            j = m.end()
            if j < len(s) and s[j] == "{":
                end = _match_brace(s, j)
                arg, nxt = s[j + 1:end].strip(), end + 1
            else:
                t = re.compile(r"\\[A-Za-z]+|[A-Za-z]").match(s, j)
                arg, nxt = (t.group(), t.end()) if t else ("", j)
            g = re.fullmatch(r"\\([A-Za-z]+)", arg)
            if re.fullmatch(r"[A-Za-z]", arg):
                name = arg
            elif g and g.group(1) in _GREEK_CMDS and g.group(1) != "pi":
                name = g.group(1)
            else:
                raise LatexError(f"\\vec{{{arg}}} isn't supported (only over one letter, e.g. \\vec{{u}}); write it "
                                 "in TI-Nspire syntax and use nspire_eval.")
            s = s[:m.start()] + self.ph("vec", name) + s[nxt:]
        return s

    def _vec_shape(self, tex: str):
        """('sym', None) for \\vec{u} or a cross product of those, ('row'|'col', n) for a
        1×n or n×1 matrix, None for anything that isn't a vector."""
        kind, payload = self.placeholders.get(tex.strip()[1:], (None, None))
        if kind == "vec":
            return ("sym", None)
        if kind == "matrix":
            rows, cols = len(payload), len(payload[0])
            if rows == 1 and cols >= 2:
                return ("row", cols)
            if cols == 1 and rows >= 2:
                return ("col", rows)
        if kind == "vecop" and payload[0] == "cross":
            return payload[3]
        return None

    def _vector_ops(self, s: str) -> str:
        """u \\cdot v and u \\times v between two vectors (\\vec names or row/column matrices)
        -> dotP / crossP. The parser would read both as u*v."""
        atom = re.compile(r"\\" + _PH + r"_\{9\d+\}")
        op_re = re.compile(r"\s*(\\cdot|\\times)(?![A-Za-z])\s*")
        while True:
            # (u \times v) \cdot w: drop parentheses around a lone vector next to a vector operator
            for m in reversed(list(re.finditer(r"\(\s*(" + atom.pattern + r")\s*\)", s))):
                if not self._vec_shape(m.group(1)):
                    continue
                before, after = s[:m.start()].rstrip(), s[m.end():]
                op_before = re.search(r"\\(cdot|times)$", before) is not None
                op_after = op_re.match(after) is not None
                called = re.search(r"[A-Za-z0-9}\])'!]$", before) is not None
                if op_before or (op_after and not called):
                    s = s[:m.start()] + " " + m.group(1) + " " + s[m.end():]
            atoms = list(atom.finditer(s))
            pairs = []
            for x, y in zip(atoms, atoms[1:], strict=False):
                om = op_re.fullmatch(s, x.end(), y.start())
                if om and self._vec_shape(x.group()) and self._vec_shape(y.group()):
                    pairs.append((x, om.group(1), y))
            if not pairs:
                return s
            for p, q in zip(pairs, pairs[1:], strict=False):
                if p[2].start() == q[0].start():
                    raise LatexError("Put parentheses around one of the vector products, e.g. "
                                     "\\vec{u}\\cdot(\\vec{v}\\times\\vec{w}): which one comes first changes the answer.")
            for x, op, y in reversed(pairs):
                if re.match(r"\s*[\^_'!]", s[y.end():]) or re.search(r"[\^_]\s*$", s[:x.start()]):
                    raise LatexError("A vector in a dot or cross product has an exponent or index attached; put "
                                     "parentheses around what you mean, or use nspire_eval with dotP/crossP.")
                kind = "dot" if op == r"\cdot" else "cross"
                shape = self._vec_pair(kind, self._vec_shape(x.group()), self._vec_shape(y.group()))
                rep = self.ph("vecop", (kind, x.group(), y.group(), shape))
                s = s[:x.start()] + rep + s[y.end():]

    @staticmethod
    def _vec_pair(kind: str, a, b):
        """Check the shapes of a dot/cross product's vectors; the shape of a cross product."""
        known = [v for v in (a, b) if v[0] != "sym"]
        if len(known) == 2:
            if known[0][0] != known[1][0]:
                raise LatexError("A dot or cross product needs two row vectors or two column vectors; one of these "
                                 "is a row and the other a column.")
            if known[0][1] != known[1][1]:
                raise LatexError(f"The vectors have different lengths ({known[0][1]} and {known[1][1]}).")
        if kind == "cross" and any(v[1] not in (2, 3) for v in known):
            raise LatexError("A cross product needs vectors of length 2 or 3.")
        return known[0] if known else ("sym", None)

    # -- units: 5\,\mathrm{m} -> 5*_m

    def _units(self, s: str) -> str:
        r"""\mathrm{...} or \text{...} right after a number (or after such a unit) is a unit.

        Without this, 5\,\mathrm{m} + 3\,\mathrm{cm} would be 5*m + 3*cm (variables) and
        5\text{ m} just 5. The units in _UNITS become TI-Nspire units; any other name
        there is refused. \mathrm{d}x, \mathrm{e}, \mathrm{i} and the condition words
        of \text{for ...} aren't units.
        """
        if re.search(r"\^\s*\{?\s*\\circ\s*\}?\s*(?:" + _UNIT_WRAPPERS + r"\s*[CF]\s*\}|[CF](?![A-Za-z]))", s):
            raise LatexError("Temperatures (°C, °F) aren't supported by the LaTeX translator; write them in TI-Nspire "
                             "syntax (_°C, tmpCnv) and use nspire_eval.")
        pat = re.compile(_UNIT_WRAPPERS)
        ends: set[int] = set()
        i = 0
        while (m := pat.search(s, i)):
            end = _match_brace(s, m.end() - 1)
            content, before, after = s[m.end():end].strip(), s[:m.start()], s[end + 1:]
            words = content.lower().split()
            called = content in _KNOWN_CMDS and re.match(r"\s*\(", after)  # 2\,\mathrm{min}(a,b)
            if (not content or content in ("d", "e", "i") or words[0] in _CONDITION_WORDS | _TEXT_WORDS
                    or content in (",", "&") or called or not self._unit_place(before, ends)):
                i = end + 1
                continue
            unit = self._unit_tex(content)
            op_before = re.search(r"(?:/|\*|\\cdot|\\times)\s*$", before) is not None
            rep = ("" if op_before else r" \cdot ") + "{" + unit + "}"
            s = before + rep + after
            i = len(before) + len(rep)
            ends.add(i)
        return s

    @staticmethod
    def _unit_place(before: str, ends: set[int]) -> bool:
        """Is this spot right after a number, or after a unit (5\\,\\mathrm{m}/\\mathrm{s})?"""
        b = before.rstrip()
        if re.search(r"\d\.?$", b) or re.search(r"\{\s*-?\d+(?:[.,]\d+)?\s*\}$", b):
            return True
        b = re.sub(r"(?:/|\*|\\cdot|\\times)$", "", b).rstrip()
        b = re.sub(r"\^\s*(?:\{[^{}]*\}|-?\d|[A-Za-z])$", "", b).rstrip()
        return len(b) in ends

    def _unit_tex(self, content: str) -> str:
        """m/s^2, kg\\cdot m, km/h ... -> LaTeX over unit placeholders."""
        tok = re.compile(r"\s+|\\(?:cdot|times)(?![A-Za-z])|[/*()]|\^\s*(?:\{\s*-?\d+\s*\}|\d)|[A-Za-z]+|\S")
        out: list[str] = []
        last_unit = False
        for m in tok.finditer(content):
            t = m.group()
            if t.isspace():
                continue
            if re.fullmatch(r"[A-Za-z]+", t):
                if t not in _UNITS:
                    raise LatexError(
                        f"'{t}' after a number isn't a unit the LaTeX translator knows (it knows "
                        f"{', '.join(sorted(_UNITS, key=str.lower))}). Write units in TI-Nspire syntax (5_m) and use "
                        "nspire_eval, or put \\cdot between the number and a variable.")
                if last_unit:
                    out.append(r"\cdot")
                out.append(self.ph("unit", _UNITS[t]).strip())
                last_unit = True
            elif t.startswith("^"):
                if not last_unit:
                    raise LatexError(f"Couldn't read the unit {content!r}.")
                out[-1] = "{" + out[-1] + "}^{" + re.sub(r"[\^{}\s]", "", t) + "}"
            elif t in ("(", ")"):
                if t == "(" and last_unit:
                    out.append(r"\cdot")
                out.append(t)
                last_unit = t == ")"
            elif t in ("/", "*", r"\cdot", r"\times"):
                out.append("/" if t == "/" else r"\cdot")
                last_unit = False
            else:
                raise LatexError(f"Couldn't read the unit {content!r}; write it in TI-Nspire syntax and use nspire_eval.")
        if not out or not last_unit:
            raise LatexError(f"Couldn't read the unit {content!r}.")
        body = " ".join(out)
        return body if len(out) == 1 else f"({body})"

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


# ================================================================ parse

_FUNC_NAMES = {"f", "g", "h", "F", "G", "H"}
_RESERVED = {"e", "i", "pi"}


_antlr_imported = False


def _check_commands(src: str) -> None:
    """Refuse commands the parser would misread: it takes any unknown \\foo as a variable foo."""
    for m in re.finditer(r"\\([A-Za-z]+)|\\.", src):
        cmd = m.group(1)
        if cmd is None:
            continue
        if cmd in _FUNCLIKE:
            if not re.match(r"\s*\(", src[m.end():]):
                raise LatexError(f"Write \\{cmd} with its argument in parentheses, e.g. \\{cmd}(x).")
        elif cmd not in _PARSER_CMDS:
            if cmd.startswith("nspire"):
                raise LatexError("Couldn't place the evaluation bar or the condition in this LaTeX; write it in "
                                 "TI-Nspire syntax and use nspire_eval.")
            raise LatexError(f"\\{cmd} isn't supported by the LaTeX translator; write the problem in TI-Nspire "
                             "syntax and use nspire_eval instead.")


def _parse_one(src: str) -> sp.Basic:
    _check_commands(src)
    global _antlr_imported
    if not _antlr_imported:
        # Import the parser with evaluation on: importing it builds
        # sympy.physics.units, which fails inside evaluate(False).
        with contextlib.suppress(Exception):  # parse_latex below reports what's missing
            importlib.import_module("sympy.parsing.latex._parse_latex_antlr")
        _antlr_imported = True
    try:
        # evaluate(False) keeps what the parser builds as written: x=x stays an
        # equation, -(x-1) isn't distributed, binomial(5,2) isn't computed
        with sp.evaluate(False):
            # strict: fail if the parser stops early. Without it, "2x ) + 7" reads as 2x
            # and "x^2 = 4 ] ..." as x^2 = 4, silently dropping the rest.
            return parse_latex(src, strict=True, backend="antlr")
    except Exception as e:
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        if first == "Invalid LaTeX":
            raise LatexError(f"Couldn't parse all of the LaTeX {src.strip()!r}: something in it isn't understood "
                             "(an unmatched bracket, or text after the expression?).") from None
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


def _rename(e: sp.Basic, names: dict[str, str]) -> sp.Basic:
    """Put the subscripted names back in place of their placeholders (see _Pre._subscripts)."""
    if not names:
        return e

    def go(node):
        if isinstance(node, sp.Symbol):
            return sp.Symbol(names[node.name]) if node.name in names else node
        if isinstance(node, AppliedUndef):
            n = node.func.__name__
            return sp.Function(names.get(n, n))(*[go(a) for a in node.args])
        if not node.args:
            return node
        new = [go(a) for a in node.args]
        if all(a is b for a, b in zip(new, node.args, strict=True)):
            return node
        with sp.evaluate(False):
            return node.func(*new)
    return go(e)


def _is_symvec(e) -> bool:
    """A vector the CAS can't see is one: \\vec{u}, a cross product, or a sum or multiple of those."""
    if isinstance(e, TiVec):
        return True
    if isinstance(e, AppliedUndef) and e.func.__name__ == "crossP":
        return True
    if isinstance(e, (sp.Add, sp.Mul)):
        return any(_is_symvec(a) for a in e.args)
    return False


def _plain_names(e, out: set[str], vecs: set[str]) -> None:
    """Names of plain variables (out) and of \\vec names (vecs) in e."""
    if isinstance(e, TiVec):
        vecs.add(_clean_name(str(e.args[0])))
        return
    if isinstance(e, sp.Symbol) and not isinstance(e, TiUnit):
        out.add(_clean_name(e.name))
    for a in getattr(e, "args", ()):
        if isinstance(a, sp.Basic):
            _plain_names(a, out, vecs)


def parse(latex: str) -> Parsed:
    pre = _Pre()
    pieces = pre.run(latex)
    if not pieces:
        raise LatexError("The LaTeX is empty.")

    def parse1(src: str) -> sp.Basic:
        return _rename(_parse_one(src), pre.subnames)

    raw = [parse1(p) for p in pieces]
    raw_conds = [parse1(c) for c in pre.constraints]

    # placeholders with LaTeX inside: parse it (cleaned up by walk() below)
    ph_values: dict[str, tuple[str, object]] = {}
    for name, (kind, payload) in pre.placeholders.items():
        if kind in ("matrix", "det"):
            ph_values[name] = (kind, sp.ImmutableMatrix([[parse1(c) for c in row] for row in payload]))
        elif kind == "evalat":
            ph_values[name] = (kind, tuple(None if x is None else parse1(x) for x in payload))
        elif kind == "list":
            ph_values[name] = (kind, [parse1(x) for x in payload])
        elif kind == "vecop":
            ph_values[name] = (kind, (payload[0], parse1(payload[1]), parse1(payload[2])))

    # T(t) = 20+80e^{-0.1t} defines T: only f g h F G H are functions otherwise, so
    # T(t) would be the product T*t. A name on the left called with bare variables
    # that appear on the right (and the name itself doesn't) is a definition.
    fn_heads: set[str] = set()
    if len(raw) == 1 and isinstance(raw[0], sp.Eq) and isinstance(raw[0].lhs, AppliedUndef):
        head, rhs = raw[0].lhs, raw[0].rhs
        n = head.func.__name__
        rhs_names = {s.name for s in rhs.free_symbols} | {f.func.__name__ for f in rhs.atoms(AppliedUndef)}
        if n not in ("e", "i") and "'" not in n and n not in pre.placeholders \
                and head.args and all(a.is_Symbol and a.name != n for a in head.args) \
                and (pre.assign or (all(a.name in rhs_names for a in head.args) and n not in rhs_names)):
            if n in ("x", "y", "z", "t") and not pre.assign and n not in _FUNC_NAMES:
                # Defining x would turn the document's main variable into a function: ask
                args = ",".join(a.name for a in head.args)
                raise LatexError(f"{n}({args})=... would define {n} as a function in the document, replacing the "
                                 f"variable {n}. Write {n}({args}):=... to define it, or {n}\\cdot({args}) for a product.")
            fn_heads.add(n)

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
                    kind, v = ph_values[n]
                    if kind == "evalat":
                        return _eval_at(*v)
                    if kind == "list":
                        return TiList(*[fix(x) for x in v], evaluate=False)
                    if kind == "vecop":
                        op, a, b = v
                        return sp.Function("dotP" if op == "dot" else "crossP")(walk(a), walk(b))
                    m = v.applyfunc(walk)
                    return sp.Function("det")(m) if kind == "det" else m
                if n in pre.placeholders:
                    kind, name = pre.placeholders[n]
                    if kind == "vec":
                        return TiVec(sp.Symbol(name))
                    if kind == "unit":
                        return TiUnit(name)
                    if name.lower() in _FUNC_WORDS:
                        raise LatexError(f"Write {name} with its argument in parentheses, e.g. {name}(x).")
                    return sp.Symbol(name)
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
                           or n in _FUNCLIKE or n in fn_heads)
                base = n.split("_")[0]
                if not is_func and base in _GREEK_CMDS and base != "pi":
                    # \Gamma(5), \Phi(x): a function to the reader, a product to the parser
                    g = _clean_name(n)
                    raise LatexError(f"{g}(...) could be the function {g} or the product {g}·(...). For the "
                                     f"product write \\{n}\\cdot(...); to call a function (defined in the document "
                                     f"or built in, such as gamma), write it in TI-Nspire syntax and use nspire_eval.")
                if not is_func and len(args) == 1 and n not in ("e",):
                    # x(x+1) is a product, not a call; \pi(r+1) is π*(r+1)
                    return _mul([walk(sp.Symbol(n)), args[0]])
                return sp.Function(n)(*args)
            if not node.args:
                return node
            new = [walk(a) for a in node.args]
            if node.func is sp.Mul and sum(_is_symvec(a) for a in _flatten(sp.Mul, new)) >= 2:
                raise LatexError("Two vectors are multiplied here without \\cdot or \\times between them. Write "
                                 "\\vec{u}\\cdot\\vec{v} (dot product) or \\vec{u}\\times\\vec{v} (cross product), "
                                 "with parentheses around anything longer than one vector.")
            if node.func is sp.Pow and _is_symvec(new[0]):
                raise LatexError("A power of a vector isn't defined; write \\vec{u}\\cdot\\vec{u} for the dot "
                                 "product with itself.")
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

    def _eval_at(body, lo, hi):
        """f |_{x=a} -> (f|x=a);  f |_{a}^{b} -> (f|x=b) - (f|x=a). Applied after f is evaluated."""
        body = fix(body)

        def point(lim):
            lim = fix(lim)
            if isinstance(lim, sp.Eq):
                if not lim.lhs.is_Symbol:
                    raise LatexError("The evaluation bar's limit must look like x=2.")
                return lim.lhs, lim.rhs
            return None, lim
        if lo is None:
            raise LatexError("An evaluation bar needs a lower limit, e.g. \\Big|_{x=2} or \\Big|_{0}^{1}.")
        v_lo, a = point(lo)
        v_hi, b = point(hi) if hi is not None else (None, None)
        var = v_lo or v_hi
        if var is None:
            free = sorted(body.free_symbols, key=str)
            if len(free) != 1:
                raise LatexError("Say which variable the evaluation bar sets, e.g. \\Big|_{x=0}^{1}.")
            var = free[0]
        if hi is None:
            return sp.Subs(body, var, a)
        return sp.Add(sp.Subs(body, var, b), sp.Mul(sp.S.NegativeOne, sp.Subs(body, var, a), evaluate=False),
                      evaluate=False)

    exprs = [fix(e) for e in raw]
    conds = [fix(c) for c in raw_conds]
    plain: set[str] = set()
    vecs: set[str] = set()
    for e in exprs + conds:
        _plain_names(e, plain, vecs)
    if vecs & plain:
        n = sorted(vecs & plain)[0]
        raise LatexError(f"Both \\vec{{{n}}} and plain {n} appear, and TI-Nspire would read them as the same "
                         f"variable {n}. Rename one of them.")
    indep = pre.ode_indep
    if ode_dep and not indep:
        indep = _ode_indep(raw, ode_dep, all_syms)
    return Parsed(exprs=exprs, ode_dep=ode_dep, ode_indep=indep if ode_dep else None,
                  placeholders=pre.placeholders, constraints=conds, assign=pre.assign)


# ================================================================ print (sympy -> TI-Nspire)

_FN_MAP = {"asin": "arcsin", "acos": "arccos", "atan": "arctan", "acot": "arccot", "asec": "arcsec",
           "acsc": "arccsc", "asinh": "arcsinh", "acosh": "arccosh", "atanh": "arctanh",
           "Abs": "abs", "ceiling": "ceiling", "floor": "floor", "sign": "sign", "Max": "max",
           "Min": "min", "re": "real", "im": "imag", "Re": "real", "Im": "imag", "conjugate": "conj", "arg": "angle",
           "binomial": "nCr", "exp": "exp", "gcd": "gcd", "lcm": "lcm", "sgn": "sign"}


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
        # \frac{d}{dx}\frac{d}{dx} f is nested, or merged with an unevaluated count (x, 1 + 1)
        expr, counts = e.expr, list(e.variable_count)
        while isinstance(expr, sp.Derivative):
            counts = list(expr.variable_count) + counts
            expr = expr.expr
        merged: list[list] = []
        for v, n in counts:
            n = int(n) if n.is_number else n
            if merged and merged[-1][0] == v:
                merged[-1][1] = merged[-1][1] + n
            else:
                merged.append([v, n])
        s = self._print(expr)
        for v, n in merged:
            s = f"derivative({s},{self._print(v)}" + (f",{self._print(sp.sympify(n))})" if n != 1 else ")")
        return s

    def _print_TiList(self, e):
        return "{" + ",".join(self._print(a) for a in e.args) + "}"

    def _print_TiVec(self, e):
        return self._print(e.args[0])

    def _print_TiUnit(self, e):
        return e.name

    def _print_Subs(self, e):
        expr, vs, pts = e.args
        cond = " and ".join(f"{self._print(v)}={self._print(p)}" for v, p in zip(vs, pts, strict=True))
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
        names |= {_clean_name(s.name) for s in e.free_symbols if s.name not in bound and not isinstance(s, TiUnit)}
        names -= {"e", "i", "pi"}
    names = {n for n in names if "'" not in n}
    return sorted(names, key=lambda n: (_VAR_PREF.index(n) if n in _VAR_PREF else 99, n))


def plan(latex: str, action: str = "auto", var: str | None = None, order: int | None = None,
         point: str | None = None) -> Plan:
    action = (action or "auto").lower().replace(" ", "").replace("_", "")
    if action != "auto" and action not in ACTIONS:
        raise LatexError(f"Unknown action {action!r}. Use auto or one of: {', '.join(ACTIONS)}.")
    p = parse(latex)
    pl = _plan(p, action, var, order, point)
    if p.constraints:
        # \text{for } x>0: TI-Nspire's "with" operator, applied to the result
        pl.command += "|" + " and ".join(to_nspire(c, p) for c in p.constraints)
    return pl


def _plan(p: Parsed, action: str, var: str | None, order: int | None, point: str | None) -> Plan:
    exprs = p.exprs
    rels = [e for e in exprs if isinstance(e, Relational)]
    user_vars = [v.strip() for v in re.split(r"[,\s]+", var) if v.strip()] if var else []

    if p.assign:
        if action not in ("auto", "define"):
            raise LatexError("':=' stores a value; use action auto (or define).")
        if len(exprs) != 1 or not isinstance(exprs[0], sp.Eq):
            raise LatexError("':=' needs one name on the left, e.g. a:=5.")
        lhs = exprs[0].lhs
        if isinstance(lhs, sp.Symbol):
            name = to_nspire(lhs, p)
            return Plan(f"{name}:={to_nspire(exprs[0].rhs, p)}", "define", f"store {name}", [], p)
        if not (isinstance(lhs, AppliedUndef) and all(a.is_Symbol for a in lhs.args)):
            raise LatexError("':=' needs a name (a:=5) or a function (f(x):=x^2) on the left.")

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


# Unevaluated calculus the CAS hands back, as SymPy's unevaluated classes: they
# only print (\int, \sum, \frac{d}{dx}, \lim); nothing calls doit().
def _integral(f, x, a=None, b=None):
    return sp.Integral(f, x if a is None and b is None else (x, a, b))


def _derivative(f, x, n=1):
    return sp.Derivative(f, (x, n))


def _limit(f, x, a, d=None):
    side = "+-" if d is None else ("+" if float(d) > 0 else "-")  # TI: positive = from the right
    return sp.Limit(f, x, a, side)


def _sum(f, k, a, b):
    return sp.Sum(f, (k, a, b))


def _product(f, k, a, b):
    return sp.Product(f, (k, a, b))


# Functions the CAS prints, as SymPy classes that only display them (parsing
# runs with evaluation off). Any other called name becomes an undefined
# function and is shown as written, not recomputed.
_OUT_FN = {"sin": sp.sin, "cos": sp.cos, "tan": sp.tan, "cot": sp.cot, "sec": sp.sec, "csc": sp.csc,
           "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh, "coth": sp.coth, "sech": sp.sech,
           "csch": sp.csch, "arcsin": sp.asin, "arccos": sp.acos, "arctan": sp.atan, "arccot": sp.acot,
           "arcsec": sp.asec, "arccsc": sp.acsc, "arcsinh": sp.asinh, "arccosh": sp.acosh,
           "arctanh": sp.atanh, "ln": sp.log, "log": _log, "exp": sp.exp, "sqrt": sp.sqrt, "root": _root,
           "abs": sp.Abs, "floor": sp.floor, "ceiling": sp.ceiling, "sign": sp.sign, "nCr": sp.binomial,
           "real": sp.re, "imag": sp.im, "conj": sp.conjugate, "max": sp.Max, "min": sp.Min,
           "factorial": sp.factorial, "Matrix": sp.ImmutableMatrix, "derivative": _derivative, "limit": _limit,
           "nsIntegral": _integral, "nsSum": _sum, "nsProduct": _product}
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
    # ∫( Σ( Π( as the CAS writes unevaluated ones; a bare ∫ stays and is refused later
    t = t.replace("∫(", "nsIntegral(").replace("Σ(", "nsSum(").replace("Π(", "nsProduct(")

    def matrix(m):
        if m.group(1) is not None:  # [[1,2][3,4]], [[1][2]]
            return "Matrix([" + m.group(1).replace("][", "],[") + "])"
        return "Matrix([[" + m.group(2) + "]])"  # a row vector [1,2,3] is a 1×n matrix
    return re.sub(r"\[(\[.*?\])\]|\[([^\[\]]*)\]", matrix, t)


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
            op = two if two in ("<=", ">=", "!=") else c if c in "=<>" else None
            if op:
                parts.append(cur)
                ops.append(op)
                cur = ""
                i += len(op)
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

    def _print_Symbol(self, expr, style="plain"):
        if expr.name.startswith("_"):  # a TI-Nspire unit: _m -> \mathrm{m}
            return r"\mathrm{" + expr.name[1:] + "}"
        return super()._print_Symbol(expr, style)


def _expr_latex(s: str, ld: dict) -> str:
    with sp.evaluate(False):
        e = parse_expr(s, local_dict=dict(ld), global_dict=dict(_PARSE_GLOBALS),
                       transformations=_PARSE_TRANSFORMS, evaluate=False)
    return _ResultLatex().doprint(e)


_PW_NAME = "nspirepiecewise"  # stands in for a piecewise(...) while the rest is converted


def _piecewise_out(s: str, ld: dict) -> tuple[str, dict[str, str]]:
    """Replace each piecewise(...) in s by a name; return s and {name: its cases LaTeX}.

    The CAS's text form is assumed to be piecewise(expr1,cond1,expr2,cond2,...), with a
    last expr without a condition meaning "otherwise" (the TI-Nspire Reference Guide's
    piecewise(Expr1[, Cond1[, Expr2[, Cond2[, ...]]]])). The conditions are relations,
    chains such as 0<x<1, and "and"/"or" of those, so they are converted like answers
    are, not by SymPy's parser (which can't read x>0 and x<1).
    """
    subs: dict[str, str] = {}
    while (m := re.search(r"(?<![A-Za-z0-9_])piecewise\(", s)):
        depth, end = 0, None
        for j in range(m.end() - 1, len(s)):
            depth += {"(": 1, ")": -1}.get(s[j], 0)
            if depth == 0:
                end = j
                break
        if end is None:
            raise ValueError("unbalanced piecewise")
        args = _split_top(s[m.end():end], (",",))
        if not args:
            raise ValueError("empty piecewise")
        rows = [f"{_part_latex(args[k], ld)} & {_cond_latex(args[k + 1], ld)}" for k in range(0, len(args) - 1, 2)]
        if len(args) % 2:
            rows.append(f"{_part_latex(args[-1], ld)} & \\text{{otherwise}}")
        name = _PW_NAME + _id_letters(len(subs))
        subs[name] = r"\begin{cases} " + r" \\ ".join(rows) + r" \end{cases}"
        s = s[:m.start()] + f" {name} " + s[end + 1:]
    return s, subs


def _cond_latex(s: str, ld: dict) -> str:
    return r" \text{ or } ".join(r" \text{ and } ".join(_part_latex(c, ld) for c in _split_top(alt, (" and ",)))
                                 for alt in _split_top(s, (" or ",)))


def _part_latex(s: str, ld: dict) -> str:
    s, subs = _piecewise_out(s, ld)
    out = _atom_latex(s, ld)
    for name, tex in subs.items():
        out = re.sub(r"(?<![A-Za-z])" + name + r"(?![A-Za-z])", lambda _m, t=tex: t, out)
    return out


def _atom_latex(s: str, ld: dict) -> str:
    s = s.strip()
    if s.startswith("{") and s.endswith("}"):
        items = _split_top(s[1:-1], (",",))
        return r"\left\{" + ", ".join(_part_latex(i, ld) for i in items) + r"\right\}"
    parts, ops = _split_rel(s)
    out = _expr_latex(parts[0], ld)
    for op, side in zip(ops, parts[1:], strict=True):
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
    if not t or low == "done":
        return None
    try:
        norm = _norm_result(t)
        if _UNSAFE.search(norm) or any(ord(c) > 127 and not c.isalpha() for c in norm):
            return None  # e.g. a symbol the conversion doesn't know
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
            alts.append(r",\quad ".join(_part_latex(a, ld) for a in _split_top(alt, (" and ",))))
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
