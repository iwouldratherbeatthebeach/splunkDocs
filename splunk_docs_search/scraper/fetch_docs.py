#!/usr/bin/env python3
"""
fetch_docs.py — Splunk documentation offline scraper (HTML topics).

Captures faithful HTML topics from help.splunk.com per product, keeps the N
newest versions of each (configurable in products.yaml), rewrites inter-topic
links + images to local paths, and writes everything the in-app docs browser
and the Splunk index need:

    <data-dir>/
      topics/<hash>.html      one standalone HTML page per topic
      assets/<hash>.<ext>     downloaded images
      assets/topic.css        shared topic stylesheet
      nav.json                product -> version -> section tree
      search_index.json       [{id,title,product,version,file,text}]
      docs.ndjson             one JSON event per topic (for the splunk_docs index)
      status.json             job progress (read by the Configuration page)

Runnable standalone (staging box) or spawned by the app's REST service.

    python3 fetch_docs.py --data-dir ./data --products products.yaml --mode full
    python3 fetch_docs.py --data-dir ./data --mode incremental --limit 500
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests beautifulsoup4")
try:
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependency: pip install requests beautifulsoup4")

BASE = "https://help.splunk.com"
UA = "SplunkDocsOffline/3.0 (internal documentation mirror)"
SITEMAP_CANDIDATES = ["/sitemap.xml", "/sitemap_index.xml", "/robots.txt"]
SM_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
VERSION_RE = re.compile(r"^\d+\.\d+(?:\.\d+)?$")
CONTENT_SELECTORS = ["main", "article", '[role="main"]', "div.content", "div.topic", "div.body"]
STRIP_TAGS = ["script", "style", "nav", "header", "footer", "noscript", "form"]


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def h(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Minimal products.yaml loader (no PyYAML dependency — Splunk python friendly)
# --------------------------------------------------------------------------- #
def load_products(path: str) -> dict:
    defaults = {"versions": 2}
    products: dict[str, dict] = {}
    section = None
    cur = None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.strip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            key, _, val = line.strip().partition(":")
            key = key.strip()
            val = val.strip()
            if " #" in val:  # strip inline comments
                val = val.split(" #", 1)[0].strip()
            if indent == 0:
                section = key
                continue
            if section == "defaults":
                defaults[key] = _coerce(val)
            elif section == "products":
                if indent == 2:  # product id
                    cur = key
                    products[cur] = {}
                elif cur is not None:  # product attribute
                    products[cur][key] = _coerce(val)
    return {"defaults": defaults, "products": products}


def _coerce(val: str):
    if val == "":
        return ""
    if val.startswith("[") and val.endswith("]"):
        inner = val[1:-1].strip()
        return [x.strip() for x in inner.split(",") if x.strip()] if inner else []
    if val.isdigit():
        return int(val)
    if val.lower() in ("all", "none"):
        return val.lower()
    return val


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    return s


def get(s, url, timeout=30, retries=3):
    for attempt in range(1, retries + 1):
        try:
            r = s.get(url, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 20))
                continue
            return None
        except requests.RequestException:
            time.sleep(min(2 ** attempt, 20))
    return None


# --------------------------------------------------------------------------- #
# URL discovery + version filtering
# --------------------------------------------------------------------------- #
def all_sitemap_urls(s) -> list[str]:
    import xml.etree.ElementTree as ET
    roots, leaves, urls = [], [], []
    for path in SITEMAP_CANDIDATES:
        r = get(s, urljoin(BASE, path))
        if not r:
            continue
        if path.endswith("robots.txt"):
            for ln in r.text.splitlines():
                if ln.lower().startswith("sitemap:"):
                    roots.append(ln.split(":", 1)[1].strip())
        else:
            roots.append(urljoin(BASE, path))
    seen = set()
    work = list(dict.fromkeys(roots)) or [urljoin(BASE, "/sitemap.xml")]
    while work:
        sm = work.pop()
        if sm in seen:
            continue
        seen.add(sm)
        r = get(s, sm)
        if not r:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        if root.tag.lower().endswith("sitemapindex"):
            work += [loc.text.strip() for loc in root.findall(".//sm:sitemap/sm:loc", SM_NS) if loc.text]
        else:
            leaves.append(sm)
    for sm in leaves:
        r = get(s, sm)
        if not r:
            continue
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            continue
        urls += [loc.text.strip() for loc in root.findall(".//sm:url/sm:loc", SM_NS) if loc.text]
    return urls


def version_segments(path: str) -> list[str]:
    return [seg for seg in path.split("/") if VERSION_RE.match(seg)]


def vkey(v: str):
    return tuple(int(p) if p.isdigit() else p for p in v.split("."))


def select_versions(urls: list[str], keep) -> list[str]:
    """Filter a product's URLs to the N newest versions (keep unversioned)."""
    if keep == "all":
        return urls
    keep_n = 1 if keep in ("none", "", None) else int(keep)
    found = set()
    for u in urls:
        found.update(version_segments(urlparse(u).path))
    allow = set(sorted(found, key=vkey, reverse=True)[:keep_n])
    out = []
    for u in urls:
        segs = version_segments(urlparse(u).path)
        if not segs or all(s in allow for s in segs):
            out.append(u)
    return out


