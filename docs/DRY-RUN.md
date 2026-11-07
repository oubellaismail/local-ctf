# First dry run on a staff laptop

Goal: prove the whole platform works before any real round. It has passed
static checks only (unit tests, manifest schemas, cert generation). This run
is its first time on real hardware.

**You need:** one Ubuntu 22.04/24.04 laptop (16 GB) on the campus network,
plugged in, and a **second device on campus Wi-Fi** (phone or laptop) to act
as a player. Budget about 2 hours. Write down every failure with the step
number.

Each step lists what you should see, and what to check first if you don't.
The **⚠ untested** marks show the spots most likely to need a fix.

---

## Phase 0: Network (10 min)

| # | Do | Expect |
|---|---|---|
| 0.1 | `ip -4 addr` on the laptop, note the Wi-Fi/Ethernet IP | e.g. `10.20.x.y` |
| 0.2 | From the player device: `ping <laptop-ip>`, or open `http://<laptop-ip>` | ping replies (connection refused on :80 is fine for now) |
| 0.3 | Ask IT for a DHCP reservation for this laptop's MAC | the IP won't change between rounds |

**If 0.2 fails:** the campus Wi-Fi blocks devices from talking to each other.
Nothing else will work until that's solved. Try Ethernet for the laptop, a
different SSID, or ask IT. Stop here.

## Phase 1: Install (20 min)

```bash
git clone/copy local-ctf to the laptop && cd local-ctf
cp config.env.example config.env    # CTF_HOST=<ip from 0.1>, FLAG_PREFIX=TEST
make install
```

| # | Expect | If not |
|---|---|---|
| 1.1 | ends with "Core laptop ready." | read the last ✘ line; `sudo journalctl -u k3s -n 50` |
| 1.2 | log out, log back in, then `docker ps` works without sudo | `groups` should list `docker` |
| 1.3 | `kubectl get nodes -L ctf/role` shows 1 node, Ready, role `core` | `export KUBECONFIG=~/.kube/config` |
| 1.4 | `kubectl -n kube-system get pods` shows traefik Running | ⚠ untested: `kubectl -n kube-system logs deploy/traefik`. If the HelmChartConfig was rejected: `kubectl -n kube-system get helmchartconfig traefik -o yaml` and `kubectl -n kube-system logs job/helm-install-traefik` |
| 1.5 | `curl -s -o /dev/null -w '%{http_code}\n' http://<CTF_HOST>/` prints **404** (Traefik answers, no routes yet). Note: `ss` does **not** show ports 80/443, because hostPort is an iptables rule, not a listening socket | ⚠ untested: Traefik hostPort. `000` = nothing answers: `kubectl -n kube-system get pod -l app.kubernetes.io/name=traefik -o yaml \| grep -B2 -A2 hostPort` should list 80 and 443 |
| 1.6 | `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:5000/v2/` prints **401** | 401 = registry up with auth. If it's 000: `docker logs ctf-registry` |

## Phase 2: Platform (20 min)

```bash
make secrets
make build
make deploy
```

| # | Expect | If not |
|---|---|---|
| 2.1 | `make build` pushes ctfd, controller, static-files | `unauthorized` → `docker logout localhost:5000` and rerun |
| 2.2 | `make deploy` ends with "Platform up" | find the stuck pod: `kubectl -n ctfd get pods` |
| 2.3 | no pod in `ImagePullBackOff` | ⚠ untested: registry mirror. `kubectl -n ctfd describe pod <pod>`. Check `/etc/rancher/k3s/registries.yaml`, then `sudo systemctl restart k3s` |
| 2.4 | ctfd pods Running, 0 restarts | ⚠ untested: **user 1001**. `kubectl -n ctfd logs deploy/ctfd`. On `Permission denied` under /opt/CTFd or /var/uploads, run `docker run --rm --entrypoint id ctfd/ctfd:latest` and put the uid it prints into `runAsUser`/`fsGroup` in k8s/ctfd/ctfd.yaml and the `chown` in ctfd/Dockerfile |
| 2.5 | CTFd logs show no migration errors / duplicate-table errors | if they do: `kubectl -n ctfd scale deploy/ctfd --replicas=1`, wait, then scale back to 2 |
| 2.6 | on the laptop, `curl -I http://<CTF_HOST>` returns `301` to https | the redirect middleware isn't applied: `kubectl -n ctfd get middleware` |
| 2.7 | `curl --cacert .secrets/ca.crt https://<CTF_HOST>/healthcheck` returns 200 | cert served is Traefik's default, not ours: check the TLSStore with `kubectl -n ctfd get tlsstore default -o yaml` |

