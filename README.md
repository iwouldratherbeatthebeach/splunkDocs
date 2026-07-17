# Splunk Docs Search

Crawl the Splunk documentation set from **help.splunk.com** into a dedicated
Splunk index, then search it and **read each page as a PDF, embedded right in
the app** — with results grouped by product.

- One JSON event per doc page (full-text searchable via SPL).
- A generated PDF per page, rendered inline in the dashboard.
- A reliable `category` field (Search Commands, Enterprise Security, ITSI,
  SOAR, UBA, Cloud Platform, Add-ons, Style Guide, …) for grouping/filtering.
- Product filter, cascading Version filter, content-type filter, and a
  "Browse by product" nav menu.
- Self-contained and offline-friendly — ideal for airgapped networks.

---

## Repository layout

```
splunk-docs-search/
├── ingest/
│   ├── fetch_splunk_docs.py     # crawler: NDJSON + per-page PDFs
│   ├── requirements.txt         # requests, beautifulsoup4, reportlab
│   └── config.example.sh
├── scripts/
│   ├── build_offline_bundle.sh  # STAGING (online): crawl -> checksummed bundle
│   └── install_airgapped.sh     # OFFLINE Splunk: verify + install bundle
├── splunk_docs_search/          # the Splunk app (self-contained)
│   ├── default/
│   │   ├── app.conf indexes.conf inputs.conf props.conf
│   │   └── data/ui/{nav,views}/…
│   ├── appserver/static/
│   │   ├── pdf_viewer.js  doc_search.css
│   │   └── pdfs/                # generated PDFs served from here
│   ├── ndjson/                  # crawled events; monitored by inputs.conf
│   └── metadata/default.meta
└── splunk_docs_search.spl       # packaged app (skeleton, no bulk data)
```

## How it works

1. `fetch_splunk_docs.py` discovers every doc URL from the portal sitemaps and,
   per page, writes one NDJSON record and generates a PDF (reportlab). Records
   include the searchable `body`, a derived `category`, and a `pdf_file`.
2. A Splunk `monitor://` input ingests the NDJSON into the **`splunk_docs`**
   index. `props.conf` (KV_MODE=json) exposes the fields; the full JSON line is
   the searchable `_raw`.
3. The **Splunk Docs Search** dashboard searches, filters, and renders the
   selected page's PDF inline (`pdf_viewer.js` serves it from the app's static
   dir). Generated files are written world-readable (0644) so Splunk can serve
   them no matter who ran the crawl.

---

## Prerequisites

- **To crawl:** any box with Python 3.7+ and outbound HTTPS to
  help.splunk.com. (Not needed on the Splunk box itself.)
- **To run:** a Splunk Enterprise instance. Splunk has **no native macOS/ARM64
  build**, so on a Mac (incl. Apple Silicon) run Splunk in Docker (below); on a
  Linux box use a normal Splunk install.

---

## Run it on another box — Docker (works on macOS, incl. M-series)

This is the fastest way to stand it up on a fresh machine.

**1. Start Splunk in Docker.** On Apple Silicon, enable Docker Desktop →
Settings → General → *Use Rosetta for x86/amd64 emulation*, then:

```bash
docker run --platform linux/amd64 -d --name splunk \
  -p 8000:8000 \
  -e "SPLUNK_START_ARGS=--accept-license" \
  -e "SPLUNK_PASSWORD=<choose-a-password>" \
  splunk/splunk:latest
docker logs -f splunk        # wait for "Ansible playbook complete"
```

**2. Install the app + crawl, from inside the container.** Open a root shell
(`docker exec -it --user root splunk bash`) and run this as a single block:

```bash
cd /tmp && rm -rf splunkDocs-main repo.tgz && \
curl -sL -o repo.tgz https://github.com/<you>/splunkDocs/archive/refs/heads/main.tar.gz && \
tar xzf repo.tgz && cd splunkDocs-main && \
cp -r splunk_docs_search /opt/splunk/etc/apps/splunk_docs_search && \
/opt/splunk/bin/splunk cmd python3 -m pip install --quiet requests beautifulsoup4 reportlab && \
/opt/splunk/bin/splunk cmd python3 ingest/fetch_splunk_docs.py \
  --out /opt/splunk/etc/apps/splunk_docs_search/ndjson \
  --pdf-dir /opt/splunk/etc/apps/splunk_docs_search/appserver/static/pdfs \
  --max-pages 300
```

> Writing NDJSON into the app's own `ndjson/` folder matches the app's default
> `inputs.conf` monitor path, so no path editing is needed.

For the **full set** (~168k pages, hours-long, resumable), drop `--max-pages`
and run it detached so a closed terminal doesn't kill it:

