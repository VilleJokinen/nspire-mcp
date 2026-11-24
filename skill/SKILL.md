---
name: nspire-cas
description: Use the user's TI-Nspire CX CAS desktop software as an exact computer algebra system through the `nspire` MCP server. Trigger when the user wants exact or symbolic math checked by a real CAS - solve, simplify, factor, expand, integrate, differentiate, limits, sums, ODEs, matrices - when they paste a problem as LaTeX, when they mention their TI-Nspire or "the nspire", or when they want a worked solution written onto a TI-Nspire Notes page.
summary: Teaches the agent when and how to use the nspire MCP server to get exact CAS answers from TI-Nspire CX CAS, solve LaTeX problems, and write worked solutions onto live TI-Nspire Notes pages.
version: 1.0.0
tags: [math, cas, ti-nspire, latex, education, mcp]
keywords: [TI-Nspire, CX CAS, computer algebra, symbolic math, solve, integrate, differentiate, LaTeX, calculus, linear algebra, differential equations, notes page]
use_cases:
  - Check an exact symbolic answer (integral, limit, equation) with a real CAS instead of from memory
  - Solve a problem pasted as LaTeX and return the answer as LaTeX
  - Write a step-by-step worked solution onto a TI-Nspire Notes page with live math boxes
  - Get the same answer the student's own calculator software would give
docs_url: https://github.com/villejokinen/nspire-mcp
---

# TI-Nspire CAS via the `nspire` MCP server

This skill tells you how to use the `nspire` MCP server, which evaluates
expressions in the TI-Nspire CX CAS desktop software running on the user's
Windows PC. The server is a separate, free install (see `references/setup.md`).
If its tools (`nspire_status`, `nspire_eval`, ...) are not available in this
session, say so and point the user to `references/setup.md`; do not pretend to
have run the CAS.

## When to use the CAS, and when not to

Use the CAS when:

- the answer must be exact (closed forms, radicals, fractions, constants of
  integration, exact roots) and getting it wrong matters, such as homework,
  exam prep or checking a derivation;
- the computation is long or error-prone by hand: integrals by parts or partial
  fractions, systems of equations, determinants and inverses, eigenvalues,
  ODEs with initial conditions, series, limits that need care;
- the user wants the answer the way their TI-Nspire gives it (same form, same
  conventions), or asks for "the nspire", "my calculator", "TI-Nspire";
- the user pastes LaTeX and wants it solved;
- the user wants the solution on a TI-Nspire Notes page.

Answer from your own knowledge, without the CAS, when:

