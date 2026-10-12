"""Control-plane client: bearer-token calls to the api Function URL, and GitHub device-flow sign-in."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import net

CONFIG_PATH = Path(os.environ.get("CTRANSFER_CONFIG") or Path.home() / ".config" / "ctransfer" / "config.json")
# The hosted control plane. CTRANSFER_API_URL (or api_url in the config file) points at another deployment.
DEFAULT_API_URL = "https://api.ctransfer.itschrispy.com"
TERMS_URL = "https://ctransfer.itschrispy.com/terms"
PRIVACY_URL = "https://ctransfer.itschrispy.com/privacy"
ACCOUNT_URL = "https://ctransfer.itschrispy.com/account"  # plans and payments: the website is the only place to pay


def load_config() -> dict:
    cfg = json.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.exists() else {}
    if os.environ.get("CTRANSFER_API_URL"):
        cfg["api_url"] = os.environ["CTRANSFER_API_URL"]
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    CONFIG_PATH.chmod(0o600)  # holds the sign-in token


class Api:
    def __init__(self, url: str, token: str | None = None):
        self.url = url.rstrip("/")
        self.token = token

    @classmethod
    def from_config(cls, *, signed_in: bool = True) -> "Api":
        cfg = load_config()
        if signed_in and not cfg.get("token"):
            raise SystemExit("not signed in: run `ctransfer login`")
        return cls(cfg.get("api_url") or DEFAULT_API_URL, cfg.get("token"))

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"authorization": f"Bearer {self.token}"} if self.token else {}
        return net.request_json(self.url + path, method=method, body=body, headers=headers)


def login(api: Api, *, notify=print, sleep=time.sleep) -> dict:
    """GitHub OAuth device flow, run by the control plane: it asks GitHub for a code, the user enters it at GitHub,
    and polling then returns a ctransfer token. The GitHub token itself never reaches this machine, and the control
    plane only accepts GitHub tokens it obtained itself. Returns {token, user_id, login}."""
    code = _sign_in_call(api, "/auth/github/device", {})
    notify(f"open {code['verification_uri']} and enter code {code['user_code']}")
    interval = int(code.get("interval", 5))
    deadline = time.time() + int(code.get("expires_in", 900))
    while time.time() < deadline:
        sleep(interval)
        out = _sign_in_call(api, "/auth/github/device/token", {"device_code": code["device_code"]})
        if not out.get("pending"):
            return out
        interval = max(interval, int(out.get("interval") or 0))  # GitHub may ask us to slow down, never to speed up
    raise SystemExit("GitHub sign-in timed out; run `ctransfer login` again")


def _sign_in_call(api: Api, path: str, body: dict) -> dict:
    try:
        return api.call("POST", path, body)
    except net.HttpError as e:
        try:
            reason = str(json.loads(e.body)["error"])
        except (ValueError, KeyError, TypeError):
            reason = e.body
        raise SystemExit(f"GitHub sign-in failed: {reason}") from None
