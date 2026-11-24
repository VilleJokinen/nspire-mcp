"""Check a worked solution line by line on the TI-Nspire CAS (the nspire_check_steps tool).

prepare() reads the lines offline (LaTeX through nspire_latex, imported only then, or
TI-Nspire syntax as written), commands() lists the CAS requests, and judge() reads the
CAS's replies into one verdict per step. Only the CAS decides whether two lines agree:

  expression -> expression   expand((before)-(after)); 0 means the step holds
                              (TI-Nspire has no simplify(): it simplifies every result
                              itself, and expand() also multiplies out what is left)
  equation   -> equation     solve(before,v) and solve(after,v); the same solutions mean it
                              holds (a step that loses or gains a solution is broken)

judge() only reads what the CAS wrote. A residual that is a nonzero polynomial or
rational expression (no functions, roots, decimals or symbolic exponents) is "broken";
any other nonzero residual is "undecided", because the CAS can miss an identity
(trig, logs, roots). Undecided expression steps then get a numeric spot check
(spot_plans() / spot_judge()): the CAS evaluates both lines and their difference at
seeded random points, approx({before,after,(before)-(after)}|x=...), all in one second
batch. Python only picks the points and compares the CAS's numbers with a relative
tolerance: every usable point agreeing makes the step "probably_ok", one that differs
makes it "broken" with that point as the counterexample. Points where either side is
undefined, non-real or an error are skipped.
Nothing here imports pywin32 or SymPy at import time.
"""
from __future__ import annotations

import math
import random
import re
import zlib
from dataclasses import dataclass, field

from nspire_chars import readable

MAX_LINES = 40
MAX_LINE_CHARS = 1000
EXPRESSION, EQUATION = "expression", "equation"
OK, BROKEN, UNDECIDED, PROBABLY_OK = "ok", "broken", "undecided", "probably_ok"

# Things that store or delete: checking only evaluates.
_STORES_RE = re.compile(r":=|→|=:|\b(?:Define|DelVar|Lock|Unlock|setMode|NewProb)\b", re.IGNORECASE)
# Arrows and connectives people put before a line of working; dropped.
_LEAD_LATEX_RE = re.compile(r"^(?:\s|&|\\(?:iff|Leftrightarrow|Longleftrightarrow|Rightarrow|Longrightarrow|"
                            r"implies|therefore|quad|qquad|,|;|!)(?![A-Za-z])|⇔|⇒|∴)+")
_LEAD_RAW_RE = re.compile(r"^(?:\s|⇔|⇒|∴|<=>|=>)+")
_TRAIL_LATEX_RE = re.compile(r"(?:\s|\\\\|\\,|\\;|\\quad|\\qquad|[.,;])+$")
_IDENT_RE = re.compile(r"[^\W\d]\w*")
_WORDS = {"and", "or", "not", "xor", "π", "ℯ", "𝑖", "i", "e"}
_VAR_PREF = ["x", "y", "z", "t", "u", "v", "w", "s", "r", "n", "k", "a", "b", "c"]
_VAR_RE = re.compile(r"^[^\W\d]\w{0,15}(?:\s*,\s*[^\W\d]\w{0,15})*$")


@dataclass
class Line:
    source: str          # as given
    nspire: str          # the line in TI-Nspire syntax (the solve(...) command for an equation)
    kind: str            # EXPRESSION or EQUATION


@dataclass
class Spec:
    lines: list[Line]
    var: str | None                      # what equations are solved for, e.g. "x" or "{x,y}"
    constants: list[str] = field(default_factory=list)


@dataclass
class Step:
    step: int            # 1-based: step k goes from line k to line k+1
    from_line: int
    to_line: int
    kind: str            # EXPRESSION, EQUATION, or "mixed"
    verdict: str         # OK, BROKEN or UNDECIDED
    check: list[str]     # the CAS commands behind the verdict
    cas: list[str]       # their results (or errors), as the CAS wrote them
    reason: str
    residual: str | None = None   # expression steps: what expand left
    variables: list[str] = field(default_factory=list)  # names in either line, for the spot check
    spot_check: dict | None = None  # see spot_judge()

    def as_dict(self) -> dict:
        d = {"step": self.step, "from_line": self.from_line, "to_line": self.to_line, "kind": self.kind,
             "verdict": self.verdict, "check": self.check, "cas": self.cas, "reason": self.reason}
        if self.residual is not None:
            d["residual"] = self.residual
        if self.spot_check is not None:
            d["spot_check"] = self.spot_check
        return d