- it is arithmetic or algebra you can state with certainty (2+2, d/dx x^2);
- the question is conceptual ("what is a derivative?", "why does this method
  work?");
- the user asks for an explanation and has not asked for verification. You can
  still check the final result with one `nspire_eval` call if it helps.

When you do use it, explain the steps yourself and use the CAS for the results.
Report the CAS result as the CAS gave it; if it differs from what you expected,
trust the CAS and say so, unless you find stored variables are the cause (see
Pitfalls).

## Before the first call: `nspire_status`

Call `nspire_status()` once at the start of a math session. It returns
`{alive, api_level, cas, protocol, modes}`.

- `alive: false` means the bridge document is not open or not on its first
  page. Ask the user to open `nspire_bridge.tns` (the "TI-Nspire bridge"
  desktop shortcut does this) and leave it on the page showing
  `NSPIRE BRIDGE  tick N`. Then call `nspire_status` again.
- `cas: false` means the non-CAS edition: numeric expressions work, symbolic
  ones (solve, factor, integrals with variables) return errors. Tell the user.
- `protocol: 1` means an older `nspire_bridge.tns`. Everything works, but
  batches are slower; you can suggest the user open the current one from the
  nspire-mcp folder.
- `modes` is the document's settings, e.g. `angle: "Radian"`,
  `auto_or_approx: "Auto"`. Degree mode changes every trig answer and
  Approximate mode gives decimals instead of exact results; mention it when it
  explains an answer. `null` means they couldn't be read.

## The tools

| Tool | Use it for |
| --- | --- |
| `nspire_status()` | Health check. First call; again after any timeout. |
| `nspire_eval` (expr, timeout_s=5, isolated=False, angle, exact_mode) | One expression in TI-Nspire syntax. Your default tool. |
| `nspire_eval_batch(exprs, timeout_s=5, isolated=False)` | Several expressions in order, e.g. Define then use. With the current bridge document the whole list is one request: much faster than many single calls. |
| `nspire_latex(latex, action="auto", var, order, point, timeout_s=10, isolated=False, angle, exact_mode)` | The user's problem is LaTeX. Returns `{action, nspire, result, result_latex, approx?, warnings?}`. Pass any `warnings` on to the user. |
| `nspire_notes(steps, title, page_size="computer", graphs, save_as)` | The user wants the worked solution written into TI-Nspire itself. |
| `nspire_plot(exprs, latex, var, x_min, x_max, y_min, y_max, samples, kind="function", t_min, t_max, annotate)` | The user wants to see a graph: functions, or `kind="parametric"`, `"polar"` or `"implicit"` curves. Returns a PNG plus a summary; describe the graph from the summary and pass on its warnings. `annotate=["zeros", "extrema", "intersections"]` marks those points, found by the CAS. |
| `nspire_stats(command, csv, columns, x_column, y_column, freq_column, plot)` | The user has data: summary statistics (`OneVar`, `TwoVar`) or a regression fit (`LinRegMx`, `QuadReg`, `ExpReg`, ...). Returns `stat.results`, the fitted `equation`, `r2`/`r`, and with `plot=True` an image of the data and the fit. |
| `nspire_vars(values=True)` | List variables and functions stored in the open document. Use when an answer looks oddly specific. |
| `nspire_clear(names=None)` | **Destructive.** `DelVar` the named variables, or all of them when no names are given. Ask the user first. |

`timeout_s` is limited to 0.5–120 seconds.

- `isolated=True`: use it for scratch work (checking a step, a helper
  `Define`, trying values). Every name the call stores or deletes is put back
  afterwards, so the user's document isn't left with your definitions. It
  refuses to run, changing nothing, if it would replace a stored function or
  program; pick another name then. Leave it off when the user wants the
  definition kept.
- `angle` (`"radian"`, `"degree"`, `"gradian"`) and `exact_mode` (`"auto"`,
  `"exact"`, `"approx"`) evaluate in that mode for one call without changing the
  document's settings, e.g. `nspire_eval("sin(30)", angle="degree")`. They can't
  be combined with anything that stores or defines (`a:=5`, `Define`): store in
  one call, then evaluate with the mode in the next. If the call fails saying the
  expression runs inside a temporary function, work in the document's mode
  (`nspire_status` shows it) or ask the user to change it.
- `nspire_check_steps(lines)` checks a worked solution line by line (LaTeX, or
  `latex=False` for TI-Nspire syntax): equal expressions by
  `expand((before)-(after))`, equations by comparing their `solve()` solutions.
  Expression steps that can't be settled exactly (trig, logs, roots) are evaluated
  by the CAS at random points: `probably_ok` (agrees everywhere it was tried; not
  a proof) or `broken` with `spot_check.counterexample` (the point and both
  values; it may mean the step holds only on a smaller domain, like `√(x^2)=x`
  for x>0). Each step is ok, probably_ok, broken (with the CAS output) or
  undecided; check undecided ones another way. It stores nothing. To test
  whether two expressions are equal, pass them as two lines. TI-Nspire has no
  `simplify()`: every result is already simplified.
- The prompt `check_solution(problem, solution)` walks through checking a
  student's worked solution step by step with `nspire_check_steps` and
  `nspire_eval_batch(..., isolated=True)`.

### `nspire_eval`

TI-Nspire syntax: `solve(x^2=4,x)`, `factor(x^2-5*x+6)`, `expand((x+1)^3)`,
`∫(x*sin(x),x,0,π)`, `d(x^3,x)` or `derivative`, `limit(sin(x)/x,x,0)`,
`Σ(1/n^2,n,1,∞)`, `deSolve(y''+y=0,x,y)`, `cSolve(x^2+1=0,x)`,
`approx(π)`, `det([[1,2][3,4]])`, `nSolve(cos(x)=x,x)`.

- Write multiplication explicitly (`5*x`, `x*(x+1)`): `x(x+1)` is a function call.
- Results use the Unicode minus `−` for negatives (`x=−2 or x=2`) and show the
  constants as `ℯ`, `𝑖`. You can send these back as input.
- Arbitrary constants appear as `c1, c2` (from deSolve) and `n1, n2` (integers).
- Raise `timeout_s` (e.g. 20) for heavy symbolic work. A timeout means the
  bridge did not answer, not that the math has no answer; check
  `nspire_status` before retrying.
- With `isolated=True` or a mode override, anything that couldn't be put back
  arrives as a second content block starting `Warnings:` (structured output
  `{result, warnings}`); tell the user.

### `nspire_eval_batch`

Pass a list. Earlier definitions are visible to later items. If the bridge
stops answering, the rest are returned as skipped. Each item comes back as
`{expr, ok, result}` or `{expr, ok: false, error}`; read each one. With
`isolated=True`, an item may also carry `warnings` about a name it changed that
couldn't be put back.

### `nspire_latex`

Pass the user's LaTeX as is (`$...$` and `\[...\]` are fine). `action="auto"`:

- `f(x)=...` defines `f` (it stays defined);
- equations with `y'`, `y''`, `\frac{dy}{dx}` go to `deSolve` (initial
  conditions such as `y(0)=1` allowed);
- an equation, inequality or system is solved;
- anything else is evaluated (integrals, limits, sums, derivatives, matrices).

Force a specific operation with `action`: `approx`, `solve`, `csolve`,
`nsolve`, `factor`, `expand`, `diff`, `integrate`, `taylor` (with `order`,
`point`), `limit` (with `point`), `det`, `inverse`, `rref`, `eigenvalues`,
`eigenvectors`, `desolve`, `define`, and others. `var` names the variable(s),
e.g. `"t"` or `"x,y"`.

Conventions: `\log` without a base is base 10, `\ln` is natural; `e` and `i`
are constants unless used as a summation or integration variable; `3{,}5` is
always the decimal 3.5, and `1,5` between digits is a decimal comma at top level and
inside `{}` groups such as `\frac{1,5}{2}`, but not inside `()`, `[]` or `\{\}` (`f(1,5)` and
`\gcd(12,18)` keep their commas). For a user who writes European notation pass
`decimal_comma=true` (`nspire_latex`, `nspire_check_steps`): `1,5` is a decimal anywhere, `;`
separates arguments and list items (`f(1;5)`), `1,2,3` is refused as ambiguous, and the answer
comes back with decimal commas (`1,5`, LaTeX `1{,}5`, lists `{1,5;2}`).

The LaTeX is sent as written and the CAS does all the arithmetic, so `nspire`
(the command run) mirrors the input: `\sqrt{8}` runs as `√(8)`, not `2*√(2)`.
`result_latex` keeps the CAS's form (a factored result stays factored).

It also reads evaluation bars (`\left.\frac{d}{dx}x^2\right|_{x=2}`),
`\frac{d^n}{dx^n}`, lists `\{1,2,3\}`, conditions (`\text{for } x>0` becomes
`|x>0`) and `:=` (`a:=5` stores `a`, so it persists like a definition). Accents
are separate variables (`\bar x` is `xbar`).

If it reports that the LaTeX could not be parsed, or that a command isn't
supported (it refuses rather than guesses), translate it to TI-Nspire syntax
yourself and use `nspire_eval`. Show the user `result_latex` when it is
not null; otherwise show `result`.

### `nspire_plot`

Graphs up to 8 functions of one variable, e.g. `exprs=["sin(x)", "x^2/4"]` or
`latex=["\frac{1}{x-2}"]`, with `x_min`/`x_max` chosen to show the interesting part.
The CAS computes every point; the image is only drawn from them. Points where a function
is undefined or non-real are gaps, and lines are not drawn across asymptotes. The text
summary lists each function's y range, its gaps and where it jumps: base what you say
about the graph on it, not on guesses from the picture. If the automatic y range cuts off
spikes, pass `y_min`/`y_max`. It stores nothing: `Define` a function with `nspire_eval`
first if you want to plot `f(x)`. Every other name needs a stored value.

Curves take `kind`:
- `"parametric"`: each item is `"x(t), y(t)"`, e.g. `exprs=["cos(3*t), sin(2*t)"]` or
  `latex=["\cos t, \sin t"]`; `t_min`/`t_max` default to 0 and 2π.
- `"polar"`: each item is r(θ), e.g. `exprs=["2*sin(4*θ)"]`; θ from 0 to 2π by default and
  always in radians (the summary says so), even when the document is in degrees.
- `"implicit"`: each item is an equation in x and y, e.g. `exprs=["x^2/9+y^2/4=1"]`; the window
  is `x_min`..`y_max` (default -10..10 each) and `samples` is grid points per axis (default 60,
  up to 120). The curve is drawn where the CAS's values of lhs−rhs change sign, so a curve that
  only touches zero, like `(x-y)^2=0`, isn't found: write it as `x-y=0`.
Up to 4 curves per call. For parametric and polar, `x_min`..`y_max` only set the view.

`annotate` (function plots only) takes any of `"zeros"`, `"extrema"`, `"intersections"`. The CAS
finds them inside `x_min`..`x_max`: zeros with `zeros(f,x)`, extrema as zeros of `derivative(f,x)`
(maximum, minimum, or "stationary" when the slope doesn't change sign, as for x^3 at 0) and
intersections as zeros of f−g for every pair. They are marked on the image (○ zero, ▲ maximum,
▼ minimum, ◇ stationary, ✕ intersection) and listed in the summary and in `marks`, each with the
CAS's own form (`x: "−√(3)"`) and decimals (`x_approx`). Quote those, not values read off the
picture. `method: "numeric"` means `zeros` couldn't answer and `nSolve` found the point between
two samples where the values change sign, so a zero where the graph only touches the axis can be
missed. Corners (abs(x) at 0) and the window's ends are not reported as extrema. At most 12 per
function (or pair) and kind, 40 in all; a search that fails or times out is a warning and the
plot is still returned. For curves, find points with `nspire_eval` (`solve`) instead.

### `nspire_stats`

Runs TI-Nspire's own statistics commands on data: pass the data as `csv` text (one row per
line; tab, `;`, `,` or space separated; a first row with words is the header; decimal commas
only with `;` or tabs) or as `columns` (`[[x...], [y...]]`). `OneVar` describes one column
(mean x̄, sx := sₙ₋₁x, σx, quartiles, ...); `TwoVar` two; the regressions fit y against x:
`LinRegMx` (m*x+b), `LinRegBx` (a+b*x), `MedMed`, `QuadReg`, `CubicReg`, `QuartReg`,
`PowerReg`, `ExpReg`, `LnReg`, `Logistic`, `LogisticD`, `SinReg`. Choose columns with
`x_column`/`y_column` (number or header name) and frequencies with `freq_column`. Report
`results`, `equation` and `r2`/`r` as given (round only when presenting) and say which fit was
used; quadratic and higher fits give R² only. `plot=True` returns an image of the data with
the fitted curve. Too few points (error 235) or non-positive data for `PowerReg`/`LnReg`/`ExpReg`
(error 260) come back as errors with an explanation. The data lists are put back afterwards;
TI-Nspire's `stat.*` variables are left with this call's results.

### `nspire_notes`

Only call this when the user asks for the solution in TI-Nspire (for example
"put this on a notes page"). It opens a new document in the TI-Nspire window,
adds a Notes page and types the solution: one line of text per step, each
optionally followed by a live math box that TI-Nspire evaluates.

Steps are objects:

```json
[
  {"text": "Factor the left side:", "math": "factor(x^2-5*x+6)"},
  {"text": "Set each factor to zero:", "math": "solve(x^2-5*x+6=0,x)"},
  {"text": "so the solutions are x = 2 and x = 3."}
]
```

A step may use `"latex"` instead of `"math"` (with optional `"action"`/`"var"`
as in `nspire_latex`). Put `Define f(x)=...` in an early step to use `f` later.

Before calling it:

1. Work out the steps and check the math with `nspire_eval` or
   `nspire_eval_batch` first.
2. Tell the user it will take about a second per step, will bring TI-Nspire to
   the front and type into it, and that they should not type or click until it
   finishes.

Every math step is validated through the bridge first; if one fails, nothing
is typed and the error names the step. What validation changes in the bridge
document is undone afterwards. The new document does not have the bridge
document's stored variables: define everything a step needs in an earlier step.
The result's `warnings` lists steps that read a stored variable (their page
result may differ) and anything validation couldn't restore; pass them on.

