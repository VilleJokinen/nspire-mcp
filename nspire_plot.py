"""Plots for the nspire_plot tool: the TI-Nspire CAS computes every value, matplotlib
only draws them.

Four kinds: function y=f(x), parametric (x(t), y(t)), polar r(θ) and implicit F(x,y)=0.
Every kind is sampled the same way, as "channels": one seq(approx(...)) per channel over
one grid. A function is one channel; a parametric curve two (x and y); a polar curve two
(r·cos θ and r·sin θ, computed by the CAS in radian mode inside a temporary function); an
implicit curve one per grid row (F at every x for that row's y, through a temporary
function F(x,y)). prepare() / prepare_kind() check the arguments offline, sample() runs
inside bridge.session() (one batch with every channel, split into smaller ranges only
when one fails), and render_png() / summary() run after the session, with the user's
clipboard back.
Nothing here imports pywin32, and matplotlib is imported only in render_png(), so the
server starts fast and the parsing and gap logic can be tested anywhere.
Function plots can also be annotated (annotate()): zeros, extrema and intersections found by
the CAS in the plotted window, marked on the image and listed in the summary.
"""
from __future__ import annotations

import io
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from nspire_chars import readable

KINDS = ("function", "parametric", "polar", "implicit")
SAMPLES_MIN, SAMPLES_MAX = 10, 1000
SAMPLES_DEFAULT = {"function": 200, "parametric": 400, "polar": 400}
GRID_MIN, GRID_MAX, GRID_DEFAULT = 10, 120, 60   # implicit: points per axis
MAX_FUNCS = 8           # one categorical colour each
MAX_IMPLICIT = 4        # implicit curves per plot (each is a whole grid)
HELPER = "mcpplot"      # prefix of the temporary functions (polar, implicit), deleted afterwards
MAX_EXPR_CHARS = 1000
X_LIMIT = 1e15          # |x_min|, |x_max| at most this
SPLIT = 8               # a failed range is retried as this many smaller ones
# Error codes that mean the expression itself is wrong, so no sample point can work:
# missing brackets, syntax, implied multiply, invalid command, argument counts,
# invalid names, and the CAS-only functions on the non-CAS edition.
_FATAL_CODES = {140, 410, 440, 620, 680, 690, 700, 710, 720, 750, 910, 930, 940, 990, 1040, 1045, 1050}
_CODE_RE = re.compile(r"\berror (\d+)")
_NAME_RE = re.compile(r"^[^\W\d_]\w{0,15}$")
_NUM_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?")
# Things that store or delete: nspire_plot only evaluates.
_STORES_RE = re.compile(r":=|→|=:|\b(?:Define|DelVar|Lock|Unlock|setMode|NewProb)\b", re.IGNORECASE)
# What can't go inside a Func ... EndFunc written on one line (the polar and implicit helpers).
_FUNC_UNSAFE_RE = re.compile(r":|\b(?:Func|EndFunc|Local|Return|Prgm|EndPrgm)\b", re.IGNORECASE)
_RELATION_RE = re.compile(r"<|>|≤|≥|≠|/=")

# Chart colours (a validated categorical order) and chrome, light mode.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
SURFACE, INK, INK_2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"


class NotNumeric(ValueError):
    """The CAS returned something that isn't a number, undef, ±∞ or a non-real value."""

    def __init__(self, index: int, item: str):
        super().__init__(f"not a number: {item!r}")
        self.index, self.item = index, item


# ---------------------------------------------------------------- arguments

@dataclass(frozen=True)
class Grid:
    x_min: float
    x_max: float
    n: int

    @property
    def step(self) -> float:
        return (self.x_max - self.x_min) / (self.n - 1)

    def x(self, i: int) -> float:
        return self.x_max if i == self.n - 1 else self.x_min + i * self.step


@dataclass
class Func:
    """One channel: sampled as seq(approx(command),var,a,b,h), or as call(a,b,h) when the
    seq runs inside a temporary function (polar: radian mode)."""
    label: str    # as given: TI-Nspire syntax, or the LaTeX
    command: str  # what is sampled, in TI-Nspire syntax
    call: str | None = None


@dataclass
class Curve:
    """A parametric, polar or implicit curve: which channels hold its values."""
    label: str           # as given
    parts: list[str]     # TI-Nspire syntax: [x(t), y(t)], [r(θ)] or [F(x,y)] (F=0)
    channels: list[int]  # indices into PlotSpec.funcs: [x, y], or one per grid row (implicit)


@dataclass
class PlotSpec:
    funcs: list[Func]   # the channels (for kind "function", the functions themselves)
    var: str
    grid: Grid          # the sampled variable: x, t or θ (implicit: x, per row)
    y_min: float | None = None
    y_max: float | None = None
    notes: list[str] = field(default_factory=list)  # adjustments to mention in the summary
    kind: str = "function"
    curves: list[Curve] = field(default_factory=list)  # every kind but "function"
    x_min: float | None = None   # x view for parametric / polar (None: automatic)
    x_max: float | None = None
    ygrid: Grid | None = None    # implicit: the rows' y values
    helpers: list[tuple[str, str]] = field(default_factory=list)  # (label, Define ...) sent before sampling
    helper_names: list[str] = field(default_factory=list)         # deleted again after sampling

    @property
    def is_curve(self) -> bool:
        return self.kind in ("parametric", "polar")


def _finite(name: str, v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number")
    return float(v)


def strip_lhs(expr: str, var: str, lhs: str = "y") -> str:
    """'y=x^2' or 'f(x)=x^2' -> 'x^2': a function to plot, written as an equation.
    lhs is the bare name also taken off ('r' for polar, 'x'/'y' for parametric parts)."""
    names = r"[^\W\d_]\w*\(\s*" + re.escape(var) + r"\s*\)"
    if var.lower() != lhs.lower():
        names = re.escape(lhs) + "|" + names
    m = re.match(rf"\s*(?:{names})\s*=(?![=<>])", expr)
    return expr[m.end():].strip() if m else expr.strip()


def check_command(cmd: str, label: str) -> None:
    if not cmd.strip():
        raise ValueError(f"{label!r}: empty expression")
    if "\n" in cmd or "\r" in cmd:
        raise ValueError(f"{label!r}: an expression must be a single line")
    if len(cmd) > MAX_EXPR_CHARS:
        raise ValueError(f"{label[:40]!r}…: longer than {MAX_EXPR_CHARS} characters")
    if _STORES_RE.search(cmd):
        raise ValueError(f"{label!r}: nspire_plot only evaluates. Define functions or store values "
                         f"with nspire_eval first, then plot them, e.g. f(x).")


def prepare(funcs: list[Func], var: str, x_min, x_max, samples, y_min=None, y_max=None) -> PlotSpec:
    """Check the arguments without touching the bridge. Raises ValueError."""
    if not funcs:
        raise ValueError("Give at least one function: exprs (TI-Nspire syntax) or latex.")
    if len(funcs) > MAX_FUNCS:
        raise ValueError(f"At most {MAX_FUNCS} functions per plot.")
    if not isinstance(var, str) or not _NAME_RE.match(var.strip()):
        raise ValueError("var must be a variable name, e.g. x or t")
    var = var.strip()
    for f in funcs:
        f.command = strip_lhs(f.command, var)
        check_command(f.command, f.label)
    a, b = _finite("x_min", x_min), _finite("x_max", x_max)
    if not a < b:
        raise ValueError("x_min must be less than x_max")
    if max(abs(a), abs(b)) > X_LIMIT:
        raise ValueError(f"x_min and x_max must be within ±{X_LIMIT:g}")
    notes = []
    if isinstance(samples, bool) or not isinstance(samples, (int, float)) or not math.isfinite(samples):
        raise ValueError("samples must be a whole number")
    n = min(max(int(samples), SAMPLES_MIN), SAMPLES_MAX)
    if n != samples:
        notes.append(f"samples set to {n} (allowed: {SAMPLES_MIN} to {SAMPLES_MAX}).")
    grid = Grid(a, b, n)
    if grid.step <= 1e-12 * max(1.0, abs(a), abs(b)):
        raise ValueError("The x range is too narrow for that many samples.")
    lo = None if y_min is None else _finite("y_min", y_min)
    hi = None if y_max is None else _finite("y_max", y_max)
    if lo is not None and hi is not None and not lo < hi:
        raise ValueError("y_min must be less than y_max")
    return PlotSpec(funcs, var, grid, lo, hi, notes)


def _range(lo_name: str, hi_name: str, lo, hi) -> tuple[float, float]:
    a, b = _finite(lo_name, lo), _finite(hi_name, hi)
    if not a < b:
        raise ValueError(f"{lo_name} must be less than {hi_name}")
    if max(abs(a), abs(b)) > X_LIMIT:
        raise ValueError(f"{lo_name} and {hi_name} must be within ±{X_LIMIT:g}")
    return a, b


def _view(lo_name: str, hi_name: str, lo, hi) -> tuple[float | None, float | None]:
    a = None if lo is None else _finite(lo_name, lo)
    b = None if hi is None else _finite(hi_name, hi)
    if a is not None and b is not None and not a < b:
        raise ValueError(f"{lo_name} must be less than {hi_name}")
    return a, b


def _count(samples, lo: int, hi: int, default: int, what: str, notes: list[str]) -> int:
    if samples is None:
        return default
    if isinstance(samples, bool) or not isinstance(samples, (int, float)) or not math.isfinite(samples):
        raise ValueError("samples must be a whole number")
    n = min(max(int(samples), lo), hi)
    if n != samples:
        notes.append(f"samples set to {n} (allowed{what}: {lo} to {hi}).")
    return n


def _unwrap(s: str) -> str:
    """'(a, b)', '{a, b}', '\\left(a, b\\right)' -> 'a, b' when the brackets enclose it all."""
    t = s.strip()
    t2 = re.sub(r"^\\left\s*(\(|\\\{)", lambda m: "(" if m.group(1) == "(" else "{", t)
    t2 = re.sub(r"\\right\s*(\)|\\\})$", lambda m: ")" if m.group(1) == ")" else "}", t2)
    if len(t2) >= 2 and (t2[0], t2[-1]) in (("(", ")"), ("{", "}")):
        depth = 0
        for k, ch in enumerate(t2):
            depth += ch in "([{"
            depth -= ch in ")]}"
            if depth == 0 and k < len(t2) - 1:
                return t  # the first bracket closes early: (a)+(b)
        inner = t2[1:-1]
        if len(_split_top(inner)) > 1:
            return inner
    return t


def split_pair(text: str) -> tuple[str, str]:
    """A parametric curve 'x(t), y(t)' (also '(x, y)', '{x, y}', 'x=..., y=...') -> its two parts."""
    parts = [p.strip() for p in _split_top(_unwrap(text))]
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"{text!r}: a parametric curve is two expressions separated by a comma, "
                         f"x(t), y(t), e.g. 'cos(t), sin(t)'")
    return parts[0], parts[1]


