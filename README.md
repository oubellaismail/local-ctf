# local-ctf

The InfoDays CTF platform, rebuilt to run on one or two staff laptops for the
club's biweekly contests. Players connect over the campus network only.

It keeps the InfoDays features: CTFd with the custom plugin, a separate
instance of each challenge per team (started and stopped from CTFd), per-team
flags that reject flag sharing, per-team file downloads, network isolation
between challenges, and an automatic instance timeout.

**Sized for:** about 20 teams × 4 running instances, on 16 GB Ubuntu laptops.

## Layout

```
 players (campus LAN)
   │ https://CTF_HOST            │ http://CTF_HOST:3xxxx / nc CTF_HOST 3xxxx
   ▼                             ▼
┌─ CORE laptop (k3s server) ──────────────┐   ┌─ CHALLENGE laptop (k3s agent, optional) ─┐
│ Traefik :80/:443 → CTFd ×2              │   │ challenge pods (preferred here)          │
│ MySQL, Redis, controller API + daemon   │◄─►│                                           │
│ image registry :5000 (Docker)           │   │                                           │
│ challenge pods (if no agent)            │   └───────────────────────────────────────────┘
└─────────────────────────────────────────┘
```

| InfoDays (DigitalOcean) | local-ctf |
|---|---|
| DOKS + Terraform | k3s, `scripts/install-server.sh` |
| DOCR | `registry:2` on the core laptop, password-protected |
| do-block-storage | k3s `local-path` |
| DO load balancer + ingress-nginx | k3s Traefik on hostPort 80/443 |
| Let's Encrypt + DO DNS | own CA, cert for `CTF_HOST` (`.secrets/ca.crt`) |
| Cilium policies | standard NetworkPolicy (k3s enforces it) |
| MySQL replica + ProxySQL | single MySQL |
| Grafana Cloud | `make status` / `make logs` |

## First-time setup (core laptop, Ubuntu 22.04/24.04)

1. **Network.** Give the core laptop a fixed IP (DHCP reservation or a static IP). Use Ethernet if you can. Ask IT whether devices on campus Wi-Fi can reach each other; some networks block that.
2. **Config.**
   ```bash
   cp config.env.example config.env
   ```
   Set `CTF_HOST` to the laptop's LAN IP, and set `EVENT_NAME` and `FLAG_PREFIX`.
3. **Install and deploy.**
   ```bash
   make install        # k3s, registry, firewall, disables sleep (log out/in once after)
   make secrets        # master secret, DB passwords, HTTPS cert
   make build          # CTFd+plugin, controller, static-files images
   make deploy         # everything into the cluster
   make ctfd-setup TEAMS_CSV=data/teams.csv
   ```
   The admin password and token are saved in `.secrets/ctfd-admin.env`.
4. **Optional: second laptop.** `make install` wrote `.secrets/join-agent.sh`. Copy it to the other Ubuntu laptop and run `sudo bash join-agent.sh` there. Challenge pods then prefer that laptop.

Players open `https://CTF_HOST`. The certificate comes from our own CA, so
browsers show a warning once. Players who want to avoid it can install
`.secrets/ca.crt`.

**Before the first real round, do the dry run in [docs/DRY-RUN.md](docs/DRY-RUN.md)**
(in a VM: start with [docs/VM-SETUP.md](docs/VM-SETUP.md)).
The platform hasn't run on real hardware yet.

## Every round

```bash
make new-round                 # backup, then clear challenges/solves (accounts kept)
# put the round's challenges in place:
make push DIR=challenges/src/web-login     # build + push each service image
#   + challenges/manifests/<id>.yaml for each (see schema.yaml)
#   + challenges/static/<id>/ for download challenges
make challenges                # publish to CTFd + create per-team instances
make status
```

In the CTFd admin panel, set the start and end time (Config → Start/End
Time). Challenges stay hidden until the start time.

After the round: `make stop-instances`, then `make backup`, then copy `backups/` somewhere safe.

Laptop got a new IP? `make reip` (see docs/RUNBOOK.md).

New team mid-semester? Add it to the CSV and run `make teams CSV=data/teams.csv && make challenges`.

## Writing challenges

- **Service challenges** read their flag from the `FLAG` env var. `challenges/templates/` has working Dockerfiles for web, pwn, misc, crypto, forensics, osint and reversing. Each instance is limited to 128 MB by default (`memory_limit` up to 512Mi) and can make no outbound network connections.
- **Cookies:** challenges run on the same host as CTFd (only the port differs), and browsers don't separate cookies by port. Never name a challenge cookie `session` or `ctfd_session`; the templates use `chal_session`.
- **Download challenges:** put files containing `{{FLAG}}` in `challenges/static/<id>/`, or write a `build.sh`.
- **Remove the three `example-*` manifests** before your first real round. Use them for a dry run first.

All fields are documented in `challenges/manifests/schema.yaml`.

## Files

| Path | What |
|---|---|
| `config.env` | per-installation settings (not committed) |
| `.secrets/` | master secret, DB passwords, admin token, CA, join script (back it up, never commit it) |
| `controller/` | FastAPI controller, lifetime daemon, bulk_spawn, flag import |
| `ctfd-plugin/` | CTFd plugin (Start/Stop UI, team flags, downloads, branding) |
| `ctfd/Dockerfile` | CTFd image with the plugin baked in |
| `k8s/` | manifests (`${VAR}` placeholders filled from config.env) |
| `scripts/` | everything `make` runs |
| `docs/RUNBOOK.md` | event-day checklist and troubleshooting |
| `docs/DRY-RUN.md` | first test on real hardware, step by step |
| `docs/VM-SETUP.md` | running it in a VMware Fusion / Ubuntu Server VM |
| `docs/DESIGN-DASHBOARD.md` | proposal: admin dashboard + one-click VM install |

## License

MIT, see [LICENSE](LICENSE).
