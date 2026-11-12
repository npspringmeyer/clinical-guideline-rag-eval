"""Idempotent persistence for bounded ClinicalTrials.gov study searches."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

from src.ingestion.clinicaltrials import ClinicalTrialsClient, study_to_document
from src.ingestion.manifest import document_manifest_row, load_manifest, save_manifest
from src.ingestion.storage import atomic_write_bytes, atomic_write_text

LOGGER = logging.getLogger(__name__)
LICENSE_NOTE = "ClinicalTrials.gov API data; review source terms before redistribution."


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60] or "query"


def _manifest_path(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def collect_studies(
    client: ClinicalTrialsClient,
    searches: list[tuple[str, dict[str, str]]],
    result_limit: int = 100,
    page_size: int = 100,
    raw_dir: Path | None = None,
    processed_dir: Path | None = None,
    manifest_path: Path | None = None,
) -> int:
    """Fetch each search, preserving exact response pages and normalized records."""
    root = Path(__file__).resolve().parents[2]
    raw_dir = raw_dir or root / "data" / "raw" / "clinicaltrials"
    processed_dir = processed_dir or root / "data" / "processed" / "clinicaltrials"
    manifest_path = manifest_path or root / "data" / "manifest.csv"
    raw_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(manifest_path)
    refreshed = 0

    for label, query in searches:
        page_count = 0
        study_count = 0
        seen_ids: set[str] = set()
        for page in client.iter_study_pages(query, result_limit, page_size):
            page_count += 1
            page_hash = hashlib.sha256(page.raw_content).hexdigest()[:16]
            raw_path = raw_dir / (
                f"{_slug(label)}-page-{page_count:04d}-{page_hash}.json"
            )
            atomic_write_bytes(raw_path, page.raw_content)
            for study in page.payload.get("studies", []):
                document = study_to_document(study, page.retrieved_utc)
                if document.doc_id in seen_ids:
                    continue
                seen_ids.add(document.doc_id)
                processed_path = processed_dir / f"{document.doc_id}.json"
                atomic_write_text(
                    processed_path,
                    json.dumps(document.to_dict(), ensure_ascii=False, indent=2) + "\n",
                )
                manifest[document.doc_id] = document_manifest_row(
                    document,
                    label,
                    LICENSE_NOTE,
                    _manifest_path(processed_path, root),
                    _manifest_path(raw_path, root),
                )
                study_count += 1
                refreshed += 1
        LOGGER.info("Search %r fetched %d studies across %d pages", label, study_count, page_count)

    save_manifest(manifest, manifest_path)
    return refreshed


def build_searches(
    conditions: list[str],
    interventions: list[str] | None = None,
    query_terms: list[str] | None = None,
    extra_params: list[tuple[str, str]] | None = None,
) -> list[tuple[str, dict[str, str]]]:
    """Build supported API v2 query parameters without changing their meaning."""
    searches = []
    extras = dict(extra_params or [])
    for key, _ in extra_params or []:
        if key in {"query.cond", "query.intr", "query.term"}:
            raise ValueError(f"{key} is controlled by --condition, --intervention, or --query-term")
    for condition in conditions:
        query = {"query.cond": condition, **extras}
        searches.append((condition, query))
    for intervention in interventions or []:
        query = {"query.intr": intervention, **extras}
        searches.append((f"intervention-{intervention}", query))
    for term in query_terms or []:
        query = {"query.term": term, **extras}
        searches.append((f"term-{term}", query))
    if not searches:
        raise ValueError("Provide at least one condition, intervention, or query term")
    if len(extras) != len(extra_params or []):
        raise ValueError("Duplicate --query-param keys are not supported")
    return searches
