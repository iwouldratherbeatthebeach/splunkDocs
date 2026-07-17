#!/usr/bin/env bash
# Example invocations for fetch_splunk_docs.py. Copy/edit for your environment.
set -euo pipefail

OUT="./data"   # must match the docker-compose bind mount (./ingest/data)

# 1) Validate the pipeline with a small run first:
python3 fetch_splunk_docs.py --out "$OUT" --max-pages 200

# 2) Full crawl of all products (uncomment when ready):
# python3 fetch_splunk_docs.py --out "$OUT" --workers 4 --delay 0.3

# 3) Just a couple of products:
# python3 fetch_splunk_docs.py --out "$OUT" \
#     --include-product "Splunk Enterprise" \
#     --include-product "Splunk Cloud Platform"
