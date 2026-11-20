# Setting up nspire-mcp (guide for AI coding agents)

This file is for an agent such as Claude Code that has been asked to set this
project up for a user. Humans: see [README.md](README.md).

## Prompt to give your agent

Paste this into Claude Code (or a similar agent) in an empty folder or in this
repo's folder:

> Set up nspire-mcp for me from `<this repo's URL>`. Follow AGENTS.md in the repo.
> I have the TI-Nspire CX CAS desktop software installed.

## What you (the agent) should do

The goal: the `nspire` MCP server registered with the user's Claude Code, the
bridge document open in TI-Nspire, `selftest.py` passing, and (unless the user
declines) the desktop shortcut that opens the document and starts the clipboard
hotkey.

1. **Check the platform.** This only works on Windows with the TI-Nspire CX (CAS)
   Student or Teacher Software installed. If either is missing, stop and tell the
   user; don't try to install TI-Nspire yourself (it's licensed software from
   education.ti.com).
2. **Get the code** if you aren't already in the repo: `git clone <url>` into a
   folder the user will keep. The MCP registration and shortcut point at this
   folder, so don't use a temp directory.
3. **Run the setup script** from the repo folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```

   - If it says uv is missing, ask the user before installing software, then
     rerun with `-InstallUv` (installs uv via winget). After a fresh uv install,
     a new shell may be needed for PATH; the script refreshes PATH itself.
   - Pass `-NoShortcut` if the user doesn't want a desktop shortcut,
     `-Startup` if they want the hotkey at login, `-Scope project` if they want
     the MCP server for one project only. Default is user scope (all projects).
   - Read the output. Every step prints `OK` or `WARN`. The script registers the
     server with whichever `claude` CLI it finds (PATH, or the one bundled with the
     Claude desktop app). If it prints the `claude mcp add` command instead, run
     that command yourself with the CLI you are running in, if you can.
4. **Ask the user to open the bridge document.** You can open it for them with
   `Start-Process <repo>\nspire_bridge.tns` (or the desktop shortcut). It must
   stay on its first page, which shows `NSPIRE BRIDGE  tick N` with N counting up.
5. **Verify:** `uv run --script selftest.py` from the repo folder. Expect every
   line to say PASS. If the ping fails, see Troubleshooting in README.md. Don't
   edit code to make the test pass without telling the user.
6. **Tell the user** to restart Claude Code (the new MCP server loads on restart),
   that `claude mcp list` should show `nspire` connected, and how to use the
   hotkey: copy LaTeX, press Ctrl+Alt+Shift+L (Ctrl+Alt+Shift+K for decimals).

## Things to know

- **The clipboard is the transport.** Every request briefly replaces the user's
  clipboard and restores it afterwards. Don't run long test loops while the user
  is working, and never leave a `NSPIRE_IN:`/`NSPIRE_OUT:` string on their
  clipboard. Use `bridge.session(...)` from `nspire_bridge.py`, which saves and
  restores it.
- **Don't drive the TI-Nspire window** (keystrokes, clicks) unless the user asks.
  Nothing in setup needs it. The `nspire_notes` tool does type into it; only call
  it when the user wants a Notes page, and tell them not to use the keyboard
  while it runs.
- **If the bridge stops answering** while the page's tick count still rises but
  `served` doesn't, TI-Nspire has stopped seeing clipboard changes. Ask the user
  to close and reopen `nspire_bridge.tns`; there's no fix from the Python side.
- **Variables persist** in the open document (`a:=5`), and are substituted into
  answers. If a self-test case fails with a strange result, check with
  `nspire_eval` for stored variables before suspecting the code.
- **First MCP connection timing out** means dependencies were still installing.
  `setup.ps1` pre-installs them; otherwise run `uv sync --script server.py`.
- Everything runs through `uv run --script <file>`; each script declares its own
  dependencies inline (PEP 723). There is no requirements.txt or virtualenv to manage.

## Working on the code

- `nspire_bridge.py`: clipboard transport (`Bridge.session`, `Bridge.eval_one`).
- `nspire_latex.py`: `plan()` turns LaTeX into a TI-Nspire command without touching
  the bridge, so you can test translation offline; `solve_latex()` runs it.
- `server.py`: MCP tools. Keep heavy imports (SymPy) lazy: the client times out a
  server that is slow to answer `initialize`.
- `hotkey.py` / `launch.py`: the hotkey app and the shortcut's entry point.
- `nspire_notes.py` / `nspire_keys.py`: Notes-page builder. It works by keystrokes:
  Ctrl+N, `6` (Add Notes), text typed as Unicode, Ctrl+M for a math box, the
  expression pasted from the clipboard (typing it would fight the 2D editor's
  templates), Enter to evaluate, Backspace to drop the empty box Enter leaves.
  Steps are validated on the bridge first because a math-box error opens a modal
  popup inside TI-Nspire that can't be detected from outside.
  Computer-size pages: creating a computer-size document directly is unreliable
  (its add-page menu needs `6` then Enter, and a lost `6` makes Enter pick
  Calculator), so it makes a handheld Notes document, runs File > Convert to >
  computer size (which creates a converted copy), closes the handheld original
  without saving (Tab + Space on the save prompt; Enter would press Yes), and
  switches back with Ctrl+Tab. Each step checks the window title. Menus are
  reached by position so any UI language works.
- Test against the real CAS with `selftest.py`; add a case to `LATEX_CASES` when
  you change the translator.
