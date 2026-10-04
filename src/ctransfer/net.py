"""Minimal HTTP helpers (stdlib only, shared by the client and the VM agent)."""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request


class HttpError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def request_json(url: str, method: str = "POST", body: dict | None = None, headers: dict | None = None, timeout: int = 30) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {"content-type": "application/json", **(headers or {})}
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode(errors="replace")) from None
    return json.loads(raw) if raw else {}


def put_file(url: str, path, timeout: int = 900) -> None:
    """Upload a file to a presigned S3 PUT URL."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        req = urllib.request.Request(url, data=f, method="PUT",
                                     headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"})
        try:
            urllib.request.urlopen(req, timeout=timeout).close()
        except urllib.error.HTTPError as e:
            raise HttpError(e.code, e.read().decode(errors="replace")) from None


def get_file(url: str, path, timeout: int = 900) -> None:
    """Download a presigned S3 GET URL to path."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp, open(path, "wb") as out:
            shutil.copyfileobj(resp, out)
    except urllib.error.HTTPError as e:
        raise HttpError(e.code, e.read().decode(errors="replace")) from None