# ------------------------------------------------------------ reading the lines (offline)

def _top_level_relation(cmd: str) -> bool:
    """True if cmd has =, <, >, ≤, ≥ or ≠ outside brackets (and not :=, @>, ->)."""
    depth = 0
    s = re.sub(r'"[^"]*"', '""', cmd)
    for i, ch in enumerate(s):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0 and ch in "=<>≤≥≠":
            prev = s[i - 1] if i else ""
            if ch == "=" and prev in ":":
                continue
            if ch == ">" and prev in "@-":
                continue
            return True
    return False


def _names(cmd: str) -> list[str]:
    s = re.sub(r'"[^"]*"', '""', cmd)
    s = re.sub(r"_\w+", " ", s)  # units and constants
    out = []
    for m in _IDENT_RE.finditer(s):
        rest = s[m.end():].lstrip()
        n = m.group(0)
        if rest.startswith("(") or n in _WORDS or n in out:
            continue
        out.append(n)
    return out


def _pick_var(names: list[str]) -> str | None:
    pref = [n for n in _VAR_PREF if n in names]
    return pref[0] if pref else (sorted(names)[0] if names else None)


def _var_text(var: str) -> str:
    vs = [v.strip() for v in var.split(",") if v.strip()]
    return vs[0] if len(vs) == 1 else "{" + ",".join(vs) + "}"


def _clean(src: str, latex: bool) -> str:
    s = src.strip()
    if latex:
        s = s.strip("$").strip()
        if s.startswith(r"\[") and s.endswith(r"\]"):
            s = s[2:-2]
        s = _TRAIL_LATEX_RE.sub("", _LEAD_LATEX_RE.sub("", s))
        s = s.replace("&=", "=").replace("&<", "<").replace("&>", ">")
    else:
        s = _LEAD_RAW_RE.sub("", s)
    s = s.strip()
    if s.startswith("=") and not s.startswith("=="):  # "= x^2+2x+1": a continued chain of equal expressions
        s = s[1:].strip()
    return s


def prepare(lines: list[str], latex: bool = True, var: str | None = None,
            constants: list[str] | bool = False, decimal_comma: bool | None = None) -> Spec:
    """Translate every line, without touching the bridge. Raises ValueError (naming the line)
    for input that can't be checked."""
    if not isinstance(lines, list) or len(lines) < 2:
        raise ValueError("Give at least two lines: each step is checked against the line before it.")
    if len(lines) > MAX_LINES:
        raise ValueError(f"At most {MAX_LINES} lines per call; split the solution.")
    if var is not None and (not isinstance(var, str) or not _VAR_RE.match(var.strip())):
        raise ValueError('var must be a variable name, or several separated by commas, e.g. "x" or "x,y".')
    cleaned = []
    for n, src in enumerate(lines, 1):
        if not isinstance(src, str) or not src.strip():
            raise ValueError(f"Line {n} is empty.")
        if len(src) > MAX_LINE_CHARS:
            raise ValueError(f"Line {n} is longer than {MAX_LINE_CHARS} characters.")
        s = _clean(src, latex)
        if not s:
            raise ValueError(f"Line {n} ({src!r}) has no math in it.")
        cleaned.append(s)
    out = _prepare_latex(lines, cleaned, var, constants, decimal_comma) if latex else _prepare_raw(lines, cleaned, var)
    for n, ln in enumerate(out.lines, 1):
        if _STORES_RE.search(ln.nspire):
            raise ValueError(f"Line {n} ({ln.source!r}) stores or deletes something ({ln.nspire}); "
                             f"checking steps only evaluates. Write the value into the line instead.")
    return out


