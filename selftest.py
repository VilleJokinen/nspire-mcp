# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["pywin32>=306", "sympy>=1.13", "antlr4-python3-runtime==4.11.*"]
# ///
"""Check the bridge, the LaTeX translator and plot sampling end to end:  uv run --script selftest.py

Needs TI-Nspire open with nspire_bridge.tns on the bridge page.
On the non-CAS edition, symbolic cases pass if they fail cleanly with an error.
LaTeX cases need the CAS and are skipped without it. Variables stored in the
open document (e.g. x:=3) can change answers; DelVar them if a case fails oddly.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from nspire_bridge import Bridge, BridgeTimeout  # noqa: E402
from nspire_latex import format_answer, plan, run_plan  # noqa: E402
from nspire_plot import Func, prepare, sample  # noqa: E402

bridge = Bridge()

# (expr, expected with CAS, needs CAS?)  None expected = must be an error
CASES = [
    ("2+2", "4", False),
    ("factor(x^2-5x+6)", "(x-3)*(x-2)", True),
    ("solve(x^2=4,x)", "x=-2 or x=2", True),
    ("∫(x^2,x)", "x^3/3", True),
    ("cSolve(x^2=-1,x)", "x=𝑖 or x=-𝑖", True),
    ("2+*3", None, False),
]

# (latex, action, expected CAS result)
LATEX_CASES = [
    (r"x^2-5x+6=0", "auto", "x=2 or x=3"),
    (r"\int_0^1 x^2\,dx", "auto", "1/3"),
    (r"\lim_{x\to 0}\frac{\sin x}{x}", "auto", "1"),
    (r"\sum_{k=1}^{n} k", "auto", "n*(n+1)/2"),
    (r"\frac{d}{dx}\sin(x^2)", "auto", "2*x*cos(x^2)"),
    (r"\begin{cases} x+y=3 \\ x-y=1 \end{cases}", "auto", "x=2 and y=1"),
    (r"\begin{vmatrix}1&2\\3&4\end{vmatrix}", "auto", "-2"),
    (r"y''+y=0,\ y(0)=1,\ y'(0)=0", "auto", "y=cos(x)"),
    (r"|x-1| < 3", "auto", "-2<x<4"),
    (r"x^3-6x^2+11x-6", "factor", "(x-3)*(x-2)*(x-1)"),
    # sent unevaluated, so the CAS (not Python floats) does the arithmetic
    (r"0.1+0.2", "auto", "0.3"),
    (r"\frac{1,5}{2}", "auto", "0.75"),
    (r"\sqrt{8}", "auto", "2*√(2)"),
    (r"e^{i\pi}", "auto", "-1"),
    # x is the dependent variable here, so t is the independent one
    (r"x'=3,\ x(0)=1", "auto", "x=3*t+1"),
    (r"x'(t)=x(t),\ x(0)=2", "auto", "x=2*ℯ^t"),
    # evaluation bar, operator-form derivative, \det, lists, conditions, \bmod (none stores anything)
    (r"\left.\frac{d}{dx}x^3\right|_{x=2}", "auto", "12"),
    (r"\frac{d^2}{dx^2}x^4", "auto", "12*x^2"),
    (r"\det\begin{pmatrix}1&2\\3&4\end{pmatrix}", "auto", "-2"),
    (r"2\cdot\{1,2,3\}", "auto", "{2,4,6}"),
    (r"x^2=2 \text{ for } x>0", "auto", "x=√(2)"),
    (r"12 \bmod 5", "auto", "2"),
]


def norm(s: str) -> str:
    return s.replace("−", "-").replace(" ", "")


# nspire_plot: (functions, x_min, x_max, samples, expected value of each function, None for a gap)
# x = 0 is the 6th point: √ is 0 there, 1/x undef (a gap), and √ non-real left of it.
PLOT_CASE = ([Func("sin(x)", "sin(x)"), Func("√(x)", "√(x)"), Func("1/x", "1/x")], -1, 1, 11,
             [math.sin, lambda x: math.sqrt(max(x, 0)) if x > -1e-9 else None,
              lambda x: 1 / x if abs(x) > 1e-9 else None])


def plot_check(report: "Report") -> None:
    """Sample three functions on the CAS (inside the session): values, a non-real gap, undef at 0."""
    funcs, a, b, n, want = PLOT_CASE
    spec = prepare(funcs, "x", a, b, n)
    try:
        got = sample(bridge, spec, 10)
    except BridgeTimeout:
        raise
    except Exception as e:
        report(False, "nspire_plot sampling", f"{type(e).__name__}: {e}")
        return
    bad = []
    for f, ys, w in zip(funcs, got.ys, want):
        for i, y in enumerate(ys):
            x = spec.grid.x(i)
            e = w(x)
            if (e is None) != (y is None) or (e is not None and abs(y - e) > 1e-5 * max(1, abs(e))):
                bad.append(f"{f.command} at x={x:g}: got {y}, want {e}")
    report(not bad, "nspire_plot sampling", "; ".join(bad[:3]) or f"{got.requests} request(s), gaps where expected")


class Report:
    def __init__(self) -> None:
        self.failures = self.total = 0

    def __call__(self, ok: bool, name: str, detail: str) -> None:
        self.total += 1
        self.failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<42} {detail}")


def cas_checks(report: Report, plans: list) -> list | None:
    """Everything that needs the bridge; runs inside bridge.session().

    Returns (latex, want, CasRun or exception) for the LaTeX cases, converted
    after the session, or None if the bridge stopped answering.
    """
    try:
        st = bridge.ping()
    except BridgeTimeout as e:
        print(f"FAIL  ping  {e}")
        return None
    cas = st["cas"]
    report(True, "ping", f"api {st['api_level']}, {'CAS' if cas else 'non-CAS'}")

    for expr, want, needs_cas in CASES:
        try:
            got, err = bridge.eval_one(expr, 5), None
        except BridgeTimeout as e:
            report(False, expr, str(e))
            return None
        except ValueError as e:
            got, err = None, str(e)

        if want is None or (needs_cas and not cas):
            report(err is not None, expr, f"error as expected: {err}" if err else f"expected an error, got {got!r}")
        else:
            ok = got is not None and norm(got) == norm(want)
            report(ok, expr, repr(got) if err is None else err)

    try:
        plot_check(report)
    except BridgeTimeout as e:
        report(False, "nspire_plot sampling", str(e))
        return None

    runs: list = []
    if not cas:
        print("(non-CAS edition: LaTeX cases skipped)")
        return runs
    for latex, want, p in plans:
        if isinstance(p, Exception):
            runs.append((latex, want, p))
            continue
        try:
            runs.append((latex, want, run_plan(bridge, p, 10)))
        except BridgeTimeout as e:
            report(False, latex, str(e))
            return None
        except Exception as e:
            runs.append((latex, want, e))
    return runs


def main() -> int:
    report = Report()
    plans = []  # translated before the session: no clipboard use
    for latex, action, want in LATEX_CASES:
        try:
            plans.append((latex, want, plan(latex, action)))
        except Exception as e:
            plans.append((latex, want, e))
    runs = bridge.session(lambda: cas_checks(report, plans))
    if runs is None:
        return 1
    for latex, want, run in runs:  # LaTeX conversion after the session
        if isinstance(run, Exception):
            report(False, latex, f"{type(run).__name__}: {run}")
            continue
        r = format_answer(run)
        ok = norm(r["result"]) == norm(want) and r["result_latex"] is not None
        report(ok, latex, f"{r['nspire']}  =>  {r['result']}   [{r['result_latex']}]")

    print(f"\n{report.total - report.failures}/{report.total} passed")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # ∫ and − in piped output
    sys.exit(main())
