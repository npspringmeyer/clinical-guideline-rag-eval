import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import requests

from src.ingestion.clinicaltrials import ClinicalTrialsClient, study_to_document
from src.ingestion.collect_clinicaltrials import build_searches, collect_studies
from src.ingestion.manifest import load_manifest
from scripts.download_data import parse_mplus


def sample_study(nct_id="NCT01234567"):
    return {
        "protocolSection": {
            "identificationModule": {
                "nctId": nct_id,
                "briefTitle": "Study of a treatment",
            },
            "statusModule": {
                "overallStatus": "RECRUITING",
                "lastUpdateSubmitDate": "2025-04-03",
            },
            "conditionsModule": {"conditions": ["Hypertension"]},
            "armsInterventionsModule": {
                "interventions": [{"type": "DRUG", "name": "Example", "description": "Description"}]
            },
            "descriptionModule": {
                "briefSummary": "A study summary.",
                "detailedDescription": "Detailed methods.",
            },
            "eligibilityModule": {"eligibilityCriteria": "Adults only."},
            "outcomesModule": {
                "primaryOutcomes": [{"measure": "Blood pressure", "timeFrame": "12 weeks"}]
            },
        },
        "resultsSection": {"participantFlowModule": {"groups": []}},
    }


class FakeResponse:
    def __init__(self, payload, status_code=200, raw_content=None, headers=None):
        self.payload = payload
        self.status_code = status_code
        self.content = raw_content or json.dumps(payload).encode()
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class ClinicalTrialsTests(unittest.TestCase):
    def test_builds_condition_intervention_and_generic_parameter_searches(self):
        searches = build_searches(
            ["hypertension"],
            ["metformin"],
            ["pregnancy"],
            [("filter.overallStatus", "RECRUITING")],
        )
        self.assertEqual(len(searches), 3)
        self.assertEqual(searches[0][1]["query.cond"], "hypertension")
        self.assertEqual(searches[1][1]["query.intr"], "metformin")
        self.assertEqual(searches[2][1]["query.term"], "pregnancy")
        self.assertTrue(all(query["filter.overallStatus"] == "RECRUITING"
                            for _, query in searches))

    def test_existing_medlineplus_xml_shape_extracts_topic_text(self):
        xml_path = Path(__file__).resolve().parents[1] / "data/raw/medlineplus/_queries/edema.xml"
        documents = parse_mplus(xml_path.read_text(encoding="utf-8"))
        edema = next(document for document in documents if document["url"].endswith("/edema.html"))

        self.assertEqual(edema["fields"]["title"], "Edema")
        self.assertIn("Edema means swelling", edema["fields"]["FullSummary"])
        self.assertEqual(edema["metadata"]["id"], "1229")

    def test_paginates_with_token_and_respects_limit(self):
        session = Mock()
        session.get.side_effect = [
            FakeResponse({"studies": [{"id": 1}], "nextPageToken": "token-2"}),
            FakeResponse({"studies": [{"id": 2}]}),
        ]
        client = ClinicalTrialsClient(session=session, sleep=Mock())
        pages = list(client.iter_study_pages({"query.cond": "hypertension"}, 2, 1))

        self.assertEqual(len(pages), 2)
        self.assertEqual(session.get.call_args_list[0].kwargs["params"]["pageSize"], 1)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["pageToken"], "token-2")

    def test_retries_rate_limit_and_honors_retry_after(self):
        sleep = Mock()
        session = Mock()
        session.get.side_effect = [
            FakeResponse({}, status_code=429, headers={"Retry-After": "2"}),
            FakeResponse({"studies": []}),
        ]
        page = ClinicalTrialsClient(session=session, sleep=sleep).fetch_page({}, 10)

        self.assertEqual(page.payload, {"studies": []})
        sleep.assert_called_once_with(2.0)

    def test_study_document_retains_provenance_and_registration_caveat(self):
        document = study_to_document(sample_study(), "2025-04-04T00:00:00+00:00")
        self.assertEqual(document.doc_id, "ctg-NCT01234567")
        self.assertEqual(document.source_updated_at, "2025-04-03")
        self.assertEqual(document.metadata["nct_id"], "NCT01234567")
        self.assertTrue(document.metadata["not_clinical_evidence"])
        self.assertIn("Eligibility criteria", document.content)
        self.assertIn("Reported results data", document.content)
        self.assertEqual(len(document.content_hash), 64)

    def test_collection_preserves_raw_pages_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw_dir = root / "raw"
            processed_dir = root / "processed"
            manifest_path = root / "manifest.csv"
            raw_bytes = b'{"studies":[{"unmodified":true}]}'

            for _ in range(2):
                client = ClinicalTrialsClient(
                    session=Mock(get=Mock(return_value=FakeResponse(
                        {"studies": [sample_study()]}, raw_content=raw_bytes
                    ))),
                    sleep=Mock(),
                )
                count = collect_studies(
                    client,
                    [("hypertension", {"query.cond": "hypertension"})],
                    raw_dir=raw_dir,
                    processed_dir=processed_dir,
                    manifest_path=manifest_path,
                )
                self.assertEqual(count, 1)

            manifest = load_manifest(manifest_path)
            self.assertEqual(list(manifest), ["ctg-NCT01234567"])
            self.assertEqual((raw_dir / "hypertension-page-0001.json").read_bytes(), raw_bytes)
            normalized = json.loads((processed_dir / "ctg-NCT01234567.json").read_text())
            self.assertEqual(normalized["doc_id"], "ctg-NCT01234567")
            self.assertEqual(manifest["ctg-NCT01234567"]["document_type"],
                             "clinical_trial_registration")


if __name__ == "__main__":
    unittest.main()
