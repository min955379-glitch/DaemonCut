from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend import config
from backend.maintenance import clean_transient_data


class MaintenanceTests(unittest.TestCase):
    def test_cleanup_only_targets_old_scratch_files_and_dry_run_is_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / ".daemoncut"
            cache = runtime / "cache"
            scratch = cache / "subfolder"
            scratch.mkdir(parents=True)
            old_file = scratch / "old.tmp"
            young_file = cache / "young.tmp"
            old_file.write_bytes(b"old")
            young_file.write_bytes(b"new")
            now = datetime.now(timezone.utc)
            old_epoch = (now - timedelta(hours=30)).timestamp()
            os.utime(old_file, (old_epoch, old_epoch))
            with patch.object(config, "RUNTIME_DIR", runtime), patch.object(config, "CACHE_DIR", cache), patch.object(
                config, "TEMP_DIR", runtime / "tmp"
            ), patch.object(config, "GDRIVE_DAEMONCUT_PATH", None):
                preview = clean_transient_data(ttl_hours=24, now=now, dry_run=True, roots=[cache])
                self.assertEqual(preview.files_would_remove, 1)
                self.assertEqual(preview.files_removed, 0)
                self.assertTrue(old_file.exists())
                executed = clean_transient_data(ttl_hours=24, now=now, dry_run=False, roots=[cache])
                self.assertEqual(executed.files_removed, 1)
                self.assertFalse(old_file.exists())
                self.assertTrue(young_file.exists())

    def test_unapproved_cleanup_roots_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "unapproved"):
                clean_transient_data(roots=[Path(temporary)])


if __name__ == "__main__":
    unittest.main()
