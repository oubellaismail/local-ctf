#!/usr/bin/env bash
# deploy-platform.sh — apply all platform manifests and wait until healthy.
# Idempotent; re-run after changing config.env (e.g. FLAG_PREFIX, replicas).

. "$(dirname "$0")/lib.sh"
load_config
need kubectl
need envsubst
kubectl_ready

kubectl get secret ctfd-secrets -n ctfd >/dev/null 2>&1 || die "Secrets missing — run: make secrets"

info "Applying base (namespaces, RBAC, limits, network policies)"
kubectl apply -f "$ROOT/k8s/base/" >/dev/null
ok "base"

info "Applying CTFd stack"
# Traefik CRDs appear a few seconds after k3s first starts
for _ in $(seq 1 30); do
  kubectl get crd middlewares.traefik.io >/dev/null 2>&1 && break
  sleep 2
done
for f in mysql redis static-files ingress; do
  apply_tpl "$ROOT/k8s/ctfd/$f.yaml" >/dev/null
done
ok "mysql, redis, static-files, ingress"

info "Waiting for MySQL (first run pulls images — can take a few minutes)"
kubectl -n ctfd rollout status statefulset/mysql --timeout=600s
for _ in $(seq 1 60); do
  kubectl -n ctfd exec mysql-0 -- sh -c 'mysqladmin ping -uroot -p"$MYSQL_ROOT_PASSWORD" --silent' >/dev/null 2>&1 && break
  sleep 3
done

# Fresh database → create CTFd with 1 replica; start_ctfd scales up after
# the schema exists.
if [[ "$(ctfd_table_count || true)" == "0" ]] && ! kubectl -n ctfd get deploy ctfd >/dev/null 2>&1; then
  CTFD_REPLICAS=1 apply_tpl "$ROOT/k8s/ctfd/ctfd.yaml" >/dev/null
else
  apply_tpl "$ROOT/k8s/ctfd/ctfd.yaml" >/dev/null
fi
ok "ctfd"

info "Applying controller"
if ! kubectl get configmap challenge-manifests -n ctfd >/dev/null 2>&1; then
  kubectl create configmap challenge-manifests -n ctfd --save-config \
    --from-file="$ROOT/challenges/manifests" >/dev/null
fi
apply_tpl "$ROOT/k8s/controller/controller.yaml" >/dev/null
ok "controller-api, controller-daemon, controller-provisioner"

info "Waiting for the rest"
kubectl -n ctfd rollout status statefulset/redis --timeout=300s
start_ctfd
kubectl -n ctfd rollout status deployment/controller-api --timeout=300s
kubectl -n ctfd rollout status deployment/controller-daemon --timeout=300s
kubectl -n ctfd rollout status deployment/static-files --timeout=300s

echo ""
ok "Platform up: $CTFD_URL"
[[ -n "${QUIET_NEXT:-}" ]] || echo "  Next (first install only): make ctfd-setup"
