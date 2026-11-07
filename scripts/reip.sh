#!/usr/bin/env bash
# reip.sh — move the platform to this laptop's new IP address.
#
#   make reip              use the IP of the interface with the default route
#   make reip IP=10.0.5.7  use this IP (must be an address of this laptop)
#
# Needed when DHCP hands out a different address, when switching a VM
# between NAT and bridged networking, or after copying the VM to another
# laptop. Updates everything that contains the IP:
#   config.env (CTF_HOST), the k3s node IP, the registry mirror, the HTTPS
#   certificate (same CA: players who installed ca.crt keep trusting it),
#   the controller's challenge URLs, and the agent join script.
# CTFd data, teams, flags and challenge instances are untouched.
#
# Run it between rounds, not while players are connected: k3s restarts.

. "$(dirname "$0")/lib.sh"
load_config
OLD="$CTF_HOST"

local_ips() { ip -4 -o addr show scope global | awk '{print $4}' | cut -d/ -f1; }

NEW="${1:-}"
if [[ -z "$NEW" ]]; then
  NEW="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}')"
  [[ -n "$NEW" ]] || die "No default route to detect the IP from. Run: make reip IP=<this laptop's LAN IP>"
fi
[[ "$NEW" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]] || die "'$NEW' is not an IPv4 address"
if ! local_ips | grep -qx "$NEW"; then
  echo "  This laptop's IPv4 addresses:"; local_ips | sed 's/^/    /'
  die "$NEW is not an address of this laptop"
fi

K3S_UNIT=/etc/systemd/system/k3s.service
[[ -f "$K3S_UNIT" ]] || die "$K3S_UNIT not found. Is this the core laptop (make install)?"

if [[ "$NEW" == "$OLD" ]] && grep -q "$NEW" "$K3S_UNIT"; then
  node_ip="$(kubectl get nodes -l ctf/role=core -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
  if [[ "$node_ip" == "$NEW" ]]; then
    ok "Already on $NEW, nothing to do"
    exit 0
  fi
fi

echo "Moving the platform: ${OLD} → ${NEW}"
echo "k3s restarts: players lose the site for ~2 minutes."
if [[ -t 0 && -z "${YES:-}" ]]; then
  read -r -p "Continue? [y/N] " ans
  [[ "$ans" =~ ^[yY] ]] || die "Cancelled"
fi

esc() { printf '%s' "$1" | sed 's/\./\\./g'; }
O="$(esc "$OLD")"

# ── 1. config.env ───────────────────────────────────────────────────────────
info "1/6 config.env"
cp "$CONFIG_FILE" "$CONFIG_FILE.bak"
sed -i "s/^CTF_HOST=.*/CTF_HOST=${NEW}/" "$CONFIG_FILE"
ok "CTF_HOST=${NEW} (previous file: config.env.bak)"

# ── 2. registry mirror ──────────────────────────────────────────────────────
info "2/6 registry mirror"
sudo sed -i "s/\b${O}:5000/${NEW}:5000/g" /etc/rancher/k3s/registries.yaml
sudo grep -q "${NEW}:5000" /etc/rancher/k3s/registries.yaml || die "registries.yaml was not updated"
ok "ctf-registry:5000 → http://${NEW}:5000"

# ── 3. k3s node IP + restart ────────────────────────────────────────────────
info "3/6 k3s node IP (restarting k3s)"
sudo cp "$K3S_UNIT" "$K3S_UNIT.bak"
sudo sed -i "s/\b${O}\b/${NEW}/g" "$K3S_UNIT"
grep -q "$NEW" "$K3S_UNIT" || die "Couldn't find the old IP $OLD in $K3S_UNIT; edit --node-ip there by hand"
sudo systemctl daemon-reload
sudo systemctl restart k3s
for _ in $(seq 1 90); do
  ip_now="$(kubectl get nodes -l ctf/role=core -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || true)"
  ready="$(kubectl get nodes -l ctf/role=core --no-headers 2>/dev/null | awk '{print $2}' || true)"
  [[ "$ip_now" == "$NEW" && "$ready" == "Ready" ]] && break
  sleep 2
done
[[ "$ip_now" == "$NEW" && "$ready" == "Ready" ]] \
  || die "Node not Ready on $NEW after 3 min. Check: sudo journalctl -u k3s -n 50 (old unit: $K3S_UNIT.bak)"
ok "node Ready, InternalIP ${NEW}"

# ── 4. certificate + secrets ────────────────────────────────────────────────
info "4/6 HTTPS certificate"
CTF_HOST="$NEW" bash "$ROOT/scripts/gen-secrets.sh" | sed 's/^/  /'

# ── 5. platform (controller challenge URLs) ─────────────────────────────────
info "5/6 platform manifests"
CTF_HOST="$NEW" bash "$ROOT/scripts/deploy-platform.sh" | sed 's/^/  /'

# ── 6. agent join script ────────────────────────────────────────────────────
info "6/6 agent join script"
bash "$ROOT/scripts/make-join-script.sh" >/dev/null && ok "rewrote .secrets/join-agent.sh"
agents="$(kubectl get nodes -l '!node-role.kubernetes.io/control-plane' --no-headers 2>/dev/null | awk '{print $1}' || true)"
if [[ -n "$agents" ]]; then
  warn "Agent laptops still point at ${OLD}: $(echo $agents)"
  warn "On each one: sudo /usr/local/bin/k3s-agent-uninstall.sh, then run the new join-agent.sh"
fi

echo ""
ok "Platform now on https://${NEW}"
echo "  Instance URLs use ${NEW} as soon as teams refresh the challenge."
echo "  Players who installed ca.crt keep trusting the new certificate."