def _prepare_raw(sources: list[str], cleaned: list[str], var: str | None) -> Spec:
    kinds = [EQUATION if _top_level_relation(s) else EXPRESSION for s in cleaned]
    v = var.strip() if var else None
    if v is None and EQUATION in kinds:
        first = cleaned[kinds.index(EQUATION)]
        v = _pick_var(_names(first))
        if v is None:
            raise ValueError(f"Line {kinds.index(EQUATION) + 1} ({first!r}) has no variable to solve for; give var.")
    vt = _var_text(v) if v else None
    out = []
    for src, s, kind in zip(sources, cleaned, kinds, strict=True):
        cmd = f"solve({s},{vt})" if kind == EQUATION else s
        out.append(Line(src, cmd, kind))
    return Spec(out, vt)


_OR_LATEX_RE = re.compile(r"\\lor(?![A-Za-z])|\\vee(?![A-Za-z])|\\(?:text|textrm|mathrm)\{\s*(?:or|tai)\s*\}")


def _alternatives(p) -> bool:
    """x=2, x=3 (one name on every left side): alternatives, not a system."""
    import sympy as sp
    lhs = [e.lhs for e in p.exprs]
    return len(lhs) > 1 and all(isinstance(e, sp.Eq) for e in p.exprs) \
        and all(isinstance(x, sp.Symbol) and x == lhs[0] for x in lhs)


def _prepare_latex(sources: list[str], cleaned: list[str], var: str | None, constants,
                   decimal_comma: bool | None = None) -> Spec:
    from sympy.core.relational import Relational

    import nspire_latex as lx  # SymPy: only for LaTeX input

    def fail(n: int, msg: str):
        return ValueError(f"Line {n} ({sources[n - 1]!r}): {msg}")

    lines = []  # (pieces: [(latex, Parsed)], kind, alternatives)
    for n, s in enumerate(cleaned, 1):
        pieces = []
        for piece in (x.strip() for x in _OR_LATEX_RE.split(s)):
            try:
                p = lx.parse(piece, constants, decimal_comma)
            except lx.LatexError as e:
                raise fail(n, str(e)) from None
            if p.ode_dep:
                raise fail(n, "a differential equation; checking steps compares expressions and algebraic "
                              "equations only.")
            if p.assign:
                raise fail(n, "stores a value (:=); checking steps only evaluates.")
            pieces.append((piece, p))
        rel = [isinstance(e, Relational) for _, p in pieces for e in p.exprs]
        if any(rel) and not all(rel):
            raise fail(n, "mixes equations and expressions.")
        alts = len(pieces) > 1 or (all(rel) and _alternatives(pieces[0][1]))
        if len(pieces) > 1 and not all(rel):
            raise fail(n, "'or' joins equations, not expressions.")
        if alts and any(p.constraints or p.convert for _, p in pieces):
            raise fail(n, "conditions or unit conversions can't be combined with 'or'.")
        lines.append((pieces, EQUATION if all(rel) else EXPRESSION, alts))

    v = var.strip() if var else None
    first_eq = next((i for i, (_, k, _a) in enumerate(lines) if k == EQUATION), None)
    if v is None and first_eq is not None:
        pieces, _, alts = lines[first_eq]
        src = pieces[0][0] if not alts or len(pieces) > 1 else cleaned[first_eq].split(",")[0]
        try:
            v = ",".join(lx.plan(src, "solve", constants=constants, decimal_comma=decimal_comma).variables) or None
        except lx.LatexError as e:
            raise fail(first_eq + 1, str(e)) from None
    vt = _var_text(v) if v else None
    out = []
    used: list[str] = []
    for n, (s, (pieces, kind, alts)) in enumerate(zip(cleaned, lines, strict=True), 1):
        if alts:
            body = " or ".join(lx.to_nspire(e, p) for _, p in pieces for e in p.exprs)
            cmd = f"solve({body},{vt})"
        else:
            try:
                cmd = lx.plan(s, "solve" if kind == EQUATION else "eval", v if kind == EQUATION else None,
                              constants=constants, decimal_comma=decimal_comma).command
            except lx.LatexError as e:
                raise fail(n, str(e)) from None
        out.append(Line(sources[n - 1], cmd, kind))
        used += [c for _, p in pieces for c in p.constants if c not in used]
    return Spec(out, vt, used)


# ------------------------------------------------------------ CAS requests

