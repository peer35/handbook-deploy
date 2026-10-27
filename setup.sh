#!/usr/bin/env bash
# Prepare an Ubuntu (24.04+) host for the rootless PR preview container: Podman, the
# "preview" user that runs it, the nginx site, directories, the image and the quadlet.
# TLS is not handled here. Run it from a checkout of this repository, e.g.
# /opt/pr-preview. Safe to re-run: it keeps config.env and the nginx site, rebuilds the
# image and restarts the service.
#
#   sudo PREVIEW_HOST=previews.example.org ./setup.sh
#
# PREVIEW_HOST is only needed on the first run.
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
user=preview
conf=/etc/nginx/sites-available/pr-previews
need_host() { : "${PREVIEW_HOST:?set PREVIEW_HOST, e.g. PREVIEW_HOST=previews.example.org}"; }

echo "==> Packages"
apt-get update -q
apt-get install -y -q nginx openssl podman uidmap
version=$(podman --version | awk '{print $3}')
dpkg --compare-versions "$version" ge 4.4 || { echo "Podman $version is too old for quadlets (need 4.4+)" >&2; exit 1; }

echo "==> User '$user'"
id "$user" >/dev/null 2>&1 || useradd --create-home --shell /usr/sbin/nologin "$user"
for f in /etc/subuid /etc/subgid; do
  if ! grep -q "^$user:" "$f"; then
    start=$(awk -F: '{ e = $2 + $3; if (e > m) m = e } END { print (m > 100000 ? m : 100000) }' "$f")
    flag=$([ "$f" = /etc/subuid ] && echo --add-subuids || echo --add-subgids)
    usermod "$flag" "$start-$((start + 65535))" "$user"
  fi
done
loginctl enable-linger "$user"
uid=$(id -u "$user")
for _ in $(seq 30); do [ -S "/run/user/$uid/bus" ] && break; sleep 0.5; done
[ -S "/run/user/$uid/bus" ] || { echo "systemd user session for $user did not start" >&2; exit 1; }

echo "==> Directories"
install -d -o "$user" -g "$user" -m 755 /srv/pr-preview /var/www/previews
install -d -o "$user" -g "$user" -m 700 "$here/secrets"

echo "==> Config"
if [ ! -f "$here/config.env" ]; then
  need_host
  sed -e "s|PREVIEW_HOST|$PREVIEW_HOST|" -e "s|^WEBHOOK_SECRET=.*|WEBHOOK_SECRET=$(openssl rand -hex 32)|" \
    "$here/config.env.example" > "$here/config.env"
  echo "    created $here/config.env"
else
  echo "    keeping existing $here/config.env"
fi
chown "$user:$user" "$here/config.env"
chmod 600 "$here/config.env"

echo "==> nginx"
if [ ! -f "$conf" ]; then
  need_host
  sed "s|PREVIEW_HOST|$PREVIEW_HOST|" "$here/nginx/pr-previews.conf" > "$conf"
  ln -sf "$conf" /etc/nginx/sites-enabled/pr-previews
else
  echo "    keeping existing $conf"
fi
nginx -t
systemctl reload nginx

echo "==> Image"
"$here/previewctl" build

echo "==> Quadlet"
home=$(getent passwd "$user" | cut -d: -f6)
sudo -u "$user" mkdir -p "$home/.config/containers/systemd"
sed "s|@APP_DIR@|$here|g" "$here/deploy/pr-preview.container" > "$home/.config/containers/systemd/pr-preview.container"
chown "$user:$user" "$home/.config/containers/systemd/pr-preview.container"
"$here/previewctl" reload

if grep -qE '^(GITHUB_TOKEN|GITHUB_APP_ID)=.+' "$here/config.env"; then
  echo "==> Service"
  "$here/previewctl" restart
  "$here/previewctl" status --no-pager --lines=5 || true
fi

host=$(sed -n 's|^PREVIEW_BASE_URL=https\{0,1\}://||p' "$here/config.env")
cat <<EOF

Done. Next steps:
  1. Put a GitHub token in $here/config.env (GITHUB_TOKEN=...), then:
       sudo $here/previewctl restart && sudo $here/previewctl logs -f
  2. Add a webhook to the repository (Settings → Webhooks → Add webhook):
       Payload URL:  https://$host/_hooks/github
       Content type: application/json
       Secret:       $(sed -n 's/^WEBHOOK_SECRET=//p' "$here/config.env")
       Events:       "Let me select individual events" → Pull requests only
EOF
