import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import fetch_terminologies


class FetchTerminologiesTests(unittest.TestCase):
    def test_fetches_source_terms_and_writes_normalized_records(self):
        source_terms = {
            "MSH": [("preeclampsia", "medlineplus_query")],
            "RXNORM": [("labetalol", "openfda_generic_name")],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "terms.jsonl"
            with (
                patch.dict(os.environ, {"UMLS_API_KEY": "test-key"}),
                patch.object(fetch_terminologies, "SOURCE_TERMS", source_terms),
                patch.object(
                    sys,
                    "argv",
                    ["fetch_terminologies.py", "--output", str(output)],
                ),
                patch.object(fetch_terminologies, "UMLSClient") as client_factory,
            ):
                client = client_factory.return_value
                client.search_source.side_effect = [
                    [{"ui": "D011225", "name": "Pre-Eclampsia", "rootSource": "MSH"}],
                    [{"ui": "6185", "name": "labetalol", "rootSource": "RXNORM"}],
                ]

                self.assertEqual(fetch_terminologies.main(), 0)

            records = [
                json.loads(line)
                for line in output.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [(record["source_abbreviation"], record["query"]) for record in records],
                [("MSH", "preeclampsia"), ("RXNORM", "labetalol")],
            )
            self.assertEqual(
                [call.args[:2] for call in client.search_source.call_args_list],
                [("preeclampsia", "MSH"), ("labetalol", "RXNORM")],
            )
            self.assertEqual(
                [record["candidates"][0]["rootSource"] for record in records],
                ["MSH", "RXNORM"],
            )
            self.assertTrue(all(record["content_hash"] for record in records))
            self.assertNotIn("test-key", output.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
