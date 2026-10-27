"""Settings, read from environment variables (see config.env.example)."""

import os
from pathlib import Path


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
        self.listen = env("LISTEN", "0.0.0.0:9000")
        self.threads = int(env("THREADS", "8"))
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
