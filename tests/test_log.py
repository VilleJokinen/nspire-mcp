"""nspire_log: the session log's format, appending, failure tolerance and switches.
Standard library only; nothing here touches TI-Nspire or the clipboard."""
import datetime as dt
import multiprocessing
from pathlib import Path

import pytest

import nspire_log as sl

WHEN = dt.datetime(2026, 11, 24, 14, 3, 22)
ABS = Path(__file__).resolve().parent / "abs_notes"  # absolute on every OS; never created


def test_off_by_default():
    assert sl.log_dir({}) is None
    assert sl.record("x", command="1") is None


@pytest.mark.parametrize("env, want", [
    ({"NSPIRE_LOG": "1"}, sl.DEFAULT_DIR),
    ({"NSPIRE_LOG": "on"}, sl.DEFAULT_DIR),
    ({"NSPIRE_LOG_DIR": str(ABS)}, ABS),
    ({"NSPIRE_LOG_DIR": str(ABS), "NSPIRE_LOG": "1"}, ABS),
    ({"NSPIRE_LOG_DIR": str(ABS), "NSPIRE_LOG": "0"}, None),
    ({"NSPIRE_LOG": "0"}, None),
    ({"NSPIRE_LOG": ""}, None),
    ({"NSPIRE_LOG": "maybe"}, None),
])
def test_switches(env, want):
    assert sl.log_dir(env) == want


def test_relative_folder_is_taken_from_the_repo():
    assert sl.log_dir({"NSPIRE_LOG_DIR": "notes"}) == sl.DEFAULT_DIR.parent / "notes"


def test_default_folder_is_ignored_by_git():
    ignore = (Path(__file__).resolve().parent.parent / ".gitignore").read_text()
    assert "logs/" in ignore.splitlines()


def test_math_delimiters_are_normalised():
    for src in ("x^2", "$x^2$", "$$x^2$$", r"\[x^2\]", r"\(x^2\)", "  $x^2$ \n"):
        assert sl.math_block(src) == "$$\nx^2\n$$"
    assert sl.math_block("a\n\n\nb") == "$$\na\nb\n$$"  # a blank line would end the block


def test_code_uses_a_long_enough_fence():
    assert sl.code("2+2") == "`2+2`"
    assert sl.code("a`b") == "```\na`b\n```"
    assert sl.code("a```b").startswith("````")
    assert sl.code("x\ny") == "```\nx\ny\n```"


def test_entry_format():
    text = sl.format_entry("nspire_latex", when=WHEN, note="isolated", latex=r"$\frac{1}{2}+\frac{1}{3}$",
                           command="1/2+1/3", result="5/6", result_latex=r"\frac{5}{6}", approx="0.833333",
                           approx_latex="0.833333", warnings=["x is stored"])
    assert text == (
        "## 14:03:22 · nspire_latex (isolated)\n\n"
        "$$\n\\frac{1}{2}+\\frac{1}{3}\n$$\n\n"
        "TI-Nspire command:\n`1/2+1/3`\n\n"
        "Result (TI-Nspire text):\n`5/6`\n\n"
        "$$\n\\frac{5}{6}\n$$\n\n"
        "Approximately:\n`0.833333`\n\n$$\n0.833333\n$$\n\n"
        "Warnings:\n- `x is stored`\n\n")


def test_entry_with_error_and_items():
    text = sl.format_entry("nspire_eval_batch", when=WHEN, error="boom", items=[
        {"command": "1+1", "result": "2"}, {"command": "1+", "error": "error 910", "warnings": ["w"]}])
    assert "1. `1+1`\n   - Result: `2`" in text
    assert "2. `1+`\n   - Error: `error 910`\n   - Warning: `w`" in text
    assert text.rstrip().endswith("**Error:**\n`boom`")


def test_huge_result_is_cut():
    text = sl.format_entry("x", when=WHEN, result="9" * (sl.MAX_FIELD_CHARS + 50))
    assert "50 more characters" in text and len(text) < sl.MAX_FIELD_CHARS + 500


def test_record_appends_and_writes_one_header(tmp_path):
    p1 = sl.record("a", folder=tmp_path, when=WHEN, command="1")
    p2 = sl.record("b", folder=tmp_path, when=WHEN, command="2")
    assert p1 == p2 == tmp_path / "2026-11-24.md"
    text = p1.read_text(encoding="utf-8")
    assert text.startswith("# TI-Nspire session log 2026-11-24\n\n")
    assert text.count("# TI-Nspire session log") == 1
    assert text.index("· a") < text.index("· b")


def test_one_file_per_day(tmp_path):
    sl.record("a", folder=tmp_path, when=WHEN, command="1")
    sl.record("b", folder=tmp_path, when=WHEN + dt.timedelta(days=1), command="2")
    assert sorted(f.name for f in tmp_path.glob("*.md")) == ["2026-11-24.md", "2026-11-25.md"]


def test_creates_the_folder(tmp_path):
    p = sl.record("a", folder=tmp_path / "deep" / "er", when=WHEN, command="1")
    assert p is not None and p.exists()


def test_failure_never_raises(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert sl.record("a", folder=blocker / "sub", command="1") is None
    assert sl.record("a", folder=blocker / "sub", command="1") is None
    assert capsys.readouterr().err.count("session log not written") <= 1  # noted once


def test_bad_fields_never_raise(tmp_path):
    assert sl.record("a", folder=tmp_path, when=WHEN, unknown_field=1) is None  # a typo at a call site


def test_toggle_between_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("NSPIRE_LOG_DIR", str(tmp_path))
    assert sl.record("on", command="1") is not None
    monkeypatch.setenv("NSPIRE_LOG", "0")
    assert sl.record("off", command="2") is None
    text = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert "· on" in text and "· off" not in text


def _worker(folder: str, tag: int) -> None:
    for i in range(30):
        sl.record(f"p{tag}", folder=Path(folder), when=WHEN, command=f"{tag}-{i}", result="y" * 500)


def test_two_processes_never_interleave(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_worker, args=(str(tmp_path), n)) for n in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    text = (tmp_path / "2026-11-24.md").read_text(encoding="utf-8")
    assert text.count("# TI-Nspire session log") == 1
    entries = text.split("\n## ")[1:]
    assert len(entries) == 90
    assert all(e.count("y" * 500) == 1 and e.rstrip().endswith("`") for e in entries)


def test_read_day(tmp_path):
    assert sl.read_day() == {"enabled": False, "folder": None, "file": None, "exists": False,
                             "text": "", "truncated": False}
    r = sl.read_day("2026-11-24", folder=tmp_path)
    assert r["enabled"] and not r["exists"]
    for i in range(50):
        sl.record("a", folder=tmp_path, when=WHEN, command=str(i), result="z" * 100)
    r = sl.read_day("2026-11-24", max_chars=1000, folder=tmp_path)
    assert r["exists"] and r["truncated"] and r["text"].startswith("## ") and "`49`" in r["text"]
    assert len(r["text"]) <= 1000
    with pytest.raises(ValueError, match="2026-11-24"):
        sl.read_day("nope", folder=tmp_path)
