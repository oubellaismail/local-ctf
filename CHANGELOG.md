# Changelog

## v1.0.0 (2026-11-07)

First release. The InfoDays cloud CTF platform, rebuilt to run on one or two
Ubuntu laptops or VMs with k3s.

### Platform
- k3s single-node install with an optional challenge node (`make join-script`)
- CTFd 3.8.9 with the controller plugin built in, 2 replicas behind Traefik
  on ports 80/443, HTTPS with a local CA
- Password-protected image registry on the core laptop
- Per-team challenge instances (start/stop from CTFd, 60-minute auto-stop,
  up to 4 running per team)
- Per-team HMAC flags with a cheating log, per-team file downloads
- Network policies: challenges can't reach CTFd, the registry, other teams
  or the internet
- `make` commands for install, rounds, backup/restore, status and IP changes

### Tested
Full dry run in VMware Fusion VMs (Ubuntu 24.04): install, play from a
phone, flag sharing, isolation on both nodes, auto-stop, capacity,
new round, backup/restore, reboot, IP change, second-node failover.
See [docs/DRY-RUN.md](docs/DRY-RUN.md#results-log).

### Fixed during the dry run
- 502 errors during CTFd restarts
- Players logged out after opening a web challenge (cookie name clash;
  CTFd now uses a Secure `ctfd_session` cookie)
- ~8 s delay on every Start and failed bulk_spawn, caused by the DHCP search
  domain (fixed with `ndots:2`)
- Whole platform down after a systemd-networkd restart (it deleted the pod
  and Docker routes)
- Home page 404, late Start button, `kubectl` needing sudo
- InfoDays theme now optional (`BRANDING=false` by default)

### Known limitations
- Challenges share CTFd's IP (different ports), so challenge authors must not
  use the cookie names `session` or `ctfd_session`
- Instances on a failed challenge laptop take about 5 minutes to move
- No web UI for adding challenges yet (planned: admin dashboard)
