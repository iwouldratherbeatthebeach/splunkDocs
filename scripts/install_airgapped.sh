#!/usr/bin/env bash
#
# install_airgapped.sh — run on the OFFLINE Splunk host (no network needed).
#
# Verifies the bundle's checksums, installs the app into $SPLUNK_HOME/etc/apps,
# and tells you how to finish. The app carries its own NDJSON + PDFs + config,
# so no external paths or dependencies are required.
#
# Usage (as the user that owns your Splunk install, often 'splunk'):
#   SPLUNK_HOME=/opt/splunk scripts/install_airgapped.sh /media/usb/splunk_docs_offline_YYYYMMDD.tar.gz
#
set -euo pipefail

BUNDLE="${1:?usage: install_airgapped.sh <bundle.tar.gz>}"
: "${SPLUNK_HOME:=/opt/splunk}"
APPS="$SPLUNK_HOME/etc/apps"

if [ ! -f "$BUNDLE" ]; then echo "Bundle not found: $BUNDLE" >&2; exit 1; fi
if [ ! -d "$APPS" ]; then echo "SPLUNK_HOME apps dir not found: $APPS (set SPLUNK_HOME)" >&2; exit 1; fi

# portable sha256 -c
if command -v sha256sum >/dev/null 2>&1; then SHACHECK="sha256sum -c"; else SHACHECK="shasum -a 256 -c"; fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[*] Extracting bundle ..."
tar -xzf "$BUNDLE" -C "$TMP"
SRC="$TMP/splunk_docs_search"
[ -d "$SRC" ] || { echo "Bundle does not contain splunk_docs_search/" >&2; exit 1; }

echo "[*] Verifying checksums ..."
if ( cd "$SRC" && $SHACHECK SHA256SUMS >/dev/null 2>&1 ); then
    echo "    checksums OK"
else
    echo "    CHECKSUM MISMATCH — bundle may be corrupt or altered. Aborting." >&2
    exit 1
fi

echo "[*] Manifest:"
sed 's/^/    /' "$SRC/BUNDLE_MANIFEST.txt" 2>/dev/null || true

echo "[*] Installing app -> $APPS/splunk_docs_search"
rm -rf "$APPS/splunk_docs_search"
cp -r "$SRC" "$APPS/splunk_docs_search"

# best-effort ownership match to the apps dir owner
OWNER="$(stat -c '%u:%g' "$APPS" 2>/dev/null || stat -f '%u:%g' "$APPS" 2>/dev/null || echo '')"
[ -n "$OWNER" ] && chown -R "$OWNER" "$APPS/splunk_docs_search" 2>/dev/null || true

echo
echo "[✓] Installed. Finish with a restart so Splunk indexes the docs and serves the PDFs:"
echo "    $SPLUNK_HOME/bin/splunk restart"
echo
echo "Then open  Apps > Splunk Docs Search  and verify:"
echo "    index=splunk_docs | stats count by category"
