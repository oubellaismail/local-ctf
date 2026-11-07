# local-ctf

A self-hosted CTF platform for club contests, running on one or two staff
laptops (or VMs) over the campus network. Players get CTFd, and each team
gets its own copy of every challenge, started and stopped from the challenge
page.

It is the [InfoDays CTF](#compared-with-the-infodays-cloud-version) platform
(DigitalOcean + Kubernetes), rebuilt on [k3s](https://k3s.io) so it needs no
cloud account and no internet during a round.

**Current release: v1.0.0.** Passed a full dry run (install, play, cheating
and isolation checks, backup/restore, reboot, IP change, second node). See
the [results log](docs/DRY-RUN.md#results-log).

## Features

- **CTFd 3.8.9** with team accounts, imported from a CSV
- **One instance per team per challenge**: players click *Start Instance*, get
  their own URL or `nc` address, and it stops by itself after 60 minutes
- **Per-team flags** (`PREFIX{hmac(team, challenge)}`): a flag copied from another
  team is rejected and appears in the admin **Cheaters** log
- **Per-team file downloads** for forensics/crypto/reversing challenges
- **Isolation**: challenge containers can't reach CTFd, the registry, other
  teams' instances or the internet
- **Round lifecycle**: one command each to start a new round, back up and restore
- **Survives reboots** and DHCP address changes (`make reip`)
- **Optional second laptop** for challenge instances, with automatic failover

Sized for about **20 teams × 4 running instances** on a 16 GB Ubuntu laptop.
The platform itself uses about 3 GB, and a typical instance 1–50 MB.

## How it fits together

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

## Requirements

- Ubuntu Server or Desktop **24.04** (22.04 should work), amd64, 16 GB RAM, 40 GB+ free disk
- A **fixed IP** on the campus network (DHCP reservation), Ethernet if possible
- A network where player devices can reach each other (check with IT; dry-run step 0.2)
- Internet during installation only

No spare laptop? It runs in a VM: see [docs/VM-SETUP.md](docs/VM-SETUP.md).

## Install

```bash
git clone https://github.com/oubellaismail/local-ctf.git && cd local-ctf
cp config.env.example config.env    # set CTF_HOST, EVENT_NAME, FLAG_PREFIX
make install        # k3s, registry, firewall, no sleep (log out and in once after)
make secrets        # master secret, DB passwords, HTTPS certificate
make build          # CTFd + plugin, controller, static-files images
make deploy         # everything into the cluster
make ctfd-setup TEAMS_CSV=data/teams.csv
```

The admin password is saved in `.secrets/ctfd-admin.env`. Players open
`https://CTF_HOST` and accept the certificate warning once, or install
`.secrets/ca.crt` to avoid it.

**First time on new hardware?** Follow the [dry run](docs/DRY-RUN.md) with
the example challenges, then do the cleanup at its end before a real round.

**Second laptop:** `make join-script`, copy `.secrets/join-agent.sh` to it,
then run `sudo bash join-agent.sh` there.

## Every round

```bash
make new-round                 # backup, then clear challenges and solves (teams kept)
make push DIR=challenges/src/web-login     # build + push each service image
#   + challenges/manifests/<id>.yaml for each (see schema.yaml)
#   + challenges/static/<id>/ for download challenges
make challenges                # publish to CTFd + create per-team instances
make status
```

Set the start and end time in CTFd (Admin → Config → Start/End Time).

After the round: `make stop-instances`, then `make backup`, then copy `backups/`
to another machine.

## Commands

| Command | What it does |
|---|---|
| `make status` | nodes, pods, running instances, endpoints, disk |
| `make logs` | follow CTFd and controller logs |
| `make teams CSV=…` | add or refresh teams (then `make challenges`) |
| `make stop-instances` | stop every running instance |
| `make backup NAME=…` / `make restore BACKUP=…` | database + secrets |
| `make new-round` / `MODE=full` | reset for the next round (`full` also wipes accounts) |
| `make reip` | the laptop's IP changed: move the whole platform to it |
| `make build-ctfd` / `make build-controller` | rebuild after code changes |
| `make test` | controller unit tests |

Run `make help` for the full list. Troubleshooting is in [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Writing challenges

- **Service challenges** read their flag from the `FLAG` env var.
  `challenges/templates/` has working Dockerfiles for web, pwn, misc, crypto,
  forensics, osint and reversing. Each instance gets 128 MB by default
  (`memory_limit` up to 512Mi) and no outbound network.
- **Download challenges:** files containing `{{FLAG}}` in
  `challenges/static/<id>/`, or a `build.sh` that writes them.
- **Cookies:** challenges share CTFd's host (only the port differs) and
  browsers don't separate cookies by port. Never name a challenge cookie
  `session` or `ctfd_session`; the templates use `chal_session`.
- **Keep real challenges out of this public repo** until their round is over.

All manifest fields are documented in
[`challenges/manifests/schema.yaml`](challenges/manifests/schema.yaml).

## Configuration

`config.env` (copied from [`config.env.example`](config.env.example)):

| Setting | Default | |
|---|---|---|
| `CTF_HOST` | none (required) | the core laptop's LAN IP |
| `EVENT_NAME` | `Club CTF` | shown in CTFd |
| `FLAG_PREFIX` | `CTF` | flags look like `CTF{…}` |
| `MAX_ACTIVE_PER_TEAM` | `4` | running instances per team |
| `INSTANCE_LIFETIME_MINUTES` | `60` | auto-stop |
| `BRANDING` | `false` | `true` = InfoDays dark theme and GOAL! animation |
| `CTFD_VERSION` | `3.8.9` | tested version; change only between rounds |

## Compared with the InfoDays cloud version

| InfoDays (DigitalOcean) | local-ctf |
|---|---|
| DOKS + Terraform | k3s, `scripts/install-server.sh` |
| DOCR | `registry:2` on the core laptop, password-protected |
| do-block-storage | k3s `local-path` |
| DO load balancer + ingress-nginx | k3s Traefik on hostPort 80/443 |
| Let's Encrypt + DO DNS | own CA, certificate for `CTF_HOST` |
| Cilium policies | standard NetworkPolicy (enforced by k3s) |
| MySQL replica + ProxySQL | single MySQL |
| Grafana Cloud | `make status` / `make logs` |

## Repository layout

| Path | What |
|---|---|
| `controller/` | FastAPI controller, lifetime daemon, bulk_spawn, flag import |
| `ctfd-plugin/` | CTFd plugin: Start/Stop UI, team flags, downloads, cheating log |
| `ctfd/Dockerfile` | CTFd image with the plugin built in |
| `k8s/` | manifests (`${VAR}` placeholders filled from config.env) |
| `scripts/` | everything `make` runs |
| `challenges/` | manifest schema, examples, templates |
| `docs/` | [dry run](docs/DRY-RUN.md), [runbook](docs/RUNBOOK.md), [VM setup](docs/VM-SETUP.md), [dashboard design](docs/DESIGN-DASHBOARD.md) |

Never commit `config.env`, `.secrets/`, `data/*.csv` or `backups/`
(`.gitignore` already excludes them).

## Roadmap

Next: an **admin dashboard** inside CTFd (upload a challenge image, deploy it
to every team, manage instances and rounds from the browser) and a
**ready-made VM image** that sets itself up on first boot. See
[docs/DESIGN-DASHBOARD.md](docs/DESIGN-DASHBOARD.md).

## License

MIT, see [LICENSE](LICENSE).
