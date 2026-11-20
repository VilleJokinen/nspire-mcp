"""Offline tests for the Notes-page safety fixes (window choice, name scanning, bridge clean-up).

No TI-Nspire, clipboard or window is touched: windows and the bridge are faked.
Run from the repo root:  uv run --with pywin32 python -m unittest discover -s tests -v
(pywin32 is only needed because nspire_keys / nspire_bridge import it.)
"""
import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import nspire_keys as keys  # noqa: E402
import nspire_notes as notes  # noqa: E402
from nspire_keys import WinInfo  # noqa: E402

NSPIRE_EXE = r"C:\Program Files\TI Education\TI-Nspire CX CAS Student Software\TI-Nspire CX CAS Student Software.exe"
TEACHER_EXE = r"C:\Program Files\TI Education\TI-Nspire CX CAS Teacher Software\TI-Nspire CX CAS Teacher Software.exe"
CHROME_EXE = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CODE_EXE = r"C:\Users\Ville\AppData\Local\Programs\Microsoft VS Code\Code.exe"


def win(hwnd, title, cls, pid, image):
    return WinInfo(hwnd, title, cls, pid, image)


class IsNspireImage(unittest.TestCase):
    def test_accepts_student_and_teacher(self):
        self.assertTrue(keys.is_nspire_image(NSPIRE_EXE))
        self.assertTrue(keys.is_nspire_image(TEACHER_EXE))
        self.assertTrue(keys.is_nspire_image(r"D:\apps\ti-nspire cx ii cas student software.exe"))  # custom folder, case
        self.assertTrue(keys.is_nspire_image(r"C:\x\TI-Nspire CX Premium Teacher Software.exe"))

    def test_rejects_others(self):
        for p in (None, "", CHROME_EXE, CODE_EXE, r"C:\x\TI-Nspire Computer Link.exe",
                  r"C:\x\notes about TI-Nspire CX CAS Student Software.exe",
                  r"C:\TI-Nspire CX CAS Student Software.exe\notepad.exe"):
            self.assertFalse(keys.is_nspire_image(p), p)


class PickWindow(unittest.TestCase):
    real = win(300, "nspire_bridge - TI-Nspire™ CX CAS Student Software", "SunAwtFrame", 10, NSPIRE_EXE)
    browser = win(100, "villejokinen/nspire-mcp: TI-Nspire as a math engine - Google Chrome",
                  "Chrome_WidgetWin_1", 20, CHROME_EXE)
    editor = win(200, "README.md - TI-Nspire MCP - Visual Studio Code", "Chrome_WidgetWin_1", 30, CODE_EXE)

    def test_browser_tab_in_front_is_not_chosen(self):
        # EnumWindows lists front to back: the old code took the first title match, the browser.
        chosen, cands = keys.pick_nspire_window([self.browser, self.editor, self.real], foreground=self.browser.hwnd)
        self.assertEqual(chosen, self.real)
        self.assertEqual(cands, [self.real])

    def test_no_nspire_process_is_an_error_naming_the_impostors(self):
        with self.assertRaises(RuntimeError) as cm:
            keys.pick_nspire_window([self.browser, self.editor])
        msg = str(cm.exception)
        self.assertIn("TI-Nspire window not found", msg)
        self.assertIn("chrome.exe", msg)
        self.assertIn("Code.exe", msg)

    def test_no_windows_at_all(self):
        with self.assertRaises(RuntimeError):
            keys.pick_nspire_window([])

    def test_unreadable_image_is_not_trusted(self):
        w = win(5, "TI-Nspire", "SunAwtFrame", 99, None)
        with self.assertRaises(RuntimeError):
            keys.pick_nspire_window([w])

    def test_main_frame_beats_dialog(self):
        dialog = win(150, "TI-Nspire™ CX CAS Student Software", "SunAwtDialog", 10, NSPIRE_EXE)
        chosen, cands = keys.pick_nspire_window([dialog, self.real])
        self.assertEqual(chosen, self.real)
        self.assertEqual(cands, [self.real])

    def test_dialog_only_falls_back_to_titled_window(self):
        dialog = win(150, "TI-Nspire™ CX CAS Student Software", "SunAwtDialog", 10, NSPIRE_EXE)
        untitled_popup = win(151, "menu", "SunAwtWindow", 10, NSPIRE_EXE)
        chosen, _ = keys.pick_nspire_window([untitled_popup, dialog])
        self.assertEqual(chosen, dialog)

    def test_several_windows_foreground_wins_else_lowest_handle(self):
        a = win(500, "a - TI-Nspire™ CX CAS Teacher Software", "SunAwtFrame", 11, TEACHER_EXE)
        b = win(400, "b - TI-Nspire™ CX CAS Student Software", "SunAwtFrame", 12, NSPIRE_EXE)
        self.assertEqual(keys.pick_nspire_window([a, b], foreground=a.hwnd)[0], a)
        self.assertEqual(keys.pick_nspire_window([a, b], foreground=self.browser.hwnd)[0], b)
        self.assertEqual(keys.pick_nspire_window([b, a], foreground=0)[0], b)  # same answer in any list order
        self.assertEqual(keys.pick_nspire_window([a, b], foreground=0)[0], b)


