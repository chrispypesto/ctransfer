"""Control-plane client: SigV4-signed calls to the IAM-auth Lambda Function URL."""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import net

CONFIG_PATH = Path(os.environ.get("CTRANSFER_CONFIG") or Path.home() / ".config" / "ctransfer" / "config.json")


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    if os.environ.get("CTRANSFER_API_URL"):
        cfg["api_url"] = os.environ["CTRANSFER_API_URL"]
    if os.environ.get("CTRANSFER_REGION"):
        cfg["region"] = os.environ["CTRANSFER_REGION"]
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


class Api:
    def __init__(self, url: str, region: str, profile: str | None = None):
        import boto3

        self.url = url.rstrip("/")
        self.region = region
        self.session = boto3.Session(profile_name=profile, region_name=region)

    @classmethod
    def from_config(cls) -> "Api":
        cfg = load_config()
        if not cfg.get("api_url"):
            raise SystemExit("not configured: run `ctransfer configure --api-url <terraform output api_url>`")
        return cls(cfg["api_url"], cfg.get("region", "us-east-1"), cfg.get("profile"))

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        url = self.url + path
        data = json.dumps(body) if body is not None else None
        req = AWSRequest(method=method, url=url, data=data, headers={"content-type": "application/json"})
        SigV4Auth(self.session.get_credentials().get_frozen_credentials(), "lambda", self.region).add_auth(req)
        return net.request_json(url, method=method, body=body, headers=dict(req.headers.items()))

    def lightsail(self):
        return self.session.client("lightsail")
