import importlib.util
import sys
from pathlib import Path

# The modules live in the repo root, not in a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _missing(module: str) -> bool:
    return importlib.util.find_spec(module) is None


# The Notes-safety tests import nspire_keys, which needs pywin32 (Windows only).
# nspire_bridge imports on any OS (its clipboard backend is chosen by platform),
# so the transport tests need only lupa, which runs bridge.lua, and the server
# tests the mcp package. Skip them where those aren't installed.
collect_ignore = []
if _missing("win32api"):
    collect_ignore.append("test_notes_safety.py")
if _missing("lupa"):
    collect_ignore.append("test_transport.py")
if _missing("mcp"):
    collect_ignore.append("test_server.py")


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _session_log_off(monkeypatch):
    """No test writes a session log unless it turns one on (and then into tmp_path)."""
    monkeypatch.delenv("NSPIRE_LOG", raising=False)
    monkeypatch.delenv("NSPIRE_LOG_DIR", raising=False)


@pytest.fixture(autouse=True)
def _library_in_tmp(monkeypatch, tmp_path):
    """No test reads or writes the user's real function library."""
    monkeypatch.setenv("NSPIRE_LIBRARY_DIR", str(tmp_path / "nspire-library"))
