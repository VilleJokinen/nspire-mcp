# nspire-mcp

Use the TI-Nspire CX CAS on your computer as a math engine for Claude (or any MCP client),
and solve LaTeX from the clipboard with a hotkey.

- **MCP server**: tools to evaluate TI-Nspire expressions and to solve LaTeX problems
  (equations, systems, integrals, limits, sums, derivatives, ODEs, matrices…) with
  answers returned as text and LaTeX.
- **Clipboard hotkey**: copy LaTeX anywhere, press Ctrl+Alt+Shift+L, and the answer
  pops up and replaces the clipboard as LaTeX.

How it works: a Lua script on a page of an open TI-Nspire document watches the
Windows clipboard. The Python side writes a request there and reads the CAS's
answer back. Nothing is sent anywhere else.

> Setting this up with an AI coding agent? Point it at [AGENTS.md](AGENTS.md).

## Requirements

- Windows 10 or 11.
- **TI-Nspire CX CAS Student Software** or **Teacher Software** (desktop app).
  The non-CAS edition works for numeric expressions only.
- [uv](https://docs.astral.sh/uv/) (`winget install astral-sh.uv`). It downloads the right
  Python (3.11–3.13) and every dependency by itself; you don't need Python installed.
- For the MCP tools: [Claude Code](https://claude.com/claude-code) (CLI, desktop app or IDE),
  or another MCP client.

## Setup

1. Get the code: `git clone https://github.com/VilleJokinen/nspire-mcp` (or download the ZIP and extract it).
2. In PowerShell, in the project folder, run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File setup.ps1
   ```

   It checks for TI-Nspire and uv, installs the Python dependencies, registers the
   MCP server with Claude Code for all your projects, and puts a **TI-Nspire bridge**
   shortcut on your desktop. Options:

   | Option | Does |
   | --- | --- |
   | `-InstallUv` | install uv with winget if it's missing |
   | `-Scope local` / `-Scope project` | register the MCP server for this project only instead of all projects |
   | `-NoMcp` | skip the Claude Code registration (hotkey only) |
   | `-NoShortcut` | no desktop shortcut |
   | `-Startup` | also start the hotkey at login |

   It's safe to run again, e.g. after moving the folder.
3. Double-click **TI-Nspire bridge** on the desktop (or open `nspire_bridge.tns` in
   TI-Nspire). The page should show `NSPIRE BRIDGE  tick N` with N counting up.
4. Check everything end to end:

   ```powershell
   uv run --script selftest.py
   ```

   All lines should say PASS.
5. Restart Claude Code. `claude mcp list` should show `nspire` connected.

Doing it by hand instead, or using another MCP client: see [Manual setup](#manual-setup).

## Using it

Each time: open `nspire_bridge.tns` (the shortcut does this) and leave it on the
bridge page. TI-Nspire can be in the background. Definitions you make
(`Define f(x)=…`, `a:=5`) are kept in the document and are saved if you save it.

### With Claude

Just ask, e.g. *"Solve \int_0^\pi x\sin x\,dx on my TI-Nspire"* or
*"factor x^4-1 with the nspire"*. The tools:

| Tool | Does |
| --- | --- |
| `nspire_status()` | ping: `alive`, `api_level`, `cas` |
| `nspire_eval(expr, timeout_s=5)` | evaluate one TI-Nspire expression, e.g. `solve(x^2=4,x)` |
| `nspire_eval_batch(exprs, timeout_s=5)` | several in order: `[{expr, ok, result \| error}]`; stops at the first timeout |
| `nspire_latex(latex, action="auto", var, order, point, timeout_s=10)` | solve/evaluate LaTeX; returns `{action, nspire, result, result_latex, approx?}` |
| `nspire_notes(steps, title, page_size="computer")` | write a worked solution onto a new TI-Nspire Notes page (see below) |

### Worked solutions on a Notes page

Ask e.g. *"Solve this on a notes page in TI-Nspire: find the area between y=x² and y=2x"*.
Claude plans the steps and calls `nspire_notes`, which opens a **new document** in
TI-Nspire, adds a Notes page and types the solution: one line per step, text
followed by a live math box that TI-Nspire evaluates itself (`input ▸ result`),
so you can edit any step and the page recalculates.

The page is computer size (fills the window) by default. Ask for a handheld-size
page (`page_size="handheld"`) if you'll move the document to a calculator.

- Every math step is first checked through the bridge; if one fails, nothing is
  typed. Names the check defines (`Define f(x)=…`, `h:=3`) are deleted from the
  bridge document again unless they existed before; an existing variable that a
  step reassigns keeps the new value there.
- It **types into the TI-Nspire window**, about a second per step, and brings it to
  the front. Don't type or click while it runs. If TI-Nspire loses focus it stops
  (leaving the page half built) rather than typing anywhere else.
- The bridge document stays open on its page; the new document is unsaved.
- It navigates TI-Nspire's menus by position, not by name, so it works in any UI
  language, but it assumes the layout of CX CAS Student/Teacher Software 6.x:
  "Add Notes" is item 6 of the add-page menu (`NOTES_MENU_KEY` in
  `nspire_notes.py`), New computer document is the 2nd File menu item, and Notes
  is the 8th item of the Insert menu (the 4th menu). If a page comes out as the
  wrong app, or the wrong kind of document opens, your version's menus differ;
  `page_size="handheld"` uses Ctrl+N and the add-page menu instead.

### Clipboard hotkey

The desktop shortcut starts it. Or run it in a terminal:

```powershell
uv run --script hotkey.py          # with a console; Ctrl+C stops it
uv run --gui-script hotkey.py      # no console
```

| Hotkey | Does |
| --- | --- |
| Ctrl+Alt+Shift+L | solve/evaluate the LaTeX on the clipboard |
| Ctrl+Alt+Shift+K | same, but the decimal value is the answer |
| Ctrl+Alt+Shift+Q | quit |

The answer shows in a popup in the bottom-right corner (click it to close) and
replaces the clipboard as LaTeX. Options: `--copy text` (TI-Nspire text instead of
LaTeX), `--copy none` (leave the clipboard alone), `--hotkey ctrl+alt+f9`,
`--approx-hotkey …`, `--quit-hotkey …`. Only one copy runs at a time.

### What the LaTeX solver understands

`$…$`, `\[…\]`, `\frac`, `\sqrt[n]{}`, `\int` (limits, `\,dx`, `\mathrm{d}x`), `\iint`,
`\sum`, `\prod`, `\lim` (`x\to a`, `a^+`), `\frac{d}{dx}`, `y'`, `y''`, `\frac{dy}{dx}`,
`\dot y`, matrices (`pmatrix`, `bmatrix`; `vmatrix` = determinant), `cases`/`align`
systems, several equations separated by `\\`, commas or `;`, `30^\circ`, `|x|`, `n!`,
`\binom`, Greek letters.

With `action="auto"` (what the hotkey uses):

| Input | Runs |
| --- | --- |
| `f(x)=x^3-3x` | `Define f(x)=…` (stays defined) |
| equations with `y'`, `y''`, `\frac{dy}{dx}`, `\dot y` (+ `y(0)=1`…) | `deSolve(… and …, x, y)` |
| an equation or inequality | `solve(…, x)` (or the variable present) |
| several equations | `solve(… and …, {x,y})` |
| anything else | evaluated: integrals, limits, sums, derivatives, `f'(2)`, matrices… |

Other actions: `eval approx solve csolve nsolve zeros czeros factor cfactor
expand propfrac comdenom texpand tcollect diff integrate taylor limit det
inverse rref eigenvalues eigenvectors desolve define`.

Conventions: `\log` without a base is base 10 (as on the calculator), `\ln` is
natural; `e` and `i` are the constants unless used as a sum/integral variable;
`x(x+1)` is a product (only `f g h F G H` are functions); `1,5` between digits
is a decimal comma. Exact numeric answers also get an `approx`. Variables stored
in the open document (e.g. `a:=5`) are substituted, so `DelVar a` if answers look odd.

## Manual setup

Use full paths: Claude Code may not see a PATH that changed after it started.
`(Get-Command uv).Source` prints uv's path.

Install the dependencies once, so the first connection doesn't time out while
SymPy downloads:

```powershell
uv sync --script server.py
uv sync --script hotkey.py
```

**Claude Code** (CLI, desktop app's Code tab, IDE extensions):

```powershell
claude mcp add -s user nspire -- "<full path to uv.exe>" run --script "<full path to this folder>\server.py"
```

If `claude` isn't on PATH (desktop app only), the app bundles the CLI at
`%APPDATA%\Claude\claude-code\<version>\<hash>\claude.exe`.
Remove with `claude mcp remove -s user nspire`.

**Claude desktop app, Chat tab** (and most other MCP clients): add to
`%APPDATA%\Claude\claude_desktop_config.json` (`setup.ps1` prints this with your
paths filled in), then fully quit and reopen the app:

```json
{
  "mcpServers": {
    "nspire": {
      "command": "C:\\path\\to\\uv.exe",
      "args": ["run", "--script", "C:\\path\\to\\nspire-mcp\\server.py"]
    }
  }
}
```

**Desktop shortcut**: target `"<folder of uv.exe>\uvw.exe" run --gui-script "<this folder>\launch.py"`,
start in this folder. `uvw.exe` is uv's console-less twin. `launch.py` opens
`nspire_bridge.tns` and starts the hotkey.

### Rebuilding the bridge document

`nspire_bridge.tns` in this repo already contains the script. If it won't open in
your TI-Nspire version, make your own:

1. In TI-Nspire, File > New TI-Nspire Document. If it asks for a page type, add a Calculator page.
2. Insert > Script Editor > Insert Script. Name it `bridge`.
3. Delete the template text, paste all of `bridge.lua`, click **Set Script** (Ctrl+S), close the editor.
4. The page shows `NSPIRE BRIDGE  tick N` with N going up.
5. File > Save As > `nspire_bridge.tns` in this folder (the shortcut opens it from here).

To update the script later: open the document, Insert > Script Editor > Edit Script, paste, Set Script, save.

## Self-test

With the bridge open:

```powershell
uv run --script selftest.py
```

Pings, then checks `2+2`, `factor`, `solve`, `∫`, `cSolve`, a deliberate syntax error,
and ten LaTeX problems (integral, limit, sum, system, determinant, ODE…).
On the non-CAS edition the symbolic cases pass if they return a clean error, and
the LaTeX cases are skipped.

## Troubleshooting

- **"No reply from the TI-Nspire bridge":** TI-Nspire isn't open, `nspire_bridge.tns`
  isn't, or the bridge page isn't the active page (a script on a page you've navigated
  away from may stop). Run `uv run --script selftest.py` to check outside Claude.
- **Tick count rises but `served` doesn't change:** TI-Nspire has stopped seeing
  clipboard changes. Close and reopen `nspire_bridge.tns` (or restart the app).
- **Tick counter not moving:** click the bridge page once, or re-run Set Script.
- **apiLevel error when setting the script:** change `'2.7'` to `'2.4'` at the top of `bridge.lua`.
- **`nspire` shows failed in `claude mcp list`:** run the registered command by hand; it
  should sit silently waiting on stdin (Ctrl+C to quit). A connect timeout on the very
  first run means dependencies were still downloading: run `uv sync --script server.py`
  and restart Claude Code. An import error means the wrong Python; uv must pick 3.11–3.13.
- **"clipboard stayed locked":** a clipboard manager or remote-desktop session is holding
  it; retry, or pause that tool.
- **Hotkey does nothing:** another program owns the key combination. Start `hotkey.py`
  in a terminal to see which keys failed, and pick others with `--hotkey`.
- **Odd answers:** variables stored in the document are substituted. `DelVar` them.
- **Non-CAS edition:** symbolic input returns a TI-Nspire error with a note; numeric
  expressions work.

## How it works

### Clipboard caveat

While a request runs (well under a second) the clipboard holds the request,
then the reply, then your old text again. Don't copy anything yourself during
that window, or your copy may be lost or the request may time out.
The clipboard text is saved before each call and restored after (only if the
clipboard still holds bridge traffic, so a copy you made meanwhile wins).
Non-text clipboard contents, such as an image, are not restored. One request
is in flight at a time, across processes (the MCP server and the hotkey share a
named mutex).

### Protocol

| Message | Meaning |
| --- | --- |
| `NSPIRE_IN:<id>:<expr>` | evaluate `expr` |
| `NSPIRE_IN:<id>:__PING__` | health check |
| `NSPIRE_OUT:<id>:OK:<result>` | result, exactly as the CAS writes it |
| `NSPIRE_OUT:<id>:OK:PONG:<apiLevel>:<cas\|nocas>` | ping reply |
| `NSPIRE_OUT:<id>:ERR:<message>` | e.g. `error 910 (syntax error)` |

`<id>` must not contain `:`. Each id is answered once.

- Negative numbers come back with the Unicode minus `−` (U+2212), e.g. `x=−2 or x=2`.
- The CAS writes ℯ, 𝑖, the exponent mark ᴇ and the constants c1/n1 as private-use
  characters that render as nothing. The Python side converts them to those readable
  characters, and ℯ, 𝑖, ᴇ back on input.

### Files

| File | What |
| --- | --- |
| `bridge.lua` | the TI-Nspire side (inside `nspire_bridge.tns`) |
| `nspire_bridge.py` | clipboard transport shared by everything below |
| `nspire_latex.py` | LaTeX → TI-Nspire syntax → result → LaTeX (SymPy) |
| `nspire_notes.py` | builds a Notes page of text and math boxes |
| `nspire_keys.py` | keystrokes into the TI-Nspire window (used by `nspire_notes.py`) |
| `server.py` | MCP server |
| `hotkey.py` | clipboard hotkey and popup |
| `launch.py` | opens the document and starts the hotkey (desktop shortcut) |
| `setup.ps1` | one-step setup |
| `selftest.py` | end-to-end check |

## License

All rights reserved. You may download, run and modify this software for your
own personal, non-commercial use; redistribution and commercial use need
written permission. See [LICENSE](LICENSE).
