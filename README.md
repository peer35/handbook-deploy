# Quarto PR previews on your own server

A self-hosted replacement for Netlify deploy previews. Every pull request on the
Quarto site repository is rendered on an Ubuntu/nginx server and published at
`https://<host>/<pr-number>/`. The preview is removed when the PR is closed.

```
PR opened / pushed / closed
   │  GitHub webhook (HTTPS, HMAC-signed)
   ▼
nginx (host)  /_hooks/github ──► 127.0.0.1:9000 ──► rootless Podman container "pr-preview"
                                                     (systemd user service of user "preview")
                                                     Flask app (waitress, one process)
                                                     verifies signature, replies 202, queues a job
                                                     │
                                                     ▼
                                                  pr-preview deploy <pr> <sha>
                                                     git fetch pull/<pr>/head → fix-timestamps
                                                     → quarto render → rsync _site
                                                     → /var/www/previews/<pr>/ (swap into place)
                                                     output → /var/www/previews/_logs/<pr>/…log
                                                     │
nginx (host) serves /var/www/previews ◄──────────────┘  (bind mount)
                                                     one PR comment + commit status "pr-preview":
                                                     ⏳ queued → 🔨 building (live log) → ✅ URL / ❌ last 30 lines
```

The server only needs HTTPS inbound. GitHub Actions is not involved. Quarto, git
and the Flask app all run inside the container. nginx runs on the host and serves
the rendered previews and logs straight from disk.

**The container runs rootless** under an unprivileged `preview` user. A build runs the
PR's own code (Quarto filters and code cells), so even a container escape would only
reach that user. On top of that, the container has a read-only filesystem, drops all
capabilities and sets `no-new-privileges`.

## Files

| Path | Purpose |
|---|---|
| `Containerfile` | Image: Python 3.12, Quarto (`QUARTO_VERSION`), git, rsync, the app. Target `test` runs the tests. |
| `deploy/pr-preview.container` | Podman quadlet: the systemd user service that runs the container, with its hardening, mounts and port |
| `previewctl` | Build, test, start/stop and logs, run as the `preview` user |
| `setup.sh` | Host setup: Podman, the `preview` user, nginx site, directories, `config.env`, image, quadlet |
| `pr_preview/web.py` | Flask app: the webhook endpoint |
| `pr_preview/previewer.py` | Job queue, cancelling superseded builds, PR comment, reconcile loop |
| `pr_preview/github.py` | GitHub REST calls: token or GitHub App authentication |
| `pr_preview/static/view.html` | Live log viewer. Copied to `/var/www/previews/_logs/` at startup. |
| `bin/pr-preview` | Build script: `deploy <pr> <sha>` / `remove <pr>` |
| `nginx/pr-previews.conf` | Host nginx site: previews, logs, webhook proxy |
| `config.env.example` | Settings and secrets. Copied to `config.env`, which is not committed. |
| `tests/` | End-to-end tests of the app with a fake GitHub and a stub build |

### Host directories
These are owned by the `preview` user. The quadlet's `UserNS=keep-id` maps the
container user (uid 10001) to `preview`.
- `/var/www/previews/`: one folder per PR, plus `_logs/`. nginx reads it.
- `/srv/pr-preview/`:
  - `repo.git`: the bare mirror.
  - `work/<pr>`: one git worktree per PR.
  - `state/<pr>.built`: the last commit built for each PR.
  - `home/`: Quarto's cache.
- `~preview/.config/containers/systemd/pr-preview.container`: the installed quadlet.
- The image is in `preview`'s own Podman storage. Rootless images are per user, so
  `sudo podman images` won't show it. Use `previewctl`.

## Setup

**Requirements:** Ubuntu 24.04 or newer. Quadlets need Podman 4.4+, and Ubuntu 24.04 ships 4.9.

### 1. DNS and TLS
Point a hostname (for example `previews.example.org`) at the server. TLS is not set up by
this repository. Provide it the way your environment handles certificates, either in
front of nginx or in the `pr-previews` nginx site. GitHub must reach the webhook URL over
HTTPS.

### 2. Server
Clone this repository on the server and run the setup script:

```bash
sudo git clone <this repo> /opt/pr-preview && cd /opt/pr-preview
sudo PREVIEW_HOST=previews.example.org ./setup.sh
```

The script:
1. Installs Podman and nginx.
2. Creates the `preview` user, with subordinate uids and lingering enabled so its
   services start at boot.
3. Creates the directories and the nginx site (`/etc/nginx/sites-available/pr-previews`,
   listening on port 80).
4. Writes a `config.env` with a random webhook secret.
5. Builds the image (this downloads Quarto and takes a few minutes) and installs the quadlet.

### 3. GitHub token
Create a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new):
- **Repository access:** only the site repository.
- **Permissions:** *Pull requests* read and write, and *Commit statuses* read and write.

Put the token in `config.env` as `GITHUB_TOKEN=…`, then start the service:

```bash
sudo ./previewctl restart
sudo ./previewctl logs -f
```

