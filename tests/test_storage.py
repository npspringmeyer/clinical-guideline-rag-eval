import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ingestion.manifest import load_manifest, save_manifest
from src.ingestion.storage import atomic_write_text


class StorageTests(unittest.TestCase):
    def test_interrupted_atomic_write_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "output.json"
            path.write_text("previous\n", encoding="utf-8")

            with patch("src.ingestion.storage.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    atomic_write_text(path, "replacement\n")

            self.assertEqual(path.read_text(encoding="utf-8"), "previous\n")
            self.assertEqual(list(Path(temp_dir).glob(".output.json.*.tmp")), [])

    def test_manifest_adds_explicit_legacy_status_for_old_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manifest.csv"
            save_manifest({"doc-1": {
                "doc_id": "doc-1",
                "source": "legacy",
                "source_version": "",
                "source_updated_at": "",
            }}, path)

            row = load_manifest(path)["doc-1"]

            self.assertEqual(row["source_version_status"], "legacy_unknown")
            self.assertEqual(row["source_updated_at_status"], "legacy_unknown")

    def test_interrupted_manifest_write_preserves_previous_manifest(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "manifest.csv"
            path.write_text("existing manifest\n", encoding="utf-8")
            with patch(
                "src.ingestion.storage.os.replace",
                side_effect=OSError("interrupted"),
            ):
                with self.assertRaisesRegex(OSError, "interrupted"):
                    save_manifest({"doc-1": {"doc_id": "doc-1"}}, path)

            self.assertEqual(
                path.read_text(encoding="utf-8"), "existing manifest\n"
            )


if __name__ == "__main__":
    unittest.main()
