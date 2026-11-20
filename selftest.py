# /// script
# requires-python = ">=3.11,<3.14"
# dependencies = ["mcp>=1.2,<2", "pywin32>=306"]
# ///
"""Check the bridge end to end:  uv run --script selftest.py

Needs TI-Nspire open with nspire_bridge.tns on the bridge page.
On the non-CAS edition, symbolic cases pass if they fail cleanly with an error.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from server import BridgeTimeout, bridge  # noqa: E402

# (expr, expected with CAS, needs CAS?)  None expected = must be an error
CASES = [
    ("2+2", "4", False),
    ("factor(x^2-5x+6)", "(x-3)*(x-2)", True),
    ("solve(x^2=4,x)", "x=-2 or x=2", True),
    ("∫(x^2,x)", "x^3/3", True),
    ("2+*3", None, False),
]


def norm(s: str) -> str:
    return s.replace("−", "-").replace(" ", "")


def run() -> int:
    failures = 0

    def report(ok: bool, name: str, detail: str) -> None:
        nonlocal failures
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<20} {detail}")

    try:
        st = bridge.ping()
    except BridgeTimeout as e:
        print(f"FAIL  ping                 {e}")
        return 1
    cas = st["cas"]
    report(True, "ping", f"api {st['api_level']}, {'CAS' if cas else 'non-CAS'}")

    for expr, want, needs_cas in CASES:
        try:
            got, err = bridge.eval_one(expr, 5), None
        except BridgeTimeout as e:
            report(False, expr, str(e))
            return 1
        except ValueError as e:
            got, err = None, str(e)

        if want is None or (needs_cas and not cas):
            report(err is not None, expr, f"error as expected: {err}" if err else f"expected an error, got {got!r}")
        else:
            ok = got is not None and norm(got) == norm(want)
            report(ok, expr, repr(got) if err is None else err)

    print(f"\n{len(CASES) + 1 - failures}/{len(CASES) + 1} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")  # ∫ and − in piped output
    sys.exit(bridge.session(run))
