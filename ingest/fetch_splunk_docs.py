#!/usr/bin/env python3
"""
fetch_splunk_docs.py
====================
Crawl the Splunk documentation portal (help.splunk.com) and emit one clean
JSON object per documentation page as newline-delimited JSON (NDJSON). The
output is written to a directory that a Splunk `monitor://` input ingests into
the `splunk_docs` index, where it can be displayed and searched with SPL.

Why this design
---------------
* help.splunk.com is a Heretto-generated portal. Individual topic pages are
  server-rendered (the full article HTML is present in the initial response),
  so a plain `requests` GET is enough -- no headless browser required.
* Every topic page carries useful <meta> tags we lift directly:
      Product, Platform, Version_Number, Genre, contentType, lastModifiedISO,
      resource-ids
  plus a canonical og:url and <title>.
* URLs are discovered from the site's XML sitemap(s), which may be gzipped and
  nested behind a sitemap index. Both cases are handled.

The crawler is polite (rate limited), resumable (keeps a state file of URLs it
has already processed), and resilient (retries with backoff, skips failures
without aborting the run).

Usage
-----
    pip install -r requirements.txt

    # Full crawl of the whole portal:
    python fetch_splunk_docs.py --out ./data

    # Limit to particular products / a smaller test run:
    python fetch_splunk_docs.py --out ./data \
        --include-product "Splunk Enterprise" --include-product "Splunk SOAR" \
        --max-pages 200

    # Seed from an explicit list of sitemaps or URLs instead of auto-discovery:
    python fetch_splunk_docs.py --out ./data --sitemap https://help.splunk.com/sitemap.xml

Point the Splunk monitor input at the --out directory (see the app's
inputs.conf) and Splunk will index each page as an event.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import queue
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: run `pip install -r requirements.txt`")

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: run `pip install -r requirements.txt`")


DEFAULT_BASE = "https://help.splunk.com"
# Candidate sitemap locations to try when none is supplied explicitly.
SITEMAP_CANDIDATES = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/en/sitemap.xml",
    "/robots.txt",  # parsed for Sitemap: lines
]
USER_AGENT = (
    "SplunkDocsIngest/1.0 (+internal documentation mirror; contact your Splunk admin)"
)
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    out_dir: str
    base_url: str = DEFAULT_BASE
    sitemaps: list[str] = field(default_factory=list)
    include_products: list[str] = field(default_factory=list)
    exclude_substrings: list[str] = field(default_factory=list)
    only_english: bool = True
    workers: int = 4
    delay: float = 0.3               # seconds between requests per worker
    timeout: int = 30
    retries: int = 3
    max_pages: int = 0               # 0 == unlimited
    rotate_bytes: int = 50 * 1024 * 1024  # 50 MB per NDJSON shard
    state_file: str = ""             # defaults to <out>/.crawl_state.json


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    return s


def fetch(session: requests.Session, url: str, cfg: Config) -> requests.Response | None:
    """GET a URL with retry/backoff. Returns Response or None on failure."""
    for attempt in range(1, cfg.retries + 1):
        try:
            resp = session.get(url, timeout=cfg.timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 30))
                continue
            # 404 / 403 etc -- do not retry
            return None
        except requests.RequestException:
            time.sleep(min(2 ** attempt, 30))
    return None


def maybe_gunzip(content: bytes, url: str) -> bytes:
    """Transparently decompress gzipped sitemaps (.xml.gz or gzip magic bytes)."""
    if url.endswith(".gz") or content[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(content)
        except OSError:
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(content)) as gz:
                    return gz.read()
            except OSError:
                return content
    return content


# --------------------------------------------------------------------------- #
# Sitemap discovery
# --------------------------------------------------------------------------- #
def discover_sitemaps(session: requests.Session, cfg: Config) -> list[str]:
    """Return a list of sitemap URLs, following robots.txt and sitemap indexes."""
    if cfg.sitemaps:
        roots = list(cfg.sitemaps)
    else:
        roots = []
        for path in SITEMAP_CANDIDATES:
            url = urljoin(cfg.base_url, path)
            resp = fetch(session, url, cfg)
            if not resp:
                continue
            if path.endswith("robots.txt"):
                for line in resp.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        roots.append(line.split(":", 1)[1].strip())
            else:
                roots.append(url)
        if not roots:
            # Last resort: assume the standard location.
            roots = [urljoin(cfg.base_url, "/sitemap.xml")]

    # Expand sitemap indexes into leaf sitemaps.
    leaves: list[str] = []
    seen: set[str] = set()
    work = list(dict.fromkeys(roots))
    while work:
        sm = work.pop()
        if sm in seen:
            continue
        seen.add(sm)
        resp = fetch(session, sm, cfg)
        if not resp:
            continue
        xml = maybe_gunzip(resp.content, sm)
        try:
            root = ET.fromstring(xml)
        except ET.ParseError:
            continue
        tag = root.tag.lower()
        if tag.endswith("sitemapindex"):
            for loc in root.findall(".//sm:sitemap/sm:loc", SITEMAP_NS):
                if loc.text:
                    work.append(loc.text.strip())
        else:  # urlset
            leaves.append(sm)
    return leaves or roots


def urls_from_sitemap(session: requests.Session, sm_url: str, cfg: Config) -> list[str]:
    resp = fetch(session, sm_url, cfg)
    if not resp:
        return []
    xml = maybe_gunzip(resp.content, sm_url)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for loc in root.findall(".//sm:url/sm:loc", SITEMAP_NS):
        if loc.text:
            out.append(loc.text.strip())
    return out


def keep_url(url: str, cfg: Config) -> bool:
    p = urlparse(url)
    if cfg.base_url not in f"{p.scheme}://{p.netloc}":
        # only crawl within the docs host
        if urlparse(cfg.base_url).netloc != p.netloc:
            return False
    if cfg.only_english and "/en/" not in url and not url.rstrip("/").endswith("/en"):
        # help.splunk.com English pages live under /en/
        if "/ja-jp/" in url or "/ja/" in url:
            return False
    for bad in cfg.exclude_substrings:
        if bad in url:
            return False
    return True


# --------------------------------------------------------------------------- #
# Page parsing
# --------------------------------------------------------------------------- #
def meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name}) or soup.find(
        "meta", attrs={"property": name}
    )
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


CONTENT_SELECTORS = [
    "main",
    "article",
    '[role="main"]',
    "div.article",
    "div.content",
    "div#content",
    "div.topic",
    "div.body",
]

STRIP_TAGS = ["script", "style", "nav", "header", "footer", "noscript", "svg", "form"]


def extract_body(soup: BeautifulSoup) -> str:
    """Pull the human-readable article text, dropping chrome/navigation."""
    root = None
    for sel in CONTENT_SELECTORS:
        found = soup.select_one(sel)
        if found and len(found.get_text(strip=True)) > 200:
            root = found
            break
    if root is None:
        root = soup.body or soup

    for tag in root.find_all(STRIP_TAGS):
        tag.decompose()
    # Remove obvious side navigation / breadcrumb blocks by class hints.
    for tag in root.find_all(attrs={"class": re.compile(r"(nav|sidebar|breadcrumb|toc|menu|cookie)", re.I)}):
        tag.decompose()

    text = root.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    # collapse runs of duplicate lines that portals emit
    cleaned: list[str] = []
    for ln in lines:
        if not cleaned or cleaned[-1] != ln:
            cleaned.append(ln)
    return "\n".join(cleaned)


def breadcrumb_from_url(url: str) -> list[str]:
    parts = [p for p in urlparse(url).path.split("/") if p]
    # drop leading language code
    if parts and parts[0] in ("en", "ja-jp", "ja"):
        parts = parts[1:]
    return parts


def title_from(soup: BeautifulSoup) -> str:
    if soup.title and soup.title.string:
        # portal titles look like "stats | Splunk Enterprise (last updated ...)"
        t = soup.title.string.strip()
        t = re.split(r"\s*\|\s*", t)[0]
        t = re.sub(r"\s*\(last updated.*?\)\s*$", "", t)
        return t.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def parse_page(url: str, html: str) -> dict | None:
    soup = BeautifulSoup(html, "html.parser")
    body = extract_body(soup)
    if len(body) < 40:
        return None  # empty / redirect shell -- skip

    last_mod_iso = meta(soup, "lastModifiedISO") or meta(soup, "article:modified_time")
    crumbs = breadcrumb_from_url(url)
    record = {
        "url": meta(soup, "og:url") or url,
        "title": title_from(soup),
        "product": meta(soup, "Product"),
        "platform": meta(soup, "Platform"),
        "version": meta(soup, "Version_Number"),
        "genre": meta(soup, "Genre"),
        "content_type": meta(soup, "contentType"),
        "resource_ids": meta(soup, "resource-ids"),
        "section": crumbs[0] if crumbs else "",
        "breadcrumb": " / ".join(crumbs),
        "last_modified": last_mod_iso,
        "body": body,
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    return record


# --------------------------------------------------------------------------- #
# Output shard writer (thread-safe, size-rotating NDJSON)
# --------------------------------------------------------------------------- #
class ShardWriter:
    def __init__(self, out_dir: str, rotate_bytes: int):
        self.out_dir = out_dir
        self.rotate_bytes = rotate_bytes
        self.lock = threading.Lock()
        self.idx = 0
        self.fh = None
        self.bytes_written = 0
        os.makedirs(out_dir, exist_ok=True)
        self._open_new()

    def _open_new(self):
        if self.fh:
            self.fh.close()
        self.idx += 1
        ts = datetime.now(timezone.utc).strftime("%Y%m%d")
        path = os.path.join(self.out_dir, f"splunk_docs_{ts}_{self.idx:04d}.ndjson")
        self.fh = open(path, "w", encoding="utf-8")
        self.bytes_written = 0

    def write(self, record: dict):
        line = json.dumps(record, ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        with self.lock:
            if self.bytes_written and self.bytes_written + len(data) > self.rotate_bytes:
                self._open_new()
            self.fh.write(line)
            self.bytes_written += len(data)

    def close(self):
        with self.lock:
            if self.fh:
                self.fh.close()


# --------------------------------------------------------------------------- #
# State (resume support)
# --------------------------------------------------------------------------- #
def load_state(path: str) -> set[str]:
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_state(path: str, done: set[str]):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(sorted(done), f)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Crawl orchestration
# --------------------------------------------------------------------------- #
def crawl(cfg: Config):
    session_main = make_session()
    print(f"[*] Discovering sitemaps under {cfg.base_url} ...", flush=True)
    sitemaps = discover_sitemaps(session_main, cfg)
    print(f"[*] {len(sitemaps)} sitemap file(s) found.", flush=True)

    all_urls: list[str] = []
    seen_urls: set[str] = set()
    for sm in sitemaps:
        for u in urls_from_sitemap(session_main, sm, cfg):
            if u not in seen_urls and keep_url(u, cfg):
                seen_urls.add(u)
                all_urls.append(u)
    print(f"[*] {len(all_urls)} candidate doc URLs after filtering.", flush=True)

    state_path = cfg.state_file or os.path.join(cfg.out_dir, ".crawl_state.json")
    done = load_state(state_path)
    todo = [u for u in all_urls if u not in done]
    if cfg.max_pages:
        todo = todo[: cfg.max_pages]
    print(f"[*] {len(todo)} to fetch this run ({len(done)} already done).", flush=True)

    writer = ShardWriter(cfg.out_dir, cfg.rotate_bytes)
    q: "queue.Queue[str]" = queue.Queue()
    for u in todo:
        q.put(u)

    counters = {"ok": 0, "skip": 0, "fail": 0}
    clock = {"last_save": time.time()}
    clock_lock = threading.Lock()

    def product_ok(rec: dict) -> bool:
        if not cfg.include_products:
            return True
        prod = (rec.get("product") or "").lower()
        return any(p.lower() in prod for p in cfg.include_products)

    def worker():
        session = make_session()
        while True:
            try:
                url = q.get_nowait()
            except queue.Empty:
                return
            try:
                resp = fetch(session, url, cfg)
                if not resp or "text/html" not in resp.headers.get("Content-Type", ""):
                    counters["fail"] += 1
                else:
                    rec = parse_page(url, resp.text)
                    if rec is None or not product_ok(rec):
                        counters["skip"] += 1
                    else:
                        writer.write(rec)
                        counters["ok"] += 1
                done.add(url)
                total = counters["ok"] + counters["skip"] + counters["fail"]
                if total % 25 == 0:
                    print(
                        f"    {total} processed "
                        f"(ok={counters['ok']} skip={counters['skip']} fail={counters['fail']})",
                        flush=True,
                    )
                with clock_lock:
                    if time.time() - clock["last_save"] > 30:
                        save_state(state_path, done)
                        clock["last_save"] = time.time()
                time.sleep(cfg.delay)
            finally:
                q.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(cfg.workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    writer.close()
    save_state(state_path, done)
    print(
        f"[✓] Done. ok={counters['ok']} skipped={counters['skip']} "
        f"failed={counters['fail']}. NDJSON in {cfg.out_dir}",
        flush=True,
    )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_config(argv: list[str]) -> Config:
    ap = argparse.ArgumentParser(description="Crawl Splunk docs into NDJSON for Splunk ingest.")
    ap.add_argument("--out", required=True, help="Output directory for NDJSON shards.")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--sitemap", action="append", default=[], dest="sitemaps",
                    help="Explicit sitemap URL (repeatable). Skips auto-discovery.")
    ap.add_argument("--include-product", action="append", default=[], dest="include_products",
                    help="Only keep pages whose Product meta contains this (repeatable).")
    ap.add_argument("--exclude", action="append", default=[], dest="exclude_substrings",
                    help="Skip URLs containing this substring (repeatable).")
    ap.add_argument("--all-languages", action="store_true",
                    help="Include non-English pages (default: English only).")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.3, help="Per-worker delay between requests (s).")
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--max-pages", type=int, default=0, help="Cap pages this run (0 = all).")
    ap.add_argument("--rotate-mb", type=int, default=50, help="Rotate NDJSON shard at this size.")
    ap.add_argument("--state-file", default="")
    a = ap.parse_args(argv)
    return Config(
        out_dir=a.out,
        base_url=a.base_url.rstrip("/"),
        sitemaps=a.sitemaps,
        include_products=a.include_products,
        exclude_substrings=a.exclude_substrings,
        only_english=not a.all_languages,
        workers=a.workers,
        delay=a.delay,
        timeout=a.timeout,
        retries=a.retries,
        max_pages=a.max_pages,
        rotate_bytes=a.rotate_mb * 1024 * 1024,
        state_file=a.state_file,
    )


def main():
    cfg = build_config(sys.argv[1:])
    os.makedirs(cfg.out_dir, exist_ok=True)
    crawl(cfg)


if __name__ == "__main__":
    main()
