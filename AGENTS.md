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

1. **Check the platform.** This needs Windows or macOS with the TI-Nspire CX (CAS)
   Student or Teacher Software installed. If either is missing, stop and tell the
   user; don't try to install TI-Nspire yourself (it's licensed software from
   education.ti.com). On **macOS**, `setup.ps1`, the shortcut, the hotkey and
   `nspire_notes` don't exist or don't work: follow README.md, "macOS" (install uv,
   `uv sync --script server.py`, `claude mcp add` by hand, open the `.tns`, run
   `selftest.py`) instead of steps 3 and 6's hotkey part, and tell the user the macOS
   support hasn't been tested on a real Mac yet. Linux isn't supported (no TI-Nspire).
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
   hotkey: copy LaTeX, press Ctrl+Alt+Shift+L (Ctrl+Alt+Shift+K for decimals); copy a
   result in TI-Nspire and press Ctrl+Alt+Shift+T to get it as LaTeX.

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
  most `wait_s` for both its thread lock and the cross-process lock, then raises
  `BridgeTimeout`. The platform primitives (clipboard text, change counter, full
  snapshot and restore, keeping traffic out of history, the cross-process lock) are in
  `nspire_clip_win.py` (pywin32, named mutex) and `nspire_clip_mac.py` (NSPasteboard via
  pyobjc imported lazily, nspasteboard.org marker types, `flock` lock file), chosen by
  `sys.platform` and reached as `nspire_bridge._clip`; both expose the same names.
  Everything else (protocol, batching, late replies) is shared. `Bridge.modes()` reads `getMode(0)` and never raises (None on
  failure); the setting numbers in `MODES` come from TI's Reference Guide and haven't
  all been checked against the real CAS (the angle mode is the most certain).
  `bridge.eval_in_modes(expr, {2: 2}, timeout_s)` (inside a session) evaluates with
  other mode settings for that one expression. `math.evalStr` refuses `setMode` at top
  level (error 550, "invalid outside function or program"), so `expr` runs as the
  result of a temporary function, `Define brgmodeeval()=Func:Local brgmodeold:brgmodeold:=setMode(2,2):Return expr:EndFunc`,
  called and deleted (`DelVar`) in the same request. TI-Nspire puts the settings back
  itself when the function returns, so the document's settings never change.
  Statements a function can't run (`:=`, `→`, `Define`, `DelVar`, `:`) are refused up
  front by `mode_override_refusal()`. `selftest.py` checks this on the real CAS. A timeout while an earlier timed-out request is
  still within `LATE_WINDOW_S` says that request is probably still being computed
  (`Bridge._busy_hint`): `math.evalStr` blocks bridge.lua until it returns.