def implicit_function(cmd: str, label: str) -> str:
    """'x^2+y^2=1' -> '(x^2+y^2)-(1)'; an expression F stands for F=0."""
    if _RELATION_RE.search(cmd):
        raise ValueError(f"{label!r}: an implicit plot draws an equation F(x,y)=0 or lhs=rhs, not an inequality")
    top, depth = [], 0
    for k, ch in enumerate(cmd):
        depth += ch in "([{"
        depth -= ch in ")]}"
        if ch == "=" and depth == 0:
            top.append(k)
    if not top:
        return cmd.strip()
    if len(top) > 1:
        raise ValueError(f"{label!r}: more than one '=': give one equation per curve")
    lhs, rhs = cmd[:top[0]].strip(), cmd[top[0] + 1:].strip()
    if not lhs or not rhs:
        raise ValueError(f"{label!r}: one side of the equation is empty")
    return f"({lhs})-({rhs})"


def _check_func_body(cmd: str, label: str) -> None:
    if _FUNC_UNSAFE_RE.search(cmd):
        raise ValueError(f"{label!r}: ':' and Func/Local/Return can't be used here (the expression is "
                         f"evaluated inside a temporary function)")


def prepare_kind(kind: str, items: list[tuple[str, str]], var: str | None = None,
                 t_min=None, t_max=None, samples=None, x_min=None, x_max=None,
                 y_min=None, y_max=None) -> PlotSpec:
    """Check any kind of plot offline. items: (label, TI-Nspire command) pairs; a parametric
    command is 'x(t), y(t)', a polar one r(θ) (or 'r=...'), an implicit one 'lhs=rhs' or F
    (meaning F=0). Raises ValueError."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if kind == "function":
        lo = -10.0 if x_min is None else x_min
        hi = 10.0 if x_max is None else x_max
        if t_min is not None or t_max is not None:
            raise ValueError("t_min / t_max are for parametric and polar plots; use x_min / x_max")
        n = SAMPLES_DEFAULT["function"] if samples is None else samples
        return prepare([Func(lb, c) for lb, c in items], var or "x", lo, hi, n, y_min, y_max)
    if not items:
        raise ValueError("Give at least one curve: exprs (TI-Nspire syntax) or latex.")
    if kind == "implicit":
        return _prepare_implicit(items, var, t_min, t_max, samples, x_min, x_max, y_min, y_max)
    if len(items) > MAX_FUNCS // 2:
        raise ValueError(f"At most {MAX_FUNCS // 2} {kind} curves per plot.")
    var = (var or ("t" if kind == "parametric" else "θ")).strip()
    if not _NAME_RE.match(var) or var.lower().startswith("mcp"):  # mcpa.. are the helpers' own names
        raise ValueError("var must be a variable name, e.g. t or θ")
    a, b = _range("t_min", "t_max", 0.0 if t_min is None else t_min, 2 * math.pi if t_max is None else t_max)
    notes: list[str] = []
    n = _count(samples, SAMPLES_MIN, SAMPLES_MAX, SAMPLES_DEFAULT[kind], "", notes)
    grid = Grid(a, b, n)
    if grid.step <= 1e-12 * max(1.0, abs(a), abs(b)):
        raise ValueError(f"The {var} range is too narrow for that many samples.")
    vx, vX = _view("x_min", "x_max", x_min, x_max)
    vy, vY = _view("y_min", "y_max", y_min, y_max)
    funcs: list[Func] = []
    curves: list[Curve] = []
    helpers: list[tuple[str, str]] = []
    names: list[str] = []
    for label, cmd in items:
        if kind == "parametric":
            px, py = split_pair(cmd)
            parts = [strip_lhs(px, var, "x"), strip_lhs(py, var, "y")]
            for p in parts:
                check_command(p, label)
            funcs += [Func(label, parts[0]), Func(label, parts[1])]
        else:
            r = strip_lhs(cmd, var, "r")
            check_command(r, label)
            _check_func_body(r, label)
            parts = [r]
            for trig in ("cos", "sin"):
                name = f"{HELPER}p{len(funcs) + 1}"
                body = f"({r})*{trig}({var})"
                helpers.append((label, f"Define {name}(mcpa,mcpb,mcph)=Func:Local mcpm,{var}:mcpm:=setMode(2,1)"
                                       f":Return seq(approx({body}),{var},mcpa,mcpb,mcph):EndFunc"))
                names.append(name)
                funcs.append(Func(label, body, call=name))
        curves.append(Curve(label, parts, [len(funcs) - 2, len(funcs) - 1]))
    if kind == "polar":
        notes.append(f"{var} is in radians: the CAS computed r·cos {var} and r·sin {var} in radian mode for this "
                     f"plot only (the document's angle setting isn't changed).")
    return PlotSpec(funcs, var, grid, vy, vY, notes, kind, curves, vx, vX, None, helpers, names)


def _prepare_implicit(items, var, t_min, t_max, samples, x_min, x_max, y_min, y_max) -> PlotSpec:
    if var is not None and var.strip() not in ("x", "x,y"):
        raise ValueError("kind='implicit' plots F(x,y)=0 in x and y: leave var out")
    if t_min is not None or t_max is not None:
        raise ValueError("t_min / t_max are for parametric and polar plots; use x_min..y_max")
    if len(items) > MAX_IMPLICIT:
        raise ValueError(f"At most {MAX_IMPLICIT} implicit curves per plot.")
    a, b = _range("x_min", "x_max", -10.0 if x_min is None else x_min, 10.0 if x_max is None else x_max)
    c, d = _range("y_min", "y_max", -10.0 if y_min is None else y_min, 10.0 if y_max is None else y_max)
    notes: list[str] = []
    n = _count(samples, GRID_MIN, GRID_MAX, GRID_DEFAULT, " for implicit plots, per axis", notes)
    grid, ygrid = Grid(a, b, n), Grid(c, d, n)
    if grid.step <= 1e-12 * max(1.0, abs(a), abs(b)) or ygrid.step <= 1e-12 * max(1.0, abs(c), abs(d)):
        raise ValueError("The x or y range is too narrow for that many samples.")
    funcs: list[Func] = []
    curves: list[Curve] = []
    helpers: list[tuple[str, str]] = []
    names: list[str] = []
    for k, (label, cmd) in enumerate(items, 1):
        check_command(cmd, label)
        f = implicit_function(cmd, label)
        _check_func_body(f, label)
        name = f"{HELPER}f{k}"
        helpers.append((label, f"Define {name}(x,y)={f}"))
        names.append(name)
        first = len(funcs)
        funcs += [Func(label, f"{name}(x,{_num(ygrid.x(j))})") for j in range(n)]
        curves.append(Curve(label, [cmd.strip()], list(range(first, len(funcs)))))
    return PlotSpec(funcs, "x", grid, c, d, notes, "implicit", curves, a, b, ygrid, helpers, names)


# ---------------------------------------------------------------- CAS requests

def _num(v: float) -> str:
    """A float as a plain decimal TI-Nspire reads exactly as written (no exponent)."""
    return format(Decimal(repr(v + 0.0)), "f")


def seq_command(cmd: str, var: str, grid: Grid, i0: int, i1: int, call: str | None = None) -> str:
    """seq() of the samples i0..i1-1. The upper bound sits half a step past the last
    sample, so rounding can't add or drop one. With call, the temporary function that
    runs that seq (in radian mode) is called with the same bounds instead."""
    start, h = grid.x(i0), grid.step
    stop = start + (i1 - i0 - 0.5) * h
    if call:
        return f"{call}({_num(start)},{_num(stop)},{_num(h)})"
    return f"seq(approx({cmd}),{var},{_num(start)},{_num(stop)},{_num(h)})"


def _split_top(s: str) -> list[str]:
    """Split on commas outside brackets."""
    out, depth, cur = [], 0, []
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def parse_values(text: str) -> list[float | None]:
    """'{0.,0.841471,undef,1.2ᴇ−5}' -> [0.0, 0.841471, None, 1.2e-05].

    None is a gap: undef, ±∞, or a non-real value (one with 𝑖). Raises NotNumeric for
    anything else (a free variable, say) and ValueError if text isn't a list.
    """
    t = readable(text).strip()
    if not (t.startswith("{") and t.endswith("}")):
        raise ValueError(f"expected a list, got {t[:60]!r}")
    body = t[1:-1].strip()
    if not body:
        return []
    out: list[float | None] = []
    for k, item in enumerate(_split_top(body)):
        s = item.strip().replace(" ", "").replace("−", "-").replace("ᴇ", "e").replace("E", "e")
        if _NUM_RE.fullmatch(s):
            v = float(s)
            out.append(v if math.isfinite(v) else None)
        elif s in ("undef", "∞", "-∞", "+∞") or "𝑖" in s:
            out.append(None)
        else:
            raise NotNumeric(k, item.strip())
    return out


def _code(err: str) -> int | None:
    m = _CODE_RE.search(err)
    return int(m.group(1)) if m else None


def _split(f: int, i0: int, i1: int) -> list[tuple[int, int, int]]:
    m = i1 - i0
    parts = min(SPLIT, m)
    bounds = [i0 + m * k // parts for k in range(parts + 1)]
    return [(f, a, b) for a, b in zip(bounds, bounds[1:], strict=False)]


@dataclass
class Sampled:
    ys: list[list[float | None]]   # per channel, one per grid point
    errors: list[Counter]          # per channel: CAS error -> number of points it left empty
    requests: int = 0              # batches sent
    warnings: list[str] = field(default_factory=list)  # e.g. a temporary function not deleted


def sample(bridge, spec: PlotSpec, timeout_s: float) -> Sampled:
    """Evaluate every channel at every grid point on the CAS. Call inside bridge.session().

    The temporary functions (polar, implicit) are defined first and deleted again at the
    end, whatever happens. Then one seq() per channel, all in one batch. A range that fails (a non-real or
    domain error at some point, a result too long, a malformed reply) is retried as
    SPLIT smaller ranges, down to single points, which become gaps. After a timeout the
    unanswered ranges are retried once in pieces; a second timeout raises BridgeTimeout.
    An error that no point can avoid (a syntax error) or a result that isn't a number
    raises ValueError.
    """
    if not spec.helpers:
        return _sample(bridge, spec, timeout_s)
    res = bridge.eval_batch([d for _, d in spec.helpers], timeout_s)
    try:
        if bridge.last_timeout:
            from nspire_bridge import BridgeTimeout
            raise BridgeTimeout(f"{bridge.last_timeout} (nspire_plot: defining the temporary functions)")
        for (label, _), r in zip(spec.helpers, res, strict=True):
            if not r["ok"]:
                raise ValueError(f"{label}: {r['error']}")
        got = _sample(bridge, spec, timeout_s)
        got.requests += 1
    finally:
        warn = _delete_helpers(bridge, spec, timeout_s)
    if warn:
        got.warnings.append(warn)
    return got


def _delete_helpers(bridge, spec: PlotSpec, timeout_s: float) -> str | None:
    """DelVar the temporary functions. Returns a warning if that failed (never raises)."""
    names = ",".join(spec.helper_names)
    try:
        r = bridge.eval_batch([f"DelVar {names}"], min(timeout_s, 5.0))[0]
        if r["ok"]:
            return None
        why = r["error"]
    except Exception as e:  # a failed cleanup mustn't hide the real result or error
        why = str(e)
    return f"The temporary function(s) {names} may be left in the document ({why}); DelVar {names} removes them."


def _sample(bridge, spec: PlotSpec, timeout_s: float) -> Sampled:
    n, var = spec.grid.n, spec.var
    ys: list[list[float | None]] = [[None] * n for _ in spec.funcs]
    errors = [Counter() for _ in spec.funcs]
    todo = [(f, 0, n) for f in range(len(spec.funcs))]
    timed_out = False
    requests = 0
    while todo:
        exprs = [seq_command(spec.funcs[f].command, var, spec.grid, i0, i1, spec.funcs[f].call)
                 for f, i0, i1 in todo]
        res = bridge.eval_batch(exprs, timeout_s)
        requests += 1
        dead = bridge.last_timeout
        if dead:
            if timed_out:
                from nspire_bridge import BridgeTimeout  # here, not at import: it needs pywin32
                raise BridgeTimeout(f"{dead} (nspire_plot: try fewer samples, a larger timeout_s, or "
                                    f"fewer functions per call)")
            timed_out = True
        nxt: list[tuple[int, int, int]] = []
        for (f, i0, i1), r in zip(todo, res, strict=True):
            func = spec.funcs[f]
            if r["ok"]:
                try:
                    vals = parse_values(r["result"])
                except NotNumeric as e:
                    x = spec.grid.x(i0 + e.index)
                    at = f"{var}={x:.6g}"
                    bound = var
                    if spec.ygrid is not None:
                        at += f", y={spec.ygrid.x(f % spec.ygrid.n):.6g}"
                        bound = "x and y"
                    raise ValueError(
                        f"{func.label}: the CAS gave {e.item!r} at {at}, not a number. Every name "
                        f"other than {bound} needs a value: store it first (e.g. a:=2 with nspire_eval) or "
                        f"write the number in.") from None
                except ValueError as e:
                    vals, err = None, f"unexpected reply ({e})"
                else:
                    err = f"{len(vals)} values for {i1 - i0} points"
                if vals is not None and len(vals) == i1 - i0:
                    ys[f][i0:i1] = vals
                    continue
            else:
                err = r["error"]
                if dead and (err == dead or err.startswith("skipped: ")):
                    nxt += _split(f, i0, i1) if i1 - i0 > 1 else [(f, i0, i1)]
                    continue
                if _code(err) in _FATAL_CODES:
                    raise ValueError(f"{func.label}: {err}")
            if i1 - i0 == 1:
                errors[f][err] += 1  # this point stays empty
            else:
                nxt += _split(f, i0, i1)
        todo = nxt
    return Sampled(ys, errors, requests)


# ---------------------------------------------------------------- view and gaps

def _quantile(sorted_vals: list[float], p: float) -> float:
    return sorted_vals[round(p * (len(sorted_vals) - 1))]


def view_limits(all_ys: list[list[float | None]], y_min: float | None = None,
                y_max: float | None = None) -> tuple[float, float, bool]:
    """(lo, hi, clipped): the y range to show. Isolated spikes (points next to an
    asymptote) are left out of the automatic range: clipped is then True."""
    f = sorted(v for ys in all_ys for v in ys if v is not None)
    clipped = False
    if not f:
        lo, hi = -1.0, 1.0
    else:
        lo, hi = f[0], f[-1]
        if len(f) >= 20:
            q_lo, q_hi = _quantile(f, 0.05), _quantile(f, 0.95)
            core = q_hi - q_lo
            if core > 0 and hi - lo > 6 * core:
                lo, hi, clipped = max(lo, q_lo - core), min(hi, q_hi + core), True
        if hi == lo:
            d = abs(lo) * 0.1 or 1.0
            lo, hi = lo - d, hi + d
        else:
            d = (hi - lo) * 0.05
            lo, hi = lo - d, hi + d
    if y_min is not None or y_max is not None:
        span = hi - lo
        lo = y_min if y_min is not None else lo
        hi = y_max if y_max is not None else hi
        if not lo < hi:
            lo, hi = (lo, lo + span) if y_min is not None else (hi - span, hi)
        clipped = False
    return lo, hi, clipped


def breaks(ys: list[float | None], lo: float, hi: float) -> list[int]:
    """Indices i where the line from point i to i+1 must not be drawn: a jump across an
    asymptote. Either the two points are off opposite edges of the view, or the step is
    over half the view's height and goes against the steps on both sides of it (the curve
    climbs to +∞ and comes back from −∞)."""
    n = len(ys)

    def d(k: int) -> float | None:
        if 0 <= k < n - 1:
            a, b = ys[k], ys[k + 1]
            if a is not None and b is not None:
                return b - a
        return None

    span = hi - lo
    out = []
    for i in range(n - 1):
        a, b = ys[i], ys[i + 1]
        if a is None or b is None:
            continue
        if (a > hi and b < lo) or (a < lo and b > hi):
            out.append(i)
            continue
        di = b - a
        if abs(di) <= span / 2:
            continue
        around = [x for x in (d(i - 2), d(i - 1), d(i + 1), d(i + 2)) if x is not None]
        if around and all(x * di < 0 for x in around):
            out.append(i)
    return out


def runs(ys: list[float | None]) -> list[tuple[int, int]]:
    """(first, last) index of each run of empty points."""
    out, start = [], None
    for i, v in enumerate(ys + [0.0]):
        if v is None and start is None:
            start = i
        elif v is not None and start is not None:
            out.append((start, i - 1))
            start = None
    return out


# ---------------------------------------------------------------- curves and implicit grids

def curve_xy(got: Sampled, curve: Curve) -> tuple[list[float | None], list[float | None]]:
    """A parametric or polar curve's x and y per grid point: a gap (None in both) unless both are real."""
    xs, ys = got.ys[curve.channels[0]], got.ys[curve.channels[1]]
    keep = [a is not None and b is not None for a, b in zip(xs, ys, strict=True)]
    return ([a if k else None for a, k in zip(xs, keep, strict=True)],
            [b if k else None for b, k in zip(ys, keep, strict=True)])