class ScanNames(unittest.TestCase):
    def scan(self, cmd):
        n = notes.scan_names(cmd)
        return list(n.defines), set(n.uses)

    def test_assign(self):
        self.assertEqual(self.scan("a:=3"), (["a"], set()))
        self.assertEqual(self.scan("  b := a+1"), (["b"], {"a"}))
        self.assertEqual(self.scan("a:=a+1"), (["a"], {"a"}))

    def test_function_assign_misses_in_old_regex(self):
        self.assertEqual(self.scan("f(x):=x^2+k"), (["f"], {"k"}))
        self.assertEqual(self.scan("g(x,y):=x*y"), (["g"], set()))

    def test_define(self):
        self.assertEqual(self.scan("Define f(x)=x^2+a"), (["f"], {"a"}))
        self.assertEqual(self.scan("define LibPub h(t)=t"), (["h"], set()))
        self.assertEqual(self.scan("Define c=7"), (["c"], set()))

    def test_store_arrow(self):
        self.assertEqual(self.scan("5\u2192a"), (["a"], set()))
        self.assertEqual(self.scan("x+1=:b"), (["b"], {"x"}))

    def test_greek_and_several(self):
        self.assertEqual(self.scan("\u03b1:=2:\u03b2:=3"), (["\u03b1", "\u03b2"], set()))

    def test_plain_expression_defines_nothing(self):
        self.assertEqual(self.scan("solve(x^2=4,x)"), ([], {"solve", "x"}))
        self.assertEqual(self.scan("a=b"), ([], {"a", "b"}))

    def test_strings_ignored(self):
        self.assertEqual(self.scan('"a:=1"'), ([], set()))

    def test_parameters_are_not_uses(self):
        self.assertEqual(self.scan("Define f(x,y)=x+y+z"), (["f"], {"z"}))


class FakeBridge:
    """Just enough of a CAS: variables, getType, getVarInfo, DelVar, Define, :=, store."""
    BUILTINS = {"solve", "sin", "factor", "expand"}

    def __init__(self, stored=None, can_list=True):
        self.vars = {k: tuple(v) for k, v in (stored or {}).items()}  # lower name -> (type, value)
        self.can_list = can_list
        self.calls = []

    def eval_one(self, expr, timeout_s, raw=False):
        self.calls.append(expr)
        if expr == "getVarInfo()":
            if not self.can_list:
                raise ValueError("TI-Nspire error: error 680")
            if not self.vars:
                return '"NONE"'
            return "[" + "".join(f'[{n},"{t}","",0]' for n, (t, _) in self.vars.items()) + "]"
        m = re.fullmatch(r"getType\((\w+)\)", expr)
        if m:
            n = m.group(1).lower()
            if n in self.BUILTINS:
                raise ValueError("TI-Nspire error: error 680")
            return '"%s"' % self.vars.get(n, ("NONE", None))[0]
        m = re.fullmatch(r"DelVar (\w+)", expr)
        if m:
            self.vars.pop(m.group(1).lower(), None)
            return ""
        m = re.fullmatch(r"Define (\w+)\(.*\)=.*", expr)
        if m:
            self.vars[m.group(1).lower()] = ("FUNC", None)
            return ""
        m = re.fullmatch(r"(\w+)(\([^)]*\))?:=(.*)", expr) or re.fullmatch(r"()()", "")
        if m and m.group(1):
            self.vars[m.group(1).lower()] = ("FUNC" if m.group(2) else "NUM", m.group(3))
            return m.group(3)
        m = re.fullmatch(r"(.*)\u2192(\w+)", expr)
        if m:
            self.vars[m.group(2).lower()] = ("NUM", m.group(1))
            return m.group(1)
        if expr == "boom":
            raise ValueError("TI-Nspire error: error 1")
        if re.fullmatch(r"\w+", expr):
            t, v = self.vars.get(expr.lower(), ("NONE", None))
            if t == "FUNC":
                raise ValueError("TI-Nspire error: error 40")
            return v if t != "NONE" else expr
        return "ok"


def items(*cmds):
    return [{"text": "", "cmd": c, "result": None, "seconds": 0.0} for c in cmds]


