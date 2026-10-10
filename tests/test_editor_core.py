"""Runs the real PSCore (highlighter + completion) extracted from the fallback basic_editor.html under node.
(The primary editor is Monaco; see test_editor_page.py.)"""
import json
import os
import shutil
import subprocess
import unittest

HTML = os.path.join(os.path.dirname(__file__), "..", "src", "python_studio", "assets", "basic_editor.html")


def core_js():
    src = open(HTML, encoding="utf-8").read()
    a = src.index("/*CORE-START*/")
    b = src.index("/*CORE-END*/")
    return src[a:b]


def node(expr):
    prog = core_js() + "\nconsole.log(JSON.stringify(" + expr + "));"
    out = subprocess.run(["node", "-e", prog], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class EditorCoreTest(unittest.TestCase):
    def kinds(self, code):
        toks = node("PSCore.tokenize(%s)" % json.dumps(code))
        return [(code[t["a"]:t["b"]], t["t"]) for t in toks]

    def test_page_is_ascii(self):
        open(HTML, encoding="utf-8").read().encode("ascii")

    def test_categories(self):
        got = dict(self.kinds("def greet(name):\n    return len(name) + 42  # hi\n"))
        self.assertEqual(got["def"], "k")
        self.assertEqual(got["greet"], "f")
        self.assertEqual(got["return"], "k")
        self.assertEqual(got["len"], "b")
        self.assertEqual(got["42"], "n")
        self.assertEqual(got["# hi"], "c")
        self.assertEqual(got["name"], "i")
        self.assertEqual(got["+"], "o")

    def test_class_name_and_decorator(self):
        got = dict(self.kinds("@property\nclass Foo:\n    pass\n"))
        self.assertEqual(got["@property"], "d")
        self.assertEqual(got["Foo"], "f")

    def test_strings(self):
        got = self.kinds("a = 'x#y' + \"q\" + f'{z}' + '''t\nu'''\n")
        strings = [t for t, k in got if k == "s"]
        self.assertEqual(strings, ["'x#y'", '"q"', "f'{z}'", "'''t\nu'''"])
        self.assertFalse(any(k == "c" for t, k in got))  # '#' inside a string is not a comment

    def test_unterminated_triple_string_highlights_to_end(self):
        got = self.kinds('x = """abc\ndef')
        self.assertEqual(got[-1], ('"""abc\ndef', "s"))

    def test_html_is_escaped(self):
        html = node("PSCore.highlight(%s)" % json.dumps("a = '<b>' & 1"))
        self.assertNotIn("<b>", html)
        self.assertIn("&lt;b&gt;", html)
        self.assertIn("&amp;", html)

    def test_highlight_roundtrip_text(self):
        code = "for i in range(3):\n    print(i < 2 and 'a&b')\n"
        html = node("PSCore.highlight(%s)" % json.dumps(code))
        import re, html as h
        plain = h.unescape(re.sub(r"<[^>]+>", "", html))
        self.assertEqual(plain, code)

    def test_persian_text_survives(self):
        code = "print('سلام')  # سلام\n"
        html = node("PSCore.highlight(%s)" % json.dumps(code))
        self.assertIn("سلام", html)

    def comp(self, text, pos=None):
        pos = len(text) if pos is None else pos
        return node("PSCore.complete(%s,%d,6)" % (json.dumps(text), pos))

    def test_completes_builtin_prefix(self):
        r = self.comp("pri")
        self.assertEqual(r["items"][0], "print")
        self.assertEqual(r["start"], 0)

    def test_completes_keyword(self):
        self.assertIn("return", self.comp("    ret")["items"])

    def test_completes_names_from_file(self):
        r = self.comp("def calculate_total():\n    pass\ncalc")
        self.assertEqual(r["items"][0], "calculate_total")

    def test_no_completion_for_exact_or_short_prefix(self):
        self.assertIsNone(self.comp("p"))
        self.assertIsNone(self.comp("zzqq"))

    def test_no_completion_in_comment_or_string(self):
        self.assertIsNone(self.comp("# pri"))
        self.assertIsNone(self.comp("x = 'pri"))
        self.assertIsNone(self.comp('x = "pri'))

    def test_completion_after_closed_string(self):
        self.assertIsNotNone(self.comp("x = 'a'; pri"))

    def test_no_completion_after_dot(self):
        self.assertIsNone(self.comp("os.pri"))

    def test_current_word_not_suggested_from_itself(self):
        self.assertIsNone(self.comp("my_unique_name"))

    def test_auto_indent(self):
        self.assertEqual(node('PSCore.indentFor("if x:", 5)'), "    ")
        self.assertEqual(node('PSCore.indentFor("    a = 1", 9)'), "    ")
        self.assertEqual(node('PSCore.indentFor("    if x:  # c", 16)'), "        ")
        self.assertEqual(node('PSCore.indentFor("x = 1", 5)'), "")


if __name__ == "__main__":
    unittest.main()
