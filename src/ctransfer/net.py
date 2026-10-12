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


def post_file(upload: dict, path, timeout: int = 900) -> None:
    """Upload a file to a presigned S3 POST ({"url", "fields"}), streaming it as multipart/form-data."""
    import uuid

    boundary = uuid.uuid4().hex
    head = b"".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
                    for k, v in upload["fields"].items())
    head += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="bundle.tgz"\r\n'
             "Content-Type: application/octet-stream\r\n\r\n").encode()
    tail = f"\r\n--{boundary}--\r\n".encode()

    def body():
        yield head
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                yield chunk
        yield tail

    size = len(head) + os.path.getsize(path) + len(tail)
    req = urllib.request.Request(upload["url"], data=body(), method="POST", headers={
        "Content-Length": str(size), "Content-Type": f"multipart/form-data; boundary={boundary}"})
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