@dataclass
class View:
    x_lo: float
    x_hi: float
    y_lo: float
    y_hi: float
    clipped: bool = False   # spikes left out of an automatic range


def plot_view(spec: PlotSpec, got: Sampled, points: tuple[list[float], list[float]] | None = None) -> View:
    """The window the image shows, the same for summary() and render_png()."""
    if spec.kind == "implicit":
        g, yg = spec.grid, spec.ygrid
        assert yg is not None
        return View(g.x_min, g.x_max, yg.x_min, yg.x_max)
    if spec.is_curve:
        xy = [curve_xy(got, c) for c in spec.curves]
        x_lo, x_hi, cx = view_limits([x for x, _ in xy], spec.x_min, spec.x_max)
        y_lo, y_hi, cy = view_limits([y for _, y in xy], spec.y_min, spec.y_max)
        return View(x_lo, x_hi, y_lo, y_hi, cx or cy)
    extra: list[list[float | None]] = [list(points[1])] if points else []
    lo, hi, clipped = view_limits(got.ys + extra, spec.y_min, spec.y_max)
    return View(spec.grid.x_min, spec.grid.x_max, lo, hi, clipped)


def curve_breaks(xs: list[float | None], ys: list[float | None], view: View) -> list[int]:
    """Jumps (asymptotes) in either coordinate: the segment i to i+1 isn't drawn."""
    return sorted(set(breaks(xs, view.x_lo, view.x_hi)) | set(breaks(ys, view.y_lo, view.y_hi)))


