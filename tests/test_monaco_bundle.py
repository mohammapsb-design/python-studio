import base64
import os
import tempfile
import unittest
import zlib

from python_studio import monaco_bundle as mb
from python_studio.bridge import Controller
from python_studio.files import Document
from python_studio.monaco_assets import AssetError


def put(root, rel, data):
    p = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as fh:
        fh.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def populate(root, big=0):
    put(root, "vs/loader.js", "L;")
    put(root, "vs/editor/editor.main.js", b"M;" + b"\xc3\xa9" * big)
    put(root, "vs/basic-languages/python/python.js", "P;")
    put(root, "vs/editor/editor.main.css",
        "a{src:url(../base/browser/ui/codicons/codicon/codicon.ttf?v=1) format('truetype')}")
    put(root, "vs/base/browser/ui/codicons/codicon/codicon.ttf", b"\x00FONT\xff")


class BundleTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = os.path.join(self._td.name, "m")

    def test_files_in_order_loader_first_optional_nls_skipped(self):
        populate(self.root)
        b = mb.AssetBundle.build(self.root)
        self.assertEqual([f.name for f in b.files],
                         ["vs/loader.js", "vs/editor/editor.main.js", "vs/basic-languages/python/python.js",
                          "vs/editor/editor.main.css"])
        put(self.root, "vs/editor/editor.main.nls.js", "N;")
        names = [f.name for f in mb.AssetBundle.build(self.root).files]
        self.assertEqual(names[:3], ["vs/loader.js", "vs/editor/editor.main.nls.js", "vs/editor/editor.main.js"])

    def test_chunks_round_trip_exactly_and_manifest_matches(self):
        populate(self.root, big=mb.CHUNK_RAW)          # > 2 chunks for the main file
        b = mb.AssetBundle.build(self.root)
        man = b.manifest()
        parts = {i: {} for i in range(len(man))}
        for fi, ci, b64 in b.chunks():
            self.assertLessEqual(len(b64), 4 * mb.CHUNK_RAW // 3)
            b64.encode("ascii")
            parts[fi][ci] = b64
        for i, m in enumerate(man):
            self.assertEqual(sorted(parts[i]), list(range(m["chunks"])))
            data = b"".join(base64.b64decode(parts[i][c]) for c in range(m["chunks"]))
            self.assertEqual(data, b.files[i].data)
            self.assertEqual((m["size"], m["adler"]), (len(data), zlib.adler32(data) & 0xFFFFFFFF))
        self.assertGreater(man[1]["chunks"], 2)

    def test_css_font_inlined_as_data_uri(self):
        populate(self.root)
        css = mb.AssetBundle.build(self.root).files[-1].data.decode()
        self.assertIn("data:font/ttf;base64," + base64.b64encode(b"\x00FONT\xff").decode(), css)
        self.assertNotIn("codicon.ttf", css)

    def test_css_urls_outside_root_or_remote_are_not_inlined(self):
        populate(self.root)
        with open(os.path.join(self._td.name, "secret.ttf"), "wb") as fh:
            fh.write(b"SECRET")
        put(self.root, "vs/editor/editor.main.css",
            "a{src:url(../../../secret.ttf)}b{src:url(http://x/y.ttf)}c{src:url(../../../../etc/passwd.ttf)}")
        css = mb.AssetBundle.build(self.root).files[-1].data.decode()
        self.assertNotIn("base64", css)
        self.assertIn("http://x/y.ttf", css)           # left untouched, and never fetched by us

    def test_missing_required_file_is_a_clear_error(self):
        populate(self.root)
        os.unlink(os.path.join(self.root, "vs/basic-languages/python/python.js"))
        with self.assertRaises(AssetError) as cm:
            mb.AssetBundle.build(self.root)
        self.assertIn("python.js", str(cm.exception))
        self.assertIn("--fetch-monaco", str(cm.exception))

    def test_size_cap(self):
        populate(self.root)
        old = mb.MAX_BUNDLE_BYTES
        mb.MAX_BUNDLE_BYTES = 5
        self.addCleanup(setattr, mb, "MAX_BUNDLE_BYTES", old)
        with self.assertRaises(AssetError):
            mb.AssetBundle.build(self.root)

    def test_symlink_escape_not_followed(self):
        populate(self.root)
        outside = os.path.join(self._td.name, "evil.js")
        with open(outside, "w") as fh:
            fh.write("EVIL")
        os.unlink(os.path.join(self.root, "vs/loader.js"))
        os.symlink(outside, os.path.join(self.root, "vs/loader.js"))
        with self.assertRaises(AssetError):
            mb.AssetBundle.build(self.root)


class ControllerPushTest(unittest.TestCase):
    def test_assets_precede_document_then_notice_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            root = os.path.join(d, "m")
            populate(root)
            p = os.path.join(d, "x.py")
            with open(p, "w") as fh:
                fh.write("print(1)\n")
            sent = []
            ctl = Controller(Document.open(p), eval_js=sent.append, finish=lambda: None, notice="hi",
                             assets=mb.AssetBundle.build(root))
            ctl.handle('PSBRIDGE:{"op":"ready"}')
            ctl.shutdown()
        kinds = [c.split("(")[0] for c in sent]
        self.assertEqual(kinds[0], "PS.assetsBegin")
        self.assertEqual(kinds[-3:], ["PS.assetsEnd", "PS.load", "PS.notice"])
        self.assertEqual(set(kinds[1:-3]), {"PS.assetChunk"})
        for c in sent:
            c.encode("ascii")

    def test_no_assets_means_only_load(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "x.py")
            with open(p, "w") as fh:
                fh.write("")
            sent = []
            ctl = Controller(Document.open(p), eval_js=sent.append, finish=lambda: None)
            ctl.handle('PSBRIDGE:{"op":"ready"}')
            ctl.shutdown()
        self.assertEqual([c.split("(")[0] for c in sent], ["PS.load"])


if __name__ == "__main__":
    unittest.main()
