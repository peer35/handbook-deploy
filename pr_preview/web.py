"""Flask app receiving GitHub `pull_request` webhooks."""

import hashlib
import hmac
import json
import logging
import re
import threading
import urllib.parse

from flask import Flask, request

from .config import Config
from .github import GitHub
from .previewer import Job, Previewer

log = logging.getLogger(__name__)

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def create_app(cfg=None, gh=None, start_reconcile=True):
    cfg = cfg or Config()
    previewer = Previewer(cfg, gh or GitHub(cfg))
    if start_reconcile:
        threading.Thread(target=previewer.reconcile_loop, name="reconcile", daemon=True).start()

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # GitHub's webhook payload limit
    app.extensions["previewer"] = previewer

    @app.get("/")
    @app.get("/_hooks/github")
    def health():
        return "pr-preview-hook is running\n", 200, {"Content-Type": "text/plain; charset=utf-8"}

    @app.post("/")
    @app.post("/_hooks/github")
    def webhook():
        body = request.get_data()
        expected = "sha256=" + hmac.new(cfg.webhook_secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(request.headers.get("X-Hub-Signature-256", ""), expected):
            log.warning("rejected delivery with bad signature from %s",
                        request.headers.get("X-Forwarded-For", request.remote_addr))
            return "bad signature\n", 401

        if request.mimetype == "application/x-www-form-urlencoded":
            body = urllib.parse.parse_qs(body.decode()).get("payload", ["{}"])[0]
        try:
            payload = json.loads(body)
        except ValueError:
            return "invalid JSON\n", 400

        event = request.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return "pong\n", 200
        if event != "pull_request":
            return f"ignored event {event}\n", 202
        if payload["repository"]["full_name"].lower() != cfg.repo.lower():
            return "ignored repository\n", 202

        action, pull = payload["action"], payload["pull_request"]
        pr, sha = pull.get("number"), (pull.get("head") or {}).get("sha", "")
        if not isinstance(pr, int) or not SHA_RE.match(sha):
            return "invalid pull request payload\n", 400
        if action in ("opened", "reopened", "synchronize"):
            if previewer.eligible(pull):
                previewer.submit(Job("deploy", pr, sha))
                return f"deploying #{pr}\n", 202
            if action != "synchronize":
                threading.Thread(target=previewer.skip_notice, args=(pr,), daemon=True).start()
            return f"#{pr} not eligible for a preview\n", 202
        if action == "closed":
            previewer.submit(Job("remove", pr))
            return f"removing #{pr}\n", 202
        return f"ignored action {action}\n", 202

    return app
