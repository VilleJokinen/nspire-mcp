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
`{alive, api_level, cas, protocol}`.

- `alive: false` means the bridge document is not open or not on its first
  page. Ask the user to open `nspire_bridge.tns` (the "TI-Nspire bridge"
  desktop shortcut does this) and leave it on the page showing
  `NSPIRE BRIDGE  tick N`. Then call `nspire_status` again.
- `cas: false` means the non-CAS edition: numeric expressions work, symbolic
  ones (solve, factor, integrals with variables) return errors. Tell the user.
- `protocol: 1` means an older `nspire_bridge.tns`. Everything works, but
  batches are slower; you can suggest the user open the current one from the
  nspire-mcp folder.

## The tools

| Tool | Use it for |
| --- | --- |
| `nspire_status()` | Health check. First call; again after any timeout. |
| `nspire_eval` (expr, timeout_s=5) | One expression in TI-Nspire syntax. Your default tool. |
| `nspire_eval_batch(exprs, timeout_s=5)` | Several expressions in order, e.g. Define then use. With the current bridge document the whole list is one request: much faster than many single calls. |
| `nspire_latex(latex, action="auto", var, order, point, timeout_s=10)` | The user's problem is LaTeX. Returns `{action, nspire, result, result_latex, approx?}`. |
| `nspire_notes(steps, title, page_size="computer")` | The user wants the worked solution written into TI-Nspire itself. |
| `nspire_vars(values=True)` | List variables and functions stored in the open document. Use when an answer looks oddly specific. |
| `nspire_clear(names=None)` | **Destructive.** `DelVar` the named variables, or all of them when no names are given. Ask the user first. |

`timeout_s` is limited to 0.5–120 seconds.

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

### `nspire_eval_batch`

Pass a list. Earlier definitions are visible to later items. If the bridge
stops answering, the rest are returned as skipped. Each item comes back as
`{expr, ok, result}` or `{expr, ok: false, error}`; read each one.

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
are constants unless used as a summation or integration variable; `1,5`
between digits is a decimal comma, also inside `{}` groups such as
`\frac{1,5}{2}`, but not inside `()`, `[]` or `\{\}` (`f(1,5)` and
`\gcd(12,18)` keep their commas).

The LaTeX is sent as written and the CAS does all the arithmetic, so `nspire`
(the command run) mirrors the input: `\sqrt{8}` runs as `√(8)`, not `2*√(2)`.
`result_latex` keeps the CAS's form (a factored result stays factored).

If it reports that the LaTeX could not be parsed, translate it to TI-Nspire
syntax yourself and use `nspire_eval`. Show the user `result_latex` when it is
not null; otherwise show `result`.

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
a half-built page, rather than typing anywhere else. The new document is
unsaved; tell the user to save it if they want to keep it. Use
`page_size="handheld"` if they will move the document to a physical calculator.

## Pitfalls

- **Stored variables persist.** Definitions (`a:=5`, `Define f(x)=...`) stay in
  the open bridge document and are substituted into later answers. If a result
  looks wrong (a number where you expected a symbol), list stored variables
  with `nspire_vars`. Delete your own definitions with `nspire_clear(["f"])` or
  `DelVar f`; ask before deleting anything you didn't define, and never clear
  everything without the user's OK. Prefer names the user is unlikely to reuse.
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
