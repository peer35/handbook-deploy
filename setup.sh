#!/usr/bin/env bash
# Provision an Ubuntu server for Quarto PR previews. Safe to re-run (updates scripts,
# keeps an existing config.env).
#
#   sudo PREVIEW_HOST=previews.example.org CERTBOT_EMAIL=you@example.org ./setup.sh
#
# Optional: QUARTO_VERSION=1.8.26 (default: latest release), CERTBOT_EMAIL unset = skip TLS.
set -euo pipefail

: "${PREVIEW_HOST:?set PREVIEW_HOST, e.g. PREVIEW_HOST=previews.example.org}"
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)

echo "==> Packages"
apt-get update -q
apt-get install -y -q git rsync curl python3 python3-jwt python3-cryptography nginx certbot python3-certbot-nginx

if ! command -v quarto >/dev/null || [ -n "${QUARTO_VERSION:-}" ]; then
  version=${QUARTO_VERSION:-$(curl -fsSL https://api.github.com/repos/quarto-dev/quarto-cli/releases/latest \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')}
  arch=$(dpkg --print-architecture)
  echo "==> Quarto $version ($arch)"
  deb=$(mktemp --suffix=.deb)
  curl -fsSL -o "$deb" "https://github.com/quarto-dev/quarto-cli/releases/download/v$version/quarto-$version-linux-$arch.deb"
  apt-get install -y -q "$deb"
  rm -f "$deb"
fi
quarto --version

echo "==> User and directories"
id preview >/dev/null 2>&1 || useradd --system --home-dir /srv/pr-preview --shell /usr/sbin/nologin preview
install -d -o preview -g preview -m 755 /srv/pr-preview /srv/pr-preview/home /var/www/previews /var/www/previews/_logs

echo "==> Scripts"
install -m 755 "$here/pr-preview" /usr/local/bin/pr-preview
install -d -m 755 /opt/pr-preview
install -m 644 "$here/hook/pr_preview_hook.py" /opt/pr-preview/pr_preview_hook.py
install -m 644 -o preview -g preview "$here/www/view.html" /var/www/previews/_logs/view.html

echo "==> Config"
install -d -m 750 -g preview /etc/pr-preview
if [ ! -f /etc/pr-preview/config.env ]; then
  secret=$(openssl rand -hex 32)
  sed -e "s|PREVIEW_HOST|$PREVIEW_HOST|" -e "s|^WEBHOOK_SECRET=.*|WEBHOOK_SECRET=$secret|" \
    "$here/config.env.example" > /etc/pr-preview/config.env
  chown root:preview /etc/pr-preview/config.env
  chmod 640 /etc/pr-preview/config.env
  echo "    created /etc/pr-preview/config.env"
else
  echo "    keeping existing /etc/pr-preview/config.env"
fi

echo "==> systemd"
install -m 644 "$here/systemd/pr-preview-hook.service" /etc/systemd/system/pr-preview-hook.service
systemctl daemon-reload
systemctl enable pr-preview-hook.service

echo "==> nginx"
conf=/etc/nginx/sites-available/pr-previews
if [ ! -f "$conf" ]; then
  sed "s|PREVIEW_HOST|$PREVIEW_HOST|" "$here/nginx/pr-previews.conf" > "$conf"
  ln -sf "$conf" /etc/nginx/sites-enabled/pr-previews
else
  echo "    keeping existing $conf (certbot may have edited it)"
fi
nginx -t
systemctl reload nginx

if [ -n "${CERTBOT_EMAIL:-}" ]; then
  echo "==> TLS certificate"
  certbot --nginx -d "$PREVIEW_HOST" --non-interactive --agree-tos -m "$CERTBOT_EMAIL" --redirect
fi

if grep -q '^GITHUB_TOKEN=.\+' /etc/pr-preview/config.env || grep -q '^GITHUB_APP_ID=.\+' /etc/pr-preview/config.env; then
  systemctl restart pr-preview-hook.service
  echo "==> pr-preview-hook restarted"
fi

cat <<EOF

Done. Next steps:
  1. Put a GitHub token in /etc/pr-preview/config.env (GITHUB_TOKEN=...), then:
       sudo systemctl restart pr-preview-hook && journalctl -u pr-preview-hook -f
  2. Add a webhook to the repository (Settings → Webhooks → Add webhook):
       Payload URL:  https://$PREVIEW_HOST/_hooks/github
       Content type: application/json
       Secret:       $(sed -n 's/^WEBHOOK_SECRET=//p' /etc/pr-preview/config.env)
       Events:       "Let me select individual events" → Pull requests only
EOF
