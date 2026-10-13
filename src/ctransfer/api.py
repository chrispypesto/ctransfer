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
# A service token (`ctransfer service-token create`) for machines where `ctransfer login` can't run: CI, scripts, VMs.
# When set, it is used instead of the saved sign-in, and never written to the config file.
TOKEN_ENV = "CTRANSFER_TOKEN"


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
        """The api, with the token to send: CTRANSFER_TOKEN, else the saved sign-in. signed_in=False (signing in)
        sends none."""
        cfg = load_config()
        token = (os.environ.get(TOKEN_ENV) or cfg.get("token")) if signed_in else None
        if signed_in and not token:
            raise SystemExit(f"not signed in: run `ctransfer login` (or, in automation, set {TOKEN_ENV} to a service "
                             "token)")
        return cls(cfg.get("api_url") or DEFAULT_API_URL, token)

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        headers = {"authorization": f"Bearer {self.token}"} if self.token else {}
        return net.request_json(self.url + path, method=method, body=body, headers=headers)


def login(api: Api, *, notify=print, sleep=time.sleep) -> dict:
    """GitHub OAuth device flow, run by the control plane: it asks GitHub for a code, the user enters it at GitHub,
    and polling then returns a ctransfer token. The GitHub token itself never reaches this machine, and the control
    plane only accepts GitHub tokens it obtained itself. Returns {token, user_id, login}."""
    try:
        code = api.call("POST", "/auth/github/device", {})
    except net.HttpError as e:
        raise SystemExit(f"couldn't start signing in: {reason(e)}") from None
    notify(f"open {code['verification_uri']} and enter code {code['user_code']}")
    interval = int(code.get("interval", 5))
    deadline = time.time() + int(code.get("expires_in", 900))
    while time.time() < deadline:
        sleep(interval)
        try:
            out = api.call("POST", "/auth/github/device/token", {"device_code": code["device_code"]})
        except net.HttpError as e:
            if e.status == 429 or e.status >= 500:
                continue  # a passing hiccup; the code is still good, so keep polling until it expires
            raise SystemExit(f"GitHub sign-in failed: {reason(e)}") from None
        except OSError:
            continue  # the network dropped for a moment; same as above
        if not out.get("pending"):
            return out
        interval = max(interval, int(out.get("interval") or 0))  # GitHub may ask us to slow down, never to speed up
    raise SystemExit("GitHub sign-in timed out; run `ctransfer login` again")


def reason(e: net.HttpError) -> str:
    """The error message from a control-plane reply, or a short stand-in for one that isn't ours (e.g. HTML)."""
    try:
        return str(json.loads(e.body)["error"])
    except (ValueError, KeyError, TypeError):
        return f"the server answered HTTP {e.status}"
