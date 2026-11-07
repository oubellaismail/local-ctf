#!/usr/bin/env bash
# install-server.sh — turn this Ubuntu laptop into the CORE node.
#
# Runs: CTFd, MySQL, Redis, controller, image registry, Traefik (ports 80/443).
# Challenge pods also run here unless an agent laptop joins.
#
# Usage:  make install          (or: bash scripts/install-server.sh)
# Env:    K3S_VERSION=v1.37.2+k3s1   k3s release (default: the version this release was tested with)
#         SKIP_POWER_TWEAKS=1        don't disable sleep / lid suspend
#
# Idempotent: safe to re-run.

. "$(dirname "$0")/lib.sh"
load_config

[[ "$(uname -s)" == "Linux" ]] || die "Run this on the Ubuntu core laptop, not $(uname -s)"
[[ $EUID -ne 0 ]] || die "Run as your normal user (the script calls sudo itself)"

# ── 1. Sanity checks ────────────────────────────────────────────────────────
info "Checking network"
if ! ip -4 -o addr show | awk '{print $4}' | cut -d/ -f1 | grep -qx "$CTF_HOST"; then
  echo "  This laptop's IPv4 addresses:"
  ip -4 -o addr show | awk '{print "    " $2 "  " $4}'
  die "CTF_HOST=$CTF_HOST is not an address of this laptop. Fix config.env."
fi
ok "CTF_HOST $CTF_HOST is on this laptop"

MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
(( MEM_GB >= 14 )) || warn "Only ${MEM_GB} GB RAM — 16 GB recommended for ~20 teams"

# ── 2. Packages ─────────────────────────────────────────────────────────────
info "Installing packages"
sudo apt-get update -qq
sudo apt-get install -y -qq curl openssl gettext-base python3 python3-venv python3-pip zip >/dev/null
if ! command -v docker >/dev/null; then
  sudo apt-get install -y -qq docker.io >/dev/null
fi
# BuildKit builder (silences "legacy builder is deprecated"); optional
sudo apt-get install -y -qq docker-buildx >/dev/null 2>&1 || true
sudo systemctl enable --now docker >/dev/null
if ! id -nG "$USER" | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  warn "Added $USER to the docker group — log out and back in after this script"
fi
ok "curl, openssl, envsubst, python3, docker"

# Python deps for the admin scripts (team import, flag/static tooling)
if [[ ! -d "$ROOT/.venv" ]]; then
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/pip" install -q -r "$ROOT/controller/requirements.txt" requests pyyaml
ok "Python venv at .venv"

# ── Keep systemd-networkd's hands off Kubernetes/Docker routes ─────────────
# By default, when systemd-networkd restarts (e.g. after an automatic systemd
# security update) it deletes every route it didn't create itself: the
# flannel/cni0 pod routes and Docker's route to the registry container.
# Result: pods can't reach the API server, nothing can pull images, the whole
# platform goes down until docker + k3s are restarted.
if [[ -d /etc/systemd/network || -d /run/systemd/network ]]; then
  sudo mkdir -p /etc/systemd/networkd.conf.d
  printf '[Network]\nManageForeignRoutes=no\nManageForeignRoutingPolicyRules=no\n' | \
    sudo tee /etc/systemd/networkd.conf.d/local-ctf.conf >/dev/null
  ok "systemd-networkd won't delete pod/Docker routes"
fi

# ── 3. Keep the laptop awake ────────────────────────────────────────────────
if [[ -z "${SKIP_POWER_TWEAKS:-}" ]]; then
  info "Disabling sleep and lid-close suspend"
  sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
  sudo mkdir -p /etc/systemd/logind.conf.d
  printf '[Login]\nHandleLidSwitch=ignore\nHandleLidSwitchExternalPower=ignore\nHandleLidSwitchDocked=ignore\n' \
    | sudo tee /etc/systemd/logind.conf.d/local-ctf.conf >/dev/null
  ok "Sleep disabled (undo: sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target; sudo rm /etc/systemd/logind.conf.d/local-ctf.conf)"
fi

# ── 4. Image registry (Docker container, password-protected) ───────────────
# Players are on the same LAN, so the registry needs auth — otherwise anyone
# could pull challenge images and read their source.
info "Starting image registry on :5000"
mkdir -p "$SECRETS_DIR"; chmod 700 "$SECRETS_DIR"
if [[ ! -f "$SECRETS_DIR/registry.env" ]]; then
  REG_PASS="$(random_secret 24)"
  printf 'REGISTRY_USER=ctf\nREGISTRY_PASSWORD=%s\n' "$REG_PASS" > "$SECRETS_DIR/registry.env"
  chmod 600 "$SECRETS_DIR/registry.env"
fi
# shellcheck source=/dev/null
. "$SECRETS_DIR/registry.env"

sudo mkdir -p /var/lib/local-ctf/registry /var/lib/local-ctf/registry-auth
if [[ ! -s /var/lib/local-ctf/registry-auth/htpasswd ]]; then
  docker_cmd run --rm --entrypoint htpasswd httpd:2.4-alpine -Bbn "$REGISTRY_USER" "$REGISTRY_PASSWORD" \
    | sudo tee /var/lib/local-ctf/registry-auth/htpasswd >/dev/null