def residual_command(before: str, after: str) -> str:
    return f"expand(({before})-({after}))"


def commands(spec: Spec) -> list[str]:
    """The CAS requests, in order: one solve per equation line, one expand per pair of
    expression lines. Each command appears once."""
    out: list[str] = []
    for ln in spec.lines:
        if ln.kind == EQUATION and ln.nspire not in out:
            out.append(ln.nspire)
    for a, b in zip(spec.lines, spec.lines[1:], strict=False):
        if a.kind == b.kind == EXPRESSION:
            c = residual_command(a.nspire, b.nspire)
            if c not in out:
                out.append(c)
    return out


# ------------------------------------------------------------ reading the CAS's replies

def _norm(text: str) -> str:
    return readable(text).replace("−", "-").replace(" ", "")


def _split_or(text: str) -> list[str]:
    """'x=2 or x=3' -> ['x=2', 'x=3'], at bracket depth 0 only."""
    parts, depth, cur = [], 0, ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if depth == 0 and text.startswith(" or ", i):
            parts.append(cur)
            cur = ""
            i += 4
            continue
        cur += ch
        i += 1
    parts.append(cur)
    return [_norm(p) for p in parts if p.strip()]


_INT_POW_RE = re.compile(r"\^(?:\d+|\((?:-|−)?\d+\))")
_CALL_RE = re.compile(r"[^\W\d]\w*\s*\(")


def residual_is_decided(residual: str) -> bool:
    """True if a nonzero residual from expand is certainly nonzero: a polynomial or
    rational expression in its names (no function calls, roots, ℯ, decimals, or
    non-integer or symbolic exponents), which TI-Nspire brings to 0 whenever it is 0."""
    s = _norm(residual)
    if not s or "undef" in s:
        return False
    if any(ch in s for ch in "√ℯ|!∫∑∏ᴇ") or "." in s or _CALL_RE.search(s):
        return False
    return "^" not in _INT_POW_RE.sub("", s)


def _expr_step(k: int, a: Line, b: Line, reply: dict) -> Step:
    cmd = residual_command(a.nspire, b.nspire)
    st = Step(k, k, k + 1, EXPRESSION, UNDECIDED, [cmd], [], "")
    if not reply["ok"]:
        st.cas = [reply["error"]]
        st.reason = f"The CAS couldn't evaluate the difference: {reply['error']}"
        return st
    r = readable(reply["result"])
    st.cas, st.residual = [r], r
    st.variables = list(dict.fromkeys(_names(a.nspire) + _names(b.nspire) + _names(r)))
    if _norm(r) == "0":
        st.verdict, st.reason = OK, "The difference expands to 0, so the two lines are equal."
    elif residual_is_decided(r):
        st.verdict, st.reason = BROKEN, f"The difference expands to {r}, not 0, so the lines aren't equal."
    else:
        st.reason = (f"The difference expands to {r}; the CAS couldn't show that it is 0 (it may still be, "
                     f"e.g. a trig or log identity expand doesn't apply).")
    return st


def _eq_step(k: int, a: Line, b: Line, ra: dict, rb: dict) -> Step:
    st = Step(k, k, k + 1, EQUATION, UNDECIDED, [a.nspire, b.nspire], [], "")
    st.cas = [readable(r["result"]) if r["ok"] else r["error"] for r in (ra, rb)]
    if not (ra["ok"] and rb["ok"]):
        bad = a if not ra["ok"] else b
        st.reason = f"The CAS couldn't solve {bad.nspire}: {(ra if not ra['ok'] else rb)['error']}"
        return st
    sa, sb = _split_or(ra["result"]), _split_or(rb["result"])
    if any("solve(" in p for p in sa + sb):
        st.reason = "The CAS couldn't solve one of the equations exactly, so their solutions can't be compared."
    elif set(sa) == set(sb):
        st.verdict, st.reason = OK, "Both equations have the same solutions."
    else:
        lost = [p for p in sa if p not in sb]
        gained = [p for p in sb if p not in sa]
        what = "; ".join(x for x in (f"loses {' or '.join(lost)}" if lost else "",
                                      f"gains {' or '.join(gained)}" if gained else "") if x)
        st.verdict = BROKEN
        st.reason = f"The solutions differ: {st.cas[0]} before, {st.cas[1]} after (the step {what})."
    return st


