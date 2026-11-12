#!/usr/bin/env python3
"""Download the public source documents for the healthcare RAG project.

Sources
-------
* MedlinePlus health topics (NLM) via the search-based web service.
  Attribution to MedlinePlus.gov is expected when you use this content.
* openFDA drug labels (structured product labeling) via the openFDA API.
  openFDA states its data must not be relied on for medical care decisions.

Outputs (raw files are git-ignored; commit the manifest)
-------------------------------------------------------
data/raw/medlineplus/_queries/   raw XML responses, one per query
data/raw/openfda/<doc>.json      raw label record, one per drug
data/processed/<source>/<doc>.json
                                  normalized documents using the shared schema
data/manifest.csv                provenance for every document

Usage
-----
    python scripts/download_data.py                # everything
    python scripts/download_data.py --source openfda
    python scripts/download_data.py --source medlineplus --retmax 5

Optional: set OPENFDA_API_KEY in your environment for higher rate limits.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.ingestion.manifest import document_manifest_row, load_manifest, save_manifest
from src.ingestion.http_cache import CACHE_TIMESTAMP_HEADER, request_with_cache
from src.ingestion.storage import atomic_write_bytes, atomic_write_text
from src.ingestion.clinicaltrials import study_to_document
from src.models.document import Document

RAW_DIR = ROOT / "data" / "raw"
MANIFEST_PATH = ROOT / "data" / "manifest.csv"

HEADERS = {"User-Agent": "clinical-guideline-rag-eval (portfolio project)"}
MPLUS_URL = "https://wsearch.nlm.nih.gov/ws/query"
OPENFDA_URL = "https://api.fda.gov/drug/label.json"

MPLUS_LICENSE_NOTE = (
    "NLM MedlinePlus content; attribute MedlinePlus.gov. "
    "See https://medlineplus.gov/about/developers/"
)
OPENFDA_LICENSE_NOTE = (
    "openFDA data; see openFDA terms of service. "
    "Not for medical care decisions."
)
LOGGER = logging.getLogger(__name__)

# --- Corpus definition: edit these lists to change scope -------------------

# MedlinePlus search terms (English health topics)
MPLUS_QUERIES = [
    "high blood pressure",
    "high blood pressure in pregnancy",
    "preeclampsia",
    "gestational diabetes",
    "type 2 diabetes",
    "diabetes medicines",
    "blood pressure medicines",
    "edema",
]

# generic drug name -> topic area
DRUGS = {
    # Hypertension (including pregnancy-related use)
    "lisinopril": "hypertension",
    "losartan": "hypertension",
    "amlodipine": "hypertension",
    "hydrochlorothiazide": "hypertension",
    "metoprolol": "hypertension",
    "labetalol": "hypertension",
    "nifedipine": "hypertension",
    "methyldopa": "hypertension",
    "hydralazine": "hypertension",
    "magnesium sulfate": "preeclampsia",
    # Diabetes
    "metformin": "diabetes",
    "glyburide": "diabetes",
    "glipizide": "diabetes",
    "pioglitazone": "diabetes",
    "insulin glargine": "diabetes",
    "insulin aspart": "diabetes",
    # Edema
    "furosemide": "edema",
    "spironolactone": "edema",
}

# openFDA label fields to render into readable text, in order
LABEL_SECTIONS = [
    ("boxed_warning", "Boxed warning"),
    ("indications_and_usage", "Indications and usage"),
    ("dosage_and_administration", "Dosage and administration"),
    ("contraindications", "Contraindications"),
    ("warnings_and_cautions", "Warnings and precautions"),
    ("warnings", "Warnings"),
    ("precautions", "Precautions"),
    ("adverse_reactions", "Adverse reactions"),
    ("drug_interactions", "Drug interactions"),
    ("use_in_specific_populations", "Use in specific populations"),
    ("pregnancy", "Pregnancy"),
    ("lactation", "Lactation"),
    ("overdosage", "Overdosage"),
]

# --- Helpers ---------------------------------------------------------------


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def response_retrieved_utc(response: requests.Response) -> str:
    value = response.headers.get(CACHE_TIMESTAMP_HEADER)
    return value if isinstance(value, str) and value else now_utc()


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def save_processed_document(document: Document, root: Path = ROOT) -> Path:
    path = root / "data" / "processed" / document.source / f"{document.doc_id}.json"
    atomic_write_text(
        path, json.dumps(document.to_dict(), ensure_ascii=False, indent=2) + "\n"
    )
    return path


def get(
    url: str, params: dict, retries: int = 3, refresh: bool = False
) -> requests.Response:
    for attempt in range(1, retries + 1):
        try:
            resp = request_with_cache(
                lambda: requests.get(
                    url, params=params, headers=HEADERS, timeout=30
                ),
                method="GET",
                url=url,
                params=params,
                cache_dir=RAW_DIR / ".http-cache",
                refresh=refresh,
            )
        except requests.RequestException as error:
            if attempt == retries:
                raise RuntimeError(
                    f"Request failed after {retries} tries ({type(error).__name__})"
                ) from None
            time.sleep(1.5 * attempt)
            continue

        if resp.status_code == 404 and "api.fda.gov" in url:
            return resp  # openFDA uses 404 for "no matches"
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt < retries:
                retry_after = resp.headers.get("Retry-After", "")
                delay = float(retry_after) if retry_after.isdigit() else 1.5 * attempt
                time.sleep(min(delay, 30))
                continue
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            raise RuntimeError(
                f"Request returned HTTP {resp.status_code}"
            ) from None
        return resp
    raise RuntimeError("Request failed without a response")


def clean_html(raw: str) -> str:
    """Turn (possibly entity-escaped) HTML into readable plain text."""
    text = html.unescape(raw)
    text = re.sub(r"(?i)<\s*li[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)</\s*(p|div|ul|ol|h\d)\s*>", "\n\n", text)
    text = re.sub(r"(?i)<\s*br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# --- MedlinePlus -----------------------------------------------------------


def parse_mplus(xml_text: str) -> list[dict]:
    """Extract documents from a MedlinePlus web service response."""
    root = ET.fromstring(xml_text)
    docs = []
    for el in root.iter():
        if local_name(el.tag) != "document":
            continue
        fields: dict[str, str] = {}
        metadata: dict[str, str] = {}
        for child in el:
            if local_name(child.tag) != "content":
                continue
            name = child.get("name", "")
            if name and name not in fields:
                fields[name] = "".join(child.itertext())
            topic = next(
                (item for item in child.iter() if local_name(item.tag) == "health-topic"),
                None,
            )
            if topic is not None:
                metadata = dict(topic.attrib)
                fields["title"] = topic.get("title", "")
                summary = next(
                    (item for item in topic.iter() if local_name(item.tag) == "full-summary"),
                    None,
                )
                if summary is not None:
                    fields["FullSummary"] = "".join(summary.itertext())
        docs.append({"url": el.get("url", ""), "fields": fields, "metadata": metadata})
    return docs


def download_medlineplus(
    manifest: dict[str, dict], retmax: int, refresh: bool = False
) -> int:
    out_dir = RAW_DIR / "medlineplus"
    query_dir = out_dir / "_queries"
    query_dir.mkdir(parents=True, exist_ok=True)
    seen_urls: set[str] = set()
    added = 0

    for term in MPLUS_QUERIES:
        params = {"db": "healthTopics", "term": term, "retmax": retmax, "rettype": "topic"}
        resp = get(MPLUS_URL, params, refresh=refresh)
        raw_content = resp.content
        query_hash = hashlib.sha256(raw_content).hexdigest()[:16]
        query_path = query_dir / f"{slugify(term)}-{query_hash}.xml"
        atomic_write_bytes(query_path, raw_content)
        docs = parse_mplus(resp.text)
        if not docs:
            print(f"  [warn] no documents parsed for '{term}' "
                  f"(check data/raw/medlineplus/_queries/{slugify(term)}.xml)")
        for doc in docs:
            url = doc["url"]
            title = clean_html(doc["fields"].get("title", "")) or url
            body = clean_html(doc["fields"].get("FullSummary", ""))
            if not url or not body:
                print(f"  [warn] skipped incomplete record: {title!r}")
                continue
            if url in seen_urls:
                continue
            seen_urls.add(url)

            doc_id = f"mplus-{slugify(title)}"
            document = Document(
                doc_id=doc_id,
                source="medlineplus",
                document_type="health_topic",
                title=title,
                section="full summary",
                url=url,
                retrieved_utc=response_retrieved_utc(resp),
                source_updated_at="",
                source_version="",
                content=f"{title}\n\n{body}\n",
                metadata=doc.get("metadata", {}),
                source_version_status="not_provided",
                source_updated_at_status="not_provided",
            )
            processed_path = save_processed_document(document)
            manifest[doc_id] = document_manifest_row(
                document,
                term,
                MPLUS_LICENSE_NOTE,
                rel(processed_path),
                rel(query_path),
            )
            added += 1
            print(f"  + {title}")
        time.sleep(0.5)
    return added


# --- openFDA ---------------------------------------------------------------


def score_label(label: dict, generic: str) -> int:
    """Prefer single-ingredient labels with many useful sections."""
    score = sum(1 for key, _ in LABEL_SECTIONS if label.get(key))
    names = [n.lower() for n in label.get("openfda", {}).get("generic_name", [])]
    if len(names) == 1 and names[0] == generic.lower():
        score += 100
    return score


def render_label(label: dict, generic: str) -> str:
    brand = ", ".join(label.get("openfda", {}).get("brand_name", [])) or "n/a"
    lines = [f"Drug label: {generic}", f"Brand names: {brand}", ""]
    for key, heading in LABEL_SECTIONS:
        value = label.get(key)
        if not value:
            continue
        text = "\n".join(value) if isinstance(value, list) else str(value)
        lines += [heading.upper(), clean_html(text), ""]
    return "\n".join(lines).strip() + "\n"


def download_openfda(manifest: dict[str, dict], refresh: bool = False) -> int:
    out_dir = RAW_DIR / "openfda"
    out_dir.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("OPENFDA_API_KEY")
    added = 0

    for generic, area in DRUGS.items():
        params = {"search": f'openfda.generic_name:"{generic}"', "limit": 25}
        if api_key:
            params["api_key"] = api_key
        resp = get(OPENFDA_URL, params, refresh=refresh)
        results = resp.json().get("results", []) if resp.status_code == 200 else []
        if not results:
            print(f"  [warn] no label found for {generic}")
            continue

        label = max(results, key=lambda r: score_label(r, generic))
        set_id = label.get("set_id", "unknown")
        doc_id = f"fda-{slugify(generic)}-{set_id[:8]}"

        raw_content = (json.dumps(label, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        raw_hash = hashlib.sha256(raw_content).hexdigest()[:16]
        json_path = out_dir / f"{doc_id}-{raw_hash}.json"
        atomic_write_bytes(json_path, raw_content)
        manufacturer = ", ".join(label.get("openfda", {}).get("manufacturer_name", []))
        effective_time = label.get("effective_time")
        label_version = label.get("version")
        source_version_parts = []
        version_extraction_failed = False
        for field_name, field_value in (
            ("effective_time", effective_time),
            ("version", label_version),
        ):
            if isinstance(field_value, (str, int)) and str(field_value).strip():
                source_version_parts.append(f"{field_name}={field_value}")
            elif field_value not in (None, ""):
                version_extraction_failed = True
        source_updated_at = (
            str(effective_time)
            if isinstance(effective_time, (str, int)) and str(effective_time).strip()
            else ""
        )
        updated_status = (
            "available"
            if source_updated_at
            else "extraction_failed"
            if effective_time not in (None, "")
            else "not_provided"
        )
        document = Document(
            doc_id=doc_id,
            source="openfda",
            document_type="drug_label",
            title=f"{generic} label ({manufacturer})" if manufacturer else f"{generic} label",
            section="prescribing information",
            url=f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}",
            retrieved_utc=response_retrieved_utc(resp),
            source_updated_at=source_updated_at,
            source_version=";".join(source_version_parts),
            content=render_label(label, generic),
            metadata={"set_id": set_id, "generic_name": generic, "topic_area": area},
            source_version_status=(
                "extraction_failed"
                if version_extraction_failed
                else "available"
                if source_version_parts
                else "not_provided"
            ),
            source_updated_at_status=updated_status,
        )
        processed_path = save_processed_document(document)
        manifest[doc_id] = document_manifest_row(
            document,
            area,
            OPENFDA_LICENSE_NOTE,
            rel(processed_path),
            rel(json_path),
        )
        added += 1
        print(f"  + {generic} ({manufacturer or 'unknown manufacturer'})")
        time.sleep(0.4)
    return added


def _migrate_openfda_records(manifest: dict[str, dict], root: Path) -> None:
    for row in list(manifest.values()):
        if row.get("source") != "openfda":
            continue
        current_path = root / row.get("local_path", "")
        if current_path.suffix == ".json" and current_path.is_file():
            continue

        raw_path = root / row.get("raw_path", "") if row.get("raw_path") else None
        if raw_path is None or not raw_path.is_file():
            candidate = current_path.with_suffix(".json")
            raw_path = candidate if candidate.is_file() else None
        if raw_path is None:
            raise RuntimeError(f"Missing raw openFDA record for {row['doc_id']}")
        try:
            label = json.loads(raw_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise RuntimeError(f"Invalid existing openFDA JSON: {raw_path}") from error

        metadata = json.loads(row.get("metadata_json") or "{}")
        generic = metadata.get("generic_name") or row["title"].split(" label", 1)[0]
        if current_path.is_file() and current_path.suffix == ".txt":
            content = current_path.read_text(encoding="utf-8")
        else:
            content = render_label(label, generic)
        metadata.setdefault("set_id", label.get("set_id", ""))
        metadata.setdefault("generic_name", generic)
        metadata.setdefault("topic_area", row.get("topic_area", ""))
        raw_effective_time = label.get("effective_time")
        raw_label_version = label.get("version")
        extracted_version_parts = []
        version_extraction_failed = False
        for field_name, field_value in (
            ("effective_time", raw_effective_time),
            ("version", raw_label_version),
        ):
            if isinstance(field_value, (str, int)) and str(field_value).strip():
                extracted_version_parts.append(f"{field_name}={field_value}")
            elif field_value not in (None, ""):
                version_extraction_failed = True
        version_value = row.get("source_version") or ";".join(extracted_version_parts)
        updated_value = row.get("source_updated_at") or (
            str(raw_effective_time)
            if isinstance(raw_effective_time, (str, int))
            and str(raw_effective_time).strip()
            else ""
        )
        version_status = row.get("source_version_status", "")
        if version_status == "extraction_failed" or version_extraction_failed:
            version_status = "extraction_failed"
        elif version_value:
            version_status = "available"
        elif version_status in ("", "legacy_unknown"):
            version_status = "not_provided"
        updated_status = row.get("source_updated_at_status", "")
        if updated_value:
            updated_status = "available"
        elif updated_status in ("", "legacy_unknown"):
            updated_status = (
                "extraction_failed"
                if raw_effective_time not in (None, "")
                else "not_provided"
            )
        document = Document(
            doc_id=row["doc_id"],
            source="openfda",
            document_type=row.get("document_type") or "drug_label",
            title=row["title"],
            section=row.get("section") or "prescribing information",
            url=row["url"],
            retrieved_utc=row.get("retrieved_utc") or now_utc(),
            source_updated_at=updated_value,
            source_version=version_value,
            content=content,
            metadata=metadata or {"set_id": label.get("set_id", "")},
            source_version_status=version_status,
            source_updated_at_status=updated_status,
        )
        processed_path = save_processed_document(document, root)
        manifest[row["doc_id"]] = document_manifest_row(
            document,
            row.get("topic_area", ""),
            row.get("license_note", OPENFDA_LICENSE_NOTE),
            processed_path.relative_to(root).as_posix(),
            raw_path.relative_to(root).as_posix(),
        )
        if current_path.is_file() and current_path.suffix == ".txt":
            current_path.unlink()


def _migrate_medlineplus_records(manifest: dict[str, dict], root: Path) -> None:
    query_dir = root / "data" / "raw" / "medlineplus" / "_queries"
    seen_urls: set[str] = set()
    for term in MPLUS_QUERIES:
        query_path = query_dir / f"{slugify(term)}.xml"
        if not query_path.is_file():
            continue
        try:
            docs = parse_mplus(query_path.read_text(encoding="utf-8"))
        except ET.ParseError as error:
            raise RuntimeError(f"Invalid MedlinePlus XML response: {query_path}") from error
        captured_utc = datetime.fromtimestamp(
            query_path.stat().st_mtime, timezone.utc
        ).isoformat()
        for doc in docs:
            url = doc["url"]
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            title = clean_html(doc["fields"].get("title", "")) or url
            body = clean_html(doc["fields"].get("FullSummary", ""))
            if not body:
                continue
            doc_id = f"mplus-{slugify(title)}"
            existing = manifest.get(doc_id, {})
            existing_path = root / existing.get("local_path", "")
            if (
                existing.get("source") == "medlineplus"
                and existing_path.suffix == ".json"
                and existing_path.is_file()
            ):
                continue
            metadata = dict(doc.get("metadata", {}))
            metadata["capture_timestamp_basis"] = "local_query_file_mtime"
            document = Document(
                doc_id=doc_id,
                source="medlineplus",
                document_type="health_topic",
                title=title,
                section="full summary",
                url=url,
                retrieved_utc=captured_utc,
                source_updated_at="",
                source_version="",
                content=f"{title}\n\n{body}\n",
                metadata=metadata,
                source_version_status="not_provided",
                source_updated_at_status="not_provided",
            )
            processed_path = save_processed_document(document, root)
            manifest[doc_id] = document_manifest_row(
                document,
                term,
                MPLUS_LICENSE_NOTE,
                processed_path.relative_to(root).as_posix(),
                query_path.relative_to(root).as_posix(),
            )


def _migrate_legacy_source_metadata_statuses(
    manifest: dict[str, dict], root: Path
) -> None:
    trial_payloads: dict[Path, dict] = {}
    for row in manifest.values():
        source = row.get("source")
        version_status = row.get("source_version_status", "legacy_unknown")
        updated_status = row.get("source_updated_at_status", "legacy_unknown")
        if source == "medlineplus":
            if not row.get("source_version") and version_status == "legacy_unknown":
                row["source_version_status"] = "not_provided"
            if not row.get("source_updated_at") and updated_status == "legacy_unknown":
                row["source_updated_at_status"] = "not_provided"
        elif source == "clinicaltrials.gov" and (
            version_status == "legacy_unknown" or updated_status == "legacy_unknown"
        ):
            raw_path = root / row.get("raw_path", "")
            if not raw_path.is_file():
                continue
            if raw_path not in trial_payloads:
                try:
                    trial_payloads[raw_path] = json.loads(
                        raw_path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError as error:
                    raise RuntimeError(
                        f"Invalid ClinicalTrials.gov raw JSON: {raw_path}"
                    ) from error
            payload = trial_payloads[raw_path]
            metadata = json.loads(row.get("metadata_json") or "{}")
            nct_id = metadata.get("nct_id") or row["doc_id"].removeprefix("ctg-")
            study = next(
                (
                    item
                    for item in payload.get("studies", [])
                    if item.get("protocolSection", {})
                    .get("identificationModule", {})
                    .get("nctId")
                    == nct_id
                ),
                None,
            )
            if study is None:
                LOGGER.warning(
                    "Could not match %s to its raw ClinicalTrials.gov response",
                    row["doc_id"],
                )
                continue
            extracted = study_to_document(study, row.get("retrieved_utc"))
            if version_status == "legacy_unknown":
                row["source_version"] = extracted.source_version
                row["source_version_status"] = extracted.source_version_status
            if updated_status == "legacy_unknown":
                row["source_updated_at"] = extracted.source_updated_at
                row["source_updated_at_status"] = extracted.source_updated_at_status
                metadata["source_update_date"] = extracted.source_updated_at
                row["metadata_json"] = json.dumps(metadata, sort_keys=True)

        if source in {"medlineplus", "clinicaltrials.gov"}:
            local_path = root / row.get("local_path", "")
            if not local_path.is_file():
                continue
            try:
                document = json.loads(local_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise RuntimeError(
                    f"Invalid processed document JSON: {local_path}"
                ) from error
            document["source_version"] = row.get("source_version", "")
            document["source_version_status"] = row.get(
                "source_version_status", "legacy_unknown"
            )
            document["source_updated_at"] = row.get("source_updated_at", "")
            document["source_updated_at_status"] = row.get(
                "source_updated_at_status", "legacy_unknown"
            )
            document["metadata"] = json.loads(row.get("metadata_json") or "{}")
            atomic_write_text(
                local_path,
                json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            )


def migrate_legacy_outputs(manifest: dict[str, dict], root: Path = ROOT) -> None:
    """Move legacy normalized files out of raw storage and index cached topics."""
    _migrate_openfda_records(manifest, root)
    _migrate_medlineplus_records(manifest, root)
    _migrate_legacy_source_metadata_statuses(manifest, root)


# --- Entry point -----------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", choices=["all", "medlineplus", "openfda"], default="all")
    parser.add_argument("--retmax", type=int, default=3,
                        help="MedlinePlus results to keep per search term")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass the 12-hour local HTTP response cache",
    )
    args = parser.parse_args()

    manifest = load_manifest()
    migrate_legacy_outputs(manifest)
    total = 0
    if args.source in ("all", "medlineplus"):
        print("MedlinePlus health topics:")
        total += download_medlineplus(manifest, args.retmax, refresh=args.refresh)
    if args.source in ("all", "openfda"):
        print("openFDA drug labels:")
        total += download_openfda(manifest, refresh=args.refresh)

    save_manifest(manifest)
    print(f"\nDone. {total} documents added or refreshed; "
          f"{len(manifest)} total in {rel(MANIFEST_PATH)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
