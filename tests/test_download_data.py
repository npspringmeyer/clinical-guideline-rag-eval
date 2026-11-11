import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import download_data


class DownloadDataTests(unittest.TestCase):
    def test_medlineplus_preserves_new_raw_query_snapshots(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_dir = root / "data" / "raw"
            processed_dir = root / "data" / "processed"
            responses = [
                "first summary",
                "updated summary",
            ]

            def save_document(document):
                path = processed_dir / document.source / f"{document.doc_id}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(document.to_dict()), encoding="utf-8")
                return path

            manifest = {}
            with (
                patch.object(download_data, "ROOT", root),
                patch.object(download_data, "RAW_DIR", raw_dir),
                patch.object(download_data, "MPLUS_QUERIES", ["edema"]),
                patch.object(download_data, "save_processed_document", side_effect=save_document),
                patch.object(download_data.time, "sleep"),
            ):
                for summary in responses:
                    xml = (
                        '<nlmSearchResult><term>edema</term><list><document '
                        'url="https://medlineplus.gov/edema.html"><content name="healthTopic">'
                        '<health-topic title="Edema" id="1229"><full-summary>'
                        f'<p>{summary}</p>'
                        '</full-summary></health-topic></content></document></list></nlmSearchResult>'
                    )
                    response = Mock(text=xml, content=xml.encode("utf-8"))
                    with patch.object(download_data, "get", return_value=response):
                        download_data.download_medlineplus(manifest, retmax=3)

            snapshots = list((raw_dir / "medlineplus" / "_queries").glob("edema-*.xml"))
            self.assertEqual(len(snapshots), 2)
            self.assertEqual(
                {
                    summary
                    for summary in responses
                    if any(summary in snapshot.read_text() for snapshot in snapshots)
                },
                set(responses),
            )
            self.assertIn("updated summary", json.loads(
                (processed_dir / "medlineplus" / "mplus-edema.json").read_text()
            )["content"])
            self.assertEqual(
                manifest["mplus-edema"]["raw_path"].split("/")[-1],
                next(snapshot.name for snapshot in snapshots
                     if "updated summary" in snapshot.read_text()),
            )

    def test_openfda_keeps_versioned_raw_records_outside_processed_documents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_dir = root / "data" / "raw"
            processed_dir = root / "data" / "processed"
            labels = [
                {
                    "set_id": "12345678-example",
                    "effective_time": "20250101",
                    "version": "1",
                    "indications_and_usage": ["First version."],
                    "openfda": {"generic_name": ["example"], "manufacturer_name": ["Maker"]},
                },
                {
                    "set_id": "12345678-example",
                    "effective_time": "20260101",
                    "version": "2",
                    "indications_and_usage": ["Updated version."],
                    "openfda": {"generic_name": ["example"], "manufacturer_name": ["Maker"]},
                },
            ]

            def save_document(document):
                path = processed_dir / document.source / f"{document.doc_id}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(document.to_dict()), encoding="utf-8")
                return path

            manifest = {}
            with (
                patch.object(download_data, "ROOT", root),
                patch.object(download_data, "RAW_DIR", raw_dir),
                patch.object(download_data, "DRUGS", {"example": "hypertension"}),
                patch.object(download_data, "save_processed_document", side_effect=save_document),
                patch.object(download_data.time, "sleep"),
            ):
                for label in labels:
                    response = Mock(status_code=200)
                    response.json.return_value = {"results": [label]}
                    with patch.object(download_data, "get", return_value=response):
                        download_data.download_openfda(manifest)

            snapshots = list((raw_dir / "openfda").glob("fda-example-12345678-*.json"))
            self.assertEqual(len(snapshots), 2)
            self.assertEqual(
                {json.loads(path.read_text())["effective_time"] for path in snapshots},
                {"20250101", "20260101"},
            )
            row = manifest["fda-example-12345678"]
            self.assertTrue(row["local_path"].startswith("data/processed/openfda/"))
            self.assertEqual(
                json.loads((root / row["local_path"]).read_text())["source_updated_at"],
                "20260101",
            )
            self.assertTrue((root / row["raw_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
