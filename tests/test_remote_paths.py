"""Remote filename/download confinement regression tests; no network or writes."""
import ast
from pathlib import Path
import unittest
from unittest import mock

from src.local_paths import validate_remote_filename, confined_download_path

ROOT = Path(__file__).resolve().parents[1]


class RemoteNameTests(unittest.TestCase):
    def test_normal_international_names_are_preserved(self):
        for name in ("file.txt", ".bashrc", "report 2026.txt", "服务器.log", "a..b.json"):
            with self.subTest(name=name):
                self.assertEqual(validate_remote_filename(name), name)

    def test_rejects_windows_escape_and_special_names(self):
        names = ["", ".", "..", "../target", "..\\target", "C:\\target", "C:target",
                 "\\target", "//host/share/file", "\\\\host\\share\\file", "/etc/passwd",
                 "a/b", "a\\b", "name:ads", "file.", "file ", "x\x00y", "x\ny", "x\ty",
                 "<file>", 'a"b', "a|b", "a?b", "a*b", "NUL", "NUL.txt", "con",
                 "COM1.log", "LPT9", "com¹.txt", "CONIN$", "CONOUT$.txt", "CLOCK$"]
        for name in names:
            with self.subTest(name=repr(name)), self.assertRaises(ValueError):
                validate_remote_filename(name)

    def test_non_strings_are_rejected(self):
        for name in (None, 42, b"filename", []):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_remote_filename(name)

    def test_destination_is_direct_child_of_canonical_directory(self):
        # Existing repo directory is read-only test input, not a transfer target.
        target = Path(confined_download_path(str(ROOT), "review-placeholder.log"))
        self.assertEqual(target.parent, ROOT.resolve())
        self.assertEqual(target.name, "review-placeholder.log")
        self.assertFalse(target.exists())

    def test_existing_symlink_escape_is_rejected(self):
        outside = ROOT.parent / "outside-review-placeholder.log"
        with mock.patch.object(Path, "resolve", side_effect=[ROOT.resolve(), outside]):
            with self.assertRaisesRegex(ValueError, "escapes"):
                confined_download_path(str(ROOT), "review-placeholder.log")

    def test_validation_happens_before_temp_directory_or_worker(self):
        path = ROOT / "src/ui/sftp_browser.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SftpBrowserWindow")
        fn = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "_download_and_open")
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
        validation = next(n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id == "validate_remote_filename")
        temporary = next(n.lineno for n in calls if isinstance(n.func, ast.Attribute) and n.func.attr == "mkdtemp")
        worker = next(n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id == "SftpDownloadWorker")
        self.assertLess(validation, temporary)
        self.assertLess(validation, worker)
        self.assertTrue(any(isinstance(n.func, ast.Name) and n.func.id == "confined_download_path" for n in calls))


if __name__ == "__main__":
    unittest.main()