TI-Nspire is found by its program, never by window title, and `window` in the
result says which window was used. If TI-Nspire loses focus it stops, leaving
a half-built page, rather than typing anywhere else. Use
`page_size="handheld"` if they will move the document to a physical calculator.

Optional extras:

- `graphs=["x^2", "2*x"]` adds a Graphs page after the Notes page with those
  functions of x (TI-Nspire syntax, or LaTeX between `$...$`; a leading `y=` or
  `f(x)=` is dropped; no equations or inequalities). They are checked on the
  bridge first, like the steps.
- `spreadsheet={"csv": "t,d
0,0
1,4.9"}` (or `{"columns": [[...], ...],
  "names": [...]}`) adds a Lists & Spreadsheet page with that data in columns A,
  B, ...; each named column becomes a list variable of the new document. Names
  must be valid TI-Nspire names (no spaces, not a cell like `a1`) that no step or
  graph uses. Numbers only; values are typed one by one (about 0.15 s each).
- `save_as="C:\\Users\\...\\name.tns"` saves the new document. Only pass a path
  the user gave or agreed to; the folder must exist, and an existing file is
  refused unless the user said to replace it (`overwrite=true`). Check
  `save.saved` in the result: when it is false, `save.message` says why and the
  Save dialog may still be open in TI-Nspire for the user to finish. Without
  `save_as` the document is unsaved; tell the user to save it if they want to
  keep it.