fi
if ! docker_cmd ps --format '{{.Names}}' | grep -qx ctf-registry; then
  docker_cmd rm -f ctf-registry >/dev/null 2>&1 || true
  docker_cmd run -d --name ctf-registry --restart unless-stopped \
    -p 5000:5000 \
    -v /var/lib/local-ctf/registry:/var/lib/registry \
    -v /var/lib/local-ctf/registry-auth:/auth:ro \
    -e REGISTRY_AUTH=htpasswd \
    -e REGISTRY_AUTH_HTPASSWD_REALM=local-ctf \
    -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
    -e REGISTRY_STORAGE_DELETE_ENABLED=true \
    registry:2 >/dev/null
fi
echo "$REGISTRY_PASSWORD" | docker_cmd login "$PUSH_REGISTRY" -u "$REGISTRY_USER" --password-stdin >/dev/null 2>&1 \
  || die "docker login to $PUSH_REGISTRY failed"
ok "Registry running (push: $PUSH_REGISTRY, pull inside cluster: $REGISTRY)"

# ── 5. k3s registry mirror ──────────────────────────────────────────────────
info "Writing /etc/rancher/k3s/registries.yaml"
sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/registries.yaml >/dev/null <<EOF
mirrors:
  "${REGISTRY}":
    endpoint:
      - "http://${CTF_HOST}:5000"
configs:
  "${CTF_HOST}:5000":
    auth:
      username: ${REGISTRY_USER}
      password: ${REGISTRY_PASSWORD}
EOF
sudo chmod 600 /etc/rancher/k3s/registries.yaml
ok "Mirror ${REGISTRY} → http://${CTF_HOST}:5000"

# ── 6. Traefik settings (applied by k3s at startup) ─────────────────────────
# hostPort 80/443 on the core laptop keeps the real player IP visible to CTFd
# (rate limiting, account-sharing detection). No LoadBalancer needed.
sudo mkdir -p /var/lib/rancher/k3s/server/manifests
sudo cp "$ROOT/k8s/k3s/traefik-config.yaml" /var/lib/rancher/k3s/server/manifests/traefik-config.yaml

# ── 7. k3s server ───────────────────────────────────────────────────────────
info "Installing k3s server"
K3S_ARGS=(
  server
  --node-ip "$CTF_HOST"
  --advertise-address "$CTF_HOST"
  --tls-san "$CTF_HOST"
  --node-label ctf/role=core
  --disable servicelb
  --kubelet-arg max-pods=250
  --write-kubeconfig-mode 600
)
[[ -n "$CTF_HOSTNAME" ]] && K3S_ARGS+=(--tls-san "$CTF_HOSTNAME")
if ! systemctl is-active --quiet k3s; then
  curl -sfL https://get.k3s.io | \
    INSTALL_K3S_VERSION="${K3S_VERSION:-v1.37.2+k3s1}" INSTALL_K3S_EXEC="${K3S_ARGS[*]}" sh - >/dev/null
else
  ok "k3s already running (re-run with 'sudo systemctl restart k3s' after editing registries.yaml)"
fi

mkdir -p "$HOME/.kube"
sudo cp /etc/rancher/k3s/k3s.yaml "$HOME/.kube/config"
sudo chown "$USER:" "$HOME/.kube/config"
chmod 600 "$HOME/.kube/config"
export KUBECONFIG="$HOME/.kube/config"
# So plain `kubectl` works in new shells without sudo
grep -q "KUBECONFIG=\$HOME/.kube/config" "$HOME/.bashrc" 2>/dev/null || \
  echo 'export KUBECONFIG=$HOME/.kube/config' >> "$HOME/.bashrc"

for _ in $(seq 1 60); do
  kubectl get nodes 2>/dev/null | grep -q " Ready" && break
  sleep 2
done
kubectl get nodes 2>/dev/null | grep -q " Ready" || die "k3s node not Ready after 2 min — check: sudo journalctl -u k3s"
ok "k3s ready: $(kubectl get nodes --no-headers | wc -l) node(s)"

# ── 8. Firewall ─────────────────────────────────────────────────────────────
if sudo ufw status 2>/dev/null | grep -q "Status: active"; then
  info "Opening firewall ports (ufw is active)"
  sudo ufw allow 80/tcp  comment 'local-ctf http'  >/dev/null
  sudo ufw allow 443/tcp comment 'local-ctf https' >/dev/null
  sudo ufw allow 30000:32767/tcp comment 'local-ctf challenges' >/dev/null
  # Node-to-node (agent laptops): API, kubelet, flannel VXLAN, registry
  sudo ufw allow 6443/tcp  comment 'k3s api'      >/dev/null
  sudo ufw allow 10250/tcp comment 'kubelet'      >/dev/null
  sudo ufw allow 8472/udp  comment 'flannel'      >/dev/null
  sudo ufw allow 5000/tcp  comment 'ctf registry' >/dev/null
  ok "ufw rules added"
fi

# ── 9. Join script for agent laptops ────────────────────────────────────────
bash "$ROOT/scripts/make-join-script.sh"

echo ""
echo "Core laptop ready."
echo "  Next:  make secrets && make build && make deploy && make ctfd-setup"
echo "  Agent laptop (optional): copy .secrets/join-agent.sh to it and run: sudo bash join-agent.sh"
