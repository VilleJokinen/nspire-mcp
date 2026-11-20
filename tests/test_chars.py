import pytest

import nspire_chars
from nspire_chars import NS_C, NS_E, NS_EXP, NS_I, NS_N, readable, to_cas_chars


def test_readable_round_trip():
    raw = f"x={NS_C}1*{NS_E}^(2*{NS_I})+1.5{NS_EXP}3+{NS_N}2"
    assert readable(raw) == "x=c1*ℯ^(2*𝑖)+1.5ᴇ3+n2"
    assert to_cas_chars("ℯ^(𝑖*π)") == f"{NS_E}^({NS_I}*π)"


def test_code_points():
    assert [hex(ord(c)) for c in (NS_E, NS_I, NS_EXP, NS_C, NS_N)] == \
        ["0xf03f", "0xf02f", "0xf000", "0xf041", "0xf042"]


def test_translator_imports_without_pywin32():
    # In a fresh interpreter with pywin32's modules blocked, as on Linux/macOS.
    import subprocess
    import sys
    from pathlib import Path
    code = ("import sys\n"
            "for m in ('win32clipboard', 'win32event', 'win32api', 'pywintypes', 'winerror'):\n"
            "    sys.modules[m] = None\n"
            "import nspire_latex\n"
            "assert 'nspire_bridge' not in sys.modules\n"
            "print(nspire_latex.plan('1+1').command)\n")
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1 + 1"


def test_bridge_reexports():
    pytest.importorskip("win32clipboard")
    import nspire_bridge
    for name in ("NS_E", "NS_I", "NS_EXP", "NS_C", "NS_N", "readable", "to_cas_chars"):
        assert getattr(nspire_bridge, name) is getattr(nspire_chars, name)
