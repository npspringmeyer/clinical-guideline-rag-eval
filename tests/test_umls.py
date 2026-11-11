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


if __name__ == "__main__":
    unittest.main()
