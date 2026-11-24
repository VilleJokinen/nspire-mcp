"""Plain-language explanations for TI-Nspire error codes.

No imports beyond the standard library, so server.py and the tool modules can use it
at startup. The codes and their TI names are the ones bridge.lua reports ("error 910
(syntax error)"), taken from the Reference Guide's "Error Codes and Messages" list;
the causes and fixes are written here. Codes not in HINTS get no explanation.
Codes seen on the real CAS through the bridge: 140, 230, 240, 550, 800, 910, 930, 940.
"""
from __future__ import annotations

import re
from collections.abc import Iterable

_CODE_RE = re.compile(r"\berror (\d+)", re.IGNORECASE)

# code: (TI's name, likely cause, likely fix)
_TABLE: dict[int, tuple[str, str, str]] = {
    10: ("a function did not return a value",
         "A function you defined ended without giving back a value.",
         "End it with Return expr (or make the last statement the value)."),
    20: ("a test did not resolve to TRUE or FALSE",
         "An If/While condition doesn't reduce to true or false, usually because it contains a variable with no value.",
         "Give the variables values, or write the condition so it compares numbers."),
    40: ("argument error", "A function got an argument of the wrong kind or outside its allowed range.",
         "Check the argument types, order and range in the Reference Guide."),
    50: ("argument mismatch", "Two arguments that must agree in size or type don't (for example lists of different length).",
         "Make the arguments the same length or type."),
    60: ("argument must be a Boolean expression or integer", "A condition or count was given something else.",
         "Pass true/false or a whole number."),
    70: ("argument must be a decimal number", "The function needs a numeric (decimal) value but got an exact or symbolic one.",
         "Wrap the argument in approx(...), or give a number."),
    90: ("argument must be a list", "The function needs a list like {1,2,3}.", "Pass a list."),
    100: ("argument must be a matrix", "The function needs a matrix like [[1,2],[3,4]].", "Pass a matrix."),
    130: ("argument must be a string", "The function needs text in quotes.", 'Pass a string such as "abc", or convert with string(...).'),
    140: ("argument must be a variable name", "The function takes a variable name here, not a value or expression.",
          "Pass a bare name (DelVar a, not DelVar 5 or DelVar π); use isolated=True or nspire_vars to see what exists."),
    160: ("argument must be an expression", "The function needs an expression and got something else.", "Pass an expression."),
    170: ("bound", "The lower bound is not less than the upper bound.", "Swap the bounds, or make the lower one smaller."),
    190: ("circular definition", "A variable or function is defined in terms of itself.",
          "Give it a value first, use another name, or clear it with DelVar."),
    230: ("dimension", "A list or matrix index or size doesn't fit the operation (for example multiplying matrices whose inner sizes differ).",
          "Check the sizes: matrix product needs columns of the left = rows of the right."),
    240: ("dimension mismatch", "Lists or matrices of different sizes were combined element by element.",
          "Make the lists or matrices the same size."),
    250: ("divide by zero", "Something was divided by zero.", "Change the divisor, or restrict the input so it isn't zero."),
    260: ("domain error", "An argument is outside the function's domain.",
          "Check the argument's range (for example arcsin needs -1 to 1, ln needs a positive number)."),
    270: ("duplicate variable name", "The same name appears twice where each must be unique.", "Use different names."),
    295: ("excessive iteration", "An iterative calculation didn't finish within its limit.",
          "Use a simpler problem, a closer starting guess, or fewer iterations."),
    345: ("inconsistent units", "Quantities with incompatible units were combined.",
          "Convert to compatible units first, e.g. 5_km@>_m."),
    350: ("index out of range", "A list, matrix or string index is past the end (or below 1).", "Use an index from 1 to the length."),
    360: ("indirection string is not a valid variable name", "The text given to # is not a legal name.", "Use a valid variable name."),
    380: ("undefined Ans", "Ans was used but there is no previous answer.", "Don't rely on Ans over the bridge; write the value out."),
    390: ("invalid assignment", "The left side of := or → can't be assigned to.", "Assign to a variable name, e.g. a:=5."),
    400: ("invalid assignment value", "The value can't be stored in that variable.", "Store a value of a type the variable accepts."),
    410: ("invalid command", "The command or statement can't be used here.", "Check the spelling and where the command is allowed."),
    430: ("invalid for the current mode settings", "The operation isn't allowed with the document's mode settings.",
          "Change the mode (File > Settings > Document Settings), or use an angle / exact_mode override."),
    440: ("invalid implied multiply", "Two things were written next to each other and TI-Nspire can't read it as a product (for example a multi-letter name).",
          "Write the multiplication with * or a space, e.g. 2*x or a*b."),
    450: ("invalid in a function or current expression", "A command that only works in the calculator's own line (or a program) was used inside a function or expression.",
          "Run it as its own expression instead."),
    510: ("invalid list or matrix", "The list or matrix is malformed or an element isn't allowed.", "Check brackets, commas and element types."),
    550: ("invalid outside function or program", "The command (setMode, Return, Local, ...) can only run inside a function or program, not at top level.",
          "Wrap it in a Define ... Func/Prgm block, or use the angle / exact_mode option instead of setMode."),
    560: ("invalid outside Loop..EndLoop, For..EndFor, or While..EndWhile blocks", "Exit or Cycle was used outside a loop.",
          "Put it inside a loop."),
    565: ("invalid outside program", "The command only works inside a program.", "Put it in a Define ... Prgm block."),
    605: ("invalid use of units", "Units were used where they aren't allowed (for example inside an exponent or a function that takes plain numbers).",
          "Use units only on values; divide them out first if the function needs a bare number."),
    620: ("invalid variable or function name", "The name isn't legal (reserved word, starts with a digit, or has bad characters).",
          "Use a name that starts with a letter and has no spaces or symbols."),
    630: ("invalid variable reference", "The variable can't be referenced this way.", "Check the variable's name and type."),
    640: ("invalid vector syntax", "A vector is written wrongly.", "Write vectors as [1,2,3] (a row) or [[1][2][3]] (a column)."),
    665: ("matrix not diagonalizable", "The matrix has no full set of eigenvectors.", "Use a different method, e.g. Jordan form or just the eigenvalues."),
    670: ("low memory", "TI-Nspire ran out of memory.", "Delete large variables (ask the user before nspire_clear) or simplify the problem."),
    672: ("resource exhaustion", "The calculation needed more time or memory than TI-Nspire allows.",
          "Use a smaller problem or break it into steps; try approx(...) for a numeric answer."),
    673: ("resource exhaustion", "The calculation needed more time or memory than TI-Nspire allows.",
          "Use a smaller problem or break it into steps; try approx(...) for a numeric answer."),
    680: ("missing (", "An opening parenthesis is missing.", "Check that every ) has a matching ( and that functions are written f(x)."),
    690: ("missing )", "A closing parenthesis is missing.", "Add the missing )."),
    700: ("missing \"", "A closing quote is missing.", 'Close the string with ".'),
    710: ("missing ]", "A closing bracket is missing.", "Add the missing ]."),
    720: ("missing }", "A closing brace is missing.", "Add the missing }."),
    730: ("missing start or end of block syntax", "A block (If/For/While/Func/Prgm) lacks its start or end word.", "Add the matching EndIf, EndFor, EndFunc, ..."),
    740: ("missing Then in the If..EndIf block", "An If block with several lines is missing Then.", "Write If cond Then ... EndIf."),
    750: ("name is not a function or program", "A name was called like f(x) but isn't a function or program.",
          "Define it first (Define f(x)=...), or use * if you meant multiplication, e.g. a*(b+c)."),
    780: ("no solution found", "The numeric solver found no answer.", "Try a different starting guess or interval, or a different method."),
    800: ("non-real result", "The result isn't a real number (for example √(−4), ln of a negative, arcsin(2)) and the document is in Real mode.",
          "Use cSolve/cFactor or complex-aware functions, or ask the user to switch Real or Complex to Rectangular in Document Settings."),
    830: ("overflow", "A number is too large or small for TI-Nspire to represent.", "Rescale the problem or work symbolically."),
    860: ("recursion too deep", "A function called itself too many times.", "Add a base case, or use a loop or seq/sum instead."),
    870: ("reserved name or system variable", "The name belongs to TI-Nspire (a built-in function or system variable).", "Pick another name."),
    900: ("argument error", "A function got an argument of the wrong kind or outside its allowed range.",
          "Check the argument types, order and range in the Reference Guide."),
    910: ("syntax error", "The input isn't valid TI-Nspire syntax (unbalanced brackets, an operator with nothing after it, a stray symbol).",
          "Check brackets and operators; write multiplication with *, powers with ^, and functions as f(x)."),
    930: ("too few arguments", "The function was given fewer arguments than it needs (for example solve(x^2=4) without the variable).",
          "Add the missing ones, e.g. solve(x^2=4,x)."),
    940: ("too many arguments", "The function was given more arguments than it takes.", "Remove the extra ones or check the function's syntax."),
    950: ("too many subscripts", "More subscripts than the variable has dimensions.", "Use one index for lists, two for matrices."),
    955: ("too many undefined variables", "The system has more unknown variables than the solver can handle.", "Give some variables values, or solve fewer unknowns."),
    960: ("variable is not defined", "A variable or function has no value in the document.",
          "Define it first (a:=5 or Define f(x)=...), or pass it as a symbol the command can treat as unknown."),
    970: ("variable in use so references or changes are not allowed", "The variable is in use by a graph, table or other open item.",
          "Use another name, or remove the thing using it."),
    980: ("variable is protected", "The variable is locked or belongs to TI-Nspire.", "Use another name, or Unlock it if it is the user's own."),
    990: ("invalid variable name", "The name isn't legal here.", "Use a name that starts with a letter and has no spaces or symbols."),
    1020: ("internal error", "TI-Nspire hit an internal fault.", "Simplify the input and retry; if it repeats, tell the user."),
    1040: ("unsupported function (requires the CAS)", "A CAS-only function was used on the non-CAS edition.", "Use numeric expressions, or the CAS edition."),
    1045: ("unsupported operator (requires the CAS)", "A CAS-only operator was used on the non-CAS edition.", "Use numeric expressions, or the CAS edition."),
    1050: ("unsupported feature (requires the CAS)", "A CAS-only feature was used on the non-CAS edition.", "Use numeric expressions, or the CAS edition."),
    1060: ("input argument must be numeric", "A symbolic value (a variable with no value) was given where a number is needed.",
           "Give the variable a value, or substitute with |x=..."),
    1070: ("trig function argument too big for accurate reduction", "The angle is so large its trig value would be inaccurate.", "Reduce the angle first (subtract multiples of 2π)."),
    1090: ("function is not defined", "A function you called isn't defined in the document.", "Define it first with Define f(x)=..., or check the spelling."),
    1100: ("non-real calculation", "An intermediate step of the calculation isn't a real number.",
           "Use complex-aware functions (cSolve, cFactor), or restrict to values where it is real."),
    1110: ("invalid bounds", "The lower and upper bounds aren't valid for this command.", "Put the lower bound first and check both are numbers."),
    1120: ("no sign change", "The solver needs the function to change sign over the interval and it doesn't.", "Pick an interval where f takes both signs."),
    1130: ("argument cannot be a list or matrix", "The function takes a single value, not a list or matrix.", "Map it over the list yourself, e.g. seq(f(k),k,1,n)."),
    1140: ("argument error", "A function got an argument of the wrong kind or outside its allowed range.",
           "Check the argument types, order and range in the Reference Guide."),
    1150: ("argument error", "A function got an argument of the wrong kind or outside its allowed range.",
           "Check the argument types, order and range in the Reference Guide."),
}