def judge(spec: Spec, results: dict[str, dict]) -> list[Step]:
    """One Step per pair of consecutive lines. results: command -> {ok, result | error}."""
    steps = []
    for k, (a, b) in enumerate(zip(spec.lines, spec.lines[1:], strict=False), 1):
        if a.kind == b.kind == EXPRESSION:
            steps.append(_expr_step(k, a, b, results[residual_command(a.nspire, b.nspire)]))
        elif a.kind == b.kind == EQUATION:
            steps.append(_eq_step(k, a, b, results[a.nspire], results[b.nspire]))
        else:
            steps.append(Step(k, k, k + 1, "mixed", UNDECIDED, [], [],
                              f"Line {k} is {'an equation' if a.kind == EQUATION else 'an expression'} and line "
                              f"{k + 1} {'an equation' if b.kind == EQUATION else 'an expression'}; they can't be "
                              f"compared directly. Check this step by hand or rewrite it."))
    return steps


# ------------------------------------------------------------ numeric spot check

SPOT_POINTS = 12          # points tried per undecided step (all in the one batch)
SPOT_NEEDED = 4           # usable points needed for "probably_ok"
SPOT_MAX_STEPS = 10       # undecided steps spot-checked per call
SPOT_MAX_VARS = 8
SPOT_RTOL, SPOT_ATOL = 1e-8, 1e-12
# (kind, low, high) per point, in order: small positive decimals first (inside the domain of
# logs and roots), then negative ones, small integers (factorials, nCr) and larger values.
_SPOT_SHAPE = ((("float", 0.1, 3.0),) * 6 + (("float", -3.0, -0.1),) * 3 + (("int", 2, 6),) * 2
               + (("float", 3.0, 9.0),))
_NUM_RE = re.compile(r"^[-+]?(?:\d+\.?\d*|\.\d+)(?:e[-+]?\d+)?$")


def spot_points(variables: list[str], seed_text: str, n: int = SPOT_POINTS) -> list[dict[str, str]]:
    """n points, one value per variable, the same every time for the same seed_text. Decimals
    have four digits, so they miss 0, ±1 and multiples of π, where an identity and a wrong
    rewrite of it often agree."""
    rng = random.Random(zlib.crc32(seed_text.encode("utf-8")))
    out = []
    for i in range(n):
        kind, lo, hi = _SPOT_SHAPE[i % len(_SPOT_SHAPE)]
        out.append({v: str(rng.randint(int(lo), int(hi))) if kind == "int" else f"{rng.uniform(lo, hi):.4f}"
                    for v in variables})
    return out


def _with(pt: dict[str, str]) -> str:
    return " and ".join(f"{v}={x}" for v, x in pt.items())


def spot_command(before: str, after: str, pt: dict[str, str]) -> str:
    """approx({before,after,(before)-(after)}|x=... and y=...): both values and the CAS's own
    difference (computed before rounding for display, so the display digits don't hide it)."""
    body = f"{{{before},{after},({before})-({after})}}"
    return f"approx({body}|{_with(pt)})" if pt else f"approx({body})"


@dataclass
class SpotPlan:
    step: int
    points: list[dict[str, str]]
    commands: list[str]


def spot_plans(spec: Spec, steps: list[Step]) -> list[SpotPlan]:
    """One plan per undecided expression step with a residual (the first SPOT_MAX_STEPS)."""
    out: list[SpotPlan] = []
    for s in steps:
        if s.kind != EXPRESSION or s.verdict != UNDECIDED or s.residual is None or len(s.variables) > SPOT_MAX_VARS:
            continue
        if len(out) >= SPOT_MAX_STEPS:
            break
        a, b = spec.lines[s.from_line - 1].nspire, spec.lines[s.to_line - 1].nspire
        pts = spot_points(s.variables, f"{a}\n{b}") if s.variables else [{}]
        out.append(SpotPlan(s.step, pts, [spot_command(a, b, p) for p in pts]))
    return out


def spot_commands(plans: list[SpotPlan]) -> list[str]:
    return list(dict.fromkeys(c for p in plans for c in p.commands))


