import os
import tempfile
import unittest

from python_studio.files import Document, FileError, resolve_path


class FilesTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.dir = self._td.name
        self.addCleanup(self._td.cleanup)

    def p(self, name):
        return os.path.join(self.dir, name)

    def test_relative_and_absolute(self):
        self.assertEqual(resolve_path("a.py", self.dir), self.p("a.py"))
        self.assertEqual(resolve_path(self.p("b.py"), "/somewhere/else"), self.p("b.py"))
        self.assertEqual(resolve_path("sub/../a.py", self.dir), self.p("a.py"))

    def test_spaces_and_specials(self):
        name = "my file; $(echo hi) 'q' \"d\".py"
        path = resolve_path(name, self.dir)
        self.assertEqual(os.path.basename(path), name)
        doc = Document.open(path)
        doc.save("print(1)\n")
        self.assertEqual(open(path, encoding="utf-8").read(), "print(1)\n")

    def test_empty_or_nul(self):
        with self.assertRaises(FileError):
            resolve_path("")
        with self.assertRaises(FileError):
            resolve_path("a\x00b")

    def test_missing_file_is_not_created_on_open(self):
        path = self.p("new.py")
        doc = Document.open(path)
        self.assertFalse(doc.exists)
        self.assertFalse(os.path.exists(path))
        doc.save("x = 1\n")
        self.assertTrue(doc.exists)
        self.assertEqual(open(path).read(), "x = 1\n")

    def test_missing_directory(self):
        with self.assertRaises(FileError):
            Document.open(self.p("nope/x.py"))

    def test_directory_refused(self):
        with self.assertRaises(FileError):
            Document.open(self.dir)

    def test_utf8_and_persian(self):
        path = self.p("u.py")
        text = "print('سلام دنیا ✓')\n"
        open(path, "w", encoding="utf-8").write(text)
        doc = Document.open(path)
        self.assertEqual(doc.text, text)

    def test_invalid_utf8_refused_and_untouched(self):
        path = self.p("bad.py")
        open(path, "wb").write(b"print('\xff')\n")
        with self.assertRaises(FileError):
            Document.open(path)
        self.assertEqual(open(path, "rb").read(), b"print('\xff')\n")

    def test_empty_file(self):
        path = self.p("e.py")
        open(path, "w").close()
        doc = Document.open(path)
        self.assertEqual(doc.text, "")
        doc.save("")
        self.assertEqual(os.path.getsize(path), 0)

    def test_save_edited_contents(self):
        path = self.p("s.py")
        open(path, "w").write("a = 1\n")
        doc = Document.open(path)
        doc.save("a = 2\nb = 3\n")
        self.assertEqual(open(path).read(), "a = 2\nb = 3\n")
        self.assertEqual(doc.text, "a = 2\nb = 3\n")

    def test_bom_preserved(self):
        path = self.p("bom.py")
        open(path, "wb").write(b"\xef\xbb\xbfprint(1)\n")
        doc = Document.open(path)
        self.assertTrue(doc.bom)
        self.assertEqual(doc.text, "print(1)\n")
        doc.save("print(2)\n")
        self.assertEqual(open(path, "rb").read(), b"\xef\xbb\xbfprint(2)\n")

    def test_crlf_preserved(self):
        path = self.p("crlf.py")
        open(path, "wb").write(b"a = 1\r\nb = 2\r\n")
        doc = Document.open(path)
        self.assertTrue(doc.crlf)
        self.assertEqual(doc.text, "a = 1\nb = 2\n")
        doc.save("a = 1\nb = 3\n")
        self.assertEqual(open(path, "rb").read(), b"a = 1\r\nb = 3\r\n")

    def test_mixed_and_bare_cr_refused(self):
        for name, data in (("m.py", b"a\r\nb\n"), ("cr.py", b"a\rb\r")):
            path = self.p(name)
            open(path, "wb").write(data)
            with self.assertRaises(FileError):
                Document.open(path)
            self.assertEqual(open(path, "rb").read(), data)

    def test_save_failure_preserves_original(self):
        path = self.p("keep.py")
        open(path, "w").write("orig\n")
        doc = Document.open(path)
        os.chmod(self.dir, 0o500)
        try:
            if os.access(self.dir, os.W_OK):
                self.skipTest("running with privileges that ignore directory permissions")
            with self.assertRaises(FileError):
                doc.save("new\n")
        finally:
            os.chmod(self.dir, 0o700)
        self.assertEqual(open(path).read(), "orig\n")
        self.assertEqual(doc.text, "orig\n")  # in-memory doc not marked as saved

    def test_no_temp_files_left(self):
        path = self.p("t.py")
        open(path, "w").write("x\n")
        Document.open(path).save("y\n")
        self.assertEqual(sorted(os.listdir(self.dir)), ["t.py"])

    def test_permissions_kept(self):
        path = self.p("x.py")
        open(path, "w").write("x\n")
        os.chmod(path, 0o750)
        Document.open(path).save("y\n")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o750)


    def test_save_survives_filesystem_that_refuses_chmod(self):
        # Android shared storage (/sdcard, FUSE) answers chmod with EPERM
        path = self.p("shared.py")
        open(path, "w").write("old\n")
        doc = Document.open(path)
        real = os.chmod

        def refuse(*a, **k):
            raise PermissionError(1, "Operation not permitted")

        os.chmod = refuse
        try:
            doc.save("new\n")
        finally:
            os.chmod = real
        self.assertEqual(open(path).read(), "new\n")
        self.assertEqual(sorted(os.listdir(self.dir)), ["shared.py"])    # no temp file left behind


if __name__ == "__main__":
    unittest.main()
