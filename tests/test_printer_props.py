"""Property tests for NspirePrinter: what it prints means what the tree means.

Random unevaluated SymPy trees (the kind parse() builds: Add/Mul/Pow kept as
written, -1*x for negation, x^-1 for division, 3^-1 as a root index) are printed
in TI-Nspire syntax, the text is read back into SymPy (TI-Nspire's ^, √(, root(,
ℯ, 𝑖, π, ln(, abs( and relations mapped to SymPy's), and both are evaluated at
random points. A missing parenthesis shows up as a different number.

Plain pytest with seeded random generators: no extra test dependency.
"""
import cmath
import random
import re

import pytest

sp = pytest.importorskip("sympy")

from nspire_chars import NS_E, NS_I  # noqa: E402
from nspire_latex import to_nspire  # noqa: E402
from sympy.parsing.sympy_parser import parse_expr, standard_transformations  # noqa: E402

X, Y, Z = sp.symbols("x y z")
SYMS = (X, Y, Z)


def _num(rng):
    k = rng.randrange(7)
    if k == 0:
        return sp.Integer(rng.randint(1, 9))
    if k == 1:
        return sp.Integer(-rng.randint(1, 9))
    if k == 2:
        p, q = rng.randint(1, 7), rng.randint(2, 7)
        return sp.Rational(p * rng.choice((1, -1)), q) if sp.gcd(p, q) == 1 else sp.Integer(p)
    if k == 3:
        return sp.Float(rng.choice(("0.5", "1.25", "-2.5", "0.1", "3.75")))
    if k == 4:
        return sp.E
    if k == 5:
        return sp.pi
    return sp.Integer(0) if rng.random() < 0.2 else sp.Integer(rng.randint(2, 4))


def _leaf(rng):
    return rng.choice(SYMS) if rng.random() < 0.6 else _num(rng)


def _exponent(rng, depth):
    k = rng.randrange(10)
    fixed = [sp.Integer(2), sp.Integer(3), sp.S.NegativeOne, sp.Integer(-2), sp.S.Half, sp.Rational(1, 3),
             -sp.S.Half, sp.Rational(2, 3)]
    if k < len(fixed):
        return fixed[k]
    if k == 8:
        return sp.Pow(sp.Integer(rng.randint(2, 4)), -1, evaluate=False)  # the parser's 1/n
    return _tree(rng, min(depth, 1))


def _tree(rng, depth):
    if depth <= 0 or rng.random() < 0.2:
        return _leaf(rng)
    d = depth - 1
    k = rng.randrange(9)
    if k == 0:
        return sp.Add(*[_tree(rng, d) for _ in range(rng.randint(2, 3))], evaluate=False)
    if k == 1:
        return sp.Mul(*[_tree(rng, d) for _ in range(rng.randint(2, 3))], evaluate=False)
    if k == 2:
        return sp.Mul(sp.S.NegativeOne, _tree(rng, d), evaluate=False)  # negation, as parsed
    if k == 3:
        return sp.Pow(_tree(rng, d), _exponent(rng, d), evaluate=False)
    if k == 4:  # a - b as the parser writes it
        return sp.Add(_tree(rng, d), sp.Mul(sp.S.NegativeOne, _tree(rng, d), evaluate=False), evaluate=False)
    if k == 5:  # a / b
        return sp.Mul(_tree(rng, d), sp.Pow(_tree(rng, d), -1, evaluate=False), evaluate=False)
    if k == 6:
        f = rng.choice((sp.sin, sp.cos, sp.exp, sp.Abs, sp.atan))
        return f(_tree(rng, d), evaluate=False)
    if k == 7:
        a = _tree(rng, d)
        return rng.choice((sp.log(a, evaluate=False), sp.log(a, 10, evaluate=False), sp.log(a, 2, evaluate=False)))
    if k == 8:
        if rng.random() < 0.5:
            return sp.Pow(sp.E, _tree(rng, d), evaluate=False)
        return sp.factorial(sp.Integer(rng.randint(0, 5)), evaluate=False)
    raise AssertionError


