#!/usr/bin/env bash
# deploy-challenges.sh — publish the challenges in challenges/manifests/.
#
#   1. Validate the manifests
#   2. Add/update them in the challenge-manifests ConfigMap (challenges added
#      from the dashboard stay)
#   3. Build per-team zips for download challenges and copy them to the
#      platform-data volume
#   4. Run the deploy pipeline in controller-admin: CTFd challenges, per-team
#      instances, team flags. Same code as the dashboard's "Deploy all".
#
# Idempotent: re-run after adding challenges OR teams (new teams get their
# instances, existing ones are left alone).
#
# Teams must exist in CTFd first (make ctfd-setup TEAMS_CSV=...).
# Env: CHALLENGE_STATE=hidden   create new CTFd challenges hidden (default visible)

. "$(dirname "$0")/lib.sh"
load_config
load_secrets
load_admin_token
kubectl_ready
PY="$ROOT/.venv/bin/python"; [[ -x "$PY" ]] || PY=python3
LOG_DIR="$ROOT/build/logs"; mkdir -p "$LOG_DIR"

info "1/4 Validating manifests"
"$PY" "$ROOT/scripts/ctfd_sync.py" validate

info "2/4 Loading manifests into the cluster"
PATCH="$("$PY" "$ROOT/scripts/ctfd_sync.py" patch)"
kubectl -n ctfd get configmap challenge-manifests >/dev/null 2>&1 \
  || kubectl -n ctfd create configmap challenge-manifests >/dev/null
if [[ "$PATCH" != '{"data": {}}' ]]; then
  kubectl -n ctfd patch configmap challenge-manifests --type merge -p "$PATCH" >/dev/null
fi
ok "challenge-manifests updated"

POD="$(admin_pod)"
[[ -n "$POD" ]] || die "controller-admin pod not found"

info "3/4 Download files (static challenges)"
CTFD_ADMIN_TOKEN="$CTFD_ADMIN_TOKEN" CTF_MASTER_SECRET="$CTF_MASTER_SECRET" FLAG_PREFIX="$FLAG_PREFIX" \
  "$PY" "$ROOT/scripts/build-static.py"
if compgen -G "$ROOT/build/static/*/*.zip" >/dev/null; then
  tar -C "$ROOT/build/static" -cf - . \
    | kubectl -n ctfd exec -i "$POD" -- sh -c 'mkdir -p /data/static && tar -C /data/static -xf -'
  ok "zips copied to the platform-data volume"
fi

info "4/4 Deploying (CTFd, team instances, flags)"
set +e
kubectl -n ctfd exec "$POD" -- env CTFD_ADMIN_TOKEN="$CTFD_ADMIN_TOKEN" \
  python3 admin_cli.py deploy --state "${CHALLENGE_STATE:-visible}" 2>&1 | tee "$LOG_DIR/deploy.log"
RC=${PIPESTATUS[0]}
set -e
[[ "$RC" == "0" ]] || die "deploy failed (full log: build/logs/deploy.log)"

echo ""
ok "Challenges deployed. Check: make status (or Admin → Platform in CTFd)"
