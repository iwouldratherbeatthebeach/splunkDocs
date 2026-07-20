#!/usr/bin/env python3
"""Business logic for the Splunk Docs Search app's Configuration backend.

Handles status reporting, launching the scraper as a detached background job,
lightweight update checks, and persisted settings. Kept import-light so it
loads cleanly inside splunkd; the scraper (requests/bs4) is only imported when a
check actually runs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = APP_DIR / "appserver" / "static" / "docdata"
SCRAPER = APP_DIR / "scraper" / "fetch_docs.py"
PRODUCTS = APP_DIR / "scraper" / "products.yaml"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state_path(name: str) -> Path:
    return DATA_DIR / name


def read_json(path: Path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except OSError:
        pass


def coerce_bool(raw) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def default_settings() -> dict:
    return {
        "daily_check_enabled": False,
        "python": sys.executable,
        "scraper_root": str(SCRAPER.parent),
    }


def load_settings() -> dict:
    s = default_settings()
    s.update(read_json(state_path("settings.json"), {}))
    return s


def save_settings(patch: dict) -> dict:
    s = load_settings()
    for k in ("daily_check_enabled", "python"):
        if k in patch:
            s[k] = coerce_bool(patch[k]) if k == "daily_check_enabled" else patch[k]
    write_json(state_path("settings.json"), s)
    return s


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def get_status() -> dict:
    status = read_json(state_path("status.json"), {})
    bundle = status.get("bundle") or {"app_version": _app_version()}
    if not bundle.get("topic_count"):
        si = read_json(state_path("search_index.json"), [])
        bundle["topic_count"] = len(si) if isinstance(si, list) else 0
    bundle.setdefault("app_version", _app_version())
    bundle.setdefault("meta", {})
    return {
        "bundle": bundle,
        "job": status.get("job", {"status": "idle"}),
        "check": read_json(state_path("check_report.json"), {}),
        "settings": load_settings(),
    }


def _app_version() -> str:
    try:
        for line in open(APP_DIR / "default" / "app.conf", encoding="utf-8"):
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "3.0.0"


# --------------------------------------------------------------------------- #
# Launch scraper
# --------------------------------------------------------------------------- #
def _job_running() -> bool:
    job = read_json(state_path("status.json"), {}).get("job", {})
    return job.get("status") == "running"


def start_update(mode: str = "incremental") -> dict:
    if mode not in ("incremental", "full"):
        mode = "incremental"
    if _job_running():
        return {"ok": False, "message": "A download job is already running."}
    if not SCRAPER.exists():
        return {"ok": False, "message": f"Scraper not found at {SCRAPER}"}

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    prev = read_json(state_path("status.json"), {})
    write_json(state_path("status.json"), {
        "bundle": prev.get("bundle", {"app_version": _app_version(), "topic_count": 0, "meta": {}}),
        "job": {"status": "running", "mode": mode, "started_at": now_iso(),
                "finished_at": None, "error": None, "done": 0, "total": 0,
                "log_tail": [f"{now_iso()} queued ({mode})"]},
    })

    py = load_settings().get("python") or sys.executable
    args = [py, str(SCRAPER), "--data-dir", str(DATA_DIR), "--mode", mode,
            "--products", str(PRODUCTS), "--status-file", str(state_path("status.json"))]
    log = open(state_path("scrape.log"), "ab", buffering=0)
    try:
        subprocess.Popen(  # detached so it outlives the REST request
            args, stdout=log, stderr=subprocess.STDOUT,
            cwd=str(APP_DIR), start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"Failed to launch scraper: {exc}"}
    return {"ok": True, "message": f"Started {mode} download.", "mode": mode}


# --------------------------------------------------------------------------- #
# Update check (best-effort; needs internet + scraper deps)
# --------------------------------------------------------------------------- #
def run_check(save: bool = True) -> dict:
    report = {"checked_at": now_iso(), "updates_available": False, "products": []}
    try:
        sys.path.insert(0, str(SCRAPER.parent))
        import fetch_docs as fd  # type: ignore
        s = fd.session()
        sm = fd.all_sitemap_urls(s)
        cfg = fd.load_products(str(PRODUCTS))
        local = read_json(state_path("search_index.json"), [])
        have = {}
        for r in local if isinstance(local, list) else []:
            have[r.get("product")] = have.get(r.get("product"), 0) + 1
        for pid, pcfg in cfg["products"].items():
            planned = fd.product_urls(sm, pid, pcfg)
            missing = max(0, len(planned) - have.get(pid, 0))
            if missing:
                report["updates_available"] = True
            report["products"].append({
                "id": pid, "title": pcfg.get("title", pid),
                "online_count": len(planned), "local_count": have.get(pid, 0),
                "missing_count": missing,
            })
    except Exception as exc:  # noqa: BLE001
        report["error"] = str(exc)
    if save:
        write_json(state_path("check_report.json"), report)
    return report


if __name__ == "__main__":
    print(json.dumps(get_status(), indent=2))
