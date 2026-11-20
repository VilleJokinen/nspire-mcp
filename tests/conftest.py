import importlib.util
import sys
from pathlib import Path

# The modules live in the repo root, not in a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The Notes-safety tests import nspire_keys / nspire_bridge, which need pywin32
# (Windows only). Skip them where it isn't installed, e.g. the Linux CI job.
collect_ignore = []
if importlib.util.find_spec("win32api") is None:
    collect_ignore.append("test_notes_safety.py")
