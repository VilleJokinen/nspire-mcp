"""TI-Nspire units and physical constants: the names the LaTeX translator, the Notes
steps' "to" and result printing use, and a reader for unit text such as km/h or m/s^2.

No SymPy and no Windows dependencies, so a Notes step that converts units (and its
tests) can use it without loading the translator.
"""
from __future__ import annotations

import re


class UnitError(ValueError):
    """A unit the reader doesn't know or can't read."""


# Units written \mathrm{...} or \text{...} right after a number, and their TI-Nspire
# names (TI-Nspire guide, Appendix A "Conversion Categories and Units"). Any other name
# in that position is refused, not read as a variable.
# TI-Nspire's unit names ignore case, so many are not what the symbol says: _K is
# Boltzmann's constant (kelvin is _°K), _C the speed of light (coulomb is _coul), _H
# Planck's constant (henry is _henry), _nA Avogadro's number, _Mm a millimeter, _MW, _mW,
# _kN, _MJ, _mT, _MPa unknown. Every entry below was checked on the CAS (selftest.py
# checks a sample), and a prefix TI-Nspire doesn't have stays refused instead of guessed.
UNITS = {
    # length
    "m": "_m", "km": "_km", "dm": "_dm", "cm": "_cm", "mm": "_mm", "µm": "_µm", "nm": "_nm",
    "fm": "_fm", "Å": "_Å", "au": "_au", "ly": "_ltyr", "pc": "_pc",
    # mass
    "kg": "_kg", "g": "_gm", "mg": "_mg", "t": "_mton", "u": "_u",
    # time
    "s": "_s", "ms": "_ms", "µs": "_µs", "ns": "_ns", "min": "_min", "h": "_hr", "hr": "_hr",
    "yr": "_yr",  # no "d": \mathrm{d} is the d of dx
    # area, volume
    "ha": "_ha", "l": "_l", "L": "_l", "ml": "_ml", "mL": "_ml",
    # amount, luminous intensity, angle
    "mol": "_mol", "cd": "_cd", "rad": "_rad",
    # force, energy, power
    "N": "_N", "J": "_J", "kJ": "_kJ", "eV": "_eV", "cal": "_cal", "kcal": "_kcal", "kWh": "_kWh",
    "W": "_W", "kW": "_kW",
    # pressure
    "Pa": "_Pa", "kPa": "_kPa", "bar": "_bar", "mbar": "_mbar", "atm": "_atm", "mmHg": "_mmHg",
    # temperature
    "K": "_°K", "°C": "_°C", "°F": "_°F",
    # frequency
    "Hz": "_Hz", "kHz": "_kHz", "MHz": "_MHz", "GHz": "_GHz",
    # electricity and magnetism
    "A": "_A", "kA": "_kA", "mA": "_mA", "µA": "_µA",
    "V": "_V", "kV": "_kV", "mV": "_mV",
    "Ω": "_Ω", "kΩ": "_kΩ", "MΩ": "_MΩ",
    "C": "_coul", "F": "_F", "µF": "_µF", "nF": "_nF", "pF": "_pF",
    "H": "_henry", "mH": "_mH", "µH": "_µH", "S": "_siemens", "T": "_T", "Wb": "_Wb",
}
# How results show TI-Nspire's units in LaTeX, where the name isn't the symbol
UNIT_TEX = {"_gm": "g", "_hr": "h", "_day": "d", "_mton": "t", "_ltyr": "ly", "_l": "L", "_ml": "mL",
             "_coul": "C", "_henry": "H", "_siemens": "S", "_°K": "K", "_Ang": "Å"}
# Physical constants, used only when asked (constants=True): TI-Nspire's built-in names
# for the symbols as the translator reads them (k_B is kB, \varepsilon_0 is ε0).
CONSTANTS = {
    "g": "_g", "c": "_c", "h": "_h", "kB": "_k", "NA": "_Na", "R": "_Rc", "G": "_Gc",
    "ε0": "_ε0", "μ0": "_μ0", "me": "_Me", "mp": "_Mp", "mn": "_Mn", "qe": "_q",
}
CONSTANT_SPELLING = {"g": "g", "c": "c", "h": "h", "kB": "k_B", "NA": "N_A", "R": "R", "G": "G",
                      "ε0": "\\varepsilon_0", "μ0": "\\mu_0", "me": "m_e", "mp": "m_p", "mn": "m_n", "qe": "q_e"}
