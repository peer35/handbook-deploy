"""Minimal GitHub REST client: PR comments, commit statuses, open PRs."""

import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

MARKER = "<!-- pr-preview -->"


class GitHub:
    API = "https://api.github.com"

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._inst_token = None
        self._inst_expires = 0.0
        self._login = None
        self._comment_ids = {}

    # --- authentication -------------------------------------------------

    def _app_jwt(self):
        import jwt  # PyJWT, only needed for GitHub App auth

        now = int(time.time())
        key = Path(self.cfg.app_key_file).read_text()
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": str(self.cfg.app_id)},
                          key, algorithm="RS256")

    def _token(self):
        if self.cfg.token:
            return self.cfg.token
        with self._lock:
            if time.time() < self._inst_expires - 300:
                return self._inst_token
            app_auth = f"Bearer {self._app_jwt()}"
            inst, _ = self._request("GET", f"/repos/{self.cfg.repo}/installation", auth=app_auth)
            tok, _ = self._request("POST", f"/app/installations/{inst['id']}/access_tokens",
                                   body={}, auth=app_auth)
            self._inst_token = tok["token"]
            self._inst_expires = datetime.fromisoformat(
                tok["expires_at"].replace("Z", "+00:00")).timestamp()
            return self._inst_token

    def login(self):
        """Login name that our comments are posted under."""
        if self._login is None:
            if self.cfg.token:
                self._login = self.api("GET", "/user")["login"]
            else:
                app, _ = self._request("GET", "/app", auth=f"Bearer {self._app_jwt()}")
                self._login = f"{app['slug']}[bot]"
        return self._login

    # --- HTTP -----------------------------------------------------------

    def _request(self, method, path, body=None, auth=None):
        url = path if path.startswith("https://") else self.API + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pr-preview-hook",
            "Authorization": auth or f"Bearer {self._token()}",
        })
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return (json.loads(raw) if raw else None), resp.headers

    def api(self, method, path, body=None):
        return self._request(method, path, body)[0]

    def paginate(self, path):
        items, url = [], path
        while url:
            page, headers = self._request("GET", url)
            items.extend(page)
            match = re.search(r'<([^>]+)>;\s*rel="next"', headers.get("Link", ""))
            url = match.group(1) if match else None
        return items

    # --- operations -----------------------------------------------------

    def open_pulls(self):
        return self.paginate(f"/repos/{self.cfg.repo}/pulls?state=open&per_page=100")

    def _find_comment(self, pr):
        me = self.login()
        for c in self.paginate(f"/repos/{self.cfg.repo}/issues/{pr}/comments?per_page=100"):
            if MARKER in (c.get("body") or "") and c["user"]["login"] == me:
                return c["id"]
        return None

    def upsert_comment(self, pr, text):
        body = f"{MARKER}\n{text}"
        cid = self._comment_ids.get(pr) or self._find_comment(pr)
        if cid:
            try:
                self.api("PATCH", f"/repos/{self.cfg.repo}/issues/comments/{cid}", {"body": body})
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
                cid = None  # deleted by someone; post a new one
        if not cid:
            cid = self.api("POST", f"/repos/{self.cfg.repo}/issues/{pr}/comments", {"body": body})["id"]
        self._comment_ids[pr] = cid

    def set_status(self, sha, state, target_url, description):
        self.api("POST", f"/repos/{self.cfg.repo}/statuses/{sha}", {
            "state": state, "target_url": target_url,
            "description": description[:140], "context": "pr-preview",
        })
