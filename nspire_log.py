"""Session log: every solve is appended to a Markdown file of homework scratch notes.

Standard library only (the server imports it at startup, and the hotkey app too).

Off by default: the log keeps the user's problems and answers on disk, which they
should choose. Turn it on with an environment variable (the MCP server's `env` in
`claude mcp add -e ...`, or the shell the hotkey runs in):

    NSPIRE_LOG_DIR=<folder>   log there (a relative folder is taken from this repo's folder)
    NSPIRE_LOG=1              log to <repo>/logs  (the folder .gitignore leaves out)
    NSPIRE_LOG=0              off, even with NSPIRE_LOG_DIR set

One file per day, `YYYY-MM-DD.md` (local time), written by the MCP server and the
hotkey app alike. Each entry is one write() in append mode under a lock file (`.lock` in the folder), so entries from two
processes never interleave or overwrite each other; the day's file starts with a header.

record() never raises: a log that can't be written is skipped (and noted once on
stderr), never a failed tool call. Math is written as $$...$$ so Obsidian shows it.
"""
from __future__ import annotations

import contextlib
import datetime as _dt
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

ENV_DIR = "NSPIRE_LOG_DIR"
ENV_FLAG = "NSPIRE_LOG"
DEFAULT_DIR = Path(__file__).resolve().parent / "logs"
_OFF = {"0", "off", "false", "no", ""}
_ON = {"1", "on", "true", "yes"}
MAX_FIELD_CHARS = 20000  # one result can be huge (a long list); the log keeps the start

_warned = False


def log_dir(env: Mapping[str, str] | None = None) -> Path | None:
    """The folder to log into, or None when logging is off."""
    env = os.environ if env is None else env
    flag = env.get(ENV_FLAG)
    if flag is not None and flag.strip().lower() in _OFF:
        return None
    folder = (env.get(ENV_DIR) or "").strip()
    if folder:
        p = Path(folder).expanduser()
        return p if p.is_absolute() else DEFAULT_DIR.parent / p
    if flag is not None and flag.strip().lower() in _ON:
        return DEFAULT_DIR
    return None


def enabled() -> bool:
    return log_dir() is not None


def day_file(folder: Path, when: _dt.datetime | None = None) -> Path:
    return folder / f"{(when or _dt.datetime.now()).date().isoformat()}.md"


# ---------------------------------------------------------------- formatting

_DELIMS = [(r"\[", r"\]"), (r"\(", r"\)"), ("$$", "$$"), ("$", "$")]


def strip_math_delimiters(latex: str) -> str:
    """The LaTeX without one pair of outer $...$, $$...$$, \\[...\\] or \\(...\\)."""
    s = latex.strip()
    for a, b in _DELIMS:
        if len(s) >= len(a) + len(b) and s.startswith(a) and s.endswith(b):
            return s[len(a):len(s) - len(b)].strip()
    return s


def math_block(latex: str) -> str:
    """$$ block. Blank lines would end it in Markdown, so they go."""
    body = "\n".join(ln for ln in strip_math_delimiters(latex).splitlines() if ln.strip())
    if "$" in body:  # a stray $ (e.g. \text{$...$}) would end the block early
        body = body.replace("$", "")
    return f"$$\n{body}\n$$"


def _clip(text: str) -> str:
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + f"\n… ({len(text) - MAX_FIELD_CHARS} more characters)"


def code(text: str) -> str:
    """Inline code for one short line, a fenced block otherwise (fence longer than any backtick run in text)."""
    text = _clip(str(text))
    if "\n" not in text and "`" not in text and len(text) <= 100:
        return f"`{text}`"
    n = max([3] + [len(m) + 1 for m in re.findall(r"`+", text)])
    fence = "`" * n
    return f"{fence}\n{text}\n{fence}"


def _item(i: int, it: dict) -> str:
    lines = [f"{i}. {code(it['command'])}" if it.get("command") is not None else f"{i}."]
    if it.get("error"):
        lines.append(f"   - Error: {_one_line(it['error'])}")
    elif it.get("result") is not None:
        lines.append(f"   - Result: {_one_line(it['result'])}")
    for w in it.get("warnings") or []:
        lines.append(f"   - Warning: {_one_line(w)}")
    return "\n".join(lines)


def _one_line(s) -> str:
    s = _clip(str(s))
    return code(s) if ("\n" not in s and "`" not in s) else code(s).replace("\n", "\n   ")


