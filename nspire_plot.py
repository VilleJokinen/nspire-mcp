"""Function plots for the nspire_plot tool: the TI-Nspire CAS computes every value,
matplotlib only draws them.

prepare() checks the arguments offline, sample() runs inside bridge.session() (one
seq(approx(...)) request per function, split into smaller ranges only when one fails),
and render_png() / summary() run after the session, with the user's clipboard back.
Nothing here imports pywin32, and matplotlib is imported only in render_png(), so the
server starts fast and the parsing and gap logic can be tested anywhere.
"""
from __future__ import annotations

import io
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal

from nspire_chars import readable

SAMPLES_MIN, SAMPLES_MAX = 10, 1000
MAX_FUNCS = 8           # one categorical colour each
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
    label: str    # as given: TI-Nspire syntax, or the LaTeX
    command: str  # what is sampled, in TI-Nspire syntax


@dataclass
class PlotSpec:
    funcs: list[Func]
    var: str
    grid: Grid
    y_min: float | None = None
    y_max: float | None = None
    notes: list[str] = field(default_factory=list)  # adjustments to mention in the summary


def _finite(name: str, v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{name} must be a finite number")
    return float(v)


def strip_lhs(expr: str, var: str) -> str:
    """'y=x^2' or 'f(x)=x^2' -> 'x^2': a function to plot, written as an equation."""
    names = r"[^\W\d_]\w*\(\s*" + re.escape(var) + r"\s*\)"
    if var.lower() != "y":
        names = "y|" + names
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


# ---------------------------------------------------------------- CAS requests

def _num(v: float) -> str:
    """A float as a plain decimal TI-Nspire reads exactly as written (no exponent)."""
    return format(Decimal(repr(v + 0.0)), "f")


def seq_command(cmd: str, var: str, grid: Grid, i0: int, i1: int) -> str:
    """seq() of the samples i0..i1-1. The upper bound sits half a step past the last
    sample, so rounding can't add or drop one."""
    start, h = grid.x(i0), grid.step
    stop = start + (i1 - i0 - 0.5) * h
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
    ys: list[list[float | None]]   # per function, one per grid point
    errors: list[Counter]          # per function: CAS error -> number of points it left empty
    requests: int = 0              # batches sent


def sample(bridge, spec: PlotSpec, timeout_s: float) -> Sampled:
    """Evaluate every function at every grid point on the CAS. Call inside bridge.session().

    First one seq() per function, all in one batch. A range that fails (a non-real or
    domain error at some point, a result too long, a malformed reply) is retried as
    SPLIT smaller ranges, down to single points, which become gaps. After a timeout the
    unanswered ranges are retried once in pieces; a second timeout raises BridgeTimeout.
    An error that no point can avoid (a syntax error) or a result that isn't a number
    raises ValueError.
    """
    n, var = spec.grid.n, spec.var
    ys: list[list[float | None]] = [[None] * n for _ in spec.funcs]
    errors = [Counter() for _ in spec.funcs]
    todo = [(f, 0, n) for f in range(len(spec.funcs))]
    timed_out = False
    requests = 0
    while todo:
        exprs = [seq_command(spec.funcs[f].command, var, spec.grid, i0, i1) for f, i0, i1 in todo]
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
                    raise ValueError(
                        f"{func.label}: the CAS gave {e.item!r} at {var}={x:.6g}, not a number. Every name "
                        f"other than {var} needs a value: store it first (e.g. a:=2 with nspire_eval) or "
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


def summary(spec: PlotSpec, got: Sampled, warnings: list[str] | None = None) -> str:
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
    lines += spec.notes
    if warnings:
        lines.append("Warnings:")
        lines += [f"- {w}" for w in warnings]
    return "\n".join(lines)


def _label(text: str, limit: int = 60) -> str:
    """A legend or title label: TI-Nspire syntax, shortened, safe from mathtext."""
    t = text if len(text) <= limit else text[:limit - 1] + "…"
    t = t.replace("𝑖", "i").replace("$", r"\$")  # $ would start matplotlib's mathtext
    return " " + t if t.startswith("_") else t   # a leading _ hides a legend entry


def render_png(spec: PlotSpec, got: Sampled, points: tuple[list[float], list[float]] | None = None,
               title: str | None = None, xlabel: str | None = None, ylabel: str | None = None) -> bytes:
    """Draw the sampled values. Imports matplotlib (slow the first time).

    points: data points (xs, ys) drawn as dots under the curves (nspire_stats' data; given, not
    computed). title replaces the single function's label as the title."""
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    g = spec.grid
    extra: list[list[float | None]] = [list(points[1])] if points else []
    lo, hi, _ = view_limits(got.ys + extra, spec.y_min, spec.y_max)
    xs = [g.x(i) for i in range(g.n)]
    nan = float("nan")

    fig = Figure(figsize=(8, 5), dpi=110, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    ax = fig.add_subplot()
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
    ax.tick_params(colors=MUTED, labelcolor=INK_2, labelsize=9)
    ax.grid(True, color=GRID, linewidth=0.7)
    ax.set_axisbelow(True)
    if lo < 0 < hi:
        ax.axhline(0, color=AXIS, linewidth=1, zorder=1)
    if g.x_min < 0 < g.x_max:
        ax.axvline(0, color=AXIS, linewidth=1, zorder=1)

    if points:
        ax.scatter(points[0], points[1], s=22, color=INK_2, edgecolors=SURFACE, linewidths=0.6, zorder=4,
                   label="data")
    for k, (func, ys) in enumerate(zip(spec.funcs, got.ys, strict=True)):
        cut = set(breaks(ys, lo, hi))
        px, py = [], []
        for i, (x, y) in enumerate(zip(xs, ys, strict=True)):
            px.append(x)
            py.append(nan if y is None else y)
            if i in cut:  # NaN breaks the line
                px.append(x + g.step / 2)
                py.append(nan)
        ax.plot(px, py, color=PALETTE[k], linewidth=1.6, solid_joinstyle="round", solid_capstyle="round",
                label=_label(func.command), zorder=3)

    ax.set_xlim(g.x_min, g.x_max)
    ax.set_ylim(lo, hi)
    ax.set_xlabel(_label(xlabel) if xlabel else spec.var, color=INK_2, fontsize=10)
    if ylabel:
        ax.set_ylabel(_label(ylabel), color=INK_2, fontsize=10)
    if title is not None or len(spec.funcs) == 1:
        ax.set_title(_label(title if title is not None else spec.funcs[0].command, 80), loc="left", color=INK,
                     fontsize=11)
    elif spec.funcs:
        ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=min(len(spec.funcs), 4), frameon=False,
                  fontsize=9, labelcolor=INK_2, handlelength=1.6)
    fig.text(0.99, 0.01, "values: TI-Nspire CAS", ha="right", va="bottom", color=MUTED, fontsize=7)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    return buf.getvalue()