def _number(text: str) -> float | None:
    t = _norm(text).replace("ᴇ", "e").replace("E", "e")
    if not _NUM_RE.match(t):
        return None
    v = float(t)
    return v if math.isfinite(v) else None


def _top_split(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur.strip())
            cur = ""
        else:
            cur += ch
    parts.append(cur.strip())
    return parts


def _three(result: str) -> tuple[list[str], list[float]] | None:
    """{before,after,difference} as the CAS wrote them and as numbers, or None if any of the
    three isn't a real number (undef, ∞, a value with 𝑖, a name left without a value)."""
    t = readable(result).strip()
    if not (t.startswith("{") and t.endswith("}")):
        return None
    parts = _top_split(t[1:-1])
    vals = [_number(p) for p in parts]
    if len(parts) != 3 or any(v is None for v in vals):
        return None
    return parts, [v for v in vals if v is not None]


def agree(a: float, b: float, d: float) -> bool:
    """The CAS's difference d is negligible next to the values themselves."""
    return abs(d) <= SPOT_RTOL * max(abs(a), abs(b)) + SPOT_ATOL


def spot_judge(step: Step, plan: SpotPlan, results: dict[str, dict]) -> None:
    """Read the CAS's replies for one plan and settle the step if they allow it: broken at the
    first point where the lines differ, probably_ok if at least SPOT_NEEDED points (1 without
    variables) agree and none differs, otherwise still undecided. Sets step.spot_check to
    {tried, usable, agreed, counterexample: {at, command, before, after, difference} | None}."""
    usable = agreed = 0
    counter = None
    for pt, cmd in zip(plan.points, plan.commands, strict=True):
        r = results.get(cmd)
        got = _three(r["result"]) if r and r.get("ok") else None
        if got is None:
            continue
        usable += 1
        texts, (a, b, d) = got
        if agree(a, b, d):
            agreed += 1
        elif counter is None:
            counter = {"at": _with(pt) or "no variables", "command": cmd,
                       "before": texts[0], "after": texts[1], "difference": texts[2]}
    step.spot_check = {"tried": len(plan.commands), "usable": usable, "agreed": agreed, "counterexample": counter}
    needed = SPOT_NEEDED if plan.points[0] else 1
    if counter is not None:
        step.verdict = BROKEN
        at = f"at {counter['at']}" if plan.points[0] else "numerically"
        step.reason += (f" Numeric spot check: {at} line {step.from_line} is {counter['before']} and line "
                        f"{step.to_line} is {counter['after']} (difference {counter['difference']}), so the "
                        f"lines aren't equal there (the step may still hold on a smaller domain, such as "
                        f"positive values only).")
    elif usable >= needed:
        step.verdict = PROBABLY_OK
        step.reason += (f" Numeric spot check: the lines agree at all {usable} point(s) the CAS could evaluate "
                        f"(relative tolerance {SPOT_RTOL:g}), so they are probably equal; this isn't a proof.")
    else:
        step.reason += (f" Numeric spot check: the CAS gave real values at only {usable} of "
                        f"{len(plan.commands)} point(s), too few to judge.")


def run(bridge, spec: Spec, timeout_s: float, spot_check: bool = True) -> list[Step]:
    """All requests in one eval_batch (inside bridge.session()), then judge(). With
    spot_check, undecided expression steps get one more eval_batch, the numeric spot check,
    unless the bridge stopped answering in the first one (bridge.last_timeout)."""
    cmds = commands(spec)
    res = bridge.eval_batch(cmds, timeout_s) if cmds else []
    steps = judge(spec, {c: r for c, r in zip(cmds, res, strict=True)})
    if not spot_check or getattr(bridge, "last_timeout", None):
        return steps
    plans = spot_plans(spec, steps)
    spot_cmds = spot_commands(plans)
    if not spot_cmds:
        return steps
    res = bridge.eval_batch(spot_cmds, timeout_s)
    results = {c: r for c, r in zip(spot_cmds, res, strict=True)}
    by_step = {s.step: s for s in steps}
    for p in plans:
        spot_judge(by_step[p.step], p, results)
    return steps
