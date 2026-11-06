# Event-day runbook

## The day before

- [ ] Core laptop is plugged in, on Ethernet if possible, and has its reserved IP (`ip -4 addr`)
- [ ] `make status`: all nodes Ready, all ctfd pods Running, CTFd HTTP 200
- [ ] `make challenges` has run for this round's manifests, with no errors
- [ ] From a **phone or another laptop on campus Wi-Fi** (not the core laptop):
  - [ ] open `https://CTF_HOST`, log in with a test team, start one http and one tcp instance, and check both answer
  - [ ] submit the test team's flag: accepted. Submit another team's flag: rejected
  - [ ] download a static challenge and check its flag works
- [ ] CTFd admin → Config → Start/End time set
- [ ] `make backup NAME=pre-round`

Get a test team's flag without solving:
```bash
set -a; . .secrets/platform.env; . config.env; set +a
python3 controller/flag.py --team <ctfd team id> --challenge <manifest id>
```

## During the round

| Symptom | Check | Fix |
|---|---|---|
| CTFd slow / 502 | `make status`, `kubectl -n ctfd top pods` | `kubectl -n ctfd rollout restart deploy/ctfd` |
| "Max N active instances" | team hit the cap | they stop one, or raise `MAX_ACTIVE_PER_TEAM` and `make deploy` |
| Instance stuck "starting" | `kubectl -n challenges get pods \| grep team-<id>-` then `kubectl -n challenges describe pod <pod>` | `ErrImagePull` → image not pushed or wrong name in the manifest. `Pending` → node full, check the quota: `kubectl -n challenges describe quota` |
| Instance URL doesn't load for players | try from the core laptop: `curl http://CTF_HOST:<port>` | works locally but not for players → campus Wi-Fi blocks device-to-device traffic, or ufw. Check `sudo ufw status` |
| Correct flag rejected | `make logs`, look for the submission | wrong team id/challenge id? Compare with `controller/flag.py` |
| Laptop went to sleep / rebooted | `sudo systemctl status k3s` | k3s and the registry restart on their own. Wait 2 min, then `make status` |
| Agent laptop died | `kubectl get nodes` shows NotReady | after ~5 min its pods get rescheduled on the core laptop. To speed up: `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data --force` |

Watch live: `make logs` (CTFd + controller), `watch -n5 make status`.

## After the round

```bash
make stop-instances
make backup NAME=round-N
```

Then copy `backups/` (it includes `.secrets/`) to a USB stick or another laptop.

## Disaster recovery

The core laptop dies mid-semester:

1. Install Ubuntu on another laptop and copy the repo there.
2. Put back `config.env` (with the new IP) and `.secrets/` from the latest backup's `secrets/` folder.
3. Delete `.secrets/tls.*` if the IP changed. Delete `.secrets/join-agent.sh`.
4. Run `make install && make secrets && make build && make deploy`.
5. Run `make restore BACKUP=backups/<latest>`.
6. Re-push the round's challenge images (`make push ...`), then `make challenges`.

Keep `CTF_MASTER_SECRET` (in `.secrets/platform.env`). Every flag is derived from it.

## Useful commands

```bash
kubectl get nodes -L ctf/role                       # who's in the cluster
kubectl -n challenges get deploy -l managed-by=ctf-controller | awk 'NR==1 || $2=="1/1"'   # running instances
kubectl -n ctfd logs deploy/controller-api -f        # start/stop requests
kubectl -n ctfd exec -it mysql-0 -- sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" ctfd'
docker exec ctf-registry ls /var/lib/registry/docker/registry/v2/repositories/challenges   # pushed images
sudo journalctl -u k3s -f                            # k3s itself
```
