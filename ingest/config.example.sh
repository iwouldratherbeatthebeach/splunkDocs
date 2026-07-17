#!/usr/bin/env bash
# Example invocations for fetch_splunk_docs.py (v2: NDJSON + per-page PDFs).
set -euo pipefail

OUT="./data"
# Write PDFs straight into the app so the embedded viewer can serve them:
PDFS="../splunk_docs_search/appserver/static/pdfs"

# 1) Validate the pipeline with a small run first:
python3 fetch_splunk_docs.py --out "$OUT" --pdf-dir "$PDFS" --max-pages 200

# 2) One product category at a time (matches the app's nav grouping):
# python3 fetch_splunk_docs.py --out "$OUT" --pdf-dir "$PDFS" \
#     --include-category "Search Commands"

# 3) Full crawl of everything (large; resumable via .crawl_state.json):
# python3 fetch_splunk_docs.py --out "$OUT" --pdf-dir "$PDFS"

# 4) NDJSON/search only, no PDFs:
# python3 fetch_splunk_docs.py --out "$OUT" --no-pdf