# TI's names for the codes above, to find a code from a message without the number.
_BY_NAME: dict[str, int] = {}
for _c in sorted(_TABLE, reverse=True):      # the lowest code wins a duplicated name
    _BY_NAME[_TABLE[_c][0].lower()] = _c


def known_codes() -> list[int]:
    return sorted(_TABLE)


def code_of(code_or_message: int | str) -> int | None:
    """The error code in 800, "800", "error 800 (non-real result)" or "TI-Nspire error: error 910 ...";
    failing that, a code whose TI name the message contains ("invalid outside function or program")."""
    if isinstance(code_or_message, int):
        return code_or_message
    text = code_or_message.strip()
    if text.isdigit():
        return int(text)
    m = _CODE_RE.search(text)
    if m:
        return int(m.group(1))
    low = text.lower()
    best = [n for n in _BY_NAME if len(n) >= 8 and n in low]
    return _BY_NAME[max(best, key=len)] if best else None


def explain(code_or_message: int | str) -> str | None:
    """"Error 800 (non-real result): cause. Fix." for a code or CAS error text; None if unknown."""
    code = code_of(code_or_message)
    entry = _TABLE.get(code) if code is not None else None
    if entry is None:
        return None
    name, cause, fix = entry
    return f"Error {code} ({name}): {cause} {fix}"


def explain_all(messages: Iterable[str]) -> list[str]:
    """One explanation per distinct code among the messages, in order of first appearance."""
    seen: set[int] = set()
    out: list[str] = []
    for msg in messages:
        code = code_of(msg)
        if code is None or code in seen:
            continue
        text = explain(code)
        if text:
            seen.add(code)
            out.append(text)
    return out


def with_explanation(message: str) -> str:
    """message plus its explanation, unless it has none or already has it."""
    text = explain(message)
    return message if text is None or text in message else f"{message} {text}"