class Prepare(unittest.TestCase):
    def check(self, bridge, *cmds):
        its = items(*cmds)
        return its, notes._prepare(bridge, its, 5)

    def test_new_variables_and_functions_are_removed(self):
        b = FakeBridge()
        its, warnings = self.check(b, "a:=3", "f(x):=x^2+a", "Define g(x)=x+1", "7\u2192c", "f(2)")
        self.assertEqual(b.vars, {})
        self.assertEqual(warnings, [])
        self.assertEqual(its[-1]["result"], "ok")

    def test_overwritten_variable_is_restored(self):
        b = FakeBridge({"a": ("NUM", "5")})
        _, warnings = self.check(b, "a:=3", "a:=4")
        self.assertEqual(b.vars, {"a": ("NUM", "5")})
        self.assertEqual(warnings, [])

    def test_overwritten_function_is_reported(self):
        b = FakeBridge({"f": ("FUNC", None)})
        _, warnings = self.check(b, "Define f(x)=x^3")
        self.assertEqual(len(warnings), 1)
        self.assertIn("f (FUNC)", warnings[0])
        self.assertIn("couldn't be restored", warnings[0])

    def test_uses_of_stored_variable_warn(self):
        b = FakeBridge({"k": ("NUM", "9")})
        _, warnings = self.check(b, "solve(x^2=k,x)", "k+1")
        self.assertEqual(len(warnings), 2)
        self.assertIn("Step 1 uses k", warnings[0])
        self.assertIn("Step 2 uses k", warnings[1])
        self.assertEqual(b.vars, {"k": ("NUM", "9")})

    def test_variable_defined_by_an_earlier_step_does_not_warn(self):
        b = FakeBridge({"k": ("NUM", "9")})
        _, warnings = self.check(b, "k:=2", "k+1")
        self.assertEqual(warnings, [])
        self.assertEqual(b.vars, {"k": ("NUM", "9")})

    def test_own_redefinition_with_use_on_the_right_warns(self):
        b = FakeBridge({"a": ("NUM", "5")})
        _, warnings = self.check(b, "a:=a+1")
        self.assertEqual(len(warnings), 1)
        self.assertEqual(b.vars, {"a": ("NUM", "5")})

    def test_builtins_do_not_warn(self):
        b = FakeBridge({"zz": ("NUM", "1")})
        _, warnings = self.check(b, "factor(expand(sin(x)))")
        self.assertEqual(warnings, [])

    def test_failing_step_still_cleans_up_and_names_the_step(self):
        b = FakeBridge({"a": ("NUM", "5")})
        with self.assertRaises(ValueError) as cm:
            self.check(b, "a:=3", "b:=1", "boom")
        self.assertIn("step 3 (boom)", str(cm.exception))
        self.assertEqual(b.vars, {"a": ("NUM", "5")})

    def test_without_getvarinfo_falls_back_to_gettype(self):
        b = FakeBridge({"a": ("NUM", "5"), "k": ("NUM", "9")}, can_list=False)
        _, warnings = self.check(b, "a:=3", "solve(x=k,x)", "c:=1")
        self.assertEqual(b.vars, {"a": ("NUM", "5"), "k": ("NUM", "9")})
        self.assertEqual(len(warnings), 1)
        self.assertIn("Step 2 uses k", warnings[0])

    def test_text_only_steps_never_touch_the_bridge(self):
        b = FakeBridge()
        its = [{"text": "hello", "cmd": None, "result": None, "seconds": 0.0}]
        self.assertEqual(notes._prepare(b, its, 5), [])
        self.assertEqual(b.calls, ["getVarInfo()"])


class ArgumentChecks(unittest.TestCase):
    def test_options(self):
        notes.check_options(None, "computer", 10)
        notes.check_options("Title", "handheld", 2.5)
        for args in ((None, "tablet", 10), ("a\nb", "computer", 10), (5, "computer", 10),
                     (None, "computer", 0), (None, "computer", -1), (None, "computer", True),
                     (None, "computer", "10"), (None, "computer", 1e9)):
            with self.assertRaises(ValueError, msg=str(args)):
                notes.check_options(*args)

    def test_plan_steps_ok(self):
        out = notes.plan_steps([{"text": "Factor:", "math": "factor(x^2-5*x+6)"}, {"text": "done"}])
        self.assertEqual([i["cmd"] for i in out], ["factor(x^2-5*x+6)", None])

    def test_plan_steps_rejects(self):
        for steps in ([], None, "x", [5], [{}], [{"text": ""}], [{"text": "a\nb"}], [{"math": "1+1\n2"}],
                      [{"math": 5}], [{"text": 5}], [{"text": "ok"}, {}]):
            with self.assertRaises(ValueError, msg=repr(steps)):
                notes.plan_steps(steps)

    def test_plan_error_names_the_step(self):
        with self.assertRaises(ValueError) as cm:
            notes.plan_steps([{"text": "ok"}, {"text": "a\nb"}])
        self.assertIn("step 2", str(cm.exception))

    def test_build_notes_validates_before_touching_bridge_or_windows(self):
        class Tripwire:
            def eval_one(self, *a, **k):
                raise AssertionError("bridge used before validation")
        with mock.patch.object(notes, "find_nspire_windows", side_effect=AssertionError("windows searched")):
            with self.assertRaises(ValueError):
                notes.build_notes(Tripwire(), [{"text": "x", "math": "1+1"}], page_size="tablet")
            with self.assertRaises(ValueError):
                notes.build_notes(Tripwire(), [{"math": "1+1\n2"}])

    def test_no_window_means_no_bridge_use(self):
        class Tripwire:
            def eval_one(self, *a, **k):
                raise AssertionError("bridge used although TI-Nspire isn't running")
        with mock.patch.object(notes, "find_nspire_windows", side_effect=RuntimeError("TI-Nspire window not found.")):
            with self.assertRaises(RuntimeError):
                notes.build_notes(Tripwire(), [{"text": "x", "math": "1+1"}])


if __name__ == "__main__":
    unittest.main()
