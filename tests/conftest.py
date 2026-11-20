import importlib.util
import sys
from pathlib import Path

# The modules live in the repo root, not in a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _missing(module: str) -> bool:
    return importlib.util.find_spec(module) is None


# Some tests import nspire_keys / nspire_bridge, which need pywin32 (Windows
# only); the transport tests also run bridge.lua in lupa. Skip them where those
# aren't installed, e.g. the Linux CI job.
collect_ignore = []
if _missing("win32api"):
    collect_ignore += ["test_notes_safety.py", "test_transport.py"]
elif _missing("lupa"):
    collect_ignore.append("test_transport.py")