CONST_TEX = {"_g": "g", "_c": "c", "_h": "h", "_k": "k_{B}", "_Na": "N_{A}", "_Rc": "R", "_Gc": "G",
              "_ε0": r"\varepsilon_{0}", "_μ0": r"\mu_{0}", "_µ0": r"\mu_{0}", "_Me": "m_{e}", "_Mp": "m_{p}",
              "_Mn": "m_{n}", "_q": "e"}
UNIT_WRAPPERS = r"\\(?:mathrm|text|textrm|textnormal|mbox)\s*\{"


_NUM_END = r"(\d\.?|\{\s*-?\d+(?:[.,]\d+)?\s*\})\s*"  # a number just before a unit
_NOT_PH = r"(?![A-Za-z]|\s*_)"  # \Omega, not \Omega_{9NN} (a placeholder) or \Omega_1 (a variable)
_DEGREE = r"\^\s*\{?\s*\\circ\s*\}?\s*"
DEGREE_TEMP = _DEGREE + r"(?:" + UNIT_WRAPPERS + r"\s*[CF]\s*\}|[CF](?![A-Za-z]))"
_PREFIX_SPELLINGS = [(r"\\mu(?![A-Za-z])\s*", "µ"), ("μ", "µ"), (r"\\Omega(?![A-Za-z])\s*", "Ω"),
                     (r"\\AA(?![A-Za-z])\s*", "Å"), (_DEGREE, "°")]


def unit_spellings(s: str) -> str:
    r"""Write the usual LaTeX spellings of units as \mathrm{...}, so _units sees one form:
    5\,\Omega, 5\,\mathrm{k}\Omega, 3\,\mu\mathrm{A}, 25^\circ\mathrm{C}, 25^{\circ}C."""
    w = r"\\(?:mathrm|text|textrm|textnormal|mbox)\s*\{"
    s = re.sub(_NUM_END + r"\\mu\s*" + w + r"\s*", r"\1\\mathrm{µ", s)           # 3\,\mu\mathrm{A}
    s = re.sub(r"(" + w + r"[^{}]*?)\s*\}\s*\\Omega" + _NOT_PH, r"\1Ω}", s)        # \mathrm{k}\Omega
    s = re.sub(_NUM_END + r"\\mu\s*\\Omega" + _NOT_PH, r"\1\\mathrm{µΩ}", s)
    s = re.sub(_NUM_END + r"\\Omega" + _NOT_PH, r"\1\\mathrm{Ω}", s)               # 5\,\Omega
    s = re.sub(_NUM_END + _DEGREE + r"(?:" + w + r"\s*([CF])\s*\}|([CF])(?![A-Za-z]))",
               lambda m: m.group(1) + r"\mathrm{°" + (m.group(2) or m.group(3)) + "}", s)
    return s


