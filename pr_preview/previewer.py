"""Build/remove jobs for PR previews, the PR comment, and the reconcile loop.

Each PR has at most one current job; submitting a new one cancels the previous
(killing its build). Build output goes to a log file that nginx serves; the PR
comment and commit status are updated only when the state changes.
"""

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LOG_NAME_RE = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{7}\.log$")
VIEWER_HTML = Path(__file__).parent / "static" / "view.html"


class Job:
    def __init__(self, kind, pr, sha=None):
        self.kind = kind  # "deploy" | "remove" | "skip"
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
        shutil.copyfile(VIEWER_HTML, cfg.log_root / "view.html")

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

    def busy(self, pr=None):
        with self._lock:
            return pr in self._current if pr is not None else bool(self._current)

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
        self.comment(job, f"⏳ **Preview queued** for commit `{job.sha[:7]}`.")
        while not self._slots.acquire(timeout=1):
            if job.cancelled.is_set():
                return
        try:
            if not job.cancelled.is_set():
                self._build(job)
        finally:
            self._slots.release()

    def _build(self, job):
        cfg, pr, sha, short = self.cfg, job.pr, job.sha, job.sha[:7]
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
            self._link_latest(log_dir, log_name)
            rc, timed_out = self._exec(job, ["deploy", str(pr), sha], out)
            elapsed = time.monotonic() - start
            if rc == 0:
                result = "DONE"
            elif timed_out:
                result = f"FAILED (timeout after {fmt_duration(elapsed)})"
            elif job.cancelled.is_set():
                result = "CANCELLED (superseded by a newer event)"
            else:
                result = f"FAILED (exit code {rc})"
            out.write(f"\n### {result}\n")
        self._prune_logs(log_dir)
        log.info("%r: %s in %s", job, result, fmt_duration(elapsed))

        if result.startswith("CANCELLED"):
            self.status(sha, "error", raw_url, "Cancelled: superseded by a newer event")
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
