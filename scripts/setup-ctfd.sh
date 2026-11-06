#!/bin/bash
set -euo pipefail

# setup-ctfd.sh — Fully idempotent CTFd bootstrap + team import.
#
# Handles TWO scenarios:
#   1. Fresh CTFd (/setup → 200) → creates admin + token via Python ORM,
#      saves credentials to ENV_FILE, restarts pods, then applies config.
#   2. Already-configured CTFd (/setup → 302) → loads token from ENV_FILE,
#      validates it, and applies config (idempotent).
#
# After setup, if TEAMS_CSV is set, teams are imported automatically.
#
# Normally run through:  make ctfd-setup   (scripts/ctfd-setup.sh sets the env)
# With team import:      make ctfd-setup TEAMS_CSV=data/teams.csv
#
# Required:
#   CTFD_URL          — e.g. https://192.168.1.50
#
# Optional (with defaults):
#   CTFD_ADMIN_NAME     — admin display name        (default: admin)
#   CTFD_ADMIN_EMAIL    — admin email                (default: admin@ctf.local)
#   CTFD_ADMIN_PASSWORD — admin password             (default: auto-generated)
#   CTFD_EVENT_NAME     — CTF event name
#   CTFD_NAMESPACE      — K8s namespace for CTFd     (default: ctfd)
#   TEAMS_CSV           — path to teams CSV file     (default: empty, skips import)
#   ENV_FILE            — credentials file path      (default: .secrets/ctfd-admin.env)
#   INGRESS_IP          — LB IP to bypass DNS        (optional)
#   POLL_INTERVAL       — readiness poll interval    (default: 5s)
#   POLL_TIMEOUT        — readiness poll timeout     (default: 300s)

# ---------------------------------------------------------------------------
# Step 0: Operational params from env/defaults (command-line wins for these)
# ---------------------------------------------------------------------------
CTFD_URL="${CTFD_URL:-}"
CTFD_ADMIN_NAME="${CTFD_ADMIN_NAME:-admin}"
CTFD_ADMIN_EMAIL="${CTFD_ADMIN_EMAIL:-admin@ctf.local}"
CTFD_ADMIN_PASSWORD="${CTFD_ADMIN_PASSWORD:-}"
CTFD_EVENT_NAME="${CTFD_EVENT_NAME:-}"
CTFD_NAMESPACE="${CTFD_NAMESPACE:-ctfd}"
TEAMS_CSV="${TEAMS_CSV:-}"
INGRESS_IP="${INGRESS_IP:-}"
POLL_INTERVAL="${POLL_INTERVAL:-5}"
POLL_TIMEOUT="${POLL_TIMEOUT:-300}"
ENV_FILE="${ENV_FILE:-.secrets/ctfd-admin.env}"
PYTHON="${PYTHON:-python3}"

# Credentials (token/password) come from ENV_FILE — it always wins over the
# shell environment, ensuring stale exports from previous deploys don't interfere.
# Operational params above are NOT stored in ENV_FILE so they remain overridable.
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  echo "INFO: Loaded credentials from $ENV_FILE"
fi

# If token still not set after file load, fall back to shell env
CTFD_ADMIN_TOKEN="${CTFD_ADMIN_TOKEN:-}"

if [[ -z "$CTFD_URL" ]]; then
  echo "ERROR: CTFD_URL is not set." >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# DNS bypass: if INGRESS_IP is set, route requests directly to the LB IP.
# Useful on fresh deploys before DNS propagates.
# ---------------------------------------------------------------------------
CURL_RESOLVE_OPTS=()
if [[ -n "$INGRESS_IP" ]]; then
  CTFD_HOST="${CTFD_URL#http://}"; CTFD_HOST="${CTFD_HOST#https://}"; CTFD_HOST="${CTFD_HOST%%/*}"
  CURL_RESOLVE_OPTS+=(--resolve "${CTFD_HOST}:80:${INGRESS_IP}")
  CURL_RESOLVE_OPTS+=(--resolve "${CTFD_HOST}:443:${INGRESS_IP}")
  echo "INFO: DNS bypass active — routing $CTFD_HOST → $INGRESS_IP"
