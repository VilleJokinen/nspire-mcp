"""Statistics for the nspire_stats tool: TI-Nspire's own OneVar, TwoVar and regression commands.

prepare() checks the command and picks the data's columns offline (the data itself is read by
nspire_data), commands() lists what to send, run() sends it in one batch inside
bridge.session(), and result() reads the replies: stat.results as rows of (name, value), and for
regressions the fitted equation (stat.RegEqn) with r² and r. The CAS computes every statistic;
Python only reads the numbers in and the CAS's text out.

The data is stored as the lists mcpstatx, mcpstaty (and mcpstatf for frequencies): server.py
runs the stores inside isolated_names(), so those names are put back (or deleted) afterwards.
The statistics command itself writes TI-Nspire's stat.* variables (stat.results, stat.RegEqn,
stat.r, ...), as it does on a handheld; they are left holding this call's results.

Facts checked on the real CAS (TI-Nspire CX CAS, bridge protocol 2): the commands run at top
level through math.evalStr (no function wrapper needed) and return an empty string;
stat.results is a matrix of ["name", value] rows; stat.RegEqn is a function (getType "FUNC"),
so stat.RegEqn(v) gives the fitted expression in v; too few points is error 235, PowerReg with
x <= 0 error 260. Labels use private-use characters for x̄ (U+F003), ȳ (U+F004) and the n of
sₙ₋₁x (U+F043).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from nspire_chars import readable
from nspire_data import Table, list_chunks, list_literal

X_LIST, Y_LIST, F_LIST = "mcpstatx", "mcpstaty", "mcpstatf"
EQ_VAR = "mcpstatv"           # the variable stat.RegEqn is called with, renamed x in the answer
CHUNK = 200                   # values per store request

# command: (columns of data, regression?, takes a frequency list?)
COMMANDS: dict[str, tuple[int, bool, bool]] = {
    "OneVar": (1, False, True),
    "TwoVar": (2, False, True),
    "LinRegMx": (2, True, True),
    "LinRegBx": (2, True, True),
    "MedMed": (2, True, True),
    "QuadReg": (2, True, True),
    "CubicReg": (2, True, True),
    "QuartReg": (2, True, True),
    "PowerReg": (2, True, True),
    "ExpReg": (2, True, True),
    "LnReg": (2, True, True),
    "Logistic": (2, True, False),
    "LogisticD": (2, True, False),
    "SinReg": (2, True, False),
}
DESCRIPTIONS = {
    "OneVar": "one-variable statistics", "TwoVar": "two-variable statistics",
    "LinRegMx": "linear y=m*x+b", "LinRegBx": "linear y=a+b*x", "MedMed": "median-median line",
    "QuadReg": "quadratic", "CubicReg": "cubic", "QuartReg": "quartic", "PowerReg": "power y=a*x^b",
    "ExpReg": "exponential y=a*b^x", "LnReg": "logarithmic y=a+b*ln(x)",
    "Logistic": "logistic y=c/(1+a*e^(-b*x))", "LogisticD": "logistic y=c/(1+a*e^(-b*x))+d",
    "SinReg": "sinusoidal y=a*sin(b*x+c)+d",
}
# Private-use characters in stat.results labels, and the no-break space in "sx := sₙ₋₁x".
_LABEL_CHARS = str.maketrans({"": "x̄", "": "ȳ", "": "ₙ", " ": " "})
_EQ_VAR_RE = re.compile(rf"\b{EQ_VAR}\b")


@dataclass
class StatsSpec:
    command: str
    table: Table
    x: int                        # column index of the data (OneVar) or of x
    y: int | None = None          # column index of y
    freq: int | None = None       # column index of the frequencies
    plot: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def regression(self) -> bool:
        return COMMANDS[self.command][1]

    def label(self, k: int | None) -> str:
        if k is None:
            return ""
        return self.table.header(k, f"column {k + 1}")

    @property
    def nspire(self) -> str:
        """The command as run, with the stored lists."""
        lists = [X_LIST] + ([Y_LIST] if self.y is not None else []) + ([F_LIST] if self.freq is not None else [])
        return f"{self.command} " + ",".join(lists)


def find_command(name: str) -> str:
    """The command's TI-Nspire spelling, case ignored. Raises ValueError for an unknown one."""
    for c in COMMANDS:
        if c.lower() == str(name).strip().lower():
            return c
    raise ValueError(f"unknown statistics command {name!r}; use one of: {', '.join(COMMANDS)}")


def _column(table: Table, ref, what: str) -> int:
    """A column by 1-based number or by header name."""
    n = len(table.columns)
    if isinstance(ref, bool):
        raise ValueError(f"{what} must be a column number (1-{n}) or a header name")
    if isinstance(ref, int):
        if not 1 <= ref <= n:
            raise ValueError(f"{what}={ref}: the data has {n} column(s)")
        return ref - 1
    if isinstance(ref, str):
        s = ref.strip()
        if s.isdigit():
            return _column(table, int(s), what)
        for k, h in enumerate(table.headers or []):
            if h.lower() == s.lower():
                return k
        names = ", ".join(repr(h) for h in table.headers or []) or "none"
        raise ValueError(f"{what}={ref!r}: no column with that header (headers: {names})")
    raise ValueError(f"{what} must be a column number (1-{n}) or a header name")


