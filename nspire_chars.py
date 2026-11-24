"""TI-Nspire's private-use characters and their readable look-alikes.

The CAS writes some symbols as private-use code points, which render as
nothing almost everywhere. Results are converted to readable look-alikes,
and those look-alikes are converted back on input.

No Windows dependencies, so the LaTeX translator can be imported (and tested)
on any platform.
"""
from __future__ import annotations

NS_E = ""    # the constant e
NS_I = ""    # the imaginary unit i
NS_EXP = ""  # the exponent mark in 1.5E-3
NS_C = ""    # prefix of deSolve's arbitrary constants c1, c2, ...
NS_N = ""    # prefix of arbitrary integers n1, n2, ...

_TO_READABLE = str.maketrans({NS_E: "ℯ", NS_I: "𝑖", NS_EXP: "ᴇ", NS_C: "c", NS_N: "n"})
# ► (convert units) is written @> in what the bridge evaluates: the character itself is a syntax error there
_FROM_READABLE = str.maketrans({"ℯ": NS_E, "𝑖": NS_I, "ᴇ": NS_EXP, "►": "@>", "▸": "@>"})


def readable(text: str) -> str:
    """CAS output -> text a person (or model) can read. ℯ, 𝑖 and ᴇ round-trip as input."""
    return text.translate(_TO_READABLE)


def to_cas_chars(text: str) -> str:
    return text.translate(_FROM_READABLE)
