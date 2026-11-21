# Setting up the `nspire` MCP server

This skill needs the `nspire` MCP server, free to download from GitHub for
personal, non-commercial use (see the LICENSE file in its repository). The skill itself
contains no code; it only teaches your agent how to use the server.

## Requirements

- **Windows 10 or 11.** The server talks to TI-Nspire through the Windows
  clipboard; macOS and Linux are not supported.
- **TI-Nspire CX CAS Student Software or Teacher Software** (the desktop
  app). This is commercial software from Texas Instruments, licensed
  separately; you must obtain and license it yourself from
  education.ti.com. It is not included with this skill or the server.
  The non-CAS edition works for numeric expressions only.
- **uv** (the Python package runner from Astral). The setup script can install
  it for you with winget if you allow it.
- **Claude Code** (CLI, desktop app or IDE extension) or another MCP client.

## Install

1. Get the server code from GitHub:
   https://github.com/villejokinen/nspire-mcp
   Clone it with git, or download the ZIP and extract it, into a folder you
   will keep (the MCP registration points at that folder).
2. Open PowerShell in that folder and run the setup script:

   ```powershell
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```

   Read the script first if you like; it is short. It checks for TI-Nspire
   and uv, installs the Python dependencies, registers the `nspire` MCP
   server with Claude Code for all your projects, and puts a
   "TI-Nspire bridge" shortcut on your desktop. Useful options:

   - `-InstallUv`: install uv with winget if it is missing
   - `-Scope local -ProjectDir <folder>`: register the server for that one project folder only
   - `-NoShortcut`: no desktop shortcut
   - `-Startup`: start the clipboard hotkey at login

   It is safe to run again, for example after moving the folder.
3. Open the bridge document: double-click "TI-Nspire bridge" on the desktop,
   or open `nspire_bridge.tns` from the folder in TI-Nspire. The page shows
   `NSPIRE BRIDGE  tick N` with N counting up. Leave it on that page.
4. Check it end to end from the same folder:

   ```powershell
   uv run --script selftest.py
   ```

   Every line should say PASS.
5. Restart Claude Code. `claude mcp list` should show `nspire` connected.

## Installing the skill

Put this skill folder (SKILL.md and references/) where your agent loads
skills, for Claude Code for example `~/.claude/skills/nspire-cas/`.

## Each time you use it

Open `nspire_bridge.tns` (the desktop shortcut does this) and leave it on the
bridge page. TI-Nspire can be in the background.

## Trademark note

This skill and the `nspire` MCP server are not affiliated with or endorsed by
Texas Instruments. TI-Nspire is a trademark of Texas Instruments.
