"""Tables of numbers from CSV text or lists, checked offline and written as TI-Nspire lists.

Used by nspire_stats (and meant for anything else that sends data to TI-Nspire, such as a
Lists & Spreadsheet page). Standard library only. Python only reads the numbers: each value
reaches the CAS as written (normalised to plain decimal syntax), and nothing is computed here.

parse_table() reads CSV text (delimiter: tab, semicolon, comma or spaces; a header row is
recognised when a cell isn't a number) or a list of columns, and returns a Table whose cells
are TI-Nspire number literals. list_literal() writes a column as {1,2.5,−3}.
"""
from __future__ import annotations

import csv
import io
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

MAX_ROWS = 1000
MAX_COLS = 26             # spreadsheet columns A..Z
MAX_CELL_CHARS = 40       # one number as typed
MAX_HEADER_CHARS = 40
EXP_LIMIT = 99            # |exponent| TI-Nspire's decimals allow (1ᴇ99 is about its largest)
_NUM_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?")
_COMMA_DECIMAL_RE = re.compile(r"[+-]?\d*,\d+(?:e[+-]?\d+)?")


@dataclass
class Table:
    headers: list[str] | None   # one per column, or None when the data had no header row
    columns: list[list[str]]    # TI-Nspire number literals, column by column, all the same length
    values: list[list[float]]   # the same numbers as floats (only for drawing data points)

    @property
    def rows(self) -> int:
        return len(self.columns[0]) if self.columns else 0

    def header(self, k: int, default: str) -> str:
        return self.headers[k] if self.headers and self.headers[k] else default


def _clean(cell: str) -> str:
    return (cell.strip().replace("−", "-").replace(" ", "").replace(" ", "")
            .replace("ᴇ", "e").replace("E", "e"))


def number_literal(cell, where: str, decimal_comma: bool = False) -> tuple[str, float]:
    """A cell (str, int or float) as (TI-Nspire literal, float). Raises ValueError naming where.

    Decimal comma (1,5) only when decimal_comma. Writes the number as given, without rounding:
    plain decimals for ordinary sizes, mantissa ᴇ exponent for very large or small ones.
    """
    if isinstance(cell, bool) or cell is None:
        raise ValueError(f"{where}: {cell!r} is not a number")
    if isinstance(cell, (int, float)):
        if not math.isfinite(cell):
            raise ValueError(f"{where}: {cell!r} is not a finite number")
        text = repr(cell)
    elif isinstance(cell, str):
        text = cell
    else:
        raise ValueError(f"{where}: {cell!r} is not a number")
    s = _clean(text)
    if decimal_comma and _COMMA_DECIMAL_RE.fullmatch(s):
        s = s.replace(",", ".")
    if not s:
        raise ValueError(f"{where}: empty cell (every row needs a value in every column)")
    if len(s) > MAX_CELL_CHARS or not _NUM_RE.fullmatch(s.lower()):
        shown = text.strip()
        hint = " (decimal comma: separate the columns with ; or tabs)" if "," in shown else ""
        raise ValueError(f"{where}: {shown[:40]!r} is not a number{hint}")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"{where}: {text.strip()[:40]!r} is not a number") from None
    if d.is_zero():
        return "0", 0.0
    exp = d.adjusted()
    if abs(exp) > EXP_LIMIT:
        raise ValueError(f"{where}: {text.strip()[:40]!r} is too large or too small for TI-Nspire")
    if -6 <= exp <= 15:
        lit = format(d, "f")
    else:
        sign, digits, _ = d.as_tuple()
        mant = str(digits[0]) + ("." + "".join(map(str, digits[1:])) if len(digits) > 1 else "")
        lit = ("-" if sign else "") + f"{mant}ᴇ{exp}"
    if lit.startswith("+"):
        lit = lit[1:]
    return lit, float(d)


def _is_number(cell: str, decimal_comma: bool) -> bool:
    try:
        number_literal(cell, "", decimal_comma)
    except ValueError:
        return False
    return True


def _delimiter(lines: list[str]) -> str | None:
    """tab, ';', ',' or None (runs of spaces): the first of them found on any line."""
    for d in ("\t", ";", ","):
        if any(d in ln for ln in lines):
            return d
    return None


def _check_size(n_rows: int, n_cols: int) -> None:
    if n_cols > MAX_COLS:
        raise ValueError(f"At most {MAX_COLS} columns.")
    if n_rows > MAX_ROWS:
        raise ValueError(f"At most {MAX_ROWS} rows of data (got {n_rows}).")
    if n_rows == 0:
        raise ValueError("No data rows.")