## Pitfalls

- **Stored variables persist.** Definitions (`a:=5`, `Define f(x)=...`) stay in
  the open bridge document and are substituted into later answers. If a result
  looks wrong (a number where you expected a symbol), list stored variables
  with `nspire_vars`. `nspire_latex` adds a `warnings` entry when it
  substituted a stored variable or used a stored function. For scratch work
  pass `isolated=True` so your definitions don't stay at all. Delete your own
  definitions with `nspire_clear(["f"])` or
  `DelVar f`; ask before deleting anything you didn't define, and never clear
  everything without the user's OK. Prefer names the user is unlikely to reuse.
- **A timeout can mean TI-Nspire is still computing.** The bridge script is
  blocked until a command finishes, so after one request times out the next
  ones can't be answered either; the error then says the earlier request is
  probably still being computed. Wait, then retry with a longer `timeout_s`;
  don't fire more requests at it meanwhile.
- **Modes:** the angle mode and Auto/Approximate change answers. Prefer
  `angle=` / `exact_mode=` for one call over asking the user to change the
  document, and say which mode you used.
- **The clipboard is the transport.** Each call briefly replaces the user's
  clipboard and puts it back afterwards (well under a second), including images,
  HTML and copied files, and keeps the traffic out of Win+V history. A copy the
  user makes during a call is kept. Still, avoid long loops of calls while they
  are working; batch instead.
