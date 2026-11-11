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
data/raw/medlineplus/<doc>.txt   cleaned text, one file per health topic
data/raw/medlineplus/_queries/   raw XML responses, one per query
data/raw/openfda/<doc>.json      raw label record, one per drug
data/raw/openfda/<doc>.txt       readable text of the key label sections
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
import csv
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
]

# --- Helpers ---------------------------------------------------------------


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


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


def load_manifest() -> dict[str, dict]:
    if not MANIFEST_PATH.exists():
        return {}
    with MANIFEST_PATH.open(newline="", encoding="utf-8") as f:
        return {row["doc_id"]: row for row in csv.DictReader(f)}


def save_manifest(rows: dict[str, dict]) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST_PATH.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        for doc_id in sorted(rows):
            writer.writerow({k: rows[doc_id].get(k, "") for k in MANIFEST_FIELDS})


# --- MedlinePlus -----------------------------------------------------------


def parse_mplus(xml_text: str) -> list[dict]:
    """Extract documents from a MedlinePlus web service response."""
    root = ET.fromstring(xml_text)
    docs = []
    for el in root.iter():
        if local_name(el.tag) != "document":
            continue
        fields: dict[str, str] = {}
        for child in el:
            if local_name(child.tag) != "content":
                continue
            name = child.get("name", "")
            if name and name not in fields:
                fields[name] = "".join(child.itertext())
        docs.append({"url": el.get("url", ""), "fields": fields})
    return docs


def download_medlineplus(manifest: dict[str, dict], retmax: int) -> int:
    out_dir = RAW_DIR / "medlineplus"
    query_dir = out_dir / "_queries"
    query_dir.mkdir(parents=True, exist_ok=True)
    seen_urls = {r["url"] for r in manifest.values() if r["source"] == "medlineplus"}
    added = 0

    for term in MPLUS_QUERIES:
        params = {"db": "healthTopics", "term": term, "retmax": retmax, "rettype": "topic"}
        resp = get(MPLUS_URL, params)
        (query_dir / f"{slugify(term)}.xml").write_text(resp.text, encoding="utf-8")
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
            path = out_dir / f"{doc_id}.txt"
            path.write_text(f"{title}\n\n{body}\n", encoding="utf-8")
            manifest[doc_id] = {
                "doc_id": doc_id,
                "source": "medlineplus",
                "topic_area": term,
                "title": title,
                "url": url,
                "source_version": "",
                "retrieved_utc": now_utc(),
                "license_note": MPLUS_LICENSE_NOTE,
                "local_path": rel(path),
                "sha256": sha256_of(path),
            }
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

        json_path = out_dir / f"{doc_id}.json"
        txt_path = out_dir / f"{doc_id}.txt"
        json_path.write_text(json.dumps(label, indent=2), encoding="utf-8")
        txt_path.write_text(render_label(label, generic), encoding="utf-8")

        manufacturer = ", ".join(label.get("openfda", {}).get("manufacturer_name", []))
        manifest[doc_id] = {
            "doc_id": doc_id,
            "source": "openfda",
            "topic_area": area,
            "title": f"{generic} label ({manufacturer})" if manufacturer else f"{generic} label",
            "url": f"https://dailymed.nlm.nih.gov/dailymed/lookup.cfm?setid={set_id}",
            "source_version": f"effective_time={label.get('effective_time', '')};"
                              f"version={label.get('version', '')}",
            "retrieved_utc": now_utc(),
            "license_note": OPENFDA_LICENSE_NOTE,
            "local_path": rel(txt_path),
            "sha256": sha256_of(txt_path),
        }
        added += 1
        print(f"  + {generic} ({manufacturer or 'unknown manufacturer'})")
        time.sleep(0.4)
    return added


# --- Entry point -----------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", choices=["all", "medlineplus", "openfda"], default="all")
    parser.add_argument("--retmax", type=int, default=3,
                        help="MedlinePlus results to keep per search term")
    args = parser.parse_args()

    manifest = load_manifest()
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
