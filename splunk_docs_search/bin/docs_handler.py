#!/usr/bin/env python3
"""Persistent REST handler for the Splunk Docs Search Configuration backend.

Registered by default/restmap.conf at /docs_admin and reached from the browser
via /<locale>/splunkd/__raw/servicesNS/nobody/<app>/docs_admin/<member>.

Members:
    GET  status   -> bundle + job + check + settings
    POST update   -> {mode: incremental|full}  starts a background scrape
    GET/POST check -> lightweight comparison against help.splunk.com
    GET/POST settings -> read/update persisted settings
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

BIN_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN_DIR))

import docs_service as svc  # noqa: E402

try:
    from splunk.persistconn.application import PersistentServerConnectionApplication
except ImportError:  # allows local testing without Splunk libs
    PersistentServerConnectionApplication = object  # type: ignore


def _parse(in_string) -> dict:
    if not in_string:
        return {}
    if isinstance(in_string, bytes):
        in_string = in_string.decode("utf-8", errors="replace")
    s = in_string.strip()
    if s.startswith("{"):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return d
        except json.JSONDecodeError:
            pass
    out = {}
    for k, v in parse_qs(in_string, keep_blank_values=True).items():
        out[k] = v[-1] if len(v) == 1 else v
    return out


def _resp(payload: dict, status: int = 200) -> dict:
    return {"payload": json.dumps(payload), "status": status,
            "headers": {"Content-Type": "application/json"}}


def _member(path: str) -> str:
    path = (path or "").rstrip("/")
    return path.split("/")[-1] if path else "status"


def _body(in_dict: dict) -> dict:
    body = in_dict.get("payload") or in_dict.get("body") or ""
    if isinstance(body, dict):
        return body
    if body:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


class DocsAdminHandler(PersistentServerConnectionApplication):
    def __init__(self, command_line=None, command_arg=None):
        super().__init__()

    def handle(self, in_string):
        try:
            d = _parse(in_string)
            method = (d.get("method") or "GET").upper()
            member = _member(d.get("path", ""))
            query = d.get("query", {}) or {}
            if isinstance(query, str):
                try:
                    query = json.loads(query)
                except json.JSONDecodeError:
                    query = {}
            body = _body(d)

            if member in ("status", "docs_admin", ""):
                return _resp(svc.get_status())

            if member == "update":
                if method != "POST":
                    return _resp({"error": "POST required"}, 405)
                mode = body.get("mode") or (query.get("mode") if isinstance(query, dict) else None) or "incremental"
                res = svc.start_update(mode=mode)
                return _resp(res, 200 if res.get("ok") else 409)

            if member == "check":
                if method == "GET":
                    cached = svc.read_json(svc.state_path("check_report.json"), {})
                    return _resp(cached or {"message": "No check has been run yet."})
                return _resp(svc.run_check(save=True))

            if member == "settings":
                if method == "GET":
                    return _resp(svc.load_settings())
                if not body:
                    return _resp({"error": "No settings provided"}, 400)
                return _resp({"ok": True, "settings": svc.save_settings(body)})

            return _resp({"error": f"Unknown endpoint: {member}"}, 404)
        except Exception as exc:  # noqa: BLE001
            return _resp({"error": str(exc)}, 500)


if __name__ == "__main__":
    print(json.dumps(svc.get_status(), indent=2))
