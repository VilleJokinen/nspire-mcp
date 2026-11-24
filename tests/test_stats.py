"""Offline tests of nspire_data.py (reading CSV text and columns) and nspire_stats.py's pure
parts: choosing columns, the commands sent, and reading stat.results. No bridge and no pywin32,
so these run on every OS. The tool itself is tested in test_server.py.
"""
from __future__ import annotations

import pytest

import nspire_data as dt
import nspire_stats as ns

# stat.results as the real CAS sent them (raw private-use characters), from OneVar {1,2,3,4,10}
ONEVAR_RAW = ('[["Title","One-Variable Statistics"]["",4.]["Σx",20.]["Σx²",130.]'
              '["sx := s₋₁x",3.5355339059327]["σx := σx",3.1622776601684]["n",5.]["MinX",1.]'
              '["Q₁X",1.5]["MedianX",3.]["Q₃X",7.]["MaxX",10.]["SSX := Σ(x-)²",50.]]')
# ... and from QuadReg {1,2,3,4},{1,4,9,17}
QUAD_RAW = ('[["Title","Quadratic Regression"]["RegEqn","a*x^2+b*x+c"]["a",1.25]["b",−0.95]["c",0.75]'
            '["R²",0.99965928449744]["Resid","{...}"]]')


# ------------------------------------------------------------ nspire_data

def test_csv_with_header_and_separators():
    t = dt.parse_table("time,dist\n1,2.5\n2,4\n3,-1e-7\n")
    assert t.headers == ["time", "dist"] and t.rows == 3
    assert t.columns == [["1", "2", "3"], ["2.5", "4", "-1ᴇ-7"]]
    assert t.values[1] == [2.5, 4.0, -1e-7]
    t = dt.parse_table("x;y\n1,5;2,25\n3;4\n")  # semicolons: decimal commas
    assert t.columns == [["1.5", "3"], ["2.25", "4"]]
    t = dt.parse_table("1\t2\n3\t4")
    assert t.headers is None and t.columns == [["1", "3"], ["2", "4"]]
    t = dt.parse_table("1 2\n3   4\n\n")
    assert t.columns == [["1", "3"], ["2", "4"]]
    t = dt.parse_table("1;2;\n3;4;\n")  # trailing separator
    assert t.columns == [["1", "3"], ["2", "4"]]
    t = dt.parse_table('"a b",c\n1,2')
    assert t.headers == ["a b", "c"]


def test_numbers_are_sent_as_written():
    lit = [dt.number_literal(c, "")[0] for c in ("0.10", "+3", "−2", ".5", "5.", "1E20", "2.5e-9", "0", "-0.0",
                                                  12, 0.1, "1ᴇ3")]
    assert lit == ["0.10", "3", "-2", "0.5", "5", "1ᴇ20", "2.5ᴇ-9", "0", "0", "12", "0.1", "1000"]


@pytest.mark.parametrize("cell", ["abc", "1/2", "π", "", "1e400", "nan", "inf", "1,5", "2x", True, None, [1]])
def test_non_numbers_refused(cell):
    with pytest.raises(ValueError):
        dt.number_literal(cell, "here")


def test_table_errors_name_the_place():
    with pytest.raises(ValueError, match="line 3: 1 values"):
        dt.parse_table("x,y\n1,2\n3\n")
    with pytest.raises(ValueError, match=r"line 2, column 2: 'a' is not a number"):
        dt.parse_table("1,2\n3,a\n")
    with pytest.raises(ValueError, match="line 2: 2 values"):
        dt.parse_table("x\n1,5\n")  # commas separate columns unless there are ; or tabs
    with pytest.raises(ValueError, match="decimal comma"):
        dt.parse_columns([["1,5"]])
    with pytest.raises(ValueError, match="column 2 has 1 values"):
        dt.parse_table(columns=[[1, 2], [3]])
    with pytest.raises(ValueError, match="not both"):
        dt.parse_table("1", [[1]])
    with pytest.raises(ValueError, match="not both"):
        dt.parse_table()
    with pytest.raises(ValueError, match="No data rows"):
        dt.parse_table("x,y\n")
    with pytest.raises(ValueError, match="At most 1000 rows"):
        dt.parse_table("\n".join(["1"] * 1001))
    with pytest.raises(ValueError, match="At most 26 columns"):
        dt.parse_table(columns=[[1]] * 27)
    with pytest.raises(ValueError, match="one name per column"):
        dt.parse_table(columns=[[1], [2]], headers=["a"])
    with pytest.raises(ValueError, match="list of columns"):
        dt.parse_table(columns=[1, 2])


def test_columns_and_headers():
    t = dt.parse_table(columns=[[1, 2.5, "3"], ["-1", 0, 1e-12]], headers=["x", " y "])
    assert t.headers == ["x", "y"] and t.columns == [["1", "2.5", "3"], ["-1", "0", "1ᴇ-12"]]
    t = dt.parse_table("1,2\n3,4", headers=["a", "b"])
    assert t.headers == ["a", "b"]
    assert dt.list_literal(["1", "-2.5"]) == "{1,-2.5}"
    assert dt.list_chunks(["1"] * 450, 200) == [["1"] * 200, ["1"] * 200, ["1"] * 50]


# ------------------------------------------------------------ choosing columns and commands

def table(text: str) -> dt.Table:
    return dt.parse_table(text)


