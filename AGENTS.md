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
     `-Startup` if they want the hotkey at login, and
     `-Scope local -ProjectDir <their project folder>` if they want the MCP server
     for one project only (`-Scope project` instead writes it to that folder's
     `.mcp.json`, with this computer's paths in it). Default is user scope (all
     projects). The script stops if `-Scope local`/`project` is given without
     `-ProjectDir`.
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
  clipboard. Use `bridge.session(...)` from `nspire_bridge.py`. It saves every
  format it can copy (text, HTML, images, files) and puts it back, keeps a copy the
  user makes during a request, keeps bridge traffic out of Win+V history, and cleans
  up a reply that arrives after a timeout. `clip_set()` inside a session counts as
  temporary and is put back too.
- **Bridge script protocol.** `nspire_status` reports `protocol`: 2 is the current
  `bridge.lua` (batch requests, late replies held back), 1 an older
  `nspire_bridge.tns`. The Python side works with both. A change to `bridge.lua` only
  takes effect once the user pastes it into the document in TI-Nspire (README,
  "Rebuilding the bridge document"); ask them to, and commit the saved `.tns`.
- **Don't drive the TI-Nspire window** (keystrokes, clicks) unless the user asks.
  Nothing in setup needs it. The `nspire_notes` tool does type into it; only call
  it when the user wants a Notes page, and tell them not to use the keyboard
  while it runs.
- **If the bridge stops answering** while the page's tick count still rises but
  `served` doesn't, TI-Nspire has stopped seeing clipboard changes. Ask the user
  to close and reopen `nspire_bridge.tns`; there's no fix from the Python side.
- **Variables persist** in the open document (`a:=5`), and are substituted into
  answers. If a self-test case fails with a strange result, list stored variables
  with `nspire_vars` before suspecting the code. `nspire_clear` deletes variables
  and can't be undone: ask the user before using it, and never clear everything
  without their OK.
- **First MCP connection timing out** means dependencies were still installing.
  `setup.ps1` pre-installs them; otherwise run `uv sync --script server.py`.
- Everything runs through `uv run --script <file>`; each script declares its own
  dependencies inline (PEP 723). There is no requirements.txt or virtualenv to manage.

## Working on the code

- `nspire_bridge.py`: clipboard transport (`Bridge.session`, `Bridge.eval_one`,
  `Bridge.eval_batch`, `Bridge.list_vars`, `Bridge.clear_vars`, `Bridge.modes`).
  `timeout_s` is clamped to 0.5–120 s here. `Bridge.session(work, wait_s)` waits at
  most `wait_s` for both its thread lock and the cross-process mutex, then raises
  `BridgeTimeout`. `Bridge.modes()` reads `getMode(0)` and never raises (None on
  failure); the setting numbers in `MODES` come from TI's Reference Guide and haven't
  all been checked against the real CAS (the angle mode is the most certain).
- `nspire_chars.py`: TI-Nspire's private-use characters (`NS_*`), `readable()`,
  `to_cas_chars()`. No pywin32, so the translator imports on any OS;
  `nspire_bridge` re-exports them.
- `nspire_latex.py`: `plan()` turns LaTeX into a TI-Nspire command without touching
  the bridge, so you can test translation offline. `run_plan()` evaluates a plan
  inside `bridge.session()`, and `format_answer()` converts the result to LaTeX
  after the session. `solve_latex()` does all three and opens its own session, so
  don't call it inside one (the lock isn't reentrant). Trees stay unevaluated:
  SymPy only reads and writes the math, and the CAS does all arithmetic.
  The translator refuses rather than guesses: `parse_latex(..., strict=True)` fails
  on input it can't read to the end, and `_check_commands` rejects any `\command`
  not in `_PARSER_CMDS` (`_FUNCLIKE` ones need parentheses). To support a new
  command, rewrite it in `_Pre` or add it there, with a case in `tests/test_plan.py`.
- `server.py`: MCP tools. Keep heavy imports (SymPy) lazy: the client times out a
  server that is slow to answer `initialize` (`tests/test_server.py` checks that
  startup imports neither SymPy nor the Notes modules). It also holds the server
  `INSTRUCTIONS`, each tool's annotations (read-only / destructive), and
  `doc_warnings()`, which adds `warnings` to `nspire_latex` results (stored variables
  substituted, stored functions used or replaced, a non-radian angle mode with trig).
  `LatexAction` lists `nspire_latex.ACTIONS` by hand so SymPy stays lazy: keep the two
  in step when you add an action (a test enforces it).
- `hotkey.py` / `launch.py`: the hotkey app and the shortcut's entry point.
- `nspire_notes.py` / `nspire_keys.py`: Notes-page builder. It works by keystrokes:
  Ctrl+N, `6` (Add Notes), text typed as Unicode, Ctrl+M for a math box, the
  expression pasted from the clipboard (typing it would fight the 2D editor's
  templates), Enter to evaluate, Backspace to drop the empty box Enter leaves.
  Steps are validated on the bridge first because a math-box error opens a modal
  popup inside TI-Nspire that can't be detected from outside.
  Every chord and every typed character goes through `_Guard` (TI-Nspire still in
  front, call not cancelled); `build_notes(..., cancel=threading.Event)` is set by
  server.py when the MCP call is cancelled. Tests fake `nspire_keys._send`.
  Computer-size pages: File > New computer document (2nd File item), then
  Insert > Notes (4th menu, 8th item), which fills the new document's empty
  first page (computer documents show no add-page menu). Menus are reached by
  position (Alt, Right, Down...) so any UI language works. Right after a document
  opens TI-Nspire ignores the Alt, so each menu step checks that the menu opened
  and retries. TI-Nspire is a Java app whose menus are drawn inside its window
  (no menu window, no Windows menu mode), so the check compares sampled pixels of
  the window's top-left area before and after (`menu_area` in `nspire_keys.py`). Never send Esc to back out: if focus
  has moved, it lands in another program (in Claude it cancels the command).
  The window is chosen once per run by its process (`is_nspire_image`,
  `pick_nspire_window` in `nspire_keys.py`), never by title, and that hwnd is used
  throughout. Validation undoes what it changes in the bridge document and warns
  about steps that read variables the new document won't have.
- Offline tests: `uv run --no-project --with pytest --with "sympy>=1.13" --with "antlr4-python3-runtime==4.11.*" --with "pywin32>=306" --with lupa --with "mcp>=1.30,<2" pytest -q tests`
  (CI runs them on Ubuntu and Windows, again on Windows with the versions pinned in
  `server.py.lock`, and runs `ruff check` with `ruff.toml` and pyright with
  `pyrightconfig.json`; tests needing pywin32, lupa or mcp are skipped without them).
  Lint locally with `uvx ruff check .` and type-check with
  `uv run --no-project --with-requirements server.py --with pytest --with lupa --with "pyright[nodejs]==1.1.414" pyright`
  (expect 0 errors; the warnings are known SymPy/pywin32 typing gaps). When you change the translator, add the expected command to
  `tests/test_plan.py`, and a case to `LATEX_CASES` in `selftest.py` when the
  result needs the real CAS.
- After changing a script's PEP 723 header, run `uv lock --script <file>` so its
  `.lock` file matches.
