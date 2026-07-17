Generated PDFs live here.

The crawler writes one PDF per doc page into this folder when you run it with:

    --pdf-dir <path-to-this-app>/appserver/static/pdfs

Splunk serves them as app static assets at:

    /<locale>/static/app/splunk_docs_search/pdfs/<file>.pdf

which is exactly what pdf_viewer.js loads into the embedded reader.

Notes:
- After adding a large batch of PDFs, restart Splunk (or bump the app asset
  version) so the web tier serves the new files.
- appserver/static is fine for tens of thousands of files for local use; for a
  full 168k-page mirror consider serving PDFs from a dedicated web server or
  volume and pointing pdf_viewer.js at that base URL instead.