def implicit_rows(got: Sampled, curve: Curve) -> list[list[float | None]]:
    """F on the grid: rows[j][i] = F(x_i, y_j) as the CAS computed it (None: a gap)."""
    return [got.ys[c] for c in curve.channels]


def pole_points(rows: list[list[float | None]]) -> set[tuple[int, int]]:
    """Grid points (j, i) on either side of a sign change where F jumps through ±∞ (y = 1/x
    written as y-1/x=0) rather than through 0: the step across goes against the steps next
    to it on both sides (as in breaks()). Left out of the contour, so no line is drawn
    along a pole. Checked along rows and along columns."""
    out: set[tuple[int, int]] = set()

    def scan(line: list[float | None], at) -> None:
        m = len(line)
        for k in range(m - 1):
            a, b = line[k], line[k + 1]
            if a is None or b is None or a * b >= 0:
                continue
            d = b - a
            around = []
            before, after = (line[k - 1] if k > 0 else None), (line[k + 2] if k + 2 < m else None)
            if before is not None:
                around.append(a - before)
            if after is not None:
                around.append(after - b)
            if around and all(x * d < 0 for x in around):
                out.update((at(k), at(k + 1)))

    for j, row in enumerate(rows):
        scan(row, lambda i, j=j: (j, i))
    for i in range(len(rows[0]) if rows else 0):
        scan([r[i] for r in rows], lambda j, i=i: (j, i))
    return out


