#!/usr/bin/env python3
"""Enrich selected terms with optional UMLS terminology metadata."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.terminology.umls import UMLSClient, UMLSConfigurationError
from src.ingestion.storage import atomic_write_text

DEFAULT_OUTPUT = ROOT / "data" / "processed" / "umls" / "enrichment.jsonl"
RAW_OUTPUT = ROOT / "data" / "raw" / "umls"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query",
        action="append",
        required=True,
        help="Term to normalize (repeatable); terms are sent to the UMLS REST API",
    )
    parser.add_argument("--release", default=os.getenv("UMLS_RELEASE", "current"))
    parser.add_argument("--max-candidates", type=int, default=10)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Bypass the 12-hour local UMLS response cache",
    )
    parser.add_argument("--include-synonyms", action="store_true")
    parser.add_argument("--include-relationships", action="store_true")
    parser.add_argument("--expand", action="store_true", help="Also return optional query expansions")
    parser.add_argument("--document-id", action="append", default=[],
                        help="Optional source document ID to associate with each query")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not os.getenv("UMLS_API_KEY"):
        parser.error("UMLS_API_KEY is not set; UMLS enrichment is optional.")
    if args.max_candidates < 1:
        parser.error("--max-candidates must be at least one")

    client = UMLSClient(
        release=args.release,
        raw_dir=RAW_OUTPUT,
        cache_ttl_seconds=0 if args.refresh else 12 * 60 * 60,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()
    output = io.StringIO()
    try:
        for query in dict.fromkeys(args.query):
            method = client.expand_query if args.expand else client.normalize
            result = method(
                query,
                max_candidates=args.max_candidates,
                include_synonyms=args.include_synonyms,
                include_relationships=args.include_relationships,
            )
            result["origin_doc_ids"] = args.document_id
            key = f"{args.release}\0{query}\0{','.join(args.document_id)}"
            result["enrichment_id"] = "umls-" + hashlib.sha256(
                key.encode("utf-8")
            ).hexdigest()[:20]
            hash_payload = {key: value for key, value in result.items()
                            if key != "retrieved_utc"}
            result["content_hash"] = hashlib.sha256(
                json.dumps(hash_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            output.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
            written.add(query)
        atomic_write_text(args.output, output.getvalue())
    except UMLSConfigurationError as error:
        parser.error(str(error))
    print(f"Wrote UMLS raw API JSON responses to {RAW_OUTPUT}.")
    print(f"Wrote normalized terminology metadata for {len(written)} terms to {args.output}.")
    print("Review UMLS licensing terms before redistributing this output.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
