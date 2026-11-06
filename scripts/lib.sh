# shellcheck shell=bash
# Shared helpers for local-ctf scripts. Source it: . "$(dirname "$0")/lib.sh"

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECRETS_DIR="$ROOT/.secrets"
CONFIG_FILE="${CONFIG_FILE:-$ROOT/config.env}"

# Image names as Kubernetes sees them. "ctf-registry:5000" is not a real DNS
# name: every node's /etc/rancher/k3s/registries.yaml mirrors it to
# http://$CTF_HOST:5000. Docker on the core laptop pushes to localhost:5000,
# which is the same registry.
REGISTRY="ctf-registry:5000"
PUSH_REGISTRY="localhost:5000"

# k3s's kubectl reads root-only /etc/rancher/k3s/k3s.yaml unless KUBECONFIG
# is set. make install copies it to ~/.kube/config for the normal user.
if [[ -z "${KUBECONFIG:-}" && -r "$HOME/.kube/config" ]]; then
  export KUBECONFIG="$HOME/.kube/config"
fi

info() { printf '\033[1;34m▸\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*" >&2; }
die()  { printf '  \033[31m✘\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required but not installed"; }

load_config() {
  [[ -f "$CONFIG_FILE" ]] || die "Missing $CONFIG_FILE — run: cp config.env.example config.env"
  set -a
  # shellcheck source=/dev/null
  . "$CONFIG_FILE"
  set +a
  : "${CTF_HOST:?CTF_HOST must be set in config.env}"
  CTF_HOSTNAME="${CTF_HOSTNAME:-}"
  EVENT_NAME="${EVENT_NAME:-Club CTF}"
  FLAG_PREFIX="${FLAG_PREFIX:-CTF}"
  BRANDING="${BRANDING:-false}"
  MAX_ACTIVE_PER_TEAM="${MAX_ACTIVE_PER_TEAM:-4}"
  INSTANCE_LIFETIME_MINUTES="${INSTANCE_LIFETIME_MINUTES:-60}"
  CTFD_REPLICAS="${CTFD_REPLICAS:-2}"
  CTFD_WORKERS="${CTFD_WORKERS:-3}"
  CTFD_VERSION="${CTFD_VERSION:-latest}"
  CTFD_ADMIN_NAME="${CTFD_ADMIN_NAME:-admin}"
  CTFD_ADMIN_EMAIL="${CTFD_ADMIN_EMAIL:-admin@ctf.local}"
  CTFD_URL="https://${CTF_HOSTNAME:-$CTF_HOST}"
  export REGISTRY CTF_HOST CTF_HOSTNAME EVENT_NAME FLAG_PREFIX MAX_ACTIVE_PER_TEAM \
    INSTANCE_LIFETIME_MINUTES CTFD_REPLICAS CTFD_WORKERS CTFD_VERSION CTFD_URL \
    CTFD_ADMIN_NAME CTFD_ADMIN_EMAIL BRANDING

  # Our own CA signs the CTFd certificate; trust it for curl/python/httpx.
  if [[ -f "$SECRETS_DIR/ca.crt" ]]; then
    export CURL_CA_BUNDLE="$SECRETS_DIR/ca.crt"
    export SSL_CERT_FILE="$SECRETS_DIR/ca.crt"
    export REQUESTS_CA_BUNDLE="$SECRETS_DIR/ca.crt"
  fi
}

# Platform secrets (master secret, DB passwords, controller key). Generated
# once by gen-secrets.sh. Losing CTF_MASTER_SECRET mid-round breaks every flag.
load_secrets() {
  [[ -f "$SECRETS_DIR/platform.env" ]] || die "Missing .secrets/platform.env — run: make secrets"
  set -a
  # shellcheck source=/dev/null
  . "$SECRETS_DIR/platform.env"
  set +a
}

# CTFd admin token, written by setup-ctfd.sh.
load_admin_token() {
  if [[ -f "$SECRETS_DIR/ctfd-admin.env" ]]; then
    # shellcheck source=/dev/null
    . "$SECRETS_DIR/ctfd-admin.env"
  fi
  [[ -n "${CTFD_ADMIN_TOKEN:-}" ]] || die "No CTFd admin token — run: make ctfd-setup"
  export CTFD_ADMIN_TOKEN
}

# Render a manifest template: only the listed ${VARS} are substituted so any
# other "$" in the YAML is left alone.
RENDER_VARS='${REGISTRY} ${CTF_HOST} ${FLAG_PREFIX} ${MAX_ACTIVE_PER_TEAM} ${INSTANCE_LIFETIME_MINUTES} ${CTFD_REPLICAS} ${CTFD_WORKERS} ${BRANDING}'
render() { envsubst "$RENDER_VARS" < "$1"; }
apply_tpl() { render "$1" | kubectl apply -f -; }

kubectl_ready() {
  kubectl get nodes >/dev/null 2>&1 || die "kubectl cannot reach the cluster. On the core laptop: export KUBECONFIG=~/.kube/config (make install wrote it)"
}

# Use docker directly if the user is in the docker group, else via sudo.
docker_cmd() {
  if docker info >/dev/null 2>&1; then docker "$@"; else sudo docker "$@"; fi
}

# Log docker (as whichever user docker_cmd runs as) into the local registry.
registry_login() {
  [[ -f "$SECRETS_DIR/registry.env" ]] || die "Missing .secrets/registry.env — run: make install"
  # shellcheck source=/dev/null
  . "$SECRETS_DIR/registry.env"
  echo "$REGISTRY_PASSWORD" | docker_cmd login "$PUSH_REGISTRY" -u "$REGISTRY_USER" --password-stdin >/dev/null 2>&1 \
    || die "docker login to $PUSH_REGISTRY failed — is the registry running? (docker ps | grep ctf-registry)"
}

# Number of tables in the CTFd database (0 = fresh install).
ctfd_table_count() {
  kubectl -n ctfd exec mysql-0 -- sh -c \
    'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=\"ctfd\"" 2>/dev/null' \
    | tr -d '[:space:]'
}

# Start CTFd. On an empty database, start ONE replica first so two pods don't
# race to create the schema, then scale up.
start_ctfd() {
  local tables
  tables="$(ctfd_table_count || true)"
  if [[ "${tables:-0}" == "0" ]]; then
    kubectl -n ctfd scale deployment/ctfd --replicas=1 >/dev/null
    kubectl -n ctfd rollout status deployment/ctfd --timeout=600s >/dev/null
  fi
  kubectl -n ctfd scale deployment/ctfd --replicas="$CTFD_REPLICAS" >/dev/null
  kubectl -n ctfd rollout status deployment/ctfd --timeout=600s
}

random_secret() { python3 -c "import secrets; print(secrets.token_urlsafe(${1:-32}))"; }
