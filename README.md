# Splunk Docs Search (v2)

Crawl the Splunk documentation set from **help.splunk.com** into a dedicated
Splunk index, then search it and **read the whole page as a PDF, embedded right
in the app** — with results grouped by product.

What's new in v2:

- Generates a **PDF for every page** and displays it inline in an embedded
  reader (no leaving Splunk).
- Adds a reliable **`category`** field (Search Commands, Enterprise Security,
  ITSI, SOAR, UBA, Cloud Platform, Add-ons, Style Guide, …) derived from the
  URL, since Splunk's `Product` meta tag is blank on many pages.
- **Nav bar grouped by product** ("Browse by product") plus a Product filter.
- Cleaner UI; the old "purple square" product column and line-by-line reader
  are gone.

```
splunk-docs-search/
├── ingest/
│   ├── fetch_splunk_docs.py     # crawler: NDJSON + per-page PDFs
│   ├── requirements.txt         # requests, beautifulsoup4, reportlab
│   └── config.example.sh
└── splunk_docs_search/          # the Splunk app
    ├── default/
    │   ├── app.conf indexes.conf inputs.conf props.conf
    │   └── data/ui/
    │       ├── nav/default.xml           # grouped-by-product nav
    │       └── views/doc_search.xml       # search + embedded PDF reader
    ├── appserver/static/
    │   ├── pdf_viewer.js          # injects the inline PDF iframe
    │   ├── doc_search.css         # styling
    │   └── pdfs/                  # generated PDFs are served from here
    └── metadata/default.meta
```

## How it works

1. `fetch_splunk_docs.py` discovers every doc URL from the portal sitemaps and,
   for each page, (a) writes one JSON record (NDJSON) and (b) generates a clean
   PDF with `reportlab`. Each record includes the searchable `body`, a derived
   `category`, and a `pdf_file` name.
2. A Splunk `monitor://` input ingests the NDJSON into the **`splunk_docs`**
   index. `props.conf` (KV_MODE=json) exposes `category`, `title`, `url`,
   `version`, `pdf_file`, etc. as fields; the full JSON line is the searchable
   `_raw`.
3. The **Splunk Docs Search** dashboard lets you keyword-search, filter by
   product/type/version (or pick a product from the nav bar), and click any
   result to render its full PDF inline. PDFs are served as app static assets
   from `appserver/static/pdfs/` by `pdf_viewer.js`.

## Step 1 — Crawl (writes NDJSON + PDFs)

Run on any machine with Python 3.7+ and access to help.splunk.com:

```bash
cd ingest
pip install -r requirements.txt

# Test run: 200 pages. PDFs go straight into the app so the viewer can serve them.
python fetch_splunk_docs.py \
  --out ./data \
  --pdf-dir ../splunk_docs_search/appserver/static/pdfs \
  --max-pages 200
```

Handy flags: `--include-category "Search Commands"` (one product group),
`--no-pdf` (search only), no `--max-pages` for the full mirror (large, and
resumable via `.crawl_state.json`).

## Step 2 — Install the app

**A. Packaged app (`.spl`)** — Splunk Web → **Apps → Manage Apps → Install app
from file**, upload `splunk_docs_search.spl`, restart. (If you installed the
`.spl`, put your generated PDFs into
`$SPLUNK_HOME/etc/apps/splunk_docs_search/appserver/static/pdfs/`.)

**B. Raw app directory:**

```bash
cp -r splunk_docs_search $SPLUNK_HOME/etc/apps/
```

Then edit `default/inputs.conf` (or add `local/inputs.conf`): point the
`monitor://` path at your crawler `--out` dir and set `disabled = false`.
Restart Splunk:

```bash
$SPLUNK_HOME/bin/splunk restart
```

> After adding a big batch of PDFs to `appserver/static/pdfs/`, restart Splunk
> so the web tier serves the new files.

## Step 3 — Search & read

Open **Apps → Splunk Docs Search**. Use the Product dropdown or the "Browse by
product" nav menu, search keywords, and click a row to read its PDF inline.

SPL examples:

```spl
index=splunk_docs tstats
index=splunk_docs category="Enterprise Security" | stats count by content_type
index=splunk_docs category="Search Commands" title="stats" | table title version url pdf_file
```

## Notes & limitations

- **PDF display** uses an `<iframe>` injected by `pdf_viewer.js` into the
  dashboard (Simple XML strips iframes from static HTML, so JS does it). PDFs
  must live under the app's `appserver/static/pdfs/`.
- **Scale:** `appserver/static` is fine for tens of thousands of PDFs locally.
  For the full ~168k-page mirror, serve PDFs from a dedicated web server/volume
  and point `staticBase()` in `pdf_viewer.js` at that base URL.
- PDFs are text-rendered from the cleaned page content (reportlab), so they're
  clean and searchable-in-viewer but not pixel-identical to the web page.
- The index defaults to 5 GB / 10-year retention — tune `indexes.conf`.