- `nspire_chars.py`: TI-Nspire's private-use characters (`NS_*`), `readable()`,
  `to_cas_chars()` (which also turns ► into `@>`: the bridge's `math.evalStr` rejects ►).
  No pywin32, so the translator imports on any OS; `nspire_bridge` re-exports them.
- `nspire_units.py`: the unit table (`UNITS`, LaTeX symbol to TI-Nspire name), the physical
  constants, `unit_command()` (unit text such as `km/h` or `_km/_hr` to TI-Nspire syntax, used by
  Notes steps' `to`) and `unit_latex()`, without SymPy. TI-Nspire's unit names ignore case (`_K` is
  Boltzmann's constant, `_C` the speed of light, `_nA` Avogadro's number, `_MW` doesn't exist):
  add a unit only after checking `1_name` on the real CAS, and add a case to `tests/test_plan.py`.
  Unknown units aren't errors in TI-Nspire (`1_mt` stays `1.*_mt`), so nothing would catch a wrong entry.
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
  `nspire_to_latex()` is the reverse direction for text a person copies from TI-Nspire
  (the hotkey's Ctrl+Alt+Shift+T, `App.to_latex`, and the `nspire_to_latex` MCP tool): no
  bridge, no evaluation. It reuses `result_to_latex()` and only adds spellings around it
  (`·`/`×`/`÷`, `²`, `⁻¹`, `30°` as the symbol `nsdegree`) and splits at top-level `|`, `→`,
  `:=` and `►`/`▸`/`@>`, converting each piece on its own. Unreadable text raises
  `LatexError`; the hotkey then leaves the clipboard alone. Cases in `tests/test_nspire_to_latex.py`.
  What TI-Nspire's own Ctrl+C puts on the clipboard (formats, `·` or `*`) hasn't been checked on
  the real app: the cases are the CAS's text through the bridge plus likely display spellings.
- Decimal commas (`nspire_latex.py`): `_braced_commas()` turns `3{,}5` into `3.5` in every mode (and refuses a
  `{,}` not between digits). `plan(..., decimal_comma=)` / `parse()` / `_Pre(decimal_comma)` pick the rest:
  `None` (default) keeps `_decimal_commas()` (a plain `1,5` is a decimal only at top level and directly in `{}`),
  `False` never reads a plain comma as a decimal, `True` is decimal-comma mode, `_decimal_commas_strict()`:
  digit,digit anywhere is a decimal, `;` inside any bracket becomes `,` (top-level `;` still separates
  equations), runs such as `1,2,3` or `1.5,2` raise `LatexError` (ambiguous, so refused). The tools pass a
  `decimal_comma` option (`nspire_latex`, `nspire_check_steps` LaTeX lines; hotkey `--decimal-comma` or
  `NSPIRE_DECIMAL_COMMA=1`, which also converts the Ctrl+Alt+Shift+T output). `format_answer(run, decimal_comma)`
  writes `result`/`approx` and their LaTeX with `to_decimal_comma()` (`1.5` becomes `1,5`, `1{,}5` in LaTeX,
  list commas become `;`); `nspire` (the command) is never changed. `nspire_plot` and `nspire_stats` take
  no LaTeX; `nspire_data.parse_table` already reads decimal commas with `;` or tabs. Notes steps use the default.
- `server.py`: MCP tools. Keep heavy imports (SymPy) lazy: the client times out a
  server that is slow to answer `initialize` (`tests/test_server.py` checks that
  startup imports neither SymPy, matplotlib nor the Notes and plot modules). It also holds the server
  `INSTRUCTIONS`, each tool's annotations (read-only / destructive), and
  `doc_warnings()`, which adds `warnings` to `nspire_latex` results (stored variables
  substituted, stored functions used or replaced, a non-radian angle mode with trig).
  `LatexAction` lists `nspire_latex.ACTIONS` by hand so SymPy stays lazy: keep the two
  in step when you add an action (a test enforces it).
  `isolated=True` (`isolated_names()`) saves every name the command(s) store or delete
  with `nspire_notes`' `scan_names`/`_snapshot`/`_undo` (imported lazily) and puts them
  back after the call; it refuses up front to replace a stored function or program.
  Nest it outside `temp_modes`, so values are read and written back in the document's
  own modes. Return types are TypedDicts so FastMCP publishes an `outputSchema`:
  declare every key a tool returns (undeclared keys are dropped from
  `structuredContent`), put optional ones in a `total=False` subclass (`NotRequired`
  isn't seen under `from __future__ import annotations`), and let top-level optional
  keys be `None` (FastMCP fills them with null). `nspire_eval` returns a
  `CallToolResult` so its first content block stays the bare result text; warnings go
  in a second block. `check_solution` is an MCP prompt.
- `nspire_log.py`: the session log, standard library only. Off unless `NSPIRE_LOG=1` (`<repo>/logs`, in
  `.gitignore`) or `NSPIRE_LOG_DIR` is set (`NSPIRE_LOG=0` wins); `log_dir()` reads the environment on every
  call. Opt-in because it writes the user's homework to disk. `record(source, **fields)` appends one Markdown
  entry (`format_entry`: input LaTeX as `$$` blocks, the command, the result as text and LaTeX, `items` for
  batches and step lists, warnings, error) to `YYYY-MM-DD.md` under a `.lock` file (`msvcrt`/`fcntl`:
  "ab" alone loses entries between processes on Windows, a test showed it) and **never raises**. server.py
  imports it as `session_log` (the `nspire_log` tool is `read_day`) and logs after each result and on failure;
  hotkey.py does the same for solves and conversions. Log only what the tool already handles: never read the
  clipboard for the log. A new tool that evaluates should call `session_log.record`. tests/conftest.py
  switches it off for every test; tests that want it set `NSPIRE_LOG_DIR` to `tmp_path`.
- `nspire_errors.py`: plain-language cause and fix per TI-Nspire error code (`explain()`,
  `explain_all()`, `with_explanation()`), stdlib only. Codes and names must match `bridge.lua`'s `ERRORS`
  (a test checks); only add a code that is in that list, and say what you saw on the real CAS if you confirm it.
  server.py appends the explanation to `nspire_eval`/`nspire_latex`/`nspire_stats` error text, and puts it in `warnings` for
  `nspire_eval_batch` failed items, `nspire_check_steps` (from each step's `cas`) and `nspire_plot` (sampled errors).
  Unknown codes get nothing.
- `nspire_plot.py`: the `nspire_plot` tool's work, without pywin32 or matplotlib at import.
  `prepare()` checks arguments offline (no `:=`/`Define`/`DelVar`: plotting stores nothing),
  `sample()` runs inside `bridge.session()`: one batch with a `seq(approx(f),x,a,b,h)` per
  function, the upper bound half a step past the last point so rounding can't change the
  count. A range that errors (non-real, domain, too long, bad reply) is split into `SPLIT`
  pieces, down to single points that become gaps; syntax-type errors (`_FATAL_CODES`) and
  non-numeric results fail at once; after a timeout the rest is retried once in pieces.
  `parse_values()` reads the list (`undef`, `±∞` and values with 𝑖 are gaps),
  `breaks()` finds asymptote jumps (not drawn), and `render_png()` imports matplotlib and
  draws after the session. The CAS computes every value: never compute points in Python.
  The tool reuses `doc_warnings` with `ignore={var}` (seq binds the plot variable).
  `kind=` "parametric", "polar" and "implicit" (`prepare_kind()`; "function" calls `prepare()`) reuse
  the same sampler: `PlotSpec.funcs` are *channels*, each one seq over one grid, and `PlotSpec.curves`
  say which channels make a curve. Parametric: x(t) and y(t), two plain channels. Polar: two channels
  that call temporary functions `mcpplotp1(a,b,h)`, ... (`Func.call`), each
  `Define mcpplotpN(mcpa,mcpb,mcph)=Func:Local mcpm,θ:mcpm:=setMode(2,1):Return seq(approx((r)*cos(θ)),θ,mcpa,mcpb,mcph):EndFunc`
  (sin for y), so the CAS computes x and y in radian mode whatever the document's angle setting (setMode
  inside a Func is put back by TI-Nspire on return). Implicit: `Define mcpplotf1(x,y)=(lhs)-(rhs)` and one
  channel per grid row, `seq(approx(mcpplotf1(x,yj)),x,...)` (x and y are the helper's own parameters,
  so stored x/y don't interfere); `GRID_MAX` per axis keeps each row reply small. `sample()` sends the
  `helpers` first and DelVars `helper_names` in a `finally` (a failed cleanup is a warning in
  `Sampled.warnings`). Python only reads the CAS values: `curve_xy()` pairs x and y (a gap unless both
  are real), `curve_breaks()` applies `breaks()` to both, `pole_points()` drops grid points around a
  sign change that jumps against the neighbouring steps (F through ±∞, not 0) and `crossing_cells()`
  finds where the contour runs; `render_png()` draws implicit curves with matplotlib `contour` at 0.
  `plot_view()` is the one window shared by `summary()` and `render_png()`.
  `annotate=[...]` (kind "function" only; `prepare_annotations()` refuses others offline) finds marked points
  in `annotate()`, inside the same `bridge.session()` after `sample()` but in its own batches, so a slow
  `zeros()` can't cost the plot: (1) one `zeros(expr,x)|x>=a and x<=b` per `AnnotationTask` (expr `(f)`,
  `derivative((f),x)` or `(f)-(g)`; use `derivative()`, the bridge leaves `d()` unevaluated, and a `|x>=a`
  constraint on `d()` gives `{}`), (2) where that reply isn't a list of points (`exact_points()`: an error,
  `{n1*π}`, a condition) or is `{}` although the samples change sign, `nSolve(expr=0,x,lo,hi)` in each
  `brackets()` interval (Python only picks intervals from the CAS's samples, skipping `breaks()`), (3) one
  `{(f),approx(x),approx((f))[,approx(derivative((f),x,2))]}|x=(point)` per point, plus for extrema
  `approx(derivative((f),x)|x=(point)±step/4)`; `_classify()` reads the signs (f''=0: the slopes decide,
  no sign change is "stationary"). Caps `MAX_PER_SET`, `MAX_MARKS`; more than `MAX_LABELS` marks are drawn
  without labels. A timeout or error is a warning (`Annotations.warnings`, `errors` for `explain_all`),
  never a failed plot. `marks_summary()` writes the summary lines and `_draw_marks()` the markers.
  Checked on the real CAS by `selftest.py` (`ANNOTATE_CASES`).
  Checked on the real CAS by `selftest.py` (Func + setMode + Local θ, Define f(x,y), DelVar), with the
  document in Radian mode only.
- `nspire_data.py`: data for TI-Nspire, standard library only: `parse_table(csv_text, columns, headers, header)`
  reads CSV text (delimiter: tab, then `;`, then `,` if any line has one, else spaces; decimal commas only
  with `;`/tab; a header row when a first-row cell isn't a number) or a list of columns into a `Table`
  (`headers`, `columns` of TI-Nspire number literals written as given, `values` as floats for drawing
  only). Limits: `MAX_ROWS`, `MAX_COLS`, `EXP_LIMIT`. Very large or small numbers become `1.5ᴇ-30`
  (accepted by the real CAS). `list_literal()` / `list_chunks()` write a column as `{...}` pieces. Also
  used by `nspire_notes` to fill a Lists & Spreadsheet page.
- `nspire_stats.py`: the `nspire_stats` tool's work, without pywin32 at import. `COMMANDS` (keep
  `server.StatCommand` in step; a test checks), `prepare()` picks columns offline, `commands()` gives the
  stores (`mcpstatx:={...}`, then `:=augment(...)` per `CHUNK` values; server.py runs them inside
  `isolated_names()`, so the names are put back) and `[command, "stat.results", "stat.RegEqn(mcpstatv)"]`,
  `run()` sends everything in one `eval_batch`, `result()` reads them. On the real CAS the stat commands
  run at top level through `math.evalStr` (no Func wrapper; they return ""), `stat.results` is a matrix of
  `["name", value]` rows whose labels use private-use characters for x̄ (U+F003), ȳ (U+F004) and the n of
  sₙ₋₁x (U+F043), and `stat.RegEqn` is a function, called with the odd name `mcpstatv` that `equation()`
  renames x. `stat.*` is left holding the results (TI-Nspire's own behaviour; not put back). With
  `plot=True` the fitted equation is sampled with `nspire_plot.sample()` and drawn by `render_png(...,
  points=...)` with the data as dots (Python draws the given data; the CAS computes the curve).
- `nspire_steps.py`: the `nspire_check_steps` tool's work, without pywin32 or SymPy at import
  (`nspire_latex` is imported only for LaTeX lines). `prepare()` reads the lines offline (a leading
  `=`, `&=`, `\iff`, `⇒` is dropped; `x=2 \lor x=3` and `x=2,\ x=3` become `solve(x=2 or x=3,x)`;
  stores and definitions are refused), `commands()` lists one `solve(line,var)` per equation line and
  one `expand((before)-(after))` per pair of expression lines (TI-Nspire has no `simplify()`: it comes
  back unevaluated), `run()` sends them in one `eval_batch`
  inside `bridge.session()`, and `judge()` reads the replies. The CAS decides: Python only compares
  the CAS's own solution strings (split at top-level ` or `) and classifies a nonzero residual
  (`residual_is_decided`: polynomial or rational means broken, anything else undecided). Undecided
  expression steps then get the numeric spot check (`run(..., spot_check=True)`): `spot_plans()` picks
  12 points per step with `spot_points()` (seeded from the two lines, so reproducible: positive
  four-digit decimals first, then negative ones, small integers, larger values; each variable its
  own value), `spot_command()` asks `approx({before,after,(before)-(after)}|x=... and y=...)`, all
  steps' points in one second `eval_batch`, and `spot_judge()` reads the replies: a point whose
  three values aren't real numbers (error, undef, 𝑖, ∞, a name left over) is skipped; `agree()`
  compares the CAS's own difference with the values (relative `SPOT_RTOL`). One disagreeing point
  makes the step broken with `spot_check.counterexample`; `SPOT_NEEDED` agreeing points (1 without
  variables) make it `probably_ok`; fewer leave it undecided. Python never computes a value.
  Skipped after a timeout in the first batch. Not offered on `nspire_eval`/`nspire_latex`: two lines
  to `nspire_check_steps` is the way to test an identity.
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
  the window's top-left area just before the menu's Down key and after it (`menu_area`/`menu_changed` in
  `nspire_keys.py`; only channel changes above `MENU_PIXEL_DELTA` count, and only within
  `MENU_OPEN_WAIT_S`: with a translucent window theme an animated wallpaper drifts through the window,
  which once made every check pass, so the keys went out blind). Never send Esc to back out: if focus
  has moved, it lands in another program (in Claude it cancels the command).
  The window is chosen once per run by its process (`is_nspire_image`,
  `pick_nspire_window` in `nspire_keys.py`), never by title, and that hwnd is used
  throughout. Validation undoes what it changes in the bridge document and warns
  about steps that read variables the new document won't have.
  Menu positions are constants at the top of `nspire_notes.py` (`INSERT_MENU`,
  `INSERT_PAGE_ITEM`, `GRAPHS_MENU_KEY`, ...). `graphs=[...]` adds a Graphs page
  (Insert > Page, then Insert > Graphs; handheld: Insert > Page and `2` in the
  add-page menu), pastes each function into the entry line and presses Enter, with
  Ctrl+G to show the entry line again before f2, f3, ... Each function is checked on
  the bridge as `Define mcpgraphchk(x)=...`, which the undo deletes.
  `spreadsheet={...}` adds a Lists & Spreadsheet page after that (Insert > Page, then Insert >
  Lists & Spreadsheet, `INSERT_LISTS_ITEM`; handheld: `LISTS_MENU_KEY` `4` in the add-page menu).
  `plan_spreadsheet()` reads the data with `nspire_data.parse_table` and refuses names that aren't
  TI-Nspire names, look like a cell (`a1`), repeat, or are defined or used by a step or graph (and
  `f1`...`fn` with graphs). Each column is checked on the bridge as `name:={...}` (unnamed ones as
  `mcpsheetchk`), `SHEET_CHUNK` values per request with `augment`, which the undo removes or puts
  back. `_type_sheet()` types, never pastes: from A1, two Ups to the name row, each name then Enter
  and two Ups (back to the name row wherever Enter went), Right; Left and two Downs back to A1; then
  each value typed with Enter (moves down), Right and one Up per row to the next column. Exponents
  are typed as TI-Nspire's private `ᴇ` character (`to_cas_chars`). The menu item, the add-page key
  and the cell navigation were written from TI's layout and are not yet checked on the real app.
  `save_as=` saves with Ctrl+S, but only types into a standard Windows file dialog
  (`#32770`) whose focused control is its file name box (`is_file_name_box`), reads the
  box back (`control_text`) before Enter, and reports success only once the dialog
  closed and the file appeared or changed on disk. Anything else (a Java dialog, a
  replace-file question, an error) stops with the dialog left for the user.
  `digits=n` sets the new document's Display Digits to Float n before typing (`_set_digits`: File >
  Settings > Document Settings, a `SunAwtDialog` that opens with Display Digits focused; Down opens
  its list (Up does nothing while it's closed), then Up x20 to Float, Down x n, Enter, Enter = OK).
  `build_notes(..., progress=cb)` calls `cb(done, total, message)` from the worker
  thread; server.py forwards it with `ctx.report_progress` through
  `asyncio.run_coroutine_threadsafe` and drops it once the call is cancelled.
- Offline tests: `uv run --no-project --with pytest --with "sympy>=1.13" --with "antlr4-python3-runtime==4.11.*" --with "pywin32>=306" --with lupa --with "mcp>=1.30,<2" --with matplotlib pytest -q tests`
  (CI runs them on Ubuntu, macOS and Windows, again on Windows with the versions pinned in
  `server.py.lock`, and runs `ruff check` with `ruff.toml` and pyright with
  `pyrightconfig.json`; tests needing pywin32, lupa or mcp are skipped without them; leave
  out `--with "pywin32>=306"` off Windows). The transport tests swap `nspire_bridge._clip`
  for a fake backend, and `tests/test_clip_mac.py` tests the macOS backend against a fake
  NSPasteboard. Lint locally with `uvx ruff check .` and type-check with
  `uv run --no-project --with-requirements server.py --with pytest --with lupa --with "pyright[nodejs]==1.1.414" pyright`
  (expect 0 errors; the warnings are known SymPy/pywin32 typing gaps). When you change the translator, add the expected command to
  `tests/test_plan.py`, and a case to `LATEX_CASES` in `selftest.py` when the
  result needs the real CAS.
- After changing a script's PEP 723 header, run `uv lock --script <file>` so its
  `.lock` file matches.
