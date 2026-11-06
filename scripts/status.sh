#!/usr/bin/env bash
# status.sh — one-screen health check for event day.

. "$(dirname "$0")/lib.sh"
load_config
kubectl_ready

info "Nodes"
kubectl get nodes -L ctf/role -o wide --no-headers | awk '{printf "    %-20s %-8s role=%-11s ip=%s\n", $1, $2, $NF, $6}'
kubectl top nodes --no-headers 2>/dev/null | awk '{printf "    %-20s cpu %-6s mem %s\n", $1, $3, $5}' || true

info "Platform pods (ctfd namespace)"
kubectl -n ctfd get pods --no-headers | awk '{printf "    %-45s %-8s %s restarts\n", $1, $3, $4}'

info "Challenge instances"
TOTAL=$(kubectl -n challenges get deploy -l managed-by=ctf-controller --no-headers 2>/dev/null | wc -l | tr -d ' ')
RUNNING=$(kubectl -n challenges get deploy -l managed-by=ctf-controller \
  -o jsonpath='{range .items[?(@.spec.replicas==1)]}{.metadata.labels.team}{"\n"}{end}' 2>/dev/null)
N_RUNNING=$(grep -c . <<<"$RUNNING" || true)
echo "    $N_RUNNING running / $TOTAL provisioned"
if [[ -n "$RUNNING" ]]; then
  echo "    per team: $(sort <<<"$RUNNING" | uniq -c | awk '{printf "team %s=%s  ", $2, $1}')"
fi
NOT_READY=$(kubectl -n challenges get pods --no-headers 2>/dev/null | grep -v " Running " | awk '{print $1" ("$3")"}' || true)
[[ -z "$NOT_READY" ]] || { warn "pods not running:"; sed 's/^/      /' <<<"$NOT_READY"; }

info "Endpoints"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$CTFD_URL/healthcheck" || true)
[[ "$code" == "200" ]] && ok "CTFd $CTFD_URL (HTTP 200)" || warn "CTFd $CTFD_URL → HTTP ${code:-timeout}"
if curl -s --max-time 3 -o /dev/null "http://localhost:5000/v2/"; then ok "registry :5000"; else warn "registry :5000 not answering (docker ps)"; fi

info "Disk (core laptop)"
df -h /var/lib/rancher 2>/dev/null | awk 'NR==2 {printf "    %s used of %s (%s)\n", $3, $2, $5}' || true
