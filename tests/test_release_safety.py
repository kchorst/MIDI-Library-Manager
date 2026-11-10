"""Regression coverage for safe restore, archive verification, and scan reconciliation."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from core.app_paths import AppPaths
from core import library_backup as backup
from midi_library_scan.pipeline import LibraryScanService


class BackupSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = AppPaths.for_app(self.root / "app", portable=True).ensure()
        self.db = self.paths.library / "library_scan.sqlite3"
        self._write_database(self.db, 10)
        self.archive = self.root / "backup.zip"
        backup.create_library_intelligence_backup(self.paths, self.archive)
        self._write_database(self.db, 20)

    @staticmethod
    def _write_database(path, number):
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE IF NOT EXISTS sample (value INTEGER)")
            con.execute("DELETE FROM sample")
            con.execute("INSERT INTO sample VALUES (?)", (number,))

    def _current(self):
        with sqlite3.connect(self.db) as con:
            return con.execute("SELECT value FROM sample").fetchone()[0]

    def test_restore_happy_path(self):
        backup.restore_library_intelligence_backup(self.paths, self.archive)
        self.assertEqual(self._current(), 10)

    def test_failed_install_restores_original(self):
        original_replace = os.replace
        fired = False
        def fail_install(src, dst):
            nonlocal fired
            if not fired and Path(src).name == "library_scan.sqlite3" and "staged" in Path(src).parts:
                fired = True
                raise OSError("injected install failure")
            return original_replace(src, dst)
        with patch.object(backup.os, "replace", side_effect=fail_install):
            with self.assertRaises(OSError):
                backup.restore_library_intelligence_backup(self.paths, self.archive)
        self.assertTrue(fired)
        self.assertEqual(self._current(), 20)

    def test_failed_install_restores_sidecars(self):
        wal = Path(str(self.db) + "-wal")
        shm = Path(str(self.db) + "-shm")
        wal.write_bytes(b"original wal")
        shm.write_bytes(b"original shm")
        original_replace = os.replace
        def fail_install(src, dst):
            if Path(src).name == "library_scan.sqlite3" and "staged" in Path(src).parts:
                raise OSError("injected install failure")
            return original_replace(src, dst)
        with patch.object(backup.os, "replace", side_effect=fail_install):
            with self.assertRaises(OSError):
                backup.restore_library_intelligence_backup(self.paths, self.archive)
        self.assertEqual(wal.read_bytes(), b"original wal")
        self.assertEqual(shm.read_bytes(), b"original shm")
        # SQLite cannot read arbitrary fake WAL headers while present; examine
        # the restored main DB only after checking preserved bytes.
        wal.unlink(); shm.unlink()
        self.assertEqual(self._current(), 20)

    def test_second_database_install_failure_reverts_first_database(self):
        other = self.paths.library / "library_index.sqlite3"
        self._write_database(other, 30)
        # Include a second db in a newly generated archive.
        backup.create_library_intelligence_backup(self.paths, self.archive)
        self._write_database(self.db, 40)
        self._write_database(other, 50)
        original_replace = os.replace
        def fail_second(src, dst):
            if Path(src).name == "library_index.sqlite3" and "staged" in Path(src).parts:
                raise OSError("injected second database install failure")
            return original_replace(src, dst)
        with patch.object(backup.os, "replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                backup.restore_library_intelligence_backup(self.paths, self.archive)
        self.assertEqual(self._current(), 40)
        with sqlite3.connect(other) as con:
            self.assertEqual(con.execute("SELECT value FROM sample").fetchone()[0], 50)

    def test_manifest_only_rejected(self):
        bad = self.root / "manifest-only.zip"
        with zipfile.ZipFile(bad, "w") as out:
            out.writestr("manifest.json", json.dumps({"format":backup._FORMAT_V2,"included":[],"sqlite_user_versions":{}}))
        with self.assertRaisesRegex(RuntimeError, "no Library SQLite"):
            backup.verify_library_intelligence_backup(bad)

    def test_missing_member_rejected(self):
        bad = self.root / "mismatch.zip"
        with zipfile.ZipFile(self.archive) as src, zipfile.ZipFile(bad, "w") as dst:
            manifest = json.loads(src.read("manifest.json"))
            manifest["included"].append("library/library_index.sqlite3")
            for item in src.infolist():
                dst.writestr(item.filename, json.dumps(manifest) if item.filename == "manifest.json" else src.read(item))
        with self.assertRaisesRegex(RuntimeError, "manifest does not match"):
            backup.verify_library_intelligence_backup(bad)

    def test_valid_backup_verifies(self):
        manifest = backup.verify_library_intelligence_backup(self.archive)
        self.assertIn("library/library_scan.sqlite3", manifest["included"])


class ScanSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.service = LibraryScanService(self.root / "catalog.sqlite3")
        self.addCleanup(self.service.close)

    def test_failed_subdirectory_walk_is_reported(self):
        root = self.root / "music"
        root.mkdir()
        sub = root / "unreadable"
        sub.mkdir()
        (root / "ok.mid").write_bytes(b"MThd")
        original_scandir = os.scandir
        def failing_scandir(path):
            if Path(path) == sub:
                raise PermissionError("injected denied subtree")
            return original_scandir(path)
        errors = []
        with patch("midi_library_scan.pipeline.os.scandir", side_effect=failing_scandir):
            discovered = self.service.discover_many([root], traversal_errors_out=errors)
        self.assertEqual([p.name for p in discovered], ["ok.mid"])
        self.assertTrue(any("unreadable" in error for error in errors))

    def test_missing_root_is_reported(self):
        errors = []
        self.assertEqual(self.service.discover_many([self.root / "gone"], traversal_errors_out=errors), [])
        self.assertTrue(errors)

    def test_incomplete_walk_preserves_catalogue(self):
        root = self.root / "music"
        root.mkdir()
        (root / "nested").mkdir()
        existing = str(root / "nested" / "existing.mid")
        deleted = []
        with patch.object(self.service.database, "inventory_index", return_value={existing: (1, 1, True)}), \
             patch.object(self.service.database, "delete_paths", side_effect=lambda paths: deleted.extend(paths)), \
             patch.object(self.service.database, "catalogue_counts", return_value={"total": 1, "catalogued":1, "failed":0, "identified":0,"probable_identity":0,"unidentified":1,"exact_duplicates":0,"likely_duplicates":0,"resource_limited":0}), \
             patch("midi_library_scan.pipeline.os.scandir", side_effect=PermissionError("denied")):
            summary = self.service.quick_catalog_paths([root], replace_existing=True)
        self.assertEqual(deleted, [])
        self.assertEqual(summary.removed_files, 0)


if __name__ == "__main__":
    unittest.main()