def crossing_cells(rows: list[list[float | None]], skip: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Grid cells (j, i) whose four corners are real, not at a pole, and where F changes sign
    (or is 0 at a corner): the contour at 0 passes through them."""
    out = []
    for j in range(len(rows) - 1):
        for i in range(len(rows[j]) - 1):
            corners = [(j, i), (j, i + 1), (j + 1, i), (j + 1, i + 1)]
            if any(c in skip for c in corners):
                continue
            vals = [v for v in (rows[a][b] for a, b in corners) if v is not None]
            if len(vals) < 4:
                continue
            lo, hi = min(vals), max(vals)
            if lo <= 0 <= hi and (lo < 0 or hi > 0):
                out.append((j, i))
    return out


def cell_extent(spec: PlotSpec, cells: list[tuple[int, int]]) -> tuple[list[float], list[float]] | None:
    """[x_lo, x_hi], [y_lo, y_hi] of the grid cells given; None for none."""
    if not cells or spec.ygrid is None:
        return None
    g, yg = spec.grid, spec.ygrid
    xs = [g.x(i) for _, i in cells] + [g.x(i + 1) for _, i in cells]
    ys = [yg.x(j) for j, _ in cells] + [yg.x(j + 1) for j, _ in cells]
    return [min(xs), max(xs)], [min(ys), max(ys)]


def curve_title(spec: PlotSpec, curve: Curve) -> str:
    if spec.kind == "parametric":
        return f"({curve.parts[0]}, {curve.parts[1]})"
    if spec.kind == "polar":
        return f"r = {curve.parts[0]}"
    p = curve.parts[0]
    return p if "=" in p else f"{p} = 0"


def curve_info(spec: PlotSpec, got: Sampled) -> list[dict]:
    """Per function or curve, for the tool's structured result: label, nspire (the TI-Nspire
    commands), points (real values), total, x_range / y_range of the values (implicit: of the
    cells the curve passes through; None when there are none)."""
    def rng(vals: list[float]) -> list[float] | None:
        return [min(vals), max(vals)] if vals else None
    out = []
    if spec.kind == "function":
        for f, ys in zip(spec.funcs, got.ys, strict=True):
            vals = [v for v in ys if v is not None]
            out.append({"label": f.label, "nspire": [f.command], "points": len(vals), "total": spec.grid.n,
                        "x_range": [spec.grid.x_min, spec.grid.x_max], "y_range": rng(vals)})
        return out
    for c in spec.curves:
        if spec.kind == "implicit":
            rows = implicit_rows(got, c)
            ext = cell_extent(spec, crossing_cells(rows, pole_points(rows)))
            out.append({"label": c.label, "nspire": c.parts, "points": sum(v is not None for r in rows for v in r),
                        "total": sum(len(r) for r in rows),
                        "x_range": ext[0] if ext else None, "y_range": ext[1] if ext else None})
        else:
            px, py = curve_xy(got, c)
            xs = [v for v in px if v is not None]
            ys = [v for v in py if v is not None]
            out.append({"label": c.label, "nspire": c.parts, "points": len(xs), "total": spec.grid.n,
                        "x_range": rng(xs), "y_range": rng(ys)})
    return out


# ---------------------------------------------------------------- annotations
#
# Zeros, extrema and intersections of function plots, all found by the CAS:
#   1. one batch with zeros(expr,x)|x>=a and x<=b per function / pair (expr: f, derivative(f,x)
#      or (f)-(g)); TI-Nspire gives every point in the window, exact when it can (−√(2), π/4)
#      and as decimals otherwise. derivative(), not d(): the bridge leaves d() unevaluated.
#   2. where that fails (an error, a reply that isn't a list of points such as {n1*π}, or an
#      empty list although the samples change sign), nSolve(expr=0,x,lo,hi) in each bracket
#      between neighbouring samples where the CAS's values change sign (Python only picks where
#      nSolve looks; the brackets next to an asymptote are left out).
#   3. one batch with {f,approx(x),approx(f)[,approx(derivative(f,x,2))]}|x=(point) per point:
#      the exact y, both decimals, and for a critical point the second derivative, whose sign
#      says minimum or maximum. When it is 0 (x^3, x^4 at 0), the sign of derivative(f,x) just
#      left and right of the point, also from the CAS, decides; no sign change is "stationary".
# A separate batch from the sampling, so a slow zeros() can't cost the plot itself.

ANNOTATIONS = ("zeros", "extrema", "intersections")
MAX_MARKS = 40       # marked points per plot, all kinds together
MAX_PER_SET = 12     # per function (or pair of functions) and kind
MAX_LABELS = 20      # with more points than this on the image, they are drawn without labels
_NOT_A_POINT_RE = re.compile(r"\b[nc]\d+\b|=|\bor\b|\band\b")  # n1 (any integer), c1, a condition
MARK_TYPES = ("zero", "max", "min", "stationary", "intersection")


def prepare_annotations(spec: PlotSpec, annotate) -> list[str]:
    """Check the annotate argument offline: a list of ANNOTATIONS names (a single name is
    fine). Returns them without repeats; [] for none. Raises ValueError."""
    if not annotate:
        return []
    if isinstance(annotate, str):
        annotate = [annotate]
    if spec.kind != "function":
        raise ValueError("annotate works on function plots (kind='function') only: zeros, extrema and "
                         "intersections of y=f(x). For a curve, find its points with nspire_eval (solve, zeros).")
    out: list[str] = []
    for a in annotate:
        if a not in ANNOTATIONS:
            raise ValueError(f"annotate: {a!r} isn't one of {', '.join(ANNOTATIONS)}")
        if a not in out:
            out.append(a)
    if "intersections" in out and len(spec.funcs) < 2:
        out.remove("intersections")
        spec.notes.append("Intersections need two or more functions: none marked.")
    return out


@dataclass(frozen=True)
class AnnotationTask:
    kind: str                # "zeros", "extrema" or "intersections"
    funcs: tuple[int, ...]   # indices into spec.funcs: one, or two for an intersection
    expr: str                # zero at the points wanted: (f), derivative((f),x) or (f)-(g)


@dataclass
class Mark:
    type: str          # one of MARK_TYPES
    funcs: list[int]   # indices into spec.funcs
    x: str             # as the CAS gave it: exact (−√(2)) or, found numerically, a decimal
    y: str
    x_approx: float
    y_approx: float
    method: str        # "exact" (zeros) or "numeric" (nSolve)

    def as_dict(self) -> dict:
        return {"type": self.type, "functions": [k + 1 for k in self.funcs], "x": self.x, "y": self.y,
                "x_approx": self.x_approx, "y_approx": self.y_approx, "method": self.method}


@dataclass
class Annotations:
    marks: list[Mark] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)      # what was capped or found numerically
    warnings: list[str] = field(default_factory=list)   # what failed
    errors: list[str] = field(default_factory=list)     # CAS error texts, for nspire_errors
    requests: int = 0


def annotation_tasks(spec: PlotSpec, which: list[str]) -> list[AnnotationTask]:
    v, fs = spec.var, spec.funcs
    tasks = []
    for k, f in enumerate(fs):
        if "zeros" in which:
            tasks.append(AnnotationTask("zeros", (k,), f"({f.command})"))
        if "extrema" in which:
            tasks.append(AnnotationTask("extrema", (k,), f"derivative(({f.command}),{v})"))
    if "intersections" in which:
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                tasks.append(AnnotationTask("intersections", (i, j), f"({fs[i].command})-({fs[j].command})"))
    return tasks


def exact_command(spec: PlotSpec, task: AnnotationTask) -> str:
    v, g = spec.var, spec.grid
    return f"zeros({task.expr},{v})|{v}>={_num(g.x_min)} and {v}<={_num(g.x_max)}"


def numeric_command(spec: PlotSpec, task: AnnotationTask, lo: float, hi: float) -> str:
    return f"nSolve({task.expr}=0,{spec.var},{_num(lo)},{_num(hi)})"


def point_command(spec: PlotSpec, task: AnnotationTask, x: str) -> str:
    """The exact y, approx x, approx y (and for extrema the second derivative) at x."""
    v, f = spec.var, f"({spec.funcs[task.funcs[0]].command})"
    second = f",approx(derivative({f},{v},2))" if task.kind == "extrema" else ""
    return f"{{{f},approx({v}),approx({f}){second}}}|{v}=({x})"


def slope_command(spec: PlotSpec, task: AnnotationTask, x: str, dx: float) -> str:
    v, f = spec.var, f"({spec.funcs[task.funcs[0]].command})"
    return f"approx(derivative({f},{v})|{v}=({x}){'+' if dx > 0 else '-'}{_num(abs(dx))})"


def number(text: str) -> float | None:
    """A CAS decimal ('−1.4142135623731', '3.ᴇ−4') as a float; None for anything else."""
    s = readable(text).strip().replace(" ", "").replace("−", "-").replace("ᴇ", "e").replace("E", "e")
    if _NUM_RE.fullmatch(s):
        v = float(s)
        return v if math.isfinite(v) else None
    return None


def exact_points(reply: dict) -> list[str] | None:
    """zeros()'s points as the CAS wrote them (to send back), or None when the reply isn't a
    list of single points: an error, {n1*π} (every multiple), a condition."""
    if not reply["ok"]:
        return None
    t = reply["result"].strip()
    if not (t.startswith("{") and t.endswith("}")):
        return None
    body = t[1:-1].strip()
    if not body:
        return []
    items = [s.strip() for s in _split_top(body)]
    if any(not s or _NOT_A_POINT_RE.search(readable(s)) for s in items):
        return None
    return items


def brackets(spec: PlotSpec, got: Sampled, task: AnnotationTask, view: View) -> list[tuple[float, float]]:
    """Where nSolve should look: between neighbouring samples whose CAS values change sign
    (zeros: f; intersections: f−g; extrema: the steps between samples). Jumps across an
    asymptote (breaks()) aren't brackets. Python only chooses the intervals."""
    g = spec.grid
    ys = got.ys[task.funcs[0]]
    cut = set(breaks(ys, view.y_lo, view.y_hi))
    out: list[tuple[float, float]] = []
    if task.kind == "extrema":
        for i in range(1, g.n - 1):
            a, b, c = ys[i - 1], ys[i], ys[i + 1]
            if a is None or b is None or c is None or i - 1 in cut or i in cut:
                continue
            if (b - a) * (c - b) < 0:
                out.append((g.x(i - 1), g.x(i + 1)))
        return out
    if task.kind == "intersections":
        other = got.ys[task.funcs[1]]
        cut |= set(breaks(other, view.y_lo, view.y_hi))
        ys = [a - b if a is not None and b is not None else None for a, b in zip(ys, other, strict=True)]
    for i in range(g.n - 1):
        a, b = ys[i], ys[i + 1]
        if a is None or b is None or i in cut:
            continue
        if a == 0:
            out.append((g.x(max(i - 1, 0)), g.x(i + 1)))
        elif a * b < 0 or (b == 0 and i + 1 == g.n - 1):  # a sign change, or a zero at the last sample
            out.append((g.x(i), g.x(i + 1)))
    return out