def _check_headers(headers: list[str]) -> list[str]:
    out = []
    for h in headers:
        if not isinstance(h, str):
            raise ValueError("headers must be strings")
        h = h.strip()
        if "\n" in h or "\r" in h or len(h) > MAX_HEADER_CHARS:
            raise ValueError(f"header {h[:40]!r}: one line of at most {MAX_HEADER_CHARS} characters")
        out.append(h)
    return out


def parse_csv(text: str, header: bool | None = None) -> Table:
    """Read CSV text. header: True/False, or None to detect it (first row has a non-number)."""
    if not isinstance(text, str):
        raise ValueError("csv must be text")
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if ln.strip()]
    if not lines:
        raise ValueError("csv is empty")
    if len(lines) > MAX_ROWS + 1:
        raise ValueError(f"At most {MAX_ROWS} rows of data (got {len(lines)} lines).")
    delim = _delimiter(lines)
    if delim is None:
        rows = [ln.split() for ln in lines]
    else:
        rows = [[c for c in r] for r in csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)]
        # a trailing delimiter (1;2;) leaves an empty last cell in every row: drop it
        while rows and all(r and not r[-1].strip() for r in rows):
            rows = [r[:-1] for r in rows]
    decimal_comma = delim != ","
    if header is None:
        header = any(c.strip() and not _is_number(c, decimal_comma) for c in rows[0])
    headers = _check_headers(rows[0]) if header else None
    data = rows[1:] if header else rows
    width = len(rows[0])
    _check_size(len(data), width)
    for i, r in enumerate(data, 2 if header else 1):
        if len(r) != width:
            raise ValueError(f"line {i}: {len(r)} values, but the {'header' if header else 'first line'} "
                             f"has {width} (every row needs one value per column)")
    columns: list[list[str]] = [[] for _ in range(width)]
    values: list[list[float]] = [[] for _ in range(width)]
    for i, r in enumerate(data, 2 if header else 1):
        for k, cell in enumerate(r):
            lit, v = number_literal(cell, f"line {i}, column {k + 1}", decimal_comma)
            columns[k].append(lit)
            values[k].append(v)
    return Table(headers, columns, values)


def parse_columns(cols, headers: list[str] | None = None) -> Table:
    """Read a list of columns, each a list of numbers (or numeric strings, decimal point)."""
    if not isinstance(cols, list) or not cols or not all(isinstance(c, list) for c in cols):
        raise ValueError("columns must be a list of columns, each a list of numbers, e.g. [[1,2,3],[2,4,7]]")
    n = len(cols[0])
    _check_size(n, len(cols))
    for k, c in enumerate(cols):
        if len(c) != n:
            raise ValueError(f"column {k + 1} has {len(c)} values, column 1 has {n}: the columns must be the "
                             f"same length")
    if headers is not None:
        if not isinstance(headers, list) or len(headers) != len(cols):
            raise ValueError("headers must be a list with one name per column")
        headers = _check_headers(headers)
    columns: list[list[str]] = []
    values: list[list[float]] = []
    for k, c in enumerate(cols):
        lits = [number_literal(cell, f"column {k + 1}, value {i + 1}") for i, cell in enumerate(c)]
        columns.append([lit for lit, _ in lits])
        values.append([v for _, v in lits])
    return Table(headers, columns, values)


def parse_table(csv_text: str | None = None, columns=None, headers: list[str] | None = None,
                header: bool | None = None) -> Table:
    """CSV text or a list of columns (exactly one of them) as a Table. Raises ValueError.

    headers: names for the columns (with columns; with csv they replace the header row's).
    header: whether the CSV's first row is a header (None: detect).
    """
    if (csv_text is None) == (columns is None):
        raise ValueError("Give the data either as csv (text) or as columns (lists of numbers), not both.")
    if csv_text is not None:
        t = parse_csv(csv_text, header)
        if headers is not None:
            if not isinstance(headers, list) or len(headers) != len(t.columns):
                raise ValueError("headers must be a list with one name per column")
            t.headers = _check_headers(headers)
        return t
    return parse_columns(columns, headers)


def list_literal(column: list[str]) -> str:
    """{1,2.5,-3}: a column of literals as a TI-Nspire list."""
    return "{" + ",".join(column) + "}"


def list_chunks(column: list[str], size: int = 200) -> list[list[str]]:
    """The column in pieces of at most size values (one request each when storing a long list)."""
    return [column[i:i + size] for i in range(0, len(column), size)] or [[]]
