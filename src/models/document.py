"""Shared schema for normalized source documents."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Document:
    """A normalized, provenance-preserving corpus document."""

    doc_id: str
    source: str
    document_type: str
    title: str
    section: str
    url: str
    retrieved_utc: str
    source_updated_at: str
    source_version: str
    content: str
    metadata: dict = field(default_factory=dict)
    source_version_status: str = "legacy_unknown"
    source_updated_at_status: str = "legacy_unknown"

    def __post_init__(self) -> None:
        valid_statuses = {
            "available",
            "not_provided",
            "extraction_failed",
            "legacy_unknown",
        }
        if self.source_version_status not in valid_statuses:
            raise ValueError(
                f"Invalid source_version_status: {self.source_version_status!r}"
            )
        if self.source_updated_at_status not in valid_statuses:
            raise ValueError(
                f"Invalid source_updated_at_status: {self.source_updated_at_status!r}"
            )
        if self.source_version and self.source_version_status == "legacy_unknown":
            object.__setattr__(self, "source_version_status", "available")
        elif self.source_version and self.source_version_status == "not_provided":
            raise ValueError("A supplied source version cannot be marked not_provided")
        if self.source_updated_at and self.source_updated_at_status == "legacy_unknown":
            object.__setattr__(self, "source_updated_at_status", "available")
        elif self.source_updated_at and self.source_updated_at_status == "not_provided":
            raise ValueError("A supplied source update date cannot be marked not_provided")
        if self.source_version_status == "available" and not self.source_version:
            raise ValueError("A source version with status 'available' requires a value")
        if self.source_updated_at_status == "available" and not self.source_updated_at:
            raise ValueError("A source update date with status 'available' requires a value")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        result = asdict(self)
        result["content_hash"] = self.content_hash
        return result
