#!/usr/bin/env python3
"""Collect a bounded ClinicalTrials.gov API v2 study corpus."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.ingestion.clinicaltrials import ClinicalTrialsClient, DEFAULT_CONDITIONS
from src.ingestion.collect_clinicaltrials import build_searches, collect_studies


def _key_value(value: str) -> tuple[str, str]:
    key, separator, content = value.partition("=")
    if not separator or not key or not content:
        raise argparse.ArgumentTypeError("expected KEY=VALUE")
    return key, content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--condition", action="append", help="Condition to search (repeatable)")
    parser.add_argument("--intervention", action="append", help="Intervention to search (repeatable)")
    parser.add_argument("--query-term", action="append", help="API query.term value (repeatable)")
    parser.add_argument(
        "--query-param",
        action="append",
        type=_key_value,
        default=[],
        metavar="KEY=VALUE",
        help="Additional supported ClinicalTrials.gov API v2 parameter (repeatable)",
    )
    parser.add_argument("--limit", type=int, default=100, help="Maximum studies per search")
    parser.add_argument("--page-size", type=int, default=100, help="API page size (1-1000)")
    parser.add_argument("--timeout", type=float, default=30, help="Request timeout in seconds")
    parser.add_argument("--retries", type=int, default=3, help="Maximum attempts per page")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit cannot be negative")
    try:
        conditions = args.condition
        if conditions is None:
            conditions = (
                []
                if args.intervention or args.query_term
                else list(DEFAULT_CONDITIONS)
            )
        searches = build_searches(
            conditions,
            args.intervention,
            args.query_term,
            args.query_param,
        )
        logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
        count = collect_studies(
            ClinicalTrialsClient(timeout=args.timeout, retries=args.retries),
            searches,
            args.limit,
            args.page_size,
        )
    except (ValueError, RuntimeError) as error:
        parser.error(str(error))
    print(f"Collected or refreshed {count} ClinicalTrials.gov records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
