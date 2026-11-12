#!/usr/bin/env python3
"""Fetch corpus-scoped RxNorm and MeSH terminology metadata from UTS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.download_data import DRUGS, MPLUS_QUERIES
from src.terminology.umls import UMLSClient

DEFAULT_OUTPUT = ROOT / "data" / "processed" / "umls" / "source_enrichment.jsonl"
RAW_OUTPUT = ROOT / "data" / "raw" / "umls"
SOURCES = ("MSH", "RXNORM")
SOURCE_TERMS = {
    "MSH": [(query, "medlineplus_query") for query in MPLUS_QUERIES],
    "RXNORM": [(drug, "openfda_generic_name") for drug in DRUGS],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default=os.getenv("UMLS_RELEASE", "current"))
    parser.add_argument("--max-results", type=int, default=10)
    parser.add_argument(
        "--search-type",
        choices=("exact", "words"),
        default="exact",
        help="UTS matching mode (default: exact)",
    )
    parser.add_argument(
        "--source",
        action="append",
        choices=SOURCES,
        help="Vocabulary to fetch (repeatable; defaults to both MeSH and RxNorm)",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not os.getenv("UMLS_API_KEY"):
        parser.error("UMLS_API_KEY is not set; UTS terminology access requires a key.")
    if not 1 <= args.max_results <= 200:
        parser.error("--max-results must be between 1 and 200")

    sources = list(dict.fromkeys(args.source or SOURCES))
    client = UMLSClient(release=args.release, raw_dir=RAW_OUTPUT)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with args.output.open("w", encoding="utf-8") as output_file:
        for source in sources:
            for query, origin in SOURCE_TERMS[source]:
                record = {
                    "query": query,
                    "query_origin": origin,
                    "source_abbreviation": source,
                    "candidates": client.search_source(
                        query,
                        source,
                        max_results=args.max_results,
                        search_type=args.search_type,
                    ),
                    "search_type": args.search_type,
                    "source": "umls_source_terminology",
                    "source_version": args.release,
                    "retrieved_utc": datetime.now(timezone.utc).isoformat(),
                    "medical_evidence": False,
                }
                key = (
                    f"{args.release}\0{source}\0{args.search_type}\0{origin}\0{query}"
                )
                record["enrichment_id"] = "umls-" + hashlib.sha256(
                    key.encode("utf-8")
                ).hexdigest()[:20]
                hash_payload = {
                    field: value
                    for field, value in record.items()
                    if field != "retrieved_utc"
                }
                record["content_hash"] = hashlib.sha256(
                    json.dumps(
                        hash_payload, sort_keys=True, ensure_ascii=False
                    ).encode("utf-8")
                ).hexdigest()
                output_file.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                )
                count += 1

    print(f"Wrote exact UTS API responses to {RAW_OUTPUT}.")
    print(f"Wrote {count} source-term records to {args.output}.")
    print("Review UMLS and source-vocabulary licensing terms before redistribution.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
