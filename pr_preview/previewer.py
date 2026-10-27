#!/usr/bin/env python3
"""GitHub webhook receiver that builds Quarto previews of pull requests.

Listens for `pull_request` webhooks, runs the `pr-preview` build script for
opened/updated PRs and removes the preview when a PR is closed. Build output is
written to a log file served by nginx; a single PR comment (and a commit
status) links to the live log and, once finished, to the preview.

A reconcile loop (at startup and every RECONCILE_INTERVAL seconds) catches up
on missed webhooks: it rebuilds open PRs whose head commit was never built,
removes previews of closed PRs and expires old logs.

Configuration comes from environment variables, see config.env.example.
Only the standard library is needed, plus PyJWT when authenticating as a
GitHub App.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger("pr-preview")

MARKER = "<!-- pr-preview -->"
MAX_BODY = 25 * 1024 * 1024  # GitHub caps webhook payloads at 25 MB
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LOG_NAME_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{7}\.log$")


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing required setting {name}")
    return value


class Config:
    def __init__(self):
        self.repo = env("GITHUB_REPO")
        self.webhook_secret = env("WEBHOOK_SECRET").encode()
        self.base_url = env("PREVIEW_BASE_URL").rstrip("/")
        self.token = os.environ.get("GITHUB_TOKEN") or None
        self.app_id = os.environ.get("GITHUB_APP_ID") or None
        self.app_key_file = os.environ.get("GITHUB_APP_KEY_FILE") or None
        if not self.token and not (self.app_id and self.app_key_file):
            raise SystemExit("set GITHUB_TOKEN, or GITHUB_APP_ID and GITHUB_APP_KEY_FILE")
        self.repo_url = env("REPO_URL", f"https://github.com/{self.repo}.git")
        self.listen = env("LISTEN", "127.0.0.1:9000")
        self.preview_root = Path(env("PREVIEW_ROOT", "/var/www/previews"))
        self.work_root = Path(env("WORK_ROOT", "/srv/pr-preview"))
        self.build_script = env("BUILD_SCRIPT", "/usr/local/bin/pr-preview")
        self.pre_render = env("PRE_RENDER", "")
        self.output_dir = env("OUTPUT_DIR", "_site")
        self.max_parallel = int(env("MAX_PARALLEL", "1"))
        self.build_timeout = int(env("BUILD_TIMEOUT", "1800"))
        self.reconcile_interval = int(env("RECONCILE_INTERVAL", "3600"))
        self.logs_keep = int(env("LOGS_KEEP", "10"))
        self.closed_log_days = int(env("CLOSED_LOG_DAYS", "14"))
        self.allowed_associations = {
            a.strip().upper()
            for a in env("ALLOWED_ASSOCIATIONS", "OWNER,MEMBER,COLLABORATOR").split(",")
            if a.strip()
        }

    @property
    def log_root(self):
        return self.preview_root / "_logs"

    @property
    def state_dir(self):
        return self.work_root / "state"


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

    def has_comment(self, pr):
        return bool(self._comment_ids.get(pr) or self._find_comment(pr))

    def set_status(self, sha, state, target_url, description):
        self.api("POST", f"/repos/{self.cfg.repo}/statuses/{sha}", {
            "state": state, "target_url": target_url,
            "description": description[:140], "context": "pr-preview",
        })


class Job:
    def __init__(self, kind, pr, sha=None):
        self.kind = kind  # "deploy" | "remove"
        self.pr = pr
        self.sha = sha
        self.cancelled = threading.Event()

    def __repr__(self):
        return f"<{self.kind} #{self.pr}{' ' + self.sha[:7] if self.sha else ''}>"


def fmt_duration(seconds):
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s:02d}s"


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class Previewer:
    def __init__(self, cfg, gh):
        self.cfg = cfg
        self.gh = gh
        self._lock = threading.Lock()
        self._current = {}  # pr -> latest Job; older jobs for the same PR get cancelled
        self._slots = threading.Semaphore(cfg.max_parallel)
        self._comment_locks = defaultdict(threading.Lock)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        cfg.log_root.mkdir(parents=True, exist_ok=True)

    # --- job bookkeeping ------------------------------------------------

    def submit(self, job):
        with self._lock:
            old = self._current.get(job.pr)
            self._current[job.pr] = job
        if old:
            log.info("%r supersedes %r", job, old)
            old.cancelled.set()
        threading.Thread(target=self._run, args=(job,), name=repr(job), daemon=True).start()

    def is_current(self, job):
        with self._lock:
            return self._current.get(job.pr) is job

    def busy(self, pr):
        with self._lock:
            return pr in self._current

    def _finish(self, job):
        with self._lock:
            if self._current.get(job.pr) is job:
                del self._current[job.pr]

    def eligible(self, pull):
        head_repo = ((pull.get("head") or {}).get("repo") or {}).get("full_name", "")
        return (head_repo.lower() == self.cfg.repo.lower()
                or pull.get("author_association", "").upper() in self.cfg.allowed_associations)

    # --- GitHub side effects (never fatal) -----------------------------

    def comment(self, job, text):
        """Update the PR comment, unless a newer job for this PR has taken over."""
        with self._comment_locks[job.pr]:
            if not self.is_current(job):
                return
            try:
                self.gh.upsert_comment(job.pr, f"{text}\n\n<sub>Updated {now_utc()}</sub>")
            except Exception:
                log.exception("updating comment on #%s failed", job.pr)

    def status(self, sha, state, url, description):
        try:
            self.gh.set_status(sha, state, url, description)
        except Exception:
            log.exception("setting status on %s failed", sha[:7])

    def skip_notice(self, pr):
        job = Job("skip", pr)
        with self._lock:
            if pr in self._current:
                return
            self._current[pr] = job
        try:
            self.comment(job, "⏭️ **Preview skipped.** Previews are built automatically only for "
                              "branches in this repository and for pull requests by collaborators.")
        finally:
            self._finish(job)

    # --- state files ----------------------------------------------------

    def _state_file(self, pr):
        return self.cfg.state_dir / f"{pr}.built"

    def built_sha(self, pr):
        try:
            return self._state_file(pr).read_text().strip()
        except FileNotFoundError:
            return None

    # --- running --------------------------------------------------------

    def _build_env(self):
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": os.environ.get("HOME", str(self.cfg.work_root / "home")),
            "LANG": "C.UTF-8",
            "REPO_URL": self.cfg.repo_url,
            "PREVIEW_BASE_URL": self.cfg.base_url,
            "PREVIEW_ROOT": str(self.cfg.preview_root),
            "WORK_ROOT": str(self.cfg.work_root),
            "PRE_RENDER": self.cfg.pre_render,
            "OUTPUT_DIR": self.cfg.output_dir,
        }

    def _exec(self, job, args, out):
        """Run the build script in its own process group; kill it on cancel/timeout."""
        proc = subprocess.Popen([self.cfg.build_script, *args], stdin=subprocess.DEVNULL,
                                stdout=out, stderr=subprocess.STDOUT,
                                env=self._build_env(), start_new_session=True)
        deadline = time.monotonic() + self.cfg.build_timeout
        while True:
            try:
                return proc.wait(timeout=1), False
            except subprocess.TimeoutExpired:
                pass
            timed_out = time.monotonic() > deadline
            if job.cancelled.is_set() or timed_out:
                for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, None)):
                    try:
                        os.killpg(proc.pid, sig)
                    except ProcessLookupError:
                        break
                    try:
                        proc.wait(timeout=grace)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                return proc.wait(), timed_out

    def _run(self, job):
        try:
            if job.kind == "deploy":
                self._deploy(job)
            else:
                self._remove(job)
        except Exception:
            log.exception("%r crashed", job)
        finally:
            self._finish(job)

    def _deploy(self, job):
        pr, sha, short = job.pr, job.sha, job.sha[:7]
        self.comment(job, f"⏳ **Preview queued** for commit `{short}`.")
        while not self._slots.acquire(timeout=1):
            if job.cancelled.is_set():
                return
        try:
            if job.cancelled.is_set():
                return
            self._build(job, pr, sha, short)
        finally:
            self._slots.release()

    def _build(self, job, pr, sha, short):
        cfg = self.cfg
        log_dir = cfg.log_root / str(pr)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_name = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{short}.log"
        log_path = log_dir / log_name
        raw_url = f"{cfg.base_url}/_logs/{pr}/{log_name}"
        live_url = f"{cfg.base_url}/_logs/view.html?pr={pr}&log={log_name}"
        preview_url = f"{cfg.base_url}/{pr}/"

        log.info("building %r", job)
        self.comment(job, f"🔨 **Building preview** of commit `{short}` · "
                          f"[live log]({live_url}) · [raw log]({raw_url})")
        self.status(sha, "pending", live_url, "Building preview")

        start = time.monotonic()
        with open(log_path, "a", buffering=1, encoding="utf-8") as out:
            out.write(f"### PR #{pr} at {sha}\n### started {now_utc()}\n")
            out.flush()
            rc, timed_out = self._exec(job, ["deploy", str(pr), sha], out)
            elapsed = time.monotonic() - start
            if rc == 0:
                result = "DONE"
            elif timed_out:
                result = f"FAILED (timeout after {fmt_duration(elapsed)})"
            elif job.cancelled.is_set():
                result = "CANCELLED (superseded by a newer commit)"
            else:
                result = f"FAILED (exit code {rc})"
            out.write(f"\n### {result}\n")
        self._link_latest(log_dir, log_name)
        self._prune_logs(log_dir)
        log.info("%r: %s in %s", job, result, fmt_duration(elapsed))

        if result.startswith("CANCELLED"):
            self.status(sha, "error", raw_url, "Superseded by a newer commit")
            return
        self._state_file(pr).write_text(sha + "\n")
        if rc == 0:
            self.comment(job, f"✅ **Preview ready:** {preview_url}\n\n"
                              f"Commit `{short}` · built in {fmt_duration(elapsed)} · [build log]({raw_url})")
            self.status(sha, "success", preview_url, f"Preview ready ({fmt_duration(elapsed)})")
        else:
            tail = self._tail(log_path, 30).replace("````", "'```")
            self.comment(job, f"❌ **Preview build failed** for commit `{short}` after "
                              f"{fmt_duration(elapsed)} · [full log]({live_url})\n\n"
                              f"<details open><summary>Last 30 lines</summary>\n\n"
                              f"````text\n{tail}\n````\n</details>")
            self.status(sha, "failure", live_url, "Preview build failed")

    def _remove(self, job):
        pr = job.pr
        had_preview = (self.cfg.preview_root / str(pr)).exists()
        result = subprocess.run([self.cfg.build_script, "remove", str(pr)],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                env=self._build_env(), start_new_session=True)
        if result.returncode != 0:
            log.error("removing #%s failed: %s", pr, result.stdout + result.stderr)
            return
        self._state_file(pr).unlink(missing_ok=True)
        log.info("removed preview of #%s", pr)
        if had_preview:
            latest = self.cfg.log_root / str(pr) / "latest.log"
            logs = (f" Build logs are kept for {self.cfg.closed_log_days} days: "
                    f"[last build log]({self.cfg.base_url}/_logs/{pr}/latest.log)."
                    if latest.exists() else "")
            self.comment(job, f"🗑️ **Preview removed** (pull request closed).{logs}")

    # --- log files ------------------------------------------------------

    @staticmethod
    def _tail(path, n):
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-n:]
        return ANSI_RE.sub("", "".join(lines)).rstrip()

    @staticmethod
    def _link_latest(log_dir, log_name):
        tmp = log_dir / ".latest.tmp"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(log_name)
        tmp.replace(log_dir / "latest.log")

    def _prune_logs(self, log_dir):
        logs = sorted(p for p in log_dir.iterdir() if LOG_NAME_RE.match(p.name))
        for old in logs[:-self.cfg.logs_keep]:
            old.unlink(missing_ok=True)

    # --- reconcile ------------------------------------------------------

    def reconcile(self):
        cfg = self.cfg
        pulls = self.gh.open_pulls()
        open_prs = {p["number"] for p in pulls}
        for p in pulls:
            pr, sha = p["number"], p["head"]["sha"]
            if self.eligible(p) and self.built_sha(pr) != sha and not self.busy(pr):
                log.info("reconcile: #%s head %s not built yet", pr, sha[:7])
                self.submit(Job("deploy", pr, sha))

        leftovers = set()
        for d in (cfg.preview_root, cfg.work_root / "work"):
            if d.is_dir():
                leftovers |= {int(c.name) for c in d.iterdir() if c.name.isdigit()}
        leftovers |= {int(f.stem) for f in cfg.state_dir.glob("*.built") if f.stem.isdigit()}
        for pr in sorted(leftovers - open_prs):
            if not self.busy(pr):
                log.info("reconcile: #%s is closed, removing", pr)
                self.submit(Job("remove", pr))

        cutoff = time.time() - cfg.closed_log_days * 86400
        for d in cfg.log_root.iterdir():
            if d.is_dir() and d.name.isdigit() and int(d.name) not in open_prs:
                newest = max((f.lstat().st_mtime for f in d.iterdir()), default=0)
                if newest < cutoff:
                    log.info("reconcile: deleting logs of #%s", d.name)
                    shutil.rmtree(d, ignore_errors=True)

    def reconcile_loop(self):
        while True:
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            time.sleep(self.cfg.reconcile_interval)


def make_handler(cfg, previewer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pr-preview-hook"

        def _reply(self, code, text):
            body = (text + "\n").encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._reply(200, "pr-preview-hook is running")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._reply(413, "payload too large")
            body = self.rfile.read(length)
            expected = "sha256=" + hmac.new(cfg.webhook_secret, body, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(self.headers.get("X-Hub-Signature-256", ""), expected):
                log.warning("rejected delivery with bad signature from %s",
                            self.headers.get("X-Forwarded-For", self.client_address[0]))
                return self._reply(401, "bad signature")

            if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                body = urllib.parse.parse_qs(body.decode()).get("payload", ["{}"])[0]
            payload = json.loads(body)
            event = self.headers.get("X-GitHub-Event", "")
            if event == "ping":
                return self._reply(200, "pong")
            if event != "pull_request":
                return self._reply(202, f"ignored event {event}")
            if payload["repository"]["full_name"].lower() != cfg.repo.lower():
                return self._reply(202, "ignored repository")

            action, pull = payload["action"], payload["pull_request"]
            pr = pull["number"]
            if action in ("opened", "reopened", "synchronize"):
                if previewer.eligible(pull):
                    previewer.submit(Job("deploy", pr, pull["head"]["sha"]))
                    return self._reply(202, f"deploying #{pr}")
                if action != "synchronize":
                    threading.Thread(target=previewer.skip_notice, args=(pr,), daemon=True).start()
                return self._reply(202, f"#{pr} not eligible for a preview")
            if action == "closed":
                previewer.submit(Job("remove", pr))
                return self._reply(202, f"removing #{pr}")
            return self._reply(202, f"ignored action {action}")

        def log_message(self, fmt, *args):
            log.debug("%s %s", self.address_string(), fmt % args)

    return Handler


def main():
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(levelname)s [%(threadName)s] %(message)s")
    cfg = Config()
    gh = GitHub(cfg)
    previewer = Previewer(cfg, gh)
    threading.Thread(target=previewer.reconcile_loop, name="reconcile", daemon=True).start()

    host, port = cfg.listen.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), make_handler(cfg, previewer))
    log.info("listening on %s for %s, previews at %s", cfg.listen, cfg.repo, cfg.base_url)
    server.serve_forever()


if __name__ == "__main__":
    main()
