#!/usr/bin/env bash
# deploy-challenges.sh — publish every manifest in challenges/manifests/.
#
#   1. Validate manifests
#   2. Create/update the challenges in CTFd
#   3. Load manifests into the controller (ConfigMap + restart)
#   4. bulk_spawn: per team × service challenge, create a stopped Deployment,
#      a NodePort Service and the team's flag Secret
#   5. Register the team-bound flag for each challenge in CTFd
#   6. Build per-team zips for static challenges and ship them
#
# Idempotent: re-run after adding challenges OR teams (new teams get their
# instances, existing ones are left alone).
#
# Teams must exist in CTFd first (make ctfd-setup TEAMS_CSV=...).
# Env: CHALLENGE_STATE=hidden   create new challenges hidden (default visible)

. "$(dirname "$0")/lib.sh"
load_config
load_secrets
load_admin_token
kubectl_ready
PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY=python3
CTFD_INTERNAL_URL="http://ctfd-service.ctfd.svc.cluster.local"

info "1/6 Validating manifests"
"$PY" "$ROOT/scripts/ctfd_sync.py" validate

info "2/6 Syncing challenges to CTFd"
"$PY" "$ROOT/scripts/ctfd_sync.py" sync

info "3/6 Loading manifests into the controller"
kubectl create configmap challenge-manifests -n ctfd \
  --from-file="$ROOT/challenges/manifests" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n ctfd rollout restart deployment/controller-api >/dev/null
kubectl -n ctfd rollout status deployment/controller-api --timeout=120s >/dev/null
ok "controller-api reloaded"

info "4/6 Creating per-team instances (bulk_spawn)"
cleanup() { kubectl -n ctfd scale deployment/controller-provisioner --replicas=0 >/dev/null 2>&1 || true; }
trap cleanup EXIT
# Start from zero so exactly one fresh pod runs, mounting the ConfigMap we
# just wrote. (scale + rollout restart briefly made two pods, and we could
# exec into the one being terminated, which has no network.)
kubectl -n ctfd scale deployment/controller-provisioner --replicas=0 >/dev/null
kubectl -n ctfd wait --for=delete pod -l app=controller-provisioner --timeout=90s >/dev/null 2>&1 || true
kubectl -n ctfd scale deployment/controller-provisioner --replicas=1 >/dev/null
kubectl -n ctfd rollout status deployment/controller-provisioner --timeout=180s >/dev/null
POD=$(kubectl -n ctfd get pods -l app=controller-provisioner \
  -o jsonpath='{.items[0].metadata.name}')
[[ -n "$POD" ]] || die "provisioner pod not running"

# k3s applies network policies to a new pod a few seconds after it starts.
# Until then DNS fails ("[Errno -3] Try again"), so wait until it resolves
# and reaches CTFd.
for i in $(seq 1 30); do
  if kubectl -n ctfd exec "$POD" -- python3 -c \
      "import urllib.request; urllib.request.urlopen('$CTFD_INTERNAL_URL/healthcheck', timeout=3)" \
      >/dev/null 2>&1; then
    break
  fi
  [[ $i -eq 30 ]] && die "provisioner pod can't reach CTFd after 60 s (DNS or network policy). Check: kubectl -n ctfd exec $POD -- nslookup ctfd-service"
  sleep 2
done

LOG_DIR="$ROOT/build/logs"; mkdir -p "$LOG_DIR"
# run_in_pod <name> <cmd...>: full log to build/logs/<name>.log, show only
# errors + summary, fail on non-zero exit or any reported error.
run_in_pod() {
  local name="$1"; shift
  local log="$LOG_DIR/$name.log"
  if ! kubectl -n ctfd exec "$POD" -- env CTFD_ADMIN_TOKEN="$CTFD_ADMIN_TOKEN" "$@" >"$log" 2>&1; then
    tail -30 "$log" | sed 's/^/    /'
    die "$name failed (full log: build/logs/$name.log)"
  fi
  grep -E "ERROR|WARNING|Done:|Loaded|Fetched" "$log" | sed 's/^/    /' || true
  if grep -q "ERROR" "$log"; then
    die "$name reported errors (full log: build/logs/$name.log)"
  fi
}

run_in_pod bulk_spawn python3 /app/bulk_spawn.py --ctfd-url "$CTFD_INTERNAL_URL" --token "$CTFD_ADMIN_TOKEN"
ok "instances created (stopped until a team clicks Start)"

info "5/6 Registering team flags"
run_in_pod import_flags python3 /app/import_flags.py --ctfd-url "$CTFD_INTERNAL_URL"
ok "flags registered"

info "6/6 Static (download) challenges"
CTFD_ADMIN_TOKEN="$CTFD_ADMIN_TOKEN" CTF_MASTER_SECRET="$CTF_MASTER_SECRET" FLAG_PREFIX="$FLAG_PREFIX" \
  "$PY" "$ROOT/scripts/build-static.py"
if compgen -G "$ROOT/build/static/*/*.zip" >/dev/null; then
  registry_login
  docker_cmd build -q -t "$PUSH_REGISTRY/static-files:latest" -f "$ROOT/static-files/Dockerfile" "$ROOT/build/static" >/dev/null
  docker_cmd push -q "$PUSH_REGISTRY/static-files:latest" >/dev/null
  kubectl -n ctfd rollout restart deployment/static-files >/dev/null
  kubectl -n ctfd rollout status deployment/static-files --timeout=120s >/dev/null
  ok "static files published"
fi

echo ""
ok "Challenges deployed. Check: make status"
