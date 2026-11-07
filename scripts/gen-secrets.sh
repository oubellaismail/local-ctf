#!/usr/bin/env bash
# gen-secrets.sh — create platform secrets and the HTTPS certificate, then
# load them into the cluster.
#
#   .secrets/platform.env   CTF_MASTER_SECRET, DB passwords, CONTROLLER_KEY,
#                           ADMIN_KEY (CTFd <-> controller-admin, the dashboard)
#   .secrets/ca.crt/.key    local certificate authority (give ca.crt to players
#                           who want to remove the browser warning)
#   .secrets/tls.crt/.key   CTFd certificate for CTF_HOST / CTF_HOSTNAME
#
# Existing values are kept. CTF_MASTER_SECRET derives every flag: never
# regenerate it during a round. Delete .secrets/tls.* to reissue the cert
# (e.g. after changing CTF_HOST).

. "$(dirname "$0")/lib.sh"
load_config
need openssl
need kubectl
kubectl_ready
mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"

# ── Platform secrets ────────────────────────────────────────────────────────
info "Platform secrets"
if [[ ! -f "$SECRETS_DIR/platform.env" ]]; then
  cat > "$SECRETS_DIR/platform.env" <<EOF
# Generated $(date -u +%Y-%m-%dT%H:%M:%SZ) — back this file up with the DB dumps.
CTF_MASTER_SECRET=$(random_secret 32)
MYSQL_ROOT_PASSWORD=$(random_secret 24)
MYSQL_PASSWORD=$(random_secret 24)
CTFD_SECRET_KEY=$(random_secret 48)
CONTROLLER_KEY=$(random_secret 32)
ADMIN_KEY=$(random_secret 32)
EOF
  chmod 600 "$SECRETS_DIR/platform.env"
  ok "Generated .secrets/platform.env"
else
  ok "Keeping existing .secrets/platform.env"
fi
# Added in v1.1 (dashboard): append to older installs, keep everything else
if ! grep -q '^ADMIN_KEY=' "$SECRETS_DIR/platform.env"; then
  echo "ADMIN_KEY=$(random_secret 32)" >> "$SECRETS_DIR/platform.env"
  ok "Added ADMIN_KEY (dashboard)"
fi
load_secrets

# ── Certificates ────────────────────────────────────────────────────────────
info "TLS certificate"
cd "$SECRETS_DIR"
if [[ ! -f ca.key ]]; then
  openssl req -x509 -newkey rsa:3072 -nodes -days 1825 \
    -keyout ca.key -out ca.crt -subj "/CN=${EVENT_NAME} Local CA" 2>/dev/null
  chmod 600 ca.key
  ok "Created local CA (.secrets/ca.crt)"
fi
if [[ ! -f tls.crt ]] || ! openssl x509 -in tls.crt -noout -ext subjectAltName 2>/dev/null \
     | grep -qE "IP Address:${CTF_HOST//./\\.}(,|\$)"; then
  SAN="IP:${CTF_HOST}"
  [[ -n "$CTF_HOSTNAME" ]] && SAN="${SAN},DNS:${CTF_HOSTNAME}"
  openssl req -newkey rsa:2048 -nodes -keyout tls.key -out tls.csr \
    -subj "/CN=${CTF_HOSTNAME:-$CTF_HOST}" 2>/dev/null
  printf 'subjectAltName=%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' "$SAN" > tls.ext
  # 825 days: the max lifetime browsers accept for a server cert
  openssl x509 -req -in tls.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
    -out tls.crt -days 825 -extfile tls.ext 2>/dev/null
  rm -f tls.csr tls.ext
  chmod 600 tls.key
  ok "Issued certificate for ${SAN}"
else
  ok "Keeping existing certificate"
fi
cd "$ROOT"

# ── Load into the cluster ───────────────────────────────────────────────────
info "Applying Kubernetes secrets"
kubectl apply -f "$ROOT/k8s/base/namespaces.yaml" >/dev/null

kubectl -n ctfd create secret generic ctfd-secrets \
  --from-literal=MYSQL_ROOT_PASSWORD="$MYSQL_ROOT_PASSWORD" \
  --from-literal=MYSQL_PASSWORD="$MYSQL_PASSWORD" \
  --from-literal=MYSQL_DATABASE=ctfd \
  --from-literal=SECRET_KEY="$CTFD_SECRET_KEY" \
  --from-literal=CTF_MASTER_SECRET="$CTF_MASTER_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n ctfd create secret generic controller-key \
  --from-literal=CONTROLLER_KEY="$CONTROLLER_KEY" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n ctfd create secret tls ctfd-tls \
  --cert="$SECRETS_DIR/tls.crt" --key="$SECRETS_DIR/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

kubectl -n ctfd create secret generic controller-admin-key \
  --from-literal=ADMIN_KEY="$ADMIN_KEY" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null

[[ -f "$SECRETS_DIR/registry.env" ]] || die "Missing .secrets/registry.env (written by make install)"
# shellcheck source=/dev/null
. "$SECRETS_DIR/registry.env"
kubectl -n ctfd create secret generic registry-creds \
  --from-literal=REGISTRY_USER="$REGISTRY_USER" \
  --from-literal=REGISTRY_PASSWORD="$REGISTRY_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
sync_admin_token_secret

ok "ctfd-secrets, controller-key, controller-admin-key, registry-creds, ctfd-tls"