def product_urls(all_urls: list[str], pid: str, cfg: dict) -> list[str]:
    root = cfg["root_path"].strip("/")
    prefix = f"/en/{root}/"
    excl = cfg.get("exclude_path_contains") or []
    matched = []
    for u in all_urls:
        p = urlparse(u).path
        if not (p.startswith(prefix) or p.rstrip("/").endswith(f"/en/{root}")):
            continue
        if any(x in u for x in excl):
            continue
        if "/ja-jp/" in u or "/ja/" in u:
            continue
        matched.append(u)
    return select_versions(matched, cfg.get("versions", 2))


# --------------------------------------------------------------------------- #
# Page parsing
# --------------------------------------------------------------------------- #
def meta(soup, name):
    t = soup.find("meta", attrs={"name": name}) or soup.find("meta", attrs={"property": name})
    return t["content"].strip() if t and t.get("content") else ""


def title_of(soup):
    if soup.title and soup.title.string:
        t = re.split(r"\s*\|\s*", soup.title.string.strip())[0]
        return re.sub(r"\s*\(last updated.*?\)\s*$", "", t).strip()
    h1 = soup.find("h1")
    return h1.get_text(strip=True) if h1 else "Untitled"


def article_node(soup):
    for sel in CONTENT_SELECTORS:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 150:
            return el
    return soup.body or soup


def breadcrumb(url, root):
    parts = [p for p in urlparse(url).path.split("/") if p]
    if parts and parts[0] == "en":
        parts = parts[1:]
    root_segs = root.strip("/").split("/")
    if parts[: len(root_segs)] == root_segs:
        parts = parts[len(root_segs):]
    return parts  # e.g. ['9.4','search-commands','stats'] or ['welcome']