## Phase 3: CTFd setup + teams (10 min)

Make `data/teams.csv` with **3 test teams × 2 players** (copy
`data/teams-template.csv`). Use fake emails.

```bash
make ctfd-setup TEAMS_CSV=data/teams.csv
```

| # | Expect | If not |
|---|---|---|
| 3.1 | "Setup complete.", `.secrets/ctfd-admin.env` exists | if the token fails with 401/302: `kubectl -n ctfd exec redis-0 -- redis-cli FLUSHALL`, then rerun |
| 3.2 | **player device:** open `https://<CTF_HOST>`, accept the cert warning, see the branded login page | page loads without styles → Google Fonts are blocked by the network. That's fine, just cosmetic |
| 3.3 | log in as admin. Admin → Teams lists the 3 teams | |
| 3.4 | Admin → Config → **General**: User Mode = Teams. Open `https://<CTF_HOST>/rules` (also in the top menu): flags shown as `TEST{...}` | edit it under Admin → Pages → rules |
| 3.5 | Controller key stored (CTFd has no page for it): `kubectl -n ctfd exec mysql-0 -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -N ctfd -e "SELECT CHAR_LENGTH(value) FROM config WHERE \`key\`=\"X_CONTROLLER_KEY\""' 2>/dev/null` prints a number (key length, not the key) | empty: `make ctfd-setup` again (it re-applies the key) |

## Phase 4: Challenges (20 min)

```bash
make push DIR=challenges/templates/web  NAME=example-web
make push DIR=challenges/templates/misc NAME=example-misc
make challenges
```

| # | Expect | If not |
|---|---|---|
| 4.1 | all 6 steps ✔. bulk_spawn shows `Done: 6 created ... 0 errors` (3 teams × 2 service challenges) | full log in `build/logs/bulk_spawn.log` |
| 4.2 | import_flags shows 3 created | `build/logs/import_flags.log` |
| 4.3 | `kubectl -n challenges get deploy` shows 6 deployments at 0/0 | |
| 4.4 | CTFd shows 3 challenges with correct categories and points | |

## Phase 5: Play as a team (30 min). Do all of it from the player device.

Log in as a player of **team A**.

| # | Do | Expect | If not |
|---|---|---|---|
| 5.1 | open "Example: Login Bypass" → Start | status goes starting → running in under 30 s, URL `http://<CTF_HOST>:3xxxx` | stuck starting: `kubectl -n challenges get pods`, then `describe` it. `Pending` with an "exceeded quota" event means a quota problem |
| 5.2 | open that URL | login page loads | ⚠ untested: works on the laptop (`curl`) but not on the phone → ufw: `sudo ufw status` should allow 30000:32767 |
| 5.3 | solve it (SQLi on the login) and submit the flag | **Correct** | rejected: compare with `python3 controller/flag.py --team <id> --challenge example-web-login` (after sourcing the env, see RUNBOOK) |
| 5.4 | Netcat Puzzle → Start, then `nc <CTF_HOST> <port>` | puzzle prompt | |
| 5.5 | Welcome → Download | zip whose readme contains a `TEST{...}` flag. Submitting it is Correct | 404: check `kubectl -n ctfd logs deploy/static-files` and that `build/static/example-welcome/` has one zip per team |
| 5.6 | start 2 more instances (if you only have 2 service challenges, temporarily set `MAX_ACTIVE_PER_TEAM=1` + `make deploy`, then start a second one) | "Max N active instances reached" | |
| 5.7 | Stop an instance | status returns to stopped. `kubectl -n challenges get deploy` shows it back at 0 | |

**Cheating and isolation checks:**

| # | Do | Expect |
|---|---|---|
| 5.8 | log in as **team B** and submit **team A's** web flag | **Incorrect**. Admin side logs it as a cheating attempt |
| 5.9 | Start team A's nc-puzzle instance, then test by **IP** (names don't resolve inside Alpine challenge images, which proves nothing): `S=$(kubectl -n ctfd get svc ctfd-service -o jsonpath='{.spec.clusterIP}'); kubectl -n challenges exec deploy/team-<A>-example-nc-puzzle -- wget -qO- -T3 http://$S/healthcheck` | `Connection refused` or `download timed out` (both = blocked by policy). ⚠ If it returns `ok`, policies aren't enforced: check `kubectl get netpol -A` and that k3s wasn't started with `--disable-network-policy` |
| 5.10 | same `exec`, targets `http://<CTF_HOST>:5000/v2/` (registry) and `http://185.125.190.20/` (internet) | refused / timed out for both |
| 5.11 | as admin: `kubectl -n ctfd logs deploy/ctfd \| tail` while the phone browses | the logged client IPs are the **phone's IP**, not 10.42.x.x. ⚠ untested: if they show 10.42.x.x, CTFd's per-IP rate limit would treat every player as one client |

