#!/usr/bin/env python3
"""
fetch_splunk_docs.py
====================
Crawl the Splunk documentation portal (help.splunk.com), and for every page:

  1. write one clean JSON record (NDJSON) for Splunk to index + search, and
  2. generate a readable PDF of the page that the app embeds inline.

Each NDJSON record carries the page body (searchable) plus metadata lifted from
the page's <meta> tags, a derived `category` (used for grouping/nav), and a
`pdf_file` pointing at the generated PDF.

Why this design
---------------
* help.splunk.com is a Heretto portal; topic pages are server-rendered, so a
  plain `requests` GET returns the full article -- no headless browser needed.
* `Product`/`Version_Number` meta tags are missing on many pages (style guide,
  add-ons, etc.), so we derive a reliable `category` from the URL path instead.
* PDFs are generated locally with reportlab (pure-Python, pip-installable) from
  the cleaned text -- real, self-contained PDFs with no external dependencies.

Usage
-----
    pip install -r requirements.txt

    # Test run: 200 pages, NDJSON to ./data, PDFs straight into the app so the
    # embedded viewer can serve them:
    python fetch_splunk_docs.py \
        --out ./data \
        --pdf-dir ../splunk_docs_search/appserver/static/pdfs \
        --max-pages 200

    # Full crawl (all products), no page cap:
    python fetch_splunk_docs.py --out ./data --pdf-dir ../splunk_docs_search/appserver/static/pdfs

    # Skip PDF generation (NDJSON/search only):
    python fetch_splunk_docs.py --out ./data --no-pdf
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import html
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

# reportlab is optional: if absent we still emit NDJSON, just no PDFs.
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.lib.enums import TA_LEFT
    from reportlab.platypus import (
        SimpleDocTemplate,
        Paragraph,
        Spacer,
        Preformatted,
    )
    HAVE_REPORTLAB = True
except ImportError:
    HAVE_REPORTLAB = False


DEFAULT_BASE = "https://help.splunk.com"
SITEMAP_CANDIDATES = [
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/en/sitemap.xml",
    "/robots.txt",
]
USER_AGENT = (
    "SplunkDocsIngest/2.0 (+internal documentation mirror; contact your Splunk admin)"
)
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}

# URL-substring -> friendly category. Order matters: most specific first.
# These drive the app's product grouping / nav (Search Commands, ES, ITSI, ...).
CATEGORY_RULES = [
    ("search-commands", "Search Commands"),
    ("spl-search-reference", "Search Commands"),
    ("search-reference", "Search Commands"),
    ("splunk-enterprise-security", "Enterprise Security"),
    ("enterprise-security", "Enterprise Security"),
    ("it-service-intelligence", "ITSI"),
    ("splunk-soar", "SOAR"),
    ("/soar", "SOAR"),
    ("user-behavior-analytics", "UBA"),
    ("splunk-cloud-platform", "Cloud Platform"),
    ("observability", "Observability"),
    ("appdynamics", "AppDynamics"),
    ("supported-add-ons", "Add-ons"),
    ("splunk-style-guide", "Style Guide"),
    ("data-management", "Data Management"),
    ("security-offerings", "Security Offerings"),
    ("splunk-enterprise", "Splunk Enterprise"),
]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class Config:
    out_dir: str
    pdf_dir: str = ""
    make_pdf: bool = True
    base_url: str = DEFAULT_BASE
    sitemaps: list[str] = field(default_factory=list)
    include_products: list[str] = field(default_factory=list)
    include_categories: list[str] = field(default_factory=list)
    exclude_substrings: list[str] = field(default_factory=list)
    only_english: bool = True
    workers: int = 4
    delay: float = 0.3
    timeout: int = 30
    retries: int = 3
    max_pages: int = 0
    rotate_bytes: int = 50 * 1024 * 1024
    state_file: str = ""


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    return s


def fetch(session: requests.Session, url: str, cfg: Config) -> requests.Response | None:
    for attempt in range(1, cfg.retries + 1):
        try:
            resp = session.get(url, timeout=cfg.timeout)
            if resp.status_code == 200:
                return resp
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 30))
                continue
            return None
        except requests.RequestException:
            time.sleep(min(2 ** attempt, 30))
    return None


def maybe_gunzip(content: bytes, url: str) -> bytes:
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
            roots = [urljoin(cfg.base_url, "/sitemap.xml")]

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
        if root.tag.lower().endswith("sitemapindex"):
            for loc in root.findall(".//sm:sitemap/sm:loc", SITEMAP_NS):
                if loc.text:
                    work.append(loc.text.strip())
        else:
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
    return [loc.text.strip() for loc in root.findall(".//sm:url/sm:loc", SITEMAP_NS) if loc.text]


def keep_url(url: str, cfg: Config) -> bool:
    p = urlparse(url)
    if urlparse(cfg.base_url).netloc != p.netloc:
        return False
    if cfg.only_english and ("/ja-jp/" in url or "/ja/" in url):
        return False
    for bad in cfg.exclude_substrings:
        if bad in url:
            return False
    return True


# --------------------------------------------------------------------------- #
# Page parsing
# --------------------------------------------------------------------------- #
def meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    if tag and tag.get("content"):
        return tag["content"].strip()
    return ""


CONTENT_SELECTORS = [
    "main", "article", '[role="main"]',
    "div.article", "div.content", "div#content", "div.topic", "div.body",
]
STRIP_TAGS = ["script", "style", "nav", "header", "footer", "noscript", "svg", "form"]


def extract_body(soup: BeautifulSoup) -> str:
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
    for tag in root.find_all(attrs={"class": re.compile(r"(nav|sidebar|breadcrumb|toc|menu|cookie)", re.I)}):
        tag.decompose()
    text = root.get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    cleaned: list[str] = []
    for ln in lines:
        if not cleaned or cleaned[-1] != ln:
            cleaned.append(ln)
    return "\n".join(cleaned)


def breadcrumb_from_url(url: str) -> list[str]:
    parts = [p for p in urlparse(url).path.split("/") if p]
    if parts and parts[0] in ("en", "ja-jp", "ja"):
        parts = parts[1:]
    return parts


def categorize(url: str, product: str) -> str:
    u = url.lower()
    for key, label in CATEGORY_RULES:
        if key in u:
            return label
    if product:
        return product
    crumbs = breadcrumb_from_url(url)
    return crumbs[0].replace("-", " ").title() if crumbs else "Other"


def title_from(soup: BeautifulSoup) -> str:
    if soup.title and soup.title.string:
        t = soup.title.string.strip()
        t = re.split(r"\s*\|\s*", t)[0]
        t = re.sub(r"\s*\(last updated.*?\)\s*$", "", t)
        return t.strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else ""


def pdf_name_for(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + ".pdf"


def parse_page(url: str, html_text: str) -> dict | None:
    soup = BeautifulSoup(html_text, "html.parser")
    body = extract_body(soup)
    if len(body) < 40:
        return None
    product = meta(soup, "Product")
    record = {
        "url": meta(soup, "og:url") or url,
        "title": title_from(soup),
        "category": categorize(url, product),
        "product": product,
        "platform": meta(soup, "Platform"),
        "version": meta(soup, "Version_Number"),
        "genre": meta(soup, "Genre"),
        "content_type": meta(soup, "contentType"),
        "resource_ids": meta(soup, "resource-ids"),
        "section": (breadcrumb_from_url(url)[0] if breadcrumb_from_url(url) else ""),
        "breadcrumb": " / ".join(breadcrumb_from_url(url)),
        "last_modified": meta(soup, "lastModifiedISO") or meta(soup, "article:modified_time"),
        "body": body,
        "pdf_file": pdf_name_for(url),
        "scraped_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    return record


# --------------------------------------------------------------------------- #
# PDF generation (reportlab)
# --------------------------------------------------------------------------- #
_pdf_styles = None


def _styles():
    global _pdf_styles
    if _pdf_styles is None:
        ss = getSampleStyleSheet()
        ss.add(ParagraphStyle(
            name="DocTitle", parent=ss["Title"], fontSize=18, leading=22, spaceAfter=6,
            textColor="#65A637"))  # Splunk green
        ss.add(ParagraphStyle(
            name="Meta", parent=ss["Normal"], fontSize=8, leading=11, textColor="#666666",
            spaceAfter=12))
        ss.add(ParagraphStyle(
            name="DocBody", parent=ss["BodyText"], fontSize=10.5, leading=15,
            alignment=TA_LEFT, spaceAfter=6))
        _pdf_styles = ss
    return _pdf_styles


def _para(text: str) -> str:
    # reportlab Paragraph uses a mini-HTML; escape everything.
    return html.escape(text).replace("\t", "    ")


def write_pdf(record: dict, path: str) -> bool:
    if not HAVE_REPORTLAB:
        return False
    try:
        ss = _styles()
        doc = SimpleDocTemplate(
            path, pagesize=letter,
            leftMargin=0.9 * inch, rightMargin=0.9 * inch,
            topMargin=0.8 * inch, bottomMargin=0.8 * inch,
            title=record.get("title") or "Splunk Doc",
        )
        flow = []
        flow.append(Paragraph(_para(record.get("title") or "Untitled"), ss["DocTitle"]))
        meta_bits = [b for b in [
            record.get("category"),
            record.get("version") and f"v{record['version']}",
            record.get("content_type"),
            record.get("last_modified", "")[:10],
        ] if b]
        if meta_bits:
            flow.append(Paragraph(_para("  •  ".join(meta_bits)), ss["Meta"]))
        if record.get("url"):
            flow.append(Paragraph(
                f'<a href="{html.escape(record["url"])}">{html.escape(record["url"])}</a>',
                ss["Meta"]))
        flow.append(Spacer(1, 6))
        for block in (record.get("body") or "").split("\n"):
            block = block.strip()
            if not block:
                continue
            # crude heading heuristic: short lines with no terminal punctuation
            if len(block) < 70 and not block.endswith((".", ":", ",", ";")):
                flow.append(Paragraph(_para(block), ss["Heading3"]))
            else:
                flow.append(Paragraph(_para(block), ss["DocBody"]))
        doc.build(flow)
        # world-readable so Splunk can serve it regardless of who ran the crawl
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Output shard writer
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
        # world-readable so Splunk's monitor input can read it regardless of
        # which user ran the crawl (root vs splunk vs staging user)
        try:
            os.chmod(path, 0o644)
        except OSError:
            pass
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
# State
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
# Crawl
# --------------------------------------------------------------------------- #
def crawl(cfg: Config):
    if cfg.make_pdf and not HAVE_REPORTLAB:
        print("[!] reportlab not installed -> PDFs disabled. `pip install reportlab` to enable.",
              flush=True)
    if cfg.make_pdf and HAVE_REPORTLAB:
        os.makedirs(cfg.pdf_dir, exist_ok=True)

    session_main = make_session()
    print(f"[*] Discovering sitemaps under {cfg.base_url} ...", flush=True)
    sitemaps = discover_sitemaps(session_main, cfg)
    print(f"[*] {len(sitemaps)} sitemap file(s) found.", flush=True)

    all_urls: list[str] = []
    seen: set[str] = set()
    for sm in sitemaps:
        for u in urls_from_sitemap(session_main, sm, cfg):
            if u not in seen and keep_url(u, cfg):
                seen.add(u)
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

    counters = {"ok": 0, "skip": 0, "fail": 0, "pdf": 0}
    clock = {"last_save": time.time()}
    clock_lock = threading.Lock()

    def wanted(rec: dict) -> bool:
        if cfg.include_products:
            prod = (rec.get("product") or "").lower()
            if not any(p.lower() in prod for p in cfg.include_products):
                return False
        if cfg.include_categories:
            cat = (rec.get("category") or "").lower()
            if not any(c.lower() in cat for c in cfg.include_categories):
                return False
        return True

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
                    if rec is None or not wanted(rec):
                        counters["skip"] += 1
                    else:
                        if cfg.make_pdf and HAVE_REPORTLAB:
                            if write_pdf(rec, os.path.join(cfg.pdf_dir, rec["pdf_file"])):
                                counters["pdf"] += 1
                            else:
                                rec["pdf_file"] = ""  # generation failed -> no viewer link
                        else:
                            rec["pdf_file"] = ""
                        writer.write(rec)
                        counters["ok"] += 1
                done.add(url)
                total = counters["ok"] + counters["skip"] + counters["fail"]
                if total % 25 == 0:
                    print(f"    {total} processed "
                          f"(ok={counters['ok']} pdf={counters['pdf']} "
                          f"skip={counters['skip']} fail={counters['fail']})", flush=True)
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
    print(f"[✓] Done. ok={counters['ok']} pdf={counters['pdf']} "
          f"skipped={counters['skip']} failed={counters['fail']}.", flush=True)
    print(f"    NDJSON -> {cfg.out_dir}", flush=True)
    if cfg.make_pdf and HAVE_REPORTLAB:
        print(f"    PDFs   -> {cfg.pdf_dir}", flush=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_config(argv: list[str]) -> Config:
    ap = argparse.ArgumentParser(description="Crawl Splunk docs into NDJSON + PDFs for Splunk.")
    ap.add_argument("--out", required=True, help="Output directory for NDJSON shards.")
    ap.add_argument("--pdf-dir", default="", help="Directory for generated PDFs "
                    "(default: <out>/pdfs). Point at the app's appserver/static/pdfs "
                    "so the embedded viewer can serve them.")
    ap.add_argument("--no-pdf", action="store_true", help="Do not generate PDFs.")
    ap.add_argument("--base-url", default=DEFAULT_BASE)
    ap.add_argument("--sitemap", action="append", default=[], dest="sitemaps")
    ap.add_argument("--include-product", action="append", default=[], dest="include_products")
    ap.add_argument("--include-category", action="append", default=[], dest="include_categories",
                    help='e.g. "Search Commands", "Enterprise Security", "ITSI", "SOAR", "UBA".')
    ap.add_argument("--exclude", action="append", default=[], dest="exclude_substrings")
    ap.add_argument("--all-languages", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.3)
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--max-pages", type=int, default=0)
    ap.add_argument("--rotate-mb", type=int, default=50)
    ap.add_argument("--state-file", default="")
    a = ap.parse_args(argv)
    return Config(
        out_dir=a.out,
        pdf_dir=a.pdf_dir or os.path.join(a.out, "pdfs"),
        make_pdf=not a.no_pdf,
        base_url=a.base_url.rstrip("/"),
        sitemaps=a.sitemaps,
        include_products=a.include_products,
        include_categories=a.include_categories,
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