def test_prepare_picks_columns():
    t = table("a,b,c\n1,2,3\n4,5,6\n")
    s = ns.prepare("linregmx", t)
    assert (s.command, s.x, s.y, s.freq) == ("LinRegMx", 0, 1, None)
    assert s.notes and "c" in s.notes[0]
    s = ns.prepare("QuadReg", t, x_column="c", y_column=1, freq_column="2")
    assert (s.x, s.y, s.freq) == (2, 0, 1) and s.notes == []
    assert s.nspire == "QuadReg mcpstatx,mcpstaty,mcpstatf"
    s = ns.prepare("OneVar", t, x_column=2)
    assert (s.x, s.y) == (1, None) and s.nspire == "OneVar mcpstatx"


def test_prepare_refusals():
    t = table("a,b\n1,2\n3,4\n")
    with pytest.raises(ValueError, match="unknown statistics command"):
        ns.prepare("LinReg", t)
    with pytest.raises(ValueError, match="needs two columns"):
        ns.prepare("LinRegMx", table("1\n2\n"))
    with pytest.raises(ValueError, match="same column"):
        ns.prepare("TwoVar", t, x_column=1, y_column="a")
    with pytest.raises(ValueError, match="no column with that header"):
        ns.prepare("TwoVar", t, y_column="z")
    with pytest.raises(ValueError, match="the data has 2 column"):
        ns.prepare("TwoVar", t, y_column=3)
    with pytest.raises(ValueError, match="frequency"):
        ns.prepare("SinReg", table("1,2,3\n4,5,6"), freq_column=3)
    with pytest.raises(ValueError, match="different column"):
        ns.prepare("OneVar", t, freq_column=1)
    with pytest.raises(ValueError, match="plot needs"):
        ns.prepare("OneVar", t, plot=True)
    with pytest.raises(ValueError, match="OneVar uses one column"):
        ns.prepare("OneVar", t, y_column=2)


def test_commands_store_then_read():
    t = dt.parse_table(columns=[[1, 2, 3], [2, 4, 7], [1, 1, 2]])
    stores, stats = ns.commands(ns.prepare("LinRegBx", t, freq_column=3))
    assert stores == ["mcpstatx:={1,2,3}", "mcpstaty:={2,4,7}", "mcpstatf:={1,1,2}"]
    assert stats == ["LinRegBx mcpstatx,mcpstaty,mcpstatf", "stat.results", "stat.RegEqn(mcpstatv)"]
    stores, stats = ns.commands(ns.prepare("OneVar", dt.parse_table(columns=[list(range(450))])))
    assert stores[0].startswith("mcpstatx:={0,1,") and stores[1].startswith("mcpstatx:=augment(mcpstatx,{200,")
    assert len(stores) == 3 and stats == ["OneVar mcpstatx", "stat.results"]


def test_server_lists_the_same_commands():
    assert set(ns.COMMANDS) == set(ns.DESCRIPTIONS)


# ------------------------------------------------------------ reading the results

def test_parse_onevar_results():
    title, rows = ns.parse_results(ONEVAR_RAW)
    assert title == "One-Variable Statistics"
    d = dict(rows)
    assert d["x̄"] == "4." and d["sx := sₙ₋₁x"] == "3.5355339059327" and d["σx := σₙx"] == "3.1622776601684"
    assert d["SSX := Σ(x-x̄)²"] == "50." and d["n"] == "5." and len(rows) == 12


def test_parse_results_quotes_and_errors():
    title, rows = ns.parse_results('[["Title","Odd, \"\"quoted\"\" [x]"]["a",{1,2}]]')
    assert title == 'Odd, "quoted" [x]' and rows == [("a", "{1,2}")]
    for bad in ("5", "[[1,2,3]]", '[["a",1]', '[["a]]'):
        with pytest.raises(ValueError):
            ns.parse_results(bad)


def test_regression_result():
    s = ns.prepare("QuadReg", dt.parse_table("x,y\n1,1\n2,4\n3,9\n4,17\n"))
    out = ns.result(s, [{"ok": True, "result": ""}, {"ok": True, "result": QUAD_RAW},
                        {"ok": True, "result": "1.25*mcpstatv^2-0.95*mcpstatv+0.75"}])
    assert out["equation"] == "1.25*x^2-0.95*x+0.75" and out["equation_template"] == "a*x^2+b*x+c"
    assert out["r2"] == "0.99965928449744" and out["r"] is None
    assert out["columns"] == ["x", "y"] and out["n"] == 4 and out["title"] == "Quadratic Regression"
    assert [r["name"] for r in out["results"]] == ["a", "b", "c", "R²", "Resid"]
    assert out["results"][1]["value"] == "−0.95"
    assert any("{...}" in n for n in out["notes"]) and out["warnings"] == []
    text = ns.summary(out)
    assert "Fitted equation: y = 1.25*x^2-0.95*x+0.75" in text and "R² = 0.99965928449744" in text

    out = ns.result(s, [{"ok": True, "result": ""}, {"ok": True, "result": QUAD_RAW},
                        {"ok": False, "error": "error 910 (syntax error)"}])
    assert out["equation"] is None and "couldn't be read" in out["warnings"][0]


def test_onevar_result_has_no_fit():
    s = ns.prepare("OneVar", dt.parse_table("1\n2\n3\n4\n10"))
    out = ns.result(s, [{"ok": True, "result": ""}, {"ok": True, "result": ONEVAR_RAW}])
    assert "equation" not in out and "r2" not in out and out["columns"] == ["column 1"]
    assert out["frequencies"] is None and out["notes"] == []


def test_plot_range():
    assert ns.plot_range([1, 2, 3]) == (0.9, 3.1)
    assert ns.plot_range([0.01, 10]) == (0.005, 10.4995)   # kept positive for PowerReg/LnReg
    assert ns.plot_range([-2, 2]) == (-2.2, 2.2)
    assert ns.plot_range([5, 5]) == (4.75, 5.25)
    assert ns.plot_range([0, 0]) == (-0.05, 0.05)
