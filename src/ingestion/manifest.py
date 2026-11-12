"""Backward-compatible CSV manifest helpers shared by source collectors."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from src.models.document import Document
from src.ingestion.storage import atomic_write_text

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "data" / "manifest.csv"

MANIFEST_FIELDS = [
    "doc_id",
    "source",
    "topic_area",
    "title",
    "url",
    "source_version",
    "retrieved_utc",
    "license_note",
    "local_path",
    "sha256",
    "document_type",
    "section",
    "source_updated_at",
    "raw_path",
    "content_hash",
    "metadata_json",
    "source_version_status",
    "source_updated_at_status",
]


def load_manifest(path: Path = MANIFEST_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as manifest_file:
        rows = {}
        for row in csv.DictReader(manifest_file):
            row.setdefault(
                "source_version_status",
                "available" if row.get("source_version") else "legacy_unknown",
            )
            row.setdefault(
                "source_updated_at_status",
                "available" if row.get("source_updated_at") else "legacy_unknown",
            )
            rows[row["doc_id"]] = row
        return rows


def save_manifest(rows: dict[str, dict[str, str]], path: Path = MANIFEST_PATH) -> None:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=MANIFEST_FIELDS, lineterminator="\n"
    )
    writer.writeheader()
    for doc_id in sorted(rows):
        row = dict(rows[doc_id])
        row.setdefault(
            "source_version_status",
            "available" if row.get("source_version") else "legacy_unknown",
        )
        row.setdefault(
            "source_updated_at_status",
            "available" if row.get("source_updated_at") else "legacy_unknown",
        )
        writer.writerow({key: row.get(key, "") for key in MANIFEST_FIELDS})
    atomic_write_text(path, output.getvalue())


def document_manifest_row(
    document: Document,
    topic_area: str,
    license_note: str,
    local_path: str,
    raw_path: str,
) -> dict[str, str]:
    """Map a common document record to the legacy-compatible CSV columns."""
    return {
        "doc_id": document.doc_id,
        "source": document.source,
        "topic_area": topic_area,
        "title": document.title,
        "url": document.url,
        "source_version": document.source_version,
        "source_version_status": document.source_version_status,
        "retrieved_utc": document.retrieved_utc,
        "license_note": license_note,
        "local_path": local_path,
        "sha256": document.content_hash,
        "document_type": document.document_type,
        "section": document.section,
        "source_updated_at": document.source_updated_at,
        "source_updated_at_status": document.source_updated_at_status,
        "raw_path": raw_path,
        "content_hash": document.content_hash,
        "metadata_json": json.dumps(document.metadata, sort_keys=True),
    }
