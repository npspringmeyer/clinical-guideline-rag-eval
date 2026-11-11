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

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        result = asdict(self)
        result["content_hash"] = self.content_hash
        return result
