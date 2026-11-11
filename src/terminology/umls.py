"""Optional UMLS REST client and ambiguity-preserving term normalization."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

API_ROOT = "https://uts-ws.nlm.nih.gov/rest"


class UMLSConfigurationError(RuntimeError):
    """Raised when an API operation is attempted without an API key."""


class UMLSClient:
    """Access only the terminology resources available to the supplied API key."""

    def __init__(
        self,
        api_key: str | None = None,
        release: str | None = None,
        session: requests.Session | None = None,
        timeout: float = 30,
        retries: int = 3,
        sleep=time.sleep,
        raw_dir: Path | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("UMLS_API_KEY")
        self.release = release or os.getenv("UMLS_RELEASE", "current")
        if timeout <= 0 or retries < 1:
            raise ValueError("timeout must be positive and retries must be at least one")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep
        self.raw_dir = raw_dir

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict:
        if not self.api_key:
            raise UMLSConfigurationError(
                "UMLS is optional; set UMLS_API_KEY in the environment to enable access."
            )
        request_params = dict(params or {})
        request_params["apiKey"] = self.api_key
        url = f"{API_ROOT}/{path.lstrip('/')}"

        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.get(url, params=request_params, timeout=self.timeout)
            except requests.RequestException as error:
                if attempt == self.retries:
                    raise RuntimeError(
                        f"UMLS REST request failed after {self.retries} attempts "
                        f"(release {self.release}; {type(error).__name__})."
                    ) from None
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
                    f"UMLS REST API returned HTTP {response.status_code} "
                    f"(release {self.release})."
                ) from None
            try:
                payload = response.json()
            except ValueError as error:
                raise RuntimeError("UMLS REST API returned invalid JSON") from error
            if self.raw_dir is not None:
                self._save_raw_response(path, request_params, response.content)
            return payload
        raise RuntimeError("UMLS REST request failed without a response")

    def _save_raw_response(
        self, path: str, params: dict[str, Any], content: bytes
    ) -> None:
        """Store exact response bytes under a stable key without persisting credentials."""
        safe_params = {key: value for key, value in params.items() if key != "apiKey"}
        request_fingerprint = hashlib.sha256(
            json.dumps([path, safe_params], sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        endpoint = re.sub(r"[^a-zA-Z0-9._-]+", "-", path).strip("-")
        output_path = self.raw_dir / f"{endpoint}-{request_fingerprint}.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(content)

    def search(self, term: str, max_results: int = 10) -> list[dict]:
        if not term.strip():
            raise ValueError("Search term cannot be empty")
        if max_results < 1:
            raise ValueError("max_results must be at least one")
        response = self._get_json(
            f"search/{quote(self.release, safe='')}",
            {
                "string": term,
                "searchType": "words",
                "returnIdType": "concept",
                "pageNumber": 1,
                "pageSize": max_results,
            },
        )
        results = response.get("result", {}).get("results", [])
        return [result for result in results if result.get("ui") != "NONE"][:max_results]

    def lookup_concept(
        self,
        cui: str,
        include_synonyms: bool = False,
        include_relationships: bool = False,
        max_related: int = 20,
    ) -> dict:
        if not cui or cui == "NONE":
            raise ValueError("A valid UMLS CUI is required")
        if max_related < 0:
            raise ValueError("max_related cannot be negative")
        encoded_cui = quote(cui, safe="")
        concept_response = self._get_json(
            f"content/{quote(self.release, safe='')}/CUI/{encoded_cui}"
        )
        concept = concept_response.get("result", {})
        semantic_types = [
            item.get("name", "")
            for item in concept.get("semanticTypes", [])
            if item.get("name")
        ]
        result = {
            "cui": concept.get("ui", cui),
            "preferred_term": concept.get("name", ""),
            "semantic_types": semantic_types,
            "terminology_source": concept.get("rootSource", ""),
            "source_version": self.release,
        }
        sources = {result["terminology_source"]} if result["terminology_source"] else set()
        if include_synonyms:
            if max_related:
                atoms_response = self._get_json(
                    f"content/{quote(self.release, safe='')}/CUI/{encoded_cui}/atoms",
                    {"pageNumber": 1, "pageSize": max_related},
                )
                result["synonyms"] = sorted({
                    atom.get("name", "")
                    for atom in atoms_response.get("result", [])
                    if atom.get("name") and atom.get("name") != result["preferred_term"]
                })
                sources.update(
                    atom.get("rootSource", "")
                    for atom in atoms_response.get("result", [])
                    if atom.get("rootSource")
                )
            else:
                result["synonyms"] = []
        if include_relationships:
            if max_related:
                relations_response = self._get_json(
                    f"content/{quote(self.release, safe='')}/CUI/{encoded_cui}/relations",
                    {"pageNumber": 1, "pageSize": max_related},
                )
                result["relationships"] = [
                    relation for relation in relations_response.get("result", [])[:max_related]
                ]
            else:
                result["relationships"] = []
        result["terminology_sources"] = sorted(sources)
        if not result["terminology_source"] and sources:
            result["terminology_source"] = sorted(sources)[0]
        return result

    def normalize(
        self,
        term: str,
        max_candidates: int = 10,
        include_synonyms: bool = False,
        include_relationships: bool = False,
    ) -> dict:
        """Return ranked candidates without selecting or replacing the input term."""
        hits = self.search(term, max_results=max_candidates)
        candidates = []
        seen_cuis = set()
        for hit in hits:
            cui = hit.get("ui", "")
            if not cui or cui in seen_cuis:
                continue
            seen_cuis.add(cui)
            concept = self.lookup_concept(
                cui,
                include_synonyms=include_synonyms,
                include_relationships=include_relationships,
            )
            concept["search_term"] = term
            concept["search_score"] = hit.get("score")
            concept["search_candidate_term"] = hit.get("name", "")
            sources = {
                source
                for source in (
                    hit.get("rootSource", ""),
                    concept.get("terminology_source", ""),
                    *concept.get("terminology_sources", []),
                )
                if source
            }
            concept["terminology_sources"] = sorted(sources)
            if not concept.get("terminology_source") and sources:
                concept["terminology_source"] = sorted(sources)[0]
            candidates.append(concept)
        return {
            "query": term,
            "ambiguous": len(candidates) > 1,
            "candidates": candidates,
            "source": "umls_terminology_metadata",
            "source_version": self.release,
            "retrieved_utc": datetime.now(timezone.utc).isoformat(),
            "medical_evidence": False,
        }

    def expand_query(
        self,
        term: str,
        max_candidates: int = 10,
        include_synonyms: bool = False,
        include_relationships: bool = False,
    ) -> dict:
        """Suggest optional terms while retaining the original query and candidates."""
        normalization = self.normalize(
            term, max_candidates, include_synonyms, include_relationships
        )
        expanded_terms = {term}
        for candidate in normalization["candidates"]:
            preferred = candidate.get("preferred_term")
            if preferred:
                expanded_terms.add(preferred)
            expanded_terms.update(candidate.get("synonyms", []))
        normalization["expanded_terms"] = sorted(expanded_terms)
        return normalization
