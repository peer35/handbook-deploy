"""End-to-end tests of the webhook app with a fake GitHub and a stub build script."""

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest

from pr_preview.config import Config
from pr_preview.web import create_app

STUB = Path(__file__).with_name("stub-build")
SECRET = "s3cret"
REPO = "peer35/open-handbook-test"
A, B, C, BAD = "a" * 40, "b" * 40, "c" * 40, "dead" + "0" * 36


class FakeGitHub:
    def __init__(self):
        self.events = []
        self.pulls = []

    def upsert_comment(self, pr, text):
        self.events.append(("comment", pr, text.split("\n")[0]))

    def set_status(self, sha, state, url, description):
        self.events.append(("status", sha[:4], state))

    def open_pulls(self):
        return self.pulls

    def take(self):
        events, self.events = self.events, []
        return events

    def comments(self, pr=None):
        return [e[2] for e in self.take() if e[0] == "comment" and pr in (None, e[1])]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for key, value in {
        "GITHUB_REPO": REPO, "WEBHOOK_SECRET": SECRET, "GITHUB_TOKEN": "tok",
        "PREVIEW_BASE_URL": "https://prev.test", "PREVIEW_ROOT": str(tmp_path / "www"),
        "WORK_ROOT": str(tmp_path / "work"), "BUILD_SCRIPT": str(STUB),
    }.items():
        monkeypatch.setenv(key, value)
    gh = FakeGitHub()
    app = create_app(Config(), gh, start_reconcile=False)
    previewer = app.extensions["previewer"]

    def send(event, payload, secret=SECRET):
        body = json.dumps(payload).encode()
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return app.test_client().post("/_hooks/github", data=body, content_type="application/json",
                                      headers={"X-GitHub-Event": event, "X-Hub-Signature-256": sig})

    def idle(timeout=30):
        end = time.monotonic() + timeout
        while previewer.busy():
            assert time.monotonic() < end, "jobs did not finish"
            time.sleep(0.1)

    class Env:
        pass

    e = Env()
    e.gh, e.previewer, e.send, e.idle, e.www = gh, previewer, send, idle, tmp_path / "www"
    e.state = tmp_path / "work" / "state"
    return e


def pr_event(action, number, sha, fork=False, association="CONTRIBUTOR"):
    return {
        "action": action,
        "repository": {"full_name": REPO},
        "pull_request": {
            "number": number, "author_association": association,
            "head": {"sha": sha, "repo": {"full_name": "someone/fork" if fork else REPO}},
        },
    }


def test_health_and_ping(env):
    assert env.send("ping", {"zen": "hi"}).status_code == 200


def test_bad_signature_rejected(env):
    resp = env.send("pull_request", pr_event("opened", 5, A), secret="wrong")
    assert resp.status_code == 401
    assert not env.previewer.busy()


def test_other_repository_ignored(env):
    payload = pr_event("opened", 5, A)
    payload["repository"]["full_name"] = "someone/else"
    assert env.send("pull_request", payload).status_code == 202
    assert not env.previewer.busy()


def test_deploy_success(env):
    assert env.send("pull_request", pr_event("opened", 5, A)).status_code == 202
    env.idle()
    events = env.gh.take()
    comments = [e[2] for e in events if e[0] == "comment"]
    assert comments[0].startswith("⏳ **Preview queued**")
    assert comments[1].startswith("🔨 **Building preview**")
    assert comments[2] == "✅ **Preview ready:** https://prev.test/5/"
    assert ("status", "aaaa", "success") in events
    assert (env.www / "5" / "index.html").read_text().strip() == A
    assert (env.state / "5.built").read_text().strip() == A
    log = (env.www / "_logs" / "5" / "latest.log").read_text()
    assert log.rstrip().endswith("### DONE")
    assert "token visible: no" in log  # the GitHub token never reaches the build
    assert (env.www / "_logs" / "view.html").exists()


def test_new_push_cancels_running_build(env):
    env.send("pull_request", pr_event("synchronize", 5, B))
    time.sleep(1)
    env.send("pull_request", pr_event("synchronize", 5, C))
    env.idle()
    events = env.gh.take()
    assert ("status", "bbbb", "error") in events
    assert [e for e in events if e[0] == "comment"][-1][2] == "✅ **Preview ready:** https://prev.test/5/"
    logs = sorted((env.www / "_logs" / "5").glob("*.log"))
    tails = {p.name.split("-")[1][:7]: p.read_text().rstrip().splitlines()[-1]
             for p in logs if p.name != "latest.log"}
    assert tails["bbbbbbb"].startswith("### CANCELLED")
    assert tails["ccccccc"] == "### DONE"
    assert (env.www / "5" / "index.html").read_text().strip() == C


def test_failed_build(env):
    env.send("pull_request", pr_event("opened", 5, BAD))
    env.idle()
    events = env.gh.take()
    assert ("status", "dead", "failure") in events
    assert [e for e in events if e[0] == "comment"][-1][2].startswith("❌ **Preview build failed**")
    assert not (env.www / "5").exists()


def test_fork_policy(env):
    env.send("pull_request", pr_event("opened", 6, A, fork=True))
    env.idle()
    assert env.gh.comments(6)[0].startswith("⏭️ **Preview skipped.**")
    env.send("pull_request", pr_event("opened", 8, A, fork=True, association="COLLABORATOR"))
    env.idle()
    assert env.gh.comments(8)[-1].startswith("✅")


def test_close_removes_preview(env):
    env.send("pull_request", pr_event("opened", 5, A))
    env.idle()
    env.gh.take()
    env.send("pull_request", pr_event("closed", 5, A))
    env.idle()
    assert env.gh.comments(5)[0].startswith("🗑️ **Preview removed**")
    assert not (env.www / "5").exists()
    assert not (env.state / "5.built").exists()
    assert (env.www / "_logs" / "5" / "latest.log").exists()  # logs outlive the preview


def test_reconcile(env):
    env.send("pull_request", pr_event("opened", 8, A))
    env.idle()
    (env.www / "99").mkdir()
    env.gh.take()
    env.gh.pulls = [{"number": 7, "author_association": "NONE",
                     "head": {"sha": A, "repo": {"full_name": REPO}}}]
    env.previewer.reconcile()
    env.idle()
    comments = env.gh.take()
    assert ("comment", 7, "✅ **Preview ready:** https://prev.test/7/") in comments
    assert any(c[1] == 8 and c[2].startswith("🗑️") for c in comments)
    assert sorted(p.name for p in env.www.iterdir()) == ["7", "_logs"]


def test_invalid_payload_rejected(env):
    assert env.send("pull_request", pr_event("opened", 5, "")).status_code == 400
    assert env.send("pull_request", pr_event("opened", "5; rm", A)).status_code == 400
    assert not env.previewer.busy()