fi

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
RESP_TMP=$(mktemp "${TMPDIR:-/tmp}/ctfd_resp_XXXXXX")
trap 'rm -f "$RESP_TMP"' EXIT

ctfd_api() {
  local method="$1" endpoint="$2" data="${3:-}"
  local args=(-s -w "%{http_code}" -o "$RESP_TMP"
    "${CURL_RESOLVE_OPTS[@]+"${CURL_RESOLVE_OPTS[@]}"}"
    -X "$method"
    -H "Authorization: Token $CTFD_ADMIN_TOKEN"
    -H "Content-Type: application/json")
  [[ -n "$data" ]] && args+=(-d "$data")
  # Retry gateway errors: right after a CTFd restart, Traefik can still route
  # to a pod that is shutting down for a few seconds.
  local code attempt
  for attempt in 1 2 3 4 5 6; do
    code=$(curl "${args[@]}" "${CTFD_URL}${endpoint}") || true
    code="${code:-000}"
    case "$code" in
      502|503|504|000) [[ $attempt -lt 6 ]] && sleep 5 ;;
      *) break ;;
    esac
  done
  echo "$code"
}

wait_for_ctfd() {
  local label="${1:-CTFd}"
  local attempt=0 max_attempts=$(( POLL_TIMEOUT / POLL_INTERVAL ))
  echo "Waiting for $label ..."
  while [[ $attempt -lt $max_attempts ]]; do
    attempt=$((attempt + 1))
    local http
    http=$(curl -sk -o /dev/null -w "%{http_code}" \
      "${CURL_RESOLVE_OPTS[@]+"${CURL_RESOLVE_OPTS[@]}"}" \
      "$CTFD_URL/healthcheck" || true)
    if [[ "$http" == "200" ]]; then
      echo "$label ready (attempt ${attempt}/${max_attempts})."
      return 0
    fi
    sleep "$POLL_INTERVAL"
  done
  echo "ERROR: $label not ready after ${POLL_TIMEOUT}s." >&2
  return 1
}

save_env_file() {
  local tmp
  tmp=$(mktemp "${TMPDIR:-/tmp}/ctfd_env_XXXXXX")
  cat > "$tmp" <<EOF
# CTFd credentials — written by setup-ctfd.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Sourced automatically on every run of setup-ctfd.sh and import-teams.py.
# Do NOT add CTFD_URL here — pass it as an env var so it stays overridable.
export CTFD_ADMIN_TOKEN="$CTFD_ADMIN_TOKEN"
export CTFD_ADMIN_NAME="$CTFD_ADMIN_NAME"
export CTFD_ADMIN_EMAIL="$CTFD_ADMIN_EMAIL"
export CTFD_ADMIN_PASSWORD="$CTFD_ADMIN_PASSWORD"
EOF
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
  echo "INFO: Credentials saved to $ENV_FILE (mode 600)"
}

apply_config() {
  local label="$1" payload="$2"
  echo "Configuring $label..."
  local resp
  resp=$(ctfd_api PATCH /api/v1/configs "$payload")
  if [[ ! "$resp" =~ ^20[0-9]$ ]]; then
    echo "ERROR: Failed to configure $label (HTTP $resp)." >&2
    cat "$RESP_TMP" >&2
    exit 2
  fi
  echo "Configuring $label... OK"
}

# ---------------------------------------------------------------------------
# Step 1: Wait for CTFd to be reachable
# ---------------------------------------------------------------------------
wait_for_ctfd "CTFd"

# ---------------------------------------------------------------------------
# Step 2: Detect fresh install vs already configured
# ---------------------------------------------------------------------------
SETUP_HTTP=$(curl -s -o /dev/null -w "%{http_code}" \
  "${CURL_RESOLVE_OPTS[@]+"${CURL_RESOLVE_OPTS[@]}"}" "$CTFD_URL/setup")

