# Splunk Docs Search (v3 — offline HTML docs browser)

Browse Splunk product documentation **inside Splunk, offline** — faithful HTML
topics with product tabs, a **version picker (keep N newest, configurable)**,
client-side search, and dark mode — plus a **Configuration page** to download or
update the docs from help.splunk.com and watch progress live. Docs are also
indexed for SPL search.

Design inspired by the open-source `gosplunk/splunk-offline-docs` (Apache-2.0,
Joe Hagan), rebuilt here in this app.

## Layout

```
splunk_docs_search/               # the Splunk app
├── appserver/static/
│   ├── docs.html/js/css          # offline docs browser (tabs, versions, search, dark mode)
│   ├── config.html/js            # download/update page (calls the REST backend)
│   ├── about.html                # about page
│   ├── *_shell.js                # embed static pages into the dashboard views
│   └── docdata/                  # (generated) topics/, assets/, nav.json, search_index.json, docs.ndjson
├── bin/
│   ├── docs_handler.py           # persistent REST handler (/docs_admin)
│   └── docs_service.py           # status, launch scraper, update-check, settings
├── scraper/
│   ├── fetch_docs.py             # the HTML-topic scraper (runnable standalone)
│   ├── products.yaml             # which products + how many versions
│   └── requirements.txt          # requests, beautifulsoup4
├── default/                      # app.conf, restmap.conf, web.conf, indexes/inputs/props, views, nav
└── metadata/default.meta
```

## How it works

The scraper fetches topics from help.splunk.com per `products.yaml`, keeps the
newest N versions of each product, downloads images, rewrites inter-topic links
to local paths, and writes into `appserver/static/docdata/`:

- `topics/<hash>.html` — one standalone HTML page per topic
- `assets/` — images + shared `topic.css`
- `nav.json` — product → version → topic list (drives the browser)
- `search_index.json` — client-side search
- `docs.ndjson` — one event per topic for the **`splunk_docs`** index (SPL Search)

The **Documentation** view renders the browser; **Configuration** triggers the
scraper and shows status; **SPL Search** is the indexed full-text view.

## Getting the docs in — two ways

### A. From the app (Splunk host has internet)

1. Ensure the scraper's deps are available to the interpreter Splunk will use:
   ```bash
   $SPLUNK_HOME/bin/splunk cmd python3 -m pip install requests beautifulsoup4
   ```
2. Open **Splunk Docs Search → Configuration** and click **Download / update
   (incremental)** or **Full refresh**. Progress and a live log appear on the
   page; the docs browser fills in as topics land.
   - Restart Splunk once after a large first download so the web tier serves all
     the new static topic files.

### B. Standalone scraper (air-gapped)

On an internet-connected staging host, populate the app's `docdata/`, then move
the whole app across the gap:

```bash
cd splunk_docs_search/scraper
pip install -r requirements.txt
python3 fetch_docs.py --data-dir ../appserver/static/docdata --mode full
# (optional smaller run: add --limit 500)
```

Then copy `splunk_docs_search/` into `$SPLUNK_HOME/etc/apps/` on the offline
box, `chown -R splunk:splunk`, and restart. Everything (topics, images, nav,
search index, index feed) travels inside the app — no internet needed to use it.

## Configure coverage / versions

Edit `scraper/products.yaml`:

```yaml
products:
  es8:
    title: Enterprise Security
    root_path: splunk-enterprise-security-8
    versions: all        # all | <N newest>
  itsi:
    title: IT Service Intelligence
    root_path: splunk-it-service-intelligence
    versions: 2
```

`versions: N` keeps the N newest version branches; `all` keeps every version;
unversioned topics are always kept.

## Install

```bash
cp -r splunk_docs_search $SPLUNK_HOME/etc/apps/
chown -R splunk:splunk $SPLUNK_HOME/etc/apps/splunk_docs_search
$SPLUNK_HOME/bin/splunk restart
```

Requires **Splunk Enterprise (on-prem)** — the Configuration page uses a custom
REST endpoint, which Splunk Cloud does not permit. The `splunk_docs_search.spl`
package installs the app shell (no bundled docs); populate `docdata/` via A or B.

## Status of this build

- Verified here: Python compiles; REST handler dispatch (status/update/check/
  settings) returns correctly; all views/conf/JS are syntactically valid; the
  scraper's config parsing, version filter, link rewriting, nav/search/NDJSON
  output all work against fixtures.
- Needs on-Splunk testing (no Splunk in the build environment): the live REST
  round-trip from the Configuration page, the dashboard iframe embedding, and a
  real end-to-end scrape. Treat as v3.0 — solid foundation, expect small
  tweaks when you first run it on your instance.

## Troubleshooting

- **Config page can't reach backend:** confirm on-prem Splunk + admin role;
  `restmap.conf`/`web.conf` register `/docs_admin`. Check
  `$SPLUNK_HOME/var/log/splunk/splunkd.log` for handler errors.
- **Download starts but no topics:** the interpreter in **Settings → python**
  (defaults to splunkd's) needs `requests`/`beautifulsoup4`; see step A.1.
- **New topics don't render:** restart Splunk (static files are registered at
  startup), then hard-refresh the browser.
- **Blank version column / dropdown:** some pages have no version in metadata;
  they group under "Unversioned".
