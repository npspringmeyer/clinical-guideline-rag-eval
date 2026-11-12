import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock

from src.terminology.umls import UMLSClient, UMLSConfigurationError
from tests.test_clinicaltrials import FakeResponse


class UMLSTests(unittest.TestCase):
    def test_missing_key_is_optional_and_actionable(self):
        client = UMLSClient(api_key="", session=Mock())
        self.assertFalse(client.available)
        with self.assertRaisesRegex(UMLSConfigurationError, "UMLS_API_KEY"):
            client.search("hypertension")

    def test_normalization_preserves_ambiguous_candidates_and_expansion(self):
        session = Mock()
        session.get.side_effect = [
            FakeResponse({"result": {"results": [
                {"ui": "C0001", "name": "Hypertension", "score": "9"},
                {"ui": "C0002", "name": "Pulmonary hypertension", "score": "7"},
                {"ui": "NONE", "name": "No result"},
            ]}}),
            FakeResponse({"result": {
                "ui": "C0001",
                "name": "Hypertension",
                "rootSource": "SNOMEDCT_US",
                "semanticTypes": [{"name": "Disease or Syndrome"}],
            }}),
            FakeResponse({"result": [{"name": "High blood pressure"}]}),
            FakeResponse({"result": {
                "ui": "C0002",
                "name": "Pulmonary hypertension",
                "rootSource": "SNOMEDCT_US",
                "semanticTypes": [{"name": "Disease or Syndrome"}],
            }}),
            FakeResponse({"result": [{"name": "Pulmonary arterial hypertension"}]}),
        ]
        client = UMLSClient(api_key="test-key", session=session, release="2025AA")
        result = client.expand_query("hypertension", include_synonyms=True)

        self.assertEqual(result["query"], "hypertension")
        self.assertTrue(result["ambiguous"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn("hypertension", result["expanded_terms"])
        self.assertIn("High blood pressure", result["expanded_terms"])
        self.assertEqual(result["candidates"][0]["cui"], "C0001")
        self.assertEqual(result["candidates"][0]["semantic_types"], ["Disease or Syndrome"])
        self.assertEqual(result["candidates"][0]["terminology_source"], "SNOMEDCT_US")
        self.assertEqual(result["source_version"], "2025AA")
        self.assertFalse(result["medical_evidence"])

    def test_lookup_can_include_relationships(self):
        session = Mock()
        session.get.side_effect = [
            FakeResponse({"result": {"ui": "C1", "name": "Term", "semanticTypes": []}}),
            FakeResponse({"result": [{"relatedId": "C2", "additionalRelation": "RO"}]}),
        ]
        result = UMLSClient(api_key="test-key", session=session).lookup_concept(
            "C1", include_relationships=True
        )
        self.assertEqual(result["relationships"][0]["relatedId"], "C2")

    def test_source_search_requests_source_codes(self):
        session = Mock()
        session.get.return_value = FakeResponse({"result": {"results": [
            {"ui": "6809", "name": "metformin", "rootSource": "RXNORM"},
            {"ui": "NONE", "name": "No result"},
        ]}})
        client = UMLSClient(api_key="test-key", session=session)

        results = client.search_source("metformin", "RXNORM")

        self.assertEqual([result["ui"] for result in results], ["6809"])
        params = session.get.call_args.kwargs["params"]
        self.assertEqual(params["sabs"], "RXNORM")
        self.assertEqual(params["returnIdType"], "code")
        self.assertEqual(params["searchType"], "exact")

    def test_source_search_can_use_word_matching(self):
        session = Mock()
        session.get.return_value = FakeResponse({"result": {"results": []}})
        client = UMLSClient(api_key="test-key", session=session)

        client.search_source("metformin", "RXNORM", search_type="words")

        self.assertEqual(
            session.get.call_args.kwargs["params"]["searchType"], "words"
        )

    def test_source_search_retries_rate_limit_and_server_error(self):
        sleep = Mock()
        session = Mock()
        session.get.side_effect = [
            FakeResponse({}, status_code=503, headers={"Retry-After": "2"}),
            FakeResponse({}, status_code=429, headers={"Retry-After": "1"}),
            FakeResponse({"result": {"results": [
                {"ui": "6809", "name": "metformin", "rootSource": "RXNORM"},
            ]}}),
        ]
        client = UMLSClient(
            api_key="test-key", session=session, retries=3, sleep=sleep
        )

        results = client.search_source("metformin", "RXNORM")

        self.assertEqual(results[0]["ui"], "6809")
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(2.0), unittest.mock.call(1.0)])
        self.assertEqual(session.get.call_count, 3)

    def test_unauthorized_response_fails_without_retry_or_key_leak(self):
        session = Mock()
        session.get.return_value = FakeResponse({}, status_code=401)
        client = UMLSClient(api_key="secret-sentinel", session=session, retries=3)

        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as raised:
            client.search("preeclampsia")

        self.assertNotIn("secret-sentinel", str(raised.exception))
        session.get.assert_called_once()

    def test_search_saves_exact_raw_response_without_api_key(self):
        response_body = b'{"result":{"results":[{"ui":"C123","name":"Example"}]}}'
        session = Mock()
        session.get.return_value = FakeResponse(
            {"result": {"results": [{"ui": "C123", "name": "Example"}]}},
            raw_content=response_body,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            client = UMLSClient(
                api_key="do-not-save-this-key",
                session=session,
                raw_dir=Path(temp_dir),
            )
            client.search("example")
            saved_files = list(Path(temp_dir).glob("*.json"))

            self.assertEqual(len(saved_files), 1)
            self.assertEqual(saved_files[0].read_bytes(), response_body)
            self.assertNotIn(b"do-not-save-this-key", saved_files[0].read_bytes())

    def test_reuses_recent_cached_response_without_another_request(self):
        session = Mock()
        session.get.return_value = FakeResponse({"result": {"results": [
            {"ui": "D011225", "name": "Pre-Eclampsia"},
        ]}})
        with tempfile.TemporaryDirectory() as temp_dir:
            client = UMLSClient(
                api_key="test-key", session=session, raw_dir=Path(temp_dir)
            )

            first = client.search("preeclampsia")
            second = client.search("preeclampsia")

            self.assertEqual(first, second)
            session.get.assert_called_once()

    def test_corrupted_cache_is_refetched_and_replaced_with_valid_response(self):
        session = Mock()
        session.get.return_value = FakeResponse({"result": {"results": [
            {"ui": "6809", "name": "metformin"},
        ]}})
        with tempfile.TemporaryDirectory() as temp_dir:
            raw_dir = Path(temp_dir)
            client = UMLSClient(
                api_key="test-key", session=session, raw_dir=raw_dir
            )
            client.search_source("metformin", "RXNORM")
            cache_path = next(raw_dir.glob("*.json"))
            cache_path.write_text("{corrupt", encoding="utf-8")

            results = client.search_source("metformin", "RXNORM")

            self.assertEqual(results[0]["ui"], "6809")
            self.assertEqual(session.get.call_count, 2)
            self.assertNotIn("test-key", cache_path.read_text(encoding="utf-8"))
            self.assertEqual(
                client.search_source("metformin", "RXNORM")[0]["ui"], "6809"
            )
            self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