def prepare(command: str, table: Table, x_column=None, y_column=None, freq_column=None,
            plot: bool = False) -> StatsSpec:
    """Check the command and choose the columns, without touching the bridge. Raises ValueError.

    Defaults: OneVar uses column 1, the others column 1 as x and column 2 as y. Frequencies
    only when freq_column is given. Columns the command doesn't use are left out (a note says so).
    """
    cmd = find_command(command)
    n_cols, regression, takes_freq = COMMANDS[cmd]
    x = _column(table, 1 if x_column is None else x_column, "x_column")
    y = None
    if n_cols == 2:
        if y_column is None:
            y = next((k for k in range(len(table.columns)) if k != x), None)
            if y is None:
                raise ValueError(f"{cmd} needs two columns of data (x and y); the data has one.")
        else:
            y = _column(table, y_column, "y_column")
        if y == x:
            raise ValueError("x_column and y_column are the same column")
    elif y_column is not None:
        raise ValueError("OneVar uses one column: give it as x_column (y_column is for two-variable commands)")
    freq = None
    if freq_column is not None:
        if not takes_freq:
            raise ValueError(f"{cmd} doesn't take a frequency list here (only OneVar, TwoVar and the other "
                             f"regressions do)")
        freq = _column(table, freq_column, "freq_column")
        if freq in (x, y):
            raise ValueError("freq_column must be a different column from the data")
    if plot and n_cols == 1:
        raise ValueError("plot needs x and y data: use TwoVar or a regression (OneVar has one column)")
    spec = StatsSpec(cmd, table, x, y, freq, bool(plot))
    used = {x, y, freq} - {None}
    unused = [spec.label(k) for k in range(len(table.columns)) if k not in used]
    if unused:
        spec.notes.append("Not used: " + ", ".join(unused) + " (choose columns with x_column, y_column, "
                          "freq_column).")
    if not regression and plot:
        spec.notes.append("TwoVar fits nothing, so the plot shows only the data.")
    return spec


def _stores(name: str, column: list[str]) -> list[str]:
    parts = list_chunks(column, CHUNK)
    out = [f"{name}:={list_literal(parts[0])}"]
    out += [f"{name}:=augment({name},{list_literal(p)})" for p in parts[1:]]
    return out


def commands(spec: StatsSpec) -> tuple[list[str], list[str]]:
    """(stores, statistics): the list stores (run isolated) and what reads the results."""
    cols = spec.table.columns
    stores = _stores(X_LIST, cols[spec.x])
    if spec.y is not None:
        stores += _stores(Y_LIST, cols[spec.y])
    if spec.freq is not None:
        stores += _stores(F_LIST, cols[spec.freq])
    stats = [spec.nspire, "stat.results"]
    if spec.regression:
        stats.append(f"stat.RegEqn({EQ_VAR})")
    return stores, stats


def run(bridge, spec: StatsSpec, timeout_s: float) -> list[dict]:
    """Store the lists and run the command in one batch. Call inside bridge.session(), inside
    isolated_names() for the stores. Returns the replies to the statistics commands
    (commands()[1]). Raises ValueError when a store or the command fails, BridgeTimeout when
    the bridge stops answering."""
    stores, stats = commands(spec)
    res = bridge.eval_batch(stores + stats, timeout_s)
    if bridge.last_timeout:
        from nspire_bridge import BridgeTimeout  # here, not at import: it needs pywin32
        raise BridgeTimeout(bridge.last_timeout)
    for r in res[:len(stores)]:
        if not r["ok"]:
            raise ValueError(f"storing the data failed: {r['error']}")
    cmd = res[len(stores)]
    if not cmd["ok"]:
        raise ValueError(f"{spec.nspire}: {cmd['error']}")
    return res[len(stores):]


# ---------------------------------------------------------------- reading the results

def _split_items(s: str, sep: str = ",") -> list[str]:
    """Split on sep outside brackets and "strings" ("" inside a string is a quote)."""
    out, cur, depth, quoted = [], [], 0, False
    for ch in s:
        if ch == '"':
            quoted = not quoted
        elif not quoted:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth -= 1
            elif ch == sep and depth == 0:
                out.append("".join(cur))
                cur = []
                continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def _rows(s: str) -> list[str]:
    """'[a][b,c]' -> ['a', 'b,c']: the bracketed groups at the top level, quote-aware."""
    out, depth, quoted, start = [], 0, False, 0
    for i, ch in enumerate(s):
        if ch == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif ch == "[":
            if depth == 0:
                start = i + 1
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                out.append(s[start:i])
            elif depth < 0:
                raise ValueError("unbalanced ]")
    if depth != 0 or quoted:
        raise ValueError("unbalanced brackets or quotes")
    return out


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':
        return s[1:-1].replace('""', '"')
    return s


