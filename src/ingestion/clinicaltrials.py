"""Client and deterministic document conversion for ClinicalTrials.gov API v2."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Mapping

import requests

from src.models.document import Document
from src.ingestion.http_cache import CACHE_TIMESTAMP_HEADER, request_with_cache

LOGGER = logging.getLogger(__name__)
API_URL = "https://clinicaltrials.gov/api/v2/studies"
SOURCE_URL = "https://clinicaltrials.gov/study/"
RESULTS_DISCLAIMER = (
    "Trial registration and reported results are not, by themselves, "
    "published clinical evidence or proof of established treatment effectiveness."
)
DEFAULT_CONDITIONS = (
    "hypertension",
    "preeclampsia",
    "gestational diabetes",
    "type 2 diabetes",
)


@dataclass(frozen=True)
class StudyPage:
    payload: dict
    raw_content: bytes
    retrieved_utc: str = ""


class ClinicalTrialsClient:
    """Small API v2 client with bounded retries and token-based pagination."""

    def __init__(
        self,
        session: requests.Session | None = None,
        timeout: float = 30,
        retries: int = 3,
        sleep=time.sleep,
        cache_dir: Path | None = None,
        cache_ttl_seconds: float = 12 * 60 * 60,
        refresh: bool = False,
    ) -> None:
        if timeout <= 0 or retries < 1 or cache_ttl_seconds < 0:
            raise ValueError(
                "timeout and retries must be positive and cache TTL cannot be negative"
            )
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.cache_dir = cache_dir
        self.cache_ttl_seconds = cache_ttl_seconds
        self.refresh = refresh

    def fetch_page(
        self, query: Mapping[str, str], page_size: int, page_token: str | None = None
    ) -> StudyPage:
        if not 1 <= page_size <= 1000:
            raise ValueError("ClinicalTrials.gov page_size must be between 1 and 1000")
        params = dict(query)
        params["pageSize"] = page_size
        if page_token:
            params["pageToken"] = page_token

        for attempt in range(1, self.retries + 1):
            try:
                response = request_with_cache(
                    lambda: self.session.get(
                        API_URL,
                        params=params,
                        headers={"User-Agent": "clinical-guideline-rag-eval"},
                        timeout=self.timeout,
                    ),
                    method="GET",
                    url=API_URL,
                    params=params,
                    cache_dir=self.cache_dir,
                    ttl_seconds=self.cache_ttl_seconds,
                    refresh=self.refresh,
                )
            except requests.RequestException as error:
                if attempt == self.retries:
                    raise RuntimeError(
                        f"ClinicalTrials.gov API request failed after {self.retries} "
                        f"attempts: {error}"
                    ) from error
                self.sleep(1.5 * attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < self.retries:
                    retry_after = response.headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.isdigit() else 1.5 * attempt
                    self.sleep(min(delay, 30))
                    continue
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                raise RuntimeError(
                    f"ClinicalTrials.gov API returned HTTP {response.status_code}"
                ) from error
            try:
                payload = response.json()
            except ValueError as error:
                raise RuntimeError("ClinicalTrials.gov returned invalid JSON") from error
            retrieved_utc = response.headers.get(CACHE_TIMESTAMP_HEADER)
            if not isinstance(retrieved_utc, str) or not retrieved_utc:
                retrieved_utc = datetime.now(timezone.utc).isoformat()
            return StudyPage(payload, response.content, retrieved_utc)
        raise RuntimeError("ClinicalTrials.gov request failed without a response")

    def iter_study_pages(
        self,
        query: Mapping[str, str],
        limit: int,
        page_size: int = 100,
    ) -> Iterator[StudyPage]:
        if limit < 0:
            raise ValueError("limit cannot be negative")
        if limit == 0:
            return

        next_token = None
        yielded = 0
        while yielded < limit:
            current_size = min(page_size, limit - yielded)
            page = self.fetch_page(query, current_size, next_token)
            studies = page.payload.get("studies", [])
            yield page
            yielded += len(studies)
            next_token = page.payload.get("nextPageToken")
            if not next_token or not studies:
                break


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(item) for item in value if item]
    return []


def _module(study: dict, name: str) -> dict:
    return study.get("protocolSection", {}).get(name, {})


def _date(value: dict | str | None) -> str:
    if isinstance(value, dict):
        return str(value.get("date", ""))
    return str(value or "")


def _outcomes(items: list[dict]) -> list[str]:
    result = []
    for item in items:
        title = item.get("measure", "")
        details = [item.get("description", ""), item.get("timeFrame", "")]
        detail = "; ".join(part for part in details if part)
        result.append(f"{title}: {detail}" if detail else str(title))
    return result


def render_study(study: dict) -> tuple[str, str, str]:
    """Return NCT identifier, title, and readable content for one API study."""
    identification = _module(study, "identificationModule")
    nct_id = identification.get("nctId", "")
    title = identification.get("briefTitle") or identification.get("officialTitle") or nct_id
    status = _module(study, "statusModule")
    design = _module(study, "designModule")
    description = _module(study, "descriptionModule")
    eligibility = _module(study, "eligibilityModule")
    outcomes = _module(study, "outcomesModule")

    sections = [
        ("Study status", [status.get("overallStatus", "")]),
        ("Study dates", [
            f"{label}: {_date(status.get(key))}"
            for key, label in (
                ("studyFirstSubmitDate", "First submitted"),
                ("studyFirstPostDateStruct", "First posted"),
                ("lastUpdateSubmitDate", "Last update submitted"),
                ("lastUpdatePostDateStruct", "Last update posted"),
                ("primaryCompletionDateStruct", "Primary completion"),
                ("studyCompletionDateStruct", "Study completion"),
            )
            if _date(status.get(key))
        ]),
        ("Conditions", _strings(_module(study, "conditionsModule").get("conditions", []))),
        ("Interventions", [
            "; ".join(value for value in (item.get("type", ""), item.get("name", ""),
                                          item.get("description", "")) if value)
            for item in _module(study, "armsInterventionsModule").get("interventions", [])
        ]),
        ("Study design", [
            value for value in (
                design.get("studyType", ""),
                design.get("phases", []),
                design.get("designInfo", {}).get("allocation", ""),
                design.get("designInfo", {}).get("interventionModel", ""),
                design.get("designInfo", {}).get("primaryPurpose", ""),
                design.get("designInfo", {}).get("maskingInfo", {}).get("masking", ""),
            ) if value
        ]),
        ("Brief description", [description.get("briefSummary", "")]),
        ("Detailed description", [description.get("detailedDescription", "")]),
        ("Eligibility criteria", [eligibility.get("eligibilityCriteria", "")]),
        ("Primary outcome measures", _outcomes(outcomes.get("primaryOutcomes", []))),
        ("Secondary outcome measures", _outcomes(outcomes.get("secondaryOutcomes", []))),
        ("Other outcome measures", _outcomes(outcomes.get("otherOutcomes", []))),
    ]

    lines = [title, f"Study identifier: {nct_id}", RESULTS_DISCLAIMER]
    for heading, values in sections:
        present = []
        for value in values:
            if isinstance(value, list):
                present.extend(_strings(value))
            elif value:
                present.append(str(value))
        if present:
            lines.extend(["", heading])
            lines.extend(f"- {value}" for value in present)

    results = study.get("resultsSection")
    if results:
        lines.extend(["", "Reported results data (as registered/reported):",
                      json.dumps(results, ensure_ascii=False, sort_keys=True, indent=2)])
    return nct_id, str(title), "\n".join(lines).strip() + "\n"


def study_to_document(study: dict, retrieved_utc: str | None = None) -> Document:
    nct_id, title, content = render_study(study)
    if not re.fullmatch(r"NCT\d{8}", nct_id):
        raise ValueError(f"ClinicalTrials.gov study has an invalid NCT identifier: {nct_id!r}")
    status = _module(study, "statusModule")
    updated_value = status.get("lastUpdateSubmitDate")
    updated = _date(updated_value)
    if not updated:
        updated_value = status.get("lastUpdatePostDateStruct")
        updated = _date(updated_value)
    if updated:
        updated_status = "available"
    elif updated_value is None or updated_value == "":
        updated_status = "not_provided"
    else:
        updated_status = "extraction_failed"
    version_value = study.get("studyVersion")
    if isinstance(version_value, (str, int)) and str(version_value).strip():
        version = str(version_value)
        version_status = "available"
    elif version_value is None or version_value == "":
        version = ""
        version_status = "not_provided"
    else:
        version = ""
        version_status = "extraction_failed"
    return Document(
        doc_id=f"ctg-{nct_id}",
        source="clinicaltrials.gov",
        document_type="clinical_trial_registration",
        title=title,
        section="study record",
        url=f"{SOURCE_URL}{nct_id}",
        retrieved_utc=retrieved_utc or datetime.now(timezone.utc).isoformat(),
        source_updated_at=updated,
        source_version=version,
        content=content,
        metadata={
            "nct_id": nct_id,
            "overall_status": status.get("overallStatus", ""),
            "has_results": bool(study.get("resultsSection")),
            "not_clinical_evidence": True,
            "source_update_date": updated,
        },
        source_version_status=version_status,
        source_updated_at_status=updated_status,
    )