def _what(spec: PlotSpec, task: AnnotationTask) -> str:
    names = " and ".join(f"{k + 1} ({_short_label(spec.funcs[k].label)})" for k in task.funcs)
    return {"zeros": "zeros of ", "extrema": "extrema of ", "intersections": "intersections of "}[task.kind] + names


def _short_label(s: str, limit: int = 30) -> str:
    return s if len(s) <= limit else s[:limit - 1] + "…"


def annotate(bridge, spec: PlotSpec, got: Sampled, which: list[str], timeout_s: float) -> Annotations:
    """Find the points to mark. Call inside bridge.session(), after sample(). Never raises for
    a CAS error or a timeout: what failed is a warning and isn't marked."""
    out = Annotations()
    tasks = annotation_tasks(spec, which)
    if not tasks:
        return out
    view = plot_view(spec, got)
    g = spec.grid

    def dead(stage: str) -> bool:
        if not bridge.last_timeout:
            return False
        out.warnings.append(f"The CAS didn't finish the annotations ({stage}) in time: {bridge.last_timeout} "
                            f"Nothing is marked; try a larger timeout_s or fewer annotations.")
        return True

    # 1. exact points
    res = bridge.eval_batch([exact_command(spec, t) for t in tasks], timeout_s)
    out.requests += 1
    if dead("zeros"):
        return out
    found: list[list[tuple[str, str]]] = [[] for _ in tasks]
    numeric: list[tuple[int, float, float]] = []
    for k, (t, r) in enumerate(zip(tasks, res, strict=True)):
        pts = exact_points(r)
        if pts:
            if len(pts) > MAX_PER_SET:
                out.notes.append(f"{_what(spec, t)}: the CAS found {len(pts)}; the first {MAX_PER_SET} are marked.")
            found[k] = [(p, "exact") for p in pts[:MAX_PER_SET]]
            continue
        br = brackets(spec, got, t, view)
        if pts is None:
            why = r["error"] if not r["ok"] else f"zeros() gave {readable(r['result'])[:60]}"
            if not r["ok"]:
                out.errors.append(r["error"])
            if br:
                out.notes.append(f"{_what(spec, t)}: {why}; looked for them numerically (nSolve between "
                                 f"neighbouring samples) instead.")
            else:
                out.warnings.append(f"{_what(spec, t)}: {why}; none marked.")
        if len(br) > MAX_PER_SET:
            out.notes.append(f"{_what(spec, t)}: only the first {MAX_PER_SET} of {len(br)} sign changes searched.")
        numeric += [(k, lo, hi) for lo, hi in br[:MAX_PER_SET]]

    # 2. numeric fallback
    if numeric:
        res = bridge.eval_batch([numeric_command(spec, tasks[k], lo, hi) for k, lo, hi in numeric], timeout_s)
        out.requests += 1
        if dead("nSolve"):
            return out
        seen: dict[int, list[float]] = {}
        for (k, _, _), r in zip(numeric, res, strict=True):
            if not r["ok"]:
                out.errors.append(r["error"])
                continue
            v = number(r["result"])  # "No solution found" is a string: nothing there
            if v is None:
                continue
            if any(abs(v - w) <= g.step * 1e-3 for w in seen.setdefault(k, [])):
                continue  # neighbouring brackets found the same point
            seen[k].append(v)
            found[k].append((r["result"].strip(), "numeric"))
        for k in seen:
            if not any(m == "exact" for _, m in found[k]):
                out.notes.append(f"{_what(spec, tasks[k])}: found numerically (nSolve), not exactly.")

    # 3. the points' values
    todo = [(k, x, m) for k, pts in enumerate(found) for x, m in pts]
    if len(todo) > MAX_MARKS:
        out.notes.append(f"{len(todo)} points found; the first {MAX_MARKS} are marked.")
        todo = todo[:MAX_MARKS]
    if not todo:
        return out
    exprs: list[str] = []
    for k, x, _ in todo:
        exprs.append(point_command(spec, tasks[k], x))
        if tasks[k].kind == "extrema":
            exprs += [slope_command(spec, tasks[k], x, -g.step / 4), slope_command(spec, tasks[k], x, g.step / 4)]
    res = bridge.eval_batch(exprs, timeout_s)
    out.requests += 1
    if dead("values at the points"):
        return out
    it = iter(res)
    tol = g.step * 1e-6
    for k, x, method in todo:
        t = tasks[k]
        r = next(it)
        slopes = [next(it), next(it)] if t.kind == "extrema" else []
        if not r["ok"]:
            out.errors.append(r["error"])
            continue
        body = r["result"].strip()
        items = _split_top(body[1:-1]) if body.startswith("{") and body.endswith("}") else []
        vals = [number(s) for s in items]
        if len(items) < 3 or vals[1] is None or vals[2] is None:
            out.notes.append(f"{_what(spec, t)}: no real value at {spec.var} = {readable(x)}; not marked.")
            continue
        xa, ya = vals[1], vals[2]
        assert xa is not None and ya is not None
        if not g.x_min - tol <= xa <= g.x_max + tol:
            continue
        if t.kind == "zeros":
            kind = "zero"
        elif t.kind == "intersections":
            kind = "intersection"
        else:
            kind = _classify(vals[3] if len(vals) > 3 else None,
                             [number(s["result"]) if s["ok"] else None for s in slopes])
        out.marks.append(Mark(kind, list(t.funcs), readable(x).strip(), readable(items[0]).strip(),
                              xa, ya, method))
    out.marks.sort(key=lambda m: (MARK_TYPES.index(m.type) if m.type in MARK_TYPES else 9, m.funcs, m.x_approx))
    return out


def _classify(second: float | None, slopes: list[float | None]) -> str:
    """A critical point by the CAS's second derivative there; when that is 0 or not a number,
    by the CAS's first derivative just left and right of it."""
    if second is not None and second != 0:
        return "min" if second > 0 else "max"
    left, right = (slopes + [None, None])[:2]
    if left is not None and right is not None:
        if left < 0 < right:
            return "min"
        if left > 0 > right:
            return "max"
    return "stationary"


_TYPE_WORDS = {"zero": ("zero", "zeros"), "max": ("maximum", "maxima"), "min": ("minimum", "minima"),
               "stationary": ("stationary point (not an extremum)", "stationary points (not extrema)"),
               "intersection": ("intersection", "intersections")}


def _value(exact: str, approx: float) -> str:
    """'−√(2) ≈ −1.41421', or one number when the CAS's form is already a decimal."""
    if number(exact) is not None:
        return _g(number(exact) or 0.0)
    short = _g(approx)
    return exact if exact.replace("−", "-") == short.replace("−", "-") else f"{exact} ≈ {short}"


def marks_summary(spec: PlotSpec, ann: Annotations) -> list[str]:
    """Lines for the text summary: each set of marked points, exact and decimal."""
    lines: list[str] = []
    if ann.marks:
        lines.append("Marked points (every coordinate computed by the TI-Nspire CAS):")
    groups: dict[tuple[str, tuple[int, ...]], list[Mark]] = {}
    for m in ann.marks:
        groups.setdefault((m.type, tuple(m.funcs)), []).append(m)
    for (typ, funcs), ms in groups.items():
        word = _TYPE_WORDS[typ][len(ms) > 1]
        of = " and ".join(str(k + 1) for k in funcs)
        if typ == "zero":
            pts = [f"{spec.var} = {_value(m.x, m.x_approx)}" for m in ms]
        else:
            pts = [f"({_value(m.x, m.x_approx)}, {_value(m.y, m.y_approx)})" for m in ms]
        numeric = " (found numerically)" if all(m.method == "numeric" for m in ms) else ""
        lines.append(f"- {word} of {of}{numeric}: " + "; ".join(pts))
    lines += ann.notes
    return lines


# ---------------------------------------------------------------- output

def _g(v: float, scale: float = 0.0) -> str:
    """v to 6 digits, with the Unicode minus. Below scale·1e-9 it is 0 (rounding noise)."""
    if abs(v) < scale * 1e-9:
        v = 0.0
    return f"{v + 0.0:.6g}".replace("-", "−")


