import sys
import unittest

from python_studio.lint import lint_python


def names(src):
    return [m["message"] for m in lint_python(src)]


class LintTest(unittest.TestCase):
    def test_still_reports_real_problems(self):
        self.assertEqual(names("primt('x')\n"), ["undefined name 'primt'"])
        self.assertTrue(names("def f(:\n")[0])
        self.assertEqual(names("x = 1\nprint(x)\n"), [])

    @unittest.skipUnless(sys.version_info >= (3, 10), "match needs 3.10+")
    def test_match_bindings_are_not_undefined(self):
        src = "p = (1, 2)\nmatch p:\n    case (a, b):\n        print(a, b)\n    case [x, *rest]:\n        print(x, rest)\n    case {'k': v, **others}:\n        print(v, others)\n"
        self.assertEqual(names(src), [])

    @unittest.skipUnless(sys.version_info >= (3, 12), "PEP 695 needs 3.12+")
    def test_type_parameters_are_not_undefined(self):
        self.assertEqual(names("def f[T](x: T) -> T:\n    return x\n"), [])

    def test_pathological_nesting_gives_one_warning_not_an_exception(self):
        out = lint_python("x = " + "+".join(["1"] * 50000) + "\n")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], 4)
        self.assertIn("too deeply nested", out[0]["message"])


if __name__ == "__main__":
    unittest.main()
