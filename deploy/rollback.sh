#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf '%s\n' "rollback aborted: $1" >&2
    exit 1
}

EXPECTED_APP_DIR="/home/ubuntu/limit-order-book"
APP_DIR="$(realpath -e -- "$EXPECTED_APP_DIR")" || fail "application directory is unavailable"
[[ "$APP_DIR" == "$EXPECTED_APP_DIR" ]] || fail "application directory does not match"
[[ -d "$APP_DIR" ]] || fail "application directory is not a directory"

AVAILABLE_SITE="/etc/nginx/sites-available/lob-dashboard"
ENABLED_SITE="/etc/nginx/sites-enabled/lob-dashboard"
AUTH_FILE="/etc/nginx/lob-dashboard.htpasswd"
HASH_CONF="/etc/nginx/conf.d/lob-server-names-hash.conf"

for target in "$AVAILABLE_SITE" "$ENABLED_SITE" "$AUTH_FILE" "$HASH_CONF"; do
    if [[ "$target" != "$AVAILABLE_SITE" && "$target" != "$ENABLED_SITE" && "$target" != "$AUTH_FILE" && "$target" != "$HASH_CONF" ]]; then
        fail "unexpected deletion target"
    fi
done

targets_to_remove=()

if [[ -e "$AVAILABLE_SITE" || -L "$AVAILABLE_SITE" ]]; then
    [[ ! -L "$AVAILABLE_SITE" && -f "$AVAILABLE_SITE" ]] || fail "available site is not a regular file"
    targets_to_remove+=("$AVAILABLE_SITE")
fi

if [[ -e "$ENABLED_SITE" || -L "$ENABLED_SITE" ]]; then
    [[ -L "$ENABLED_SITE" ]] || fail "enabled site is not a symbolic link"
    [[ "$(readlink -- "$ENABLED_SITE")" == "$AVAILABLE_SITE" ]] || fail "enabled site does not target the dedicated available site"
    targets_to_remove+=("$ENABLED_SITE")
fi

if [[ -e "$AUTH_FILE" || -L "$AUTH_FILE" ]]; then
    [[ ! -L "$AUTH_FILE" && -f "$AUTH_FILE" ]] || fail "auth file is not a regular file"
    targets_to_remove+=("$AUTH_FILE")
fi

if [[ -e "$HASH_CONF" || -L "$HASH_CONF" ]]; then
    [[ ! -L "$HASH_CONF" && -f "$HASH_CONF" ]] || fail "server-name hash config is not a regular file"
    [[ "$(cat -- "$HASH_CONF")" == "server_names_hash_bucket_size 128;" ]] || fail "server-name hash config has unexpected content"
    targets_to_remove+=("$HASH_CONF")
fi

cd "$APP_DIR"
docker compose -p limit-order-book -f deploy/docker-compose.production.yml --env-file deploy/demo.env down

if (( ${#targets_to_remove[@]} > 0 )); then
    sudo rm -- "${targets_to_remove[@]}"
fi
sudo nginx -t
sudo systemctl reload nginx

printf '%s\n' "Optional certificate deletion (not executed): sudo certbot delete --cert-name dashboard.185-206-95-141.nip.io"