def parse_results(text: str) -> tuple[str | None, list[tuple[str, str]]]:
    """stat.results as (title, [(name, value), ...]). Names read: x̄, sx := sₙ₋₁x, r², ...
    Values are the CAS's text (numbers as written, strings without quotes, lists as {...}).
    Raises ValueError if text isn't a matrix of [name, value] rows."""
    t = readable(text).translate(_LABEL_CHARS).strip()
    if not (t.startswith("[") and t.endswith("]")):
        raise ValueError(f"stat.results isn't a matrix: {t[:60]!r}")
    title = None
    rows: list[tuple[str, str]] = []
    for row in _rows(t[1:-1]):
        items = _split_items(row)
        if len(items) != 2:
            raise ValueError(f"stat.results row isn't [name, value]: {row[:60]!r}")
        name, value = _unquote(items[0]), _unquote(items[1])
        if name == "Title" and title is None:
            title = value
        else:
            rows.append((name, value))
    return title, rows


def equation(text: str) -> str:
    """stat.RegEqn(mcpstatv) as CAS text in x."""
    return _EQ_VAR_RE.sub("x", readable(text).strip())


def result(spec: StatsSpec, replies: list[dict]) -> dict:
    """The tool's answer from run()'s replies. Raises ValueError for an unreadable reply."""
    warnings: list[str] = []
    res = replies[1]
    if not res["ok"]:
        raise ValueError(f"stat.results: {res['error']}")
    title, rows = parse_results(res["result"])
    cols = [spec.label(spec.x)] + ([spec.label(spec.y)] if spec.y is not None else [])
    out: dict = {
        "command": spec.command,
        "nspire": spec.nspire,
        "title": title,
        "n": spec.table.rows,
        "columns": cols,
        "frequencies": spec.label(spec.freq) if spec.freq is not None else None,
        "results": [{"name": n, "value": v} for n, v in rows],
    }
    if spec.regression:
        template = next((v for n, v in rows if n == "RegEqn"), None)
        out["results"] = [r for r in out["results"] if r["name"] != "RegEqn"]
        out["equation_template"] = template
        eq = replies[2] if len(replies) > 2 else None
        if eq is not None and eq["ok"]:
            out["equation"] = equation(eq["result"])
        else:
            out["equation"] = None
            warnings.append(f"The fitted equation couldn't be read: {eq['error'] if eq else 'no reply'}")
        out["r2"] = next((v for n, v in rows if n in ("r²", "R²")), None)
        out["r"] = next((v for n, v in rows if n == "r"), None)
    notes = list(spec.notes)
    if any(v == "{...}" for _, v in rows):
        notes.append("Lists in stat.results (such as Resid, the residuals) show as {...}.")
    out["notes"] = notes
    out["warnings"] = warnings
    return out


def summary(out: dict) -> str:
    """The answer as text: title, one name = value line per result, the equation."""
    head = out["title"] or out["command"]
    cols = " and ".join(out["columns"])
    lines = [f"{head} ({out['nspire']}): {out['n']} rows of {cols}"
             + (f", frequencies from {out['frequencies']}" if out.get("frequencies") else "")
             + ". Every value computed by the TI-Nspire CAS."]
    if out.get("equation") is not None:
        lines.append(f"Fitted equation: y = {out['equation']}")
    lines += [f"{r['name']} = {r['value']}" for r in out["results"]]
    lines += out.get("notes") or []
    if out.get("plot"):
        lines.append("Plot: " + out["plot"])
    if out.get("warnings"):
        lines.append("Warnings:")
        lines += [f"- {w}" for w in out["warnings"]]
    return "\n".join(lines)


# ---------------------------------------------------------------- plot

PLOT_SAMPLES = 200


def plot_range(xs: list[float]) -> tuple[float, float]:
    """The x range to draw: the data's, 5% wider on each side, kept positive if the data is
    (PowerReg and LnReg are only defined there)."""
    lo, hi = min(xs), max(xs)
    span = hi - lo or abs(lo) or 1.0
    a, b = lo - 0.05 * span, hi + 0.05 * span
    if lo > 0 >= a:
        a = lo / 2
    return a, b


def plot_spec(spec: StatsSpec, eq: str | None):
    """A nspire_plot PlotSpec for the fitted curve (no functions for TwoVar or without an equation)."""
    import nspire_plot as pl
    xs = spec.table.values[spec.x]
    a, b = plot_range(xs)
    funcs = [pl.Func(f"y = {eq}", eq)] if eq else []
    for f in funcs:
        pl.check_command(f.command, f.label)
    return pl.PlotSpec(funcs, "x", pl.Grid(a, b, PLOT_SAMPLES))


def plot_points(spec: StatsSpec) -> tuple[list[float], list[float]]:
    assert spec.y is not None
    return spec.table.values[spec.x], spec.table.values[spec.y]
