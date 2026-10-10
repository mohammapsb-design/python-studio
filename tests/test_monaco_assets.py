import base64
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from unittest import mock

from python_studio import monaco_assets as ma

def write(path, data):
    with open(path, "w") as fh:
        fh.write(data)



def make_tgz(extra=None, skip=(), prefix="package/min/"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        def add(name, data=b"//x\n"):
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
        for rel in ma.REQUIRED_FILES:
            if rel not in skip:
                add(prefix + rel)
        add("package/LICENSE", b"MIT\n")
        add("package/esm/vs/editor/editor.main.js")      # not part of the AMD runtime: must be ignored
        add("package/package.json", b"{}")
        for name, data in (extra or []):
            add(name, data)
    return buf.getvalue()


class Resp:
    def __init__(self, data):
        self._b = io.BytesIO(data)
    def read(self, n=-1):
        return self._b.read(n)
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def fake_opener(tgz, integrity=None, shasum=None):
    dist = {"tarball": "https://registry.npmjs.org/monaco-editor/-/monaco-editor-%s.tgz" % ma.MONACO_VERSION}
    if integrity is None and shasum is None:
        integrity = "sha512-" + base64.b64encode(hashlib.sha512(tgz).digest()).decode()
    if integrity:
        dist["integrity"] = integrity
    if shasum:
        dist["shasum"] = shasum
    meta = json.dumps({"dist": dist}).encode()
    def opener(url, timeout=None):
        return Resp(meta if url == ma.REGISTRY_URL else tgz)
    return opener


class FindAssetsTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.root = self._td.name
        patcher = mock.patch.dict(os.environ, {"XDG_DATA_HOME": os.path.join(self.root, "xdg")})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(ma.ENV_VAR, None)

    def populate(self, path):
        for rel in ma.REQUIRED_FILES:
            full = os.path.join(path, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            write(full, "//")

    def test_none_when_absent_and_message_is_actionable(self):
        with mock.patch.object(ma, "bundled_dir", return_value=os.path.join(self.root, "nope")):
            self.assertIsNone(ma.find_assets())
            msg = ma.describe_missing()
        self.assertIn("--fetch-monaco", msg)
        self.assertIn("--basic-editor", msg)

    def test_user_dir_found(self):
        self.populate(ma.user_dir())
        a = ma.find_assets()
        self.assertEqual((a.path, a.origin), (ma.user_dir(), "user"))

    def test_env_override_wins_and_incomplete_is_skipped_with_report(self):
        d = os.path.join(self.root, "custom")
        os.makedirs(os.path.join(d, "vs"))
        os.environ[ma.ENV_VAR] = d
        self.populate(ma.user_dir())
        self.assertEqual(ma.find_assets().origin, "user")      # incomplete env dir is not used...
        self.assertIn("incomplete", ma.describe_missing())     # ...and the report names it
        self.populate(d)
        self.assertEqual(ma.find_assets().origin, "env")


class FetchTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dest = os.path.join(self._td.name, "data", "monaco")

    def test_fetch_installs_only_amd_runtime_and_license(self):
        ma.fetch(self.dest, opener=fake_opener(make_tgz()), log=lambda m: None)
        self.assertEqual(ma.missing_files(self.dest), [])
        self.assertTrue(os.path.isfile(os.path.join(self.dest, "LICENSE")))
        self.assertEqual(open(os.path.join(self.dest, "VERSION")).read().strip(), ma.MONACO_VERSION)
        self.assertFalse(os.path.exists(os.path.join(self.dest, "package.json")))
        self.assertFalse(os.path.exists(os.path.join(self.dest, "esm")))
        self.assertEqual([n for n in os.listdir(os.path.dirname(self.dest)) if n.startswith(".monaco-new")], [])

    def test_integrity_mismatch_rejected_and_nothing_installed(self):
        tgz = make_tgz()
        bad = "sha512-" + base64.b64encode(hashlib.sha512(b"other").digest()).decode()
        with self.assertRaises(ma.AssetError) as cm:
            ma.fetch(self.dest, opener=fake_opener(tgz, integrity=bad), log=lambda m: None)
        self.assertIn("integrity", str(cm.exception))
        self.assertFalse(os.path.exists(self.dest))

    def test_sha1_fallback_and_missing_checksum(self):
        tgz = make_tgz()
        ma.fetch(self.dest, opener=fake_opener(tgz, shasum=hashlib.sha1(tgz).hexdigest(), integrity=""), log=lambda m: None)
        os.rename(self.dest, self.dest + "_x")
        with self.assertRaises(ma.AssetError):
            ma.fetch(self.dest, opener=fake_opener(tgz, integrity="", shasum=""), log=lambda m: None)

    def test_path_traversal_member_rejected(self):
        tgz = make_tgz(extra=[("package/min/vs/../../evil.js", b"x")])
        with self.assertRaises(ma.AssetError):
            ma.fetch(self.dest, opener=fake_opener(tgz), log=lambda m: None)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.dest), "evil.js")))
        self.assertFalse(os.path.exists(self.dest))

    def test_symlink_member_rejected(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ti = tarfile.TarInfo("package/min/vs/link.js")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tar.addfile(ti)
        with self.assertRaises(ma.AssetError):
            ma.extract_tarball(buf.getvalue(), self._td.name)

    def test_incomplete_package_rejected(self):
        tgz = make_tgz(skip=("vs/basic-languages/python/python.js",))
        with self.assertRaises(ma.AssetError) as cm:
            ma.fetch(self.dest, opener=fake_opener(tgz), log=lambda m: None)
        self.assertIn("python.js", str(cm.exception))
        self.assertFalse(os.path.exists(self.dest))

    def test_failed_refetch_keeps_previous_install(self):
        ma.fetch(self.dest, opener=fake_opener(make_tgz()), log=lambda m: None)
        def broken(url, timeout=None):
            raise OSError("network down")
        with self.assertRaises(ma.AssetError):
            ma.fetch(self.dest, opener=broken, log=lambda m: None)
        self.assertEqual(ma.missing_files(self.dest), [])

    def test_network_error_is_asset_error(self):
        def broken(url, timeout=None):
            raise OSError("no route")
        with self.assertRaises(ma.AssetError) as cm:
            ma.fetch(self.dest, opener=broken, log=lambda m: None)
        self.assertIn("no route", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
