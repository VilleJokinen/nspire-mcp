# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = [
#     "pywin32>=306; sys_platform == 'win32'",
#     "pyobjc-framework-Cocoa>=10.3; sys_platform == 'darwin'",
#     "sympy>=1.13",
#     "antlr4-python3-runtime==4.11.*",
# ]
# ///
"""Check the bridge, the LaTeX translator and plot sampling end to end:  uv run --script selftest.py

Needs TI-Nspire open with nspire_bridge.tns on the bridge page.
On the non-CAS edition, symbolic cases pass if they fail cleanly with an error.
LaTeX cases need the CAS and are skipped without it. Variables stored in the
open document (e.g. x:=3) can change answers; DelVar them if a case fails oddly.
It also reads the mode settings, stores and deletes brgselftest (a variable of its
own), and evaluates once each in another angle mode and in Approximate mode (the
per-call override behind nspire_eval's angle / exact_mode, which runs setMode inside a
temporary function), then checks the document's settings didn't change.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nspire_steps  # noqa: E402
from nspire_bridge import MODE_FUNC, Bridge, BridgeTimeout  # noqa: E402
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
    # dot and cross products (dotP, crossP), and units (TI-Nspire's _km, _m, _kg, _gm)
    (r"\begin{pmatrix}1\\2\\3\end{pmatrix}\cdot\begin{pmatrix}4\\5\\6\end{pmatrix}", "auto", "32"),
    (r"\begin{pmatrix}1&0&0\end{pmatrix}\times\begin{pmatrix}0&1&0\end{pmatrix}", "auto", "[[0,0,1]]"),
    # unit conversions come back as decimals
    (r"\frac{1\,\mathrm{km}}{1\,\mathrm{m}}", "auto", "1000."),
    (r"\frac{2\,\mathrm{kg}}{500\,\mathrm{g}}", "auto", "4."),
    # physics: ► conversion (sent as @>), units whose TI-Nspire names differ from the symbol
    # (K is _°K, C is _coul: _K and _C are constants there), µ and Ω, and constants on request
    (r"90\,\mathrm{km/h} \to \mathrm{m/s}", "auto", "25.*(_m/_s)"),
    (r"2\,\mathrm{mA}\cdot 4.7\,\mathrm{k}\Omega \to \mathrm{V}", "auto", "9.4*_V"),
    (r"3\,\mu\mathrm{A} \to \mathrm{nA}", "auto", None),  # TI-Nspire has no _nA: refused before the CAS
    (r"25^\circ\mathrm{C} \to \mathrm{K}", "auto", "298.15*_°K"),
    (r"3600\,\mathrm{J} \to \mathrm{kWh}", "auto", "0.001*_kWh"),
    (r"2\,\mathrm{A}\cdot 3\,\mathrm{s} \to \mathrm{C}", "auto", "6.*_coul"),
    (r"5\,\mathrm{kg}\cdot g \to \mathrm{N}", "auto", "49.03325*_N", {"constants": ["g"]}),
    # the length of a vector is norm, not the elementwise abs
    (r"\left|\begin{pmatrix}3\\4\end{pmatrix}\right|", "auto", "5"),
    (r"\lVert\begin{pmatrix}3&4\end{pmatrix}\rVert", "auto", "5"),
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
    for f, ys, w in zip(funcs, got.ys, want, strict=True):
        for i, y in enumerate(ys):
            x = spec.grid.x(i)
            e = w(x)
            if (e is None) != (y is None) or (e is not None and abs(y - e) > 1e-5 * max(1, abs(e))):
                bad.append(f"{f.command} at x={x:g}: got {y}, want {e}")
    report(not bad, "nspire_plot sampling", "; ".join(bad[:3]) or f"{got.requests} request(s), gaps where expected")


# nspire_check_steps: (lines, latex?, var, expected verdict of each step)
STEPS_CASES = [
    (["(x+1)^2", "x^2+2*x+1", "x^2+1"], False, None, ["ok", "broken"]),
    (["x^2=x", "x=1"], False, None, ["broken"]),  # dividing by x loses x=0
    (["x+y=3 and x-y=1", "x=2 and y=1"], False, "x,y", ["ok"]),
    ([r"x^2-5x+6=0", r"\iff (x-2)(x-3)=0", r"x=2 \lor x=3"], True, None, ["ok", "ok"]),
    ([r"\sin^2 x+\cos^2 x", r"= 1"], True, None, ["ok"]),
    # numeric spot check (a tuple: any of these verdicts passes, as simplify may or may not settle it)
    (["√(x^2)", "x"], False, None, ["broken"]),  # differs at negative x
    (["sin(x)^2-cos(x)^2", "1-cos(x)^2"], False, None, ["broken"]),
    (["ln(x*y)", "ln(x)+ln(y)"], False, None, [("ok", "probably_ok")]),  # negative points skipped
    ([r"\sin(2x)", r"2\sin x\cos x"], True, None, [("ok", "probably_ok")]),
]


def steps_check(report: "Report", specs: list) -> None:
    """Check worked solutions with nspire_steps (inside the session): one batch each."""
    for (lines, _latex, _var, want), spec in zip(STEPS_CASES, specs, strict=True):
        name = f"check_steps {' | '.join(lines)}"[:42]
        if isinstance(spec, Exception):
            report(False, name, f"{type(spec).__name__}: {spec}")
            continue
        steps = nspire_steps.run(bridge, spec, 10)
        if bridge.last_timeout:
            raise BridgeTimeout(bridge.last_timeout)
        got = [s.verdict for s in steps]
        good = len(got) == len(want) and all(g in w if isinstance(w, tuple) else g == w
                                              for g, w in zip(got, want, strict=True))
        spots = [f"spot {s.spot_check['agreed']}/{s.spot_check['usable']}/{s.spot_check['tried']}"
                 + (f" at {s.spot_check['counterexample']['at']}" if s.spot_check['counterexample'] else "")
                 for s in steps if s.spot_check]
        report(good, name, f"{got}  " + "; ".join(spots + [f"{c} -> {r}" for s in steps
                                                           for c, r in zip(s.check, s.cas, strict=False)]))


class Report:
    def __init__(self) -> None:
        self.failures = self.total = 0

    def __call__(self, ok: bool, name: str, detail: str) -> None:
        self.total += 1
        self.failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<42} {detail}")


SELFTEST_VAR = "brgselftest"  # created and deleted here; an odd name so it can't be the user's


def _names() -> set[str]:
    return {v["name"].lower() for v in bridge.list_vars(values=False)}


def document_checks(report: Report, cas: bool) -> None:
    """Mode settings, a variable round trip, and the per-call mode override (setMode
    inside a temporary function, which nspire_eval's angle/exact_mode rely on). Inside
    the session."""
    modes = bridge.modes()
    report(modes is not None, "modes (getMode(0))", repr(modes))

    name = SELFTEST_VAR
    if name in _names():
        report(False, "variables round trip", f"{name} is already stored: DelVar {name}, then run again")
    else:
        try:
            try:
                bridge.eval_one(f"{name}:=5", 5)
                listed = [v for v in bridge.list_vars() if v["name"].lower() == name]
            finally:
                bridge.eval_one(f"DelVar {name}", 5)
            gone = name not in _names()
            ok = len(listed) == 1 and listed[0]["type"].upper() == "NUM" and listed[0].get("value") == "5" and gone
            report(ok, "variables round trip", f"listed {listed}, deleted again: {gone}")
        except ValueError as e:
            report(False, "variables round trip", str(e))

    if modes is None or "angle" not in modes:
        report(False, "mode override", "skipped: the mode settings couldn't be read")
        return
    # Evaluate once in the other angle mode, then check the document's settings didn't change.
    to_degree = modes["angle"] != "Degree"
    expr = "sin(30)" if to_degree else "sin(π/6)"
    try:
        got, warnings = bridge.eval_in_modes(expr, {2: 2 if to_degree else 1}, 5)
        third, warnings2 = bridge.eval_in_modes("1/3", {5: 2}, 5)  # Approximate
    except ValueError as e:
        report(False, "mode override", str(e))
        return
    after = bridge.modes()
    left = MODE_FUNC in _names()
    want = ("1/2",) if cas else ("0.5", "1/2")
    report(norm(got) in want, f"mode override: {expr} in {'Degree' if to_degree else 'Radian'}", repr(got))
    report(third.startswith("0.333"), "mode override: 1/3 in Approximate", repr(third))
    report(after == modes and not left and not warnings and not warnings2, "mode override: document unchanged",
           f"{after}, {MODE_FUNC} left: {left}" + "".join(f"; {w}" for w in warnings + warnings2))


def cas_checks(report: Report, plans: list, step_specs: list) -> list | None:
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
        document_checks(report, cas)
    except BridgeTimeout as e:
        report(False, "document checks", str(e))
        return None
    try:
        plot_check(report)
    except BridgeTimeout as e:
        report(False, "nspire_plot sampling", str(e))
        return None

    runs: list = []
    if not cas:
        print("(non-CAS edition: LaTeX and check_steps cases skipped)")
        return runs
    try:
        steps_check(report, step_specs)
    except BridgeTimeout as e:
        report(False, "check_steps", str(e))
        return None
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
    for latex, action, want, *opts in LATEX_CASES:
        try:
            plans.append((latex, want, plan(latex, action, **(opts[0] if opts else {}))))
        except Exception as e:
            plans.append((latex, want, e))
    step_specs = []
    for lines, latex, var, _want in STEPS_CASES:
        try:
            step_specs.append(nspire_steps.prepare(lines, latex, var))
        except Exception as e:
            step_specs.append(e)
    runs = bridge.session(lambda: cas_checks(report, plans, step_specs))
    if runs is None:
        return 1
    for latex, want, run in runs:  # LaTeX conversion after the session
        if want is None:  # must be refused by the translator
            report(isinstance(run, Exception), latex, f"refused as expected: {run}" if isinstance(run, Exception)
                   else "expected the translator to refuse it")
            continue
        if isinstance(run, Exception):
            report(False, latex, f"{type(run).__name__}: {run}")
            continue
        r = format_answer(run)
        ok = norm(r["result"]) == norm(want) and r["result_latex"] is not None
        report(ok, latex, f"{r['nspire']}  =>  {r['result']}   [{r['result_latex']}]")

    print(f"\n{report.total - report.failures}/{report.total} passed")
    return 1 if report.failures else 0


if __name__ == "__main__":
    # ∫ and − in piped output (sys.stdout is a TextIOWrapper here; typeshed says TextIO)
    sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]
    sys.exit(main())
