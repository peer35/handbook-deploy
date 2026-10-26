# Quarto PR previews on your own server

A self-hosted replacement for Netlify deploy previews. Every pull request on the
Quarto site repository is rendered on an Ubuntu/nginx server and published at
`https://<host>/<pr-number>/`. The preview is removed when the PR is closed.

```
PR opened / pushed / closed
   │  GitHub webhook (HTTPS, HMAC-signed)
   ▼
nginx  /_hooks/github ──► pr_preview_hook.py (systemd, user "preview")
                             │ verifies signature, replies 202, queues a job
                             ▼
                          pr-preview deploy <pr> <sha>
                             git fetch pull/<pr>/head → fix-timestamps → quarto render
                             → rsync _site → /var/www/previews/<pr>/ (swap into place)
                             output → /var/www/previews/_logs/<pr>/<time>-<sha>.log
                             │
                             ▼
                          one PR comment + commit status "pr-preview"
                          ⏳ queued → 🔨 building (live log link) → ✅ preview URL / ❌ last 30 log lines
```

The server only needs HTTPS inbound. GitHub Actions is not involved.

## Files

| Path | Installed to | Purpose |
|---|---|---|
| `server/pr-preview` | `/usr/local/bin/pr-preview` | Build script: `deploy <pr> <sha>` / `remove <pr>` |
| `server/hook/pr_preview_hook.py` | `/opt/pr-preview/` | Webhook receiver, job queue, GitHub comments, reconcile loop |
| `server/www/view.html` | `/var/www/previews/_logs/view.html` | Live log viewer (follows the log while it is written) |
| `server/nginx/pr-previews.conf` | `/etc/nginx/sites-available/pr-previews` | Serves previews, logs and the webhook endpoint |
| `server/systemd/pr-preview-hook.service` | `/etc/systemd/system/` | Runs the receiver, sandboxed |
| `server/config.env.example` | `/etc/pr-preview/config.env` | Settings and secrets |
| `server/setup.sh` | – | Installs all of the above |

On the server, runtime data lives in `/srv/pr-preview/`:
- `repo.git` is a bare mirror.
- `work/<pr>` holds one git worktree per PR.
- `state/<pr>.built` records the last commit built for each PR.
- `locks/` holds the lock files.

## Setup

### 1. DNS
Point a hostname (for example `previews.example.org`) at the server, and open ports 80 and 443.

### 2. Server
Copy the `server/` directory to the server, then run:

```bash
sudo PREVIEW_HOST=previews.example.org CERTBOT_EMAIL=you@example.org ./setup.sh
```

This installs git, rsync, nginx, certbot, Python and the latest Quarto release (set
`QUARTO_VERSION=` to pin a version). It also creates the `preview` user, the directories,
the nginx site and the TLS certificate. The script can be re-run safely. It keeps your
`config.env` and nginx site file, and only updates the scripts.

### 3. GitHub token
Create a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new):
- **Repository access:** only `peer35/open-handbook-test`.
- **Permissions:** *Pull requests* read and write, and *Commit statuses* read and write.

Put the token in `/etc/pr-preview/config.env` as `GITHUB_TOKEN=…`, then run:

```bash
sudo systemctl restart pr-preview-hook
journalctl -u pr-preview-hook -f
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
  the newest log. The last `LOGS_KEEP` logs (default 10) are kept for each PR.
- **Reconcile:** this runs at startup and then every `RECONCILE_INTERVAL` seconds (default
  1 hour). It builds any open PR whose head commit was never built, and removes previews
  of PRs that are closed. GitHub does not retry failed webhook deliveries, so this is the
  safety net for downtime and restarts. A commit that failed to build is not retried
  automatically. Push again, or re-deliver the webhook from GitHub.
- **Who gets a preview:** PRs from branches in the repository itself, or opened by an
  `OWNER`, `MEMBER` or `COLLABORATOR` (`ALLOWED_ASSOCIATIONS`). For other fork PRs the
  service posts a "preview skipped" comment, because rendering runs the PR's code
  (Lua filters and code cells) on your server.
- **`site-url`:** each build renders with a generated Quarto profile (`QUARTO_PROFILE=pr-preview`)
  that sets `website.site-url` to the preview URL, so the sitemap, feeds and canonical links
  point at the preview. Quarto already turns root-absolute links in the site (`/topics.html`,
  `/public/...`) into relative ones, so the site works under `/<pr>/` without changes.

## Testing checklist

1. Open a PR. The comment should go from ⏳ to 🔨 (with a live log link) to ✅ (with the URL), and the preview should load.
2. Push another commit. The same comment should update, and the preview should show the change.
3. Push two commits quickly. The first build's log should end with `### CANCELLED`, and the second build should be the one published.
4. Push a broken `.qmd` (for example an unclosed `:::` div or invalid YAML). The comment should show ❌ with the last 30 log lines.
5. Close the PR. `/<pr>/` should return 404 and the comment should say 🗑️.
6. Re-deliver a webhook with a changed body, or send one with a wrong secret. The response should be 401.
7. Run `systemctl stop pr-preview-hook`, push to a PR, then `start` it again. The reconcile job should build the new commit.

## Operations

- **Service logs:** `journalctl -u pr-preview-hook -f`
- **Manual rebuild:** delete `/srv/pr-preview/state/<pr>.built` and restart the service, or use *Redeliver* on the webhook delivery in GitHub.
- **Manual build from the shell:** `sudo -u preview env REPO_URL=… PREVIEW_BASE_URL=… pr-preview deploy <pr> <sha>`
- **Updating Quarto:** `sudo QUARTO_VERSION=x.y.z PREVIEW_HOST=… ./setup.sh`

## Switching to a GitHub App

With an App, comments show up as a bot account and no personal token is needed:

1. Create a GitHub App with these permissions: *Pull requests* read and write, *Commit
   statuses* read and write, *Metadata* read. Subscribe it to **Pull request** events, set
   its webhook URL and secret to the values above, and install it on the repository.
2. Save the App's private key as `/etc/pr-preview/app.pem` (`root:preview`, mode 640).
3. In `config.env`, clear `GITHUB_TOKEN` and set `GITHUB_APP_ID` and `GITHUB_APP_KEY_FILE`.
4. Remove the repository webhook, since the App now delivers the events, and restart the
   service.

The service doesn't take over comments posted by the old token. The first build after the
switch posts a new comment.

## Limits

- **Public repositories only:** the mirror is cloned anonymously over HTTPS. A private
  repository needs a read-only deploy key and `REPO_URL=git@github.com:…`.
- **Logs are public:** they are as public as the previews themselves. No secrets reach the
  build, because the build script gets a minimal environment without the GitHub token.
  Add basic auth to the nginx server block if the previews themselves should be private.
