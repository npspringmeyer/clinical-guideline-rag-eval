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


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def save_processed_document(document: Document, root: Path = ROOT) -> Path:
    path = root / "data" / "processed" / document.source / f"{document.doc_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def get(url: str, params: dict, retries: int = 3) -> requests.Response:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if resp.status_code == 404 and "api.fda.gov" in url:
                return resp  # openFDA uses 404 for "no matches"
            resp.raise_for_status()
            return resp
        except requests.RequestException as err:
            last_err = err
            time.sleep(1.5 * attempt)
    raise RuntimeError(f"Request failed after {retries} tries: {last_err}")


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


def download_medlineplus(manifest: dict[str, dict], retmax: int) -> int:
    out_dir = RAW_DIR / "medlineplus"
    query_dir = out_dir / "_queries"
    query_dir.mkdir(parents=True, exist_ok=True)
    seen_urls: set[str] = set()
    added = 0

    for term in MPLUS_QUERIES:
        params = {"db": "healthTopics", "term": term, "retmax": retmax, "rettype": "topic"}
        resp = get(MPLUS_URL, params)
        raw_content = resp.content
        query_hash = hashlib.sha256(raw_content).hexdigest()[:16]
        query_path = query_dir / f"{slugify(term)}-{query_hash}.xml"
        query_path.write_bytes(raw_content)
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
                retrieved_utc=now_utc(),
                source_updated_at="",
                source_version="",
                content=f"{title}\n\n{body}\n",
                metadata=doc.get("metadata", {}),
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


def download_openfda(manifest: dict[str, dict]) -> int:
    out_dir = RAW_DIR / "openfda"
    out_dir.mkdir(parents=True, exist_ok=True)
    api_key = os.environ.get("OPENFDA_API_KEY")
    added = 0

    for generic, area in DRUGS.items():
        params = {"search": f'openfda.generic_name:"{generic}"', "limit": 25}
        if api_key:
            params["api_key"] = api_key
        resp = get(OPENFDA_URL, params)
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
        json_path.write_bytes(raw_content)
        manufacturer = ", ".join(label.get("openfda", {}).get("manufacturer_name", []))
        document = Document(
            doc_id=doc_id,
            source="openfda",
            document_type="drug_label",
            title=f"{generic} label ({manufacturer})" if manufacturer else f"{generic} label",
            section="prescribing information",
            url=f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}",
            retrieved_utc=now_utc(),
            source_updated_at=label.get("effective_time", ""),
            source_version=f"effective_time={label.get('effective_time', '')};"
                           f"version={label.get('version', '')}",
            content=render_label(label, generic),
            metadata={"set_id": set_id, "generic_name": generic, "topic_area": area},
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
        document = Document(
            doc_id=row["doc_id"],
            source="openfda",
            document_type=row.get("document_type") or "drug_label",
            title=row["title"],
            section=row.get("section") or "prescribing information",
            url=row["url"],
            retrieved_utc=row.get("retrieved_utc") or now_utc(),
            source_updated_at=row.get("source_updated_at") or label.get("effective_time", ""),
            source_version=row.get("source_version", ""),
            content=content,
            metadata=metadata or {"set_id": label.get("set_id", "")},
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
            )
            processed_path = save_processed_document(document, root)
            manifest[doc_id] = document_manifest_row(
                document,
                term,
                MPLUS_LICENSE_NOTE,
                processed_path.relative_to(root).as_posix(),
                query_path.relative_to(root).as_posix(),
            )


def migrate_legacy_outputs(manifest: dict[str, dict], root: Path = ROOT) -> None:
    """Move legacy normalized files out of raw storage and index cached topics."""
    _migrate_openfda_records(manifest, root)
    _migrate_medlineplus_records(manifest, root)


# --- Entry point -----------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", choices=["all", "medlineplus", "openfda"], default="all")
    parser.add_argument("--retmax", type=int, default=3,
                        help="MedlinePlus results to keep per search term")
    args = parser.parse_args()

    manifest = load_manifest()
    migrate_legacy_outputs(manifest)
    total = 0
    if args.source in ("all", "medlineplus"):
        print("MedlinePlus health topics:")
        total += download_medlineplus(manifest, args.retmax)
    if args.source in ("all", "openfda"):
        print("openFDA drug labels:")
        total += download_openfda(manifest)

    save_manifest(manifest)
    print(f"\nDone. {total} documents added or refreshed; "
          f"{len(manifest)} total in {rel(MANIFEST_PATH)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
