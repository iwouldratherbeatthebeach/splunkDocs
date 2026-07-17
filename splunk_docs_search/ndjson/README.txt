Crawled documentation events (NDJSON) live here.

The app's inputs.conf monitors this folder:
    [monitor://$SPLUNK_HOME/etc/apps/splunk_docs_search/ndjson]

For the airgapped workflow, the offline bundle ships the crawler's output in
this folder, so installing the app + restarting Splunk indexes everything with
no external paths and no network access.

To populate it, run the crawler with:
    --out <path-to-this-app>/ndjson
(the scripts/build_offline_bundle.sh helper does this for you).