## Phase 6: Timeout + load (15 min)

| # | Do | Expect |
|---|---|---|
| 6.1 | set `INSTANCE_LIFETIME_MINUTES=2` → `make deploy`, then start an instance | within ~3 min it stops by itself. `kubectl -n ctfd logs deploy/controller-daemon` shows "Auto-suspended" |
| 6.2 | set it back to 60 → `make deploy` | |
| 6.3 | start as many instances as you can across the 3 teams, then `make status` | memory % under 70% on the node. Note the number: it's your real per-laptop capacity |

## Phase 7: Round lifecycle (15 min)

| # | Do | Expect |
|---|---|---|
| 7.1 | `make backup NAME=dryrun` | `backups/<date>_dryrun/` with `ctfd.sql.gz` + `secrets/` |
| 7.2 | `make new-round` (type `reset`) | solves and challenges gone in CTFd, the 3 teams still there, `kubectl -n challenges get deploy` empty |
| 7.3 | `make challenges` again | challenges back, team A's earlier solve is gone |
| 7.4 | `make restore BACKUP=backups/<date>_dryrun` | team A's solve from 5.3 is back on the scoreboard |
| 7.5 | `sudo reboot`, wait **3–4 min** (CTFd answers 503 until its health check passes), then `make status` | everything Running again with no manual action. CTFd reachable from the phone |

## Optional Phase 8: Second laptop (20 min)

| # | Do | Expect |
|---|---|---|
| 8.1 | copy `.secrets/join-agent.sh` to laptop B, then `sudo bash join-agent.sh` | "Joined." |
| 8.2 | `kubectl get nodes -L ctf/role` | 2 nodes, B with role `challenges` |
| 8.3 | `make new-round && make challenges`, then start an instance | `kubectl -n challenges get pods -o wide` shows it on laptop B. It's reachable at `CTF_HOST:<port>` (traffic goes through laptop A) |
| 8.4 | unplug laptop B's network for 6 min | its instances reschedule onto A. CTFd stays up |

---

## Pass criteria

Ready for a real round when **0.2, 2.4, 5.2, 5.3, 5.8, 5.9, 7.4, 7.5** pass.
Everything else can be fixed between rounds.

After the dry run:

```bash
rm challenges/manifests/example-*.yaml
rm -r challenges/static/example-welcome
make new-round MODE=full
```

Set `FLAG_PREFIX` to its real value, then `make deploy && make ctfd-setup TEAMS_CSV=data/teams.csv`.

---

## Results log

### Run 1: VMware Fusion VM (Ubuntu 24.04, 10 GB, NAT), 2026-11-06/07

Passed: 0.2, 1.x, 2.x, 3.x, 4.x, 5.1–5.11, 6.1, 6.3, 7.1–7.5. **All pass
criteria met.** 0.2/5.2/5.11 confirmed from a phone after switching the VM
to bridged Wi-Fi and running `make reip` (172.16.130.155 → 192.168.1.116).
Phase 8 passed with a second VM (`ctf-agent-1`, bridged): join, image pull
from the core's registry, instances scheduled on the agent and reachable via
the core IP, network policy enforced on the agent, instances moved to the
core ~5 min after the agent disconnected and back after it returned.
`make reip` used twice (NAT → bridged, then a DHCP change).

Bugs found and fixed during the run:
- `kubectl` needed sudo (KUBECONFIG not set for the user)
- 502s during every CTFd rolling restart (no preStop delay)
- `/` returned 404 (setup never created CTFd's index page)
- InfoDays theme forced on; now `BRANDING=false` by default
- bulk_spawn DNS failure and ~8 s delay on every Start/status call: the
  DHCP search domain (`localdomain`) went to a DNS server that never
  answers. Fixed with `ndots:2` on controller and CTFd pods
- Start button appeared late (CTFd re-renders the challenge modal)
- Players logged out after visiting a web challenge: cookie name clash
  (`session`). CTFd now uses a Secure `ctfd_session` cookie
- Whole platform down after a systemd-networkd restart (deleted pod and
  Docker routes). Installer now sets `ManageForeignRoutes=no`

Capacity measured: platform ≈ 3 GB; example instances 1–25 MB each.