def _where(grid: Grid, var: str, spans: list[tuple[int, int]], limit: int = 4) -> str:
    def gx(i: int) -> str:
        return _g(grid.x(i), grid.step)
    parts = []
    for a, b in spans[:limit]:
        parts.append(f"{var}≈{gx(a)}" if a == b else f"{var} in [{gx(a)}, {gx(b)}]")
    if len(spans) > limit:
        parts.append(f"{len(spans) - limit} more")
    return ", ".join(parts)


def _tail(lines: list[str], spec: PlotSpec, got: Sampled, warnings: list[str] | None) -> str:
    lines += spec.notes
    warnings = list(got.warnings) + list(warnings or [])
    if warnings:
        lines.append("Warnings:")
        lines += [f"- {w}" for w in warnings]
    return "\n".join(lines)


def _errs(got: Sampled, channels: list[int]) -> Counter:
    out: Counter = Counter()
    for c in channels:
        out.update(got.errors[c])
    return out


def _head(label: str, cmd: str) -> str:
    return label if label.replace(" ", "") == cmd.replace(" ", "") else f"{label}  (TI-Nspire: {cmd})"


def _curve_summary(spec: PlotSpec, got: Sampled, warnings: list[str] | None) -> str:
    g, var, many = spec.grid, spec.var, len(spec.curves) > 1
    view = plot_view(spec, got)
    if spec.kind == "polar":
        lines = [f"Plotted {len(spec.curves)} polar curve{'s' if many else ''} r({var}) for {var} from "
                 f"{_g(g.x_min)} to {_g(g.x_max)}: {g.n} points each; the TI-Nspire CAS computed x = r·cos {var} "
                 f"and y = r·sin {var} at every point (the image is drawn from those values)."]
    else:
        lines = [f"Plotted {len(spec.curves)} parametric curve{'s' if many else ''} for {var} from "
                 f"{_g(g.x_min)} to {_g(g.x_max)}: {g.n} points each, every x and y computed by the TI-Nspire CAS "
                 f"(the image is drawn from those values)."]
    for k, c in enumerate(spec.curves, 1):
        head = _head(c.label, curve_title(spec, c))
        px, py = curve_xy(got, c)
        errs = _errs(got, c.channels)
        xs = [v for v in px if v is not None]
        ys = [v for v in py if v is not None]
        if not xs:
            why = f" ({errs.most_common(1)[0][0]})" if errs else ""
            lines.append(f"{k}. {head}: no real point anywhere in this range{why}.")
            continue
        bits = [f"x from {_g(min(xs))} to {_g(max(xs))}, y from {_g(min(ys))} to {_g(max(ys))}"]
        empty = runs(px)
        if empty:
            why = f"; TI-Nspire: {errs.most_common(1)[0][0]}" if errs else ""
            bits.append(f"undefined or non-real at {g.n - len(xs)} of {g.n} points ({_where(g, var, empty)}{why})")
        br = curve_breaks(px, py, view)
        if br:
            near = [f"{var}≈{_g(g.x(i) + g.step / 2, g.step)}" for i in br[:6]]
            if len(br) > 6:
                near.append(f"{len(br) - 6} more")
            bits.append("not connected across jumps (asymptotes) near " + ", ".join(near))
        lines.append(f"{k}. {head}: " + "; ".join(bits) + ".")
    if view.clipped:
        lines.append(f"The view shows x from {_g(view.x_lo)} to {_g(view.x_hi)} and y from {_g(view.y_lo)} to "
                     f"{_g(view.y_hi)}; spikes beyond that are cut off (set x_min..y_max to change it).")
    return _tail(lines, spec, got, warnings)


def _implicit_summary(spec: PlotSpec, got: Sampled, warnings: list[str] | None) -> str:
    g, yg, many = spec.grid, spec.ygrid, len(spec.curves) > 1
    assert yg is not None
    lines = [f"Plotted {len(spec.curves)} implicit curve{'s' if many else ''} F(x,y)=0 for x from {_g(g.x_min)} "
             f"to {_g(g.x_max)} and y from {_g(yg.x_min)} to {_g(yg.x_max)}: the TI-Nspire CAS computed F on a "
             f"{g.n}×{yg.n} grid, and the curve is drawn where F changes sign (a contour at 0, interpolated "
             f"between grid points)."]
    for k, c in enumerate(spec.curves, 1):
        head = _head(c.label, curve_title(spec, c))
        rows = implicit_rows(got, c)
        errs = _errs(got, c.channels)
        total = g.n * yg.n
        real = sum(v is not None for r in rows for v in r)
        if not real:
            why = f" ({errs.most_common(1)[0][0]})" if errs else ""
            lines.append(f"{k}. {head}: F has no real value anywhere in this window{why}.")
            continue
        poles = pole_points(rows)
        ext = cell_extent(spec, crossing_cells(rows, poles))
        if ext:
            n_cells = len(crossing_cells(rows, poles))
            bits = [f"passes through {n_cells} grid cells, x from about {_g(ext[0][0])} to {_g(ext[0][1])}, "
                    f"y from about {_g(ext[1][0])} to {_g(ext[1][1])}"]
        else:
            bits = ["F doesn't change sign anywhere in this window, so nothing is drawn (a curve where F only "
                    "touches 0, like (x−y)^2=0, can't be found this way: write it as x−y=0)"]
        if real < total:
            why = f"; TI-Nspire: {errs.most_common(1)[0][0]}" if errs else ""
            bits.append(f"F undefined or non-real at {total - real} of {total} grid points{why}")
        if poles:
            bits.append(f"not drawn next to {len(poles)} grid points where F jumps through ±∞ (a pole, not a zero)")
        lines.append(f"{k}. {head}: " + "; ".join(bits) + ".")
    return _tail(lines, spec, got, warnings)


def summary(spec: PlotSpec, got: Sampled, warnings: list[str] | None = None,
            ann: Annotations | None = None) -> str:
    if spec.kind == "implicit":
        return _implicit_summary(spec, got, warnings)
    if spec.is_curve:
        return _curve_summary(spec, got, warnings)
    g, var = spec.grid, spec.var
    lo, hi, clipped = view_limits(got.ys, spec.y_min, spec.y_max)
    lines = [f"Plotted {len(spec.funcs)} function{'s' if len(spec.funcs) > 1 else ''} for {var} from "
             f"{_g(g.x_min)} to {_g(g.x_max)}: {g.n} points each, every value computed by the TI-Nspire CAS "
             f"(the image is drawn from those values)."]
    for k, (func, ys, errs) in enumerate(zip(spec.funcs, got.ys, got.errors, strict=True), 1):
        head = func.label if func.label == func.command else f"{func.label}  (TI-Nspire: {func.command})"
        vals = [v for v in ys if v is not None]
        if not vals:
            why = f" ({errs.most_common(1)[0][0]})" if errs else ""
            lines.append(f"{k}. {head}: no real value anywhere in this range{why}.")
            continue
        bits = [f"y from {_g(min(vals))} to {_g(max(vals))}"]
        empty = runs(ys)
        if empty:
            why = f"; TI-Nspire: {errs.most_common(1)[0][0]}" if errs else ""
            bits.append(f"undefined or non-real at {len(ys) - len(vals)} of {g.n} points "
                        f"({_where(g, var, empty)}{why})")
        br = breaks(ys, lo, hi)
        if br:
            near = [f"{var}≈{_g(g.x(i) + g.step / 2, g.step)}" for i in br[:6]]
            if len(br) > 6:
                near.append(f"{len(br) - 6} more")
            bits.append("not connected across jumps (asymptotes) near " + ", ".join(near))
        lines.append(f"{k}. {head}: " + "; ".join(bits) + ".")
    if clipped:
        lines.append(f"The y axis shows {_g(lo)} to {_g(hi)}; spikes beyond that are cut off "
                     f"(set y_min / y_max to change it).")
    if ann is not None:
        lines += marks_summary(spec, ann)
        off = len(ann.marks) - len(visible_marks(ann.marks, View(g.x_min, g.x_max, lo, hi)))
        if off:
            lines.append(f"{off} marked point{'s are' if off > 1 else ' is'} outside the y range "
                         f"shown, so not drawn.")
        warnings = ann.warnings + list(warnings or [])
    return _tail(lines, spec, got, warnings)


