# Examples

Results are shown in the shape the CAS returns them; exact text can vary with
the TI-Nspire version. Negatives use the Unicode minus `−`.

## nspire_eval

| Task | expr passed to `nspire_eval` | Typical result |
| --- | --- | --- |
| Solve a quadratic | `solve(x^2-5*x+6=0,x)` | `x=2 or x=3` |
| Factor | `factor(x^4-1)` | `(x−1)*(x+1)*(x^2+1)` |
| Complex roots | `cSolve(x^2+1=0,x)` | `x=−𝑖 or x=𝑖` |
| Definite integral | `∫(x*sin(x),x,0,π)` | `π` |
| Antiderivative | `∫(1/(x^2+1),x)` | `tan⁻¹(x)` |
| Derivative | `d(x^3*ln(x),x)` | `3*x^2*ln(x)+x^2` |
| Limit | `limit(sin(x)/x,x,0)` | `1` |
| Series | `Σ(1/n^2,n,1,∞)` | `π^2/6` |
| Decimal value | `approx(√(2))` | `1.41421356237` |
| Determinant | `det([[1,2][3,4]])` | `−2` |

## nspire_eval_batch: define, then use

```
nspire_eval_batch([
  "Define f(x)=x^3-3*x",
  "solve(d(f(x),x)=0,x)",
  "f(−1)",
  "f(1)",
  "DelVar f"
])
```

Finds the critical points x = −1 and x = 1 and their values 2 and −2, then
removes `f` so it does not affect later work.

## nspire_latex

| LaTeX | action | What runs |
| --- | --- | --- |
| `\int_0^1 x e^x\,dx` | auto | evaluate the integral (result `1`) |
| `x^2-4=0` | auto | `solve(x^2-4=0,x)` |
| `\begin{cases}2x+y=5\\x-y=1\end{cases}` | auto | solve the system for `{x,y}` |
| `\lim_{x\to 0^+} x\ln x` | auto | one-sided limit (result `0`) |
| `y''+y=0,\ y(0)=0,\ y'(0)=1` | auto | `deSolve(...)` (result `y=sin(x)`) |
| `\begin{vmatrix}1&2\\3&4\end{vmatrix}` | auto | determinant (result `−2`) |
| `\sin x` | taylor, order 5, point 0 | Taylor polynomial |
| `x^4-1` | factor | `factor(x^4-1)` |
| `f(x)=x^2+1` | auto | `Define f(x)=x^2+1` (stays defined) |

Show the user `result_latex` when present; it is ready to paste into a
document.

## nspire_notes: area between curves

User: "Find the area between y = x^2 and y = 2x on a notes page."

1. Check the math first:
   `nspire_eval_batch(["solve(x^2=2*x,x)", "∫(2*x-x^2,x,0,2)"])`
   gives `x=0 or x=2` and `4/3`.
2. Tell the user it will type into TI-Nspire for a few seconds and they
   should not use the keyboard or mouse meanwhile.
3. Call:

```json
{
  "title": "Area between y = x^2 and y = 2x",
  "steps": [
    {"text": "Intersections, where x^2 = 2x:", "math": "solve(x^2=2*x,x)"},
    {"text": "On [0, 2] the line is above the parabola, so the area is"},
    {"text": "A =", "math": "∫(2*x-x^2,x,0,2)"},
    {"text": "The area is 4/3."}
  ]
}
```

4. Tell the user the new document is unsaved.
