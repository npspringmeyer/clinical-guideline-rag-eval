# clinical-guideline-rag-eval
A RAG assistant that answers questions over public health guidelines, with a measurable evaluation framework

## Data collection

The existing MedlinePlus and openFDA downloads remain available:

```bash
python scripts/download_data.py
python scripts/download_data.py --source medlineplus --retmax 5
python scripts/download_data.py --source openfda
```

ClinicalTrials.gov API v2 records can be collected separately. By default, the
collector searches hypertension, preeclampsia, gestational diabetes, and type 2
diabetes, with a limit of 100 records per search:

```bash
python scripts/collect_clinicaltrials.py --limit 100
python scripts/collect_clinicaltrials.py \
  --condition hypertension --intervention metformin --limit 50 --page-size 25
python scripts/collect_clinicaltrials.py \
  --condition hypertension --query-param filter.overallStatus=RECRUITING
```

`--condition`, `--intervention`, and `--query-term` are repeatable. Additional
supported API v2 parameters can be passed as `--query-param KEY=VALUE`.
Pagination follows the API's `nextPageToken`; requests have timeouts, bounded
retries, and rate-limit handling. Raw API response snapshots use content-derived
filenames and are retained under
`data/raw/clinicaltrials/`, and normalized documents using the shared schema
are written under `data/processed/clinicaltrials/`.

UMLS terminology enrichment is optional and uses the REST API only. Set
`UMLS_API_KEY` in the environment (never in a checked-in file) before running:

```bash
export UMLS_API_KEY='<your UMLS API key>'
python scripts/enrich_umls.py --query hypertension --query preeclampsia
python scripts/enrich_umls.py --query 'high blood pressure' \
  --include-synonyms --include-relationships --expand
```

Use `--release` to choose a UMLS release (default: `current`), and
`--document-id` to associate results with source document IDs. Exact UMLS API
JSON responses are saved under `data/raw/umls/` using filenames derived from
the request parameters and response content; normalized terminology output
is a separate enrichment layer at `data/processed/umls/enrichment.jsonl`. It
retains the original query, all returned candidate CUIs, preferred terms,
semantic types, terminology sources, and the release. Raw response filenames
are derived from the endpoint, a hash of non-secret request parameters, and a
hash of response content; the API key is not written to disk. Query expansion
is opt-in and never replaces the original query or resolves ambiguity by
selecting a single candidate.
UMLS terminology is metadata, not clinical evidence. Access, storage, and
redistribution of UMLS data and derivatives are subject to the UMLS license
and the licenses of the source vocabularies available to the account. Raw and
processed outputs are git-ignored; review rights before sharing either.

## Document and provenance layout

`src/models/document.py` defines the common normalized document fields:
stable `doc_id`, source, document type, title, section, source URL, retrieval
timestamp, source update date, source version, content, metadata, and a
SHA-256 content hash. All evidence-source normalized documents use this schema
as JSON under `data/processed/<source>/`; `data/raw/` holds source responses
(MedlinePlus XML, openFDA label JSON, and API responses). `data/manifest.csv`
keeps its original columns and adds
the standardized type, section, update date, raw path, content hash, and
source-specific metadata columns. The legacy `sha256` column remains for
compatibility. The downloader migrates existing openFDA text files and the
cached MedlinePlus query XML into the processed schema and manifest without
refetching those records. New MedlinePlus query snapshots and openFDA label
records also use content-derived filenames so later downloads do not overwrite
raw inputs referenced by earlier manifest rows. MedlinePlus captures did not
retain an API retrieval timestamp, so their migration records the local XML
file modification time as the capture-time estimate and leaves the source
update date blank (the source `date-created` attribute is preserved as
metadata, not misrepresented as an update date).

Clinical trial registrations and any reported registry results are explicitly
identified as registry information, not published evidence or proof of
treatment effectiveness. UMLS results are stored separately and must not be
treated as evidence documents. The four public-source integrations can
therefore be inspected and validated before any embedding or indexing work.

## Tests

Run deterministic mocked API tests with the Python standard library:

```bash
python -m unittest discover -s tests -v
```

These tests do not make live API calls. Live UMLS collection requires a valid
`UMLS_API_KEY`; the ClinicalTrials.gov API is public but live collection is a
separate, explicit command.