_RELS = {"=": sp.Eq, "≠": sp.Ne, "<": sp.Lt, ">": sp.Gt, "≤": sp.Le, "≥": sp.Ge}
def _ti_log(a, b=10, **kw):
    return sp.log(a, b, evaluate=False)  # TI-Nspire's log(x,b): base b


def _ti_root(a, n, **kw):
    return sp.Pow(a, sp.Pow(n, -1, evaluate=False), evaluate=False)


_LOCALS = {"sin": sp.sin, "cos": sp.cos, "exp": sp.exp, "ln": sp.log, "log": _ti_log, "abs": sp.Abs,
           "arctan": sp.atan, "sqrt": sp.sqrt, "root": _ti_root, "E": sp.E, "I": sp.I, "pi": sp.pi,
           "x": X, "y": Y, "z": Z}


def _read_back(text: str):
    """TI-Nspire text from to_nspire -> SymPy, unevaluated like the tree, so both are
    evaluated the same way (the same rounding near a branch cut such as sqrt(-0.75))."""
    t = text.replace(NS_E, "E").replace(NS_I, "I").replace("π", "pi").replace("√(", "sqrt(").replace("^", "**")
    with sp.evaluate(False):
        return parse_expr(t, local_dict=dict(_LOCALS), transformations=standard_transformations,
                          evaluate=False)


def _split_relation(text: str):
    depth = 0
    for i, c in enumerate(text):
        depth += {"(": 1, ")": -1}.get(c, 0)
        if depth == 0 and c in _RELS:
            return text[:i], c, text[i + 1:]
    return None


def _value(e, point):
    # an unevaluated log(a, b) (as the parser builds \log_b a) evaluates as ln(a) by evalf
    e = e.replace(lambda n: isinstance(n, sp.log) and len(n.args) == 2,
                  lambda n: sp.log(n.args[0]) / sp.log(n.args[1]))
    v = complex(sp.N(e.xreplace(point), 30))
    if not (cmath.isfinite(v)):
        raise ArithmeticError
    return v


def _same(a, b, rng):
    """Equal at three random points (points where either side is undefined are skipped)."""
    checked = 0
    for _ in range(6):
        point = {s: sp.Float(rng.uniform(0.3, 2.0), 30) for s in SYMS}
        try:
            va, vb = _value(a, point), _value(b, point)
        except (ArithmeticError, TypeError, ValueError, OverflowError):
            continue
        assert abs(va - vb) <= 1e-9 * max(1.0, abs(va)), (va, vb)
        checked += 1
        if checked == 3:
            break


@pytest.mark.parametrize("seed", range(300))
def test_printed_expression_means_the_tree(seed):
    rng = random.Random(seed)
    tree = _tree(rng, 4)
    text = to_nspire(tree)
    assert not re.search(r"[+\-*/^]\s*[*/^]|--|\+ -|- -", text), text
    try:
        back = _read_back(text)
    except Exception as e:  # the printed text must at least be readable
        raise AssertionError(f"{text!r}: {e}") from None
    try:
        _same(tree, back, rng)
    except AssertionError as e:
        raise AssertionError(f"{text!r} != tree {sp.srepr(tree)}: {e}") from None


@pytest.mark.parametrize("seed", range(100))
def test_printed_relation_means_the_tree(seed):
    rng = random.Random(10_000 + seed)
    op = rng.choice(list(_RELS))
    rel = _RELS[op](_tree(rng, 3), _tree(rng, 3), evaluate=False)
    text = to_nspire(rel)
    parts = _split_relation(text)
    assert parts and parts[1] == op, text
    _same(rel.lhs, _read_back(parts[0]), rng)
    _same(rel.rhs, _read_back(parts[2]), rng)
