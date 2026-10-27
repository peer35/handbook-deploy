#!/usr/bin/env bash
# Prepare an Ubuntu host for the PR preview container: Docker, nginx (+ TLS) and the
# directories the container mounts. Run it from a checkout of this repository, e.g.
# /opt/pr-preview. Safe to re-run; keeps an existing config.env and nginx site.
#
#   sudo PREVIEW_HOST=previews.example.org CERTBOT_EMAIL=you@example.org ./setup.sh
#
# CERTBOT_EMAIL unset = skip the TLS certificate.
set -euo pipefail

: "${PREVIEW_HOST:?set PREVIEW_HOST, e.g. PREVIEW_HOST=previews.example.org}"
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
ID=10001  # uid/gid of the container user, see Dockerfile

echo "==> Packages"
apt-get update -q
apt-get install -y -q nginx certbot python3-certbot-nginx openssl
if ! command -v docker >/dev/null; then
  apt-get install -y -q docker.io docker-compose-v2
fi
docker compose version

echo "==> Directories"
install -d -o $ID -g $ID -m 755 /srv/pr-preview /srv/pr-preview/home /var/www/previews /var/www/previews/_logs
install -d -o root -g $ID -m 750 "$here/secrets"

echo "==> Config"
if [ ! -f "$here/config.env" ]; then
  sed -e "s|PREVIEW_HOST|$PREVIEW_HOST|" -e "s|^WEBHOOK_SECRET=.*|WEBHOOK_SECRET=$(openssl rand -hex 32)|" \
    "$here/config.env.example" > "$here/config.env"
  chmod 600 "$here/config.env"
  echo "    created $here/config.env"
else
  echo "    keeping existing $here/config.env"
fi

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

if grep -qE '^(GITHUB_TOKEN|GITHUB_APP_ID)=.+' "$here/config.env"; then
  echo "==> Container"
  (cd "$here" && docker compose up -d --build)
fi

cat <<EOF

Done. Next steps:
  1. Put a GitHub token in $here/config.env (GITHUB_TOKEN=...), then:
       cd $here && sudo docker compose up -d --build && sudo docker compose logs -f
  2. Add a webhook to the repository (Settings → Webhooks → Add webhook):
       Payload URL:  https://$PREVIEW_HOST/_hooks/github
       Content type: application/json
       Secret:       $(sed -n 's/^WEBHOOK_SECRET=//p' "$here/config.env")
       Events:       "Let me select individual events" → Pull requests only
EOF
