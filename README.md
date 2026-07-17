# Splunk Docs Search

Ingest the full Splunk documentation set from **help.splunk.com** into a
dedicated Splunk index, then browse and search it with SPL from a purpose-built
dashboard.

This package has two parts:

```
splunk_docs_app/
├── ingest/                     # Python crawler that turns docs into NDJSON
│   ├── fetch_splunk_docs.py
│   ├── requirements.txt
│   └── config.example.sh
└── splunk_docs_search/         # The installable Splunk app
    ├── default/
    │   ├── app.conf
    │   ├── indexes.conf        # creates the splunk_docs index
    │   ├── inputs.conf         # monitors the crawler output dir
    │   ├── props.conf          # NDJSON -> one page per event, JSON fields
    │   └── data/ui/
    │       ├── nav/default.xml
    │       └── views/doc_search.xml
    └── metadata/default.meta
```

## How it works

1. `fetch_splunk_docs.py` discovers every documentation URL from the portal's
   XML sitemap(s), fetches each page, strips navigation/header/footer chrome,
   and writes **one JSON object per page** to newline-delimited JSON (NDJSON)
   files. Each record carries the page body plus metadata lifted straight from
   the page's `<meta>` tags:

   | field | example |
   |-------|---------|
   | `title` | `stats` |
   | `product` | `Splunk Enterprise` |
   | `version` | `9.4.2` |
   | `genre` | `SPL and SPL2 References` |
   | `content_type` | `Topic` |
   | `section` / `breadcrumb` | `splunk-enterprise / search / ... / stats` |
   | `url` | canonical help.splunk.com link |
   | `last_modified` | page's own last-updated timestamp |
   | `body` | cleaned article text |

2. Splunk's `monitor://` input ingests those NDJSON files into the
   **`splunk_docs`** index. `props.conf` breaks one event per line and exposes
   the JSON keys as fields at search time. The full JSON line is the searchable
   `_raw`, so plain keyword search matches anything in a page's body.

3. The **Splunk Docs Search** dashboard lets you type keywords, filter by
   product / version / content type, and click any result to read the page text
   inline (with a link back to the live page).

## Step 1 — Run the crawler

Run this on any machine with Python 3.9+ and outbound access to
`help.splunk.com` (it does **not** have to be the Splunk server):

```bash
cd ingest
pip install -r requirements.txt

# Full crawl of the entire portal (all products). Expect this to take a while
# and produce a few GB of NDJSON; the crawler is polite and resumable.
python fetch_splunk_docs.py --out /opt/splunk/splunk_docs_ingest
```

Useful flags:

```bash
# Smaller test run (first 200 pages):
python fetch_splunk_docs.py --out ./data --max-pages 200

# Limit to specific products:
python fetch_splunk_docs.py --out ./data \
    --include-product "Splunk Enterprise" \
    --include-product "Splunk Cloud Platform"

# Tune politeness / speed:
python fetch_splunk_docs.py --out ./data --workers 4 --delay 0.3
```

The crawler keeps a `.crawl_state.json` in the output directory, so re-running
it resumes where it left off and only fetches new/unseen pages — handy for a
scheduled refresh (e.g. weekly cron).

> **Note on scope & etiquette.** "All Splunk product docs" is large. Start with
> `--max-pages` or `--include-product` to validate the pipeline end to end,
> then remove the cap for the full mirror. Keep `--workers`/`--delay` modest so
> you stay a courteous client of help.splunk.com, and check that your use is
> consistent with Splunk's terms of use before mirroring the whole site.

## Step 2 — Install the app

Two options:

**A. Install the packaged app (`.spl`)** — in Splunk Web go to
**Apps → Manage Apps → Install app from file**, upload `splunk_docs_search.spl`,
then restart.

**B. Copy the raw app directory** — drop it into your apps folder:

```bash
cp -r splunk_docs_search $SPLUNK_HOME/etc/apps/
```

Then edit **`$SPLUNK_HOME/etc/apps/splunk_docs_search/default/inputs.conf`**:

- set the monitor path to the crawler's `--out` directory, and
- change `disabled = true` to `disabled = false`.

```ini
[monitor:///opt/splunk/splunk_docs_ingest]
disabled   = false
index      = splunk_docs
sourcetype = splunk_docs
whitelist  = \.ndjson$
crcSalt    = <SOURCE>
```

Restart Splunk (or reload):

```bash
$SPLUNK_HOME/bin/splunk restart
```

> For production, put local overrides in a `local/` directory rather than
> editing `default/` (standard Splunk practice). A `local/inputs.conf` with
> your real path and `disabled = false` is the cleanest approach.

## Step 3 — Search

Open **Apps → Splunk Docs Search**. The dashboard gives you keyword search plus
product / version / content-type filters, and an inline reader.

Or search directly in SPL:

```spl
# Every page that mentions "tstats"
index=splunk_docs tstats

# stats command page for Enterprise 9.4, newest first
index=splunk_docs title="stats" product="Splunk Enterprise" version=9.4*
| sort - last_modified
| table title version last_modified url

# How many pages per product
index=splunk_docs | stats count by product | sort - count

# Pull the readable body of one page
index=splunk_docs url="https://help.splunk.com/en/splunk-enterprise/search/spl-search-reference/9.4/search-commands/stats"
| head 1 | table body
```

## Refreshing on a schedule

Re-run the crawler on a cron (it's incremental via the state file) and the
monitor input picks up new shards automatically:

```cron
# Refresh Splunk docs every Sunday at 02:00
0 2 * * 0 cd /path/to/ingest && /usr/bin/python3 fetch_splunk_docs.py --out /opt/splunk/splunk_docs_ingest >> /var/log/splunk_docs_crawl.log 2>&1
```

## Notes & limitations

- Pages on help.splunk.com are server-rendered, so no headless browser is
  needed. If Splunk later moves to a fully client-rendered layout, add a
  Playwright fetch step in `fetch_splunk_docs.py` (the parsing stage is already
  isolated in `parse_page`).
- Older docs still on `docs.splunk.com` (legacy MediaWiki) are not crawled by
  default. Point `--base-url`/`--sitemap` at that host to include them; the
  meta-tag extraction is help.splunk.com specific but the body extraction is
  generic.
- The index defaults to 5 GB max and a 10-year retention. Tune `indexes.conf`.
