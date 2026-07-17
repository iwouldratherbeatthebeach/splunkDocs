#!/usr/bin/env bash
#
# build_offline_bundle.sh — run on an INTERNET-CONNECTED staging host.
#
# Crawls the Splunk docs, writes NDJSON + per-page PDFs *into the app*, generates
# a checksum manifest, and packages one self-contained tarball to carry across
# the air gap. The offline Splunk host then installs it with install_airgapped.sh.
#
# Usage:
#   scripts/build_offline_bundle.sh                 # full crawl (all docs)
#   scripts/build_offline_bundle.sh --max-pages 500 # smaller test bundle
#   scripts/build_offline_bundle.sh --include-category "Search Commands"
#
# Any extra args are passed straight through to the crawler.
#
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP="$REPO/splunk_docs_search"
NDJSON="$APP/ndjson"
PDFS="$APP/appserver/static/pdfs"
DIST="$REPO/dist"
STAMP="$(date +%Y%m%d)"
BUNDLE="$DIST/splunk_docs_offline_${STAMP}.tar.gz"

# portable sha256 command (Linux: sha256sum, macOS: shasum -a 256)
if command -v sha256sum >/dev/null 2>&1; then SHACMD="sha256sum"; else SHACMD="shasum -a 256"; fi

mkdir -p "$NDJSON" "$PDFS" "$DIST"

echo "[*] Installing crawler dependencies (staging host only)..."
python3 -m pip install -r "$REPO/ingest/requirements.txt"

echo "[*] Crawling help.splunk.com -> NDJSON + PDFs inside the app ..."
echo "    (the full set is ~168k pages and can take hours; it is resumable)"
python3 "$REPO/ingest/fetch_splunk_docs.py" \
    --out "$NDJSON" \
    --pdf-dir "$PDFS" \
    "$@"

echo "[*] Writing bundle manifest ..."
EVENTS=$(cat "$NDJSON"/*.ndjson 2>/dev/null | wc -l | tr -d ' ')
PDFCOUNT=$(find "$PDFS" -name '*.pdf' 2>/dev/null | wc -l | tr -d ' ')
cat > "$APP/BUNDLE_MANIFEST.txt" <<EOF
Splunk Docs Search — offline bundle
built_utc:     $(date -u +%Y-%m-%dT%H:%M:%SZ)
crawler_args:  ${*:-<full crawl>}
ndjson_events: ${EVENTS}
pdf_files:     ${PDFCOUNT}
EOF
cat "$APP/BUNDLE_MANIFEST.txt"

echo "[*] Generating SHA256SUMS ..."
( cd "$APP" && find . -type f ! -name 'SHA256SUMS' -print0 \
    | xargs -0 $SHACMD > SHA256SUMS )

echo "[*] Packaging -> $BUNDLE"
tar -czf "$BUNDLE" -C "$REPO" splunk_docs_search

echo
echo "[✓] Bundle ready:"
echo "    $BUNDLE"
echo "    events=${EVENTS} pdfs=${PDFCOUNT}"
echo
echo "Next: transfer that tarball across the air gap, then on the offline"
echo "Splunk host run:  scripts/install_airgapped.sh <bundle.tar.gz>"