if [[ "$SETUP_HTTP" == "200" ]]; then
  echo ""
  echo "=== Fresh CTFd detected — running initial setup ==="
  echo ""

  if [[ -z "$CTFD_ADMIN_PASSWORD" ]]; then
    CTFD_ADMIN_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(16))")
    echo "INFO: Generated admin password — saved to $ENV_FILE (value hidden from logs)"
  fi

  CTFD_POD=$(kubectl -n "$CTFD_NAMESPACE" get pods -l app=ctfd \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  if [[ -z "$CTFD_POD" ]]; then
    echo "ERROR: No CTFd pod found in namespace '$CTFD_NAMESPACE'." >&2
    exit 1
  fi
  echo "Using pod: $CTFD_POD"

  # All values injected via env (not string-interpolated) to safely handle
  # special characters in passwords, names, and email addresses.
  SETUP_OUTPUT=$(
    kubectl -n "$CTFD_NAMESPACE" exec "$CTFD_POD" \
      -- env \
        "SETUP_ADMIN_NAME=$CTFD_ADMIN_NAME" \
        "SETUP_ADMIN_EMAIL=$CTFD_ADMIN_EMAIL" \
        "SETUP_ADMIN_PASSWORD=$CTFD_ADMIN_PASSWORD" \
        "SETUP_EVENT_NAME=$CTFD_EVENT_NAME" \
      python3 -c '
import sys, os, secrets, datetime
sys.path.insert(0, "/opt/CTFd")
os.chdir("/opt/CTFd")

from CTFd import create_app
from CTFd.utils.crypto import hash_password
from CTFd.models import db

admin_name     = os.environ["SETUP_ADMIN_NAME"]
admin_email    = os.environ["SETUP_ADMIN_EMAIL"]
admin_password = os.environ["SETUP_ADMIN_PASSWORD"]
event_name     = os.environ["SETUP_EVENT_NAME"]

app = create_app()
with app.app_context():
    user_count = db.session.execute(db.text("SELECT COUNT(*) FROM users")).scalar()
    if user_count > 0:
        print("ALREADY_SETUP")
        sys.exit(0)

    pw_hash = hash_password(admin_password)
    db.session.execute(db.text(
        "INSERT INTO users (name, email, password, type, verified, hidden, banned) "
        "VALUES (:n, :e, :p, :t, :v, :h, :b)"
    ), {"n": admin_name, "e": admin_email,
        "p": pw_hash, "t": "admin", "v": 1, "h": 0, "b": 0})
    db.session.flush()

    uid = db.session.execute(
        db.text("SELECT id FROM users WHERE email=:e"), {"e": admin_email}
    ).scalar()

    for k, val in [
        ("ctf_name",                event_name),
        ("user_mode",               "teams"),
        ("registration_visibility", "private"),
        ("setup",                   "True"),
    ]:
        db.session.execute(db.text(
            "INSERT INTO config (`key`, value) VALUES (:k, :v) "
            "ON DUPLICATE KEY UPDATE value = :v"
        ), {"k": k, "v": val})

    expiry = datetime.datetime.utcnow() + datetime.timedelta(days=365)
    token_value = "ctfd_" + secrets.token_hex(32)
    db.session.execute(db.text(
        "INSERT INTO tokens (type, user_id, created, expiration, value, description) "
        "VALUES (:t, :uid, :c, :e, :v, :d)"
    ), {"t": "user", "uid": uid,
        "c": datetime.datetime.utcnow(),
        "e": expiry,
        "v": token_value, "d": "setup-ctfd-auto"})

    db.session.commit()

    # Flush Redis memoize cache so new pods read fresh config from MySQL.
    from CTFd.cache import cache
    cache.clear()

    print("TOKEN=" + token_value)
' 2>&1 | grep -E '^TOKEN=|^ALREADY_SETUP' || true
  )

  if [[ "$SETUP_OUTPUT" == "ALREADY_SETUP" ]]; then
    echo "Admin user already exists (partial previous setup)."
    if [[ -z "$CTFD_ADMIN_TOKEN" ]]; then
      echo "ERROR: No token available. Either:" >&2
      echo "  - Re-run with CTFD_ADMIN_TOKEN=<token>" >&2
      echo "  - Or wipe the CTFd database and start fresh." >&2
      exit 2
    fi
    echo "INFO: Using token from $ENV_FILE"

  elif [[ "$SETUP_OUTPUT" == TOKEN=* ]]; then
    CTFD_ADMIN_TOKEN="${SETUP_OUTPUT#TOKEN=}"

    echo ""
    echo "  Admin:    $CTFD_ADMIN_NAME <$CTFD_ADMIN_EMAIL>"
    echo "  Password: (stored in $ENV_FILE — not shown)"
    echo "  Token:    (stored in $ENV_FILE — not shown)"
    echo ""

    save_env_file

    # Pods that were running before the ORM insert have a stale in-memory config
    # cache and still redirect API calls to /setup. Rolling restart clears it.
    echo "Restarting CTFd to flush config cache..."
    kubectl rollout restart deployment/ctfd -n "$CTFD_NAMESPACE"
    kubectl rollout status deployment/ctfd -n "$CTFD_NAMESPACE" --timeout=360s
    echo "CTFd restarted."
    echo ""

    # Flush Redis memoize cache after restart — CTFd memoizes token lookups and
    # other auth state in Redis. After rolling restart old cached entries from
    # pre-restart pods can cause sporadic 401s. A FLUSHDB forces re-validation
    # from MySQL on the next request. Safe to run: caches rebuild on demand.
    REDIS_POD=$(kubectl -n "$CTFD_NAMESPACE" get pods -l app=redis \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
    if [[ -n "$REDIS_POD" ]]; then
      kubectl -n "$CTFD_NAMESPACE" exec "$REDIS_POD" -- redis-cli FLUSHDB >/dev/null 2>&1 || true
      echo "INFO: Redis cache flushed (post-restart)"
    fi

    wait_for_ctfd "CTFd (post-restart)"

  else
    echo "ERROR: Unexpected output from setup script: '$SETUP_OUTPUT'" >&2
    echo "  Check that the CTFd pod is healthy and the Python path is correct." >&2
    exit 1
  fi

else
  echo "CTFd already configured (HTTP $SETUP_HTTP on /setup)."

  if [[ -z "$CTFD_ADMIN_TOKEN" ]]; then
    echo "ERROR: No admin token available." >&2
    echo "  Run: CTFD_ADMIN_TOKEN=<token> $0" >&2
    echo "  Or place credentials in $ENV_FILE" >&2
    exit 2
  fi
fi

# ---------------------------------------------------------------------------
# Step 3: Validate token
# ---------------------------------------------------------------------------
echo "Validating admin token..."
TOKEN_CHECK=$(ctfd_api GET /api/v1/users/me)
if [[ "$TOKEN_CHECK" != "200" ]]; then
  echo "ERROR: Admin token is invalid (HTTP $TOKEN_CHECK)." >&2
  cat "$RESP_TMP" >&2
  exit 2
fi
echo "Validating admin token... OK"

# ---------------------------------------------------------------------------
# Step 4: Apply configuration (idempotent)
# ---------------------------------------------------------------------------
apply_config "event name"   "$(python3 -c 'import json,sys; print(json.dumps({"ctf_name": sys.argv[1]}))' "$CTFD_EVENT_NAME")"
apply_config "team mode"    '{"user_mode": "teams"}'
apply_config "registration" '{"registration_visibility": "private"}'

# Sync controller key if provided (plugin stores it as X_CONTROLLER_KEY in Configs table)
if [[ -n "${CONTROLLER_KEY:-}" ]]; then
  apply_config "controller key" "{\"X_CONTROLLER_KEY\": \"$CONTROLLER_KEY\"}"
fi

# ---------------------------------------------------------------------------
# Step 5: Create / update CTFd pages (idempotent)
# ---------------------------------------------------------------------------
create_or_update_page() {
  local route="$1" title="$2" content="$3" auth_required="${4:-false}" hidden="${5:-false}"
  # Check if the page already exists
  local resp
  resp=$(ctfd_api GET "/api/v1/pages?route=$route")
  local existing_id=""
  if [[ "$resp" == "200" ]]; then
    existing_id=$(python3 -c "
import json, sys
data = json.load(open('$RESP_TMP'))
pages = data.get('data', [])
for p in pages:
    if p.get('route') == '$route':
        print(p['id'])
        break
" 2>/dev/null || true)
  fi

  local payload
  payload=$(python3 -c "
import json, sys
print(json.dumps({
    'title': sys.argv[1],
    'route': sys.argv[2],
    'content': sys.argv[3],
    'draft': False,
    'hidden': sys.argv[5] == 'true',
    'auth_required': sys.argv[4] == 'true'
}))
" "$title" "$route" "$content" "$auth_required" "$hidden")

  if [[ -n "$existing_id" ]]; then
    resp=$(ctfd_api PATCH "/api/v1/pages/$existing_id" "$payload")
    if [[ "$resp" =~ ^20[0-9]$ ]]; then
      echo "  Updated page: /$route (id=$existing_id)"
    else
      echo "  WARNING: Failed to update page /$route (HTTP $resp)" >&2
      cat "$RESP_TMP" >&2
    fi
  else
    resp=$(ctfd_api POST "/api/v1/pages" "$payload")
    if [[ "$resp" =~ ^20[0-9]$ ]]; then
      echo "  Created page: /$route"
    else
      echo "  WARNING: Failed to create page /$route (HTTP $resp)" >&2
      cat "$RESP_TMP" >&2
    fi
  fi
}

echo "Creating CTFd pages..."

# Rules page — HTML content loaded from plugin assets
RULES_HTML_FILE="$(cd "$(dirname "$0")/.." && pwd)/ctfd-plugin/assets/rules.html"
if [[ -f "$RULES_HTML_FILE" ]]; then
  RULES_CONTENT=$(cat "$RULES_HTML_FILE")
  RULES_CONTENT="${RULES_CONTENT//\{\{FLAG_PREFIX\}\}/${FLAG_PREFIX:-CTF}}"
  create_or_update_page "rules" "Rules" "$RULES_CONTENT" "false"
else
  echo "  WARNING: rules.html not found at $RULES_HTML_FILE — skipping rules page"
fi

# Home page. Our setup writes the DB directly instead of using CTFd's /setup
# wizard, so CTFd's default "index" page is never created and / returns 404.
# Only created if missing, so admins can edit it freely afterwards.
if ! ctfd_api GET "/api/v1/pages?route=index" >/dev/null || \
   ! grep -q '"route": *"index"' "$RESP_TMP"; then
  INDEX_CONTENT="<div class=\"row\"><div class=\"col-md-8 offset-md-2 text-center py-5\">
<h1>${CTFD_EVENT_NAME}</h1>
<p class=\"lead\">Log in with the account you were given, then open <a href=\"/challenges\">Challenges</a>.</p>
<p>Read the <a href=\"/rules\">rules</a> first. Flags look like <code>${FLAG_PREFIX:-CTF}{...}</code>.</p>
</div></div>"
  # hidden = not listed in the navbar (the logo already links to /)
  create_or_update_page "index" "Home" "$INDEX_CONTENT" "false" "true"
fi

echo "Creating CTFd pages... OK"

# ---------------------------------------------------------------------------
# Step 6: Import teams (optional — only if TEAMS_CSV is set)
# ---------------------------------------------------------------------------
if [[ -n "$TEAMS_CSV" ]]; then
  if [[ ! -f "$TEAMS_CSV" ]]; then
    echo "ERROR: TEAMS_CSV='$TEAMS_CSV' not found." >&2
    exit 2
  fi
  echo ""
  echo "=== Importing teams from $TEAMS_CSV ==="
  CTFD_URL="$CTFD_URL" CTFD_ADMIN_TOKEN="$CTFD_ADMIN_TOKEN" \
    "$PYTHON" "$(dirname "$0")/import-teams.py" "$TEAMS_CSV"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo "Setup complete."
echo "  URL:   $CTFD_URL"
echo "  Creds: $ENV_FILE (token and password stored there — not shown)"
exit 0