# --------------------------------------------------------------------------- #
# Scrape
# --------------------------------------------------------------------------- #
class Status:
    def __init__(self, path, app_version="3.0.0"):
        self.path = path
        self.data = {
            "bundle": {"app_version": app_version, "topic_count": 0, "meta": {"last_sync_at": None}},
            "job": {"status": "running", "mode": "", "started_at": now_iso(),
                    "finished_at": None, "error": None, "done": 0, "total": 0, "log_tail": []},
            "settings": {"scraper_root": os.path.dirname(os.path.abspath(__file__))},
        }

    def log(self, msg):
        line = f"{now_iso()} {msg}"
        self.data["job"]["log_tail"] = (self.data["job"]["log_tail"] + [line])[-40:]
        print(line, flush=True)
        self.flush()

    def flush(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
            os.chmod(self.path, 0o644)
        except OSError:
            pass


TOPIC_CSS = """
:root{--fg:#1a1c20;--bg:#fff;--link:#3863A0;--muted:#5b6770;--border:#e3e8ee}
@media (prefers-color-scheme:dark){:root{--fg:#e6e8eb;--bg:#16181c;--link:#7ab0ff;--muted:#9aa7b4;--border:#2a2e35}}
html,body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.topic{max-width:820px;margin:0 auto;padding:28px 32px}
.topic h1,.topic h2,.topic h3{line-height:1.25}
.topic h1{font-size:26px;color:#65A637}
.topic a{color:var(--link);text-decoration:none}
.topic a:hover{text-decoration:underline}
.topic pre,.topic code{background:rgba(127,127,127,.12);border-radius:4px}
.topic pre{padding:12px;overflow:auto}
.topic code{padding:1px 4px}
.topic table{border-collapse:collapse;width:100%;margin:12px 0}
.topic th,.topic td{border:1px solid var(--border);padding:6px 10px;text-align:left;vertical-align:top}
.topic img{max-width:100%;height:auto}
""".strip()


def scrape(cfg, data_dir, mode, limit, status: Status):
    s = session()
    topics_dir = os.path.join(data_dir, "topics")
    assets_dir = os.path.join(data_dir, "assets")
    for d in (topics_dir, assets_dir):
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(assets_dir, "topic.css"), "w", encoding="utf-8") as f:
        f.write(TOPIC_CSS)

    status.log("Discovering doc URLs from sitemaps...")
    sm_urls = all_sitemap_urls(s)
    status.log(f"{len(sm_urls)} URLs in sitemaps")

    # url -> record; build the full target set per product first
    plan = []  # (pid, product_title, url)
    for pid, pcfg in cfg["products"].items():
        purls = product_urls(sm_urls, pid, pcfg)
        for u in purls:
            plan.append((pid, pcfg.get("title", pid), u))
    if limit:
        plan = plan[:limit]
    status.data["job"]["total"] = len(plan)
    status.log(f"{len(plan)} topics planned across {len(cfg['products'])} products")

    url2hash = {u: h(u) for _, _, u in plan}
    captured = {}   # hash -> {rec, article_html}
    ndjson_path = os.path.join(data_dir, "docs.ndjson")
    seen_before = set()
    if mode == "incremental":
        # skip topics we already have an HTML file for
        for _, _, u in plan:
            fp = os.path.join(topics_dir, url2hash[u] + ".html")
            if os.path.exists(fp):
                seen_before.add(u)

    img_cache = {}
    done = 0
    for pid, ptitle, url in plan:
        done += 1
        status.data["job"]["done"] = done
        if url in seen_before:
            continue
        r = get(s, url)
        if not r or "text/html" not in r.headers.get("Content-Type", ""):
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        node = article_node(soup)
        for t in node.find_all(STRIP_TAGS):
            t.decompose()
        for t in node.find_all(attrs={"class": re.compile(r"(sidebar|breadcrumb|toc|cookie|feedback)", re.I)}):
            t.decompose()

        # download images referenced in the article
        for img in node.find_all("img"):
            src = img.get("src")
            if not src:
                continue
            absu = urljoin(url, src)
            if absu not in img_cache:
                ext = os.path.splitext(urlparse(absu).path)[1][:5] or ".img"
                fn = h(absu) + ext
                ir = get(s, absu)
                if ir:
                    with open(os.path.join(assets_dir, fn), "wb") as fh:
                        fh.write(ir.content)
                    try:
                        os.chmod(os.path.join(assets_dir, fn), 0o644)
                    except OSError:
                        pass
                    img_cache[absu] = fn
                else:
                    img_cache[absu] = None
            fn = img_cache.get(absu)
            img["src"] = ("../assets/" + fn) if fn else absu

        crumbs = breadcrumb(url, cfg["products"][pid]["root_path"])
        vsegs = version_segments(urlparse(url).path)
        rec = {
            "id": url2hash[url],
            "url": meta(soup, "og:url") or url,
            "title": title_of(soup),
            "product": pid,
            "product_title": ptitle,
            "version": vsegs[0] if vsegs else (meta(soup, "Version_Number") or ""),
            "content_type": meta(soup, "contentType"),
            "section": crumbs[0] if crumbs else "",
            "breadcrumb": crumbs,
            "last_modified": meta(soup, "lastModifiedISO"),
            "file": url2hash[url] + ".html",
        }
        captured[rec["id"]] = {"rec": rec, "node": node}
        if done % 25 == 0:
            status.log(f"fetched {done}/{len(plan)}")
        time.sleep(0.15)

    # link-rewriting pass: absolute help.splunk.com links we captured -> local
    status.log("Rewriting inter-topic links...")
    for hid, item in captured.items():
        node = item["node"]
        for a in node.find_all("a", href=True):
            absu = urljoin(item["rec"]["url"], a["href"])
            key = absu.split("#")[0]
            frag = absu[len(key):]
            if key in url2hash and url2hash[key] in captured:
                a["href"] = url2hash[key] + ".html" + frag
            elif key in url2hash:
                a["href"] = url2hash[key] + ".html" + frag

    # write topic HTML, search index, ndjson, nav
    status.log("Writing topics, search index, navigation...")
    search_index = []
    nav = {}  # product -> version -> list of {title,file,path}
    with open(ndjson_path, "w", encoding="utf-8") as nf:
        for hid, item in captured.items():
            rec = item["rec"]
            body_html = item["node"].decode_contents() if hasattr(item["node"], "decode_contents") else str(item["node"])
            text = item["node"].get_text(" ", strip=True)
            html_doc = (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<link rel='stylesheet' href='../assets/topic.css'>"
                f"<title>{_esc(rec['title'])}</title></head>"
                f"<body><article class='topic'>{body_html}</article></body></html>"
            )
            fp = os.path.join(topics_dir, rec["file"])
            with open(fp, "w", encoding="utf-8") as tf:
                tf.write(html_doc)
            try:
                os.chmod(fp, 0o644)
            except OSError:
                pass
            search_index.append({
                "id": rec["id"], "title": rec["title"], "product": rec["product"],
                "product_title": rec["product_title"], "version": rec["version"],
                "file": rec["file"], "text": text[:1200],
            })
            nf.write(json.dumps({**{k: rec[k] for k in
                     ("url", "title", "product", "product_title", "version",
                      "content_type", "section", "last_modified", "file")},
                     "body": text}, ensure_ascii=False) + "\n")
            ver = rec["version"] or "—"
            nav.setdefault(rec["product_title"], {}).setdefault(ver, []).append(
                {"title": rec["title"], "file": rec["file"], "section": rec["section"]})

    _write_json(os.path.join(data_dir, "search_index.json"), search_index)
    _write_json(os.path.join(data_dir, "nav.json"), nav)
    try:
        os.chmod(ndjson_path, 0o644)
    except OSError:
        pass

    status.data["bundle"]["topic_count"] = len(captured)
    status.data["bundle"]["meta"]["last_sync_at"] = now_iso()
    status.log(f"Done: {len(captured)} topics written.")


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description="Scrape Splunk docs into offline HTML topics.")
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--products", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "products.yaml"))
    ap.add_argument("--mode", choices=["full", "incremental"], default="full")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--status-file", default="")
    a = ap.parse_args()

    os.makedirs(a.data_dir, exist_ok=True)
    status = Status(a.status_file or os.path.join(a.data_dir, "status.json"))
    status.data["job"]["mode"] = a.mode
    status.flush()
    try:
        cfg = load_products(a.products)
        scrape(cfg, a.data_dir, a.mode, a.limit, status)
        status.data["job"]["status"] = "success"
    except Exception as exc:  # noqa: BLE001
        status.data["job"]["status"] = "error"
        status.data["job"]["error"] = str(exc)
        status.log(f"ERROR: {exc}")
        raise
    finally:
        status.data["job"]["finished_at"] = now_iso()
        status.flush()


if __name__ == "__main__":
    main()