### 4. Webhook
In the repository, go to **Settings → Webhooks → Add webhook** and set:
- **Payload URL:** `https://previews.example.org/_hooks/github`
- **Content type:** `application/json`
- **Secret:** the `WEBHOOK_SECRET` from `config.env` (`setup.sh` prints it)
- **Events:** "Let me select individual events", then tick **Pull requests** only.

GitHub sends a `ping` straight away. The *Recent Deliveries* tab should show a 200 response.

## Behaviour

- **Opened, reopened or new commits:** the PR is queued and built, then published at
  `/<pr>/`. A newer push to the same PR cancels the running build. At most `MAX_PARALLEL`
  builds (default 1) run at once.
- **Closed or merged:** the preview and the worktree are deleted. The logs are kept for
  `CLOSED_LOG_DAYS` (default 14).
- **One comment per PR:** it's found by a hidden `<!-- pr-preview -->` marker and edited
  only when the status changes.
- **Logs:** `https://<host>/_logs/<pr>/<time>-<sha>.log` is plain text, and
  `/_logs/view.html?pr=…&log=…` follows it live. `/_logs/<pr>/latest.log` always points at
  the newest log, including a build that's still running. The last `LOGS_KEEP` logs
  (default 10) are kept for each PR.
- **Reconcile:** this runs at startup and then every `RECONCILE_INTERVAL` seconds (default
  1 hour). It builds any open PR whose head commit was never built, and removes previews
  of PRs that are closed. GitHub does not retry failed webhook deliveries, so this is the
  safety net for downtime and restarts. A commit that failed to build is not retried
  automatically. Push again, or re-deliver the webhook from GitHub.
- **Who gets a preview:** PRs from branches in the repository itself, or opened by an
  `OWNER`, `MEMBER` or `COLLABORATOR` (`ALLOWED_ASSOCIATIONS`). For other fork PRs the
  service posts a "preview skipped" comment.
- **`site-url`:** each build renders with a generated Quarto profile (`QUARTO_PROFILE=pr-preview`)
  that sets `website.site-url` to the preview URL. Quarto already turns root-absolute links
  (`/topics.html`, `/public/...`) into relative ones, so the site works under `/<pr>/` without changes.
- **One process only:** the app runs as a single waitress process with threads. Running builds
  are tracked in memory, which is how a new push finds and cancels the old build. Don't run more
  than one container, and don't switch to a multi-process server.

## Operations

All commands are run from the repository directory as root. `previewctl` switches to the `preview` user.

| Task | Command |
|---|---|
| Status | `sudo ./previewctl status` |
| Logs | `sudo ./previewctl logs -f`, or `sudo ./previewctl logs --since today` |
| Restart | `sudo ./previewctl restart` |
| Update the app | `sudo git pull && sudo ./setup.sh` (rebuilds the image, reinstalls the quadlet, restarts) |
| Update Quarto | `sudo QUARTO_VERSION=x.y.z ./previewctl build && sudo ./previewctl restart` |
| Run the tests | `sudo ./previewctl test` |
| Rebuild one PR | Delete `/srv/pr-preview/state/<pr>.built` and restart, or *Redeliver* the webhook in GitHub |

The default Quarto version is set in `Containerfile` and `previewctl`. Restarting kills a
running build, and the reconcile job at startup rebuilds it.

## Testing checklist

Run these against the test repository:

1. Open a PR. The comment should go from ⏳ to 🔨 (with a live log link) to ✅ (with the URL), and the preview should load.
2. Push another commit. The same comment should update, and the preview should show the change.
3. Push two commits quickly. The first build's log should end with `### CANCELLED`, and the second build should be the one published.
4. Push a broken `.qmd` (for example invalid YAML front matter). The comment should show ❌ with the last 30 log lines.
5. Close the PR. `/<pr>/` should return 404 and the comment should say 🗑️.
6. Send a delivery with a wrong secret. The response should be 401.
7. Run `previewctl stop`, push to a PR, then `previewctl start`. The reconcile job should build the new commit.
8. Reboot the server. The service should come back on its own, because lingering is enabled.

## Switching to a GitHub App

With an App, comments show up as a bot account and no personal token is needed:

1. Create a GitHub App with these permissions: *Pull requests* read and write, *Commit
   statuses* read and write, *Metadata* read. Subscribe it to **Pull request** events, set
   its webhook URL and secret to the values above, and install it on the repository.
2. Save the App's private key as `secrets/app.pem`:
   ```bash
   sudo install -m 600 -o preview -g preview app-key.pem secrets/app.pem
   ```
3. In `config.env`, clear `GITHUB_TOKEN` and set `GITHUB_APP_ID=…` and
   `GITHUB_APP_KEY_FILE=/secrets/app.pem`.
4. Remove the repository webhook, since the App now delivers the events. Then run
   `sudo ./previewctl restart`.

The service doesn't take over comments posted by the old token. The first build after the
switch posts a new comment.

## Limits

- **Public repositories only:** the mirror is cloned anonymously over HTTPS. A private
  repository needs credentials for git inside the container.
- **Logs are public:** they are as public as the previews themselves. No secrets reach the
  build, because the build script gets a minimal environment without the GitHub token.
  Add basic auth to the nginx server block if the previews themselves should be private.
