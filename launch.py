# /// script
# requires-python = ">=3.11,<3.15"
# dependencies = ["pywin32>=306", "sympy>=1.13", "antlr4-python3-runtime==4.11.*"]
# ///
"""Open nspire_bridge.tns in TI-Nspire and start the LaTeX hotkey.

Meant for a desktop shortcut:  uvw.exe run --gui-script launch.py
(the MCP server isn't started here; Claude Code starts it when needed).
Extra arguments are passed on to hotkey.py, e.g. --copy text.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.startfile(os.path.join(HERE, "nspire_bridge.tns"))  # opens in TI-Nspire (file association)

import hotkey  # noqa: E402

hotkey.main()  # returns at once if the hotkey is already running