def _label(text: str, limit: int = 60) -> str:
    """A legend or title label: TI-Nspire syntax, shortened, safe from mathtext."""
    t = text if len(text) <= limit else text[:limit - 1] + "…"
    t = t.replace("𝑖", "i").replace("$", r"\$")  # $ would start matplotlib's mathtext
    return " " + t if t.startswith("_") else t   # a leading _ hides a legend entry




def _frame(fig, view: View):
    """An axes with the house style: light grid, no top/right spines, the axes through 0."""
    ax = fig.add_subplot()
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelcolor=INK_2, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    if view.y_lo < 0 < view.y_hi:
        ax.axhline(0, color=AXIS, linewidth=1, zorder=1)
    if view.x_lo < 0 < view.x_hi:
        ax.axvline(0, color=AXIS, linewidth=1, zorder=1)
    return ax


def _line(ax, xs: list[float | None], ys: list[float | None], cut: set[int], color: str, label: str) -> None:
    """A polyline with NaN for the gaps and after each index in cut (a jump not to draw)."""
    nan = float("nan")
    px: list[float] = []
    py: list[float] = []
    for i, (x, y) in enumerate(zip(xs, ys, strict=True)):
        px.append(nan if x is None else x)
        py.append(nan if y is None else y)
        if i in cut:
            px.append(nan)
            py.append(nan)
    ax.plot(px, py, color=color, linewidth=1.6, solid_joinstyle="round", solid_capstyle="round",
            label=label, zorder=3)


def _draw_functions(ax, spec: PlotSpec, got: Sampled, view: View) -> None:
    g = spec.grid
    xs: list[float | None] = [g.x(i) for i in range(g.n)]
    for k, (func, ys) in enumerate(zip(spec.funcs, got.ys, strict=True)):
        cut = set(breaks(ys, view.y_lo, view.y_hi))
        _line(ax, xs, ys, cut, PALETTE[k], _label(func.command))


def _draw_curves(ax, spec: PlotSpec, got: Sampled, view: View) -> None:
    for k, c in enumerate(spec.curves):
        px, py = curve_xy(got, c)
        _line(ax, px, py, set(curve_breaks(px, py, view)), PALETTE[k], _label(curve_title(spec, c)))


def _draw_implicit(ax, spec: PlotSpec, got: Sampled) -> list:
    """Contours at 0 of the CAS's grid values; returns legend handles (contours have none)."""
    import numpy as np
    from matplotlib.lines import Line2D

    assert spec.ygrid is not None
    g, yg = spec.grid, spec.ygrid
    xs = [g.x(i) for i in range(g.n)]
    ys = [yg.x(j) for j in range(yg.n)]
    handles = []
    for k, c in enumerate(spec.curves):
        rows = implicit_rows(got, c)
        poles = pole_points(rows)
        z = np.array([[np.nan if v is None or (j, i) in poles else v for i, v in enumerate(r)]
                      for j, r in enumerate(rows)], dtype=float)
        if crossing_cells(rows, poles):
            ax.contour(xs, ys, np.ma.masked_invalid(z), levels=[0.0], colors=[PALETTE[k]], linewidths=1.6,
                       zorder=3)
        handles.append(Line2D([], [], color=PALETTE[k], linewidth=1.6, label=_label(curve_title(spec, c))))
    return handles


def _equal_aspect(ax, spec: PlotSpec, view: View) -> None:
    """Polar, parametric and implicit plots keep circles round when the window's sides are
    within a factor 3 of each other (polar: always)."""
    if spec.kind == "function":
        return
    ratio = (view.x_hi - view.x_lo) / (view.y_hi - view.y_lo)
    if spec.kind != "polar" and not 1 / 3 <= ratio <= 3:
        return
    ax.set_aspect("equal", adjustable="box")  # the plot box shrinks; the window stays as computed


_MARK_STYLE = {"zero": ("o", 36), "max": ("^", 48), "min": ("v", 48), "stationary": ("D", 30),
               "intersection": ("X", 56)}
_MARK_KEY = {"zero": "○ zero", "max": "▲ maximum", "min": "▼ minimum", "stationary": "◇ stationary",
             "intersection": "✕ intersection"}


def _coord(v: float, span: float) -> str:
    """A coordinate for a label: 3 significant digits, 0 for rounding noise."""
    if abs(v) < span * 1e-9:
        v = 0.0
    return f"{v + 0.0:.3g}".replace("-", "−")


def visible_marks(marks: list[Mark], view: View) -> list[Mark]:
    return [m for m in marks if view.x_lo <= m.x_approx <= view.x_hi and view.y_lo <= m.y_approx <= view.y_hi]


def _draw_marks(fig, ax, marks: list[Mark], view: View) -> None:
    """Each mark in its function's colour (intersections in ink), with its rounded coordinates
    unless there are more than MAX_LABELS, and a key of the marker shapes under the plot."""
    shown = visible_marks(marks, view)
    labels = len(shown) <= MAX_LABELS
    sx, sy = view.x_hi - view.x_lo, view.y_hi - view.y_lo
    done: set[str] = set()   # one label per spot (a zero of two functions that is also where they meet)
    for m in shown:
        color = INK if m.type == "intersection" else PALETTE[m.funcs[0] % len(PALETTE)]
        marker, size = _MARK_STYLE.get(m.type, ("o", 36))
        face = SURFACE if m.type in ("zero", "stationary") else color
        ax.scatter([m.x_approx], [m.y_approx], marker=marker, s=size, facecolors=face, edgecolors=color,
                   linewidths=1.3, zorder=5)
        text = f"({_coord(m.x_approx, sx)}, {_coord(m.y_approx, sy)})"
        if labels and text not in done:
            done.add(text)
            below = m.type == "min"
            ax.annotate(text, (m.x_approx, m.y_approx),
                        xytext=(5, -11 if below else 5), textcoords="offset points", fontsize=7, color=INK_2,
                        zorder=6, bbox={"boxstyle": "round,pad=0.15", "fc": SURFACE, "ec": "none", "alpha": 0.8})
    kinds = [t for t in MARK_TYPES if any(m.type == t for m in shown)]
    if kinds:
        fig.text(0.01, 0.01, "   ".join(_MARK_KEY[t] for t in kinds) + "  (points: TI-Nspire CAS)",
                 ha="left", va="bottom", color=MUTED, fontsize=7)


def render_png(spec: PlotSpec, got: Sampled, points: tuple[list[float], list[float]] | None = None,
               title: str | None = None, xlabel: str | None = None, ylabel: str | None = None,
               marks: list[Mark] | None = None) -> bytes:
    """Draw the sampled values. Imports matplotlib (slow the first time).

    points: data points (xs, ys) drawn as dots under the curves (nspire_stats' data; given, not
    computed). title replaces the single function's or curve's label as the title. marks:
    annotate()'s points (zeros, extrema, intersections), drawn on top."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    view = plot_view(spec, got, points)
    fig = Figure(figsize=(8, 5), dpi=110, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    ax = _frame(fig, view)
    if points:
        ax.scatter(points[0], points[1], s=22, color=INK_2, edgecolors=SURFACE, linewidths=0.6, zorder=4,
                   label="data")
    handles = None
    if spec.kind == "implicit":
        handles = _draw_implicit(ax, spec, got)
        labels = [curve_title(spec, c) for c in spec.curves]
    elif spec.is_curve:
        _draw_curves(ax, spec, got, view)
        labels = [curve_title(spec, c) for c in spec.curves]
    else:
        _draw_functions(ax, spec, got, view)
        labels = [f.command for f in spec.funcs]
    if marks:
        _draw_marks(fig, ax, marks, view)

    ax.set_xlim(view.x_lo, view.x_hi)
    ax.set_ylim(view.y_lo, view.y_hi)
    _equal_aspect(ax, spec, view)
    ax.set_xlabel(_label(xlabel) if xlabel else ("x" if spec.kind != "function" else spec.var),
                  color=INK_2, fontsize=10)
    if ylabel or spec.kind != "function":
        ax.set_ylabel(_label(ylabel) if ylabel else "y", color=INK_2, fontsize=10)
    if title is not None or len(labels) == 1:
        ax.set_title(_label(title if title is not None else labels[0], 80), loc="left", color=INK, fontsize=11)
    elif labels:
        ax.legend(handles=handles, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=min(len(labels), 4),
                  frameon=False, fontsize=9, labelcolor=INK_2, handlelength=1.6)
    fig.text(0.99, 0.01, "values: TI-Nspire CAS", ha="right", va="bottom", color=MUTED, fontsize=7)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    return buf.getvalue()