def format_entry(source: str, *, when: _dt.datetime | None = None, note: str | None = None,
                 latex: str | list[str] | None = None, command: str | None = None,
                 result: str | None = None, result_latex: str | None = None,
                 approx: str | None = None, approx_latex: str | None = None,
                 items: list[dict] | None = None, summary: str | None = None,
                 warnings: list[str] | None = None, error: str | None = None) -> str:
    """One entry. items: [{command, result | error, warnings}] for batches and step lists."""
    when = when or _dt.datetime.now()
    head = f"## {when:%H:%M:%S} · {source}" + (f" ({note})" if note else "")
    parts = [head]
    if latex:
        for ln in ([latex] if isinstance(latex, str) else latex):
            if ln and ln.strip():
                parts.append(math_block(ln))
    if command is not None:
        parts.append("TI-Nspire command:\n" + code(command))
    if items:
        parts.append("\n".join(_item(i, it) for i, it in enumerate(items, 1)))
    if summary:
        parts.append(_clip(summary))
    if result is not None:
        parts.append("Result (TI-Nspire text):\n" + code(result))
    if result_latex:
        parts.append(math_block(result_latex))
    if approx:
        parts.append("Approximately:\n" + code(approx))
        if approx_latex:
            parts.append(math_block(approx_latex))
    if warnings:
        parts.append("Warnings:\n" + "\n".join(f"- {_one_line(w)}" for w in warnings))
    if error:
        parts.append("**Error:**\n" + code(error))
    return "\n\n".join(parts) + "\n\n"


# ---------------------------------------------------------------- writing

def _note_failure(e: BaseException) -> None:
    global _warned
    if not _warned:
        _warned = True
        with contextlib.suppress(Exception):
            print(f"nspire: session log not written ({type(e).__name__}: {e})", file=sys.stderr)


@contextlib.contextmanager
def _locked(folder: Path):
    """Cross-process lock on a file in folder. Appending with "ab" alone isn't enough on Windows
    (the C runtime seeks to the end and then writes, so two processes can overwrite each other:
    a test lost 2 of 90 entries), hence the lock. Raises if it can't be had in about 10 s."""
    with open(folder / ".lock", "a+b") as lock:
        if sys.platform == "win32":
            import msvcrt
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)  # retries for 10 s, then OSError
            try:
                yield
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def append(folder: Path, text: str, when: _dt.datetime | None = None) -> Path:
    """Append text to the day's file under folder (created, with its header, if new). Raises on failure."""
    folder.mkdir(parents=True, exist_ok=True)
    path = day_file(folder, when)
    with _locked(folder), open(path, "ab") as f:
        if f.seek(0, os.SEEK_END) == 0:
            text = f"# TI-Nspire session log {path.stem}\n\n" + text
        f.write(text.encode("utf-8", "replace"))
    return path


def record(source: str, *, folder: Path | None = None, when: _dt.datetime | None = None, **fields) -> Path | None:
    """Log one entry (see format_entry for the fields). Returns the file written, or None when
    logging is off or failed. Never raises."""
    try:
        folder = folder if folder is not None else log_dir()
        if folder is None:
            return None
        return append(folder, format_entry(source, when=when, **fields), when)
    except Exception as e:
        _note_failure(e)
        return None


# ---------------------------------------------------------------- reading (the nspire_log tool)

def read_day(day: str | None = None, max_chars: int = 6000, folder: Path | None = None) -> dict:
    """{enabled, folder, file, exists, text, truncated}: the end of a day's log. day: YYYY-MM-DD, default today.
    Never raises."""
    folder = folder if folder is not None else log_dir()
    if folder is None:
        return {"enabled": False, "folder": None, "file": None, "exists": False, "text": "", "truncated": False}
    try:
        when = _dt.datetime.fromisoformat(day) if day else _dt.datetime.now()
    except ValueError:
        raise ValueError(f"day must look like 2026-11-24, not {day!r}") from None
    path = day_file(folder, when)
    out = {"enabled": True, "folder": str(folder), "file": str(path), "exists": False, "text": "", "truncated": False}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return out
    except OSError as e:
        out["text"] = f"couldn't read the log: {e}"
        return out
    out["exists"] = True
    max_chars = max(200, min(int(max_chars), 100000))
    if len(text) > max_chars:
        # start at an entry boundary when there is one
        cut = text[-max_chars:]
        k = cut.find("\n## ")
        out["text"] = cut[k + 1:] if k >= 0 else cut
        out["truncated"] = True
    else:
        out["text"] = text
    return out