- **The bridge page must stay open and on its first page.** The document
  `nspire_bridge.tns` must be open in TI-Nspire, showing
  `NSPIRE BRIDGE  tick N` with N counting up. TI-Nspire may be in the
  background, but if the user navigates away from that page, calls time out.
- **Do not touch the keyboard during `nspire_notes`.** Neither you nor the user
  should send other input to TI-Nspire while it types.
- **Only drive the TI-Nspire window through `nspire_notes`**, and only when the
  user asked for a Notes page.
- **If the bridge stops answering:** call `nspire_status`. If it is not alive,
  ask the user to check that the bridge page is open and its tick count rises.
  If the tick count rises but requests still time out, TI-Nspire has stopped
  seeing clipboard changes: ask the user to close and reopen
  `nspire_bridge.tns`. Retrying from your side will not fix that. More in
  `references/troubleshooting.md`.

## Worked examples

The exact text of results can vary with the TI-Nspire version; the shape is
what matters.

**Definite integral, exact.** User: "What's the integral of x sin x from 0 to
pi? Check it on my nspire."

1. `nspire_status()` returns `{alive: true, cas: true}`.
2. `nspire_eval` with `∫(x*sin(x),x,0,π)` returns `π`.
3. Answer: integration by parts with u = x, dv = sin x dx gives
   [-x cos x + sin x] from 0 to pi = pi. The CAS confirms: **π**.

**LaTeX system.** User pastes `\begin{cases} 2x+y=5 \\ x-y=1 \end{cases}`.

1. `nspire_latex(latex)` returns `action: "solve"`, `result: "x=2 and y=1"`.
2. Answer with `result_latex`: x = 2, y = 1.

**ODE with an initial condition.** User: "Solve y' = 2y, y(0)=3."

1. `nspire_latex("y'=2y,\\ y(0)=3")` runs `deSolve(...)` and returns
   `y=3*ℯ^(2*x)`.
2. Answer: y = 3e^{2x}.

More in `references/examples.md`.