def unit_parts(content: str) -> list[tuple[str, str]]:
    """'km/h^2' -> [(unit, _km), (op, /), (unit, _hr), (pow, 2)]; refuses what isn't in UNITS."""
    c = content
    for a, b in _PREFIX_SPELLINGS:
        c = re.sub(a, b, c)
    c = re.sub(r"\\(?:mathrm|text|textrm|textnormal|mbox)\s*\{([^{}]*)\}", r"\1", c)  # \mathrm{km}/\mathrm{h}
    tok = re.compile(r"\s+|\\(?:cdot|times)(?![A-Za-z])|[/*()·]|\^\s*(?:\{\s*-?\d+\s*\}|-?\d)|[A-Za-zµΩÅ°]+|\S")
    out: list[tuple[str, str]] = []
    last_unit = False
    for m in tok.finditer(c):
        t = m.group()
        if t.isspace():
            continue
        if re.fullmatch(r"[A-Za-zµΩÅ°]+", t):
            if t not in UNITS:
                raise UnitError(
                    f"'{t}' isn't a unit the LaTeX translator knows. It knows SI units (m, s, kg, A, K, mol, cd, "
                    "N, J, W, Pa, Hz, V, Ω, C, F, H, T, Wb, S, eV, °C ...) and the prefixed ones TI-Nspire has "
                    f"({', '.join(sorted(u for u in UNITS if len(u) > 1 and u[1:] in UNITS))}). TI-Nspire has "
                    "no MW, mW, kN, MJ, mT, MPa, µV or µC: write those as a power of ten (5\\cdot 10^{6}\\,\\mathrm{W}). "
                    "Put \\cdot between a number and a variable.")
            if last_unit:
                out.append(("op", "*"))
            out.append(("unit", UNITS[t]))
            last_unit = True
        elif t.startswith("^"):
            if not last_unit:
                raise UnitError(f"Couldn't read the unit {content!r}.")
            out.append(("pow", re.sub(r"[\^{}\s]", "", t)))
        elif t in ("(", ")"):
            if t == "(" and last_unit:
                out.append(("op", "*"))
            out.append(("paren", t))
            last_unit = t == ")"
        elif t in ("/", "*", "·", r"\cdot", r"\times"):
            if not last_unit:
                raise UnitError(f"Couldn't read the unit {content!r}.")
            out.append(("op", "/" if t == "/" else "*"))
            last_unit = False
        else:
            raise UnitError(f"Couldn't read the unit {content!r}; write it in TI-Nspire syntax (_km/_hr).")
    if not out or not last_unit:
        raise UnitError(f"Couldn't read the unit {content!r}.")
    return out


_TI_UNIT_TEXT = re.compile(r"\s*_(?:°[CFK]|[^\W\d_]\w*)(?:\s*(?:\^\s*\(?-?\d+\)?|[*/]\s*_(?:°[CFK]|[^\W\d_]\w*)"
                           r"|[*/]\s*\(|\)))*\s*")


def unit_command(text: str) -> str:
    """A unit to convert to, as LaTeX or plain text (km/h, \\mathrm{m}/\\mathrm{s}^2, k\\Omega)
    or already in TI-Nspire syntax (_km/_hr) -> TI-Nspire syntax, e.g. '_km/_hr'."""
    t = (text or "").strip()
    if not t:
        raise UnitError("The unit to convert to is empty.")
    if "_" in t:
        if not _TI_UNIT_TEXT.fullmatch(t):
            raise UnitError(f"Couldn't read the unit {t!r}: write it as km/h, or in TI-Nspire syntax as _km/_hr.")
        return re.sub(r"\s+", "", t)
    out = []
    for kind, v in unit_parts(t):
        out.append(f"^({v})" if kind == "pow" and v.startswith("-") else ("^" + v if kind == "pow" else v))
    return "".join(out)


UNIT_NAME = re.compile(r"_(?:°[CFK]|[^\W\d_]\w*)")


def encode_unit(m: re.Match) -> str:
    """_µA -> _Z00B5ZA: SymPy's parser and _UNSAFE see only ASCII; _decode_unit undoes it."""
    return "".join(c if c.isascii() else f"Z{ord(c):04X}Z" for c in m.group())


def decode_unit(name: str) -> str:
    return re.sub(r"Z([0-9A-F]{4})Z", lambda m: chr(int(m.group(1), 16)), name)


def unit_latex(name: str) -> str:
    r"""A TI-Nspire unit or constant name -> LaTeX: _m -> \mathrm{m}, _kΩ -> \mathrm{k}\Omega,
    _µA -> \mu\mathrm{A}, _°C -> {}^{\circ}\mathrm{C}, _coul -> \mathrm{C}, _g -> g."""
    if name in CONST_TEX:
        return CONST_TEX[name]
    body = UNIT_TEX.get(name, name[1:])
    if body.startswith("°"):
        return r"{}^{\circ}\mathrm{" + body[1:] + "}"
    pre = ""
    if body[:1] in "µμ" and len(body) > 1:
        pre, body = r"\mu", body[1:]
    tex = ""
    for part in re.split(r"(Ω|Å)", body):
        if part == "Ω":
            tex += r"\Omega"
        elif part == "Å":
            tex += r"\mathring{\mathrm{A}}"
        elif part:
            tex += r"\mathrm{" + part + "}"
    return pre + tex
