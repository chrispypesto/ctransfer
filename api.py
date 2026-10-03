"""Control-plane client: bearer-token calls to the api Function URL, and GitHub device-flow sign-in."""

from __future__ import annotations

import json
import os
import time
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
    CONFIG_PATH.chmod(0o600)  # holds the sign-in token


class Api:
    def __init__(self, url: str, token: str | None = None, region: str = "us-east-1", profile: str | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self.region = region
        self.profile = profile

    @classmethod
    def from_config(cls, *, signed_in: bool = True) -> "Api":
        cfg = load_config()
        if not cfg.get("api_url"):
            raise SystemExit("not configured: run `ctransfer configure --api-url <terraform output api_url>`")
        if signed_in and not cfg.get("token"):
            raise SystemExit("not signed in: run `ctransfer login`")
        return cls(cfg["api_url"], cfg.get("token"), cfg.get("region", "us-east-1"), cfg.get("profile"))

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"authorization": f"Bearer {self.token}"} if self.token else {}
        return net.request_json(self.url + path, method=method, body=body, headers=headers)

    def lightsail(self):
        import boto3

        return boto3.Session(profile_name=self.profile, region_name=self.region).client("lightsail")


GITHUB_DEVICE_URL = "https://github.com/login/device/code"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"


def login(api: Api, *, post_form=net.post_form, notify=print, sleep=time.sleep) -> dict:
    """GitHub OAuth device flow, then trade the GitHub token for a ctransfer token. Returns the /auth/github reply."""
    client_id = api.call("GET", "/auth/config")["github_client_id"]
    code = post_form(GITHUB_DEVICE_URL, {"client_id": client_id, "scope": ""})
    notify(f"open {code['verification_uri']} and enter code {code['user_code']}")
    interval = int(code.get("interval", 5))
    deadline = time.time() + int(code.get("expires_in", 900))
    while time.time() < deadline:
        sleep(interval)
        out = post_form(GITHUB_TOKEN_URL, {"client_id": client_id, "device_code": code["device_code"],
                                           "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        if out.get("access_token"):
            # The GitHub token only proves identity once; it is not stored.
            return api.call("POST", "/auth/github", {"github_token": out["access_token"]})
        if out.get("error") == "slow_down":
            interval += 5
        elif out.get("error") != "authorization_pending":
            raise SystemExit(f"GitHub sign-in failed: {out.get('error_description') or out.get('error')}")
    raise SystemExit("GitHub sign-in timed out")
