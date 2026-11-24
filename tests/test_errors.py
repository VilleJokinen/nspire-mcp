"""nspire_errors: the error-code table and its lookups. No bridge, no pywin32."""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import nspire_errors as ne  # noqa: E402


def lua_errors() -> dict[int, str]:
    """bridge.lua's ERRORS table: the codes the bridge reports, with TI's names."""
    text = (REPO / "bridge.lua").read_text(encoding="utf-8")
    body = text.split("local ERRORS = {", 1)[1].split("\n}", 1)[0]
    return {int(c): n for c, n in re.findall(r'\[(\d+)\]\s*=\s*"((?:[^"\\]|\\.)*)"', body)}


def test_every_code_is_one_the_bridge_reports_under_the_same_name():
    lua = lua_errors()
    for code in ne.known_codes():
        assert code in lua, f"{code} isn't in bridge.lua's ERRORS"
        name = ne._TABLE[code][0].replace('\\"', '"')
        assert name == lua[code].replace('\\"', '"') or name in lua[code], (code, name, lua[code])


def test_lookup_forms_agree():
    want = ne.explain(800)
    assert want and want.startswith("Error 800 (non-real result): ")
    assert ne.explain("800") == want
    assert ne.explain("error 800 (non-real result)") == want
    assert ne.explain("TI-Nspire error: error 800 (non-real result)") == want
    assert ne.explain("error 800 (Non-real result)") == want  # the code decides, not the wording


def test_lookup_by_ti_name_without_a_code():
    assert ne.code_of("setMode: invalid outside function or program") == 550
    assert ne.code_of("Syntax error in the input") == 910
    assert ne.code_of("hello") is None


def test_unknown_gets_nothing():
    for x in (99999, "error 99999", "no code here", "", "error 0"):
        assert ne.explain(x) is None


def test_confirmed_on_the_real_cas():
    # codes the real CAS returned for these inputs (checked through the bridge)
    seen = {"√(−4)": 800, "solve(x^2=4)": 930, "sin(1,2)": 940, "{1,2}+{1,2,3}": 240, "2+": 910,
            "setMode(1,2)": 550, "DelVar π": 140}
    for code in seen.values():
        assert ne.explain(code)


def test_plot_fatal_codes_all_explained():
    import nspire_plot
    assert set(ne.known_codes()) >= nspire_plot._FATAL_CODES


def test_the_codes_the_code_already_knows_are_explained():
    # nspire_bridge.eval_in_modes treats these as "setMode isn't allowed here"
    for code in (450, 550, 565, 800, 1090, 980):
        assert ne.explain(code)


def test_explain_all_dedupes_by_code_in_order():
    out = ne.explain_all(["error 800 (non-real result)", "error 910 (syntax error)", "error 800 (x)", "weird", "ok"])
    assert [o.split(" ")[1] for o in out] == ["800", "910"]


def test_with_explanation():
    msg = "TI-Nspire error: error 910 (syntax error)"
    once = ne.with_explanation(msg)
    assert once.startswith(msg + " Error 910 (syntax error): ")
    assert ne.with_explanation(once) == once              # not added twice
    assert ne.with_explanation("No reply from the bridge") == "No reply from the bridge"


def test_every_entry_reads_as_sentences():
    for code in ne.known_codes():
        _, cause, fix = ne._TABLE[code]
        assert cause.endswith(".") and fix.endswith("."), code