```bash
docker exec -d --user root splunk bash -c 'cd /tmp/splunkDocs-main && /opt/splunk/bin/splunk cmd python3 ingest/fetch_splunk_docs.py --out /opt/splunk/etc/apps/splunk_docs_search/ndjson --pdf-dir /opt/splunk/etc/apps/splunk_docs_search/appserver/static/pdfs --workers 8 --delay 0.2 > /opt/splunk/crawl.log 2>&1'
docker exec splunk tail -n 5 /opt/splunk/crawl.log      # watch progress
```

**3. Restart so Splunk indexes the data and serves the PDFs:**

```bash
docker restart splunk
```

**4. Open** http://localhost:8000 → **Apps → Splunk Docs Search**. Verify:

```spl
index=splunk_docs | stats count by category
```

## Run it on an existing Splunk box (Linux)

```bash
# 1) crawl on any internet-connected host
cd ingest && pip install -r requirements.txt
python3 fetch_splunk_docs.py \
  --out ./out/ndjson --pdf-dir ./out/pdfs --max-pages 300

# 2) install the app
cp -r splunk_docs_search $SPLUNK_HOME/etc/apps/
cp ./out/ndjson/*.ndjson $SPLUNK_HOME/etc/apps/splunk_docs_search/ndjson/
cp ./out/pdfs/*.pdf      $SPLUNK_HOME/etc/apps/splunk_docs_search/appserver/static/pdfs/

# 3) restart
$SPLUNK_HOME/bin/splunk restart
```

Or install the packaged `splunk_docs_search.spl` via **Apps → Manage Apps →
Install app from file**, then drop your `.ndjson` and `.pdf` files into the two
folders above and restart.

## Airgapped deployment

The app is self-contained (data, PDFs, dashboards, config all inside the app),
so it moves as one package with **no network or dependencies on the offline
side**.

```bash
# On an internet-connected staging host:
scripts/build_offline_bundle.sh                 # full crawl -> dist/splunk_docs_offline_<date>.tar.gz
scripts/build_offline_bundle.sh --max-pages 1000 # smaller test bundle

# Carry the tarball across the gap, then on the offline Splunk host:
SPLUNK_HOME=/opt/splunk scripts/install_airgapped.sh splunk_docs_offline_<date>.tar.gz
$SPLUNK_HOME/bin/splunk restart
```

`install_airgapped.sh` verifies every file against `SHA256SUMS` before
installing. Nothing in the app calls the internet (Splunk's bundled JS + local
PDFs only), so it's safe for disconnected/classified environments.

## Using the app

- **Keywords** searches the full page text; **Product / Content type /
  Version** filter the list (Version cascades from the selected Product).
- **Browse by product** in the top nav jumps straight to a filtered view.
- **Click any row** to render that page's PDF in the Reader pane, with a link
  to the live page.

Useful SPL:

```spl
index=splunk_docs tstats
index=splunk_docs category="Enterprise Security" | stats count by content_type
index=splunk_docs category="Search Commands" title="stats" | table title version url pdf_file
```

## Refreshing

Re-run the crawler (incremental via `.crawl_state.json`) and restart Splunk;
new shards/PDFs are picked up automatically. Good candidate for a weekly cron
(online) or a periodic re-bundle (airgap).

## Troubleshooting

- **PDF opens but is blank / Reader empty:** almost always a file the web tier
  can't read or hasn't picked up yet.
  - New files added after Splunk started aren't served until a **restart** (or
    `http://<host>:8000/en-US/_bump`, then reload with a `?v=2` cache-buster).
  - If you crawled as a different user (e.g. root) on an older build, ownership
    could block reads; this build writes PDFs `0644`, and a
    `chown -R splunk:splunk $SPLUNK_HOME/etc/apps/splunk_docs_search` clears any
    leftovers.
- **Filters/dashboard changes don't take effect:** Splunk caches views. Reload
  via `http://<host>:8000/en-US/debug/refresh` (uses your login session) or
  restart Splunk.
- **`Argument list too long` when listing PDFs:** use `find … -name '*.pdf'`
  instead of a shell `*.pdf` glob (there can be 100k+ files).
- **Blank `version` column:** expected — many pages (Add-ons, Style Guide) carry
  no version in their source metadata; shown as "—".

## Notes & limitations

- **Scale:** serving PDFs from `appserver/static` is fine for tens of thousands
  of files. For the full ~168k-page mirror, serve PDFs from a dedicated web
  server/volume and point `staticBase()` in `pdf_viewer.js` at that base URL.
- PDFs are text-rendered from cleaned page content (reportlab): clean and
  searchable, not pixel-identical to the web page.
- The index defaults to 5 GB / 10-year retention — tune `indexes.conf`.
