import json
import requests
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import download_data


class DownloadDataTests(unittest.TestCase):
    def test_get_retries_rate_limit_and_honors_retry_after(self):
        limited = requests.Response()
        limited.status_code = 429
        limited.headers["Retry-After"] = "2"
        success = requests.Response()
        success.status_code = 200
        success._content = b"ok"

        with (
            patch.object(download_data.requests, "get", side_effect=[limited, success]) as get,
            patch.object(download_data.time, "sleep") as sleep,
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(download_data, "RAW_DIR", Path(temp_dir)),
        ):
            response = download_data.get("https://example.test/data", {}, retries=2)

        self.assertIs(response, success)
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_get_reuses_cached_response_and_refresh_bypasses_cache(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b"snapshot"
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(download_data, "RAW_DIR", Path(temp_dir)),
            patch.object(download_data.requests, "get", return_value=response) as get,
        ):
            first = download_data.get("https://example.test/data", {"q": "x"})
            second = download_data.get("https://example.test/data", {"q": "x"})
            self.assertEqual(get.call_count, 1)

            download_data.get("https://example.test/data", {"q": "x"}, refresh=True)

        self.assertEqual(first.content, second.content)
        self.assertEqual(get.call_count, 2)

    def test_get_returns_openfda_not_found_without_retry(self):
        not_found = requests.Response()
        not_found.status_code = 404
        with patch.object(download_data.requests, "get", return_value=not_found) as get:
            response = download_data.get(download_data.OPENFDA_URL, {}, retries=2)

        self.assertIs(response, not_found)
        get.assert_called_once()

    def test_get_does_not_retry_permanent_http_error(self):
        bad_request = requests.Response()
        bad_request.status_code = 400
        with (
            patch.object(download_data.requests, "get", return_value=bad_request) as get,
            patch.object(download_data.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 400"):
                download_data.get("https://example.test/data", {}, retries=3)

        get.assert_called_once()
        sleep.assert_not_called()

    def test_get_does_not_expose_request_url_on_connection_failure(self):
        error = requests.ConnectionError(
            "connection failed for https://example.test/data?api_key=secret-sentinel"
        )
        with (
            patch.object(download_data.requests, "get", side_effect=error),
            patch.object(download_data.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "ConnectionError") as raised:
                download_data.get("https://example.test/data", {}, retries=1)

        self.assertNotIn("secret-sentinel", str(raised.exception))

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
            self.assertEqual(
                manifest["mplus-edema"]["source_version_status"], "not_provided"
            )
            self.assertEqual(
                manifest["mplus-edema"]["source_updated_at_status"], "not_provided"
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
            self.assertEqual(row["source_version_status"], "available")
            self.assertEqual(row["source_updated_at_status"], "available")
            self.assertTrue((root / row["raw_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
